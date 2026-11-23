"""Public-CLI coverage for schema-version-2 real snapshots and the real baseline.

The fixtures are synthetic JSON labelled ``data_kind: "real"``: they exercise the
mechanism (evidence block, per-field availability, halt statuses, derivation
declaration), not real market data.  Nothing here seals or claims a real
snapshot.
"""

import json
import subprocess
from datetime import date, timedelta, timezone
from pathlib import Path

import pytest

from cli_helpers import cli
from prajnatrade.data import ETF_SYMBOLS

SYMBOLS = ETF_SYMBOLS
SESSIONS = 15
START = date(2024, 1, 2)
CN_TZ = timezone(timedelta(hours=8))
HALT_INDEX = 5
SUSPENDED_INDEX = 7
COLLECTED_AT = "2024-02-01T00:00:00+08:00"
CALENDAR_AVAILABLE_AT = "2024-01-01T00:00:00+08:00"
DERIVATION = {
    "rule_version": "tr-reinvest-exclose-v1",
    "method": "cash_dividend_reinvested_at_ex_date_close",
    "evidence": "synthetic:official-dividends+unadjusted-closes",
}
KINDS = ("calendar", "raw_bars", "corporate_actions", "derived")


def _day(index: int) -> str:
    return (START + timedelta(days=index)).isoformat()


def _at(day: str, hour: int, minute: int = 0) -> str:
    return f"{day}T{hour:02d}:{minute:02d}:00+08:00"


def _evidence_items() -> dict:
    return {
        "items": [
            {
                "id": "calendar-2024",
                "kind": "trading_calendar",
                "reference": "synthetic:szse-monthly-calendar@2024-01",
                "sha256": "a" * 64,
                "available_at": CALENDAR_AVAILABLE_AT,
            },
            {
                "id": "prices-2024",
                "kind": "price_archive",
                "reference": "synthetic:tdx-g4day@2024",
                "sha256": "b" * 64,
                "available_at": "2024-12-31T23:59:59+08:00",
            },
        ]
    }


def _row_evidence(day: str) -> dict:
    return {
        "available_at": _at(day, 15, 5),
        "availability_evidence": f"synthetic:tdx:{day}:close-batch",
        "collected_at": COLLECTED_AT,
    }


def _real_input() -> dict:
    days = [_day(index) for index in range(SESSIONS)]
    sources = {
        name: {
            "source": f"synthetic-real-{name}",
            "version": "2",
            "coverage_start": days[0],
            "coverage_end": days[-1],
            "available_at": CALENDAR_AVAILABLE_AT,
            "availability_evidence": "synthetic:szse-monthly-calendar@2024-01",
            "collected_at": COLLECTED_AT,
        }
        for name in KINDS
    }
    calendar = [
        {
            "date": day,
            "available_at": CALENDAR_AVAILABLE_AT,
            "availability_evidence": "synthetic:szse-monthly-calendar@2024-01",
            "collected_at": COLLECTED_AT,
        }
        for day in days
    ]
    raw_bars = []
    derived = []
    for index, day in enumerate(days):
        for symbol in SYMBOLS:
            row = {
                "date": day,
                "symbol": symbol,
                "open": 10.0,
                "high": 10.0,
                "low": 10.0,
                "close": 10.0,
                "volume": 1000,
                "trading_status": "trading",
                "open_available_at": _at(day, 9, 30),
                "close_available_at": _at(day, 15, 5),
                **_row_evidence(day),
            }
            value = 10.0 + index / 10.0
            if symbol == SYMBOLS[-1] and index == HALT_INDEX:
                row.update(
                    trading_status="partial_halt",
                    open=None,
                    high=10.5,
                    low=10.0,
                    close=10.4,
                    volume=400,
                    open_available_at=_at(day, 10, 30),
                    halt={"start": _at(day, 9, 30), "end": _at(day, 10, 30)},
                )
            elif symbol == SYMBOLS[-1] and index == SUSPENDED_INDEX:
                row.update(
                    trading_status="suspended",
                    open=None,
                    high=None,
                    low=None,
                    close=None,
                    volume=None,
                    open_available_at=None,
                    close_available_at=None,
                    halt={"start": _at(day, 9, 30), "end": _at(day, 15, 0)},
                )
                value = None
            raw_bars.append(row)
            derived.append(
                {
                    "date": day,
                    "symbol": symbol,
                    "value": value,
                    "available_at": _at(day, 15, 5),
                    "availability_evidence": f"synthetic:derivation:{day}",
                    "collected_at": COLLECTED_AT,
                }
            )
    return {
        "schema_version": 2,
        "data_kind": "real",
        "raw_price_basis": "unadjusted",
        "sources": sources,
        "symbol_coverage": {
            symbol: {"start": days[0], "end": days[-1]} for symbol in SYMBOLS
        },
        "derivation_rule_version": DERIVATION["rule_version"],
        "evidence": _evidence_items(),
        "derivation": dict(DERIVATION),
        "calendar": calendar,
        "raw_bars": raw_bars,
        "corporate_actions": [],
        "derived": derived,
    }


