from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from prajnatrade.data import ETF_SYMBOLS

KINDS = ("calendar", "raw_bars", "corporate_actions", "derived")
RAW_FIELDS = ("open", "high", "low", "close", "volume")
SCHEMA_VERSION = {1: "synthetic", 2: "real", 3: "real"}
SYNTHETIC_STATUSES = ("trading", "suspended", "unknown")
REAL_STATUSES = ("trading", "partial_halt", "suspended", "unknown")
DERIVATION_METHOD = "cash_dividend_reinvested_at_ex_date_close"
HEX_SHA256 = re.compile(r"[0-9a-f]{64}")
DISCOVERY_PRIOR_HISTORY = "discovery_prior_history"
#: The schema-3 raw-bar keys that bind an executable open and a trading-status
#: notice to sealed evidence.  Schema 2 rejects them so its contract stays crisp.
SCHEMA_3_ROW_KEYS = ("open_source", "status_evidence_id", "open_conflict")
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


def _bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _hash(value: object) -> str:
    return hashlib.sha256(_bytes(value)).hexdigest()


def _date(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Trading dates must be ISO dates.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"Invalid trading date: {value!r}.") from error
    if parsed.isoformat() != value:
        raise ValueError(f"Invalid trading date: {value!r}.")
    return value


def _instant(value: object, key: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"Missing or invalid {key}; expected an ISO timestamp.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"Invalid {key}.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{key} must include a timezone.")
    return parsed


def _evidence(record: object, *, allow_unknown: bool = False) -> None:
    if not isinstance(record, dict):
        raise ValueError("Each source and record must be an object.")
    for key in ("available_at", "collected_at"):
        if key == "available_at" and allow_unknown and key in record and record[key] is None:
            continue
        value = record.get(key)
        if not isinstance(value, str):
            raise ValueError(f"Missing {key}; collection time is not availability evidence.")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError(f"Invalid {key}.") from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{key} must include a timezone.")
    if allow_unknown and "availability_evidence" in record and record["availability_evidence"] is None:
        return
    if not isinstance(record.get("availability_evidence"), str) or not record["availability_evidence"].strip():
        raise ValueError("Missing availability_evidence.")


def _input_kind(data: object) -> tuple[int, str]:
    """Return the declared schema version and data kind; never infer one."""
    if not isinstance(data, dict):
        raise ValueError("Snapshot input must be an object.")
    schema_version = data.get("schema_version")
    data_kind = data.get("data_kind")
    if type(schema_version) is int and SCHEMA_VERSION.get(schema_version) == data_kind:
        return schema_version, data_kind
    raise ValueError(
        "Snapshot input must be schema version 1 synthetic data or "
        "schema version 2 or 3 real data."
    )


def _evidence_bundle(data: dict) -> dict:
    evidence = data.get("evidence")
    if not isinstance(evidence, dict) or set(evidence) != {"items"}:
        raise ValueError("Real snapshot input requires an evidence block with only items.")
    items = evidence["items"]
    if not isinstance(items, list) or not items:
        raise ValueError("The evidence block requires at least one item.")
    seen = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {
            "id", "kind", "reference", "sha256", "available_at",
        }:
            raise ValueError(
                "Each evidence item needs id, kind, reference, sha256 and available_at."
            )
        for key in ("id", "kind", "reference"):
            if not isinstance(item[key], str) or not item[key].strip():
                raise ValueError(f"Invalid evidence {key}.")
        if item["id"] in seen:
            raise ValueError("Evidence item ids must be unique.")
        seen.add(item["id"])
        if not isinstance(item["sha256"], str) or not HEX_SHA256.fullmatch(item["sha256"]):
            raise ValueError("Evidence sha256 must be 64 lowercase hex characters.")
        _instant(item["available_at"], "available_at")
    return evidence


def _derivation(data: dict, rule: str) -> dict:
    derivation = data.get("derivation")
    if not isinstance(derivation, dict) or set(derivation) != {
        "rule_version", "method", "evidence",
    }:
        raise ValueError(
            "Real snapshot input requires a derivation block with rule_version, "
            "method and evidence."
        )
    if not isinstance(derivation["rule_version"], str) or not derivation["rule_version"].strip():
        raise ValueError("Invalid derivation rule_version.")
    if derivation["rule_version"] != rule:
        raise ValueError("The derivation rule_version must match derivation_rule_version.")
    if derivation["method"] != DERIVATION_METHOD:
        raise ValueError(
            f"Unsupported derivation method; expected {DERIVATION_METHOD!r}."
        )
    evidence = derivation["evidence"]
    if isinstance(evidence, str):
        nonempty = bool(evidence.strip())
    elif isinstance(evidence, list):
        nonempty = bool(evidence) and all(
            isinstance(item, str) and item.strip() for item in evidence
        )
    else:
        nonempty = False
    if not nonempty:
        raise ValueError("The derivation block requires non-empty evidence.")
    return derivation


def _halt(value: object, *, required: bool) -> dict | None:
    if value is None:
        if required:
            raise ValueError("A partial_halt raw bar requires a halt window.")
        return None
    if not isinstance(value, dict) or set(value) != {"start", "end"}:
        raise ValueError("Halt must be an object with start and end timestamps.")
    start = _instant(value["start"], "halt.start")
    end = _instant(value["end"], "halt.end")
    if end <= start:
        raise ValueError("The halt window must end after it starts.")
    return value


def _per_field_instant(row: dict, key: str, *, required: bool) -> None:
    if key not in row:
        raise ValueError(f"Missing {key} in raw_bars; per-field availability is required.")
    value = row[key]
    if value is None:
        if required:
            raise ValueError(f"Missing {key}; this trading status needs that timestamp.")
    else:
        _instant(value, key)


def _reject_schema_3_row_keys(row: dict) -> None:
    """Keep schema 2's raw-bar contract crisp: no schema-3 evidence keys."""
    if any(key in row for key in SCHEMA_3_ROW_KEYS):
        raise ValueError(
            "Schema version 2 raw bars cannot store open_source, status_evidence_id "
            "or open_conflict."
        )


def _open_source_evidence(row: dict, evidence_ids: set[str]) -> dict | None:
    """Validate the sealed price source of an actual daily open (schema 3).

    An open is executable only with the price source that produced it, and a row
    without an open cannot carry one.  A resumption notice is never a price
    source: it is validated separately as ``status_evidence_id``.
    """
    value = row.get("open_source")
    if row.get("open") is None:
        if value is not None:
            raise ValueError("A raw bar without an open cannot carry open_source.")
        return None
    if not isinstance(value, dict) or set(value) != {"source", "evidence_id"}:
        raise ValueError(
            "A schema-3 raw bar with an open requires open_source with source "
            "and evidence_id."
        )
    source = value.get("source")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("Invalid open_source source.")
    if value.get("evidence_id") not in evidence_ids:
        raise ValueError("Unknown open_source evidence_id in raw_bars.")
    return value


def _status_evidence(
    row: dict, evidence_ids: set[str], open_source: dict | None
) -> None:
    status = row.get("trading_status")
    value = row.get("status_evidence_id")
    if not isinstance(value, str) or not value.strip():
        if status in ("partial_halt", "suspended"):
            raise ValueError(
                "A schema-3 partial_halt or suspended raw bar requires "
                "status_evidence_id."
            )
        return
    if value not in evidence_ids:
        raise ValueError("Unknown status_evidence_id in raw_bars.")
    if open_source is not None and value == open_source["evidence_id"]:
        raise ValueError(
            "status_evidence_id must name the trading-status notice, not the "
            "open price source."
        )


def _open_conflict(row: dict, evidence_ids: set[str]) -> None:
    """Validate a sealed conflict; a disagreement is never silently resolved."""
    value = row.get("open_conflict")
    if value is None:
        return
    if row.get("open") is not None:
        raise ValueError(
            "An open_conflict raw bar must not store an executable open price."
        )
    if not isinstance(value, dict) or set(value) != {"sources"}:
        raise ValueError("open_conflict must be an object with only sources.")
    sources = value["sources"]
    if not isinstance(sources, list) or len(sources) < 2:
        raise ValueError("An open_conflict requires at least two source entries.")
    names: list[str] = []
    opens: list[float] = []
    for entry in sources:
        if not isinstance(entry, dict) or set(entry) != {"source", "evidence_id", "open"}:
            raise ValueError(
                "Each open_conflict source needs source, evidence_id and open."
            )
        name = entry["source"]
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Invalid open_conflict source name.")
        if entry["evidence_id"] not in evidence_ids:
            raise ValueError("Unknown open_conflict evidence_id in raw_bars.")
        price = entry["open"]
        if (
            isinstance(price, bool)
            or not isinstance(price, (int, float))
            or not math.isfinite(price)
            or price <= 0
        ):
            raise ValueError("open_conflict open prices must be positive and finite.")
        names.append(name)
        opens.append(float(price))
    if len(set(names)) != len(names):
        raise ValueError("open_conflict sources must be distinct.")
    if len(set(opens)) < 2:
        raise ValueError("An open_conflict requires at least two distinct open values.")


def _real_raw_bar_v2(row: dict, status: str) -> None:
    if status == "trading":
        if any(row.get(field) is None for field in RAW_FIELDS):
            raise ValueError(
                "A trading raw bar requires open, high, low, close and volume."
            )
        if row.get("halt") is not None:
            raise ValueError("A trading raw bar cannot declare a halt window.")
    elif status == "partial_halt":
        if row.get("open") is not None:
            raise ValueError(
                "A partial_halt raw bar must not store an executable open price."
            )
        _halt(row.get("halt"), required=True)
    elif status == "suspended":
        if any(row.get(field) is not None for field in RAW_FIELDS):
            raise ValueError(
                "A suspended raw bar requires null open, high, low, close and volume."
            )
        _halt(row.get("halt"), required=False)
    elif row.get("halt") is not None:
        raise ValueError("An unknown raw bar cannot declare a halt window.")


def _real_raw_bar_v3(row: dict, status: str, evidence_ids: set[str]) -> None:
    _open_conflict(row, evidence_ids)
    open_source = _open_source_evidence(row, evidence_ids)
    if status == "trading":
        if any(row.get(field) is None for field in RAW_FIELDS):
            raise ValueError(
                "A trading raw bar requires open, high, low, close and volume."
            )
        if row.get("halt") is not None:
            raise ValueError("A trading raw bar cannot declare a halt window.")
    elif status == "partial_halt":
        _halt(row.get("halt"), required=True)
    elif status == "suspended":
        if any(row.get(field) is not None for field in RAW_FIELDS):
            raise ValueError(
                "A suspended raw bar requires null open, high, low, close and volume."
            )
        _halt(row.get("halt"), required=False)
    elif row.get("halt") is not None:
        raise ValueError("An unknown raw bar cannot declare a halt window.")
    _status_evidence(row, evidence_ids, open_source)


def _real_raw_bar(row: dict, *, schema_version: int, evidence_ids: set[str]) -> None:
    status = row.get("trading_status")
    if status not in REAL_STATUSES:
        raise ValueError("Invalid or missing trading_status in raw_bars.")
    if schema_version >= 3:
        _real_raw_bar_v3(row, status, evidence_ids)
    else:
        _reject_schema_3_row_keys(row)
        _real_raw_bar_v2(row, status)
    _per_field_instant(
        row, "open_available_at",
        required=status in ("trading", "partial_halt") or row.get("open") is not None,
    )
    _per_field_instant(
        row, "close_available_at", required=row.get("close") is not None
    )


def _real_corporate_action(row: dict) -> None:
    if "record_date" in row and row["record_date"] is not None:
        _date(row["record_date"])
    action_type = row["type"]
    required = (
        ("cash_per_share", "pay_date", "terms_evidence")
        if action_type == "cash_dividend"
        else ("ratio", "terms_evidence")
    )
    if any(row.get(key) is None for key in required):
        raise ValueError(
            "Real corporate action terms must be present; use a verified value, "
            "not null."
        )


def _real_derived(rows: list[dict], raw_bars: dict[tuple[str, str], dict]) -> None:
    for row in rows:
        raw = raw_bars.get((row["date"], row["symbol"]))
        if raw is None:
            continue
        status = raw["trading_status"]
        if status == "trading" and row.get("value") is None:
            raise ValueError(
                "A real derived value must be present on a trading session."
            )
        if status == "suspended" and row.get("value") is not None:
            raise ValueError(
                "A real derived value must be null on a suspended session."
            )


def _complete_row(row: dict, kind: str, *, is_real: bool) -> bool:
    """Whether one row confirms its value; a real partial halt still has a close."""
    if kind == "derived":
        return row.get("value") is not None
    if not is_real:
        return all(row.get(field) is not None for field in RAW_FIELDS)
    status = row.get("trading_status")
    if status == "trading":
        return all(row.get(field) is not None for field in RAW_FIELDS)
    if status == "partial_halt":
        return row.get("close") is not None
    return False


_SHANGHAI = timezone(timedelta(hours=8))


def _halt_covers_session(halt: object, day: str) -> bool:
    """Whether a halt window spans the whole session (09:30-15:00 Shanghai).

    A session the notice closes for the whole day is a confirmed no-trade day;
    anything less leaves a required open the row does not store.
    """
    if not isinstance(halt, dict):
        return False
    try:
        start = _instant(halt["start"], "halt.start")
        end = _instant(halt["end"], "halt.end")
    except (KeyError, ValueError):
        return False
    session = datetime.fromisoformat(day).replace(tzinfo=_SHANGHAI)
    return start <= session.replace(hour=9, minute=30) and session.replace(
        hour=15, minute=0
    ) <= end


def _execution_evidence_row(row: dict) -> dict | None:
    """Classify one sealed session/symbol for the manifest execution evidence."""
    status = row["trading_status"]
    if status == "trading" and "open_conflict" not in row:
        return None
    record: dict = {
        "date": row["date"],
        "symbol": row["symbol"],
        "trading_status": status,
    }
    if "open_conflict" in row:
        record.update(
            classification="source_conflict", open_conflict=row["open_conflict"],
        )
    elif status == "unknown":
        record["classification"] = "unknown_status"
    elif status == "suspended":
        record["classification"] = "confirmed_no_trade"
    else:
        if row.get("open") is not None and row.get("open_source") is not None:
            record.update(
                classification="actual_open_after_halt",
                open=row["open"],
                open_source=row["open_source"],
            )
        elif _halt_covers_session(row.get("halt"), row["date"]):
            record["classification"] = "confirmed_no_trade"
        else:
            record.update(
                classification="missing_required_open",
                reason="partial_halt_without_actual_open",
            )
    if row.get("halt") is not None:
        record["halt"] = row["halt"]
    if row.get("status_evidence_id") is not None:
        record["status_evidence_id"] = row["status_evidence_id"]
    return record


def _execution_evidence(index: dict[tuple[str, str], dict], sessions: list[str]) -> dict:
    """Bind every non-trading sealed session to its execution classification.

    One row per sealed session/symbol whose ``trading_status`` is not
    ``trading`` or that carries an ``open_conflict``, sorted by date and symbol.
    This is metadata only: the pre-read gate can classify a known gap without
    opening the Discovery payload.
    """
    rows = []
    counts = {name: 0 for name in EXECUTION_CLASSIFICATIONS}
    for day in sessions:
        for symbol in sorted(ETF_SYMBOLS):
            row = index.get((day, symbol))
            if row is None:
                continue
            record = _execution_evidence_row(row)
            if record is None:
                continue
            rows.append(record)
            counts[record["classification"]] += 1
    rows.sort(key=lambda row: (row["date"], row["symbol"]))
    return {
        "schema_version": EXECUTION_EVIDENCE_SCHEMA_VERSION,
        "policy": dict(EXECUTION_EVIDENCE_POLICY),
        "rows": rows,
        "counts": counts,
    }


def _store_path(path: Path) -> Path:
    path = path.resolve()
    existing = path
    while not existing.exists():
        existing = existing.parent
    if not existing.is_dir():
        raise ValueError("Snapshot store must be a directory.")
    check = subprocess.run(
        ["git", "-C", str(existing), "rev-parse", "--absolute-git-dir"],
        capture_output=True, text=True, check=False,
    )
    if check.returncode == 0:
        raise ValueError("Snapshot store must be outside Git repositories.")
    return path


def _read_json(path: Path) -> object:
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def _prepare(data: object) -> tuple[dict, dict[str, dict]]:
    schema_version, data_kind = _input_kind(data)
    is_real = data_kind == "real"
    sources = data.get("sources")
    if not isinstance(sources, dict) or set(sources) != set(KINDS):
        raise ValueError("Sources must describe calendar, raw_bars, corporate_actions and derived.")
    for source in sources.values():
        _evidence(source)
        if any(not isinstance(source.get(k), str) or not source[k].strip() for k in ("source", "version")):
            raise ValueError("Each source needs a source and version.")
        if _date(source.get("coverage_start")) > _date(source.get("coverage_end")):
            raise ValueError("Source coverage ends before it starts.")
    rule = data.get("derivation_rule_version")
    if not isinstance(rule, str) or not rule.strip():
        raise ValueError("Missing derivation_rule_version.")
    evidence = derivation = None
    evidence_ids: set[str] = set()
    if is_real:
        evidence = _evidence_bundle(data)
        derivation = _derivation(data, rule)
        evidence_ids = {item["id"] for item in evidence["items"]}
        if schema_version >= 3:
            if data.get("raw_price_basis") != "unadjusted":
                raise ValueError(
                    "Schema version 3 real snapshots require raw_price_basis to be "
                    "declared as unadjusted."
                )
        elif data.get("raw_price_basis", "unadjusted") != "unadjusted":
            raise ValueError("Real snapshots require unadjusted raw prices.")

    records: dict[str, list[dict]] = {}
    real_raw_bars: dict[tuple[str, str], dict] = {}
    for kind in KINDS:
        rows = data.get(kind)
        if not isinstance(rows, list) or (kind != "corporate_actions" and not rows):
            raise ValueError(f"{kind} must be a nonempty array (actions may be empty).")
        for row in rows:
            _evidence(row, allow_unknown=True)
            _date(row.get("date"))
            if kind != "calendar" and row.get("symbol") not in ETF_SYMBOLS:
                raise ValueError(f"Invalid symbol in {kind}.")
            if kind == "corporate_actions":
                action_type = row.get("type")
                if action_type not in ("cash_dividend", "share_adjustment"):
                    raise ValueError("Invalid corporate action type.")
                field = "cash_per_share" if action_type == "cash_dividend" else "ratio"
                required = (field, "terms_evidence") + (("pay_date",) if action_type == "cash_dividend" else ())
                if any(key not in row for key in required):
                    raise ValueError("Corporate action terms must be present; use null for unknown terms.")
                value = row[field]
                if value is not None and (
                    isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= 0
                ):
                    raise ValueError("Invalid corporate action amount or ratio.")
                if row["terms_evidence"] is not None and (
                    not isinstance(row["terms_evidence"], str) or not row["terms_evidence"].strip()
                ):
                    raise ValueError("Invalid corporate action terms evidence.")
                if action_type == "cash_dividend" and row["pay_date"] is not None:
                    _date(row["pay_date"])
                if is_real:
                    _real_corporate_action(row)
            if kind in ("raw_bars", "derived"):
                for field in (RAW_FIELDS if kind == "raw_bars" else ("value",)):
                    value = row.get(field)
                    if value is not None and (
                        isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(value)
                        or (value < 0 if field == "volume" else value <= 0)
                    ):
                        raise ValueError(f"Invalid {field} in {kind}.")
                    if field not in row:
                        raise ValueError(f"Missing {field} in {kind}; use null for a gap.")
            if kind == "raw_bars":
                if row.get("trading_status") not in (
                    REAL_STATUSES if is_real else SYNTHETIC_STATUSES
                ):
                    raise ValueError("Invalid or missing trading_status in raw_bars.")
                high, low = row["high"], row["low"]
                prices = [v for v in (row["open"], row["close"]) if v is not None]
                if ((high is not None and any(high < price for price in prices))
                    or (low is not None and any(low > price for price in prices))
                    or (high is not None and low is not None and high < low)):
                    raise ValueError("Invalid raw_bars OHLC values.")
                if is_real:
                    _real_raw_bar(
                        row, schema_version=schema_version, evidence_ids=evidence_ids,
                    )
                    real_raw_bars[(row["date"], row["symbol"])] = row
        records[kind] = sorted(rows, key=lambda row: (row["date"], row.get("symbol", ""), _bytes(row)))
    if is_real:
        _real_derived(records["derived"], real_raw_bars)

    dates = [row["date"] for row in records["calendar"]]
    date_set = set(dates)
    if len(date_set) != len(dates):
        raise ValueError("Duplicate calendar date.")
    for kind in KINDS[1:]:
        if any(row["date"] not in date_set for row in records[kind]):
            raise ValueError(f"{kind} contains a date outside the calendar.")
    indexed = {}
    for kind in ("raw_bars", "derived"):
        indexed[kind] = {(row["date"], row["symbol"]): row for row in records[kind]}
        if len(indexed[kind]) != len(records[kind]):
            raise ValueError(f"Duplicate date and symbol in {kind}.")

    coverage = data.get("symbol_coverage")
    if not isinstance(coverage, dict) or set(coverage) != set(ETF_SYMBOLS):
        raise ValueError("symbol_coverage must declare coverage for all five ETFs.")
    for symbol, interval in coverage.items():
        if not isinstance(interval, dict) or _date(interval.get("start")) > _date(interval.get("end")):
            raise ValueError(f"Invalid coverage for {symbol}.")
    start = max(
        *(interval["start"] for interval in coverage.values()),
        *(source["coverage_start"] for source in sources.values()),
    )
    source_end = min(
        *(interval["end"] for interval in coverage.values()),
        *(source["coverage_end"] for source in sources.values()),
    )
    action_gaps = []
    for row in records["corporate_actions"]:
        fields = ("cash_per_share", "pay_date", "terms_evidence") if row["type"] == "cash_dividend" else ("ratio", "terms_evidence")
        for field in fields:
            if row[field] is None:
                action_gaps.append({"date": row["date"], "symbol": row["symbol"], "field": field})
    availability_gaps = [
        {"kind": kind, "date": row["date"], "symbol": row.get("symbol")}
        for kind in KINDS for row in records[kind]
        if row["available_at"] is None or row["availability_evidence"] is None
    ]
    incomplete_days = {gap["date"] for gap in action_gaps + availability_gaps}
    incomplete_days.update(
        row["date"] for row in records["raw_bars"] if row["trading_status"] == "unknown"
    )
    complete = [
        day for day in dates if start <= day <= source_end and all(
            (day, symbol) in indexed[kind]
            and _complete_row(indexed[kind][day, symbol], kind, is_real=is_real)
            for kind in ("raw_bars", "derived")
            for symbol in ETF_SYMBOLS
        ) and day not in incomplete_days
    ]
    if not complete:
        raise ValueError("No complete common trading session.")
    start = next(day for day in dates if day >= start)
    end = complete[-1]
    sessions = [day for day in dates if start <= day <= end]
    prior_history = None
    if "discovery_start" in data:
        discovery_start = _date(data["discovery_start"])
        if discovery_start not in sessions:
            raise ValueError("discovery_start must be a trading session in the common range.")
        prior_days = {day for day in sessions if day < discovery_start}
        if prior_days:
            prior_history = {
                kind: [row for row in records[kind] if row["date"] in prior_days]
                for kind in ("calendar", "derived")
            }
        start = discovery_start
        sessions = [day for day in sessions if day >= start]
    if len(sessions) < 5:
        raise ValueError("At least five common trading sessions are required.")
    gaps = {
        kind: [
            {"date": day, "symbol": symbol}
            for day in sessions for symbol in sorted(ETF_SYMBOLS)
            if (day, symbol) not in indexed[kind]
            or any(indexed[kind][day, symbol][field] is None for field in fields)
            or (kind == "raw_bars" and indexed[kind][day, symbol]["trading_status"] == "unknown")
        ]
        for kind, fields in (("raw_bars", RAW_FIELDS), ("derived", ("value",)))
    }
    gaps["corporate_actions"] = [gap for gap in action_gaps if start <= gap["date"] <= end]
    gaps["availability"] = [gap for gap in availability_gaps if start <= gap["date"] <= end]
    discovery_end = len(sessions) * 3 // 5
    validation_end = discovery_end + len(sessions) // 5
    groups = (sessions[:discovery_end], sessions[discovery_end:validation_end], sessions[validation_end:])
    partitions = {}
    descriptions = []
    for name, days in zip(("discovery", "validation", "final_holdout"), groups, strict=True):
        day_set = set(days)
        content = {kind: [row for row in records[kind] if row["date"] in day_set] for kind in KINDS}
        partitions[name] = content
        descriptions.append({
            "name": name, "start": days[0], "end": days[-1], "sessions": len(days),
            "content_hash": _hash(content),
        })
    manifest = {
        "schema_version": schema_version, "data_kind": data_kind,
        "symbols": sorted(ETF_SYMBOLS),
        "sources": sources, "derivation_rule_version": rule,
        "symbol_coverage": coverage,
        "common_range": {"start": start, "end": end}, "gaps": gaps,
        "partitions": descriptions,
        "content_hashes": {
            kind: _hash([row for row in records[kind] if start <= row["date"] <= end])
            for kind in KINDS
        },
        "coverage": {
            symbol: {
                kind: {
                    "start": min((r["date"] for r in records[kind] if r["symbol"] == symbol), default=None),
                    "end": max((r["date"] for r in records[kind] if r["symbol"] == symbol), default=None),
                }
                for kind in ("raw_bars", "derived")
            } for symbol in sorted(ETF_SYMBOLS)
        },
    }
    if is_real:
        manifest["raw_price_basis"] = "unadjusted"
        manifest["evidence"] = evidence
        manifest["derivation"] = derivation
        if schema_version >= 3:
            manifest["execution_evidence"] = _execution_evidence(
                indexed["raw_bars"], sessions
            )
    if prior_history:
        calendar = prior_history["calendar"]
        manifest[DISCOVERY_PRIOR_HISTORY] = {
            "start": calendar[0]["date"],
            "end": calendar[-1]["date"],
            "sessions": len(calendar),
            "content_hash": _hash(prior_history),
        }
        partitions[DISCOVERY_PRIOR_HISTORY] = prior_history
    return manifest, partitions


def _manifest(store: Path, snapshot_id: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{64}", snapshot_id):
        raise ValueError("Invalid snapshot ID.")
    value = _read_json(store / snapshot_id / "manifest.json")
    if not isinstance(value, dict) or _hash(value) != snapshot_id:
        raise ValueError("Snapshot manifest integrity check failed.")
    return value


def seal(source: Path, store: Path) -> dict:
    store = _store_path(store)
    manifest, partitions = _prepare(_read_json(source))
    return _publish(store, manifest, partitions)


def _publish(store: Path, manifest: dict, partitions: dict) -> dict:
    snapshot_id = _hash(manifest)
    store.mkdir(parents=True, exist_ok=True)
    target = store / snapshot_id
    if target.exists():
        if _manifest(store, snapshot_id) != manifest:
            raise ValueError("Existing snapshot manifest differs.")
        for name, value in partitions.items():
            if _hash(_read_json(target / f"{name}.json")) != _hash(value):
                raise ValueError("Existing snapshot partition integrity check failed.")
    else:
        staging = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=store))
        try:
            for name, value in (("manifest", manifest), *partitions.items()):
                path = staging / f"{name}.json"
                path.write_bytes(_bytes(value))
                path.chmod(0o444)
            try:
                os.rename(staging, target)
            except FileExistsError:
                raise ValueError(
                    "Snapshot was concurrently created; retry to verify it."
                ) from None
            target.chmod(0o555)
        finally:
            if staging.exists():
                staging.chmod(0o755)
                shutil.rmtree(staging)
    return {
        "schema_version": manifest["schema_version"],
        "snapshot_id": snapshot_id,
        "data_kind": manifest["data_kind"],
    }


