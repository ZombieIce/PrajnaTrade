"""The acquisition conversion, sealed and scored through the public CLI (#57).

The acquisition script used to read the vendor's unadjusted daily open and then
write ``open: null`` for a halted-at-open session, so the resume open was lost
before the snapshot existed.  These tests drive the production conversion entry
points directly with synthetic vendor bars and synthetic halt notices, seal the
resulting schema-3 document and score it under ``actual-open-v1``.  Every value
here is synthetic; nothing is fetched, and the real 45-row verification stays
with #35.
"""

from __future__ import annotations

import importlib.util
from functools import cache
from pathlib import Path
from types import ModuleType

import pytest
from cli_helpers import cli, ok, rejected, repo_root
from test_actual_open_cli import derived_row, raw_row, seal_v3
from test_discovery_split_cli import manifest
from test_formal_validation_cli import formal_scenario, round_uses, validate_current
from test_protected_eval_cli import create_campaign, submit_experiment

from prajnatrade.data import ETF_SYMBOLS
from prajnatrade.snapshot_baseline import ACTUAL_OPEN_RULES, EXECUTION_PRICE_BASIS

ACQUISITION_SCRIPT = repo_root() / "scripts" / "acquire_real_etf_snapshot.py"
#: Nine sessions seal to a five-session Discovery interval.
SESSION_DATES = (
    "2024-01-02",
    "2024-01-03",
    "2024-01-04",
    "2024-01-05",
    "2024-01-08",
    "2024-01-09",
    "2024-01-10",
    "2024-01-11",
    "2024-01-12",
)
FORMAL_SESSIONS = (
    *SESSION_DATES,
    "2024-01-15",
    "2024-01-16",
    "2024-01-17",
    "2024-01-18",
    "2024-01-19",
    "2024-01-22",
)
TRAILING_HALT_DAY = FORMAL_SESSIONS[7]
HALT_DAY = SESSION_DATES[1]
RESUME_DAY = SESSION_DATES[2]
HALTED = ETF_SYMBOLS[0]
SUSPENDED = ETF_SYMBOLS[1]
PRICE_EVIDENCE_ID = "price-tencent-unadjusted"
PRICE_SOURCE = "tencent:unadjusted"
HALT_EVIDENCE_ID = f"halt-{HALTED}-{SESSION_DATES[0]}"
SUSPENDED_EVIDENCE_ID = f"halt-{SUSPENDED}-{SESSION_DATES[0]}"
COLLECTED_AT = "2024-01-23T18:00:00+08:00"