def _cli(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return cli(*args, cwd=cwd)


def _snapshot_command(
    tmp_path: Path,
    *args: str,
    payload: dict | None = None,
    store_name: str = "store",
) -> dict:
    if payload is not None:
        path = tmp_path / "input.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        args = (*args, "--input", str(path))
    result = _cli(tmp_path, "snapshot", *args, "--store", str(tmp_path / store_name))
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_real_snapshot_seals_schema_v2_manifest_and_reads_data_kind(
    tmp_path: Path,
) -> None:
    payload = _real_input()
    receipt = _snapshot_command(tmp_path, "seal", payload=payload)
    assert receipt == {
        "schema_version": 2,
        "snapshot_id": receipt["snapshot_id"],
        "data_kind": "real",
    }

    manifest = _snapshot_command(tmp_path, "manifest", "--id", receipt["snapshot_id"])
    assert manifest["schema_version"] == 2
    assert manifest["data_kind"] == "real"
    assert manifest["raw_price_basis"] == "unadjusted"
    assert manifest["evidence"] == payload["evidence"]
    assert manifest["derivation"] == payload["derivation"]
    assert manifest["symbols"] == sorted(SYMBOLS)
    assert manifest["common_range"] == {"start": _day(0), "end": _day(SESSIONS - 1)}
    assert [
        (item["name"], item["sessions"], item["start"], item["end"])
        for item in manifest["partitions"]
    ] == [
        ("discovery", 9, _day(0), _day(8)),
        ("validation", 3, _day(9), _day(11)),
        ("final_holdout", 3, _day(12), _day(14)),
    ]
    assert all(len(item["content_hash"]) == 64 for item in manifest["partitions"])
    assert set(manifest["content_hashes"]) == set(KINDS)
    assert manifest["gaps"]["raw_bars"] == [
        {"date": _day(HALT_INDEX), "symbol": SYMBOLS[-1]},
        {"date": _day(SUSPENDED_INDEX), "symbol": SYMBOLS[-1]},
    ]
    assert manifest["gaps"]["derived"] == [
        {"date": _day(SUSPENDED_INDEX), "symbol": SYMBOLS[-1]}
    ]

    read = _snapshot_command(
        tmp_path, "read", "--id", receipt["snapshot_id"], "--partition", "discovery"
    )
    assert read["schema_version"] == 2
    assert read["data_kind"] == "real"
    rows = {
        (row["date"], row["symbol"]): row
        for row in read["data"]["raw_bars"]
    }
    row = rows[(_day(1), SYMBOLS[0])]
    assert row["open_available_at"] == _at(_day(1), 9, 30)
    assert row["close_available_at"] == _at(_day(1), 15, 5)
    halted = rows[(_day(HALT_INDEX), SYMBOLS[-1])]
    assert halted["trading_status"] == "partial_halt"
    assert halted["open"] is None
    assert halted["halt"]["start"] == _at(_day(HALT_INDEX), 9, 30)

    repeated = _snapshot_command(tmp_path, "seal", payload=payload)
    assert repeated == receipt


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


def test_real_baseline_reports_data_kind_and_fills_at_per_field_open(
    tmp_path: Path,
) -> None:
    # Every row's single available_at is the 15:05 close publication; the fill
    # must come from open_available_at (09:30), which is the authoritative field.
    result = _run_baseline(tmp_path, _real_input(), "real-fill")
    assert result["data_kind"] == "real"
    assert result["signals"][0]["date"] == _day(0)
    assert result["signals"][0]["execution_date"] == _day(1)
    assert result["order_attempts"] == [
        {
            "signal_date": _day(0),
            "date": _day(1),
            "attempts": 1,
            "status": "filled",
        }
    ]
    assert len(result["fills"]) == len(SYMBOLS)
    assert {fill["date"] for fill in result["fills"]} == {_day(1)}
    assert {fill["raw_open"] for fill in result["fills"]} == {10.0}


def _raw_row(payload: dict, day: str, symbol: str) -> dict:
    return next(
        row
        for row in payload["raw_bars"]
        if row["date"] == day and row["symbol"] == symbol
    )


def _make_partial_halt(row: dict, day: str) -> None:
    row.update(
        trading_status="partial_halt",
        open=None,
        high=10.0,
        low=10.0,
        close=10.0,
        volume=500,
        open_available_at=_at(day, 10, 30),
        halt={"start": _at(day, 9, 30), "end": _at(day, 10, 30)},
    )


def test_real_baseline_partial_halt_blocks_the_attempt_without_deferral(
    tmp_path: Path,
) -> None:
    payload = _real_input()
    _make_partial_halt(_raw_row(payload, _day(1), SYMBOLS[0]), _day(1))

    result = _run_baseline(tmp_path, payload, "real-partial-open")

    assert result["order_attempts"] == [
        {
            "signal_date": _day(0),
            "date": _day(1),
            "attempts": 1,
            "status": "not_filled",
            "reason": "missing_or_unconfirmed_open",
            "missing_symbols": [SYMBOLS[0]],
        }
    ]
    assert result["fills"] == []
    # The single attempt is never retried on a later session.
    assert all(point["cash"] == 5000 for point in result["equity_curve"])


def test_real_baseline_open_after_0930_blocks_fill_despite_early_available_at(
    tmp_path: Path,
) -> None:
    payload = _real_input()
    day = _day(1)
    row = _raw_row(payload, day, SYMBOLS[1])
    row["available_at"] = _at(day, 9, 30)
    row["open_available_at"] = _at(day, 9, 31)

    result = _run_baseline(tmp_path, payload, "real-late-open")

    assert result["order_attempts"][0]["status"] == "not_filled"
    assert result["order_attempts"][0]["missing_symbols"] == [SYMBOLS[1]]
    assert result["fills"] == []


def test_real_baseline_marks_partial_halt_held_day_at_its_confirmed_close(
    tmp_path: Path,
) -> None:
    result = _run_baseline(tmp_path, _real_input(), "real-partial-close")
    day = _day(HALT_INDEX)
    point = next(row for row in result["equity_curve"] if row["date"] == day)
    assert point["holdings"][SYMBOLS[-1]]["close"] == 10.4
    assert point["nav"] == pytest.approx(5040.0)
    assert not any(
        gap["date"] == day for gap in result["gaps"]["valuation"]
    )
    raw_gap = next(
        gap for gap in result["gaps"]["raw_bars"]
        if gap["date"] == day and gap["symbol"] == SYMBOLS[-1]
    )
    assert raw_gap["trading_status"] == "partial_halt"
    assert raw_gap["reason"] == "partial_halt_no_executable_open"
    assert raw_gap["halt"] == {
        "start": _at(day, 9, 30),
        "end": _at(day, 10, 30),
    }


def test_real_campaign_copies_only_discovery_and_uses_field_availability(tmp_path: Path) -> None:
    payload = _real_input()
    payload["raw_bars"][0]["availability_evidence"] = "fixture:收盘行情"
    sealed = _snapshot_command(tmp_path, "seal", payload=payload)
    snapshot_id = sealed["snapshot_id"]
    store = tmp_path / "store"
    snapshot = store / snapshot_id
    snapshot.chmod(0o755)
    for partition in ("validation", "final_holdout"):
        (snapshot / f"{partition}.json").unlink()
    project = tmp_path / "strategy"
    assert _cli(tmp_path, "project", "init", "--project", str(project)).returncode == 0
    created = _cli(
        tmp_path, "campaign", "create", "--project", str(project), "--store", str(store),
        "--snapshot-id", snapshot_id, "--attempts", "1", "--max-seconds", "30",
    )
    assert created.returncode == 0, created.stderr
    campaign = json.loads(created.stdout)
    assert campaign["data_kind"] == "real"
    store.rename(tmp_path / "detached")
    request = tmp_path / "request.json"
    request.write_text(json.dumps({
        "schema_version": 1, "campaign_id": campaign["campaign_id"], "submission_id": "first",
        "hypothesis": "Equal weights match the comparator.",
        "strategy": {"family": "equal_weight"}, "parameters": {},
    }), encoding="utf-8")
    submitted = cli(
        "experiment", "submit", "--project", str(project), "--input", str(request),
        cwd=tmp_path, agent=True,
    )
    assert submitted.returncode == 0, submitted.stderr
    receipt = json.loads(submitted.stdout)
    assert receipt["data_kind"] == "real"
    assert receipt["fingerprints"]["snapshot"] == campaign["fingerprints"]["snapshot"]
    discovery = receipt["discovery"]
    benchmark = discovery["baseline_comparison"]["baseline"]
    for result in (discovery, benchmark):
        assert len(result["fills"]) == len(SYMBOLS)
        assert all(fill["date"] == _day(1) for fill in result["fills"])
        nav = {point["date"]: point["nav"] for point in result["equity_curve"]}
        assert nav[_day(HALT_INDEX)] is not None
        assert nav[_day(SUSPENDED_INDEX)] is None


def test_real_baseline_suspended_held_day_is_an_unknown_nav_gap(
    tmp_path: Path,
) -> None:
    result = _run_baseline(tmp_path, _real_input(), "real-suspended")
    day = _day(SUSPENDED_INDEX)
    point = next(row for row in result["equity_curve"] if row["date"] == day)
    assert point["nav"] is None
    gap = next(
        gap for gap in result["gaps"]["valuation"]
        if gap["date"] == day and gap["symbol"] == SYMBOLS[-1]
    )
    assert gap["reason"] == "missing_or_unconfirmed_unadjusted_close"
    assert gap["trading_status"] == "suspended"
    assert gap["status_reason"] == "suspended_no_confirmed_close"
    assert gap["halt"] == {
        "start": _at(day, 9, 30),
        "end": _at(day, 15, 0),
    }
    raw_gap = next(
        gap for gap in result["gaps"]["raw_bars"]
        if gap["date"] == day and gap["symbol"] == SYMBOLS[-1]
    )
    assert raw_gap["trading_status"] == "suspended"
    assert raw_gap["reason"] == "suspended_session"
    derived_gap = next(
        gap for gap in result["gaps"]["derived"]
        if gap["date"] == day and gap["symbol"] == SYMBOLS[-1]
    )
    assert derived_gap["trading_status"] == "suspended"
    assert derived_gap["reason"] == "suspended_no_confirmed_close"
    # The next session's confirmed close restores NAV; no stale mark is carried.
    recovered = next(
        row for row in result["equity_curve"] if row["date"] == _day(SUSPENDED_INDEX + 1)
    )
    assert recovered["nav"] == 5000


def test_real_baseline_close_availability_controls_nav(tmp_path: Path) -> None:
    payload = _real_input()
    day = _day(3)
    row = _raw_row(payload, day, SYMBOLS[0])
    row["close_available_at"] = _at(_day(4), 0, 1)

    result = _run_baseline(tmp_path, payload, "real-late-close")

    point = next(row for row in result["equity_curve"] if row["date"] == day)
    assert point["nav"] is None
    assert {
        "date": day,
        "symbol": SYMBOLS[0],
        "reason": "missing_or_unconfirmed_unadjusted_close",
    } in result["gaps"]["valuation"]




def _seal_result(
    tmp_path: Path, payload: dict, *, store_name: str = "rejected-store"
) -> subprocess.CompletedProcess[str]:
    source = tmp_path / "invalid.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    return _cli(
        tmp_path,
        "snapshot",
        "seal",
        "--input",
        str(source),
        "--store",
        str(tmp_path / store_name),
    )


