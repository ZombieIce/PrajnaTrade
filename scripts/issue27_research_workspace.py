"""Build the project-only research workspace for issue #27.

The workspace is deliberately created **outside Git** (default
``~/PrajnaTrade-research/issue27``).  It contains only public campaign
metadata and the operating contract for the external model; the trusted
project, the sealed snapshot store and the repository are never referenced.

Usage::

    python scripts/issue27_research_workspace.py \
        --output ~/PrajnaTrade-research/issue27
    python scripts/issue27_research_workspace.py \
        --output ~/PrajnaTrade-research/issue27 \
        --campaign-json /path/to/public-campaign.json

Without ``--campaign-json`` the builder writes ``campaign.json`` with status
``pending_operator``; ``issue27_setup_campaign.py`` fills it from the real
campaign.  The builder refuses an output directory inside any Git checkout and
refuses to overwrite an existing workspace without ``--force``.

Only these public campaign fields exist::

    schema_version, status, campaign_id, snapshot_id, data_kind,
    discovery {start, end, sessions, content_hash},
    budgets {attempts, execution_seconds},
    executor {kind, image, digest, limits}

Absolute paths, ``.prajnatrade`` references and ``project.sqlite`` references
are rejected, so a public campaign document cannot smuggle a trusted path into
the model's directory.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
WORKSPACE_DIRS = ("inbox", "outbox", "processed", "notes", "receipts")
CAMPAIGN_KEYS = {
    "schema_version",
    "status",
    "campaign_id",
    "snapshot_id",
    "data_kind",
    "discovery",
    "budgets",
    "executor",
}
DISCOVERY_KEYS = {"start", "end", "sessions", "content_hash"}
BUDGET_KEYS = {"attempts", "execution_seconds"}
EXECUTOR_KEYS = {"kind", "image", "digest", "limits"}
HASH = re.compile(r"[0-9a-f]{64}\Z")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
WINDOWS_ABSOLUTE = re.compile(r"[A-Za-z]:[\\/]")


class WorkspaceError(Exception):
    """A public-campaign or destination error."""


def pending_campaign() -> dict[str, Any]:
    """Return the placeholder campaign.json written before the operator setup."""
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "pending_operator",
        "campaign_id": None,
        "snapshot_id": None,
        "data_kind": None,
        "discovery": {
            "start": None,
            "end": None,
            "sessions": None,
            "content_hash": None,
        },
        "budgets": {"attempts": None, "execution_seconds": None},
        "executor": {"kind": None, "image": None, "digest": None, "limits": None},
    }


def _public_text(value: str, field: str) -> str:
    if not value.strip():
        raise WorkspaceError(f"{field} must be a non-empty string.")
    if value.startswith(("/", "~", "\\")) or WINDOWS_ABSOLUTE.match(value):
        raise WorkspaceError(f"{field} must not be an absolute path.")
    if ".prajnatrade" in value or "project.sqlite" in value:
        raise WorkspaceError(f"{field} must not reference trusted project data.")
    return value


def _mapping(value: object, field: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise WorkspaceError(f"{field} must contain exactly: {', '.join(sorted(keys))}.")
    return value


def _nullable(value: object, check, field: str) -> object:
    if value is None:
        return None
    return check(value, field)


def _hash_value(value: object, field: str) -> str:
    if not isinstance(value, str) or not HASH.fullmatch(value):
        raise WorkspaceError(f"{field} must be a lowercase SHA-256 hex digest.")
    return value


def _date_value(value: object, field: str) -> str:
    if not isinstance(value, str) or not DATE.fullmatch(value):
        raise WorkspaceError(f"{field} must be YYYY-MM-DD.")
    return value


def _positive_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise WorkspaceError(f"{field} must be a positive number.")
    return float(value)


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise WorkspaceError(f"{field} must be a positive integer.")
    return value


def normalize_public_campaign(raw: object, *, pending: bool = False) -> dict[str, Any]:
    """Validate and return a campaign document containing only public fields.

    ``pending=True`` validates the placeholder shape (all metadata may be null);
    otherwise a complete, ready document is required.
    """
    if not isinstance(raw, dict):
        raise WorkspaceError("campaign metadata must be a JSON object.")
    if set(raw) - CAMPAIGN_KEYS:
        raise WorkspaceError(
            "campaign metadata contains non-public fields: "
            + ", ".join(sorted(set(raw) - CAMPAIGN_KEYS))
        )
    if set(raw) != CAMPAIGN_KEYS:
        raise WorkspaceError("campaign metadata is missing public fields.")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != SCHEMA_VERSION:
        raise WorkspaceError("Unsupported campaign schema_version.")
    status = raw["status"]
    if status not in {"pending_operator", "ready"}:
        raise WorkspaceError("campaign status must be pending_operator or ready.")
    if status == "pending_operator" and not pending:
        raise WorkspaceError("campaign metadata is still pending_operator.")

    discovery = _mapping(raw["discovery"], "discovery", DISCOVERY_KEYS)
    budgets = _mapping(raw["budgets"], "budgets", BUDGET_KEYS)
    executor = _mapping(raw["executor"], "executor", EXECUTOR_KEYS)

    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "campaign_id": _nullable(
            raw["campaign_id"], _public_text, "campaign_id"
        ),
        "snapshot_id": _nullable(
            raw["snapshot_id"], lambda v, f: _hash_value(v, f), "snapshot_id"
        ),
        "data_kind": _nullable(raw["data_kind"], _public_text, "data_kind"),
        "discovery": {
            "start": _nullable(discovery["start"], _date_value, "discovery.start"),
            "end": _nullable(discovery["end"], _date_value, "discovery.end"),
            "sessions": _nullable(discovery["sessions"], _positive_int, "discovery.sessions"),
            "content_hash": _nullable(
                discovery["content_hash"], _hash_value, "discovery.content_hash"
            ),
        },
        "budgets": {
            "attempts": _nullable(budgets["attempts"], _positive_int, "budgets.attempts"),
            "execution_seconds": _nullable(
                budgets["execution_seconds"],
                _positive_number,
                "budgets.execution_seconds",
            ),
        },
        "executor": {"kind": None, "image": None, "digest": None, "limits": None},
    }
    result["executor"]["kind"] = _nullable(executor["kind"], _public_text, "executor.kind")
    result["executor"]["image"] = _nullable(executor["image"], _public_text, "executor.image")
    result["executor"]["digest"] = _nullable(executor["digest"], _public_text, "executor.digest")
    limits = executor["limits"]
    if limits is not None:
        if not isinstance(limits, dict):
            raise WorkspaceError("executor.limits must be an object or null.")
        result["executor"]["limits"] = _validate_limits(limits)

    if status == "ready":
        if result["campaign_id"] is None or result["snapshot_id"] is None:
            raise WorkspaceError("A ready campaign needs campaign_id and snapshot_id.")
        if result["data_kind"] is None:
            raise WorkspaceError("A ready campaign needs data_kind.")
        if any(value is None for value in result["discovery"].values()):
            raise WorkspaceError("A ready campaign needs the full Discovery range and content hash.")
        if any(value is None for value in result["budgets"].values()):
            raise WorkspaceError("A ready campaign needs the attempt and execution budgets.")
        if result["executor"]["kind"] not in {"none", "container"}:
            raise WorkspaceError("A ready campaign executor.kind must be none or container.")
        if result["executor"]["kind"] == "container":
            if result["executor"]["image"] is None or result["executor"]["digest"] is None:
                raise WorkspaceError(
                    "A container campaign needs executor.image and executor.digest."
                )
        elif result["executor"]["image"] is not None or result["executor"]["digest"] is not None:
            raise WorkspaceError("A declarative campaign must not record an executor image.")
    return result


def _validate_limits(limits: dict[str, Any]) -> dict[str, Any]:
    validated: dict[str, Any] = {}
    for key, value in limits.items():
        if not isinstance(key, str) or not key:
            raise WorkspaceError("executor.limits keys must be non-empty strings.")
        if isinstance(value, bool):
            validated[key] = value
        elif isinstance(value, (int, float)):
            validated[key] = value
        elif isinstance(value, str):
            validated[key] = _public_text(value, f"executor.limits.{key}")
        else:
            raise WorkspaceError("executor.limits values must be numbers, strings or booleans.")
    return validated


def write_public_campaign(
    research_dir: Path, campaign: dict[str, Any], *, forbidden: tuple[str, ...] = ()
) -> Path:
    """Validate and atomically write ``campaign.json`` into a research dir."""
    pending = isinstance(campaign, dict) and campaign.get("status") == "pending_operator"
    normalized = normalize_public_campaign(campaign, pending=pending)
    text = json.dumps(normalized, indent=2, sort_keys=True, allow_nan=False) + "\n"
    for token in forbidden:
        if token and token in text:
            raise WorkspaceError("campaign metadata contains a forbidden trusted path.")
    path = research_dir / "campaign.json"
    _write_atomic(path, text.encode("utf-8"))
    return path


def _write_atomic(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_campaign(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise WorkspaceError("--campaign-json must be readable JSON.") from error
    return normalize_public_campaign(value)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _reject_git_destination(output: Path) -> None:
    root = _repo_root()
    if output == root or output.is_relative_to(root):
        raise WorkspaceError("The research workspace must be outside the Git checkout.")
    for parent in (output, *output.parents):
        if (parent / ".git").exists():
            raise WorkspaceError("The research workspace must be outside any Git checkout.")


def build_workspace(
    output: Path,
    *,
    campaign: dict[str, Any] | None = None,
    force: bool = False,
    forbidden: tuple[str, ...] = (),
) -> list[Path]:
    """Create the project-only workspace and return the written paths."""
    output = output.expanduser().resolve()
    _reject_git_destination(output)
    if output.exists() and not output.is_dir():
        raise WorkspaceError("--output exists and is not a directory.")
    if output.is_dir() and any(output.iterdir()) and not force:
        raise WorkspaceError("--output is not empty; pass --force to overwrite generated files.")
    if (output / ".prajnatrade").exists() or any(output.rglob("project.sqlite")):
        raise WorkspaceError("--output contains trusted project data.")
    output.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name in WORKSPACE_DIRS:
        directory = output / name
        if directory.is_symlink():
            raise WorkspaceError(f"{name}/ must not be a symbolic link.")
        directory.mkdir(exist_ok=True)
        written.append(directory)
    files = {
        "README.md": README,
        "AGENT_PROMPT.md": AGENT_PROMPT,
        "SUBMISSION_FORMAT.md": SUBMISSION_FORMAT,
        "notes/000-template.md": NOTE_TEMPLATE,
    }
    for relative, content in files.items():
        path = output / relative
        _write_atomic(path, content.encode("utf-8"))
        written.append(path)
    public = campaign if campaign is not None else pending_campaign()
    written.append(write_public_campaign(output, public, forbidden=forbidden))
    return written


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="issue27_research_workspace.py",
        description="Build the project-only research workspace for issue #27.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("~/PrajnaTrade-research/issue27"),
        help="workspace directory outside Git (default: ~/PrajnaTrade-research/issue27)",
    )
    parser.add_argument(
        "--campaign-json",
        type=Path,
        help="public campaign metadata to validate and install as campaign.json",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite generated files in a non-empty workspace",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        campaign = _read_campaign(args.campaign_json) if args.campaign_json else None
        written = build_workspace(args.output, campaign=campaign, force=args.force)
    except (WorkspaceError, OSError) as error:
        print(f"workspace: {error}", file=sys.stderr)
        return 2
    root = args.output.expanduser().resolve()
    print(
        json.dumps(
            {
                "output": str(root),
                "campaign_status": json.loads((root / "campaign.json").read_text())["status"],
                "written": [str(path.relative_to(root)) if path.is_file() else path.name + "/" for path in written],
            },
            sort_keys=True,
        )
    )
    return 0


README = """# Issue #27 research workspace