def manifest(store: Path, snapshot_id: str) -> dict:
    return {"snapshot_id": snapshot_id, **_manifest(_store_path(store), snapshot_id)}


def lineage_ids(store: Path, snapshot_id: str) -> list[str]:
    """Snapshot ID followed by its ``lineage.predecessor_id`` chain.

    Metadata only: this reads each snapshot's immutable ``manifest.json`` and
    never opens a partition payload.  A malformed or cyclic lineage raises
    ``ValueError``; there is no fallback to another snapshot.
    """
    if not isinstance(snapshot_id, str) or not re.fullmatch(r"[0-9a-f]{64}", snapshot_id):
        raise ValueError("Invalid snapshot ID.")
    store = _store_path(Path(store))
    ids: list[str] = []
    current = snapshot_id
    while True:
        if current in ids:
            raise ValueError("The snapshot lineage contains a cycle.")
        ids.append(current)
        metadata = _manifest(store, current)
        if "lineage" not in metadata:
            return ids
        lineage = metadata["lineage"]
        if not isinstance(lineage, dict):
            raise ValueError("The snapshot lineage is not an object.")
        predecessor = lineage.get("predecessor_id")
        if not isinstance(predecessor, str) or not re.fullmatch(r"[0-9a-f]{64}", predecessor):
            raise ValueError("The snapshot lineage has an invalid predecessor ID.")
        current = predecessor


