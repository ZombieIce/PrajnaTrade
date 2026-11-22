"""Trusted-side relay for the issue #27 controlled model iteration.

The external model never gets the trusted project, the sealed snapshot store or
the ``prajnatrade-agent`` CLI.  It only gets a project-only research directory
(built by ``issue27_research_workspace.py``) and writes JSON requests into that
directory's ``inbox``.  A trusted operator process, this script, reads those
requests, validates that they address the trusted campaign, invokes the agent
CLI in a subprocess and republishes the CLI stdout as a receipt.  The trusted
strategy execution still happens inside the experiment CLI; the relay never
opens the snapshot, never executes a strategy and never writes the trusted
project database.

Layout (the research side of the contract)
------------------------------------------

``inbox/<submission-id>.json``
    A versioned experiment submission::

        {"schema_version": 1,
         "campaign_id": "<campaign id from campaign.json>",
         "submission_id": "exp-001",
         "hypothesis": "A falsifiable statement.",
         "strategy": {"family": "python", "source": "def strategy(history, state): ..."},
         "parameters": {},
         "parent_experiment_id": "<experiment id or omitted>"}

``inbox/status-<submission-id>.json``
    A status request (does not consume an attempt)::

        {"schema_version": 1, "request": "status", "submission_id": "exp-001"}

``outbox/<submission-id>.receipt.json``
    The trusted agent CLI's stdout JSON, byte-for-byte when it contains no
    trusted path.  If a trusted path is present it is replaced with
    ``<trusted>`` and the value is re-serialised.  On a local rejection the
    relay writes its own error receipt with an ``error.code`` starting with
    ``RELAY_``.

``outbox/<submission-id>.meta.json``
    Sidecar for a submission: ``processed_at`` (UTC), ``command`` (the logical
    argv with trusted paths replaced by ``<trusted>``; the trusted CLI receives
    an identical private copy of the input), ``returncode``, ``input_sha256``,
    ``receipt_sha256`` and ``redacted``.  A status query writes
    ``<submission-id>.status.meta.json`` instead so it never overwrites the
    submission sidecar.

``processed/<original file>``
    Every inbox request that reached a decision is moved here.  A repeated file
    name is preserved as ``<stem>.<payload sha8>.json``.

The relay never records environment variables, never writes a receipt that
contains a trusted absolute path and never writes into the research directory
anything except receipts, sidecars and archived requests.  ``--status`` is the
operator-side one-shot query; the model can also drop a ``status-*.json``
request into ``inbox`` so ``--once`` / ``--serve`` answers it.

Exit codes: ``0`` when the requested scan or query completed (individual
submission rejections are receipts, not relay failures), ``2`` for a relay or
usage failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_AGENT_COMMAND = f"{shlex.quote(sys.executable)} -m prajnatrade"
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]")
# Any absolute path that reaches into a trusted project's bookkeeping.
PRAJNATRADE_PATH = re.compile(r"(?<![\w<])/(?:[^\s\"'<>]+/)*\.prajnatrade(?:/[^\s\"'<>]*)?")
STATUS_FIELDS = {"schema_version", "request", "submission_id", "campaign_id"}
RELAY_ERRORS = {
    "RELAY_INVALID_JSON": "correct",
    "RELAY_INVALID_SUBMISSION": "correct",
    "RELAY_INVALID_REQUEST": "correct",
    "RELAY_CAMPAIGN_MISMATCH": "correct",
    "RELAY_INVALID_INPUT_FILE": "correct",
    "RELAY_AGENT_FAILED": "inspect",
    "RELAY_AGENT_TIMEOUT": "inspect",
    "RELAY_INVALID_RECEIPT": "inspect",
}


class RelayError(Exception):
    """A fatal relay failure: no research-side receipt can be produced."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Context:
    """Paths and identifiers that are trusted for one relay process."""

    project: Path
    research: Path
    campaign_id: str
    agent_command: tuple[str, ...]
    agent_timeout: float
    replacements: dict[str, str] = field(default_factory=dict)

    @property
    def inbox(self) -> Path:
        return self.research / "inbox"

    @property
    def outbox(self) -> Path:
        return self.research / "outbox"

    @property
    def processed(self) -> Path:
        return self.research / "processed"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _write_atomic(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _safe_stem(value: str) -> str:
    if SAFE_ID.fullmatch(value):
        return value
    cleaned = SAFE_FILENAME.sub("_", value).strip("._-")[:96]
    digest = _sha256(value.encode("utf-8"))[:16]
    return f"invalid-{digest}-{cleaned}" if cleaned else f"invalid-{digest}"


def _redact_text(text: str, replacements: dict[str, str]) -> str:
    # Replace any absolute path ending in trusted bookkeeping first, so a
    # known prefix plus "/.prajnatrade/..." becomes one "<trusted>" token.
    text = PRAJNATRADE_PATH.sub("<trusted>", text)
    for source in sorted(replacements, key=len, reverse=True):
        if source:
            text = text.replace(source, replacements[source])
    return text


def redact_value(value: object, replacements: dict[str, str]) -> object:
    """Recursively replace trusted paths in a JSON-compatible value."""
    if isinstance(value, str):
        return _redact_text(value, replacements)
    if isinstance(value, list):
        return [redact_value(item, replacements) for item in value]
    if isinstance(value, dict):
        return {
            _redact_text(str(key), replacements): redact_value(item, replacements)
            for key, item in value.items()
        }
    return value


def _needs_redaction(text: str, replacements: dict[str, str]) -> bool:
    return any(source and source in text for source in replacements) or bool(
        PRAJNATRADE_PATH.search(text)
    )


def _error_receipt(
    *,
    code: str,
    message: str,
    submission_id: str,
    campaign_id: str | None,
    expected_campaign_id: str | None = None,
) -> bytes:
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "rejected",
        "submission_id": submission_id,
        "campaign_id": campaign_id,
        "error": {
            "code": code,
            "phase": "relay",
            "next_action": RELAY_ERRORS.get(code, "inspect"),
            "message": message,
        },
    }
    if expected_campaign_id is not None:
        receipt["expected_campaign_id"] = expected_campaign_id
    return _json_bytes(redact_value(receipt, {}))