You are the external research model in the controlled iteration for
PrajnaTrade issue #27. This directory is the whole world you may use.

## Role and boundary

- Work only inside **this directory** (`inbox/`, `outbox/`, `processed/`,
  `notes/`, `receipts/`). Do not read, list, search or write anything outside
  it. In particular never access a repository, a `.prajnatrade` directory, a
  project database, a sealed snapshot store, another host, or the network.
- Do not run `git`. Do not open a shell command that names an absolute path.
  Do not create symbolic links.
- You have no API key, token or other credential, and you must not ask for
  one. There are no real broker connections and no real orders.
- `campaign.json` is the only campaign metadata you receive. It contains a
  public campaign ID, snapshot ID, Discovery range/content hash, budgets and
  the executor image digest. It deliberately contains no trusted path.
- The trusted side executes every submission. You never run the strategy, the
  backtest or the experiment CLI yourself, and you never see the sealed
  Discovery payload or any protected partition.

If `campaign.json` has `"status": "pending_operator"`, the operator has not
installed the campaign yet. Stop and write a note saying so.

## The loop

1. Read `campaign.json` and `SUBMISSION_FORMAT.md`.
2. Write your hypothesis, falsifiable prediction and planned strategy to
   `notes/<NNN>-plan.md` before submitting.
