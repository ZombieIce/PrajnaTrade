"""Research rounds, lightweight Validation, review admission and revalidation (spec #43).

This module is the manual simulated-tracking admission path.  It is deliberately
separate from the protected ``evaluation``/``holdout`` flow and from conditional
``release``: lightweight Validation replays the locked Discovery version over the
Campaign's frozen Discovery interval using the same sealed snapshot, point-in-time
cutoffs, execution rules, costs and initial cash.  It never opens a protected
partition, never authorizes a stage and never writes a query debit.

Contract resolutions that the shared design contract leaves implicit:

* ``round_id`` reuses :func:`research_direction.direction_id`, so a direction
  keeps its identity across renamed keys and new Campaigns.
* ``validation_id`` includes the *current* accounting-engine fingerprint, so an
  accounting-platform fix yields a new identity and re-runs the replay.  The
  running attempt records the current runtime.
* Review decisions are immutable content-addressed epochs per version with
  exactly one current state.  Approval binds the stored terminal passed receipt,
  writes a write-once ``manual_tracking`` release artifact and appends one
  ``enable`` event at the first approved calendar session after the decision; it
  never runs Validation again, so re-selecting a version reuses its receipt.
* ``replay_engine_identity`` fingerprints this module's own replay/metric code.
  ``validation_id`` does not cover it, so :func:`revalidate` binds it together
  with the current runtime and evidence source into the new identity and records
  the superseded receipt as historical evidence.
* :func:`revalidate` accepts a sealed successor of the locked snapshot (explicitly
  or as the store's only discoverable successor).  A corrected successor keeps
  the strategy identity but changes the evidence source, so the candidate and
  the benchmark are recomputed over the corrected Discovery partition and the
  original Discovery result, the affected-session diff and the new receipt are
  recorded together.  The verdict can only pass when the recomputed result still
  meets the frozen protocol and the replay reproduces it.
* Inputs that cannot be replayed at all (a different snapshot store, a drifted
  Campaign binding) are protocol rejections; evidence verdicts are
  ``passed`` / ``failed`` / ``insufficient_evidence`` and exit 0.
* The trusted validation copy keeps ``attempts`` / ``resumed`` provenance; the
  project copy publishes only the research-visible field allowlist.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import operator
import os
import sqlite3
from collections.abc import Generator, Mapping
from contextlib import closing, contextmanager
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from prajnatrade import release, shadow_calendar, snapshot
from prajnatrade.container_executor import executor_from_config
from prajnatrade.data import ETF_SYMBOLS
from prajnatrade.experiment import (
    ProtocolError,
    campaign_discovery,
    campaign_execution_lock,
    campaign_experiment_history,
    evaluation_subject,
    project_status,
    runtime_identity,
    signal_window_sessions,
)
from prajnatrade.research_direction import direction_id
from prajnatrade.snapshot_baseline import (
    NEXT_OPEN_RULES,
    run_discovery_equal_weight,
    run_partition_strategy,
)
from prajnatrade.strategies import make_strategy

SCHEMA_VERSION = 1
METRIC_TOLERANCE = 1e-9
SCORING_MODES = ("valid_observations", "consecutive_sessions")
DATA_AVAILABILITY = "next_open"
BENCHMARK = "equal_weight_buy_and_hold"
ROUND_NAME = "research-loop-rounds"
VALIDATION_NAME = "research-loop-validations"
REVIEW_NAME = "research-loop-reviews"
REVALIDATION_NAME = "research-loop-revalidations"
VALIDATION_FAILPOINT = "round_validation_after_attempt"
REVIEW_STATES = ("pending", "approved", "returned", "ended")
REVIEW_ACTION_STATE = {"approve": "approved", "return": "returned", "end": "ended"}
EVIDENCE_NAME = "research-loop-evidence"
EVIDENCE_DECISIONS = ("continue", "pause", "retire")
EVIDENCE_STATES = ("tracking", "review_due", "observation_extended", "paused", "ended")
MAX_OBSERVATION_SESSIONS = 252
SHANGHAI = ZoneInfo("Asia/Shanghai")

METRIC_REGISTRY = {
    "net_return": "total_return",
    "max_drawdown": "max_drawdown",
    "sharpe": "sharpe",
    "turnover": "turnover",
    "unfilled_orders": "orders_unfilled",
    "valued_sessions": "valued_sessions",
}
OPERATORS = {
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
    "==": operator.eq,
}
_POSITION_STATES = {"initial_position_established", "intentional_cash"}
_OBSERVATION_FORMULA = (
    "ceil(sessions / max(1, opportunities)) * minimum_opportunities, "
    "clamped to [minimum_opportunities, 252]"
)
_RECEIPT_FIELDS = (
    "schema_version", "round_id", "version_id", "validation_id", "status", "outcome",
    "reason", "metrics", "checks", "observation", "replay", "receipt_hash",
    "opened_protected_source", "query_debited",
)
_OPTIONAL_RECEIPT_FIELDS = (
    "evidence", "original_discovery", "recomputed_discovery", "affected",
    "prior_validation_id",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_loop_rounds (
    round_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL UNIQUE REFERENCES campaigns(campaign_id),
    kickoff_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_loop_submissions (
    version_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL REFERENCES research_loop_rounds(round_id),
    submission_id TEXT NOT NULL,
    submission_json TEXT NOT NULL,
    UNIQUE (round_id, submission_id)
);
CREATE TABLE IF NOT EXISTS research_loop_validations (
    validation_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL UNIQUE REFERENCES research_loop_submissions(version_id),
    receipt_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_loop_validation_attempts (
    validation_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'interrupted')),
    attempt_json TEXT NOT NULL,
    PRIMARY KEY (validation_id, attempt)
);
CREATE TABLE IF NOT EXISTS research_loop_reviews (
    decision_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES research_loop_submissions(version_id),
    review_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_loop_observations (
    version_id TEXT PRIMARY KEY REFERENCES research_loop_submissions(version_id),
    observation_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_loop_closures (
    round_id TEXT PRIMARY KEY REFERENCES research_loop_rounds(round_id),
    closure_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_loop_revalidations (
    revalidation_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES research_loop_submissions(version_id),
    validation_id TEXT NOT NULL,
    prior_validation_id TEXT NOT NULL,
    revalidation_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_loop_evidence_decisions (
    decision_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES research_loop_submissions(version_id),
    decision_json TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# Canonical values and project database
# ---------------------------------------------------------------------------


def _json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _hash(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


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
        names = ", ".join(sorted(required))
        raise ValueError(f"Expected fields: {names}.")
    return raw


def _text(raw: object, field: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{field} must be a non-empty string.")
    return raw.strip()


def _texts(raw: object, field: str) -> list[str]:
    if not isinstance(raw, list):
        raise ValueError(f"{field} must be a list of strings.")
    return [_text(value, field) for value in raw]


def _positive_integer(raw: object, field: str) -> int:
    if type(raw) is not int or raw <= 0:
        raise ValueError(f"{field} must be a positive integer.")
    return raw


def _non_negative_integer(raw: object, field: str) -> int:
    if type(raw) is not int or raw < 0:
        raise ValueError(f"{field} must be a non-negative integer.")
    return raw


def _number(raw: object, field: str) -> float | int:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        raise ValueError(f"{field} must be a finite number.")
    return raw


def _require_hex(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"Invalid {label} ID.")
    return value


@contextmanager
def _round_lock(project: Path, round_id: str) -> Generator[None, None, None]:
    _require_hex(round_id, "research round")
    project_status(project)
    locks = project / ".prajnatrade" / "research-loop-locks"
    locks.mkdir(exist_ok=True)
    with (locks / f"{round_id}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


# ---------------------------------------------------------------------------
# Approved round input
# ---------------------------------------------------------------------------


def _hard_conditions(raw: object) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("hard_conditions must be a list.")
    conditions: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    for item in raw:
        entry = _object(item, {"id", "metric", "operator", "value"})
        identifier = _text(entry["id"], "hard_conditions.id")
        if identifier in identifiers:
            raise ValueError(f"Duplicate hard condition ID: {identifier!r}.")
        identifiers.add(identifier)
        metric = entry["metric"]
        if metric not in METRIC_REGISTRY:
            raise ValueError(f"Unsupported hard condition metric: {metric!r}.")
        rule = entry["operator"]
        if rule not in OPERATORS:
            raise ValueError(f"Unsupported hard condition operator: {rule!r}.")
        conditions.append(
            {
                "id": identifier,
                "metric": metric,
                "operator": rule,
                "value": _number(entry["value"], "hard_conditions.value"),
            }
        )
    return conditions


def _scoring_policy(raw: object) -> dict[str, Any]:
    entry = _object(raw, {"mode", "data_availability", "consecutive_sessions"})
    mode = entry["mode"]
    if mode not in SCORING_MODES:
        raise ValueError(
            "scoring_policy.mode must be valid_observations or consecutive_sessions."
        )
    if entry["data_availability"] != DATA_AVAILABILITY:
        raise ValueError("scoring_policy.data_availability must be next_open.")
    flag = entry["consecutive_sessions"]
    if type(flag) is not bool:
        raise ValueError("scoring_policy.consecutive_sessions must be a boolean.")
    if flag != (mode == "consecutive_sessions"):
        raise ValueError("scoring_policy.consecutive_sessions must match the scoring mode.")
    return {"mode": mode, "data_availability": DATA_AVAILABILITY, "consecutive_sessions": flag}


def _observation_policy(raw: object) -> dict[str, Any]:
    entry = _object(raw, {"basis", "minimum_opportunities", "review_conditions"})
    return {
        "basis": _text(entry["basis"], "observation.basis"),
        "minimum_opportunities": _positive_integer(
            entry["minimum_opportunities"], "observation.minimum_opportunities"
        ),
        "review_conditions": _texts(entry["review_conditions"], "observation.review_conditions"),
    }


def _invalidation_policy(raw: object) -> dict[str, Any]:
    entry = _object(
        raw, {"max_unfilled_opportunities", "max_abs_drawdown", "max_data_gap_sessions"}
    )
    drawdown = _number(entry["max_abs_drawdown"], "invalidation.max_abs_drawdown")
    if drawdown <= 0:
        raise ValueError("invalidation.max_abs_drawdown must be positive.")
    return {
        "max_unfilled_opportunities": _non_negative_integer(
            entry["max_unfilled_opportunities"], "invalidation.max_unfilled_opportunities"
        ),
        "max_abs_drawdown": drawdown,
        "max_data_gap_sessions": _non_negative_integer(
            entry["max_data_gap_sessions"], "invalidation.max_data_gap_sessions"
        ),
    }


def _round_contract(source: object) -> dict[str, Any]:
    payload = _object(
        source,
        {
            "objective", "constraints", "comparison", "selection_rules", "hard_conditions",
            "disclosed_risks", "scoring_policy", "observation", "invalidation", "reviewer",
            "approval_reason",
        },
    )
    comparison = _object(payload["comparison"], {"benchmark", "statement"})
    if comparison["benchmark"] != BENCHMARK:
        raise ValueError(f"comparison.benchmark must be {BENCHMARK}.")
    return {
        "objective": _text(payload["objective"], "objective"),
        "constraints": _texts(payload["constraints"], "constraints"),
        "comparison": {
            "benchmark": BENCHMARK,
            "statement": _text(comparison["statement"], "comparison.statement"),
        },
        "selection_rules": _texts(payload["selection_rules"], "selection_rules"),
        "hard_conditions": _hard_conditions(payload["hard_conditions"]),
        "disclosed_risks": _texts(payload["disclosed_risks"], "disclosed_risks"),
        "scoring_policy": _scoring_policy(payload["scoring_policy"]),
        "observation": _observation_policy(payload["observation"]),
        "invalidation": _invalidation_policy(payload["invalidation"]),
        "reviewer": _text(payload["reviewer"], "reviewer"),
        "approval_reason": _text(payload["approval_reason"], "approval_reason"),
    }


def _frozen_scoring_policy(policy: object) -> dict[str, Any]:
    if not isinstance(policy, dict) or not policy:
        raise ProtocolError(
            "SCORING_POLICY_NOT_FROZEN", "round", "inspect",
            "The Campaign has no frozen Discovery scoring policy.",
        )
    if "scoring_start" not in policy or "warmup_sessions" not in policy:
        raise ProtocolError(
            "SCORING_POLICY_NOT_FROZEN", "round", "inspect",
            "The Campaign's frozen scoring policy is incomplete.",
        )
    scoring_start = policy["scoring_start"]
    if not isinstance(scoring_start, str) or len(scoring_start) != 10:
        raise ValueError("The frozen scoring_start must be YYYY-MM-DD.")
    _non_negative_integer(policy["warmup_sessions"], "history_policy.warmup_sessions")
    frozen = dict(policy)
    if "prior_history_hash" in frozen:
        _require_hex(frozen["prior_history_hash"], "prior history")
    return frozen


# ---------------------------------------------------------------------------
# Trusted round copies
# ---------------------------------------------------------------------------


def _round_path(store: Path, round_id: str) -> Path:
    return store / ROUND_NAME / f"{_require_hex(round_id, 'research round')}.json"


def _verify_round(record: dict[str, Any], round_id: str) -> None:
    body = {
        key: value for key, value in record.items() if key not in {"kickoff_hash", "opened_at"}
    }
    if record.get("round_id") != round_id or record.get("kickoff_hash") != _hash(body):
        raise ValueError("Research round fingerprint mismatch.")


def _load_round_file(path: Path, round_id: str) -> dict[str, Any]:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ProtocolError(
            "ROUND_NOT_FOUND", "round", "check_id", "Unknown trusted research round."
        ) from error
    if not isinstance(record, dict):
        raise ValueError("Invalid trusted research round.")
    _verify_round(record, round_id)
    return record


def load_round(store: Path, round_id: str) -> dict[str, Any]:
    """Load and verify the trusted approved round with its frozen contract."""
    from prajnatrade.protected_eval import evaluation_store

    return _load_round_file(_round_path(evaluation_store(store), round_id), round_id)


def _project_round(connection: sqlite3.Connection, round_id: str) -> dict[str, Any]:
    _require_hex(round_id, "research round")
    row = connection.execute(
        "SELECT kickoff_json FROM research_loop_rounds WHERE round_id = ?", (round_id,),
    ).fetchone()
    if row is None:
        raise ProtocolError(
            "ROUND_NOT_FOUND", "round", "check_id", "Unknown research round."
        )
    record = json.loads(row["kickoff_json"])
    _verify_round(record, round_id)
    return record


def _closure(connection: sqlite3.Connection, round_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT closure_json FROM research_loop_closures WHERE round_id = ?", (round_id,),
    ).fetchone()
    return json.loads(row["closure_json"]) if row is not None else None


# ---------------------------------------------------------------------------
# round open
# ---------------------------------------------------------------------------


def prepare_round(
    project: Path, campaign_id: str, direction_key: str, name: str,
    authorization_id: str, source: object, *, store: Path,
) -> dict[str, Any]:
    """Normalize the approved round contract and its frozen Campaign bindings."""
    contract = _round_contract(source)
    campaign = campaign_discovery(project, campaign_id)
    evaluation = campaign["evaluation"]
    frozen = _frozen_scoring_policy(evaluation.get("history_policy"))
    if store.resolve().is_relative_to(project.resolve()):
        raise ValueError("Trusted evaluation store must be outside the research project.")
    direction = direction_id(direction_key)
    round_id = _hash(
        {
            "direction_id": direction,
            "project_id": campaign["project_id"],
            "campaign_id": campaign_id,
        }
    )
    kickoff = {
        **contract,
        "scoring_policy": {**contract["scoring_policy"], "frozen": frozen},
        "frozen_campaign": {
            "snapshot_id": evaluation["snapshot_id"],
            "snapshot_hash": campaign["snapshot_hash"],
            "campaign_evaluation_hash": campaign["campaign_evaluation_hash"],
            "partition_content_hash": evaluation["partition_content_hash"],
            "data_kind": evaluation["data_kind"],
            "execution_rules": evaluation["execution_rules"],
            "strategy_executor": evaluation.get("strategy_executor", {"kind": "none"}),
            "runtime": evaluation["runtime"],
            "initial_cash": evaluation["initial_cash"],
            "transaction_cost_bps": evaluation["transaction_cost_bps"],
            "slippage_bps": evaluation["slippage_bps"],
        },
        "budget": {
            "attempts": campaign["discovery_budget"]["attempts"],
            "max_seconds": campaign["discovery_budget"]["max_seconds"],
        },
    }
    body = {
        "schema_version": SCHEMA_VERSION,
        "round_id": round_id,
        "direction_id": direction,
        "project_id": campaign["project_id"],
        "campaign_id": campaign_id,
        "name": _text(name, "name"),
        "authorization_id": _text(authorization_id, "authorization_id"),
        "scope": evaluation["data_kind"],
        "data_kind": evaluation["data_kind"],
        "kickoff": kickoff,
    }
    return {**body, "kickoff_hash": _hash(body), "opened_at": _now()}


def _opened(record: dict[str, Any], *, reused: bool) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "opened",
        "reused": reused,
        "round_id": record["round_id"],
        "direction_id": record["direction_id"],
        "project_id": record["project_id"],
        "campaign_id": record["campaign_id"],
        "scope": record["scope"],
        "data_kind": record["data_kind"],
        "kickoff": record["kickoff"],
        "kickoff_hash": record["kickoff_hash"],
    }


def _require_campaign_binding(record: dict[str, Any], campaign: Mapping[str, Any]) -> None:
    """Refuse to replay against a Campaign that changed after the round opened."""
    frozen = record["kickoff"]["frozen_campaign"]
    evaluation = campaign["evaluation"]
    bindings = {
        "campaign evaluation": (
            frozen["campaign_evaluation_hash"], campaign["campaign_evaluation_hash"],
        ),
        "snapshot": (frozen["snapshot_id"], evaluation["snapshot_id"]),
        "snapshot content": (frozen["snapshot_hash"], campaign["snapshot_hash"]),
        "Discovery partition": (
            frozen["partition_content_hash"], evaluation["partition_content_hash"],
        ),
        "accounting runtime": (frozen["runtime"], evaluation["runtime"]),
        "execution rules": (frozen["execution_rules"], evaluation["execution_rules"]),
        "scoring policy": (
            record["kickoff"]["scoring_policy"]["frozen"], evaluation.get("history_policy"),
        ),
    }
    for label, (expected, actual) in bindings.items():
        if expected != actual:
            raise ValueError(f"The Campaign {label} changed after the research round opened.")


def open_round(
    project: Path, store: Path, prepared: dict[str, Any],
) -> dict[str, Any]:
    """Persist the write-once trusted approval and its research-visible copy."""
    from prajnatrade.protected_eval import evaluation_store, write_trusted_record_once

    round_id = prepared["round_id"]
    _verify_round(prepared, round_id)
    campaign = campaign_discovery(project, prepared["campaign_id"])
    _require_campaign_binding(prepared, campaign)
    trusted = evaluation_store(store)
    if trusted.is_relative_to(project.resolve()):
        raise ValueError("Trusted evaluation store must be outside the research project.")
    target = _round_path(trusted, round_id)
    with _round_lock(project, round_id), campaign_execution_lock(
        project, prepared["campaign_id"]
    ), _project(project) as connection:
        connection.execute("BEGIN IMMEDIATE")
        previous = connection.execute(
            "SELECT kickoff_json FROM research_loop_rounds WHERE campaign_id = ?",
            (prepared["campaign_id"],),
        ).fetchone()
        if previous is not None:
            stored = json.loads(previous["kickoff_json"])
            _verify_round(stored, round_id)
            if stored["kickoff_hash"] != prepared["kickoff_hash"]:
                raise ProtocolError(
                    "ROUND_ALREADY_OPENED", "round", "inspect",
                    "The Campaign already has a different approved research round.",
                )
            trusted_record = _load_round_file(target, round_id)
            if trusted_record != stored:
                raise ValueError("The trusted research round copy differs from the project copy.")
            return _opened(trusted_record, reused=True)
        stored = write_trusted_record_once(target, prepared)
        if stored != prepared:
            raise ProtocolError(
                "ROUND_ALREADY_OPENED", "round", "inspect",
                "The Campaign already has a different approved research round.",
            )
        connection.execute(
            "INSERT INTO research_loop_rounds (round_id, campaign_id, kickoff_json) "
            "VALUES (?, ?, ?)",
            (round_id, prepared["campaign_id"], _json(prepared)),
        )
    return _opened(prepared, reused=False)


# ---------------------------------------------------------------------------
# round submit
# ---------------------------------------------------------------------------


def _locked_strategy(attempt: Mapping[str, Any]) -> dict[str, Any]:
    strategy = attempt["strategy"]
    locked: dict[str, Any] = {
        "family": strategy["family"],
        "parameters": attempt["parameters"],
    }
    if strategy["family"] == "python":
        locked["source"] = strategy["source"]
        if "source_sha256" in strategy:
            locked["source_sha256"] = strategy["source_sha256"]
        if "parameter_schema" in strategy:
            locked["parameter_schema"] = strategy["parameter_schema"]
    return locked


def _protocol_hash(kickoff: Mapping[str, Any]) -> str:
    return _hash(
        {
            "schema_version": SCHEMA_VERSION,
            "comparison": kickoff["comparison"],
            "hard_conditions": kickoff["hard_conditions"],
            "selection_rules": kickoff["selection_rules"],
        }
    )


def _scoring_policy_hash(kickoff: Mapping[str, Any]) -> str:
    return _hash(kickoff["scoring_policy"])


def _current_runtime(campaign: Mapping[str, Any]) -> str:
    evaluation = campaign["evaluation"]
    return runtime_identity(
        evaluation.get("strategy_executor", {"kind": "none"}),
        execution_rules=evaluation["execution_rules"],
        history_policy=evaluation.get("history_policy"),
    )


def _version_identity(
    record: Mapping[str, Any],
    strategy_fingerprint: str,
    experiment_hash: str,
    snapshot_hash: str,
    evaluation_hash: str,
    execution_rules: Mapping[str, Any],
    base_costs: Mapping[str, Any],
    protocol_hash: str,
) -> str:
    return _hash(
        {
            "round_id": record["round_id"],
            "strategy_fingerprint": strategy_fingerprint,
            "experiment_hash": experiment_hash,
            "snapshot_hash": snapshot_hash,
            "evaluation_hash": evaluation_hash,
            "execution_rules": execution_rules,
            "base_costs": base_costs,
            "protocol_hash": protocol_hash,
        }
    )


def _submitted(body: dict[str, Any], *, reused: bool, requested: str | None = None) -> dict[str, Any]:
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": "submitted",
        "reused": reused,
        "round_id": body["round_id"],
        "version_id": body["version_id"],
        "submission_id": body["submission_id"],
        "experiment_id": body["experiment_id"],
        "strategy_fingerprint": body["strategy_fingerprint"],
        "locked": body["locked"],
    }
    if requested is not None and requested != body["submission_id"]:
        result["requested_submission_id"] = requested
    return result


def submit(
    project: Path, round_id: str, submission_id: str, source: object,
) -> dict[str, Any]:
    """Lock one successful Discovery experiment into a content-addressed version."""
    payload = _object(
        source,
        {
            "experiment_id", "change", "question", "expected_change", "revisit_condition",
            "risks",
        },
        frozenset({"related_experiment_ids"}),
    )
    experiment_id = _text(payload["experiment_id"], "experiment_id")
    change = _text(payload["change"], "change")
    question = _text(payload["question"], "question")
    expected_change = _text(payload["expected_change"], "expected_change")
    revisit_condition = _text(payload["revisit_condition"], "revisit_condition")
    risks = _texts(payload["risks"], "risks")
    related = _texts(payload.get("related_experiment_ids", []), "related_experiment_ids")
    submission_id = _text(submission_id, "submission_id")
    with _round_lock(project, round_id), _project(project) as connection:
        record = _project_round(connection, round_id)
        if _closure(connection, round_id) is not None:
            raise ProtocolError(
                "ROUND_CLOSED", "submission", "inspect",
                "A closed research round cannot accept a new version.",
            )
        campaign = campaign_discovery(project, record["campaign_id"])
        attempts = campaign_experiment_history(project, record["campaign_id"])
        experiment = next(
            (item for item in attempts if item["experiment_id"] == experiment_id), None
        )
        if experiment is None or experiment["status"] != "succeeded":
            raise ProtocolError(
                "EXPERIMENT_NOT_ELIGIBLE", "submission", "correct",
                "The submission must lock a successful experiment from this Campaign.",
            )
        other_ids = {
            item["experiment_id"] for item in attempts if item["experiment_id"] != experiment_id
        }
        if not set(related) <= other_ids:
            raise ValueError("Related experiment IDs must refer to other attempts in this Campaign.")
        subject = evaluation_subject(project, record["campaign_id"], experiment["submission_id"])
        evaluation = campaign["evaluation"]
        strategy = _locked_strategy(experiment)
        base_costs = {
            "initial_cash": evaluation["initial_cash"],
            "transaction_cost_bps": evaluation["transaction_cost_bps"],
            "slippage_bps": evaluation["slippage_bps"],
        }
        protocol_hash = _protocol_hash(record["kickoff"])
        strategy_fingerprint = experiment["fingerprints"]["strategy_and_parameters"]
        experiment_hash = subject["candidate_hash"]
        version_id = _version_identity(
            record,
            strategy_fingerprint,
            experiment_hash,
            campaign["snapshot_hash"],
            campaign["campaign_evaluation_hash"],
            evaluation["execution_rules"],
            base_costs,
            protocol_hash,
        )
        body = {
            "schema_version": SCHEMA_VERSION,
            "round_id": round_id,
            "version_id": version_id,
            "submission_id": submission_id,
            "experiment_id": experiment_id,
            "experiment_submission_id": experiment["submission_id"],
            "change": change,
            "question": question,
            "expected_change": expected_change,
            "revisit_condition": revisit_condition,
            "risks": sorted(set(risks)),
            "related_experiment_ids": sorted(set(related)),
            "strategy_fingerprint": strategy_fingerprint,
            "experiment_hash": experiment_hash,
            "locked": {
                "snapshot_id": evaluation["snapshot_id"],
                "snapshot_hash": campaign["snapshot_hash"],
                "evaluation_hash": campaign["campaign_evaluation_hash"],
                "execution_rules": evaluation["execution_rules"],
                "base_costs": base_costs,
                "protocol_hash": protocol_hash,
                "signal_window_sessions": signal_window_sessions(strategy),
                "history_requirements": experiment["strategy"].get("history_requirements", []),
                "history_policy": record["kickoff"]["scoring_policy"]["frozen"],
                "strategy_executor": evaluation.get("strategy_executor", {"kind": "none"}),
                "runtime": evaluation["runtime"],
                "strategy": strategy,
            },
        }
        previous = connection.execute(
            "SELECT submission_json FROM research_loop_submissions "
            "WHERE round_id = ? AND submission_id = ?",
            (round_id, submission_id),
        ).fetchone()
        if previous is not None:
            prior = json.loads(previous["submission_json"])
            if _json(
                {key: value for key, value in prior.items() if key != "version_id"}
            ) != _json({key: value for key, value in body.items() if key != "version_id"}):
                raise ProtocolError(
                    "SUBMISSION_CONFLICT", "submission", "correct",
                    "Submission ID already has different content.",
                )
            return _submitted(prior, reused=True)
        existing = connection.execute(
            "SELECT submission_json FROM research_loop_submissions WHERE version_id = ?",
            (version_id,),
        ).fetchone()
        if existing is not None:
            return _submitted(json.loads(existing["submission_json"]), reused=True, requested=submission_id)
        connection.execute(
            "INSERT INTO research_loop_submissions "
            "(version_id, round_id, submission_id, submission_json) VALUES (?, ?, ?, ?)",
            (version_id, round_id, submission_id, _json(body)),
        )
    return _submitted(body, reused=False)


# ---------------------------------------------------------------------------
# Lightweight Validation: replay and receipt
# ---------------------------------------------------------------------------


def _strategy_from_locked(
    strategy: Mapping[str, Any], executor: Mapping[str, Any],
):
    spec: dict[str, Any] = {
        "family": strategy["family"],
        "parameters": strategy["parameters"],
    }
    if strategy["family"] != "python":
        return make_strategy(spec)
    source = strategy["source"]
    if "source_sha256" in strategy:
        digest = hashlib.sha256(str(strategy["source"]).encode("utf-8")).hexdigest()
        if digest != strategy["source_sha256"]:
            raise ValueError("The locked Python strategy source changed.")
    spec["source"] = source
    if "parameter_schema" in strategy:
        spec["parameter_schema"] = strategy["parameter_schema"]
    if executor.get("kind") != "container":
        raise ValueError("The locked Python strategy needs its container executor.")
    return make_strategy(spec, container_executor=executor_from_config(dict(executor)))


def _experiment_result(
    project: Path, campaign_id: str, experiment_id: str,
) -> dict[str, Any]:
    experiment = next(
        (
            item for item in campaign_experiment_history(project, campaign_id)
            if item["experiment_id"] == experiment_id
        ),
        None,
    )
    if experiment is None or experiment["status"] != "succeeded":
        raise ValueError("The locked Discovery experiment is unavailable.")
    return experiment


def _evidence_runs(
    snapshot_store: Path,
    record: Mapping[str, Any],
    submission: Mapping[str, Any],
    campaign: Mapping[str, Any],
    source: Mapping[str, Any],
) -> dict[str, Any]:
    """Run the locked candidate and the benchmark over one Discovery source."""
    kickoff = record["kickoff"]
    frozen = kickoff["scoring_policy"]["frozen"]
    locked = submission["locked"]
    evaluation = campaign["evaluation"]
    cutoff = source["as_of"] if "as_of" in source else evaluation.get("as_of")
    receipt = snapshot.read_discovery(
        Path(snapshot_store), source["snapshot_id"], "discovery", cutoff
    )
    if receipt["data_kind"] != source["data_kind"]:
        raise ValueError("The snapshot store data kind differs from the Campaign.")
    partition_hash = receipt.get("partition_content_hash", receipt["content_hash"])
    if partition_hash != source["partition_content_hash"]:
        raise ValueError("The snapshot store Discovery content differs from the Campaign.")
    if "snapshot_hash" in source and receipt["content_hash"] != source["snapshot_hash"]:
        raise ValueError("The snapshot Discovery view changed at the frozen cutoff.")
    if receipt.get("prior_history_hash") != frozen.get("prior_history_hash"):
        raise ValueError("The snapshot store prior history differs from the Campaign.")
    data = receipt["data"]
    prior = receipt.get("prior_history")
    sessions = sorted({row["date"] for row in data["calendar"]})
    scoring_start = frozen["scoring_start"]
    if scoring_start not in sessions:
        raise ValueError("The frozen scoring start is not a Discovery trading session.")
    expected = [day for day in sessions if day >= scoring_start]
    prior_history = (prior["sessions"], prior["derived"]) if prior is not None else None
    rules = locked["execution_rules"]
    rule_version = rules.get("version", NEXT_OPEN_RULES)
    options = {
        "data_kind": source["data_kind"],
        "signal_window_sessions": locked["signal_window_sessions"],
        "prior_history": prior_history,
        "initial_cash": locked["base_costs"]["initial_cash"],
        "transaction_cost_bps": locked["base_costs"]["transaction_cost_bps"],
        "slippage_bps": locked["base_costs"]["slippage_bps"],
        "execution_rule_version": rule_version,
        "history_requirements": locked["history_requirements"],
        "scoring_start": scoring_start,
    }
    strategy = _strategy_from_locked(locked["strategy"], locked["strategy_executor"])
    candidate = run_partition_strategy(
        data,
        source["snapshot_id"],
        source["partition_content_hash"],
        strategy,
        partition_name="discovery",
        **options,
    )
    benchmark = run_discovery_equal_weight(
        data,
        source["snapshot_id"],
        source["partition_content_hash"],
        data_kind=source["data_kind"],
        initial_cash=locked["base_costs"]["initial_cash"],
        transaction_cost_bps=locked["base_costs"]["transaction_cost_bps"],
        slippage_bps=locked["base_costs"]["slippage_bps"],
        execution_rule_version=rule_version,
        history_requirements=[],
        scoring_start=scoring_start,
    )
    return {
        "candidate": candidate,
        "benchmark": benchmark,
        "expected_sessions": expected,
        "scoring_start": scoring_start,
        "data": data,
        "snapshot": {
            "verified": True,
            "snapshot_id": source["snapshot_id"],
            "partition_content_hash": source["partition_content_hash"],
            "prior_history_hash": receipt.get("prior_history_hash"),
            "scope": source["data_kind"],
            "data_kind": source["data_kind"],
            "as_of": cutoff,
        },
    }


def _replay(
    project: Path,
    snapshot_store: Path,
    record: Mapping[str, Any],
    submission: Mapping[str, Any],
    campaign: Mapping[str, Any],
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    evaluation = campaign["evaluation"]
    source = evidence if evidence is not None else {
        "snapshot_id": evaluation["snapshot_id"],
        "partition_content_hash": evaluation["partition_content_hash"],
        "data_kind": evaluation["data_kind"],
    }
    runs = _evidence_runs(snapshot_store, record, submission, campaign, source)
    experiment = _experiment_result(
        project, record["campaign_id"], submission["experiment_id"]
    )
    original = experiment["discovery"]
    return {
        **runs,
        "original_candidate": original,
        "original_benchmark": original["baseline_comparison"]["baseline"],
    }


def _metrics(run: Mapping[str, Any]) -> dict[str, Any]:
    metrics = dict(run["metrics"])
    metrics["turnover"] = math.fsum(
        float(fill["notional"]) for fill in run["fills"]
    ) / float(run["initial_cash"])
    return metrics


def _within(left: object, right: object) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return abs(float(left) - float(right)) <= METRIC_TOLERANCE


def _fills_match(left: list[dict], right: list[dict]) -> bool:
    if len(left) != len(right):
        return False
    for one, other in zip(left, right, strict=True):
        if any(one.get(field) != other.get(field) for field in ("date", "symbol", "side")):
            return False
        if not all(
            _within(one.get(field), other.get(field))
            for field in ("quantity", "price", "notional", "fee", "slippage")
        ):
            return False
    return True


def _run_summary(run: Mapping[str, Any], *, first_decision: bool = False) -> dict[str, Any]:
    metrics = run["metrics"]
    summary: dict[str, Any] = {
        "start_date": run["start_date"],
        "end_date": run["end_date"],
        "sessions": metrics["sessions"],
        "valued_sessions": metrics["valued_sessions"],
        "fills": metrics["fills"],
        "ending_equity": metrics["ending_equity"],
        "fees_paid": metrics["fees_paid"],
        "slippage_paid": metrics["slippage_paid"],
        "orders_filled": metrics["orders_filled"],
        "orders_unfilled": metrics["orders_unfilled"],
        "first_fill": run["fills"][0] if run["fills"] else None,
    }
    if first_decision:
        signals = run.get("signals") or []
        summary["first_decision"] = (
            {
                "date": signals[0]["date"],
                "window": list(signals[0].get("window", [])),
                "target_weights": dict(signals[0]["target_weights"]),
            }
            if signals
            else None
        )
    return summary


def _gap_summary(gaps: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(gaps),
        "dates": [item.get("date") for item in gaps],
        "first": gaps[0] if gaps else None,
    }


def _position_summary(run: Mapping[str, Any]) -> dict[str, Any]:
    evidence = run["position_evidence"]
    readiness = run.get("history_readiness") or {}
    return {
        "state": evidence["state"],
        "invested_sessions": evidence["invested_sessions"],
        "first_position_signal_date": evidence["first_position_signal_date"],
        "first_fill_date": evidence["first_fill_date"],
        "fills": run["metrics"]["fills"],
        "unfilled_orders": run["metrics"]["orders_unfilled"],
        "intentional_cash_sessions": list(readiness.get("ready_cash_sessions", [])),
    }


def observation_recommendation(
    run: Mapping[str, Any], minimum_opportunities: int,
) -> dict[str, Any]:
    """Recommend an observation term from the candidate replay's rebalance cadence."""
    sessions = int(run["metrics"]["sessions"])
    previous: dict[str, float] = {}
    rebalance_sessions: list[str] = []
    for signal in run.get("signals") or []:
        target = {
            symbol: float(weight)
            for symbol, weight in signal["target_weights"].items()
            if symbol in ETF_SYMBOLS and float(weight) > 1e-12
        }
        if target != previous:
            rebalance_sessions.append(signal["date"])
        previous = target
    opportunities = len(rebalance_sessions)
    recommended = math.ceil(sessions / max(1, opportunities)) * minimum_opportunities
    return {
        "recommended_sessions": max(minimum_opportunities, min(252, recommended)),
        "basis": {
            "opportunities": opportunities,
            "rebalance_sessions": rebalance_sessions,
            "sessions": sessions,
            "minimum_opportunities": minimum_opportunities,
            "formula": _OBSERVATION_FORMULA,
        },
    }


