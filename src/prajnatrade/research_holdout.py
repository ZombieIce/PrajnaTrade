"""Direction-bound Final Holdout authorization and query accounting."""

from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from prajnatrade import protected_eval
from prajnatrade.snapshot_baseline import CN_TZ

WORKFLOW = "research_holdout"
STAGE = protected_eval.HOLDOUT_STAGE
_SCHEMA = """
CREATE TABLE IF NOT EXISTS holdout_authorizations (
    plan_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS holdout_queries (
    plan_id TEXT PRIMARY KEY,
    candidate_hash TEXT NOT NULL,
    scope TEXT NOT NULL,
    direction_id TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    partition_content_hash TEXT NOT NULL,
    evaluation_id TEXT NOT NULL UNIQUE,
    authorization_id TEXT NOT NULL,
    status TEXT NOT NULL,
    debited_at TEXT NOT NULL,
    completed_at TEXT,
    prior_exposures TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS holdout_blockers (
    blocker_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _hash(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _rejection(code: str, message: str) -> protected_eval.EvaluationRejection:
    return protected_eval.EvaluationRejection(
        code, "holdout_authorization", "inspect", message,
        context={"opened_protected_source": False, "query_debited": False},
    )


def _database(store: Path) -> sqlite3.Connection:
    store.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection = sqlite3.connect(store / "queries.sqlite", timeout=30)
    connection.row_factory = sqlite3.Row
    connection.executescript(_SCHEMA)
    return connection


def _rows(connection: sqlite3.Connection, table: str, sql: str, params: tuple) -> list[dict]:
    item = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return [dict(row) for row in connection.execute(sql, params)] if item else []


def _read_rows(store: Path, table: str, sql: str, params: tuple) -> list[dict]:
    database = store / "queries.sqlite"
    if not database.is_file():
        return []
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        return _rows(connection, table, sql, params)


def _decode_authorization(payload: str) -> dict[str, Any]:
    record = json.loads(payload)
    if not isinstance(record, dict):
        raise _rejection("AUTHORIZATION_INTEGRITY_FAILED", "The saved Holdout authorization is invalid.")
    auth_id = record.get("auth_id")
    if auth_id != _hash({key: value for key, value in record.items() if key != "auth_id"}):
        raise _rejection("AUTHORIZATION_INTEGRITY_FAILED", "The Holdout authorization hash differs.")
    return record


def load_authorization(store: str | Path, plan_id: str) -> dict[str, Any] | None:
    """Read and verify the unique pre-read Holdout authorization."""
    rows = _read_rows(
        Path(store).resolve(), "holdout_authorizations",
        "SELECT payload FROM holdout_authorizations WHERE plan_id = ?", (plan_id,),
    )
    if not rows:
        return None
    authorization = _decode_authorization(rows[0]["payload"])
    if authorization["plan_id"] != plan_id:
        raise _rejection("AUTHORIZATION_INTEGRITY_FAILED", "The Holdout authorization plan differs.")
    return authorization


def load_holdout_claim(store: str | Path, plan_id: str) -> dict[str, Any] | None:
    """Return the debit for a direction-bound Holdout without modifying its ledger."""
    rows = _read_rows(
        Path(store).resolve(), "holdout_queries",
        "SELECT * FROM holdout_queries WHERE plan_id = ?", (plan_id,),
    )
    if not rows:
        return None
    row = rows[0]
    row["prior_exposures"] = json.loads(row["prior_exposures"])
    return {**row, "partition_name": STAGE, "stage": STAGE}


def blockers(store: str | Path, plan_id: str) -> list[dict]:
    """Return the durable eligibility decisions for one plan."""
    return [
        json.loads(row["payload"])
        for row in _read_rows(
            Path(store).resolve(), "holdout_blockers",
            "SELECT payload FROM holdout_blockers WHERE plan_id = ? ORDER BY blocker_id", (plan_id,),
        )
    ]


def _legacy_exposures(connection: sqlite3.Connection, store: Path, scope: str) -> list[dict]:
    rows = _rows(
        connection, "consumed_queries",
        "SELECT plan_id, stage, evaluation_id, status FROM consumed_queries WHERE stage = ?", (STAGE,),
    )
    result = []
    for row in rows:
        plan = protected_eval.load_plan(store, row["plan_id"])
        if plan["scope"] == scope:
            partition = plan["partitions"][STAGE]
            result.append({
                **row, "workflow": "legacy", "start_date": partition["start"],
                "end_date": partition["end"], "direction_id": None,
            })
    return result


def _overlapping_exposures(
    connection: sqlite3.Connection, store: Path, plan: dict,
) -> list[dict]:
    partition = plan["partitions"][STAGE]
    rows = connection.execute(
        "SELECT * FROM holdout_queries WHERE scope = ? AND plan_id != ? "
        "AND start_date <= ? AND end_date >= ? ORDER BY debited_at, plan_id",
        (plan["scope"], plan["plan_id"], partition["end"], partition["start"]),
    )
    exposures = []
    for row in rows:
        auth_row = connection.execute(
            "SELECT payload FROM holdout_authorizations WHERE plan_id = ?", (row["plan_id"],),
        ).fetchone()
        if auth_row is None:
            raise _rejection("AUTHORIZATION_INTEGRITY_FAILED", "A prior Holdout has no authorization.")
        auth = _decode_authorization(auth_row["payload"])
        if auth["auth_id"] != row["authorization_id"] or auth["direction_id"] != row["direction_id"]:
            raise _rejection("AUTHORIZATION_INTEGRITY_FAILED", "Prior Holdout identity differs from its debit.")
        exposures.append({
            key: row[key] for key in (
                "scope", "direction_id", "plan_id", "evaluation_id", "start_date", "end_date",
                "partition_content_hash", "authorization_id", "debited_at",
            )
        } | {
            "workflow": WORKFLOW,
            "design_independence": auth["design_independence"],
            "selection_independence": auth["selection_independence"],
        })
    exposures.extend(
        row for row in _legacy_exposures(connection, store, plan["scope"])
        if row["start_date"] <= partition["end"] and row["end_date"] >= partition["start"]
    )
    return exposures


def _eligibility(direction: dict, exposures: list[dict]) -> tuple[str, str] | None:
    if any(item["direction_id"] == direction["direction_id"] for item in exposures):
        return "DIRECTION_ALREADY_EVALUATED", "This direction has already used an overlapping Holdout range."
    if any(item["direction_id"] is None for item in exposures):
        return "UNSEEN_HOLDOUT_REQUIRED", "An overlapping legacy Holdout has no verifiable direction declaration."
    if exposures and not (
        direction["design_independence"]["independent"]
        and direction["selection_independence"]["independent"]
        and direction["research_selection_independence"]["independent"]
    ):
        return "UNSEEN_HOLDOUT_REQUIRED", "The declared influence of prior Holdout results requires an unseen range."
    return None


def _block(
    connection: sqlite3.Connection, plan: dict, direction: dict,
    issue: tuple[str, str], exposures: list[dict],
) -> dict:
    code, message = issue
    body = {
        "schema_version": 2, "status": "blocked", "workflow": WORKFLOW,
        "plan_id": plan["plan_id"], "direction_id": direction["direction_id"],
        "round_id": direction["round_id"], "scope": plan["scope"],
        "candidate_id": plan["candidate_id"], "partition": plan["partitions"][STAGE],
        "error": {"code": code, "phase": "holdout", "next_action": "use_unseen_range", "message": message},
        "prior_exposures": exposures, "query_debited": False, "opened_protected_source": False,
    }
    blocker_id = _hash(body)
    payload = {**body, "blocker_id": blocker_id}
    connection.execute(
        "INSERT OR IGNORE INTO holdout_blockers VALUES (?, ?, ?)",
        (blocker_id, plan["plan_id"], _json(payload)),
    )
    return payload


def authorize(
    store: Path,
    snapshot_store: Path,
    project: Path,
    round_id: str,
    validation_evaluation_id: str,
    *,
    reviewer: str,
    reason: str,
    selection_independent: bool,
    independence_reason: str,
    calendar: dict,
) -> dict:
    """Bind approval to the selected evidence and to release on a trusted pass."""
    from prajnatrade import research_direction, shadow_calendar
    from prajnatrade.experiment import ProtocolError

    if not all(value.strip() for value in (reviewer, reason, independence_reason)):
        raise _rejection("INVALID_AUTHORIZATION", "Provide a reviewer and reasons for approval and independence.")
    try:
        selection = research_direction.selected_candidate(project, round_id)
    except (ProtocolError, ValueError) as error:
        raise _rejection("SELECTION_REQUIRED", "Freeze a successful Validation selection first.") from error
    if not selection:
        raise _rejection("SELECTION_REQUIRED", "Freeze a successful Validation selection first.")
    if selection["validation_evaluation_id"] != validation_evaluation_id:
        raise _rejection("VALIDATION_SELECTION_MISMATCH", "The requested Validation evaluation differs from the frozen selection.")
    kickoff = research_direction.load_kickoff(store, round_id)
    if any(
        kickoff[key] != selection[key] for key in
        ("direction_id", "kickoff_id", "project_id", "campaign_id", "snapshot_id")
    ) or kickoff["independence"] != selection["design_independence"]:
        raise _rejection("SELECTION_INTEGRITY_FAILED", "The selection differs from its authorized research kickoff.")
    planned = protected_eval.create_research_holdout_plan(
        store, snapshot_store, project, selection["candidate_id"], round_id, validation_evaluation_id,
    )
    plan = protected_eval.load_plan(store.resolve(), planned["plan_id"])
    evidence = protected_eval.selected_validation_evidence(
        store, round_id, validation_evaluation_id, plan["candidate"],
    )
    if (
        selection["candidate_hash"] != plan["candidate_hash"]
        or selection["validation_plan_id"] != evidence["plan"]["plan_id"]
        or selection["validation_evaluation_hash"] != evidence["record_hash"]
    ):
        raise _rejection("VALIDATION_SELECTION_MISMATCH", "The trusted Validation proof differs from the frozen selection.")
    calendar = shadow_calendar.validate_calendar(calendar)
    if calendar["data_kind"] != plan["scope"]:
        raise _rejection("INVALID_CALENDAR", "The authorization calendar must match the evaluation data scope.")
    earliest = max(plan["partitions"][STAGE]["end"], datetime.now(CN_TZ).date().isoformat())
    if not any(session > earliest for session in calendar["sessions"]):
        raise _rejection("INVALID_CALENDAR", "The authorization calendar needs a trading session after the Holdout and approval date.")
    direction = {
        "direction_id": selection["direction_id"],
        "round_id": round_id,
        "kickoff_id": selection["kickoff_id"],
        "design_independence": selection["design_independence"],
        "research_selection_independence": selection["selection_independence"],
        "selection_independence": {"independent": selection_independent, "reason": independence_reason.strip()},
    }
    body = {
        "schema_version": 2,
        "workflow": WORKFLOW,
        "scope": plan["scope"],
        **direction,
        "plan_id": plan["plan_id"],
        "plan_hash": plan["plan_id"],
        "candidate_id": plan["candidate_id"],
        "candidate_hash": plan["candidate_hash"],
        "snapshot_id": plan["snapshot_id"],
        "stage": STAGE,
        "partition": STAGE,
        "partition_content_hash": plan["partitions"][STAGE]["content_hash"],
        "interval": {key: plan["partitions"][STAGE][key] for key in ("start", "end")},
        "reviewer": reviewer.strip(),
        "reason": reason.strip(),
        "validation": {
            "evaluation_id": evidence["evaluation_id"],
            "record_hash": evidence["record_hash"],
            "plan_id": evidence["plan"]["plan_id"],
            "outcome": "passed",
            "partition": protected_eval.VALIDATION_STAGE,
            "partition_content_hash": evidence["partition_content_hash"],
            "export_hash": evidence["export_hash"],
        },
        "automatic_release": {"calendar": calendar},
    }
    store = store.resolve()
    with closing(_database(store)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT payload FROM holdout_authorizations WHERE plan_id = ?", (plan["plan_id"],)
        ).fetchone()
        if row:
            existing = _decode_authorization(row["payload"])
            if any(existing.get(key) != value for key, value in body.items()):
                raise _rejection("AUTHORIZATION_CONFLICT", "The first Holdout approval already binds different instructions.")
            return {**_authorized(existing), "reused": True}
        exposures = _overlapping_exposures(connection, store, plan)
        issue = _eligibility(direction, exposures)
        if issue:
            return _block(connection, plan, direction, issue, exposures)
        payload = {**body, "prior_exposures": exposures, "authorized_at": _now()}
        authorization = {**payload, "auth_id": _hash(payload)}
        connection.execute(
            "INSERT INTO holdout_authorizations VALUES (?, ?)", (plan["plan_id"], _json(authorization)),
        )
        return _authorized(authorization)


def _authorized(record: dict) -> dict:
    return {
        "schema_version": 2, "status": "authorized", "scope": record["scope"],
        "plan_id": record["plan_id"], "authorization": record,
        "opened_protected_source": False, "query_debited": False,
    }


def claim(store: Path, plan: dict, authorization: dict) -> dict:
    """Serialize the range eligibility check with the one pre-read debit."""
    with closing(_database(store)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        previous = connection.execute(
            "SELECT * FROM holdout_queries WHERE plan_id = ?", (plan["plan_id"],),
        ).fetchone()
        if previous:
            if previous["authorization_id"] != authorization["auth_id"]:
                raise _rejection("AUTHORIZATION_INTEGRITY_FAILED", "The debit and authorization differ.")
            return {**dict(previous), "state": "existing"}
        exposures = _overlapping_exposures(connection, store, plan)
        issue = _eligibility(authorization, exposures)
        if issue:
            return {"state": "blocked", "blocker": _block(connection, plan, authorization, issue, exposures)}
        partition = plan["partitions"][STAGE]
        evaluation_id, debited_at = str(uuid.uuid4()), _now()
        connection.execute(
            "INSERT INTO holdout_queries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'reading', ?, NULL, ?)",
            (plan["plan_id"], plan["candidate_hash"], plan["scope"], authorization["direction_id"],
             partition["start"], partition["end"], partition["content_hash"],
             evaluation_id, authorization["auth_id"], debited_at, _json(exposures)),
        )
        return {
            "state": "debited", "evaluation_id": evaluation_id, "debited_at": debited_at,
            "prior_exposures": exposures,
        }


def legacy_conflict(connection: sqlite3.Connection, plan: dict, stage: str) -> bool:
    """Guard undeclared legacy reads after direction-bound Holdout exposure."""
    if stage != STAGE:
        return False
    partition = plan["partitions"][STAGE]
    return bool(_rows(
        connection, "holdout_queries",
        "SELECT 1 FROM holdout_queries WHERE scope = ? AND start_date <= ? AND end_date >= ?",
        (plan["scope"], partition["end"], partition["start"]),
    ))


def finish(store: Path, plan_id: str, evaluation_id: str, status: str, completed_at: str) -> None:
    """Reconcile a committed record or an interrupted read with its debit."""
    with closing(_database(store)) as connection, connection:
        connection.execute(
            "UPDATE holdout_queries SET status = ?, completed_at = ? "
            "WHERE plan_id = ? AND evaluation_id = ? AND status = 'reading'",
            (status, completed_at, plan_id, evaluation_id),
        )


def run(store: Path, snapshot_store: Path, project: Path, plan_id: str) -> dict:
    """Run or recover a Holdout and settle the pre-authorized release."""
    from prajnatrade import release

    store = store.resolve()
    plan = protected_eval.load_plan(store, plan_id)
    if plan.get("workflow") != WORKFLOW:
        raise _rejection("HOLDOUT_PLAN_REQUIRED", "Use a direction-bound Holdout plan.")
    authorization = load_authorization(store, plan_id)
    if authorization is None:
        raise _rejection("AUTHORIZATION_REQUIRED", "The frozen selection requires human Holdout approval.")
    lock_path = store / "holdout-locks" / plan_id
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = protected_eval.run_research_holdout_evaluation(
            store, snapshot_store, project, plan, authorization,
        )
        if result.get("outcome") == "passed":
            result["release"] = release.settle_authorized_release(store, plan_id)
        elif result.get("outcome") == "rejected":
            record = result["record"]
            result["retrospective"] = {
                "status": "round_ended", "reason": record["reason"],
                "message": record["message"], "thresholds": record["thresholds"],
                "evaluation_id": record["evaluation_id"], "record_hash": record["record_hash"],
            }
        return result