3. Write one submission to `inbox/<submission-id>.json` (use an ID such as
   `exp-001`; letters, digits, `.`, `_` and `-` only).
4. Wait for `outbox/<submission-id>.receipt.json`. Poll at a modest interval
   (for example every five seconds) and do not rewrite the inbox file while it
   is being processed. The trusted side moves the request to `processed/`.
5. Read the receipt and `outbox/<submission-id>.meta.json`. The meta file's
   `receipt_sha256` is the exact digest of the receipt. A missing receipt
   means the trusted operator has not processed the request yet: use a
   `status-<submission-id>.json` request (see below) rather than resubmitting.
6. Write `notes/<NNN>-<submission-id>.md` recording the receipt status, the
   metrics you actually saw, the exact `receipt_sha256`, and what the result
   means for your hypothesis.
7. Only after reading the real receipt, decide the next hypothesis. A
   follow-up uses a **new** `submission_id` and sets `parent_experiment_id` to
   the previous receipt's `experiment_id`. Never claim a result you did not
   read from a receipt.
8. Stop when the receipt reports an exhausted budget, when a request is
   rejected with no useful correction, or when the operator asks you to stop.
   At the end, write `notes/final-summary.md` with every submission ID, parent
   link, receipt digest and your stated reasoning for the follow-up.