def _real_cash_dividend(**overrides: object) -> dict:
    action = {
        "date": _day(3),
        "symbol": SYMBOLS[0],
        "type": "cash_dividend",
        "record_date": _day(2),
        "cash_per_share": 1.36,
        "pay_date": _day(5),
        "terms_evidence": "synthetic:511260-dividend@2024-01",
        "available_at": _at(_day(1), 23, 59),
        "availability_evidence": "synthetic:511260-dividend@2024-01",
        "collected_at": COLLECTED_AT,
    }
    action.update(overrides)
    return action


def _missing_evidence(payload: dict) -> None:
    del payload["evidence"]


def _bad_evidence_sha(payload: dict) -> None:
    payload["evidence"]["items"][0]["sha256"] = "x" * 64


def _naive_evidence_available_at(payload: dict) -> None:
    payload["evidence"]["items"][0]["available_at"] = "2024-01-01T00:00:00"


def _missing_open_available_at(payload: dict) -> None:
    del _raw_row(payload, _day(0), SYMBOLS[0])["open_available_at"]


def _missing_close_available_at(payload: dict) -> None:
    del _raw_row(payload, _day(0), SYMBOLS[0])["close_available_at"]


def _naive_open_available_at(payload: dict) -> None:
    _raw_row(payload, _day(0), SYMBOLS[0])["open_available_at"] = "2024-01-02T09:30:00"


