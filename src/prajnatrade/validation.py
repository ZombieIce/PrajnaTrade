from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from prajnatrade.backtest import equal_weight_buy_and_hold, run_backtest
from prajnatrade.data import ValidatedData, fingerprint_bars, load_bars
from prajnatrade.isolation import probe_isolation
from prajnatrade.store import SQLiteStore
from prajnatrade.strategies import (
    make_strategy,
    normalize_strategy_spec,
    parameter_constraints_hold,
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
    robustness_plan = _make_robustness_plan(strategy, campaign["max_drawdown_limit"])
    candidate_hash = _candidate_fingerprint(
        fingerprint,
        campaign["data_hash"],
        campaign["split"],
        name.strip(),
        conclusion.strip(),
        experiment_id,
        robustness_plan,
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
        "robustness_plan": robustness_plan,
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
        "robustness_plan": robustness_plan,
    }


def validate_candidate(
    database: str | Path,
    candidate_id: str,
    data_path: str | Path,
    data_kind: str,
    price_basis: str,
    *,
    isolated_python: bool = False,
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
        make_strategy(candidate["strategy"], isolated_python=isolated_python),
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
        make_strategy(candidate["strategy"], isolated_python=isolated_python),
        initial_cash=campaign["initial_cash"],
        transaction_cost_bps=sensitivity_bps,
        slippage_bps=campaign["slippage_bps"],
        start_date=start_date,
        end_date=end_date,
    )
    robustness = _parameter_robustness(
        candidate, campaign, dataset, start_date, end_date, candidate_result.metrics,
        isolated_python=isolated_python,
    )
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
        "data_hash": interval_hash,
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
    isolated_python: bool = False,
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
        candidate["strategy"], isolated_python=isolated_python
    )
    if candidate["strategy"]["family"] == "python":
        probe_isolation()
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

    development = _load_campaign_data(
        campaign, campaign["data_path"], data_kind, price_basis
    )
    holdout = load_bars(data_path, price_basis)
    _require_interval(holdout, start_date, end_date)
    if len(_interval_bars(holdout, start_date, end_date)) != len(holdout.bars):
        raise ValueError("Holdout file must contain only the frozen holdout interval.")
    interval_hash = fingerprint_bars(holdout.bars)
    combined_bars = pd.concat([development.bars, holdout.bars], ignore_index=True)
    dataset = ValidatedData(
        bars=combined_bars,
        data_hash=fingerprint_bars(combined_bars),
        report=holdout.report,
    )
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
            make_strategy(candidate["strategy"], isolated_python=isolated_python),
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
    candidate_output["source_data_hash"] = holdout.data_hash
    baseline_output["source_data_hash"] = holdout.data_hash
    candidate_output["data_hash"] = interval_hash
    baseline_output["data_hash"] = interval_hash
    result = {
        "candidate_name": candidate["name"],
        "candidate_hash": candidate["candidate_hash"],
        "data_kind": data_kind,
        "data_hash": interval_hash,
        "source_data_hash": holdout.data_hash,
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
        "data_hash": interval_hash,
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
    original_metrics: dict[str, Any],
    *,
    isolated_python: bool,
) -> dict[str, Any]:
    strategy = candidate["strategy"]
    plan = candidate.get("robustness_plan")
    risk_limit = plan.get("hard_risk_limit") if plan else None
    original_risk_breach = (
        abs(original_metrics["max_drawdown"]) > risk_limit["max_drawdown"]
        if risk_limit is not None else None
    )
    if plan is None or plan["status"] in {
        "missing_parameter_schema", "unsupported_non_numeric_parameters"
    }:
        return {
            "applicable": False,
            "status": "inconclusive",
            "variants": [],
            "reason": plan["status"] if plan else "missing_parameter_schema",
            "risk_limit_breach": original_risk_breach,
        }
    if plan["status"] == "not_applicable":
        return {
            "applicable": False, "status": "not_applicable", "variants": [], "plan": plan,
            "risk_limit_breach": original_risk_breach,
        }
    variants = []
    original_sharpe = original_metrics["sharpe"]
    original_drawdown = abs(original_metrics["max_drawdown"])
    thresholds = plan["thresholds"]
    for entry in plan["variants"]:
        variant = dict(entry)
        if entry["reason"] is not None:
            variant["status"] = "invalid_config"
        else:
            varied = {**strategy, "parameters": entry["parameters"]}
            try:
                result = run_backtest(
                    dataset,
                    make_strategy(varied, isolated_python=isolated_python),
                    initial_cash=campaign["initial_cash"],
                    transaction_cost_bps=campaign["transaction_cost_bps"],
                    slippage_bps=campaign["slippage_bps"],
                    start_date=start_date,
                    end_date=end_date,
                )
            except ValueError as error:
                variant.update(status="execution_failed", error=str(error))
            else:
                metrics = result.metrics
                variant["metrics"] = metrics
                variant["risk_limit_breach"] = (
                    abs(metrics["max_drawdown"]) > risk_limit["max_drawdown"]
                    if risk_limit is not None else None
                )
                if original_sharpe is None or metrics["sharpe"] is None:
                    variant["status"] = "not_evaluable"
                    variant["reason"] = "sharpe_unavailable"
                else:
                    sharpe_drop = original_sharpe - metrics["sharpe"]
                    drawdown_increase = abs(metrics["max_drawdown"]) - original_drawdown
                    degraded = (
                        sharpe_drop >= max(
                            thresholds["sharpe_absolute"],
                            thresholds["sharpe_relative"] * max(original_sharpe, 0),
                        )
                        or drawdown_increase >= max(
                            thresholds["drawdown_absolute"],
                            thresholds["drawdown_relative"] * original_drawdown,
                        )
                    )
                    variant.update(
                        status="evaluated", outcome="degraded" if degraded else "stable",
                        sharpe_drop=sharpe_drop, drawdown_increase=drawdown_increase,
                    )
        variants.append(variant)
    evaluated = [item for item in variants if item["status"] == "evaluated"]
    covered = {
        name for item in evaluated for name in item["changed_parameters"]
    }
    sufficient = (
        bool(variants)
        and len(evaluated) / len(variants) >= thresholds["coverage"]
        and set(plan["parameters"]) <= covered
    )
    stable = sum(item.get("outcome") == "stable" for item in evaluated)
    status = (
        "inconclusive" if not sufficient else
        "passed" if stable / len(evaluated) >= thresholds["stable_fraction"] else "failed"
    )
    return {
        "applicable": True,
        "status": status,
        "plan": plan,
        "variants": variants,
        "coverage": len(evaluated) / len(variants) if variants else 0.0,
        "stable_fraction": stable / len(evaluated) if evaluated else None,
        "risk_limit_breach": original_risk_breach,
    }