def _discovery_descriptor(metadata: dict) -> dict:
    return next(p for p in metadata["partitions"] if p["name"] == "discovery")


def _discovery_history(store: Path, snapshot_id: str, metadata: dict) -> tuple[dict, list[dict]]:
    revisions = []
    while "lineage" in metadata:
        if "discovery_dividend" in metadata:
            revisions.append(metadata["discovery_dividend"])
        snapshot_id = metadata["lineage"]["predecessor_id"]
        metadata = _manifest(store, snapshot_id)
    content = _read_json(store / snapshot_id / "discovery.json")
    if _hash(content) != _discovery_descriptor(metadata)["content_hash"]:
        raise ValueError("Discovery partition integrity check failed.")
    return content, list(reversed(revisions))


def _dividend_index(data: dict, symbol: str, ex_date: str) -> int:
    matches = [
        i for i, row in enumerate(data["corporate_actions"])
        if row["type"] == "cash_dividend" and row["symbol"] == symbol and row["date"] == ex_date
    ]
    if len(matches) != 1:
        raise ValueError("Expected exactly one cash dividend for this ETF and ex-date.")
    return matches[0]


def _known_at(row: dict, cutoff: datetime) -> bool:
    return (
        row["available_at"] is not None and row["availability_evidence"] is not None
        and _instant(row["available_at"], "available_at") <= cutoff
    )


