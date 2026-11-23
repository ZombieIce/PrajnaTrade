"""Protected Validation and Final Holdout over sealed snapshots.

The workflow freezes a candidate, both protected partition fingerprints, the
execution rules, costs, thresholds, minimum samples, regimes and parameter
neighbourhood **before** opening Validation; requires a matching human
authorization and a verified OS-isolation probe; debits the sealed range in an
atomic SQLite ledger **before** the read; records the trusted evidence in an
immutable record; and publishes only a coarse receipt to the research project.

Final Holdout requires a *second*, separate human authorization that is only
granted after the same frozen plan's Validation evaluation passed.  Its warm-up
history comes from Discovery (research-visible) plus a derived export stored in
the Validation record, so the sealed Validation payload is never read twice.
The research-side receipt for Holdout is a constant, outcome-independent
document.

Research rounds preregister the shared Validation protocol and authorize a
version budget before any protected read. Their selected record binds the
frozen candidate used for a separately authorized Final Holdout.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import sqlite3
import statistics
import uuid
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from datetime import time as clock_time
from pathlib import Path
from typing import Any

import pandas as pd

from prajnatrade import snapshot
from prajnatrade.backtest import equal_weight_buy_and_hold
from prajnatrade.container_executor import ContainerStrategyExecutor, executor_from_config
from prajnatrade.data import ETF_SYMBOLS
from prajnatrade.experiment import (
    candidate_status,
    project_status,
    runtime_identity,
    signal_window_sessions,
)
from prajnatrade.isolation import IsolationError, probe_isolation_evidence
from prajnatrade.snapshot_baseline import (
    CN_TZ,
    available_by,
    run_partition_strategy,
)
from prajnatrade.strategies import make_strategy
from prajnatrade.validation import make_robustness_plan

SCHEMA_VERSION = 1
RESEARCH_SCHEMA_VERSION = 2
RESEARCH_VALIDATION = "research_validation"
RESEARCH_HOLDOUT = "research_holdout"
SCOPE = "synthetic"
VALIDATION_STAGE = "validation"
HOLDOUT_STAGE = "final_holdout"
STAGES = (VALIDATION_STAGE, HOLDOUT_STAGE)

THRESHOLD_METRICS = (
    "sessions",
    "valued_sessions",
    "total_return",
    "max_drawdown",
    "sharpe",
    "return_delta",
    "stress_total_return",
    "stress_return_delta",
    "fees_paid",
    "slippage_paid",
    "unfilled_orders",
)
THRESHOLD_OPERATORS = (">=", "<=", ">", "<", "==")
BENCHMARK_THRESHOLD_METRICS = {"total_return", "sharpe", "max_drawdown"}

REASON_THRESHOLD = "THRESHOLD_NOT_MET"
REASON_COVERAGE = "COVERAGE_INSUFFICIENT"
REASON_EVIDENCE = "EVIDENCE_INCOMPLETE"
EVIDENCED_OPEN_HALT_POLICY = "evidenced_open_halt_v1"
COARSE_MESSAGES = {
    REASON_THRESHOLD: "At least one preregistered hard threshold was not met.",
    REASON_COVERAGE: "Required coverage or minimum samples were incomplete.",
    REASON_EVIDENCE: "Required evidence was unavailable or incomplete.",
}

PLAN_INPUT_FIELDS = frozenset({
    "schema_version",
    "minimum_samples",
    "thresholds",
    "stress_costs",
    "regimes",
    "stages",
    "reason",
})

NOT_ADMISSION_STATEMENT = (
    "This synthetic trusted record is engineering evidence only; it does not "
    "grant admission, approve a release, or prove real-market performance."
)
REAL_EVALUATION_STATEMENT = (
    "This trusted record covers one preregistered protected stage. "
    "Admission requires the approvals and evidence bound to the frozen plan."
)

#: The research side learns exactly one thing about Final Holdout: the request
#: was received.  It is identical for completed, rejected, aborted, unauthorized
#: and re-submitted requests, and contains no outcome, timing or diagnostics.
HOLDOUT_RECEIPT = {
    "schema_version": SCHEMA_VERSION,
    "stage": HOLDOUT_STAGE,
    "status": "accepted",
    "message": "Request received.",
}

_QUERIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS consumed_queries (
  partition_name TEXT NOT NULL,
  partition_content_hash TEXT NOT NULL,
  plan_id TEXT NOT NULL,
  candidate_hash TEXT NOT NULL,
  stage TEXT NOT NULL,
  evaluation_id TEXT,
  status TEXT NOT NULL,
  authorization_id TEXT NOT NULL,
  debited_at TEXT NOT NULL,
  PRIMARY KEY (partition_name, partition_content_hash)
);
CREATE TABLE IF NOT EXISTS validation_queries (
  round_id TEXT NOT NULL,
  strategy_fingerprint TEXT NOT NULL,
  plan_id TEXT NOT NULL,
  candidate_hash TEXT NOT NULL,
  partition_content_hash TEXT NOT NULL,
  partition_name TEXT NOT NULL CHECK (partition_name = 'validation'),
  stage TEXT NOT NULL CHECK (stage = 'validation'),
  submission_id TEXT NOT NULL,
  experiment_id TEXT NOT NULL,
  proposal_id TEXT NOT NULL,
  evaluation_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK (status IN ('reading', 'completed', 'aborted')),
  authorization_id TEXT NOT NULL,
  debited_at TEXT NOT NULL,
  PRIMARY KEY (round_id, strategy_fingerprint)
);
CREATE TABLE IF NOT EXISTS evaluations (
  evaluation_id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL,
  stage TEXT NOT NULL,
  candidate_hash TEXT NOT NULL,
  partition_content_hash TEXT NOT NULL,
  outcome TEXT,
  record_path TEXT,
  created_at TEXT NOT NULL,
  completed_at TEXT
);
"""

_PROJECT_SCHEMA = """
CREATE TABLE IF NOT EXISTS evaluation_plans (
  plan_id TEXT PRIMARY KEY,
  candidate_id TEXT,
  plan_hash TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS evaluation_receipts (
  plan_id TEXT,
  stage TEXT,
  candidate_id TEXT,
  payload_json TEXT,
  published_at TEXT,
  PRIMARY KEY(plan_id, stage)
);
"""


