import json
from pathlib import Path

import pytest

from cli_helpers import cli

SYMBOLS = ("511260", "518880", "515080", "513300", "159612")


def _write_bars(path: Path) -> None:
    lines = ["date,symbol,open,high,low,close,volume,price_basis"]
    prices = (
        ("2024-01-02", 48, 50),
        ("2024-01-03", 100, 110),
        ("2024-01-04", 110, 121),
    )
    for day, open_price, close_price in prices:
        for symbol in SYMBOLS:
            lines.append(
                f"{day},{symbol},{open_price},{close_price},{open_price},"
                f"{close_price},1000,total_return"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_baseline_cli_buys_at_the_next_open_and_reports_reproducible_value(
    tmp_path: Path,
) -> None:
    bars = tmp_path / "bars.csv"
    output = tmp_path / "baseline.json"
    _write_bars(bars)
    arguments = (
        "baseline",
        "--data",
        str(bars),
        "--output",
        str(output),
        "--initial-cash",
        "10000",
        "--price-basis",
        "total_return",
        "--data-kind",
        "synthetic",
        "--transaction-cost-bps",
        "0",
        "--slippage-bps",
        "0",
    )

    completed = cli(*arguments)
    assert completed.returncode == 0, completed.stderr

    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["data_kind"] == "synthetic"
    assert result["ending_equity"] == 12100
    assert abs(result["metrics"]["total_return"] - 0.21) < 1e-12
    assert result["metrics"]["turnover"] == 1
    assert {fill["date"] for fill in result["fills"]} == {"2024-01-03"}
    assert {fill["price"] for fill in result["fills"]} == {100}
    assert result["relative_benchmark"] == "CASH"
    assert result["relative_returns"]["total_return"] == pytest.approx(0.21)
    assert result["relative_returns"]["yearly_returns"] == {
        "2024": pytest.approx(0.21)
    }
    repeat = cli(*arguments)
    assert repeat.returncode == 0, repeat.stderr
    assert json.loads(output.read_text(encoding="utf-8")) == result