## Reading a receipt

- `status: "succeeded"` means the trusted experiment ran on the real
  Discovery partition. `discovery.metrics`, `discovery.fills`,
  `discovery.nav` and `discovery.benchmark` are the observations. There are
  no real forward trades and no protected Validation or Final Holdout
  results.
- `status: "rejected"` or `"failed"` contains an `error` object with `code`,
  `phase`, `next_action` and `message`. Follow `next_action`: correct the
  request, resume with a `status-*.json` request, or stop.
- `budget.attempts_remaining` and `budget.execution_seconds_remaining` are the
  remaining campaign budget. Do not exceed it.
- A code starting with `RELAY_` came from the relay, not from the experiment
  protocol. Correct the request or ask the operator.
- A missing `outbox/<id>.receipt.json` is normal while the relay has not
  processed the request. Do not submit the same ID with different content.

## Request and response layout

| Path | Written by | Meaning |
| --- | --- | --- |
| `inbox/<submission-id>.json` | you | one experiment submission |
| `inbox/status-<submission-id>.json` | you | query an existing submission without spending an attempt |
| `outbox/<submission-id>.receipt.json` | relay | the trusted CLI receipt (JSON) |
| `outbox/<submission-id>.meta.json` | relay | submission sidecar: `processed_at`, `command` (logical argv with trusted paths redacted), `returncode`, `input_sha256`, `receipt_sha256`, `redacted` |
| `outbox/<submission-id>.status.meta.json` | relay | sidecar for a status query |
| `processed/<original name>` | relay | archived request |
| `notes/*.md` | you | the reasoning transcript |
| `receipts/*.json` | you | optional archived copies of receipts you cite; the relay never reads this directory |

