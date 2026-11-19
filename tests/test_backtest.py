from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
import pytest

from prajnatrade.backtest import (
    StrategyError,
    benchmark_relative_returns,
    equal_weight_buy_and_hold,
    run_backtest,
)
from prajnatrade.data import load_bars

SYMBOLS = ("511260", "518880", "515080", "513300", "159612")


def _write_bars(
    path: Path,
    unavailable_open: bool = False,
    unavailable_close: bool = False,
    zero_volume: bool = False,
) -> None:
    lines = ["date,symbol,open,high,low,close,volume"]
    days = (
        ("2024-01-02", 48, 50),
        ("2024-01-03", 100, 110),
        ("2024-01-04", 110, 121),
    )
    for day, open_price, close_price in days:
        for symbol in SYMBOLS:
            open_text = (
                ""
                if unavailable_open
                and day == "2024-01-03"
                and symbol == "511260"
                else str(open_price)
            )
            close_text = (
                ""
                if unavailable_close
                and day == "2024-01-03"
                and symbol == "511260"
                else str(close_price)
            )
            volume = (
                0
                if zero_volume
                and day == "2024-01-03"
                and symbol == "511260"
                else 1000
            )
            lines.append(
                f"{day},{symbol},{open_text},{close_price},{open_text},"
                f"{close_text},{volume}"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_missing_open_defers_the_whole_target_to_the_next_available_session(
    tmp_path: Path,
) -> None:
    bars_path = tmp_path / "bars.csv"
    _write_bars(bars_path, unavailable_open=True)
    dataset = load_bars(bars_path, "total_return")
    seen_dates: list[str] = []

    def strategy(history: pd.DataFrame, state: dict[str, object]):
        seen_dates.append(history["date"].max().strftime("%Y-%m-%d"))
        return equal_weight_buy_and_hold(history, state)

    result = run_backtest(dataset, strategy, initial_cash=10_000, transaction_cost_bps=0)

    assert {fill["date"] for fill in result.fills} == {"2024-01-04"}
    assert {fill["price"] for fill in result.fills} == {110}
    assert {
        (item["date"], item["symbol"], item["purpose"])
        for item in result.unavailable_prices
    } == {("2024-01-03", "511260", "execution")}
    assert seen_dates == ["2024-01-02", "2024-01-03", "2024-01-04"]


def test_costs_and_slippage_reduce_baseline_equity(tmp_path: Path) -> None:
    bars_path = tmp_path / "bars.csv"
    _write_bars(bars_path)
    dataset = load_bars(bars_path, "total_return")

    free = run_backtest(
        dataset,
        equal_weight_buy_and_hold,
        initial_cash=10_000,
        transaction_cost_bps=0,
        slippage_bps=0,
    )
    charged = run_backtest(
        dataset,
        equal_weight_buy_and_hold,
        initial_cash=10_000,
        transaction_cost_bps=10,
        slippage_bps=20,
    )

    assert charged.ending_equity < free.ending_equity
    assert charged.metrics["transaction_costs"]["total"] > 0
    assert charged.metrics["turnover"] > 0
    expected_notional = 10_000 / (1.002 * 1.001)
    assert charged.metrics["turnover"] == pytest.approx(expected_notional / 10_000)
    assert charged.metrics["transaction_costs"]["fees"] == pytest.approx(
        expected_notional * 1.002 * 0.001
    )
    assert charged.metrics["transaction_costs"]["slippage"] == pytest.approx(
        expected_notional * 0.002
    )
    assert sum(
        fill["quantity"] * fill["price"] + fill["fee"] for fill in charged.fills
    ) == pytest.approx(10_000)
    assert all(point["cash"] >= 0 for point in charged.equity_curve)
    assert charged.equity_curve[1]["cash"] == pytest.approx(0, abs=1e-8)
    assert charged.ending_equity == pytest.approx(expected_notional * 1.21)


def test_metrics_on_known_gain_and_drawdown(tmp_path: Path) -> None:
    bars_path = tmp_path / "bars.csv"
    _write_bars(bars_path)
    bars = pd.read_csv(bars_path, dtype={"symbol": str})
    last_day = bars["date"] == "2024-01-04"
    bars.loc[last_day, "close"] = 99
    bars.loc[last_day, "low"] = 99
    bars.to_csv(bars_path, index=False)
    result = run_backtest(
        load_bars(bars_path, "total_return"),
        equal_weight_buy_and_hold,
        initial_cash=10_000,
        transaction_cost_bps=0,
    )

    metrics = result.metrics
    assert metrics["total_return"] == pytest.approx(-0.01)
    assert metrics["cagr"] == pytest.approx(0.99 ** (365.25 / 2) - 1)
    assert metrics["annualized_volatility"] == pytest.approx(math.sqrt(5.04))
    assert metrics["sharpe"] == pytest.approx(0, abs=1e-12)
    assert metrics["max_drawdown"] == pytest.approx(-0.1)
    assert metrics["calmar"] == pytest.approx(metrics["cagr"] / 0.1)
    assert metrics["yearly_returns"] == {"2024": pytest.approx(-0.01)}


def test_baseline_waits_for_later_listed_etf(tmp_path: Path) -> None:
    bars_path = tmp_path / "bars.csv"
    _write_bars(bars_path)
    bars = pd.read_csv(bars_path, dtype={"symbol": str})
    bars = bars.loc[
        (bars["symbol"] != "511260") | (bars["date"] == "2024-01-04")
    ]
    bars.to_csv(bars_path, index=False)
    dataset = load_bars(bars_path, "total_return")
    result = run_backtest(dataset, equal_weight_buy_and_hold, transaction_cost_bps=0)

    assert dataset.report["missing_symbols_by_date"]["2024-01-02"] == ["511260"]
    assert {fill["date"] for fill in result.fills} == {"2024-01-04"}
    assert ("2024-01-03", "511260", "execution") in {
        (item["date"], item["symbol"], item["purpose"])
        for item in result.unavailable_prices
    }


def test_missing_close_is_reported_and_uses_the_last_known_mark(tmp_path: Path) -> None:
    bars_path = tmp_path / "bars.csv"
    _write_bars(bars_path, unavailable_close=True)
    dataset = load_bars(bars_path, "total_return")

    result = run_backtest(
        dataset,
        equal_weight_buy_and_hold,
        initial_cash=10_000,
        transaction_cost_bps=0,
    )

    assert {
        (item["date"], item["symbol"], item["purpose"])
        for item in result.unavailable_prices
    } == {("2024-01-03", "511260", "mark")}


def test_zero_volume_bar_is_not_an_executable_open(tmp_path: Path) -> None:
    bars_path = tmp_path / "bars.csv"
    _write_bars(bars_path, zero_volume=True)
    dataset = load_bars(bars_path, "total_return")

    result = run_backtest(
        dataset,
        equal_weight_buy_and_hold,
        initial_cash=10_000,
        transaction_cost_bps=0,
    )

    assert {fill["date"] for fill in result.fills} == {"2024-01-04"}
    assert {
        (item["date"], item["symbol"], item["purpose"])
        for item in result.unavailable_prices
    } == {("2024-01-03", "511260", "execution")}


def test_strategy_weights_must_be_long_only_and_sum_to_one(tmp_path: Path) -> None:
    bars_path = tmp_path / "bars.csv"
    _write_bars(bars_path)
    dataset = load_bars(bars_path, "total_return")

    def invalid_strategy(history: pd.DataFrame, state: dict[str, object]):
        return {"511260": 1.1}

    with pytest.raises(StrategyError, match="between 0 and 1"):
        run_backtest(dataset, invalid_strategy, initial_cash=10_000)


def test_first_missing_close_uses_open_then_last_known_mark(tmp_path: Path) -> None:
    bars_path = tmp_path / "bars.csv"
    _write_bars(bars_path)
    bars = pd.read_csv(bars_path, dtype={"symbol": str})
    bars.loc[bars["symbol"] == "511260", "close"] = float("nan")
    bars.loc[
        (bars["date"] == "2024-01-04") & (bars["symbol"] == "511260"), "open"
    ] = float("nan")
    bars.to_csv(bars_path, index=False)

    result = run_backtest(
        load_bars(bars_path, "total_return"),
        equal_weight_buy_and_hold,
        initial_cash=10_000,
        transaction_cost_bps=0,
    )

    assert result.equity_curve[1]["equity"] == 10_800
    assert result.equity_curve[2]["equity"] == 11_680
    assert ("2024-01-03", "511260", "mark_open_fallback") in {
        (item["date"], item["symbol"], item["purpose"])
        for item in result.unavailable_prices
    }


def test_benchmark_relative_returns_require_matching_interval_and_data(
    tmp_path: Path,
) -> None:
    bars_path = tmp_path / "bars.csv"
    _write_bars(bars_path)
    dataset = load_bars(bars_path, "total_return")
    benchmark = run_backtest(
        dataset, equal_weight_buy_and_hold, initial_cash=10_000, transaction_cost_bps=0
    )
    cash = run_backtest(
        dataset, lambda history, state: {"CASH": 1.0},
        initial_cash=10_000, transaction_cost_bps=0,
    )

    relative = benchmark_relative_returns(cash, benchmark)
    assert relative["total_return"] == pytest.approx(-0.21)
    assert relative["yearly_returns"]["2024"] == pytest.approx(-0.21)
    shorter = run_backtest(
        dataset, equal_weight_buy_and_hold, end_date="2024-01-03"
    )
    with pytest.raises(ValueError, match="same dataset and sessions"):
        benchmark_relative_returns(cash, shorter)

    _write_bars(bars_path, zero_volume=True)
    other_dataset = load_bars(bars_path, "total_return")
    other = run_backtest(other_dataset, equal_weight_buy_and_hold)
    with pytest.raises(ValueError, match="same dataset and sessions"):
        benchmark_relative_returns(cash, other)


@pytest.mark.parametrize(
    "weights",
    [
        {"511260": -0.1, "CASH": 1.1},
        {"511260": float("nan"), "CASH": 1.0},
        {"511260": 0.4, "CASH": 0.4},
        {"UNKNOWN": 1.0},
    ],
)
def test_invalid_weight_variants_are_rejected(
    tmp_path: Path, weights: dict[str, float]
) -> None:
    bars_path = tmp_path / "bars.csv"
    _write_bars(bars_path)
    with pytest.raises(StrategyError):
        run_backtest(
            load_bars(bars_path, "total_return"), lambda history, state: weights
        )
