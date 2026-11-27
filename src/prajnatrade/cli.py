from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from prajnatrade.decision_window import DECISION_POLICY_VERSIONS
from prajnatrade.trading_status import (
    EXECUTION_RULE_VERSIONS,
    NEXT_OPEN_RULES,
    STRICT_CLOSE_VALUATION,
    VALUATION_POLICY_VERSIONS,
)

if TYPE_CHECKING:
    from prajnatrade.providers import ResearchProvider


def _add_plugin_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--plugin-config", type=Path, default=None)
    parser.add_argument("--plugin", action="append", default=[], dest="plugin_specs")


def build_parser(*, agent_only: bool = False) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prajnatrade-agent" if agent_only else "prajnatrade")
    commands = parser.add_subparsers(dest="command", required=True)

    project = commands.add_parser("project", help="inspect a project" if agent_only else "initialize or inspect a project")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    for action in (("status",) if agent_only else ("init", "status")):
        project_action = project_commands.add_parser(action)
        project_action.add_argument("--project", required=True, type=Path)

    plugin_parser = commands.add_parser(
        "plugin",
        help="list registered local strategy plugins" if agent_only else "register or list local strategy plugins",
    )
    plugin_commands = plugin_parser.add_subparsers(dest="plugin_command", required=True)
    plugin_list = plugin_commands.add_parser("list", help="list registered local strategy plugins")
    plugin_list.add_argument("--project", required=True, type=Path)
    if not agent_only:
        plugin_register = plugin_commands.add_parser("register", help="register operator-approved local strategy plugins")
        plugin_register.add_argument("--project", required=True, type=Path)
        _add_plugin_flags(plugin_register)

    experiment = commands.add_parser("experiment", help="submit, inspect, and resume Discovery experiments")
    experiment_commands = experiment.add_subparsers(dest="experiment_command", required=True)
    submit = experiment_commands.add_parser("submit")
    submit.add_argument("--project", required=True, type=Path)
    submit.add_argument("--input", required=True, type=Path)
    if not agent_only:
        _add_plugin_flags(submit)
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
    _add_direction_workflow(commands, agent_only=agent_only)
    _add_research_loop_workflow(commands, agent_only=agent_only)
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
        "--data", type=Path, help="Shadow Live feed used to derive the pause point"
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
        help="account one Shadow Live trading day and audit the paper ledger",
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
    _add_plugin_flags(shadow_update)
    shadow_report = shadow_commands.add_parser(
        "report", help="dump the committed day records for trusted audit"
    )
    shadow_report.add_argument("--ledger", required=True, type=Path)
    shadow_report.add_argument("--version", required=True)
    shadow_report.add_argument("--output", required=True, type=Path)
    shadow_tick = shadow_commands.add_parser(
        "tick", help="run the trading-day clock and report Shadow Live alerts",
    )
    shadow_tick.add_argument("--ledger", type=Path, required=True)
    shadow_tick.add_argument("--store", type=Path, required=True)
    shadow_tick.add_argument("--calendar", type=Path, required=True)
    shadow_tick.add_argument("--data", type=Path, required=True)
    shadow_tick.add_argument("--corrections-store", type=Path, required=True)
    _add_plugin_flags(shadow_tick)
    shadow_alerts = shadow_commands.add_parser("alerts", help="show recorded Shadow Live scheduler alerts")
    shadow_alerts.add_argument("--ledger", type=Path, required=True)
    shadow_compare = shadow_commands.add_parser(
        "compare",
        help="compare the longest common committed forward interval of selected versions",
    )
    shadow_compare.add_argument("--ledger", required=True, type=Path)
    shadow_compare.add_argument(
        "--data", required=True, type=Path, help="Shadow Live feed calendar"
    )
    shadow_compare.add_argument(
        "--versions",
        required=True,
        help="comma-separated release IDs; every requested version must share the run",
    )
    shadow_compare.add_argument(
        "--output", type=Path, help="write the deterministic comparison document"
    )
    shadow_export = shadow_commands.add_parser(
        "export-feed",
        help="export a verifiable daily Shadow feed from a sealed Discovery payload",
    )
    shadow_export.add_argument("--store", required=True, type=Path)
    shadow_export.add_argument("--snapshot-id", required=True)
    shadow_export.add_argument("--through-date", required=True)
    shadow_export.add_argument("--output", required=True, type=Path)
    shadow_migrate = shadow_commands.add_parser(
        "migrate-state",
        help="verify or migrate a legacy Shadow ledger's stateful tracking state",
    )
    shadow_migrate.add_argument("--ledger", required=True, type=Path)
    shadow_migrate.add_argument("--version", required=True)
    shadow_migrate.add_argument("--new-start-date", default=None)
    shadow_migrate.add_argument("--initial-cash", type=float, default=None)
    shadow_migrate.add_argument("--initial-positions", default=None)

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
    create.add_argument(
        "--discovery-warmup-sessions", type=int,
        help="use this many opening Discovery sessions for history before a common scoring start",
    )
    create.add_argument(
        "--discovery-split",
        help="freeze a leading-development split of Discovery, e.g. 70/30",
    )
    create.add_argument(
        "--execution-rules", choices=EXECUTION_RULE_VERSIONS, default=NEXT_OPEN_RULES,
        help="freeze platform matching rules for Discovery and protected evaluation",
    )
    create.add_argument(
        "--valuation-policy", choices=VALUATION_POLICY_VERSIONS, default=STRICT_CLOSE_VALUATION,
        help="freeze close valuation policy for held symbols (default: strict-close-v1)",
    )
    create.add_argument(
        "--decision-policy", choices=DECISION_POLICY_VERSIONS, default=None,
        help="opt-in frozen after-close decision timing policy (e.g. after-close-common-v1)",
    )
    create.add_argument(
        "--signal-cutoff", default=None,
        help="frozen signal cutoff time HH:MM (requires --decision-policy)",
    )
    create.add_argument(
        "--signal-timezone", default=None,
        help="frozen signal cutoff IANA timezone (requires --decision-policy)",
    )
    create.add_argument(
        "--monthly-deferral", action="store_true", default=None,
        help="enable intra-month deferral of paused scheduled monthly signals (requires --decision-policy)",
    )
    create.add_argument(
        "--lot-size", type=int,
        help="uniform positive integer lot size per ETF (only for actual-open-lot-v1)",
    )
    create.add_argument(
        "--lot-sizes",
        help="JSON mapping of ETF symbol to positive integer lot size (only for actual-open-lot-v1)",
    )
    create.add_argument(
        "--strategy-executor", choices=["none", "container"], default="none",
        help="verify generated Python in a Docker container (default: none)",
    )
    create.add_argument(
        "--strategy-image",
        help="container image for generated Python (required with --strategy-executor container)",
    )

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

    snapshots = commands.add_parser("snapshot", help="seal and inspect shared snapshots")
    snapshot_commands = snapshots.add_subparsers(dest="snapshot_command", required=True)
    seal = snapshot_commands.add_parser(
        "seal", help="seal synthetic v1 or real v2/v3 inputs in a trusted store"
    )
    seal.add_argument("--input", required=True, type=Path)
    seal.add_argument("--store", required=True, type=Path)
    seal.add_argument(
        "--full-history",
        action="store_true",
        help="seal one explicitly authorized synthetic interval for a research 70/30 split",
    )
    seal.add_argument("--authorization-id")
    seal.add_argument("--authorized-start")
    seal.add_argument("--authorized-end")
    seal.add_argument(
        "--source-snapshot-id",
        help="previous synthetic snapshot used to derive this authorized interval",
    )
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


