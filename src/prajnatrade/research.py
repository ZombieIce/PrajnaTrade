from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from prajnatrade.backtest import equal_weight_buy_and_hold, run_backtest
from prajnatrade.data import ValidatedData, load_bars
from prajnatrade.providers import (
    MockProvider,
    OpenAICompatibleProvider,
    ProviderBudgetExceeded,
    ProviderError,
    Proposal,
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
    holdout_start: str,
    holdout_end: str,
    provider_config: dict[str, Any] | None = None,
    trusted_local_python: bool = False,
    max_drawdown_limit: float | None = None,
) -> dict[str, Any]:
    _validate_campaign_limits(
        experiment_budget,
        max_consecutive_failures,
        max_seconds,
        token_budget,
        cost_budget_usd,
        steps,
    )
    if max_drawdown_limit is not None and (
        isinstance(max_drawdown_limit, bool)
        or not math.isfinite(max_drawdown_limit)
        or not 0 <= max_drawdown_limit <= 1
    ):
        raise ValueError("Maximum drawdown limit must be a finite proportion between 0 and 1.")
    store = SQLiteStore(database)
    campaign_id = str(uuid.uuid4())
    now = _now()
    split = _split_dates(dataset, holdout_start, holdout_end)
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
            "status": "paused",
            "stop_reason": None,
            "initial_cash": initial_cash,
            "transaction_cost_bps": transaction_cost_bps,
            "slippage_bps": slippage_bps,
            "token_budget": token_budget,
            "cost_budget_usd": cost_budget_usd,
            "max_drawdown_limit": max_drawdown_limit,
            "baseline_json": {
                **baseline.to_dict(data_kind),
                "data_provenance": {
                    key: dataset.report[key] for key in ("start_date", "end_date", "rows")
                },
            },
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
    _require_external_holdout(campaign)
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
    _require_external_holdout(campaign)
    if campaign["data_hash"] != dataset.data_hash:
        raise ValueError("Campaign data fingerprint does not match the loaded dataset.")
    if steps is not None and steps <= 0:
        raise ValueError("steps must be greater than zero.")
    if isinstance(provider, OpenAICompatibleProvider):
        provider.bind_budget_store(store, campaign_id)
    prior_elapsed = float(campaign["elapsed_seconds"])
    checkpoint = campaign["active_checkpoint_at"]
    if campaign["status"] == "running":
        if checkpoint is None:
            raise ValueError(
                "Interrupted campaign has no recoverable time checkpoint; "
                "start a new campaign rather than resetting its time budget."
            )
        try:
            checkpoint_time = datetime.fromisoformat(checkpoint)
            recovery_seconds = (datetime.now(timezone.utc) - checkpoint_time).total_seconds()
        except (TypeError, ValueError) as error:
            raise ValueError("Invalid interrupted campaign time checkpoint.") from error
        if recovery_seconds < 0:
            raise ValueError("System clock precedes the interrupted campaign time checkpoint.")
        prior_elapsed += recovery_seconds
    elif checkpoint is not None:
        raise ValueError("Paused campaign unexpectedly has an active time checkpoint.")
    checkpoint_at = _now()
    started = time.monotonic()
    store.update_campaign(
        campaign_id, updated_at=checkpoint_at, elapsed_seconds=prior_elapsed,
        active_checkpoint_at=checkpoint_at, status="running", stop_reason=None,
    )
    finished = False
    try:
        result = _advance_active_campaign(
            store, campaign_id, campaign, dataset, provider, steps=steps,
            trusted_local_python=trusted_local_python, prior_elapsed=prior_elapsed,
            started=started,
        )
        finished = True
        return result
    finally:
        if not finished:
            store.update_campaign(
                campaign_id, updated_at=_now(),
                elapsed_seconds=prior_elapsed + time.monotonic() - started,
                active_checkpoint_at=None, status="paused", stop_reason="interrupted",
            )