def _hard_condition(
    condition: Mapping[str, Any], metrics: Mapping[str, Any],
) -> dict[str, Any]:
    observed = metrics.get(METRIC_REGISTRY[condition["metric"]])
    if observed is None:
        status = "unavailable"
    elif OPERATORS[condition["operator"]](observed, condition["value"]):
        status = "met"
    else:
        status = "failed"
    return {**condition, "observed": observed, "status": status}


def _replay_comparison(
    replay: Mapping[str, Any],
    reference: Mapping[str, Any],
    *,
    reference_name: str,
) -> dict[str, Any]:
    mismatches: list[str] = []
    for label, run, original in (
        ("candidate", replay["candidate"], reference["candidate"]),
        ("benchmark", replay["benchmark"], reference["benchmark"]),
    ):
        if run["start_date"] != original["start_date"]:
            mismatches.append(f"{label}.start_date")
        if run["end_date"] != original["end_date"]:
            mismatches.append(f"{label}.end_date")
        if run["metrics"]["sessions"] != original["metrics"]["sessions"]:
            mismatches.append(f"{label}.sessions")
        if run["metrics"]["valued_sessions"] != original["metrics"]["valued_sessions"]:
            mismatches.append(f"{label}.valued_sessions")
        if not _fills_match(run["fills"], original["fills"]):
            mismatches.append(f"{label}.fills")
        if not _within(run["metrics"]["ending_equity"], original["metrics"]["ending_equity"]):
            mismatches.append(f"{label}.ending_equity")
    return {
        "tolerance": METRIC_TOLERANCE,
        "reference": reference_name,
        "matches_discovery": not mismatches,
        "mismatches": mismatches,
        "candidate": _run_summary(replay["candidate"], first_decision=True),
        "benchmark": _run_summary(replay["benchmark"]),
    }


