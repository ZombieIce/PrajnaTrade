"""Point-in-time Discovery accounting shared by the candidate and benchmark.

``next-open-v1`` (the default) and ``retry-initial-v1`` keep the legacy
all-or-nothing basket attempt at the next session's open.  ``actual-open-v1``
matches every required symbol separately at the sealed actual unadjusted daily
open under a retrospective rule: no open's publication time gates an order that
was effective before the open.  A symbol without an executable open is recorded
unfilled with its sealed-row reason.  Sells match before buys in frozen symbol
order and buys are funded from initial cash plus realized sell net.  A held
symbol without a trustworthy open makes that day's total equity basis
unknowable, so equity-dependent orders are unfilled with
``equity_basis_unknown`` (ADR 0002). Explicit exits still sell in full, and a
single 100% target can buy with the cash available after those exits. An entry
basket with no holdings still fills its priced symbols per symbol. The rule
never retries automatically.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time
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
from prajnatrade.snapshot import read_discovery
from prajnatrade.trading_status import (
    ACTUAL_OPEN_RULES as ACTUAL_OPEN_RULES,
)
from prajnatrade.trading_status import (
    CARRY_FORWARD_SUSPENDED_VALUATION as CARRY_FORWARD_SUSPENDED_VALUATION,
)
from prajnatrade.trading_status import (
    CN_TZ as CN_TZ,
)
from prajnatrade.trading_status import (
    EXECUTION_PRICE_BASIS as EXECUTION_PRICE_BASIS,
)
from prajnatrade.trading_status import (
    EXECUTION_RULE_VERSIONS as EXECUTION_RULE_VERSIONS,
)
from prajnatrade.trading_status import (
    NEXT_OPEN_RULES as NEXT_OPEN_RULES,
)
from prajnatrade.trading_status import (
    RETRY_INITIAL_RULES as RETRY_INITIAL_RULES,
)
from prajnatrade.trading_status import (
    STRICT_CLOSE_VALUATION as STRICT_CLOSE_VALUATION,
)
from prajnatrade.trading_status import (
    VALUATION_POLICY_VERSIONS as VALUATION_POLICY_VERSIONS,
)
from prajnatrade.trading_status import (
    confirmed_no_trade_shape,
    halt_conflicts_with_trade,
    halt_conflicts_with_volume,
    halt_covers_session,
)
from prajnatrade.trading_status import (
    valuation_policy_version as valuation_policy_version,
)

_CN_TZ = CN_TZ  # backward-compatible private alias
_TARGET_WEIGHTS = {symbol: 1.0 / len(ETF_SYMBOLS) for symbol in ETF_SYMBOLS}
_EPSILON = 1e-12


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
    valuation_policy_version: str = STRICT_CLOSE_VALUATION,
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
        valuation_policy_version=valuation_policy_version,
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
    valuation_policy_version: str = STRICT_CLOSE_VALUATION,
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
    if valuation_policy_version not in VALUATION_POLICY_VERSIONS:
        raise ValueError(f"Unknown valuation policy version: {valuation_policy_version!r}.")
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
    carry_states: dict[str, _CarryState] = {
        symbol: _CarryState() for symbol in ETF_SYMBOLS
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

        applied_actions_today: dict[str, list[dict[str, Any]]] = {
            symbol: [] for symbol in ETF_SYMBOLS
        }
        _apply_actions(
            day,
            actions.get(day, []),
            shares,
            cash,
            receivables,
            unresolved_actions,
            applied_actions=applied_actions_today,
            enforce_action_order=(
                valuation_policy_version == CARRY_FORWARD_SUSPENDED_VALUATION
            ),
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
                    if is_executable_open(
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
                day_fills = []
                costs = {"notional": 0.0, "fees": 0.0, "slippage": 0.0}
                if missing_opens:
                    attempt["reason"] = "missing_or_unconfirmed_open"
                    attempt["missing_symbols"] = missing_opens
                    if execution_rule_version == RETRY_INITIAL_RULES and not invested_sessions:
                        pending = (signal, weights)
                else:
                    day_fills, cash, shares, costs = _rebalance_at_open(
                        pd.Timestamp(day),
                        open_prices,
                        _target_shares_at_open(weights, open_prices, cash, shares),
                        cash,
                        shares,
                        fee_rate,
                        slippage_rate,
                    )
            # One common tail for both rules: every fill echoes the sealed base
            # price it matched at, and an actual-open fill also echoes the
            # sealed price source that produced it.
            for fill in day_fills:
                fill["raw_open"] = open_prices[fill["symbol"]]
                fill["price_basis"] = EXECUTION_PRICE_BASIS
                if execution_rule_version == ACTUAL_OPEN_RULES:
                    open_source = raw_bars[(day, fill["symbol"])].get("open_source")
                    if isinstance(open_source, Mapping):
                        fill["open_evidence"] = {
                            "source": open_source["source"],
                            "evidence_id": open_source["evidence_id"],
                        }
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
        if valuation_policy_version == CARRY_FORWARD_SUSPENDED_VALUATION:
            nav_point, gaps = _valuation_carry_forward(
                day,
                raw_bars,
                shares,
                cash,
                receivables,
                unresolved_actions,
                carry_states,
                applied_actions_today,
                data_kind=data_kind,
            )
        else:
            nav_point, gaps = _valuation(
                day,
                raw_bars,
                shares,
                cash,
                receivables,
                unresolved_actions,
                data_kind=data_kind,
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
        valuation_policy_version=valuation_policy_version,
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
        **(
            {
                "valuation_summary": _valuation_summary(
                    sessions,
                    equity_curve,
                    metrics,
                    policy_version=valuation_policy_version,
                ),
            }
            if valuation_policy_version == CARRY_FORWARD_SUSPENDED_VALUATION
            else {}
        ),
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
        # ``partial`` means the last positive target started building: at least
        # one fill and at least one unfilled required symbol.  A target that
        # produced no fill at all is ``unfilled``; a complete one is
        # ``completed``; no positive target is ``none``.
        if latest is None:
            state = "none"
        elif latest["basket_complete"]:
            state = "completed"
        elif latest["filled_symbols"]:
            state = "partial"
        else:
            state = "unfilled"
        evidence["basket_build_state"] = state
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
    valuation_policy_version: str = STRICT_CLOSE_VALUATION,
) -> dict[str, Any]:
    """Partition metrics; unavailable values are ``None`` rather than fabricated."""
    ending_equity = equity_curve[-1]["nav"] if equity_curve else None
    total_return = ending_equity / initial_cash - 1.0 if ending_equity is not None else None
    max_drawdown = None
    sharpe = None
    nav_by_date = {point["date"]: point["nav"] for point in equity_curve}
    adjacent_returns = [
        float(nav_by_date[right]) / float(nav_by_date[left]) - 1.0
        for left, right in zip(sessions, sessions[1:], strict=False)
        if nav_by_date.get(left) is not None and nav_by_date.get(right) is not None
    ]
    if len(adjacent_returns) >= 2:
        returns = np.asarray(adjacent_returns, dtype=float)
        std = float(np.std(returns, ddof=1))
        if std > 0:
            sharpe = float(np.mean(returns) / std * math.sqrt(252.0))
    complete_curve = (
        len(equity_curve) == len(sessions)
        and all(point["nav"] is not None for point in equity_curve)
    )
    if complete_curve:
        valued = [initial_cash, *(float(point["nav"]) for point in equity_curve)]
        drawdowns = np.asarray(valued) / np.maximum.accumulate(np.asarray(valued)) - 1.0
        max_drawdown = float(np.min(drawdowns))
    metrics = {
        "sessions": len(sessions),
        "valued_sessions": sum(point["nav"] is not None for point in equity_curve),
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
    if valuation_policy_version == CARRY_FORWARD_SUSPENDED_VALUATION:
        metrics["actual_close_sessions"] = sum(
            point["nav"] is not None and point.get("valuation_basis") == "actual_close"
            for point in equity_curve
        )
        metrics["carried_forward_sessions"] = sum(
            point["nav"] is not None and point.get("valuation_basis") == "carried_forward"
            for point in equity_curve
        )
        metrics["missing_valuation_sessions"] = sum(
            point["nav"] is None for point in equity_curve
        )
    return metrics


def _valuation_summary(
    sessions: list[str],
    equity_curve: list[dict[str, Any]],
    metrics: Mapping[str, Any],
    *,
    policy_version: str,
) -> dict[str, Any]:
    """Summarize actual-close vs carried-forward valuation across scoring sessions."""
    ending_carried: dict[str, Any] = {}
    if equity_curve:
        last_holdings = equity_curve[-1].get("holdings", {})
        for symbol in ETF_SYMBOLS:
            holding = last_holdings.get(symbol, {})
            if holding.get("valuation_basis") == "carried_forward_prior_close":
                ending_carried[symbol] = dict(holding["carry_forward"])
    return {
        "policy_version": policy_version,
        "sessions": len(sessions),
        "valued_sessions": metrics["valued_sessions"],
        "actual_close_sessions": metrics["actual_close_sessions"],
        "carried_forward_sessions": metrics["carried_forward_sessions"],
        "missing_valuation_sessions": metrics["missing_valuation_sessions"],
        "ending_carried_forward": ending_carried,
    }


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
    valuation_policy_version: str = STRICT_CLOSE_VALUATION,
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
        valuation_policy_version=valuation_policy_version,
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
                if not is_executable_open(
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


def is_executable_open(
    row: dict | None,
    cutoff: datetime,
    *,
    data_kind: str = "synthetic",
    execution_rule_version: str = NEXT_OPEN_RULES,
) -> bool:
    """Whether one raw bar confirms an open the execution rule may fill at.

    Legacy rules execute only a normal trading session whose open is known by
    the 09:30 cutoff.  ``actual-open-v1`` instead matches an order that was
    already effective before the open retrospectively, so no publication time
    gates the fill: a ``trading`` row needs a valid open and positive volume,
    and a ``partial_halt`` row also needs the sealed price source and a halt
    window that leaves part of the session open for trading. The daily open is
    a matching base price only. Signal inputs keep their own point-in-time cutoffs
    (ADR 0002).
    """
    if row is None or not _valid_price(row.get("open")):
        return False
    status = row.get("trading_status")
    if execution_rule_version == ACTUAL_OPEN_RULES:
        if not _valid_price(row.get("volume")):
            return False
        if status == "partial_halt":
            return (
                isinstance(row.get("open_source"), Mapping)
                and not halt_covers_session(row.get("halt"), row.get("date"))
            )
        return status == "trading"
    if status != "trading" or not _valid_price(row.get("volume")):
        return False
    if data_kind == "real":
        return _instant_by(row.get("open_available_at"), cutoff)
    return _available_by(row, cutoff)


def _target_shares_at_open(
    weights: Mapping[str, float],
    open_prices: Mapping[str, float | None],
    cash: float,
    shares: Mapping[str, float],
) -> dict[str, float]:
    """Size targets when every held symbol has an executable open."""
    open_equity = cash + math.fsum(
        shares[symbol] * price
        for symbol in ETF_SYMBOLS
        if (price := open_prices.get(symbol)) is not None
    )
    return {
        symbol: (
            open_equity * weights.get(symbol, 0.0) / price
            if (price := open_prices.get(symbol)) is not None
            else 0.0
        )
        for symbol in ETF_SYMBOLS
    }


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
    its cash or shares. A held symbol without one makes the day's total equity
    unknowable, leaving equity-dependent orders unfilled with
    ``equity_basis_unknown``. Explicit exits still sell in full at their usable
    opens, and the cash cap can size the only buy for a single 100% target.
    The unpriced symbols keep their sealed-row reasons, and ``basket_complete``
    stays false while any required symbol is unfilled.
    """
    required = [
        symbol
        for symbol in ETF_SYMBOLS
        if weights.get(symbol, 0.0) > _EPSILON or shares[symbol] > _EPSILON
    ]
    unfilled_reasons = {
        symbol: unfilled_reason(raw_bars.get((day, symbol)))
        for symbol in required
        if open_prices.get(symbol) is None
    }
    cash_capped_buy = None
    if any(shares[symbol] > _EPSILON for symbol in unfilled_reasons):
        exits = {
            symbol
            for symbol in required
            if weights.get(symbol, 0.0) <= _EPSILON
            and shares[symbol] > _EPSILON
            and open_prices.get(symbol) is not None
        }
        target_shares = {
            symbol: 0.0 if symbol in exits else shares[symbol]
            for symbol in ETF_SYMBOLS
        }
        positive_targets = [
            symbol for symbol in required if weights.get(symbol, 0.0) > _EPSILON
        ]
        for symbol in positive_targets:
            unfilled_reasons.setdefault(symbol, "equity_basis_unknown")
        if len(positive_targets) == 1 and weights[positive_targets[0]] == 1.0:
            # At 100%, the target's own market value cancels from its buy size.
            # Cash and other holdings' open value bound demand from below. The
            # realized sell net and buy costs only reduce what cash can buy.
            cash_capped_buy = positive_targets[0]
    else:
        target_shares = _target_shares_at_open(weights, open_prices, cash, shares)

    day_fills, cash, shares, costs = _rebalance_at_open(
        pd.Timestamp(day),
        open_prices,
        target_shares,
        cash,
        shares,
        fee_rate,
        slippage_rate,
        cash_capped_buy=cash_capped_buy,
    )
    traded = {fill["symbol"] for fill in day_fills}
    unfilled = [
        {"symbol": symbol, "reason": unfilled_reasons[symbol]}
        for symbol in required
        if symbol in unfilled_reasons and symbol not in traded
    ]
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