def _trading_null_open(payload: dict) -> None:
    _raw_row(payload, _day(0), SYMBOLS[0])["open"] = None


def _partial_halt_with_open(payload: dict) -> None:
    _raw_row(payload, _day(HALT_INDEX), SYMBOLS[-1])["open"] = 10.0


def _partial_halt_without_halt(payload: dict) -> None:
    del _raw_row(payload, _day(HALT_INDEX), SYMBOLS[-1])["halt"]


def _suspended_with_price(payload: dict) -> None:
    _raw_row(payload, _day(SUSPENDED_INDEX), SYMBOLS[-1])["close"] = 10.0


def _action_null_terms(payload: dict) -> None:
    payload["corporate_actions"] = [_real_cash_dividend(cash_per_share=None)]


def _action_null_pay_date(payload: dict) -> None:
    payload["corporate_actions"] = [_real_cash_dividend(pay_date=None)]


def _missing_derivation(payload: dict) -> None:
    del payload["derivation"]


def _wrong_derivation_method(payload: dict) -> None:
    payload["derivation"]["method"] = "adjusted_vendor_series"


def _empty_derivation_evidence(payload: dict) -> None:
    payload["derivation"]["evidence"] = "   "


def _mismatched_derivation_rule(payload: dict) -> None:
    payload["derivation"]["rule_version"] = "some-other-rule"


