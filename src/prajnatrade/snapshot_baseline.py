from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Mapping
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from prajnatrade.backtest import (
    Strategy,
    _rebalance_at_open,
    _validate_weights,
    equal_weight_buy_and_hold,
)
from prajnatrade.data import ETF_SYMBOLS
from prajnatrade.snapshot import halt_covers_session, read_discovery

#: Asia/Shanghai is the trading-time anchor for both baselines and Shadow Live.
CN_TZ = timezone(timedelta(hours=8))
_CN_TZ = CN_TZ  # backward-compatible private alias
_TARGET_WEIGHTS = {symbol: 1.0 / len(ETF_SYMBOLS) for symbol in ETF_SYMBOLS}
_EPSILON = 1e-12
NEXT_OPEN_RULES = "next-open-v1"
RETRY_INITIAL_RULES = "retry-initial-v1"
#: Fill an order effective before the open at the sealed, evidenced actual daily
#: open, including a halted-at-open session whose bar carries that open (ADR 0002).
ACTUAL_OPEN_RULES = "actual-open-v1"
EXECUTION_RULE_VERSIONS = (NEXT_OPEN_RULES, RETRY_INITIAL_RULES, ACTUAL_OPEN_RULES)
#: The sealed execution basis echoed by every fill and by a round receipt.
EXECUTION_PRICE_BASIS = "unadjusted_open"


def run_discovery_strategy(
    data: dict[str, list[dict]],
    snapshot_id: str,
    partition_content_hash: str,
    strategy: Strategy,
    *,
    data_kind: str = "synthetic",
    signal_window_sessions: int,
    initial_cash: float = 100_000.0,
    transaction_cost_bps: float = 5.0,
    slippage_bps: float = 0.0,
    execution_rule_version: str = NEXT_OPEN_RULES,
    history_requirements: list[dict] | None = None,
    scoring_start: str | None = None,
    prior_history: tuple[list[str], list[dict]] | None = None,
) -> dict[str, Any]:
    """Run a strategy with point-in-time signals and formal Discovery accounting."""
    return run_partition_strategy(
        data,
        snapshot_id,
        partition_content_hash,
        strategy,
        partition_name="discovery",
        data_kind=data_kind,
        signal_window_sessions=signal_window_sessions,
        prior_history=prior_history,
        initial_cash=initial_cash,
        transaction_cost_bps=transaction_cost_bps,
        slippage_bps=slippage_bps,
        execution_rule_version=execution_rule_version,
        history_requirements=history_requirements,
        scoring_start=scoring_start,
    )


def first_open_history(
    decision_date: str,
    prior_history: tuple[list[str], list[dict]] | None,
    history_requirements: list[dict],
) -> dict[str, Any]:
    """Assess required inputs for an open without opening its partition."""
    sessions, derived = _authorized_history([decision_date], prior_history)
    readiness, _, _ = _history_at_open(sessions, derived, decision_date, history_requirements)
    return readiness


def first_ready_open(
    data: dict[str, list[dict]],
    *,
    history_requirements: list[dict],
    prior_history: tuple[list[str], list[dict]] | None = None,
    scoring_start: str | None = None,
) -> str | None:
    """Find the first open whose completed history satisfies the requirements."""
    sessions = sorted({row["date"] for row in data["calendar"]})
    history_sessions, history_derived = _authorized_history(sessions, prior_history)
    history_derived.update(_index_rows(data["derived"], "derived"))
    for day in sessions:
        if scoring_start is None or day >= scoring_start:
            readiness, _, _ = _history_at_open(
                history_sessions, history_derived, day, history_requirements,
            )
            if readiness["ready"]:
                return day
    return None