def _add_research_loop_workflow(
    commands: argparse._SubParsersAction, *, agent_only: bool,
) -> None:
    research_round = commands.add_parser(
        "round", help="open, submit to, validate, and close a research round"
    )
    rounds = research_round.add_subparsers(dest="round_action", required=True)
    status = rounds.add_parser("status", help="report research-visible round progress")
    status.add_argument("--project", required=True, type=Path)
    status.add_argument("--round-id")
    submit = rounds.add_parser("submit", help="lock a successful Discovery version")
    submit.add_argument("--project", required=True, type=Path)
    submit.add_argument("--round-id", required=True)
    submit.add_argument("--submission-id", required=True)
    submit.add_argument("--input", required=True, type=Path)

    detail = rounds.add_parser(
        "detail", help="answer one question from a version's published Validation receipt"
    )
    detail.add_argument("--project", required=True, type=Path)
    detail.add_argument("--round-id", required=True)
    detail.add_argument("--version-id", required=True)
    detail.add_argument("--input", required=True, type=Path)

    feedback = rounds.add_parser(
        "feedback", help="record what a version's Validation receipt changed"
    )
    feedback.add_argument("--project", required=True, type=Path)
    feedback.add_argument("--round-id", required=True)
    feedback.add_argument("--version-id", required=True)
    feedback.add_argument("--input", required=True, type=Path)

    if agent_only:
        return

    opened = rounds.add_parser(
        "open", help="approve the round contract and its frozen Campaign bindings"
    )
    opened.add_argument("--project", required=True, type=Path)
    opened.add_argument("--store", required=True, type=Path)
    opened.add_argument("--campaign-id", required=True)
    opened.add_argument("--direction-key", required=True)
    opened.add_argument("--name", required=True)
    opened.add_argument("--authorization-id", required=True)
    opened.add_argument("--input", required=True, type=Path)

    validate = rounds.add_parser(
        "validate", help="replay a locked version as lightweight Validation"
    )
    validate.add_argument("--project", required=True, type=Path)
    validate.add_argument("--store", required=True, type=Path)
    validate.add_argument("--snapshot-store", required=True, type=Path)
    validate.add_argument("--round-id", required=True)
    validate.add_argument("--version-id", required=True)
    _add_plugin_flags(validate)

    revalidate = rounds.add_parser(
        "revalidate",
        help=(
            "recompute lightweight Validation after a data correction, "
            "evidence source or platform fix"
        ),
    )
    revalidate.add_argument("--project", required=True, type=Path)
    revalidate.add_argument("--store", required=True, type=Path)
    revalidate.add_argument("--snapshot-store", required=True, type=Path)
    revalidate.add_argument("--round-id", required=True)
    revalidate.add_argument("--version-id", required=True)
    revalidate.add_argument("--reason", required=True)
    revalidate.add_argument(
        "--evidence-snapshot-id",
        help="corrected successor snapshot to recompute the Discovery evidence on",
    )
    _add_plugin_flags(revalidate)

    reviewed = rounds.add_parser(
        "review", help="record the human approve, return or end decision for one version"
    )
    reviewed.add_argument("--project", required=True, type=Path)
    reviewed.add_argument("--store", required=True, type=Path)
    reviewed.add_argument("--round-id", required=True)
    reviewed.add_argument("--version-id", required=True)
    reviewed.add_argument(
        "--decision", required=True, choices=["approve", "return", "end"]
    )
    reviewed.add_argument("--reviewer", required=True)
    reviewed.add_argument("--reason", required=True)
    reviewed.add_argument("--input", type=Path)
    reviewed.add_argument(
        "--calendar",
        type=Path,
        help="approved trading calendar required by approve",
    )

    close = rounds.add_parser("close", help="record an early or exhausted round stop")
    close.add_argument("--project", required=True, type=Path)
    close.add_argument("--round-id", required=True)
    close.add_argument("--reviewer", required=True)
    close.add_argument("--reason", required=True)
    close.add_argument("--input", required=True, type=Path)

    admitted = rounds.add_parser(
        "admit",
        help="grant one manual trailing Validation read after the automatic limit",
    )
    admitted.add_argument("--project", required=True, type=Path)
    admitted.add_argument("--store", required=True, type=Path)
    admitted.add_argument("--round-id", required=True)
    admitted.add_argument("--version-id", required=True)
    admitted.add_argument("--reviewer", required=True)
    admitted.add_argument("--reason", required=True)
    admitted.add_argument("--input", required=True, type=Path)

    evidence = rounds.add_parser(
        "evidence",
        help="inspect the Evidence Loop ledger state and record the human decision",
    )
    evidence_actions = evidence.add_subparsers(
        dest="evidence_action", required=True
    )
    evidence_status = evidence_actions.add_parser(
        "status",
        help="report per-version tracking facts, review trigger, pause and state",
    )
    evidence_status.add_argument("--project", required=True, type=Path)
    evidence_status.add_argument("--store", required=True, type=Path)
    evidence_status.add_argument("--ledger", required=True, type=Path)
    evidence_status.add_argument("--round-id", required=True)
    evidence_status.add_argument("--version-id")
    evidence_decide = evidence_actions.add_parser(
        "decide",
        help="record the immutable continue, pause or retire decision for one version",
    )
    evidence_decide.add_argument("--project", required=True, type=Path)
    evidence_decide.add_argument("--store", required=True, type=Path)
    evidence_decide.add_argument("--ledger", required=True, type=Path)
    evidence_decide.add_argument("--round-id", required=True)
    evidence_decide.add_argument("--version-id", required=True)
    evidence_decide.add_argument(
        "--decision", required=True, choices=["continue", "pause", "retire"]
    )
    evidence_decide.add_argument("--reviewer", required=True)
    evidence_decide.add_argument("--reason", required=True)


