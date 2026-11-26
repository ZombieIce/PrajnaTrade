from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from prajnatrade.data import CASH, ETF_SYMBOLS, ValidatedData

_CASH_ROUNDING_EPSILON = 1e-8

Strategy = Callable[
    [pd.DataFrame, Mapping[str, Any]], Mapping[str, float] | None
]


class StrategyError(ValueError):
    """Raised when a strategy requests invalid portfolio weights."""


@dataclass
class BacktestResult:
    data_hash: str
    price_basis: str
    initial_equity: float
    ending_equity: float
    metrics: dict[str, Any]
    equity_curve: list[dict[str, Any]]
    fills: list[dict[str, Any]]
    unavailable_prices: list[dict[str, str]]

    def to_dict(self, data_kind: str) -> dict[str, Any]:
        return {
            "data_kind": data_kind,
            "data_hash": self.data_hash,
            "price_basis": self.price_basis,
            "initial_equity": self.initial_equity,
            "ending_equity": self.ending_equity,
            "metrics": self.metrics,
            "equity_curve": self.equity_curve,
            "fills": self.fills,
            "unavailable_prices": self.unavailable_prices,
        }


def equal_weight_buy_and_hold(
    history: pd.DataFrame, state: Mapping[str, Any]
) -> Mapping[str, float] | None:
    if any(quantity > 1e-12 for quantity in state["shares"].values()):
        return None
    weight = 1.0 / len(ETF_SYMBOLS)
    return {**{symbol: weight for symbol in ETF_SYMBOLS}, CASH: 0.0}


def run_backtest(
    dataset: ValidatedData,
    strategy: Strategy,
    *,
    initial_cash: float = 100_000.0,
    transaction_cost_bps: float = 5.0,
    slippage_bps: float = 0.0,
    start_date: str | pd.Timestamp | None = None,
    end_date: str | pd.Timestamp | None = None,
) -> BacktestResult:
    _validate_parameters(initial_cash, transaction_cost_bps, slippage_bps)
    bars = dataset.bars
    dates = list(bars["date"].drop_duplicates())
    start = pd.Timestamp(start_date).normalize() if start_date is not None else None
    end = pd.Timestamp(end_date).normalize() if end_date is not None else None
    dates = [
        date
        for date in dates
        if (start is None or date >= start) and (end is None or date <= end)
    ]
    if not dates:
        raise ValueError("The requested backtest interval has no data sessions.")
    cash = float(initial_cash)
    shares = {symbol: 0.0 for symbol in ETF_SYMBOLS}
    last_mark: dict[str, float] = {}
    pending_weights: Mapping[str, float] | None = None
    fills: list[dict[str, Any]] = []
    unavailable: list[dict[str, str]] = []
    equity_curve: list[dict[str, Any]] = []
    traded_notional = fees_paid = slippage_paid = 0.0
    fee_rate = transaction_cost_bps / 10_000.0
    slippage_rate = slippage_bps / 10_000.0

    for date in dates:
        day = bars.loc[bars["date"] == date].set_index("symbol")
        open_prices = {
            symbol: _price(day, symbol, "open") for symbol in ETF_SYMBOLS
        }

        if pending_weights is not None:
            active_symbols = {
                symbol
                for symbol in ETF_SYMBOLS
                if pending_weights.get(symbol, 0.0) > 0 or shares[symbol] > 1e-12
            }
            missing = sorted(
                symbol
                for symbol in active_symbols
                if open_prices.get(symbol) is None
            )
            if missing:
                unavailable.extend(
                    {
                        "date": date.strftime("%Y-%m-%d"),
                        "symbol": symbol,
                        "purpose": "execution",
                    }
                    for symbol in missing
                )
            else:
                open_marks = {
                    symbol: open_prices[symbol] or last_mark.get(symbol, 0.0)
                    for symbol in ETF_SYMBOLS
                }
                open_equity = cash + sum(
                    shares[symbol] * open_marks[symbol] for symbol in ETF_SYMBOLS
                )
                target_shares = {
                    symbol: (
                        open_equity * pending_weights.get(symbol, 0.0)
                        / open_marks[symbol]
                        if open_marks[symbol] > 0
                        else 0.0
                    )
                    for symbol in ETF_SYMBOLS
                }
                day_fills, cash, shares, costs = _rebalance_at_open(
                    date,
                    open_prices,
                    target_shares,
                    cash,
                    shares,
                    fee_rate,
                    slippage_rate,
                )
                fills.extend(day_fills)
                traded_notional += costs["notional"]
                fees_paid += costs["fees"]
                slippage_paid += costs["slippage"]
                pending_weights = None

        close_marks: dict[str, float] = {}
        for symbol in ETF_SYMBOLS:
            close_price = _price(day, symbol, "close")
            if close_price is not None:
                last_mark[symbol] = close_price
            elif shares[symbol] > 1e-12:
                purpose = "mark"
                if symbol not in last_mark:
                    if open_prices[symbol] is None:
                        raise ValueError(
                            f"No available mark for {symbol} on {date:%Y-%m-%d}."
                        )
                    last_mark[symbol] = open_prices[symbol]
                    purpose = "mark_open_fallback"
                unavailable.append(
                    {
                        "date": date.strftime("%Y-%m-%d"),
                        "symbol": symbol,
                        "purpose": purpose,
                    }
                )
            if symbol in last_mark:
                close_marks[symbol] = last_mark[symbol]
        equity = cash + sum(
            shares[symbol] * close_marks.get(symbol, 0.0) for symbol in ETF_SYMBOLS
        )
        equity_curve.append(
            {"date": date.strftime("%Y-%m-%d"), "equity": float(equity), "cash": float(cash)}
        )

        history = bars.loc[bars["date"] <= date].copy()
        state = {
            "cash": cash,
            "shares": dict(shares),
            "equity": float(equity),
        }
        requested = strategy(history, state)
        if requested is not None:
            pending_weights = _validate_weights(requested)

    ending_equity = equity_curve[-1]["equity"]
    metrics = _calculate_metrics(
        equity_curve,
        initial_cash,
        ending_equity,
        traded_notional,
        fees_paid,
        slippage_paid,
    )
    return BacktestResult(
        data_hash=dataset.data_hash,
        price_basis=str(bars["price_basis"].iloc[0]),
        initial_equity=float(initial_cash),
        ending_equity=float(ending_equity),
        metrics=metrics,
        equity_curve=equity_curve,
        fills=fills,
        unavailable_prices=unavailable,
    )


