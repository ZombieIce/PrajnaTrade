import json
import os
import subprocess
import sys
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path

import pytest

from prajnatrade.data import ETF_SYMBOLS


SYMBOLS = ETF_SYMBOLS


def _snapshot_input() -> dict:
    dates = [(date(2024, 1, 1) + timedelta(days=i)).isoformat() for i in range(12)]
    evidence = {
        "available_at": "2024-01-01T00:00:00+08:00",
        "availability_evidence": "synthetic release schedule",
        "collected_at": "2024-02-01T00:00:00+08:00",
    }
    return {
        "schema_version": 1,
        "data_kind": "synthetic",
        "sources": {
            name: {
                "source": "synthetic",
                "version": "1",
                "coverage_start": dates[0],
                "coverage_end": dates[-1],
                **evidence,
            }
            for name in ("calendar", "raw_bars", "corporate_actions", "derived")
        },
        "symbol_coverage": {
            symbol: {
                "start": dates[1] if symbol == SYMBOLS[-1] else dates[0],
                "end": dates[-1],
            }
            for symbol in SYMBOLS
        },
        "derivation_rule_version": "synthetic-total-return-v1",
        "calendar": [{"date": day, **evidence} for day in dates],
        "raw_bars": [
            {
                "date": day,
                "symbol": symbol,
                "open": 10,
                "high": 10,
                "low": 10,
                "close": 10,
                "volume": 1000,
                "trading_status": "trading",
                **evidence,
            }
            for day in dates
            for symbol in SYMBOLS
        ],
        "corporate_actions": [],
        "derived": [
            {"date": day, "symbol": symbol, "value": 10 + index / 10, **evidence}
            for index, day in enumerate(dates)
            for symbol in SYMBOLS
        ],
    }


