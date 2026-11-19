from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from prajnatrade.backtest import equal_weight_buy_and_hold, run_backtest
from prajnatrade.data import ValidatedData, fingerprint_bars, load_bars
from prajnatrade.store import SQLiteStore
from prajnatrade.strategies import (
    make_strategy,
    normalize_strategy_spec,
    require_execution_permission,
    strategy_fingerprint,
)

HOLDOUT_CONFIRMATION = "I CONFIRM THE CANDIDATE AND CONCLUSIONS ARE FROZEN"


def freeze_candidate(
    database: str | Path,
    campaign_id: str,
    experiment_id: str,
    name: str,
    conclusion: str,
) -> dict[str, Any]:
    if not name.strip() or not conclusion.strip():
        raise ValueError("Candidate name and frozen conclusion must not be empty.")
    store = SQLiteStore(database)
    campaign = store.get_campaign(campaign_id)
    if campaign["status"] in {"running", "paused"}:
        raise ValueError("Pause or finish discovery before freezing a candidate.")
    experiment = store.get_experiment(campaign_id, experiment_id)
    if experiment["status"] != "succeeded":
        raise ValueError("Only a successful experiment can be frozen as a candidate.")
    strategy = normalize_strategy_spec(experiment["strategy"])
    fingerprint = strategy_fingerprint(strategy)
    if fingerprint != experiment["strategy_hash"]:
        raise ValueError("Experiment strategy fingerprint does not match its stored strategy.")
    candidate_hash = _candidate_fingerprint(
        fingerprint,
        campaign["data_hash"],
        campaign["split"],
        name.strip(),
        conclusion.strip(),
        experiment_id,
    )
    candidate = {
        "candidate_id": str(uuid.uuid4()),
        "campaign_id": campaign_id,
        "experiment_id": experiment_id,
        "name": name.strip(),
        "strategy": strategy,
        "strategy_hash": fingerprint,
        "candidate_hash": candidate_hash,
        "data_hash": campaign["data_hash"],
        "split": campaign["split"],
        "frozen_at": _now(),
        "conclusion": conclusion.strip(),
    }
    store.create_candidate(candidate)
    return {
        "candidate_id": candidate["candidate_id"],
        "campaign_id": campaign_id,
        "experiment_id": experiment_id,
        "name": candidate["name"],
        "strategy_hash": fingerprint,
        "candidate_hash": candidate_hash,
        "frozen_at": candidate["frozen_at"],
    }


def validate_candidate(
    database: str | Path,
    candidate_id: str,
    data_path: str | Path,
    data_kind: str,
    price_basis: str,
    *,
    trusted_local_python: bool = False,
) -> dict[str, Any]:
    store = SQLiteStore(database)
    candidate = store.get_candidate(candidate_id)
    campaign = store.get_campaign(candidate["campaign_id"])
    _verify_candidate_freeze(candidate, campaign)
    split = campaign["split"]
    start_date = split["validation_start"]
    end_date = split["validation_end"]
    dataset = _load_campaign_data(campaign, data_path, data_kind, price_basis)
    _require_interval(dataset, start_date, end_date)
    interval_hash = fingerprint_bars(
        _interval_bars(dataset, start_date, end_date)
    )

    candidate_result = run_backtest(
        dataset,
        make_strategy(candidate["strategy"], trusted_local_python=trusted_local_python),
        initial_cash=campaign["initial_cash"],
        transaction_cost_bps=campaign["transaction_cost_bps"],
        slippage_bps=campaign["slippage_bps"],
        start_date=start_date,
        end_date=end_date,
    )
    baseline_result = run_backtest(
        dataset,
        equal_weight_buy_and_hold,
        initial_cash=campaign["initial_cash"],
        transaction_cost_bps=campaign["transaction_cost_bps"],
        slippage_bps=campaign["slippage_bps"],
        start_date=start_date,
        end_date=end_date,
    )
    sensitivity_bps = (
        campaign["transaction_cost_bps"] * 2
        if campaign["transaction_cost_bps"] > 0
        else 5.0
    )
    cost_sensitive_result = run_backtest(
        dataset,
        make_strategy(candidate["strategy"], trusted_local_python=trusted_local_python),
        initial_cash=campaign["initial_cash"],
        transaction_cost_bps=sensitivity_bps,
        slippage_bps=campaign["slippage_bps"],
        start_date=start_date,
        end_date=end_date,
    )
    robustness = _parameter_robustness(candidate, campaign, dataset, start_date, end_date)
    candidate_output = candidate_result.to_dict(data_kind)
    baseline_output = baseline_result.to_dict(data_kind)
    candidate_output["source_data_hash"] = dataset.data_hash
    baseline_output["source_data_hash"] = dataset.data_hash
    candidate_output["data_hash"] = interval_hash
    baseline_output["data_hash"] = interval_hash
    result = {
        "candidate_name": candidate["name"],
        "candidate_hash": candidate["candidate_hash"],
        "data_kind": data_kind,
        "data_hash": interval_hash,
        "source_data_hash": dataset.data_hash,
        "start_date": start_date,
        "end_date": end_date,
        "candidate": candidate_output,
        "baseline": baseline_output,
        "benchmark_excess_return": (
            candidate_result.metrics["total_return"]
            - baseline_result.metrics["total_return"]
        ),
        "cost_sensitivity": {
            "transaction_cost_bps": sensitivity_bps,
            "total_return": cost_sensitive_result.metrics["total_return"],
            "metrics": cost_sensitive_result.metrics,
        },
        "parameter_robustness": robustness,
        "market_period_breakdown": candidate_result.metrics["yearly_returns"],
    }
    evaluation = {
        "evaluation_id": str(uuid.uuid4()),
        "candidate_id": candidate_id,
        "kind": "validation",
        "data_hash": interval_hash,
        "start_date": start_date,
        "end_date": end_date,
        "result": result,
        "created_at": _now(),
    }
    store.insert_evaluation(evaluation)
    return {
        "evaluation_id": evaluation["evaluation_id"],
        "candidate_id": candidate_id,
        "kind": "validation",
        "data_hash": dataset.data_hash,
        "start_date": start_date,
        "end_date": end_date,
        "result": result,
    }