def _validate_project(project: Path) -> None:
    database = project / ".prajnatrade" / "project.sqlite"
    if not database.is_file():
        raise RelayError(
            "RELAY_PROJECT_MISSING",
            "--project must be an initialized trusted project with a campaign.",
        )


def _validate_research(project: Path, research: Path) -> None:
    if not research.is_dir():
        raise RelayError("RELAY_RESEARCH_DIR_MISSING", "--research-dir must be an existing directory.")
    if (research / "inbox").is_symlink() or not (research / "inbox").is_dir():
        raise RelayError("RELAY_RESEARCH_DIR_UNSAFE", "--research-dir must contain a real inbox/ directory.")
    if research == project or research.is_relative_to(project) or project.is_relative_to(research):
        raise RelayError(
            "RELAY_RESEARCH_DIR_UNSAFE",
            "The research directory must be separate from the trusted project.",
        )
    if (research / ".prajnatrade").exists() or any(research.rglob("project.sqlite")):
        raise RelayError(
            "RELAY_RESEARCH_DIR_UNSAFE",
            "The research directory contains trusted project data.",
        )
    for name in ("inbox", "outbox", "processed"):
        path = research / name
        if path.is_symlink():
            raise RelayError(
                "RELAY_RESEARCH_DIR_UNSAFE",
                f"The research {name}/ directory must not be a symbolic link.",
            )
        if path.exists():
            if not path.is_dir():
                raise RelayError(
                    "RELAY_RESEARCH_DIR_UNSAFE",
                    f"The research {name}/ entry is not a directory.",
                )
            if not path.resolve().is_relative_to(research):
                raise RelayError(
                    "RELAY_RESEARCH_DIR_UNSAFE",
                    f"The research {name}/ directory escapes the research directory.",
                )