def _cli(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    return subprocess.run(
        [sys.executable, "-m", "prajnatrade", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )


def _run_baseline(tmp_path: Path, payload: dict, name: str) -> dict:
    source = tmp_path / f"{name}.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    store = tmp_path / f"{name}-store"
    sealed = _cli(tmp_path, "snapshot", "seal", "--input", str(source), "--store", str(store))
    assert sealed.returncode == 0, sealed.stderr
    snapshot_id = json.loads(sealed.stdout)["snapshot_id"]
    output = tmp_path / f"{name}-baseline.json"
    completed = _cli(
        tmp_path,
        "baseline",
        "--store",
        str(store),
        "--snapshot-id",
        snapshot_id,
        "--output",
        str(output),
        "--initial-cash",
        "5000",
        "--transaction-cost-bps",
        "0",
        "--slippage-bps",
        "0",
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(output.read_text(encoding="utf-8"))


def test_snapshot_baseline_accounts_for_dividend_receivable_payment_and_split(
    tmp_path: Path,
) -> None:
    payload = _snapshot_input()
    first_discovery = "2024-01-02"
    execution_day = "2024-01-03"
    ex_date = "2024-01-04"
    adjustment_date = "2024-01-05"
    pay_date = "2024-01-06"
    for row in payload["raw_bars"]:
        if row["date"] >= ex_date and row["symbol"] == SYMBOLS[0]:
            row.update(open=9.8, high=9.8, low=9.8, close=9.8)
        if row["date"] == execution_day and row["symbol"] == SYMBOLS[1]:
            row.update(close=9.8, low=9.8)
        if row["date"] >= adjustment_date and row["symbol"] == SYMBOLS[1]:
            row.update(open=5, high=5, low=5, close=5)
    evidence = {
        "available_at": "2024-01-01T00:00:00+08:00",
        "availability_evidence": "synthetic announcement",
        "collected_at": "2024-02-01T00:00:00+08:00",
    }
    payload["corporate_actions"] = [
        {
            "date": ex_date,
            "symbol": SYMBOLS[0],
            "type": "cash_dividend",
            "cash_per_share": 0.2,
            "pay_date": pay_date,
            "terms_evidence": "synthetic dividend terms",
            **evidence,
        },
        {
            "date": execution_day,
            "symbol": SYMBOLS[1],
            "type": "cash_dividend",
            "cash_per_share": 0.2,
            "pay_date": pay_date,
            "terms_evidence": "synthetic dividend terms",
            **evidence,
        },
        {
            "date": adjustment_date,
            "symbol": SYMBOLS[1],
            "type": "share_adjustment",
            "ratio": 2,
            "terms_evidence": "synthetic split terms",
            **evidence,
        },
    ]

    result = _run_baseline(tmp_path, payload, "actions")
    assert result["signals"][0]["date"] == first_discovery
    assert result["signals"][0]["execution_date"] == execution_day
    assert len(result["order_attempts"]) == 1
    assert result["order_attempts"][0]["attempts"] == 1
    assert len(result["fills"]) == len(SYMBOLS)

    ex_day = next(row for row in result["equity_curve"] if row["date"] == ex_date)
    entitled = ex_day["holdings"][SYMBOLS[0]]
    assert entitled["quantity"] == 100
    assert entitled["market_value"] == pytest.approx(980)
    assert ex_day["dividend_receivables"] == 20
    assert entitled["market_value"] + ex_day["receivables"][0]["amount"] == pytest.approx(1000)
    assert ex_day["nav"] == 5000
    assert all(item["symbol"] != SYMBOLS[1] for item in ex_day["receivables"])

    adjusted = next(row for row in result["equity_curve"] if row["date"] == adjustment_date)
    assert adjusted["holdings"][SYMBOLS[1]]["quantity"] == 200
    assert adjusted["holdings"][SYMBOLS[1]]["market_value"] == 1000
    paid = next(row for row in result["equity_curve"] if row["date"] == pay_date)
    assert paid["cash"] == 20
    assert paid["dividend_receivables"] == 0
    assert paid["nav"] == 5000

    repeated = _run_baseline(tmp_path, payload, "actions-repeat")
    for key in ("signals", "order_attempts", "fills", "equity_curve", "gaps", "metrics"):
        assert repeated[key] == result[key]


def test_snapshot_baseline_attempts_missing_open_once_without_deferral(tmp_path: Path) -> None:
    payload = _snapshot_input()
    execution_date = "2024-01-03"
    payload["raw_bars"] = [
        row for row in payload["raw_bars"]
        if not (row["date"] == execution_date and row["symbol"] == SYMBOLS[-1])
    ]

    result = _run_baseline(tmp_path, payload, "missing-open")
    assert result["order_attempts"] == [{
        "signal_date": "2024-01-02",
        "date": execution_date,
        "attempts": 1,
        "status": "not_filled",
        "reason": "missing_or_unconfirmed_open",
        "missing_symbols": [SYMBOLS[-1]],
    }]
    assert result["fills"] == []
    assert result["equity_curve"][2]["cash"] == 5000
    assert result["gaps"]["raw_bars"]


def test_snapshot_baseline_rejects_open_not_available_by_execution_time(
    tmp_path: Path,
) -> None:
    payload = _snapshot_input()
    row = next(
        row for row in payload["raw_bars"]
        if row["date"] == "2024-01-03" and row["symbol"] == SYMBOLS[-1]
    )
    row["available_at"] = "2024-01-03T09:31:00+08:00"

    result = _run_baseline(tmp_path, payload, "late-open")

    assert result["order_attempts"][0]["status"] == "not_filled"
    assert result["order_attempts"][0]["missing_symbols"] == [SYMBOLS[-1]]
    assert result["fills"] == []


def test_snapshot_baseline_does_not_backdate_late_close_or_action_evidence(
    tmp_path: Path,
) -> None:
    payload = _snapshot_input()
    day = "2024-01-04"
    close_row = next(
        row for row in payload["raw_bars"]
        if row["date"] == day and row["symbol"] == SYMBOLS[0]
    )
    close_row["available_at"] = "2024-01-05T00:01:00+08:00"
    payload["corporate_actions"] = [{
        "date": day,
        "symbol": SYMBOLS[1],
        "type": "cash_dividend",
        "cash_per_share": 0.2,
        "pay_date": "2024-01-06",
        "terms_evidence": "synthetic dividend terms",
        "available_at": "2024-01-04T09:31:00+08:00",
        "availability_evidence": "synthetic announcement",
        "collected_at": "2024-02-01T00:00:00+08:00",
    }]

    result = _run_baseline(tmp_path, payload, "late-evidence")

    day_point = next(row for row in result["equity_curve"] if row["date"] == day)
    assert day_point["nav"] is None
    assert any(
        gap["date"] == day and gap["reason"] == "missing_or_unconfirmed_unadjusted_close"
        for gap in result["gaps"]["valuation"]
    )
    assert any(
        gap["date"] == day
        and gap["reason"] == "corporate_action_terms_or_availability_unknown"
        for gap in result["gaps"]["valuation"]
    )


def test_snapshot_baseline_does_not_emit_signal_for_a_derived_gap(tmp_path: Path) -> None:
    payload = _snapshot_input()
    payload["derived"] = [
        row for row in payload["derived"]
        if not (row["date"] == "2024-01-02" and row["symbol"] == SYMBOLS[0])
    ]

    result = _run_baseline(tmp_path, payload, "derived-gap")
    assert result["signal_gaps"][0] == {
        "date": "2024-01-02",
        "missing_symbols": [SYMBOLS[0]],
    }
    assert result["signals"][0]["date"] == "2024-01-03"
    assert result["signals"][0]["execution_date"] == "2024-01-04"


def test_snapshot_baseline_skips_signal_not_available_by_next_open(
    tmp_path: Path,
) -> None:
    payload = _snapshot_input()
    row = next(
        row for row in payload["derived"]
        if row["date"] == "2024-01-02" and row["symbol"] == SYMBOLS[0]
    )
    row["available_at"] = "2024-01-03T09:31:00+08:00"

    result = _run_baseline(tmp_path, payload, "late-signal")

    assert result["signal_gaps"][0] == {
        "date": "2024-01-02",
        "missing_symbols": [SYMBOLS[0]],
        "reason": "total_return_value_not_available_before_next_open",
    }
    assert result["signals"][0]["date"] == "2024-01-03"
    assert result["signals"][0]["execution_date"] == "2024-01-04"


def test_snapshot_baseline_missing_close_has_no_nav_and_future_changes_do_not_leak(
    tmp_path: Path,
) -> None:
    payload = _snapshot_input()
    missing_date = "2024-01-04"
    for row in payload["raw_bars"]:
        if row["date"] == missing_date and row["symbol"] == SYMBOLS[0]:
            row["close"] = None
    original = _run_baseline(tmp_path, payload, "missing-close")
    missing_nav = next(row for row in original["equity_curve"] if row["date"] == missing_date)
    assert missing_nav["nav"] is None
    assert any(gap["date"] == missing_date for gap in original["gaps"]["valuation"])

    changed = deepcopy(payload)
    for row in changed["raw_bars"]:
        if row["date"] == "2024-01-12" and row["symbol"] == SYMBOLS[0]:
            row.update(open=11, high=12, low=11, close=12)
    for row in changed["derived"]:
        if row["date"] == "2024-01-12" and row["symbol"] == SYMBOLS[0]:
            row["value"] = 99
    future_variant = _run_baseline(tmp_path, changed, "future-change")
    discovery_end = "2024-01-07"
    assert original["snapshot_id"] != future_variant["snapshot_id"]
    for key in ("signals", "fills"):
        assert original[key] == future_variant[key]
    assert [
        row for row in original["equity_curve"] if row["date"] <= discovery_end
    ] == [
        row for row in future_variant["equity_curve"] if row["date"] <= discovery_end
    ]


def test_snapshot_baseline_does_not_mark_held_etf_from_suspended_close(
    tmp_path: Path,
) -> None:
    payload = _snapshot_input()
    row = next(
        row for row in payload["raw_bars"]
        if row["date"] == "2024-01-04" and row["symbol"] == SYMBOLS[0]
    )
    row["trading_status"] = "suspended"

    result = _run_baseline(tmp_path, payload, "suspended-close")

    point = next(row for row in result["equity_curve"] if row["date"] == "2024-01-04")
    assert point["nav"] is None
    assert any(
        gap["date"] == "2024-01-04"
        and gap["symbol"] == SYMBOLS[0]
        and gap["reason"] == "missing_or_unconfirmed_unadjusted_close"
        for gap in result["gaps"]["valuation"]
    )