def _run_record(run: Mapping[str, Any]) -> dict[str, Any]:
    """One recomputed/archival Discovery run with its metrics, fills and ending cash."""
    metrics = _metrics(run)
    curve = run["equity_curve"]
    return {
        "start_date": run["start_date"],
        "end_date": run["end_date"],
        "sessions": metrics["sessions"],
        "valued_sessions": metrics["valued_sessions"],
        "fills": list(run["fills"]),
        "fill_count": metrics["fills"],
        "ending_equity": metrics["ending_equity"],
        "ending_cash": curve[-1]["cash"] if curve else None,
        "fees_paid": metrics["fees_paid"],
        "slippage_paid": metrics["slippage_paid"],
        "orders_filled": metrics["orders_filled"],
        "orders_unfilled": metrics["orders_unfilled"],
        "signal_gaps": metrics["signal_gaps"],
        "valuation_gaps": metrics["valuation_gaps"],
        "metrics": metrics,
    }


def _discovery_record(
    snapshot_id: str,
    partition_content_hash: str,
    as_of: str | None,
    scoring_start: str,
    runs: Mapping[str, Any],
) -> dict[str, Any]:
    """Content-addressed record of one candidate+benchmark Discovery result."""
    body = {
        "snapshot_id": snapshot_id,
        "partition_content_hash": partition_content_hash,
        "as_of": as_of,
        "scoring_start": scoring_start,
        "candidate": _run_record(runs["candidate"]),
        "benchmark": _run_record(runs["benchmark"]),
    }
    body["content_hash"] = _hash(body)
    return body


def _row_key(kind: str, row: Mapping[str, Any]) -> tuple[Any, ...]:
    if kind == "calendar":
        return (row["date"],)
    if kind == "corporate_actions":
        return (row["date"], row.get("symbol"), row.get("type"))
    return (row["date"], row.get("symbol"))


def _changed_sessions(
    original_rows: list[dict], corrected_rows: list[dict], *,
    kind: str,
) -> list[str]:
    """Sessions whose row content differs between the two Discovery payloads."""
    original = {_row_key(kind, row): row for row in original_rows}
    corrected = {_row_key(kind, row): row for row in corrected_rows}
    return [
        key[0]
        for key in original.keys() | corrected.keys()
        if _json(original.get(key)) != _json(corrected.get(key))
    ]


def _fill_sessions(fills: list[dict]) -> dict[tuple[str, str, str], list[dict]]:
    grouped: dict[tuple[str, str, str], list[dict]] = {}
    for fill in fills:
        grouped.setdefault((fill["date"], fill["symbol"], fill["side"]), []).append(fill)
    return grouped


def _changed_fill_sessions(
    original_fills: list[dict], corrected_fills: list[dict],
) -> list[str]:
    original = _fill_sessions(original_fills)
    corrected = _fill_sessions(corrected_fills)
    changed = set()
    for key in original.keys() | corrected.keys():
        if not _fills_match(original.get(key, []), corrected.get(key, [])):
            changed.add(key[0])
    return sorted(changed)


def _affected(
    original: Mapping[str, Any],
    recomputed: Mapping[str, Any],
    original_data: Mapping[str, Any],
    corrected_data: Mapping[str, Any],
    scoring_sessions: list[str],
) -> dict[str, Any]:
    """Summarize where the corrected Discovery evidence moves the scored result."""
    changed_rows: dict[str, int] = {}
    changed_sessions: set[str] = set()
    for kind in ("calendar", "raw_bars", "corporate_actions", "derived"):
        changed = _changed_sessions(
            list(original_data.get(kind, [])), list(corrected_data.get(kind, [])), kind=kind
        )
        changed_rows[kind] = len(changed)
        changed_sessions.update(changed)
    original_nav = {
        point["date"]: point["nav"] for point in original["candidate"]["equity_curve"]
    }
    corrected_nav = {
        point["date"]: point["nav"] for point in recomputed["candidate"]["equity_curve"]
    }
    changed_nav_sessions = [
        day for day in scoring_sessions
        if not _within(original_nav.get(day), corrected_nav.get(day))
    ]
    changed_fill_sessions = _changed_fill_sessions(
        original["candidate"]["fills"], recomputed["candidate"]["fills"]
    )
    original_ending = original["candidate"]["metrics"]["ending_equity"]
    corrected_ending = recomputed["candidate"]["metrics"]["ending_equity"]
    changed = sum(changed_rows.values())
    summary = ", ".join(
        f"{count} {kind}" for kind, count in changed_rows.items() if count
    ) or "no rows"
    fills = "unchanged" if not changed_fill_sessions else (
        f"changed on {', '.join(changed_fill_sessions)}"
    )
    return {
        "changed_rows": changed_rows,
        "changed_sessions": sorted(changed_sessions),
        "changed_nav_sessions": changed_nav_sessions,
        "changed_fill_sessions": changed_fill_sessions,
        "fills": {
            "original": original["candidate"]["metrics"]["fills"],
            "recomputed": recomputed["candidate"]["metrics"]["fills"],
        },
        "nav_sessions": {
            "original": len(original["candidate"]["equity_curve"]),
            "recomputed": len(recomputed["candidate"]["equity_curve"]),
        },
        "ending_equity": {
            "original": original_ending,
            "recomputed": corrected_ending,
            "delta": (
                corrected_ending - original_ending
                if corrected_ending is not None and original_ending is not None
                else None
            ),
        },
        "reason": (
            f"corrected Discovery evidence changed {changed} rows ({summary}) across "
            f"{len(changed_sessions)} sessions; NAV changed on "
            f"{len(changed_nav_sessions)} of {len(scoring_sessions)} scored sessions; "
            f"fills {fills}"
        ),
    }


def _receipt(
    record: Mapping[str, Any],
    submission: Mapping[str, Any],
    *,
    validation_id: str,
    attempts: int,
    resumed: bool,
    replay: Mapping[str, Any],
    recomputed: Mapping[str, Any] | None = None,
    corrected: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    kickoff = record["kickoff"]
    expected = replay["expected_sessions"]
    scoring_start = replay["scoring_start"]
    expected_end = expected[-1] if expected else None
    evidence_run = recomputed if recomputed is not None else replay
    candidate = evidence_run["candidate"]
    benchmark = evidence_run["benchmark"]
    candidate_metrics = _metrics(candidate)
    benchmark_metrics = _metrics(benchmark)
    if recomputed is not None:
        reference = recomputed
        reference_name = "recomputed_discovery"
    else:
        reference = {
            "candidate": replay["original_candidate"],
            "benchmark": replay["original_benchmark"],
        }
        reference_name = "original_discovery"
    reference_candidate = reference["candidate"]["metrics"]
    reference_benchmark = reference["benchmark"]["metrics"]
    comparison = _replay_comparison(
        replay, reference, reference_name=reference_name
    )

    def covered(run: Mapping[str, Any]) -> bool:
        return (
            run["start_date"] == scoring_start
            and run["end_date"] == expected_end
            and run["metrics"]["sessions"] == len(expected)
            and run["metrics"]["valued_sessions"] == len(expected)
        )

    def covered_summary(run: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "start_date": run["start_date"],
            "end_date": run["end_date"],
            "sessions": run["metrics"]["sessions"],
            "valued_sessions": run["metrics"]["valued_sessions"],
        }

    readiness = candidate.get("history_readiness") or {}
    first_scored_open = readiness.get("first_scored_open")
    scoring_start_check = {
        "ok": (
            candidate["start_date"] == scoring_start
            and benchmark["start_date"] == scoring_start
            and isinstance(first_scored_open, dict)
            and first_scored_open.get("ready") is True
            and readiness.get("first_ready_date") == scoring_start
        ),
        "expected": scoring_start,
        "candidate": {
            "start_date": candidate["start_date"],
            "first_ready_date": readiness.get("first_ready_date"),
            "first_scored_open": first_scored_open,
        },
        "benchmark": {"start_date": benchmark["start_date"]},
    }
    coverage = {
        "ok": covered(candidate) and covered(benchmark),
        "expected": {
            "start": scoring_start,
            "end": expected_end,
            "sessions": len(expected),
        },
        "candidate": covered_summary(candidate),
        "benchmark": covered_summary(benchmark),
    }
    candidate_valuation = candidate["gaps"]["valuation"]
    benchmark_valuation = benchmark["gaps"]["valuation"]
    data_availability = {
        "ok": not (
            candidate["signal_gaps"]
            or candidate_valuation
            or benchmark["signal_gaps"]
            or benchmark_valuation
        ),
        "snapshot": replay["snapshot"],
        "candidate": {
            "signal_gaps": _gap_summary(candidate["signal_gaps"]),
            "valuation_gaps": _gap_summary(candidate_valuation),
        },
        "benchmark": {
            "signal_gaps": _gap_summary(benchmark["signal_gaps"]),
            "valuation_gaps": _gap_summary(benchmark_valuation),
        },
    }
    position = {
        "ok": (
            candidate["position_evidence"]["state"] in _POSITION_STATES
            and benchmark["position_evidence"]["state"] in _POSITION_STATES
        ),
        "candidate": _position_summary(candidate),
        "benchmark": _position_summary(benchmark),
    }
    costs = {
        "ok": all(
            _within(run_metrics["fees_paid"], original_metrics["fees_paid"])
            and _within(run_metrics["slippage_paid"], original_metrics["slippage_paid"])
            for run_metrics, original_metrics in (
                (candidate_metrics, reference_candidate),
                (benchmark_metrics, reference_benchmark),
            )
        ),
        "candidate": {
            "fees_paid": candidate_metrics["fees_paid"],
            "slippage_paid": candidate_metrics["slippage_paid"],
            "discovery": {
                "fees_paid": reference_candidate["fees_paid"],
                "slippage_paid": reference_candidate["slippage_paid"],
            },
        },
        "benchmark": {
            "fees_paid": benchmark_metrics["fees_paid"],
            "slippage_paid": benchmark_metrics["slippage_paid"],
            "discovery": {
                "fees_paid": reference_benchmark["fees_paid"],
                "slippage_paid": reference_benchmark["slippage_paid"],
            },
        },
    }
    hard_conditions = [
        _hard_condition(condition, candidate_metrics)
        for condition in kickoff["hard_conditions"]
    ]
    checks = {
        "data_availability": data_availability,
        "scoring_start": scoring_start_check,
        "coverage": coverage,
        "position": position,
        "costs": costs,
        "replay": {
            "ok": comparison["matches_discovery"],
            "mismatches": comparison["mismatches"],
        },
        "hard_conditions": hard_conditions,
    }
    observation = observation_recommendation(
        candidate, kickoff["observation"]["minimum_opportunities"]
    )
    observation["review_conditions"] = kickoff["observation"]["review_conditions"]
    outcome, reason = _verdict(checks, comparison)
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "round_id": record["round_id"],
        "version_id": submission["version_id"],
        "validation_id": validation_id,
        "status": "completed",
        "outcome": outcome,
        "reason": reason,
        "metrics": {"candidate": candidate_metrics, "benchmark": benchmark_metrics},
        "checks": checks,
        "observation": observation,
        "replay": comparison,
        "attempts": attempts,
        "resumed": resumed,
        "opened_protected_source": False,
        "query_debited": False,
    }
    if corrected is not None:
        receipt.update(dict(corrected))
    receipt["receipt_hash"] = _hash(receipt)
    return receipt