A status request is:

```json
{"schema_version": 1, "request": "status", "submission_id": "exp-001"}
```

The relay answers it with the current receipt for `exp-001`, including
`EXECUTION_INTERRUPTED` and `NOT_FOUND` receipts. Status queries do not
consume an attempt.

## Style rules

- Every hypothesis must be falsifiable and every follow-up must cite the
  receipt that motivated it.
- Keep notes plain Markdown. Quote the receipt SHA-256 exactly as the meta
  file gives it.
- Do not paste credentials, environment variables, absolute paths or any
  trusted-side information into notes or submissions; you do not have them.
- If a file outside this directory would be needed, stop and report the
  blocker to the operator instead of trying to reach it.
"""

AGENT_PROMPT = """You are the external research model for PrajnaTrade issue #27.

Your working directory is a project-only research workspace. Read
`README.md`, `campaign.json` and `SUBMISSION_FORMAT.md` before doing anything.
You may read and write only inside this directory; never access a repository,
a `.prajnatrade` directory, a project database, a snapshot store, another
host or the network. You have no API key and no credentials, and there are no
real orders.

Run one controlled iteration: state a falsifiable hypothesis, write a strategy
submission to `inbox/<submission-id>.json`, wait for the trusted side to write
`outbox/<submission-id>.receipt.json`, read the real receipt, write your
reasoning to `notes/`, and then decide and submit one linked follow-up using a
new submission ID and `parent_experiment_id` from the first receipt. Quote the
receipt SHA-256 from the meta sidecar. Stop when the budget is exhausted and
finish with `notes/final-summary.md` listing both submissions, parent links,
receipt digests and your stated reason for the follow-up.

Do not run the experiment CLI or any strategy yourself. If `campaign.json` is
still `pending_operator`, stop and report that to the operator.
"""

SUBMISSION_FORMAT = """# Submission format (schema version 1)

Every file in `inbox/` is one JSON object. Use UTF-8, no comments, no trailing
commas and no extra fields. The trusted side validates the request; an invalid
request is rejected without spending an attempt.

## Common envelope

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `schema_version` | integer | yes | must be `1` |
| `campaign_id` | string | yes | exactly `campaign.json`'s `campaign_id` |
| `submission_id` | string | yes | unique; letters, digits, `.`, `_`, `-` |
| `hypothesis` | string | yes | non-empty, falsifiable |
| `strategy` | object | yes | declarative family or Python source (below) |
| `parameters` | object | yes | strategy parameters; `{}` is valid |
| `parent_experiment_id` | string | no | previous receipt's `experiment_id` |

A follow-up must use a new `submission_id`. Reusing an ID with different
content is a conflict and does not spend an attempt.

## Declarative strategy

```json
{
  "schema_version": 1,
  "campaign_id": "<campaign-id from campaign.json>",
  "submission_id": "exp-001",
  "hypothesis": "The equal-weight benchmark can be matched by the Declarative baseline.",
  "strategy": {"family": "equal_weight"},
  "parameters": {}
}
```