def _discovery_view(base: dict, revisions: list[dict], cutoff: datetime | None = None) -> dict:
    data = {kind: list(rows) for kind, rows in base.items()}
    affected = set()
    for rev in revisions:
        available = _instant(rev["official_available_at"], "official_available_at")
        if cutoff is not None and available > cutoff:
            continue
        idx = _dividend_index(data, rev["symbol"], rev["ex_date"])
        action = data["corporate_actions"][idx]
        if action["cash_per_share"] != rev["old_amount"]:
            raise ValueError("The old amount does not match the predecessor's cash dividend.")
        original = base["corporate_actions"][idx]
        if cutoff is not None and not _known_at(original, cutoff):
            original = {"date": action["date"], "symbol": action["symbol"], "type": "cash_dividend",
                        "pay_date": None, "terms_evidence": None}
        data["corporate_actions"][idx] = {
            **original, "cash_per_share": rev["new_amount"],
            "available_at": rev["official_available_at"], "collected_at": rev["collected_at"],
            "availability_evidence": rev["url"],
        }
        affected.add(rev["symbol"])
    # No point-in-time derivation engine is attached to these snapshots.
    data["derived"] = [
        {**row, "value": None} if row["symbol"] in affected else row for row in data["derived"]
    ]
    if cutoff is not None:
        local_day = cutoff.astimezone(timezone(timedelta(hours=8))).date().isoformat()
        data = {
            kind: [row for row in rows
                   if row["date"] <= local_day and _known_at(row, cutoff)]
            for kind, rows in data.items()
        }
    return data


