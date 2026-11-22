from __future__ import annotations

import fcntl
import hashlib
import json
import math
import platform
import signal
import sqlite3
import time
import uuid
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any, TypedDict

from prajnatrade.container_executor import (
    ContainerStrategyExecutor,
    executor_from_config,
)
from prajnatrade.snapshot import read_discovery
from prajnatrade.snapshot_baseline import (
    run_discovery_equal_weight,
    run_discovery_strategy,
)
from prajnatrade.strategies import (
    STRATEGY_FAMILIES,
    make_strategy,
    normalize_strategy_spec,
)


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _runtime_components(
    strategy_executor: dict[str, Any] | None = None,
) -> dict[str, str]:
    directory = Path(__file__).parent
    files = (
        "strategies.py", "backtest.py", "data.py", "snapshot.py", "snapshot_baseline.py",
        "container_executor.py",
    )
    executor = dict(strategy_executor) if strategy_executor is not None else {"kind": "none"}
    return {
        **{
            name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
            for name in files
        },
        "strategy_executor": _fingerprint(executor),
        "numpy": version("numpy"),
        "pandas": version("pandas"),
        "pyarrow": version("pyarrow"),
        "python": platform.python_version(),
    }


def _runtime_identity(strategy_executor: dict[str, Any] | None = None) -> str:
    return _fingerprint(_runtime_components(strategy_executor))


def _executor_config(config: Mapping[str, Any]) -> dict[str, Any]:
    candidate = config.get("strategy_executor")
    return dict(candidate) if isinstance(candidate, Mapping) else {"kind": "none"}


_EXECUTION_RULES = {
    "signal": {
        "source": "sealed_discovery.derived.value",
        "basis": "total_return",
        "availability": "evidence_required_before_next_open",
    },
    "execution": {
        "source": "sealed_discovery.raw_bars.open",
        "basis": "unadjusted",
        "attempts": 1,
        "missing_required_open": "whole_rebalance_not_filled",
    },
    "valuation": {
        "source": "sealed_discovery.raw_bars.close",
        "basis": "unadjusted",
        "includes": ["cash", "dividend_receivables"],
        "missing_required_close": "nav_unavailable",
    },
    "corporate_actions": {
        "dividend": "ex_date_receivable_paid_on_pay_date",
        "share_adjustment": "apply_before_open_execution",
    },
}


def _campaign_evaluation(
    *,
    data_kind: str,
    snapshot_id: str,
    partition_content_hash: str,
    as_of: str | None,
    initial_cash: float,
    transaction_cost_bps: float,
    slippage_bps: float,
    strategy_executor: dict[str, Any],
) -> dict[str, Any]:
    runtime_dependencies = _runtime_components(strategy_executor)
    return {
        "engine": "sealed-discovery-v1",
        "snapshot_id": snapshot_id,
        "partition_content_hash": partition_content_hash,
        "as_of": as_of,
        "data_kind": data_kind,
        "initial_cash": initial_cash,
        "transaction_cost_bps": transaction_cost_bps,
        "slippage_bps": slippage_bps,
        "execution_rules": _EXECUTION_RULES,
        "strategy_executor": strategy_executor,
        "runtime_dependencies": runtime_dependencies,
        "runtime": _fingerprint(runtime_dependencies),
    }


