from __future__ import annotations

import math
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from prajnatrade.data import ETF_SYMBOLS
from prajnatrade.snapshot import read_discovery


_CN_TZ = timezone(timedelta(hours=8))
_TARGET_WEIGHTS = {symbol: 1.0 / len(ETF_SYMBOLS) for symbol in ETF_SYMBOLS}
_EPSILON = 1e-12


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

    for day_index, day in enumerate(sessions):
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
                if not _is_executable_open(raw_bars.get((day, symbol)), open_cutoff)
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
            day, raw_bars, shares, cash, receivables, unresolved_actions
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
        "data_kind": "synthetic",
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
            "raw_bars": _data_gaps(sessions, raw_bars, "raw_bars"),
            "derived": _data_gaps(sessions, derived, "derived"),
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


def _is_executable_open(row: dict | None, cutoff: datetime) -> bool:
    return (
        row is not None
        and row.get("trading_status") == "trading"
        and _valid_price(row.get("open"))
        and _available_by(row, cutoff)
    )


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
        close = row.get("close") if row else None
        if quantity > _EPSILON and (
            row is None
            or row.get("trading_status") != "trading"
            or not _valid_price(close)
            or not _available_by(row, close_cutoff)
        ):
            valid = False
            gaps.append({
                "date": day,
                "symbol": symbol,
                "reason": "missing_or_unconfirmed_unadjusted_close",
            })
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
            if missing:
                gaps.append({"date": day, "symbol": symbol, "missing_fields": missing})
    return gaps