PROTECTED_PARTITIONS = ("validation", "final_holdout")


def partition_descriptor(metadata: dict, partition: str) -> dict:
    """Return the frozen descriptor for one partition of a sealed manifest."""
    if partition not in ("discovery", *PROTECTED_PARTITIONS):
        raise ValueError(f"Unknown snapshot partition: {partition!r}.")
    matches = [
        item for item in metadata.get("partitions", [])
        if isinstance(item, dict) and item.get("name") == partition
    ]
    if len(matches) != 1:
        raise ValueError(f"Missing or duplicate {partition} partition descriptor.")
    descriptor = matches[0]
    fields = ("start", "end", "sessions", "content_hash")
    if any(field not in descriptor for field in fields):
        raise ValueError(f"Incomplete {partition} partition descriptor.")
    _date(descriptor["start"])
    _date(descriptor["end"])
    if type(descriptor["sessions"]) is not int or descriptor["sessions"] < 1:
        raise ValueError(f"Invalid {partition} partition session count.")
    if not isinstance(descriptor["content_hash"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", descriptor["content_hash"]
    ):
        raise ValueError(f"Invalid {partition} partition content hash.")
    return {
        "name": partition,
        "start": descriptor["start"],
        "end": descriptor["end"],
        "sessions": descriptor["sessions"],
        "content_hash": descriptor["content_hash"],
    }


def read_protected(store: Path, snapshot_id: str, partition: str) -> dict:
    """Trusted read of a sealed Validation or Final Holdout partition.

    Research-side access stays limited to Discovery; this reader is only called by
    the trusted evaluation workflow after authorization, isolation and a ledger
    debit.  Dividend-successor manifests inherit protected payloads, so the reader
    walks ``lineage.predecessor_id`` until the payload is found and always verifies
    it against the descriptor of the manifest that was *requested*.
    """
    if partition not in PROTECTED_PARTITIONS:
        raise ValueError(
            "Protected access is limited to Validation and Final Holdout."
        )
    store = _store_path(store)
    metadata = _manifest(store, snapshot_id)
    descriptor = partition_descriptor(metadata, partition)
    lineage: list[str] = []
    current = snapshot_id
    payload = None
    while True:
        lineage.append(current)
        path = store / current / f"{partition}.json"
        if path.is_file():
            payload = _read_json(path)
            break
        current_metadata = metadata if current == snapshot_id else _manifest(store, current)
        lineage_meta = current_metadata.get("lineage")
        if not isinstance(lineage_meta, dict) or not lineage_meta.get(
            "protected_partitions_inherited"
        ):
            raise ValueError(f"The {partition} partition payload is missing.")
        predecessor = lineage_meta.get("predecessor_id")
        if not isinstance(predecessor, str) or predecessor in lineage:
            raise ValueError(f"The {partition} partition lineage is invalid.")
        current = predecessor
    if _hash(payload) != descriptor["content_hash"]:
        raise ValueError(f"{partition} partition integrity check failed.")
    return {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "partition": partition,
        "content_hash": descriptor["content_hash"],
        "data": payload,
        "descriptor": {key: descriptor[key] for key in (
            "start", "end", "sessions", "content_hash"
        )},
        "lineage": lineage,
    }


def read_discovery(store: Path, snapshot_id: str, partition: str, as_of: str | None = None) -> dict:
    if partition != "discovery":
        raise ValueError("Research access is limited to Discovery.")
    store = _store_path(store)
    metadata = _manifest(store, snapshot_id)
    if "lineage" in metadata and as_of is None:
        raise ValueError("Successor Discovery reads require --as-of.")
    cutoff = _instant(as_of, "--as-of") if as_of is not None else None
    base, revisions = _discovery_history(store, snapshot_id, metadata)
    fingerprint = _discovery_descriptor(metadata)["content_hash"]
    latest = _discovery_view(base, revisions) if revisions else base
    if _hash(latest) != fingerprint:
        raise ValueError("Discovery partition integrity check failed.")
    content = latest if cutoff is None else _discovery_view(base, revisions, cutoff)
    prior = _discovery_prior_history(store, snapshot_id, metadata, cutoff)
    return {
        "schema_version": metadata["schema_version"],
        "data_kind": metadata["data_kind"],
        "snapshot_id": snapshot_id, "partition": partition,
        "content_hash": _hash(content), "data": content,
        **({"prior_history": prior, "prior_history_hash": _hash(prior)} if prior is not None else {}),
        **({"as_of": cutoff.isoformat(), "partition_content_hash": fingerprint}
           if cutoff is not None else {}),
    }


SPLIT_SCHEMA_VERSION = 1
SPLIT_RULE = "leading-development-share"
SPLIT_BOUNDARY_RULE = "floor(development_sessions = sessions * 70 / 100)"
SPLIT_DEVELOPMENT_SHARE = 70
SPLIT_VALIDATION_SHARE = 100 - SPLIT_DEVELOPMENT_SHARE
_SPLIT_FIELDS = frozenset(
    {
        "schema_version", "rule", "ratio", "boundary", "as_of", "partition",
        "development", "trailing", "split_hash",
    }
)
_SPLIT_SEGMENT_FIELDS = frozenset({"start", "end", "sessions", "content_hash"})


def _split_content(content: object) -> dict[str, list[dict]]:
    """Validate a sealed ``{kind: rows}`` payload and return its four kind arrays."""
    if not isinstance(content, dict) or set(content) != set(KINDS):
        raise ValueError(
            "Discovery content must contain calendar, raw_bars, corporate_actions "
            "and derived."
        )
    for kind in KINDS:
        rows = content[kind]
        if not isinstance(rows, list):
            raise ValueError(f"Discovery content {kind} must be an array.")
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"Discovery content {kind} rows must be objects.")
    return {kind: content[kind] for kind in KINDS}