def benchmark_relative_returns(
    result: BacktestResult, benchmark: BacktestResult
) -> dict[str, Any]:
    result_dates = [point["date"] for point in result.equity_curve]
    benchmark_dates = [point["date"] for point in benchmark.equity_curve]
    if result.data_hash != benchmark.data_hash or result_dates != benchmark_dates:
        raise ValueError("Benchmark and result must use the same dataset and sessions.")
    return {
        "total_return": (
            result.metrics["total_return"] - benchmark.metrics["total_return"]
        ),
        "yearly_returns": {
            year: value - benchmark.metrics["yearly_returns"][year]
            for year, value in result.metrics["yearly_returns"].items()
        },
    }


def _validate_parameters(initial_cash: float, cost_bps: float, slippage_bps: float) -> None:
    for name, value in (
        ("initial_cash", initial_cash),
        ("transaction_cost_bps", cost_bps),
        ("slippage_bps", slippage_bps),
    ):
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be finite and nonnegative.")
    if initial_cash <= 0:
        raise ValueError("initial_cash must be greater than zero.")
    if cost_bps >= 10_000:
        raise ValueError("transaction_cost_bps must be less than 10,000.")
    if slippage_bps >= 10_000:
        raise ValueError("slippage_bps must be less than 10,000.")


def _validate_weights(weights: Mapping[str, float]) -> dict[str, float]:
    if not isinstance(weights, Mapping):
        raise StrategyError("Strategy must return a mapping of asset weights or None.")
    unknown = sorted(set(weights) - set((*ETF_SYMBOLS, CASH)))
    if unknown:
        raise StrategyError(f"Strategy returned unsupported assets: {', '.join(unknown)}.")
    normalized: dict[str, float] = {}
    for symbol in (*ETF_SYMBOLS, CASH):
        raw = weights.get(symbol, 0.0)
        try:
            weight = float(raw)
        except (TypeError, ValueError) as error:
            raise StrategyError(f"Weight for {symbol} must be numeric.") from error
        if not math.isfinite(weight) or weight < 0 or weight > 1:
            raise StrategyError(f"Weight for {symbol} must be finite and between 0 and 1.")
        normalized[symbol] = weight
    if not math.isclose(sum(normalized.values()), 1.0, rel_tol=0.0, abs_tol=1e-8):
        raise StrategyError("ETF and CASH weights must sum to 1.")
    return normalized


def _price(day: pd.DataFrame, symbol: str, column: str) -> float | None:
    if symbol not in day.index:
        return None
    if column == "open" and float(day.at[symbol, "volume"]) <= 0:
        return None
    value = day.at[symbol, column]
    return float(value) if pd.notna(value) else None


def _execution_price(value: float | None) -> float | None:
    """A usable open: finite and positive, otherwise the symbol cannot match."""
    if value is None:
        return None
    price = float(value)
    return price if math.isfinite(price) and price > 0 else None


