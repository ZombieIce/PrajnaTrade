"""Public-CLI coverage for schema-version-3 real snapshots and actual-open fills.

Ticket #57 (spec #56): a halted-at-open session may seal its actual unadjusted
daily open with a price source and a distinct trading-status notice, and
``--execution-rules actual-open-v1`` fills an order effective before that open at
the sealed actual open.  The fixtures are synthetic JSON labelled
``data_kind: "real"``: they exercise the mechanism (price-source and
trading-status evidence, halted-at-open rows, source conflicts, evidence-bound
execution), not real market data.  Nothing here seals or claims a real snapshot.

Test ownership: this module owns the reusable schema-3 fixture builders
(``schema_v3_fixture``, the halt/suspension/conflict mutators and ``seal_v3``).
#58 and #59 import them instead of copying them.
"""

from __future__ import annotations

from datetime import date, timedelta, timezone
from pathlib import Path

import pytest
from cli_helpers import cli, ok, write_json
from test_real_snapshot import schema_v2_fixture

from prajnatrade.data import ETF_SYMBOLS

SYMBOLS = ETF_SYMBOLS
SESSIONS = 15
START = date(2024, 1, 2)
CN_TZ = timezone(timedelta(hours=8))
COLLECTED_AT = "2024-02-01T00:00:00+08:00"
CALENDAR_AVAILABLE_AT = "2024-01-01T00:00:00+08:00"
KINDS = ("calendar", "raw_bars", "corporate_actions", "derived")
DERIVATION = {
    "rule_version": "tr-reinvest-exclose-v1",
    "method": "cash_dividend_reinvested_at_ex_date_close",
    "evidence": "synthetic:official-dividends+unadjusted-closes",
}
#: The sealed price archive every executable schema-3 open cites.
PRICE_EVIDENCE = "prices-2024"
#: The suspension/resumption notice every halted or suspended row cites.
STATUS_EVIDENCE = "halt-notices-2024"
#: Two conflicting vendor feeds used by the source-conflict fixture.
CONFLICT_EVIDENCE = ("conflict-vendor-a", "conflict-vendor-b")
#: The per-row open price source echoed onto an ``actual-open-v1`` fill.
OPEN_SOURCE = {"source": "synthetic-tdx-unadjusted", "evidence_id": PRICE_EVIDENCE}
EXECUTION_EVIDENCE_SCHEMA_VERSION = 1
EXECUTION_EVIDENCE_POLICY = {
    "price_basis": "unadjusted_open",
    "price_source": "raw_bars[].open_source -> evidence.items",
    "status_evidence": "raw_bars[].status_evidence_id -> evidence.items",
    "halted_open": (
        "the day's actual unadjusted daily open for an order effective before the open"
    ),
}
EXECUTION_CLASSIFICATIONS = (
    "actual_open_after_halt",
    "confirmed_no_trade",
    "missing_required_open",
    "source_conflict",
    "unknown_status",
)


def day_at(index: int) -> str:
    """The ISO trading date of one fixture session index."""
    return (START + timedelta(days=index)).isoformat()


def _at(day: str, hour: int, minute: int = 0) -> str:
    return f"{day}T{hour:02d}:{minute:02d}:00+08:00"