def make_robustness_plan(
    strategy: dict[str, Any], max_drawdown_limit: float | None,
) -> dict[str, Any]:
    """Public frozen parameter-neighbourhood planner (trusted seam, #28)."""
    return _make_robustness_plan(strategy, max_drawdown_limit)


def _make_robustness_plan(
    strategy: dict[str, Any], max_drawdown_limit: float | None,
) -> dict[str, Any]:
    params = strategy["parameters"]
    risk_limit = (
        {"max_drawdown": max_drawdown_limit} if max_drawdown_limit is not None else None
    )
    if strategy["family"] == "python":
        schema = strategy.get("parameter_schema")
        if schema is None:
            return {"status": "missing_parameter_schema", "hard_risk_limit": risk_limit}
    else:
        definitions = {}
        if "lookback" in params:
            definitions["lookback"] = {
                "type": "integer", "default": params["lookback"], "min": 2, "max": 252, "step": 1,
            }
        if "top_k" in params:
            definitions["top_k"] = {
                "type": "integer", "default": params["top_k"], "min": 1, "max": 5, "step": 1,
            }
        schema = {"parameters": definitions, "constraints": []}
    names = sorted(schema["parameters"])
    if not names:
        if params:
            return {
                "status": "unsupported_non_numeric_parameters", "schema": schema,
                "hard_risk_limit": risk_limit,
            }
        return {"status": "not_applicable", "schema": schema, "hard_risk_limit": risk_limit}
    plan = {
        "status": "planned",
        "schema": schema,
        "parameters": names,
        "variants": [],
        "excluded": [],
        "thresholds": {
            "sharpe_absolute": 0.30, "sharpe_relative": 0.25,
            "drawdown_absolute": 0.03, "drawdown_relative": 0.25,
            "coverage": 0.8, "stable_fraction": 0.8,
        },
        "hard_risk_limit": risk_limit,
    }
    singles = {}
    for name in names:
        definition = schema["parameters"][name]
        original = Decimal(str(params[name]))
        step = Decimal(str(definition["step"]))
        delta = max(
            step, (abs(original) * Decimal("0.1") / step).to_integral_value(rounding=ROUND_HALF_UP) * step
        )
        options = []
        for direction in (-1, 1):
            value = original + direction * delta
            replacement = int(value) if definition["type"] == "integer" else float(value)
            varied = {**params, name: replacement}
            reason = None
            if value < Decimal(str(definition["min"])) or value > Decimal(str(definition["max"])):
                reason = "outside_declared_range"
            elif not parameter_constraints_hold(varied, schema["constraints"]):
                reason = "joint_constraint"
            options.append({
                "parameter": name, "changed_parameters": [name],
                "parameters": varied, "reason": reason,
                "requested_delta": float(abs(original) * Decimal("0.1")),
                "applied_delta": float(delta),
            })
        singles[name] = options
    joints = []
    for joint in schema.get("joint_variants", []):
        names_changed = sorted(joint["parameters"])
        varied = {**params, **joint["parameters"]}
        reason = None
        for name in names_changed:
            definition = schema["parameters"][name]
            value = Decimal(str(varied[name]))
            minimum = Decimal(str(definition["min"]))
            maximum = Decimal(str(definition["max"]))
            step = Decimal(str(definition["step"]))
            if value < minimum or value > maximum or (value - minimum) % step:
                reason = "outside_declared_range_or_step"
                break
            if value == Decimal(str(params[name])):
                reason = "unchanged_parameter"
                break
        if reason is None and not parameter_constraints_hold(varied, schema["constraints"]):
            reason = "joint_constraint"
        entry = {
            "changed_parameters": names_changed, "parameters": varied,
            "reason": reason, "selection_reason": joint["reason"],
        }
        joints.append(entry)

    def select(entry: dict[str, Any]) -> None:
        counts = {
            name: sum(name in item["changed_parameters"] for item in plan["variants"])
            for name in entry["changed_parameters"]
        }
        if len(plan["variants"]) >= 10 or any(count >= 2 for count in counts.values()):
            plan["excluded"].append({**entry, "exclusion_reason": "plan_limit"})
        else:
            plan["variants"].append(entry)

    for name in names:
        legal = [entry for entry in singles[name] if entry["reason"] is None]
        if legal:
            select(legal[0])
    for entry in joints:
        if entry["reason"] is None:
            select(entry)
    for name in names:
        legal = [entry for entry in singles[name] if entry["reason"] is None]
        for entry in legal[1:]:
            select(entry)
    for name in names:
        invalid = [entry for entry in singles[name] if entry["reason"] is not None]
        covered = any(name in entry["changed_parameters"] for entry in plan["variants"])
        if covered:
            plan["excluded"].extend(invalid)
        else:
            for entry in invalid:
                select(entry)
    for entry in joints:
        if entry["reason"] is not None:
            select(entry)
    return plan


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
    robustness_plan: dict[str, Any] | None = None,
) -> str:
    contents = {
            "strategy_hash": strategy_hash,
            "data_hash": data_hash,
            "split": split,
            "name": name,
            "conclusion": conclusion,
            "experiment_id": experiment_id,
        }
    if robustness_plan is not None:
        contents["robustness_plan"] = robustness_plan
    payload = json.dumps(
        contents,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _verify_candidate_freeze(
    candidate: dict[str, Any], campaign: dict[str, Any]
) -> None:
    if campaign["split"].get("holdout_storage") != "external":
        raise ValueError(
            "This legacy campaign included holdout data during research; "
            "start a new campaign with separately stored holdout data."
        )
    if strategy_fingerprint(candidate["strategy"]) != candidate["strategy_hash"]:
        raise ValueError("Frozen candidate strategy does not match its stored fingerprint.")
    if candidate["data_hash"] != campaign["data_hash"]:
        raise ValueError("Frozen candidate data fingerprint differs from its campaign.")
    if candidate["split"] != campaign["split"]:
        raise ValueError("Frozen candidate split differs from its campaign.")
    if candidate["robustness_plan"] is not None:
        expected_limit = (
            {"max_drawdown": campaign["max_drawdown_limit"]}
            if campaign["max_drawdown_limit"] is not None else None
        )
        if candidate["robustness_plan"].get("hard_risk_limit") != expected_limit:
            raise ValueError("Frozen risk limit differs from its campaign.")
    expected_hash = _candidate_fingerprint(
        candidate["strategy_hash"],
        candidate["data_hash"],
        candidate["split"],
        candidate["name"],
        candidate["conclusion"],
        candidate["experiment_id"],
        candidate["robustness_plan"],
    )
    if expected_hash != candidate["candidate_hash"]:
        raise ValueError("Frozen candidate fingerprint does not match its stored contents.")


def _now() -> str:
    return datetime.now(UTC).isoformat()