class EvaluationRejection(Exception):
    """A machine-readable request rejection (never a strategy failure)."""

    def __init__(
        self,
        code: str,
        phase: str,
        next_action: str,
        message: str,
        *,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.details = {
            "code": code,
            "phase": phase,
            "next_action": next_action,
            "message": message,
        }
        self.context = dict(context or {})


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _hash(value: object) -> str:
    return snapshot._hash(value)


def _bytes(value: object) -> bytes:
    return snapshot._bytes(value)


def _store(path: str | Path) -> Path:
    resolved = snapshot._store_path(Path(path))
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _file_once(path: Path, record: dict) -> dict:
    """Create an immutable file once; return the already-stored object if present."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    import tempfile

    with tempfile.NamedTemporaryFile(dir=path.parent) as staged:
        staged.write(_bytes(record))
        staged.flush()
        os.fsync(staged.fileno())
        os.chmod(staged.name, 0o400)
        try:
            os.link(staged.name, path)
        except FileExistsError:
            value = snapshot._read_json(path)
            return value if isinstance(value, dict) else {}
    return record


def evaluation_store(path: str | Path) -> Path:
    """Resolve a trusted evaluation store outside the repository."""
    return _store(path)


def write_trusted_record_once(path: Path, value: dict[str, Any]) -> dict[str, Any]:
    """Write an immutable document to a trusted store."""
    return _file_once(path, value)


# ---------------------------------------------------------------------------
# Immutable record integrity
# ---------------------------------------------------------------------------


def _record_hash(record: dict[str, Any]) -> str:
    """Content address of a trusted record (the hash field itself excluded)."""
    return _hash({key: value for key, value in record.items() if key != "record_hash"})


def _verify_record(record: object) -> dict[str, Any]:
    """Verify a trusted record's content address; never fall back to a re-run."""
    if not isinstance(record, dict):
        raise ValueError("The stored evaluation record is not an object.")
    stored = record.get("record_hash")
    if not isinstance(stored, str) or stored != _record_hash(record):
        raise ValueError(
            "The stored evaluation record failed its integrity check "
            "(record_hash missing or mismatched)."
        )
    return record


def _record_matches(record: dict[str, Any], plan: dict[str, Any], stage: str) -> bool:
    fingerprints = record.get("fingerprints")
    if not isinstance(fingerprints, dict):
        return False
    partitions = fingerprints.get("partitions")
    expected = plan["partitions"][stage]["content_hash"]
    evaluated = fingerprints.get("evaluated_partition")
    if evaluated is None:
        # Aborted records never opened a scored partition descriptor; the
        # planned partition fingerprint is still bound in ``partitions``.
        staged = record.get("partitions")
        evaluated = (
            staged.get(stage, {}).get("content_hash")
            if isinstance(staged, dict) and isinstance(staged.get(stage), dict)
            else None
        )
    return (
        record.get("plan_id") == plan["plan_id"]
        and record.get("scope") == plan["scope"]
        and record.get("data_kind") == plan["data_kind"]
        and record.get("candidate_hash") == plan["candidate_hash"]
        and record.get("snapshot_id") == plan["snapshot_id"]
        and record.get("stage") == stage
        and record.get("partition") == stage
        and evaluated == expected
        and isinstance(partitions, dict)
        and partitions.get(VALIDATION_STAGE)
        == plan["partitions"][VALIDATION_STAGE]["content_hash"]
        and partitions.get(HOLDOUT_STAGE)
        == plan["partitions"][HOLDOUT_STAGE]["content_hash"]
        and record.get("schema_version") == plan.get("schema_version")
    )


class _EvidenceProblem(Exception):
    """A validation-evidence problem with a stable public error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _read_ledger_row(
    store: Path, partition: str, content_hash: str
) -> dict[str, Any] | None:
    """Read one consumed-query row without creating or modifying the ledger."""
    database = store / "queries.sqlite"
    if not database.is_file():
        return None
    connection = sqlite3.connect(database, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM consumed_queries "
            "WHERE partition_name = ? AND partition_content_hash = ?",
            (partition, content_hash),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    finally:
        connection.close()
    return None if row is None else dict(row)


def _load_stored_record(store: Path, evaluation_id: str) -> dict[str, Any]:
    path = store / "records" / evaluation_id / "record.json"
    if not path.is_file():
        raise _EvidenceProblem(
            "VALIDATION_EVIDENCE_INVALID",
            "The stored Validation evaluation record is missing.",
        )
    try:
        record = snapshot._read_json(path)
        return _verify_record(record)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise _EvidenceProblem(
            "VALIDATION_EVIDENCE_INVALID",
            f"The stored Validation evaluation record is unusable: {error}",
        ) from error


def _validation_prior_evidence(store: Path, plan: dict[str, Any]) -> dict[str, Any]:
    """Load and verify the Validation record that unlocks Final Holdout.

    This reads only the trusted Validation *record* (which carries the derived
    prior export for Holdout); it never opens the sealed Validation payload.
    """
    content_hash = plan["partitions"][VALIDATION_STAGE]["content_hash"]
    row = _read_ledger_row(store, VALIDATION_STAGE, content_hash)
    if row is None:
        raise _EvidenceProblem(
            "VALIDATION_NOT_PASSED",
            "No completed Validation evaluation exists for this frozen plan; "
            "Final Holdout cannot be authorized.",
        )
    if (
        row.get("plan_id") != plan["plan_id"]
        or row.get("candidate_hash") != plan["candidate_hash"]
        or row.get("stage") != VALIDATION_STAGE
    ):
        raise _EvidenceProblem(
            "VALIDATION_NOT_PASSED",
            "The Validation evaluation for this sealed range belongs to another "
            "frozen plan or candidate.",
        )
    if row.get("status") != "completed":
        raise _EvidenceProblem(
            "VALIDATION_NOT_PASSED",
            "The Validation evaluation did not complete; it can never authorize "
            "Final Holdout.",
        )
    evaluation_id = row.get("evaluation_id")
    if not isinstance(evaluation_id, str) or not evaluation_id:
        raise _EvidenceProblem(
            "VALIDATION_EVIDENCE_INVALID",
            "The Validation ledger row does not reference an evaluation record.",
        )
    record = _load_stored_record(store, evaluation_id)
    if record.get("evaluation_id") != evaluation_id or not _record_matches(
        record, plan, VALIDATION_STAGE
    ):
        raise _EvidenceProblem(
            "VALIDATION_EVIDENCE_INVALID",
            "The stored Validation record is not bound to this frozen plan, "
            "candidate, snapshot and partition fingerprint.",
        )
    if (
        record.get("outcome") != "passed"
        or record.get("scoring") is not True
        or record.get("aborted") is not False
    ):
        raise _EvidenceProblem(
            "VALIDATION_NOT_PASSED",
            "Only a passed, fully scored Validation evaluation can authorize "
            "Final Holdout.",
        )
    return _verified_validation_export(record, plan)


def _verified_validation_export(
    record: dict[str, Any], plan: dict[str, Any]
) -> dict[str, Any]:
    evaluation_id = record["evaluation_id"]
    content_hash = plan["partitions"][VALIDATION_STAGE]["content_hash"]
    export = record.get("prior_export_for_holdout")
    if not isinstance(export, dict):
        raise _EvidenceProblem(
            "VALIDATION_EVIDENCE_INVALID",
            "The stored Validation record predates the derived prior export "
            "required by Final Holdout; it is rejected rather than re-reading the "
            "sealed Validation payload.",
        )
    body = {key: value for key, value in export.items() if key != "export_hash"}
    if export.get("export_hash") != _hash(body):
        raise _EvidenceProblem(
            "VALIDATION_EVIDENCE_INVALID",
            "The Validation prior export failed its integrity check.",
        )
    if (
        export.get("source_stage") != VALIDATION_STAGE
        or export.get("evaluation_id") != evaluation_id
        or export.get("plan_id") != plan["plan_id"]
        or export.get("candidate_hash") != plan["candidate_hash"]
        or export.get("snapshot_id") != plan["snapshot_id"]
        or export.get("partition_content_hash") != content_hash
    ):
        raise _EvidenceProblem(
            "VALIDATION_EVIDENCE_INVALID",
            "The Validation prior export is not bound to this frozen plan and "
            "sealed Validation range.",
        )
    sessions = export.get("sessions")
    derived = export.get("derived")
    if (
        not isinstance(sessions, list)
        or not sessions
        or not all(isinstance(day, str) for day in sessions)
        or sessions != sorted(set(sessions))
        or not isinstance(derived, list)
        or not derived
        or export.get("signal_window_sessions") != plan["signal_window_sessions"]
        or export.get("regime_lookback") != plan["regimes"]["lookback"]
    ):
        raise _EvidenceProblem(
            "VALIDATION_EVIDENCE_INVALID",
            "The Validation prior export is incomplete for this plan's signal "
            "window and regime lookback.",
        )
    return {
        "evaluation_id": evaluation_id,
        "record_hash": record["record_hash"],
        "export": export,
        "export_hash": export["export_hash"],
        "partition_content_hash": content_hash,
        "record": record,
    }


# ---------------------------------------------------------------------------
# Plan freeze
# ---------------------------------------------------------------------------


def _reject(code: str, phase: str, next_action: str, message: str) -> EvaluationRejection:
    return EvaluationRejection(code, phase, next_action, message)


def _invalid(message: str) -> EvaluationRejection:
    return _reject("INVALID_PLAN_INPUT", "plan", "correct", message)


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid(f"{field} must be a number.")
    number = float(value)
    if not math.isfinite(number):
        raise _invalid(f"{field} must be finite.")
    return number


def _integer(value: object, field: str) -> int:
    if type(value) is not int:
        raise _invalid(f"{field} must be an integer.")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"{field} must be a non-empty string.")
    return value.strip()


def _exact_object(value: object, fields: set[str], name: str) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise _invalid(f"{name} must contain exactly: {', '.join(sorted(fields))}.")
    return value


def _plan_input(
    raw: object, base_costs: dict[str, float], *, research_round: bool = False
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise _invalid("Plan input must be a JSON object.")
    unknown = sorted(set(raw) - PLAN_INPUT_FIELDS - {"allow_discovery_halt_warmup"})
    if unknown:
        raise _invalid(
            "Plan input cannot restate bound fields or add unknown fields: "
            + ", ".join(unknown)
            + "."
        )
    missing = sorted(PLAN_INPUT_FIELDS - set(raw))
    if missing:
        raise _invalid("Plan input is missing: " + ", ".join(missing) + ".")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != SCHEMA_VERSION:
        raise _invalid("Plan input must use schema_version 1.")
    if raw["stages"] != list(STAGES):
        raise _invalid("The plan must fix exactly the validation and final_holdout stages.")
    reason = _text(raw["reason"], "reason")
    allow_discovery_halt_warmup = raw.get("allow_discovery_halt_warmup", False)
    if type(allow_discovery_halt_warmup) is not bool:
        raise _invalid("allow_discovery_halt_warmup must be a boolean.")

    minimum = _exact_object(
        raw["minimum_samples"], {"sessions", "valued_sessions"}, "minimum_samples"
    )
    sessions_min = _integer(minimum["sessions"], "minimum_samples.sessions")
    valued_min = _integer(minimum["valued_sessions"], "minimum_samples.valued_sessions")
    if sessions_min < 1 or valued_min < 1 or valued_min > sessions_min:
        raise _invalid(
            "minimum_samples requires 1 <= valued_sessions <= sessions."
        )

    if not isinstance(raw["thresholds"], list):
        raise _invalid("thresholds must be a list (possibly empty).")
    thresholds = []
    seen: set[str] = set()
    for entry in raw["thresholds"]:
        fields = {"id", "metric", "op", "value"}
        if isinstance(entry, dict) and "benchmark_multiplier" in entry:
            fields.add("benchmark_multiplier")
        item = _exact_object(entry, fields, "each threshold")
        identifier = _text(item["id"], "threshold.id")
        if identifier in seen:
            raise _invalid(f"Duplicate threshold id: {identifier}.")
        seen.add(identifier)
        if item["metric"] not in THRESHOLD_METRICS and not (
            research_round and item["metric"] == "turnover"
        ):
            raise _invalid(
                f"Threshold metric {item['metric']!r} is not in the frozen registry."
            )
        if item["op"] not in THRESHOLD_OPERATORS:
            raise _invalid(f"Threshold operator {item['op']!r} is not supported.")
        _number(item["value"], "threshold.value")
        if "benchmark_multiplier" in item:
            multiplier = _number(item["benchmark_multiplier"], "threshold.benchmark_multiplier")
            if item["metric"] not in BENCHMARK_THRESHOLD_METRICS or multiplier <= 0:
                raise _invalid("Benchmark thresholds require return, Sharpe or drawdown and a positive multiplier.")
        thresholds.append({**item, "id": identifier})

    stress = _exact_object(
        raw["stress_costs"],
        {"transaction_cost_bps", "slippage_bps"},
        "stress_costs",
    )
    stress_costs = {
        "transaction_cost_bps": _number(
            stress["transaction_cost_bps"], "stress_costs.transaction_cost_bps"
        ),
        "slippage_bps": _number(stress["slippage_bps"], "stress_costs.slippage_bps"),
    }
    for name, value in stress_costs.items():
        if value < 0 or value >= 10_000:
            raise _invalid(f"stress_costs.{name} must be between 0 and 10,000.")
        if value < base_costs[name]:
            raise _invalid(
                f"stress_costs.{name} must be at least the frozen base cost."
            )

    regimes = _exact_object(
        raw["regimes"],
        {"method", "lookback", "volatility_threshold", "labels"},
        "regimes",
    )
    if regimes["method"] != "trailing_volatility":
        raise _invalid("regimes.method must be 'trailing_volatility'.")
    lookback = _integer(regimes["lookback"], "regimes.lookback")
    if lookback < 2:
        raise _invalid("regimes.lookback must be at least 2.")
    volatility = _number(regimes["volatility_threshold"], "regimes.volatility_threshold")
    if volatility <= 0:
        raise _invalid("regimes.volatility_threshold must be positive.")
    labels = regimes["labels"]
    if (
        not isinstance(labels, list)
        or len(labels) != 2
        or any(not isinstance(label, str) or not label.strip() for label in labels)
        or len(set(labels)) != 2
    ):
        raise _invalid("regimes.labels must declare exactly two distinct labels.")
    return {
        "schema_version": SCHEMA_VERSION,
        "minimum_samples": {
            "sessions": sessions_min,
            "valued_sessions": valued_min,
        },
        "thresholds": thresholds,
        "stress_costs": stress_costs,
        "regimes": {
            "method": "trailing_volatility",
            "lookback": lookback,
            "volatility_threshold": volatility,
            "labels": [label.strip() for label in labels],
        },
        "stages": list(STAGES),
        "reason": reason,
        **({"allow_discovery_halt_warmup": True} if allow_discovery_halt_warmup else {}),
    }


def _base_costs(evaluation: dict[str, Any]) -> dict[str, float]:
    return {
        key: float(evaluation[key])
        for key in ("transaction_cost_bps", "slippage_bps")
    }


def normalize_validation_protocol(raw: object, campaign_evaluation: dict[str, Any]) -> dict[str, Any]:
    """Verify and normalize the frozen comparison protocol before kickoff."""
    return _plan_input(raw, _base_costs(campaign_evaluation), research_round=True)


def _read_plan_input(source: Path) -> object:
    try:
        return snapshot._read_json(Path(source))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise _invalid(f"Plan input could not be read: {error}") from error


def _signal_window(candidate: dict[str, Any], parameters: dict[str, Any] | None = None) -> int:
    return signal_window_sessions({
        **candidate["strategy"],
        "parameters": candidate["parameters"] if parameters is None else parameters,
    })


def _neighbourhood(candidate: dict[str, Any]) -> dict[str, Any]:
    plan = make_robustness_plan(
        {
            **candidate["strategy"],
            "parameters": candidate["parameters"],
        },
        None,
    )
    if plan.get("status") == "not_applicable":
        return {"no_tunable_parameters": True}
    return plan


def _plan_mapping(project: Path, plan_id: str) -> str | None:
    database = Path(project) / ".prajnatrade" / "project.sqlite"
    try:
        with sqlite3.connect(database, timeout=30) as connection:
            row = connection.execute(
                "SELECT candidate_id FROM evaluation_plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    return None if row is None else str(row[0])


def _register_plan(project: Path, plan: dict[str, Any]) -> None:
    database = Path(project) / ".prajnatrade" / "project.sqlite"
    with sqlite3.connect(database, timeout=30) as connection:
        connection.executescript(_PROJECT_SCHEMA)
        row = connection.execute(
            "SELECT candidate_id FROM evaluation_plans WHERE plan_id = ?",
            (plan["plan_id"],),
        ).fetchone()
        if row is not None and row[0] != plan["candidate_id"]:
            raise ValueError("The stored evaluation plan binding conflicts with this plan.")
        connection.execute(
            "INSERT OR IGNORE INTO evaluation_plans "
            "(plan_id, candidate_id, plan_hash, created_at) VALUES (?, ?, ?, ?)",
            (
                plan["plan_id"],
                plan["candidate_id"],
                plan["plan_id"],
                plan["frozen_at"],
            ),
        )


def _plan_fingerprint(plan: dict[str, Any]) -> str:
    body = {
        key: value
        for key, value in plan.items()
        if key not in {"plan_id", "frozen_at"}
    }
    return _hash(body)


def _build_plan(
    project_id: str,
    candidate: dict[str, Any],
    metadata: dict[str, Any],
    payload: dict[str, Any],
    *,
    snapshot_store: Path,
    workflow: str | None = None,
) -> dict[str, Any]:
    snapshot_id = candidate.get("snapshot_id")
    evaluation = candidate.get("evaluation")
    if not isinstance(evaluation, dict) or evaluation.get("snapshot_id") != snapshot_id:
        raise ValueError("The frozen candidate snapshot reference is inconsistent.")
    data_kind = metadata.get("data_kind")
    if (
        data_kind not in (SCOPE, "real")
        or snapshot.SCHEMA_VERSION.get(metadata.get("schema_version")) != data_kind
        or evaluation.get("data_kind", SCOPE) != data_kind
    ):
        raise _invalid("The frozen candidate and snapshot data kind must match.")
    partitions = {
        name: snapshot.partition_descriptor(metadata, name) for name in STAGES
    }
    discovery = snapshot.partition_descriptor(metadata, "discovery")
    if evaluation.get("partition_content_hash") != discovery["content_hash"]:
        raise ValueError(
            "The frozen candidate Discovery fingerprint is not this snapshot's Discovery content."
        )
    if data_kind == "real" and not payload["thresholds"]:
        raise _invalid("Real Validation requires preregistered hard thresholds.")
    family = candidate["strategy"]["family"]
    neighbourhood = _neighbourhood(candidate)
    executor_config = evaluation.get("strategy_executor", {"kind": "none"})
    if evaluation.get("runtime") != runtime_identity(executor_config):
        raise _invalid("The candidate's frozen runtime is unavailable for protected evaluation.")
    if family == "python":
        executor_from_config(executor_config)
        if neighbourhood.get("status") == "missing_parameter_schema":
            raise _invalid("Frozen Python strategy must declare its parameter neighbourhood or no tunable parameters.")
    plan = {
        "schema_version": RESEARCH_SCHEMA_VERSION if workflow else SCHEMA_VERSION,
        **({"workflow": workflow} if workflow else {}),
        "scope": data_kind,
        "data_kind": data_kind,
        "project_id": project_id,
        "candidate_id": candidate["candidate_id"],
        "candidate_hash": candidate["candidate_hash"],
        "candidate": candidate,
        "snapshot_id": snapshot_id,
        "partitions": partitions,
        "prior_history": {
            VALIDATION_STAGE: {
                "partitions": ["discovery"],
                "discovery_content_hash": discovery["content_hash"],
                "as_of": evaluation.get("as_of"),
            },
            HOLDOUT_STAGE: {"partitions": ["discovery", VALIDATION_STAGE]},
        },
        "stages": list(STAGES),
        "conclusion": candidate["conclusion"],
        "runtime": candidate["fingerprints"]["runtime_dependencies"],
        "signal_window_sessions": _signal_window(candidate),
        "initial_cash": float(evaluation["initial_cash"]),
        "base_costs": _base_costs(evaluation),
        "stress_costs": payload["stress_costs"],
        "execution_rules": candidate["execution_rules"],
        "baseline": {
            "method": "equal_weight_buy_and_hold",
            "weights": "equal",
            "same_signal_window": workflow is None,
            "same_execution_rules": True,
            "same_costs": True,
            **({"signal_window_sessions": 1} if workflow else {}),
        },
        "thresholds": payload["thresholds"],
        "minimum_samples": payload["minimum_samples"],
        "regimes": payload["regimes"],
        "neighborhood": neighbourhood,
        "reason": payload["reason"],
    }
    if data_kind == "real":
        plan["coverage_policy"] = EVIDENCED_OPEN_HALT_POLICY
    if workflow == RESEARCH_HOLDOUT:
        plan["prior_history"] = payload["prior_history"]
    elif payload.get("allow_discovery_halt_warmup"):
        if data_kind != "real":
            raise _invalid("Discovery halt warm-up requires a schema-v2 snapshot.")
        warmup = _freeze_halt_warmup(snapshot_store, plan)
        if warmup:
            plan["prior_history"][VALIDATION_STAGE]["halt_warmup"] = warmup
    return plan


def _freeze_plan(store: Path, plan: dict[str, Any]) -> dict[str, Any]:
    plan_id = _plan_fingerprint(plan)
    frozen = {**plan, "plan_id": plan_id, "frozen_at": _now()}
    stored = _file_once(store / "plans" / f"{plan_id}.json", frozen)
    if stored.get("plan_id") != plan_id or _plan_fingerprint(stored) != plan_id:
        raise ValueError("Existing frozen plan integrity check failed.")
    return stored


def create_plan(
    store: str | Path,
    snapshot_store: str | Path,
    project: str | Path,
    candidate_id: str,
    source: str | Path,
) -> dict[str, Any]:
    """Freeze the evaluation plan before Validation is opened."""
    from prajnatrade.research_direction import campaign_has_kickoff

    eval_store = _store(store)
    project = Path(project)
    project_id = str(project_status(project)["project_id"])
    candidate = candidate_status(project, candidate_id)
    if candidate.get("research_selection") or campaign_has_kickoff(
        eval_store, project_id, candidate["campaign_id"],
    ):
        raise _invalid("This Campaign uses the direction-bound research workflow.")
    snapshot_id = candidate.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise ValueError("The frozen candidate does not reference a sealed snapshot.")
    evaluation = candidate.get("evaluation")
    if not isinstance(evaluation, dict):
        raise ValueError("The frozen candidate is missing its evaluation settings.")
    metadata = snapshot.manifest(Path(snapshot_store), snapshot_id)
    data_kind = metadata["data_kind"]
    base_costs = _base_costs(evaluation)
    payload = _plan_input(_read_plan_input(Path(source)), base_costs)
    plan = _build_plan(project_id, candidate, metadata, payload, snapshot_store=Path(snapshot_store))
    stored = _freeze_plan(eval_store, plan)
    _register_plan(project, stored)
    plan_id = stored["plan_id"]
    partitions = stored["partitions"]
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "planned",
        "scope": data_kind,
        "data_kind": data_kind,
        "plan_id": plan_id,
        "plan_hash": plan_id,
        "candidate_id": candidate_id,
        "candidate_hash": candidate["candidate_hash"],
        "snapshot_id": snapshot_id,
        "stages": list(STAGES),
        "fingerprints": {
            "plan": plan_id,
            "candidate": candidate["candidate_hash"],
            "snapshot_manifest": snapshot_id,
            "partitions": {
                "validation": partitions[VALIDATION_STAGE]["content_hash"],
                "final_holdout": partitions[HOLDOUT_STAGE]["content_hash"],
            },
        },
        "partitions": partitions,
        "base_costs": base_costs,
        "stress_costs": stored["stress_costs"],
        "thresholds": stored["thresholds"],
        "minimum_samples": stored["minimum_samples"],
        "regimes": stored["regimes"],
        "neighborhood": stored["neighborhood"],
        "signal_window_sessions": stored["signal_window_sessions"],
        "initial_cash": stored["initial_cash"],
        "execution_rules": stored["execution_rules"],
        "baseline": stored["baseline"],
        "conclusion": stored["conclusion"],
        "reason": stored["reason"],
        "frozen_at": stored["frozen_at"],
        "plan": stored,
    }


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


def _load_plan(store: Path, plan_id: str) -> dict[str, Any]:
    if not isinstance(plan_id, str) or not plan_id:
        raise _reject("PLAN_NOT_FOUND", "plan", "check_id", "Unknown evaluation plan.")
    path = store / "plans" / f"{plan_id}.json"
    if not path.is_file():
        raise _reject("PLAN_NOT_FOUND", "plan", "check_id", "Unknown evaluation plan.")
    plan = snapshot._read_json(path)
    if not isinstance(plan, dict):
        raise _reject(
            "FINGERPRINT_MISMATCH", "plan", "inspect", "The frozen plan is not an object."
        )
    if plan.get("plan_id") != plan_id or _plan_fingerprint(plan) != plan_id:
        raise _reject(
            "FINGERPRINT_MISMATCH",
            "plan",
            "inspect",
            "The frozen plan does not match its content address.",
        )
    return plan


def load_plan(store: Path, plan_id: str) -> dict[str, Any]:
    """Public trusted loader for one frozen plan (no sealed read, no debit)."""
    return _load_plan(store, plan_id)


def _authorization_files(directory: Path, stage: str) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(directory.glob(f"{stage}-*.json"))


def _verified_authorization(path: Path) -> dict[str, Any]:
    record = snapshot._read_json(path)
    if not isinstance(record, dict):
        raise _reject(
            "AUTHORIZATION_REQUIRED",
            "run",
            "authorize",
            "The stored authorization is not an object.",
        )
    auth_id = record.get("auth_id")
    body = {key: value for key, value in record.items() if key != "auth_id"}
    if not isinstance(auth_id, str) or _hash(body) != auth_id:
        raise _reject(
            "AUTHORIZATION_REQUIRED",
            "run",
            "authorize",
            "The stored authorization failed its integrity check.",
        )
    return record


def _authorizations(store: Path, plan_id: str, stage: str) -> list[dict[str, Any]]:
    directory = store / "authorizations" / plan_id
    marker = directory / f"{stage}.authorized.json"
    if marker.is_file():
        claim = snapshot._read_json(marker)
        if not isinstance(claim, dict) or claim.get("stage") != stage:
            raise _reject(
                "AUTHORIZATION_REQUIRED",
                "run",
                "authorize",
                "The authorization marker is invalid.",
            )
        name = claim.get("file")
        if not isinstance(name, str):
            raise _reject(
                "AUTHORIZATION_REQUIRED",
                "run",
                "authorize",
                "The authorization marker is invalid.",
            )
        path = directory / name
        if not path.is_file():
            raise _reject(
                "AUTHORIZATION_REQUIRED",
                "run",
                "authorize",
                "The authorized record is missing.",
            )
        record = _verified_authorization(path)
        expected_id = name[len(stage) + 1 : -len(".json")]
        if record.get("auth_id") != expected_id:
            raise _reject(
                "AUTHORIZATION_REQUIRED",
                "run",
                "authorize",
                "The authorized record does not match its file name.",
            )
        return [record]
    return [
        _verified_authorization(path)
        for path in _authorization_files(directory, stage)
    ]


def authorize(
    store: str | Path,
    plan_id: str,
    stage: str,
    reviewer: str,
    reason: str,
) -> dict[str, Any]:
    """Freeze one immutable stage authorization.

    Validation may be authorized any time after the plan is frozen.  Final
    Holdout additionally requires the same plan's completed, fully scored and
    **passed** Validation record, and refuses a second Holdout authorization.
    A rejected or insufficient Validation never authorizes Holdout.
    """
    if stage not in STAGES:
        raise _reject("UNSUPPORTED_STAGE", "authorization", "correct", "Unknown stage.")
    reviewer = reviewer.strip()
    reason = reason.strip()
    if not reviewer or not reason:
        raise _reject(
            "INVALID_AUTHORIZATION",
            "authorization",
            "correct",
            "Authorization requires a reviewer and a reason.",
        )
    eval_store = _store(store)
    plan = _load_plan(eval_store, plan_id)
    if plan.get("workflow"):
        raise _reject(
            "RESEARCH_WORKFLOW_REQUIRED", "authorization", "correct",
            "Use the research workflow to authorize this plan.",
        )
    directory = eval_store / "authorizations" / plan_id
    context = {
        "scope": plan["scope"],
        "plan_id": plan_id,
        "stage": stage,
        "candidate_id": plan.get("candidate_id"),
        "candidate_hash": plan.get("candidate_hash"),
        "snapshot_id": plan.get("snapshot_id"),
        "partition": stage,
        "partition_content_hash": plan["partitions"][stage]["content_hash"],
    }

    def reject(
        code: str, message: str, *, next_action: str = "inspect"
    ) -> None:
        audit_id = _audit(
            eval_store,
            context,
            code,
            message,
            command="evaluation authorize",
            phase="authorization",
        )
        raise EvaluationRejection(
            code,
            "authorization",
            next_action,
            message,
            context={
                "audit_id": audit_id,
                "opened_protected_source": False,
                "query_debited": False,
            },
        )

    if stage == HOLDOUT_STAGE and plan.get("stages") != list(STAGES):
        reject(
            "UNSUPPORTED_STAGE",
            "The frozen plan does not fix the Final Holdout stage.",
            next_action="stop",
        )
    marker = directory / f"{stage}.authorized.json"
    if marker.is_file() or _authorization_files(directory, stage):
        raise _reject(
            "AUTHORIZATION_CONFLICT",
            "authorization",
            "stop",
            "This stage already has an authorization; the first one is the one used.",
        )

    validation_binding = None
    if stage == HOLDOUT_STAGE:
        try:
            evidence = _validation_prior_evidence(eval_store, plan)
        except _EvidenceProblem as problem:
            reject(
                problem.code,
                problem.message,
                next_action=(
                    "run-validation"
                    if problem.code == "VALIDATION_NOT_PASSED"
                    else "inspect"
                ),
            )
        validation_binding = {
            "evaluation_id": evidence["evaluation_id"],
            "record_hash": evidence["record_hash"],
            "outcome": "passed",
            "partition": VALIDATION_STAGE,
            "partition_content_hash": evidence["partition_content_hash"],
            "export_hash": evidence["export_hash"],
        }

    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    body = {
        "schema_version": SCHEMA_VERSION,
        "scope": plan["scope"],
        "plan_id": plan_id,
        "plan_hash": plan_id,
        "candidate_id": plan["candidate_id"],
        "candidate_hash": plan["candidate_hash"],
        "snapshot_id": plan["snapshot_id"],
        "stage": stage,
        "partition": stage,
        "partition_content_hash": plan["partitions"][stage]["content_hash"],
        "reviewer": reviewer,
        "reason": reason,
        "authorized_at": _now(),
    }
    if validation_binding is not None:
        body["validation"] = validation_binding
    auth_id = _hash(body)
    record = {**body, "auth_id": auth_id}
    name = f"{stage}-{auth_id}.json"
    stored = _file_once(directory / name, record)
    if stored != record:
        raise _reject(
            "AUTHORIZATION_CONFLICT",
            "authorization",
            "stop",
            "A different authorization with the same identity is already stored.",
        )
    claim = _file_once(
        directory / f"{stage}.authorized.json",
        {
            "schema_version": SCHEMA_VERSION,
            "stage": stage,
            "plan_id": plan_id,
            "authorization_id": auth_id,
            "file": name,
        },
    )
    if claim.get("authorization_id") != auth_id:
        try:
            (directory / name).unlink()
        except FileNotFoundError:
            pass
        raise _reject(
            "AUTHORIZATION_CONFLICT",
            "authorization",
            "stop",
            "This stage already has an authorization; the first one is the one used.",
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "authorized",
        "scope": plan["scope"],
        "plan_id": plan_id,
        "stage": stage,
        "authorization": stored,
    }


def authorization_matches(
    record: dict[str, Any], plan: dict[str, Any], stage: str
) -> bool:
    """Check whether a stored authorization matches the trusted plan for its stage."""
    # Kickoff authorizations derive their scope from the verified round/plan binding.
    scope = record.get("scope")
    if scope is None and plan.get("workflow") == RESEARCH_VALIDATION and record.get("authorization_kind") == "research_kickoff":
        scope = plan["scope"]
    matched = (
        scope == plan["scope"]
        and record.get("plan_id") == plan["plan_id"]
        and record.get("plan_hash") == plan["plan_id"]
        and record.get("candidate_id") == plan["candidate_id"]
        and record.get("candidate_hash") == plan["candidate_hash"]
        and record.get("snapshot_id") == plan["snapshot_id"]
        and record.get("stage") == stage
        and record.get("partition") == stage
        and record.get("partition_content_hash")
        == plan["partitions"][stage]["content_hash"]
    )
    if not matched or stage != HOLDOUT_STAGE:
        return matched
    binding = record.get("validation")
    return (
        isinstance(binding, dict)
        and binding.get("outcome") == "passed"
        and binding.get("partition") == VALIDATION_STAGE
        and binding.get("partition_content_hash")
        == plan["partitions"][VALIDATION_STAGE]["content_hash"]
    )


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


def _ledger(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.executescript(_QUERIES_SCHEMA)
    return connection


def _claim_partition(
    store: Path, snapshot_store: Path, plan: dict[str, Any], stage: str,
    authorization: dict[str, Any],
) -> dict[str, Any]:
    """Atomically reserve the sealed range before the protected read."""
    content_hash = plan["partitions"][stage]["content_hash"]
    evaluation_id = str(uuid.uuid4())
    debited_at = _now()
    connection = _ledger(store / "queries.sqlite")
    try:
        row = None
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM consumed_queries "
                "WHERE partition_name = ? AND partition_content_hash = ?",
                (stage, content_hash),
            ).fetchone()
            if row is None:
                from prajnatrade.research_holdout import legacy_conflict

                if legacy_conflict(connection, plan, stage):
                    connection.execute("ROLLBACK")
                    return {"state": "consumed"}
                if not any(
                    (snapshot_store / snapshot_id / f"{stage}.json").is_file()
                    for snapshot_id in snapshot.lineage_ids(snapshot_store, plan["snapshot_id"])
                ):
                    connection.execute("ROLLBACK")
                    return {"state": "source_missing"}
                connection.execute(
                    "INSERT INTO consumed_queries "
                    "(partition_name, partition_content_hash, plan_id, candidate_hash, "
                    " stage, evaluation_id, status, authorization_id, debited_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'reading', ?, ?)",
                    (
                        stage,
                        content_hash,
                        plan["plan_id"],
                        plan["candidate_hash"],
                        stage,
                        evaluation_id,
                        authorization["auth_id"],
                        debited_at,
                    ),
                )
                connection.execute(
                    "INSERT INTO evaluations "
                    "(evaluation_id, plan_id, stage, candidate_hash, "
                    " partition_content_hash, outcome, record_path, created_at, completed_at) "
                    "VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, NULL)",
                    (
                        evaluation_id,
                        plan["plan_id"],
                        stage,
                        plan["candidate_hash"],
                        content_hash,
                        debited_at,
                    ),
                )
                connection.execute("COMMIT")
                return {
                    "state": "debited",
                    "evaluation_id": evaluation_id,
                    "debited_at": debited_at,
                }
            connection.execute("ROLLBACK")
        except sqlite3.IntegrityError:
            connection.execute("ROLLBACK")
            row = connection.execute(
                "SELECT * FROM consumed_queries "
                "WHERE partition_name = ? AND partition_content_hash = ?",
                (stage, content_hash),
            ).fetchone()
        if row is None:
            raise ValueError("The query ledger could not be read after a concurrent debit.")
        return _existing_claim(connection, store, row, plan, stage)
    finally:
        connection.close()


def _existing_claim(
    connection: sqlite3.Connection,
    store: Path,
    row: sqlite3.Row,
    plan: dict[str, Any],
    stage: str,
) -> dict[str, Any]:
    same = (
        row["plan_id"] == plan["plan_id"]
        and row["candidate_hash"] == plan["candidate_hash"]
        and row["stage"] == stage
    )
    evaluation = None
    if row["evaluation_id"]:
        evaluation = connection.execute(
            "SELECT * FROM evaluations WHERE evaluation_id = ?",
            (row["evaluation_id"],),
        ).fetchone()
    record_path = None
    if evaluation is not None:
        for candidate_path in (
            Path(evaluation["record_path"]) if evaluation["record_path"] else None,
            store / "records" / str(row["evaluation_id"]) / "record.json"
            if row["evaluation_id"]
            else None,
        ):
            if candidate_path is not None and candidate_path.is_file():
                record_path = candidate_path
                break
    if same and record_path is not None:
        record = snapshot._read_json(record_path)
        if isinstance(record, dict):
            return {
                "state": "reuse",
                "evaluation_id": row["evaluation_id"],
                "debited_at": row["debited_at"],
                "record": record,
                "record_path": record_path,
            }
    if same:
        return {
            "state": "in_progress" if row["status"] == "reading" else "unrecorded",
            "evaluation_id": row["evaluation_id"],
            "debited_at": row["debited_at"],
        }
    return {"state": "consumed", "evaluation_id": row["evaluation_id"]}


def _mark_state(
    store: Path,
    evaluation_id: str,
    stage: str,
    content_hash: str,
    *,
    status: str,
    outcome: str,
    record_path: Path,
    completed_at: str,
) -> None:
    connection = _ledger(store / "queries.sqlite")
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE consumed_queries SET status = ?, evaluation_id = ? "
            "WHERE partition_name = ? AND partition_content_hash = ?",
            (status, evaluation_id, stage, content_hash),
        )
        connection.execute(
            "UPDATE evaluations SET outcome = ?, record_path = ?, completed_at = ? "
            "WHERE evaluation_id = ?",
            (outcome, str(record_path), completed_at, evaluation_id),
        )
        connection.execute("COMMIT")
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Research receipt
# ---------------------------------------------------------------------------


def _coarse_receipt(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "stage": record["stage"],
        "status": record["outcome"],
        "reason": record["reason"],
        "message": record["message"],
    }


def _receipt_for_record(record: dict[str, Any]) -> dict[str, Any]:
    """The research-side receipt for one trusted record.

    Validation publishes its coarse, outcome-bearing receipt.  Final Holdout
    publishes the same constant document for every outcome, so the research side
    cannot distinguish completed, rejected, aborted, unauthorized or replayed
    requests.
    """
    if record.get("stage") == HOLDOUT_STAGE:
        return dict(HOLDOUT_RECEIPT)
    return _coarse_receipt(record)


def _project_database(project: Path) -> sqlite3.Connection:
    database = Path(project) / ".prajnatrade" / "project.sqlite"
    connection = sqlite3.connect(database, timeout=30)
    connection.executescript(_PROJECT_SCHEMA)
    return connection


def _publish_receipt(
    project: Path, plan: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    with _project_database(project) as connection:
        # The research receipt is the coarse view of the latest completed
        # evaluation of this frozen plan.  It is normally unchanged on replay
        # because a replay reuses the stored record instead of re-evaluating.
        connection.execute(
            "INSERT INTO evaluation_receipts "
            "(plan_id, stage, candidate_id, payload_json, published_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(plan_id, stage) DO UPDATE SET "
            "payload_json = excluded.payload_json, "
            "candidate_id = excluded.candidate_id, "
            "published_at = excluded.published_at "
            "WHERE evaluation_receipts.payload_json != excluded.payload_json",
            (
                plan["plan_id"],
                payload["stage"],
                plan["candidate_id"],
                json.dumps(payload, sort_keys=True, allow_nan=False),
                _now(),
            ),
        )
        row = connection.execute(
            "SELECT payload_json FROM evaluation_receipts "
            "WHERE plan_id = ? AND stage = ?",
            (plan["plan_id"], payload["stage"]),
        ).fetchone()
    return json.loads(row[0]) if row else payload


def agent_evaluation_status(project: str | Path, candidate_id: str) -> dict[str, Any]:
    """Research-side view: a coarse Validation receipt and a constant Holdout receipt.

    Nothing from a trusted record is exposed here.  The Final Holdout value is
    the same four-key document for pending, authorized, completed, rejected,
    aborted and re-submitted requests.
    """
    project = Path(project)
    project_status(project)
    validation: dict[str, Any] = {"status": "pending"}
    database = project / ".prajnatrade" / "project.sqlite"
    try:
        with sqlite3.connect(database, timeout=30) as connection:
            row = connection.execute(
                "SELECT payload_json FROM evaluation_receipts "
                "WHERE candidate_id = ? AND stage = ? "
                "ORDER BY published_at DESC LIMIT 1",
                (candidate_id, VALIDATION_STAGE),
            ).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row is not None:
        validation = json.loads(row[0])
    return {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "validation": validation,
        "final_holdout": dict(HOLDOUT_RECEIPT),
    }


#: Backwards-compatible name for the MVP-10 caller.
agent_validation_status = agent_evaluation_status


def evaluation_status(store: str | Path, plan_id: str) -> dict[str, Any]:
    """Operator view of the frozen plan, authorizations, ledger and records."""
    eval_store = _store(store)
    plan = _load_plan(eval_store, plan_id)
    if plan.get("workflow") == RESEARCH_HOLDOUT:
        from prajnatrade.evaluation_report import build_evaluation_report

        report = build_evaluation_report(eval_store, plan_id)
        return {
            "schema_version": 2, "scope": plan["scope"], "plan_id": plan_id, "plan": plan,
            "authorizations": report["authorizations"], "queries": report["query_receipts"],
            "stages": report["stages"], "verdict": report["verdict"],
            "blockers": report["holdout_blockers"],
        }
    if plan.get("workflow") == RESEARCH_VALIDATION:
        return {**research_validation_status(eval_store, plan["round_id"]), "plan": plan}
    authorizations = []
    for stage in STAGES:
        authorizations.extend(_authorizations(eval_store, plan_id, stage))
    connection = _ledger(eval_store / "queries.sqlite")
    try:
        queries = [
            {
                "partition": row["partition_name"],
                "content_hash": row["partition_content_hash"],
                "plan_id": row["plan_id"],
                "candidate_hash": row["candidate_hash"],
                "status": row["status"],
                "authorization_id": row["authorization_id"],
                "evaluation_id": row["evaluation_id"],
                "debited_at": row["debited_at"],
            }
            for row in connection.execute(
                "SELECT * FROM consumed_queries WHERE plan_id = ? ORDER BY debited_at",
                (plan_id,),
            )
        ]
        evaluations = []
        for row in connection.execute(
            "SELECT * FROM evaluations WHERE plan_id = ? ORDER BY created_at",
            (plan_id,),
        ):
            record = None
            integrity = "unavailable"
            if row["record_path"] and Path(row["record_path"]).is_file():
                try:
                    record = _verify_record(
                        snapshot._read_json(Path(row["record_path"]))
                    )
                    integrity = "valid"
                except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                    record = None
                    integrity = "invalid"
            evaluations.append(
                {
                    "evaluation_id": row["evaluation_id"],
                    "stage": row["stage"],
                    "outcome": row["outcome"],
                    "record_path": row["record_path"],
                    "created_at": row["created_at"],
                    "completed_at": row["completed_at"],
                    "scoring": record.get("scoring") if isinstance(record, dict) else None,
                    "record_hash": (
                        record.get("record_hash") if isinstance(record, dict) else None
                    ),
                    "record_integrity": integrity,
                }
            )
    finally:
        connection.close()
    stages = {}
    for stage in STAGES:
        stage_authorization = next(
            (item for item in authorizations if item.get("stage") == stage), None
        )
        stage_query = next(
            (item for item in queries if item["partition"] == stage), None
        )
        stage_evaluation = next(
            (item for item in evaluations if item["stage"] == stage), None
        )
        stages[stage] = {
            "authorization": stage_authorization,
            "query": stage_query,
            "evaluation": stage_evaluation,
            "authorized": stage_authorization is not None,
            "query_debited": stage_query is not None,
            "record_path": (
                stage_evaluation["record_path"] if stage_evaluation else None
            ),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": plan["scope"],
        "plan_id": plan_id,
        "plan": plan,
        "authorizations": authorizations,
        "queries": queries,
        "evaluations": evaluations,
        "stages": stages,
    }


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def _audit(
    store: Path,
    context: dict[str, Any],
    code: str,
    message: str,
    *,
    command: str = "evaluation run",
    phase: str = "run",
) -> str | None:
    body = {
        "schema_version": SCHEMA_VERSION,
        "scope": SCOPE,
        "received_at": _now(),
        "command": command,
        **context,
        "error": {
            "code": code,
            "phase": phase,
            "next_action": "inspect",
            "message": message,
        },
        "opened_protected_source": False,
        "query_debited": False,
    }
    audit_id = _hash(body)
    record = {**body, "audit_id": audit_id}
    try:
        _file_once(store / "audit" / f"{audit_id}.json", record)
    except OSError:
        return None
    return audit_id


def audit(
    store: Path,
    context: dict[str, Any],
    code: str,
    message: str,
    *,
    command: str = "evaluation run",
    phase: str = "run",
) -> str | None:
    """Public trusted-side refusal audit writer (no sealed read, no debit)."""
    return _audit(store, context, code, message, command=command, phase=phase)


def _failpoint(name: str) -> None:
    if os.environ.get("PRAJNATRADE_TEST_FAILPOINT") == name:
        raise RuntimeError(f"synthetic test failpoint {name!r} reached")


def _partition_gaps(metadata: dict[str, Any], descriptor: dict[str, Any]) -> dict[str, list]:
    gaps = metadata.get("gaps")
    if not isinstance(gaps, dict):
        gaps = metadata.get("base_gaps")
    if not isinstance(gaps, dict):
        gaps = {}
    start, end = descriptor["start"], descriptor["end"]
    return {
        kind: [
            gap
            for gap in gaps.get(kind, [])
            if isinstance(gap, dict) and start <= str(gap.get("date", "")) <= end
        ]
        for kind in ("raw_bars", "derived", "corporate_actions", "availability")
    }


def _evidenced_open_halts(raw_bars: list[dict]) -> dict[tuple[str, str], dict]:
    """Identify rows whose only missing field is an evidenced non-executable open."""
    result = {}
    for row in raw_bars:
        if (
            row["trading_status"] != "partial_halt"
            or row["open"] is not None
            or any(row[field] is None for field in ("high", "low", "close", "volume"))
            or not row["available_at"]
            or not row["availability_evidence"]
        ):
            continue
        day = date.fromisoformat(row["date"])
        session_open = datetime.combine(day, clock_time(9, 30), tzinfo=CN_TZ)
        halt = row["halt"]
        resume = datetime.fromisoformat(halt["end"])
        open_available = datetime.fromisoformat(row["open_available_at"])
        close_available = datetime.fromisoformat(row["close_available_at"])
        if (
            datetime.fromisoformat(halt["start"]) <= session_open < resume
            and open_available == resume
            and resume <= close_available
            and close_available.astimezone(CN_TZ).date() == day
        ):
            result[(row["date"], row["symbol"])] = {
                "date": row["date"],
                "symbol": row["symbol"],
                "halt": halt,
                "availability_evidence": row["availability_evidence"],
            }
    return result


def _verified_discovery(snapshot_store: Path, plan: dict[str, Any]) -> dict:
    rule = plan["prior_history"][VALIDATION_STAGE]
    receipt = snapshot.read_discovery(
        snapshot_store, plan["snapshot_id"], "discovery", rule.get("as_of"),
    )
    content_hash = receipt.get("partition_content_hash", receipt["content_hash"])
    if (
        content_hash != rule["discovery_content_hash"]
        or receipt["content_hash"] != plan["candidate"]["fingerprints"]["snapshot"]
    ):
        raise ValueError("The frozen Discovery prior history differs from the sealed snapshot.")
    return receipt


def _freeze_halt_warmup(snapshot_store: Path, plan: dict[str, Any]) -> dict | None:
    """Bind only full-halt gaps in the already visible Discovery tail."""
    data = _verified_discovery(snapshot_store, plan)["data"]
    prior_needed = plan["signal_window_sessions"] - 1
    sessions = sorted({row["date"] for row in data["calendar"]})
    if prior_needed == 0 and plan.get("workflow") == RESEARCH_VALIDATION:
        return None
    if not 0 < prior_needed <= len(sessions):
        raise _invalid("The frozen signal window has no verifiable Discovery warm-up.")
    tail = sessions[-prior_needed:]
    cutoff = datetime.combine(
        date.fromisoformat(plan["partitions"][VALIDATION_STAGE]["start"]),
        clock_time(9, 30), tzinfo=CN_TZ,
    )
    unavailable = {
        (row["date"], row["symbol"]) for row in data["derived"] if row["value"] is None
    }
    halts = []
    for row in data["raw_bars"]:
        day, symbol = row["date"], row["symbol"]
        if (
            day not in tail or (day, symbol) not in unavailable
            or row["trading_status"] != "suspended" or not row.get("halt")
            or not available_by(row, cutoff)
        ):
            continue
        session = date.fromisoformat(day)
        halt = row["halt"]
        if (
            datetime.fromisoformat(halt["start"]) <= datetime.combine(session, clock_time(9, 30), CN_TZ)
            and datetime.fromisoformat(halt["end"]) >= datetime.combine(session, clock_time(15), CN_TZ)
        ):
            halts.append({"date": day, "symbol": symbol})
    if not halts:
        if plan.get("workflow") == RESEARCH_VALIDATION:
            return None
        raise _invalid("The frozen Discovery tail has no evidenced full-halt signal gaps.")
    skipped = tail.index(max(halt["date"] for halt in halts)) + 1
    if skipped >= plan["partitions"][VALIDATION_STAGE]["sessions"] - 1:
        raise _invalid("The Discovery halt warm-up leaves no possible Validation signal.")
    return {"halted_inputs": halts, "initial_signal_sessions": skipped}


def _prior_discovery(
    snapshot_store: Path,
    plan: dict[str, Any],
    reject,
) -> tuple[list[str], list[dict], str]:
    try:
        receipt = _verified_discovery(snapshot_store, plan)
    except ValueError as error:
        reject(
            "FINGERPRINT_MISMATCH",
            f"The frozen Discovery prior history could not be verified: {error}",
        )
    content_hash = receipt.get("partition_content_hash", receipt["content_hash"])
    sessions = sorted({row["date"] for row in receipt["data"]["calendar"]})
    return sessions, receipt["data"]["derived"], content_hash


def _validation_export(
    plan: dict[str, Any], protected: dict[str, Any], evaluation_id: str
) -> dict[str, Any]:
    """Derived Validation rows needed later as Holdout warm-up.

    Only the tail required by the frozen signal window and regime lookback is
    kept.  This export lives in the trusted Validation record so the Holdout run
    never has to re-open the sealed Validation payload.
    """
    data = protected["data"]
    sessions = sorted({row["date"] for row in data["calendar"]})
    family = plan["candidate"]["strategy"]["family"]
    variant_windows = [
        signal_window_sessions({"family": family, "parameters": entry["parameters"]})
        for entry in plan["neighborhood"].get("variants", [])
        if entry.get("reason") is None
    ]
    needed = max(
        plan["signal_window_sessions"], plan["regimes"]["lookback"] + 1,
        *variant_windows,
    )
    export_sessions = sessions[-needed:]
    keep = set(export_sessions)
    body = {
        "schema_version": SCHEMA_VERSION,
        "source_stage": VALIDATION_STAGE,
        "evaluation_id": evaluation_id,
        "plan_id": plan["plan_id"],
        "candidate_hash": plan["candidate_hash"],
        "snapshot_id": plan["snapshot_id"],
        "partition": VALIDATION_STAGE,
        "partition_content_hash": protected["content_hash"],
        "sessions": export_sessions,
        "derived": [row for row in data["derived"] if row["date"] in keep],
        "signal_window_sessions": plan["signal_window_sessions"],
        "regime_lookback": plan["regimes"]["lookback"],
    }
    return {**body, "export_hash": _hash(body)}


def _prior_holdout(
    snapshot_store: Path,
    plan: dict[str, Any],
    evidence: dict[str, Any],
    descriptor: dict[str, Any],
    reject,
) -> tuple[tuple[list[str], list[dict]], dict[str, Any]]:
    """Discovery (research-visible) plus the stored Validation export as prior."""
    discovery_sessions, discovery_rows, discovery_hash = _prior_discovery(
        snapshot_store, plan, reject
    )
    export = evidence["export"]
    previous = export["sessions"]
    if previous[-1] >= descriptor["start"]:
        reject(
            "VALIDATION_EVIDENCE_INVALID",
            "The Validation prior export overlaps the sealed Holdout range.",
        )
    # Discovery is contiguous with the export only when it covers all of Validation.
    complete_validation = len(previous) == plan["partitions"][VALIDATION_STAGE]["sessions"]
    sessions = [*discovery_sessions, *previous] if complete_validation else previous
    rows = [*discovery_rows, *export["derived"]] if complete_validation else export["derived"]
    prior_evidence = {
        "partitions": ["discovery", VALIDATION_STAGE] if complete_validation else [VALIDATION_STAGE],
        "discovery_content_hash": discovery_hash,
        "validation": {
            "evaluation_id": evidence["evaluation_id"],
            "record_hash": evidence["record_hash"],
            "partition_content_hash": evidence["partition_content_hash"],
            "export_hash": evidence["export_hash"],
            "sessions": len(previous),
            "derived_rows": len(export["derived"]),
        },
    }
    return (sessions, rows), prior_evidence


def _equal_weight_run(
    data: dict[str, list[dict]],
    plan: dict[str, Any],
    stage: str,
    content_hash: str,
    prior: tuple[list[str], list[dict]],
    costs: dict[str, float],
) -> dict[str, Any]:
    submitted = False

    def equal_weight(history, state):
        nonlocal submitted
        if submitted:
            return None
        weights = equal_weight_buy_and_hold(history, state)
        submitted = weights is not None
        return weights

    return run_partition_strategy(
        data,
        plan["snapshot_id"],
        content_hash,
        equal_weight,
        partition_name=stage,
        data_kind=plan["data_kind"],
        signal_window_sessions=plan["baseline"].get(
            "signal_window_sessions", plan["signal_window_sessions"]
        ),
        prior_history=prior,
        initial_cash=plan["initial_cash"],
        transaction_cost_bps=costs["transaction_cost_bps"],
        slippage_bps=costs["slippage_bps"],
    )


def _candidate_run(
    data: dict[str, list[dict]],
    plan: dict[str, Any],
    stage: str,
    content_hash: str,
    prior: tuple[list[str], list[dict]],
    costs: dict[str, float],
    *,
    parameters: dict[str, Any] | None = None,
    executor: ContainerStrategyExecutor | None = None,
    invocations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    candidate = plan["candidate"]
    variant_parameters = (
        candidate["parameters"] if parameters is None else parameters
    )
    spec = {**candidate["strategy"], "parameters": variant_parameters}
    # The frozen original candidate keeps the plan's window; a neighbourhood
    # variant must use the window implied by its own parameters.
    window = (
        plan["signal_window_sessions"]
        if parameters is None
        else signal_window_sessions(spec)
    )
    strategy = make_strategy(
        spec,
        container_executor=executor,
        evidence_callback=invocations.append if invocations is not None else None,
    )
    return run_partition_strategy(
        data,
        plan["snapshot_id"],
        content_hash,
        strategy,
        partition_name=stage,
        data_kind=plan["data_kind"],
        signal_window_sessions=window,
        prior_history=prior,
        initial_cash=plan["initial_cash"],
        transaction_cost_bps=costs["transaction_cost_bps"],
        slippage_bps=costs["slippage_bps"],
    )


def _run_summary(run: dict[str, Any], *, research: bool = False) -> dict[str, Any]:
    return {
        "metrics": run["metrics"],
        **({
            "positions": [
                {
                    "date": point["date"],
                    "holdings": {
                        symbol: holding["quantity"] for symbol, holding in point["holdings"].items()
                    },
                }
                for point in run["equity_curve"]
            ],
        } if research else {}),
        "start_date": run["start_date"],
        "end_date": run["end_date"],
        "signals": [
            {
                "date": signal["date"],
                "execution_date": signal["execution_date"],
                "target_weights": signal["target_weights"],
            }
            for signal in run["signals"]
        ],
        "fills": run["fills"],
        "unfilled_attempts": [
            attempt
            for attempt in run["order_attempts"]
            if attempt["status"] != "filled"
        ],
        "signal_gaps": run["signal_gaps"],
        "valuation_gaps": run["gaps"]["valuation"],
        "data_gaps": {
            "raw_bars": run["gaps"]["raw_bars"],
            "derived": run["gaps"]["derived"],
        },
    }


def _nav_curve(run: dict[str, Any]) -> list[dict[str, Any]]:
    points = []
    peak: float | None = None
    for point in run["equity_curve"]:
        nav = point["nav"]
        drawdown = None
        if nav is not None:
            peak = nav if peak is None or nav > peak else peak
            drawdown = nav / peak - 1.0 if peak else None
        points.append({"date": point["date"], "nav": nav, "drawdown": drawdown})
    return points


def _delta(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return left - right


def _threshold_values(
    base: dict[str, Any], stress: dict[str, Any]
) -> dict[str, float | int | None]:
    candidate_base = base["candidate"]["metrics"]
    baseline_base = base["baseline"]["metrics"]
    candidate_stress = stress["candidate"]["metrics"]
    baseline_stress = stress["baseline"]["metrics"]
    return {
        "sessions": candidate_base["sessions"],
        "valued_sessions": candidate_base["valued_sessions"],
        "total_return": candidate_base["total_return"],
        "max_drawdown": candidate_base["max_drawdown"],
        "sharpe": candidate_base["sharpe"],
        "turnover": candidate_base.get("turnover"),
        "fees_paid": candidate_base["fees_paid"],
        "slippage_paid": candidate_base["slippage_paid"],
        "unfilled_orders": candidate_base["orders_unfilled"],
        "return_delta": _delta(
            candidate_base["total_return"], baseline_base["total_return"]
        ),
        "stress_total_return": candidate_stress["total_return"],
        "stress_return_delta": _delta(
            candidate_stress["total_return"], baseline_stress["total_return"]
        ),
    }


def _compare(actual: float, operator: str, expected: float) -> bool:
    if operator == "==":
        return math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-12)
    if operator == ">=":
        return actual >= expected
    if operator == "<=":
        return actual <= expected
    if operator == ">":
        return actual > expected
    return actual < expected


def _evaluate_thresholds(
    plan: dict[str, Any], base: dict[str, Any], stress: dict[str, Any]
) -> list[dict[str, Any]]:
    values = _threshold_values(base, stress)
    results = []
    for entry in plan["thresholds"]:
        actual = values.get(entry["metric"])
        expected = entry["value"]
        comparison = {}
        if "benchmark_multiplier" in entry:
            benchmark = base["baseline"]["metrics"].get(entry["metric"])
            expected = (
                None if benchmark is None else entry["benchmark_multiplier"] * benchmark + expected
            )
            if expected is not None and not math.isfinite(expected):
                expected = None
            comparison = {"benchmark_value": benchmark, "expected": expected}
        if actual is None or expected is None:
            status = "unavailable"
            message = f"Evidence for the {entry['metric']} threshold is unavailable."
        elif _compare(float(actual), entry["op"], float(expected)):
            status = "passed"
            message = f"{entry['metric']} met the frozen threshold."
        else:
            status = "failed"
            message = f"{entry['metric']} did not meet the frozen threshold."
        results.append(
            {
                **entry,
                **comparison,
                "actual": actual,
                "status": status,
                "message": message,
            }
        )
    return results


def _observed_warmup_sessions(runs: list[dict], policy: dict, initial_cash: float) -> list[str]:
    """Verify the preregistered cash-only prefix of every supplied run."""
    size = policy["initial_signal_sessions"]
    allowed = {(row["date"], row["symbol"]) for row in policy["halted_inputs"]}
    first_dates = []
    for run in runs:
        prefix = run["equity_curve"][:size]
        gaps = run["signal_gaps"][:size]
        days = [row["date"] for row in prefix]
        if (
            len(days) != size or days != [gap["date"] for gap in gaps]
            or any(row["nav"] != initial_cash for row in prefix)
            or any(
                (missing["date"], symbol) not in allowed
                for gap in gaps for missing in gap["missing_sessions"]
                for symbol in missing["missing_symbols"]
            )
        ):
            return []
        first_dates = days
    return first_dates


def _neighbourhood_result(
    plan: dict[str, Any],
    data: dict[str, list[dict]],
    stage: str,
    content_hash: str,
    prior: tuple[list[str], list[dict]],
    base_metrics: dict[str, Any],
    *,
    executor: ContainerStrategyExecutor | None = None,
    invocations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    definition = plan["neighborhood"]
    counts = {"variants": 0, "stable": 0, "degraded": 0, "not_evaluable": 0, "excluded": 0}
    if definition.get("no_tunable_parameters") is True:
        return {
            "definition": definition,
            "applicable": False,
            "reason": "no_tunable_parameters",
            "variants": [],
            "excluded": [],
            "counts": counts,
            "diagnostic_only": True,
        }
    if definition.get("status") != "planned":
        return {
            "definition": definition,
            "applicable": False,
            "reason": definition.get("status", "not_planned"),
            "variants": [],
            "excluded": list(definition.get("excluded", [])),
            "counts": counts,
            "diagnostic_only": True,
        }
    thresholds = definition["thresholds"]
    original_sharpe = base_metrics["sharpe"]
    original_drawdown = (
        abs(base_metrics["max_drawdown"])
        if base_metrics["max_drawdown"] is not None
        else None
    )
    variants = []
    for entry in definition.get("variants", []):
        variant = {
            "changed_parameters": entry["changed_parameters"],
            "parameters": entry["parameters"],
            "requested_delta": entry.get("requested_delta"),
            "applied_delta": entry.get("applied_delta"),
        }
        if entry.get("reason") is not None:
            variant.update(status="invalid_config", reason=entry["reason"])
            variants.append(variant)
            continue
        variant["signal_window_sessions"] = signal_window_sessions(
            {"family": plan["candidate"]["strategy"]["family"], "parameters": entry["parameters"]}
        )
        try:
            run = _candidate_run(
                data,
                plan,
                stage,
                content_hash,
                prior,
                plan["base_costs"],
                parameters=entry["parameters"],
                executor=executor,
                invocations=invocations,
            )
        except IsolationError:
            raise
        except ValueError as error:
            variant.update(status="not_evaluable", reason="execution_failed", error=str(error))
        else:
            metrics = run["metrics"]
            variant["metrics"] = metrics
            if (
                original_sharpe is None
                or metrics["sharpe"] is None
                or original_drawdown is None
                or metrics["max_drawdown"] is None
            ):
                variant.update(status="not_evaluable", reason="metric_unavailable")
            else:
                sharpe_drop = original_sharpe - metrics["sharpe"]
                drawdown_increase = abs(metrics["max_drawdown"]) - original_drawdown
                degraded = (
                    sharpe_drop
                    >= max(
                        thresholds["sharpe_absolute"],
                        thresholds["sharpe_relative"] * max(original_sharpe, 0.0),
                    )
                    or drawdown_increase
                    >= max(
                        thresholds["drawdown_absolute"],
                        thresholds["drawdown_relative"] * original_drawdown,
                    )
                )
                variant.update(
                    status="degraded" if degraded else "stable",
                    sharpe_drop=sharpe_drop,
                    drawdown_increase=drawdown_increase,
                )
        variants.append(variant)
    counts["variants"] = len(variants)
    for variant in variants:
        if variant["status"] in counts:
            counts[variant["status"]] += 1
    excluded = list(definition.get("excluded", []))
    counts["excluded"] = len(excluded)
    return {
        "definition": definition,
        "applicable": True,
        "variants": variants,
        "excluded": excluded,
        "counts": counts,
        "diagnostic_only": True,
    }


def _equal_weight_index(
    days: list[str], derived: dict[tuple[str, str], dict]
) -> dict[str, float]:
    index: dict[str, float] = {}
    for day in days:
        values = []
        for symbol in ETF_SYMBOLS:
            row = derived.get((day, symbol))
            if row is None or row.get("value") is None:
                values = []
                break
            values.append(float(row["value"]))
        if values:
            index[day] = sum(values) / len(values)
    return index


def _day_available(
    day: str, cutoff: datetime, derived: dict[tuple[str, str], dict]
) -> bool:
    for symbol in ETF_SYMBOLS:
        row = derived.get((day, symbol))
        if row is None or row.get("value") is None or not available_by(row, cutoff):
            return False
    return True


def _regime_labels(
    plan: dict[str, Any],
    sessions: list[str],
    prior: tuple[list[str], list[dict]],
    data: dict[str, list[dict]],
) -> dict[str, str | None]:
    definition = plan["regimes"]
    prior_sessions, prior_rows = prior
    days = sorted({*[day for day in prior_sessions if day < sessions[0]], *sessions})
    derived = {
        (row["date"], row["symbol"]): row for row in [*prior_rows, *data["derived"]]
    }
    index = _equal_weight_index(days, derived)
    ordered = [day for day in days if day in index]
    calm, volatile = definition["labels"]
    labels: dict[str, str | None] = {}
    for day in sessions:
        cutoff = datetime.combine(
            date.fromisoformat(day), clock_time(9, 30), tzinfo=CN_TZ
        )
        prior_days = [candidate for candidate in ordered if candidate < day]
        if len(prior_days) < definition["lookback"] + 1:
            labels[day] = None
            continue
        window = prior_days[-(definition["lookback"] + 1) :]
        if not all(_day_available(candidate, cutoff, derived) for candidate in window):
            labels[day] = None
            continue
        returns = [
            index[right] / index[left] - 1.0
            for left, right in zip(window, window[1:], strict=False)
        ]
        volatility = statistics.stdev(returns)
        labels[day] = volatile if volatility >= definition["volatility_threshold"] else calm
    return labels


def _session_returns(curve: list[dict[str, Any]]) -> dict[str, float]:
    returns: dict[str, float] = {}
    previous: float | None = None
    for point in curve:
        nav = point["nav"]
        if nav is None:
            continue
        if previous is not None:
            returns[point["date"]] = float(nav) / previous - 1.0
        previous = float(nav)
    return returns


def _regime_groups(
    plan: dict[str, Any],
    sessions: list[str],
    labels: dict[str, str | None],
    candidate_curve: list[dict[str, Any]],
    baseline_curve: list[dict[str, Any]],
) -> dict[str, Any]:
    candidate_returns = _session_returns(candidate_curve)
    baseline_returns = _session_returns(baseline_curve)
    groups = []
    for label in [*plan["regimes"]["labels"], "unclassified"]:
        if label == "unclassified":
            days = [day for day in sessions if labels.get(day) is None]
        else:
            days = [day for day in sessions if labels.get(day) == label]
        candidate_samples = [
            candidate_returns[day] for day in days if day in candidate_returns
        ]
        baseline_samples = [
            baseline_returns[day] for day in days if day in baseline_returns
        ]
        relative = [
            candidate_returns[day] - baseline_returns[day]
            for day in days
            if day in candidate_returns and day in baseline_returns
        ]
        groups.append(
            {
                "label": label,
                "sessions": len(days),
                "candidate_samples": len(candidate_samples),
                "baseline_samples": len(baseline_samples),
                "candidate_mean_return": (
                    sum(candidate_samples) / len(candidate_samples)
                    if candidate_samples
                    else None
                ),
                "baseline_mean_return": (
                    sum(baseline_samples) / len(baseline_samples)
                    if baseline_samples
                    else None
                ),
                "mean_relative_return": (
                    sum(relative) / len(relative) if relative else None
                ),
            }
        )
    return {
        "definition": plan["regimes"],
        "session_labels": [
            {"date": day, "label": labels.get(day)} for day in sessions
        ],
        "groups": groups,
        "diagnostic_only": True,
    }


def _evaluation_record(
    *,
    plan: dict[str, Any],
    stage: str,
    protected: dict[str, Any],
    prior: tuple[list[str], list[dict]],
    prior_evidence: dict[str, Any],
    isolation: dict[str, Any],
    authorization: dict[str, Any],
    metadata: dict[str, Any],
    project_id: str,
    evaluation_id: str,
    debited_at: str,
    executor: ContainerStrategyExecutor | None = None,
) -> dict[str, Any]:
    data = protected["data"]
    research = plan.get("workflow") is not None
    invocations = isolation.get("invocations")
    descriptor = protected["descriptor"]
    content_hash = protected["content_hash"]
    runs = {}
    for label, costs in (
        ("base", plan["base_costs"]),
        ("stress", plan["stress_costs"]),
    ):
        runs[label] = {
            "candidate": _candidate_run(
                data, plan, stage, content_hash, prior, costs,
                executor=executor, invocations=invocations,
            ),
            "baseline": _equal_weight_run(
                data, plan, stage, content_hash, prior, costs
            ),
        }
        if research:
            for run in runs[label].values():
                run["metrics"]["turnover"] = math.fsum(
                    fill["notional"] for fill in run["fills"]
                ) / plan["initial_cash"]

    base_metrics = runs["base"]["candidate"]["metrics"]
    stress_metrics = runs["stress"]["candidate"]["metrics"]
    baseline_base = runs["base"]["baseline"]["metrics"]
    baseline_stress = runs["stress"]["baseline"]["metrics"]
    thresholds = _evaluate_thresholds(plan, runs["base"], runs["stress"])
    minimum = plan["minimum_samples"]
    samples_sufficient = (
        base_metrics["sessions"] >= minimum["sessions"]
        and base_metrics["valued_sessions"] >= minimum["valued_sessions"]
    )
    valuation_complete = base_metrics["valued_sessions"] == base_metrics["sessions"]
    partition_gaps = _partition_gaps(metadata, descriptor)
    evidenced_halts = (
        _evidenced_open_halts(data["raw_bars"])
        if plan.get("coverage_policy") == EVIDENCED_OPEN_HALT_POLICY else {}
    )
    blocking_snapshot_gap = any(
        kind != "raw_bars" or (gap.get("date"), gap.get("symbol")) not in evidenced_halts
        for kind, gaps in partition_gaps.items()
        for gap in gaps
    )
    runner = runs["base"]["candidate"]
    baseline_runner = runs["base"]["baseline"]
    warmup_policy = plan["prior_history"][stage].get("halt_warmup")
    warmup_sessions = []
    signal_coverage_complete = not runner["signal_gaps"]
    if warmup_policy:
        # A research round has a fixed one-session baseline regardless of candidate lookback.
        shared_window = plan["baseline"]["same_signal_window"]
        participants = [run for scenario in runs.values() for name, run in scenario.items()
                        if name == "candidate" or shared_window]
        warmup_sessions = _observed_warmup_sessions(participants, warmup_policy, plan["initial_cash"])
        signal_coverage_complete = bool(warmup_sessions) and all(
            len(run["signal_gaps"]) == len(warmup_sessions) for run in participants
        )
        if not shared_window:
            signal_coverage_complete &= all(not scenario["baseline"]["signal_gaps"] for scenario in runs.values())
    coverage_complete = (
        samples_sufficient
        and valuation_complete
        and bool(runner["signals"])
        and bool(baseline_runner["signals"])
        and signal_coverage_complete
        and not runner["gaps"]["valuation"]
        and not blocking_snapshot_gap
    )
    failed = [item["id"] for item in thresholds if item["status"] == "failed"]
    unavailable = [item["id"] for item in thresholds if item["status"] == "unavailable"]
    if failed:
        outcome, reason = "rejected", REASON_THRESHOLD
    elif not coverage_complete:
        outcome, reason = "insufficient_evidence", REASON_COVERAGE
    elif unavailable:
        outcome, reason = "insufficient_evidence", REASON_EVIDENCE
    else:
        outcome, reason = "passed", None
    message = (
        "All preregistered thresholds and minimum samples were met."
        if outcome == "passed"
        else COARSE_MESSAGES[reason]
    )
    sessions = [point["date"] for point in runner["equity_curve"]]
    neighbourhood = _neighbourhood_result(
        plan, data, stage, content_hash, prior, base_metrics,
        executor=executor, invocations=invocations,
    )
    labels = _regime_labels(plan, sessions, prior, data)
    regimes = _regime_groups(
        plan,
        sessions,
        labels,
        runs["base"]["candidate"]["equity_curve"],
        runs["base"]["baseline"]["equity_curve"],
    )
    prior_export = (
        _validation_export(plan, protected, evaluation_id)
        if stage == VALIDATION_STAGE
        else None
    )
    return {
        "schema_version": plan["schema_version"],
        "scope": plan["scope"],
        "data_kind": plan["data_kind"],
        "evaluation_id": evaluation_id,
        "plan_id": plan["plan_id"],
        "project_id": project_id,
        "candidate_id": plan["candidate_id"],
        "candidate_hash": plan["candidate_hash"],
        "snapshot_id": plan["snapshot_id"],
        "stage": stage,
        "partition": stage,
        "outcome": outcome,
        "reason": reason,
        "message": message,
        "scoring": True,
        "aborted": False,
        "not_admission": True,
        "not_admission_statement": (
            NOT_ADMISSION_STATEMENT if plan["data_kind"] == SCOPE else REAL_EVALUATION_STATEMENT
        ),
        "fingerprints": {
            "plan": plan["plan_id"],
            "candidate": plan["candidate_hash"],
            "snapshot_manifest": plan["snapshot_id"],
            "partitions": {
                name: plan["partitions"][name]["content_hash"] for name in STAGES
            },
            "evaluated_partition": content_hash,
            "runtime_dependencies": plan["candidate"].get("runtime_dependencies"),
        },
        "partitions": {
            name: plan["partitions"][name] for name in STAGES
        },
        "partition_descriptor": descriptor,
        "lineage": protected["lineage"],
        "prior_evidence": prior_evidence,
        **(
            {"prior_export_for_holdout": prior_export}
            if prior_export is not None
            else {}
        ),
        "authorization": authorization,
        "query_receipt": {
            "partition": stage,
            "partition_content_hash": content_hash,
            "plan_id": plan["plan_id"],
            "candidate_hash": plan["candidate_hash"],
            "authorization_id": authorization["auth_id"],
            "debited": True,
            "debited_at": debited_at,
            "status": "completed",
        },
        "isolation": isolation,
        "isolation_verified": isolation.get("verified") is True,
        "memory_limit_enforced": isolation.get("memory_limit_enforced"),
        "isolation_failure_reason": isolation.get("failure_reason"),
        "opened_protected_source": True,
        "availability": {
            "timezone": "Asia/Shanghai (UTC+08:00)",
            "signal_cutoff": "next_session_open_09:30",
            "valuation_cutoff": "session_close_end_of_day",
            "regime_cutoff": "target_session_open_09:30_excluding_target_and_future",
            "prior_history_rule": plan["prior_history"][stage],
        },
        "execution": {
            "rules": plan["execution_rules"],
            "initial_cash": plan["initial_cash"],
            "signal_window_sessions": plan["signal_window_sessions"],
            "base_costs": plan["base_costs"],
            "stress_costs": plan["stress_costs"],
            "partitions": {
                "validation": plan["partitions"]["validation"],
                "final_holdout": plan["partitions"]["final_holdout"],
            },
        },
        "baseline": plan["baseline"],
        "cost_stress": {
            "base": {
                "candidate": base_metrics,
                "baseline": baseline_base,
                "return_delta": _delta(
                    base_metrics["total_return"], baseline_base["total_return"]
                ),
                "fees_delta": base_metrics["fees_paid"] - baseline_base["fees_paid"],
                "slippage_delta": (
                    base_metrics["slippage_paid"] - baseline_base["slippage_paid"]
                ),
            },
            "stress": {
                "candidate": stress_metrics,
                "baseline": baseline_stress,
                "return_delta": _delta(
                    stress_metrics["total_return"], baseline_stress["total_return"]
                ),
                "fees_delta": stress_metrics["fees_paid"] - baseline_stress["fees_paid"],
                "slippage_delta": (
                    stress_metrics["slippage_paid"] - baseline_stress["slippage_paid"]
                ),
            },
            "stress_minus_base": {
                "candidate_return_delta": _delta(
                    stress_metrics["total_return"], base_metrics["total_return"]
                ),
                "baseline_return_delta": _delta(
                    baseline_stress["total_return"], baseline_base["total_return"]
                ),
            },
        },
        "candidate_run": {
            "base": _run_summary(runs["base"]["candidate"], research=research),
            "stress": _run_summary(runs["stress"]["candidate"]),
        },
        "baseline_run": {
            "base": _run_summary(runs["base"]["baseline"]),
            "stress": _run_summary(runs["stress"]["baseline"]),
        },
        "thresholds": thresholds,
        "minimum_samples": minimum,
        "verdict": {
            "outcome": outcome,
            "reason": reason,
            "message": message,
            "failed_thresholds": failed,
            "unavailable_thresholds": unavailable,
            "samples_sufficient": samples_sufficient,
            "coverage_complete": coverage_complete,
        },
        "coverage": {
            "sessions": base_metrics["sessions"],
            "valued_sessions": base_metrics["valued_sessions"],
            "minimum_samples": minimum,
            "samples_sufficient": samples_sufficient,
            "candidate_signals": len(runner["signals"]),
            "baseline_signals": len(baseline_runner["signals"]),
            "snapshot_gaps": partition_gaps,
            **({"discovery_warmup_sessions": warmup_sessions} if warmup_policy else {}),
            **(
                {"evidenced_open_halts": list(evidenced_halts.values())}
                if plan.get("coverage_policy") == EVIDENCED_OPEN_HALT_POLICY else {}
            ),
            "runner_gaps": {
                "raw_bars": runner["gaps"]["raw_bars"],
                "derived": runner["gaps"]["derived"],
                "valuation": runner["gaps"]["valuation"],
                "signal_gaps": runner["signal_gaps"],
            },
            "complete": coverage_complete,
        },
        "curves": {
            "sessions": sessions,
            "candidate": {
                "base": _nav_curve(runs["base"]["candidate"]),
                "stress": _nav_curve(runs["stress"]["candidate"]),
            },
            "baseline": {
                "base": _nav_curve(runs["base"]["baseline"]),
                "stress": _nav_curve(runs["stress"]["baseline"]),
            },
        },
        "neighbourhood": neighbourhood,
        "regimes": regimes,
        "created_at": _now(),
        "completed_at": _now(),
    }


def _aborted_record(
    *,
    plan: dict[str, Any],
    stage: str,
    project_id: str,
    evaluation_id: str,
    authorization: dict[str, Any],
    isolation: dict[str, Any],
    protected: dict[str, Any] | None,
    opened: bool | str,
    debited_at: str,
    error: BaseException,
    prior_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    message = COARSE_MESSAGES[REASON_EVIDENCE]
    return {
        "schema_version": plan["schema_version"],
        "scope": plan["scope"],
        "data_kind": plan["data_kind"],
        "evaluation_id": evaluation_id,
        "plan_id": plan["plan_id"],
        "project_id": project_id,
        "candidate_id": plan["candidate_id"],
        "candidate_hash": plan["candidate_hash"],
        "snapshot_id": plan["snapshot_id"],
        "stage": stage,
        "partition": stage,
        "outcome": "insufficient_evidence",
        "reason": REASON_EVIDENCE,
        "message": message,
        "scoring": False,
        "aborted": True,
        "not_admission": True,
        "not_admission_statement": (
            NOT_ADMISSION_STATEMENT if plan["data_kind"] == SCOPE else REAL_EVALUATION_STATEMENT
        ),
        "fingerprints": {
            "plan": plan["plan_id"],
            "candidate": plan["candidate_hash"],
            "snapshot_manifest": plan["snapshot_id"],
            "partitions": {
                name: plan["partitions"][name]["content_hash"] for name in STAGES
            },
        },
        "partitions": {name: plan["partitions"][name] for name in STAGES},
        "partition_descriptor": (
            protected["descriptor"] if protected is not None else None
        ),
        "lineage": protected["lineage"] if protected is not None else None,
        "prior_evidence": prior_evidence,
        "authorization": authorization,
        "query_receipt": {
            "partition": stage,
            "partition_content_hash": plan["partitions"][stage]["content_hash"],
            "plan_id": plan["plan_id"],
            "candidate_hash": plan["candidate_hash"],
            "authorization_id": authorization["auth_id"],
            "debited": True,
            "debited_at": debited_at,
            "status": "aborted",
        },
        "isolation": isolation,
        "isolation_verified": isolation.get("verified") is True,
        "memory_limit_enforced": isolation.get("memory_limit_enforced"),
        "isolation_failure_reason": isolation.get("failure_reason"),
        "opened_protected_source": opened,
        "availability": {
            "timezone": "Asia/Shanghai (UTC+08:00)",
            "signal_cutoff": "next_session_open_09:30",
            "valuation_cutoff": "session_close_end_of_day",
            "regime_cutoff": "target_session_open_09:30_excluding_target_and_future",
            "prior_history_rule": plan["prior_history"][stage],
        },
        "execution": {
            "rules": plan["execution_rules"],
            "initial_cash": plan["initial_cash"],
            "signal_window_sessions": plan["signal_window_sessions"],
            "base_costs": plan["base_costs"],
            "stress_costs": plan["stress_costs"],
        },
        "baseline": plan["baseline"],
        "thresholds": [],
        "minimum_samples": plan["minimum_samples"],
        "verdict": {
            "outcome": "insufficient_evidence",
            "reason": REASON_EVIDENCE,
            "message": message,
            "failed_thresholds": [],
            "unavailable_thresholds": [],
            "samples_sufficient": False,
            "coverage_complete": False,
        },
        "failure": {
            "phase": "evaluation",
            "type": type(error).__name__,
            "message": str(error),
        },
        "created_at": _now(),
        "completed_at": _now(),
    }


def _write_record(store: Path, evaluation_id: str, record: dict[str, Any]) -> Path:
    """Write the trusted record once, binding its content address."""
    record["record_hash"] = _record_hash(record)
    path = store / "records" / evaluation_id / "record.json"
    stored = _file_once(path, record)
    if (
        stored.get("evaluation_id") != evaluation_id
        or stored.get("record_hash") != _record_hash(stored)
    ):
        raise ValueError("Existing evaluation record integrity check failed.")
    return path


def _isolation_evidence_summary(isolation: dict[str, Any]) -> dict[str, Any]:
    return {
        "verified": isolation.get("verified") is True,
        "memory_limit_enforced": isolation.get("memory_limit_enforced"),
        "failure_reason": isolation.get("failure_reason"),
    }


def _probe_container(executor: ContainerStrategyExecutor) -> dict[str, Any]:
    """Verify the frozen runner with empty input before claiming the query."""
    try:
        probe = executor.run("def strategy(history, state): return None", {}, pd.DataFrame(), {})
    except IsolationError as error:
        return {
            "verified": False,
            "failure_reason": str(error),
            "probe": error.evidence,
        }
    return {
        "verified": True,
        "memory_limit_enforced": probe.evidence["isolation_checks"]["memory_limited"],
        "failure_reason": None,
        "probe": probe.evidence,
        "invocations": [],
    }


def _isolation_summary(record: dict[str, Any]) -> dict[str, Any]:
    isolation = record.get("isolation")
    if not isinstance(isolation, dict):
        isolation = {}
    return {
        "verified": (
            record.get("isolation_verified", isolation.get("verified")) is True
        ),
        "memory_limit_enforced": record.get(
            "memory_limit_enforced", isolation.get("memory_limit_enforced")
        ),
        "failure_reason": record.get(
            "isolation_failure_reason", isolation.get("failure_reason")
        ),
    }


def _run_response(
    store: Path, record: dict[str, Any], *, reused: bool
) -> dict[str, Any]:
    return {
        "schema_version": record["schema_version"],
        "status": "completed" if record.get("scoring") else "aborted",
        "scope": record["scope"],
        "plan_id": record["plan_id"],
        "stage": record["stage"],
        "evaluation_id": record["evaluation_id"],
        "outcome": record["outcome"],
        "reason": record["reason"],
        "scoring": record["scoring"],
        "query": {
            **record["query_receipt"],
            "reused": reused,
            "opened_protected_source": record.get("opened_protected_source") is True,
        },
        "receipt": record["research_receipt"],
        "isolation": _isolation_summary(record),
        "record_path": str(store / "records" / record["evaluation_id"] / "record.json"),
        "record": record,
    }


def _score_claim(
    store: Path,
    snapshot_store: Path,
    plan: dict[str, Any],
    stage: str,
    authorization: dict[str, Any],
    claim: dict[str, Any],
    metadata: dict[str, Any],
    prior: tuple[list[str], list[dict]] | None,
    prior_evidence: dict[str, Any],
    isolation: dict[str, Any],
    executor: ContainerStrategyExecutor | None = None,
) -> tuple[dict[str, Any], Path, str]:
    evaluation_id = claim["evaluation_id"]
    project_id = plan["project_id"]
    protected: dict[str, Any] | None = None
    opened: bool | str = "unverified"
    try:
        if prior is None:
            raise ValueError("An interrupted evaluation already reserved this protected read.")
        protected = snapshot.read_protected(
            snapshot_store, plan["snapshot_id"], stage
        )
        opened = True
        if plan["data_kind"] == SCOPE:
            _failpoint("evaluation_after_read")
        record = _evaluation_record(
            plan=plan,
            stage=stage,
            protected=protected,
            prior=prior,
            prior_evidence=prior_evidence,
            isolation=isolation,
            authorization=authorization,
            metadata=metadata,
            project_id=project_id,
            evaluation_id=evaluation_id,
            debited_at=claim["debited_at"],
            executor=executor,
        )
        record["research_receipt"] = _receipt_for_record(record)
        record_path = _write_record(store, evaluation_id, record)
        ledger_status = "completed"
    except Exception as error:  # noqa: BLE001 - abort after read must never score
        record = _aborted_record(
            plan=plan,
            stage=stage,
            project_id=project_id,
            evaluation_id=evaluation_id,
            authorization=authorization,
            isolation=isolation,
            protected=protected,
            opened=opened,
            debited_at=claim["debited_at"],
            error=error,
            prior_evidence=prior_evidence,
        )
        record["research_receipt"] = _receipt_for_record(record)
        record_path = _write_record(store, evaluation_id, record)
        ledger_status = "aborted"

    return record, record_path, ledger_status


def run_evaluation(
    store: str | Path,
    snapshot_store: str | Path,
    project: str | Path,
    plan_id: str,
    stage: str = VALIDATION_STAGE,
) -> dict[str, Any]:
    """Run one authorized, isolated, debited Validation or Final Holdout evaluation."""
    eval_store = _store(store)
    snapshot_store = Path(snapshot_store)
    project = Path(project)
    project_id = str(project_status(project)["project_id"])
    plan = _load_plan(eval_store, plan_id)
    label = "Validation" if stage == VALIDATION_STAGE else "Final Holdout"
    context = {
        "scope": plan.get("scope"),
        "plan_id": plan.get("plan_id"),
        "stage": stage,
        "candidate_id": plan.get("candidate_id"),
        "candidate_hash": plan.get("candidate_hash"),
        "snapshot_id": plan.get("snapshot_id"),
        "project_id": project_id,
        "partition": stage,
        "partition_content_hash": (
            plan.get("partitions", {}).get(stage, {}).get("content_hash")
            if isinstance(plan.get("partitions"), dict)
            else None
        ),
    }

    def reject(
        code: str,
        message: str,
        *,
        next_action: str = "inspect",
        details: dict[str, Any] | None = None,
    ) -> None:
        extra = dict(details or {})
        audit_id = _audit(eval_store, {**context, **extra}, code, message)
        raise EvaluationRejection(
            code,
            "run",
            next_action,
            message,
            context={
                "audit_id": audit_id,
                "opened_protected_source": False,
                "query_debited": False,
                **extra,
            },
        )

    if stage not in STAGES:
        reject("UNSUPPORTED_STAGE", "Unknown evaluation stage.", next_action="correct")
    if plan.get("workflow"):
        reject(
            "RESEARCH_WORKFLOW_REQUIRED", "Use the research workflow to execute this plan.",
            next_action="correct",
        )
    data_kind = plan.get("data_kind")
    if data_kind not in (SCOPE, "real") or plan.get("scope") != data_kind:
        reject("FINGERPRINT_MISMATCH", "The frozen plan has an invalid data kind.")
    if plan.get("stages") != list(STAGES):
        reject("FINGERPRINT_MISMATCH", "The frozen plan does not fix both protected stages.")
    if plan.get("project_id") != project_id:
        reject("FINGERPRINT_MISMATCH", "The frozen plan is bound to another project.")
    if _plan_mapping(project, plan_id) != plan["candidate_id"]:
        reject(
            "FINGERPRINT_MISMATCH",
            "The frozen plan is not registered to this project's candidate.",
        )
    try:
        candidate = candidate_status(project, plan["candidate_id"])
    except ValueError as error:
        reject("FINGERPRINT_MISMATCH", f"The frozen candidate failed verification: {error}")
    if (
        candidate.get("candidate_hash") != plan["candidate_hash"]
        or candidate != plan["candidate"]
    ):
        reject(
            "FINGERPRINT_MISMATCH",
            "The frozen candidate differs from the plan's frozen candidate.",
        )
    evaluation = candidate["evaluation"]
    executor_config = evaluation.get("strategy_executor", {"kind": "none"})
    runtime = runtime_identity(executor_config)
    if plan.get("runtime") != runtime or evaluation.get("runtime") != runtime:
        reject(
            "FINGERPRINT_MISMATCH",
            "The trusted accounting engine differs from the plan's frozen runtime.",
        )
    try:
        metadata = snapshot.manifest(snapshot_store, plan["snapshot_id"])
        descriptors = {
            name: snapshot.partition_descriptor(metadata, name) for name in STAGES
        }
    except ValueError as error:
        reject(
            "FINGERPRINT_MISMATCH",
            f"The frozen snapshot manifest could not be verified: {error}",
        )
    if (
        metadata.get("data_kind") != data_kind
        or snapshot.SCHEMA_VERSION.get(metadata.get("schema_version")) != data_kind
        or evaluation.get("data_kind", SCOPE) != data_kind
    ):
        reject("FINGERPRINT_MISMATCH", "The frozen candidate and snapshot data kind must match.")
    for name in STAGES:
        if descriptors[name] != plan["partitions"][name]:
            reject(
                "FINGERPRINT_MISMATCH",
                f"The sealed {name} descriptor differs from the frozen plan.",
            )

    try:
        authorizations = _authorizations(eval_store, plan_id, stage)
    except EvaluationRejection as error:
        reject("AUTHORIZATION_REQUIRED", str(error), next_action="authorize")
    if len(authorizations) != 1 or not authorization_matches(
        authorizations[0], plan, stage
    ):
        reject(
            "AUTHORIZATION_REQUIRED",
            f"A matching {label} authorization is required before opening the "
            "sealed partition.",
            next_action="authorize",
        )
    authorization = authorizations[0]

    validation_evidence: dict[str, Any] | None = None
    if stage == HOLDOUT_STAGE:
        # The authorization is only valid while the Validation evidence it was
        # granted against still exists and still passes.  This reads the trusted
        # Validation record, never the sealed Validation payload.
        try:
            validation_evidence = _validation_prior_evidence(eval_store, plan)
        except _EvidenceProblem as problem:
            reject(
                problem.code,
                problem.message,
                next_action=(
                    "run-validation"
                    if problem.code == "VALIDATION_NOT_PASSED"
                    else "inspect"
                ),
            )
        binding = authorization.get("validation")
        if (
            not isinstance(binding, dict)
            or binding.get("evaluation_id") != validation_evidence["evaluation_id"]
            or binding.get("record_hash") != validation_evidence["record_hash"]
            or binding.get("export_hash") != validation_evidence["export_hash"]
        ):
            reject(
                "AUTHORIZATION_REQUIRED",
                "The stored Final Holdout authorization does not match the "
                "completed Validation record; authorize again after inspecting.",
                next_action="authorize",
            )

    executor = None
    if candidate["strategy"]["family"] == "python":
        try:
            executor = executor_from_config(executor_config)
        except ValueError as error:
            reject("ISOLATION_UNAVAILABLE", str(error), next_action="stop")
    isolation = _probe_container(executor) if executor else probe_isolation_evidence()
    if isolation.get("verified") is not True:
        reject(
            "ISOLATION_UNAVAILABLE",
            "OS isolation could not be verified ("
            + str(isolation.get("failure_reason"))
            + "); the sealed partition was not opened.",
            next_action="stop",
            details={"isolation": _isolation_evidence_summary(isolation)},
        )

    if stage == VALIDATION_STAGE:
        discovery_sessions, discovery_rows, discovery_hash = _prior_discovery(
            snapshot_store, plan, reject
        )
        prior = (discovery_sessions, discovery_rows)
        prior_evidence = {
            "partitions": list(plan["prior_history"][VALIDATION_STAGE]["partitions"]),
            "discovery_content_hash": discovery_hash,
        }
    else:
        prior, prior_evidence = _prior_holdout(
            snapshot_store,
            plan,
            validation_evidence,
            descriptors[HOLDOUT_STAGE],
            reject,
        )

    claim = _claim_partition(eval_store, snapshot_store, plan, stage, authorization)
    if claim["state"] == "source_missing":
        reject("PROTECTED_SOURCE_MISSING", f"The sealed {label} payload is unavailable.")
    if claim["state"] == "consumed":
        reject(
            "QUERY_ALREADY_CONSUMED",
            f"This sealed {label} range was already consumed by another frozen "
            "plan or candidate.",
            next_action="stop",
        )
    if claim["state"] in {"in_progress", "unrecorded"}:
        reject(
            "EVALUATION_IN_PROGRESS",
            f"This sealed {label} range is already reserved; no second read is allowed.",
            next_action="inspect",
        )
    if claim["state"] == "reuse":
        record = claim["record"]
        try:
            _verify_record(record)
            if not _record_matches(record, plan, stage):
                raise ValueError(
                    "The stored evaluation record is not bound to this frozen plan, "
                    "candidate, snapshot and partition fingerprint."
                )
        except ValueError as error:
            reject("RECORD_INTEGRITY_FAILED", str(error), next_action="inspect")
        receipt = _publish_receipt(
            project, plan, _receipt_for_record(record)
        )
        _mark_state(
            eval_store,
            record["evaluation_id"],
            stage,
            plan["partitions"][stage]["content_hash"],
            status="completed" if record.get("scoring") else "aborted",
            outcome=record["outcome"],
            record_path=claim["record_path"],
            completed_at=record.get("completed_at") or _now(),
        )
        record["research_receipt"] = receipt
        return _run_response(eval_store, record, reused=True)

    record, record_path, ledger_status = _score_claim(
        eval_store, snapshot_store, plan, stage, authorization, claim,
        metadata, prior, prior_evidence, isolation, executor,
    )
    evaluation_id = claim["evaluation_id"]
    receipt = _publish_receipt(project, plan, record["research_receipt"])
    record["research_receipt"] = receipt
    _mark_state(
        eval_store,
        evaluation_id,
        stage,
        plan["partitions"][stage]["content_hash"],
        status=ledger_status,
        outcome=record["outcome"],
        record_path=record_path,
        completed_at=record["completed_at"],
    )
    return _run_response(eval_store, record, reused=False)


def _research_authorization(plan: dict[str, Any], kickoff: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": RESEARCH_SCHEMA_VERSION,
        "auth_id": kickoff["kickoff_id"],
        "authorization_kind": "research_kickoff",
        "authorization_id": kickoff["authorization_id"],
        "reviewer": kickoff["reviewer"],
        "reason": kickoff["approval_reason"],
        "round_id": kickoff["round_id"],
        "plan_id": plan["plan_id"],
        "plan_hash": plan["plan_id"],
        "candidate_id": plan["candidate_id"],
        "candidate_hash": plan["candidate_hash"],
        "snapshot_id": plan["snapshot_id"],
        "stage": VALIDATION_STAGE,
        "partition": VALIDATION_STAGE,
        "partition_content_hash": plan["partitions"][VALIDATION_STAGE]["content_hash"],
    }


def _round_budget(kickoff: dict[str, Any], used: int) -> dict[str, int]:
    limit = kickoff["validation_budget"]
    return {"limit": limit, "used": used, "remaining": limit - used}


def _validation_rows(store: Path, round_id: str) -> list[dict[str, Any]]:
    connection = _ledger(store / "queries.sqlite")
    try:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM validation_queries WHERE round_id = ? ORDER BY debited_at",
            (round_id,),
        )]
    finally:
        connection.close()


def _research_record(
    store: Path, row: dict[str, Any], kickoff: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    plan = _load_plan(store, row["plan_id"])
    candidate = plan["candidate"]
    record = _load_stored_record(store, row["evaluation_id"])
    auth = _research_authorization(plan, kickoff)
    expected_claim = {
        "partition": VALIDATION_STAGE,
        "partition_content_hash": row["partition_content_hash"],
        "plan_id": row["plan_id"],
        "candidate_hash": row["candidate_hash"],
        "authorization_id": row["authorization_id"],
        "debited": True,
        "debited_at": row["debited_at"],
        "status": "completed" if record.get("scoring") is True else "aborted",
    }
    if (
        not _record_matches(record, plan, VALIDATION_STAGE)
        or plan.get("workflow") != RESEARCH_VALIDATION
        or plan.get("round_id") != kickoff["round_id"]
        or plan.get("kickoff_id") != kickoff["kickoff_id"]
        or plan.get("strategy_fingerprint") != row["strategy_fingerprint"]
        or plan.get("proposal", {}).get("proposal_id") != row["proposal_id"]
        or candidate.get("submission_id") != row["submission_id"]
        or candidate.get("experiment_id") != row["experiment_id"]
        or row["authorization_id"] != auth["auth_id"]
        or record.get("evaluation_id") != row["evaluation_id"]
        or record.get("authorization") != auth
        or record.get("query_receipt") != expected_claim
        or row["status"] not in ("reading", expected_claim["status"])
    ):
        raise _EvidenceProblem(
            "VALIDATION_EVIDENCE_INVALID",
            "The Validation record does not match its kickoff, proposal and trusted debit.",
        )
    return plan, record, auth


def _research_validation_receipt(
    row: dict[str, Any], kickoff: dict[str, Any], used: int, record: dict[str, Any] | None = None
) -> dict[str, Any]:
    if record and record.get("scoring"):
        base = record["cost_stress"]["base"]
        metrics = {
            key: base["candidate"][key]
            for key in (
                "total_return", "max_drawdown", "turnover", "fees_paid", "slippage_paid",
                "sharpe", "sessions", "valued_sessions",
            )
        }
        metrics.update(
            return_delta=base["return_delta"],
            benchmark_total_return=base["baseline"]["total_return"],
        )
    else:
        metrics = None
    return {
        "schema_version": RESEARCH_SCHEMA_VERSION,
        "stage": VALIDATION_STAGE,
        "round_id": row["round_id"],
        "kickoff_id": kickoff["kickoff_id"],
        "direction_id": kickoff["direction_id"],
        "scope": kickoff["scope"],
        "data_kind": kickoff["data_kind"],
        "proposal_id": row["proposal_id"],
        "submission_id": row["submission_id"],
        "experiment_id": row["experiment_id"],
        "evaluation_id": row["evaluation_id"],
        "plan_id": row["plan_id"],
        "record_hash": record["record_hash"] if record else None,
        "status": row["status"],
        "outcome": record["outcome"] if record else None,
        "reason": record["reason"] if record else None,
        "metrics": metrics,
        "budget": _round_budget(kickoff, used),
    }


def _reusable_validation_receipt(
    store: Path, row: dict[str, Any], kickoff: dict[str, Any], used: int
) -> dict[str, Any]:
    record = None
    try:
        if row["status"] != "reading":
            _, record, _ = _research_record(store, row, kickoff)
    except _EvidenceProblem as error:
        raise _reject(error.code, "status", "inspect", error.message) from error
    return _research_validation_receipt(row, kickoff, used, record)


def research_validation_status(
    store: str | Path, round_id: str, evaluation_id: str | None = None
) -> dict[str, Any]:
    """Read status for one authorized Validation round without spending budget."""
    from prajnatrade.research_direction import load_kickoff

    store = _store(store)
    kickoff = load_kickoff(store, round_id)
    rows = _validation_rows(store, round_id)
    if evaluation_id is not None:
        row = next((row for row in rows if row["evaluation_id"] == evaluation_id), None)
        if row is None:
            raise _reject("VALIDATION_NOT_FOUND", "status", "check_id", "Unknown Validation evaluation.")
        return _reusable_validation_receipt(store, row, kickoff, len(rows))
    return {
        "schema_version": RESEARCH_SCHEMA_VERSION,
        "round_id": round_id,
        "status": "available",
        "budget": _round_budget(kickoff, len(rows)),
        "evaluations": [
            _reusable_validation_receipt(store, row, kickoff, len(rows)) for row in rows
        ],
    }


def _probe_research_isolation(
    plan: dict[str, Any],
) -> tuple[dict[str, Any], ContainerStrategyExecutor | None]:
    candidate = plan["candidate"]
    if candidate["strategy"]["family"] != "python":
        return probe_isolation_evidence(), None

    executor = executor_from_config(candidate["evaluation"]["strategy_executor"])
    return _probe_container(executor), executor


def _research_run_context(
    snapshot_store: Path, plan: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], ContainerStrategyExecutor | None]:
    evaluation = plan["candidate"]["evaluation"]
    runtime = runtime_identity(evaluation.get("strategy_executor"))
    if runtime != plan["runtime"] or runtime != evaluation["runtime"]:
        raise _reject(
            "FINGERPRINT_MISMATCH", "run", "inspect",
            "The trusted accounting engine differs from the frozen runtime.",
        )
    metadata = snapshot.manifest(snapshot_store, plan["snapshot_id"])
    if (
        metadata["data_kind"] != plan["data_kind"]
        or any(snapshot.partition_descriptor(metadata, stage) != plan["partitions"][stage]
               for stage in STAGES)
    ):
        raise _reject(
            "FINGERPRINT_MISMATCH", "run", "inspect",
            "The sealed partition descriptors differ from the frozen plan.",
        )
    try:
        isolation, executor = _probe_research_isolation(plan)
    except ValueError as error:
        raise _reject("ISOLATION_UNAVAILABLE", "run", "stop", str(error)) from error
    if isolation.get("verified") is not True:
        raise _reject(
            "ISOLATION_UNAVAILABLE", "run", "stop",
            "OS isolation could not be verified (" + str(isolation.get("failure_reason")) + ").",
        )
    return metadata, isolation, executor


def _claim_validation(
    store: Path,
    plan: dict[str, Any],
    kickoff: dict[str, Any],
    publish_reading: Callable[[dict[str, Any]], object],
) -> tuple[dict[str, Any], int]:
    round_id = kickoff["round_id"]
    fingerprint = plan["strategy_fingerprint"]
    evaluation_id = str(uuid.uuid5(uuid.NAMESPACE_OID, _hash([round_id, fingerprint])))
    row = {
        "round_id": round_id,
        "strategy_fingerprint": fingerprint,
        "plan_id": plan["plan_id"],
        "candidate_hash": plan["candidate_hash"],
        "partition_content_hash": plan["partitions"][VALIDATION_STAGE]["content_hash"],
        "partition_name": VALIDATION_STAGE,
        "stage": VALIDATION_STAGE,
        "submission_id": plan["candidate"]["submission_id"],
        "experiment_id": plan["candidate"]["experiment_id"],
        "proposal_id": plan["proposal"]["proposal_id"],
        "evaluation_id": evaluation_id,
        "status": "reading",
        "authorization_id": kickoff["kickoff_id"],
        "debited_at": _now(),
    }
    connection = _ledger(store / "queries.sqlite")
    try:
        connection.execute("BEGIN IMMEDIATE")
        used = connection.execute(
            "SELECT COUNT(*) FROM validation_queries WHERE round_id = ?", (round_id,)
        ).fetchone()[0]
        if used >= kickoff["validation_budget"]:
            raise _reject(
                "VALIDATION_BUDGET_EXHAUSTED", "run", "review",
                "The authorized Validation budget is exhausted.",
            )
        connection.execute(
            """INSERT INTO validation_queries
            (round_id, strategy_fingerprint, plan_id, candidate_hash, partition_content_hash,
             partition_name, stage, submission_id, experiment_id, proposal_id, evaluation_id,
             status, authorization_id, debited_at)
            VALUES (:round_id, :strategy_fingerprint, :plan_id, :candidate_hash,
                    :partition_content_hash, :partition_name, :stage, :submission_id,
                    :experiment_id, :proposal_id, :evaluation_id, :status, :authorization_id,
                    :debited_at)""",
            row,
        )
        connection.execute(
            """INSERT INTO evaluations
            (evaluation_id, plan_id, stage, candidate_hash, partition_content_hash, created_at)
            VALUES (:evaluation_id, :plan_id, :stage, :candidate_hash, :partition_content_hash,
                    :debited_at)""",
            row,
        )
        publish_reading(_research_validation_receipt(row, kickoff, used + 1))
        connection.execute("COMMIT")
    finally:
        connection.close()
    return row, used + 1


def _mark_validation(
    store: Path, row: dict[str, Any], record: dict[str, Any], record_path: Path
) -> None:
    status = "completed" if record["scoring"] else "aborted"
    connection = _ledger(store / "queries.sqlite")
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE validation_queries SET status = ? WHERE evaluation_id = ?",
            (status, row["evaluation_id"]),
        )
        connection.execute(
            "UPDATE evaluations SET outcome = ?, record_path = ?, completed_at = ? "
            "WHERE evaluation_id = ?",
            (record["outcome"], str(record_path), record["completed_at"], row["evaluation_id"]),
        )
        connection.execute("COMMIT")
    finally:
        connection.close()
    row["status"] = status


def _recover_validation(
    store: Path, snapshot_store: Path, row: dict[str, Any], kickoff: dict[str, Any]
) -> dict[str, Any]:
    """Reconcile after the submitting process has released the round lock."""
    record_path = store / "records" / row["evaluation_id"] / "record.json"
    if record_path.is_file():
        _, record, _ = _research_record(store, row, kickoff)
    else:
        plan = _load_plan(store, row["plan_id"])
        record, record_path, _ = _score_claim(
            store, snapshot_store, plan, VALIDATION_STAGE, _research_authorization(plan, kickoff),
            row, {}, None, {}, {},
        )
    _mark_validation(store, row, record, record_path)
    return record


@contextmanager
def _trusted_validation_lock(store: Path, round_id: str) -> Generator[None, None, None]:
    # Multiple project copies referencing the same kickoff share this lock.
    directory = store / "validation-locks"
    directory.mkdir(mode=0o700, exist_ok=True)
    with (directory / _hash(round_id)).open("a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def run_research_validation(
    store: str | Path,
    snapshot_store: str | Path,
    project: str | Path,
    *,
    round_id: str,
    submission_id: str,
    proposal_id: str,
) -> dict[str, Any]:
    """Execute a preregistered version within the project's confirmed Validation budget."""
    from prajnatrade.experiment import evaluation_subject
    from prajnatrade.research_direction import (
        load_kickoff,
        publish_validation,
        require_open_round,
        require_selection_impact,
        validation_submission,
    )

    eval_store = _store(store)
    project = Path(project)
    snapshot_store = Path(snapshot_store)
    kickoff = load_kickoff(eval_store, round_id)
    with (
        _trusted_validation_lock(eval_store, round_id),
        validation_submission(project, round_id, proposal_id) as proposal,
    ):
        subject = evaluation_subject(project, kickoff["campaign_id"], submission_id)
        if (
            proposal["submission_id"] != submission_id
            or proposal["experiment_id"] != subject["experiment_id"]
            or proposal["experiment_hash"] != subject["candidate_hash"]
            or proposal["kickoff_id"] != kickoff["kickoff_id"]
            or subject["project_id"] != kickoff["project_id"]
            or subject["snapshot_id"] != kickoff["snapshot_id"]
            or subject["fingerprints"]["evaluation"] != kickoff["campaign_evaluation_hash"]
        ):
            raise _reject(
                "VALIDATION_PROPOSAL_MISMATCH", "run", "correct",
                "The preregistration and successful Discovery experiment must match the kickoff.",
            )

        def publish(receipt: dict[str, Any]) -> object:
            return publish_validation(project, round_id, proposal_id, receipt)

        fingerprint = subject["fingerprints"]["strategy_and_parameters"]
        rows = _validation_rows(eval_store, round_id)
        for row in rows:
            if row["strategy_fingerprint"] != fingerprint:
                continue
            if row["status"] == "reading":
                _recover_validation(eval_store, snapshot_store, row, kickoff)
            receipt = _reusable_validation_receipt(eval_store, row, kickoff, len(rows))
            publish(receipt)
            return receipt
        require_open_round(project, round_id)
        if len(rows) >= kickoff["validation_budget"]:
            raise _reject(
                "VALIDATION_BUDGET_EXHAUSTED", "run", "review",
                "The authorized Validation budget is exhausted.",
            )
        if not proposal["selection_impact"]:
            previously_passed = any(
                _reusable_validation_receipt(eval_store, row, kickoff, len(rows))["outcome"] == "passed"
                for row in rows if row["status"] == "completed"
            )
            require_selection_impact(proposal, "run", passed=previously_passed)

        metadata = snapshot.manifest(snapshot_store, subject["snapshot_id"])
        body = _build_plan(
            kickoff["project_id"], subject, metadata, kickoff["validation_settings"],
            snapshot_store=snapshot_store, workflow=RESEARCH_VALIDATION,
        )
        plan = _freeze_plan(eval_store, {
            **body,
            "round_id": round_id,
            "kickoff_id": kickoff["kickoff_id"],
            "strategy_fingerprint": fingerprint,
            "proposal": proposal,
        })
        authorization = _research_authorization(plan, kickoff)
        metadata, isolation, executor = _research_run_context(snapshot_store, plan)

        def reject(code: str, message: str) -> None:
            raise _reject(code, "run", "inspect", message)

        sessions, derived, discovery_hash = _prior_discovery(snapshot_store, plan, reject)
        row, used = _claim_validation(eval_store, plan, kickoff, publish)
        record, record_path, _ = _score_claim(
            eval_store, snapshot_store, plan, VALIDATION_STAGE, authorization, row, metadata,
            (sessions, derived), {
                "partitions": ["discovery"], "discovery_content_hash": discovery_hash,
            }, isolation, executor,
        )
        _mark_validation(eval_store, row, record, record_path)
        receipt = _research_validation_receipt(row, kickoff, used, record)
        publish(receipt)
        return receipt


def _selected_validation(
    store: Path, round_id: str, evaluation_id: str
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    from prajnatrade.research_direction import load_kickoff

    kickoff = load_kickoff(store, round_id)
    row = next(
        (row for row in _validation_rows(store, round_id) if row["evaluation_id"] == evaluation_id),
        None,
    )
    if row is None or row["status"] != "completed":
        raise _EvidenceProblem(
            "VALIDATION_EVIDENCE_INVALID", "The round has no completed Validation for this ID.",
        )
    plan, record, authorization = _research_record(store, row, kickoff)
    return plan, record, authorization, row


def selected_validation_evidence(
    store: str | Path,
    round_id: str,
    evaluation_id: str,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Verify the selected Validation record, immutable debit and Holdout prior export."""
    try:
        plan, record, authorization, row = _selected_validation(_store(store), round_id, evaluation_id)
        compared = plan["candidate"]
        keys = ("project_id", "campaign_id", "experiment_id", "submission_id", "snapshot_id",
                "strategy", "parameters", "fingerprints", "evaluation", "execution_rules")
        if any(candidate.get(key) != compared.get(key) for key in keys):
            raise _EvidenceProblem(
                "VALIDATION_CANDIDATE_MISMATCH",
                "The selected Validation evaluation is bound to another experiment.",
            )
        if (
            record.get("outcome") != "passed"
            or record.get("scoring") is not True
            or record.get("aborted") is not False
        ):
            raise _EvidenceProblem(
                "VALIDATION_NOT_PASSED", "The selected Validation evaluation did not pass.",
            )
        return {
            **_verified_validation_export(record, plan),
            "plan": plan,
            "debit": row,
            "authorization": authorization,
        }
    except _EvidenceProblem as error:
        raise _reject(error.code, "selection", "inspect", error.message) from error


def research_validation_details(
    store: str | Path, round_id: str, request: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Record a question before publishing its projection from committed Validation evidence."""
    from prajnatrade.research_direction import validation_detail_receipt

    store = _store(store)
    try:
        plan, record, authorization, query = _selected_validation(
            store, round_id, request["evaluation_id"],
        )
    except _EvidenceProblem as error:
        raise _reject(error.code, "detail", "inspect", error.message) from error
    expected = {
        "round_id": round_id, "record_hash": record["record_hash"], "kickoff_id": plan["kickoff_id"],
        "scope": plan["scope"], "data_kind": plan["data_kind"],
    }
    if any(request.get(key) != value for key, value in expected.items()):
        raise _reject(
            "VALIDATION_DETAIL_REQUEST_MISMATCH", "detail", "inspect",
            "The detail question does not match its trusted Validation evidence.",
        )
    details = None
    signal_date = request["date"]
    if signal_date in record["curves"]["sessions"]:
        candidate = record["candidate_run"]["base"]
        details = {
            "signals": [signal for signal in candidate["signals"] if signal["date"] == signal_date],
            "positions": next(
                point["holdings"] for point in candidate["positions"] if point["date"] == signal_date
            ),
            "fills": [fill for fill in candidate["fills"] if fill["date"] == signal_date],
        }
    receipt = validation_detail_receipt(
        request, authorization["authorization_id"], query["partition_content_hash"], details,
    )
    path = store / "validation-details" / f"{request['detail_access_id']}.json"
    stored = write_trusted_record_once(path, receipt)
    if stored != receipt:
        raise _reject(
            "VALIDATION_DETAIL_RECEIPT_MISMATCH", "detail", "inspect",
            "The trusted receipt for the detail question differs from the new projection.",
        )
    return stored, details


def create_research_holdout_plan(
    store: str | Path,
    snapshot_store: str | Path,
    project: str | Path,
    candidate_id: str,
    round_id: str,
    validation_evaluation_id: str,
) -> dict[str, Any]:
    """Bind the selected final candidate to the comparison's unchanged evaluation protocol."""
    eval_store = _store(store)
    project = Path(project)
    candidate = candidate_status(project, candidate_id)
    evidence = selected_validation_evidence(eval_store, round_id, validation_evaluation_id, candidate)
    metadata = snapshot.manifest(Path(snapshot_store), candidate["snapshot_id"])
    body = _build_plan(
        candidate["project_id"], candidate, metadata, evidence["plan"],
        snapshot_store=Path(snapshot_store), workflow=RESEARCH_HOLDOUT,
    )
    if body["partitions"] != evidence["plan"]["partitions"]:
        raise _reject(
            "FINGERPRINT_MISMATCH", "plan", "inspect",
            "The sealed partitions differ from those in the selected Validation plan.",
        )
    return _freeze_plan(eval_store, {
        **body,
        "round_id": round_id,
        "kickoff_id": evidence["plan"]["kickoff_id"],
        "validation_binding": {
            "round_id": round_id,
            "evaluation_id": validation_evaluation_id,
            "plan_id": evidence["plan"]["plan_id"],
            **{key: evidence[key] for key in ("record_hash", "export_hash", "partition_content_hash")},
        },
    })


def run_research_holdout_evaluation(
    store: Path,
    snapshot_store: Path,
    project: Path,
    plan: dict[str, Any],
    authorization: dict[str, Any],
) -> dict[str, Any]:
    """Score a direction-bound Holdout with the pre-authorized Validation proof."""
    from prajnatrade import research_holdout

    def reject(code: str, message: str) -> None:
        audit_id = _audit(store, {
            "plan_id": plan["plan_id"], "stage": HOLDOUT_STAGE,
            "candidate_id": plan["candidate_id"], "snapshot_id": plan["snapshot_id"],
        }, code, message, command="holdout run")
        raise EvaluationRejection(code, "holdout_run", "inspect", message, context={
            "audit_id": audit_id, "opened_protected_source": False, "query_debited": False,
        })

    candidate = candidate_status(project, plan["candidate_id"])
    if candidate != plan["candidate"] or str(project_status(project)["project_id"]) != plan["project_id"]:
        reject("FINGERPRINT_MISMATCH", "The project and frozen candidate must match the Holdout plan.")
    if (
        research_holdout.load_authorization(store, plan["plan_id"]) != authorization
        or not authorization_matches(authorization, plan, HOLDOUT_STAGE)
    ):
        reject("AUTHORIZATION_REQUIRED", "The saved Holdout authorization must match the frozen plan.")
    selection = candidate["research_selection"]
    evidence = selected_validation_evidence(
        store, selection["round_id"], selection["validation_evaluation_id"], candidate,
    )
    binding = authorization["validation"]
    if (
        evidence["evaluation_id"] != binding["evaluation_id"]
        or evidence["record_hash"] != binding["record_hash"]
        or evidence["plan"]["plan_id"] != binding["plan_id"]
        or evidence["export_hash"] != binding["export_hash"]
        or authorization["direction_id"] != selection["direction_id"]
        or authorization["round_id"] != selection["round_id"]
    ):
        reject("VALIDATION_SELECTION_MISMATCH", "Holdout is bound to different selected Validation evidence.")

    existing = research_holdout.load_holdout_claim(store, plan["plan_id"])
    if existing:
        return _recover_research_holdout(store, plan, authorization, existing)

    metadata, isolation, executor = _research_run_context(snapshot_store, plan)
    if not any(
        (snapshot_store / version / f"{HOLDOUT_STAGE}.json").is_file()
        for version in snapshot.lineage_ids(snapshot_store, plan["snapshot_id"])
    ):
        reject("SOURCE_MISSING", "The authorized sealed Holdout payload is unavailable.")
    prior, prior_evidence = _prior_holdout(
        snapshot_store, plan, evidence, plan["partitions"][HOLDOUT_STAGE], reject,
    )
    claim = research_holdout.claim(store, plan, authorization)
    if claim["state"] == "blocked":
        return claim["blocker"]
    if claim["state"] == "existing":
        return _recover_research_holdout(store, plan, authorization, claim)
    prior_evidence["holdout_exposures"] = claim["prior_exposures"]
    record, _, status = _score_claim(
        store, snapshot_store, plan, HOLDOUT_STAGE, authorization, claim, metadata,
        prior, prior_evidence, isolation, executor,
    )
    research_holdout.finish(
        store, plan["plan_id"], record["evaluation_id"], status, record["completed_at"],
    )
    return {**_run_response(store, record, reused=False), "schema_version": 2, "scope": plan["scope"]}


def _recover_research_holdout(store: Path, plan: dict, authorization: dict, query: dict) -> dict:
    from prajnatrade import research_holdout

    evaluation_id = query["evaluation_id"]
    if query["authorization_id"] != authorization["auth_id"]:
        raise _reject("AUTHORIZATION_INTEGRITY_FAILED", "holdout_run", "inspect", "The authorization and debit differ.")
    path = store / "records" / evaluation_id / "record.json"
    if not path.is_file() and query["status"] in {"reading", "interrupted"}:
        research_holdout.finish(store, plan["plan_id"], evaluation_id, "interrupted", _now())
        return {
            "schema_version": 2, "scope": plan["scope"], "status": "interrupted",
            "plan_id": plan["plan_id"], "evaluation_id": evaluation_id,
            "query_debited": True, "opened_protected_source": "unverified",
            "message": "The reserved Holdout has no committed record. The read cannot be retried.",
        }
    try:
        record = _load_stored_record(store, evaluation_id)
    except _EvidenceProblem as problem:
        raise _reject(problem.code, "holdout_run", "inspect", problem.message) from problem
    if (
        not _record_matches(record, plan, HOLDOUT_STAGE)
        or record["authorization"] != authorization
        or record["query_receipt"]["debited_at"] != query["debited_at"]
    ):
        raise _reject("RECORD_INTEGRITY_FAILED", "holdout_run", "inspect", "The stored Holdout record differs from its debit.")
    research_holdout.finish(
        store, plan["plan_id"], evaluation_id, "completed" if record["scoring"] else "aborted",
        record["completed_at"],
    )
    return {**_run_response(store, record, reused=True), "schema_version": 2, "scope": plan["scope"]}