def _campaign_ids(project: Path) -> list[str]:
    database = project / ".prajnatrade" / "project.sqlite"
    try:
        with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=30)) as connection:
            rows = connection.execute("SELECT campaign_id FROM campaigns ORDER BY rowid").fetchall()
    except sqlite3.Error as error:
        raise RelayError(
            "RELAY_PROJECT_UNREADABLE",
            f"The trusted project database could not be read: {type(error).__name__}.",
        ) from error
    return [str(row[0]) for row in rows]


def _public_campaign(research: Path) -> dict[str, Any] | None:
    path = research / "campaign.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _resolve_campaign(project: Path, research: Path, explicit: str | None) -> str:
    ids = _campaign_ids(project)
    if explicit is not None:
        if explicit not in ids:
            raise RelayError(
                "RELAY_CAMPAIGN_UNKNOWN",
                "The explicit campaign ID is not present in the trusted project.",
            )
        return explicit
    if len(ids) == 1:
        resolved = ids[0]
    elif not ids:
        raise RelayError(
            "RELAY_CAMPAIGN_MISSING",
            "The trusted project has no campaign; run issue27_setup_campaign.py first.",
        )
    else:
        public = _public_campaign(research)
        selected = public.get("campaign_id") if public else None
        if not isinstance(selected, str) or selected not in ids:
            raise RelayError(
                "RELAY_CAMPAIGN_AMBIGUOUS",
                "The trusted project has several campaigns; pass --campaign-id.",
            )
        resolved = selected
    public = _public_campaign(research)
    if public is not None and public.get("campaign_id") not in (None, resolved):
        print(
            "relay: warning: research campaign.json names a different campaign than the relay target",
            file=sys.stderr,
        )
    return resolved


