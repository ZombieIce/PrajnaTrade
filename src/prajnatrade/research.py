from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from prajnatrade.backtest import equal_weight_buy_and_hold, run_backtest
from prajnatrade.data import ValidatedData, load_bars
from prajnatrade.providers import (
    MockProvider,
    ProviderBudgetExceeded,
    ProviderError,
    ResearchProvider,
    combine_usage,
    recent_experiment_context,
)
from prajnatrade.store import SQLiteStore
from prajnatrade.strategies import (
    make_strategy,
    normalize_strategy_spec,
    strategy_fingerprint,
)

RESEARCH_OBJECTIVE = (
    "Reproducibly investigate ETF strategy hypotheses against an equal-weight benchmark."
)


def start_campaign(
    *,
    database: str | Path,
    dataset: ValidatedData,
    data_path: str | Path,
    data_kind: str,
    provider: ResearchProvider,
    seed: int,
    experiment_budget: int,
    max_consecutive_failures: int,
    max_seconds: float,
    initial_cash: float,
    transaction_cost_bps: float,
    slippage_bps: float,
    token_budget: int,
    cost_budget_usd: float,
    steps: int | None,
    provider_config: dict[str, Any] | None = None,
    trusted_local_python: bool = False,
) -> dict[str, Any]:
    _validate_campaign_limits(
        experiment_budget,
        max_consecutive_failures,
        max_seconds,
        token_budget,
        cost_budget_usd,
        steps,
    )
    store = SQLiteStore(database)
    campaign_id = str(uuid.uuid4())
    now = _now()
    split = _split_dates(dataset)
    baseline = run_backtest(
        dataset,
        equal_weight_buy_and_hold,
        initial_cash=initial_cash,
        transaction_cost_bps=transaction_cost_bps,
        slippage_bps=slippage_bps,
        end_date=split["discovery_end"],
    )
    store.create_campaign(
        {
            "campaign_id": campaign_id,
            "objective": RESEARCH_OBJECTIVE,
            "created_at": now,
            "updated_at": now,
            "data_path": str(Path(data_path).resolve()),
            "data_hash": dataset.data_hash,
            "data_kind": data_kind,
            "price_basis": baseline.price_basis,
            "provider": provider.name,
            "provider_model": provider.model,
            "provider_config_json": provider_config or {},
            "seed": seed,
            "experiment_budget": experiment_budget,
            "max_consecutive_failures": max_consecutive_failures,
            "max_seconds": max_seconds,
            "elapsed_seconds": 0.0,
            "status": "running",
            "stop_reason": None,
            "initial_cash": initial_cash,
            "transaction_cost_bps": transaction_cost_bps,
            "slippage_bps": slippage_bps,
            "token_budget": token_budget,
            "cost_budget_usd": cost_budget_usd,
            "baseline_json": baseline.to_dict(data_kind),
            "split_json": split,
        }
    )
    return advance_campaign(
        store, campaign_id, dataset, provider, steps=steps,
        trusted_local_python=trusted_local_python,
    )


def resume_campaign(
    *,
    database: str | Path,
    campaign_id: str,
    provider: ResearchProvider | None = None,
    data_path: str | Path | None = None,
    steps: int | None = None,
    trusted_local_python: bool = False,
) -> dict[str, Any]:
    if steps is not None and steps <= 0:
        raise ValueError("steps must be greater than zero.")
    store = SQLiteStore(database)
    campaign = store.get_campaign(campaign_id)
    if campaign["status"] not in {"paused", "running"}:
        raise ValueError(
            f"Campaign {campaign_id} is {campaign['status']!r} and cannot be resumed."
        )
    source = Path(data_path or campaign["data_path"])
    dataset = load_bars(source, campaign["price_basis"])
    if dataset.data_hash != campaign["data_hash"]:
        raise ValueError(
            "Resume data fingerprint does not match the campaign's original dataset."
        )
    if provider is None:
        if campaign["provider"] != "mock":
            raise ValueError("Resume requires the original provider configuration.")
        provider = MockProvider(campaign["seed"])
    if provider.name != campaign["provider"] or provider.model != campaign["provider_model"]:
        raise ValueError("Resume provider does not match the original campaign.")
    return advance_campaign(
        store, campaign_id, dataset, provider, steps=steps,
        trusted_local_python=trusted_local_python,
    )