def _verdict(checks: Mapping[str, Any], comparison: Mapping[str, Any]) -> tuple[str, str | None]:
    if any(item["status"] == "failed" for item in checks["hard_conditions"]):
        return "failed", "HARD_CONDITION_NOT_MET"
    if not comparison["matches_discovery"]:
        return "insufficient_evidence", "REPLAY_MISMATCH"
    if not checks["scoring_start"]["ok"]:
        return "insufficient_evidence", "SCORING_START_INSUFFICIENT"
    if not checks["coverage"]["ok"] or not checks["data_availability"]["ok"]:
        return "insufficient_evidence", "COVERAGE_INSUFFICIENT"
    if not checks["position"]["ok"]:
        return "insufficient_evidence", "POSITION_EVIDENCE_INSUFFICIENT"
    if not checks["costs"]["ok"]:
        return "insufficient_evidence", "COST_EVIDENCE_INSUFFICIENT"
    if any(item["status"] == "unavailable" for item in checks["hard_conditions"]):
        return "insufficient_evidence", "METRICS_NOT_READY"
    return "passed", None


# ---------------------------------------------------------------------------
# Trusted validation copies and attempts
# ---------------------------------------------------------------------------


def _validation_path(store: Path, validation_id: str) -> Path:
    return store / VALIDATION_NAME / f"{_require_hex(validation_id, 'validation')}.json"


def _published(receipt: Mapping[str, Any]) -> dict[str, Any]:
    published = {field: receipt[field] for field in _RECEIPT_FIELDS}
    published.update(
        {field: receipt[field] for field in _OPTIONAL_RECEIPT_FIELDS if field in receipt}
    )
    return published


def _atomic_write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staged = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        staged.write_text(_json(value), encoding="utf-8")
        os.chmod(staged, 0o400)
        os.replace(staged, path)
    finally:
        if staged.exists():
            staged.unlink()


def _write_running_marker(
    store: Path,
    validation_id: str,
    record: Mapping[str, Any],
    submission: Mapping[str, Any],
    attempt: int,
) -> None:
    path = _validation_path(store, validation_id)
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("status") == "completed":
            return
    _atomic_write(
        path,
        {
            "schema_version": SCHEMA_VERSION,
            "status": "running",
            "round_id": record["round_id"],
            "version_id": submission["version_id"],
            "validation_id": validation_id,
            "attempt": attempt,
            "started_at": _now(),
        },
    )


def _write_terminal_receipt(
    store: Path, validation_id: str, receipt: Mapping[str, Any],
) -> None:
    path = _validation_path(store, validation_id)
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("status") == "completed":
            if existing != receipt:
                raise ValueError("A terminal research-loop Validation receipt cannot change.")
            return
    _atomic_write(path, receipt)


def _load_trusted_receipt(
    store: Path, validation_id: str, published_json: str,
) -> dict[str, Any]:
    path = _validation_path(store, validation_id)
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError("The trusted research-loop Validation receipt is missing.") from error
    if not isinstance(receipt, dict) or receipt.get("status") != "completed":
        raise ValueError("The trusted research-loop Validation receipt is not terminal.")
    expected = _hash({key: value for key, value in receipt.items() if key != "receipt_hash"})
    if receipt.get("receipt_hash") != expected or receipt.get("validation_id") != validation_id:
        raise ValueError("The trusted research-loop Validation receipt failed its integrity check.")
    if _json(_published(receipt)) != published_json:
        raise ValueError("The trusted Validation receipt differs from the project copy.")
    return {**receipt, "status": "completed"}


def _attempt_rows(
    connection: sqlite3.Connection, validation_id: str,
) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            "SELECT attempt, status, attempt_json FROM research_loop_validation_attempts "
            "WHERE validation_id = ? ORDER BY attempt",
            (validation_id,),
        )
    )


def _failpoint(name: str) -> None:
    if os.environ.get("PRAJNATRADE_TEST_FAILPOINT") == name:
        raise OSError(f"synthetic test failpoint {name!r} reached")


def _validation_identity(
    record: Mapping[str, Any],
    submission: Mapping[str, Any],
    campaign: Mapping[str, Any],
) -> str:
    locked = submission["locked"]
    return _hash(
        {
            "version_id": submission["version_id"],
            "scoring_policy_hash": _scoring_policy_hash(record["kickoff"]),
            "snapshot_hash": locked["snapshot_hash"],
            "protocol_hash": locked["protocol_hash"],
            "engine_runtime_identity": _current_runtime(campaign),
        }
    )


def _submission(
    connection: sqlite3.Connection,
    round_id: str,
    version_id: str,
    *,
    phase: str = "validation",
) -> dict[str, Any]:
    _require_hex(version_id, "version")
    row = connection.execute(
        "SELECT submission_json FROM research_loop_submissions "
        "WHERE round_id = ? AND version_id = ?",
        (round_id, version_id),
    ).fetchone()
    if row is None:
        raise ProtocolError(
            "SUBMISSION_NOT_FOUND", phase, "check_id",
            "Unknown locked version for this research round.",
        )
    body = json.loads(row["submission_json"])
    if body["version_id"] != version_id:
        raise ValueError("Locked version fingerprint mismatch.")
    return body


def validate(
    project: Path,
    store: Path,
    snapshot_store: Path,
    round_id: str,
    version_id: str,
) -> dict[str, Any]:
    """Replay a locked version over the frozen Discovery interval (no protected read)."""
    from prajnatrade.protected_eval import evaluation_store

    trusted = evaluation_store(store)
    if trusted.is_relative_to(project.resolve()):
        raise ValueError("Trusted evaluation store must be outside the research project.")
    with _round_lock(project, round_id):
        with _project(project) as connection:
            record = _project_round(connection, round_id)
            trusted_record = _load_round_file(_round_path(trusted, round_id), round_id)
            if trusted_record != record:
                raise ValueError("The research round copy does not match the trusted approval.")
            if _closure(connection, round_id) is not None:
                raise ProtocolError(
                    "ROUND_CLOSED", "validation", "inspect",
                    "A closed research round cannot validate another version.",
                )
            submission = _submission(connection, round_id, version_id)
            state = _review_state(connection, version_id)
            if state == "ended":
                raise ProtocolError(
                    "VERSION_ENDED", "validation", "inspect",
                    "An ended version cannot run another lightweight Validation.",
                )
            if state == "approved":
                raise ProtocolError(
                    "REVIEW_CONFLICT", "validation", "inspect",
                    "An approved version keeps its admitted receipt; use revalidate "
                    "only after the approval is ended.",
                )
            campaign = campaign_discovery(project, record["campaign_id"])
            _require_campaign_binding(record, campaign)
            superseded = connection.execute(
                "SELECT revalidation_id FROM research_loop_revalidations "
                "WHERE version_id = ? ORDER BY rowid DESC LIMIT 1",
                (version_id,),
            ).fetchone()
            if superseded is not None:
                raise ProtocolError(
                    "REVALIDATION_SUPERSEDED", "validation", "inspect",
                    "This version already has revalidated evidence; re-run round "
                    "revalidate on the current evidence source instead of replaying "
                    "the superseded locked snapshot.",
                )
            validation_id = _validation_identity(record, submission, campaign)
            terminal = connection.execute(
                "SELECT receipt_json FROM research_loop_validations WHERE validation_id = ?",
                (validation_id,),
            ).fetchone()
            if terminal is not None:
                receipt = _load_trusted_receipt(trusted, validation_id, terminal["receipt_json"])
                return {**receipt, "reused": True}
            attempts = _attempt_rows(connection, validation_id)
            resumed = bool(attempts)
            attempt = len(attempts) + 1
            for row in attempts:
                if row["status"] == "running":
                    connection.execute(
                        "UPDATE research_loop_validation_attempts SET status = 'interrupted' "
                        "WHERE validation_id = ? AND attempt = ?",
                        (validation_id, row["attempt"]),
                    )
            connection.execute(
                "INSERT INTO research_loop_validation_attempts "
                "(validation_id, attempt, status, attempt_json) VALUES (?, ?, 'running', ?)",
                (
                    validation_id,
                    attempt,
                    _json(
                        {
                            "round_id": round_id,
                            "version_id": version_id,
                            "validation_id": validation_id,
                            "attempt": attempt,
                            "started_at": _now(),
                            "engine_runtime_identity": _current_runtime(campaign),
                        }
                    ),
                ),
            )
        _write_running_marker(trusted, validation_id, record, submission, attempt)
        _failpoint(VALIDATION_FAILPOINT)
        replay = _replay(project, snapshot_store, record, submission, campaign)
        receipt = _receipt(
            record,
            submission,
            validation_id=validation_id,
            attempts=attempt,
            resumed=resumed,
            replay=replay,
        )
        # The trusted evidence is written before the research-visible copy is
        # published, so a crash between the two is recovered by re-running the
        # same identity and finding the identical terminal receipt.
        _write_terminal_receipt(trusted, validation_id, receipt)
        with _project(project) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE research_loop_validation_attempts SET status = 'completed', "
                "attempt_json = ? WHERE validation_id = ? AND attempt = ?",
                (
                    _json(
                        {
                            "round_id": round_id,
                            "version_id": version_id,
                            "validation_id": validation_id,
                            "attempt": attempt,
                            "outcome": receipt["outcome"],
                            "receipt_hash": receipt["receipt_hash"],
                            "finished_at": _now(),
                        }
                    ),
                    validation_id,
                    attempt,
                ),
            )
            connection.execute(
                "DELETE FROM research_loop_validations WHERE version_id = ?", (version_id,),
            )
            connection.execute(
                "INSERT INTO research_loop_validations "
                "(validation_id, version_id, receipt_json) VALUES (?, ?, ?)",
                (validation_id, version_id, _json(_published(receipt))),
            )
    return {**receipt, "reused": False}


# ---------------------------------------------------------------------------
# Review decisions: approve, return, end
# ---------------------------------------------------------------------------


def replay_engine_identity() -> str:
    """Fingerprint this module's own replay and metric code.

    The accounting-engine fingerprint in ``validation_id`` does not cover
    ``research_loop.py`` itself, so a fix here could otherwise silently reuse
    an old receipt.  Revalidation identities bind this value explicitly.
    """
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _review_path(store: Path, decision_id: str) -> Path:
    return store / REVIEW_NAME / f"{_require_hex(decision_id, 'review decision')}.json"


def _revalidation_path(store: Path, revalidation_id: str) -> Path:
    return (
        store
        / REVALIDATION_NAME
        / f"{_require_hex(revalidation_id, 'revalidation')}.json"
    )


def _verify_decision(body: object) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ValueError("Invalid review decision record.")
    decision_id = body.get("decision_id")
    _require_hex(decision_id, "review decision")
    expected = _hash({key: value for key, value in body.items() if key != "decision_id"})
    if decision_id != expected:
        raise ValueError("Review decision fingerprint mismatch.")
    if body.get("review_state") != REVIEW_ACTION_STATE.get(body.get("decision")):
        raise ValueError("Review decision state does not match its decision.")
    return body


def _verify_revalidation(body: object) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ValueError("Invalid revalidation record.")
    revalidation_id = body.get("revalidation_id")
    _require_hex(revalidation_id, "revalidation")
    expected = _hash(
        {key: value for key, value in body.items() if key != "revalidation_id"}
    )
    if revalidation_id != expected:
        raise ValueError("Revalidation record fingerprint mismatch.")
    return body


def _latest_review(
    connection: sqlite3.Connection, version_id: str,
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT review_json FROM research_loop_reviews WHERE version_id = ? "
        "ORDER BY rowid DESC LIMIT 1",
        (version_id,),
    ).fetchone()
    return _verify_decision(json.loads(row["review_json"])) if row is not None else None


def _review_state(connection: sqlite3.Connection, version_id: str) -> str:
    latest = _latest_review(connection, version_id)
    return latest["review_state"] if latest is not None else "pending"


def _latest_tracking(
    connection: sqlite3.Connection, version_id: str,
) -> dict[str, Any] | None:
    for row in connection.execute(
        "SELECT review_json FROM research_loop_reviews WHERE version_id = ? "
        "ORDER BY rowid DESC",
        (version_id,),
    ):
        body = _verify_decision(json.loads(row["review_json"]))
        if body["decision"] == "approve":
            return {
                **body["tracking"],
                "state": _review_state(connection, version_id),
            }
    return None