def run_partition_strategy(
    data: dict[str, list[dict]],
    snapshot_id: str,
    partition_content_hash: str,
    strategy: Strategy,
    *,
    partition_name: str,
    data_kind: str = "synthetic",
    signal_window_sessions: int,
    prior_history: tuple[list[str], list[dict]] | None = None,
    initial_cash: float = 100_000.0,
    transaction_cost_bps: float = 5.0,
    slippage_bps: float = 0.0,
    execution_rule_version: str = NEXT_OPEN_RULES,
    history_requirements: list[dict] | None = None,
    scoring_start: str | None = None,
) -> dict[str, Any]:
    """Run a strategy over one sealed partition with formal accounting.

    With declared history requirements, signals can use completed observations
    from authorized earlier sessions at each scoring open. Accounting starts from
    initial cash and empty holdings on the scoring start. The strategy receives
    the union of selected observations and per-indicator dates in
    ``state["history_windows"]``. Legacy runs keep their partition-close signals
    and next-open execution.
    """
    _validate_parameters(initial_cash, transaction_cost_bps, slippage_bps)
    if execution_rule_version not in EXECUTION_RULE_VERSIONS:
        raise ValueError(f"Unknown execution rules version: {execution_rule_version!r}.")
    if signal_window_sessions <= 0:
        raise ValueError("signal_window_sessions must be positive.")
    if not isinstance(partition_name, str) or not partition_name.strip():
        raise ValueError("partition_name must be a nonempty string.")
    sessions = sorted({row["date"] for row in data["calendar"]})
    if not sessions:
        raise ValueError(f"The {partition_name} partition has no trading sessions.")

    raw_bars = _index_rows(data["raw_bars"], "raw_bars")
    derived = _index_rows(data["derived"], "derived")
    if history_requirements is None:
        if scoring_start is not None:
            raise ValueError("scoring_start requires history_requirements.")
        prior_sessions, prior_rows = prior_history if prior_history is not None else ([], [])
        history_sessions = sorted({
            *(day for day in prior_sessions if day < sessions[0]),
            *sessions,
        })
        history_derived = _index_rows(list(prior_rows), "derived")
    else:
        history_sessions, history_derived = _authorized_history(sessions, prior_history)
        if scoring_start is not None:
            if scoring_start not in sessions:
                raise ValueError("scoring_start must be a trading session in the partition.")
            sessions = sessions[sessions.index(scoring_start):]
    history_derived.update(derived)
    positions = {day: index for index, day in enumerate(history_sessions)}

    actions = _index_actions(data["corporate_actions"])
    shares = {symbol: 0.0 for symbol in ETF_SYMBOLS}
    cash = float(initial_cash)
    receivables: list[dict[str, Any]] = []
    unresolved_actions: dict[str, list[dict[str, str]]] = {
        symbol: [] for symbol in ETF_SYMBOLS
    }
    signals: list[dict[str, Any]] = []
    signal_gaps: list[dict[str, Any]] = []
    first_scored_open = None
    first_ready_date = None
    history_gaps: list[dict[str, Any]] = []
    ready_cash_sessions: list[str] = []
    fills: list[dict[str, Any]] = []
    order_attempts: list[dict[str, Any]] = []
    positive_attempts: list[dict[str, Any]] = []
    valuation_gaps: list[dict[str, str]] = []
    equity_curve: list[dict[str, Any]] = []
    invested_sessions = 0
    fees_paid = slippage_paid = 0.0
    fee_rate = transaction_cost_bps / 10_000.0
    slippage_rate = slippage_bps / 10_000.0
    pending: tuple[dict[str, Any], dict[str, float]] | None = None

    for day_index, day in enumerate(sessions):
        ready = False
        if history_requirements is not None:
            readiness, windows, observation_values = _history_at_open(
                history_sessions, history_derived, day, history_requirements,
            )
            if day_index == 0:
                first_scored_open = readiness
            ready = readiness["ready"]
            if ready:
                if first_ready_date is None:
                    first_ready_date = day
                state = {
                    "cash": cash,
                    "shares": dict(shares),
                    "equity": equity_curve[-1]["nav"] if equity_curve else cash,
                    "history_windows": {
                        indicator: list(days) for indicator, days in windows.items()
                    },
                }
                observation_days = sorted(observation_values)
                history_rows = [
                    {"date": observation_day, "symbol": symbol, "close": value}
                    for observation_day in observation_days
                    for symbol, value in observation_values[observation_day].items()
                ]
                history = pd.DataFrame(history_rows, columns=["date", "symbol", "close"])
                weights = strategy(history, state)
                if weights is not None:
                    normalized = _validate_weights(weights)
                    signal = {
                        "date": day,
                        "decision_date": day,
                        "execution_date": day,
                        "target_weights": normalized,
                        "total_return_values": (
                            observation_values[observation_days[-1]] if observation_days else {}
                        ),
                        "window": observation_days,
                        "history_windows": windows,
                    }
                    signals.append(signal)
                    pending = (signal, normalized)
            else:
                history_gaps.append(readiness)
                missing = {
                    gap["date"]: gap
                    for requirement in readiness["requirements"]
                    if requirement["available_observations"] < requirement["required_observations"]
                    for gap in requirement["missing_sessions"]
                }
                signal_gaps.append({
                    "date": day,
                    "reason": "required_total_return_window_unavailable",
                    "missing_sessions": [missing[gap_day] for gap_day in sorted(missing)],
                    "requirements": readiness["requirements"],
                })

        _apply_actions(
            day, actions.get(day, []), shares, cash, receivables, unresolved_actions
        )
        for item in receivables[:]:
            if item["pay_date"] <= day:
                cash += item["amount"]
                receivables.remove(item)

        positive_order = pending is not None and _has_position(pending[1])
        if pending is not None:
            signal, weights = pending
            pending = None
            open_cutoff = datetime.combine(
                datetime.fromisoformat(day).date(), time(9, 30), tzinfo=_CN_TZ
            )
            open_prices = {
                symbol: (
                    float(row["open"])
                    if _is_executable_open(
                        row, open_cutoff, data_kind=data_kind,
                        execution_rule_version=execution_rule_version,
                    )
                    else None
                )
                for symbol in ETF_SYMBOLS
                if (row := raw_bars.get((day, symbol))) is not None
            }
            if execution_rule_version == ACTUAL_OPEN_RULES:
                # Every required symbol matches on its own: one missing open no
                # longer aborts the basket, and no sell funds a buy unless it
                # actually filled.
                attempt, day_fills, cash, shares, costs = _match_actual_open_day(
                    day, signal, weights, open_prices, raw_bars, cash, shares,
                    fee_rate, slippage_rate,
                )
                fills.extend(day_fills)
                fees_paid += costs["fees"]
                slippage_paid += costs["slippage"]
            else:
                missing_opens = sorted(
                    symbol
                    for symbol in ETF_SYMBOLS
                    if (
                        weights.get(symbol, 0.0) > _EPSILON or shares[symbol] > _EPSILON
                    ) and open_prices.get(symbol) is None
                )
                attempt = {
                    "signal_date": signal["date"],
                    "date": day,
                    "attempts": 1,
                    "status": "not_filled" if missing_opens else "filled",
                }
                if missing_opens:
                    attempt["reason"] = "missing_or_unconfirmed_open"
                    attempt["missing_symbols"] = missing_opens
                    if execution_rule_version == RETRY_INITIAL_RULES and not invested_sessions:
                        pending = (signal, weights)
                else:
                    open_equity = cash + math.fsum(
                        shares[symbol] * float(open_prices.get(symbol) or 0.0)
                        for symbol in ETF_SYMBOLS
                    )
                    target_shares = {
                        symbol: (
                            open_equity * weights.get(symbol, 0.0) / float(open_prices[symbol])
                            if open_prices.get(symbol) is not None
                            else 0.0
                        )
                        for symbol in ETF_SYMBOLS
                    }
                    day_fills, cash, shares, costs = _rebalance_at_open(
                        pd.Timestamp(day),
                        open_prices,
                        target_shares,
                        cash,
                        shares,
                        fee_rate,
                        slippage_rate,
                    )
                    for fill in day_fills:
                        fill["raw_open"] = open_prices[fill["symbol"]]
                        fill["price_basis"] = EXECUTION_PRICE_BASIS
                    fills.extend(day_fills)
                    fees_paid += costs["fees"]
                    slippage_paid += costs["slippage"]
            order_attempts.append(attempt)
            if positive_order:
                positive_attempts.append(attempt)

        invested = _has_position(shares)
        invested_sessions += invested
        if ready and not invested and not positive_order:
            ready_cash_sessions.append(day)
        nav_point, gaps = _valuation(
            day, raw_bars, shares, cash, receivables, unresolved_actions, data_kind=data_kind,
        )
        equity_curve.append(nav_point)
        valuation_gaps.extend(gaps)

        if history_requirements is not None or day_index + 1 >= len(sessions):
            continue
        cutoff = datetime.combine(
            datetime.fromisoformat(sessions[day_index + 1]).date(),
            time(9, 30),
            tzinfo=_CN_TZ,
        )
        window_start = positions[day] - signal_window_sessions + 1
        if window_start < 0:
            continue
        window_days = history_sessions[window_start : positions[day] + 1]
        missing: dict[str, set[str]] = {}
        history_rows: list[dict[str, Any]] = []
        latest_values: dict[str, float] = {}
        for history_day in window_days:
            day_values = {}
            for symbol in ETF_SYMBOLS:
                row = history_derived.get((history_day, symbol))
                if row is None or row.get("value") is None or not _available_by(row, cutoff):
                    missing.setdefault(history_day, set()).add(symbol)
                else:
                    day_values[symbol] = float(row["value"])
            if len(day_values) == len(ETF_SYMBOLS):
                history_rows.extend(
                    {"date": history_day, "symbol": symbol, "close": value}
                    for symbol, value in day_values.items()
                )
                latest_values = day_values
        if missing:
            signal_gaps.append({
                "date": day,
                "missing_sessions": [
                    {"date": gap_day, "missing_symbols": sorted(symbols)}
                    for gap_day, symbols in sorted(missing.items())
                ],
                "reason": "required_total_return_window_unavailable",
            })
            continue

        state = {
            "cash": cash,
            "shares": dict(shares),
            "equity": nav_point["nav"],
        }
        weights = strategy(pd.DataFrame(history_rows), state)
        if weights is not None:
            normalized = _validate_weights(weights)
            signal = {
                "date": day,
                "execution_date": sessions[day_index + 1],
                "target_weights": normalized,
                "total_return_values": latest_values,
                "window": list(window_days),
            }
            signals.append(signal)
            pending = (signal, normalized)

    metrics = _metrics(
        sessions,
        equity_curve,
        initial_cash=initial_cash,
        fees_paid=fees_paid,
        slippage_paid=slippage_paid,
        fills=fills,
        order_attempts=order_attempts,
        signal_gaps=signal_gaps,
        valuation_gaps=valuation_gaps,
        execution_rule_version=execution_rule_version,
    )
    return {
        "snapshot_id": snapshot_id,
        "partition_name": partition_name,
        "partition_content_hash": partition_content_hash,
        "start_date": sessions[0],
        "end_date": sessions[-1],
        "initial_cash": initial_cash,
        "ending_equity": equity_curve[-1]["nav"] if equity_curve else None,
        "signals": signals,
        "signal_gaps": signal_gaps,
        "order_attempts": order_attempts,
        "fills": fills,
        "equity_curve": equity_curve,
        "position_evidence": _position_evidence(
            signals, fills, invested_sessions, ready_cash=bool(ready_cash_sessions),
            positive_attempts=positive_attempts,
            execution_rule_version=execution_rule_version,
        ),
        **({
            "history_readiness": {
                "first_scored_open": first_scored_open,
                "first_ready_date": first_ready_date,
                "gaps": history_gaps,
                "ready_cash_sessions": ready_cash_sessions,
            },
        } if history_requirements is not None else {}),
        "metrics": metrics,
        "gaps": {
            "raw_bars": _data_gaps(sessions, raw_bars, "raw_bars", data_kind=data_kind),
            "derived": _data_gaps(
                sessions, derived, "derived", raw_bars=raw_bars, data_kind=data_kind,
            ),
            "valuation": valuation_gaps,
        },
    }


