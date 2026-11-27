"""Research rounds, lightweight Validation, review admission and revalidation (spec #43).

This module is the manual simulated-tracking admission path.  It is deliberately
separate from the protected ``evaluation``/``holdout`` flow and from conditional
``release``: protocol-v1 lightweight Validation replays the locked Discovery
version over the Campaign's frozen Discovery interval using the same sealed
snapshot, point-in-time cutoffs, execution rules, costs and initial cash, while
protocol-v2 formal trailing Validation scores only the frozen trailing segment
with the development segment as prehistory.  Both protocols read only the sealed
Discovery payload; neither opens a protected partition, authorizes a stage or
writes a query debit.

A protocol-v2 round (a Campaign created with ``--discovery-split``) counts one
trailing read per distinct locked version.  ``round validate`` reserves that use
atomically before any attempt or sealed read, blocks a declared trailing gap
from sealed manifest metadata with zero count, reads the payload once at the
frozen ``as_of``, verifies the data kind, partition/prior-history hashes and the
recomputed development/trailing slices, then scores the candidate and the
benchmark from the trailing first session.  ``round revalidate`` keeps the same
version/use and recomputes the trailing segment on a corrected successor that
preserves the frozen development slice, recording the original and recomputed
epochs and the affected diff.  Protocol-v1 rounds keep the #43 replay, receipt
shape and identity exactly.

Contract resolutions that the shared design contract leaves implicit:

* ``round_id`` reuses :func:`research_direction.direction_id`, so a direction
  keeps its identity across renamed keys and new Campaigns.
* ``validation_id`` includes the accounting-engine fingerprint. For protocol v1,
  an accounting-platform fix yields a new identity and re-runs the replay. For
  protocol v2, the counted use binds the initial Validation identity; a completed
  receipt replays from that identity across runtime changes. An unfinished read
  can resume only under its original runtime.
* Review decisions are immutable content-addressed epochs per version with
  exactly one current state.  Approval binds the stored terminal passed receipt,
  writes a write-once ``manual_tracking`` release artifact and appends one
  ``enable`` event at the first approved calendar session after the decision; it
  never runs Validation again, so re-selecting a version reuses its receipt.
  The replay key is the human decision content, so replaying it on a later day
  returns the first stored effective session instead of conflicting.
* Ending an approved version is one terminal path whether it comes from
  ``round review --decision end`` or ``round evidence decide retire``: the
  immutable decision is recorded, the release timeline gets its terminal retire
  event and the next Shadow Live tick marks the ledger progress ``ended``
  without adding a day, signal or order.  ``round close`` only selects a
  currently ``approved``, not-ended version.
* ``round revalidate`` also accepts an ``approved`` version after a data/platform
  correction.  A passing corrected receipt keeps the current state ``approved``;
  a failing or insufficient one keeps the approval but records a concrete review
  trigger rather than silently revoking it or pausing tracking.  ``ended`` stays
  terminal and is refused.
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
from collections.abc import Collection, Generator, Mapping
from contextlib import closing, contextmanager
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from prajnatrade import release, shadow_calendar, snapshot
from prajnatrade.container_executor import executor_from_config
from prajnatrade.data import ETF_SYMBOLS
from prajnatrade.discovery_summary import summarize_discovery_quarters
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
    ACTUAL_OPEN_LOT_RULES,
    ACTUAL_OPEN_RULES,
    EXECUTION_PRICE_BASIS,
    NEXT_OPEN_RULES,
    STRICT_CLOSE_VALUATION,
    run_discovery_equal_weight,
    run_partition_strategy,
    valuation_policy_version,
)
from prajnatrade.strategies import make_strategy

SCHEMA_VERSION = 1
METRIC_TOLERANCE = 1e-9
SCORING_MODES = ("valid_observations", "consecutive_sessions")
DATA_AVAILABILITY = "next_open"
BENCHMARK = "equal_weight_buy_and_hold"
VALIDATION_PROTOCOL_LEGACY = {
    "schema_version": 1,
    "name": "discovery-replay",
    "validation_kind": "discovery_replay",
}
VALIDATION_PROTOCOL_TRAILING = {
    "schema_version": 1,
    "name": "trailing-70-30",
    "validation_kind": "trailing_validation",
}
ROUND_NAME = "research-loop-rounds"
VALIDATION_NAME = "research-loop-validations"
REVIEW_NAME = "research-loop-reviews"
REVALIDATION_NAME = "research-loop-revalidations"
ADMISSION_NAME = "research-loop-admissions"
VALIDATION_FAILPOINT = "round_validation_after_attempt"
VALIDATION_TERMINAL_FAILPOINT = "round_validation_after_terminal_receipt"
EVIDENCE_DECIDE_FAILPOINT = "round_evidence_after_apply"
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
#: Basket-build states that leave a required basket unfinished; a formal
#: position prerequisite is not satisfied while either side reports one.
_UNFINISHED_BUILD_STATES = {"partial", "unfilled"}
_OBSERVATION_FORMULA = (
    "ceil(sessions / max(1, opportunities)) * minimum_opportunities, "
    "clamped to [minimum_opportunities, 252]"
)
_RECEIPT_FIELDS = (
    "schema_version", "round_id", "version_id", "validation_id", "status", "outcome",
    "reason", "metrics", "checks", "observation", "replay", "receipt_hash",
    "opened_protected_source", "query_debited",
)
# Additive fields are published only when the receipt actually carries them, so
# every legacy (#43) receipt keeps its exact shape.  ``replay`` is absent from a
# formal trailing receipt by design and is therefore only published when present.
_OPTIONAL_RECEIPT_FIELDS = (
    "evidence", "original_discovery", "recomputed_discovery", "affected",
    "prior_validation_id", "validation_kind", "protocol", "development", "trailing",
    "scoring", "warmup", "provenance", "usage", "summary", "runs",
    "original_validation", "recomputed_validation", "discovery_precheck",
)
VALIDATION_KIND_TRAILING = "trailing_validation"
_VALIDATION_USE_FIELDS = (
    "use_id", "round_id", "version_id", "sequence", "kind", "admission_id",
    "validation_id", "consumed_at",
)
_GAP_KINDS = ("raw_bars", "derived", "corporate_actions", "availability")
#: #56 schema-3 execution-evidence classifications a counted trailing read may
#: accept, versus the ones that must block before any read.
_EXECUTION_EVIDENCE_BENIGN = ("actual_open_after_halt", "confirmed_no_trade")
_EXECUTION_EVIDENCE_BLOCKING = (
    "missing_required_open", "source_conflict", "unknown_status",
)
#: Which manifest gap kinds a benign execution-evidence row actually explains.
#: A trusted daily open proves the raw bar (and a confirmed no-trade day also
#: proves its null derived value); neither says anything about availability or
#: corporate-action terms, so those gaps are never explained by evidence.
_EXECUTION_EVIDENCE_EXPLAINS = {
    "actual_open_after_halt": ("raw_bars",),
    "confirmed_no_trade": ("raw_bars", "derived"),
}

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
CREATE TABLE IF NOT EXISTS research_loop_validation_uses (
    use_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL REFERENCES research_loop_rounds(round_id),
    version_id TEXT NOT NULL REFERENCES research_loop_submissions(version_id),
    sequence INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('automatic', 'manual')),
    admission_id TEXT,
    validation_id TEXT NOT NULL,
    consumed_at TEXT NOT NULL,
    UNIQUE (round_id, version_id),
    UNIQUE (round_id, sequence)
);
CREATE TABLE IF NOT EXISTS research_loop_admissions (
    admission_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL REFERENCES research_loop_rounds(round_id),
    version_id TEXT NOT NULL REFERENCES research_loop_submissions(version_id),
    admission_json TEXT NOT NULL,
    UNIQUE (round_id, version_id)
);
CREATE TABLE IF NOT EXISTS research_loop_details (
    detail_access_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL REFERENCES research_loop_rounds(round_id),
    version_id TEXT NOT NULL REFERENCES research_loop_submissions(version_id),
    validation_id TEXT NOT NULL,
    request_json TEXT NOT NULL,
    resolution_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_loop_feedback (
    feedback_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL REFERENCES research_loop_rounds(round_id),
    version_id TEXT NOT NULL REFERENCES research_loop_submissions(version_id),
    feedback_json TEXT NOT NULL
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
    """The decision timestamp; ``PRAJNATRADE_TEST_NOW`` is a test-only clock."""
    fixed = os.environ.get("PRAJNATRADE_TEST_NOW")
    if fixed:
        parsed = datetime.fromisoformat(fixed)
        if parsed.tzinfo is None:
            raise ValueError("PRAJNATRADE_TEST_NOW must include a timezone.")
        return parsed.isoformat()
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


def _rejected(
    code: str, phase: str, next_action: str, message: str, **context: object,
) -> ProtocolError:
    """Build a typed rejection with the machine-readable context the CLI echoes."""
    error = ProtocolError(code, phase, next_action, message)
    if context:
        error.context = context
    return error


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
        frozenset({"validation_policy"}),
    )
    comparison = _object(payload["comparison"], {"benchmark", "statement"})
    if comparison["benchmark"] != BENCHMARK:
        raise ValueError(f"comparison.benchmark must be {BENCHMARK}.")
    contract = {
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
    if "validation_policy" in payload:
        contract["validation_policy"] = payload["validation_policy"]
    return contract


def _validation_policy(raw: object) -> dict[str, int]:
    """Normalize the v2 round's automatic Validation allowance."""
    if not isinstance(raw, dict) or set(raw) != {"auto_validation_limit"}:
        raise ProtocolError(
            "VALIDATION_POLICY_REQUIRED", "round", "correct",
            "A round on a Campaign with a frozen Discovery split requires "
            "validation_policy.auto_validation_limit.",
        )
    limit = raw["auto_validation_limit"]
    if type(limit) is not int or limit <= 0:
        raise ProtocolError(
            "VALIDATION_POLICY_REQUIRED", "round", "correct",
            "validation_policy.auto_validation_limit must be a positive integer.",
        )
    return {"auto_validation_limit": limit}


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
    # ``opened_at`` and the derived cross-round ``reuse_disclosure`` are stored
    # alongside the approved kickoff but are deliberately outside its hash.
    body = {
        key: value
        for key, value in record.items()
        if key not in {"kickoff_hash", "opened_at", "reuse_disclosure"}
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
    split = evaluation.get("discovery_split")
    if split is None:
        if "validation_policy" in contract:
            raise ProtocolError(
                "DISCOVERY_SPLIT_NOT_FROZEN", "round", "correct",
                "validation_policy requires a Campaign created with "
                "--discovery-split.",
            )
    else:
        split = snapshot.verify_discovery_split(split)
        if split["development"]["content_hash"] != campaign["snapshot_hash"]:
            raise ValueError(
                "The Campaign's Discovery development binding changed."
            )
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
    if split is not None:
        kickoff["protocol"] = dict(VALIDATION_PROTOCOL_TRAILING)
        kickoff["research_partition"] = split
        kickoff["validation_policy"] = _validation_policy(
            contract.pop("validation_policy", None)
        )
    full_history = evaluation.get("full_history")
    if full_history is not None:
        if authorization_id != full_history["authorization_id"]:
            raise ProtocolError(
                "AUTHORIZATION_MISMATCH", "round", "correct",
                "The round authorization must match the full-history authorization.",
            )
        kickoff["full_history"] = dict(full_history)
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
    result = {
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
    if "reuse_disclosure" in record:
        result["reuse_disclosure"] = record["reuse_disclosure"]
    return result


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
    split = record["kickoff"].get("research_partition")
    if split is not None:
        frozen_split = evaluation.get("discovery_split")
        if not isinstance(frozen_split, Mapping):
            raise ValueError(
                "The Campaign Discovery split changed after the research round opened."
            )
        bindings["Discovery split"] = (split["split_hash"], frozen_split.get("split_hash"))
        bindings["Discovery development content"] = (
            split["development"]["content_hash"], campaign["snapshot_hash"],
        )
    for label, (expected, actual) in bindings.items():
        if expected != actual:
            raise ValueError(f"The Campaign {label} changed after the research round opened.")


def _receipt_used_interval(receipt: Mapping[str, Any]) -> dict[str, str] | None:
    """The interval a published Validation receipt actually used.

    A formal trailing receipt records its ``trailing`` segment.  A legacy
    receipt records the replayed Discovery scoring interval in ``replay`` (its
    ``scoring`` block is additive for protocol v2).  Old receipts keep being
    read under their original protocol.
    """
    trailing = receipt.get("trailing")
    if isinstance(trailing, Mapping) and {"start", "end"} <= set(trailing):
        return {"start": str(trailing["start"]), "end": str(trailing["end"])}
    replay = receipt.get("replay")
    if isinstance(replay, Mapping):
        candidate = replay.get("candidate")
        if isinstance(candidate, Mapping) and {
            "start_date", "end_date"
        } <= set(candidate):
            return {
                "start": str(candidate["start_date"]),
                "end": str(candidate["end_date"]),
            }
    scoring = receipt.get("scoring")
    if isinstance(scoring, Mapping) and {"start", "end"} <= set(scoring):
        return {"start": str(scoring["start"]), "end": str(scoring["end"])}
    return None


def _reuse_disclosure(
    connection: sqlite3.Connection, prepared: Mapping[str, Any],
) -> dict[str, Any]:
    """Disclose the intervals, reads and feedback a previous round used.

    Derived once, at v2 ``round open``, from the project's own published
    receipts, Validation-use rows and feedback rows: no sealed payload is read
    and no protected record is touched.  A prior round is matched either on the
    same sealed ``snapshot_id`` (``match: "same_snapshot"``) or on the same
    frozen development content hash (``match: "same_development"``), so a
    corrected successor snapshot that preserves the approved development
    prehistory is still disclosed; a round is listed once with the stronger
    snapshot match.  Each entry also carries the prior round's counted read
    history.  The result is outside ``kickoff_hash`` and is written once with
    the trusted round record, so a replay returns the stored disclosure instead
    of a value that a later prior round could change.
    """
    kickoff = prepared["kickoff"]
    snapshot_id = kickoff["frozen_campaign"]["snapshot_id"]
    development_hash = kickoff["research_partition"]["development"]["content_hash"]
    trailing = kickoff["research_partition"]["trailing"]
    authorized_range = kickoff.get("full_history", {}).get("authorized_range")
    previous_rounds: list[dict[str, Any]] = []
    disclosed_feedback: list[dict[str, Any]] = []
    for row in connection.execute(
        "SELECT r.round_id, r.campaign_id, r.kickoff_json, c.snapshot_id, "
        "c.evaluation_json FROM research_loop_rounds r "
        "JOIN campaigns c ON c.campaign_id = r.campaign_id ORDER BY r.rowid"
    ):
        evaluation = json.loads(row["evaluation_json"])
        used_snapshot = row["snapshot_id"] or evaluation.get("snapshot_id")
        prior_split = evaluation.get("discovery_split")
        prior_development = None
        if isinstance(prior_split, Mapping):
            prior_development = prior_split.get("development")
            prior_development = (
                prior_development.get("content_hash")
                if isinstance(prior_development, Mapping) else None
            )
        if used_snapshot == snapshot_id:
            match = "same_snapshot"
        elif prior_development == development_hash:
            match = "same_development"
        else:
            prior_full_history = evaluation.get("full_history")
            prior_range = (
                prior_full_history.get("authorized_range")
                if isinstance(prior_full_history, Mapping) else None
            )
            if (
                isinstance(authorized_range, Mapping)
                and isinstance(prior_range, Mapping)
                and max(authorized_range["start"], prior_range["start"])
                <= min(authorized_range["end"], prior_range["end"])
            ):
                match = "overlapping_authorized_interval"
            else:
                continue
        receipt_row = connection.execute(
            "SELECT v.receipt_json FROM research_loop_validations v "
            "JOIN research_loop_submissions s USING (version_id) "
            "WHERE s.round_id = ? ORDER BY v.rowid LIMIT 1",
            (row["round_id"],),
        ).fetchone()
        if receipt_row is None:
            continue
        used = _receipt_used_interval(json.loads(receipt_row["receipt_json"]))
        if used is None:
            continue
        start = max(used["start"], trailing["start"])
        end = min(used["end"], trailing["end"])
        prior_kickoff = json.loads(row["kickoff_json"])["kickoff"]
        protocol = prior_kickoff.get("protocol", VALIDATION_PROTOCOL_LEGACY)
        uses = _validation_uses(connection, row["round_id"])
        previous_rounds.append(
            {
                "round_id": row["round_id"],
                "campaign_id": row["campaign_id"],
                "protocol": protocol["name"],
                "match": match,
                "used": used,
                "overlap": {"start": start, "end": end} if start <= end else None,
                "automatic_used": sum(1 for item in uses if item["kind"] == "automatic"),
                "manual_used": sum(1 for item in uses if item["kind"] == "manual"),
                "total_used": len(uses),
                "uses": [
                    {
                        "version_id": item["version_id"],
                        "validation_id": item["validation_id"],
                        "kind": item["kind"],
                        "admission_id": item["admission_id"],
                        "sequence": item["sequence"],
                    }
                    for item in uses
                ],
            }
        )
        for feedback_row in connection.execute(
            "SELECT f.feedback_json FROM research_loop_feedback f "
            "JOIN research_loop_submissions s ON s.version_id = f.version_id "
            "WHERE s.round_id = ? ORDER BY f.rowid",
            (row["round_id"],),
        ):
            body = json.loads(feedback_row["feedback_json"])
            disclosed_feedback.append(
                {
                    "feedback_id": body["feedback_id"],
                    "round_id": body["round_id"],
                    "version_id": body["version_id"],
                    "impact": body["impact"],
                }
            )
    return {
        "snapshot_id": snapshot_id,
        "previous_rounds": previous_rounds,
        "feedback": disclosed_feedback,
        "overlapping": any(item["overlap"] is not None for item in previous_rounds),
    }


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
        candidate = prepared
        if "research_partition" in prepared["kickoff"]:
            if target.is_file():
                # Crash window: the trusted round was written before the
                # project projection.  Reuse its once-computed disclosure (a
                # prior round created meanwhile must not rewrite history).
                trusted_record = _load_round_file(target, round_id)
                if trusted_record["kickoff_hash"] != prepared["kickoff_hash"]:
                    raise ProtocolError(
                        "ROUND_ALREADY_OPENED", "round", "inspect",
                        "The Campaign already has a different approved research round.",
                    )
                candidate = trusted_record
            else:
                candidate = {
                    **prepared,
                    "reuse_disclosure": _reuse_disclosure(connection, prepared),
                }
        stored = write_trusted_record_once(target, candidate)
        if stored != candidate:
            raise ProtocolError(
                "ROUND_ALREADY_OPENED", "round", "inspect",
                "The Campaign already has a different approved research round.",
            )
        connection.execute(
            "INSERT INTO research_loop_rounds (round_id, campaign_id, kickoff_json) "
            "VALUES (?, ?, ?)",
            (round_id, prepared["campaign_id"], _json(candidate)),
        )
    return _opened(candidate, reused=False)


# ---------------------------------------------------------------------------
# round submit
# ---------------------------------------------------------------------------


def _locked_strategy(attempt: Mapping[str, Any]) -> dict[str, Any]:
    strategy = attempt["strategy"]
    locked: dict[str, Any] = {
        "family": strategy["family"],
        "parameters": attempt["parameters"],
    }
    if "plugin" in strategy:
        locked["plugin"] = dict(strategy["plugin"])
    if strategy["family"] == "python":
        locked["source"] = strategy["source"]
        if "source_sha256" in strategy:
            locked["source_sha256"] = strategy["source_sha256"]
        if "parameter_schema" in strategy:
            locked["parameter_schema"] = strategy["parameter_schema"]
    return locked


def _protocol_hash(kickoff: Mapping[str, Any]) -> str:
    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "comparison": kickoff["comparison"],
        "hard_conditions": kickoff["hard_conditions"],
        "selection_rules": kickoff["selection_rules"],
    }
    # Protocol v1 hashes only the legacy contract fields.  Protocol v2 also
    # binds the trailing-validation protocol block and the frozen split so a
    # locked version traces to the partition protocol it was scored under.
    if "protocol" in kickoff:
        body["protocol"] = kickoff["protocol"]
        split = kickoff["research_partition"]
        body["research_partition"] = {
            key: split[key] for key in ("split_hash", "development", "trailing")
        }
    return _hash(body)


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
    for field in ("revision_of", "feedback_ids"):
        if field in body:
            result[field] = body[field]
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
        frozenset({"related_experiment_ids", "revision_of", "feedback_ids"}),
    )
    experiment_id = _text(payload["experiment_id"], "experiment_id")
    change = _text(payload["change"], "change")
    question = _text(payload["question"], "question")
    expected_change = _text(payload["expected_change"], "expected_change")
    revisit_condition = _text(payload["revisit_condition"], "revisit_condition")
    risks = _texts(payload["risks"], "risks")
    related = _texts(payload.get("related_experiment_ids", []), "related_experiment_ids")
    revision_of = payload.get("revision_of")
    if revision_of is not None:
        revision_of = _require_hex(revision_of, "revision")
    feedback_ids = payload.get("feedback_ids", [])
    feedback_ids = sorted(set(_texts(feedback_ids, "feedback_ids")))
    for feedback_id in feedback_ids:
        _require_hex(feedback_id, "feedback")
    submission_id = _text(submission_id, "submission_id")
    with _round_lock(project, round_id), _project(project) as connection:
        record = _project_round(connection, round_id)
        if _closure(connection, round_id) is not None:
            raise ProtocolError(
                "ROUND_CLOSED", "submission", "inspect",
                "A closed research round cannot accept a new version.",
            )
        campaign = campaign_discovery(project, record["campaign_id"])
        _require_campaign_binding(record, campaign)
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
        # Revision and feedback links are provenance, not identity: they are
        # validated here but deliberately excluded from ``version_id``.
        if revision_of is not None:
            if revision_of == version_id:
                raise _rejected(
                    "REVISION_CONFLICT", "submission", "correct",
                    "A version cannot revise itself.",
                )
            prior = connection.execute(
                "SELECT 1 FROM research_loop_submissions "
                "WHERE round_id = ? AND version_id = ?",
                (round_id, revision_of),
            ).fetchone()
            if prior is None:
                raise _rejected(
                    "REVISION_NOT_FOUND", "submission", "correct",
                    "revision_of must name a prior version locked in this round.",
                )
        for feedback_id in feedback_ids:
            row = connection.execute(
                "SELECT round_id, version_id FROM research_loop_feedback "
                "WHERE feedback_id = ?",
                (feedback_id,),
            ).fetchone()
            if (
                row is None
                or row["round_id"] != round_id
                or row["version_id"] == version_id
            ):
                raise _rejected(
                    "FEEDBACK_LINK_INVALID", "submission", "correct",
                    "feedback_ids must link feedback from prior versions of this "
                    "round, never another round or the version being locked.",
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
            **({"revision_of": revision_of} if revision_of is not None else {}),
            **({"feedback_ids": feedback_ids} if feedback_ids else {}),
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
                **(
                    {"decision_policy": evaluation["decision_policy"]}
                    if evaluation.get("decision_policy") is not None
                    else {}
                ),
                **(
                    {
                        "protocol": record["kickoff"]["protocol"],
                        "research_partition": record["kickoff"]["research_partition"],
                    }
                    if "protocol" in record["kickoff"] else {}
                ),
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
    strategy: Mapping[str, Any],
    executor: Mapping[str, Any],
    *,
    plugin_registry: Mapping[str, Mapping[str, Any]] | None = None,
):
    spec: dict[str, Any] = {
        "family": strategy["family"],
        "parameters": strategy["parameters"],
    }
    if "plugin" in strategy:
        spec["plugin"] = strategy["plugin"]
        return make_strategy(spec, plugin_registry=plugin_registry)
    if strategy["family"] != "python":
        return make_strategy(spec, plugin_registry=plugin_registry)
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
    *,
    plugin_registry: Mapping[str, Mapping[str, Any]] | None = None,
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
    valuation_version = valuation_policy_version(rules)
    decision_policy = locked.get("decision_policy")
    lot_sizes = rules.get("execution", {}).get("lot_sizes")
    symbol_order = rules.get("execution", {}).get("symbol_order")
    options = {
        "data_kind": source["data_kind"],
        "signal_window_sessions": locked["signal_window_sessions"],
        "prior_history": prior_history,
        "initial_cash": locked["base_costs"]["initial_cash"],
        "transaction_cost_bps": locked["base_costs"]["transaction_cost_bps"],
        "slippage_bps": locked["base_costs"]["slippage_bps"],
        "execution_rule_version": rule_version,
        "valuation_policy_version": valuation_version,
        "lot_sizes": lot_sizes,
        "symbol_order": symbol_order,
        "history_requirements": locked["history_requirements"],
        "scoring_start": scoring_start,
        "decision_policy": decision_policy,
    }
    strategy = _strategy_from_locked(
        locked["strategy"], locked["strategy_executor"], plugin_registry=plugin_registry,
    )
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
        valuation_policy_version=valuation_version,
        lot_sizes=lot_sizes,
        symbol_order=symbol_order,
        history_requirements=[],
        scoring_start=scoring_start,
        decision_policy=decision_policy,
    )
    return {
        "candidate": candidate,
        "benchmark": benchmark,
        "expected_sessions": expected,
        "scoring_start": scoring_start,
        "data": data,
        # The #56 identity lives beside the snapshot block, never inside it:
        # ``checks.data_availability.snapshot`` keeps its exact pre-#56 shape.
        "snapshot_schema_version": receipt["schema_version"],
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
    *,
    plugin_registry: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    evaluation = campaign["evaluation"]
    source = evidence if evidence is not None else {
        "snapshot_id": evaluation["snapshot_id"],
        "partition_content_hash": evaluation["partition_content_hash"],
        "data_kind": evaluation["data_kind"],
    }
    runs = _evidence_runs(
        snapshot_store, record, submission, campaign, source, plugin_registry=plugin_registry,
    )
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


def _ending_cash(run: Mapping[str, Any]) -> float | None:
    curve = run.get("equity_curve") or []
    return curve[-1]["cash"] if curve else None


def _run_summary(run: Mapping[str, Any], *, first_decision: bool = False) -> dict[str, Any]:
    metrics = run["metrics"]
    summary: dict[str, Any] = {
        "start_date": run["start_date"],
        "end_date": run["end_date"],
        "sessions": metrics["sessions"],
        "valued_sessions": metrics["valued_sessions"],
        "fills": metrics["fills"],
        "ending_equity": metrics["ending_equity"],
        "ending_cash": _ending_cash(run),
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
    summary = {
        "state": evidence["state"],
        "invested_sessions": evidence["invested_sessions"],
        "first_position_signal_date": evidence["first_position_signal_date"],
        "first_fill_date": evidence["first_fill_date"],
        "fills": run["metrics"]["fills"],
        "unfilled_orders": run["metrics"]["orders_unfilled"],
        "intentional_cash_sessions": list(readiness.get("ready_cash_sessions", [])),
    }
    # Additive #56 basket-build disclosure, published only when the run carries
    # it, so every legacy position-evidence shape stays byte-identical.
    for key in ("basket_build_state", "unfilled_basket_signals"):
        if key in evidence:
            summary[key] = evidence[key]
    return summary


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
        # Fills and cash must match Discovery, not only ending equity (spec #43
        # L56): two runs can agree on NAV while holding different cash.
        if not _within(_ending_cash(run), _ending_cash(original)):
            mismatches.append(f"{label}.ending_cash")
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
    record = {
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
    if "valuation_summary" in run:
        record["valuation_summary"] = dict(run["valuation_summary"])
    return record


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


def _scoring_start_check(
    candidate: Mapping[str, Any], benchmark: Mapping[str, Any], scoring_start: str,
) -> dict[str, Any]:
    readiness = candidate.get("history_readiness") or {}
    first_scored_open = readiness.get("first_scored_open")
    return {
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


def _coverage_check(
    candidate: Mapping[str, Any],
    benchmark: Mapping[str, Any],
    scoring_start: str,
    expected: list[str],
) -> dict[str, Any]:
    expected_end = expected[-1] if expected else None

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

    return {
        "ok": covered(candidate) and covered(benchmark),
        "expected": {
            "start": scoring_start,
            "end": expected_end,
            "sessions": len(expected),
        },
        "candidate": covered_summary(candidate),
        "benchmark": covered_summary(benchmark),
    }


def _data_availability_check(
    candidate: Mapping[str, Any],
    benchmark: Mapping[str, Any],
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    candidate_valuation = candidate["gaps"]["valuation"]
    benchmark_valuation = benchmark["gaps"]["valuation"]
    return {
        "ok": not (
            candidate["signal_gaps"]
            or candidate_valuation
            or benchmark["signal_gaps"]
            or benchmark_valuation
        ),
        "snapshot": snapshot,
        "candidate": {
            "signal_gaps": _gap_summary(candidate["signal_gaps"]),
            "valuation_gaps": _gap_summary(candidate_valuation),
        },
        "benchmark": {
            "signal_gaps": _gap_summary(benchmark["signal_gaps"]),
            "valuation_gaps": _gap_summary(benchmark_valuation),
        },
    }


def _position_check(
    candidate: Mapping[str, Any], benchmark: Mapping[str, Any],
) -> dict[str, Any]:
    """The formal position prerequisite for both sides of the frozen comparison.

    A side satisfies it when its position evidence state targets a real
    position or explicit cash *and* its latest basket build is not still
    unfinished: a plan that required the whole basket and filled only part of it
    (``partial``) or none of it (``unfilled``) leaves the scored premise unmet
    even though shares were bought.  A run without a build state (the legacy
    execution rules) is judged by its position state alone, exactly as before.
    """
    def satisfied(run: Mapping[str, Any]) -> bool:
        evidence = run["position_evidence"]
        if evidence["state"] not in _POSITION_STATES:
            return False
        build_state = evidence.get("basket_build_state")
        return build_state is None or build_state not in _UNFINISHED_BUILD_STATES

    return {
        "ok": satisfied(candidate) and satisfied(benchmark),
        "candidate": _position_summary(candidate),
        "benchmark": _position_summary(benchmark),
    }


def _receipt(
    record: Mapping[str, Any],
    submission: Mapping[str, Any],
    *,
    validation_id: str,
    replay: Mapping[str, Any],
    recomputed: Mapping[str, Any] | None = None,
    corrected: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    kickoff = record["kickoff"]
    expected = replay["expected_sessions"]
    scoring_start = replay["scoring_start"]
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

    scoring_start_check = _scoring_start_check(candidate, benchmark, scoring_start)
    coverage = _coverage_check(candidate, benchmark, scoring_start, expected)
    data_availability = _data_availability_check(candidate, benchmark, replay["snapshot"])
    position = _position_check(candidate, benchmark)
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
        "opened_protected_source": False,
        "query_debited": False,
    }
    # Additive #56/#68 identity, published only for the opt-in protocols: a
    # schema-3 snapshot, the actual-open execution rule, or a non-legacy
    # valuation policy.  A legacy schema-1/2 v1 receipt under legacy rules
    # keeps its exact old keys and receipt hash.
    snapshot_schema_version = replay["snapshot_schema_version"]
    execution_rule_version = submission["locked"]["execution_rules"].get(
        "version", NEXT_OPEN_RULES
    )
    valuation_version = valuation_policy_version(submission["locked"]["execution_rules"])
    decision_policy = submission["locked"].get("decision_policy")
    if (
        (type(snapshot_schema_version) is int and snapshot_schema_version >= 3)
        or execution_rule_version in (ACTUAL_OPEN_RULES, ACTUAL_OPEN_LOT_RULES)
        or valuation_version != STRICT_CLOSE_VALUATION
        or decision_policy is not None
    ):
        provenance = {
            "snapshot_id": replay["snapshot"]["snapshot_id"],
            "snapshot_schema_version": snapshot_schema_version,
            "execution_rule_version": execution_rule_version,
            "price_basis": EXECUTION_PRICE_BASIS,
        }
        if execution_rule_version == ACTUAL_OPEN_LOT_RULES:
            exec_rules = submission["locked"]["execution_rules"].get("execution", {})
            if "lot_sizes" in exec_rules:
                provenance["lot_sizes"] = dict(exec_rules["lot_sizes"])
            if "daily_simulation_assumptions" in exec_rules:
                provenance["daily_simulation_assumptions"] = dict(
                    exec_rules["daily_simulation_assumptions"]
                )
        if valuation_version != STRICT_CLOSE_VALUATION:
            provenance["valuation_policy_version"] = valuation_version
        if decision_policy is not None:
            provenance["decision_policy"] = decision_policy
        receipt["provenance"] = provenance
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
# Formal trailing-segment Validation (protocol v2)
# ---------------------------------------------------------------------------


def _formal_split(record: Mapping[str, Any]) -> dict[str, Any]:
    return record["kickoff"]["research_partition"]


def _trailing_gaps(
    metadata: Mapping[str, Any],
    start: str,
    end: str,
    explained: Mapping[str, Collection[tuple[str, object]]] | None = None,
) -> dict[str, list[Any]]:
    """The sealed manifest's per-kind gaps inside ``[start, end]``.

    Metadata only: the evidence-classified gate and the legacy gaps-only gate
    judge exactly this range-filtered projection.  ``explained`` maps each gap
    kind to the ``(date, symbol)`` pairs that kind's evidence covers, so a
    benign row explains only the fields it proves; without it every entry is
    judged.
    """
    explained_by_kind = explained or {}
    return {
        kind: [
            entry for entry in metadata.get("gaps", {}).get(kind, [])
            if isinstance(entry, Mapping)
            and start <= str(entry.get("date")) <= end
            and (str(entry.get("date")), entry.get("symbol"))
            not in explained_by_kind.get(kind, ())
        ]
        for kind in _GAP_KINDS
    }


def _formal_execution_evidence_gate(
    metadata: Mapping[str, Any],
    evidence: Mapping[str, Any],
    trailing: Mapping[str, Any],
    allowance: Mapping[str, Any] | None,
) -> ProtocolError | None:
    """Classify the trailing segment from sealed execution evidence only.

    Metadata-only, like the legacy gap check: it never opens the Discovery
    payload and runs before the counted reservation.  A benign row explains
    only the gap kinds its evidence covers -- ``actual_open_after_halt`` the
    ``raw_bars`` gap, ``confirmed_no_trade`` the ``raw_bars`` and ``derived``
    gaps -- so a trusted halt open can never mask a same-day availability or
    corporate-action gap.  The three blocking classifications reject with the
    sealed row itself (their own gap entries are suppressed only so they are
    not reported twice).  A trailing gap whose ``(date, symbol)`` no evidence
    row mentions for that kind is still unexplained and blocks, so a v3
    manifest cannot silently skip an unclassified hole.
    """
    start, end = trailing["start"], trailing["end"]
    sealed_rows = evidence.get("rows")
    rows = (
        [
            dict(row)
            for row in sealed_rows
            if isinstance(row, Mapping) and start <= str(row.get("date")) <= end
        ]
        if isinstance(sealed_rows, list)
        else []
    )
    benign = [
        row for row in rows
        if row.get("classification") in _EXECUTION_EVIDENCE_BENIGN
    ]
    blocking = [
        row for row in rows
        if row.get("classification") in _EXECUTION_EVIDENCE_BLOCKING
    ]
    explained: dict[str, set[tuple[str, object]]] = {
        kind: set() for kind in _GAP_KINDS
    }
    for row in benign:
        pair = (str(row.get("date")), row.get("symbol"))
        for kind in _EXECUTION_EVIDENCE_EXPLAINS.get(row.get("classification"), ()):
            explained[kind].add(pair)
    for row in blocking:
        pair = (str(row.get("date")), row.get("symbol"))
        for kind in _GAP_KINDS:
            explained[kind].add(pair)
    gaps = _trailing_gaps(metadata, start, end, explained)
    unexplained = {kind: entries for kind, entries in gaps.items() if entries}
    if not blocking and not unexplained:
        return None
    if blocking:
        first = min(
            blocking,
            key=lambda row: (str(row.get("date")), str(row.get("symbol"))),
        )
        message = (
            "The sealed execution evidence blocks the trailing segment: "
            f"{first.get('date')} {first.get('symbol')} is "
            f"{first.get('classification')}; a counted trailing read cannot be "
            "honest until it is corrected."
        )
    else:
        kind, entries = next(
            (kind, entries) for kind, entries in unexplained.items() if entries
        )
        first = entries[0]
        message = (
            "The sealed manifest declares unexplained gaps inside the trailing "
            f"segment: {kind} {first.get('date')} {first.get('symbol')}; a "
            "counted trailing read cannot be honest until they are corrected."
        )
    return _rejected(
        "VALIDATION_DATA_GAP", "validation", "inspect",
        message,
        counted=False,
        allowance=dict(allowance) if allowance is not None else None,
        execution_evidence=blocking,
        gaps=unexplained,
        trailing=dict(trailing),
    )


def _formal_discovery_precheck(
    project: Path,
    record: Mapping[str, Any],
    submission: Mapping[str, Any],
    campaign: Mapping[str, Any],
    allowance: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Verify the locked Discovery receipt and frozen scoring premises before use."""
    def reject_binding(message: str) -> None:
        raise _rejected(
            "VALIDATION_EVIDENCE_MISMATCH", "validation", "inspect", message,
            counted=False,
            allowance=dict(allowance) if allowance is not None else None,
            outcome="rejected",
            reason="VALIDATION_EVIDENCE_MISMATCH",
            precheck={"status": "rejected", "reason": "EVIDENCE_MISMATCH"},
        )

    experiment = next(
        (
            item
            for item in campaign_experiment_history(project, record["campaign_id"])
            if item["experiment_id"] == submission["experiment_id"]
        ),
        None,
    )
    if experiment is None or experiment.get("status") != "succeeded":
        reject_binding("The locked version has no successful Discovery receipt.")
    assert experiment is not None

    kickoff = record["kickoff"]
    locked = submission["locked"]
    frozen_campaign = kickoff["frozen_campaign"]
    split = _formal_split(record)
    development = split["development"]
    scoring_policy = kickoff["scoring_policy"]["frozen"]
    discovery = experiment.get("discovery")
    quarterly = experiment.get("quarterly_summary")
    if not isinstance(discovery, Mapping) or not isinstance(quarterly, Mapping):
        reject_binding("The locked Discovery receipt lacks its quarterly summary.")
    baseline_comparison = discovery.get("baseline_comparison")
    baseline = (
        baseline_comparison.get("baseline")
        if isinstance(baseline_comparison, Mapping) else None
    )
    identity = quarterly.get("identity")
    fingerprints = experiment.get("fingerprints")
    if (
        not isinstance(baseline, Mapping)
        or not isinstance(identity, Mapping)
        or not isinstance(fingerprints, Mapping)
    ):
        reject_binding("The locked Discovery receipt is incomplete.")

    expected_end = development["end"]
    as_of = split.get("as_of")
    if isinstance(as_of, str):
        expected_end = min(expected_end, as_of)
    expected_identity = {
        "experiment_id": submission["experiment_id"],
        "submission_id": submission["experiment_submission_id"],
        "campaign_id": record["campaign_id"],
        "strategy": {
            "family": locked["strategy"]["family"],
            "parameters": locked["strategy"]["parameters"],
        },
        "snapshot_id": locked["snapshot_id"],
        "fingerprints": dict(fingerprints),
        "interval": {
            "start_date": scoring_policy["scoring_start"],
            "end_date": expected_end,
        },
        "execution_rules": locked["execution_rules"],
        "evaluation_protocol": locked["evaluation_hash"],
    }
    expected_protocol_hash = _protocol_hash(kickoff)
    expected_base_costs = {
        key: frozen_campaign[key]
        for key in ("initial_cash", "transaction_cost_bps", "slippage_bps")
    }
    if (
        experiment.get("campaign_id") != record["campaign_id"]
        or experiment.get("submission_id") != submission["experiment_submission_id"]
        or identity != expected_identity
        or fingerprints.get("strategy_and_parameters") != submission["strategy_fingerprint"]
        or fingerprints.get("snapshot") != locked["snapshot_hash"]
        or fingerprints.get("partition") != frozen_campaign["partition_content_hash"]
        or fingerprints.get("evaluation") != frozen_campaign["campaign_evaluation_hash"]
        or locked["snapshot_id"] != frozen_campaign["snapshot_id"]
        or locked["snapshot_hash"] != development["content_hash"]
        or locked["evaluation_hash"] != frozen_campaign["campaign_evaluation_hash"]
        or locked["execution_rules"] != frozen_campaign["execution_rules"]
        or locked["base_costs"] != expected_base_costs
        or locked["history_policy"] != scoring_policy
        or locked["protocol_hash"] != expected_protocol_hash
        or locked.get("protocol") != kickoff.get("protocol")
        or locked.get("research_partition") != split
        or campaign["snapshot_id"] != frozen_campaign["snapshot_id"]
        or campaign["campaign_evaluation_hash"] != frozen_campaign["campaign_evaluation_hash"]
        or campaign["evaluation"]["execution_rules"] != frozen_campaign["execution_rules"]
    ):
        reject_binding("The Discovery summary does not match the locked version and round.")

    try:
        expected_quarterly = summarize_discovery_quarters(
            discovery, baseline, identity=identity,
        )
    except (KeyError, TypeError, ValueError):
        reject_binding("The Discovery quarterly summary cannot be verified.")
    if expected_quarterly != quarterly:
        reject_binding("The Discovery quarterly summary does not match its ledger receipt.")

    candidate_metrics = _metrics(discovery)
    hard_conditions = [
        _hard_condition(condition, candidate_metrics)
        for condition in kickoff["hard_conditions"]
    ]
    expected_start = scoring_policy["scoring_start"]
    expected_sessions = max(
        0, development["sessions"] - scoring_policy["warmup_sessions"],
    )
    expected_dates = [expected_end] * expected_sessions
    scoring_start = _scoring_start_check(discovery, baseline, expected_start)
    coverage = _coverage_check(
        discovery, baseline, expected_start, expected_dates,
    )
    snapshot_evidence = {
        "verified": True,
        "snapshot_id": locked["snapshot_id"],
        "partition_content_hash": frozen_campaign["partition_content_hash"],
        "prior_history_hash": scoring_policy.get("prior_history_hash"),
        "data_kind": frozen_campaign["data_kind"],
        "scope": frozen_campaign["data_kind"],
        "as_of": as_of,
    }
    data_availability = _data_availability_check(
        discovery, baseline, snapshot_evidence,
    )
    position = _position_check(discovery, baseline)
    prerequisites = {
        "scoring_start": scoring_start,
        "coverage": coverage,
        "data_availability": data_availability,
        "position": position,
    }
    hard_failed = any(item["status"] == "failed" for item in hard_conditions)
    hard_unavailable = any(item["status"] == "unavailable" for item in hard_conditions)
    hard_metric_names = {
        condition["metric"] for condition in kickoff["hard_conditions"]
    }
    unavailable_disclosures = [
        metric
        for metric, field in METRIC_REGISTRY.items()
        if metric not in hard_metric_names and candidate_metrics.get(field) is None
    ]
    precheck = {
        "status": "eligible",
        "quarterly_summary_hash": _hash(quarterly),
        "hard_conditions": hard_conditions,
        "scoring_prerequisites": prerequisites,
        "disclosure_only_metrics_unavailable": unavailable_disclosures,
    }
    if hard_failed:
        precheck["status"] = "rejected"
        raise _rejected(
            "VALIDATION_PRECHECK_REJECTED", "validation", "inspect",
            "A frozen hard condition already fails on the trusted Discovery evidence.",
            counted=False,
            allowance=dict(allowance) if allowance is not None else None,
            outcome="rejected",
            reason="HARD_CONDITION_NOT_MET",
            precheck=precheck,
        )
    if hard_unavailable or any(not item["ok"] for item in prerequisites.values()):
        precheck["status"] = "insufficient_evidence"
        reason = "METRICS_NOT_READY" if hard_unavailable else "SCORING_PREREQUISITES_NOT_MET"
        raise _rejected(
            "VALIDATION_PRECHECK_INSUFFICIENT", "validation", "inspect",
            "The trusted Discovery evidence cannot satisfy the frozen scoring premises.",
            counted=False,
            allowance=dict(allowance) if allowance is not None else None,
            outcome="insufficient_evidence",
            reason=reason,
            precheck=precheck,
        )
    return precheck


def _formal_manifest_gate(
    snapshot_store: Path,
    record: Mapping[str, Any],
    campaign: Mapping[str, Any],
    allowance: Mapping[str, Any] | None,
) -> None:
    """Block a known-bad trailing read from sealed manifest metadata only.

    This runs before the counted reservation, so a blocked read has zero uses.
    It never opens the Discovery payload and deliberately does not judge the
    strategy's history sufficiency: a shortfall discovered by the run is an
    ``insufficient_evidence`` verdict and keeps its counted use.
    """
    split = _formal_split(record)
    evaluation = campaign["evaluation"]
    metadata = snapshot.manifest(Path(snapshot_store), evaluation["snapshot_id"])
    descriptor = snapshot.partition_descriptor(metadata, "discovery")
    frozen, trailing = split["partition"], split["trailing"]
    if split["as_of"] is None:
        interval_ok = (
            frozen["start"] == descriptor["start"]
            and frozen["end"] == descriptor["end"]
            and frozen["sessions"] == descriptor["sessions"]
            and frozen["content_hash"] == descriptor["content_hash"]
        )
    else:
        # An as-of projection may be a strict prefix of the sealed interval.
        interval_ok = (
            frozen["start"] >= descriptor["start"]
            and frozen["end"] <= descriptor["end"]
            and frozen["sessions"] <= descriptor["sessions"]
        )
    if not interval_ok:
        raise _rejected(
            "VALIDATION_DATA_GAP", "validation", "inspect",
            "The frozen research interval does not match the sealed Discovery "
            "partition; the trailing documents cannot be scored as frozen.",
            counted=False,
            allowance=dict(allowance) if allowance is not None else None,
            frozen={"start": frozen["start"], "end": frozen["end"],
                    "sessions": frozen["sessions"]},
            manifest={"start": descriptor["start"], "end": descriptor["end"],
                      "sessions": descriptor["sessions"]},
            trailing=dict(trailing),
        )
    schema_version = metadata.get("schema_version")
    if type(schema_version) is int and schema_version >= 3:
        evidence = metadata.get("execution_evidence")
        policy = evidence.get("policy") if isinstance(evidence, Mapping) else None
        version = policy.get("classification_version") if isinstance(policy, Mapping) else None
        required_version = snapshot.EXECUTION_EVIDENCE_CLASSIFICATION_VERSION
        if type(version) is not int or version != required_version:
            raise _rejected(
                "VALIDATION_DATA_GAP", "validation", "inspect",
                "The sealed snapshot lacks the required execution evidence classification. "
                "Re-seal the input and bind a Campaign to the new snapshot.",
                counted=False,
                allowance=dict(allowance) if allowance is not None else None,
                snapshot_id=evaluation["snapshot_id"],
                required_classification_version=required_version,
                trailing=dict(trailing),
            )
        rejection = _formal_execution_evidence_gate(
            metadata, evidence, trailing, allowance
        )
        if rejection is not None:
            raise rejection
        return
    gaps = _trailing_gaps(metadata, trailing["start"], trailing["end"])
    blocked = {kind: entries for kind, entries in gaps.items() if entries}
    if blocked:
        raise _rejected(
            "VALIDATION_DATA_GAP", "validation", "inspect",
            "The sealed manifest declares gaps inside the trailing segment; a "
            "counted trailing read cannot be honest until they are corrected.",
            counted=False,
            allowance=dict(allowance) if allowance is not None else None,
            gaps=blocked,
            trailing=dict(trailing),
        )


def _formal_partition_mismatch(
    receipt: Mapping[str, Any],
    record: Mapping[str, Any],
    campaign: Mapping[str, Any],
    *,
    require_trailing: bool,
    frozen_read: bool = True,
) -> str | None:
    """Verify one sealed Discovery read against the frozen formal protocol.

    A corrected successor keeps the frozen development slice and session dates
    but legitimately changes the trailing rows and the full partition hash, so
    the frozen-hash checks apply only to the formal Validation read itself.
    """
    split = _formal_split(record)
    evaluation = campaign["evaluation"]
    frozen = record["kickoff"]["scoring_policy"]["frozen"]
    if receipt["data_kind"] != evaluation["data_kind"]:
        return "The sealed Discovery payload data kind differs from the Campaign."
    if frozen_read:
        if receipt.get("partition_content_hash", receipt["content_hash"]) != (
            evaluation["partition_content_hash"]
        ):
            return "The sealed Discovery partition hash differs from the Campaign."
        if receipt["content_hash"] != split["partition"]["content_hash"]:
            return "The sealed Discovery view differs from the frozen as-of projection."
    if receipt.get("prior_history_hash") != frozen.get("prior_history_hash"):
        return "The sealed Discovery prior history differs from the frozen evidence."
    content = receipt["data"]
    if require_trailing:
        try:
            snapshot.verify_discovery_split(split, content=content)
        except ValueError as error:
            return f"The frozen Discovery split does not reproduce: {error}"
    development = split["development"]
    sliced = snapshot.partition_slice(content, development["start"], development["end"])
    if sliced != development:
        return "The frozen development slice does not reproduce from the sealed payload."
    trailing = snapshot.partition_slice(
        content, split["trailing"]["start"], split["trailing"]["end"]
    )
    if (
        trailing["start"] != split["trailing"]["start"]
        or trailing["end"] != split["trailing"]["end"]
        or trailing["sessions"] != split["trailing"]["sessions"]
    ):
        return "The frozen trailing session dates changed in the sealed payload."
    return None


def _formal_snapshot_record(
    receipt: Mapping[str, Any],
    record: Mapping[str, Any],
    campaign: Mapping[str, Any],
    *,
    snapshot_id: str | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    evaluation = campaign["evaluation"]
    return {
        "verified": True,
        "snapshot_id": snapshot_id or evaluation["snapshot_id"],
        "partition_content_hash": receipt.get(
            "partition_content_hash", receipt["content_hash"]
        ),
        "as_of": as_of,
        "prior_history_hash": receipt.get("prior_history_hash"),
        "data_kind": receipt["data_kind"],
        "scope": evaluation["data_kind"],
    }


def _formal_segment_record(
    receipt: Mapping[str, Any], record: Mapping[str, Any], campaign: Mapping[str, Any],
) -> dict[str, Any]:
    split = _formal_split(record)
    return {
        "ok": True,
        "validation_kind": VALIDATION_KIND_TRAILING,
        "split_hash": split["split_hash"],
        "boundary": split["boundary"],
        "as_of": split["as_of"],
        "data_kind": receipt["data_kind"],
        "partition_content_hash": receipt.get(
            "partition_content_hash", receipt["content_hash"]
        ),
        "frozen_partition_content_hash": campaign["evaluation"]["partition_content_hash"],
        "prior_history_hash": receipt.get("prior_history_hash"),
        "research_interval": dict(split["partition"]),
        "development": dict(split["development"]),
        "trailing": dict(split["trailing"]),
        "verified": True,
    }


def _formal_runs(
    snapshot_store: Path,
    record: Mapping[str, Any],
    submission: Mapping[str, Any],
    campaign: Mapping[str, Any],
    *,
    receipt: Mapping[str, Any] | None = None,
    require_trailing: bool = True,
    phase: str = "validation",
    read_as_of: str | None = None,
    frozen_read: bool = True,
    snapshot_id: str | None = None,
    plugin_registry: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Score the locked candidate and benchmark on the trailing segment only."""
    split = _formal_split(record)
    evaluation = campaign["evaluation"]
    source_id = snapshot_id or evaluation["snapshot_id"]
    if receipt is None:
        try:
            receipt = snapshot.read_discovery(
                Path(snapshot_store),
                source_id,
                "discovery",
                split["as_of"] if frozen_read else read_as_of,
                include_trailing=True,
            )
        except (OSError, ValueError) as error:
            raise _rejected(
                "VALIDATION_PARTITION_MISMATCH", phase, "inspect",
                f"The sealed Discovery payload cannot be read at the frozen "
                f"as-of cutoff: {error}",
                counted=True,
            ) from error
    mismatch = _formal_partition_mismatch(
        receipt,
        record,
        campaign,
        require_trailing=require_trailing,
        frozen_read=frozen_read,
    )
    if mismatch is not None:
        raise _rejected(
            "VALIDATION_PARTITION_MISMATCH", phase, "inspect", mismatch, counted=True,
        )
    data = receipt["data"]
    trailing = split["trailing"]
    sessions = sorted({row["date"] for row in data["calendar"]})
    scoring_start = trailing["start"]
    expected = [
        day for day in sessions if trailing["start"] <= day <= trailing["end"]
    ]
    if (
        not expected
        or expected[0] != trailing["start"]
        or expected[-1] != trailing["end"]
        or len(expected) != trailing["sessions"]
    ):
        raise _rejected(
            "VALIDATION_PARTITION_MISMATCH", phase, "inspect",
            "The frozen trailing session dates are not the sealed scoring window.",
            counted=True,
        )
    locked = submission["locked"]
    prior = receipt.get("prior_history")
    prior_history = (prior["sessions"], prior["derived"]) if prior is not None else None
    rules = locked["execution_rules"]
    rule_version = rules.get("version", NEXT_OPEN_RULES)
    valuation_version = valuation_policy_version(rules)
    decision_policy = locked.get("decision_policy")
    lot_sizes = rules.get("execution", {}).get("lot_sizes")
    symbol_order = rules.get("execution", {}).get("symbol_order")
    options = {
        "data_kind": evaluation["data_kind"],
        "signal_window_sessions": locked["signal_window_sessions"],
        "prior_history": prior_history,
        "initial_cash": locked["base_costs"]["initial_cash"],
        "transaction_cost_bps": locked["base_costs"]["transaction_cost_bps"],
        "slippage_bps": locked["base_costs"]["slippage_bps"],
        "execution_rule_version": rule_version,
        "valuation_policy_version": valuation_version,
        "lot_sizes": lot_sizes,
        "symbol_order": symbol_order,
        "history_requirements": list(locked["history_requirements"] or []),
        "scoring_start": scoring_start,
        "decision_policy": decision_policy,
    }
    strategy = _strategy_from_locked(
        locked["strategy"], locked["strategy_executor"], plugin_registry=plugin_registry,
    )
    candidate = run_partition_strategy(
        data,
        evaluation["snapshot_id"],
        evaluation["partition_content_hash"],
        strategy,
        partition_name="discovery",
        **options,
    )
    benchmark = run_discovery_equal_weight(
        data,
        evaluation["snapshot_id"],
        evaluation["partition_content_hash"],
        data_kind=evaluation["data_kind"],
        initial_cash=locked["base_costs"]["initial_cash"],
        transaction_cost_bps=locked["base_costs"]["transaction_cost_bps"],
        slippage_bps=locked["base_costs"]["slippage_bps"],
        execution_rule_version=rule_version,
        valuation_policy_version=valuation_version,
        lot_sizes=lot_sizes,
        symbol_order=symbol_order,
        history_requirements=[],
        scoring_start=scoring_start,
        decision_policy=decision_policy,
    )
    readiness = candidate.get("history_readiness") or {}
    warmup = {
        "source": (
            "sealed_prior_history+development" if prior is not None else "development"
        ),
        "start": split["development"]["start"],
        "end": split["development"]["end"],
        "sessions": split["development"]["sessions"],
        "prior_history_hash": receipt.get("prior_history_hash"),
        "first_ready_date": readiness.get("first_ready_date"),
    }
    return {
        "candidate": candidate,
        "benchmark": benchmark,
        "expected_sessions": expected,
        "scoring_start": scoring_start,
        "data": data,
        "receipt": receipt,
        # The #56 identity lives beside the snapshot block, never inside it:
        # ``checks.data_availability.snapshot`` keeps its exact pre-#56 shape.
        "snapshot_schema_version": receipt["schema_version"],
        "snapshot": _formal_snapshot_record(
            receipt,
            record,
            campaign,
            snapshot_id=source_id,
            as_of=split["as_of"] if frozen_read else read_as_of,
        ),
        "segment": _formal_segment_record(receipt, record, campaign),
        "warmup": warmup,
        "as_of": split["as_of"] if frozen_read else read_as_of,
    }


def _formal_run_record(run: Mapping[str, Any]) -> dict[str, Any]:
    """A formal run record: the #43 record plus its frozen cash basis."""
    return {**_run_record(run), "initial_cash": run["initial_cash"]}


def _formal_verdict(checks: Mapping[str, Any]) -> tuple[str, str | None]:
    """The formal protocol's verdict precedence (design contract §4)."""
    if any(item["status"] == "failed" for item in checks["hard_conditions"]):
        return "failed", "HARD_CONDITION_NOT_MET"
    if not checks["scoring_start"]["ok"]:
        return "insufficient_evidence", "SCORING_START_INSUFFICIENT"
    if not checks["coverage"]["ok"] or not checks["data_availability"]["ok"]:
        return "insufficient_evidence", "COVERAGE_INSUFFICIENT"
    if not checks["position"]["ok"]:
        return "insufficient_evidence", "POSITION_EVIDENCE_INSUFFICIENT"
    if any(item["status"] == "unavailable" for item in checks["hard_conditions"]):
        return "insufficient_evidence", "METRICS_NOT_READY"
    return "passed", None


def _formal_summary(
    candidate_metrics: Mapping[str, Any],
    benchmark_metrics: Mapping[str, Any],
    *,
    scoring: Mapping[str, Any],
    outcome: str,
    reason: str | None,
) -> dict[str, Any]:
    def side(metrics: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "total_return": metrics["total_return"],
            "max_drawdown": metrics["max_drawdown"],
            "fees_paid": metrics["fees_paid"],
            "slippage_paid": metrics["slippage_paid"],
            "turnover": metrics["turnover"],
            "fills": metrics["fills"],
            "orders_filled": metrics["orders_filled"],
            "orders_unfilled": metrics["orders_unfilled"],
            "ending_equity": metrics["ending_equity"],
        }

    def delta(key: str) -> float | None:
        left, right = candidate_metrics[key], benchmark_metrics[key]
        if left is None or right is None:
            return None
        return float(left) - float(right)

    return {
        "scoring": dict(scoring),
        "candidate": side(candidate_metrics),
        "benchmark": side(benchmark_metrics),
        "delta": {
            "total_return": delta("total_return"),
            "max_drawdown": delta("max_drawdown"),
        },
        "verdict": {"outcome": outcome, "reason": reason},
    }


def _formal_usage(use: Mapping[str, Any]) -> dict[str, Any]:
    """The immutable, deterministic usage block of a formal receipt."""
    return {
        "kind": use["kind"],
        "admission_id": use["admission_id"],
        "sequence": use["sequence"],
        "use_id": use["use_id"],
    }


def _formal_receipt(
    record: Mapping[str, Any],
    submission: Mapping[str, Any],
    *,
    validation_id: str,
    runs: Mapping[str, Any],
    use: Mapping[str, Any],
    discovery_precheck: Mapping[str, Any] | None = None,
    corrected: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    kickoff = record["kickoff"]
    split = _formal_split(record)
    evaluation = record["kickoff"]["frozen_campaign"]
    candidate = runs["candidate"]
    benchmark = runs["benchmark"]
    candidate_metrics = _metrics(candidate)
    benchmark_metrics = _metrics(benchmark)
    expected = runs["expected_sessions"]
    checks = {
        "data_availability": _data_availability_check(
            candidate, benchmark, runs["snapshot"]
        ),
        "scoring_start": _scoring_start_check(
            candidate, benchmark, runs["scoring_start"]
        ),
        "coverage": _coverage_check(
            candidate, benchmark, runs["scoring_start"], expected
        ),
        "position": _position_check(candidate, benchmark),
        "segment": dict(runs["segment"]),
        "hard_conditions": [
            _hard_condition(condition, candidate_metrics)
            for condition in kickoff["hard_conditions"]
        ],
    }
    observation = observation_recommendation(
        candidate, kickoff["observation"]["minimum_opportunities"]
    )
    observation["review_conditions"] = kickoff["observation"]["review_conditions"]
    outcome, reason = _formal_verdict(checks)
    scoring = {
        "start": runs["scoring_start"],
        "end": split["trailing"]["end"],
        "sessions": len(expected),
        "valued_sessions": candidate_metrics["valued_sessions"],
    }
    valuation_version = valuation_policy_version(submission["locked"]["execution_rules"])
    decision_policy = submission["locked"].get("decision_policy")
    provenance = {
        "snapshot_id": runs["snapshot"]["snapshot_id"],
        "partition_content_hash": runs["snapshot"]["partition_content_hash"],
        "frozen_partition_content_hash": evaluation["partition_content_hash"],
        "as_of": runs["as_of"],
        "round_id": record["round_id"],
        "version_id": submission["version_id"],
        "submission_id": submission["submission_id"],
        "experiment_id": submission["experiment_id"],
        "development_content_hash": split["development"]["content_hash"],
        "trailing_content_hash": split["trailing"]["content_hash"],
        # Additive #56/#68 identity, flat like the merged v1 receipt: a reviewer
        # can read which snapshot contract, execution rule, and opt-in valuation
        # policy scored this.
        "snapshot_schema_version": runs["snapshot_schema_version"],
        "execution_rule_version": submission["locked"]["execution_rules"].get(
            "version", NEXT_OPEN_RULES
        ),
        "price_basis": EXECUTION_PRICE_BASIS,
        **(
            {
                "lot_sizes": dict(
                    submission["locked"]["execution_rules"].get("execution", {}).get("lot_sizes", {})
                ),
                "daily_simulation_assumptions": dict(
                    submission["locked"]["execution_rules"]
                    .get("execution", {})
                    .get("daily_simulation_assumptions", {"late_resume_sell_funds_same_day_buy": True})
                ),
            }
            if submission["locked"]["execution_rules"].get("version") == ACTUAL_OPEN_LOT_RULES
            else {}
        ),
        **(
            {"valuation_policy_version": valuation_version}
            if valuation_version != STRICT_CLOSE_VALUATION
            else {}
        ),
        **(
            {"decision_policy": decision_policy}
            if decision_policy is not None
            else {}
        ),
    }
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "round_id": record["round_id"],
        "version_id": submission["version_id"],
        "validation_id": validation_id,
        "status": "completed",
        "validation_kind": VALIDATION_KIND_TRAILING,
        "outcome": outcome,
        "reason": reason,
        "protocol": {
            **VALIDATION_PROTOCOL_TRAILING,
            "split_hash": split["split_hash"],
            "boundary": split["boundary"],
            "research_interval": {
                "start": split["partition"]["start"],
                "end": split["partition"]["end"],
                "sessions": split["partition"]["sessions"],
            },
            **(
                {"decision_policy": decision_policy}
                if decision_policy is not None
                else {}
            ),
        },
        "development": dict(split["development"]),
        "trailing": dict(split["trailing"]),
        "scoring": scoring,
        "warmup": dict(runs["warmup"]),
        "provenance": provenance,
        "usage": _formal_usage(use),
        "summary": _formal_summary(
            candidate_metrics, benchmark_metrics,
            scoring=scoring, outcome=outcome, reason=reason,
        ),
        "metrics": {"candidate": candidate_metrics, "benchmark": benchmark_metrics},
        "checks": checks,
        "runs": {
            "candidate": _formal_run_record(candidate),
            "benchmark": _formal_run_record(benchmark),
        },
        "observation": observation,
        # Trusted-only per-session evidence: the detail projection and a later
        # revalidation's affected diff read this, never the published receipt.
        "curves": {
            "candidate": list(candidate["equity_curve"]),
            "benchmark": list(benchmark["equity_curve"]),
        },
        "opened_protected_source": False,
        "query_debited": False,
    }
    if discovery_precheck is not None:
        receipt["discovery_precheck"] = dict(discovery_precheck)
    if corrected is not None:
        receipt.update(dict(corrected))
    receipt["receipt_hash"] = _hash(
        {key: value for key, value in receipt.items() if key != "receipt_hash"}
    )
    return receipt


def _formal_validation_record(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """One formal Validation epoch, addressable from a revalidation record."""
    return {
        "validation_id": receipt["validation_id"],
        "outcome": receipt["outcome"],
        "reason": receipt["reason"],
        "snapshot_id": receipt["provenance"]["snapshot_id"],
        "as_of": receipt["provenance"]["as_of"],
        "partition_content_hash": receipt["provenance"]["partition_content_hash"],
        "development": dict(receipt["development"]),
        "trailing": dict(receipt["trailing"]),
        "scoring": dict(receipt["scoring"]),
        "warmup": dict(receipt["warmup"]),
        "candidate": dict(receipt["runs"]["candidate"]),
        "benchmark": dict(receipt["runs"]["benchmark"]),
    }


def _prior_formal_runs(prior: Mapping[str, Any]) -> dict[str, Any]:
    """Rebuild the run container an affected diff needs from a formal receipt."""
    curves = prior.get("curves")
    if not isinstance(curves, Mapping):
        raise ValueError(
            "The prior formal Validation receipt has no per-session evidence."
        )
    runs = {}
    for side in ("candidate", "benchmark"):
        record = prior["runs"][side]
        runs[side] = {
            "equity_curve": list(curves[side]),
            "fills": list(record["fills"]),
            "metrics": dict(record["metrics"]),
        }
    return runs


def _formal_affected(
    prior: Mapping[str, Any],
    runs: Mapping[str, Any],
    original_content: Mapping[str, Any],
    record: Mapping[str, Any],
) -> dict[str, Any]:
    split = _formal_split(record)
    trailing = split["trailing"]
    return _affected(
        _prior_formal_runs(prior),
        runs,
        snapshot.slice_content(
            original_content, trailing["start"], trailing["end"]
        ),
        snapshot.slice_content(runs["data"], trailing["start"], trailing["end"]),
        runs["expected_sessions"],
    )


# ---------------------------------------------------------------------------
# Trusted validation copies and attempts
# ---------------------------------------------------------------------------


def _validation_path(store: Path, validation_id: str) -> Path:
    return store / VALIDATION_NAME / f"{_require_hex(validation_id, 'validation')}.json"


def _published(receipt: Mapping[str, Any]) -> dict[str, Any]:
    # Required legacy fields are published when present, so a formal receipt
    # (which has no ``replay``) and a legacy receipt share one allowlist.
    published = {
        field: receipt[field] for field in _RECEIPT_FIELDS if field in receipt
    }
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
) -> bool:
    """Write the immutable receipt; a retry recovers an identical existing one.

    Only deterministic evidence is stored, so any recomputation must reproduce
    the original content.
    Returns True when the receipt was already there (recovered, not rewritten).
    """
    path = _validation_path(store, validation_id)
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("status") == "completed":
            if existing != receipt:
                raise ValueError("A terminal research-loop Validation receipt cannot change.")
            return True
    _atomic_write(path, receipt)
    return False


def _load_trusted_receipt(
    store: Path, validation_id: str, published_json: str,
) -> dict[str, Any]:
    path = _validation_path(store, validation_id)
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError("The trusted research-loop Validation receipt is missing.") from error
    receipt = _check_terminal_receipt(receipt, validation_id)
    if _json(_published(receipt)) != published_json:
        raise ValueError("The trusted Validation receipt differs from the project copy.")
    return receipt


def _check_terminal_receipt(receipt: object, validation_id: str) -> dict[str, Any]:
    if not isinstance(receipt, dict) or receipt.get("status") != "completed":
        raise ValueError("The trusted research-loop Validation receipt is not terminal.")
    expected = _hash({key: value for key, value in receipt.items() if key != "receipt_hash"})
    if receipt.get("receipt_hash") != expected or receipt.get("validation_id") != validation_id:
        raise ValueError("The trusted research-loop Validation receipt failed its integrity check.")
    return receipt


def _recover_formal_terminal(store: Path, use: Mapping[str, Any]) -> dict[str, Any] | None:
    """Recover a trusted write that finished before the project publication."""
    validation_id = use["validation_id"]
    try:
        receipt = json.loads(_validation_path(store, validation_id).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if isinstance(receipt, dict) and receipt.get("status") == "running":
        return None
    receipt = _check_terminal_receipt(receipt, validation_id)
    if (
        receipt.get("round_id") != use["round_id"]
        or receipt.get("version_id") != use["version_id"]
        or receipt.get("validation_kind") != VALIDATION_KIND_TRAILING
        or receipt.get("usage") != _formal_usage(use)
    ):
        raise ValueError("The trusted Validation receipt differs from its reserved use.")
    return receipt


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


# ---------------------------------------------------------------------------
# Automatic Validation allowance and its atomic reservation
# ---------------------------------------------------------------------------


def _validation_use(row: sqlite3.Row) -> dict[str, Any]:
    return {field: row[field] for field in _VALIDATION_USE_FIELDS}


def _validation_uses(
    connection: sqlite3.Connection, round_id: str,
) -> list[dict[str, Any]]:
    return [
        _validation_use(row)
        for row in connection.execute(
            "SELECT use_id, round_id, version_id, sequence, kind, admission_id, "
            "validation_id, consumed_at FROM research_loop_validation_uses "
            "WHERE round_id = ? ORDER BY sequence",
            (round_id,),
        )
    ]


def _validation_allowance(
    connection: sqlite3.Connection, record: Mapping[str, Any],
) -> dict[str, Any] | None:
    """The protocol-v2 counter view; a protocol-v1 round has no allowance."""
    policy = record["kickoff"].get("validation_policy")
    if not isinstance(policy, Mapping):
        return None
    uses = _validation_uses(connection, record["round_id"])
    automatic = sum(1 for item in uses if item["kind"] == "automatic")
    manual = sum(1 for item in uses if item["kind"] == "manual")
    limit = policy["auto_validation_limit"]
    return {
        "auto_limit": limit,
        "automatic_used": automatic,
        "manual_used": manual,
        "total_used": len(uses),
        "remaining": max(0, limit - automatic),
    }


def _validation_allowance_status(
    connection: sqlite3.Connection, record: Mapping[str, Any],
) -> dict[str, Any] | None:
    allowance = _validation_allowance(connection, record)
    if allowance is None:
        return None
    return {
        **allowance,
        "uses": _validation_uses(connection, record["round_id"]),
    }


def _validation_use_for_version(
    connection: sqlite3.Connection, round_id: str, version_id: str,
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT use_id, round_id, version_id, sequence, kind, admission_id, "
        "validation_id, consumed_at FROM research_loop_validation_uses "
        "WHERE round_id = ? AND version_id = ?",
        (round_id, version_id),
    ).fetchone()
    return _validation_use(row) if row is not None else None


def _insert_validation_use(
    connection: sqlite3.Connection,
    record: Mapping[str, Any],
    submission: Mapping[str, Any],
    validation_id: str,
    *,
    kind: str,
    admission_id: str | None,
) -> dict[str, Any]:
    """Insert the next immutable use row for this version's one counted read."""
    sequence = connection.execute(
        "SELECT COALESCE(MAX(sequence), 0) + 1 FROM research_loop_validation_uses "
        "WHERE round_id = ?",
        (record["round_id"],),
    ).fetchone()[0]
    use = {
        "use_id": "",
        "round_id": record["round_id"],
        "version_id": submission["version_id"],
        "sequence": sequence,
        "kind": kind,
        "admission_id": admission_id,
        "validation_id": validation_id,
        "consumed_at": _now(),
    }
    use["use_id"] = _hash({field: use[field] for field in _VALIDATION_USE_FIELDS if field != "use_id"})
    connection.execute(
        "INSERT INTO research_loop_validation_uses "
        "(use_id, round_id, version_id, sequence, kind, admission_id, validation_id, "
        "consumed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            use["use_id"], use["round_id"], use["version_id"], use["sequence"],
            use["kind"], use["admission_id"], use["validation_id"], use["consumed_at"],
        ),
    )
    return use


def _reserve_validation_use(
    connection: sqlite3.Connection,
    record: Mapping[str, Any],
    submission: Mapping[str, Any],
    validation_id: str,
) -> dict[str, Any]:
    """Reserve this version's one counted trailing read, atomically.

    Caller holds the round lock and an open ``BEGIN IMMEDIATE`` transaction, so
    the existence check, the admission check, the allowance check and the insert
    are one serialized step: two processes racing the last slot or one manual
    admission yield exactly one row and exactly one read. The caller has
    already checked this version has no existing use. Consume an unused
    manual admission, take an automatic slot, or reject before the read.
    ``admit`` refuses a grant while automatic slots remain, so a manual row
    can only be the researcher's post-limit extra read.
    The manual branch still comes first defensively: a grant is explicit, so a
    row recorded before this gate (or by a future caller) is honoured as manual
    rather than silently consumed as an automatic slot.
    """
    admission = _admission_for_version(
        connection, record["round_id"], submission["version_id"]
    )
    if admission is not None:
        consumed = _admission_use(connection, admission["admission_id"])
        if consumed is not None:
            # Defensive crash/retry path: an admission already reflected by a
            # use row is reused, never consumed a second time.
            if consumed["version_id"] != submission["version_id"]:
                raise ValueError(
                    "The Validation admission was consumed by another version."
                )
            return consumed
        return _insert_validation_use(
            connection,
            record,
            submission,
            validation_id,
            kind="manual",
            admission_id=admission["admission_id"],
        )
    policy = record["kickoff"]["validation_policy"]
    limit = policy["auto_validation_limit"]
    automatic = connection.execute(
        "SELECT count(*) FROM research_loop_validation_uses "
        "WHERE round_id = ? AND kind = 'automatic'",
        (record["round_id"],),
    ).fetchone()[0]
    if automatic >= limit:
        raise _rejected(
            "VALIDATION_ALLOWANCE_EXHAUSTED", "validation", "admit",
            "This round has used every automatic trailing Validation slot; an "
            "operator must admit this version before it can be read.",
            allowance=_validation_allowance(connection, record),
            round_state="awaiting_validation_admission",
        )
    return _insert_validation_use(
        connection,
        record,
        submission,
        validation_id,
        kind="automatic",
        admission_id=None,
    )


def _lifecycle(
    connection: sqlite3.Connection,
    record: Mapping[str, Any],
    closure: Mapping[str, Any] | None,
) -> str:
    """The research-visible round lifecycle: converged / awaiting admission / research."""
    if closure is not None:
        return "converged"
    allowance = _validation_allowance(connection, record)
    if allowance is not None and allowance["automatic_used"] >= allowance["auto_limit"]:
        return "awaiting_validation_admission"
    return "research"


# ---------------------------------------------------------------------------
# Manual Validation admissions (the researcher's extra read after the limit)
# ---------------------------------------------------------------------------

_ADMISSION_IDENTITY_FIELDS = (
    "schema_version", "round_id", "version_id", "reviewer", "reason",
    "comparison", "selection_impact",
)


def _admission_path(store: Path, admission_id: str) -> Path:
    return (
        store
        / ADMISSION_NAME
        / f"{_require_hex(admission_id, 'admission')}.json"
    )


def _verify_admission(body: object) -> dict[str, Any]:
    """Load and content-check one write-once admission record.

    ``admission_id`` addresses the immutable decision body; ``granted_at`` is
    stored alongside it and is deliberately outside the content address, so a
    replay returns the originally recorded grant time.
    """
    if not isinstance(body, dict) or not set(_ADMISSION_IDENTITY_FIELDS) <= body.keys():
        raise ValueError("Invalid Validation admission record.")
    admission_id = body.get("admission_id")
    _require_hex(admission_id, "admission")
    if admission_id != _hash({field: body[field] for field in _ADMISSION_IDENTITY_FIELDS}):
        raise ValueError("Validation admission fingerprint mismatch.")
    _require_hex(body["round_id"], "research round")
    _require_hex(body["version_id"], "version")
    if not isinstance(body.get("granted_at"), str) or not body["granted_at"]:
        raise ValueError("Invalid Validation admission timestamp.")
    return body


def _admissions(
    connection: sqlite3.Connection, round_id: str,
) -> list[dict[str, Any]]:
    return [
        _verify_admission(json.loads(row["admission_json"]))
        for row in connection.execute(
            "SELECT admission_json FROM research_loop_admissions "
            "WHERE round_id = ? ORDER BY rowid",
            (round_id,),
        )
    ]


def _admission_for_version(
    connection: sqlite3.Connection, round_id: str, version_id: str,
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT admission_json FROM research_loop_admissions "
        "WHERE round_id = ? AND version_id = ?",
        (round_id, version_id),
    ).fetchone()
    return _verify_admission(json.loads(row["admission_json"])) if row is not None else None


def _trusted_admission_for_version(
    trusted: Path, round_id: str, version_id: str,
) -> dict[str, Any] | None:
    """Find a trusted admission a crashed grant wrote before its project row."""
    directory = trusted / ADMISSION_NAME
    if not directory.is_dir():
        return None
    for path in sorted(directory.glob("*.json")):
        try:
            body = _verify_admission(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeError, ValueError):
            continue
        if body["round_id"] == round_id and body["version_id"] == version_id:
            return body
    return None


def _admission_use(
    connection: sqlite3.Connection, admission_id: str,
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT use_id, round_id, version_id, sequence, kind, admission_id, "
        "validation_id, consumed_at FROM research_loop_validation_uses "
        "WHERE admission_id = ?",
        (admission_id,),
    ).fetchone()
    return _validation_use(row) if row is not None else None


def _admission_view(
    connection: sqlite3.Connection, body: Mapping[str, Any],
) -> dict[str, Any]:
    """The admission body plus its derived use state (never mutated in place)."""
    use = _admission_use(connection, body["admission_id"])
    return {
        **{
            field: body[field]
            for field in (
                "admission_id", "round_id", "version_id", "reviewer", "reason",
                "comparison", "selection_impact", "granted_at",
            )
        },
        "used": use is not None,
        "consumed_validation_id": use["validation_id"] if use is not None else None,
    }


def _admission_status_view(
    connection: sqlite3.Connection, body: Mapping[str, Any],
) -> dict[str, Any]:
    """The round-status projection: no human reviewer identity or reason (F8)."""
    view = _admission_view(connection, body)
    return {
        field: view[field]
        for field in (
            "admission_id", "round_id", "version_id", "granted_at", "comparison",
            "selection_impact", "used", "consumed_validation_id",
        )
    }


def _publish_admission(
    connection: sqlite3.Connection,
    trusted: Path,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    """Write the write-once trusted record, then its project projection."""
    from prajnatrade.protected_eval import write_trusted_record_once

    path = _admission_path(trusted, body["admission_id"])
    stored = dict(body)
    if path.is_file():
        existing = _verify_admission(json.loads(path.read_text(encoding="utf-8")))
        if existing != stored:
            raise ValueError("A Validation admission record is immutable and cannot change.")
        stored = existing
    else:
        stored = write_trusted_record_once(path, stored)
    connection.execute(
        "INSERT OR IGNORE INTO research_loop_admissions "
        "(admission_id, round_id, version_id, admission_json) VALUES (?, ?, ?, ?)",
        (
            stored["admission_id"],
            stored["round_id"],
            stored["version_id"],
            _json(stored),
        ),
    )
    return stored


def _admission_result(
    connection: sqlite3.Connection,
    record: Mapping[str, Any],
    body: Mapping[str, Any],
    *,
    reused: bool,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "admitted",
        "reused": reused,
        "admission": _admission_view(connection, body),
        "allowance": _validation_allowance(connection, record),
    }


def _validation_identity(
    record: Mapping[str, Any],
    submission: Mapping[str, Any],
    runtime: str,
) -> str:
    """Content-addressed Validation identity for the round's protocol.

    Protocol v1 keeps its exact #43 body.  Protocol v2 additionally binds the
    evidence source the receipt claims: the sealed snapshot ID and both frozen
    slice hashes, so a changed development or trailing payload cannot replay an
    old formal receipt.  (The protocol hash already covers ``split_hash``.)
    """
    locked = submission["locked"]
    identity: dict[str, Any] = {
        "version_id": submission["version_id"],
        "scoring_policy_hash": _scoring_policy_hash(record["kickoff"]),
        "snapshot_hash": locked["snapshot_hash"],
        "protocol_hash": locked["protocol_hash"],
        "engine_runtime_identity": runtime,
    }
    split = record["kickoff"].get("research_partition")
    if split is not None:
        identity["validation_kind"] = VALIDATION_KIND_TRAILING
        identity["snapshot_id"] = locked["snapshot_id"]
        identity["split_hash"] = split["split_hash"]
        identity["development_content_hash"] = split["development"]["content_hash"]
        identity["trailing_content_hash"] = split["trailing"]["content_hash"]
    return _hash(identity)


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


def _resolve_round_plugins(
    project: Path,
    plugin_config: Path | None = None,
    plugin_specs: tuple[str, ...] | list[str] = (),
) -> dict[str, dict[str, Any]]:
    from prajnatrade.experiment import load_project_plugin_registry
    from prajnatrade.plugins import load_plugin_registry

    registry = load_project_plugin_registry(project)
    explicit = load_plugin_registry(plugin_config=plugin_config, plugin_specs=plugin_specs)
    if explicit:
        registry = {**registry, **explicit}
    return registry


def _verify_locked_plugin(
    submission: Mapping[str, Any],
    plugin_registry: Mapping[str, Mapping[str, Any]],
) -> None:
    from prajnatrade.plugins import PluginError, verify_plugin_identity
    from prajnatrade.strategies import STRATEGY_FAMILIES

    strategy = submission["locked"]["strategy"]
    family = strategy["family"]
    if "plugin" not in strategy and family in (*STRATEGY_FAMILIES, "python"):
        return
    if "plugin" not in strategy or not isinstance(strategy["plugin"], Mapping):
        raise PluginError(
            "PLUGIN_NOT_REGISTERED",
            f"Locked strategy {family!r} is missing its frozen plugin identity.",
        )
    verify_plugin_identity(strategy["plugin"], plugin_registry)


def validate(
    project: Path,
    store: Path,
    snapshot_store: Path,
    round_id: str,
    version_id: str,
    *,
    plugin_config: Path | None = None,
    plugin_specs: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    """Validate a locked version under the round's recorded protocol.

    Protocol v1 replays the frozen Discovery interval.  Protocol v2 scores the
    frozen trailing segment only, after atomically reserving this version's one
    counted read; every path after the reservation keeps that use.
    """
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
                    "An ended version cannot run another Validation.",
                )
            if state == "approved":
                raise ProtocolError(
                    "REVIEW_CONFLICT", "validation", "inspect",
                    "An approved version keeps its admitted receipt; use round "
                    "revalidate to re-check corrected evidence.",
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
            formal = "research_partition" in record["kickoff"]
            use: dict[str, Any] | None = None
            if formal:
                # The reservation, the attempt row and every later state change
                # are serialized by the round lock plus this write transaction.
                connection.execute("BEGIN IMMEDIATE")
                use = _validation_use_for_version(connection, round_id, version_id)
            runtime: str | None = None
            if use is None:
                runtime = _current_runtime(campaign)
                validation_id = _validation_identity(record, submission, runtime)
            else:
                validation_id = use["validation_id"]
            terminal = connection.execute(
                "SELECT receipt_json FROM research_loop_validations WHERE validation_id = ?",
                (validation_id,),
            ).fetchone()
            if terminal is not None:
                receipt = _load_trusted_receipt(trusted, validation_id, terminal["receipt_json"])
                stored_attempts = len(_attempt_rows(connection, validation_id))
                result = {
                    **_published(receipt),
                    "reused": True,
                    "attempts": stored_attempts,
                    "resumed": stored_attempts > 1,
                }
                if formal:
                    result["allowance"] = _validation_allowance(connection, record)
                return result
            recovered = _recover_formal_terminal(trusted, use) if use is not None else None
            if runtime is None:
                runtime = _current_runtime(campaign)
            if use is not None and recovered is None and validation_id != _validation_identity(
                record, submission, runtime,
            ):
                raise ProtocolError(
                    "VALIDATION_RUNTIME_CHANGED", "validation", "inspect",
                    "The interrupted formal Validation requires its original accounting "
                    "runtime to resume.",
                )
            attempts = _attempt_rows(connection, validation_id)
            plugin_registry = _resolve_round_plugins(project, plugin_config, plugin_specs)
            if recovered is None:
                from prajnatrade.plugins import PluginError

                try:
                    _verify_locked_plugin(submission, plugin_registry)
                except PluginError as error:
                    raise ProtocolError(
                        error.code, "validation", "correct", error.message,
                    ) from error
            discovery_precheck: dict[str, Any] | None = None
            if formal and recovered is None:
                # The summary and frozen scoring premises are checked before
                # either metadata admission or the counted reservation.
                if use is None:
                    discovery_precheck = _formal_discovery_precheck(
                        project,
                        record,
                        submission,
                        campaign,
                        _validation_allowance(connection, record),
                    )
                elif attempts:
                    first_attempt = json.loads(attempts[0]["attempt_json"])
                    stored_precheck = first_attempt.get("discovery_precheck")
                    if isinstance(stored_precheck, dict):
                        discovery_precheck = stored_precheck
                _formal_manifest_gate(
                    snapshot_store,
                    record,
                    campaign,
                    _validation_allowance(connection, record),
                )
                if use is None:
                    use = _reserve_validation_use(connection, record, submission, validation_id)
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
                            "engine_runtime_identity": runtime,
                            **({"validation_kind": VALIDATION_KIND_TRAILING} if formal else {}),
                            **(
                                {"discovery_precheck": discovery_precheck}
                                if discovery_precheck is not None else {}
                            ),
                        }
                    ),
                ),
            )
        _write_running_marker(trusted, validation_id, record, submission, attempt)
        _failpoint(VALIDATION_FAILPOINT)
        if recovered is not None:
            receipt = recovered
        elif formal:
            if use is None:
                raise ValueError("The formal Validation use was not reserved.")
            runs = _formal_runs(
                snapshot_store, record, submission, campaign, plugin_registry=plugin_registry,
            )
            receipt = _formal_receipt(
                record,
                submission,
                validation_id=validation_id,
                runs=runs,
                use=use,
                discovery_precheck=discovery_precheck,
            )
        else:
            replay = _replay(
                project, snapshot_store, record, submission, campaign,
                plugin_registry=plugin_registry,
            )
            receipt = _receipt(
                record,
                submission,
                validation_id=validation_id,
                replay=replay,
            )
        # The trusted evidence is written before the research-visible copy.
        # For formal Validation, the reserved identity recovers an already
        # completed trusted write without another sealed read.
        _write_terminal_receipt(trusted, validation_id, receipt)
        _failpoint(VALIDATION_TERMINAL_FAILPOINT)
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
            allowance = _validation_allowance(connection, record)
    result = {
        **_published(receipt),
        # A recovered terminal receipt means this invocation still completes the
        # attempt and publishes it, so it is resumed rather than a plain replay.
        "reused": False,
        "attempts": attempt,
        "resumed": resumed,
    }
    if formal:
        result["allowance"] = allowance
    return result


def admit(
    project: Path,
    store: Path,
    round_id: str,
    version_id: str,
    reviewer: str,
    reason: str,
    source: object,
) -> dict[str, Any]:
    """Record one write-once manual Validation admission for a v2 round.

    Granting is an operator decision and is not a read: it writes no use row and
    leaves the allowance counters unchanged.  The admission is bound to exactly
    one locked version, replays by identical request content, and consumes only
    when (and if) that version is validated.
    """
    from prajnatrade.protected_eval import evaluation_store

    payload = _object(source, {"comparison", "selection_impact"})
    reviewer = _text(reviewer, "reviewer")
    reason = _text(reason, "reason")
    comparison = _text(payload["comparison"], "comparison")
    selection_impact = _text(payload["selection_impact"], "selection_impact")
    trusted = evaluation_store(store)
    if trusted.is_relative_to(project.resolve()):
        raise ValueError("Trusted evaluation store must be outside the research project.")
    with _round_lock(project, round_id), _project(project) as connection:
        record = _project_round(connection, round_id)
        trusted_record = _load_round_file(_round_path(trusted, round_id), round_id)
        if trusted_record != record:
            raise ValueError("The research round copy does not match the trusted approval.")
        submission = _submission(connection, round_id, version_id, phase="admission")
        body = {
            "schema_version": SCHEMA_VERSION,
            "round_id": record["round_id"],
            "version_id": submission["version_id"],
            "reviewer": reviewer,
            "reason": reason,
            "comparison": comparison,
            "selection_impact": selection_impact,
        }
        admission_id = _hash(body)
        existing = _admission_for_version(connection, round_id, version_id)
        if existing is not None and existing["admission_id"] == admission_id:
            # An identical request replays the stored grant, including its
            # original recorded time; a completed read still reports used.
            return _admission_result(connection, record, existing, reused=True)
        if _closure(connection, round_id) is not None:
            raise _rejected(
                "ROUND_CLOSED", "admission", "inspect",
                "A closed research round cannot admit another version.",
            )
        if "research_partition" not in record["kickoff"]:
            raise _rejected(
                "VALIDATION_POLICY_REQUIRED", "admission", "inspect",
                "A manual Validation admission applies only to a protocol-v2 round "
                "with a frozen automatic Validation limit.",
            )
        state = _review_state(connection, version_id)
        if state == "ended":
            raise _rejected(
                "VERSION_ENDED", "admission", "inspect",
                "An ended version is terminal and cannot be admitted.",
            )
        if state == "approved":
            raise _rejected(
                "REVIEW_CONFLICT", "admission", "inspect",
                "An approved version keeps its admitted receipt; a manual "
                "Validation admission would be a second decision for it.",
            )
        if existing is not None:
            raise _rejected(
                "ADMISSION_CONFLICT", "admission", "inspect",
                "This version already has a different manual Validation admission.",
            )
        counted = connection.execute(
            "SELECT 1 FROM research_loop_validation_uses "
            "WHERE round_id = ? AND version_id = ?",
            (round_id, version_id),
        ).fetchone()
        completed = connection.execute(
            "SELECT 1 FROM research_loop_validations WHERE version_id = ?",
            (version_id,),
        ).fetchone()
        if counted is not None or completed is not None:
            raise _rejected(
                "VALIDATION_ALREADY_COMPLETED", "admission", "inspect",
                "This version already has a counted or completed trailing "
                "Validation read; a manual admission would be stranded.",
            )
        # A manual admission is the researcher's extra read *after* the
        # automatic allowance, never a way to bypass it: the round must have
        # used every automatic slot first.  This is what makes the paused
        # lifecycle (`awaiting_validation_admission`) meaningful, and it means a
        # granted manual read can never be pre-empted by an unused automatic slot.
        allowance = _validation_allowance(connection, record)
        if allowance is None:
            raise ValueError("The protocol-v2 round has no frozen Validation policy.")
        if allowance["automatic_used"] < allowance["auto_limit"]:
            raise _rejected(
                "VALIDATION_ALLOWANCE_REMAINING", "admission", "validate",
                "This round still has automatic trailing Validation slots; the "
                "researcher must let those automatic reads be used before an "
                "operator can admit an extra version.",
                allowance=allowance,
                round_state="research",
            )
        trusted_existing = _trusted_admission_for_version(trusted, round_id, version_id)
        if (
            trusted_existing is not None
            and trusted_existing["admission_id"] != admission_id
        ):
            raise _rejected(
                "ADMISSION_CONFLICT", "admission", "inspect",
                "This version already has a different manual Validation admission.",
            )
        if trusted_existing is not None:
            # A crash after the trusted write recovered on retry: publish the
            # project projection only, keeping the recorded grant time.
            stored = _publish_admission(connection, trusted, trusted_existing)
            return _admission_result(connection, record, stored, reused=True)
        stored = _publish_admission(
            connection,
            trusted,
            {**body, "admission_id": admission_id, "granted_at": _now()},
        )
        return _admission_result(connection, record, stored, reused=False)


# ---------------------------------------------------------------------------
# Controlled Validation detail queries (protocol v2 and legacy receipts)
# ---------------------------------------------------------------------------

# The fixed allowlist projects the *published* project receipt only.  It never
# names ``curves`` (trusted-only per-session evidence), a sealed payload, a
# ledger row or a price, so a detail query cannot widen the agent boundary.
_DETAIL_RECORD_NAMES = (
    "summary", "metrics.candidate", "metrics.benchmark", "runs.candidate",
    "runs.benchmark", "fills.candidate", "fills.benchmark", "observation",
    "protocol", "provenance", "usage", "development", "trailing", "scoring",
    "warmup", "replay",
)
_DETAIL_CHECK_GROUPS = (
    "data_availability", "scoring_start", "coverage", "position", "costs",
    "replay", "segment", "hard_conditions",
)
_DETAIL_MISSING = object()

_FEEDBACK_IDENTITY_FIELDS = (
    "schema_version", "round_id", "version_id", "validation_id", "receipt_hash",
    "impact", "detail_access_id", "next_experiment_id",
)


def _detail_block(receipt: Mapping[str, Any], record: str) -> object:
    """The published receipt block a record name projects (``_DETAIL_MISSING``)."""
    if record == "summary":
        return receipt.get("summary", _DETAIL_MISSING)
    if record.startswith("metrics.") or record.startswith("runs."):
        side = record.split(".", 1)[1]
        block = receipt.get(record.split(".", 1)[0])
        if isinstance(block, Mapping) and side in block:
            return block[side]
        return _DETAIL_MISSING
    if record.startswith("fills."):
        side = record.split(".", 1)[1]
        runs = receipt.get("runs")
        if isinstance(runs, Mapping) and isinstance(runs.get(side), Mapping):
            return runs[side].get("fills", _DETAIL_MISSING)
        return _DETAIL_MISSING
    if record.startswith("checks."):
        group = record.split(".", 1)[1]
        checks = receipt.get("checks")
        if isinstance(checks, Mapping) and group in checks:
            return checks[group]
        return _DETAIL_MISSING
    return receipt.get(record, _DETAIL_MISSING)


def _detail_request(round_id: str, version_id: str, receipt: Mapping[str, Any], source: object):
    payload = _object(source, {"question", "records"})
    question = _text(payload["question"], "question")
    records = _texts(payload["records"], "records")
    if not records:
        raise ValueError("records must name at least one published receipt block.")
    normalized = sorted(set(records))
    unknown = [
        record
        for record in normalized
        if record not in _DETAIL_RECORD_NAMES
        and not (
            record.startswith("checks.")
            and record.split(".", 1)[1] in _DETAIL_CHECK_GROUPS
        )
    ]
    if unknown:
        raise _rejected(
            "DETAIL_RECORD_UNKNOWN", "detail", "correct",
            "The requested record is not in the published detail allowlist.",
            records=unknown,
        )
    request = {
        "schema_version": SCHEMA_VERSION,
        "round_id": round_id,
        "version_id": version_id,
        "validation_id": receipt["validation_id"],
        "receipt_hash": receipt["receipt_hash"],
        "question": question,
        "records": normalized,
    }
    details: dict[str, Any] = {}
    unavailable: list[str] = []
    for record in normalized:
        value = _detail_block(receipt, record)
        if value is _DETAIL_MISSING:
            unavailable.append(record)
        else:
            details[record] = value
    if unavailable:
        raise _rejected(
            "DETAIL_RECORD_UNAVAILABLE", "detail", "inspect",
            "A requested record is not part of this published Validation receipt.",
            records=unavailable,
        )
    return request, details


def _verify_detail(row: sqlite3.Row) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load one content-addressed detail request and its stored resolution."""
    request = json.loads(row["request_json"])
    if not isinstance(request, dict) or request.get("detail_access_id") != row["detail_access_id"]:
        raise ValueError("Validation detail request fingerprint mismatch.")
    if _hash(
        {key: value for key, value in request.items() if key != "detail_access_id"}
    ) != row["detail_access_id"]:
        raise ValueError("Validation detail request fingerprint mismatch.")
    resolution = json.loads(row["resolution_json"])
    if not isinstance(resolution, dict) or not isinstance(resolution.get("details"), dict):
        raise ValueError("Invalid Validation detail resolution.")
    return request, resolution


def _detail_view(
    request: Mapping[str, Any],
    resolution: Mapping[str, Any],
    *,
    reused: bool,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "completed",
        "reused": reused,
        **dict(request),
        "details": resolution["details"],
        "recorded_at": resolution["recorded_at"],
    }


def _detail_rows(
    connection: sqlite3.Connection, version_id: str,
) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            "SELECT detail_access_id, round_id, version_id, validation_id, "
            "request_json, resolution_json FROM research_loop_details "
            "WHERE version_id = ? ORDER BY rowid",
            (version_id,),
        )
    )


def _version_details(
    connection: sqlite3.Connection, version_id: str,
) -> list[dict[str, Any]]:
    """The status projection: recorded questions without duplicating answers."""
    listed: list[dict[str, Any]] = []
    for row in _detail_rows(connection, version_id):
        request, resolution = _verify_detail(row)
        listed.append(
            {
                "detail_access_id": row["detail_access_id"],
                "question": request["question"],
                "records": list(request["records"]),
                "recorded_at": resolution["recorded_at"],
            }
        )
    return listed


def detail(
    project: Path, round_id: str, version_id: str, source: object,
) -> dict[str, Any]:
    """Answer one specific question from the version's *published* receipt.

    Agent + operator command, project database only: the resolution is projected
    from the project's published receipt and never re-reads the sealed snapshot,
    the trusted per-session curves or a ledger.  A detail query is not a
    Validation read: it writes no use row and never changes the allowance.
    """
    with _round_lock(project, round_id), _project(project) as connection:
        _project_round(connection, round_id)
        _submission(connection, round_id, version_id, phase="detail")
        row = connection.execute(
            "SELECT validation_id, receipt_json FROM research_loop_validations "
            "WHERE version_id = ?",
            (version_id,),
        ).fetchone()
        if row is None:
            raise _rejected(
                "VALIDATION_NOT_PASSED", "detail", "validate",
                "A detail question needs a terminal published Validation receipt.",
            )
        receipt = json.loads(row["receipt_json"])
        if receipt.get("status") != "completed" or "receipt_hash" not in receipt:
            raise _rejected(
                "VALIDATION_NOT_PASSED", "detail", "validate",
                "A detail question needs a terminal published Validation receipt.",
            )
        receipt = {**receipt, "validation_id": row["validation_id"]}
        request, details = _detail_request(round_id, version_id, receipt, source)
        detail_access_id = _hash(request)
        stored_request = {**request, "detail_access_id": detail_access_id}
        existing = connection.execute(
            "SELECT detail_access_id, request_json, resolution_json "
            "FROM research_loop_details WHERE detail_access_id = ?",
            (detail_access_id,),
        ).fetchone()
        if existing is not None:
            existing_request, existing_resolution = _verify_detail(existing)
            return _detail_view(
                existing_request, existing_resolution, reused=True
            )
        resolution = {
            "status": "completed",
            "details": details,
            "recorded_at": _now(),
        }
        connection.execute(
            "INSERT OR IGNORE INTO research_loop_details "
            "(detail_access_id, round_id, version_id, validation_id, request_json, "
            "resolution_json) VALUES (?, ?, ?, ?, ?, ?)",
            (
                detail_access_id,
                round_id,
                version_id,
                receipt["validation_id"],
                _json(stored_request),
                _json(resolution),
            ),
        )
        stored = connection.execute(
            "SELECT detail_access_id, request_json, resolution_json "
            "FROM research_loop_details WHERE detail_access_id = ?",
            (detail_access_id,),
        ).fetchone()
        stored_request_json, stored_resolution = _verify_detail(stored)
        return _detail_view(
            stored_request_json,
            stored_resolution,
            reused=stored_resolution != resolution,
        )


# ---------------------------------------------------------------------------
# Validation feedback and the revision link
# ---------------------------------------------------------------------------


def _verify_feedback(body: object) -> dict[str, Any]:
    if not isinstance(body, dict) or not set(_FEEDBACK_IDENTITY_FIELDS) <= body.keys():
        raise ValueError("Invalid Validation feedback record.")
    feedback_id = body.get("feedback_id")
    _require_hex(feedback_id, "feedback")
    if feedback_id != _hash(
        {field: body[field] for field in _FEEDBACK_IDENTITY_FIELDS}
    ):
        raise ValueError("Validation feedback fingerprint mismatch.")
    if not isinstance(body.get("recorded_at"), str) or not body["recorded_at"]:
        raise ValueError("Invalid Validation feedback timestamp.")
    return body


def _version_feedback(
    connection: sqlite3.Connection, version_id: str,
) -> list[dict[str, Any]]:
    return [
        _verify_feedback(json.loads(row["feedback_json"]))
        for row in connection.execute(
            "SELECT feedback_json FROM research_loop_feedback "
            "WHERE version_id = ? ORDER BY rowid",
            (version_id,),
        )
    ]


def _feedback_result(
    body: Mapping[str, Any], *, reused: bool,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "recorded",
        "reused": reused,
        "feedback": dict(body),
    }


def feedback(
    project: Path, round_id: str, version_id: str, source: object,
) -> dict[str, Any]:
    """Link V1's Validation evidence (and one recorded detail) to what it changed."""
    with _round_lock(project, round_id), _project(project) as connection:
        record = _project_round(connection, round_id)
        submission = _submission(connection, round_id, version_id, phase="feedback")
        row = connection.execute(
            "SELECT validation_id, receipt_json FROM research_loop_validations "
            "WHERE version_id = ?",
            (version_id,),
        ).fetchone()
        if row is None:
            raise _rejected(
                "VALIDATION_NOT_PASSED", "feedback", "validate",
                "Feedback needs a terminal published Validation receipt.",
            )
        receipt = json.loads(row["receipt_json"])
        if receipt.get("status") != "completed" or "receipt_hash" not in receipt:
            raise _rejected(
                "VALIDATION_NOT_PASSED", "feedback", "validate",
                "Feedback needs a terminal published Validation receipt.",
            )
        payload = _object(
            source, {"impact", "detail_access_id", "next_experiment_id"}
        )
        impact = _text(payload["impact"], "impact")
        detail_access_id = payload["detail_access_id"]
        if detail_access_id is not None:
            detail_access_id = _require_hex(detail_access_id, "detail access")
            request_row = connection.execute(
                "SELECT round_id, version_id, request_json FROM research_loop_details "
                "WHERE detail_access_id = ?",
                (detail_access_id,),
            ).fetchone()
            request = (
                json.loads(request_row["request_json"])
                if request_row is not None
                else None
            )
            if (
                request_row is None
                or request_row["round_id"] != round_id
                or request_row["version_id"] != version_id
                or request["validation_id"] != row["validation_id"]
                or request["receipt_hash"] != receipt["receipt_hash"]
            ):
                raise _rejected(
                    "DETAIL_NOT_FOUND", "feedback", "detail",
                    "The referenced detail access does not belong to this "
                    "version's published Validation receipt.",
                )
        next_experiment_id = payload["next_experiment_id"]
        if next_experiment_id is not None:
            next_experiment_id = _text(next_experiment_id, "next_experiment_id")
            attempts = campaign_experiment_history(project, record["campaign_id"])
            known = any(
                item["experiment_id"] == next_experiment_id for item in attempts
            )
            if not known or next_experiment_id == submission["experiment_id"]:
                raise _rejected(
                    "FEEDBACK_LINK_INVALID", "feedback", "correct",
                    "Feedback must link another experiment in the same Campaign.",
                )
        identity = {
            "schema_version": SCHEMA_VERSION,
            "round_id": round_id,
            "version_id": version_id,
            "validation_id": row["validation_id"],
            "receipt_hash": receipt["receipt_hash"],
            "impact": impact,
            "detail_access_id": detail_access_id,
            "next_experiment_id": next_experiment_id,
        }
        feedback_id = _hash(identity)
        existing = connection.execute(
            "SELECT feedback_json FROM research_loop_feedback WHERE feedback_id = ?",
            (feedback_id,),
        ).fetchone()
        if existing is not None:
            return _feedback_result(_verify_feedback(json.loads(existing["feedback_json"])), reused=True)
        body = {**identity, "feedback_id": feedback_id, "recorded_at": _now()}
        connection.execute(
            "INSERT OR IGNORE INTO research_loop_feedback "
            "(feedback_id, round_id, version_id, feedback_json) VALUES (?, ?, ?, ?)",
            (feedback_id, round_id, version_id, _json(body)),
        )
        stored = connection.execute(
            "SELECT feedback_json FROM research_loop_feedback WHERE feedback_id = ?",
            (feedback_id,),
        ).fetchone()
        return _feedback_result(_verify_feedback(json.loads(stored["feedback_json"])), reused=False)


def load_tracking_receipt(store: str | Path, validation_id: str) -> dict[str, Any]:
    """Public seam: load one completed lightweight Validation receipt safely.

    Cross-module readers (``release``) must not reach into the trusted directory
    layout or use a generic JSON reader: this seam verifies the content address,
    the terminal status and the identity every time.
    """
    validation_id = _require_hex(validation_id, "validation")
    path = _validation_path(Path(store), validation_id)
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(
            "The bound lightweight Validation receipt is missing."
        ) from error
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError(
            f"The bound lightweight Validation receipt is unreadable: {error}"
        ) from error
    if not isinstance(receipt, dict) or receipt.get("status") != "completed":
        raise ValueError("The bound lightweight Validation receipt is not terminal.")
    expected = _hash({key: value for key, value in receipt.items() if key != "receipt_hash"})
    if (
        receipt.get("receipt_hash") != expected
        or receipt.get("validation_id") != validation_id
    ):
        raise ValueError(
            "The bound lightweight Validation receipt failed its content-address check."
        )
    return receipt


def verify_tracking_binding(
    store: str | Path,
    validation_id: str,
    version_id: str,
    receipt_hash: str,
) -> dict[str, Any]:
    """Verify the receipt a manual-tracking release artifact binds.

    The release contract (spec #43 L31/L34) admits only a *passed* terminal
    lightweight Validation for exactly this version, so a tampered or non-passed
    trusted record fails closed even when the artifact itself is intact.
    """
    receipt = load_tracking_receipt(store, validation_id)
    if receipt.get("version_id") != version_id:
        raise ValueError(
            "The bound lightweight Validation receipt belongs to another version."
        )
    if receipt.get("receipt_hash") != receipt_hash:
        raise ValueError(
            "The bound lightweight Validation receipt differs from the release binding."
        )
    if receipt.get("outcome") != "passed":
        raise ValueError(
            "The bound lightweight Validation receipt did not pass; a manual-tracking "
            "release requires a passed receipt."
        )
    return receipt


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


def _terminal_session(calendar: Mapping[str, Any], decided_at: str) -> str:
    """The first calendar session after a terminal decision.

    The review CLI does not receive the Shadow Live ledger, so it cannot know
    the last committed session.  The first session after "now" is the earliest
    after-close point the ledger cannot have committed yet; when the approved
    calendar is already exhausted it falls back to its last session (the release
    timeline only refuses a *decreasing* effective date).
    """
    local_date = datetime.fromisoformat(decided_at).astimezone(SHANGHAI).date()
    sessions = calendar["sessions"]
    for session in sessions:
        if date.fromisoformat(session) > local_date:
            return session
    return sessions[-1]


def _release_protocol(phase: str, call):
    """Run one release seam call, rendering its rejection as a ProtocolError."""
    try:
        return call()
    except release.ReleaseRejection as error:
        converted = ProtocolError(
            error.details["code"],
            phase,
            error.details["next_action"],
            error.details["message"],
        )
        converted.context = dict(error.context)
        raise converted from error


def _retire_tracking(
    trusted: Path, release_id: str, decided_at: str, reason: str, *, phase: str,
) -> None:
    """Append the terminal retire event for a manually tracked version (once)."""
    status = _release_protocol(
        phase, lambda: release.status(trusted, release_id)
    )
    if status["current_state"] == "retired":
        return
    artifact = _release_protocol(
        phase, lambda: release.fetch(trusted, release_id)
    )["artifact"]
    effective = _terminal_session(artifact["admission"]["calendar"], decided_at)
    _release_protocol(
        phase, lambda: release.retire(trusted, release_id, effective, reason)
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
        # The release content-address contract keeps ``plan_id``/``plan_hash``
        # equal to the lightweight Validation ID.  The explicit ``plan_kind`` and
        # ``validation_id`` fields make that reusable evidence unambiguous: this
        # is not a frozen protected plan and never authorizes a protected read.
        "plan_kind": "lightweight_validation",
        "validation_id": validation_id,
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
            **(
                {"decision_policy": locked["decision_policy"]}
                if locked.get("decision_policy") is not None
                else {}
            ),
            **(
                {"history_requirements": list(locked["history_requirements"] or [])}
                if locked.get("decision_policy") is not None or locked.get("history_requirements")
                else {}
            ),
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
        tracking = _latest_tracking(connection, version_id)
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
            # The replay key is the human decision content, never the clock: the
            # first computed effective session is stored on the decision and a
            # later replay returns it unchanged (spec #43 L31).
            request_payload = {
                "observation": confirmation,
                "calendar_hash": _hash(calendar_data),
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
                _release_protocol(
                    "review",
                    lambda: release.register_tracking_version(
                        trusted,
                        artifact,
                        existing["tracking"]["enabled_signal_date"],
                        existing["reason"],
                    ),
                )
                _store_observation(connection, existing)
            elif existing["decision"] == "end" and tracking is not None:
                _retire_tracking(
                    trusted,
                    tracking["release_id"],
                    existing["decided_at"],
                    existing["reason"],
                    phase="review",
                )
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
                "enabled_signal_date": _first_signal_session(
                    calendar_data, decided_at
                ),
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
            _release_protocol(
                "review",
                lambda: release.register_tracking_version(
                    trusted,
                    artifact,
                    body["tracking"]["enabled_signal_date"],
                    reason,
                ),
            )
            _store_observation(connection, body)
        else:
            _store_decision(connection, trusted, body)
            if decision == "end" and tracking is not None:
                _retire_tracking(
                    trusted, tracking["release_id"], decided_at, reason, phase="review"
                )
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
            # A genuinely unrelated snapshot is not successor evidence and is
            # skipped; a successor-shaped snapshot is never silently ignored.
            continue
        candidates.append(
            _successor_evidence(
                snapshot_store, campaign, record, locked_snapshot_id, snapshot_id
            )
        )
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


def _formal_revalidation_receipt(
    snapshot_store: Path,
    record: Mapping[str, Any],
    campaign: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Read and verify the corrected successor for formal revalidation.

    The successor must preserve the frozen development slice hash, data kind,
    prior history, research interval/session dates and as-of.  Its trailing rows
    may legitimately change (that is the correction this path recomputes).
    """
    split = _formal_split(record)
    frozen = record["kickoff"]["scoring_policy"]["frozen"]
    evaluation = campaign["evaluation"]
    try:
        receipt = snapshot.read_discovery(
            Path(snapshot_store),
            evidence["snapshot_id"],
            "discovery",
            evidence.get("as_of"),
        )
    except (OSError, ValueError) as error:
        raise _rejected(
            "EVIDENCE_INTEGRITY_VIOLATION", "revalidation", "inspect",
            f"The corrected evidence payload cannot be read at its recorded "
            f"as-of cutoff: {error}",
        ) from error
    if receipt["data_kind"] != evaluation["data_kind"]:
        raise _rejected(
            "EVIDENCE_DATA_KIND_MISMATCH", "revalidation", "inspect",
            "The corrected evidence snapshot data kind differs from the Campaign.",
        )
    if receipt.get("prior_history_hash") != frozen.get("prior_history_hash"):
        raise _rejected(
            "EVIDENCE_PRIOR_HISTORY_MISMATCH", "revalidation", "inspect",
            "The corrected evidence snapshot changed the frozen Discovery prior history.",
        )
    development = split["development"]
    sliced = snapshot.partition_slice(
        receipt["data"], development["start"], development["end"]
    )
    if sliced != development:
        raise _rejected(
            "EVIDENCE_DEVELOPMENT_MISMATCH", "revalidation", "inspect",
            "The corrected evidence snapshot changed the frozen development slice.",
            expected=dict(development),
            actual=sliced,
        )
    trailing = snapshot.partition_slice(
        receipt["data"], split["trailing"]["start"], split["trailing"]["end"]
    )
    if (
        trailing["start"] != split["trailing"]["start"]
        or trailing["end"] != split["trailing"]["end"]
        or trailing["sessions"] != split["trailing"]["sessions"]
    ):
        raise _rejected(
            "EVIDENCE_INTERVAL_MISMATCH", "revalidation", "inspect",
            "The corrected evidence snapshot changed the frozen trailing session dates.",
        )
    return receipt


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
    if "validation_kind" in receipt:
        body["validation_kind"] = receipt["validation_kind"]
    for field in (
        "original_discovery", "recomputed_discovery", "affected",
        "original_validation", "recomputed_validation",
    ):
        if field in receipt:
            body[field] = receipt[field]
    review_block: dict[str, Any] | None = None
    if review_state == "approved":
        trigger = None
        if receipt["outcome"] == "failed":
            trigger = "REVALIDATION_FAILED"
        elif receipt["outcome"] == "insufficient_evidence":
            trigger = "REVALIDATION_INSUFFICIENT_EVIDENCE"
        review_block = {
            "required": trigger is not None,
            "trigger": trigger,
            "reason": receipt["reason"],
        }
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
    if "validation_kind" in receipt:
        result["validation_kind"] = receipt["validation_kind"]
    if review_block is not None:
        body["review"] = review_block
        result["review"] = review_block
    for field in (
        "original_discovery", "recomputed_discovery", "affected",
        "original_validation", "recomputed_validation",
    ):
        if field in receipt:
            result[field] = receipt[field]
    body["result"] = result
    body["revalidation_id"] = _hash(body)
    return body


def _require_revalidation_eligible(
    submission: Mapping[str, Any],
    prior: Mapping[str, Any],
    campaign: Mapping[str, Any],
    plugin_registry: Mapping[str, Mapping[str, Any]],
) -> None:
    """Enforce #75 platform-fix revalidation eligibility before re-running."""
    from prajnatrade.plugins import PluginError, verify_plugin_identity
    from prajnatrade.strategies import STRATEGY_FAMILIES, normalize_strategy_spec

    if prior.get("version_id") != submission["version_id"] or (
        "strategy_fingerprint" in prior
        and prior["strategy_fingerprint"] != submission["strategy_fingerprint"]
    ):
        raise ProtocolError(
            "REVALIDATION_INELIGIBLE",
            "revalidation",
            "correct",
            "PRIOR_IDENTITY_UNVERIFIABLE: prior receipt identity does not match locked version.",
        )
    locked = submission["locked"]
    evaluation = campaign["evaluation"]
    if locked["execution_rules"] != evaluation["execution_rules"]:
        raise ProtocolError(
            "REVALIDATION_INELIGIBLE",
            "revalidation",
            "correct",
            "EXECUTION_RULES_CHANGED: frozen execution/valuation/lot rules differ from Campaign.",
        )
    if locked.get("decision_policy") != evaluation.get("decision_policy"):
        raise ProtocolError(
            "REVALIDATION_INELIGIBLE",
            "revalidation",
            "correct",
            "DECISION_POLICY_CHANGED: frozen decision policy differs from Campaign.",
        )
    strategy = locked["strategy"]
    family = strategy["family"]
    if "plugin" in strategy or family not in (*STRATEGY_FAMILIES, "python"):
        frozen_plugin = strategy.get("plugin")
        if (
            not isinstance(frozen_plugin, Mapping)
            or not frozen_plugin.get("plugin_id")
            or not frozen_plugin.get("dependencies_hash")
        ):
            raise ProtocolError(
                "REVALIDATION_INELIGIBLE",
                "revalidation",
                "correct",
                "PRIOR_IDENTITY_UNVERIFIABLE: locked plugin source identity is missing.",
            )
        try:
            verify_plugin_identity(frozen_plugin, plugin_registry)
        except PluginError as error:
            raise ProtocolError(
                "REVALIDATION_INELIGIBLE",
                "revalidation",
                "correct",
                f"{error.code}: {error.message}",
            ) from error
    if family == "python":
        source_text = strategy.get("source")
        source_hash = strategy.get("source_sha256")
        if (
            not isinstance(source_text, str)
            or not source_hash
            or hashlib.sha256(source_text.encode("utf-8")).hexdigest() != source_hash
        ):
            raise ProtocolError(
                "REVALIDATION_INELIGIBLE",
                "revalidation",
                "correct",
                "PRIOR_IDENTITY_UNVERIFIABLE: locked Python source identity is unverifiable.",
            )
    try:
        normalize_strategy_spec(strategy, plugin_registry=plugin_registry)
    except (ValueError, TypeError) as error:
        raise ProtocolError(
            "REVALIDATION_INELIGIBLE",
            "revalidation",
            "correct",
            f"PRIOR_IDENTITY_UNVERIFIABLE: {error}",
        ) from error


def revalidate(
    project: Path,
    store: Path,
    snapshot_store: Path,
    round_id: str,
    version_id: str,
    reason: str,
    evidence_snapshot_id: str | None = None,
    *,
    plugin_config: Path | None = None,
    plugin_specs: tuple[str, ...] | list[str] = (),
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
            # An approved version can still be re-checked after a data or
            # platform correction (spec #43 L33); only ``ended`` is terminal.
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
            formal = "research_partition" in record["kickoff"]
            if formal and prior.get("validation_kind") != VALIDATION_KIND_TRAILING:
                raise ValueError(
                    "This v2 round's stored receipt is not a formal trailing Validation."
                )
            use = (
                _validation_use_for_version(connection, round_id, version_id)
                if formal else None
            )
            if formal and use is None:
                raise ValueError("The formal Validation use for this version is missing.")
            campaign = campaign_discovery(project, record["campaign_id"])
            plugin_registry = _resolve_round_plugins(project, plugin_config, plugin_specs)
            _require_revalidation_eligible(submission, prior, campaign, plugin_registry)
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
            formal_source: dict[str, Any] | None = None
            if formal:
                formal_source = _formal_revalidation_receipt(
                    snapshot_store, record, campaign, evidence
                )
                split = _formal_split(record)
                evidence["validation_kind"] = VALIDATION_KIND_TRAILING
                evidence["split_hash"] = split["split_hash"]
                evidence["development"] = dict(split["development"])
                evidence["trailing"] = dict(split["trailing"])
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
                stored_attempts = len(_attempt_rows(connection, validation_id))
                return {
                    **body["result"],
                    "revalidation_id": body["revalidation_id"],
                    "reused": True,
                    "attempts": stored_attempts,
                    "resumed": stored_attempts > 1,
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
        if formal:
            if use is None or formal_source is None:
                raise ValueError("The formal revalidation evidence was not prepared.")
            recomputed = _formal_runs(
                snapshot_store,
                record,
                submission,
                campaign,
                receipt=formal_source,
                require_trailing=False,
                phase="revalidation",
                read_as_of=evidence.get("as_of"),
                frozen_read=False,
                snapshot_id=evidence["snapshot_id"],
                plugin_registry=plugin_registry,
            )
            receipt = _formal_receipt(
                record,
                submission,
                validation_id=attempt_context[0],
                runs=recomputed,
                use=use,
                discovery_precheck=prior.get("discovery_precheck"),
            )
            original_view = snapshot.read_discovery(
                Path(snapshot_store),
                prior["provenance"]["snapshot_id"],
                "discovery",
                prior["provenance"]["as_of"],
            )
            receipt.update(
                {
                    "evidence": dict(evidence),
                    "prior_validation_id": prior["validation_id"],
                    "original_validation": _formal_validation_record(prior),
                    "recomputed_validation": _formal_validation_record(receipt),
                    "affected": _formal_affected(
                        prior, recomputed, original_view["data"], record
                    ),
                }
            )
            receipt["receipt_hash"] = _hash(
                {key: value for key, value in receipt.items() if key != "receipt_hash"}
            )
        elif corrected:
            recomputed = _evidence_runs(
                snapshot_store, record, submission, campaign, evidence,
                plugin_registry=plugin_registry,
            )
            replay = _evidence_runs(
                snapshot_store, record, submission, campaign, evidence,
                plugin_registry=plugin_registry,
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
        "attempts": attempt_context[1],
        "resumed": resumed,
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
            "SELECT revalidation_id, prior_validation_id, revalidation_json FROM "
            "research_loop_revalidations WHERE validation_id = ?",
            (terminal["validation_id"],),
        ).fetchone()
        if revalidated is not None:
            summary["revalidation_id"] = revalidated["revalidation_id"]
            summary["revalidated_from"] = revalidated["prior_validation_id"]
            review_block = json.loads(revalidated["revalidation_json"]).get("review")
            if review_block is not None:
                summary["review"] = review_block
        # The protocol-v2 receipt fields are exposed when present; a legacy
        # receipt keeps its exact summary shape.
        for field, name in (
            ("validation_kind", "kind"),
            ("protocol", "protocol"),
            ("development", "development"),
            ("trailing", "trailing"),
            ("scoring", "scoring"),
            ("warmup", "warmup"),
            ("provenance", "provenance"),
            ("usage", "usage"),
            ("summary", "summary"),
        ):
            if field in receipt:
                summary[name] = receipt[field]
        for field in (
            "evidence", "original_discovery", "recomputed_discovery", "affected",
            "original_validation", "recomputed_validation",
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
    # Round status is agent-visible: it may show the state, the decision kind,
    # the return instructions and the confirmed observation, but never the human
    # reviewer's identity or reasoning (F8).
    return {
        "state": latest["review_state"],
        "decision": latest["decision"],
        "decision_id": latest["decision_id"],
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
        entry = {
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
            "details": _version_details(connection, version_id),
            "feedback": _version_feedback(connection, version_id),
            "review_state": review_state,
            "review": _review_summary(connection, version_id),
            "observation": _version_observation(connection, version_id),
            "tracking": _latest_tracking(connection, version_id),
        }
        for field in ("revision_of", "feedback_ids"):
            if field in body:
                entry[field] = body[field]
        locked = body["locked"]
        if "protocol" in locked:
            entry["protocol"] = locked["protocol"]
            entry["research_partition"] = locked["research_partition"]
        submissions.append(entry)
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
        "protocol": dict(kickoff.get("protocol", VALIDATION_PROTOCOL_LEGACY)),
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
        },
    }
    if "research_partition" in kickoff:
        result["research_partition"] = kickoff["research_partition"]
        result["validation_policy"] = kickoff["validation_policy"]
        if "full_history" in kickoff:
            result["full_history"] = kickoff["full_history"]
        if "reuse_disclosure" in record:
            result["reuse_disclosure"] = record["reuse_disclosure"]
        allowance = _validation_allowance_status(connection, record)
        if allowance is not None:
            result["validation_allowance"] = allowance
        result["admissions"] = [
            _admission_status_view(connection, body)
            for body in _admissions(connection, round_id)
        ]
    result["lifecycle"] = _lifecycle(connection, record, closure)
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


def _release_event(
    status: Mapping[str, Any], action: str, reason: str,
) -> dict[str, Any] | None:
    """The last lifecycle event when a crashed decision already applied it."""
    timeline = status.get("timeline") or []
    if not timeline:
        return None
    event = timeline[-1]
    if event.get("action") == action and event.get("reason") == reason:
        return event
    return None


def _evidence_end_review(
    connection: sqlite3.Connection,
    trusted: Path,
    record: Mapping[str, Any],
    submission: Mapping[str, Any],
    reviewer: str,
    reason: str,
) -> tuple[dict[str, Any] | None, dict[str, Any], str, dict[str, Any]]:
    """The terminal review content for an Evidence Loop retirement decision."""
    binding = _review_binding(connection, record, submission, trusted)
    payload = {
        "detail": reason,
        "continue_direction": True,
        "evidence_decision": "retire",
    }
    request_key = _review_request_key(
        record["round_id"],
        submission["version_id"],
        "end",
        reviewer,
        reason,
        payload,
        binding,
    )
    existing = _matching_review(
        connection, trusted, submission["version_id"], request_key
    )
    return existing, payload, request_key, binding


def _store_evidence_end_review(
    connection: sqlite3.Connection,
    trusted: Path,
    record: Mapping[str, Any],
    submission: Mapping[str, Any],
    reviewer: str,
    reason: str,
) -> dict[str, Any]:
    """Record the one terminal review state for an Evidence Loop retirement.

    ``round evidence decide retire`` is the same terminal decision as
    ``round review end``: it must leave exactly one current review state
    (``ended``) so both status views agree and ``round close`` refuses the
    version.  The record is content-addressed and replay-safe.
    """
    existing, payload, request_key, binding = _evidence_end_review(
        connection, trusted, record, submission, reviewer, reason
    )
    if existing is not None:
        # A crash can leave the immutable trusted record without its project row
        # (the write-ahead file is not transactional); reconcile it here.
        _store_decision(connection, trusted, existing)
        return existing
    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "round_id": record["round_id"],
        "version_id": submission["version_id"],
        "decision": "end",
        "review_state": "ended",
        "reviewer": reviewer,
        "reason": reason,
        "decided_at": _now(),
        "binding": binding,
        "request_key": request_key,
        "end": payload,
    }
    body["decision_id"] = _hash(body)
    _store_decision(connection, trusted, body)
    return body


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
    pause_scope: str,
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
            "pause_scope": pause_scope,
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


def _review_signature(trigger: str, evidence: Mapping[str, Any]) -> str | None:
    """Stable identity of one review trigger instance (F12 acknowledgement).

    A human ``continue`` acknowledges the trigger it saw, not the trigger code
    forever: a materially new instance (deeper drawdown, another unfilled run,
    more gap sessions) has a different signature and reopens the review.
    """
    if trigger == "max_unfilled_opportunities":
        salient: dict[str, Any] = {
            "unfilled": evidence["unfilled_opportunities"],
            "consecutive": evidence["consecutive_intent_without_fill"],
        }
    elif trigger == "max_abs_drawdown":
        salient = {"observed": round(float(evidence["max_abs_drawdown"]), 12)}
    elif trigger == "max_data_gap_sessions":
        salient = {"gaps": evidence["data_gap_sessions"]}
    elif trigger in ("observation_term", "observation_extended"):
        salient = {
            "due": evidence["due_opportunities"],
            "committed": evidence["committed_sessions"],
        }
    else:
        salient = {}
    return _hash({"trigger": trigger, "salient": salient})


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
        "key": None if trigger is None else _review_signature(trigger, evidence),
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
    *,
    acknowledged: bool = False,
) -> str:
    if review_state == "ended" or release_state == "retired":
        return "ended"
    if release_state == "paused" or pauses:
        return "paused"
    if review["trigger"] == "observation_extended":
        return "observation_extended"
    if review["trigger"] is not None and not acknowledged:
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
                "scope": latest_decision.get(
                    "pause_scope", latest_decision.get("scope", "version")
                ),
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
        "data_kind": body.get("data_kind"),
        "pause_scope": body.get("pause_scope", "version"),
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
            acknowledgement = (
                latest_decision.get("review") or {}
                if latest_decision is not None
                and latest_decision.get("decision") == "continue"
                else {}
            )
            acknowledged = bool(
                review["key"] is not None
                and acknowledgement.get("key") == review["key"]
            )
            pauses = list(evidence.get("active_pauses") or [])
            state = _evidence_state(
                review,
                review_state,
                release_state,
                pauses,
                acknowledged=acknowledged,
            )
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
                        not in (None, "observation_extended")
                        and not acknowledged,
                        "acknowledged": acknowledged,
                        "acknowledged_by": (
                            latest_decision["decision_id"]
                            if acknowledged and latest_decision is not None
                            else None
                        ),
                        "acknowledged_trigger": (
                            acknowledgement.get("trigger")
                            if acknowledged
                            else None
                        ),
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
        record = _project_round(connection, round_id)
        submission = _submission(connection, round_id, version_id, phase="evidence")
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
        # Artifact labelling: ``scope`` is the data scope and ``data_kind`` its
        # kind; the pause range is recorded separately (CODING_STANDARDS 1).
        scope = record["scope"]
        data_kind = record["data_kind"]
        pause_scope = "version"
        request_key = _evidence_request_key(
            round_id, version_id, release_id, decision, pause_scope, reviewer, reason
        )
        existing = _matching_evidence_decision(
            connection, trusted, version_id, request_key
        )
        if existing is not None:
            return _evidence_decision_output(existing, reused=True)
        review_state = _review_state(connection, version_id)
        effective = _evidence_effective_session(artifact, evidence, release_status)
        action = "none"
        state_after = state_before
        recovered = False
        if decision != "retire" and (
            state_before == "retired" or review_state == "ended"
        ):
            raise ProtocolError(
                "VERSION_ENDED", "evidence", "inspect",
                "An ended version is terminal; the round may continue with a new version.",
            )
        if decision == "pause":
            if state_before == "paused":
                event = _release_event(release_status, "pause", reason)
                if event is None:
                    raise ProtocolError(
                        "REVIEW_CONFLICT", "evidence", "inspect",
                        "Pause requires an enabled tracking version; it is already paused.",
                    )
                recovered = True
                state_before = event["state_before"]
                effective = event["effective_signal_date"]
            elif state_before != "enabled":
                raise ProtocolError(
                    "REVIEW_CONFLICT", "evidence", "inspect",
                    "Pause requires an enabled tracking version; it is already paused.",
                )
            action = "pause"
            state_after = "paused"
        elif decision == "continue":
            if state_before == "paused":
                action = "resume"
                state_after = "enabled"
            elif state_before == "enabled":
                # A crashed resume already appended its event; acknowledge it
                # instead of applying a second one.
                event = _release_event(release_status, "resume", reason)
                if event is not None:
                    recovered = True
                    state_before = event["state_before"]
                    effective = event["effective_signal_date"]
                    action = "resume"
                    state_after = "enabled"
            else:
                raise ProtocolError(
                    "VERSION_ENDED", "evidence", "inspect",
                    "An ended version is terminal; the round may continue with a new version.",
                )
        else:
            own_review: dict[str, Any] | None = None
            if review_state == "ended":
                own_review, _, _, _ = _evidence_end_review(
                    connection, trusted, record, submission, reviewer, reason
                )
                if own_review is None:
                    raise ProtocolError(
                        "VERSION_ENDED", "evidence", "inspect",
                        "An ended version is terminal; the round may continue with a new version.",
                    )
            if state_before == "retired":
                event = _release_event(release_status, "retire", reason)
                if event is None:
                    raise ProtocolError(
                        "VERSION_ENDED", "evidence", "inspect",
                        "An ended version is terminal; the round may continue with a new version.",
                    )
                recovered = True
                state_before = event["state_before"]
                effective = event["effective_signal_date"]
            action = "retire"
            state_after = "retired"
        if decision == "retire":
            _store_evidence_end_review(
                connection, trusted, record, submission, reviewer, reason
            )
            shadow.mark_version_ended(ledger, release_id, release_id)
            if not recovered:
                _release_protocol(
                    "evidence",
                    lambda: release.retire(trusted, release_id, effective, reason),
                )
        elif action == "pause" and not recovered:
            _release_protocol(
                "evidence",
                lambda: release.pause(trusted, release_id, effective, reason),
            )
        elif action == "resume" and not recovered:
            _release_protocol(
                "evidence",
                lambda: release.resume(trusted, release_id, effective, reason),
            )
        _failpoint(EVIDENCE_DECIDE_FAILPOINT)
        acknowledged_review: dict[str, Any] | None = None
        if decision == "continue":
            enabled = tracking["enabled_signal_date"]
            committed_sessions = sum(
                1 for day in evidence["committed_dates"] if day >= enabled
            )
            current = _evidence_review(
                record["kickoff"], evidence, confirmed, committed_sessions
            )
            acknowledged_review = {
                "trigger": current["trigger"],
                "key": current["key"],
                "reason": current["reason"],
            }
        body: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "round_id": round_id,
            "version_id": version_id,
            "release_id": release_id,
            "decision": decision,
            "scope": scope,
            "data_kind": data_kind,
            "pause_scope": pause_scope,
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
        if acknowledged_review is not None:
            body["review"] = acknowledged_review
        body["decision_id"] = _hash(body)
        _store_evidence_decision(connection, trusted, body)
    return _evidence_decision_output(body, reused=False)


# ---------------------------------------------------------------------------
# round close
# ---------------------------------------------------------------------------


def _version_approved(connection: sqlite3.Connection, version_id: str) -> bool:
    """Whether the version's one current review state is ``approved``."""
    return _review_state(connection, version_id) == "approved"


def _closure_validation(
    connection: sqlite3.Connection, version_id: str,
) -> dict[str, Any]:
    """The published receipt an approved selected version is closed on."""
    row = connection.execute(
        "SELECT validation_id, receipt_json FROM research_loop_validations "
        "WHERE version_id = ?",
        (version_id,),
    ).fetchone()
    if row is None:
        raise ProtocolError(
            "VALIDATION_NOT_PASSED", "close", "review",
            "The selected version has no published Validation receipt.",
        )
    receipt = json.loads(row["receipt_json"])
    return {
        "validation_id": receipt.get("validation_id", row["validation_id"]),
        "receipt_hash": receipt["receipt_hash"],
        "validation_kind": receipt.get(
            "validation_kind", VALIDATION_PROTOCOL_LEGACY["validation_kind"]
        ),
        "protocol": dict(receipt.get("protocol", VALIDATION_PROTOCOL_LEGACY)),
    }


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
        # Protocol-v2 closures also freeze the allowance at the stop, and a
        # selected version records the receipt it was approved on.  A protocol-v1
        # closure keeps the exact pre-#50 body -- even for a new closure -- so a
        # v1 round's closure identity never gains v2-only fields.
        protocol_v2 = "research_partition" in record["kickoff"]
        if protocol_v2:
            allowance = _validation_allowance(connection, record)
            if allowance is not None:
                body["validation_allowance"] = allowance
        if selected is not None:
            if selected not in versions:
                raise ProtocolError(
                    "VALIDATION_NOT_PASSED", "close", "review",
                    "The selected version is not locked in this research round.",
                )
            state = _review_state(connection, selected)
            if state == "ended":
                raise ProtocolError(
                    "VERSION_ENDED", "close", "inspect",
                    "An ended or retired version is terminal and cannot be selected "
                    "as the round's candidate; the round may cite a new version.",
                )
            if not _version_approved(connection, selected):
                raise ProtocolError(
                    "VALIDATION_NOT_PASSED", "close", "review",
                    "The selected version has no approving review decision.",
                )
            if protocol_v2:
                body["selected_validation"] = _closure_validation(connection, selected)
        closure_id = _hash(body)
        closure = {**body, "closure_id": closure_id}
        legacy_id = _hash(
            {
                key: value
                for key, value in body.items()
                if key not in {"validation_allowance", "selected_validation"}
            }
        )
        if previous is not None:
            if previous.get("closure_id") in {closure_id, legacy_id}:
                return {**previous, "status": "closed", "reused": True}
            raise ProtocolError(
                "ROUND_CLOSED", "close", "inspect",
                "The research round already has a different closure.",
            )
        connection.execute(
            "INSERT INTO research_loop_closures (round_id, closure_json) VALUES (?, ?)",
            (round_id, _json(closure)),
        )
    return {**closure, "status": "closed", "reused": False}