def _rebalance_at_open(
    date: pd.Timestamp,
    open_prices: Mapping[str, float | None],
    target_shares: Mapping[str, float],
    cash: float,
    shares: dict[str, float],
    fee_rate: float,
    slippage_rate: float,
    *,
    cash_capped_buy: str | None = None,
) -> tuple[list[dict[str, Any]], float, dict[str, float], dict[str, float]]:
    """Match sells then buys at the given opens, skipping symbols without one.

    A symbol whose open is absent or not a positive price is left untouched: it
    cannot trade, so its shares and cash stay in the account. Without a missing
    price the result is the same as matching every symbol at its open.
    A designated cash-capped buy is the only buy and uses cash after sells.
    """
    fills: list[dict[str, Any]] = []
    costs = {"notional": 0.0, "fees": 0.0, "slippage": 0.0}

    for symbol in ETF_SYMBOLS:
        quantity = max(0.0, shares[symbol] - target_shares[symbol])
        if quantity <= 1e-12:
            continue
        market_price = _execution_price(open_prices.get(symbol))
        if market_price is None:
            continue
        fill_price = market_price * (1.0 - slippage_rate)
        notional = quantity * market_price
        fee = quantity * fill_price * fee_rate
        slippage_cost = quantity * (market_price - fill_price)
        cash += quantity * fill_price - fee
        shares[symbol] -= quantity
        fills.append(
            _fill(date, symbol, "sell", quantity, fill_price, notional, fee, slippage_cost)
        )
        costs["notional"] += notional
        costs["fees"] += fee
        costs["slippage"] += slippage_cost

    buys: list[tuple[str, float, float, float, float]] = []
    total_buy_cost = 0.0
    for symbol in ETF_SYMBOLS:
        market_price = _execution_price(open_prices.get(symbol))
        if market_price is None:
            continue
        fill_price = market_price * (1.0 + slippage_rate)
        if symbol == cash_capped_buy:
            if cash < _CASH_ROUNDING_EPSILON:
                continue
            quantity = cash / (fill_price * (1.0 + fee_rate))
        else:
            quantity = max(0.0, target_shares[symbol] - shares[symbol])
        if quantity <= 1e-12:
            continue
        fee = quantity * fill_price * fee_rate
        cost = quantity * fill_price + fee
        buys.append((symbol, quantity, market_price, fill_price, fee))
        total_buy_cost += cost

    scale = min(1.0, cash / total_buy_cost) if total_buy_cost > 0 else 1.0
    for symbol, requested_quantity, market_price, fill_price, _ in buys:
        quantity = requested_quantity * scale
        notional = quantity * market_price
        fee = quantity * fill_price * fee_rate
        slippage_cost = quantity * (fill_price - market_price)
        cash -= quantity * fill_price + fee
        shares[symbol] += quantity
        fills.append(
            _fill(date, symbol, "buy", quantity, fill_price, notional, fee, slippage_cost)
        )
        costs["notional"] += notional
        costs["fees"] += fee
        costs["slippage"] += slippage_cost

    if -_CASH_ROUNDING_EPSILON < cash < 0:
        cash = 0.0
    return fills, cash, shares, costs


def _fill(
    date: pd.Timestamp,
    symbol: str,
    side: str,
    quantity: float,
    price: float,
    notional: float,
    fee: float,
    slippage: float,
) -> dict[str, Any]:
    return {
        "date": date.strftime("%Y-%m-%d"),
        "symbol": symbol,
        "side": side,
        "quantity": float(quantity),
        "price": float(price),
        "notional": float(notional),
        "fee": float(fee),
        "slippage": float(slippage),
    }


def _calculate_metrics(
    curve: list[dict[str, Any]],
    initial_equity: float,
    ending_equity: float,
    traded_notional: float,
    fees_paid: float,
    slippage_paid: float,
) -> dict[str, Any]:
    values = np.asarray([point["equity"] for point in curve], dtype=float)
    daily_returns = values[1:] / values[:-1] - 1.0
    if len(curve) > 1:
        start = pd.Timestamp(curve[0]["date"])
        end = pd.Timestamp(curve[-1]["date"])
        years = max((end - start).days / 365.25, 0.0)
    else:
        years = 0.0
    total_return = ending_equity / initial_equity - 1.0
    cagr = (ending_equity / initial_equity) ** (1.0 / years) - 1.0 if years else 0.0
    volatility = (
        float(np.std(daily_returns, ddof=1) * np.sqrt(252))
        if len(daily_returns) > 1
        else 0.0
    )
    daily_std = float(np.std(daily_returns, ddof=1)) if len(daily_returns) > 1 else 0.0
    sharpe = (
        float(np.mean(daily_returns) / daily_std * np.sqrt(252))
        if daily_std > 0
        else None
    )
    drawdowns = values / np.maximum.accumulate(values) - 1.0
    max_drawdown = float(np.min(drawdowns)) if len(drawdowns) else 0.0
    calmar = cagr / abs(max_drawdown) if max_drawdown < 0 else None
    year_end: dict[int, float] = {}
    for point in curve:
        year_end[int(point["date"][:4])] = point["equity"]
    yearly_returns: dict[str, float] = {}
    prior_equity = initial_equity
    for year, equity in sorted(year_end.items()):
        yearly_returns[str(year)] = float(equity / prior_equity - 1.0)
        prior_equity = equity
    return {
        "total_return": float(total_return),
        "cagr": float(cagr),
        "annualized_volatility": volatility,
        "sharpe": sharpe,
        "calmar": float(calmar) if calmar is not None else None,
        "max_drawdown": max_drawdown,
        "turnover": float(traded_notional / initial_equity),
        "transaction_costs": {
            "fees": float(fees_paid),
            "slippage": float(slippage_paid),
            "total": float(fees_paid + slippage_paid),
        },
        "yearly_returns": yearly_returns,
    }
