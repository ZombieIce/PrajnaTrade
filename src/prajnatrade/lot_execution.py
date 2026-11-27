"""Pre-open target-weight / fixed-amount freezing and actual-open whole-lot settlement."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from prajnatrade.trading_status import (
    ACTUAL_OPEN_LOT_RULES,
    ETF_SYMBOLS,
    halt_conflicts_with_volume,
    halt_covers_session,
)

DEFAULT_LOT_SIZE = 100
_EPSILON = 1e-12
_CASH_TOLERANCE = 1e-12


def normalize_lot_sizes(
    lot_sizes: Mapping[str, int] | None = None,
    *,
    default_lot_size: int = DEFAULT_LOT_SIZE,
    symbols: Sequence[str] = ETF_SYMBOLS,
) -> dict[str, int]:
    """Validate and normalize per-symbol lot sizes as positive integers."""
    if (
        isinstance(default_lot_size, bool)
        or not isinstance(default_lot_size, int)
        or default_lot_size <= 0
    ):
        raise ValueError(f"Lot size must be a positive integer, got {default_lot_size!r}.")
    normalized = {symbol: default_lot_size for symbol in symbols}
    if lot_sizes is None:
        return normalized
    if not isinstance(lot_sizes, Mapping):
        raise ValueError("lot_sizes must be a mapping of symbol to positive integer.")
    unknown = set(lot_sizes) - set(symbols)
    if unknown:
        raise ValueError(f"Unknown symbol(s) in lot_sizes: {', '.join(sorted(unknown))}.")
    for symbol, value in lot_sizes.items():
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(
                f"Lot size for {symbol} must be a positive integer, got {value!r}."
            )
        normalized[symbol] = value
    return normalized


def classify_unfilled_reason(row: Mapping[str, Any] | None) -> str:
    """Classify why a required symbol cannot match at today's open."""
    if row is None:
        return "missing_bar"
    status = row.get("trading_status")
    if status == "unknown":
        return "unknown_trading_status"
    if status == "suspended":
        return "confirmed_no_trade"
    if row.get("open_conflict") is not None:
        return "missing_required_open"
    if status == "partial_halt":
        halt, day = row.get("halt"), row.get("date")
        if halt_conflicts_with_volume(halt, str(day), row.get("volume")):
            return "unknown_trading_status"
        if halt_covers_session(halt, str(day)):
            return "confirmed_no_trade"
    return "missing_required_open"


def _is_late_resume_bar(row: Mapping[str, Any] | None) -> bool:
    return isinstance(row, Mapping) and row.get("trading_status") == "partial_halt"


def _make_fill(
    day: str,
    symbol: str,
    side: str,
    quantity: float,
    raw_open: float,
    fill_price: float,
    fee: float,
    slippage_cost: float,
) -> dict[str, Any]:
    return {
        "date": day,
        "symbol": symbol,
        "side": side,
        "quantity": float(quantity),
        "raw_open": float(raw_open),
        "price": float(fill_price),
        "price_basis": "unadjusted_open",
        "notional": float(quantity * raw_open),
        "fee": float(fee),
        "slippage": float(slippage_cost),
    }