def _validated_binding(
    connection: sqlite3.Connection, version_id: str, trusted: Path,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    row = connection.execute(
        "SELECT validation_id, receipt_json FROM research_loop_validations "
        "WHERE version_id = ?",
        (version_id,),
    ).fetchone()
    if row is None:
        return None, None
    receipt = _load_trusted_receipt(trusted, row["validation_id"], row["receipt_json"])
    return (
        {
            "validation_id": row["validation_id"],
            "outcome": receipt["outcome"],
            "receipt_hash": receipt["receipt_hash"],
        },
        receipt,
    )


def _review_binding(
    connection: sqlite3.Connection,
    record: Mapping[str, Any],
    submission: Mapping[str, Any],
    trusted: Path,
) -> dict[str, Any]:
    validation, _ = _validated_binding(connection, submission["version_id"], trusted)
    return {
        "submission_id": submission["submission_id"],
        "experiment_id": submission["experiment_id"],
        "strategy_fingerprint": submission["strategy_fingerprint"],
        "experiment_hash": submission["experiment_hash"],
        "question": submission["question"],
        "revisit_condition": submission["revisit_condition"],
        "risks": list(submission["risks"]),
        "disclosed_risks": list(record["kickoff"]["disclosed_risks"]),
        "scope": record["scope"],
        "data_kind": record["data_kind"],
        "validation": validation,
    }


def _review_request_key(
    round_id: str,
    version_id: str,
    decision: str,
    reviewer: str,
    reason: str,
    payload: Mapping[str, Any],
    binding: Mapping[str, Any],
) -> str:
    return _hash(
        {
            "schema_version": SCHEMA_VERSION,
            "round_id": round_id,
            "version_id": version_id,
            "decision": decision,
            "reviewer": reviewer,
            "reason": reason,
            "payload": payload,
            "binding": binding,
        }
    )


def _approve_confirmation(
    source: object,
    kickoff: Mapping[str, Any],
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    if source is None:
        raise ProtocolError(
            "OBSERVATION_REQUIRED", "review", "correct",
            "Approval requires the reviewer's explicit observation confirmation.",
        )
    try:
        entry = _object(
            source,
            {"observation_sessions", "accepted_risks", "observation_reason"},
        )
    except ValueError as error:
        raise ProtocolError(
            "OBSERVATION_REQUIRED", "review", "correct",
            "Approval requires observation_sessions, accepted_risks and "
            "observation_reason; the platform never pre-fills a decision.",
        ) from error
    sessions = entry["observation_sessions"]
    if (
        type(sessions) is not int
        or sessions < 1
        or sessions > MAX_OBSERVATION_SESSIONS
    ):
        raise ValueError(
            f"observation_sessions must be a positive integer no greater than "
            f"{MAX_OBSERVATION_SESSIONS}."
        )
    accepted = _texts(entry["accepted_risks"], "accepted_risks")
    observation_reason = _text(entry["observation_reason"], "observation_reason")
    missing = [
        risk for risk in kickoff["disclosed_risks"] if risk not in set(accepted)
    ]
    if missing:
        raise ProtocolError(
            "RISK_NOT_ACCEPTED", "review", "correct",
            "Approval must explicitly accept every disclosed round risk; "
            f"missing: {missing}.",
        )
    recommendation = receipt.get("observation") or {}
    return {
        "observation_sessions": sessions,
        "accepted_risks": sorted(set(accepted)),
        "observation_reason": observation_reason,
        "recommended_sessions": recommendation.get("recommended_sessions"),
        "recommendation_basis": recommendation.get("basis"),
        "minimum_opportunities": kickoff["observation"]["minimum_opportunities"],
        "review_conditions": list(kickoff["observation"]["review_conditions"]),
    }


def _return_decision(source: object) -> dict[str, Any]:
    if source is None:
        raise ValueError("A return decision requires --input with the change request.")
    entry = _object(
        source,
        {"change", "comparison", "revisit_condition"},
        frozenset({"next_submission_id"}),
    )
    payload = {
        "change": _text(entry["change"], "change"),
        "comparison": _text(entry["comparison"], "comparison"),
        "revisit_condition": _text(entry["revisit_condition"], "revisit_condition"),
    }
    if "next_submission_id" in entry:
        payload["next_submission_id"] = _text(
            entry["next_submission_id"], "next_submission_id"
        )
    return payload


def _end_decision(source: object) -> dict[str, Any]:
    if source is None:
        raise ValueError("An end decision requires --input with the closing detail.")
    entry = _object(source, {"detail", "continue_direction"})
    continue_direction = entry["continue_direction"]
    if type(continue_direction) is not bool:
        raise ValueError("continue_direction must be a boolean.")
    return {
        "detail": _text(entry["detail"], "detail"),
        "continue_direction": continue_direction,
    }


def _tracking_calendar(path: Path | None, record: Mapping[str, Any]) -> dict[str, Any]:
    if path is None:
        raise ProtocolError(
            "CALENDAR_COVERAGE_MISSING", "review", "inspect",
            "Approval requires --calendar with the operator's approved trading calendar.",
        )
    try:
        calendar = shadow_calendar.load_calendar(path)
    except (OSError, UnicodeError, ValueError) as error:
        raise ProtocolError(
            "CALENDAR_COVERAGE_MISSING", "review", "inspect",
            f"The approved trading calendar is unusable: {error}",
        ) from error
    if calendar["data_kind"] != record["data_kind"]:
        raise ProtocolError(
            "CALENDAR_COVERAGE_MISSING", "review", "inspect",
            "The approved calendar data kind must match the research round.",
        )
    return calendar


def _first_signal_session(calendar: Mapping[str, Any], decided_at: str) -> str:
    local_date = datetime.fromisoformat(decided_at).astimezone(SHANGHAI).date()
    for session in calendar["sessions"]:
        if date.fromisoformat(session) > local_date:
            return session
    raise ProtocolError(
        "CALENDAR_COVERAGE_MISSING", "review", "inspect",
        "The approved trading calendar has no session after the decision date.",
    )


def _tracking_artifact(
    record: Mapping[str, Any],
    submission: Mapping[str, Any],
    receipt: Mapping[str, Any],
    body: Mapping[str, Any],
) -> dict[str, Any]:
    locked = submission["locked"]
    version_id = submission["version_id"]
    validation_id = receipt["validation_id"]
    strategy = locked["strategy"]
    effective = body["tracking"]["enabled_signal_date"]
    calendar = body["calendar"]
    artifact_body = {
        "schema_version": SCHEMA_VERSION,
        "kind": release.ARTIFACT_KIND,
        "scope": record["scope"],
        "data_kind": record["data_kind"],
        "release_version": release.RELEASE_VERSION,
        "workflow": release.TRACKING_WORKFLOW,
        "simulated_tracking": True,
        "release_id": release.tracking_id(version_id, validation_id),
        "candidate_id": submission["submission_id"],
        "candidate_hash": version_id,
        "candidate": {
            "version_id": version_id,
            "submission_id": submission["submission_id"],
            "experiment_id": submission["experiment_id"],
            "strategy_fingerprint": submission["strategy_fingerprint"],
            "experiment_hash": submission["experiment_hash"],
            "strategy": strategy,
            "parameters": strategy["parameters"],
            "evaluation": {
                "strategy_executor": locked["strategy_executor"],
                "runtime": locked["runtime"],
                "execution_rules": locked["execution_rules"],
                "history_policy": locked["history_policy"],
            },
        },
        "project_id": record["project_id"],
        "round_id": record["round_id"],
        "direction_id": record["direction_id"],
        "campaign_id": record["campaign_id"],
        "plan_id": validation_id,
        "plan_hash": validation_id,
        "plan_frozen_at": body["decided_at"],
        "snapshot_id": locked["snapshot_id"],
        "snapshot_hash": locked["snapshot_hash"],
        "partition_fingerprints": {
            "discovery": {
                "content_hash": locked["snapshot_hash"],
                "scope": record["scope"],
                "data_kind": record["data_kind"],
            }
        },
        "binding": {
            **body["binding"],
            "version_id": version_id,
            "validation_id": validation_id,
            "validation_outcome": receipt["outcome"],
            "receipt_hash": receipt["receipt_hash"],
            "round_id": record["round_id"],
            "kickoff_hash": record["kickoff_hash"],
            "snapshot_id": locked["snapshot_id"],
            "snapshot_hash": locked["snapshot_hash"],
            "evaluation_hash": locked["evaluation_hash"],
            "protocol_hash": locked["protocol_hash"],
            "scoring_policy_hash": _scoring_policy_hash(record["kickoff"]),
            "initial_cash": locked["base_costs"]["initial_cash"],
        },
        "execution": {
            "strategy": strategy,
            "strategy_family": strategy["family"],
            "parameters": strategy["parameters"],
            "initial_cash": locked["base_costs"]["initial_cash"],
            "transaction_cost_bps": locked["base_costs"]["transaction_cost_bps"],
            "slippage_bps": locked["base_costs"]["slippage_bps"],
            "base_costs": locked["base_costs"],
            "signal_window_sessions": locked["signal_window_sessions"],
            "execution_rules": locked["execution_rules"],
            "history_policy": locked["history_policy"],
            "strategy_executor": locked["strategy_executor"],
            "runtime": locked["runtime"],
        },
        "initial_cash": locked["base_costs"]["initial_cash"],
        "observation": body["observation"],
        "decision": {
            "decision_id": body["decision_id"],
            "reviewer": body["reviewer"],
            "reason": body["reason"],
            "decided_at": body["decided_at"],
        },
        "approval": {
            "reviewer": body["reviewer"],
            "decision": "approved",
            "reason": body["reason"],
            "approved_at": body["decided_at"],
            "decision_id": body["decision_id"],
        },
        "admission": {
            "decision": "approved",
            "automatic_enable": True,
            "statement": release.TRACKING_ADMISSION_STATEMENT,
            "mode": "simulated_tracking",
            "simulated_tracking": True,
            "protected_evaluation": False,
            "conditional_release": False,
            "effective_signal_date": effective,
            "calendar_hash": _hash(calendar),
            "calendar": calendar,
        },
        "tracking": {
            "workflow": release.TRACKING_WORKFLOW,
            "simulated_tracking": True,
            "release_id": release.tracking_id(version_id, validation_id),
            "enabled_signal_date": effective,
            "calendar_hash": _hash(calendar),
        },
    }
    return {**artifact_body, "artifact_hash": _hash(artifact_body)}


def _matching_review(
    connection: sqlite3.Connection,
    trusted: Path,
    version_id: str,
    request_key: str,
) -> dict[str, Any] | None:
    """Find the stored decision with this exact human content (crash recovery)."""
    for row in connection.execute(
        "SELECT review_json FROM research_loop_reviews WHERE version_id = ? "
        "ORDER BY rowid",
        (version_id,),
    ):
        body = _verify_decision(json.loads(row["review_json"]))
        if body.get("request_key") == request_key:
            return body
    directory = trusted / REVIEW_NAME
    if not directory.is_dir():
        return None
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix != ".json":
            continue
        try:
            body = _verify_decision(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeError, ValueError):
            continue
        if (
            body.get("version_id") == version_id
            and body.get("request_key") == request_key
        ):
            return body
    return None


def _store_decision(
    connection: sqlite3.Connection, trusted: Path, body: Mapping[str, Any],
) -> None:
    from prajnatrade.protected_eval import write_trusted_record_once

    path = _review_path(trusted, body["decision_id"])
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing != body:
            raise ValueError("A review decision record is immutable and cannot change.")
    else:
        write_trusted_record_once(path, dict(body))
    connection.execute(
        "INSERT OR IGNORE INTO research_loop_reviews "
        "(decision_id, version_id, review_json) VALUES (?, ?, ?)",
        (body["decision_id"], body["version_id"], _json(body)),
    )


def _store_observation(connection: sqlite3.Connection, body: Mapping[str, Any]) -> None:
    observation = body.get("observation")
    if observation is None:
        return
    connection.execute(
        "DELETE FROM research_loop_observations WHERE version_id = ?",
        (body["version_id"],),
    )
    connection.execute(
        "INSERT INTO research_loop_observations (version_id, observation_json) "
        "VALUES (?, ?)",
        (body["version_id"], _json(observation)),
    )


def _refuse_transition(state: str, decision: str) -> None:
    if state == "ended":
        raise ProtocolError(
            "VERSION_ENDED", "review", "inspect",
            "An ended version is terminal; the round may continue with a new version.",
        )
    if state == REVIEW_ACTION_STATE[decision]:
        raise ProtocolError(
            "REVIEW_CONFLICT", "review", "review",
            "A different decision already holds this version's current review state.",
        )
    if decision == "return" and state == "approved":
        raise ProtocolError(
            "REVIEW_CONFLICT", "review", "inspect",
            "An approved version can only be ended, not returned to research.",
        )
    if decision == "approve" and state not in ("pending", "returned"):
        raise ProtocolError(
            "REVIEW_CONFLICT", "review", "inspect",
            "Approval is allowed only from pending or returned.",
        )


def _validation_output(receipt: Mapping[str, Any], *, reused: bool) -> dict[str, Any]:
    return {
        "validation_id": receipt["validation_id"],
        "outcome": receipt["outcome"],
        "reason": receipt["reason"],
        "receipt_hash": receipt["receipt_hash"],
        "reused": reused,
    }


def _review_result(
    body: Mapping[str, Any],
    *,
    reused: bool,
    validation: Mapping[str, Any] | None,
) -> dict[str, Any]:
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": body["review_state"],
        "review_state": body["review_state"],
        "decision_id": body["decision_id"],
        "round_id": body["round_id"],
        "version_id": body["version_id"],
        "reviewer": body["reviewer"],
        "reason": body["reason"],
        "decided_at": body["decided_at"],
        "reused": reused,
    }
    if body["decision"] == "approve":
        result["observation"] = body["observation"]
        result["tracking"] = body["tracking"]
    elif body["decision"] == "return":
        result["return"] = body["return"]
    else:
        result["end"] = body["end"]
    if validation is not None:
        result["validation"] = dict(validation)
    return result


def review(
    project: Path,
    store: Path,
    round_id: str,
    version_id: str,
    decision: str,
    reviewer: str,
    reason: str,
    source: object = None,
    *,
    calendar: Path | None = None,
) -> dict[str, Any]:
    """Record one immutable, content-addressed human review decision."""
    from prajnatrade.protected_eval import evaluation_store

    if decision not in REVIEW_ACTION_STATE:
        raise ValueError("decision must be approve, return or end.")
    reviewer = _text(reviewer, "reviewer")
    reason = _text(reason, "reason")
    trusted = evaluation_store(store)
    if trusted.is_relative_to(project.resolve()):
        raise ValueError("Trusted evaluation store must be outside the research project.")
    with _round_lock(project, round_id), _project(project) as connection:
        record = _project_round(connection, round_id)
        trusted_record = _load_round_file(_round_path(trusted, round_id), round_id)
        if trusted_record != record:
            raise ValueError("The research round copy does not match the trusted approval.")
        submission = _submission(connection, round_id, version_id, phase="review")
        kickoff = record["kickoff"]
        binding = _review_binding(connection, record, submission, trusted)
        receipt: dict[str, Any] | None = None
        if binding["validation"] is not None:
            _, receipt = _validated_binding(connection, version_id, trusted)
        if decision == "approve":
            if receipt is None:
                state = _review_state(connection, version_id)
                if state == "ended":
                    raise ProtocolError(
                        "VERSION_ENDED", "review", "inspect",
                        "An ended version is terminal and cannot be approved.",
                    )
                if state == "approved":
                    raise ProtocolError(
                        "REVIEW_CONFLICT", "review", "inspect",
                        "An approved version has no lightweight Validation "
                        "receipt that matches this decision.",
                    )
                raise ProtocolError(
                    "VALIDATION_NOT_PASSED", "review", "new-version",
                    "Approval requires a terminal lightweight Validation receipt "
                    "for exactly this version.",
                )
            if receipt["outcome"] != "passed":
                raise ProtocolError(
                    "VALIDATION_NOT_PASSED", "review", "new-version",
                    "Approval requires a passed receipt; hard-condition failures "
                    "and insufficient evidence cannot be waived.",
                )
            confirmation = _approve_confirmation(source, kickoff, receipt)
            decided_at = _now()
            calendar_data = _tracking_calendar(calendar, record)
            effective = _first_signal_session(calendar_data, decided_at)
            request_payload = {
                "observation": confirmation,
                "calendar_hash": _hash(calendar_data),
                "effective_signal_date": effective,
            }
        elif decision == "return":
            decided_at = _now()
            request_payload = _return_decision(source)
        else:
            decided_at = _now()
            request_payload = _end_decision(source)
        request_key = _review_request_key(
            round_id, version_id, decision, reviewer, reason, request_payload, binding
        )
        existing = _matching_review(connection, trusted, version_id, request_key)
        if existing is not None:
            if existing["decision"] == "approve":
                await_receipt = receipt
                assert await_receipt is not None
                artifact = _tracking_artifact(
                    record, submission, await_receipt, existing
                )
                release.register_tracking_version(
                    trusted,
                    artifact,
                    existing["tracking"]["enabled_signal_date"],
                    existing["reason"],
                )
                _store_observation(connection, existing)
            _store_decision(connection, trusted, existing)
            return _review_result(
                existing,
                reused=True,
                validation=(
                    _validation_output(receipt, reused=True)
                    if receipt is not None
                    else None
                ),
            )
        if _closure(connection, round_id) is not None:
            raise ProtocolError(
                "ROUND_CLOSED", "review", "inspect",
                "A closed research round cannot record another review decision.",
            )
        _refuse_transition(_review_state(connection, version_id), decision)
        body: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "round_id": round_id,
            "version_id": version_id,
            "decision": decision,
            "review_state": REVIEW_ACTION_STATE[decision],
            "reviewer": reviewer,
            "reason": reason,
            "decided_at": decided_at,
            "binding": binding,
            "request_key": request_key,
        }
        if decision == "approve":
            assert receipt is not None
            body["observation"] = {
                **request_payload["observation"],
                "confirmed_at": decided_at,
            }
            body["calendar"] = calendar_data
            body["tracking"] = {
                "release_id": release.tracking_id(
                    version_id, receipt["validation_id"]
                ),
                "enabled_signal_date": request_payload["effective_signal_date"],
                "calendar_hash": request_payload["calendar_hash"],
            }
        elif decision == "return":
            body["return"] = request_payload
        else:
            body["end"] = request_payload
        body["decision_id"] = _hash(body)
        if decision == "approve":
            artifact = _tracking_artifact(record, submission, receipt, body)
            _store_decision(connection, trusted, body)
            release.register_tracking_version(
                trusted,
                artifact,
                body["tracking"]["enabled_signal_date"],
                reason,
            )
            _store_observation(connection, body)
        else:
            _store_decision(connection, trusted, body)
        return _review_result(
            body,
            reused=False,
            validation=(
                _validation_output(receipt, reused=True)
                if receipt is not None
                else None
            ),
        )


# ---------------------------------------------------------------------------
# Platform-fix revalidation
# ---------------------------------------------------------------------------


def _sealed_manifests(root: Path) -> list[dict[str, Any]]:
    """Every readable sealed manifest in a trusted snapshot store (metadata only)."""
    manifests: list[dict[str, Any]] = []
    if not root.is_dir():
        return manifests
    for path in sorted(root.iterdir()):
        name = path.name
        if (
            not path.is_dir()
            or len(name) != 64
            or any(char not in "0123456789abcdef" for char in name)
        ):
            continue
        try:
            manifests.append(snapshot.manifest(root, name))
        except (OSError, ValueError):
            continue
    return manifests


def _discovery_cutoff(
    snapshot_store: Path,
    snapshot_id: str,
    descriptor: Mapping[str, Any],
) -> str:
    """The latest point-in-time cutoff that still reproduces the sealed view.

    The corrected successor's own manifest records when each correction became
    official; using the latest of those instants (and the last Discovery
    session) applies every available revision without admitting data that was
    not yet published.
    """
    cutoff = datetime.combine(
        date.fromisoformat(descriptor["end"]),
        time(23, 59, 59, 999999),
        tzinfo=SHANGHAI,
    )
    for identifier in snapshot.lineage_ids(Path(snapshot_store), snapshot_id):
        metadata = snapshot.manifest(Path(snapshot_store), identifier)
        revision = metadata.get("discovery_dividend")
        if isinstance(revision, dict) and isinstance(
            revision.get("official_available_at"), str
        ):
            cutoff = max(cutoff, datetime.fromisoformat(revision["official_available_at"]))
    return cutoff.isoformat()