@cache
def _acquisition_module() -> ModuleType:
    """Import the checked-in script by path: its ``main`` is guarded."""
    spec = importlib.util.spec_from_file_location(
        "acquire_real_etf_snapshot", ACQUISITION_SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _vendor_bar(day: str, open_price: float = 10.0) -> list[str]:
    """One Tencent unadjusted daily bar (date, open, close, high, low, volume)."""
    close = round(open_price + 0.2, 6)
    return [
        day,
        str(open_price),
        str(close),
        str(round(close + 0.1, 6)),
        str(round(open_price - 0.1, 6)),
        "1200",
    ]


def _vendor_bars(
    sessions: tuple[str, ...] = SESSION_DATES,
) -> dict[str, dict[str, list[str]]]:
    bars = {
        symbol: {
            day: _vendor_bar(day, 10.5 if (symbol, day) == (HALTED, HALT_DAY) else 10.0)
            for day in sessions
        }
        for symbol in ETF_SYMBOLS
    }
    # A confirmed whole-day halt has no vendor daily bar.
    del bars[SUSPENDED][HALT_DAY]
    return bars


def _halt_rows() -> list[dict]:
    return [
        {
            "code": HALTED,
            "announced": SESSION_DATES[0],
            "kind": "open_halt",
            "date": HALT_DAY,
            "resume": "10:30",
        },
        {
            "code": SUSPENDED,
            "announced": SESSION_DATES[0],
            "kind": "full_halt_until",
            "date": HALT_DAY,
            "resume": f"{RESUME_DAY} 09:30",
        },
    ]


def _evidence_items() -> list[dict]:
    return [
        {
            "id": PRICE_EVIDENCE_ID,
            "kind": "price_archive",
            "reference": "synthetic:tencent-unadjusted-bars@2024",
            "sha256": "b" * 64,
            "available_at": "2024-12-31T23:59:59+08:00",
        },
        {
            "id": HALT_EVIDENCE_ID,
            "kind": "halt",
            "reference": f"https://example.invalid/{HALT_EVIDENCE_ID}.pdf",
            "sha256": "c" * 64,
            "available_at": f"{SESSION_DATES[0]}T23:59:59+08:00",
        },
        {
            "id": SUSPENDED_EVIDENCE_ID,
            "kind": "halt",
            "reference": f"https://example.invalid/{SUSPENDED_EVIDENCE_ID}.pdf",
            "sha256": "d" * 64,
            "available_at": f"{SESSION_DATES[0]}T23:59:59+08:00",
        },
    ]


def _build_payload(
    *,
    sessions: tuple[str, ...] = SESSION_DATES,
    bars: dict | None = None,
    halts: list[dict] | None = None,
) -> dict:
    module = _acquisition_module()
    return module.build_snapshot_input(
        sessions=list(sessions),
        bars=bars if bars is not None else _vendor_bars(sessions),
        halts=halts if halts is not None else _halt_rows(),
        dividends=[],
        evidence=_evidence_items(),
        price_evidence_id=PRICE_EVIDENCE_ID,
        halt_notice_evidence={
            (symbol, SESSION_DATES[0]): {
                "id": evidence_id,
                "reference": f"https://example.invalid/{evidence_id}.pdf",
            }
            for symbol, evidence_id in (
                (HALTED, HALT_EVIDENCE_ID), (SUSPENDED, SUSPENDED_EVIDENCE_ID)
            )
        },
        price_collected_at=COLLECTED_AT,
        calendar_collected_at=COLLECTED_AT,
        action_collected_at=COLLECTED_AT,
        derived_collected_at=COLLECTED_AT,
        derivation_evidence=("synthetic:official-halts+unadjusted-bars",),
    )


def test_conversion_keeps_the_resume_open_and_emits_schema_3() -> None:
    """The conversion emits schema 3 and never drops a vendor resume open."""
    payload = _build_payload()
    assert payload["schema_version"] == 3
    assert payload["data_kind"] == "real"
    assert payload["raw_price_basis"] == "unadjusted"

    # A normal session keeps the vendor's unadjusted open and cites the price
    # archive.
    normal = raw_row(payload, SESSION_DATES[0], HALTED)
    assert normal["trading_status"] == "trading"
    assert normal["open"] == 10.0
    assert normal["open_source"] == {
        "source": PRICE_SOURCE,
        "evidence_id": PRICE_EVIDENCE_ID,
    }

    # The reviewer's failing step: a halted-at-open session keeps the vendor's
    # actual open (10.5) plus its price source and a distinct halt notice.
    halted = raw_row(payload, HALT_DAY, HALTED)
    assert halted["trading_status"] == "partial_halt"
    assert halted["open"] == 10.5
    assert halted["volume"] == 1200
    assert halted["open_source"] == {
        "source": PRICE_SOURCE,
        "evidence_id": PRICE_EVIDENCE_ID,
    }
    assert halted["status_evidence_id"] == HALT_EVIDENCE_ID
    assert halted["halt"] == {
        "start": f"{HALT_DAY}T09:30:00+08:00",
        "end": f"{HALT_DAY}T10:30:00+08:00",
    }

    # A whole-day halt becomes ``suspended`` with null prices and the notice as
    # its trading-status evidence.
    suspended = raw_row(payload, HALT_DAY, SUSPENDED)
    assert suspended["trading_status"] == "suspended"
    assert all(
        suspended[field] is None
        for field in ("open", "high", "low", "close", "volume")
    )
    assert suspended["status_evidence_id"] == SUSPENDED_EVIDENCE_ID
    assert suspended["halt"] == {
        "start": f"{HALT_DAY}T09:30:00+08:00",
        "end": f"{RESUME_DAY}T09:30:00+08:00",
    }


def test_conversion_rejects_a_vendor_bar_for_the_wrong_session() -> None:
    bars = _vendor_bars()
    bars[HALTED][HALT_DAY] = _vendor_bar(RESUME_DAY, 10.5)

    with pytest.raises(
        _acquisition_module().AcquisitionError,
        match=rf"{HALTED}.*{HALT_DAY}.*{RESUME_DAY}",
    ):
        _build_payload(bars=bars)


def test_a_full_halt_until_with_a_same_day_resume_keeps_its_open() -> None:
    """The real 2026-11-20 case: ``full_halt_until`` can be a partial halt.

    The vendor kind only says the halt starts at the open; its halt window
    decides whether the session traded.  A same-day resume leaves the vendor's
    actual open, exactly like ``open_halt``, and the converted row stays a
    sealable ``partial_halt`` (marking it ``suspended`` would also contradict
    the vendor close that makes its derived value non-null).  A resume on a
    later session covers the whole day and stays ``suspended``.
    """
    module = _acquisition_module()
    bars = _vendor_bars()
    bars[HALTED][HALT_DAY] = _vendor_bar(HALT_DAY, 10.5)
    bars[SUSPENDED][HALT_DAY] = _vendor_bar(HALT_DAY)
    halt = {
        "code": HALTED,
        "announced": SESSION_DATES[0],
        "kind": "full_halt_until",
        "date": HALT_DAY,
        "resume": f"{HALT_DAY} 10:30",
    }
    rows = module.convert_raw_bars(
        sessions=list(SESSION_DATES),
        bars=bars,
        halts=[halt],
        price_evidence_id=PRICE_EVIDENCE_ID,
        halt_notice_evidence={
            (HALTED, SESSION_DATES[0]): {
                "id": HALT_EVIDENCE_ID,
                "reference": "https://example.invalid/halt.pdf",
            }
        },
        price_collected_at=COLLECTED_AT,
    )
    row = next(
        row for row in rows if (row["date"], row["symbol"]) == (HALT_DAY, HALTED)
    )
    assert row["trading_status"] == "partial_halt"
    assert row["open"] == 10.5
    assert row["open_source"] == {
        "source": PRICE_SOURCE,
        "evidence_id": PRICE_EVIDENCE_ID,
    }
    assert row["status_evidence_id"] == HALT_EVIDENCE_ID
    assert row["halt"] == {
        "start": f"{HALT_DAY}T09:30:00+08:00",
        "end": f"{HALT_DAY}T10:30:00+08:00",
    }


def test_converted_halt_open_seals_and_fills_in_discovery(tmp_path: Path) -> None:
    """Conversion -> seal -> Campaign -> Discovery fill keeps the resume open.

    The equal-weight order is effective before the open, so the halted symbol
    fills at the converted actual open 10.5 with the sealed price-source
    reference, while the confirmed no-trade symbol does not fill at all.
    """
    payload = _build_payload()
    sealed = seal_v3(tmp_path, payload, store="conversion-store", name="conversion")
    assert sealed["receipt"] == {
        "schema_version": 3,
        "snapshot_id": sealed["snapshot_id"],
        "data_kind": "real",
    }
    rows = {
        (row["date"], row["symbol"]): row
        for row in sealed["manifest"]["execution_evidence"]["rows"]
    }
    assert rows[(HALT_DAY, HALTED)]["classification"] == "actual_open_after_halt"
    assert rows[(HALT_DAY, HALTED)]["open_source"] == {
        "source": PRICE_SOURCE,
        "evidence_id": PRICE_EVIDENCE_ID,
    }
    assert rows[(HALT_DAY, SUSPENDED)]["classification"] == "confirmed_no_trade"
    assert rows[(HALT_DAY, SUSPENDED)]["status_evidence_id"] == SUSPENDED_EVIDENCE_ID

    project = tmp_path / "conversion-project"
    ok(cli("project", "init", "--project", str(project)))
    campaign = create_campaign(
        project, sealed["store"], sealed["snapshot_id"],
        "--execution-rules", ACTUAL_OPEN_RULES,
    )
    experiment = submit_experiment(
        project, campaign["campaign_id"], tmp_path / "conversion-experiment.json",
        "conversion", "Equal weights fill at the converted actual daily open.",
        "equal_weight", {},
    )
    discovery = experiment["discovery"]
    fills = discovery["fills"]
    # Four symbols with an executable open fill on the halt day; the confirmed
    # no-trade symbol keeps its cash allocation.
    assert [fill["date"] for fill in fills] == [HALT_DAY] * 4
    assert [fill["symbol"] for fill in fills] == [
        symbol for symbol in ETF_SYMBOLS if symbol != SUSPENDED
    ]
    assert discovery["order_attempts"] == [{
        "signal_date": SESSION_DATES[0],
        "date": HALT_DAY,
        "attempts": 1,
        "status": "partially_filled",
        "filled_symbols": [symbol for symbol in ETF_SYMBOLS if symbol != SUSPENDED],
        "unfilled": [{"symbol": SUSPENDED, "reason": "confirmed_no_trade"}],
        "basket_complete": False,
    }]
    resumed = next(fill for fill in fills if fill["symbol"] == HALTED)
    sealed_row = raw_row(payload, HALT_DAY, HALTED)
    assert resumed["raw_open"] == sealed_row["open"] == 10.5
    assert resumed["price"] == pytest.approx(10.5)
    assert resumed["open_evidence"] == sealed_row["open_source"]
    assert resumed["price_basis"] == EXECUTION_PRICE_BASIS
    assert SUSPENDED not in {fill["symbol"] for fill in fills}
    assert discovery["position_evidence"]["basket_build_state"] == "partial"
    assert discovery["position_evidence"]["unfilled_basket_signals"] == 1


def _assert_formal_validation_blocked(scenario: dict, evidence: dict) -> None:
    blocked = rejected(
        validate_current(scenario), "VALIDATION_DATA_GAP", phase="validation"
    )
    assert blocked["counted"] is False
    assert blocked["allowance"]["automatic_used"] == 0
    assert blocked["execution_evidence"] == [evidence]
    assert all(
        evidence[key] in blocked["error"]["message"]
        for key in ("date", "symbol", "classification")
    )
    assert round_uses(scenario) == []
    assert not (scenario["store"] / "research-loop-validations").exists()


@pytest.mark.parametrize("missing", ["whole-bar", "open-only", "zero-volume"])
def test_converted_missing_resume_open_blocks_formal_validation(
    tmp_path: Path, missing: str,
) -> None:
    bars = _vendor_bars(FORMAL_SESSIONS)
    bars[SUSPENDED][HALT_DAY] = _vendor_bar(HALT_DAY)
    if missing == "whole-bar":
        del bars[HALTED][TRAILING_HALT_DAY]
    elif missing == "open-only":
        bars[HALTED][TRAILING_HALT_DAY][1] = ""
    else:
        bars[HALTED][TRAILING_HALT_DAY][-1] = "0"
    halts = [{**_halt_rows()[0], "date": TRAILING_HALT_DAY}]
    payload = _build_payload(sessions=FORMAL_SESSIONS, bars=bars, halts=halts)
    scenario = formal_scenario(
        tmp_path, payload=payload, limit=1, execution_rules=ACTUAL_OPEN_RULES
    )
    assert TRAILING_HALT_DAY in scenario["trailing_dates"]
    sealed = manifest(scenario["snapshots"], scenario["campaign"]["snapshot_id"])
    gap = {"date": TRAILING_HALT_DAY, "symbol": HALTED}
    assert gap in sealed["gaps"]["raw_bars"]
    assert (gap in sealed["gaps"]["derived"]) is (missing != "open-only")
    [evidence] = [
        row for row in sealed["execution_evidence"]["rows"]
        if row["date"] == TRAILING_HALT_DAY
    ]
    assert evidence["symbol"] == HALTED
    assert evidence["classification"] == "missing_required_open"
    assert evidence["status_evidence_id"] == HALT_EVIDENCE_ID
    assert "open_source" not in evidence

    _assert_formal_validation_blocked(scenario, evidence)


@pytest.mark.parametrize("volume", ["1200", "0"], ids=["contradiction", "stale-bar"])
def test_converted_vendor_bar_during_a_whole_day_halt(
    tmp_path: Path, volume: str,
) -> None:
    bars = _vendor_bars(FORMAL_SESSIONS)
    bars[SUSPENDED][HALT_DAY] = _vendor_bar(HALT_DAY)
    bars[SUSPENDED][TRAILING_HALT_DAY] = _vendor_bar(TRAILING_HALT_DAY, 99.0)
    bars[SUSPENDED][TRAILING_HALT_DAY][-1] = volume
    halts = [{
        **_halt_rows()[1],
        "date": TRAILING_HALT_DAY,
        "resume": f"{TRAILING_HALT_DAY} 15:00",
    }]
    payload = _build_payload(sessions=FORMAL_SESSIONS, bars=bars, halts=halts)
    scenario = formal_scenario(
        tmp_path, payload=payload, limit=1, execution_rules=ACTUAL_OPEN_RULES,
    )
    assert TRAILING_HALT_DAY in scenario["trailing_dates"]
    sealed = manifest(scenario["snapshots"], scenario["campaign"]["snapshot_id"])
    gap = {"date": TRAILING_HALT_DAY, "symbol": SUSPENDED}
    for kind in ("raw_bars", "derived"):
        assert gap in sealed["gaps"][kind]
    row = raw_row(payload, TRAILING_HALT_DAY, SUSPENDED)
    assert row["open"] is None
    assert row["close"] is None
    assert derived_row(payload, TRAILING_HALT_DAY, SUSPENDED)["value"] is None
    assert derived_row(payload, FORMAL_SESSIONS[8], SUSPENDED)["value"] == pytest.approx(1.0)

    [evidence] = [
        row for row in sealed["execution_evidence"]["rows"]
        if row["date"] == TRAILING_HALT_DAY
    ]
    assert evidence["symbol"] == SUSPENDED
    assert evidence["status_evidence_id"] == SUSPENDED_EVIDENCE_ID
    if volume == "0":
        assert evidence["classification"] == "confirmed_no_trade"
        validated = ok(validate_current(scenario))
        assert validated["usage"]["sequence"] == 1
        assert len(round_uses(scenario)) == 1
        return

    assert evidence["classification"] == "unknown_status"
    assert PRICE_EVIDENCE_ID in row["availability_evidence"]
    _assert_formal_validation_blocked(scenario, evidence)


def test_the_conversion_entries_are_importable_without_running_main() -> None:
    """Importing the script exposes the pure entries and never calls ``main``."""
    module = _acquisition_module()
    assert callable(module.convert_raw_bars)
    assert callable(module.build_snapshot_input)
    assert module.PRICE_SOURCE == PRICE_SOURCE
    assert module.halt_evidence_id(HALTED, SESSION_DATES[0]) == HALT_EVIDENCE_ID