def unfilled_reason(row: dict | None) -> str:
    """Classify a required symbol the sealed row shows cannot match today."""
    if row is None:
        return "missing_bar"
    status = row.get("trading_status")
    if status == "unknown":
        return "unknown_trading_status"
    if status == "suspended":
        return "confirmed_no_trade"
    # A sealed source conflict is never resolved into a confirmed no-trade or a
    # fill: the required open is missing.  An explicit null is not a conflict.
    if row.get("open_conflict") is not None:
        return "missing_required_open"
    if status == "partial_halt":
        halt, day = row.get("halt"), row.get("date")
        if halt_conflicts_with_volume(halt, day, row.get("volume")):
            return "unknown_trading_status"
        if halt_covers_session(halt, day):
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


def _valid_action_sequence(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 1


def _validate_action(day: str, action: dict, cutoff: datetime) -> str | None:
    if (
        not _available_by(action, cutoff)
        or not isinstance(action.get("terms_evidence"), str)
        or not action["terms_evidence"].strip()
    ):
        return "corporate_action_terms_or_availability_unknown"
    action_type = action.get("type")
    if action_type == "cash_dividend":
        amount = action.get("cash_per_share")
        pay_date = action.get("pay_date")
        if (
            not _valid_price(amount)
            or not isinstance(pay_date, str)
            or not pay_date
        ):
            return "cash_dividend_terms_unknown"
        try:
            datetime.fromisoformat(pay_date).date()
        except ValueError:
            return "cash_dividend_terms_unknown"
        if pay_date < day:
            return "cash_dividend_pay_date_precedes_ex_date"
        return None
    if action_type == "share_adjustment":
        if not _valid_price(action.get("ratio")):
            return "share_adjustment_ratio_unknown"
        return None
    return "corporate_action_type_unknown"


def _apply_single_action(
    day: str,
    symbol: str,
    action: dict,
    shares: dict[str, float],
    receivables: list[dict[str, Any]],
    applied_actions: dict[str, list[dict[str, Any]]] | None,
) -> None:
    action_type = action["type"]
    if action_type == "cash_dividend":
        quantity = shares[symbol]
        amount = action["cash_per_share"]
        receivables.append({
            "symbol": symbol,
            "ex_date": day,
            "pay_date": action["pay_date"],
            "quantity": quantity,
            "cash_per_share": amount,
            "amount": quantity * amount,
        })
        if applied_actions is not None:
            applied_actions[symbol].append(dict(action))
    elif action_type == "share_adjustment":
        shares[symbol] *= action["ratio"]
        if applied_actions is not None:
            applied_actions[symbol].append(dict(action))


def _apply_actions(
    day: str,
    actions: list[dict],
    shares: dict[str, float],
    cash: float,
    receivables: list[dict[str, Any]],
    unresolved: dict[str, list[dict[str, str]]],
    *,
    applied_actions: dict[str, list[dict[str, Any]]] | None = None,
    enforce_action_order: bool = False,
) -> float:
    cutoff = datetime.combine(
        datetime.fromisoformat(day).date(), time(9, 30), tzinfo=_CN_TZ
    )
    if not enforce_action_order:
        for action in actions:
            symbol = action["symbol"]
            if shares[symbol] <= _EPSILON:
                continue
            error = _validate_action(day, action, cutoff)
            if error is not None:
                unresolved[symbol].append({"date": day, "reason": error})
                continue
            _apply_single_action(day, symbol, action, shares, receivables, applied_actions)
        return cash

    by_symbol: dict[str, list[dict]] = {}
    for action in actions:
        by_symbol.setdefault(action["symbol"], []).append(action)
    for symbol, sym_actions in by_symbol.items():
        if shares[symbol] <= _EPSILON:
            continue
        if len(sym_actions) > 1:
            sequences = [action.get("sequence") for action in sym_actions]
            if not all(
                "sequence" in action and _valid_action_sequence(seq)
                for action, seq in zip(sym_actions, sequences, strict=True)
            ) or len(set(sequences)) != len(sequences):
                unresolved[symbol].append({
                    "date": day,
                    "reason": "corporate_action_order_ambiguous",
                })
                continue
            sym_actions = sorted(sym_actions, key=lambda item: item["sequence"])
        elif "sequence" in sym_actions[0] and not _valid_action_sequence(
            sym_actions[0]["sequence"]
        ):
            unresolved[symbol].append({
                "date": day,
                "reason": "corporate_action_order_ambiguous",
            })
            continue

        errors = [
            error
            for action in sym_actions
            if (error := _validate_action(day, action, cutoff)) is not None
        ]
        if errors:
            for error in errors:
                unresolved[symbol].append({"date": day, "reason": error})
            continue

        for action in sym_actions:
            _apply_single_action(day, symbol, action, shares, receivables, applied_actions)
    return cash


def _parse_instant(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _close_source_evidence(row: Mapping[str, Any], *, data_kind: str) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "availability_evidence": row.get("availability_evidence"),
        "available_at": (
            row.get("close_available_at") if data_kind == "real" else row.get("available_at")
        ),
    }
    open_source = row.get("open_source")
    if isinstance(open_source, Mapping):
        evidence["source"] = open_source.get("source")
        evidence["evidence_id"] = open_source.get("evidence_id")
    return evidence


def _status_source_evidence(row: Mapping[str, Any]) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "trading_status": row.get("trading_status"),
        "availability_evidence": row.get("availability_evidence"),
        "available_at": row.get("available_at"),
    }
    status_id = row.get("status_evidence_id")
    if isinstance(status_id, str) and status_id.strip():
        evidence["status_evidence_id"] = status_id
    halt = row.get("halt")
    if isinstance(halt, Mapping):
        evidence["halt"] = dict(halt)
    return evidence