def _advance_active_campaign(
    store: SQLiteStore,
    campaign_id: str,
    campaign: dict[str, Any],
    dataset: ValidatedData,
    provider: ResearchProvider,
    *,
    steps: int | None,
    trusted_local_python: bool,
    prior_elapsed: float,
    started: float,
) -> dict[str, Any]:
    attempts_this_run = 0
    experiments = store.list_experiments(campaign_id)
    pending = None
    if experiments and experiments[-1]["status"] == "running":
        pending = experiments.pop()
    consecutive_failures = _trailing_failures(experiments)
    stop_reason: str | None = None

    while True:
        elapsed = prior_elapsed + time.monotonic() - started
        requests = store.list_provider_requests(campaign_id)
        if any(item["status"] == "reserved" for item in requests):
            stop_reason = "provider_request_unconfirmed"
            break
        usages = (
            [item["usage"] for item in requests]
            if isinstance(provider, OpenAICompatibleProvider)
            else [item["provider_usage"] for item in experiments]
            + ([pending["provider_usage"]] if pending is not None else [])
        )
        token_usage = sum(int(item.get("total_tokens", 0)) for item in usages)
        cost_usage = sum(
            float(item.get("actual_cost_usd", item.get("estimated_cost_usd", 0.0)))
            for item in usages
        )
        if pending is None and len(experiments) >= campaign["experiment_budget"]:
            stop_reason = "experiment_budget"
            break
        if pending is None and consecutive_failures >= campaign["max_consecutive_failures"]:
            stop_reason = "consecutive_failures"
            break
        if elapsed >= campaign["max_seconds"]:
            stop_reason = "time_budget"
            break
        backtest_pending = pending is not None and pending["metrics"] is None and pending["error"] is None
        if token_usage >= campaign["token_budget"] and not backtest_pending:
            stop_reason = "token_budget"
            break
        if cost_usage >= campaign["cost_budget_usd"] and not backtest_pending:
            stop_reason = "cost_budget"
            break
        if pending is None and steps is not None and attempts_this_run >= steps:
            break

        ordinal = len(experiments) + 1
        context = {
            "campaign_id": campaign_id,
            "experiment_ordinal": ordinal,
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
        if pending is None:
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
            pending_record = {
                "experiment_id": str(uuid.uuid4()),
                "campaign_id": campaign_id,
                "data_hash": campaign["data_hash"],
                "ordinal": ordinal,
                "hypothesis": proposal.hypothesis,
                "rationale": proposal.rationale,
                "strategy_json": proposal.strategy,
                "strategy_hash": _strategy_hash(proposal.strategy),
                "parameters_json": proposal.strategy.get("parameters", {}),
                "metrics_json": None,
                "status": "running",
                "decision": "pending",
                "reflection": "",
                "error": None,
                "provider_usage_json": proposal.usage,
                "created_at": _now(),
            }
            store.insert_experiment(pending_record)
            pending = _decode_experiment(pending_record)
            _save_progress(store, campaign_id, prior_elapsed, started)
        else:
            proposal = Proposal(
                hypothesis=pending["hypothesis"],
                rationale=pending["rationale"],
                strategy=pending["strategy"],
                usage=pending["provider_usage"],
            )

        if pending["metrics"] is not None or pending["error"] is not None:
            strategy_spec = pending["strategy"]
            metrics = pending["metrics"]
            outcome = {
                "status": "failed" if pending["error"] else "succeeded",
                "metrics": metrics,
                "error": pending["error"],
            }
        else:
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
                metrics = {
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
            store.update_experiment(
                pending["experiment_id"],
                strategy_json=strategy_spec,
                strategy_hash=_strategy_hash(strategy_spec),
                parameters_json=strategy_spec.get("parameters", {}),
                metrics_json=metrics,
                error=outcome["error"],
            )
            pending = store.get_experiment(campaign_id, pending["experiment_id"])
            _save_progress(store, campaign_id, prior_elapsed, started)

        reflection_context = dict(context)
        reflection_limits = dict(context["limits"])
        if isinstance(provider, OpenAICompatibleProvider):
            requests = store.list_provider_requests(campaign_id)
            if any(item["status"] == "reserved" for item in requests):
                stop_reason = "provider_request_unconfirmed"
                break
            reflection_limits["remaining_tokens"] = max(
                0, campaign["token_budget"] - sum(
                    int(item["usage"].get("total_tokens", 0)) for item in requests
                )
            )
            reflection_limits["remaining_cost_usd"] = max(
                0.0, campaign["cost_budget_usd"] - sum(
                    float(item["usage"].get(
                        "actual_cost_usd", item["usage"].get("estimated_cost_usd", 0.0)
                    )) for item in requests
                )
            )
        else:
            attempt_usages = [item["provider_usage"] for item in experiments] + [proposal.usage]
            reflection_limits["remaining_tokens"] = max(
                0, campaign["token_budget"] - sum(
                    int(item.get("total_tokens", 0)) for item in attempt_usages
                )
            )
            reflection_limits["remaining_cost_usd"] = max(
                0.0, campaign["cost_budget_usd"] - sum(
                    float(item.get("actual_cost_usd", item.get("estimated_cost_usd", 0.0)))
                    for item in attempt_usages
                )
            )
        reflection_limits["remaining_seconds"] = max(
            0.0, campaign["max_seconds"] - (prior_elapsed + time.monotonic() - started)
        )
        reflection_context["limits"] = reflection_limits
        if reflection_limits["remaining_seconds"] <= 0:
            stop_reason = "time_budget"
            break
        if reflection_limits["remaining_tokens"] <= 0:
            stop_reason = "token_budget"
            break
        if reflection_limits["remaining_cost_usd"] <= 0:
            stop_reason = "cost_budget"
            break
        try:
            reflection = provider.reflect(proposal, outcome, reflection_context)
        except ProviderBudgetExceeded as error:
            stop_reason = error.budget_kind
            break
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
        provider_usage = (
            store.experiment_request_usage(campaign_id, ordinal)
            if isinstance(provider, OpenAICompatibleProvider)
            else combine_usage(proposal.usage, reflection_usage)
        )
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
        experiment["experiment_id"] = pending["experiment_id"]
        experiment["created_at"] = pending["created_at"]
        store.update_experiment(
            experiment["experiment_id"],
            strategy_json=experiment["strategy_json"],
            strategy_hash=experiment["strategy_hash"],
            parameters_json=experiment["parameters_json"],
            metrics_json=experiment["metrics_json"],
            status=experiment["status"],
            decision=experiment["decision"],
            reflection=experiment["reflection"],
            error=experiment["error"],
            provider_usage_json=experiment["provider_usage_json"],
        )
        experiments.append(_decode_experiment(experiment))
        pending = None
        attempts_this_run += 1
        _save_progress(store, campaign_id, prior_elapsed, started)
        if decision == "stop":
            stop_reason = provider_stop_reason or (
                "consecutive_failures"
                if attempt_failed and consecutive_failures >= campaign["max_consecutive_failures"]
                else "provider_decision"
            )
            break

    if (
        pending is not None
        and stop_reason in {"time_budget", "token_budget", "cost_budget"}
    ):
        usage = (
            store.experiment_request_usage(campaign_id, pending["ordinal"])
            if isinstance(provider, OpenAICompatibleProvider)
            else pending["provider_usage"]
        )
        backtest_unavailable = pending["metrics"] is None and pending["error"] is None
        store.update_experiment(
            pending["experiment_id"],
            status="failed" if pending["error"] or backtest_unavailable else "succeeded",
            decision="stop",
            reflection=f"Reflection not completed: {stop_reason}.",
            error=(
                f"Backtest not completed: {stop_reason}."
                if backtest_unavailable else pending["error"]
            ),
            provider_usage_json=usage,
        )
        experiments.append(store.get_experiment(campaign_id, pending["experiment_id"]))
        pending = None

    elapsed = prior_elapsed + time.monotonic() - started
    if stop_reason is None:
        if len(experiments) >= campaign["experiment_budget"]:
            stop_reason = "experiment_budget"
        elif consecutive_failures >= campaign["max_consecutive_failures"]:
            stop_reason = "consecutive_failures"
        elif elapsed >= campaign["max_seconds"]:
            stop_reason = "time_budget"
        elif token_usage >= campaign["token_budget"]:
            stop_reason = "token_budget"
        elif cost_usage >= campaign["cost_budget_usd"]:
            stop_reason = "cost_budget"

    if stop_reason:
        status = (
            "completed" if stop_reason == "experiment_budget"
            else "paused" if stop_reason in {
                "cost_bound_unverifiable", "provider_request_unconfirmed"
            }
            else "stopped"
        )
    else:
        status = "paused"
        stop_reason = "step_limit"
    store.update_campaign(
        campaign_id,
        updated_at=_now(),
        elapsed_seconds=elapsed,
        active_checkpoint_at=None,
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
        "provider_requests": store.list_provider_requests(campaign_id),
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


def _split_dates(
    dataset: ValidatedData, holdout_start: str, holdout_end: str
) -> dict[str, str]:
    dates = list(dataset.bars["date"].drop_duplicates())
    if len(dates) < 5:
        raise ValueError("Research campaigns require at least five development sessions.")
    try:
        start = date.fromisoformat(holdout_start)
        end = date.fromisoformat(holdout_end)
    except (TypeError, ValueError) as error:
        raise ValueError("Holdout boundaries must be valid ISO dates (YYYY-MM-DD).") from error
    if start <= dates[-1].date():
        raise ValueError(
            "Development data must end before the separately stored holdout starts."
        )
    if end < start:
        raise ValueError("Holdout end must not precede its start.")
    discovery_end = max(1, int(len(dates) * 0.75) - 1)
    validation_start = discovery_end + 1
    return {
        "discovery_start": dates[0].strftime("%Y-%m-%d"),
        "discovery_end": dates[discovery_end].strftime("%Y-%m-%d"),
        "validation_start": dates[validation_start].strftime("%Y-%m-%d"),
        "validation_end": dates[-1].strftime("%Y-%m-%d"),
        "holdout_start": start.isoformat(),
        "holdout_end": end.isoformat(),
        "holdout_storage": "external",
    }


def _require_external_holdout(campaign: dict[str, Any]) -> None:
    if campaign["split"].get("holdout_storage") != "external":
        raise ValueError(
            "This legacy campaign included holdout data during research; "
            "start a new campaign with separately stored holdout data."
        )


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
    checkpoint_at = _now()
    store.update_campaign(
        campaign_id,
        updated_at=checkpoint_at,
        elapsed_seconds=prior_elapsed + time.monotonic() - started,
        active_checkpoint_at=checkpoint_at,
        status="running",
        stop_reason=None,
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