def _authorized_history(
    sessions: list[str], prior_history: tuple[list[str], list[dict]] | None,
) -> tuple[list[str], dict[tuple[str, str], dict]]:
    if prior_history is None or not sessions:
        return sessions, {}
    prior_sessions, prior_rows = prior_history
    allowed = {day for day in prior_sessions if day < sessions[0]}
    return sorted(allowed.union(sessions)), _index_rows(
        [row for row in prior_rows if row["date"] in allowed], "derived",
    )


def _history_at_open(
    sessions: list[str],
    derived: dict[tuple[str, str], dict],
    decision_date: str,
    history_requirements: list[dict],
) -> tuple[dict[str, Any], dict[str, list[str]], dict[str, dict[str, float]]]:
    """Resolve aligned ETF observations as of the open's information cutoff."""
    valid_mode = "valid_observations"
    consecutive_mode = "consecutive_sessions"
    longest = {valid_mode: 0, consecutive_mode: 0}
    for requirement in history_requirements:
        count, mode = requirement["observations"], requirement["mode"]
        if mode not in longest or type(count) is not int or count < 1:
            raise ValueError("Each history requirement needs a valid mode and positive observations.")
        longest[mode] = max(longest[mode], count)

    cutoff = datetime.combine(
        datetime.fromisoformat(decision_date).date(), time(9, 30), tzinfo=_CN_TZ,
    )
    panel: list[tuple[str, dict[str, float], list[str]]] = []
    valid_indices: list[int] = []
    for index in range(bisect_left(sessions, decision_date) - 1, -1, -1):
        if len(panel) >= longest[consecutive_mode] and len(valid_indices) >= longest[valid_mode]:
            break
        day = sessions[index]
        values = {}
        missing = []
        for symbol in ETF_SYMBOLS:
            row = derived.get((day, symbol))
            if row is None or not _valid_price(row.get("value")) or not _available_by(row, cutoff):
                missing.append(symbol)
            else:
                values[symbol] = float(row["value"])
        if not missing:
            valid_indices.append(len(panel))
        panel.append((day, values, sorted(missing)))

    results = []
    windows = {}
    selected = {}
    for requirement in history_requirements:
        required = requirement["observations"]
        mode = requirement["mode"]
        if mode == consecutive_mode:
            end = required
        elif len(valid_indices) >= required:
            end = valid_indices[required - 1] + 1
        else:
            end = len(panel)
        valid = []
        missing_sessions = []
        for day, values, missing in reversed(panel[:end]):
            if missing:
                missing_sessions.append({"date": day, "missing_symbols": missing})
            else:
                valid.append(day)
                selected[day] = values
        windows[requirement["id"]] = valid
        results.append({
            "id": requirement["id"],
            "mode": mode,
            "required_observations": required,
            "available_observations": len(valid),
            "missing_sessions": missing_sessions,
        })
    ready = all(
        item["available_observations"] == item["required_observations"] for item in results
    )
    return {"decision_date": decision_date, "ready": ready, "requirements": results}, windows, selected