def _classify_no_trade_for_valuation(
    row: Mapping[str, Any] | None,
    close_cutoff: datetime,
    *,
    data_kind: str,
) -> tuple[bool, str | None]:
    """Return whether ``row`` confirms full-day no-trade by ``close_cutoff``.

    When False, returns a specific ``valuation_gap_reason`` distinguishing
    missing bars, unknown status, late notices, conflicting evidence and
    resumed partial halts without a confirmed close.
    """
    if row is None:
        return False, "missing_bar"
    confirmed_shape, shape_reason = confirmed_no_trade_shape(row, data_kind=data_kind)
    if confirmed_shape:
        if not _available_by(dict(row), close_cutoff):
            available_at = _parse_instant(row.get("available_at"))
            if available_at is not None and available_at > close_cutoff:
                return False, "status_evidence_after_cutoff"
            return False, "missing_status_availability_evidence"
        return True, None
    if shape_reason is not None:
        return False, shape_reason
    if row.get("trading_status") == "trading" and _valid_price(row.get("close")):
        ts_field = "close_available_at" if data_kind == "real" else "available_at"
        available_at = _parse_instant(row.get(ts_field))
        if available_at is not None and available_at > close_cutoff:
            return False, "close_evidence_after_cutoff"
        return False, "missing_close_availability_evidence"
    return False, "missing_or_unconfirmed_unadjusted_close"


