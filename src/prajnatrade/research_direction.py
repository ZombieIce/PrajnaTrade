"""Kickoff, development evidence, and selection for a research direction."""

from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
import unicodedata
from collections.abc import Generator
from contextlib import closing, contextmanager
from datetime import date as calendar_date
from pathlib import Path
from typing import Any

from prajnatrade.experiment import (
    ProtocolError,
    campaign_discovery,
    campaign_execution_lock,
    campaign_experiment_history,
    evaluation_subject,
    freeze_research_candidate,
    project_status,
    research_candidate_status,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_rounds (
    round_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL UNIQUE REFERENCES campaigns(campaign_id),
    kickoff_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_validation_proposals (
    proposal_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL REFERENCES research_rounds(round_id),
    proposal_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_validation_receipts (
    evaluation_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL REFERENCES research_rounds(round_id),
    proposal_id TEXT NOT NULL REFERENCES research_validation_proposals(proposal_id),
    receipt_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_validation_proposal_results (
    proposal_id TEXT PRIMARY KEY REFERENCES research_validation_proposals(proposal_id),
    evaluation_id TEXT NOT NULL REFERENCES research_validation_receipts(evaluation_id)
);
CREATE TABLE IF NOT EXISTS research_validation_details (
    detail_access_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL REFERENCES research_rounds(round_id),
    request_json TEXT NOT NULL,
    resolution_json TEXT
);
CREATE TABLE IF NOT EXISTS research_validation_feedback (
    feedback_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL REFERENCES research_rounds(round_id),
    receipt_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_retrospectives (
    round_id TEXT PRIMARY KEY REFERENCES research_rounds(round_id),
    receipt_json TEXT NOT NULL
);
"""


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: object) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


@contextmanager
def _project(project: Path) -> Generator[sqlite3.Connection, None, None]:
    project_status(project)
    database = project / ".prajnatrade" / "project.sqlite"
    with closing(sqlite3.connect(database, timeout=30)) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(_SCHEMA)
        yield connection


def _object(raw: object, required: set[str], optional: frozenset[str] = frozenset()) -> dict:
    if not isinstance(raw, dict) or not required <= raw.keys() or raw.keys() - required - optional:
        required_names = ", ".join(sorted(required))
        raise ValueError(f"Expected fields: {required_names}.")
    return raw


def _text(raw: object, field: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{field} must be a non-empty string.")
    return raw.strip()


def _texts(raw: object, field: str) -> list[str]:
    if not isinstance(raw, list):
        raise ValueError(f"{field} must be a list of strings.")
    return [_text(value, field) for value in raw]


def _independence(raw: object) -> dict[str, Any]:
    declaration = _object(raw, {"independent", "reason"})
    if type(declaration["independent"]) is not bool:
        raise ValueError("independence.independent must be a boolean.")
    return {
        "independent": declaration["independent"],
        "reason": _text(declaration["reason"], "independence.reason"),
    }


def _canonical_direction_key(raw: str) -> str:
    # The research lead continues to use the design key across renamed directions
    # and new Campaigns. Ignore formatting differences in the key.
    normalized = unicodedata.normalize("NFKC", _text(raw, "direction_key")).casefold()
    key = "".join(
        char for char in normalized
        if not char.isspace() and not unicodedata.category(char).startswith("P")
    )
    return _text(key, "direction_key")


def direction_id(direction_key: str) -> str:
    """Return the stable identity of a research-direction key.

    The research lead continues to use the design key across renamed directions
    and new Campaigns, so formatting differences in the key do not change it.
    """
    return _hash({"direction_key": _canonical_direction_key(direction_key)})


def prepare_kickoff(
    project: Path, campaign_id: str, direction_key: str, name: str,
    authorization_id: str, source: object, *, store: Path,
) -> dict[str, Any]:
    """Normalize the research lead's authorization before recording it."""
    from prajnatrade.protected_eval import normalize_validation_protocol

    payload = _object(source, {
        "objective", "constraints", "validation_budget", "validation_settings",
        "independence", "reviewer", "approval_reason",
    })
    budget = payload["validation_budget"]
    if type(budget) is not int or budget <= 0:
        raise ValueError("validation_budget must be a positive integer.")
    campaign = campaign_discovery(project, campaign_id)
    evaluation = campaign["evaluation"]
    key = _canonical_direction_key(direction_key)
    direction = direction_id(direction_key)
    round_id = _hash({
        "direction_id": direction,
        "project_id": campaign["project_id"],
        "campaign_id": campaign_id,
    })
    trusted = store.resolve()
    if trusted.is_relative_to(project.resolve()):
        raise ValueError("Trusted evaluation store must be outside the research project.")
    previous_path = _campaign_kickoff_path(trusted, campaign["project_id"], campaign_id)
    replay_legacy_kickoff = False
    if previous_path.is_file():
        previous = json.loads(previous_path.read_text(encoding="utf-8"))
        if not isinstance(previous, dict) or not isinstance(previous.get("round_id"), str):
            raise ValueError("Invalid trusted research kickoff.")
        _verify_kickoff(previous, previous["round_id"])
        replay_legacy_kickoff = (
            previous["round_id"] == round_id
            and previous["project_id"] == campaign["project_id"]
            and previous["campaign_id"] == campaign_id
            and "scoring_prerequisites" not in previous["validation_settings"]
        )
    approved = {
        "schema_version": 1,
        "direction_id": direction,
        "direction_key": key,
        "round_id": round_id,
        "name": _text(name, "name"),
        **campaign,
        "scope": evaluation["data_kind"],
        "data_kind": evaluation["data_kind"],
        "objective": _text(payload["objective"], "objective"),
        "constraints": _texts(payload["constraints"], "constraints"),
        "validation_budget": budget,
        "baseline": {"method": "equal_weight_buy_and_hold", "weights": "equal"},
        "validation_settings": normalize_validation_protocol(
            payload["validation_settings"], evaluation, replay_legacy_kickoff=replay_legacy_kickoff,
        ),
        "independence": _independence(payload["independence"]),
        "authorization_id": _text(authorization_id, "authorization_id"),
        "reviewer": _text(payload["reviewer"], "reviewer"),
        "approval_reason": _text(payload["approval_reason"], "approval_reason"),
    }
    return {**approved, "kickoff_id": _hash(approved)}


def _require_round_id(round_id: str) -> None:
    if len(round_id) != 64 or any(char not in "0123456789abcdef" for char in round_id):
        raise ValueError("Invalid research round ID.")


def _kickoff_path(store: Path, round_id: str) -> Path:
    _require_round_id(round_id)
    return store / "research-kickoffs" / f"{round_id}.json"


def _campaign_kickoff_path(store: Path, project_id: str, campaign_id: str) -> Path:
    key = _hash({"project_id": project_id, "campaign_id": campaign_id})
    return store / "research-kickoffs-by-campaign" / f"{key}.json"


def _verify_kickoff(approved: dict[str, Any], round_id: str) -> None:
    binding = {key: value for key, value in approved.items() if key != "kickoff_id"}
    if approved.get("round_id") != round_id or approved.get("kickoff_id") != _hash(binding):
        raise ValueError("Research kickoff fingerprint mismatch.")


def kickoff(project: Path, store: Path, prepared: dict[str, Any]) -> dict[str, Any]:
    """Persist an immutable trusted approval and its research-visible copy."""
    from prajnatrade.protected_eval import evaluation_store, write_trusted_record_once

    round_id = prepared["round_id"]
    _verify_kickoff(prepared, round_id)
    campaign = campaign_discovery(project, prepared["campaign_id"])
    if any(prepared[field] != value for field, value in campaign.items()):
        raise ValueError("Research kickoff does not match the project's Campaign.")
    if Path(store).resolve().is_relative_to(project.resolve()):
        raise ValueError("Trusted evaluation store must be outside the research project.")
    trusted = evaluation_store(store)
    target = _kickoff_path(trusted, round_id)
    with campaign_execution_lock(project, prepared["campaign_id"]), _project(project) as connection:
        connection.execute("BEGIN IMMEDIATE")
        previous = connection.execute(
            "SELECT kickoff_json FROM research_rounds WHERE campaign_id = ?",
            (prepared["campaign_id"],),
        ).fetchone()
        content = _json(prepared)
        if previous is not None and previous["kickoff_json"] != content:
            raise ValueError("Campaign already has an approved research kickoff.")

        campaign_record = write_trusted_record_once(
            _campaign_kickoff_path(trusted, prepared["project_id"], prepared["campaign_id"]),
            prepared,
        )
        if campaign_record != prepared:
            raise ValueError("Campaign already has a different trusted kickoff.")
        stored = write_trusted_record_once(target, prepared)
        if stored != prepared:
            raise ValueError("Research round already has a different trusted kickoff.")
        if previous is None:
            connection.execute(
                "INSERT INTO research_rounds VALUES (?, ?, ?)",
                (round_id, prepared["campaign_id"], content),
            )
    return prepared


def load_kickoff(store: Path, round_id: str) -> dict[str, Any]:
    """Load and verify the trusted human authorization."""
    try:
        approved = json.loads(_kickoff_path(Path(store).resolve(), round_id).read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError("Unknown trusted research kickoff.") from error
    if not isinstance(approved, dict):
        raise ValueError("Invalid trusted research kickoff.")
    _verify_kickoff(approved, round_id)
    return approved


def campaign_has_kickoff(store: Path, project_id: str, campaign_id: str) -> bool:
    """Recognize an approved research Campaign inside the trusted store."""
    path = _campaign_kickoff_path(Path(store).resolve(), project_id, campaign_id)
    return path.is_file()


def _project_kickoff(connection: sqlite3.Connection, round_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT kickoff_json FROM research_rounds WHERE round_id = ?", (round_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Unknown research round.")
    approved = json.loads(row["kickoff_json"])
    _verify_kickoff(approved, round_id)
    return approved


@contextmanager
def _round_lock(project: Path, round_id: str) -> Generator[None, None, None]:
    _require_round_id(round_id)
    project_status(project)
    locks = project / ".prajnatrade" / "research-locks"
    locks.mkdir(exist_ok=True)
    with (locks / f"{round_id}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _proposal(connection: sqlite3.Connection, round_id: str, proposal_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT proposal_json FROM research_validation_proposals WHERE round_id = ? AND proposal_id = ?",
        (round_id, proposal_id),
    ).fetchone()
    if row is None:
        raise ValueError("Unknown Validation proposal for this research round.")
    proposal = json.loads(row["proposal_json"])
    body = {key: value for key, value in proposal.items() if key != "proposal_id"}
    if proposal["proposal_id"] != proposal_id or _hash(body) != proposal_id:
        raise ValueError("Validation proposal fingerprint mismatch.")
    return proposal


def proposal_status(project: Path, round_id: str, proposal_id: str) -> dict[str, Any]:
    with _project(project) as connection:
        return _proposal(connection, round_id, proposal_id)


def proposal_submit(
    project: Path, round_id: str, submission_id: str, source: object,
) -> dict[str, Any]:
    payload = _object(
        source, {"change", "question", "expected_change", "related_experiment_ids"},
        frozenset({"selection_impact"}),
    )
    related = _texts(payload["related_experiment_ids"], "related_experiment_ids")
    with _round_lock(project, round_id), _project(project) as connection:
        approved = _project_kickoff(connection, round_id)
        campaign_id = approved["campaign_id"]
        subject = evaluation_subject(project, campaign_id, submission_id)
        history = campaign_experiment_history(project, campaign_id)
        attempts = {entry["experiment_id"]: entry for entry in history}
        experiment_id = subject["experiment_id"]
        if any(identifier not in attempts or identifier == experiment_id for identifier in related):
            raise ValueError("Related experiment IDs must refer to other attempts in this Campaign.")
        parent = attempts[experiment_id]["parent_experiment_id"]
        if parent is not None and parent not in related:
            raise ValueError("The Validation proposal must link its parent experiment.")
        proposal = {
            "schema_version": 1,
            **{field: approved[field] for field in (
                "round_id", "kickoff_id", "direction_id", "project_id", "campaign_id",
                "snapshot_id", "data_kind",
            )},
            "submission_id": submission_id,
            "experiment_id": experiment_id,
            "experiment_hash": subject["candidate_hash"],
            "change": _text(payload["change"], "change"),
            "question": _text(payload["question"], "question"),
            "expected_change": _text(payload["expected_change"], "expected_change"),
            "related_experiment_ids": sorted(set(related)),
            "selection_impact": (
                _text(payload["selection_impact"], "selection_impact")
                if "selection_impact" in payload else None
            ),
        }
        proposal_id = _hash(proposal)
        receipt = {**proposal, "proposal_id": proposal_id}
        exists = connection.execute(
            "SELECT 1 FROM research_validation_proposals WHERE proposal_id = ?", (proposal_id,),
        ).fetchone()
        if exists is None:
            evaluations = _evaluations(connection, round_id)
            fingerprint = subject["fingerprints"]["strategy_and_parameters"]
            if fingerprint not in _version_fingerprints(project, campaign_id, evaluations):
                _require_open(project, connection, round_id)
                require_selection_impact(proposal, "proposal", passed=_has_passed(evaluations))
            connection.execute(
                "INSERT INTO research_validation_proposals VALUES (?, ?, ?)",
                (proposal_id, round_id, _json(receipt)),
            )
        return _proposal(connection, round_id, proposal_id)


def _proposals(connection: sqlite3.Connection, round_id: str) -> list[dict[str, Any]]:
    return [
        _proposal(connection, round_id, row["proposal_id"])
        for row in connection.execute(
            "SELECT proposal_id FROM research_validation_proposals WHERE round_id = ? ORDER BY rowid",
            (round_id,),
        )
    ]


def _evaluations(connection: sqlite3.Connection, round_id: str) -> list[dict[str, Any]]:
    return [
        json.loads(row["receipt_json"])
        for row in connection.execute(
            "SELECT receipt_json FROM research_validation_receipts WHERE round_id = ? ORDER BY rowid",
            (round_id,),
        )
    ]


def _evaluation(
    connection: sqlite3.Connection, round_id: str, evaluation_id: str,
) -> dict[str, Any]:
    row = connection.execute(
        "SELECT receipt_json FROM research_validation_receipts "
        "WHERE round_id = ? AND evaluation_id = ?", (round_id, evaluation_id),
    ).fetchone()
    if row is None:
        raise ValueError("Unknown Validation evaluation for this research round.")
    return json.loads(row["receipt_json"])


def _passed(evaluation: dict[str, Any]) -> bool:
    return evaluation["status"] == "completed" and evaluation["outcome"] == "passed"


def _has_passed(evaluations: list[dict[str, Any]]) -> bool:
    return any(_passed(evaluation) for evaluation in evaluations)


def _version_fingerprints(
    project: Path, campaign_id: str, evaluations: list[dict[str, Any]],
) -> set[str]:
    return {
        evaluation_subject(project, campaign_id, evaluation["submission_id"])
        ["fingerprints"]["strategy_and_parameters"]
        for evaluation in evaluations
    }


def _finished_retrospective(connection: sqlite3.Connection, round_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT receipt_json FROM research_retrospectives WHERE round_id = ?", (round_id,),
    ).fetchone()
    return json.loads(row["receipt_json"]) if row is not None else None


def _require_open(project: Path, connection: sqlite3.Connection, round_id: str) -> None:
    if _finished_retrospective(connection, round_id) or research_candidate_status(project, round_id):
        raise ProtocolError("RESEARCH_ROUND_CLOSED", "round", "review", "The research round has ended.")


def require_discovery_work(
    project: Path, connection: sqlite3.Connection, campaign_id: str,
) -> None:
    """Allow computation on unmanaged Campaigns or rounds with room for new versions."""
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'research_rounds'",
    ).fetchone() is None:
        return
    managed = connection.execute(
        "SELECT round_id FROM research_rounds WHERE campaign_id = ?", (campaign_id,),
    ).fetchone()
    if managed is None:
        return
    round_id = managed["round_id"]
    approved = _project_kickoff(connection, round_id)
    _require_open(project, connection, round_id)
    if len(_evaluations(connection, round_id)) >= approved["validation_budget"]:
        raise ProtocolError(
            "VALIDATION_BUDGET_EXHAUSTED", "discovery", "review",
            "The round has no Validation allowance left for a new Discovery version.",
        )


def require_selection_impact(
    proposal: dict[str, Any], phase: str, *, passed: bool,
) -> None:
    """Require a comparison question for a new version after a Validation pass."""
    if passed and not proposal["selection_impact"]:
        raise ProtocolError(
            "VALIDATION_SELECTION_QUESTION_REQUIRED", phase, "preregister",
            "A passed version exists. Explain how the question could change selection.",
        )


@contextmanager
def validation_submission(
    project: Path, round_id: str, proposal_id: str,
) -> Generator[dict[str, Any], None, None]:
    """Serialize trusted claims with selection and let known submissions replay."""
    with _round_lock(project, round_id):
        with _project(project) as connection:
            proposal = _proposal(connection, round_id, proposal_id)
        yield proposal


def require_open_round(project: Path, round_id: str) -> None:
    """Check the round before the evaluator admits a new Validation version."""
    with _project(project) as connection:
        _require_open(project, connection, round_id)


_RECEIPT_FIELDS = (
    "schema_version", "stage", "round_id", "scope", "data_kind",
    "proposal_id", "submission_id", "experiment_id", "evaluation_id", "plan_id",
    "record_hash", "status", "outcome", "reason", "metrics",
)
_OPTIONAL_RECEIPT_FIELDS = ("execution_rules", "scoring_prerequisites", "scoring_evidence")


def publish_validation(
    project: Path, round_id: str, proposal_id: str, receipt: dict[str, Any],
) -> None:
    """Publish only research-visible evidence from the trusted evaluator."""
    if (
        receipt.get("stage") != "validation"
        or receipt.get("round_id") != round_id
        or receipt.get("status") not in {"reading", "completed", "aborted"}
    ):
        raise ValueError("Invalid research-visible Validation receipt.")
    evaluation_id = _text(receipt.get("evaluation_id"), "evaluation_id")
    with _project(project) as connection:
        connection.execute("BEGIN IMMEDIATE")
        approved = _project_kickoff(connection, round_id)
        _proposal(connection, round_id, proposal_id)
        original = _proposal(connection, round_id, receipt["proposal_id"])
        if any(receipt.get(field) != original[field] for field in (
            "experiment_id", "submission_id", "data_kind",
        )):
            raise ValueError("Validation receipt does not bind to its original proposal.")
        safe = {field: receipt.get(field) for field in _RECEIPT_FIELDS}
        safe.update({field: receipt[field] for field in _OPTIONAL_RECEIPT_FIELDS if field in receipt})
        safe.update({
            "direction_id": approved["direction_id"],
            "kickoff_id": approved["kickoff_id"],
            "experiment_hash": original["experiment_hash"],
        })
        stored = connection.execute(
            "SELECT receipt_json FROM research_validation_receipts WHERE evaluation_id = ?",
            (evaluation_id,),
        ).fetchone()
        if stored is None:
            connection.execute(
                "INSERT INTO research_validation_receipts VALUES (?, ?, ?, ?)",
                (evaluation_id, round_id, original["proposal_id"], _json(safe)),
            )
        else:
            prior = json.loads(stored["receipt_json"])
            for field in ("round_id", "proposal_id", "evaluation_id", "plan_id"):
                if prior[field] != safe[field]:
                    raise ValueError("Validation evaluation binding changed.")
            if prior["status"] == "reading":
                connection.execute(
                    "UPDATE research_validation_receipts SET receipt_json = ? WHERE evaluation_id = ?",
                    (_json(safe), evaluation_id),
                )
            elif prior != safe:
                raise ValueError("A terminal Validation receipt cannot change.")

        for linked in {original["proposal_id"], proposal_id}:
            connection.execute(
                "INSERT OR IGNORE INTO research_validation_proposal_results VALUES (?, ?)",
                (linked, evaluation_id),
            )
            binding = connection.execute(
                "SELECT evaluation_id FROM research_validation_proposal_results WHERE proposal_id = ?",
                (linked,),
            ).fetchone()
            if binding["evaluation_id"] != evaluation_id:
                raise ValueError("Validation proposal is linked to a different evaluation.")


def _details_row(
    connection: sqlite3.Connection, round_id: str, detail_access_id: str,
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT request_json, resolution_json FROM research_validation_details "
        "WHERE round_id = ? AND detail_access_id = ?", (round_id, detail_access_id),
    ).fetchone()
    if row is None:
        raise ValueError("Unknown Validation detail request for this research round.")
    return row


def _detail_receipt(row: sqlite3.Row, *, include_details: bool) -> dict[str, Any]:
    request = json.loads(row["request_json"])
    resolution = row["resolution_json"]
    if resolution is None:
        return {**request, "status": "queued"}
    receipt = json.loads(resolution)
    if not include_details:
        receipt.pop("details", None)
    return {**request, **receipt}


def validation_details(
    project: Path, round_id: str, evaluation_id: str, date: str, question: str,
) -> dict[str, Any]:
    """Record a specific question and request one Validation session's evidence."""
    try:
        parsed = calendar_date.fromisoformat(date)
    except ValueError as error:
        raise ValueError("Validation detail date must be YYYY-MM-DD.") from error
    if parsed.isoformat() != date:
        raise ValueError("Validation detail date must be YYYY-MM-DD.")
    with _project(project) as connection:
        evaluation = _evaluation(connection, round_id, evaluation_id)
        if evaluation["status"] != "completed":
            raise ValueError("Details require a completed Validation evaluation.")
        question = _text(question, "question")
        request = {
            "schema_version": 1, "round_id": round_id, "evaluation_id": evaluation_id,
            "record_hash": evaluation["record_hash"], "kickoff_id": evaluation["kickoff_id"],
            "scope": evaluation["scope"], "data_kind": evaluation["data_kind"],
            "date": date, "question": question,
        }
        access_id = _hash(request)
        connection.execute(
            "INSERT OR IGNORE INTO research_validation_details VALUES (?, ?, ?, NULL)",
            (access_id, round_id, _json({**request, "detail_access_id": access_id})),
        )
        return _detail_receipt(_details_row(connection, round_id, access_id), include_details=True)


def validation_detail_receipt(
    request: dict[str, Any], authorization_id: str, partition_hash: str,
    details: dict[str, Any] | None,
) -> dict[str, Any]:
    """Bind a recorded Validation question to the verified record and its projection."""
    access_id = request["detail_access_id"]
    request_body = {key: value for key, value in request.items() if key != "detail_access_id"}
    if _hash(request_body) != access_id:
        raise ValueError("Validation detail request fingerprint mismatch.")
    provenance = {
        "authorization_id": authorization_id,
        "validation_record_hash": request["record_hash"],
        "partition_content_hash": partition_hash,
        "projection_hash": _hash(details),
    }
    receipt = {
        "schema_version": 1, "request": request, "provenance": provenance,
        "status": "completed" if details is not None else "unavailable",
        "opened_protected_source": False, "query_debited": False,
    }
    return {**receipt, "provenance": {**provenance, "receipt_id": _hash(receipt)}}


def publish_validation_details(
    project: Path, round_id: str, detail_access_id: str, receipt: dict[str, Any],
    details: dict[str, Any] | None,
) -> dict[str, Any]:
    """Publish a single-date projection only after the trusted receipt is recorded."""
    with _project(project) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _details_row(connection, round_id, detail_access_id)
        request = json.loads(row["request_json"])
        provenance = receipt["provenance"]
        if receipt != validation_detail_receipt(
            request, provenance["authorization_id"], provenance["partition_content_hash"], details,
        ):
            raise ValueError("Validation detail response must match the trusted receipt.")
        resolution = {"status": receipt["status"], "provenance": provenance}
        if details is not None:
            resolution["details"] = details
        content = _json(resolution)
        if row["resolution_json"] is None:
            connection.execute(
                "UPDATE research_validation_details SET resolution_json = ? WHERE detail_access_id = ?",
                (content, detail_access_id),
            )
        elif content != row["resolution_json"]:
            raise ValueError("A completed Validation detail response cannot change.")
        return {**request, **resolution}


def _detail_history(connection: sqlite3.Connection, round_id: str) -> list[dict[str, Any]]:
    return [
        _detail_receipt(row, include_details=False)
        for row in connection.execute(
            "SELECT request_json, resolution_json FROM research_validation_details "
            "WHERE round_id = ? ORDER BY rowid", (round_id,),
        )
    ]


def record_feedback(
    project: Path,
    round_id: str,
    evaluation_id: str,
    impact: str,
    next_experiment_id: str | None = None,
    detail_access_id: str | None = None,
) -> dict[str, Any]:
    """Link Validation evidence to a research decision or subsequent revision."""
    with _round_lock(project, round_id), _project(project) as connection:
        approved = _project_kickoff(connection, round_id)
        evaluation = _evaluation(connection, round_id, evaluation_id)
        if evaluation["status"] != "completed":
            raise ValueError("Feedback requires a completed Validation evaluation.")
        if detail_access_id is not None:
            row = _details_row(connection, round_id, detail_access_id)
            details = _detail_receipt(row, include_details=False)
            if details["evaluation_id"] != evaluation_id or details["status"] != "completed":
                raise ValueError("Feedback needs completed details from this Validation evaluation.")
        if next_experiment_id is not None:
            attempts = campaign_experiment_history(project, approved["campaign_id"])
            if (
                next_experiment_id == evaluation["experiment_id"]
                or not any(item["experiment_id"] == next_experiment_id for item in attempts)
            ):
                raise ValueError("Feedback must link another experiment in the same Campaign.")
        body = {
            "schema_version": 1, "round_id": round_id, "evaluation_id": evaluation_id,
            "experiment_id": evaluation["experiment_id"], "detail_access_id": detail_access_id,
            "next_experiment_id": next_experiment_id,
            "scope": approved["scope"], "data_kind": approved["data_kind"],
            "impact": _text(impact, "impact"),
        }
        feedback_id = _hash(body)
        receipt = {**body, "feedback_id": feedback_id}
        exists = connection.execute(
            "SELECT 1 FROM research_validation_feedback WHERE feedback_id = ?", (feedback_id,),
        ).fetchone()
        if exists is None:
            _require_open(project, connection, round_id)
            connection.execute(
                "INSERT INTO research_validation_feedback VALUES (?, ?, ?)",
                (feedback_id, round_id, _json(receipt)),
            )
        return receipt


def _feedback(connection: sqlite3.Connection, round_id: str) -> list[dict[str, Any]]:
    return [
        json.loads(row["receipt_json"])
        for row in connection.execute(
            "SELECT receipt_json FROM research_validation_feedback WHERE round_id = ? ORDER BY rowid",
            (round_id,),
        )
    ]


def _proposal_results(connection: sqlite3.Connection, round_id: str) -> dict[str, str]:
    return dict(connection.execute(
        "SELECT p.proposal_id, p.evaluation_id FROM research_validation_proposal_results AS p "
        "JOIN research_validation_receipts AS r USING (evaluation_id) WHERE r.round_id = ?",
        (round_id,),
    ))


def _validation_budget(approved: dict[str, Any], evaluations: list[dict[str, Any]]) -> dict[str, int]:
    limit = approved["validation_budget"]
    return {"limit": limit, "used": len(evaluations), "remaining": max(0, limit - len(evaluations))}


def pending_validation_work(
    project: Path, round_id: str, *, benchmark_failed: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Return actionable proposals and detail questions, including proposals awaiting a benchmark."""
    frozen = research_candidate_status(project, round_id)
    with _project(project) as connection:
        connection.execute("BEGIN")
        approved = _project_kickoff(connection, round_id)
        closed = frozen or _finished_retrospective(connection, round_id)
        evaluations = _evaluations(connection, round_id)
        by_id = {item["evaluation_id"]: item for item in evaluations}
        links = _proposal_results(connection, round_id)
        budget = _validation_budget(approved, evaluations)
        passed = _has_passed(evaluations)
        versions = _version_fingerprints(project, approved["campaign_id"], evaluations)
        proposals = []
        for proposal in _proposals(connection, round_id):
            existing = links.get(proposal["proposal_id"])
            if existing is not None:
                if by_id[existing]["status"] == "reading":
                    proposals.append(proposal)
            elif not closed and (
                benchmark_failed or (budget["remaining"] and (not passed or proposal["selection_impact"]))
            ):
                proposals.append(proposal)
            elif evaluation_subject(project, approved["campaign_id"], proposal["submission_id"])[
                "fingerprints"
            ]["strategy_and_parameters"] in versions:
                proposals.append(proposal)
        details = [
            {key: value for key, value in item.items() if key != "status"}
            for item in _detail_history(connection, round_id) if item["status"] == "queued"
        ]
        return {"proposals": proposals, "details": details}


def _assert_settled(evaluations: list[dict[str, Any]]) -> None:
    if any(evaluation["status"] == "reading" for evaluation in evaluations):
        raise ValueError("A Validation evaluation is awaiting recovery. Reconcile it before ending the round.")


@contextmanager
def _campaign_round(
    project: Path, round_id: str,
) -> Generator[tuple[dict[str, Any], sqlite3.Connection], None, None]:
    with _round_lock(project, round_id), _project(project) as connection:
        approved = _project_kickoff(connection, round_id)
        with campaign_execution_lock(project, approved["campaign_id"]):
            yield approved, connection


def selected_candidate(project: Path, round_id: str) -> dict[str, Any]:
    """Recover the immutable selection with its exact Candidate and Validation reference."""
    frozen = research_candidate_status(project, round_id)
    if frozen is None:
        raise ValueError("Research round has no frozen Candidate.")
    return {
        **frozen["research_selection"],
        "candidate_id": frozen["candidate_id"],
        "candidate_hash": frozen["candidate_hash"],
    }


def direction_status(project: Path, round_id: str) -> dict[str, Any]:
    frozen = research_candidate_status(project, round_id)
    with _project(project) as connection:
        connection.execute("BEGIN")
        approved = _project_kickoff(connection, round_id)
        evaluations = _evaluations(connection, round_id)
        budget = _validation_budget(approved, evaluations)
        retrospective = _finished_retrospective(connection, round_id)
        status = "open"
        if frozen:
            status = "frozen"
        elif retrospective:
            status = "retrospective_submitted"
        elif budget["remaining"] == 0 and not _has_passed(evaluations) and all(
            item["status"] != "reading" for item in evaluations
        ):
            status = "retrospective_required"
        result = {
            "schema_version": 1, "status": status, "round_id": round_id,
            "direction_id": approved["direction_id"],
            "scope": approved["scope"], "data_kind": approved["data_kind"],
            "kickoff": approved, "budget": budget,
            "proposals": _proposals(connection, round_id),
            "evaluations": evaluations,
            "proposal_evaluations": _proposal_results(connection, round_id),
            "feedback": _feedback(connection, round_id),
            "detail_requests": _detail_history(connection, round_id),
        }
    result["experiments"] = campaign_experiment_history(project, approved["campaign_id"])
    if frozen:
        result["selection"] = selected_candidate(project, round_id)
    if retrospective:
        result["retrospective"] = retrospective
    return result


def list_directions(project: Path) -> dict[str, Any]:
    """Recover durable round IDs and approvals from a project directory."""
    project_id = project_status(project)["project_id"]
    with _project(project) as connection:
        rounds = [
            _project_kickoff(connection, row["round_id"])
            for row in connection.execute("SELECT round_id FROM research_rounds ORDER BY rowid")
        ]
    return {"schema_version": 1, "project_id": project_id, "rounds": rounds}


def _attempt_history(
    experiments: list[dict[str, Any]], evaluations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    evaluations_by_experiment: dict[str, list[dict[str, Any]]] = {}
    for evaluation in evaluations:
        evidence = {field: evaluation[field] for field in (
            "evaluation_id", "plan_id", "record_hash", "status", "outcome", "reason",
        )}
        evaluations_by_experiment.setdefault(evaluation["experiment_id"], []).append(evidence)
    return [
        {
            "experiment_id": experiment["experiment_id"],
            "submission_id": experiment["submission_id"],
            "parent_experiment_id": experiment["parent_experiment_id"],
            "hypothesis": experiment["hypothesis"],
            "discovery_status": experiment["status"],
            "validation": evaluations_by_experiment.get(experiment["experiment_id"], []),
        }
        for experiment in experiments
    ]


def _eliminations(
    experiments: list[dict[str, Any]], selected: str, supplied: dict[str, str],
) -> dict[str, str]:
    alternatives = {item["experiment_id"]: item for item in experiments if item["experiment_id"] != selected}
    if supplied.keys() - alternatives.keys():
        raise ValueError("Elimination reasons must refer to other experiments in this Campaign.")
    eliminated = dict(supplied)
    for identifier, entry in alternatives.items():
        if identifier in eliminated:
            continue
        if entry["status"] == "failed":
            eliminated[identifier] = entry.get("error", {}).get("code", "Discovery failed")
            continue
        raise ValueError(f"Provide an elimination reason for experiment {identifier}.")
    return eliminated


def freeze(
    project: Path, round_id: str, evaluation_id: str, name: str, source: object,
) -> dict[str, Any]:
    """Select one passing Validation proof and freeze its Discovery experiment."""
    payload = _object(source, {"selection_reason", "remaining_risks", "eliminations", "independence"})
    if not isinstance(payload["eliminations"], dict):
        raise ValueError("eliminations must map experiment IDs to reasons.")
    name = _text(name, "name")
    selection_reason = _text(payload["selection_reason"], "selection_reason")
    remaining_risks = _texts(payload["remaining_risks"], "remaining_risks")
    eliminations = {
        _text(key, "experiment_id"): _text(value, "elimination reason")
        for key, value in payload["eliminations"].items()
    }
    selection_independence = _independence(payload["independence"])
    request_hash = _hash({
        "evaluation_id": evaluation_id, "name": name, "selection_reason": selection_reason,
        "remaining_risks": remaining_risks, "eliminations": eliminations,
        "independence": selection_independence,
    })
    with _campaign_round(project, round_id) as (approved, connection):
        previous = research_candidate_status(project, round_id)
        if previous is not None:
            if previous["research_selection"]["selection_request_hash"] == request_hash:
                return previous
            raise ValueError("Research round already has a frozen Candidate.")
        if _finished_retrospective(connection, round_id):
            raise ValueError("The research round ended with a retrospective.")
        selected = _evaluation(connection, round_id, evaluation_id)
        evaluations = _evaluations(connection, round_id)
        _assert_settled(evaluations)
        if not _passed(selected) or not selected["record_hash"]:
            raise ValueError("Freezing requires a passed Validation evaluation.")
        experiments = campaign_experiment_history(project, approved["campaign_id"])
        selection = {
            **{field: approved[field] for field in (
                "direction_id", "round_id", "kickoff_id", "project_id", "campaign_id",
                "snapshot_id", "scope", "data_kind",
            )},
            "design_independence": approved["independence"],
            "selection_independence": selection_independence,
            "selection_request_hash": request_hash,
            "experiment_id": selected["experiment_id"],
            "experiment_hash": selected["experiment_hash"],
            "validation_evaluation_id": evaluation_id,
            "validation_evaluation_hash": selected["record_hash"],
            "validation_plan_id": selected["plan_id"],
            "summary": {
                "versions_attempted": len(experiments),
                "attempts": _attempt_history(experiments, evaluations),
                "budget": _validation_budget(approved, evaluations),
                "eliminations": _eliminations(experiments, selected["experiment_id"], eliminations),
                "validation_feedback": _feedback(connection, round_id),
                "selection_reason": selection_reason,
                "remaining_risks": remaining_risks,
            },
        }
        return freeze_research_candidate(
            project, approved["campaign_id"], selected["submission_id"], name, selection,
        )


def retrospective(project: Path, round_id: str, source: object) -> dict[str, Any]:
    """Record the exhausted round for the research lead's decision."""
    payload = _object(source, {"findings", "next_step"})
    findings = _text(payload["findings"], "findings")
    next_step = _text(payload["next_step"], "next_step")
    with _campaign_round(project, round_id) as (approved, connection):
        previous = _finished_retrospective(connection, round_id)
        if previous is not None:
            if previous["findings"] == findings and previous["next_step"] == next_step:
                return previous
            raise ValueError("The research round already has a submitted retrospective.")
        _require_open(project, connection, round_id)
        evaluations = _evaluations(connection, round_id)
        _assert_settled(evaluations)
        budget = _validation_budget(approved, evaluations)
        if budget["remaining"] or _has_passed(evaluations):
            raise ValueError("Submit a retrospective when the Validation budget is exhausted without a pass.")
        experiments = campaign_experiment_history(project, approved["campaign_id"])
        body = {
            "schema_version": 1,
            "status": "retrospective_submitted",
            **{field: approved[field] for field in (
                "round_id", "direction_id", "kickoff_id", "scope", "data_kind",
            )},
            "findings": findings,
            "next_step": next_step,
            "budget": budget,
            "versions_attempted": len(experiments),
            "attempts": _attempt_history(experiments, evaluations),
            "evaluations": evaluations,
            "validation_feedback": _feedback(connection, round_id),
        }
        receipt = {**body, "retrospective_id": _hash(body)}
        connection.execute(
            "INSERT INTO research_retrospectives VALUES (?, ?)", (round_id, _json(receipt)),
        )
        return receipt
