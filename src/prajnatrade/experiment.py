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
from copy import deepcopy
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any, TypedDict

from prajnatrade.container_executor import (
    ContainerStrategyExecutor,
    executor_from_config,
)
from prajnatrade.discovery_summary import summarize_discovery_quarters
from prajnatrade.snapshot import (
    discovery_split as snapshot_discovery_split,
)
from prajnatrade.snapshot import (
    manifest as snapshot_manifest,
)
from prajnatrade.snapshot import (
    partition_descriptor as snapshot_partition_descriptor,
)
from prajnatrade.snapshot import (
    partition_slice,
    read_discovery,
    slice_content,
)
from prajnatrade.trading_status import (
    ACTUAL_OPEN_RULES,
    CARRY_FORWARD_SUSPENDED_VALUATION,
    EXECUTION_RULE_VERSIONS,
    NEXT_OPEN_RULES,
    RETRY_INITIAL_RULES,
    STRICT_CLOSE_VALUATION,
    VALUATION_POLICY_VERSIONS,
    valuation_policy_version,
)


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _runtime_components(
    strategy_executor: dict[str, Any] | None = None,
    *,
    execution_rules: Mapping[str, Any] | None = None,
    history_policy: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    directory = Path(__file__).parent
    files = (
        "strategies.py", "backtest.py", "data.py", "snapshot.py", "snapshot_baseline.py",
        "container_executor.py",
    )
    valuation_version = valuation_policy_version(execution_rules)
    # Actual-open matching and carry-forward valuation consult trading_status.py.
    if (
        (execution_rules is not None and execution_rules.get("version") == ACTUAL_OPEN_RULES)
        or valuation_version != STRICT_CLOSE_VALUATION
    ):
        files += ("trading_status.py",)
    if history_policy is not None:
        files += ("experiment.py",)
    executor = dict(strategy_executor) if strategy_executor is not None else {"kind": "none"}
    return {
        **{
            name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
            for name in files
        },
        **({"valuation_policy": valuation_version} if valuation_version != STRICT_CLOSE_VALUATION else {}),
        "strategy_executor": _fingerprint(executor),
        "numpy": version("numpy"),
        "pandas": version("pandas"),
        "pyarrow": version("pyarrow"),
        "python": platform.python_version(),
    }


# The declared-history engine adds a separate path. Bind exact source revisions
# when recognizing a legacy campaign built before that path was introduced.
_LEGACY_SNAPSHOT_SOURCE = "f79160cacc77f7e0be10d4f43b03a854370b7caa1c03ea757a78df9b1ede1e77"
_LEGACY_ACCOUNTING_SOURCE = "a7d354f0665281bbda758958ba93a7772427a5f48b8594a94590508c48494c34"
_UNVERSIONED_ACCOUNTING_SOURCE = "bdcb9c0c14c138d1ad7f4c8b35e6c9b0c056d5bb7af45e762024e3cc8a3e6e72"
_COMPATIBLE_RUNTIME_SOURCES = {
    "snapshot.py": "46bd58c44b85fd6990f4a16bc247b42e80ac5d07ca273e168ed23a78451b4554",
    "snapshot_baseline.py": "8af82f3b897e5105927828e4b116cb1642a02b4eef688c6da3b396dd19a449a2",
}


def runtime_identity(
    strategy_executor: dict[str, Any] | None = None,
    *,
    execution_rules: Mapping[str, Any] | None = None,
    history_policy: Mapping[str, Any] | None = None,
) -> str:
    """Fingerprint the accounting dependencies and optional strategy executor.

    The Campaign's ``execution_rules`` select rule-specific dependencies.
    Its ``history_policy`` also binds the declared-history engine
    (``experiment.py``). A source fix changes the corresponding identity.
    """
    dependencies = _runtime_components(
        strategy_executor, execution_rules=execution_rules, history_policy=history_policy,
    )
    if (
        history_policy is None
        and execution_rules is not None
        and "version" not in execution_rules
        and dependencies["snapshot_baseline.py"] == _LEGACY_ACCOUNTING_SOURCE
    ):
        dependencies["snapshot_baseline.py"] = _UNVERSIONED_ACCOUNTING_SOURCE
    return _fingerprint(dependencies)


def runtime_matches(
    stored: str,
    strategy_executor: dict[str, Any] | None = None,
    *,
    execution_rules: Mapping[str, Any] | None = None,
    history_policy: Mapping[str, Any] | None = None,
) -> bool:
    """Check the exact current runtime or a compatible legacy dependency set."""
    dependencies = _runtime_components(
        strategy_executor, execution_rules=execution_rules, history_policy=history_policy,
    )
    if stored == _fingerprint(dependencies):
        return True
    if (
        history_policy is not None
        or "trading_status.py" in dependencies
        or "valuation_policy" in dependencies
        or any(
            dependencies[name] != digest for name, digest in _COMPATIBLE_RUNTIME_SOURCES.items()
        )
    ):
        return False
    legacy = {
        **dependencies,
        "snapshot.py": _LEGACY_SNAPSHOT_SOURCE,
        "snapshot_baseline.py": (
            _UNVERSIONED_ACCOUNTING_SOURCE
            if execution_rules is not None and "version" not in execution_rules
            else _LEGACY_ACCOUNTING_SOURCE
        ),
    }
    return stored == _fingerprint(legacy)


def _executor_config(config: Mapping[str, Any]) -> dict[str, Any]:
    candidate = config.get("strategy_executor")
    return dict(candidate) if isinstance(candidate, Mapping) else {"kind": "none"}


_NEXT_OPEN_EXECUTION = {
    "signal": {
        "source": "sealed_partition.derived.value",
        "basis": "total_return",
        "availability": "evidence_required_before_next_open",
    },
    "execution": {
        "source": "sealed_partition.raw_bars.open",
        "basis": "unadjusted",
        "attempts": 1,
        "missing_required_open": "whole_rebalance_not_filled",
    },
    "valuation": {
        "source": "sealed_partition.raw_bars.close",
        "basis": "unadjusted",
        "includes": ["cash", "dividend_receivables"],
        "missing_required_close": "nav_unavailable",
    },
    "corporate_actions": {
        "dividend": "ex_date_receivable_paid_on_pay_date",
        "share_adjustment": "apply_before_open_execution",
    },
}


def _execution_policy(
    version: str, *, valuation_policy: str = STRICT_CLOSE_VALUATION,
) -> dict[str, Any]:
    if version not in EXECUTION_RULE_VERSIONS:
        raise ValueError(f"Unknown execution rules version: {version!r}.")
    if valuation_policy not in VALUATION_POLICY_VERSIONS:
        raise ValueError(f"Unknown valuation policy version: {valuation_policy!r}.")
    rules = {"version": version, **deepcopy(_NEXT_OPEN_EXECUTION)}
    if version == RETRY_INITIAL_RULES:
        rules["execution"].update(
            initial_entry_retry="next_open_until_filled_or_superseded",
            retry_scope="before_first_position_is_established",
        )
    elif version == ACTUAL_OPEN_RULES:
        # The sealed actual daily open is a matching base price only, never a
        # signal input. Each required symbol matches separately, sells fund the
        # same day's buys with their realized net, and an unfilled symbol keeps
        # its cash or shares instead of blocking the whole basket (ADR 0002).
        rules["execution"].update(
            halted_open="actual_daily_open_when_evidenced",
            partial_halt_open_availability="not_gated_by_0930",
            missing_required_open="symbol_not_filled",
            partial_fill="recorded_per_symbol",
            funding="initial_cash_plus_realized_sell_net",
            order="sells_before_buys",
            matching="per_symbol",
        )
    if valuation_policy == CARRY_FORWARD_SUSPENDED_VALUATION:
        rules["valuation"] = {
            "version": CARRY_FORWARD_SUSPENDED_VALUATION,
            "source": "sealed_partition.raw_bars.close",
            "basis": "unadjusted",
            "includes": ["cash", "dividend_receivables"],
            "confirmed_suspended_close": "carry_forward_prior_trusted_unadjusted_close",
            "corporate_actions_during_suspension": "adjust_carried_basis_when_evidenced",
            "missing_required_close": "nav_unavailable",
        }
    return rules


def _campaign_evaluation(
    *,
    data_kind: str,
    snapshot_schema_version: int,
    snapshot_id: str,
    partition_content_hash: str,
    as_of: str | None,
    initial_cash: float,
    transaction_cost_bps: float,
    slippage_bps: float,
    strategy_executor: dict[str, Any],
    execution_rules: dict[str, Any],
    history_policy: Mapping[str, Any] | None = None,
    discovery_split: Mapping[str, Any] | None = None,
    full_history: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    runtime_dependencies = _runtime_components(
        strategy_executor, execution_rules=execution_rules, history_policy=history_policy,
    )
    valuation_version = valuation_policy_version(execution_rules)
    return {
        "engine": "sealed-discovery-v1",
        # The opt-in 70/30 protocol is the one new research Campaigns must use;
        # the field is additive so readers of legacy stored Campaigns use
        # ``.get("research_protocol", "discovery-replay")``.
        "research_protocol": (
            "trailing-70-30" if discovery_split is not None else "discovery-replay"
        ),
        "snapshot_id": snapshot_id,
        "partition_content_hash": partition_content_hash,
        "as_of": as_of,
        "data_kind": data_kind,
        "snapshot_schema_version": snapshot_schema_version,
        "initial_cash": initial_cash,
        "transaction_cost_bps": transaction_cost_bps,
        "slippage_bps": slippage_bps,
        "execution_rules": execution_rules,
        **(
            {"valuation_policy": dict(execution_rules["valuation"])}
            if valuation_version != STRICT_CLOSE_VALUATION
            else {}
        ),
        "strategy_executor": strategy_executor,
        "runtime_dependencies": runtime_dependencies,
        "runtime": _fingerprint(runtime_dependencies),
        **({"history_policy": dict(history_policy)} if history_policy is not None else {}),
        **({"discovery_split": dict(discovery_split)} if discovery_split is not None else {}),
        **({"full_history": dict(full_history)} if full_history is not None else {}),
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
    execution_rules: str = NEXT_OPEN_RULES,
    valuation_policy: str = STRICT_CLOSE_VALUATION,
    strategy_executor: str = "none",
    strategy_image: str | None = None,
    executor: ContainerStrategyExecutor | None = None,
    discovery_warmup_sessions: int | None = None,
    discovery_split: str | None = None,
) -> dict[str, object]:
    project_id = project_status(project)["project_id"]
    if attempts <= 0 or not math.isfinite(max_seconds) or max_seconds <= 0:
        raise ValueError("Attempts and execution seconds must be positive.")
    execution_policy = _execution_policy(execution_rules, valuation_policy=valuation_policy)
    executor_config = _resolve_executor_config(
        strategy_executor, strategy_image, executor,
    )
    split_ratio = (
        _discovery_split_ratio(discovery_split) if discovery_split is not None else None
    )
    # Read the sealed Discovery payload exactly once; an opt-in split then
    # restricts the Campaign copy and every later Discovery read to its
    # development slice.
    receipt = read_discovery(
        store,
        snapshot_id,
        "discovery",
        as_of,
        include_trailing=True,
    )
    data = receipt["data"]
    data_kind = receipt["data_kind"]
    metadata = snapshot_manifest(store, snapshot_id)
    authorized_history = metadata.get("authorized_history")
    if authorized_history is not None:
        if as_of is not None:
            raise ValueError("An authorized full-history Campaign cannot use --as-of.")
        if split_ratio != (70, 30):
            raise ValueError(
                "Authorized full-history Campaigns require --discovery-split 70/30."
            )
        if discovery_warmup_sessions is None:
            raise ValueError(
                "Authorized full-history Campaigns require --discovery-warmup-sessions."
            )
        authorized_range = authorized_history.get("range")
        sealed_partition = snapshot_partition_descriptor(metadata, "discovery")
        if not isinstance(authorized_range, dict) or (
            sealed_partition["content_hash"] != authorized_range.get("content_hash")
            or sealed_partition["start"] != authorized_range.get("start")
            or sealed_partition["end"] != authorized_range.get("end")
            or sealed_partition["sessions"] != authorized_range.get("sessions")
        ):
            raise ValueError("The sealed Discovery partition does not match its authorized range.")
    split_descriptor = (
        snapshot_discovery_split(
            data, development=split_ratio[0], as_of=receipt.get("as_of")
        )
        if split_ratio is not None else None
    )
    if split_descriptor is not None:
        development = split_descriptor["development"]
        data = slice_content(data, development["start"], development["end"])
        sliced = partition_slice(data, development["start"], development["end"])
        if sliced != development:
            raise ValueError("The development Discovery slice does not reproduce.")
        snapshot_hash = development["content_hash"]
    else:
        snapshot_hash = receipt["content_hash"]
    content = _discovery_json(data)
    partition_hash = receipt.get("partition_content_hash", receipt["content_hash"])
    history_policy = None
    prior_history = None
    baseline_options: dict[str, Any] = {}
    if discovery_warmup_sessions is not None:
        history_policy = _discovery_history_policy(data, receipt, discovery_warmup_sessions)
        prior_history = receipt.get("prior_history")
        baseline_options = {"history_requirements": [], "scoring_start": history_policy["scoring_start"]}
    full_history = None
    if authorized_history is not None:
        if split_descriptor is None or history_policy is None:
            raise ValueError("The authorized full-history split and scoring policy are required.")
        development_sessions = sorted({row["date"] for row in data["calendar"]})
        warmup = history_policy["warmup_sessions"]
        warmup_dates = development_sessions[:warmup]
        scoring_dates = development_sessions[warmup:]
        full_history = {
            "authorization_id": authorized_history["authorization_id"],
            "authorized_range": dict(authorized_history["range"]),
            "source": dict(authorized_history["source"]),
            "discovery": dict(split_descriptor["development"]),
            "validation": dict(split_descriptor["trailing"]),
            "warmup": {
                "source": "authorized_history",
                "start": warmup_dates[0] if warmup_dates else None,
                "end": warmup_dates[-1] if warmup_dates else None,
                "sessions": len(warmup_dates),
            },
            "scoring": {
                "start": scoring_dates[0],
                "end": scoring_dates[-1],
                "sessions": len(scoring_dates),
            },
        }
    evaluation = _campaign_evaluation(
        data_kind=data_kind,
        snapshot_schema_version=receipt["schema_version"],
        snapshot_id=snapshot_id,
        partition_content_hash=partition_hash,
        as_of=as_of,
        initial_cash=initial_cash,
        transaction_cost_bps=transaction_cost_bps,
        slippage_bps=slippage_bps,
        strategy_executor=executor_config,
        execution_rules=execution_policy,
        history_policy=history_policy,
        discovery_split=split_descriptor,
        full_history=full_history,
    )
    from prajnatrade.snapshot_baseline import run_discovery_equal_weight

    baseline = run_discovery_equal_weight(
        data,
        snapshot_id,
        partition_hash,
        data_kind=data_kind,
        initial_cash=initial_cash,
        transaction_cost_bps=transaction_cost_bps,
        slippage_bps=slippage_bps,
        execution_rule_version=execution_rules,
        valuation_policy_version=valuation_policy,
        **baseline_options,
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
        if prior_history is not None:
            connection.execute(
                "INSERT INTO campaign_prior_history (campaign_id, payload) VALUES (?, ?)",
                (campaign_id, _discovery_json(prior_history)),
            )
    return {
        "schema_version": 1,
        "project_id": project_id,
        "campaign_id": campaign_id,
        "snapshot_id": snapshot_id,
        "data_kind": data_kind,
        "research_protocol": evaluation["research_protocol"],
        "fingerprints": {
            "snapshot": snapshot_hash,
            "partition": partition_hash,
            "evaluation": evaluation_hash,
        },
        "budget": {"attempts_remaining": attempts, "execution_seconds_remaining": max_seconds},
        "execution_rules": execution_policy,
        **(
            {"valuation_policy": dict(execution_policy["valuation"])}
            if valuation_policy != STRICT_CLOSE_VALUATION
            else {}
        ),
        "strategy_executor": executor_config,
        **({"history_policy": history_policy} if history_policy is not None else {}),
        **({"discovery_split": split_descriptor} if split_descriptor is not None else {}),
        **({"full_history": full_history} if full_history is not None else {}),
    }


def _discovery_json(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")


def _discovery_split_ratio(raw: object) -> tuple[int, int]:
    """Parse the opt-in ``--discovery-split development/validation`` pair.

    Both parts must be positive integers summing to 100.  Only the
    leading-development share is implemented, so anything but ``70/30`` is a
    clear ``ValueError`` rather than a silently different split.
    """
    if not isinstance(raw, str) or raw.count("/") != 1:
        raise ValueError(
            "--discovery-split must be a development/validation ratio such as 70/30."
        )
    development_text, validation_text = raw.split("/")
    if not (development_text.isdigit() and validation_text.isdigit()):
        raise ValueError(
            "--discovery-split parts must be positive integers summing to 100."
        )
    development, validation = int(development_text), int(validation_text)
    if development <= 0 or validation <= 0 or development + validation != 100:
        raise ValueError(
            "--discovery-split parts must be positive integers summing to 100."
        )
    if development != 70:
        raise ValueError("Only the leading 70/30 Discovery split is implemented.")
    return development, validation


def _discovery_history_policy(
    data: dict[str, list[dict]], receipt: Mapping[str, Any], warmup_sessions: int,
) -> dict[str, Any]:
    sessions = sorted({row["date"] for row in data["calendar"]})
    if type(warmup_sessions) is not int or not 0 <= warmup_sessions < len(sessions):
        raise ValueError("Discovery warmup must leave at least one scoring session.")
    policy = {
        "schema_version": 1,
        "warmup_sessions": warmup_sessions,
        "scoring_start": sessions[warmup_sessions],
    }
    if "prior_history" in receipt:
        policy["prior_history_hash"] = receipt["prior_history_hash"]
    return policy


def _history_options(
    policy: Mapping[str, Any], prior_history: dict[str, list] | None,
    requirements: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "scoring_start": policy["scoring_start"],
        "history_requirements": requirements,
        "prior_history": (
            (prior_history["sessions"], prior_history["derived"])
            if prior_history is not None else None
        ),
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
        """CREATE TABLE IF NOT EXISTS campaign_prior_history (
            campaign_id TEXT PRIMARY KEY, payload BLOB NOT NULL,
            FOREIGN KEY(campaign_id) REFERENCES campaigns(campaign_id)
        )"""
    )
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


def campaign_discovery(project: Path, campaign_id: str) -> dict[str, Any]:
    """Return the frozen Discovery bindings available to a research round."""
    project_id = project_status(project)["project_id"]
    with _connection(project) as connection:
        campaign = _campaign(connection, campaign_id)
        evaluation = json.loads(campaign["evaluation_json"])
        if _fingerprint(evaluation) != campaign["evaluation_hash"]:
            raise ValueError("Campaign evaluation fingerprint mismatch.")
        return {
            "project_id": project_id,
            "campaign_id": campaign_id,
            "snapshot_id": evaluation["snapshot_id"],
            "snapshot_hash": campaign["snapshot_hash"],
            "campaign_evaluation_hash": campaign["evaluation_hash"],
            "evaluation": evaluation,
            "discovery_budget": {
                "attempts": campaign["attempts"],
                "max_seconds": campaign["max_seconds"],
            },
        }


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
    from prajnatrade.strategies import STRATEGY_FAMILIES, normalize_strategy_spec

    family = strategy.get("family")
    if family in STRATEGY_FAMILIES:
        if set(strategy) - {"family", "history_requirements"}:
            raise ValueError("Declarative strategies accept only family and history_requirements.")
        spec = normalize_strategy_spec(
            {"family": family, "parameters": raw["parameters"]}
        )
        canonical_strategy: dict[str, Any] = {"family": spec["family"]}
    elif family == "python":
        if "source" not in strategy or set(strategy) - {
            "family", "source", "parameter_schema", "history_requirements",
        }:
            raise ValueError(
                "Python strategies require family and source and accept parameter_schema "
                "and history_requirements."
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
    if "history_requirements" in strategy:
        canonical_strategy["history_requirements"] = strategy_history_requirements({
            **spec, "history_requirements": strategy["history_requirements"],
        })
    return {
        "schema_version": 1, "campaign_id": raw["campaign_id"],
        "submission_id": raw["submission_id"], "hypothesis": raw["hypothesis"],
        "strategy": canonical_strategy, "parameters": spec["parameters"],
        "parent_experiment_id": parent,
    }


@contextmanager
def campaign_execution_lock(
    project: Path, campaign_id: str, *, blocking: bool = True
) -> Generator[None, None, None]:
    """Serialize a Campaign's Discovery attempts with research round transitions."""
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
    from prajnatrade.research_direction import require_discovery_work

    project_id = project_status(project)["project_id"]
    with campaign_execution_lock(project, campaign_id), _connection(project) as connection:
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
            require_discovery_work(project, connection, campaign_id)
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
            _freeze_history_requirements(campaign, request)
            canonical = json.dumps(request, sort_keys=True, separators=(",", ":"), allow_nan=False)
            if previous is not None:
                if previous["request_json"] != canonical:
                    raise ProtocolError("SUBMISSION_CONFLICT", "admission", "use_new_submission_id", "Submission ID already has different content.")
                return _receipt(project_id, connection, campaign, previous)
            require_discovery_work(project, connection, campaign_id)
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


def _freeze_history_requirements(campaign: sqlite3.Row, request: dict[str, Any]) -> None:
    policy = json.loads(campaign["evaluation_json"]).get("history_policy")
    strategy = request["strategy"]
    if policy is not None:
        strategy["history_requirements"] = strategy_history_requirements({
            **strategy, "parameters": request["parameters"],
        })
    elif "history_requirements" in strategy:
        raise ProtocolError(
            "INVALID_SUBMISSION", "admission", "correct",
            "Strategy history requirements need a Campaign with a frozen Discovery scoring start.",
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
    if "history_requirements" in strategy:
        identity["history_requirements"] = strategy["history_requirements"]
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
        rules = config["execution_rules"]
        policy = config.get("history_policy")
        if not runtime_matches(
            config["runtime"], executor_config, execution_rules=rules, history_policy=policy,
        ):
            raise ValueError("Campaign runtime fingerprint mismatch.")
        spec: dict[str, Any] = {
            "family": family, "parameters": request["parameters"],
        }
        history_options = (
            _attempt_history_options(connection, campaign_id, policy, data, request["strategy"])
            if policy is not None else {}
        )
        from prajnatrade.snapshot_baseline import run_discovery_strategy
        from prajnatrade.strategies import make_strategy

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
        window = signal_window_sessions(spec)
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
            execution_rule_version=rules.get("version", NEXT_OPEN_RULES),
            valuation_policy_version=valuation_policy_version(rules),
            **history_options,
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
            ] if policy is None and window > 1 else [],
        }
        if family == "python":
            result["strategy"] = dict(request["strategy"])
            result["executor"] = _executor_evidence(executor_config, executor_evidence)
        # Additive identity echo: the sealed snapshot protocol this result was
        # computed under (a Campaign stored before #56 lacks the evaluation key).
        if "snapshot_schema_version" in config:
            result["snapshot_schema_version"] = config["snapshot_schema_version"]
        result["quarterly_summary"] = summarize_discovery_quarters(
            discovery,
            benchmark,
            identity={
                "experiment_id": experiment_id,
                "submission_id": request["submission_id"],
                "campaign_id": campaign_id,
                "strategy": {
                    "family": family,
                    "parameters": request["parameters"],
                },
                "snapshot_id": config["snapshot_id"],
                "fingerprints": result["fingerprints"],
                "interval": {
                    "start_date": discovery["start_date"],
                    "end_date": discovery["end_date"],
                },
                "execution_rules": rules,
                "evaluation_protocol": campaign["evaluation_hash"],
            },
        )
        if time.monotonic() - started >= remaining:
            raise _TimeExpired()
    except _TimeExpired:
        error_details = {"code": "BUDGET_EXHAUSTED", "phase": "discovery", "next_action": "stop", "message": "Execution time exhausted."}
    except ProtocolError as error:
        error_details = error.details
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


def _attempt_history_options(
    connection: sqlite3.Connection, campaign_id: str, policy: Mapping[str, Any],
    data: dict[str, list[dict]], strategy: Mapping[str, Any],
) -> dict[str, Any]:
    row = connection.execute(
        "SELECT payload FROM campaign_prior_history WHERE campaign_id = ?", (campaign_id,),
    ).fetchone()
    content = row["payload"] if row is not None else None
    actual_hash = hashlib.sha256(content).hexdigest() if content is not None else None
    if actual_hash != policy.get("prior_history_hash"):
        raise ValueError("Discovery prior history fingerprint mismatch.")
    options = _history_options(
        policy, json.loads(content) if content is not None else None,
        strategy["history_requirements"],
    )
    from prajnatrade.snapshot_baseline import first_ready_open

    if first_ready_open(data, **options) != policy["scoring_start"]:
        raise ProtocolError(
            "INSUFFICIENT_HISTORY", "discovery", "inspect",
            f"Strategy history is unavailable at the Discovery scoring start {policy['scoring_start']}.",
        )
    return options


def experiment_status(project: Path, campaign_id: str, submission_id: str) -> dict[str, Any]:
    project_id = project_status(project)["project_id"]
    try:
        with campaign_execution_lock(project, campaign_id, blocking=False), _connection(project) as connection:
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


def signal_window_sessions(spec: Mapping[str, Any]) -> int:
    """Return the frozen trailing window required by the strategy."""
    family = spec["family"]
    parameters = spec["parameters"]
    raw_lookback = parameters.get("lookback", 1)
    if family == "python":
        # Generated Python needs a real history panel, not the one-session
        # cross-section a lookback-less declarative strategy sees.  A strategy
        # may request a smaller window via ``parameters.lookback``; the window
        # is always point-in-time and capped to one trading year.
        requested_lookback = parameters.get("lookback")
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


_signal_window = signal_window_sessions


def strategy_history_requirements(
    spec: Mapping[str, Any], *, parameter_variant: bool = False,
) -> list[dict[str, Any]]:
    """Return the canonical declaration, with the family minimum for each variant."""
    family = spec["family"]
    minimum = 0 if family == "equal_weight" else signal_window_sessions(spec)
    default = {"id": "strategy_lookback", "observations": minimum, "mode": "valid_observations"}
    if "history_requirements" not in spec:
        return [default] if minimum else []
    raw = spec["history_requirements"]
    if not isinstance(raw, list):
        raise ValueError("history_requirements must be an array.")
    requirements: dict[str, dict[str, Any]] = {}
    for entry in raw:
        if not isinstance(entry, dict) or set(entry) != {"id", "observations", "mode"}:
            raise ValueError("History requirements need id, observations and mode.")
        identifier, observations, mode = entry["id"], entry["observations"], entry["mode"]
        if not isinstance(identifier, str) or not identifier or identifier != identifier.strip():
            raise ValueError("History requirement IDs must be nonempty strings without outer whitespace.")
        if identifier in requirements:
            raise ValueError(f"Duplicate history requirement ID: {identifier!r}.")
        if type(observations) is not int or observations <= 0:
            raise ValueError("History requirement observations must be a positive integer.")
        if mode not in ("valid_observations", "consecutive_sessions"):
            raise ValueError("History requirement mode must be valid_observations or consecutive_sessions.")
        requirements[identifier] = dict(entry)
    declared = max((item["observations"] for item in requirements.values()), default=0)
    if family != "python" and declared < minimum:
        if not parameter_variant:
            raise ValueError(f"{family} requires at least {minimum} observations.")
        if "strategy_lookback" in requirements:
            requirements["strategy_lookback"]["observations"] = minimum
        else:
            requirements["strategy_lookback"] = default
    return [requirements[name] for name in sorted(requirements)]


def _candidate_body(
    connection: sqlite3.Connection, project_id: object, campaign_id: str, submission_id: str,
) -> tuple[dict[str, Any], sqlite3.Row]:
    campaign = _campaign(connection, campaign_id)
    experiment = _find_experiment(connection, campaign_id, submission_id)
    if experiment is None:
        raise ValueError("Unknown experiment submission.")
    if experiment["status"] != "succeeded" or not experiment["result_json"]:
        raise ValueError("Only a successful experiment can be frozen as a candidate or evaluated.")
    request = json.loads(experiment["request_json"])
    result = json.loads(experiment["result_json"])
    config = json.loads(campaign["evaluation_json"])
    return {
        "schema_version": 1,
        "project_id": project_id,
        "campaign_id": campaign_id,
        "experiment_id": experiment["experiment_id"],
        "submission_id": submission_id,
        "hypothesis": request["hypothesis"],
        "strategy": request["strategy"],
        "parameters": request["parameters"],
        "fingerprints": result["fingerprints"],
        "snapshot_id": config["snapshot_id"],
        "evaluation": config,
        "runtime_dependencies": result["runtime_dependencies"],
        "execution_rules": config["execution_rules"],
    }, experiment


def _evaluation_subject(body: dict[str, Any], experiment: sqlite3.Row) -> dict[str, Any]:
    subject = {
        **body,
        "candidate_id": body["experiment_id"],
        "name": body["submission_id"],
        "conclusion": body["hypothesis"],
        "frozen_at": datetime.fromtimestamp(experiment["started_at"], UTC).isoformat(),
    }
    return {**subject, "candidate_hash": _fingerprint(subject)}


def evaluation_subject(project: Path, campaign_id: str, submission_id: str) -> dict[str, Any]:
    """Derive a stable evaluation subject from successful Discovery without freezing it."""
    project_id = project_status(project)["project_id"]
    with _connection(project) as connection:
        body, experiment = _candidate_body(connection, project_id, campaign_id, submission_id)
    return _evaluation_subject(body, experiment)


def campaign_experiment_history(project: Path, campaign_id: str) -> list[dict[str, Any]]:
    """Return the existing Discovery experiment receipts in attempt order."""
    project_id = project_status(project)["project_id"]
    with _connection(project) as connection:
        connection.execute("BEGIN")
        campaign = _campaign(connection, campaign_id)
        attempts = connection.execute(
            "SELECT * FROM experiments WHERE campaign_id = ? ORDER BY rowid", (campaign_id,),
        )
        return [_receipt(project_id, connection, campaign, attempt) for attempt in attempts]


def _store_candidate(connection: sqlite3.Connection, body: dict[str, Any]) -> dict[str, Any]:
    frozen = {**body, "candidate_hash": _fingerprint(body)}
    canonical = json.dumps(frozen, sort_keys=True, separators=(",", ":"), allow_nan=False)
    connection.execute(
        """INSERT INTO frozen_candidates
        (candidate_id, campaign_id, experiment_id, candidate_hash, candidate_json)
        VALUES (?, ?, ?, ?, ?)""",
        (
            frozen["candidate_id"],
            frozen["campaign_id"],
            frozen["experiment_id"],
            frozen["candidate_hash"],
            canonical,
        ),
    )
    return frozen


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
        body, _ = _candidate_body(connection, project_id, campaign_id, submission_id)
        return _store_candidate(connection, {
            **body,
            "candidate_id": str(uuid.uuid4()),
            "name": name.strip(),
            "conclusion": conclusion.strip(),
            "frozen_at": datetime.now(UTC).isoformat(),
        })


def _research_candidate_id(round_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"prajnatrade:research-candidate:{round_id}"))


def freeze_research_candidate(
    project: Path, campaign_id: str, submission_id: str, name: str, selection: dict[str, Any],
) -> dict[str, Any]:
    """Freeze the selected Discovery subject with its exact Validation binding."""
    project_id = project_status(project)["project_id"]
    with _connection(project) as connection:
        body, experiment = _candidate_body(connection, project_id, campaign_id, submission_id)
        subject = _evaluation_subject(body, experiment)
        if (
            selection["experiment_id"] != subject["experiment_id"]
            or selection["experiment_hash"] != subject["candidate_hash"]
        ):
            raise ValueError("The selected Validation does not match this experiment.")
        return _store_candidate(connection, {
            **body,
            "candidate_id": _research_candidate_id(selection["round_id"]),
            "name": name,
            "conclusion": selection["summary"]["selection_reason"],
            "frozen_at": datetime.now(UTC).isoformat(),
            "research_selection": selection,
        })


def _load_candidate(connection: sqlite3.Connection, candidate_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT candidate_hash, candidate_json FROM frozen_candidates WHERE candidate_id = ?",
        (candidate_id,),
    ).fetchone()
    if row is None:
        return None
    candidate = json.loads(row["candidate_json"])
    expected_hash = _fingerprint({
        key: value for key, value in candidate.items() if key != "candidate_hash"
    })
    if expected_hash != candidate["candidate_hash"] or row["candidate_hash"] != expected_hash:
        raise ValueError("Frozen candidate fingerprint mismatch.")
    return candidate


def candidate_status(project: Path, candidate_id: str) -> dict[str, Any]:
    with _connection(project) as connection:
        candidate = _load_candidate(connection, candidate_id)
    if candidate is None:
        raise ValueError("Unknown frozen candidate.")
    return candidate


def research_candidate_status(project: Path, round_id: str) -> dict[str, Any] | None:
    with _connection(project) as connection:
        return _load_candidate(connection, _research_candidate_id(round_id))


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