def _has_position(weights_or_shares: Mapping[str, float]) -> bool:
    return any(weights_or_shares[symbol] > _EPSILON for symbol in ETF_SYMBOLS)


def _position_evidence(
    signals: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    invested_sessions: int,
    *,
    ready_cash: bool = False,
    positive_attempts: list[dict[str, Any]] | None = None,
    execution_rule_version: str = NEXT_OPEN_RULES,
) -> dict[str, Any]:
    positive = [signal for signal in signals if _has_position(signal["target_weights"])]
    if invested_sessions:
        state = "initial_position_established"
    elif positive:
        state = "initial_position_not_established"
    elif signals or ready_cash:
        state = "intentional_cash"
    else:
        state = "no_position_signal"
    evidence = {
        "state": state,
        "positive_target_signals": len(positive),
        "cash_target_signals": len(signals) - len(positive),
        "invested_sessions": invested_sessions,
        "first_position_signal_date": positive[0]["date"] if positive else None,
        "first_fill_date": fills[0]["date"] if fills else None,
    }
    if execution_rule_version == ACTUAL_OPEN_RULES:
        latest = positive_attempts[-1] if positive_attempts else None
        evidence["basket_build_state"] = (
            "none"
            if latest is None
            else "completed" if latest["basket_complete"] else "partial"
        )
        evidence["unfilled_basket_signals"] = sum(
            1 for attempt in positive_attempts or [] if attempt["unfilled"]
        )
    return evidence


def _metrics(
    sessions: list[str],
    equity_curve: list[dict[str, Any]],
    *,
    initial_cash: float,
    fees_paid: float,
    slippage_paid: float,
    fills: list[dict[str, Any]],
    order_attempts: list[dict[str, Any]],
    signal_gaps: list[dict[str, Any]],
    valuation_gaps: list[dict[str, Any]],
    execution_rule_version: str = NEXT_OPEN_RULES,
) -> dict[str, Any]:
    """Partition metrics; unavailable values are ``None`` rather than fabricated."""
    valued = [float(point["nav"]) for point in equity_curve if point["nav"] is not None]
    ending_equity = equity_curve[-1]["nav"] if equity_curve else None
    total_return = ending_equity / initial_cash - 1.0 if ending_equity is not None else None
    max_drawdown = None
    sharpe = None
    if len(valued) >= 2:
        returns = np.asarray(
            [right / left - 1.0 for left, right in zip(valued, valued[1:], strict=False)],
            dtype=float,
        )
        std = float(np.std(returns, ddof=1)) if len(returns) > 1 else 0.0
        if std > 0:
            sharpe = float(np.mean(returns) / std * math.sqrt(252.0))
    if valued:
        drawdowns = np.asarray(valued) / np.maximum.accumulate(np.asarray(valued)) - 1.0
        max_drawdown = float(np.min(drawdowns))
    metrics = {
        "sessions": len(sessions),
        "valued_sessions": len(valued),
        "total_return": total_return,
        "max_drawdown": max_drawdown,
        "sharpe": sharpe,
        "fees_paid": fees_paid,
        "slippage_paid": slippage_paid,
        "ending_equity": ending_equity,
        "fills": len(fills),
        "orders_filled": sum(
            1 for attempt in order_attempts if attempt.get("status") == "filled"
        ),
        "orders_unfilled": sum(
            1 for attempt in order_attempts if attempt.get("status") != "filled"
        ),
        "signal_gaps": len(signal_gaps),
        "valuation_gaps": len(valuation_gaps),
    }
    if execution_rule_version == ACTUAL_OPEN_RULES:
        # A partial attempt still counts as unfilled above; this additive count
        # records the baskets that matched some symbols but not all of them.
        metrics["orders_partially_filled"] = sum(
            1 for attempt in order_attempts if attempt.get("status") == "partially_filled"
        )
    return metrics


