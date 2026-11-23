"""Kickoff, development evidence, and selection for a research direction."""

from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
import unicodedata
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

from prajnatrade.experiment import (
    campaign_discovery,
    campaign_experiment_history,
    evaluation_subject,
    project_status,
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
"""


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: object) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


@contextmanager
def _project(project: Path) -> Iterator[sqlite3.Connection]:
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


def prepare_kickoff(
    project: Path, campaign_id: str, direction_key: str, name: str,
    authorization_id: str, source: object,
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
    direction_id = _hash({"direction_key": key})
    round_id = _hash({
        "direction_id": direction_id,
        "project_id": campaign["project_id"],
        "campaign_id": campaign_id,
    })
    approved = {
        "schema_version": 1,
        "direction_id": direction_id,
        "direction_key": key,
        "round_id": round_id,
        "name": _text(name, "name"),
        **campaign,
        "scope": evaluation["data_kind"],
        "data_kind": evaluation["data_kind"],
        "objective": _text(payload["objective"], "objective"),
        "constraints": _texts(payload["constraints"], "constraints"),
        "validation_budget": budget,
        "validation_settings": normalize_validation_protocol(
            payload["validation_settings"], evaluation,
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
    target = _kickoff_path(evaluation_store(store), round_id)
    with _project(project) as connection:
        connection.execute("BEGIN IMMEDIATE")
        previous = connection.execute(
            "SELECT kickoff_json FROM research_rounds WHERE campaign_id = ?",
            (prepared["campaign_id"],),
        ).fetchone()
        content = _json(prepared)
        if previous is not None and previous["kickoff_json"] != content:
            raise ValueError("Campaign already has an approved research kickoff.")

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
def _round_lock(project: Path, round_id: str) -> Iterator[None]:
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
        connection.execute(
            "INSERT OR IGNORE INTO research_validation_proposals VALUES (?, ?, ?)",
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


def direction_status(project: Path, round_id: str) -> dict[str, Any]:
    with _project(project) as connection:
        approved = _project_kickoff(connection, round_id)
        proposals = _proposals(connection, round_id)
    limit = approved["validation_budget"]
    return {
        "schema_version": 1, "status": "open", "round_id": round_id,
        "direction_id": approved["direction_id"],
        "scope": approved["scope"], "data_kind": approved["data_kind"],
        "kickoff": approved,
        "budget": {"limit": limit, "used": 0, "remaining": limit},
        "experiments": campaign_experiment_history(project, approved["campaign_id"]),
        "proposals": proposals,
    }