def project_init(project: Path) -> dict[str, object]:
    directory = project / ".prajnatrade"
    directory.mkdir(parents=True, exist_ok=True)
    database = directory / "project.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE project (project_id TEXT NOT NULL)")
        connection.execute(
            """CREATE TABLE campaigns (
                campaign_id TEXT PRIMARY KEY, snapshot BLOB NOT NULL,
                suffix TEXT NOT NULL, snapshot_hash TEXT NOT NULL,
                snapshot_id TEXT, evaluation_json TEXT NOT NULL,
                evaluation_hash TEXT NOT NULL, attempts INTEGER NOT NULL,
                max_seconds REAL NOT NULL, elapsed_seconds REAL NOT NULL DEFAULT 0,
                baseline_json TEXT NOT NULL
            )"""
        )
        connection.execute(
            """CREATE TABLE experiments (
                experiment_id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL,
                submission_id TEXT NOT NULL, request_json TEXT NOT NULL,
                status TEXT NOT NULL, started_at REAL NOT NULL,
                result_json TEXT, error_json TEXT,
                UNIQUE(campaign_id, submission_id),
                FOREIGN KEY(campaign_id) REFERENCES campaigns(campaign_id)
            )"""
        )
        connection.execute(
            """CREATE TABLE frozen_candidates (
                candidate_id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL,
                experiment_id TEXT NOT NULL, candidate_hash TEXT NOT NULL,
                candidate_json TEXT NOT NULL,
                FOREIGN KEY(campaign_id) REFERENCES campaigns(campaign_id),
                FOREIGN KEY(experiment_id) REFERENCES experiments(experiment_id)
            )"""
        )
        connection.executescript(
            """
            CREATE TRIGGER frozen_candidates_no_update
            BEFORE UPDATE ON frozen_candidates
            BEGIN SELECT RAISE(ABORT, 'frozen candidates are immutable'); END;
            CREATE TRIGGER frozen_candidates_no_delete
            BEFORE DELETE ON frozen_candidates
            BEGIN SELECT RAISE(ABORT, 'frozen candidates are immutable'); END;
            """
        )
        project_id = str(uuid.uuid4())
        connection.execute("INSERT INTO project VALUES (?)", (project_id,))
    return {"schema_version": 1, "project_id": project_id}


def project_status(project: Path) -> dict[str, object]:
    database = project / ".prajnatrade" / "project.sqlite"
    if not database.is_file():
        raise ValueError("Project is not initialized.")
    with sqlite3.connect(database) as connection:
        row = connection.execute("SELECT project_id FROM project").fetchone()
    if row is None:
        raise ValueError("Project identity is missing.")
    return {"schema_version": 1, "project_id": row[0]}