def _run_agent(argv: tuple[str, ...], project: Path, timeout: float) -> tuple[int | None, str, str, str | None]:
    """Run the trusted agent CLI and return (returncode, stdout, stderr, failure)."""
    environment = os.environ.copy()
    try:
        completed = subprocess.run(
            list(argv),
            cwd=str(project),
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        return None, _decode_output(error.stdout), _decode_output(error.stderr), "timeout"
    except OSError as error:
        return None, "", "", f"{type(error).__name__}"
    return completed.returncode, completed.stdout, completed.stderr, None


def _decode_output(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else ""


def _receipt_bytes(stdout: str, replacements: dict[str, str]) -> tuple[bytes, bool]:
    """Return (receipt bytes, redacted) for the agent CLI's stdout."""
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise RelayError("RELAY_INVALID_RECEIPT", "The trusted command did not print JSON.") from error
    if not isinstance(value, dict):
        raise RelayError("RELAY_INVALID_RECEIPT", "The trusted command did not print a JSON object.")
    if _needs_redaction(stdout, replacements):
        try:
            return _json_bytes(redact_value(value, replacements)), True
        except (TypeError, ValueError) as error:
            raise RelayError(
                "RELAY_INVALID_RECEIPT", "The trusted command printed non-JSON values."
            ) from error
    return stdout.encode("utf-8"), False


def _log(message: str) -> None:
    print(f"relay: {message}", file=sys.stderr)


def _summary_for_receipt(
    receipt: bytes,
    *,
    submission_id: str,
    campaign_id: str | None,
    receipt_name: str,
    receipt_sha256: str,
    kind: str,
) -> dict[str, Any]:
    """Build a small, model-readable summary of a trusted receipt.

    The full receipt stays in ``<stem>.receipt.json``; this summary carries only
    the decision-relevant fields (status, budget, metrics, fills, errors and the
    container-isolation evidence).  It never adds information that is not in the
    receipt, and it carries the receipt SHA-256 so a note can cite the source.
    """
    try:
        value = json.loads(receipt)
    except (json.JSONDecodeError, UnicodeDecodeError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    discovery = value.get("discovery")
    discovery = discovery if isinstance(discovery, dict) else {}
    metrics = discovery.get("metrics")
    benchmark = discovery.get("benchmark")
    gaps = discovery.get("gaps")
    gaps = gaps if isinstance(gaps, dict) else {}
    fills = discovery.get("fills")
    fills = fills if isinstance(fills, list) else []
    signals = discovery.get("signals")
    signals = signals if isinstance(signals, list) else []
    executor = value.get("executor")
    executor = executor if isinstance(executor, dict) else {}
    invocations = executor.get("invocations")
    invocations = invocations if isinstance(invocations, list) else []
    isolation = [
        inv.get("isolation_checks")
        for inv in invocations
        if isinstance(inv, dict) and isinstance(inv.get("isolation_checks"), dict)
    ]
    strategy = value.get("strategy")
    strategy = strategy if isinstance(strategy, dict) else {}
    summary = {
        "schema_version": SCHEMA_VERSION,
        "kind": "receipt_summary",
        "submission_id": submission_id,
        "campaign_id": campaign_id,
        "receipt_file": receipt_name,
        "receipt_sha256": receipt_sha256,
        "receipt_kind": kind,
        "status": value.get("status"),
        "experiment_id": value.get("experiment_id"),
        "parent_experiment_id": value.get("parent_experiment_id"),
        "data_kind": value.get("data_kind"),
        "budget": value.get("budget"),
        "hypothesis": value.get("hypothesis"),
        "strategy": {
            "family": strategy.get("family"),
            "source_sha256": strategy.get("source_sha256"),
        },
        "discovery": {
            "start_date": discovery.get("start_date"),
            "end_date": discovery.get("end_date"),
            "metrics": metrics,
            "benchmark": benchmark,
            "return_delta": discovery.get("return_delta"),
            "signal_count": len(signals),
            "fills_count": len(fills),
            "fills": fills[:20],
            "gaps_counts": {
                key: len(item) if isinstance(item, list) else item
                for key, item in gaps.items()
            },
            "equity_curve_points": len(discovery.get("equity_curve") or []),
        },
        "diagnostics": value.get("diagnostics"),
        "error": value.get("error"),
        "executor": {
            "kind": executor.get("kind"),
            "image": executor.get("image"),
            "invocation_count": executor.get("invocation_count"),
            "evidence_sha256": executor.get("evidence_sha256"),
            "isolation_checks_all": (
                all(check is True for item in isolation for check in item.values())
                if isolation
                else None
            ),
        },
        "note": (
            "Derived from the trusted receipt for the research model. Cite "
            "receipt_sha256; the full record is receipt_file."
        ),
    }
    return summary


def _emit(
    context: Context,
    *,
    submission_id: str,
    campaign_id: str | None,
    receipt: bytes,
    command: list[str],
    returncode: int | None,
    input_sha256: str | None,
    kind: str,
    redacted: bool = False,
    meta_name: str | None = None,
) -> Path:
    stem = _safe_stem(submission_id)
    receipt_name = f"{stem}.receipt.json"
    _write_atomic(context.outbox / receipt_name, receipt)
    meta = {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "submission_id": submission_id,
        "campaign_id": campaign_id,
        "processed_at": _utc_now(),
        "command": redact_value(command, context.replacements),
        "returncode": returncode,
        "input_sha256": input_sha256,
        "receipt_sha256": _sha256(receipt),
        "receipt_file": receipt_name,
        "redacted": redacted,
    }
    sidecar = meta_name if meta_name is not None else f"{stem}.meta.json"
    _write_atomic(context.outbox / sidecar, _json_bytes(meta))
    summary_name = (
        f"{stem}.status.summary.json"
        if meta_name is not None and ".status." in meta_name
        else f"{stem}.summary.json"
    )
    _write_atomic(
        context.outbox / summary_name,
        _json_bytes(
            _summary_for_receipt(
                receipt,
                submission_id=submission_id,
                campaign_id=campaign_id,
                receipt_name=receipt_name,
                receipt_sha256=meta["receipt_sha256"],
                kind=kind,
            )
        ),
    )
    _log(f"{kind} {submission_id}: returncode={returncode} receipt={receipt_name}")
    return context.outbox / receipt_name


def _archive(context: Context, path: Path, raw: bytes) -> Path:
    target = context.processed / path.name
    if target.is_symlink():
        target.unlink()
    if target.exists():
        if target.is_file() and target.read_bytes() == raw:
            path.unlink()
            return target
        target = context.processed / f"{path.stem}.{_sha256(raw)[:8]}{path.suffix}"
    os.replace(path, target)
    return target


def _reject(
    context: Context,
    path: Path,
    raw: bytes,
    *,
    submission_id: str,
    submitted_campaign_id: str | None,
    code: str,
    message: str,
    command: list[str] | None = None,
    returncode: int | None = None,
    meta_name: str | None = None,
    kind: str = "rejected",
) -> bool:
    expected = context.campaign_id if code == "RELAY_CAMPAIGN_MISMATCH" else None
    receipt = _error_receipt(
        code=code,
        message=message,
        submission_id=submission_id,
        campaign_id=submitted_campaign_id or context.campaign_id,
        expected_campaign_id=expected,
    )
    _emit(
        context,
        submission_id=submission_id,
        campaign_id=submitted_campaign_id or context.campaign_id,
        receipt=receipt,
        command=command or [],
        returncode=returncode,
        input_sha256=_sha256(raw),
        kind=kind,
        meta_name=meta_name,
    )
    _archive(context, path, raw)
    return False


def _dispatch(
    context: Context,
    *,
    path: Path,
    raw: bytes,
    submission_id: str,
    logical_command: list[str],
    real_command: list[str],
    kind: str,
    status: bool,
) -> bool:
    returncode, stdout, stderr, failure = _run_agent(
        tuple(real_command), context.project, context.agent_timeout
    )
    if failure == "timeout":
        return _reject(
            context,
            path,
            raw,
            submission_id=submission_id,
            submitted_campaign_id=context.campaign_id,
            code="RELAY_AGENT_TIMEOUT",
            message="The trusted experiment command exceeded the relay timeout.",
            command=logical_command,
            meta_name=f"{_safe_stem(submission_id)}.status.meta.json" if status else None,
            kind="status" if status else "rejected",
        )
    if failure is not None:
        return _reject(
            context,
            path,
            raw,
            submission_id=submission_id,
            submitted_campaign_id=context.campaign_id,
            code="RELAY_AGENT_FAILED",
            message="The trusted experiment command could not be started.",
            command=logical_command,
            meta_name=f"{_safe_stem(submission_id)}.status.meta.json" if status else None,
            kind="status" if status else "rejected",
        )
    try:
        receipt, redacted = _receipt_bytes(stdout, context.replacements)
    except RelayError:
        if stderr:
            _log(f"{kind} {submission_id}: stderr (trusted side only): {stderr.strip()[:2000]}")
        return _reject(
            context,
            path,
            raw,
            submission_id=submission_id,
            submitted_campaign_id=context.campaign_id,
            code="RELAY_AGENT_FAILED",
            message=f"The trusted experiment command exited with status {returncode} without a receipt.",
            command=logical_command,
            returncode=returncode,
            meta_name=f"{_safe_stem(submission_id)}.status.meta.json" if status else None,
            kind="status" if status else "rejected",
        )
    _emit(
        context,
        submission_id=submission_id,
        campaign_id=context.campaign_id,
        receipt=receipt,
        command=logical_command,
        returncode=returncode,
        input_sha256=_sha256(raw),
        kind=kind,
        redacted=redacted,
        meta_name=f"{_safe_stem(submission_id)}.status.meta.json" if status else None,
    )
    _archive(context, path, raw)
    return not status


def _campaign_mismatch(
    payload: dict[str, Any], context: Context, *, optional: bool = False
) -> tuple[str, str | None] | None:
    submitted = payload.get("campaign_id")
    if optional and submitted is None:
        return None
    if isinstance(submitted, str) and submitted == context.campaign_id:
        return None
    submission_id = payload.get("submission_id")
    if not isinstance(submission_id, str) or not submission_id.strip():
        submission_id = ""
    return submission_id, submitted if isinstance(submitted, str) else None


def _process_status_request(path: Path, raw: bytes, payload: dict[str, Any], context: Context) -> bool:
    stem = path.stem
    if set(payload) - STATUS_FIELDS or payload.get("request") != "status":
        return _reject(
            context,
            path,
            raw,
            submission_id=str(payload.get("submission_id") or stem),
            submitted_campaign_id=None,
            code="RELAY_INVALID_REQUEST",
            message="A status request has fields: schema_version, request, submission_id, campaign_id.",
            meta_name=f"{_safe_stem(str(payload.get('submission_id') or stem))}.status.meta.json",
            kind="status",
        )
    submission_id = payload.get("submission_id")
    if not isinstance(submission_id, str) or not submission_id.strip():
        return _reject(
            context,
            path,
            raw,
            submission_id=stem,
            submitted_campaign_id=None,
            code="RELAY_INVALID_REQUEST",
            message="A status request needs a non-empty submission_id.",
            meta_name=f"{_safe_stem(stem)}.status.meta.json",
            kind="status",
        )
    mismatch = _campaign_mismatch(payload, context, optional=True)
    if mismatch is not None:
        return _reject(
            context,
            path,
            raw,
            submission_id=submission_id,
            submitted_campaign_id=mismatch[1],
            code="RELAY_CAMPAIGN_MISMATCH",
            message="The request campaign_id does not match the trusted campaign.",
            meta_name=f"{_safe_stem(submission_id)}.status.meta.json",
            kind="status",
        )
    real = [
        *context.agent_command,
        "experiment",
        "status",
        "--project",
        str(context.project),
        "--campaign-id",
        context.campaign_id,
        "--submission-id",
        submission_id,
    ]
    logical = [
        *context.agent_command,
        "experiment",
        "status",
        "--project",
        str(context.project),
        "--campaign-id",
        context.campaign_id,
        "--submission-id",
        submission_id,
    ]
    return _dispatch(
        context,
        path=path,
        raw=raw,
        submission_id=submission_id,
        logical_command=logical,
        real_command=real,
        kind="status",
        status=True,
    )


def _process_submission(path: Path, raw: bytes, payload: dict[str, Any], context: Context) -> bool:
    submission_id = payload.get("submission_id")
    if not isinstance(submission_id, str) or not submission_id.strip():
        return _reject(
            context,
            path,
            raw,
            submission_id=path.stem,
            submitted_campaign_id=None,
            code="RELAY_INVALID_SUBMISSION",
            message="A submission needs a non-empty submission_id.",
        )
    mismatch = _campaign_mismatch(payload, context)
    if mismatch is not None:
        return _reject(
            context,
            path,
            raw,
            submission_id=submission_id,
            submitted_campaign_id=mismatch[1],
            code="RELAY_CAMPAIGN_MISMATCH",
            message="The submission campaign_id does not match the trusted campaign.",
        )
    logical = [
        *context.agent_command,
        "experiment",
        "submit",
        "--project",
        str(context.project),
        "--input",
        str(path),
    ]
    with tempfile.TemporaryDirectory(prefix="issue27-relay-") as directory:
        copy = Path(directory) / "submission.json"
        copy.write_bytes(raw)
        real = [
            *context.agent_command,
            "experiment",
            "submit",
            "--project",
            str(context.project),
            "--input",
            str(copy),
        ]
        return _dispatch(
            context,
            path=path,
            raw=raw,
            submission_id=submission_id,
            logical_command=logical,
            real_command=real,
            kind="submit",
            status=False,
        )


def _process_file(path: Path, context: Context) -> bool:
    """Process one inbox request; return True when it dispatched a submission."""
    try:
        mode = path.lstat().st_mode
    except OSError:
        return False
    if not stat.S_ISREG(mode):
        path.unlink(missing_ok=True)
        _log(f"ignored non-regular inbox entry {path.name}")
        return False
    try:
        raw = path.read_bytes()
    except OSError:
        return _reject(
            context,
            path,
            b"",
            submission_id=path.stem,
            submitted_campaign_id=None,
            code="RELAY_INVALID_INPUT_FILE",
            message="The inbox request could not be read.",
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _reject(
            context,
            path,
            raw,
            submission_id=path.stem,
            submitted_campaign_id=None,
            code="RELAY_INVALID_JSON",
            message="The inbox request is not valid UTF-8 JSON.",
        )
    if not isinstance(payload, dict):
        return _reject(
            context,
            path,
            raw,
            submission_id=path.stem,
            submitted_campaign_id=None,
            code="RELAY_INVALID_SUBMISSION",
            message="The inbox request must be a JSON object.",
        )
    if payload.get("request") == "status":
        return _process_status_request(path, raw, payload, context)
    return _process_submission(path, raw, payload, context)


def _prepare_directories(context: Context) -> None:
    context.outbox.mkdir(exist_ok=True)
    context.processed.mkdir(exist_ok=True)


def _scan(context: Context, max_submissions: int, dispatched: int) -> tuple[bool, int]:
    """Process a sorted inbox snapshot; return (limit_reached, dispatched total)."""
    requests = sorted(
        (
            path
            for path in context.inbox.glob("*.json")
            if not path.is_dir() and not path.name.startswith(".")
        ),
        key=lambda path: path.name,
    )
    for path in requests:
        if dispatched >= max_submissions:
            return True, dispatched
        try:
            if _process_file(path, context):
                dispatched += 1
        except RelayError as error:
            _log(f"{path.name}: {error.code}: {error.message}")
        except (OSError, TypeError, ValueError) as error:
            _log(f"{path.name}: {type(error).__name__}")
    return dispatched >= max_submissions, dispatched


def _run_status(context: Context, submission_id: str) -> None:
    if not SAFE_ID.fullmatch(submission_id):
        raise RelayError("RELAY_INVALID_REQUEST", "--status needs a safe submission ID.")
    command = [
        *context.agent_command,
        "experiment",
        "status",
        "--project",
        str(context.project),
        "--campaign-id",
        context.campaign_id,
        "--submission-id",
        submission_id,
    ]
    returncode, stdout, stderr, failure = _run_agent(
        tuple(command), context.project, context.agent_timeout
    )
    if failure is not None or (returncode is not None and returncode not in (0, 2)):
        if stderr:
            _log(f"status {submission_id}: stderr (trusted side only): {stderr.strip()[:2000]}")
        raise RelayError(
            "RELAY_AGENT_FAILED",
            f"The trusted status command failed (timeout={failure == 'timeout'}, returncode={returncode}).",
        )
    try:
        receipt, redacted = _receipt_bytes(stdout, context.replacements)
    except RelayError as error:
        if stderr:
            _log(f"status {submission_id}: stderr (trusted side only): {stderr.strip()[:2000]}")
        raise RelayError("RELAY_INVALID_RECEIPT", error.message) from error
    _emit(
        context,
        submission_id=submission_id,
        campaign_id=context.campaign_id,
        receipt=receipt,
        command=command,
        returncode=returncode,
        input_sha256=None,
        kind="status",
        redacted=redacted,
        meta_name=f"{_safe_stem(submission_id)}.status.meta.json",
    )


def _build_context(args: argparse.Namespace) -> Context:
    project = args.project.expanduser().resolve()
    research = args.research_dir.expanduser().resolve()
    _validate_project(project)
    _validate_research(project, research)
    agent_command = tuple(shlex.split(args.agent_command))
    if not agent_command:
        raise RelayError("RELAY_INVALID_ARGUMENT", "--agent-command must not be empty.")
    campaign_id = _resolve_campaign(project, research, args.campaign_id)
    replacements: dict[str, str] = {}
    for redacted in args.redact_path:
        resolved = redacted.expanduser().resolve()
        replacements[str(resolved)] = "<trusted>"
        replacements[resolved.as_uri()] = "<trusted>"
    replacements[str(project)] = "<trusted>"
    replacements[project.as_uri()] = "<trusted>"
    replacements[str(research)] = "<research>"
    replacements[research.as_uri()] = "<research>"
    context = Context(
        project=project,
        research=research,
        campaign_id=campaign_id,
        agent_command=agent_command,
        agent_timeout=args.agent_timeout,
        replacements=replacements,
    )
    _prepare_directories(context)
    return context


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="issue27_relay.py",
        description="Trusted relay for the issue #27 external-model research workspace.",
    )
    parser.add_argument("--project", required=True, type=Path, help="initialized trusted project")
    parser.add_argument("--research-dir", required=True, type=Path, help="project-only research directory")
    parser.add_argument("--campaign-id", help="trusted campaign ID (required if the project has several)")
    parser.add_argument(
        "--redact-path",
        action="append",
        default=[],
        type=Path,
        help="additional trusted absolute path to replace with <trusted> in receipts (repeatable)",
    )
    parser.add_argument(
        "--agent-command",
        default=DEFAULT_AGENT_COMMAND,
        help="trusted agent CLI command, default: python -m prajnatrade",
    )
    parser.add_argument(
        "--agent-timeout", type=float, default=3600.0, help="per-command timeout in seconds"
    )
    parser.add_argument(
        "--max-submissions",
        type=int,
        default=10,
        help="submission dispatches this relay process may perform (status queries do not count)",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="process the current inbox and exit")
    mode.add_argument("--serve", action="store_true", help="poll the inbox until --max-submissions")
    mode.add_argument("--status", metavar="SUBMISSION_ID", help="query one submission once and exit")
    parser.add_argument(
        "--poll-interval", type=float, default=2.0, help="seconds between inbox scans in --serve mode"
    )
    return parser


def _validate_arguments(args: argparse.Namespace) -> None:
    if args.max_submissions < 0:
        raise RelayError("RELAY_INVALID_ARGUMENT", "--max-submissions must not be negative.")
    if args.agent_timeout <= 0:
        raise RelayError("RELAY_INVALID_ARGUMENT", "--agent-timeout must be positive.")
    if args.serve and args.poll_interval <= 0:
        raise RelayError("RELAY_INVALID_ARGUMENT", "--poll-interval must be positive.")
    if not args.once and not args.serve and args.status is None:
        raise RelayError("RELAY_INVALID_ARGUMENT", "Choose --once, --serve or --status.")


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        _validate_arguments(args)
        context = _build_context(args)
        if args.status is not None:
            _run_status(context, args.status)
            return 0
        dispatched = 0
        if args.once:
            limited, _ = _scan(context, args.max_submissions, dispatched)
            if limited:
                _log(f"submission limit {args.max_submissions} reached; inbox not drained")
            return 0
        while True:
            limited, dispatched = _scan(context, args.max_submissions, dispatched)
            if limited:
                _log(f"submission limit {args.max_submissions} reached; stopping")
                return 0
            time.sleep(args.poll_interval)
    except RelayError as error:
        print(f"relay: {error.code}: {error.message}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