def run_holdout(
    database: str | Path,
    candidate_id: str,
    data_path: str | Path,
    data_kind: str,
    price_basis: str,
    confirmation: str,
    *,
    trusted_local_python: bool = False,
) -> dict[str, Any]:
    if confirmation != HOLDOUT_CONFIRMATION:
        raise ValueError(
            "Holdout confirmation must exactly match: "
            f"{HOLDOUT_CONFIRMATION!r}."
        )
    store = SQLiteStore(database)
    candidate = store.get_candidate(candidate_id)
    campaign = store.get_campaign(candidate["campaign_id"])
    _verify_candidate_freeze(candidate, campaign)
    require_execution_permission(
        candidate["strategy"], trusted_local_python=trusted_local_python
    )
    if store.get_holdout_receipt(candidate["campaign_id"]) is not None:
        raise ValueError(
            f"Final holdout for campaign {candidate['campaign_id']} was already consumed."
        )
    validations = store.list_evaluations(candidate_id)
    if not any(item["kind"] == "validation" for item in validations):
        raise ValueError("Run independent candidate validation before unlocking the holdout.")
    split = campaign["split"]
    start_date = split["holdout_start"]
    end_date = split["holdout_end"]

    dataset = _load_campaign_data(campaign, data_path, data_kind, price_basis)
    _require_interval(dataset, start_date, end_date)
    interval_hash = fingerprint_bars(_interval_bars(dataset, start_date, end_date))
    if store.get_holdout_receipt_by_data_hash(interval_hash) is not None:
        raise ValueError(
            f"Final holdout dataset fingerprint {interval_hash} was already consumed."
        )
    unlocked_at = _now()
    try:
        store.create_holdout_receipt(
            {
                "campaign_id": candidate["campaign_id"],
                "candidate_id": candidate_id,
                "data_hash": interval_hash,
                "confirmation": confirmation,
                "unlocked_at": unlocked_at,
            }
        )
    except sqlite3.IntegrityError as error:
        if (
            store.get_holdout_receipt(candidate["campaign_id"]) is not None
            or store.get_holdout_receipt_by_data_hash(interval_hash) is not None
        ):
            raise ValueError("This final holdout was already consumed.") from error
        raise
    try:
        candidate_result = run_backtest(
            dataset,
            make_strategy(candidate["strategy"], trusted_local_python=trusted_local_python),
            initial_cash=campaign["initial_cash"],
            transaction_cost_bps=campaign["transaction_cost_bps"],
            slippage_bps=campaign["slippage_bps"],
            start_date=start_date,
            end_date=end_date,
        )
        baseline_result = run_backtest(
            dataset,
            equal_weight_buy_and_hold,
            initial_cash=campaign["initial_cash"],
            transaction_cost_bps=campaign["transaction_cost_bps"],
            slippage_bps=campaign["slippage_bps"],
            start_date=start_date,
            end_date=end_date,
        )
    except ValueError as error:
        store.finish_holdout_receipt(
            candidate["campaign_id"], evaluation_id=None, error=str(error)
        )
        raise

    candidate_output = candidate_result.to_dict(data_kind)
    baseline_output = baseline_result.to_dict(data_kind)
    candidate_output["source_data_hash"] = dataset.data_hash
    baseline_output["source_data_hash"] = dataset.data_hash
    candidate_output["data_hash"] = interval_hash
    baseline_output["data_hash"] = interval_hash
    result = {
        "candidate_name": candidate["name"],
        "candidate_hash": candidate["candidate_hash"],
        "data_kind": data_kind,
        "data_hash": interval_hash,
        "source_data_hash": dataset.data_hash,
        "start_date": start_date,
        "end_date": end_date,
        "candidate": candidate_output,
        "baseline": baseline_output,
        "benchmark_excess_return": (
            candidate_result.metrics["total_return"]
            - baseline_result.metrics["total_return"]
        ),
        "unlocked_at": unlocked_at,
        "confirmation": confirmation,
    }
    evaluation = {
        "evaluation_id": str(uuid.uuid4()),
        "candidate_id": candidate_id,
        "kind": "holdout",
        "data_hash": interval_hash,
        "start_date": start_date,
        "end_date": end_date,
        "result": result,
        "created_at": _now(),
    }
    store.insert_evaluation(evaluation)
    store.finish_holdout_receipt(
        candidate["campaign_id"], evaluation_id=evaluation["evaluation_id"], error=None
    )
    return {
        "evaluation_id": evaluation["evaluation_id"],
        "candidate_id": candidate_id,
        "kind": "holdout",
        "data_hash": dataset.data_hash,
        "start_date": start_date,
        "end_date": end_date,
        "result": result,
    }