@dataclass
class _CarryState:
    """Per-symbol carry-forward valuation anchor and chain-break status."""

    anchor: dict[str, Any] | None = None
    chain_broken: bool = False

    def reset(self) -> None:
        self.anchor = None
        self.chain_broken = False

    def break_chain(self) -> None:
        if self.anchor is not None:
            self.chain_broken = True
        self.anchor = None


def _missing_holding(
    quantity: float, gap_reason: str | None, *, close: float | None = None
) -> dict[str, Any]:
    return {
        "quantity": quantity,
        "close": close,
        "market_value": None,
        "valuation_price": None,
        "valuation_basis": "missing",
        "gap_reason": gap_reason,
    }


def _valuation_gap(
    day: str,
    symbol: str,
    gap_reason: str | None,
    *,
    status: str | None = None,
    row: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    gap: dict[str, Any] = {
        "date": day,
        "symbol": symbol,
        "reason": "missing_or_unconfirmed_unadjusted_close",
        "valuation_gap_reason": gap_reason,
    }
    if status is not None:
        gap["trading_status"] = status
    if row is not None and isinstance(row.get("halt"), dict):
        gap["halt"] = row["halt"]
    return gap


def _adjust_carried_basis_for_actions(
    day: str,
    anchor: dict[str, Any],
    applied_actions: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str | None]:
    """Adjust a carried-forward valuation anchor for corporate actions on ``day``."""
    if not applied_actions:
        return anchor, None
    price = float(anchor["valuation_price"])
    adjustments = [dict(item) for item in anchor["adjustments"]]
    for action in applied_actions:
        action_type = action.get("type")
        if action_type == "cash_dividend":
            amount = float(action["cash_per_share"])
            new_price = price - amount
            extra: dict[str, Any] = {
                "cash_per_share": amount,
                "pay_date": action["pay_date"],
            }
        elif action_type == "share_adjustment":
            ratio = float(action["ratio"])
            new_price = price / ratio
            extra = {"ratio": ratio}
        else:
            return None, "suspended_corporate_action_unadjusted"
        if not _valid_price(new_price) or new_price <= _EPSILON:
            return None, "carried_valuation_price_non_positive"
        entry: dict[str, Any] = {
            "date": day,
            "type": action_type,
            **extra,
            "terms_evidence": action["terms_evidence"],
            "available_at": action["available_at"],
            "availability_evidence": action["availability_evidence"],
            "valuation_price_before": price,
            "valuation_price_after": new_price,
        }
        if "sequence" in action:
            entry["sequence"] = action["sequence"]
        adjustments.append(entry)
        price = new_price
    updated = dict(anchor)
    updated["valuation_price"] = price
    updated["adjustments"] = adjustments
    return updated, None


def _valuation_carry_forward(
    day: str,
    raw_bars: dict[tuple[str, str], dict],
    shares: dict[str, float],
    cash: float,
    receivables: list[dict[str, Any]],
    unresolved: dict[str, list[dict[str, str]]],
    carry_states: dict[str, _CarryState],
    applied_actions_today: Mapping[str, list[dict[str, Any]]],
    *,
    data_kind: str = "synthetic",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Value holdings under ``carry-forward-suspended-v1`` (ADR 0003)."""
    holdings: dict[str, Any] = {}
    gaps: list[dict[str, Any]] = []
    carried_symbols: list[str] = []
    market_value = 0.0
    valid = True
    close_cutoff = datetime.combine(
        datetime.fromisoformat(day).date(), time(23, 59, 59), tzinfo=_CN_TZ
    )
    for symbol in ETF_SYMBOLS:
        quantity = shares[symbol]
        state = carry_states[symbol]
        row = raw_bars.get((day, symbol))
        status = row.get("trading_status") if row is not None else None
        markable = status == "trading" or (
            data_kind == "real" and status == "partial_halt"
        )
        close_conflict = row is not None and (
            row.get("status_conflict") is not None
            or (
                data_kind == "real"
                and status == "partial_halt"
                and halt_conflicts_with_trade(row.get("halt"), day, row)
            )
        )
        raw_close = row.get("close") if row is not None else None
        has_trusted_close = (
            row is not None
            and markable
            and not close_conflict
            and _valid_price(raw_close)
            and _close_available(row, close_cutoff, data_kind)
        )

        if quantity <= _EPSILON:
            state.reset()
            trusted_close = float(raw_close) if has_trusted_close else None
            if unresolved[symbol]:
                valid = False
                for action_gap in unresolved[symbol]:
                    gaps.append({"symbol": symbol, **action_gap})
                holdings[symbol] = _missing_holding(
                    quantity, unresolved[symbol][0]["reason"], close=trusted_close
                )
            else:
                holdings[symbol] = {
                    "quantity": quantity,
                    "close": trusted_close,
                    "market_value": 0.0,
                    "valuation_price": None,
                    "valuation_basis": "not_held",
                }
            continue

        if has_trusted_close:
            actual_close = float(raw_close)
            state.anchor = {
                "source_close_date": day,
                "source_close_price": actual_close,
                "source_close_evidence": _close_source_evidence(row, data_kind=data_kind),
                "valuation_price": actual_close,
                "carried_sessions": 0,
                "adjustments": [],
            }
            state.chain_broken = False
            if unresolved[symbol]:
                valid = False
                for action_gap in unresolved[symbol]:
                    gaps.append({"symbol": symbol, **action_gap})
                holdings[symbol] = _missing_holding(
                    quantity, unresolved[symbol][0]["reason"], close=actual_close
                )
            else:
                value = quantity * actual_close
                market_value += value
                holdings[symbol] = {
                    "quantity": quantity,
                    "close": actual_close,
                    "market_value": value,
                    "valuation_price": actual_close,
                    "valuation_basis": "actual_close",
                }
            continue

        confirmed_no_trade, non_trade_reason = _classify_no_trade_for_valuation(
            row, close_cutoff, data_kind=data_kind
        )
        if confirmed_no_trade:
            if unresolved[symbol]:
                valid = False
                state.break_chain()
                for action_gap in unresolved[symbol]:
                    gaps.append({"symbol": symbol, **action_gap})
                holdings[symbol] = _missing_holding(
                    quantity, unresolved[symbol][0]["reason"]
                )
                continue

            anchor = state.anchor
            if anchor is None:
                valid = False
                gap_reason = (
                    "carry_forward_chain_broken"
                    if state.chain_broken
                    else "missing_authorized_prior_close"
                )
                gaps.append(_valuation_gap(day, symbol, gap_reason, status=status))
                holdings[symbol] = _missing_holding(quantity, gap_reason)
                continue

            day_actions = applied_actions_today.get(symbol, [])
            if day_actions:
                adjusted, adj_gap_reason = _adjust_carried_basis_for_actions(
                    day, anchor, day_actions
                )
                if adjusted is None:
                    valid = False
                    state.break_chain()
                    gaps.append(_valuation_gap(day, symbol, adj_gap_reason, status=status))
                    holdings[symbol] = _missing_holding(quantity, adj_gap_reason)
                    continue
                anchor = adjusted
                state.anchor = anchor

            anchor["carried_sessions"] += 1
            valuation_price = float(anchor["valuation_price"])
            value = quantity * valuation_price
            market_value += value
            carried_symbols.append(symbol)
            holdings[symbol] = {
                "quantity": quantity,
                "close": None,
                "market_value": value,
                "valuation_price": valuation_price,
                "valuation_basis": "carried_forward_prior_close",
                "carry_forward": {
                    "source_close_date": anchor["source_close_date"],
                    "source_close_price": anchor["source_close_price"],
                    "source_close_evidence": dict(anchor["source_close_evidence"]),
                    "valuation_price": valuation_price,
                    "carried_sessions": anchor["carried_sessions"],
                    "adjustments": [dict(item) for item in anchor["adjustments"]],
                    "status_evidence": _status_source_evidence(row),
                },
            }
            continue

        # Not a trusted close and not a confirmed full-day no-trade: gap & break chain.
        valid = False
        state.break_chain()
        gaps.append(_valuation_gap(day, symbol, non_trade_reason, status=status, row=row))
        if unresolved[symbol]:
            for action_gap in unresolved[symbol]:
                gaps.append({"symbol": symbol, **action_gap})
        holdings[symbol] = _missing_holding(quantity, non_trade_reason)

    receivable_total = math.fsum(item["amount"] for item in receivables)
    nav = cash + market_value + receivable_total if valid else None
    day_basis = (
        "missing"
        if not valid
        else "carried_forward"
        if carried_symbols
        else "actual_close"
    )
    return {
        "date": day,
        "nav": nav,
        "cash": cash,
        "market_value": market_value if valid else None,
        "dividend_receivables": receivable_total if valid else None,
        "receivables": [dict(item) for item in receivables],
        "holdings": holdings,
        "valuation_basis": day_basis,
        "carried_forward_symbols": sorted(carried_symbols),
    }, gaps


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