def _split_sessions(content: dict[str, list[dict]]) -> list[str]:
    sessions = sorted({_date(row.get("date")) for row in content["calendar"]})
    if not sessions:
        raise ValueError("Discovery content has no trading session.")
    return sessions


def _split_segment(descriptor: object, label: str) -> dict:
    if not isinstance(descriptor, dict) or set(descriptor) != _SPLIT_SEGMENT_FIELDS:
        raise ValueError(f"The frozen Discovery split {label} descriptor is incomplete.")
    _date(descriptor["start"])
    _date(descriptor["end"])
    if descriptor["start"] > descriptor["end"]:
        raise ValueError(f"The frozen Discovery split {label} range is reversed.")
    if type(descriptor["sessions"]) is not int or descriptor["sessions"] < 1:
        raise ValueError(f"The frozen Discovery split {label} session count is invalid.")
    if not isinstance(descriptor["content_hash"], str) or not HEX_SHA256.fullmatch(
        descriptor["content_hash"]
    ):
        raise ValueError(f"The frozen Discovery split {label} content hash is invalid.")
    return {
        "start": descriptor["start"],
        "end": descriptor["end"],
        "sessions": descriptor["sessions"],
        "content_hash": descriptor["content_hash"],
    }


def slice_content(content: object, start: str, end: str) -> dict[str, list[dict]]:
    """Copy one date range of a sealed Discovery content payload.

    The payload is the ``{"calendar", "raw_bars", "corporate_actions",
    "derived"}`` object returned by :func:`read_discovery`; rows keep their
    sealed canonical order and the four kind arrays are always present.  This is
    the one place the slice rows are derived, so the frozen split, the
    development-only Campaign copy and the formal trailing run cannot drift.
    """
    rows = _split_content(content)
    start, end = _date(start), _date(end)
    if start > end:
        raise ValueError("A partition slice starts after it ends.")
    return {
        kind: [row for row in rows[kind] if start <= _date(row.get("date")) <= end]
        for kind in KINDS
    }