def match_open_lot_day(
    day: str,
    signal: Mapping[str, Any],
    weights: Mapping[str, float],
    open_prices: Mapping[str, float | None],
    raw_bars: Mapping[tuple[str, str], Mapping[str, Any]],
    cash: float,
    shares: Mapping[str, float],
    fee_rate: float,
    slippage_rate: float,
    *,
    lot_sizes: Mapping[str, int] | None = None,
    symbol_order: Sequence[str] = ETF_SYMBOLS,
    fixed_amounts: Mapping[str, float] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], float, dict[str, float], dict[str, float]]:
    """Match pre-open target weights / fixed amounts at actual open with whole-lot rounding.

    Sells execute first in ``symbol_order``; only realized net sell proceeds add to
    available cash. When any held symbol lacks an executable open, portfolio open
    equity is unknown: explicit full exits (``w_s == 0``) and explicit fixed-amount
    instructions still execute, while weight-dependent orders are unfilled with
    ``equity_basis_unknown``. Remaining buy demands are scaled proportionally if
    they exceed available cash, then floored to whole lots without overspending.
    """
    ordered_symbols = tuple(symbol_order)
    lots_map = normalize_lot_sizes(lot_sizes, symbols=ordered_symbols)
    fixed_map = {
        symbol: float(amount)
        for symbol, amount in (fixed_amounts or {}).items()
        if amount is not None and float(amount) > _EPSILON
    }
    new_shares = {symbol: float(shares.get(symbol, 0.0)) for symbol in ordered_symbols}
    current_cash = float(cash)
    starting_cash = current_cash

    required = [
        symbol
        for symbol in ordered_symbols
        if weights.get(symbol, 0.0) > _EPSILON
        or new_shares[symbol] > _EPSILON
        or symbol in fixed_map
    ]
    unfilled_reasons: dict[str, str] = {
        symbol: classify_unfilled_reason(raw_bars.get((day, symbol)))
        for symbol in required
        if open_prices.get(symbol) is None
    }
    equity_unknown = any(
        new_shares[symbol] > _EPSILON and open_prices.get(symbol) is None
        for symbol in ordered_symbols
    )

    open_equity: float | None = None
    if not equity_unknown:
        open_equity = current_cash + math.fsum(
            new_shares[symbol] * float(open_prices[symbol])
            for symbol in ordered_symbols
            if new_shares[symbol] > _EPSILON and open_prices.get(symbol) is not None
        )

    sell_quantities: dict[str, float] = {}
    buy_demands: dict[str, float] = {}
    for symbol in ordered_symbols:
        if symbol not in required:
            continue
        open_price = open_prices.get(symbol)
        if open_price is None:
            continue
        held_qty = new_shares[symbol]
        if symbol in fixed_map:
            buy_demands[symbol] = fixed_map[symbol]
            continue
        weight = float(weights.get(symbol, 0.0))
        if weight <= _EPSILON:
            if held_qty > _EPSILON:
                sell_quantities[symbol] = held_qty
            continue
        if equity_unknown or open_equity is None:
            unfilled_reasons.setdefault(symbol, "equity_basis_unknown")
            continue
        target_value = open_equity * weight
        current_value = held_qty * float(open_price)
        delta = target_value - current_value
        if delta < -_EPSILON:
            lot_unit = lots_map[symbol]
            lots_to_sell = math.floor((abs(delta) / float(open_price) + _EPSILON) / lot_unit)
            sell_qty = min(held_qty, float(lots_to_sell * lot_unit))
            if sell_qty > _EPSILON:
                sell_quantities[symbol] = sell_qty
        elif delta > _EPSILON:
            buy_demands[symbol] = delta

    day_fills: list[dict[str, Any]] = []
    costs = {"notional": 0.0, "fees": 0.0, "slippage": 0.0}
    late_resume_sell_symbols: list[str] = []
    funded_buy_symbols: list[str] = []

    for symbol in ordered_symbols:
        sell_qty = sell_quantities.get(symbol, 0.0)
        if sell_qty <= _EPSILON:
            continue
        raw_open = float(open_prices[symbol])  # type: ignore[arg-type]
        fill_price = raw_open * (1.0 - slippage_rate)
        notional = sell_qty * raw_open
        fee = sell_qty * fill_price * fee_rate
        slippage_cost = sell_qty * (raw_open - fill_price)
        current_cash += sell_qty * fill_price - fee
        new_shares[symbol] -= sell_qty
        if abs(new_shares[symbol]) <= _EPSILON:
            new_shares[symbol] = 0.0
        day_fills.append(
            _make_fill(day, symbol, "sell", sell_qty, raw_open, fill_price, fee, slippage_cost)
        )
        costs["notional"] += notional
        costs["fees"] += fee
        costs["slippage"] += slippage_cost
        if _is_late_resume_bar(raw_bars.get((day, symbol))):
            late_resume_sell_symbols.append(symbol)

    available_cash = max(0.0, current_cash)
    total_demand = math.fsum(buy_demands.values())
    cash_constrained = total_demand > available_cash + _CASH_TOLERANCE
    scale = (available_cash / total_demand) if (cash_constrained and total_demand > 0) else 1.0

    lot_rounding_residual = 0.0
    total_buy_cost = 0.0
    for symbol in ordered_symbols:
        demand = buy_demands.get(symbol, 0.0)
        if demand <= _EPSILON:
            continue
        raw_open = float(open_prices[symbol])  # type: ignore[arg-type]
        lot_unit = lots_map[symbol]
        fill_price = raw_open * (1.0 + slippage_rate)
        lot_cost = lot_unit * fill_price * (1.0 + fee_rate)
        allocated = demand * scale
        lots = math.floor((allocated + _CASH_TOLERANCE) / lot_cost)
        while lots > 0 and lots * lot_cost > current_cash + _CASH_TOLERANCE:
            lots -= 1
        actual_cost = lots * lot_cost
        lot_rounding_residual += max(0.0, allocated - actual_cost)
        if lots <= 0:
            continue
        buy_qty = float(lots * lot_unit)
        notional = buy_qty * raw_open
        fee = buy_qty * fill_price * fee_rate
        slippage_cost = buy_qty * (fill_price - raw_open)
        debit = buy_qty * fill_price + fee
        current_cash -= debit
        total_buy_cost += debit
        new_shares[symbol] += buy_qty
        day_fills.append(
            _make_fill(day, symbol, "buy", buy_qty, raw_open, fill_price, fee, slippage_cost)
        )
        costs["notional"] += notional
        costs["fees"] += fee
        costs["slippage"] += slippage_cost
        funded_buy_symbols.append(symbol)

    if -_CASH_TOLERANCE < current_cash < 0.0:
        current_cash = 0.0

    traded = {fill["symbol"] for fill in day_fills}
    unfilled = [
        {"symbol": symbol, "reason": unfilled_reasons[symbol]}
        for symbol in required
        if symbol in unfilled_reasons and symbol not in traded
    ]
    batch_complete = (not unfilled) and (not cash_constrained)
    if cash_constrained:
        residual_reason: str | None = "cash_constrained"
    elif lot_rounding_residual > _EPSILON:
        residual_reason = "lot_rounding_residual"
    else:
        residual_reason = None

    sell_funded_buy = bool(
        late_resume_sell_symbols
        and funded_buy_symbols
        and total_buy_cost > max(0.0, starting_cash) + _CASH_TOLERANCE
    )
    assumptions = {
        "late_resume_sell_funds_same_day_buy": True,
        "triggered": sell_funded_buy,
        "late_resume_sell_symbols": late_resume_sell_symbols,
        "funded_buy_symbols": funded_buy_symbols if sell_funded_buy else [],
    }
    attempt = {
        "signal_date": signal.get("date", day),
        "date": day,
        "attempts": 1,
        "status": (
            "filled"
            if not unfilled
            else "partially_filled"
            if day_fills
            else "not_filled"
        ),
        "filled_symbols": [symbol for symbol in ordered_symbols if symbol in traded],
        "unfilled": unfilled,
        "basket_complete": batch_complete,
        "batch_complete": batch_complete,
        "cash_constrained": cash_constrained,
        "lot_rounding_residual": float(lot_rounding_residual),
        "residual_reason": residual_reason,
        "daily_simulation_assumptions": assumptions,
    }
    return attempt, day_fills, current_cash, new_shares, costs


__all__ = [
    "ACTUAL_OPEN_LOT_RULES",
    "DEFAULT_LOT_SIZE",
    "classify_unfilled_reason",
    "match_open_lot_day",
    "normalize_lot_sizes",
]