def advance_campaign(
    store: SQLiteStore,
    campaign_id: str,
    dataset: ValidatedData,
    provider: ResearchProvider,
    *,
    steps: int | None,
    trusted_local_python: bool = False,
) -> dict[str, Any]:
    campaign = store.get_campaign(campaign_id)
    if campaign["data_hash"] != dataset.data_hash:
        raise ValueError("Campaign data fingerprint does not match the loaded dataset.")
    if steps is not None and steps <= 0:
        raise ValueError("steps must be greater than zero.")
    started = time.monotonic()
    prior_elapsed = float(campaign["elapsed_seconds"])
    attempts_this_run = 0
    experiments = store.list_experiments(campaign_id)
    consecutive_failures = _trailing_failures(experiments)
    stop_reason: str | None = None

    while True:
        elapsed = prior_elapsed + time.monotonic() - started
        token_usage = sum(
            int(item["provider_usage"].get("total_tokens", 0))
            for item in experiments
        )
        cost_usage = sum(
            float(item["provider_usage"].get("estimated_cost_usd", 0.0))
            for item in experiments
        )
        if len(experiments) >= campaign["experiment_budget"]:
            stop_reason = "experiment_budget"
            break
        if consecutive_failures >= campaign["max_consecutive_failures"]:
            stop_reason = "consecutive_failures"
            break
        if elapsed >= campaign["max_seconds"]:
            stop_reason = "time_budget"
            break
        if token_usage >= campaign["token_budget"]:
            stop_reason = "token_budget"
            break
        if cost_usage >= campaign["cost_budget_usd"]:
            stop_reason = "cost_budget"
            break
        if steps is not None and attempts_this_run >= steps:
            break

        ordinal = len(experiments) + 1
        context = {
            "campaign_id": campaign_id,
            "data_kind": campaign["data_kind"],
            "data_hash": campaign["data_hash"],
            "baseline": campaign["baseline"]["metrics"],
            "experiments": recent_experiment_context(experiments),
            "trusted_local_python": trusted_local_python,
            "limits": {
                "remaining_experiments": campaign["experiment_budget"] - len(experiments),
                "remaining_seconds": max(0.0, campaign["max_seconds"] - elapsed),
                "remaining_tokens": max(0, campaign["token_budget"] - token_usage),
                "remaining_cost_usd": max(0.0, campaign["cost_budget_usd"] - cost_usage),
            },
        }
        try:
            proposal = provider.propose(ordinal, context)
        except ProviderBudgetExceeded as error:
            stop_reason = error.budget_kind
            break
        except ProviderError as error:
            consecutive_failures += 1
            experiment = _failed_experiment(
                campaign_id,
                ordinal,
                hypothesis=f"Provider proposal attempt {ordinal}",
                rationale="No valid proposal was returned.",
                strategy={},
                data_hash=campaign["data_hash"],
                error=str(error),
                decision=(
                    "stop"
                    if consecutive_failures >= campaign["max_consecutive_failures"]
                    else "investigate"
                ),
                reflection="Retain this failed provider attempt and stop or retry within the configured failure budget.",
                usage=error.usage or {
                    "requests": 1, "total_tokens": 0, "estimated_cost_usd": 0.0
                },
            )
            store.insert_experiment(experiment)
            experiments.append(_decode_experiment(experiment))
            attempts_this_run += 1
            _save_progress(store, campaign_id, prior_elapsed, started)
            continue

        raw_strategy = proposal.strategy
        try:
            strategy_spec = normalize_strategy_spec(raw_strategy)
            strategy = make_strategy(strategy_spec, trusted_local_python=trusted_local_python)
            backtest = run_backtest(
                dataset,
                strategy,
                initial_cash=campaign["initial_cash"],
                transaction_cost_bps=campaign["transaction_cost_bps"],
                slippage_bps=campaign["slippage_bps"],
                end_date=campaign["split"]["discovery_end"],
            )
            metrics: dict[str, Any] | None = {
                **backtest.metrics,
                "benchmark_excess_return": (
                    backtest.metrics["total_return"]
                    - campaign["baseline"]["metrics"]["total_return"]
                ),
                "ending_equity": backtest.ending_equity,
                "equity_curve": backtest.equity_curve,
                "fills": backtest.fills,
                "unavailable_prices": backtest.unavailable_prices,
            }
            outcome = {"status": "succeeded", "metrics": metrics, "error": None}
        except (ValueError, KeyError, TypeError) as error:
            strategy_spec = raw_strategy if isinstance(raw_strategy, dict) else {}
            metrics = None
            outcome = {"status": "failed", "metrics": None, "error": str(error)}

        reflection_context = dict(context)
        reflection_limits = dict(context["limits"])
        reflection_limits["remaining_tokens"] = max(
            0, reflection_limits["remaining_tokens"] - proposal.usage.get("total_tokens", 0)
        )
        reflection_limits["remaining_cost_usd"] = max(
            0.0,
            reflection_limits["remaining_cost_usd"]
            - proposal.usage.get("estimated_cost_usd", 0.0),
        )
        reflection_context["limits"] = reflection_limits
        try:
            reflection = provider.reflect(proposal, outcome, reflection_context)
        except ProviderBudgetExceeded as error:
            reflection = None
            reflection_error = str(error)
            provider_stop_reason = error.budget_kind
            failed_reflection_usage: dict[str, Any] = {}
        except ProviderError as error:
            reflection = None
            reflection_error = str(error)
            provider_stop_reason = None
            failed_reflection_usage = error.usage or {
                "requests": 1, "total_tokens": 0, "estimated_cost_usd": 0.0
            }
        else:
            reflection_error = None
            provider_stop_reason = None
        if reflection is None:
            decision = "stop" if provider_stop_reason else "investigate"
            reflection_text = f"Provider reflection failed: {reflection_error}"
            reflection_usage: dict[str, Any] = failed_reflection_usage
        else:
            decision = reflection.decision
            reflection_text = reflection.text
            reflection_usage = reflection.usage

        attempt_failed = outcome["status"] == "failed" or reflection_error is not None
        consecutive_failures = consecutive_failures + 1 if attempt_failed else 0
        if attempt_failed and consecutive_failures >= campaign["max_consecutive_failures"]:
            decision = "stop"
        strategy_hash = _strategy_hash(strategy_spec)
        provider_usage = combine_usage(proposal.usage, reflection_usage)
        if outcome["status"] == "failed":
            experiment = _failed_experiment(
                campaign_id,
                ordinal,
                hypothesis=proposal.hypothesis,
                rationale=proposal.rationale,
                strategy=strategy_spec,
                data_hash=campaign["data_hash"],
                error=outcome["error"] or "Strategy evaluation failed.",
                decision=decision,
                reflection=reflection_text,
                usage=provider_usage,
                strategy_hash=strategy_hash,
            )
        else:
            experiment = {
                "experiment_id": str(uuid.uuid4()),
                "campaign_id": campaign_id,
                "data_hash": campaign["data_hash"],
                "ordinal": ordinal,
                "hypothesis": proposal.hypothesis,
                "rationale": proposal.rationale,
                "strategy_json": strategy_spec,
                "strategy_hash": strategy_hash,
                "parameters_json": strategy_spec.get("parameters", {}),
                "metrics_json": metrics,
                "status": "failed" if reflection_error else "succeeded",
                "decision": decision,
                "reflection": reflection_text,
                "error": reflection_error,
                "provider_usage_json": provider_usage,
                "created_at": _now(),
            }
        store.insert_experiment(experiment)
        experiments.append(_decode_experiment(experiment))
        attempts_this_run += 1
        _save_progress(store, campaign_id, prior_elapsed, started)
        if decision == "stop":
            stop_reason = provider_stop_reason or (
                "consecutive_failures"
                if attempt_failed and consecutive_failures >= campaign["max_consecutive_failures"]
                else "provider_decision"
            )
            break

    elapsed = prior_elapsed + time.monotonic() - started
    if stop_reason is None:
        if len(experiments) >= campaign["experiment_budget"]:
            stop_reason = "experiment_budget"
        elif consecutive_failures >= campaign["max_consecutive_failures"]:
            stop_reason = "consecutive_failures"
        elif elapsed >= campaign["max_seconds"]:
            stop_reason = "time_budget"
        elif sum(
            int(item["provider_usage"].get("total_tokens", 0)) for item in experiments
        ) >= campaign["token_budget"]:
            stop_reason = "token_budget"
        elif sum(
            float(item["provider_usage"].get("estimated_cost_usd", 0.0))
            for item in experiments
        ) >= campaign["cost_budget_usd"]:
            stop_reason = "cost_budget"

    if stop_reason:
        status = "completed" if stop_reason == "experiment_budget" else "stopped"
    else:
        status = "paused"
        stop_reason = "step_limit"
    store.update_campaign(
        campaign_id,
        updated_at=_now(),
        elapsed_seconds=elapsed,
        status=status,
        stop_reason=stop_reason,
    )
    updated = store.get_campaign(campaign_id)
    return {
        "campaign_id": campaign_id,
        "status": updated["status"],
        "stop_reason": updated["stop_reason"],
        "provider": updated["provider"],
        "provider_model": updated["provider_model"],
        "data_kind": updated["data_kind"],
        "completed_experiments": len(experiments),
        "experiment_budget": updated["experiment_budget"],
        "elapsed_seconds": updated["elapsed_seconds"],
    }