def partition_slice(content: object, start: str, end: str) -> dict:
    """Fingerprint one date range of a sealed Discovery content payload.

    The payload is the ``{"calendar", "raw_bars", "corporate_actions",
    "derived"}`` object returned by :func:`read_discovery`; rows keep their
    sealed canonical order.  The returned ``content_hash`` is the seal's
    per-partition convention: the canonical hash of ``{kind: rows}`` for the
    rows whose date falls inside ``[start, end]``.  The returned ``start`` and
    ``end`` are the first and last trading session actually covered, so a range
    aligned to trading sessions reproduces the requested bounds exactly.
    """
    sliced = slice_content(content, start, end)
    sessions = _split_sessions(sliced)
    return {
        "start": sessions[0],
        "end": sessions[-1],
        "sessions": len(sessions),
        "content_hash": _hash(sliced),
    }


def discovery_split(
    content: object, *, development: int = SPLIT_DEVELOPMENT_SHARE,
    as_of: str | None = None,
) -> dict:
    """Freeze the 70/30 split of a Campaign's Discovery interval.

    The split covers the sealed Discovery payload (or its ``as_of``
    projection), never the sealed ``validation``/``final_holdout`` partitions.
    ``development_sessions = N * 70 // 100`` (floor); the remainder stays on the
    trailing side.  Both segments are non-empty and together they are
    contiguous, disjoint and exhaustive.  Only the leading-development share is
    implemented; other ratios are rejected.  ``partition.content_hash`` is the
    hash of the payload the split was computed from (the sealed partition hash
    for a direct read, the projection hash for an ``as_of`` read), matching
    :func:`read_discovery`'s ``content_hash`` for that read.
    """
    if type(development) is not int or development != SPLIT_DEVELOPMENT_SHARE:
        raise ValueError("Only the leading 70/30 Discovery split is implemented.")
    rows = _split_content(content)
    sessions = _split_sessions(rows)
    total = len(sessions)
    development_sessions = total * SPLIT_DEVELOPMENT_SHARE // 100
    if development_sessions < 1 or total - development_sessions < 1:
        raise ValueError("A Discovery split needs non-empty development and trailing segments.")
    if as_of is not None:
        _instant(as_of, "as_of")
    body = {
        "schema_version": SPLIT_SCHEMA_VERSION,
        "rule": SPLIT_RULE,
        "ratio": {
            "development": SPLIT_DEVELOPMENT_SHARE,
            "validation": SPLIT_VALIDATION_SHARE,
        },
        "boundary": SPLIT_BOUNDARY_RULE,
        "as_of": as_of,
        "partition": partition_slice(rows, sessions[0], sessions[-1]),
        "development": partition_slice(
            rows, sessions[0], sessions[development_sessions - 1]
        ),
        "trailing": partition_slice(rows, sessions[development_sessions], sessions[-1]),
    }
    return {**body, "split_hash": _hash(body)}


