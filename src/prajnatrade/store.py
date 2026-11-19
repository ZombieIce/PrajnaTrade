from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

MAX_CANDIDATES_PER_CAMPAIGN = 3


_CAMPAIGN_JSON_COLUMNS = {"baseline_json", "provider_config_json", "split_json"}
_EXPERIMENT_JSON_COLUMNS = {
    "strategy_json",
    "parameters_json",
    "metrics_json",
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
                    status TEXT NOT NULL,
                    stop_reason TEXT,
                    initial_cash REAL NOT NULL,
                    transaction_cost_bps REAL NOT NULL,
                    slippage_bps REAL NOT NULL,
                    token_budget INTEGER NOT NULL,
                    cost_budget_usd REAL NOT NULL,
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
            experiment_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(experiments)").fetchall()
            }
            if "data_hash" not in experiment_columns:
                connection.execute(
                    "ALTER TABLE experiments ADD COLUMN data_hash TEXT NOT NULL DEFAULT ''"
                )
            candidate_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(candidates)").fetchall()
            }
            for column, declaration in (
                ("candidate_hash", "TEXT NOT NULL DEFAULT ''"),
                ("data_hash", "TEXT NOT NULL DEFAULT ''"),
                ("split_json", "TEXT NOT NULL DEFAULT '{}'"),
            ):
                if column not in candidate_columns:
                    connection.execute(
                        f"ALTER TABLE candidates ADD COLUMN {column} {declaration}"
                    )
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
                 strategy_hash, candidate_hash, data_hash, split_json, frozen_at, conclusion)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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


def _decode_row(row: sqlite3.Row, json_columns: set[str]) -> dict[str, Any]:
    result = dict(row)
    for column in json_columns:
        if column in result and result[column] is not None:
            value = json.loads(result[column])
            if column.endswith("_json"):
                result[column.removesuffix("_json")] = value
                del result[column]
            else:
                result[column] = value
    return result
