"""Set up the real #27 campaign on the trusted side and publish public metadata.

The trusted operator runs this once before starting the external model.  It
initializes the trusted project (outside the research directory), creates the
container-executor campaign with the real Discovery snapshot, and writes the
public ``campaign.json`` consumed by the research workspace.  The snapshot
store and project paths never enter the research directory.

Example (the command the lead runs)::

    python scripts/issue27_setup_campaign.py \
        --store <trusted-snapshot-store> \
        --snapshot-id <approved-24-snapshot-id> \
        --trusted-project <trusted-project-outside-research> \
        --research-dir ~/PrajnaTrade-research/issue27 \
        --strategy-image prajnatrade-issue27-runner:local \
        --attempts 3 --max-seconds 3600

``--strategy-executor container`` (the default) requires the
``--strategy-executor`` / ``--strategy-image`` flags from the Python-executor
branch; without them the script fails before touching anything and says so.
The image is resolved by ``campaign create`` (``docker image inspect``) and its
``sha256:`` local image ID is frozen into the campaign and the public metadata.
Build the runner with ``scripts/issue27-runner/build.sh`` first.

Explicit attempt/time budgets are recorded in the public metadata; the
campaign itself still owns the authoritative counters.  The container executor
starts one fresh container per rebalance (about 0.68 s per invocation in the
accepted runner evidence).  A full 656-session Discovery run is roughly 650
invocations, so the execution budget must be minutes, not the #26 Mock's 90 s;
the default 3600 s covers two full Python attempts with headroom.  An attempt
is charged exactly once per submission even though it may spawn many
containers.

Safety rules:

* the research directory must be separate from the project and the store;
* an existing campaign is reused only when snapshot, budgets, costs and
  executor config match exactly; otherwise the script refuses, and
  ``--force-new`` creates a fresh campaign instead;
* the public ``campaign.json`` is validated against the public-field allowlist
  and is checked not to contain the store or project path;
* ``--dry-run`` performs the capability check and prints the planned commands
  without writing anything; ``--skip-create`` adopts an existing campaign and
  writes only the public metadata.

This script intentionally has no Docker dependency of its own: the campaign
CLI resolves the container image digest at creation and records it in the
campaign's evaluation config.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shlex
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

from issue27_research_workspace import WorkspaceError, write_public_campaign

SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class SetupError(Exception):
    """A trusted-side setup failure with an operator-facing message."""


def _command(text: str) -> list[str]:
    command = shlex.split(text)
    if not command:
        raise SetupError("--operator-command must not be empty.")
    return command


def _run(command: list[str], *, expected: int = 0) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=600, check=False
        )
    except OSError as error:
        raise SetupError(
            f"Could not run {command[0]}: {type(error).__name__}."
        ) from error
    if completed.returncode != expected:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        message = detail[-1] if detail else f"exit status {completed.returncode}"
        raise SetupError(f"Command {' '.join(command[:2])} failed: {message}")
    return completed


def _call(command: list[str], *args: str) -> dict[str, Any]:
    completed = _run([*command, *args])
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise SetupError(f"Command {' '.join(args[:2])} did not print JSON.") from error
    if not isinstance(value, dict):
        raise SetupError(f"Command {' '.join(args[:2])} did not print a JSON object.")
    return value


def _resolve(path: Path) -> Path:
    return path.expanduser().resolve()


def _validate_paths(store: Path, project: Path, research: Path) -> None:
    if not store.is_dir():
        raise SetupError("--store must be an existing trusted snapshot store.")
    if research.exists() and not research.is_dir():
        raise SetupError("--research-dir must be a directory.")
    if not research.is_dir():
        raise SetupError(
            "--research-dir must exist; run issue27_research_workspace.py first."
        )
    if research == project or research.is_relative_to(project) or project.is_relative_to(research):
        raise SetupError("The research directory must be separate from the trusted project.")
    if research == store or research.is_relative_to(store) or store.is_relative_to(research):
        raise SetupError("The research directory must be separate from the snapshot store.")
    if (research / ".prajnatrade").exists() or any(research.rglob("project.sqlite")):
        raise SetupError("The research directory contains trusted project data.")


def _campaign_rows(project: Path) -> list[dict[str, Any]]:
    database = project / ".prajnatrade" / "project.sqlite"
    if not database.is_file():
        return []
    try:
        with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=30)) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(campaigns)").fetchall()
            }
            if "campaign_id" not in columns:
                raise SetupError("The trusted project database has no campaigns table.")
            wanted = [
                name
                for name in ("campaign_id", "snapshot_id", "attempts", "max_seconds", "evaluation_json")
                if name in columns
            ]
            rows = connection.execute(
                f"SELECT {', '.join(wanted)} FROM campaigns ORDER BY rowid"
            ).fetchall()
    except sqlite3.Error as error:
        raise SetupError(
            f"The trusted project database could not be read: {type(error).__name__}."
        ) from error
    result = []
    for row in rows:
        record = dict(zip(wanted, row, strict=True))
        try:
            evaluation = json.loads(record.get("evaluation_json") or "{}")
        except json.JSONDecodeError:
            evaluation = {}
        record["evaluation"] = evaluation if isinstance(evaluation, dict) else {}
        result.append(record)
    return result


def _stored_executor(row: dict[str, Any]) -> dict[str, Any]:
    executor = row.get("evaluation", {}).get("strategy_executor")
    if isinstance(executor, dict) and executor.get("kind") in {"none", "container"}:
        return {
            "kind": executor["kind"],
            "image": executor.get("image"),
            "digest": executor.get("digest"),
            "limits": executor.get("limits"),
        }
    return {"kind": "none", "image": None, "digest": None, "limits": None}


def _close(left: object, right: object) -> bool:
    try:
        return math.isclose(float(left), float(right), rel_tol=1e-12, abs_tol=1e-9)
    except (TypeError, ValueError):
        return False


def _configuration(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "snapshot_id": args.snapshot_id,
        "attempts": args.attempts,
        "max_seconds": args.max_seconds,
        "initial_cash": args.initial_cash,
        "transaction_cost_bps": args.transaction_cost_bps,
        "slippage_bps": args.slippage_bps,
        "strategy_executor": {
            "kind": args.strategy_executor,
            "image": args.strategy_image if args.strategy_executor == "container" else None,
            "digest": args.image_digest if args.strategy_executor == "container" else None,
        },
    }


def _differences(row: dict[str, Any], requested: dict[str, Any], *, check_executor: bool) -> list[str]:
    differences: list[str] = []
    evaluation = row.get("evaluation", {})
    if row.get("snapshot_id") != requested["snapshot_id"]:
        differences.append("snapshot_id")
    if row.get("attempts") != requested["attempts"]:
        differences.append("attempts")
    if not _close(row.get("max_seconds"), requested["max_seconds"]):
        differences.append("max_seconds")
    for field in ("initial_cash", "transaction_cost_bps", "slippage_bps"):
        if not _close(evaluation.get(field), requested[field]):
            differences.append(field)
    if not check_executor:
        return differences
    stored = _stored_executor(row)
    wanted = requested["strategy_executor"]
    if stored["kind"] != wanted["kind"]:
        differences.append("strategy_executor")
    elif wanted["kind"] == "container":
        if stored["image"] != wanted["image"]:
            differences.append("strategy_image")
        if wanted["digest"] is not None and stored["digest"] != wanted["digest"]:
            differences.append("strategy_image_digest")
    return differences


def _select_existing(
    rows: list[dict[str, Any]], args: argparse.Namespace, requested: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    if args.force_new:
        return None, []
    if args.campaign_id is not None:
        matching = [row for row in rows if row.get("campaign_id") == args.campaign_id]
        if not matching:
            raise SetupError("--campaign-id is not present in the trusted project.")
        if len(matching) > 1:
            raise SetupError("--campaign-id matched more than one campaign.")
        differences = _differences(matching[0], requested, check_executor=True)
        return matching[0], differences
    if not rows:
        return None, []
    if args.skip_create:
        if len(rows) > 1:
            raise SetupError(
                "--skip-create needs --campaign-id when the project has several campaigns."
            )
        differences = _differences(rows[0], requested, check_executor=True)
        return rows[0], differences
    matches = [
        row for row in rows if not _differences(row, requested, check_executor=True)
    ]
    if len(matches) == 1:
        return matches[0], []
    if len(matches) > 1:
        raise SetupError(
            "Several existing campaigns match the requested config; pass --campaign-id."
        )
    differences = _differences(rows[0], requested, check_executor=True)
    return None, differences


def _capability(command: list[str], executor: str) -> None:
    if executor != "container":
        return
    completed = _run([*command, "campaign", "create", "--help"])
    help_text = completed.stdout + completed.stderr
    missing = [flag for flag in ("--strategy-executor", "--strategy-image") if flag not in help_text]
    if missing:
        raise SetupError(
            "The installed `campaign create` does not support "
            + ", ".join(missing)
            + "; merge the issue27-python-executor branch (container executor) first."
        )


def _create_arguments(args: argparse.Namespace) -> list[str]:
    arguments = [
        "campaign",
        "create",
        "--project",
        str(args.trusted_project),
        "--store",
        str(args.store),
        "--snapshot-id",
        args.snapshot_id,
        "--attempts",
        str(args.attempts),
        "--max-seconds",
        str(args.max_seconds),
        "--initial-cash",
        str(args.initial_cash),
        "--transaction-cost-bps",
        str(args.transaction_cost_bps),
        "--slippage-bps",
        str(args.slippage_bps),
    ]
    if args.strategy_executor == "container":
        arguments.extend(
            ["--strategy-executor", "container", "--strategy-image", args.strategy_image]
        )
    return arguments


def _executor_from_receipt(
    receipt: dict[str, Any], args: argparse.Namespace
) -> dict[str, Any]:
    if args.strategy_executor == "none":
        return {"kind": "none", "image": None, "digest": None, "limits": None}
    executor = receipt.get("strategy_executor")
    if not isinstance(executor, dict) or executor.get("kind") != "container":
        raise SetupError(
            "campaign create did not return the container executor evidence; "
            "merge the issue27-python-executor branch first."
        )
    digest = executor.get("digest")
    image = executor.get("image")
    if (
        not isinstance(image, str)
        or not image.strip()
        or not isinstance(digest, str)
        or SHA256_DIGEST.fullmatch(digest) is None
    ):
        raise SetupError("campaign create did not return the container image and sha256 digest.")
    if args.image_digest is not None and args.image_digest != digest:
        raise SetupError("--image-digest does not match the digest campaign create recorded.")
    return {
        "kind": "container",
        "image": image,
        "digest": digest,
        "limits": executor.get("limits"),
    }


def _discovery_metadata(command: list[str], store: Path, snapshot_id: str) -> dict[str, Any]:
    manifest = _call(
        command, "snapshot", "manifest", "--store", str(store), "--id", snapshot_id
    )
    partitions = manifest.get("partitions")
    if not isinstance(partitions, list):
        raise SetupError("snapshot manifest did not return partitions.")
    discovery = next(
        (
            partition
            for partition in partitions
            if isinstance(partition, dict) and partition.get("name") == "discovery"
        ),
        None,
    )
    if discovery is None:
        raise SetupError("The snapshot has no Discovery partition.")
    result = {
        key: discovery.get(key) for key in ("start", "end", "sessions", "content_hash")
    }
    if any(value is None for value in result.values()):
        raise SetupError("The Discovery descriptor is missing its range or content hash.")
    return result


def _public_campaign(
    *,
    campaign_id: str,
    snapshot_id: str,
    data_kind: str,
    discovery: dict[str, Any],
    args: argparse.Namespace,
    executor: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "ready",
        "campaign_id": campaign_id,
        "snapshot_id": snapshot_id,
        "data_kind": data_kind,
        "discovery": discovery,
        "budgets": {
            "attempts": args.attempts,
            "execution_seconds": args.max_seconds,
        },
        "executor": executor,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="issue27_setup_campaign.py",
        description="Create the trusted #27 campaign and publish public research metadata.",
    )
    parser.add_argument("--store", required=True, type=Path, help="trusted sealed snapshot store")
    parser.add_argument("--snapshot-id", required=True, help="approved real Discovery snapshot")
    parser.add_argument(
        "--trusted-project", required=True, type=Path, help="project directory outside research"
    )
    parser.add_argument(
        "--research-dir", required=True, type=Path, help="project-only research workspace"
    )
    parser.add_argument(
        "--strategy-executor",
        choices=["container", "none"],
        default="container",
        help="campaign strategy executor (default: container)",
    )
    parser.add_argument(
        "--strategy-image",
        help=(
            "container image for generated Python (required for --strategy-executor container; "
            "build it with scripts/issue27-runner/build.sh)"
        ),
    )
    parser.add_argument(
        "--image-digest",
        help="expected immutable image digest; verified against campaign create's evidence",
    )
    parser.add_argument("--attempts", type=int, default=3, help="attempt budget (default: 3)")
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=3600.0,
        help=(
            "execution budget in seconds (default: 3600). A full 656-session Python "
            "Discovery run is ~650 container invocations (~0.68 s each), so allow minutes."
        ),
    )
    parser.add_argument("--initial-cash", type=float, default=100_000.0)
    parser.add_argument("--transaction-cost-bps", type=float, default=5.0)
    parser.add_argument("--slippage-bps", type=float, default=0.0)
    parser.add_argument(
        "--operator-command",
        default=f"{shlex.quote(sys.executable)} -m prajnatrade",
        help="trusted operator CLI (default: python -m prajnatrade)",
    )
    parser.add_argument("--campaign-id", help="select an existing campaign by ID")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and write nothing")
    parser.add_argument(
        "--skip-create",
        action="store_true",
        help="adopt an existing matching campaign instead of creating one",
    )
    parser.add_argument(
        "--force-new",
        action="store_true",
        help="create a fresh campaign even when another campaign exists",
    )
    return parser


def _validate_arguments(args: argparse.Namespace) -> None:
    if args.skip_create and args.force_new:
        raise SetupError("--skip-create and --force-new are mutually exclusive.")
    if args.skip_create and args.dry_run:
        raise SetupError("--skip-create and --dry-run are mutually exclusive.")
    if args.attempts <= 0 or args.max_seconds <= 0:
        raise SetupError("--attempts and --max-seconds must be positive.")
    if args.initial_cash <= 0:
        raise SetupError("--initial-cash must be positive.")
    if args.transaction_cost_bps < 0 or args.slippage_bps < 0:
        raise SetupError("Costs must not be negative.")
    if args.strategy_executor == "container" and not args.strategy_image:
        if not args.skip_create:
            raise SetupError("--strategy-image is required for --strategy-executor container.")
    if args.strategy_executor == "none" and args.strategy_image:
        raise SetupError("--strategy-image requires --strategy-executor container.")
    if args.strategy_executor == "none" and args.image_digest:
        raise SetupError("--image-digest requires --strategy-executor container.")
    if args.image_digest is not None and SHA256_DIGEST.fullmatch(args.image_digest) is None:
        raise SetupError("--image-digest must look like sha256:<64 lowercase hex characters>.")


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        _validate_arguments(args)
        args.store = _resolve(args.store)
        args.trusted_project = _resolve(args.trusted_project)
        args.research_dir = _resolve(args.research_dir)
        _validate_paths(args.store, args.trusted_project, args.research_dir)
        command = _command(args.operator_command)
        _capability(command, args.strategy_executor)
        requested = _configuration(args)
        rows = _campaign_rows(args.trusted_project)
        existing, differences = _select_existing(rows, args, requested)
        if args.skip_create and existing is None:
            raise SetupError("--skip-create needs an existing campaign in the trusted project.")

        if args.dry_run:
            initialized = (
                args.trusted_project / ".prajnatrade" / "project.sqlite"
            ).is_file()
            would_create = args.force_new or (existing is None and not rows)
            would_refuse = not args.force_new and not would_create
            planned = []
            if not initialized:
                planned.append(
                    [*command, "project", "init", "--project", str(args.trusted_project)]
                )
            planned.append([*command, *_create_arguments(args)])
            plan = {
                "dry_run": True,
                "store": str(args.store),
                "trusted_project": str(args.trusted_project),
                "research_dir": str(args.research_dir),
                "project_initialized": initialized,
                "existing_campaigns": [row.get("campaign_id") for row in rows],
                "existing_match": existing is not None and not differences,
                "would_create": would_create,
                "would_refuse": would_refuse,
                "differences": differences,
                "commands": planned,
                "campaign_config": requested,
            }
            print(json.dumps(plan, indent=2, sort_keys=True))
            return 0

        if existing is not None and not args.force_new:
            if differences:
                raise SetupError(
                    "The existing campaign differs from the requested config ("
                    + ", ".join(differences)
                    + "); pass --force-new for a fresh campaign."
                )
            campaign_id = str(existing["campaign_id"])
            action = "reused"
            data_kind = existing.get("evaluation", {}).get("data_kind")
            if not isinstance(data_kind, str) or not data_kind:
                raise SetupError("The existing campaign does not record its data_kind.")
            executor = _stored_executor(existing)
            if args.strategy_executor == "container":
                if executor["kind"] != "container":
                    raise SetupError("The existing campaign has no container executor.")
                if not isinstance(executor["digest"], str) or SHA256_DIGEST.fullmatch(
                    executor["digest"]
                ) is None:
                    raise SetupError("The existing campaign records no valid sha256 image digest.")
                if args.image_digest is not None and args.image_digest != executor["digest"]:
                    raise SetupError("--image-digest does not match the existing campaign.")
                executor = _stored_executor(existing)
        else:
            if args.force_new and args.campaign_id is not None:
                raise SetupError("--campaign-id cannot be combined with --force-new.")
            if differences and existing is None and rows and not args.force_new:
                raise SetupError(
                    "The project already has a campaign with a different config ("
                    + ", ".join(differences)
                    + "); pass --force-new for a fresh campaign."
                )
            if not (args.trusted_project / ".prajnatrade" / "project.sqlite").is_file():
                _call(command, "project", "init", "--project", str(args.trusted_project))
            receipt = _call(command, *_create_arguments(args))
            campaign_id = receipt.get("campaign_id")
            data_kind = receipt.get("data_kind")
            if not isinstance(campaign_id, str) or not isinstance(data_kind, str):
                raise SetupError("campaign create did not return campaign_id and data_kind.")
            if receipt.get("snapshot_id") not in (None, args.snapshot_id):
                raise SetupError("campaign create returned a different snapshot ID.")
            executor = _executor_from_receipt(receipt, args)
            action = "created"

        discovery = _discovery_metadata(command, args.store, args.snapshot_id)
        public = _public_campaign(
            campaign_id=campaign_id,
            snapshot_id=args.snapshot_id,
            data_kind=data_kind,
            discovery=discovery,
            args=args,
            executor=executor,
        )
        write_public_campaign(
            args.research_dir,
            public,
            forbidden=(
                str(args.store),
                str(args.trusted_project),
                str(args.trusted_project / ".prajnatrade"),
            ),
        )
        if data_kind != "real":
            print(
                "setup: warning: the campaign data_kind is not 'real'; this setup is only "
                "usable for #27 acceptance when data_kind is 'real'",
                file=sys.stderr,
            )
        print(
            json.dumps(
                {
                    "action": action,
                    "campaign_id": campaign_id,
                    "snapshot_id": args.snapshot_id,
                    "data_kind": data_kind,
                    "research_dir": str(args.research_dir),
                    "campaign_json": str(args.research_dir / "campaign.json"),
                    "budgets": {
                        "attempts": args.attempts,
                        "execution_seconds": args.max_seconds,
                    },
                    "executor": executor["kind"],
                    "image_digest": executor["digest"],
                },
                sort_keys=True,
            )
        )
        return 0
    except (SetupError, WorkspaceError, OSError) as error:
        print(f"setup: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