def _parameter_robustness(
    candidate: dict[str, Any],
    campaign: dict[str, Any],
    dataset: ValidatedData,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    strategy = candidate["strategy"]
    params = strategy["parameters"]
    if strategy["family"] == "python":
        return {
            "applicable": False,
            "variants": [],
            "reason": "Generated Python parameters have no declared robustness schema.",
        }
    if "lookback" not in params:
        return {"applicable": False, "variants": []}
    lookback = int(params["lookback"])
    variants = []
    for nearby in sorted({max(2, lookback - 1), min(252, lookback + 1)}):
        varied = {
            "family": strategy["family"],
            "parameters": {**params, "lookback": nearby},
        }
        result = run_backtest(
            dataset,
            make_strategy(varied),
            initial_cash=campaign["initial_cash"],
            transaction_cost_bps=campaign["transaction_cost_bps"],
            slippage_bps=campaign["slippage_bps"],
            start_date=start_date,
            end_date=end_date,
        )
        variants.append(
            {"lookback": nearby, "total_return": result.metrics["total_return"]}
        )
    returns = [item["total_return"] for item in variants]
    return {
        "applicable": True,
        "variants": variants,
        "total_return_spread": max(returns) - min(returns) if returns else 0.0,
    }


def _require_interval(dataset: ValidatedData, start_date: str, end_date: str) -> None:
    dates = set(dataset.bars["date"])
    if pd.Timestamp(start_date) not in dates or pd.Timestamp(end_date) not in dates:
        raise ValueError(
            f"Dataset must contain both interval boundaries {start_date} and {end_date}."
        )
    if pd.Timestamp(end_date) < pd.Timestamp(start_date):
        raise ValueError("Evaluation interval end precedes its start.")


def _load_campaign_data(
    campaign: dict[str, Any], data_path: str | Path, data_kind: str, price_basis: str
) -> ValidatedData:
    if data_kind != campaign["data_kind"]:
        raise ValueError("Evaluation data kind must match the frozen campaign.")
    if price_basis != campaign["price_basis"]:
        raise ValueError("Evaluation price basis must match the frozen campaign.")
    dataset = load_bars(data_path, price_basis)
    if dataset.data_hash != campaign["data_hash"]:
        raise ValueError("Evaluation data fingerprint must match the frozen campaign.")
    return dataset


def _interval_bars(
    dataset: ValidatedData, start_date: str, end_date: str
) -> pd.DataFrame:
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    return dataset.bars.loc[
        (dataset.bars["date"] >= start) & (dataset.bars["date"] <= end)
    ]


def _candidate_fingerprint(
    strategy_hash: str,
    data_hash: str,
    split: dict[str, str],
    name: str,
    conclusion: str,
    experiment_id: str,
) -> str:
    payload = json.dumps(
        {
            "strategy_hash": strategy_hash,
            "data_hash": data_hash,
            "split": split,
            "name": name,
            "conclusion": conclusion,
            "experiment_id": experiment_id,
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _verify_candidate_freeze(
    candidate: dict[str, Any], campaign: dict[str, Any]
) -> None:
    if strategy_fingerprint(candidate["strategy"]) != candidate["strategy_hash"]:
        raise ValueError("Frozen candidate strategy does not match its stored fingerprint.")
    if candidate["data_hash"] != campaign["data_hash"]:
        raise ValueError("Frozen candidate data fingerprint differs from its campaign.")
    if candidate["split"] != campaign["split"]:
        raise ValueError("Frozen candidate split differs from its campaign.")
    expected_hash = _candidate_fingerprint(
        candidate["strategy_hash"],
        candidate["data_hash"],
        candidate["split"],
        candidate["name"],
        candidate["conclusion"],
        candidate["experiment_id"],
    )
    if expected_hash != candidate["candidate_hash"]:
        raise ValueError("Frozen candidate fingerprint does not match its stored contents.")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
