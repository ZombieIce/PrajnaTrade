import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from prajnatrade.data import load_bars

SYMBOLS = ("511260", "518880", "515080", "513300", "159612")


def _write_bars(path: Path, mixed_basis: bool = False) -> None:
    headers = "date,symbol,open,high,low,close,volume"
    if mixed_basis:
        headers += ",price_basis"
    lines = [headers]
    for day, open_price, close_price in (
        ("2024-01-02", "48", 50),
        ("2024-01-03", "100", 110),
        ("2024-01-04", "110", 121),
    ):
        for symbol in SYMBOLS:
            if day == "2024-01-03" and symbol == "513300":
                continue
            row_open = "" if day == "2024-01-03" and symbol == "515080" else open_price
            row = f"{day},{symbol},{row_open},{close_price},{row_open},{close_price},1000"
            if mixed_basis:
                basis = "raw" if day == "2024-01-03" and symbol == "511260" else "total_return"
                row += f",{basis}"
            lines.append(row)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        (str(Path(__file__).parents[1] / "src"), env.get("PYTHONPATH", ""))
    )
    return subprocess.run(
        [sys.executable, "-m", "prajnatrade", *args],
        capture_output=True,
        text=True,
        env=env,
    )


def test_data_validate_reports_missing_bars_and_unavailable_opens(
    tmp_path: Path,
) -> None:
    bars = tmp_path / "bars.csv"
    _write_bars(bars)

    completed = _run_cli(
        "data",
        "validate",
        "--data",
        str(bars),
        "--price-basis",
        "total_return",
        "--data-kind",
        "synthetic",
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["valid"] is True
    assert result["data_kind"] == "synthetic"
    assert result["missing_symbols_by_date"]["2024-01-03"] == ["513300"]
    assert result["unavailable_opens"] == 1
    assert result["calendar_validation"]["status"] == "not_checked"


def test_data_validate_checks_entirely_missing_dates_against_calendar(
    tmp_path: Path,
) -> None:
    bars = tmp_path / "bars.csv"
    calendar = tmp_path / "calendar.csv"
    _write_bars(bars)
    calendar.write_text(
        "date\n2024-01-02\n2024-01-03\n2024-01-05\n", encoding="utf-8"
    )

    completed = _run_cli(
        "data", "validate", "--data", str(bars), "--price-basis", "total_return",
        "--data-kind", "synthetic", "--calendar", str(calendar),
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["calendar_validation"] == {
        "status": "checked",
        "missing_dates": ["2024-01-05"],
        "unexpected_dates": ["2024-01-04"],
    }


def test_data_validate_rejects_duplicate_calendar_dates(tmp_path: Path) -> None:
    bars = tmp_path / "bars.csv"
    calendar = tmp_path / "calendar.csv"
    _write_bars(bars)
    calendar.write_text("date\n2024-01-02\n2024-01-02\n", encoding="utf-8")

    completed = _run_cli(
        "data", "validate", "--data", str(bars), "--price-basis", "total_return",
        "--data-kind", "synthetic", "--calendar", str(calendar),
    )

    assert completed.returncode == 2
    assert "duplicate" in completed.stderr.lower()


def test_data_validate_rejects_mixed_price_bases(tmp_path: Path) -> None:
    bars = tmp_path / "mixed.csv"
    _write_bars(bars, mixed_basis=True)

    completed = _run_cli(
        "data",
        "validate",
        "--data",
        str(bars),
        "--price-basis",
        "total_return",
        "--data-kind",
        "real",
    )

    assert completed.returncode == 2
    assert "Mixed or mismatched price basis" in completed.stderr


def test_data_validate_reads_parquet_bars(tmp_path: Path) -> None:
    csv_path = tmp_path / "bars.csv"
    parquet_path = tmp_path / "bars.parquet"
    _write_bars(csv_path)
    pd.read_csv(csv_path).to_parquet(parquet_path, index=False)

    completed = _run_cli(
        "data",
        "validate",
        "--data",
        str(parquet_path),
        "--price-basis",
        "total_return",
        "--data-kind",
        "synthetic",
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["rows"] == 14


def test_data_validate_rejects_duplicate_and_invalid_ohlc_rows(
    tmp_path: Path,
) -> None:
    bars = tmp_path / "duplicate.csv"
    _write_bars(bars)
    lines = bars.read_text(encoding="utf-8").splitlines()
    lines.append(lines[1])
    bars.write_text("\n".join(lines) + "\n", encoding="utf-8")

    completed = _run_cli(
        "data",
        "validate",
        "--data",
        str(bars),
        "--price-basis",
        "total_return",
        "--data-kind",
        "synthetic",
    )

    assert completed.returncode == 2
    assert "Duplicate date/symbol bars" in completed.stderr

    lines.pop()
    values = lines[1].split(",")
    values[3] = "40"
    lines[1] = ",".join(values)
    bars.write_text("\n".join(lines) + "\n", encoding="utf-8")
    invalid_bar = _run_cli(
        "data",
        "validate",
        "--data",
        str(bars),
        "--price-basis",
        "total_return",
        "--data-kind",
        "synthetic",
    )
    assert invalid_bar.returncode == 2
    assert "high is below open/close" in invalid_bar.stderr


@pytest.mark.parametrize("column", ["open", "high", "low", "close", "volume"])
def test_data_validate_rejects_nonfinite_values(tmp_path: Path, column: str) -> None:
    bars = tmp_path / "nonfinite.csv"
    _write_bars(bars)
    lines = bars.read_text(encoding="utf-8").splitlines()
    values = lines[1].split(",")
    values[lines[0].split(",").index(column)] = "inf"
    lines[1] = ",".join(values)
    bars.write_text("\n".join(lines) + "\n", encoding="utf-8")

    completed = _run_cli(
        "data", "validate", "--data", str(bars), "--price-basis", "total_return",
        "--data-kind", "synthetic",
    )

    assert completed.returncode == 2
    assert f"Column '{column}' must contain finite values" in completed.stderr


def test_data_fingerprint_distinguishes_close_floating_point_values(tmp_path: Path) -> None:
    bars = tmp_path / "bars.csv"
    _write_bars(bars)
    original = load_bars(bars, "total_return").data_hash
    text = bars.read_text(encoding="utf-8").replace(
        "2024-01-03,511260,100,110,100,110,1000",
        "2024-01-03,511260,100.00000000001,110,100,110,1000",
    )
    bars.write_text(text, encoding="utf-8")
    assert load_bars(bars, "total_return").data_hash != original