def _discovery_evidence(
    snapshot_store: Path,
    campaign: Mapping[str, Any],
    record: Mapping[str, Any],
    locked_snapshot_id: str,
) -> dict[str, Any]:
    """Find the one sealed snapshot that reproduces the frozen Discovery partition."""
    evaluation = campaign["evaluation"]
    frozen = record["kickoff"]["scoring_policy"]["frozen"]
    root = Path(snapshot_store)
    matches: dict[str, dict[str, Any]] = {}
    if not root.is_dir():
        raise ProtocolError(
            "REPLAY_MISMATCH", "revalidation", "inspect",
            "The supplied snapshot store does not exist; the original selection "
            "reason cannot be replayed.",
        )
    for path in sorted(root.iterdir()):
        name = path.name
        if (
            not path.is_dir()
            or len(name) != 64
            or any(char not in "0123456789abcdef" for char in name)
        ):
            continue
        try:
            metadata = snapshot.manifest(root, name)
            descriptor = snapshot.partition_descriptor(metadata, "discovery")
            receipt = snapshot.read_discovery(
                root, name, "discovery", evaluation.get("as_of")
            )
        except (OSError, ValueError):
            continue
        if receipt["data_kind"] != evaluation["data_kind"]:
            continue
        if (
            receipt.get("partition_content_hash", receipt["content_hash"])
            != evaluation["partition_content_hash"]
        ):
            continue
        if receipt.get("prior_history_hash") != frozen.get("prior_history_hash"):
            continue
        matches[name] = {
            "verified": True,
            "successor": False,
            "snapshot_id": name,
            "snapshot_hash": receipt["content_hash"],
            "partition_content_hash": evaluation["partition_content_hash"],
            "lineage": [name],
            "schema_version": metadata["schema_version"],
            "data_kind": receipt["data_kind"],
            "prior_history_hash": receipt.get("prior_history_hash"),
            "as_of": evaluation.get("as_of"),
            "partition": {
                "start": descriptor["start"],
                "end": descriptor["end"],
                "sessions": descriptor["sessions"],
            },
            "discovery_changed": False,
        }
    if not matches:
        raise ProtocolError(
            "REPLAY_MISMATCH", "revalidation", "inspect",
            "The supplied snapshot store cannot reproduce the frozen Discovery "
            "partition; the original selection reason no longer holds.",
        )
    if len(matches) == 1:
        return next(iter(matches.values()))
    if locked_snapshot_id in matches:
        candidates = [
            item for name, item in matches.items() if name != locked_snapshot_id
        ]
        if len(candidates) == 1:
            return candidates[0]
    raise ValueError(
        "The snapshot store has more than one snapshot matching the frozen "
        "Discovery partition; keep exactly the corrected snapshot."
    )


def _successor_evidence(
    snapshot_store: Path,
    campaign: Mapping[str, Any],
    record: Mapping[str, Any],
    locked_snapshot_id: str,
    snapshot_id: str,
) -> dict[str, Any]:
    """Verify one named successor snapshot and return its corrected evidence."""
    evaluation = campaign["evaluation"]
    frozen = record["kickoff"]["scoring_policy"]["frozen"]
    root = Path(snapshot_store)
    if (
        not isinstance(snapshot_id, str)
        or len(snapshot_id) != 64
        or any(char not in "0123456789abcdef" for char in snapshot_id)
    ):
        raise ProtocolError(
            "EVIDENCE_SNAPSHOT_NOT_FOUND", "revalidation", "check_id",
            "The corrected evidence snapshot ID must be 64 lowercase hex characters.",
        )
    try:
        metadata = snapshot.manifest(root, snapshot_id)
        lineage = snapshot.lineage_ids(root, snapshot_id)
    except (OSError, ValueError) as error:
        raise ProtocolError(
            "EVIDENCE_SNAPSHOT_NOT_FOUND", "revalidation", "check_id",
            "The corrected evidence snapshot is not sealed in the supplied store.",
        ) from error
    if locked_snapshot_id not in lineage[1:]:
        raise ProtocolError(
            "EVIDENCE_NOT_SUCCESSOR", "revalidation", "check_id",
            "The corrected evidence snapshot is not a successor of the locked snapshot.",
        )
    try:
        previous = snapshot.manifest(root, locked_snapshot_id)
        descriptor = snapshot.partition_descriptor(metadata, "discovery")
        original_descriptor = snapshot.partition_descriptor(previous, "discovery")
    except (OSError, ValueError) as error:
        raise ProtocolError(
            "EVIDENCE_SNAPSHOT_NOT_FOUND", "revalidation", "inspect",
            "The corrected evidence snapshot manifest is incomplete.",
        ) from error
    if (
        metadata["data_kind"] != evaluation["data_kind"]
        or metadata["data_kind"] != previous["data_kind"]
        or metadata["schema_version"] != previous["schema_version"]
    ):
        raise ProtocolError(
            "EVIDENCE_DATA_KIND_MISMATCH", "revalidation", "inspect",
            "The corrected evidence snapshot does not keep the frozen data kind "
            "and schema version.",
        )
    interval = (descriptor["start"], descriptor["end"], descriptor["sessions"])
    frozen_interval = (
        original_descriptor["start"],
        original_descriptor["end"],
        original_descriptor["sessions"],
    )
    if interval != frozen_interval:
        raise ProtocolError(
            "EVIDENCE_INTERVAL_MISMATCH", "revalidation", "inspect",
            "The corrected evidence snapshot changed the frozen Discovery interval.",
        )
    try:
        cutoff = evaluation.get("as_of") or _discovery_cutoff(
            root, snapshot_id, descriptor
        )
        receipt = snapshot.read_discovery(root, snapshot_id, "discovery", cutoff)
    except (OSError, ValueError) as error:
        raise ProtocolError(
            "EVIDENCE_INTEGRITY_VIOLATION", "revalidation", "inspect",
            "The corrected evidence snapshot failed its sealed manifest or "
            "partition integrity check.",
        ) from error
    if (
        receipt.get("partition_content_hash") != descriptor["content_hash"]
        or receipt["content_hash"] != descriptor["content_hash"]
    ):
        raise ProtocolError(
            "EVIDENCE_AS_OF_VIOLATION", "revalidation", "inspect",
            "The corrected evidence payload only became available after the "
            "point-in-time cutoff, so the sealed Discovery partition cannot be "
            "recomputed honestly.",
        )
    if receipt["data_kind"] != evaluation["data_kind"]:
        raise ProtocolError(
            "EVIDENCE_DATA_KIND_MISMATCH", "revalidation", "inspect",
            "The corrected evidence snapshot data kind differs from the Campaign.",
        )
    if receipt.get("prior_history_hash") != frozen.get("prior_history_hash"):
        raise ProtocolError(
            "EVIDENCE_PRIOR_HISTORY_MISMATCH", "revalidation", "inspect",
            "The corrected evidence snapshot changed the frozen Discovery prior history.",
        )
    sessions = sorted({row["date"] for row in receipt["data"]["calendar"]})
    if frozen["scoring_start"] not in sessions:
        raise ProtocolError(
            "EVIDENCE_INTERVAL_MISMATCH", "revalidation", "inspect",
            "The frozen scoring start is not a trading session in the corrected evidence.",
        )
    return {
        "verified": True,
        "successor": True,
        "snapshot_id": snapshot_id,
        "snapshot_hash": receipt["content_hash"],
        "partition_content_hash": descriptor["content_hash"],
        "lineage": list(lineage),
        "schema_version": metadata["schema_version"],
        "data_kind": receipt["data_kind"],
        "prior_history_hash": receipt.get("prior_history_hash"),
        "as_of": cutoff,
        "partition": {
            "start": descriptor["start"],
            "end": descriptor["end"],
            "sessions": descriptor["sessions"],
        },
        "discovery_changed": (
            descriptor["content_hash"] != evaluation["partition_content_hash"]
        ),
    }


def _successor_candidates(
    snapshot_store: Path,
    campaign: Mapping[str, Any],
    record: Mapping[str, Any],
    locked_snapshot_id: str,
) -> list[dict[str, Any]]:
    """Every valid sealed successor of the locked snapshot in one store."""
    root = Path(snapshot_store)
    candidates: list[dict[str, Any]] = []
    for metadata in _sealed_manifests(root):
        snapshot_id = metadata["snapshot_id"]
        if snapshot_id == locked_snapshot_id or not isinstance(
            metadata.get("lineage"), dict
        ):
            continue
        try:
            lineage = snapshot.lineage_ids(root, snapshot_id)
        except (OSError, ValueError):
            continue
        if locked_snapshot_id not in lineage[1:]:
            continue
        try:
            candidates.append(
                _successor_evidence(
                    snapshot_store, campaign, record, locked_snapshot_id, snapshot_id
                )
            )
        except ProtocolError:
            continue
    return candidates


def _corrected_evidence(
    snapshot_store: Path,
    campaign: Mapping[str, Any],
    record: Mapping[str, Any],
    locked_snapshot_id: str,
    evidence_snapshot_id: str | None,
) -> dict[str, Any] | None:
    """Resolve the corrected successor evidence; None means the platform-fix path."""
    if evidence_snapshot_id is not None:
        return _successor_evidence(
            snapshot_store, campaign, record, locked_snapshot_id, evidence_snapshot_id
        )
    candidates = _successor_candidates(
        snapshot_store, campaign, record, locked_snapshot_id
    )
    if len(candidates) > 1:
        raise ProtocolError(
            "EVIDENCE_SNAPSHOT_AMBIGUOUS", "revalidation", "check_id",
            "The snapshot store holds more than one successor of the locked "
            "snapshot; pass --evidence-snapshot-id to select the corrected evidence.",
        )
    return candidates[0] if candidates else None


def _revalidation_identity(
    record: Mapping[str, Any],
    submission: Mapping[str, Any],
    campaign: Mapping[str, Any],
    evidence: Mapping[str, Any],
    prior_validation_id: str,
    reason: str,
) -> str:
    return _hash(
        {
            "version_id": submission["version_id"],
            "scoring_policy_hash": _scoring_policy_hash(record["kickoff"]),
            "evidence_snapshot_id": evidence["snapshot_id"],
            "snapshot_hash": evidence["snapshot_hash"],
            "evidence_as_of": evidence.get("as_of"),
            "protocol_hash": submission["locked"]["protocol_hash"],
            "engine_runtime_identity": _current_runtime(campaign),
            "replay_engine_identity": replay_engine_identity(),
            "prior_validation_id": prior_validation_id,
            "reason": reason,
        }
    )


def _revalidation_body(
    record: Mapping[str, Any],
    submission: Mapping[str, Any],
    prior: Mapping[str, Any],
    receipt: Mapping[str, Any],
    *,
    reason: str,
    review_state: str,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    body = {
        "schema_version": SCHEMA_VERSION,
        "round_id": record["round_id"],
        "version_id": submission["version_id"],
        "review_state": review_state,
        "strategy_fingerprint": submission["strategy_fingerprint"],
        "prior_validation_id": prior["validation_id"],
        "prior_receipt_hash": prior["receipt_hash"],
        "prior_outcome": prior["outcome"],
        "prior_receipt": _published(prior),
        "validation_id": receipt["validation_id"],
        "receipt_hash": receipt["receipt_hash"],
        "outcome": receipt["outcome"],
        "validation_reason": receipt["reason"],
        "reason": reason,
        "evidence": dict(evidence),
        "revalidated_at": _now(),
        "opened_protected_source": False,
        "query_debited": False,
    }
    for field in ("original_discovery", "recomputed_discovery", "affected"):
        if field in receipt:
            body[field] = receipt[field]
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": "revalidated",
        "round_id": record["round_id"],
        "version_id": submission["version_id"],
        "review_state": review_state,
        "strategy_fingerprint": submission["strategy_fingerprint"],
        "previous_validation_id": prior["validation_id"],
        "previous_receipt_hash": prior["receipt_hash"],
        "previous_outcome": prior["outcome"],
        "validation_id": receipt["validation_id"],
        "receipt_hash": receipt["receipt_hash"],
        "outcome": receipt["outcome"],
        "reason": receipt["reason"],
        "evidence": dict(evidence),
    }
    for field in ("original_discovery", "recomputed_discovery", "affected"):
        if field in receipt:
            result[field] = receipt[field]
    body["result"] = result
    body["revalidation_id"] = _hash(body)
    return body


def revalidate(
    project: Path,
    store: Path,
    snapshot_store: Path,
    round_id: str,
    version_id: str,
    reason: str,
    evidence_snapshot_id: str | None = None,
) -> dict[str, Any]:
    """Recompute lightweight Validation after the evidence source or platform changed.

    With ``evidence_snapshot_id`` (or exactly one discoverable successor) the
    corrected Discovery values are recomputed and recorded as new evidence; the
    locked strategy identity and every earlier receipt stay untouched.  Without
    a successor the platform-fix path reproduces the frozen partition.
    """
    from prajnatrade.protected_eval import evaluation_store, write_trusted_record_once

    reason = _text(reason, "reason")
    trusted = evaluation_store(store)
    if trusted.is_relative_to(project.resolve()):
        raise ValueError("Trusted evaluation store must be outside the research project.")
    with _round_lock(project, round_id):
        with _project(project) as connection:
            record = _project_round(connection, round_id)
            trusted_record = _load_round_file(_round_path(trusted, round_id), round_id)
            if trusted_record != record:
                raise ValueError(
                    "The research round copy does not match the trusted approval."
                )
            if _closure(connection, round_id) is not None:
                raise ProtocolError(
                    "ROUND_CLOSED", "revalidation", "inspect",
                    "A closed research round cannot revalidate another version.",
                )
            submission = _submission(
                connection, round_id, version_id, phase="revalidation"
            )
            state = _review_state(connection, version_id)
            if state == "ended":
                raise ProtocolError(
                    "VERSION_ENDED", "revalidation", "inspect",
                    "An ended version is terminal and cannot be revalidated.",
                )
            if state == "approved":
                raise ProtocolError(
                    "REVIEW_CONFLICT", "revalidation", "inspect",
                    "An approved version keeps its admitted receipt; end it before "
                    "revalidating a fresh review.",
                )
            row = connection.execute(
                "SELECT validation_id, receipt_json FROM research_loop_validations "
                "WHERE version_id = ?",
                (version_id,),
            ).fetchone()
            if row is None:
                raise ProtocolError(
                    "VALIDATION_NOT_PASSED", "revalidation", "correct",
                    "Revalidation requires a completed lightweight Validation "
                    "receipt to record as historical evidence.",
                )
            prior = _load_trusted_receipt(
                trusted, row["validation_id"], row["receipt_json"]
            )
            campaign = campaign_discovery(project, record["campaign_id"])
            locked_snapshot_id = submission["locked"]["snapshot_id"]
            corrected_evidence = _corrected_evidence(
                snapshot_store,
                campaign,
                record,
                locked_snapshot_id,
                evidence_snapshot_id,
            )
            corrected = corrected_evidence is not None
            evidence = (
                corrected_evidence
                if corrected
                else _discovery_evidence(
                    snapshot_store, campaign, record, locked_snapshot_id
                )
            )
            runtime = _current_runtime(campaign)
            evidence["previous_snapshot_id"] = submission["locked"]["snapshot_id"]
            evidence["snapshot_changed"] = (
                evidence["snapshot_id"] != submission["locked"]["snapshot_id"]
            )
            evidence["runtime_changed"] = runtime != campaign["evaluation"].get(
                "runtime"
            )
            evidence["engine_runtime_identity"] = runtime
            evidence["replay_engine_identity"] = replay_engine_identity()
            # A repeated revalidation must key the same evidence epoch, so the
            # identity uses the base (non-revalidated) receipt as its anchor.
            base_validation_id = prior["validation_id"]
            previous_revalidation = connection.execute(
                "SELECT revalidation_json FROM research_loop_revalidations "
                "WHERE validation_id = ?",
                (prior["validation_id"],),
            ).fetchone()
            if previous_revalidation is not None:
                base_validation_id = json.loads(
                    previous_revalidation["revalidation_json"]
                )["prior_validation_id"]
            validation_id = _revalidation_identity(
                record, submission, campaign, evidence, base_validation_id, reason
            )
            stored = connection.execute(
                "SELECT revalidation_json FROM research_loop_revalidations "
                "WHERE validation_id = ?",
                (validation_id,),
            ).fetchone()
            if stored is not None:
                body = _verify_revalidation(json.loads(stored["revalidation_json"]))
                return {
                    **body["result"],
                    "revalidation_id": body["revalidation_id"],
                    "reused": True,
                }
            attempts = _attempt_rows(connection, validation_id)
            resumed = bool(attempts)
            attempt = len(attempts) + 1
            for item in attempts:
                if item["status"] == "running":
                    connection.execute(
                        "UPDATE research_loop_validation_attempts "
                        "SET status = 'interrupted' WHERE validation_id = ? AND attempt = ?",
                        (validation_id, item["attempt"]),
                    )
            connection.execute(
                "INSERT INTO research_loop_validation_attempts "
                "(validation_id, attempt, status, attempt_json) "
                "VALUES (?, ?, 'running', ?)",
                (
                    validation_id,
                    attempt,
                    _json(
                        {
                            "round_id": round_id,
                            "version_id": version_id,
                            "validation_id": validation_id,
                            "attempt": attempt,
                            "revalidation": True,
                            "prior_validation_id": prior["validation_id"],
                            "reason": reason,
                            "evidence_snapshot_id": evidence["snapshot_id"],
                            "corrected_evidence": corrected,
                            "replay_engine_identity": evidence["replay_engine_identity"],
                            "started_at": _now(),
                        }
                    ),
                ),
            )
            attempt_context = (validation_id, attempt)
        _write_running_marker(
            trusted, attempt_context[0], record, submission, attempt_context[1]
        )
        _failpoint(VALIDATION_FAILPOINT)
        if corrected:
            recomputed = _evidence_runs(
                snapshot_store, record, submission, campaign, evidence
            )
            replay = _evidence_runs(
                snapshot_store, record, submission, campaign, evidence
            )
            original = _experiment_result(
                project, record["campaign_id"], submission["experiment_id"]
            )["discovery"]
            original_runs = {
                "candidate": original,
                "benchmark": original["baseline_comparison"]["baseline"],
            }
            original_view = snapshot.read_discovery(
                Path(snapshot_store),
                evidence["previous_snapshot_id"],
                "discovery",
                campaign["evaluation"].get("as_of"),
            )
            receipt = _receipt(
                record,
                submission,
                validation_id=attempt_context[0],
                attempts=attempt_context[1],
                resumed=resumed,
                replay=replay,
                recomputed=recomputed,
                corrected={
                    "evidence": dict(evidence),
                    "prior_validation_id": prior["validation_id"],
                    "original_discovery": _discovery_record(
                        evidence["previous_snapshot_id"],
                        record["kickoff"]["frozen_campaign"]["partition_content_hash"],
                        campaign["evaluation"].get("as_of"),
                        replay["scoring_start"],
                        original_runs,
                    ),
                    "recomputed_discovery": _discovery_record(
                        evidence["snapshot_id"],
                        evidence["partition_content_hash"],
                        evidence["as_of"],
                        replay["scoring_start"],
                        recomputed,
                    ),
                    "affected": _affected(
                        original_runs,
                        recomputed,
                        original_view["data"],
                        recomputed["data"],
                        replay["expected_sessions"],
                    ),
                },
            )
        else:
            replay = _replay(
                project, snapshot_store, record, submission, campaign, evidence
            )
            receipt = _receipt(
                record,
                submission,
                validation_id=attempt_context[0],
                attempts=attempt_context[1],
                resumed=resumed,
                replay=replay,
            )
        _write_terminal_receipt(trusted, attempt_context[0], receipt)
        body = _revalidation_body(
            record,
            submission,
            prior,
            receipt,
            reason=reason,
            review_state=state,
            evidence=evidence,
        )
        path = _revalidation_path(trusted, body["revalidation_id"])
        if path.is_file():
            existing = _verify_revalidation(
                json.loads(path.read_text(encoding="utf-8"))
            )
            if existing != body:
                raise ValueError("A revalidation record is immutable and cannot change.")
        else:
            write_trusted_record_once(path, body)
        with _project(project) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE research_loop_validation_attempts SET status = 'completed', "
                "attempt_json = ? WHERE validation_id = ? AND attempt = ?",
                (
                    _json(
                        {
                            "round_id": round_id,
                            "version_id": version_id,
                            "validation_id": receipt["validation_id"],
                            "attempt": attempt_context[1],
                            "revalidation": True,
                            "outcome": receipt["outcome"],
                            "receipt_hash": receipt["receipt_hash"],
                            "finished_at": _now(),
                        }
                    ),
                    attempt_context[0],
                    attempt_context[1],
                ),
            )
            connection.execute(
                "DELETE FROM research_loop_validations WHERE version_id = ?",
                (version_id,),
            )
            connection.execute(
                "INSERT INTO research_loop_validations "
                "(validation_id, version_id, receipt_json) VALUES (?, ?, ?)",
                (
                    validation_id,
                    version_id,
                    _json(_published(receipt)),
                ),
            )
            connection.execute(
                "INSERT INTO research_loop_revalidations "
                "(revalidation_id, version_id, validation_id, "
                "prior_validation_id, revalidation_json) VALUES (?, ?, ?, ?, ?)",
                (
                    body["revalidation_id"],
                    version_id,
                    validation_id,
                    prior["validation_id"],
                    _json(body),
                ),
            )
    return {
        **body["result"],
        "revalidation_id": body["revalidation_id"],
        "reused": False,
    }