def campaign_create(
    project: Path, store: Path, snapshot_id: str, attempts: int, max_seconds: float,
    initial_cash: float, transaction_cost_bps: float, slippage_bps: float,
    as_of: str | None = None, *,
    strategy_executor: str = "none",
    strategy_image: str | None = None,
    executor: ContainerStrategyExecutor | None = None,
) -> dict[str, object]:
    project_id = project_status(project)["project_id"]
    if attempts <= 0 or not math.isfinite(max_seconds) or max_seconds <= 0:
        raise ValueError("Attempts and execution seconds must be positive.")
    executor_config = _resolve_executor_config(
        strategy_executor, strategy_image, executor,
    )
    receipt = read_discovery(store, snapshot_id, "discovery", as_of)
    data = receipt["data"]
    data_kind = receipt["data_kind"]
    content = json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")
    snapshot_hash = receipt["content_hash"]
    partition_hash = receipt.get("partition_content_hash", snapshot_hash)
    evaluation = _campaign_evaluation(
        data_kind=data_kind,
        snapshot_id=snapshot_id,
        partition_content_hash=partition_hash,
        as_of=as_of,
        initial_cash=initial_cash,
        transaction_cost_bps=transaction_cost_bps,
        slippage_bps=slippage_bps,
        strategy_executor=executor_config,
    )
    baseline = run_discovery_equal_weight(
        data,
        snapshot_id,
        partition_hash,
        data_kind=data_kind,
        initial_cash=initial_cash,
        transaction_cost_bps=transaction_cost_bps,
        slippage_bps=slippage_bps,
    )
    campaign_id = str(uuid.uuid4())
    evaluation_hash = _fingerprint(evaluation)
    with sqlite3.connect(project / ".prajnatrade" / "project.sqlite") as connection:
        _ensure_project_schema(connection)
        connection.execute(
            """INSERT INTO campaigns
            (campaign_id, snapshot, suffix, snapshot_hash, snapshot_id, evaluation_json,
             evaluation_hash, attempts, max_seconds, baseline_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                campaign_id,
                content,
                ".json",
                snapshot_hash,
                snapshot_id,
                json.dumps(evaluation, sort_keys=True, separators=(",", ":")),
                evaluation_hash,
                attempts,
                max_seconds,
                json.dumps(baseline, sort_keys=True, separators=(",", ":"), allow_nan=False),
            ),
        )
    return {
        "schema_version": 1,
        "project_id": project_id,
        "campaign_id": campaign_id,
        "snapshot_id": snapshot_id,
        "data_kind": data_kind,
        "fingerprints": {
            "snapshot": snapshot_hash,
            "partition": partition_hash,
            "evaluation": evaluation_hash,
        },
        "budget": {"attempts_remaining": attempts, "execution_seconds_remaining": max_seconds},
        "strategy_executor": executor_config,
    }


def _resolve_executor_config(
    strategy_executor: str,
    strategy_image: str | None,
    executor: ContainerStrategyExecutor | None,
) -> dict[str, Any]:
    if strategy_executor not in {"none", "container"}:
        raise ValueError("Strategy executor must be none or container.")
    if strategy_executor == "none":
        if strategy_image is not None:
            raise ValueError("--strategy-image requires --strategy-executor container.")
        if executor is not None:
            raise ValueError("A container executor was supplied without the container kind.")
        return {"kind": "none"}
    if not isinstance(strategy_image, str) or not strategy_image.strip():
        raise ValueError("--strategy-image is required when --strategy-executor is container.")
    configured = executor or ContainerStrategyExecutor(strategy_image)
    configuration = configured.resolve_configuration()
    if configuration.get("kind") != "container":
        raise ValueError("The supplied container executor did not resolve a container config.")
    return configuration


def _ensure_project_schema(connection: sqlite3.Connection) -> None:
    campaign_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(campaigns)").fetchall()
    }
    if "snapshot_id" not in campaign_columns:
        connection.execute("ALTER TABLE campaigns ADD COLUMN snapshot_id TEXT")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS frozen_candidates (
            candidate_id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL,
            experiment_id TEXT NOT NULL, candidate_hash TEXT NOT NULL,
            candidate_json TEXT NOT NULL,
            FOREIGN KEY(campaign_id) REFERENCES campaigns(campaign_id),
            FOREIGN KEY(experiment_id) REFERENCES experiments(experiment_id)
        )"""
    )
    connection.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS frozen_candidates_no_update
        BEFORE UPDATE ON frozen_candidates
        BEGIN SELECT RAISE(ABORT, 'frozen candidates are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS frozen_candidates_no_delete
        BEFORE DELETE ON frozen_candidates
        BEGIN SELECT RAISE(ABORT, 'frozen candidates are immutable'); END;
        """
    )


class ProtocolError(Exception):
    def __init__(self, code: str, phase: str, next_action: str, message: str) -> None:
        super().__init__(message)
        self.details = {
            "code": code, "phase": phase, "next_action": next_action, "message": message,
        }


def _connection(project: Path) -> sqlite3.Connection:
    project_status(project)
    connection = sqlite3.connect(project / ".prajnatrade" / "project.sqlite", timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    _ensure_project_schema(connection)
    return connection


def _campaign(connection: sqlite3.Connection, campaign_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM campaigns WHERE campaign_id = ?", (campaign_id,),
    ).fetchone()
    if row is None:
        raise ProtocolError("INVALID_SUBMISSION", "admission", "correct", "Unknown campaign.")
    return row


class _Budget(TypedDict):
    attempts_remaining: int
    execution_seconds_remaining: float


def _budget(connection: sqlite3.Connection, campaign: sqlite3.Row) -> _Budget:
    count = connection.execute(
        "SELECT count(*) FROM experiments WHERE campaign_id = ?", (campaign["campaign_id"],),
    ).fetchone()[0]
    running = connection.execute(
        "SELECT started_at FROM experiments WHERE campaign_id = ? AND status = 'running'",
        (campaign["campaign_id"],),
    ).fetchone()
    elapsed = campaign["elapsed_seconds"]
    if running is not None:
        running_elapsed = time.time() - running["started_at"]
        elapsed += running_elapsed if running_elapsed >= 0 else campaign["max_seconds"]
    return {
        "attempts_remaining": max(0, campaign["attempts"] - count),
        "execution_seconds_remaining": max(0.0, campaign["max_seconds"] - elapsed),
    }


def _receipt(
    project_id: object, connection: sqlite3.Connection, campaign: sqlite3.Row,
    experiment: sqlite3.Row,
) -> dict[str, Any]:
    request = json.loads(experiment["request_json"])
    result: dict[str, Any] = {
        "schema_version": 1, "project_id": project_id,
        "campaign_id": experiment["campaign_id"], "submission_id": experiment["submission_id"],
        "experiment_id": experiment["experiment_id"], "status": experiment["status"],
        "hypothesis": request["hypothesis"], "strategy": request["strategy"],
        "parameters": request["parameters"], "parent_experiment_id": request["parent_experiment_id"],
        "budget": _budget(connection, campaign),
    }
    if experiment["result_json"]:
        result.update(json.loads(experiment["result_json"]))
    if experiment["error_json"]:
        result["error"] = json.loads(experiment["error_json"])
        if result["error"]["code"] == "EXECUTION_INTERRUPTED":
            result["error"]["next_action"] = (
                "resume" if result["budget"]["execution_seconds_remaining"] > 0 else "stop"
            )
    return result


def _normalize_request(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("Submission must be an object.")
    required = {"schema_version", "campaign_id", "submission_id", "hypothesis", "strategy", "parameters"}
    if not required <= raw.keys() or raw.keys() - required - {"parent_experiment_id"}:
        raise ValueError("Submission fields do not match schema version 1.")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise ValueError("Unsupported schema version.")
    for key in ("campaign_id", "submission_id", "hypothesis"):
        if not isinstance(raw[key], str) or not raw[key].strip():
            raise ValueError(f"{key} must be a non-empty string.")
    parent = raw.get("parent_experiment_id")
    if parent is not None and (not isinstance(parent, str) or not parent.strip()):
        raise ValueError("parent_experiment_id must be a non-empty string.")
    strategy = raw["strategy"]
    if not isinstance(strategy, dict):
        raise ValueError("Strategy must be a JSON object.")
    family = strategy.get("family")
    if family in STRATEGY_FAMILIES:
        if set(strategy) != {"family"}:
            raise ValueError("Declarative strategies accept only the family field.")
        spec = normalize_strategy_spec(
            {"family": family, "parameters": raw["parameters"]}
        )
        canonical_strategy: dict[str, Any] = {"family": spec["family"]}
    elif family == "python":
        if "source" not in strategy or set(strategy) - {"family", "source", "parameter_schema"}:
            raise ValueError(
                "Python strategies require family and source and accept only parameter_schema."
            )
        candidate: dict[str, Any] = {
            "family": "python",
            "source": strategy["source"],
            "parameters": raw["parameters"],
        }
        if "parameter_schema" in strategy:
            candidate["parameter_schema"] = strategy["parameter_schema"]
        spec = normalize_strategy_spec(candidate)
        canonical_source = str(spec["source"])
        canonical_strategy = {
            "family": "python",
            "source": canonical_source,
            "source_sha256": hashlib.sha256(canonical_source.encode("utf-8")).hexdigest(),
        }
        if "parameter_schema" in spec:
            canonical_strategy["parameter_schema"] = spec["parameter_schema"]
    else:
        raise ValueError("Unsupported strategy family.")
    return {
        "schema_version": 1, "campaign_id": raw["campaign_id"],
        "submission_id": raw["submission_id"], "hypothesis": raw["hypothesis"],
        "strategy": canonical_strategy, "parameters": spec["parameters"],
        "parent_experiment_id": parent,
    }


@contextmanager
def _campaign_lock(
    project: Path, campaign_id: str, *, blocking: bool = True
) -> Generator[None, None, None]:
    filename = hashlib.sha256(campaign_id.encode()).hexdigest() + ".lock"
    with (project / ".prajnatrade" / filename).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _settle_interrupted(connection: sqlite3.Connection, campaign: sqlite3.Row) -> None:
    running = connection.execute(
        "SELECT * FROM experiments WHERE campaign_id = ? AND status = 'running'",
        (campaign["campaign_id"],),
    ).fetchone()
    if running is None:
        return
    elapsed = time.time() - running["started_at"]
    if not math.isfinite(elapsed) or elapsed < 0:
        elapsed = campaign["max_seconds"]
    error = {
        "code": "EXECUTION_INTERRUPTED", "phase": "discovery",
        "next_action": "stop", "message": "Execution was interrupted; the attempt remains charged.",
    }
    connection.execute(
        "UPDATE campaigns SET elapsed_seconds = elapsed_seconds + ? WHERE campaign_id = ?",
        (elapsed, campaign["campaign_id"]),
    )
    connection.execute(
        "UPDATE experiments SET status = 'failed', error_json = ? WHERE experiment_id = ?",
        (json.dumps(error), running["experiment_id"]),
    )
    connection.commit()


class _TimeExpired(Exception):
    pass


def _timeout(_signum: int, _frame: object) -> None:
    raise _TimeExpired()


def experiment_submit(project: Path, raw: object) -> dict[str, Any]:
    try:
        request = _normalize_request(raw)
    except (ValueError, TypeError) as error:
        raise ProtocolError("INVALID_SUBMISSION", "admission", "correct", str(error)) from error
    return _execute_request(project, request["campaign_id"], request["submission_id"], request)


def experiment_resume(project: Path, campaign_id: str, submission_id: str) -> dict[str, Any]:
    return _execute_request(project, campaign_id, submission_id, None)


def _find_experiment(
    connection: sqlite3.Connection, campaign_id: str, submission_id: str,
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM experiments WHERE campaign_id = ? AND submission_id = ?",
        (campaign_id, submission_id),
    ).fetchone()


def _execute_request(
    project: Path, campaign_id: str, submission_id: str, request: dict[str, Any] | None,
) -> dict[str, Any]:
    project_id = project_status(project)["project_id"]
    with _campaign_lock(project, campaign_id), _connection(project) as connection:
        campaign = _campaign(connection, campaign_id)
        _settle_interrupted(connection, campaign)
        campaign = _campaign(connection, campaign_id)
        previous = _find_experiment(connection, campaign_id, submission_id)
        if request is None:
            if previous is None:
                raise ProtocolError("NOT_FOUND", "resume", "check_id", "Unknown submission.")
            receipt = _receipt(project_id, connection, campaign, previous)
            if receipt.get("error", {}).get("code") != "EXECUTION_INTERRUPTED":
                return receipt
            remaining = _budget(connection, campaign)["execution_seconds_remaining"]
            if remaining <= 0:
                raise ProtocolError("BUDGET_EXHAUSTED", "resume", "stop", "Execution time exhausted.")
            experiment_id = previous["experiment_id"]
            request = json.loads(previous["request_json"])
            started = time.monotonic()
            connection.execute(
                "UPDATE experiments SET status = 'running', started_at = ?, error_json = NULL "
                "WHERE experiment_id = ?",
                (time.time(), experiment_id),
            )
        else:
            canonical = json.dumps(request, sort_keys=True, separators=(",", ":"), allow_nan=False)
            if previous is not None:
                if previous["request_json"] != canonical:
                    raise ProtocolError("SUBMISSION_CONFLICT", "admission", "use_new_submission_id", "Submission ID already has different content.")
                return _receipt(project_id, connection, campaign, previous)
            _require_container_executor(campaign, request)
            budget = _budget(connection, campaign)
            remaining = budget["execution_seconds_remaining"]
            experiment_id, started = _admit_attempt(
                connection, campaign_id, submission_id, request, canonical, budget,
            )
        connection.commit()
        return _run_attempt(
            connection, campaign, project_id, experiment_id, request, started, remaining,
        )


def _require_container_executor(
    campaign: sqlite3.Row, request: Mapping[str, Any],
) -> None:
    if request["strategy"]["family"] != "python":
        return
    executor = _executor_config(json.loads(campaign["evaluation_json"]))
    if executor.get("kind") != "container":
        raise ProtocolError(
            "INVALID_SUBMISSION", "admission", "correct",
            "Generated Python requires a campaign created with "
            "--strategy-executor container.",
        )


def _admit_attempt(
    connection: sqlite3.Connection, campaign_id: str, submission_id: str,
    request: dict[str, Any], canonical: str, budget: _Budget,
) -> tuple[str, float]:
    if request["parent_experiment_id"] is not None:
        parent = connection.execute(
            "SELECT 1 FROM experiments WHERE campaign_id = ? AND experiment_id = ?",
            (campaign_id, request["parent_experiment_id"]),
        ).fetchone()
        if parent is None:
            raise ProtocolError("INVALID_SUBMISSION", "admission", "correct", "Parent experiment is not in this campaign.")
    if budget["attempts_remaining"] <= 0 or budget["execution_seconds_remaining"] <= 0:
        raise ProtocolError("BUDGET_EXHAUSTED", "admission", "stop", "Campaign budget exhausted.")
    experiment_id = str(uuid.uuid4())
    started = time.monotonic()
    connection.execute(
        """INSERT INTO experiments
        (experiment_id, campaign_id, submission_id, request_json, status, started_at)
        VALUES (?, ?, ?, ?, 'running', ?)""",
        (experiment_id, campaign_id, submission_id, canonical, time.time()),
    )
    return experiment_id, started


def _strategy_identity(request: Mapping[str, Any]) -> dict[str, Any]:
    strategy = request["strategy"]
    identity: dict[str, Any] = {
        "family": strategy["family"], "parameters": request["parameters"],
    }
    if strategy["family"] == "python":
        identity["source_sha256"] = strategy["source_sha256"]
        if "parameter_schema" in strategy:
            identity["parameter_schema"] = strategy["parameter_schema"]
    return identity


def _executor_evidence(
    config: Mapping[str, Any], invocations: list[dict[str, Any]],
) -> dict[str, Any]:
    limits = config.get("limits")
    return {
        "kind": "container",
        "image": {"reference": config.get("image"), "digest": config.get("digest")},
        "limits": dict(limits) if isinstance(limits, Mapping) else {},
        "invocation_count": len(invocations),
        "invocations": list(invocations),
        "evidence_sha256": _fingerprint(list(invocations)),
    }


def _run_attempt(
    connection: sqlite3.Connection, campaign: sqlite3.Row, project_id: object,
    experiment_id: str, request: dict[str, Any], started: float, remaining: float,
) -> dict[str, Any]:
    campaign_id = campaign["campaign_id"]
    family = request["strategy"]["family"]
    executor_config: dict[str, Any] = {"kind": "none"}
    executor_evidence: list[dict[str, Any]] = []
    error_details = None
    result = None
    old_handler = signal.signal(signal.SIGALRM, _timeout)
    try:
        slice_seconds = remaining - (time.monotonic() - started)
        if slice_seconds <= 0:
            raise _TimeExpired()
        signal.setitimer(signal.ITIMER_REAL, slice_seconds)
        content = bytes(campaign["snapshot"])
        if hashlib.sha256(content).hexdigest() != campaign["snapshot_hash"]:
            raise ValueError("Snapshot fingerprint mismatch.")
        data = json.loads(content)
        config = json.loads(campaign["evaluation_json"])
        if _fingerprint(config) != campaign["evaluation_hash"]:
            raise ValueError("Evaluation fingerprint mismatch.")
        executor_config = _executor_config(config)
        if config["runtime"] != _runtime_identity(executor_config):
            raise ValueError("Campaign runtime fingerprint mismatch.")
        spec: dict[str, Any] = {
            "family": family, "parameters": request["parameters"],
        }
        strategy = None
        if family == "python":
            if executor_config.get("kind") != "container":
                raise ValueError(
                    "Generated Python requires a campaign created with "
                    "--strategy-executor container."
                )
            spec["source"] = str(request["strategy"]["source"])
            if "parameter_schema" in request["strategy"]:
                spec["parameter_schema"] = request["strategy"]["parameter_schema"]
            strategy = make_strategy(
                spec,
                container_executor=executor_from_config(executor_config),
                evidence_callback=executor_evidence.append,
            )
        else:
            strategy = make_strategy(spec)
        window = _signal_window(spec)
        discovery = run_discovery_strategy(
            data,
            config["snapshot_id"],
            config["partition_content_hash"],
            strategy,
            data_kind=config["data_kind"],
            signal_window_sessions=window,
            initial_cash=config["initial_cash"],
            transaction_cost_bps=config["transaction_cost_bps"],
            slippage_bps=config["slippage_bps"],
        )
        benchmark = json.loads(campaign["baseline_json"])
        baseline_return = benchmark["metrics"]["total_return"]
        result_return = discovery["metrics"]["total_return"]
        comparison = {
            "start_date": discovery["start_date"],
            "end_date": discovery["end_date"],
            "baseline": benchmark,
            "return_delta": (
                result_return - baseline_return
                if result_return is not None and baseline_return is not None
                else None
            ),
            "fees_paid_delta": (
                discovery["metrics"]["fees_paid"] - benchmark["metrics"]["fees_paid"]
            ),
            "slippage_paid_delta": (
                discovery["metrics"]["slippage_paid"] - benchmark["metrics"]["slippage_paid"]
            ),
        }
        discovery.update({
            "benchmark": benchmark["metrics"],
            "return_delta": comparison["return_delta"],
            "baseline_comparison": comparison,
        })
        result = {
            "data_kind": config["data_kind"],
            "fingerprints": {
                "strategy_and_parameters": _fingerprint({
                    "strategy": _strategy_identity(request), "runtime": config["runtime"],
                }),
                "runtime_dependencies": config["runtime"],
                "snapshot": campaign["snapshot_hash"],
                "partition": config["partition_content_hash"],
                "evaluation": campaign["evaluation_hash"],
            },
            "runtime_dependencies": config["runtime_dependencies"],
            "discovery": discovery,
            "diagnostics": [
                {"code": "WARMUP_REQUIRED", "required_sessions": window}
            ] if window > 1 else [],
        }
        if family == "python":
            result["strategy"] = dict(request["strategy"])
            result["executor"] = _executor_evidence(executor_config, executor_evidence)
        if time.monotonic() - started >= remaining:
            raise _TimeExpired()
    except _TimeExpired:
        error_details = {"code": "BUDGET_EXHAUSTED", "phase": "discovery", "next_action": "stop", "message": "Execution time exhausted."}
    except Exception as error:
        error_details = {
            "code": "EXECUTION_FAILED", "phase": "discovery", "next_action": "inspect",
            "message": f"{type(error).__name__}: {error}",
        }
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
    if error_details is not None and family == "python":
        if executor_config.get("kind") == "container":
            error_details["executor"] = _executor_evidence(
                executor_config, executor_evidence,
            )
    elapsed = time.monotonic() - started
    if error_details and error_details["code"] == "BUDGET_EXHAUSTED":
        elapsed = max(elapsed, remaining)
    connection.execute(
        "UPDATE campaigns SET elapsed_seconds = elapsed_seconds + ? WHERE campaign_id = ?",
        (elapsed, campaign_id),
    )
    connection.execute(
        "UPDATE experiments SET status = ?, result_json = ?, error_json = ? WHERE experiment_id = ?",
        ("failed" if error_details else "succeeded", json.dumps(result) if not error_details else None,
         json.dumps(error_details) if error_details else None, experiment_id),
    )
    connection.commit()
    campaign = _campaign(connection, campaign_id)
    experiment = _find_experiment(connection, campaign_id, request["submission_id"])
    assert experiment is not None
    return _receipt(project_id, connection, campaign, experiment)


def experiment_status(project: Path, campaign_id: str, submission_id: str) -> dict[str, Any]:
    project_id = project_status(project)["project_id"]
    try:
        with _campaign_lock(project, campaign_id, blocking=False), _connection(project) as connection:
            _settle_interrupted(connection, _campaign(connection, campaign_id))
    except BlockingIOError:
        pass
    with _connection(project) as connection:
        connection.execute("BEGIN")
        campaign = _campaign(connection, campaign_id)
        experiment = _find_experiment(connection, campaign_id, submission_id)
        if experiment is None:
            raise ProtocolError("NOT_FOUND", "status", "check_id", "Unknown submission.")
        return _receipt(project_id, connection, campaign, experiment)


PYTHON_SIGNAL_WINDOW_SESSIONS = 60
MAX_SIGNAL_WINDOW_SESSIONS = 252


def _signal_window(spec: dict[str, Any]) -> int:
    family = spec["family"]
    raw_lookback = spec["parameters"].get("lookback", 1)
    if family == "python":
        # Generated Python needs a real history panel, not the one-session
        # cross-section a lookback-less declarative strategy sees.  A strategy
        # may request a smaller window via ``parameters.lookback``; the window
        # is always point-in-time and capped to one trading year.
        requested_lookback = spec["parameters"].get("lookback")
        if (
            isinstance(requested_lookback, int)
            and not isinstance(requested_lookback, bool)
            and 1 <= requested_lookback <= MAX_SIGNAL_WINDOW_SESSIONS
        ):
            lookback = requested_lookback
        else:
            lookback = PYTHON_SIGNAL_WINDOW_SESSIONS
    else:
        lookback = int(raw_lookback)
    return lookback + 1 if family in {"momentum", "inverse_volatility"} else lookback


def freeze_experiment_candidate(
    project: Path,
    campaign_id: str,
    submission_id: str,
    name: str,
    conclusion: str,
) -> dict[str, Any]:
    if not name.strip() or not conclusion.strip():
        raise ValueError("Candidate name and conclusion must not be empty.")
    project_id = project_status(project)["project_id"]
    with _connection(project) as connection:
        campaign = _campaign(connection, campaign_id)
        experiment = _find_experiment(connection, campaign_id, submission_id)
        if experiment is None:
            raise ValueError("Unknown experiment submission.")
        if experiment["status"] != "succeeded" or not experiment["result_json"]:
            raise ValueError("Only a successful experiment can be frozen as a candidate.")
        request = json.loads(experiment["request_json"])
        result = json.loads(experiment["result_json"])
        config = json.loads(campaign["evaluation_json"])
        candidate_id = str(uuid.uuid4())
        frozen = {
            "schema_version": 1,
            "candidate_id": candidate_id,
            "project_id": project_id,
            "campaign_id": campaign_id,
            "experiment_id": experiment["experiment_id"],
            "submission_id": submission_id,
            "name": name.strip(),
            "hypothesis": request["hypothesis"],
            "conclusion": conclusion.strip(),
            "strategy": request["strategy"],
            "parameters": request["parameters"],
            "fingerprints": result["fingerprints"],
            "snapshot_id": config["snapshot_id"],
            "evaluation": config,
            "runtime_dependencies": result["runtime_dependencies"],
            "execution_rules": config["execution_rules"],
            "frozen_at": datetime.now(UTC).isoformat(),
        }
        frozen["candidate_hash"] = _fingerprint(frozen)
        canonical = json.dumps(frozen, sort_keys=True, separators=(",", ":"), allow_nan=False)
        connection.execute(
            """INSERT INTO frozen_candidates
            (candidate_id, campaign_id, experiment_id, candidate_hash, candidate_json)
            VALUES (?, ?, ?, ?, ?)""",
            (
                candidate_id,
                campaign_id,
                experiment["experiment_id"],
                frozen["candidate_hash"],
                canonical,
            ),
        )
        return frozen


def candidate_status(project: Path, candidate_id: str) -> dict[str, Any]:
    project_status(project)
    with _connection(project) as connection:
        row = connection.execute(
            "SELECT candidate_hash, candidate_json FROM frozen_candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Unknown frozen candidate.")
        candidate = json.loads(row["candidate_json"])
        expected_hash = _fingerprint({
            key: value for key, value in candidate.items() if key != "candidate_hash"
        })
        if expected_hash != candidate["candidate_hash"] or row["candidate_hash"] != expected_hash:
            raise ValueError("Frozen candidate fingerprint mismatch.")
        return candidate


def error_context(project: Path, campaign_id: object, submission_id: object) -> dict[str, Any]:
    result: dict[str, Any] = {"project_id": project_status(project)["project_id"]}
    if isinstance(campaign_id, str):
        result["campaign_id"] = campaign_id
        with _connection(project) as connection:
            campaign = connection.execute(
                "SELECT * FROM campaigns WHERE campaign_id = ?", (campaign_id,),
            ).fetchone()
            if campaign is not None:
                result["budget"] = _budget(connection, campaign)
                if isinstance(submission_id, str):
                    experiment = connection.execute(
                        "SELECT experiment_id FROM experiments WHERE campaign_id = ? AND submission_id = ?",
                        (campaign_id, submission_id),
                    ).fetchone()
                    if experiment is not None:
                        result["experiment_id"] = experiment["experiment_id"]
    if isinstance(submission_id, str):
        result["submission_id"] = submission_id
    return result