def campaign_status(database: str | Path, campaign_id: str) -> dict[str, Any]:
    store = SQLiteStore(database)
    campaign = store.get_campaign(campaign_id)
    experiments = store.list_experiments(campaign_id)
    return {
        "campaign": campaign,
        "experiments": experiments,
    }


def _validate_campaign_limits(
    experiment_budget: int,
    max_consecutive_failures: int,
    max_seconds: float,
    token_budget: int,
    cost_budget_usd: float,
    steps: int | None,
) -> None:
    if experiment_budget <= 0:
        raise ValueError("experiments must be greater than zero.")
    if max_consecutive_failures <= 0:
        raise ValueError("max_consecutive_failures must be greater than zero.")
    if not math.isfinite(max_seconds) or max_seconds <= 0:
        raise ValueError("max_seconds must be finite and greater than zero.")
    if token_budget <= 0:
        raise ValueError("token_budget must be greater than zero.")
    if not math.isfinite(cost_budget_usd) or cost_budget_usd <= 0:
        raise ValueError("cost_budget_usd must be finite and greater than zero.")
    if steps is not None and steps <= 0:
        raise ValueError("steps must be greater than zero.")


def _split_dates(dataset: ValidatedData) -> dict[str, str]:
    dates = list(dataset.bars["date"].drop_duplicates())
    if len(dates) < 5:
        raise ValueError("Research campaigns require at least five distinct sessions for data splits.")
    discovery_end = max(1, int(len(dates) * 0.6) - 1)
    validation_start = discovery_end + 1
    validation_end = max(validation_start, int(len(dates) * 0.8) - 1)
    holdout_start = validation_end + 1
    return {
        "discovery_start": dates[0].strftime("%Y-%m-%d"),
        "discovery_end": dates[discovery_end].strftime("%Y-%m-%d"),
        "validation_start": dates[validation_start].strftime("%Y-%m-%d"),
        "validation_end": dates[validation_end].strftime("%Y-%m-%d"),
        "holdout_start": dates[holdout_start].strftime("%Y-%m-%d"),
        "holdout_end": dates[-1].strftime("%Y-%m-%d"),
    }