Supported families: `equal_weight`, `momentum`, `trend_filter`,
`inverse_volatility`. Parameters belong in the top-level `parameters` object
(for example `{"lookback": 20, "top_k": 1, "cash_if_negative": true}`).

## Python strategy

The campaign must have been created with the container executor
(`campaign.json` has `"executor": {"kind": "container", ...}`); a Python
submission to a declarative campaign is rejected at admission. Put the source
**inside** `strategy` and nothing else there:

```json
{
  "schema_version": 1,
  "campaign_id": "<campaign-id from campaign.json>",
  "submission_id": "exp-002",
  "hypothesis": "The strongest ETF by recent close can beat the equal-weight benchmark.",
  "strategy": {
    "family": "python",
    "source": "def strategy(history, state):\\n    latest = history.sort_values('date').drop_duplicates('symbol', keep='last')\\n    closes = dict(zip(latest['symbol'], latest['close']))\\n    ranked = sorted(ETF_SYMBOLS, key=lambda symbol: closes.get(symbol, 0.0), reverse=True)\\n    if not ranked or closes.get(ranked[0], 0.0) <= 0:\\n        return {CASH: 1.0}\\n    return {ranked[0]: 1.0}\\n"
  },
  "parameters": {},
  "parent_experiment_id": "<first receipt's experiment_id>"
}
```

The source must define `strategy(history, state)` and return a
`{symbol: weight}` mapping, or `None` for no change. `history` is a
point-in-time pandas DataFrame of the approved ETF bars; `state` is the
engine state. `ETF_SYMBOLS`, `CASH` and `parameters` are available globals.
Do not import network, process or filesystem modules; the trusted side runs
the source in the isolated executor. Do not send `source_sha256`; the trusted
side computes and records it.

`strategy.parameter_schema` is optional; if you send it, it is validated like
a declarative parameter schema. Unknown strategy keys are rejected.

## Status request

Write `inbox/status-<submission-id>.json`:

```json
{"schema_version": 1, "request": "status", "submission_id": "exp-001"}
```

The relay writes `outbox/exp-001.receipt.json` and
`outbox/exp-001.status.meta.json`. Status queries do not consume an attempt.
Do not put a status request in `inbox/<submission-id>.json`; that path is
only for submissions.

## Error receipts

A local relay rejection has `status: "rejected"` and an `error.code` starting
with `RELAY_`:

| Code | Meaning | Next action |
| --- | --- | --- |
| `RELAY_INVALID_JSON` | not a JSON object | fix the file |
| `RELAY_INVALID_SUBMISSION` | missing or malformed IDs | fix the envelope |
| `RELAY_INVALID_REQUEST` | malformed status request | fix the request |
| `RELAY_INVALID_INPUT_FILE` | inbox entry could not be read | resubmit the request |
| `RELAY_CAMPAIGN_MISMATCH` | wrong campaign ID | use `campaign.json`'s ID |
| `RELAY_AGENT_FAILED` | trusted command failed | stop and tell the operator |
| `RELAY_AGENT_TIMEOUT` | trusted command timed out | stop and tell the operator |

A protocol rejection (`BUDGET_EXHAUSTED`, `INVALID_SUBMISSION`,
`SUBMISSION_CONFLICT`, `NOT_FOUND`, `EXECUTION_INTERRUPTED`, ...) comes from
the trusted experiment CLI and carries the authoritative `next_action`.
"""

NOTE_TEMPLATE = """# Research note template

Copy this file to `notes/<NNN>-<submission-id>.md` for each step.

- Submission ID:
- Parent experiment ID (or none):
- Hypothesis:
- Falsifiable prediction:
- Strategy family and parameters:
- Receipt path and `receipt_sha256`:
- Receipt status:
- Observed metrics (quote the receipt, do not invent):
- What this means for the hypothesis:
- Reason for the next step (or why stopping):
"""


if __name__ == "__main__":
    raise SystemExit(main())