def _adjusted_price_basis(payload: dict) -> None:
    payload["raw_price_basis"] = "qfq"


def _derived_missing_on_trading(payload: dict) -> None:
    row = next(
        row for row in payload["derived"]
        if row["date"] == _day(0) and row["symbol"] == SYMBOLS[0]
    )
    row["value"] = None


def _derived_present_on_suspension(payload: dict) -> None:
    row = next(
        row for row in payload["derived"]
        if row["date"] == _day(SUSPENDED_INDEX) and row["symbol"] == SYMBOLS[-1]
    )
    row["value"] = 10.0


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (_missing_evidence, "evidence"),
        (_bad_evidence_sha, "sha256"),
        (_naive_evidence_available_at, "timezone"),
        (_missing_open_available_at, "open_available_at"),
        (_missing_close_available_at, "close_available_at"),
        (_naive_open_available_at, "timezone"),
        (_trading_null_open, "trading raw bar"),
        (_partial_halt_with_open, "partial_halt"),
        (_partial_halt_without_halt, "halt"),
        (_suspended_with_price, "suspended"),
        (_action_null_terms, "corporate action"),
        (_action_null_pay_date, "corporate action"),
        (_missing_derivation, "derivation"),
        (_wrong_derivation_method, "method"),
        (_empty_derivation_evidence, "evidence"),
        (_mismatched_derivation_rule, "rule_version"),
        (_adjusted_price_basis, "unadjusted"),
        (_derived_missing_on_trading, "trading session"),
        (_derived_present_on_suspension, "suspended session"),
    ],
)
def test_real_snapshot_rejects_incomplete_or_inconsistent_inputs(
    tmp_path: Path, mutate, message: str
) -> None:
    payload = _real_input()
    mutate(payload)
    result = _seal_result(tmp_path, payload)
    assert result.returncode != 0
    assert not result.stdout
    assert message in result.stderr


