from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Sequence

from prajnatrade.backtest import (
    benchmark_relative_returns,
    equal_weight_buy_and_hold,
    run_backtest,
)
from prajnatrade.data import CASH, DataValidationError, load_bars, validate_calendar
from prajnatrade.experiment import (
    ProtocolError, campaign_create, error_context, experiment_resume, experiment_status,
    experiment_submit, project_init, project_status,
)
from prajnatrade.providers import MockProvider, OpenAICompatibleProvider, ResearchProvider
from prajnatrade.report import write_campaign_report
from prajnatrade.research import campaign_status, resume_campaign, start_campaign
from prajnatrade import dividend, snapshot
from prajnatrade.snapshot_baseline import run_discovery_baseline
from prajnatrade.store import SQLiteStore
from prajnatrade.validation import freeze_candidate, run_holdout, validate_candidate


def build_parser(*, agent_only: bool = False) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prajnatrade-agent" if agent_only else "prajnatrade")
    commands = parser.add_subparsers(dest="command", required=True)

    project = commands.add_parser("project", help="inspect a project" if agent_only else "initialize or inspect a project")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    for action in (("status",) if agent_only else ("init", "status")):
        project_action = project_commands.add_parser(action)
        project_action.add_argument("--project", required=True, type=Path)

    experiment = commands.add_parser("experiment", help="submit, inspect, and resume Discovery experiments")
    experiment_commands = experiment.add_subparsers(dest="experiment_command", required=True)
    submit = experiment_commands.add_parser("submit")
    submit.add_argument("--project", required=True, type=Path)
    submit.add_argument("--input", required=True, type=Path)
    for action in ("status", "resume"):
        lookup = experiment_commands.add_parser(action)
        lookup.add_argument("--project", required=True, type=Path)
        lookup.add_argument("--campaign-id", required=True)
        lookup.add_argument("--submission-id", required=True)
    if agent_only:
        return parser

    campaign = commands.add_parser("campaign", help="operator campaign setup")
    campaign_commands = campaign.add_subparsers(dest="campaign_command", required=True)
    create = campaign_commands.add_parser("create")
    create.add_argument("--project", required=True, type=Path)
    create.add_argument("--data", required=True, type=Path, help="synthetic Discovery bars")
    create.add_argument("--data-kind", required=True, choices=["synthetic"])
    create.add_argument("--attempts", required=True, type=int)
    create.add_argument("--max-seconds", required=True, type=float)
    create.add_argument("--initial-cash", type=float, default=100_000.0)
    create.add_argument("--transaction-cost-bps", type=float, default=5.0)
    create.add_argument("--slippage-bps", type=float, default=0.0)

    data = commands.add_parser("data", help="inspect and validate local datasets")
    data_commands = data.add_subparsers(dest="data_command", required=True)
    validate = data_commands.add_parser("validate", help="validate CSV or Parquet bars")
    _add_data_arguments(validate)
    validate.add_argument("--calendar", type=Path, help="CSV of expected trading dates")

    snapshots = commands.add_parser("snapshot", help="seal and inspect synthetic snapshots")
    snapshot_commands = snapshots.add_subparsers(dest="snapshot_command", required=True)
    seal = snapshot_commands.add_parser("seal", help="seal synthetic inputs in a trusted store")
    seal.add_argument("--input", required=True, type=Path)
    seal.add_argument("--store", required=True, type=Path)
    manifest = snapshot_commands.add_parser("manifest", help="inspect a snapshot manifest")
    manifest.add_argument("--store", required=True, type=Path)
    manifest.add_argument("--id", required=True)
    read = snapshot_commands.add_parser("read", help="read the approved Discovery partition")
    read.add_argument("--store", required=True, type=Path)
    read.add_argument("--id", required=True)
    read.add_argument("--partition", required=True)
    read.add_argument("--as-of", help="timezone-aware historical input cutoff (required for successors)")
    correction = snapshot_commands.add_parser("correction", help="trusted cash dividend corrections")
    correction_commands = correction.add_subparsers(dest="correction_command", required=True)
    register = correction_commands.add_parser("register", help="file a pending supplier observation")
    register.add_argument("--input", required=True, type=Path)
    register.add_argument("--store", required=True, type=Path)
    verify = correction_commands.add_parser("verify", help="file a reviewed dividend notice")
    verify.add_argument("--input", required=True, type=Path)
    verify.add_argument("--store", required=True, type=Path)
    verify.add_argument("--proposal-id", required=True)
    successor = correction_commands.add_parser("successor", help="seal a verified correction as a successor")
    successor.add_argument("--store", required=True, type=Path)
    successor.add_argument("--proposal-id", required=True)
    successor.add_argument("--revision-id", required=True)

    baseline = commands.add_parser("baseline", help="run the equal-weight buy-and-hold benchmark")
    baseline.add_argument("--data", type=Path, help="legacy CSV/Parquet total-return bars")
    baseline.add_argument("--price-basis", choices=["total_return"])
    baseline.add_argument("--data-kind", choices=["real", "synthetic"])
    baseline.add_argument("--store", type=Path, help="trusted sealed snapshot store")
    baseline.add_argument("--snapshot-id", help="content-addressed snapshot ID")
    baseline.add_argument("--output", required=True, type=Path)
    baseline.add_argument("--initial-cash", type=float, default=100_000.0)
    baseline.add_argument("--transaction-cost-bps", type=float, default=5.0)
    baseline.add_argument("--slippage-bps", type=float, default=0.0)

    research = commands.add_parser("research", help="run and inspect bounded campaigns")
    research_commands = research.add_subparsers(dest="research_command", required=True)
    run = research_commands.add_parser("run", help="start a research campaign")
    run.add_argument("--db", required=True, type=Path)
    _add_data_arguments(run)
    run.add_argument("--holdout-start", required=True, help="first date of the separately stored holdout")
    run.add_argument("--holdout-end", required=True, help="last date of the separately stored holdout")
    run.add_argument(
        "--provider", choices=["mock", "openai-compatible"], default="mock"
    )
    run.add_argument(
        "--endpoint", default=os.environ.get("PRAJNATRADE_OPENAI_ENDPOINT")
    )
    run.add_argument("--model", default=os.environ.get("PRAJNATRADE_OPENAI_MODEL"))
    run.add_argument("--api-key-env", default="OPENAI_API_KEY")
    run.add_argument("--provider-timeout-seconds", type=float, default=30.0)
    run.add_argument("--provider-max-tokens", type=int, default=1024)
    run.add_argument("--input-cost-per-1k-tokens", type=float)
    run.add_argument("--output-cost-per-1k-tokens", type=float)
    run.add_argument("--seed", type=int, default=0)
    run.add_argument("--experiments", type=int, default=20)
    run.add_argument("--steps", type=int)
    run.add_argument("--max-consecutive-failures", type=int, default=3)
    run.add_argument("--max-seconds", type=float, default=3600.0)
    run.add_argument("--initial-cash", type=float, default=100_000.0)
    run.add_argument("--transaction-cost-bps", type=float, default=5.0)
    run.add_argument("--slippage-bps", type=float, default=0.0)
    run.add_argument("--token-budget", type=int, default=12_000)
    run.add_argument("--cost-budget-usd", type=float, default=2.0)
    run.add_argument(
        "--max-drawdown-limit", type=float,
        help="optional hard maximum drawdown as a positive proportion (0 to 1)",
    )
    _add_budget_mode_arguments(run, default="strict")
    _add_trusted_python_argument(run)

    resume = research_commands.add_parser("resume", help="resume a paused campaign")
    resume.add_argument("--db", required=True, type=Path)
    resume.add_argument("--campaign-id", required=True)
    resume.add_argument("--steps", type=int)
    _add_budget_mode_arguments(resume, default=None)
    _add_trusted_python_argument(resume)

    status = research_commands.add_parser("status", help="show campaign and experiment history")
    status.add_argument("--db", required=True, type=Path)
    status.add_argument("--campaign-id", required=True)

    reconcile = research_commands.add_parser(
        "reconcile-request", help="record verified provider usage and actual charges"
    )
    reconcile.add_argument("--db", required=True, type=Path)
    reconcile.add_argument("--campaign-id", required=True)
    reconcile.add_argument("--request-id", required=True)
    reconcile.add_argument("--prompt-tokens", required=True, type=int)
    reconcile.add_argument("--completion-tokens", required=True, type=int)
    reconcile.add_argument("--actual-cost-usd", required=True, type=float)
    reconcile.add_argument("--evidence", required=True)

    freeze = research_commands.add_parser("freeze", help="freeze a successful experiment")
    freeze.add_argument("--db", required=True, type=Path)
    freeze.add_argument("--campaign-id", required=True)
    freeze.add_argument("--experiment-id", required=True)
    freeze.add_argument("--name", required=True)
    freeze.add_argument("--conclusion", required=True)

    validate = research_commands.add_parser(
        "validate", help="evaluate a frozen candidate on the independent validation interval"
    )
    validate.add_argument("--db", required=True, type=Path)
    validate.add_argument("--candidate-id", required=True)
    _add_data_arguments(validate)
    _add_trusted_python_argument(validate)

    holdout = research_commands.add_parser(
        "holdout", help="consume the one-use final holdout after explicit confirmation"
    )
    holdout.add_argument("--db", required=True, type=Path)
    holdout.add_argument("--candidate-id", required=True)
    _add_data_arguments(holdout)
    holdout.add_argument("--confirmation", required=True)
    _add_trusted_python_argument(holdout)

    report = research_commands.add_parser(
        "report", help="render a self-contained HTML campaign report"
    )
    report.add_argument("--db", required=True, type=Path)
    report.add_argument("--campaign-id", required=True)
    report.add_argument("--output", required=True, type=Path)
    return parser