def _open_source() -> dict:
    return dict(OPEN_SOURCE)


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
                "id": PRICE_EVIDENCE,
                "kind": "price_archive",
                "reference": "synthetic:tdx-g4day-unadjusted@2024",
                "sha256": "b" * 64,
                "available_at": "2024-12-31T23:59:59+08:00",
            },
            {
                "id": STATUS_EVIDENCE,
                "kind": "suspension_notice",
                "reference": "synthetic:szse-halt-notices@2024",
                "sha256": "c" * 64,
                "available_at": "2024-12-31T23:59:59+08:00",
            },
            {
                "id": CONFLICT_EVIDENCE[0],
                "kind": "price_archive",
                "reference": "synthetic:vendor-a-unadjusted@2024",
                "sha256": "d" * 64,
                "available_at": "2024-12-31T23:59:59+08:00",
            },
            {
                "id": CONFLICT_EVIDENCE[1],
                "kind": "price_archive",
                "reference": "synthetic:vendor-b-unadjusted@2024",
                "sha256": "e" * 64,
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


def schema_v3_fixture(session_count: int = SESSIONS) -> dict:
    """A clean synthetic schema-3 real payload: all sessions trade normally.

    Every raw bar carries its sealed ``open_source``; ``raw_price_basis`` is
    declared.  Mutators below turn individual rows into halted-at-open,
    suspended, conflicted or unknown sessions.
    """
    days = [day_at(index) for index in range(session_count)]
    sources = {
        name: {
            "source": f"synthetic-real-{name}",
            "version": "3",
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
            raw_bars.append(
                {
                    "date": day,
                    "symbol": symbol,
                    "open": 10.0,
                    "high": 10.0,
                    "low": 10.0,
                    "close": 10.0,
                    "volume": 1000,
                    "trading_status": "trading",
                    "open_source": _open_source(),
                    "open_available_at": _at(day, 9, 30),
                    "close_available_at": _at(day, 15, 5),
                    **_row_evidence(day),
                }
            )
            derived.append(
                {
                    "date": day,
                    "symbol": symbol,
                    "value": 10.0 + index / 10.0,
                    "available_at": _at(day, 15, 5),
                    "availability_evidence": f"synthetic:derivation:{day}",
                    "collected_at": COLLECTED_AT,
                }
            )
    return {
        "schema_version": 3,
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


def raw_row(payload: dict, day: str, symbol: str) -> dict:
    return next(
        row
        for row in payload["raw_bars"]
        if row["date"] == day and row["symbol"] == symbol
    )


def derived_row(payload: dict, day: str, symbol: str) -> dict:
    return next(
        row
        for row in payload["derived"]
        if row["date"] == day and row["symbol"] == symbol
    )


def make_actual_open_halt(
    payload: dict, day: str, symbol: str, *, open_price: float = 10.0
) -> dict:
    """A halted-at-open session whose sealed daily bar keeps the actual open.

    The resume notice proves trading status only; the open cites the price
    archive separately.
    """
    row = raw_row(payload, day, symbol)
    row.update(
        trading_status="partial_halt",
        open=open_price,
        high=max(float(row["high"]), open_price),
        low=min(float(row["low"]), open_price),
        open_source=_open_source(),
        status_evidence_id=STATUS_EVIDENCE,
        open_available_at=_at(day, 10, 30),
        halt={"start": _at(day, 9, 30), "end": _at(day, 10, 30)},
    )
    return row


def make_confirmed_suspension(payload: dict, day: str, symbol: str) -> dict:
    """A session with no trade at all: the notice confirms the whole-day halt."""
    row = raw_row(payload, day, symbol)
    row.pop("open_source", None)
    row.update(
        trading_status="suspended",
        open=None,
        high=None,
        low=None,
        close=None,
        volume=None,
        open_available_at=None,
        close_available_at=None,
        status_evidence_id=STATUS_EVIDENCE,
        halt={"start": _at(day, 9, 30), "end": _at(day, 15, 0)},
    )
    derived_row(payload, day, symbol)["value"] = None
    return row


def make_whole_day_halt(payload: dict, day: str, symbol: str) -> dict:
    """A partial_halt row whose window covers the session: confirmed no trade."""
    row = raw_row(payload, day, symbol)
    row.pop("open_source", None)
    row.update(
        trading_status="partial_halt",
        open=None,
        open_available_at=_at(day, 15, 0),
        close_available_at=_at(day, 15, 5),
        status_evidence_id=STATUS_EVIDENCE,
        halt={"start": _at(day, 9, 30), "end": _at(day, 15, 0)},
    )
    return row


def make_missing_required_open(payload: dict, day: str, symbol: str) -> dict:
    """A partial halt that resumes mid-session without a trusted daily open."""
    row = raw_row(payload, day, symbol)
    row.pop("open_source", None)
    row.update(
        trading_status="partial_halt",
        open=None,
        open_available_at=_at(day, 10, 30),
        status_evidence_id=STATUS_EVIDENCE,
        halt={"start": _at(day, 9, 30), "end": _at(day, 10, 30)},
    )
    return row


def make_unknown_status(payload: dict, day: str, symbol: str) -> dict:
    """A session whose trading status is unknown; it is a declared gap."""
    row = raw_row(payload, day, symbol)
    row.pop("open_source", None)
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
    derived_row(payload, day, symbol)["value"] = None
    return row


def make_open_conflict(
    payload: dict, day: str, symbol: str, *, opens: tuple[float, float] = (10.0, 10.4)
) -> dict:
    """Two sealed vendor opens disagree; neither is silently chosen."""
    row = raw_row(payload, day, symbol)
    row.pop("open_source", None)
    row.update(
        trading_status="partial_halt",
        open=None,
        open_available_at=_at(day, 10, 30),
        status_evidence_id=STATUS_EVIDENCE,
        halt={"start": _at(day, 9, 30), "end": _at(day, 10, 30)},
        open_conflict={
            "sources": [
                {
                    "source": "synthetic-vendor-a",
                    "evidence_id": CONFLICT_EVIDENCE[0],
                    "open": opens[0],
                },
                {
                    "source": "synthetic-vendor-b",
                    "evidence_id": CONFLICT_EVIDENCE[1],
                    "open": opens[1],
                },
            ]
        },
    )
    return row


def seal_v3(
    root: Path,
    payload: dict,
    *,
    store: str = "store",
    name: str = "schema-v3",
) -> dict:
    """Seal one schema-3 payload and return its receipt, store and manifest."""
    source = write_json(root / f"{name}-input.json", payload)
    sealed = ok(
        cli("snapshot", "seal", "--input", str(source), "--store", str(root / store))
    )
    manifest = ok(
        cli("snapshot", "manifest", "--store", str(root / store), "--id", sealed["snapshot_id"])
    )
    return {
        "receipt": sealed,
        "store": root / store,
        "snapshot_id": sealed["snapshot_id"],
        "manifest": manifest,
    }


def _seal_rejected(root: Path, payload: dict) -> object:
    source = write_json(root / "invalid-v3.json", payload)
    return cli(
        "snapshot", "seal", "--input", str(source), "--store", str(root / "rejected-store")
    )


# ---------------------------------------------------------------------------
# Seal contract: schema-3 execution evidence and classifications
# ---------------------------------------------------------------------------


def test_schema_v3_seal_classifies_execution_evidence(tmp_path: Path) -> None:
    payload = schema_v3_fixture()
    actual_day = day_at(2)
    make_actual_open_halt(payload, actual_day, SYMBOLS[0], open_price=10.2)
    suspended_day = day_at(3)
    make_confirmed_suspension(payload, suspended_day, SYMBOLS[1])
    whole_halt_day = day_at(4)
    make_whole_day_halt(payload, whole_halt_day, SYMBOLS[2])
    missing_day = day_at(5)
    make_missing_required_open(payload, missing_day, SYMBOLS[3])
    conflict_day = day_at(6)
    make_open_conflict(payload, conflict_day, SYMBOLS[4])
    unknown_day = day_at(7)
    make_unknown_status(payload, unknown_day, SYMBOLS[0])

    sealed = seal_v3(tmp_path, payload)

    assert sealed["receipt"] == {
        "schema_version": 3,
        "snapshot_id": sealed["snapshot_id"],
        "data_kind": "real",
    }
    manifest = sealed["manifest"]
    assert manifest["schema_version"] == 3
    assert manifest["raw_price_basis"] == "unadjusted"
    assert manifest["evidence"] == payload["evidence"]
    evidence = manifest["execution_evidence"]
    assert evidence["schema_version"] == EXECUTION_EVIDENCE_SCHEMA_VERSION
    assert evidence["policy"] == EXECUTION_EVIDENCE_POLICY
    assert evidence["rows"] == [
        {
            "date": actual_day,
            "symbol": SYMBOLS[0],
            "trading_status": "partial_halt",
            "classification": "actual_open_after_halt",
            "open": 10.2,
            "open_source": dict(OPEN_SOURCE),
            "halt": {"start": _at(actual_day, 9, 30), "end": _at(actual_day, 10, 30)},
            "status_evidence_id": STATUS_EVIDENCE,
        },
        {
            "date": suspended_day,
            "symbol": SYMBOLS[1],
            "trading_status": "suspended",
            "classification": "confirmed_no_trade",
            "halt": {"start": _at(suspended_day, 9, 30), "end": _at(suspended_day, 15, 0)},
            "status_evidence_id": STATUS_EVIDENCE,
        },
        {
            "date": whole_halt_day,
            "symbol": SYMBOLS[2],
            "trading_status": "partial_halt",
            "classification": "confirmed_no_trade",
            "halt": {"start": _at(whole_halt_day, 9, 30), "end": _at(whole_halt_day, 15, 0)},
            "status_evidence_id": STATUS_EVIDENCE,
        },
        {
            "date": missing_day,
            "symbol": SYMBOLS[3],
            "trading_status": "partial_halt",
            "classification": "missing_required_open",
            "reason": "partial_halt_without_actual_open",
            "halt": {"start": _at(missing_day, 9, 30), "end": _at(missing_day, 10, 30)},
            "status_evidence_id": STATUS_EVIDENCE,
        },
        {
            "date": conflict_day,
            "symbol": SYMBOLS[4],
            "trading_status": "partial_halt",
            "classification": "source_conflict",
            "open_conflict": raw_row(payload, conflict_day, SYMBOLS[4])["open_conflict"],
            "halt": {"start": _at(conflict_day, 9, 30), "end": _at(conflict_day, 10, 30)},
            "status_evidence_id": STATUS_EVIDENCE,
        },
        {
            "date": unknown_day,
            "symbol": SYMBOLS[0],
            "trading_status": "unknown",
            "classification": "unknown_status",
        },
    ]
    assert evidence["counts"] == {
        "actual_open_after_halt": 1,
        "confirmed_no_trade": 2,
        "missing_required_open": 1,
        "source_conflict": 1,
        "unknown_status": 1,
    }
    # ``gaps`` keeps its historical shape and population: a confirmed no-trade
    # or missing/conflicted open is still a declared raw-bar gap, while a sealed
    # actual open is no longer a gap.
    assert {"date": suspended_day, "symbol": SYMBOLS[1]} in manifest["gaps"]["raw_bars"]
    assert {"date": missing_day, "symbol": SYMBOLS[3]} in manifest["gaps"]["raw_bars"]
    assert {"date": conflict_day, "symbol": SYMBOLS[4]} in manifest["gaps"]["raw_bars"]
    assert {"date": suspended_day, "symbol": SYMBOLS[1]} in manifest["gaps"]["derived"]
    assert {"date": actual_day, "symbol": SYMBOLS[0]} not in manifest["gaps"]["raw_bars"]


def _missing_open_source(payload: dict) -> None:
    raw_row(payload, day_at(0), SYMBOLS[0]).pop("open_source")


def _unknown_open_source_evidence(payload: dict) -> None:
    raw_row(payload, day_at(0), SYMBOLS[0])["open_source"]["evidence_id"] = "missing"


def _empty_open_source_name(payload: dict) -> None:
    raw_row(payload, day_at(0), SYMBOLS[0])["open_source"]["source"] = "   "


def _extra_open_source_member(payload: dict) -> None:
    raw_row(payload, day_at(0), SYMBOLS[0])["open_source"]["basis"] = "unadjusted"


def _missing_status_evidence(payload: dict) -> None:
    row = make_actual_open_halt(payload, day_at(0), SYMBOLS[0])
    row.pop("status_evidence_id")


def _unknown_status_evidence(payload: dict) -> None:
    row = make_actual_open_halt(payload, day_at(0), SYMBOLS[0])
    row["status_evidence_id"] = "missing"


def _status_evidence_reuses_the_price_source(payload: dict) -> None:
    row = make_actual_open_halt(payload, day_at(0), SYMBOLS[0])
    row["status_evidence_id"] = PRICE_EVIDENCE


def _suspended_without_status_evidence(payload: dict) -> None:
    row = make_confirmed_suspension(payload, day_at(0), SYMBOLS[0])
    row.pop("status_evidence_id")


def _missing_raw_price_basis(payload: dict) -> None:
    payload.pop("raw_price_basis")


def _conflict_with_an_open(payload: dict) -> None:
    row = make_open_conflict(payload, day_at(0), SYMBOLS[0])
    row["open"] = 10.0


def _conflict_with_one_source(payload: dict) -> None:
    row = make_open_conflict(payload, day_at(0), SYMBOLS[0])
    row["open_conflict"]["sources"] = row["open_conflict"]["sources"][:1]


def _conflict_with_duplicate_sources(payload: dict) -> None:
    row = make_open_conflict(payload, day_at(0), SYMBOLS[0])
    row["open_conflict"]["sources"][1]["source"] = row["open_conflict"]["sources"][0]["source"]


def _conflict_with_unknown_evidence(payload: dict) -> None:
    row = make_open_conflict(payload, day_at(0), SYMBOLS[0])
    row["open_conflict"]["sources"][0]["evidence_id"] = "missing"


def _conflict_with_negative_open(payload: dict) -> None:
    row = make_open_conflict(payload, day_at(0), SYMBOLS[0])
    row["open_conflict"]["sources"][0]["open"] = -1.0


def _conflict_with_identical_opens(payload: dict) -> None:
    row = make_open_conflict(payload, day_at(0), SYMBOLS[0])
    row["open_conflict"]["sources"][1]["open"] = row["open_conflict"]["sources"][0]["open"]


def _partial_halt_without_a_halt(payload: dict) -> None:
    row = make_actual_open_halt(payload, day_at(0), SYMBOLS[0])
    row.pop("halt")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (_missing_open_source, "open_source"),
        (_unknown_open_source_evidence, "open_source evidence_id"),
        (_empty_open_source_name, "open_source source"),
        (_extra_open_source_member, "open_source"),
        (_missing_status_evidence, "status_evidence_id"),
        (_unknown_status_evidence, "status_evidence_id"),
        (_status_evidence_reuses_the_price_source, "status_evidence_id"),
        (_suspended_without_status_evidence, "status_evidence_id"),
        (_missing_raw_price_basis, "raw_price_basis"),
        (_conflict_with_an_open, "open_conflict"),
        (_conflict_with_one_source, "open_conflict"),
        (_conflict_with_duplicate_sources, "open_conflict"),
        (_conflict_with_unknown_evidence, "open_conflict evidence_id"),
        (_conflict_with_negative_open, "open_conflict open"),
        (_conflict_with_identical_opens, "open_conflict"),
        (_partial_halt_without_a_halt, "halt"),
    ],
)
def test_schema_v3_seal_rejects_invalid_execution_evidence(
    tmp_path: Path, mutate, message: str
) -> None:
    payload = schema_v3_fixture()
    mutate(payload)
    result = _seal_rejected(tmp_path, payload)
    assert result.returncode != 0
    assert not result.stdout
    assert message in result.stderr


def test_schema_v2_keeps_its_contract_and_has_no_execution_evidence(
    tmp_path: Path,
) -> None:
    payload = schema_v2_fixture()
    sealed = ok(
        cli(
            "snapshot", "seal", "--input",
            str(write_json(tmp_path / "v2.json", payload)),
            "--store", str(tmp_path / "v2-store"),
        )
    )
    manifest = ok(
        cli(
            "snapshot", "manifest", "--store", str(tmp_path / "v2-store"),
            "--id", sealed["snapshot_id"],
        )
    )
    assert manifest["schema_version"] == 2
    assert "execution_evidence" not in manifest

    day = payload["calendar"][0]["date"]
    for key, value in (
        ("open_source", dict(OPEN_SOURCE)),
        ("status_evidence_id", STATUS_EVIDENCE),
        ("open_conflict", {"sources": []}),
    ):
        candidate = schema_v2_fixture()
        next(
            row for row in candidate["raw_bars"]
            if row["date"] == day and row["symbol"] == SYMBOLS[0]
        )[key] = value
        result = cli(
            "snapshot", "seal", "--input",
            str(write_json(tmp_path / f"v2-{key}.json", candidate)),
            "--store", str(tmp_path / f"v2-{key}-store"),
        )
        assert result.returncode != 0
        assert not result.stdout
        assert key in result.stderr