def test_real_snapshot_preserves_verified_action_terms_and_record_date(
    tmp_path: Path,
) -> None:
    payload = _real_input()
    action = _real_cash_dividend()
    payload["corporate_actions"] = [action]

    receipt = _snapshot_command(tmp_path, "seal", payload=payload)
    read = _snapshot_command(
        tmp_path, "read", "--id", receipt["snapshot_id"], "--partition", "discovery"
    )
    assert read["data"]["corporate_actions"] == [action]


def _as_synthetic(payload: dict) -> dict:
    payload["schema_version"] = 1
    payload["data_kind"] = "synthetic"
    for key in ("raw_price_basis", "evidence", "derivation"):
        payload.pop(key)
    for row in payload["raw_bars"]:
        row.pop("open_available_at")
        row.pop("close_available_at")
        if row["trading_status"] == "partial_halt":
            row.pop("halt")
            row.update(
                trading_status="suspended",
                open=None,
                high=None,
                low=None,
                close=None,
                volume=None,
            )
    return payload


def test_schema_version_and_data_kind_must_pair_without_fallback(
    tmp_path: Path,
) -> None:
    real_as_synthetic = _real_input()
    real_as_synthetic["schema_version"] = 1
    real_as_synthetic["data_kind"] = "synthetic"
    result = _seal_result(tmp_path, real_as_synthetic, store_name="real-as-synthetic")
    assert result.returncode != 0 and not result.stdout
    assert "trading_status" in result.stderr

    synthetic_as_real = _as_synthetic(_real_input())
    synthetic_as_real["schema_version"] = 2
    synthetic_as_real["data_kind"] = "real"
    result = _seal_result(tmp_path, synthetic_as_real, store_name="synthetic-as-real")
    assert result.returncode != 0 and not result.stdout
    assert "evidence" in result.stderr

    mismatch = _real_input()
    mismatch["schema_version"] = 1
    result = _seal_result(tmp_path, mismatch, store_name="mismatched-version")
    assert result.returncode != 0 and not result.stdout
    assert "schema version" in result.stderr


def test_tampered_real_discovery_payload_is_rejected(tmp_path: Path) -> None:
    payload = _real_input()
    receipt = _snapshot_command(tmp_path, "seal", payload=payload)
    directory = tmp_path / "store" / receipt["snapshot_id"]
    directory.chmod(0o755)
    discovery = directory / "discovery.json"
    discovery.chmod(0o644)
    discovery.write_text("{}", encoding="utf-8")
    directory.chmod(0o555)

    result = _cli(
        tmp_path,
        "snapshot",
        "read",
        "--store",
        str(tmp_path / "store"),
        "--id",
        receipt["snapshot_id"],
        "--partition",
        "discovery",
    )
    assert result.returncode != 0
    assert not result.stdout
    assert "integrity" in result.stderr.lower()


