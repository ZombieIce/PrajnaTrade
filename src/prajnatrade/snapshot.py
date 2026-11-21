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
    if not isinstance(data, dict) or data.get("schema_version") != 1 or data.get("data_kind") != "synthetic":
        raise ValueError("Snapshot input must be schema version 1 synthetic data.")
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

    records: dict[str, list[dict]] = {}
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
                if row.get("trading_status") not in ("trading", "suspended", "unknown"):
                    raise ValueError("Invalid or missing trading_status in raw_bars.")
                high, low = row["high"], row["low"]
                prices = [v for v in (row["open"], row["close"]) if v is not None]
                if ((high is not None and any(high < price for price in prices))
                    or (low is not None and any(low > price for price in prices))
                    or (high is not None and low is not None and high < low)):
                    raise ValueError("Invalid raw_bars OHLC values.")
        records[kind] = sorted(rows, key=lambda row: (row["date"], row.get("symbol", ""), _bytes(row)))

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
            and all(indexed[kind][day, symbol][field] is not None for field in fields)
            for kind, fields in (("raw_bars", RAW_FIELDS), ("derived", ("value",)))
            for symbol in ETF_SYMBOLS
        ) and day not in incomplete_days
    ]
    if not complete:
        raise ValueError("No complete common trading session.")
    start = next(day for day in dates if day >= start)
    end = complete[-1]
    sessions = [day for day in dates if start <= day <= end]
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
        "schema_version": 1, "data_kind": "synthetic", "symbols": sorted(ETF_SYMBOLS),
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
    return {"schema_version": 1, "snapshot_id": snapshot_id, "data_kind": "synthetic"}


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
    return {
        "schema_version": 1, "snapshot_id": snapshot_id, "partition": partition,
        "content_hash": _hash(content), "data": content,
        **({"as_of": cutoff.isoformat(), "partition_content_hash": fingerprint}
           if cutoff is not None else {}),
    }