def _add_direction_workflow(commands: argparse._SubParsersAction, *, agent_only: bool) -> None:
    direction = commands.add_parser("direction", help="manage an authorized research direction")
    directions = direction.add_subparsers(dest="workflow_action", required=True)
    for action in ("status", "freeze", "retrospective"):
        item = directions.add_parser(action)
        item.add_argument("--project", type=Path, required=True)
        item.add_argument("--round-id", required=action != "status")
        if action in ("freeze", "retrospective"):
            item.add_argument("--input", type=Path, required=True)
        if action == "freeze":
            item.add_argument("--evaluation-id", required=True)
            item.add_argument("--name", required=True)

    validation_parser = commands.add_parser("validation", help="compare Discovery versions on reusable Validation")
    validations = validation_parser.add_subparsers(dest="workflow_action", required=True)
    for action in ("propose", "details", "feedback"):
        item = validations.add_parser(action)
        item.add_argument("--project", type=Path, required=True)
        item.add_argument("--round-id", required=True)
        if action == "propose":
            item.add_argument("--submission-id", required=True)
            item.add_argument("--input", type=Path, required=True)
        else:
            item.add_argument("--evaluation-id", required=True)
    validations.choices["details"].add_argument("--date", required=True)
    validations.choices["details"].add_argument("--question", required=True)
    validations.choices["feedback"].add_argument("--impact", required=True)
    validations.choices["feedback"].add_argument("--next-experiment-id")
    validations.choices["feedback"].add_argument("--detail-access-id")

    if agent_only:
        return

    kickoff = directions.add_parser("kickoff", help="approve the direction, constraints, protocol and Validation budget")
    kickoff.add_argument("--project", type=Path, required=True)
    kickoff.add_argument("--store", type=Path, required=True)
    kickoff.add_argument("--campaign-id", required=True)
    kickoff.add_argument("--direction-key", required=True)
    kickoff.add_argument("--name", required=True)
    kickoff.add_argument("--authorization-id", required=True)
    kickoff.add_argument("--input", type=Path, required=True)

    for action in ("run", "service", "status"):
        item = validations.add_parser(action)
        item.add_argument("--store", type=Path, required=True)
        item.add_argument("--round-id", required=True)
        if action != "status":
            item.add_argument("--snapshot-store", type=Path, required=True)
            item.add_argument("--project", type=Path, required=True)
        else:
            item.add_argument("--evaluation-id")
        if action == "run":
            item.add_argument("--submission-id", required=True)
            item.add_argument("--proposal-id", required=True)

    holdout_parser = commands.add_parser("holdout", help="authorize and run a frozen direction's Final Holdout")
    holdouts = holdout_parser.add_subparsers(dest="workflow_action", required=True)
    for action in ("authorize", "run"):
        item = holdouts.add_parser(action)
        item.add_argument("--store", type=Path, required=True)
        item.add_argument("--snapshot-store", type=Path, required=True)
        item.add_argument("--project", type=Path, required=True)
    holdouts.choices["run"].add_argument("--plan-id", required=True)
    approval = holdouts.choices["authorize"]
    approval.add_argument("--round-id", required=True)
    approval.add_argument("--validation-evaluation-id", required=True)
    approval.add_argument("--reviewer", required=True)
    approval.add_argument("--reason", required=True)
    approval.add_argument("--selection-independent", choices=["yes", "no"], required=True)
    approval.add_argument("--independence-reason", required=True)
    approval.add_argument("--calendar", type=Path, required=True)


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
    if args.command in {"direction", "validation", "holdout"}:
        return _direction_workflow_command(args)
    if args.command == "round":
        return _research_loop_command(args)
    if args.command == "evaluation":
        return _evaluation_command(args, agent_only=agent_only)
    if args.command == "release":
        return _release_command(args)
    if args.command == "shadow":
        return _shadow_command(args)
    if args.command == "acceptance":
        return _acceptance_command(args)
    if args.command == "experiment":
        from prajnatrade.experiment import (
            ProtocolError,
            error_context,
            experiment_resume,
            experiment_status,
            experiment_submit,
        )

        request = None
        try:
            if args.experiment_command == "submit":
                try:
                    request = json.loads(args.input.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as error:
                    raise ProtocolError(
                        "INVALID_SUBMISSION", "admission", "correct", str(error),
                    ) from error
                result = experiment_submit(
                    args.project,
                    request,
                    plugin_config=getattr(args, "plugin_config", None),
                    plugin_specs=getattr(args, "plugin_specs", ()),
                )
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
            if args.snapshot_command == "correction":
                from prajnatrade import dividend

                if args.correction_command == "register":
                    result = dividend.register(args.input, args.store)
                elif args.correction_command == "verify":
                    result = dividend.verify(args.input, args.store, args.proposal_id)
                else:
                    result = dividend.successor(args.store, args.proposal_id, args.revision_id)
            else:
                from prajnatrade import snapshot

                if args.snapshot_command == "seal":
                    result = snapshot.seal(
                        args.input,
                        args.store,
                        full_history=args.full_history,
                        authorization_id=args.authorization_id,
                        authorized_start=args.authorized_start,
                        authorized_end=args.authorized_end,
                        source_snapshot_id=args.source_snapshot_id,
                    )
                elif args.snapshot_command == "manifest":
                    result = snapshot.manifest(args.store, args.id)
                else:
                    result = snapshot.read_discovery(args.store, args.id, args.partition, args.as_of)
            print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
            return 0
        if args.command == "project":
            from prajnatrade.experiment import project_init, project_status

            result = (
                project_init(args.project) if args.project_command == "init"
                else project_status(args.project)
            )
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.command == "plugin":
            from prajnatrade.experiment import list_registered_plugins, register_plugins
            from prajnatrade.plugins import PluginError

            try:
                if args.plugin_command == "list":
                    result = list_registered_plugins(args.project)
                else:
                    result = register_plugins(
                        args.project,
                        plugin_config=args.plugin_config,
                        plugin_specs=args.plugin_specs,
                    )
            except PluginError as error:
                print(
                    json.dumps(
                        {"schema_version": 1, "status": "rejected", "error": error.details},
                        sort_keys=True,
                        allow_nan=False,
                    )
                )
                return 2
            print(json.dumps(result, sort_keys=True, allow_nan=False))
            return 0
        if args.command == "campaign":
            from prajnatrade.experiment import campaign_create

            parsed_lot_sizes = None
            if args.lot_sizes is not None:
                parsed_lot_sizes = json.loads(args.lot_sizes)
            result = campaign_create(
                args.project, args.store, args.snapshot_id, args.attempts,
                args.max_seconds, args.initial_cash, args.transaction_cost_bps,
                args.slippage_bps, args.as_of,
                execution_rules=args.execution_rules,
                valuation_policy=args.valuation_policy,
                decision_policy=args.decision_policy,
                signal_cutoff=args.signal_cutoff,
                signal_timezone=args.signal_timezone,
                monthly_deferral=True if args.monthly_deferral else None,
                lot_size=args.lot_size,
                lot_sizes=parsed_lot_sizes,
                strategy_executor=args.strategy_executor,
                strategy_image=args.strategy_image,
                discovery_warmup_sessions=args.discovery_warmup_sessions,
                discovery_split=args.discovery_split,
            )
            print(json.dumps(result, sort_keys=True, allow_nan=False))
            return 0
        if args.command == "candidate":
            from prajnatrade.experiment import candidate_status, freeze_experiment_candidate

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
                from prajnatrade.isolation import probe_isolation

                probe_isolation()
            if args.research_command == "run":
                from prajnatrade.data import load_bars
                from prajnatrade.research import start_campaign

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
                from prajnatrade.research import resume_campaign
                from prajnatrade.store import SQLiteStore

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
                from prajnatrade.research import campaign_status

                result = campaign_status(args.db, args.campaign_id)
            elif args.research_command == "reconcile-request":
                from prajnatrade.store import SQLiteStore

                result = SQLiteStore(args.db).reconcile_provider_request(
                    args.campaign_id, args.request_id, prompt_tokens=args.prompt_tokens,
                    completion_tokens=args.completion_tokens,
                    actual_cost_usd=args.actual_cost_usd, evidence=args.evidence,
                )
            elif args.research_command == "freeze":
                from prajnatrade.validation import freeze_candidate

                result = freeze_candidate(
                    args.db,
                    args.campaign_id,
                    args.experiment_id,
                    args.name,
                    args.conclusion,
                )
            elif args.research_command == "validate":
                from prajnatrade.validation import validate_candidate

                result = validate_candidate(
                    args.db,
                    args.candidate_id,
                    args.data,
                    args.data_kind,
                    args.price_basis,
                    isolated_python=isolated_python,
                )
            elif args.research_command == "holdout":
                from prajnatrade.validation import run_holdout

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
                from prajnatrade.report import write_campaign_report

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
                from prajnatrade.snapshot_baseline import run_discovery_baseline

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
                from prajnatrade.backtest import (
                    benchmark_relative_returns,
                    equal_weight_buy_and_hold,
                    run_backtest,
                )
                from prajnatrade.data import CASH, load_bars

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

        if args.command == "data":
            from prajnatrade.data import load_bars, validate_calendar

            dataset = load_bars(args.data, args.price_basis)
            result = {
                "valid": True,
                "data_kind": args.data_kind,
                **dataset.report,
                "calendar_validation": validate_calendar(args.calendar, dataset.bars),
            }
            print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
            return 0

        parser.error(f"unsupported command {args.command!r}")
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


def _rejection_result(error: Exception, *, schema_version: int) -> dict:
    """One machine-readable rejection shape for every workflow dispatch."""
    return {
        "schema_version": schema_version,
        "status": "rejected",
        "error": error.details,
        **getattr(error, "context", {}),
    }


def _direction_workflow_command(args: argparse.Namespace) -> int:
    from prajnatrade import release, research_direction
    from prajnatrade.experiment import ProtocolError
    from prajnatrade.protected_eval import EvaluationRejection

    try:
        if args.command == "direction":
            result = _direction_action(args, research_direction)
        elif args.command == "validation":
            result = _validation_action(args, research_direction)
        else:
            result = _holdout_action(args)
    except (EvaluationRejection, release.ReleaseRejection, ProtocolError) as error:
        result = _rejection_result(error, schema_version=2)
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 2 if result.get("status") in {"rejected", "blocked", "failed", "aborted", "interrupted"} else 0


def _research_loop_command(args: argparse.Namespace) -> int:
    from prajnatrade import release, research_loop
    from prajnatrade.experiment import ProtocolError

    try:
        if args.round_action == "open":
            prepared = research_loop.prepare_round(
                args.project,
                args.campaign_id,
                args.direction_key,
                args.name,
                args.authorization_id,
                json.loads(args.input.read_text(encoding="utf-8")),
                store=args.store,
            )
            result = research_loop.open_round(args.project, args.store, prepared)
        elif args.round_action == "status":
            result = research_loop.round_status(args.project, args.round_id)
        elif args.round_action == "submit":
            result = research_loop.submit(
                args.project,
                args.round_id,
                args.submission_id,
                json.loads(args.input.read_text(encoding="utf-8")),
            )
        elif args.round_action == "validate":
            result = research_loop.validate(
                args.project,
                args.store,
                args.snapshot_store,
                args.round_id,
                args.version_id,
                plugin_config=args.plugin_config,
                plugin_specs=args.plugin_specs,
            )
        elif args.round_action == "revalidate":
            result = research_loop.revalidate(
                args.project,
                args.store,
                args.snapshot_store,
                args.round_id,
                args.version_id,
                args.reason,
                evidence_snapshot_id=args.evidence_snapshot_id,
                plugin_config=args.plugin_config,
                plugin_specs=args.plugin_specs,
            )
        elif args.round_action == "review":
            source = (
                json.loads(args.input.read_text(encoding="utf-8"))
                if args.input is not None
                else None
            )
            result = research_loop.review(
                args.project,
                args.store,
                args.round_id,
                args.version_id,
                args.decision,
                args.reviewer,
                args.reason,
                source,
                calendar=args.calendar,
            )
        elif args.round_action == "evidence":
            if args.evidence_action == "status":
                result = research_loop.evidence_status(
                    args.project,
                    args.store,
                    args.ledger,
                    args.round_id,
                    args.version_id,
                )
            else:
                result = research_loop.evidence_decide(
                    args.project,
                    args.store,
                    args.ledger,
                    args.round_id,
                    args.version_id,
                    args.decision,
                    args.reviewer,
                    args.reason,
                )
        elif args.round_action == "detail":
            result = research_loop.detail(
                args.project,
                args.round_id,
                args.version_id,
                json.loads(args.input.read_text(encoding="utf-8")),
            )
        elif args.round_action == "feedback":
            result = research_loop.feedback(
                args.project,
                args.round_id,
                args.version_id,
                json.loads(args.input.read_text(encoding="utf-8")),
            )
        elif args.round_action == "admit":
            result = research_loop.admit(
                args.project,
                args.store,
                args.round_id,
                args.version_id,
                args.reviewer,
                args.reason,
                json.loads(args.input.read_text(encoding="utf-8")),
            )
        else:
            result = research_loop.close(
                args.project,
                args.round_id,
                args.reviewer,
                args.reason,
                json.loads(args.input.read_text(encoding="utf-8")),
            )
    except (ProtocolError, release.ReleaseRejection) as error:
        # Both the module's own ProtocolError and any release-layer refusal use
        # the shared renderer, so the protocol context is never dropped.
        result = _rejection_result(error, schema_version=1)
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 2 if result.get("status") in {"rejected", "blocked", "failed", "aborted", "interrupted"} else 0


def _direction_action(args: argparse.Namespace, direction) -> dict:
    action = args.workflow_action
    if action == "kickoff":
        prepared = direction.prepare_kickoff(
            args.project, args.campaign_id, args.direction_key, args.name, args.authorization_id,
            json.loads(args.input.read_text(encoding="utf-8")), store=args.store,
        )
        return direction.kickoff(args.project, args.store, prepared)
    if action == "status":
        if args.round_id is None:
            return {"status": "ok", "directions": direction.list_directions(args.project)}
        return direction.direction_status(args.project, args.round_id)
    source = json.loads(args.input.read_text(encoding="utf-8"))
    if action == "freeze":
        return direction.freeze(args.project, args.round_id, args.evaluation_id, args.name, source)
    return direction.retrospective(args.project, args.round_id, source)


def _validation_action(args: argparse.Namespace, direction) -> dict:
    from prajnatrade import protected_eval

    action = args.workflow_action
    if action == "propose":
        return direction.proposal_submit(
            args.project, args.round_id, args.submission_id,
            json.loads(args.input.read_text(encoding="utf-8")),
        )
    if action == "details":
        return direction.validation_details(
            args.project, args.round_id, args.evaluation_id, args.date, args.question,
        )
    if action == "feedback":
        return direction.record_feedback(
            args.project, args.round_id, args.evaluation_id, args.impact,
            next_experiment_id=args.next_experiment_id, detail_access_id=args.detail_access_id,
        )
    if action == "status":
        return protected_eval.research_validation_status(args.store, args.round_id, args.evaluation_id)
    if action == "run":
        return protected_eval.run_research_validation(
            args.store, args.snapshot_store, args.project, round_id=args.round_id,
            submission_id=args.submission_id, proposal_id=args.proposal_id,
        )
    return _service_validation(args, direction, protected_eval)


def _service_validation(args: argparse.Namespace, direction, evaluator) -> dict:
    from prajnatrade.experiment import ProtocolError
    from prajnatrade.protected_eval import EvaluationRejection

    round_status = evaluator.research_validation_status(args.store, args.round_id)
    benchmark_failed = round_status["status"] == "paused"
    queued = direction.pending_validation_work(
        args.project, args.round_id, benchmark_failed=benchmark_failed,
    )
    results = []
    for proposal in queued["proposals"]:
        try:
            result = evaluator.run_research_validation(
                args.store, args.snapshot_store, args.project, round_id=args.round_id,
                submission_id=proposal["submission_id"], proposal_id=proposal["proposal_id"],
            )
        except (EvaluationRejection, ProtocolError) as error:
            result = {"status": "rejected", "proposal_id": proposal["proposal_id"], "error": error.details}
        results.append(result)
    for request in queued["details"]:
        receipt, details = evaluator.research_validation_details(args.store, args.round_id, request)
        results.append(direction.publish_validation_details(
            args.project, args.round_id, request["detail_access_id"], receipt, details,
        ))
    paused = benchmark_failed or any(
        result["status"] == "paused"
        or result.get("scoring_evidence", {}).get("baseline", {}).get("satisfied") is False
        for result in results
    )
    return {
        "schema_version": 2,
        "scope": round_status["scope"],
        "data_kind": round_status["data_kind"],
        "status": "paused" if paused else "completed",
        "round_id": args.round_id,
        "results": results,
    }


def _holdout_action(args: argparse.Namespace) -> dict:
    from prajnatrade import research_holdout, shadow_calendar

    if args.workflow_action == "run":
        return research_holdout.run(args.store, args.snapshot_store, args.project, args.plan_id)
    return research_holdout.authorize(
        args.store, args.snapshot_store, args.project, args.round_id, args.validation_evaluation_id,
        reviewer=args.reviewer, reason=args.reason, selection_independent=args.selection_independent == "yes",
        independence_reason=args.independence_reason, calendar=shadow_calendar.load_calendar(args.calendar),
    )


def _evaluation_command(args: argparse.Namespace, *, agent_only: bool) -> int:
    from prajnatrade.protected_eval import (
        EvaluationRejection,
        agent_evaluation_status,
        authorize,
        create_plan,
        evaluation_status,
        run_evaluation,
    )

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
            from prajnatrade.evaluation_report import write_evaluation_report

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
    from prajnatrade import release

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
    from prajnatrade import shadow

    try:
        if args.shadow_command in {"tick", "alerts"}:
            from prajnatrade import shadow_schedule

            if args.shadow_command == "alerts":
                result = {"status": "ok", "notifications": shadow_schedule.notifications(args.ledger)}
                print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
                return 0
            result, blocked = shadow_schedule.tick(
                args.ledger, args.store, args.calendar, args.data,
                corrections_store=args.corrections_store,
                plugin_config=getattr(args, "plugin_config", None),
                plugin_specs=getattr(args, "plugin_specs", ()),
            )
            print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
            return 2 if blocked else 0
        if args.shadow_command == "update":
            result, blocked = shadow.update(
                args.ledger,
                args.store,
                args.data,
                args.date,
                getattr(args, "release_ids", None),
                corrections_store=getattr(args, "corrections_store", None),
                plugin_config=getattr(args, "plugin_config", None),
                plugin_specs=getattr(args, "plugin_specs", ()),
            )
            print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
            return 2 if blocked else 0
        if args.shadow_command == "compare":
            result = shadow.compare(
                args.ledger, args.data, args.versions, args.output
            )
        elif args.shadow_command == "export-feed":
            result = shadow.build_daily_feed_from_snapshot(
                args.store,
                args.snapshot_id,
                through_date=args.through_date,
                output=args.output,
            )
        elif args.shadow_command == "migrate-state":
            parsed_pos = (
                json.loads(args.initial_positions)
                if args.initial_positions is not None
                else None
            )
            result = shadow.verify_or_migrate_ledger(
                args.ledger,
                args.version,
                new_start_date=args.new_start_date,
                initial_cash=args.initial_cash,
                initial_positions=parsed_pos,
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
    from prajnatrade.acceptance import AcceptanceRejection, write_acceptance_report

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
    from prajnatrade.providers import MockProvider, OpenAICompatibleProvider

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
    from prajnatrade.providers import OpenAICompatibleProvider

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