def _add_data_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--price-basis", required=True, choices=["total_return"])
    parser.add_argument("--data-kind", required=True, choices=["real", "synthetic"])


def _add_trusted_python_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--trusted-local-generated-python",
        action="store_true",
        help="execute generated Python locally without isolation; use only with trusted code",
    )


def _add_budget_mode_arguments(
    parser: argparse.ArgumentParser, *, default: str | None
) -> None:
    parser.add_argument("--budget-mode", choices=["strict", "best-effort"], default=default)
    parser.add_argument(
        "--accept-budget-overrun-risk",
        action="store_true",
        help="explicitly accept that best-effort calls may exceed the budget",
    )


def main(argv: Sequence[str] | None = None) -> int:
    return _main(argv, agent_only=False)


def agent_main(argv: Sequence[str] | None = None) -> int:
    return _main(argv, agent_only=True)


def _main(argv: Sequence[str] | None, *, agent_only: bool) -> int:
    parser = build_parser(agent_only=agent_only)
    args = parser.parse_args(argv)
    if args.command == "experiment":
        request = None
        try:
            if args.experiment_command == "submit":
                try:
                    request = json.loads(args.input.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as error:
                    raise ProtocolError(
                        "INVALID_SUBMISSION", "admission", "correct", str(error),
                    ) from error
                result = experiment_submit(args.project, request)
            elif args.experiment_command == "status":
                result = experiment_status(args.project, args.campaign_id, args.submission_id)
            else:
                result = experiment_resume(args.project, args.campaign_id, args.submission_id)
        except ProtocolError as error:
            result = {"schema_version": 1, "status": "rejected", "error": error.details}
            if args.experiment_command != "submit":
                campaign_id, submission_id = args.campaign_id, args.submission_id
            elif isinstance(request, dict):
                campaign_id, submission_id = request.get("campaign_id"), request.get("submission_id")
            else:
                campaign_id = submission_id = None
            try:
                result.update(error_context(args.project, campaign_id, submission_id))
            except (OSError, sqlite3.Error, ValueError):
                result["error"]["project_context"] = "unavailable"
        except (OSError, sqlite3.Error, ValueError) as error:
            result = {
                "schema_version": 1, "status": "failed",
                "error": {"code": "PROJECT_ERROR", "phase": "project", "next_action": "inspect", "message": str(error)},
            }
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 2 if result["status"] in {"failed", "rejected"} else 0
    try:
        if args.command == "snapshot":
            if args.snapshot_command == "seal":
                result = snapshot.seal(args.input, args.store)
            elif args.snapshot_command == "manifest":
                result = snapshot.manifest(args.store, args.id)
            elif args.snapshot_command == "correction":
                if args.correction_command == "register":
                    result = dividend.register(args.input, args.store)
                elif args.correction_command == "verify":
                    result = dividend.verify(args.input, args.store, args.proposal_id)
                else:
                    result = dividend.successor(args.store, args.proposal_id, args.revision_id)
            else:
                result = snapshot.read_discovery(args.store, args.id, args.partition, args.as_of)
            print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
            return 0
        if args.command == "project":
            result = (
                project_init(args.project) if args.project_command == "init"
                else project_status(args.project)
            )
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.command == "campaign":
            result = campaign_create(
                args.project, args.data, args.attempts, args.max_seconds,
                args.initial_cash, args.transaction_cost_bps, args.slippage_bps,
            )
            print(json.dumps(result, sort_keys=True, allow_nan=False))
            return 0
        if args.command == "research":
            trusted_local_python = getattr(args, "trusted_local_generated_python", False)
            if trusted_local_python:
                print(
                    "warning: generated Python runs with full local process privileges; "
                    "this is not a sandbox.",
                    file=sys.stderr,
                )
            if args.research_command == "run":
                if args.budget_mode == "best-effort" and args.accept_budget_overrun_risk:
                    print(
                        "warning: best-effort provider calls may exceed the configured budget.",
                        file=sys.stderr,
                    )
                dataset = load_bars(args.data, args.price_basis)
                provider, provider_config = _make_provider(args)
                result = start_campaign(
                    database=args.db,
                    dataset=dataset,
                    data_path=args.data,
                    data_kind=args.data_kind,
                    provider=provider,
                    seed=args.seed,
                    experiment_budget=args.experiments,
                    max_consecutive_failures=args.max_consecutive_failures,
                    max_seconds=args.max_seconds,
                    initial_cash=args.initial_cash,
                    transaction_cost_bps=args.transaction_cost_bps,
                    slippage_bps=args.slippage_bps,
                    token_budget=args.token_budget,
                    cost_budget_usd=args.cost_budget_usd,
                    steps=args.steps,
                    holdout_start=args.holdout_start,
                    holdout_end=args.holdout_end,
                    provider_config=provider_config,
                    trusted_local_python=trusted_local_python,
                    max_drawdown_limit=args.max_drawdown_limit,
                )
            elif args.research_command == "resume":
                campaign = SQLiteStore(args.db).get_campaign(args.campaign_id)
                provider = _resume_provider(
                    campaign,
                    budget_mode=args.budget_mode,
                    accept_budget_risk=args.accept_budget_overrun_risk,
                )
                if args.budget_mode == "best-effort" and args.accept_budget_overrun_risk:
                    print(
                        "warning: best-effort provider calls may exceed the configured budget.",
                        file=sys.stderr,
                    )
                result = resume_campaign(
                    database=args.db,
                    campaign_id=args.campaign_id,
                    provider=provider,
                    steps=args.steps,
                    trusted_local_python=trusted_local_python,
                )
            elif args.research_command == "status":
                result = campaign_status(args.db, args.campaign_id)
            elif args.research_command == "reconcile-request":
                result = SQLiteStore(args.db).reconcile_provider_request(
                    args.campaign_id, args.request_id, prompt_tokens=args.prompt_tokens,
                    completion_tokens=args.completion_tokens,
                    actual_cost_usd=args.actual_cost_usd, evidence=args.evidence,
                )
            elif args.research_command == "freeze":
                result = freeze_candidate(
                    args.db,
                    args.campaign_id,
                    args.experiment_id,
                    args.name,
                    args.conclusion,
                )
            elif args.research_command == "validate":
                result = validate_candidate(
                    args.db,
                    args.candidate_id,
                    args.data,
                    args.data_kind,
                    args.price_basis,
                    trusted_local_python=trusted_local_python,
                )
            elif args.research_command == "holdout":
                result = run_holdout(
                    args.db,
                    args.candidate_id,
                    args.data,
                    args.data_kind,
                    args.price_basis,
                    args.confirmation,
                    trusted_local_python=trusted_local_python,
                )
            else:
                result = write_campaign_report(
                    args.db, args.campaign_id, args.output
                )
            print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
            return 0

        if args.command == "baseline":
            if args.store is not None or args.snapshot_id is not None:
                if args.store is None or args.snapshot_id is None or args.data is not None:
                    raise ValueError(
                        "Snapshot baseline requires --store and --snapshot-id, and cannot use --data."
                    )
                result = run_discovery_baseline(
                    args.store,
                    args.snapshot_id,
                    initial_cash=args.initial_cash,
                    transaction_cost_bps=args.transaction_cost_bps,
                    slippage_bps=args.slippage_bps,
                )
            else:
                if args.data is None or args.price_basis is None or args.data_kind is None:
                    raise ValueError(
                        "CSV/Parquet baseline requires --data, --price-basis, and --data-kind; "
                        "use --store and --snapshot-id for a sealed Discovery snapshot."
                    )
                dataset = load_bars(args.data, args.price_basis)
                backtest = run_backtest(
                    dataset,
                    equal_weight_buy_and_hold,
                    initial_cash=args.initial_cash,
                    transaction_cost_bps=args.transaction_cost_bps,
                    slippage_bps=args.slippage_bps,
                )
                cash_benchmark = run_backtest(
                    dataset,
                    lambda history, state: {CASH: 1.0},
                    initial_cash=args.initial_cash,
                    transaction_cost_bps=args.transaction_cost_bps,
                    slippage_bps=args.slippage_bps,
                )
                result = backtest.to_dict(args.data_kind)
                result["relative_benchmark"] = CASH
                result["relative_returns"] = benchmark_relative_returns(
                    backtest, cash_benchmark
                )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n",
                encoding="utf-8",
            )
            print(json.dumps(
                {
                    "data_kind": result["data_kind"],
                    "ending_equity": result["ending_equity"],
                    "output": str(args.output),
                },
                sort_keys=True,
            ))
            return 0

        dataset = load_bars(args.data, args.price_basis)
        if args.command == "data":
            result = {
                "valid": True,
                "data_kind": args.data_kind,
                **dataset.report,
                "calendar_validation": validate_calendar(args.calendar, dataset.bars),
            }
            print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
            return 0

        parser.error(f"unsupported command {args.command!r}")
    except (DataValidationError, OSError, sqlite3.Error, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


def _make_provider(args: argparse.Namespace) -> tuple[ResearchProvider, dict[str, object]]:
    if args.provider == "mock":
        return MockProvider(args.seed), {}
    if not args.endpoint or not args.model:
        raise ValueError(
            "OpenAI-compatible campaigns require --endpoint and --model (or their PRAJNATRADE_OPENAI_* environment variables)."
        )
    if (
        args.input_cost_per_1k_tokens is None
        or args.output_cost_per_1k_tokens is None
    ):
        raise ValueError(
            "OpenAI-compatible campaigns require input and output token cost rates."
        )
    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        raise ValueError(
            f"Provider API key is missing from environment variable {args.api_key_env!r}."
        )
    config = {
        "endpoint": args.endpoint,
        "api_key_env": args.api_key_env,
        "timeout": args.provider_timeout_seconds,
        "max_tokens": args.provider_max_tokens,
        "input_cost_per_1k_tokens": args.input_cost_per_1k_tokens,
        "output_cost_per_1k_tokens": args.output_cost_per_1k_tokens,
        "budget_mode": args.budget_mode,
    }
    return (
        OpenAICompatibleProvider(
            endpoint=args.endpoint,
            model=args.model,
            api_key=api_key,
            timeout=args.provider_timeout_seconds,
            max_tokens=args.provider_max_tokens,
            input_cost_per_1k_tokens=args.input_cost_per_1k_tokens,
            output_cost_per_1k_tokens=args.output_cost_per_1k_tokens,
            budget_mode=args.budget_mode,
            accept_budget_risk=args.accept_budget_overrun_risk,
        ),
        config,
    )


def _resume_provider(
    campaign: dict[str, object],
    *,
    budget_mode: str | None = None,
    accept_budget_risk: bool = False,
) -> ResearchProvider | None:
    if campaign["provider"] == "mock":
        return None
    if campaign["provider"] != "openai-compatible":
        raise ValueError(f"Unsupported persisted provider: {campaign['provider']!r}.")
    config = campaign["provider_config"]
    api_key_env = str(config["api_key_env"])
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise ValueError(
            f"Provider API key is missing from environment variable {api_key_env!r}."
        )
    return OpenAICompatibleProvider(
        endpoint=str(config["endpoint"]),
        model=str(campaign["provider_model"]),
        api_key=api_key,
        timeout=float(config["timeout"]),
        max_tokens=int(config["max_tokens"]),
        input_cost_per_1k_tokens=float(config["input_cost_per_1k_tokens"]),
        output_cost_per_1k_tokens=float(config["output_cost_per_1k_tokens"]),
        budget_mode=budget_mode or str(config.get("budget_mode", "strict")),
        accept_budget_risk=accept_budget_risk,
    )