def _trailing_failures(experiments: list[dict[str, Any]]) -> int:
    failures = 0
    for experiment in reversed(experiments):
        if experiment["status"] != "failed":
            break
        failures += 1
    return failures


def _failed_experiment(
    campaign_id: str,
    ordinal: int,
    *,
    hypothesis: str,
    rationale: str,
    strategy: dict[str, Any],
    data_hash: str,
    error: str,
    decision: str,
    reflection: str,
    usage: dict[str, Any],
    strategy_hash: str | None = None,
) -> dict[str, Any]:
    return {
        "experiment_id": str(uuid.uuid4()),
        "campaign_id": campaign_id,
        "data_hash": data_hash,
        "ordinal": ordinal,
        "hypothesis": hypothesis,
        "rationale": rationale,
        "strategy_json": strategy,
        "strategy_hash": strategy_hash or _strategy_hash(strategy),
        "parameters_json": strategy.get("parameters", {}),
        "metrics_json": None,
        "status": "failed",
        "decision": decision,
        "reflection": reflection,
        "error": error,
        "provider_usage_json": usage,
        "created_at": _now(),
    }


def _strategy_hash(strategy: dict[str, Any]) -> str:
    encoded = json.dumps(strategy, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _decode_experiment(experiment: dict[str, Any]) -> dict[str, Any]:
    result = dict(experiment)
    for column, key in (
        ("strategy_json", "strategy"),
        ("parameters_json", "parameters"),
        ("metrics_json", "metrics"),
        ("provider_usage_json", "provider_usage"),
    ):
        value = result.pop(column)
        if isinstance(value, str):
            value = json.loads(value)
        result[key] = value
    return result


def _save_progress(
    store: SQLiteStore, campaign_id: str, prior_elapsed: float, started: float
) -> None:
    store.update_campaign(
        campaign_id,
        updated_at=_now(),
        elapsed_seconds=prior_elapsed + time.monotonic() - started,
        status="running",
        stop_reason=None,
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