def run_discovery_equal_weight(
    data: dict[str, list[dict]],
    snapshot_id: str,
    partition_content_hash: str,
    *,
    data_kind: str = "synthetic",
    initial_cash: float,
    transaction_cost_bps: float,
    slippage_bps: float,
    execution_rule_version: str = NEXT_OPEN_RULES,
    history_requirements: list[dict] | None = None,
    scoring_start: str | None = None,
    prior_history: tuple[list[str], list[dict]] | None = None,
) -> dict[str, Any]:
    """Run the fixed equal-weight buy-and-hold comparator."""
    submitted = False

    def buy_and_hold(history: pd.DataFrame, state: Mapping[str, Any]):
        nonlocal submitted
        if submitted:
            return None
        weights = equal_weight_buy_and_hold(history, state)
        submitted = weights is not None
        return weights

    return run_discovery_strategy(
        data,
        snapshot_id,
        partition_content_hash,
        buy_and_hold,
        data_kind=data_kind,
        signal_window_sessions=1,
        initial_cash=initial_cash,
        transaction_cost_bps=transaction_cost_bps,
        slippage_bps=slippage_bps,
        execution_rule_version=execution_rule_version,
        history_requirements=history_requirements,
        scoring_start=scoring_start,
        prior_history=prior_history,
    )


def run_discovery_baseline(
    store: Path,
    snapshot_id: str,
    *,
    initial_cash: float = 100_000.0,
    transaction_cost_bps: float = 5.0,
    slippage_bps: float = 0.0,
) -> dict[str, Any]:
    """Run the baseline using only the sealed Discovery partition."""
    _validate_parameters(initial_cash, transaction_cost_bps, slippage_bps)
    receipt = read_discovery(store, snapshot_id, "discovery")
    data = receipt["data"]
    data_kind = receipt["data_kind"]
    sessions = sorted({row["date"] for row in data["calendar"]})
    if not sessions:
        raise ValueError("The Discovery partition has no trading sessions.")

    raw_bars = _index_rows(data["raw_bars"], "raw_bars")
    derived = _index_rows(data["derived"], "derived")
    actions = _index_actions(data["corporate_actions"])
    signal, signal_gaps = _first_signal(sessions, derived)

    shares = {symbol: 0.0 for symbol in ETF_SYMBOLS}
    cash = float(initial_cash)
    receivables: list[dict[str, Any]] = []
    unresolved_actions: dict[str, list[dict[str, str]]] = {
        symbol: [] for symbol in ETF_SYMBOLS
    }
    fills: list[dict[str, Any]] = []
    order_attempts: list[dict[str, Any]] = []
    valuation_gaps: list[dict[str, str]] = []
    equity_curve: list[dict[str, Any]] = []
    fees_paid = 0.0
    slippage_paid = 0.0
    signal_attempted = False

    fee_rate = transaction_cost_bps / 10_000.0
    slippage_rate = slippage_bps / 10_000.0

    for day in sessions:
        day_actions = actions.get(day, [])
        _apply_actions(
            day, day_actions, shares, cash, receivables, unresolved_actions
        )
        for item in receivables[:]:
            if item["pay_date"] <= day:
                cash += item["amount"]
                receivables.remove(item)

        if signal is not None and not signal_attempted and day == signal["execution_date"]:
            signal_attempted = True
            open_cutoff = datetime.combine(
                datetime.fromisoformat(day).date(), time(9, 30), tzinfo=_CN_TZ
            )
            missing_opens = [
                symbol for symbol in ETF_SYMBOLS
                if not _is_executable_open(
                    raw_bars.get((day, symbol)), open_cutoff, data_kind=data_kind
                )
            ]
            attempt = {
                "signal_date": signal["date"],
                "date": day,
                "attempts": 1,
                "status": "not_filled" if missing_opens else "filled",
            }
            if missing_opens:
                attempt["reason"] = "missing_or_unconfirmed_open"
                attempt["missing_symbols"] = missing_opens
            else:
                day_fills, cash, shares, fees, slippage = _buy_target(
                    day, raw_bars, cash, fee_rate, slippage_rate
                )
                fills.extend(day_fills)
                fees_paid += fees
                slippage_paid += slippage
            order_attempts.append(attempt)

        nav_point, gaps = _valuation(
            day, raw_bars, shares, cash, receivables, unresolved_actions,
            data_kind=data_kind,
        )
        equity_curve.append(nav_point)
        valuation_gaps.extend(gaps)

    final_nav = equity_curve[-1]["nav"]
    metrics = {
        "total_return": (
            final_nav / initial_cash - 1.0 if final_nav is not None else None
        ),
        "fees_paid": fees_paid,
        "slippage_paid": slippage_paid,
    }
    return {
        "schema_version": 1,
        "data_kind": data_kind,
        "snapshot_id": receipt["snapshot_id"],
        "partition": "discovery",
        "partition_content_hash": receipt["content_hash"],
        "price_roles": {
            "signal": {
                "source": "derived.value",
                "basis": "total_return",
                "dividend_reinvestment": "unadjusted_ex_dividend_close",
            },
            "execution": {
                "source": "raw_bars.open",
                "basis": "unadjusted",
                "costs_frozen_at_run": {
                    "transaction_cost_bps": transaction_cost_bps,
                    "slippage_bps": slippage_bps,
                },
            },
            "valuation": {
                "source": "raw_bars.close",
                "basis": "unadjusted",
                "includes": ["cash", "dividend_receivables"],
            },
        },
        "initial_cash": initial_cash,
        "signals": [signal] if signal is not None else [],
        "signal_gaps": signal_gaps,
        "order_attempts": order_attempts,
        "fills": fills,
        "equity_curve": equity_curve,
        "ending_equity": final_nav,
        "metrics": metrics,
        "gaps": {
            "raw_bars": _data_gaps(
                sessions, raw_bars, "raw_bars", data_kind=data_kind
            ),
            "derived": _data_gaps(
                sessions, derived, "derived",
                raw_bars=raw_bars, data_kind=data_kind,
            ),
            "valuation": valuation_gaps,
        },
    }