def verify_discovery_split(descriptor: object, content: object | None = None) -> dict:
    """Verify a frozen split descriptor's internal consistency.

    Checks the fixed rule and ratio, the floor boundary, the segment ordering
    and session arithmetic, the date bounds and every 64-hex fingerprint, and
    recomputes ``split_hash`` over the descriptor body.  When ``content`` is
    supplied (the same Discovery payload or ``as_of`` projection the split was
    frozen from), the descriptor must also reproduce exactly from that payload,
    including the real session dates and slice fingerprints.  Returns the
    verified descriptor unchanged.
    """
    if not isinstance(descriptor, dict) or set(descriptor) != _SPLIT_FIELDS:
        raise ValueError("The frozen Discovery split descriptor is incomplete.")
    if type(descriptor["schema_version"]) is not int or descriptor["schema_version"] != 1:
        raise ValueError("Unsupported frozen Discovery split schema version.")
    if descriptor["rule"] != SPLIT_RULE or descriptor["boundary"] != SPLIT_BOUNDARY_RULE:
        raise ValueError("The frozen Discovery split rule is not the supported 70/30 floor rule.")
    ratio = descriptor["ratio"]
    if not isinstance(ratio, dict) or set(ratio) != {"development", "validation"} or any(
        type(value) is not int for value in ratio.values()
    ) or ratio != {
        "development": SPLIT_DEVELOPMENT_SHARE,
        "validation": SPLIT_VALIDATION_SHARE,
    }:
        raise ValueError("The frozen Discovery split ratio is not the supported 70/30 ratio.")
    partition = _split_segment(descriptor["partition"], "partition")
    development = _split_segment(descriptor["development"], "development")
    trailing = _split_segment(descriptor["trailing"], "trailing")
    if partition["sessions"] < 2:
        raise ValueError("The frozen Discovery split needs at least two sessions.")
    if development["sessions"] != partition["sessions"] * SPLIT_DEVELOPMENT_SHARE // 100:
        raise ValueError("The frozen Discovery split boundary is not the floor 70% rule.")
    if trailing["sessions"] != partition["sessions"] - development["sessions"]:
        raise ValueError("The frozen Discovery split does not partition every session.")
    if development["start"] != partition["start"] or trailing["end"] != partition["end"]:
        raise ValueError("The frozen Discovery split does not cover the whole interval.")
    if development["end"] >= trailing["start"]:
        raise ValueError("The frozen Discovery split segments are not contiguous.")
    if not isinstance(descriptor["split_hash"], str) or not HEX_SHA256.fullmatch(
        descriptor["split_hash"]
    ):
        raise ValueError("The frozen Discovery split hash is invalid.")
    if descriptor["as_of"] is not None:
        _instant(descriptor["as_of"], "as_of")
    body = {key: value for key, value in descriptor.items() if key != "split_hash"}
    if descriptor["split_hash"] != _hash(body):
        raise ValueError("The frozen Discovery split hash does not cover its body.")
    if content is not None:
        recomputed = discovery_split(
            content,
            development=ratio["development"],
            as_of=descriptor["as_of"],
        )
        if recomputed != descriptor:
            raise ValueError("The frozen Discovery split does not reproduce from the payload.")
    return descriptor


def _discovery_prior_history(
    store: Path, snapshot_id: str, metadata: dict, cutoff: datetime | None,
) -> dict | None:
    descriptor = metadata.get(DISCOVERY_PRIOR_HISTORY)
    if descriptor is None:
        return None
    founding_snapshot = lineage_ids(store, snapshot_id)[-1] if "lineage" in metadata else snapshot_id
    payload = _read_json(store / founding_snapshot / f"{DISCOVERY_PRIOR_HISTORY}.json")
    if _hash(payload) != descriptor["content_hash"]:
        raise ValueError("Discovery prior history integrity check failed.")
    local_day = cutoff.astimezone(timezone(timedelta(hours=8))).date().isoformat() if cutoff else None
    calendar = {
        row["date"]: row for row in payload["calendar"]
        if local_day is None or row["date"] <= local_day
    }
    rows = []
    for row in payload["derived"]:
        session = calendar.get(row["date"])
        if session is None or any(
            item["available_at"] is None or item["availability_evidence"] is None
            for item in (session, row)
        ):
            continue
        availability = max((session, row), key=lambda item: _instant(item["available_at"], "available_at"))
        if cutoff is None or _known_at(availability, cutoff):
            rows.append({**row, "available_at": availability["available_at"]})
    return {"sessions": list(calendar), "derived": rows}
