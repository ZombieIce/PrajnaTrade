from __future__ import annotations

import hashlib
import fcntl
import json
import math
import platform
import signal
import sqlite3
import tempfile
import time
import uuid
from contextlib import contextmanager
from importlib.metadata import version
from pathlib import Path
from typing import Any, Iterator, TypedDict

from prajnatrade.backtest import equal_weight_buy_and_hold, run_backtest
from prajnatrade.data import ValidatedData, load_bars
from prajnatrade.strategies import STRATEGY_FAMILIES, make_strategy, normalize_strategy_spec


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _load_snapshot(content: bytes, suffix: str) -> ValidatedData:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / ("discovery" + suffix)
        path.write_bytes(content)
        return load_bars(path, "total_return")


def _runtime_identity() -> str:
    directory = Path(__file__).parent
    files = ("strategies.py", "backtest.py", "data.py")
    return _fingerprint({
        "files": {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in files},
        "numpy": version("numpy"), "pandas": version("pandas"),
        "pyarrow": version("pyarrow"), "python": platform.python_version(),
    })


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
                evaluation_json TEXT NOT NULL, evaluation_hash TEXT NOT NULL,
                attempts INTEGER NOT NULL, max_seconds REAL NOT NULL,
                elapsed_seconds REAL NOT NULL DEFAULT 0, baseline_json TEXT NOT NULL
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
    project: Path, data: Path, attempts: int, max_seconds: float,
    initial_cash: float, transaction_cost_bps: float, slippage_bps: float,
) -> dict[str, object]:
    project_id = project_status(project)["project_id"]
    if attempts <= 0 or not math.isfinite(max_seconds) or max_seconds <= 0:
        raise ValueError("Attempts and execution seconds must be positive.")
    content = data.read_bytes()
    suffix = data.suffix.lower()
    dataset = _load_snapshot(content, suffix)
    evaluation = {
        "price_basis": "total_return",
        "data_kind": "synthetic",
        "initial_cash": initial_cash,
        "transaction_cost_bps": transaction_cost_bps,
        "slippage_bps": slippage_bps,
        "runtime": _runtime_identity(),
    }
    baseline = run_backtest(
        dataset, equal_weight_buy_and_hold, initial_cash=initial_cash,
        transaction_cost_bps=transaction_cost_bps, slippage_bps=slippage_bps,
    )
    campaign_id = str(uuid.uuid4())
    evaluation_hash = _fingerprint(evaluation)
    with sqlite3.connect(project / ".prajnatrade" / "project.sqlite") as connection:
        connection.execute(
            """INSERT INTO campaigns
            (campaign_id, snapshot, suffix, snapshot_hash, evaluation_json,
             evaluation_hash, attempts, max_seconds, baseline_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (campaign_id, content, suffix, dataset.data_hash, json.dumps(evaluation),
             evaluation_hash, attempts, max_seconds, json.dumps(baseline.metrics)),
        )
    return {
        "schema_version": 1, "project_id": project_id, "campaign_id": campaign_id,
        "fingerprints": {"snapshot": dataset.data_hash, "evaluation": evaluation_hash},
        "budget": {"attempts_remaining": attempts, "execution_seconds_remaining": max_seconds},
    }


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
    if not isinstance(strategy, dict) or set(strategy) != {"family"}:
        raise ValueError("Strategy must identify a declarative family.")
    if strategy["family"] not in STRATEGY_FAMILIES:
        raise ValueError("Unsupported declarative strategy family.")
    spec = normalize_strategy_spec({"family": strategy["family"], "parameters": raw["parameters"]})
    return {
        "schema_version": 1, "campaign_id": raw["campaign_id"],
        "submission_id": raw["submission_id"], "hypothesis": raw["hypothesis"],
        "strategy": {"family": spec["family"]}, "parameters": spec["parameters"],
        "parent_experiment_id": parent,
    }


@contextmanager
def _campaign_lock(project: Path, campaign_id: str, *, blocking: bool = True) -> Iterator[None]:
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
            budget = _budget(connection, campaign)
            remaining = budget["execution_seconds_remaining"]
            experiment_id, started = _admit_attempt(
                connection, campaign_id, submission_id, request, canonical, budget,
            )
        connection.commit()
        return _run_attempt(
            connection, campaign, project_id, experiment_id, request, started, remaining,
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


def _run_attempt(
    connection: sqlite3.Connection, campaign: sqlite3.Row, project_id: object,
    experiment_id: str, request: dict[str, Any], started: float, remaining: float,
) -> dict[str, Any]:
    campaign_id = campaign["campaign_id"]
    error_details = None
    result = None
    old_handler = signal.signal(signal.SIGALRM, _timeout)
    try:
        slice_seconds = remaining - (time.monotonic() - started)
        if slice_seconds <= 0:
            raise _TimeExpired()
        signal.setitimer(signal.ITIMER_REAL, slice_seconds)
        dataset = _load_snapshot(campaign["snapshot"], campaign["suffix"])
        if dataset.data_hash != campaign["snapshot_hash"]:
            raise ValueError("Snapshot fingerprint mismatch.")
        config = json.loads(campaign["evaluation_json"])
        if _fingerprint(config) != campaign["evaluation_hash"]:
            raise ValueError("Evaluation fingerprint mismatch.")
        if config["runtime"] != _runtime_identity():
            raise ValueError("Campaign runtime fingerprint mismatch.")
        spec = {"family": request["strategy"]["family"], "parameters": request["parameters"]}
        backtest = run_backtest(
            dataset, make_strategy(spec), initial_cash=config["initial_cash"],
            transaction_cost_bps=config["transaction_cost_bps"], slippage_bps=config["slippage_bps"],
        )
        benchmark = json.loads(campaign["baseline_json"])
        result = {
            "fingerprints": {
                "strategy_and_parameters": _fingerprint({"strategy": spec, "runtime": config["runtime"]}),
                "snapshot": campaign["snapshot_hash"], "evaluation": campaign["evaluation_hash"],
            },
            "discovery": {
                "start_date": dataset.report["start_date"], "end_date": dataset.report["end_date"],
                "metrics": backtest.metrics, "benchmark": benchmark,
                "return_delta": backtest.metrics["total_return"] - benchmark["total_return"],
            },
            "diagnostics": [
                {"code": "WARMUP_REQUIRED", "required_sessions": request["parameters"]["lookback"]}
            ] if "lookback" in request["parameters"] else [],
        }
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
