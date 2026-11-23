from __future__ import annotations

import json
import math
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_CANDIDATES_PER_CAMPAIGN = 3


_CAMPAIGN_JSON_COLUMNS = {"baseline_json", "provider_config_json", "split_json"}
_EXPERIMENT_JSON_COLUMNS = {
    "strategy_json",
    "parameters_json",
    "metrics_json",
    "execution_evidence_json",
    "provider_usage_json",
}


class SQLiteStore:
    """Durable registry for campaigns, experiments, candidates, and evaluations."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS campaigns (
                    campaign_id TEXT PRIMARY KEY,
                    objective TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    data_path TEXT NOT NULL,
                    data_hash TEXT NOT NULL,
                    data_kind TEXT NOT NULL,
                    price_basis TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    provider_model TEXT NOT NULL,
                    provider_config_json TEXT NOT NULL,
                    seed INTEGER NOT NULL,
                    experiment_budget INTEGER NOT NULL,
                    max_consecutive_failures INTEGER NOT NULL,
                    max_seconds REAL NOT NULL,
                    elapsed_seconds REAL NOT NULL DEFAULT 0,
                    active_checkpoint_at TEXT,
                    status TEXT NOT NULL,
                    stop_reason TEXT,
                    initial_cash REAL NOT NULL,
                    transaction_cost_bps REAL NOT NULL,
                    slippage_bps REAL NOT NULL,
                    token_budget INTEGER NOT NULL,
                    cost_budget_usd REAL NOT NULL,
                    max_drawdown_limit REAL,
                    baseline_json TEXT NOT NULL,
                    split_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS experiments (
                    experiment_id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
                    data_hash TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    hypothesis TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    strategy_json TEXT NOT NULL,
                    strategy_hash TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    metrics_json TEXT,
                    status TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reflection TEXT NOT NULL,
                    error TEXT,
                    execution_evidence_json TEXT NOT NULL DEFAULT '[]',
                    provider_usage_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(campaign_id, ordinal)
                );
                CREATE TABLE IF NOT EXISTS candidates (
                    candidate_id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
                    experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
                    name TEXT NOT NULL,
                    strategy_json TEXT NOT NULL,
                    strategy_hash TEXT NOT NULL,
                    candidate_hash TEXT NOT NULL,
                    data_hash TEXT NOT NULL,
                    split_json TEXT NOT NULL,
                    robustness_plan_json TEXT,
                    frozen_at TEXT NOT NULL,
                    conclusion TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evaluations (
                    evaluation_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
                    kind TEXT NOT NULL CHECK(kind IN ('validation', 'holdout')),
                    data_hash TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS holdout_receipts (
                    campaign_id TEXT PRIMARY KEY REFERENCES campaigns(campaign_id),
                    candidate_id TEXT NOT NULL UNIQUE REFERENCES candidates(candidate_id),
                    data_hash TEXT NOT NULL,
                    confirmation TEXT NOT NULL,
                    unlocked_at TEXT NOT NULL,
                    evaluation_id TEXT UNIQUE REFERENCES evaluations(evaluation_id),
                    status TEXT NOT NULL,
                    error TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS holdout_data_hash_once
                    ON holdout_receipts(data_hash);
                CREATE TABLE IF NOT EXISTS provider_requests (
                    request_id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
                    stage TEXT NOT NULL,
                    experiment_ordinal INTEGER,
                    status TEXT NOT NULL,
                    usage_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            campaign_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(campaigns)").fetchall()
            }
            if "split_json" not in campaign_columns:
                connection.execute(
                    "ALTER TABLE campaigns ADD COLUMN split_json TEXT NOT NULL DEFAULT '{}'"
                )
            if "objective" not in campaign_columns:
                connection.execute(
                    "ALTER TABLE campaigns ADD COLUMN objective TEXT NOT NULL DEFAULT 'Reproducibly investigate ETF strategy hypotheses against an equal-weight benchmark.'"
                )
            if "max_drawdown_limit" not in campaign_columns:
                connection.execute("ALTER TABLE campaigns ADD COLUMN max_drawdown_limit REAL")
            if "active_checkpoint_at" not in campaign_columns:
                connection.execute("ALTER TABLE campaigns ADD COLUMN active_checkpoint_at TEXT")
            experiment_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(experiments)").fetchall()
            }
            if "data_hash" not in experiment_columns:
                connection.execute(
                    "ALTER TABLE experiments ADD COLUMN data_hash TEXT NOT NULL DEFAULT ''"
                )
            if "execution_evidence_json" not in experiment_columns:
                connection.execute(
                    "ALTER TABLE experiments ADD COLUMN execution_evidence_json TEXT NOT NULL DEFAULT '[]'"
                )
            candidate_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(candidates)").fetchall()
            }
            for column, declaration in (
                ("candidate_hash", "TEXT NOT NULL DEFAULT ''"),
                ("data_hash", "TEXT NOT NULL DEFAULT ''"),
                ("split_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("robustness_plan_json", "TEXT"),
            ):
                if column not in candidate_columns:
                    connection.execute(
                        f"ALTER TABLE candidates ADD COLUMN {column} {declaration}"
                    )
            request_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(provider_requests)")
            }
            if "experiment_ordinal" not in request_columns:
                connection.execute("ALTER TABLE provider_requests ADD COLUMN experiment_ordinal INTEGER")
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def create_campaign(self, campaign: dict[str, Any]) -> None:
        columns = tuple(campaign)
        values = tuple(
            _json_dump(campaign[column]) if column in _CAMPAIGN_JSON_COLUMNS else campaign[column]
            for column in columns
        )
        placeholders = ", ".join("?" for _ in columns)
        with closing(self._connect()) as connection:
            connection.execute(
                f"INSERT INTO campaigns ({', '.join(columns)}) VALUES ({placeholders})",
                values,
            )
            connection.commit()

    def get_campaign(self, campaign_id: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM campaigns WHERE campaign_id = ?", (campaign_id,)
            ).fetchone()
        if row is None:
            raise ValueError(f"Unknown campaign: {campaign_id}")
        return _decode_row(row, _CAMPAIGN_JSON_COLUMNS)

    def update_campaign(self, campaign_id: str, **updates: Any) -> None:
        allowed = {
            "updated_at",
            "elapsed_seconds",
            "active_checkpoint_at",
            "status",
            "stop_reason",
        }
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError(f"Unsupported campaign fields: {', '.join(sorted(unknown))}.")
        if not updates:
            return
        assignments = ", ".join(f"{column} = ?" for column in updates)
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                f"UPDATE campaigns SET {assignments} WHERE campaign_id = ?",
                (*updates.values(), campaign_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Unknown campaign: {campaign_id}")
            connection.commit()

    def insert_experiment(self, experiment: dict[str, Any]) -> None:
        columns = tuple(experiment)
        values = tuple(
            _json_dump(experiment[column]) if column in _EXPERIMENT_JSON_COLUMNS else experiment[column]
            for column in columns
        )
        placeholders = ", ".join("?" for _ in columns)
        with closing(self._connect()) as connection:
            connection.execute(
                f"INSERT INTO experiments ({', '.join(columns)}) VALUES ({placeholders})",
                values,
            )
            connection.commit()

    def reserve_provider_request(
        self, campaign_id: str, stage: str, ordinal: int, usage: dict[str, Any]
    ) -> str:
        request_id = str(uuid.uuid4())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            outstanding = connection.execute(
                "SELECT 1 FROM provider_requests WHERE campaign_id = ? AND status = 'reserved'",
                (campaign_id,),
            ).fetchone()
            if outstanding is not None:
                raise ValueError("An unconfirmed provider request requires reconciliation.")
            connection.execute(
                """
                INSERT INTO provider_requests
                (request_id, campaign_id, stage, experiment_ordinal, status, usage_json, created_at)
                VALUES (?, ?, ?, ?, 'reserved', ?, ?)
                """,
                (
                    request_id, campaign_id, stage, ordinal, _json_dump(usage),
                    datetime.now(UTC).isoformat(),
                ),
            )
            connection.commit()
        return request_id

    def settle_provider_request(self, request_id: str, usage: dict[str, Any]) -> None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT usage_json FROM provider_requests WHERE request_id = ? AND status = 'reserved'",
                (request_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"No reserved provider request: {request_id}")
            settled = {**usage, "reservation": json.loads(row["usage_json"])}
            cursor = connection.execute(
                """
                UPDATE provider_requests SET status = 'settled', usage_json = ?
                WHERE request_id = ? AND status = 'reserved'
                """,
                (_json_dump(settled), request_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"No reserved provider request: {request_id}")
            connection.commit()

    def reconcile_provider_request(
        self, campaign_id: str, request_id: str, *, prompt_tokens: int,
        completion_tokens: int, actual_cost_usd: float, evidence: str,
    ) -> dict[str, Any]:
        if (
            prompt_tokens < 0 or completion_tokens < 0
            or not math.isfinite(actual_cost_usd) or actual_cost_usd < 0
            or not evidence.strip()
        ):
            raise ValueError("Reconciliation requires nonnegative usage, finite cost and evidence.")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM provider_requests WHERE request_id = ? AND campaign_id = ?",
                (request_id, campaign_id),
            ).fetchone()
            if row is None or row["status"] not in {"reserved", "settled"}:
                raise ValueError("Request is unknown or has already been reconciled.")
            previous = json.loads(row["usage_json"])
            usage = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "actual_cost_usd": actual_cost_usd,
                "requests": 1,
                "usage_estimated": False,
                "reconciliation_evidence": evidence.strip(),
                "reconciled_at": datetime.now(UTC).isoformat(),
                "previous_usage": previous,
            }
            connection.execute(
                "UPDATE provider_requests SET status = 'reconciled', usage_json = ? WHERE request_id = ?",
                (_json_dump(usage), request_id),
            )
            if row["experiment_ordinal"] is not None:
                rows = connection.execute(
                    "SELECT usage_json FROM provider_requests WHERE campaign_id = ? AND experiment_ordinal = ?",
                    (campaign_id, row["experiment_ordinal"]),
                ).fetchall()
                combined = _combine_request_usage([json.loads(item["usage_json"]) for item in rows])
                connection.execute(
                    "UPDATE experiments SET provider_usage_json = ? WHERE campaign_id = ? AND ordinal = ?",
                    (_json_dump(combined), campaign_id, row["experiment_ordinal"]),
                )
            connection.commit()
        return {"request_id": request_id, "status": "reconciled", "usage": usage}

    def list_provider_requests(self, campaign_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM provider_requests WHERE campaign_id = ? ORDER BY rowid",
                (campaign_id,),
            ).fetchall()
        requests = []
        for row in rows:
            request = dict(row)
            request["usage"] = json.loads(request.pop("usage_json"))
            requests.append(request)
        return requests

    def experiment_request_usage(self, campaign_id: str, ordinal: int) -> dict[str, Any]:
        requests = self.list_provider_requests(campaign_id)
        return _combine_request_usage([
            request["usage"] for request in requests if request["experiment_ordinal"] == ordinal
        ])

    def update_experiment(self, experiment_id: str, **updates: Any) -> None:
        allowed = {
            "strategy_json", "strategy_hash", "parameters_json", "metrics_json",
            "execution_evidence_json", "status", "decision", "reflection", "error",
            "provider_usage_json",
        }
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError(f"Unsupported experiment fields: {', '.join(sorted(unknown))}.")
        if not updates:
            return
        values = [
            _json_dump(value) if column in _EXPERIMENT_JSON_COLUMNS and value is not None
            else value
            for column, value in updates.items()
        ]
        assignments = ", ".join(f"{column} = ?" for column in updates)
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                f"UPDATE experiments SET {assignments} WHERE experiment_id = ?",
                (*values, experiment_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Unknown experiment: {experiment_id}")
            connection.commit()

    def list_experiments(self, campaign_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM experiments WHERE campaign_id = ? ORDER BY ordinal",
                (campaign_id,),
            ).fetchall()
        return [_decode_row(row, _EXPERIMENT_JSON_COLUMNS) for row in rows]

    def get_experiment(self, campaign_id: str, experiment_id: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM experiments
                WHERE campaign_id = ? AND experiment_id = ?
                """,
                (campaign_id, experiment_id),
            ).fetchone()
        if row is None:
            raise ValueError(f"Unknown experiment {experiment_id} in campaign {campaign_id}.")
        return _decode_row(row, _EXPERIMENT_JSON_COLUMNS)

    def create_candidate(self, candidate: dict[str, Any]) -> None:
        values = (
            candidate["candidate_id"],
            candidate["campaign_id"],
            candidate["experiment_id"],
            candidate["name"],
            _json_dump(candidate["strategy"]),
            candidate["strategy_hash"],
            candidate["candidate_hash"],
            candidate["data_hash"],
            _json_dump(candidate["split"]),
            _json_dump(candidate["robustness_plan"]),
            candidate["frozen_at"],
            candidate["conclusion"],
        )
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            count = connection.execute(
                "SELECT COUNT(*) FROM candidates WHERE campaign_id = ?",
                (candidate["campaign_id"],),
            ).fetchone()[0]
            if count >= MAX_CANDIDATES_PER_CAMPAIGN:
                raise ValueError(
                    f"Candidate limit of {MAX_CANDIDATES_PER_CAMPAIGN} reached for this campaign; "
                    "validate the frozen candidates or start a new campaign."
                )
            connection.execute(
                """
                INSERT INTO candidates
                (candidate_id, campaign_id, experiment_id, name, strategy_json,
                 strategy_hash, candidate_hash, data_hash, split_json, robustness_plan_json,
                 frozen_at, conclusion)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            connection.commit()

    def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM candidates WHERE candidate_id = ?", (candidate_id,)
            ).fetchone()
        if row is None:
            raise ValueError(f"Unknown candidate: {candidate_id}")
        candidate = dict(row)
        candidate["strategy"] = json.loads(candidate.pop("strategy_json"))
        candidate["split"] = json.loads(candidate.pop("split_json"))
        raw_plan = candidate.pop("robustness_plan_json")
        candidate["robustness_plan"] = json.loads(raw_plan) if raw_plan is not None else None
        return candidate

    def list_candidates(self, campaign_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM candidates WHERE campaign_id = ? ORDER BY frozen_at",
                (campaign_id,),
            ).fetchall()
        candidates = []
        for row in rows:
            candidate = dict(row)
            candidate["strategy"] = json.loads(candidate.pop("strategy_json"))
            candidate["split"] = json.loads(candidate.pop("split_json"))
            raw_plan = candidate.pop("robustness_plan_json")
            candidate["robustness_plan"] = json.loads(raw_plan) if raw_plan is not None else None
            candidates.append(candidate)
        return candidates

    def insert_evaluation(self, evaluation: dict[str, Any]) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO evaluations
                (evaluation_id, candidate_id, kind, data_hash, start_date, end_date,
                 result_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation["evaluation_id"],
                    evaluation["candidate_id"],
                    evaluation["kind"],
                    evaluation["data_hash"],
                    evaluation["start_date"],
                    evaluation["end_date"],
                    _json_dump(evaluation["result"]),
                    evaluation["created_at"],
                ),
            )
            connection.commit()

    def list_evaluations(self, candidate_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM evaluations WHERE candidate_id = ? ORDER BY created_at",
                (candidate_id,),
            ).fetchall()
        evaluations = []
        for row in rows:
            evaluation = dict(row)
            evaluation["result"] = json.loads(evaluation.pop("result_json"))
            evaluations.append(evaluation)
        return evaluations

    def create_holdout_receipt(self, receipt: dict[str, Any]) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO holdout_receipts
                (campaign_id, candidate_id, data_hash, confirmation, unlocked_at, status)
                VALUES (?, ?, ?, ?, ?, 'unlocked_pending')
                """,
                (
                    receipt["campaign_id"],
                    receipt["candidate_id"],
                    receipt["data_hash"],
                    receipt["confirmation"],
                    receipt["unlocked_at"],
                ),
            )
            connection.commit()

    def finish_holdout_receipt(
        self, campaign_id: str, *, evaluation_id: str | None, error: str | None
    ) -> None:
        status = "evaluated" if evaluation_id is not None else "failed"
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE holdout_receipts
                SET evaluation_id = ?, status = ?, error = ?
                WHERE campaign_id = ? AND status = 'unlocked_pending'
                """,
                (evaluation_id, status, error, campaign_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"No pending holdout receipt for campaign {campaign_id}.")
            connection.commit()

    def get_holdout_receipt(self, campaign_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM holdout_receipts WHERE campaign_id = ?", (campaign_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def get_holdout_receipt_by_data_hash(self, data_hash: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM holdout_receipts WHERE data_hash = ?", (data_hash,)
            ).fetchone()
        return dict(row) if row is not None else None


def _json_dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _combine_request_usage(usages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "prompt_tokens": sum(int(item.get("prompt_tokens", 0)) for item in usages),
        "completion_tokens": sum(int(item.get("completion_tokens", 0)) for item in usages),
        "total_tokens": sum(int(item.get("total_tokens", 0)) for item in usages),
        "estimated_cost_usd": sum(float(item.get("estimated_cost_usd", 0.0)) for item in usages),
        "actual_cost_usd": sum(float(item.get("actual_cost_usd", 0.0)) for item in usages),
        "requests": len(usages),
        "usage_estimated": any(bool(item.get("usage_estimated")) for item in usages),
    }


def _decode_row(row: sqlite3.Row, json_columns: set[str]) -> dict[str, Any]:
    result = dict(row)
    for column in json_columns:
        if column in result:
            value = json.loads(result[column]) if result[column] is not None else None
            if column.endswith("_json"):
                result[column.removesuffix("_json")] = value
                del result[column]
            else:
                result[column] = value
    return result