# ---------------------------------------------------------------------------
# round status
# ---------------------------------------------------------------------------


def _validation_summary(
    connection: sqlite3.Connection, version_id: str,
) -> dict[str, Any]:
    terminal = connection.execute(
        "SELECT validation_id, receipt_json FROM research_loop_validations "
        "WHERE version_id = ?",
        (version_id,),
    ).fetchone()
    if terminal is not None:
        receipt = json.loads(terminal["receipt_json"])
        attempts = connection.execute(
            "SELECT count(*) FROM research_loop_validation_attempts WHERE validation_id = ?",
            (terminal["validation_id"],),
        ).fetchone()[0]
        summary = {
            "validation_id": terminal["validation_id"],
            "status": "completed",
            "outcome": receipt["outcome"],
            "reason": receipt["reason"],
            "observation": receipt.get("observation"),
            "attempts": attempts,
        }
        revalidated = connection.execute(
            "SELECT revalidation_id, prior_validation_id FROM "
            "research_loop_revalidations WHERE validation_id = ?",
            (terminal["validation_id"],),
        ).fetchone()
        if revalidated is not None:
            summary["revalidation_id"] = revalidated["revalidation_id"]
            summary["revalidated_from"] = revalidated["prior_validation_id"]
        for field in (
            "evidence", "original_discovery", "recomputed_discovery", "affected",
        ):
            if field in receipt:
                summary[field] = receipt[field]
        history: list[dict[str, Any]] = []
        for row in connection.execute(
            "SELECT revalidation_json FROM research_loop_revalidations "
            "WHERE version_id = ? ORDER BY rowid",
            (version_id,),
        ):
            body = _verify_revalidation(json.loads(row["revalidation_json"]))
            prior_receipt = body["prior_receipt"]
            if not history or history[-1]["validation_id"] != prior_receipt["validation_id"]:
                history.append(
                    {
                        "validation_id": prior_receipt["validation_id"],
                        "outcome": prior_receipt["outcome"],
                        "receipt_hash": prior_receipt["receipt_hash"],
                    }
                )
        if history:
            history.append(
                {
                    "validation_id": terminal["validation_id"],
                    "outcome": receipt["outcome"],
                    "receipt_hash": receipt["receipt_hash"],
                }
            )
            summary["history"] = history
        return summary
    running: dict[str, Any] | None = None
    for row in connection.execute(
        "SELECT validation_id, attempt, status, attempt_json FROM "
        "research_loop_validation_attempts ORDER BY rowid"
    ):
        detail = json.loads(row["attempt_json"])
        if detail.get("version_id") == version_id:
            running = {"validation_id": row["validation_id"], "attempts": 0, "status": "running"}
    if running is None:
        return {"status": "not_run", "attempts": 0}
    running["attempts"] = connection.execute(
        "SELECT count(*) FROM research_loop_validation_attempts WHERE validation_id = ?",
        (running["validation_id"],),
    ).fetchone()[0]
    return running


def _review_summary(
    connection: sqlite3.Connection, version_id: str,
) -> dict[str, Any]:
    latest = _latest_review(connection, version_id)
    if latest is None:
        return {
            "state": "pending",
            "decision": None,
            "decision_id": None,
            "reviewer": None,
            "reason": None,
            "decided_at": None,
            "summary": None,
        }
    if latest["decision"] == "approve":
        summary: dict[str, Any] | None = {
            "observation_sessions": latest["observation"]["observation_sessions"],
            "accepted_risks": latest["observation"]["accepted_risks"],
            "observation_reason": latest["observation"]["observation_reason"],
            "tracking": latest["tracking"],
        }
    elif latest["decision"] == "return":
        summary = latest["return"]
    else:
        summary = latest["end"]
    return {
        "state": latest["review_state"],
        "decision": latest["decision"],
        "decision_id": latest["decision_id"],
        "reviewer": latest["reviewer"],
        "reason": latest["reason"],
        "decided_at": latest["decided_at"],
        "summary": summary,
    }


def _version_observation(
    connection: sqlite3.Connection, version_id: str,
) -> dict[str, Any]:
    validated = _validation_summary(connection, version_id)
    recommended = validated.get("observation")
    row = connection.execute(
        "SELECT observation_json FROM research_loop_observations WHERE version_id = ?",
        (version_id,),
    ).fetchone()
    confirmed = json.loads(row["observation_json"]) if row is not None else None
    return {"recommended": recommended, "confirmed": confirmed}


def _round_observation(
    connection: sqlite3.Connection, round_id: str,
) -> dict[str, Any]:
    recommended = None
    row = connection.execute(
        "SELECT v.receipt_json FROM research_loop_validations AS v "
        "JOIN research_loop_submissions AS s USING (version_id) "
        "WHERE s.round_id = ? ORDER BY v.rowid DESC LIMIT 1",
        (round_id,),
    ).fetchone()
    if row is not None:
        recommended = json.loads(row["receipt_json"]).get("observation")
    confirmed = None
    row = connection.execute(
        "SELECT o.observation_json FROM research_loop_observations AS o "
        "JOIN research_loop_submissions AS s USING (version_id) "
        "WHERE s.round_id = ? ORDER BY o.rowid DESC LIMIT 1",
        (round_id,),
    ).fetchone()
    if row is not None:
        confirmed = json.loads(row["observation_json"])
    return {"recommended": recommended, "confirmed": confirmed}


def _round_status_body(
    connection: sqlite3.Connection, round_id: str,
) -> dict[str, Any]:
    record = _project_round(connection, round_id)
    kickoff = record["kickoff"]
    submissions = []
    for row in connection.execute(
        "SELECT submission_json FROM research_loop_submissions "
        "WHERE round_id = ? ORDER BY rowid",
        (round_id,),
    ):
        body = json.loads(row["submission_json"])
        version_id = body["version_id"]
        review_state = _review_state(connection, version_id)
        submissions.append(
            {
                "version_id": version_id,
                "submission_id": body["submission_id"],
                "experiment_id": body["experiment_id"],
                "change": body["change"],
                "question": body["question"],
                "expected_change": body["expected_change"],
                "revisit_condition": body["revisit_condition"],
                "risks": body["risks"],
                "related_experiment_ids": body["related_experiment_ids"],
                "strategy_fingerprint": body["strategy_fingerprint"],
                "validation": _validation_summary(connection, version_id),
                "review_state": review_state,
                "review": _review_summary(connection, version_id),
                "observation": _version_observation(connection, version_id),
                "tracking": _latest_tracking(connection, version_id),
            }
        )
    tracked = [
        {
            "version_id": item["version_id"],
            "release_id": item["tracking"]["release_id"],
            "enabled_signal_date": item["tracking"]["enabled_signal_date"],
            "state": item["tracking"]["state"],
        }
        for item in submissions
        if item["tracking"] is not None
    ]
    closure = _closure(connection, round_id)
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": "closed" if closure else "open",
        "round_id": round_id,
        "direction_id": record["direction_id"],
        "project_id": record["project_id"],
        "campaign_id": record["campaign_id"],
        "scope": record["scope"],
        "data_kind": record["data_kind"],
        "kickoff": {
            field: kickoff[field]
            for field in (
                "objective", "constraints", "comparison", "selection_rules",
                "hard_conditions", "disclosed_risks", "scoring_policy", "observation",
                "invalidation", "reviewer", "approval_reason",
            )
        },
        "budget": dict(kickoff["budget"]),
        "submissions": submissions,
        "observation": _round_observation(connection, round_id),
        "tracking": {
            "status": (
                "tracking"
                if any(item["state"] == "approved" for item in tracked)
                else ("ended" if tracked else "not_tracking")
            ),
            "release_ids": [item["release_id"] for item in tracked],
            "versions": tracked,
            "committed_sessions": 0,
        },
    }
    if closure is not None:
        result["closure"] = closure
    return result


def round_status(project: Path, round_id: str | None = None) -> dict[str, Any]:
    """Report research-visible round progress from the project database only."""
    project_id = project_status(project)["project_id"]
    with _project(project) as connection:
        if round_id is None:
            identifiers = [
                row["round_id"]
                for row in connection.execute(
                    "SELECT round_id FROM research_loop_rounds ORDER BY rowid"
                )
            ]
            return {
                "schema_version": SCHEMA_VERSION,
                "status": "ok",
                "project_id": project_id,
                "rounds": [_round_status_body(connection, item) for item in identifiers],
            }
        return _round_status_body(connection, round_id)


# ---------------------------------------------------------------------------
# round evidence: opportunities, scoped pauses and review decisions (T3)
# ---------------------------------------------------------------------------


def _evidence_path(store: Path, decision_id: str) -> Path:
    return store / EVIDENCE_NAME / f"{decision_id}.json"


def _verify_evidence_decision(body: object) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ValueError("An evidence decision record must be an object.")
    expected = _hash({key: value for key, value in body.items() if key != "decision_id"})
    if body.get("decision_id") != expected:
        raise ValueError("The evidence decision record fingerprint mismatch.")
    return body


def _latest_evidence_decision(
    connection: sqlite3.Connection, version_id: str,
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT decision_json FROM research_loop_evidence_decisions "
        "WHERE version_id = ? ORDER BY rowid DESC LIMIT 1",
        (version_id,),
    ).fetchone()
    return _verify_evidence_decision(json.loads(row["decision_json"])) if row else None


