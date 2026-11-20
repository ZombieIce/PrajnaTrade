from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Sequence

from prajnatrade.acceptance import (
    AcceptanceRejection,
    write_acceptance_report,
)
from prajnatrade.backtest import (
    benchmark_relative_returns,
    equal_weight_buy_and_hold,
    run_backtest,
)
from prajnatrade.data import CASH, DataValidationError, load_bars, validate_calendar
from prajnatrade.isolation import probe_isolation
from prajnatrade.experiment import (
    ProtocolError, campaign_create, candidate_status, error_context,
    experiment_resume, experiment_status, experiment_submit,
    freeze_experiment_candidate, project_init, project_status,
)
from prajnatrade.evaluation_report import write_evaluation_report
from prajnatrade.protected_eval import (
    EvaluationRejection, agent_evaluation_status, authorize, create_plan,
    evaluation_status, run_evaluation,
)
from prajnatrade.providers import MockProvider, OpenAICompatibleProvider, ResearchProvider
from prajnatrade.report import write_campaign_report
from prajnatrade.research import campaign_status, resume_campaign, start_campaign
from prajnatrade import dividend, release, shadow, snapshot
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

    evaluation = commands.add_parser(
        "evaluation",
        help="inspect coarse evaluation receipts" if agent_only
        else "freeze, authorize, run, and report protected evaluations",
    )
    evaluation_commands = evaluation.add_subparsers(
        dest="evaluation_command", required=True
    )
    if agent_only:
        evaluation_lookup = evaluation_commands.add_parser("status")
        evaluation_lookup.add_argument("--project", required=True, type=Path)
        evaluation_lookup.add_argument("--candidate-id", required=True)
        return parser

    evaluation_plan = evaluation_commands.add_parser(
        "plan", help="freeze a candidate's protected evaluation plan"
    )
    evaluation_plan.add_argument("--store", required=True, type=Path)
    evaluation_plan.add_argument("--snapshot-store", required=True, type=Path)
    evaluation_plan.add_argument("--project", required=True, type=Path)
    evaluation_plan.add_argument("--candidate-id", required=True)
    evaluation_plan.add_argument("--input", required=True, type=Path)

    evaluation_authorize = evaluation_commands.add_parser(
        "authorize", help="record the single human Validation or Final Holdout authorization"
    )
    evaluation_authorize.add_argument("--store", required=True, type=Path)
    evaluation_authorize.add_argument("--plan-id", required=True)
    evaluation_authorize.add_argument(
        "--stage", required=True, choices=["validation", "final_holdout"]
    )
    evaluation_authorize.add_argument("--reviewer", required=True)
    evaluation_authorize.add_argument("--reason", required=True)

    evaluation_run = evaluation_commands.add_parser(
        "run", help="run one authorized, isolated, debited protected evaluation"
    )
    evaluation_run.add_argument("--store", required=True, type=Path)
    evaluation_run.add_argument("--snapshot-store", required=True, type=Path)
    evaluation_run.add_argument("--plan-id", required=True)
    evaluation_run.add_argument(
        "--stage", required=True, choices=["validation", "final_holdout"]
    )
    evaluation_run.add_argument("--project", required=True, type=Path)

    evaluation_status_parser = evaluation_commands.add_parser(
        "status", help="show the frozen plan, authorizations, ledger and records"
    )
    evaluation_status_parser.add_argument("--store", required=True, type=Path)
    evaluation_status_parser.add_argument("--plan-id", required=True)

    evaluation_report = evaluation_commands.add_parser(
        "report", help="render the controlled evaluation report from stored records"
    )
    evaluation_report.add_argument("--store", required=True, type=Path)
    evaluation_report.add_argument("--plan-id", required=True)
    evaluation_report.add_argument("--output", required=True, type=Path)

    release_parser = commands.add_parser(
        "release", help="approve frozen candidates and manage immutable release versions"
    )
    release_commands = release_parser.add_subparsers(
        dest="release_command", required=True
    )
    release_approve = release_commands.add_parser(
        "approve", help="record human approval and the unique immutable release artifact"
    )
    release_approve.add_argument("--store", required=True, type=Path)
    release_approve.add_argument("--plan-id", required=True)
    release_approve.add_argument("--reviewer", required=True)
    release_approve.add_argument("--reason", required=True)

    release_reject = release_commands.add_parser(
        "reject",
        help="record an immutable human rejection that blocks a later approval",
    )
    release_reject.add_argument("--store", required=True, type=Path)
    release_reject.add_argument("--plan-id", required=True)
    release_reject.add_argument("--reviewer", required=True)
    release_reject.add_argument("--reason", required=True)

    release_list = release_commands.add_parser(
        "list", help="list every stored release and explicit rejection decision"
    )
    release_list.add_argument("--store", required=True, type=Path)

    release_fetch = release_commands.add_parser(
        "fetch", help="fetch one release artifact by exact ID and verify its content"
    )
    release_fetch.add_argument("--store", required=True, type=Path)
    release_fetch.add_argument("--release-id", required=True)

    release_status_parser = release_commands.add_parser(
        "status", help="show the artifact fingerprints, admission and lifecycle timeline"
    )
    release_status_parser.add_argument("--store", required=True, type=Path)
    release_status_parser.add_argument("--release-id", required=True)
    release_status_parser.add_argument(
        "--as-of", help="derive the tracked state at this after-close signal date"
    )

    release_disposition = release_commands.add_parser(
        "disposition",
        help="record a verified dividend-correction impact and pause new signals",
    )
    release_disposition.add_argument("--store", required=True, type=Path)
    release_disposition.add_argument("--snapshot-store", required=True, type=Path)
    release_disposition.add_argument("--release-id", required=True)
    release_disposition.add_argument("--revision-id", required=True)
    release_disposition.add_argument("--reviewer", required=True)
    release_disposition.add_argument("--reason", required=True)
    release_disposition.add_argument(
        "--impact", choices=["sustained", "pending_review", "no_longer_supported"]
    )
    release_disposition.add_argument(
        "--evidence",
        help="required re-review evidence when --impact overrides the computed default",
    )
    release_disposition.add_argument(
        "--ledger", type=Path, help="Shadow Live ledger used to derive the pause point"
    )
    release_disposition.add_argument(
        "--data", type=Path, help="synthetic Shadow Live feed used to derive the pause point"
    )
    release_disposition.add_argument(
        "--effective-signal-date",
        help="explicit first not-yet-committed after-close signal date",
    )

    for action, description in (
        ("enable", "start tracking at a pre-registered after-close signal date"),
        ("pause", "stop new signals at a pre-registered after-close signal date"),
        ("resume", "resume the same ID while admission is still valid"),
        ("retire", "terminally retire the version, keeping its history"),
    ):
        lifecycle = release_commands.add_parser(action, help=description)
        lifecycle.add_argument("--store", required=True, type=Path)
        lifecycle.add_argument("--release-id", required=True)
        lifecycle.add_argument("--effective-signal-date", required=True)
        lifecycle.add_argument("--reason", required=True)

    shadow_parser = commands.add_parser(
        "shadow",
        help="account one synthetic Shadow Live trading day and audit the ledger",
    )
    shadow_commands = shadow_parser.add_subparsers(
        dest="shadow_command", required=True
    )
    shadow_update = shadow_commands.add_parser(
        "update",
        help="backfill and atomically commit one trading day for every tracked version",
    )
    shadow_update.add_argument("--ledger", required=True, type=Path)
    shadow_update.add_argument("--store", required=True, type=Path)
    shadow_update.add_argument("--data", required=True, type=Path)
    shadow_update.add_argument("--date", required=True)
    shadow_update.add_argument(
        "--corrections-store",
        required=True,
        type=Path,
        help="trusted snapshot store whose verified dividend-correction journal is "
        "applied; required so an eligible correction can never be silently skipped",
    )
    shadow_update.add_argument(
        "--release-id",
        action="append",
        dest="release_ids",
        help="process this exact release ID (repeatable); default is every tracked version",
    )
    shadow_report = shadow_commands.add_parser(
        "report", help="dump the committed day records for trusted audit"
    )
    shadow_report.add_argument("--ledger", required=True, type=Path)
    shadow_report.add_argument("--version", required=True)
    shadow_report.add_argument("--output", required=True, type=Path)
    shadow_compare = shadow_commands.add_parser(
        "compare",
        help="compare the longest common committed forward interval of selected versions",
    )
    shadow_compare.add_argument("--ledger", required=True, type=Path)
    shadow_compare.add_argument(
        "--data", required=True, type=Path, help="synthetic Shadow Live feed calendar"
    )
    shadow_compare.add_argument(
        "--versions",
        required=True,
        help="comma-separated release IDs; every requested version must share the run",
    )
    shadow_compare.add_argument(
        "--output", type=Path, help="write the deterministic comparison document"
    )

    acceptance = commands.add_parser(
        "acceptance",
        help="validate the six-scenario acceptance manifest and render its report",
    )
    acceptance_commands = acceptance.add_subparsers(
        dest="acceptance_command", required=True
    )
    acceptance_report = acceptance_commands.add_parser(
        "report",
        help="render a deterministic Markdown or JSON report from an acceptance manifest",
    )
    acceptance_report.add_argument("--input", required=True, type=Path)
    acceptance_report.add_argument("--output", required=True, type=Path)

    campaign = commands.add_parser("campaign", help="operator campaign setup")
    campaign_commands = campaign.add_subparsers(dest="campaign_command", required=True)
    create = campaign_commands.add_parser("create")
    create.add_argument("--project", required=True, type=Path)
    create.add_argument("--store", required=True, type=Path, help="trusted sealed snapshot store")
    create.add_argument("--snapshot-id", required=True, help="sealed snapshot with a Discovery partition")
    create.add_argument("--as-of", help="historical cutoff for a successor snapshot")
    create.add_argument("--attempts", required=True, type=int)
    create.add_argument("--max-seconds", required=True, type=float)
    create.add_argument("--initial-cash", type=float, default=100_000.0)
    create.add_argument("--transaction-cost-bps", type=float, default=5.0)
    create.add_argument("--slippage-bps", type=float, default=0.0)

    candidate = commands.add_parser("candidate", help="freeze and inspect successful Discovery candidates")
    candidate_commands = candidate.add_subparsers(dest="candidate_command", required=True)
    freeze = candidate_commands.add_parser("freeze")
    freeze.add_argument("--project", required=True, type=Path)
    freeze.add_argument("--campaign-id", required=True)
    freeze.add_argument("--submission-id", required=True)
    freeze.add_argument("--name", required=True)
    freeze.add_argument("--conclusion", required=True)
    candidate_lookup = candidate_commands.add_parser("status")
    candidate_lookup.add_argument("--project", required=True, type=Path)
    candidate_lookup.add_argument("--candidate-id", required=True)

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
    _add_isolated_python_argument(run)

    resume = research_commands.add_parser("resume", help="resume a paused campaign")
    resume.add_argument("--db", required=True, type=Path)
    resume.add_argument("--campaign-id", required=True)
    resume.add_argument("--steps", type=int)
    _add_budget_mode_arguments(resume, default=None)
    _add_isolated_python_argument(resume)

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
    _add_isolated_python_argument(validate)

    holdout = research_commands.add_parser(
        "holdout", help="consume the one-use final holdout after explicit confirmation"
    )
    holdout.add_argument("--db", required=True, type=Path)
    holdout.add_argument("--candidate-id", required=True)
    _add_data_arguments(holdout)
    holdout.add_argument("--confirmation", required=True)
    _add_isolated_python_argument(holdout)

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


