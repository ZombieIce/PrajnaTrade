from __future__ import annotations

import math
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from prajnatrade import snapshot
from prajnatrade.trading_status import ETF_SYMBOLS


class RevisionError(ValueError):
    """A stored dividend revision cannot be used as trusted evidence (#31)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _object(value: object, fields: str) -> dict:
    if not isinstance(value, dict) or set(value) != set(fields.split()):
        raise ValueError(f"Expected fields: {fields}.")
    return value


def _text(value: object, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing {field}.")


def _amount(value: object) -> float:
    if type(value) not in (int, float) or not 0 < value <= 1e308 or not math.isfinite(value):
        raise ValueError("Dividend amounts must be positive finite CNY per unit.")
    return float(value)


def _journal(store: Path, kind: str, record_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", record_id):
        raise ValueError(f"Invalid correction {kind} ID.")
    return store / ".dividend-corrections" / f"{kind}-{record_id}.json"


def _file_once(path: Path, record: dict) -> dict:
    path.parent.mkdir(mode=0o700, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent) as staged:
        staged.write(snapshot._bytes(record))
        staged.flush()
        os.fsync(staged.fileno())
        os.chmod(staged.name, 0o400)
        try:
            os.link(staged.name, path)
        except FileExistsError:
            return snapshot._read_json(path)
    return record


def _partition(metadata: dict, ex_date: str) -> str:
    for part in metadata["partitions"]:
        if part["start"] <= ex_date <= part["end"]:
            return part["name"]
    raise ValueError("Ex-date is outside the predecessor's partitions.")


def register(source: Path, store: Path) -> dict:
    store = snapshot._store_path(store)
    proposal = _object(snapshot._read_json(source),
                       "schema_version predecessor_id symbol ex_date currency old_amount new_amount supplier")
    if (type(proposal["schema_version"]) is not int or proposal["schema_version"] != 1
            or proposal["symbol"] not in ETF_SYMBOLS or proposal["currency"] != "CNY"):
        raise ValueError("Expected schema version 1 and a five-ETF CNY cash dividend.")
    _text(proposal["predecessor_id"], "predecessor_id")
    ex_date = snapshot._date(proposal["ex_date"])
    for field in ("old_amount", "new_amount"):
        proposal[field] = _amount(proposal[field])
    if proposal["old_amount"] == proposal["new_amount"]:
        raise ValueError("The old and new amounts must differ.")
    supplier = _object(proposal["supplier"], "source archive_reference observed_at")
    for field in ("source", "archive_reference"):
        _text(supplier[field], field)
    snapshot._instant(supplier["observed_at"], "observed_at")

    previous = snapshot._manifest(store, proposal["predecessor_id"])
    partition = _partition(previous, ex_date)
    if partition == "discovery":
        base, history = snapshot._discovery_history(store, proposal["predecessor_id"], previous)
        data = snapshot._discovery_view(base, history)
        idx = snapshot._dividend_index(data, proposal["symbol"], ex_date)
        if data["corporate_actions"][idx]["cash_per_share"] != proposal["old_amount"]:
            raise ValueError("The old amount does not match the predecessor's cash dividend.")

    proposal_id = snapshot._hash(proposal)
    receipt = {"schema_version": 1, "data_kind": previous["data_kind"],
               "status": "pending_verification", "proposal_id": proposal_id,
               "partition": partition, "proposal": proposal}
    if _file_once(_journal(store, "proposal", proposal_id), receipt) != receipt:
        raise ValueError("Existing supplier observation differs.")
    return receipt


def _pending(store: Path, proposal_id: str) -> dict:
    record = snapshot._read_json(_journal(store, "proposal", proposal_id))
    if not isinstance(record, dict) or snapshot._hash(record.get("proposal")) != proposal_id:
        raise ValueError("Supplier observation integrity check failed.")
    return record


def _verification_key(proposal_id: str, revision_id: str) -> str:
    return snapshot._hash([proposal_id, revision_id])


def verify(source: Path, store: Path, proposal_id: str) -> dict:
    store = snapshot._store_path(store)
    pending = _pending(store, proposal_id)
    proposal = pending["proposal"]
    notice = _object(snapshot._read_json(source),
                     "kind authority issuer url reference symbol ex_date currency amount available_at collected_at")
    if notice["kind"] != "synthetic":
        raise ValueError("This snapshot workflow accepts only explicitly synthetic notices.")
    if notice["authority"] not in ("exchange", "fund_manager"):
        raise ValueError("Evidence authority must be an exchange or fund manager.")
    for field in ("issuer", "reference", "url"):
        _text(notice[field], field)
    url = urlsplit(notice["url"])
    if url.scheme != "https" or not (url.hostname or "").endswith(".invalid") or url.username is not None:
        raise ValueError("Synthetic notices require an HTTPS URL under .invalid.")
    if (any(notice[key] != proposal[key] for key in ("symbol", "ex_date", "currency"))
            or _amount(notice["amount"]) != proposal["new_amount"]):
        raise ValueError("The notice must match the proposed ETF, ex-date, currency, and new amount.")
    available = snapshot._instant(notice["available_at"], "available_at")
    collected = snapshot._instant(notice["collected_at"], "collected_at")
    now = datetime.now(UTC)
    if not available <= collected <= now:
        raise ValueError("Official availability must precede collection and actual filing.")
    if snapshot._instant(proposal["supplier"]["observed_at"], "observed_at") > now:
        raise ValueError("Supplier observation cannot be in the future at filing.")

    revision = {key: proposal[key] for key in ("symbol", "ex_date", "currency", "old_amount", "new_amount")}
    revision["official_available_at"] = available.astimezone(UTC).isoformat()
    revision_id = snapshot._hash(revision)
    receipt = {"schema_version": 1, "data_kind": "synthetic", "status": "verified",
               "revision_id": revision_id, "revision": revision, "evidence": notice,
               "filed_at": now.isoformat()}
    filed = _file_once(_journal(store, "revision", revision_id), receipt)
    if filed.get("revision") != revision or filed.get("revision_id") != revision_id:
        raise ValueError("Existing official revision differs.")
    link = {"proposal_id": proposal_id, "revision_id": revision_id,
            "verification_hash": snapshot._hash(filed)}
    if _file_once(_journal(store, "verification", _verification_key(proposal_id, revision_id)), link) != link:
        raise ValueError("Existing verification link differs.")
    # Keep alternate evidence without changing the original revision or its filing time.
    evidence_id = snapshot._hash([revision_id, notice])
    _file_once(_journal(store, "notice", evidence_id),
               {"revision_id": revision_id, "evidence": notice, "filed_at": now.isoformat()})
    return filed


def load_verified_revision(store: str | Path, revision_id: str) -> dict:
    """Load one verified revision journal record for trusted impact review (#31).

    The journal entry is written once by :func:`verify` and is usable only while
    it is ``verified`` and ``sha256(revision)`` equals the requested revision ID.
    A proposal, an unverified observation or a tampered record is never evidence.
    Reading the journal never opens a sealed partition and never debits a query.
    """
    if not isinstance(revision_id, str) or not re.fullmatch(r"[0-9a-f]{64}", revision_id):
        raise ValueError(
            "Invalid revision ID; revision IDs are 64-character lowercase "
            "hexadecimal content addresses."
        )
    store_path = snapshot._store_path(Path(store))
    path = _journal(store_path, "revision", revision_id)
    if not path.is_file():
        raise RevisionError(
            "REVISION_NOT_FOUND",
            f"No verified revision {revision_id} is stored in the snapshot-store journal.",
        )
    try:
        record = snapshot._read_json(path)
    except (OSError, UnicodeError, ValueError) as error:
        raise RevisionError(
            "REVISION_INVALID", f"The stored revision is unreadable: {error}"
        ) from error
    if not isinstance(record, dict):
        raise RevisionError(
            "REVISION_INVALID", "The stored revision journal record is not an object."
        )
    if record.get("status") != "verified":
        raise RevisionError(
            "REVISION_NOT_VERIFIED",
            "The stored revision is not verified; only a verified official revision "
            "can be reviewed for impact.",
        )
    revision = record.get("revision")
    if not isinstance(revision, dict) or snapshot._hash(revision) != revision_id:
        raise RevisionError(
            "REVISION_INVALID",
            "The stored revision failed its content-address check.",
        )
    if record.get("revision_id") != revision_id:
        raise RevisionError(
            "REVISION_INVALID",
            "The stored revision is bound to a different revision ID.",
        )
    try:
        if revision.get("symbol") not in ETF_SYMBOLS:
            raise ValueError("The revision is not one of the five ETF symbols.")
        snapshot._date(revision.get("ex_date"))
        for field in ("old_amount", "new_amount"):
            _amount(revision.get(field))
        if revision.get("old_amount") == revision.get("new_amount"):
            raise ValueError("The old and new amounts must differ.")
        snapshot._instant(revision.get("official_available_at"), "official_available_at")
        snapshot._instant(record.get("filed_at"), "filed_at")
    except ValueError as error:
        raise RevisionError(
            "REVISION_INVALID",
            f"The stored revision is incomplete or invalid: {error}",
        ) from error
    return record


def revision_predecessor(store: str | Path, revision_id: str) -> str | None:
    """The snapshot ID that held the pre-correction amount for one revision.

    This is a metadata-only lookup: it reads the write-once proposal and
    verification-link journal entries (and their content addresses), never a
    sealed partition payload and never a query ledger.  ``None`` means the
    journal cannot bind the revision to a predecessor snapshot, so a release
    cannot claim that lineage.
    """
    if not isinstance(revision_id, str) or not re.fullmatch(r"[0-9a-f]{64}", revision_id):
        raise ValueError(
            "Invalid revision ID; revision IDs are 64-character lowercase "
            "hexadecimal content addresses."
        )
    store_path = snapshot._store_path(Path(store))
    journal = store_path / ".dividend-corrections"
    if not journal.is_dir():
        return None
    for path in sorted(journal.glob("proposal-*.json")):
        proposal_id = path.stem[len("proposal-") :]
        if not re.fullmatch(r"[0-9a-f]{64}", proposal_id):
            continue
        link_path = _journal(
            store_path, "verification", _verification_key(proposal_id, revision_id)
        )
        if not link_path.is_file():
            continue
        try:
            link = snapshot._read_json(link_path)
        except (OSError, UnicodeError, ValueError):
            continue
        if not isinstance(link, dict) or (
            link.get("proposal_id") != proposal_id
            or link.get("revision_id") != revision_id
        ):
            continue
        try:
            pending = _pending(store_path, proposal_id)
        except (OSError, UnicodeError, ValueError):
            continue
        predecessor = (pending.get("proposal") or {}).get("predecessor_id")
        if isinstance(predecessor, str) and re.fullmatch(r"[0-9a-f]{64}", predecessor):
            return predecessor
    return None


def successor(store: Path, proposal_id: str, revision_id: str) -> dict:
    store = snapshot._store_path(store)
    pending = _pending(store, proposal_id)
    proposal = pending["proposal"]
    revision_path = _journal(store, "revision", revision_id)
    link_path = _journal(store, "verification", _verification_key(proposal_id, revision_id))
    if not link_path.is_file():
        raise ValueError("The supplier observation has no matching verified revision.")
    filed = snapshot._read_json(revision_path)
    verification_hash = snapshot._hash(filed)
    if snapshot._read_json(link_path) != {
        "proposal_id": proposal_id, "revision_id": revision_id, "verification_hash": verification_hash,
    } or snapshot._hash(filed["revision"]) != revision_id:
        raise ValueError("Official revision integrity check failed.")
    predecessor_id = proposal["predecessor_id"]
    previous = snapshot._manifest(store, predecessor_id)
    metadata = dict(previous)
    for field in ("gaps", "content_hashes"):
        if field in metadata:
            metadata[f"base_{field}"] = metadata.pop(field)
    metadata.pop("discovery_dividend", None)
    metadata["lineage"] = {
        "predecessor_id": predecessor_id, "revision_id": revision_id,
        "verification_hash": verification_hash,
        "protected_partitions_inherited": True,
    }
    if _partition(previous, proposal["ex_date"]) == "discovery":
        base, history = snapshot._discovery_history(store, predecessor_id, previous)
        correction = {**filed["revision"], "url": filed["evidence"]["url"],
                      "collected_at": filed["evidence"]["collected_at"]}
        available = snapshot._instant(correction["official_available_at"], "official_available_at")
        for rev in history:
            if (
                (rev["symbol"], rev["ex_date"]) == (correction["symbol"], correction["ex_date"])
                and snapshot._instant(rev["official_available_at"], "official_available_at") >= available
            ):
                raise ValueError("Successive official amounts must have increasing availability times.")
        content = snapshot._discovery_view(base, [*history, correction])
        metadata["discovery_dividend"] = correction
        metadata["partitions"] = [
            {**part, "content_hash": snapshot._hash(content)} if part["name"] == "discovery" else part
            for part in previous["partitions"]
        ]
    return snapshot._publish(store, metadata, {})