def _validate_parameters(
    initial_cash: float, transaction_cost_bps: float, slippage_bps: float
) -> None:
    for name, value in (
        ("initial_cash", initial_cash),
        ("transaction_cost_bps", transaction_cost_bps),
        ("slippage_bps", slippage_bps),
    ):
        if isinstance(value, bool) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be finite and nonnegative.")
    if initial_cash <= 0:
        raise ValueError("initial_cash must be greater than zero.")
    if transaction_cost_bps >= 10_000 or slippage_bps >= 10_000:
        raise ValueError("transaction_cost_bps and slippage_bps must be less than 10,000.")


def _index_rows(rows: list[dict], kind: str) -> dict[tuple[str, str], dict]:
    indexed = {}
    for row in rows:
        key = (row["date"], row["symbol"])
        if key in indexed:
            raise ValueError(f"Duplicate {kind} row for {key[0]} {key[1]}.")
        indexed[key] = row
    return indexed


def _index_actions(rows: list[dict]) -> dict[str, list[dict]]:
    indexed: dict[str, list[dict]] = {}
    for row in rows:
        indexed.setdefault(row["date"], []).append(row)
    return indexed


def _first_signal(
    sessions: list[str], derived: dict[tuple[str, str], dict]
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    gaps = []
    for index, day in enumerate(sessions[:-1]):
        values = {}
        missing = []
        for symbol in ETF_SYMBOLS:
            row = derived.get((day, symbol))
            if row is None or row.get("value") is None:
                missing.append(symbol)
            else:
                values[symbol] = row["value"]
        if missing:
            gaps.append({"date": day, "missing_symbols": sorted(missing)})
            continue

        available_by = datetime.combine(
            datetime.fromisoformat(sessions[index + 1]).date(),
            time(9, 30),
            tzinfo=_CN_TZ,
        )
        late = [
            symbol for symbol in ETF_SYMBOLS
            if not _available_by(derived[(day, symbol)], available_by)
        ]
        if late:
            gaps.append({
                "date": day,
                "missing_symbols": sorted(late),
                "reason": "total_return_value_not_available_before_next_open",
            })
            continue

        return {
            "date": day,
            "execution_date": sessions[index + 1],
            "target_weights": dict(_TARGET_WEIGHTS),
            "total_return_values": values,
            "window": [day],
        }, gaps
    return None, gaps


def available_by(row: dict, cutoff: datetime) -> bool:
    """Public point-in-time availability check with evidence (trusted seam)."""
    return _available_by(row, cutoff)


def _available_by(row: dict, cutoff: datetime) -> bool:
    value = row.get("available_at")
    evidence = row.get("availability_evidence")
    if not isinstance(value, str) or not isinstance(evidence, str) or not evidence.strip():
        return False
    try:
        available_at = datetime.fromisoformat(value)
    except ValueError:
        return False
    return available_at.tzinfo is not None and available_at <= cutoff


def _instant_by(value: object, cutoff: datetime) -> bool:
    if not isinstance(value, str):
        return False
    try:
        available_at = datetime.fromisoformat(value)
    except ValueError:
        return False
    return available_at.tzinfo is not None and available_at <= cutoff


def _is_executable_open(
    row: dict | None,
    cutoff: datetime,
    *,
    data_kind: str = "synthetic",
    execution_rule_version: str = NEXT_OPEN_RULES,
) -> bool:
    """Whether one raw bar confirms an open the execution rule may fill at.

    Legacy rules execute only a normal trading session whose open is known by
    the 09:30 cutoff.  ``actual-open-v1`` also executes a halted-at-open session
    whose sealed bar carries the actual daily open and its price source: that
    daily open is only knowable after the close, and it is a matching base price
    for an order effective before the open, never a signal input (ADR 0002).
    """
    if row is None or not _valid_price(row.get("open")):
        return False
    status = row.get("trading_status")
    if execution_rule_version == ACTUAL_OPEN_RULES and status == "partial_halt":
        return isinstance(row.get("open_source"), Mapping)
    if status != "trading" or not _valid_price(row.get("volume")):
        return False
    if data_kind == "real":
        return _instant_by(row.get("open_available_at"), cutoff)
    return _available_by(row, cutoff)


def _match_actual_open_day(
    day: str,
    signal: dict[str, Any],
    weights: Mapping[str, float],
    open_prices: Mapping[str, float | None],
    raw_bars: Mapping[tuple[str, str], dict],
    cash: float,
    shares: dict[str, float],
    fee_rate: float,
    slippage_rate: float,
) -> tuple[dict[str, Any], list[dict[str, Any]], float, dict[str, float], dict[str, float]]:
    """Match every required symbol at its own executable open, sells first.

    A required symbol without an executable open is recorded unfilled and keeps
    its cash or shares. Holdings without an executable open are left out of the
    open equity that sizes the targets, so no stale price sizes an order.
    """
    required = [
        symbol
        for symbol in ETF_SYMBOLS
        if weights.get(symbol, 0.0) > _EPSILON or shares[symbol] > _EPSILON
    ]
    unfilled = [
        {"symbol": symbol, "reason": _unfilled_reason(raw_bars.get((day, symbol)))}
        for symbol in required
        if open_prices.get(symbol) is None
    ]
    open_equity = cash + math.fsum(
        shares[symbol] * float(open_prices[symbol])
        for symbol in ETF_SYMBOLS
        if open_prices.get(symbol) is not None
    )
    target_shares = {
        symbol: (
            open_equity * weights.get(symbol, 0.0) / float(open_prices[symbol])
            if open_prices.get(symbol) is not None
            else 0.0
        )
        for symbol in ETF_SYMBOLS
    }
    day_fills, cash, shares, costs = _rebalance_at_open(
        pd.Timestamp(day),
        open_prices,
        target_shares,
        cash,
        shares,
        fee_rate,
        slippage_rate,
    )
    for fill in day_fills:
        fill["raw_open"] = open_prices[fill["symbol"]]
        fill["price_basis"] = EXECUTION_PRICE_BASIS
        open_source = raw_bars[(day, fill["symbol"])].get("open_source")
        if isinstance(open_source, Mapping):
            fill["open_evidence"] = {
                "source": open_source["source"],
                "evidence_id": open_source["evidence_id"],
            }
    traded = {fill["symbol"] for fill in day_fills}
    attempt = {
        "signal_date": signal["date"],
        "date": day,
        "attempts": 1,
        "status": (
            "filled" if not unfilled
            else "partially_filled" if day_fills
            else "not_filled"
        ),
        "filled_symbols": [
            symbol for symbol in ETF_SYMBOLS if symbol in traded
        ],
        "unfilled": unfilled,
        "basket_complete": not unfilled,
    }
    return attempt, day_fills, cash, shares, costs


def _unfilled_reason(row: dict | None) -> str:
    """Classify a required symbol the sealed row shows cannot match today."""
    if row is None:
        return "missing_bar"
    status = row.get("trading_status")
    if status == "unknown":
        return "unknown_trading_status"
    if status == "suspended":
        return "confirmed_no_trade"
    # A sealed source conflict is never resolved into a confirmed no-trade or a
    # fill: the required open is missing.
    if "open_conflict" in row:
        return "missing_required_open"
    if status == "partial_halt" and halt_covers_session(row.get("halt"), row.get("date")):
        return "confirmed_no_trade"
    return "missing_required_open"


def _close_available(row: dict, cutoff: datetime, data_kind: str) -> bool:
    if data_kind == "real":
        return _instant_by(row.get("close_available_at"), cutoff)
    return _available_by(row, cutoff)


def _valid_price(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value > 0
    )


def _buy_target(
    day: str,
    raw_bars: dict[tuple[str, str], dict],
    cash: float,
    fee_rate: float,
    slippage_rate: float,
) -> tuple[list[dict[str, Any]], float, dict[str, float], float, float]:
    denominator = (1.0 + slippage_rate) * (1.0 + fee_rate)
    invested_notional = cash / denominator
    quantities: dict[str, float] = {}
    for symbol in ETF_SYMBOLS:
        open_price = float(raw_bars[(day, symbol)]["open"])
        quantities[symbol] = (
            invested_notional * _TARGET_WEIGHTS[symbol] / open_price
        )

    day_fills = []
    fees_paid = slippage_paid = total_debit = 0.0
    for symbol in ETF_SYMBOLS:
        raw_open = float(raw_bars[(day, symbol)]["open"])
        quantity = quantities[symbol]
        execution_price = raw_open * (1.0 + slippage_rate)
        notional = quantity * execution_price
        fee = notional * fee_rate
        slippage = quantity * (execution_price - raw_open)
        total_debit += notional + fee
        fees_paid += fee
        slippage_paid += slippage
        day_fills.append({
            "date": day,
            "symbol": symbol,
            "quantity": quantity,
            "raw_open": raw_open,
            "price": execution_price,
            "price_basis": "unadjusted_open",
            "notional": notional,
            "fee": fee,
            "slippage": slippage,
        })
    cash -= total_debit
    if abs(cash) <= _EPSILON * max(1.0, invested_notional):
        cash = 0.0
    return day_fills, cash, quantities, fees_paid, slippage_paid


def _apply_actions(
    day: str,
    actions: list[dict],
    shares: dict[str, float],
    cash: float,
    receivables: list[dict[str, Any]],
    unresolved: dict[str, list[dict[str, str]]],
) -> float:
    cutoff = datetime.combine(
        datetime.fromisoformat(day).date(), time(9, 30), tzinfo=_CN_TZ
    )
    for action in actions:
        symbol = action["symbol"]
        quantity = shares[symbol]
        if quantity <= _EPSILON:
            continue
        if (
            not _available_by(action, cutoff)
            or not isinstance(action.get("terms_evidence"), str)
            or not action["terms_evidence"].strip()
        ):
            unresolved[symbol].append({
                "date": day,
                "reason": "corporate_action_terms_or_availability_unknown",
            })
            continue
        if action["type"] == "cash_dividend":
            amount = action.get("cash_per_share")
            pay_date = action.get("pay_date")
            if (
                not _valid_price(amount)
                or not isinstance(pay_date, str)
                or not pay_date
            ):
                unresolved[symbol].append({
                    "date": day,
                    "reason": "cash_dividend_terms_unknown",
                })
                continue
            if pay_date < day:
                unresolved[symbol].append({
                    "date": day,
                    "reason": "cash_dividend_pay_date_precedes_ex_date",
                })
                continue
            receivables.append({
                "symbol": symbol,
                "ex_date": day,
                "pay_date": pay_date,
                "quantity": quantity,
                "cash_per_share": amount,
                "amount": quantity * amount,
            })
        elif action["type"] == "share_adjustment":
            ratio = action.get("ratio")
            if not _valid_price(ratio):
                unresolved[symbol].append({
                    "date": day,
                    "reason": "share_adjustment_ratio_unknown",
                })
                continue
            shares[symbol] *= ratio
    return cash


def _valuation(
    day: str,
    raw_bars: dict[tuple[str, str], dict],
    shares: dict[str, float],
    cash: float,
    receivables: list[dict[str, Any]],
    unresolved: dict[str, list[dict[str, str]]],
    *,
    data_kind: str = "synthetic",
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    holdings = {}
    gaps = []
    market_value = 0.0
    valid = True
    close_cutoff = datetime.combine(
        datetime.fromisoformat(day).date(), time(23, 59, 59), tzinfo=_CN_TZ
    )
    for symbol in ETF_SYMBOLS:
        quantity = shares[symbol]
        row = raw_bars.get((day, symbol))
        status = row.get("trading_status") if row is not None else None
        markable = status == "trading" or (
            data_kind == "real" and status == "partial_halt"
        )
        close = row.get("close") if row is not None else None
        if quantity > _EPSILON and (
            row is None
            or not markable
            or not _valid_price(close)
            or not _close_available(row, close_cutoff, data_kind)
        ):
            valid = False
            gap = {
                "date": day,
                "symbol": symbol,
                "reason": "missing_or_unconfirmed_unadjusted_close",
            }
            if data_kind == "real" and status in ("partial_halt", "suspended", "unknown"):
                gap["trading_status"] = status
                gap["status_reason"] = {
                    "partial_halt": "partial_halt_no_confirmed_close",
                    "suspended": "suspended_no_confirmed_close",
                    "unknown": "unknown_trading_status",
                }[status]
                if row is not None and isinstance(row.get("halt"), dict):
                    gap["halt"] = row["halt"]
            gaps.append(gap)
            close = None
        if unresolved[symbol]:
            valid = False
            for action_gap in unresolved[symbol]:
                gaps.append({"symbol": symbol, **action_gap})
        value = quantity * float(close) if quantity > _EPSILON and _valid_price(close) else 0.0
        market_value += value
        holdings[symbol] = {
            "quantity": quantity,
            "close": close,
            "market_value": value if close is not None or quantity <= _EPSILON else None,
        }

    receivable_total = math.fsum(item["amount"] for item in receivables)
    nav = cash + market_value + receivable_total if valid else None
    return {
        "date": day,
        "nav": nav,
        "cash": cash,
        "market_value": market_value if valid else None,
        "dividend_receivables": receivable_total if valid else None,
        "receivables": [dict(item) for item in receivables],
        "holdings": holdings,
    }, gaps


def _data_gaps(
    sessions: list[str],
    rows: dict[tuple[str, str], dict],
    kind: str,
    *,
    raw_bars: dict[tuple[str, str], dict] | None = None,
    data_kind: str = "synthetic",
) -> list[dict[str, Any]]:
    required = ("open", "close") if kind == "raw_bars" else ("value",)
    gaps = []
    for day in sessions:
        for symbol in sorted(ETF_SYMBOLS):
            row = rows.get((day, symbol))
            missing = [
                field for field in required
                if row is None or row.get(field) is None
            ]
            if kind == "raw_bars" and row is not None and row.get("trading_status") == "unknown":
                missing.append("trading_status")
            if not missing:
                continue
            gap = {"date": day, "symbol": symbol, "missing_fields": missing}
            if data_kind == "real":
                _describe_real_gap(gap, row, kind, raw_bars)
            gaps.append(gap)
    return gaps


def _describe_real_gap(
    gap: dict[str, Any],
    row: dict | None,
    kind: str,
    raw_bars: dict[tuple[str, str], dict] | None,
) -> None:
    status_row = row if kind == "raw_bars" else (raw_bars or {}).get(
        (gap["date"], gap["symbol"])
    )
    if status_row is None:
        return
    status = status_row.get("trading_status")
    if status not in ("partial_halt", "suspended", "unknown"):
        return
    gap["trading_status"] = status
    gap["reason"] = (
        {
            "partial_halt": "partial_halt_no_executable_open",
            "suspended": "suspended_session",
            "unknown": "unknown_trading_status",
        }[status]
        if kind == "raw_bars"
        else {
            "partial_halt": "partial_halt_no_confirmed_close",
            "suspended": "suspended_no_confirmed_close",
            "unknown": "unknown_trading_status",
        }[status]
    )
    if isinstance(status_row.get("halt"), dict):
        gap["halt"] = status_row["halt"]