def test_real_unknown_status_is_a_gap_and_never_fills(tmp_path: Path) -> None:
    payload = _real_input()
    day = _day(1)
    row = _raw_row(payload, day, SYMBOLS[2])
    row.update(
        trading_status="unknown",
        open=None,
        high=None,
        low=None,
        close=None,
        volume=None,
        open_available_at=None,
        close_available_at=None,
    )
    derived = next(
        item for item in payload["derived"]
        if item["date"] == day and item["symbol"] == SYMBOLS[2]
    )
    derived["value"] = None

    receipt = _snapshot_command(tmp_path, "seal", payload=payload)
    manifest = _snapshot_command(tmp_path, "manifest", "--id", receipt["snapshot_id"])
    assert {"date": day, "symbol": SYMBOLS[2]} in manifest["gaps"]["raw_bars"]

    store = tmp_path / "store"
    output = tmp_path / "unknown-baseline.json"
    completed = _cli(
        tmp_path,
        "baseline",
        "--store",
        str(store),
        "--snapshot-id",
        receipt["snapshot_id"],
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
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["order_attempts"][0]["status"] == "not_filled"
    assert result["order_attempts"][0]["missing_symbols"] == [SYMBOLS[2]]
    gap = next(
        gap for gap in result["gaps"]["raw_bars"]
        if gap["date"] == day and gap["symbol"] == SYMBOLS[2]
    )
    assert gap["trading_status"] == "unknown"
    assert gap["reason"] == "unknown_trading_status"


def test_synthetic_v1_manifest_stays_synthetic_and_unversioned(
    tmp_path: Path,
) -> None:
    payload = _as_synthetic(_real_input())
    receipt = _snapshot_command(
        tmp_path, "seal", payload=payload, store_name="synthetic-store"
    )
    assert receipt["schema_version"] == 1
    assert receipt["data_kind"] == "synthetic"
    manifest = _snapshot_command(
        tmp_path,
        "manifest",
        "--id",
        receipt["snapshot_id"],
        store_name="synthetic-store",
    )
    assert manifest["schema_version"] == 1
    assert manifest["data_kind"] == "synthetic"
    assert "raw_price_basis" not in manifest
    assert "evidence" not in manifest
    assert "derivation" not in manifest
    read = _snapshot_command(
        tmp_path,
        "read",
        "--id",
        receipt["snapshot_id"],
        "--partition",
        "discovery",
        store_name="synthetic-store",
    )
    assert read["schema_version"] == 1
    assert read["data_kind"] == "synthetic"
    assert all("open_available_at" not in row for row in read["data"]["raw_bars"])


def test_real_partial_halt_final_session_stays_in_range_and_marks_nav(
    tmp_path: Path,
) -> None:
    # 2026-11-20 ends the real window with 513300 and 159612 partial halts; a
    # confirmed close must keep the session in the common range without making
    # its missing 09:30 open executable.
    payload = _real_input()
    last = _day(SESSIONS - 1)
    for symbol in (SYMBOLS[-1], SYMBOLS[3]):
        _make_partial_halt(_raw_row(payload, last, symbol), last)

    result = _run_baseline(tmp_path, payload, "real-final-halt")
    manifest = _snapshot_command(
        tmp_path,
        "manifest",
        "--id",
        result["snapshot_id"],
        store_name="real-final-halt-store",
    )
    assert manifest["common_range"] == {"start": _day(0), "end": last}
    assert [
        (item["name"], item["sessions"], item["start"], item["end"])
        for item in manifest["partitions"]
    ] == [
        ("discovery", 9, _day(0), _day(8)),
        ("validation", 3, _day(9), _day(11)),
        ("final_holdout", 3, _day(12), last),
    ]
    assert {"date": last, "symbol": SYMBOLS[-1]} in manifest["gaps"]["raw_bars"]
    final_point = result["equity_curve"][-1]
    assert final_point["date"] == _day(8)
    assert final_point["nav"] == 5000
    assert not any(
        gap["date"] == _day(8) for gap in result["gaps"]["valuation"]
    )