def _add_isolated_python_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--isolated-generated-python",
        action="store_true",
        help="allow generated Python only in the verified OS-isolated worker",
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
    if args.command == "evaluation":
        return _evaluation_command(args, agent_only=agent_only)
    if args.command == "release":
        return _release_command(args)
    if args.command == "shadow":
        return _shadow_command(args)
    if args.command == "acceptance":
        return _acceptance_command(args)
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
                args.project, args.store, args.snapshot_id, args.attempts,
                args.max_seconds, args.initial_cash, args.transaction_cost_bps,
                args.slippage_bps, args.as_of,
            )
            print(json.dumps(result, sort_keys=True, allow_nan=False))
            return 0
        if args.command == "candidate":
            result = (
                freeze_experiment_candidate(
                    args.project, args.campaign_id, args.submission_id,
                    args.name, args.conclusion,
                )
                if args.candidate_command == "freeze"
                else candidate_status(args.project, args.candidate_id)
            )
            print(json.dumps(result, sort_keys=True, allow_nan=False))
            return 0
        if args.command == "research":
            isolated_python = getattr(args, "isolated_generated_python", False)
            if isolated_python and args.research_command in {"validate", "holdout"}:
                raise ValueError(
                    "Generated Python validation and holdout are disabled until results "
                    "can be kept outside the research CLI."
                )
            if isolated_python and args.research_command in {"run", "resume"}:
                probe_isolation()
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
                    isolated_python=isolated_python,
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
                    isolated_python=isolated_python,
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
                    isolated_python=isolated_python,
                )
            elif args.research_command == "holdout":
                result = run_holdout(
                    args.db,
                    args.candidate_id,
                    args.data,
                    args.data_kind,
                    args.price_basis,
                    args.confirmation,
                    isolated_python=isolated_python,
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


def _evaluation_command(args: argparse.Namespace, *, agent_only: bool) -> int:
    try:
        if args.evaluation_command == "status":
            if agent_only:
                result = agent_evaluation_status(args.project, args.candidate_id)
            else:
                result = evaluation_status(args.store, args.plan_id)
        elif args.evaluation_command == "plan":
            result = create_plan(
                args.store, args.snapshot_store, args.project,
                args.candidate_id, args.input,
            )
        elif args.evaluation_command == "authorize":
            result = authorize(
                args.store, args.plan_id, args.stage, args.reviewer, args.reason
            )
        elif args.evaluation_command == "report":
            result = write_evaluation_report(
                args.store, args.plan_id, args.output
            )
        else:
            result = run_evaluation(
                args.store, args.snapshot_store, args.project,
                args.plan_id, args.stage,
            )
    except EvaluationRejection as error:
        result = {
            "schema_version": 1,
            "status": "rejected",
            "error": error.details,
            **error.context,
        }
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 2
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0


def _release_command(args: argparse.Namespace) -> int:
    try:
        if args.release_command == "approve":
            result = release.approve(
                args.store, args.plan_id, args.reviewer, args.reason
            )
        elif args.release_command == "reject":
            result = release.reject(
                args.store, args.plan_id, args.reviewer, args.reason
            )
        elif args.release_command == "list":
            result = {
                "schema_version": 1,
                "status": "ok",
                "releases": release.list_releases(args.store),
            }
        elif args.release_command == "fetch":
            result = release.fetch(args.store, args.release_id)
        elif args.release_command == "status":
            result = release.status(args.store, args.release_id, args.as_of)
        elif args.release_command == "disposition":
            result = release.disposition(
                args.store,
                args.snapshot_store,
                args.release_id,
                args.revision_id,
                args.reviewer,
                args.reason,
                impact=args.impact,
                evidence=args.evidence,
                ledger=args.ledger,
                data=args.data,
                effective_signal_date=args.effective_signal_date,
            )
        else:
            result = getattr(release, args.release_command)(
                args.store,
                args.release_id,
                args.effective_signal_date,
                args.reason,
            )
    except release.ReleaseRejection as error:
        result = {
            "schema_version": 1,
            "status": "rejected",
            "error": error.details,
            **error.context,
        }
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 2
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0


def _shadow_command(args: argparse.Namespace) -> int:
    try:
        if args.shadow_command == "update":
            result, blocked = shadow.update(
                args.ledger,
                args.store,
                args.data,
                args.date,
                getattr(args, "release_ids", None),
                corrections_store=getattr(args, "corrections_store", None),
            )
            print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
            return 2 if blocked else 0
        if args.shadow_command == "compare":
            result = shadow.compare(
                args.ledger, args.data, args.versions, args.output
            )
        else:
            result = shadow.write_report(args.ledger, args.version, args.output)
    except shadow.ShadowRejection as error:
        result = {
            "schema_version": 1,
            "status": "rejected",
            "error": error.details,
            **error.context,
        }
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 2
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0


def _acceptance_command(args: argparse.Namespace) -> int:
    try:
        result = write_acceptance_report(args.input, args.output)
    except AcceptanceRejection as error:
        result = {
            "schema_version": 1,
            "status": "rejected",
            "error": error.details,
            **error.context,
        }
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 2
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0


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