def _matching_evidence_decision(
    connection: sqlite3.Connection,
    trusted: Path,
    version_id: str,
    request_key: str,
) -> dict[str, Any] | None:
    """Find the stored decision with this exact human content (crash recovery)."""
    for row in connection.execute(
        "SELECT decision_json FROM research_loop_evidence_decisions "
        "WHERE version_id = ? ORDER BY rowid",
        (version_id,),
    ):
        body = _verify_evidence_decision(json.loads(row["decision_json"]))
        if body.get("request_key") == request_key:
            return body
    directory = trusted / EVIDENCE_NAME
    if not directory.is_dir():
        return None
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix != ".json":
            continue
        try:
            body = _verify_evidence_decision(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except (OSError, UnicodeError, ValueError):
            continue
        if (
            body.get("version_id") == version_id
            and body.get("request_key") == request_key
        ):
            return body
    return None


def _store_evidence_decision(
    connection: sqlite3.Connection, trusted: Path, body: Mapping[str, Any],
) -> None:
    from prajnatrade.protected_eval import write_trusted_record_once

    path = _evidence_path(trusted, body["decision_id"])
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing != body:
            raise ValueError(
                "An evidence decision record is immutable and cannot change."
            )
    else:
        write_trusted_record_once(path, dict(body))
    connection.execute(
        "INSERT OR IGNORE INTO research_loop_evidence_decisions "
        "(decision_id, version_id, decision_json) VALUES (?, ?, ?)",
        (body["decision_id"], body["version_id"], _json(body)),
    )


def _confirmed_observation(
    connection: sqlite3.Connection, version_id: str,
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT observation_json FROM research_loop_observations WHERE version_id = ?",
        (version_id,),
    ).fetchone()
    return json.loads(row["observation_json"]) if row is not None else None


def _evidence_request_key(
    round_id: str,
    version_id: str,
    release_id: str,
    decision: str,
    scope: str,
    reviewer: str,
    reason: str,
) -> str:
    return _hash(
        {
            "schema_version": SCHEMA_VERSION,
            "round_id": round_id,
            "version_id": version_id,
            "release_id": release_id,
            "decision": decision,
            "scope": scope,
            "reviewer": reviewer,
            "reason": reason,
        }
    )


def _evidence_tracking_release(
    trusted: Path, release_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fetch one manual-tracking artifact and its timeline state (never a plan)."""
    try:
        artifact = release.fetch(trusted, release_id)["artifact"]
        status = release.status(trusted, release_id)
    except release.ReleaseRejection as error:
        raise ProtocolError(
            error.details["code"], "evidence", error.details["next_action"],
            error.details["message"],
        ) from error
    if artifact.get("workflow") != release.TRACKING_WORKFLOW:
        raise ProtocolError(
            "REVIEW_CONFLICT", "evidence", "inspect",
            "Only a manual simulated-tracking version can record an evidence decision.",
        )
    return artifact, status


def _next_calendar_session(sessions: list[str], after: str) -> str:
    later = [session for session in sessions if session > after]
    if later:
        return later[0]
    return (date.fromisoformat(after) + timedelta(days=1)).isoformat()


def _evidence_effective_session(
    artifact: Mapping[str, Any],
    evidence: Mapping[str, Any],
    status: Mapping[str, Any],
) -> str:
    """The first uncommitted after-close session the decision applies to."""
    admission = artifact["admission"]
    sessions = list(admission["calendar"]["sessions"])
    progress = evidence.get("progress") or {}
    last = progress.get("last_committed_date")
    if isinstance(last, str) and last:
        effective = _next_calendar_session(sessions, last)
    else:
        enabled = admission["effective_signal_date"]
        later = [session for session in sessions if session >= enabled]
        effective = later[0] if later else enabled
    timeline = status.get("timeline") or []
    if timeline and effective < timeline[-1]["effective_signal_date"]:
        effective = timeline[-1]["effective_signal_date"]
    return effective


def _evidence_review(
    kickoff: Mapping[str, Any],
    evidence: Mapping[str, Any],
    confirmed: Mapping[str, Any] | None,
    committed_sessions: int,
) -> dict[str, Any]:
    """Evaluate the round's invalidation policy and confirmed observation term."""
    policy = kickoff.get("invalidation") or {}
    term = None
    minimum = None
    if confirmed is not None:
        raw_term = confirmed.get("observation_sessions")
        raw_minimum = confirmed.get("minimum_opportunities")
        if type(raw_term) is int:
            term = raw_term
        if type(raw_minimum) is int:
            minimum = raw_minimum
    term_reached = term is not None and committed_sessions >= term
    enough = minimum is not None and evidence["due_opportunities"] >= minimum
    trigger: str | None = None
    reason: str | None = None
    maximum_unfilled = policy.get("max_unfilled_opportunities")
    if type(maximum_unfilled) is int and maximum_unfilled > 0:
        consecutive = evidence["consecutive_intent_without_fill"]
        unfilled = evidence["unfilled_opportunities"]
        if consecutive >= maximum_unfilled:
            trigger = "max_unfilled_opportunities"
            reason = (
                f"{consecutive} consecutive intent-without-fill opportunities "
                f"reached the round limit of {maximum_unfilled}."
            )
        elif unfilled >= maximum_unfilled:
            trigger = "max_unfilled_opportunities"
            reason = (
                f"{unfilled} unfilled opportunities reached the round limit of "
                f"{maximum_unfilled}."
            )
    if trigger is None:
        maximum_drawdown = policy.get("max_abs_drawdown")
        if isinstance(maximum_drawdown, (int, float)) and not isinstance(
            maximum_drawdown, bool
        ) and maximum_drawdown >= 0:
            observed = evidence["max_abs_drawdown"]
            if observed > 0 and observed >= float(maximum_drawdown):
                trigger = "max_abs_drawdown"
                reason = (
                    f"The full-history maximum drawdown {observed:.4f} reached the "
                    f"round limit {float(maximum_drawdown):.4f}."
                )
    if trigger is None:
        maximum_gap = policy.get("max_data_gap_sessions")
        if type(maximum_gap) is int and maximum_gap > 0:
            gaps = evidence["data_gap_sessions"]
            if gaps >= maximum_gap:
                trigger = "max_data_gap_sessions"
                reason = (
                    f"{gaps} distorted feed sessions reached the round limit of "
                    f"{maximum_gap}."
                )
    if trigger is None and term_reached and not enough:
        trigger = "observation_extended"
        reason = (
            f"The confirmed {term}-session observation term is reached with "
            f"{evidence['due_opportunities']} due opportunities, below the "
            f"minimum of {minimum}; tracking continues."
        )
    if trigger is None and term_reached and enough:
        trigger = "observation_term"
        reason = (
            f"The confirmed {term}-session observation term is reached with "
            f"{evidence['due_opportunities']} due opportunities (minimum {minimum})."
        )
    return {
        "trigger": trigger,
        "reason": reason,
        "term": term,
        "minimum": minimum,
        "term_reached": term_reached,
        "enough": enough,
    }


def _evidence_state(
    review: Mapping[str, Any],
    review_state: str,
    release_state: str,
    pauses: list[dict[str, Any]],
) -> str:
    if review_state == "ended" or release_state == "retired":
        return "ended"
    if release_state == "paused" or pauses:
        return "paused"
    if review["trigger"] == "observation_extended":
        return "observation_extended"
    if review["trigger"] is not None:
        return "review_due"
    return "tracking"


def _pause_record(
    release_state: str,
    pauses: list[dict[str, Any]],
    latest_decision: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if pauses:
        record = sorted(
            pauses, key=lambda item: (item["session"], item["scope"], item["code"])
        )[0]
        return {
            key: record.get(key)
            for key in ("session", "scope", "code", "message", "recorded_at")
        }
    if release_state == "paused":
        if latest_decision is not None and latest_decision.get("decision") == "pause":
            effect = latest_decision.get("effect") or {}
            return {
                "session": effect.get("effective_signal_date"),
                "scope": latest_decision.get("scope", "version"),
                "code": "MANUAL_PAUSE",
                "message": latest_decision["reason"],
                "recorded_at": latest_decision["decided_at"],
                "decision_id": latest_decision["decision_id"],
            }
        return {
            "session": None,
            "scope": "version",
            "code": "MANUAL_PAUSE",
            "message": "The release timeline is paused.",
            "recorded_at": None,
            "decision_id": None,
        }
    return None


def _evidence_facts(
    evidence: Mapping[str, Any], confirmed: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "committed_sessions": evidence["committed_sessions"],
        "first_committed_date": evidence["first_committed_date"],
        "last_committed_date": evidence["last_committed_date"],
        "first_signal_date": evidence["first_signal_date"],
        "first_fill_date": evidence["first_fill_date"],
        "due_opportunities": evidence["due_opportunities"],
        "filled_opportunities": evidence["filled_opportunities"],
        "unfilled_opportunities": evidence["unfilled_opportunities"],
        "intent_without_fill": evidence["intent_without_fill"],
        "consecutive_intent_without_fill": evidence["consecutive_intent_without_fill"],
        "metrics_not_ready": evidence["metrics_not_ready"]["count"],
        "data_gap_sessions": evidence["data_gap_sessions"],
        "max_abs_drawdown": evidence["max_abs_drawdown"],
        "observation": (
            None
            if confirmed is None
            else {
                "confirmed_sessions": confirmed.get("observation_sessions"),
                "minimum_opportunities": confirmed.get("minimum_opportunities"),
            }
        ),
    }


def _evidence_decision_output(
    body: Mapping[str, Any], *, reused: bool,
) -> dict[str, Any]:
    effect = body["effect"]
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "round_id": body["round_id"],
        "version_id": body["version_id"],
        "release_id": body["release_id"],
        "decision": body["decision"],
        "decision_id": body["decision_id"],
        "reviewer": body["reviewer"],
        "reason": body["reason"],
        "decided_at": body["decided_at"],
        "scope": body.get("scope", "version"),
        "facts": body["facts"],
        "effect": effect,
        "state": (
            "ended"
            if effect["state_after"] == "retired"
            else ("paused" if effect["state_after"] == "paused" else "tracking")
        ),
        "release_state": effect["state_after"],
        "reused": reused,
    }


def _shadow_evidence(ledger: Path, release_id: str) -> dict[str, Any]:
    from prajnatrade import shadow

    try:
        return shadow.evidence(ledger, release_id)
    except shadow.ShadowRejection as error:
        raise ProtocolError(
            error.details["code"], "evidence", error.details["next_action"],
            error.details["message"],
        ) from error


def evidence_status(
    project: Path,
    store: Path,
    ledger: Path,
    round_id: str,
    version_id: str | None = None,
) -> dict[str, Any]:
    """Per approved version: ledger facts, review trigger, pause and state (T3)."""
    from prajnatrade.protected_eval import evaluation_store

    trusted = evaluation_store(store)
    if trusted.is_relative_to(project.resolve()):
        raise ValueError("Trusted evaluation store must be outside the research project.")
    with _project(project) as connection:
        record = _project_round(connection, round_id)
        if version_id is not None:
            _require_hex(version_id, "version")
        submissions = [
            json.loads(row["submission_json"])
            for row in connection.execute(
                "SELECT submission_json FROM research_loop_submissions "
                "WHERE round_id = ? ORDER BY rowid",
                (round_id,),
            )
        ]
        if version_id is not None and not any(
            item["version_id"] == version_id for item in submissions
        ):
            raise ValueError("Unknown version for this research round.")
        versions: list[dict[str, Any]] = []
        for submission in submissions:
            item_version = submission["version_id"]
            if version_id is not None and item_version != version_id:
                continue
            tracking = _latest_tracking(connection, item_version)
            if tracking is None:
                continue
            release_id = tracking["release_id"]
            review_state = _review_state(connection, item_version)
            latest_decision = _latest_evidence_decision(connection, item_version)
            confirmed = _confirmed_observation(connection, item_version)
            evidence = _shadow_evidence(ledger, release_id)
            _, release_status = _evidence_tracking_release(trusted, release_id)
            release_state = release_status["current_state"]
            enabled = tracking["enabled_signal_date"]
            committed_dates = evidence["committed_dates"]
            committed_sessions = sum(
                1 for session in committed_dates if session >= enabled
            )
            review = _evidence_review(
                record["kickoff"], evidence, confirmed, committed_sessions
            )
            pauses = list(evidence.get("active_pauses") or [])
            state = _evidence_state(review, review_state, release_state, pauses)
            term = review["term"]
            versions.append(
                {
                    "version_id": item_version,
                    "review_state": review_state,
                    "tracking": {
                        "release_id": release_id,
                        "enabled_signal_date": enabled,
                        "committed_sessions": evidence["committed_sessions"],
                        "first_committed_date": evidence["first_committed_date"],
                        "last_committed_date": evidence["last_committed_date"],
                        "first_signal_date": evidence["first_signal_date"],
                        "first_fill_date": evidence["first_fill_date"],
                        "due_opportunities": evidence["due_opportunities"],
                        "filled_opportunities": evidence["filled_opportunities"],
                        "unfilled_opportunities": evidence["unfilled_opportunities"],
                        "intent_without_fill": evidence["intent_without_fill"],
                        "consecutive_intent_without_fill": evidence[
                            "consecutive_intent_without_fill"
                        ],
                        "metrics_not_ready": evidence["metrics_not_ready"],
                        "data_gap_sessions": evidence["data_gap_sessions"],
                        "max_abs_drawdown": evidence["max_abs_drawdown"],
                        "observation": {
                            "confirmed_sessions": term,
                            "minimum_opportunities": review["minimum"],
                            "remaining_sessions": (
                                None
                                if term is None
                                else max(0, term - committed_sessions)
                            ),
                            "status": state,
                        },
                        "opportunities": evidence["opportunities"],
                    },
                    "review": {
                        "trigger": review["trigger"],
                        "reason": review["reason"],
                        "required": review["trigger"]
                        not in (None, "observation_extended"),
                    },
                    "pause": _pause_record(release_state, pauses, latest_decision),
                    "state": state,
                }
            )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "round_id": round_id,
        "direction_id": record["direction_id"],
        "project_id": record["project_id"],
        "scope": record["scope"],
        "data_kind": record["data_kind"],
        "versions": versions,
    }


def evidence_decide(
    project: Path,
    store: Path,
    ledger: Path,
    round_id: str,
    version_id: str,
    decision: str,
    reviewer: str,
    reason: str,
) -> dict[str, Any]:
    """Record one immutable human continue/pause/retire decision (T3)."""
    from prajnatrade import shadow
    from prajnatrade.protected_eval import evaluation_store

    if decision not in EVIDENCE_DECISIONS:
        raise ValueError("decision must be continue, pause or retire.")
    reviewer = _text(reviewer, "reviewer")
    reason = _text(reason, "reason")
    trusted = evaluation_store(store)
    if trusted.is_relative_to(project.resolve()):
        raise ValueError("Trusted evaluation store must be outside the research project.")
    with _round_lock(project, round_id), _project(project) as connection:
        _project_round(connection, round_id)
        _submission(connection, round_id, version_id, phase="evidence")
        tracking = _latest_tracking(connection, version_id)
        if tracking is None:
            raise ProtocolError(
                "REVIEW_CONFLICT", "evidence", "review",
                "Only an approved simulated-tracking version can record an evidence decision.",
            )
        release_id = tracking["release_id"]
        artifact, release_status = _evidence_tracking_release(trusted, release_id)
        state_before = release_status["current_state"]
        evidence = _shadow_evidence(ledger, release_id)
        confirmed = _confirmed_observation(connection, version_id)
        scope = "version"
        request_key = _evidence_request_key(
            round_id, version_id, release_id, decision, scope, reviewer, reason
        )
        existing = _matching_evidence_decision(
            connection, trusted, version_id, request_key
        )
        if existing is not None:
            return _evidence_decision_output(existing, reused=True)
        if state_before == "retired" or _review_state(connection, version_id) == "ended":
            raise ProtocolError(
                "VERSION_ENDED", "evidence", "inspect",
                "An ended version is terminal; the round may continue with a new version.",
            )
        if decision == "pause" and state_before != "enabled":
            raise ProtocolError(
                "REVIEW_CONFLICT", "evidence", "inspect",
                "Pause requires an enabled tracking version; it is already paused.",
            )
        effective = _evidence_effective_session(artifact, evidence, release_status)
        action = "none"
        state_after = state_before
        if decision == "pause":
            release.pause(trusted, release_id, effective, reason)
            action = "pause"
            state_after = "paused"
        elif decision == "continue":
            if state_before == "paused":
                release.resume(trusted, release_id, effective, reason)
                action = "resume"
                state_after = "enabled"
        else:
            release.retire(trusted, release_id, effective, reason)
            action = "retire"
            state_after = "retired"
            shadow.mark_version_ended(ledger, release_id, release_id)
        body: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "round_id": round_id,
            "version_id": version_id,
            "release_id": release_id,
            "decision": decision,
            "scope": scope,
            "reviewer": reviewer,
            "reason": reason,
            "decided_at": _now(),
            "request_key": request_key,
            "facts": _evidence_facts(evidence, confirmed),
            "effect": {
                "action": action,
                "effective_signal_date": effective,
                "state_before": state_before,
                "state_after": state_after,
            },
        }
        body["decision_id"] = _hash(body)
        _store_evidence_decision(connection, trusted, body)
    return _evidence_decision_output(body, reused=False)


# ---------------------------------------------------------------------------
# round close
# ---------------------------------------------------------------------------


def _version_approved(connection: sqlite3.Connection, version_id: str) -> bool:
    row = connection.execute(
        "SELECT review_json FROM research_loop_reviews WHERE version_id = ? "
        "ORDER BY rowid DESC LIMIT 1",
        (version_id,),
    ).fetchone()
    return row is not None and json.loads(row["review_json"]).get("decision") == "approve"


def close(
    project: Path, round_id: str, reviewer: str, reason: str, source: object,
) -> dict[str, Any]:
    """Record an early or exhausted stop with the round's selected candidate."""
    payload = _object(source, {"stop_reason", "selected_version", "next_experiment"})
    stop_reason = _text(payload["stop_reason"], "stop_reason")
    selected = payload["selected_version"]
    selected = None if selected is None else _text(selected, "selected_version")
    next_experiment = payload["next_experiment"]
    if next_experiment is not None:
        next_experiment = _text(next_experiment, "next_experiment")
    reviewer = _text(reviewer, "reviewer")
    reason = _text(reason, "reason")
    with _round_lock(project, round_id), _project(project) as connection:
        record = _project_round(connection, round_id)
        previous = _closure(connection, round_id)
        versions = [
            json.loads(row["submission_json"])["version_id"]
            for row in connection.execute(
                "SELECT submission_json FROM research_loop_submissions "
                "WHERE round_id = ? ORDER BY rowid",
                (round_id,),
            )
        ]
        body = {
            "schema_version": SCHEMA_VERSION,
            "round_id": round_id,
            "direction_id": record["direction_id"],
            "project_id": record["project_id"],
            "campaign_id": record["campaign_id"],
            "scope": record["scope"],
            "data_kind": record["data_kind"],
            "reviewer": reviewer,
            "reason": reason,
            "stop_reason": stop_reason,
            "candidate_selected": selected is not None,
            "selected_version": selected,
            "next_experiment": next_experiment,
            "budget": dict(record["kickoff"]["budget"]),
            "versions_attempted": len(versions),
        }
        closure_id = _hash(body)
        closure = {**body, "closure_id": closure_id}
        if previous is not None:
            if previous.get("closure_id") == closure_id:
                return {**previous, "status": "closed", "reused": True}
            raise ProtocolError(
                "ROUND_CLOSED", "close", "inspect",
                "The research round already has a different closure.",
            )
        if selected is not None:
            if selected not in versions:
                raise ProtocolError(
                    "VALIDATION_NOT_PASSED", "close", "review",
                    "The selected version is not locked in this research round.",
                )
            if not _version_approved(connection, selected):
                raise ProtocolError(
                    "VALIDATION_NOT_PASSED", "close", "review",
                    "The selected version has no approving review decision.",
                )
        connection.execute(
            "INSERT INTO research_loop_closures (round_id, closure_json) VALUES (?, ?)",
            (round_id, _json(closure)),
        )
    return {**closure, "status": "closed", "reused": False}
