"""Human approval, immutable release artifacts, and tracked-version lifecycle (#30).

A release is a write-once, content-addressed artifact for one frozen candidate
and plan. Legacy plans require a distinct human approval and enablement event.
Research Holdout plans use a pre-read authorization which takes effect after
the two selected protected stages have passed and verified evidence is stored.

Enablement, pause, resume and retirement are append-only events effective on
registered after-close signal dates. Replays use the state registered for the
signal date.

Store layout (the same trusted store used by ``protected_eval``)::

    <store>/releases/<release_id>/artifact.json                  # immutable (0444)
    <store>/releases/<release_id>/events/<seq>.json              # append-only (0444)
    <store>/releases/<release_id>/dispositions/<revision_id>.json           # #30 direct (immutable)
    <store>/releases/<release_id>/dispositions/<revision-id>-<seq>-<id>.json  # #31 write-once
    <store>/audit/<audit_id>.json                                # refused approvals/events

``release_id = sha256(canonical_json({"candidate_hash", "plan_hash",
"release": "release-v1"}))``; the artifact stores its own ``artifact_hash``
(the SHA-256 of the artifact without that field), which every fetch verifies.
"""

from __future__ import annotations

import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from prajnatrade import dividend, protected_eval, shadow_calendar, snapshot
from prajnatrade.snapshot_baseline import CN_TZ

SCHEMA_VERSION = 1
SCOPE = "synthetic"
RELEASE_VERSION = "release-v1"
RESEARCH_HOLDOUT = "research_holdout"
ARTIFACT_KIND = "immutable_release_artifact"
STATUS_KIND = "immutable_release_status"
REJECTION_KIND = "immutable_release_rejection"
REJECTION_STATUS_KIND = "immutable_release_rejection_status"
REJECTION_STATEMENT = (
    "This immutable record is the human admission decision for this frozen "
    "candidate and release version: the reviewer explicitly rejected it. It "
    "creates no release artifact, no lifecycle event and no tracking. A later "
    "approval for the same candidate and plan is refused; only a new candidate "
    "and a new frozen plan can be approved."
)
STAGES = protected_eval.STAGES
ACTIONS = ("enable", "pause", "resume", "retire")
STATE_FOR_ACTION = {
    "enable": "enabled",
    "pause": "paused",
    "resume": "enabled",
    "retire": "retired",
}
#: ``enable`` is only legal from the never-enabled published state (an event
#: timeline that has no entries at all).
ALLOWED_FROM = {
    "enable": frozenset({"published"}),
    "pause": frozenset({"enabled"}),
    "resume": frozenset({"paused"}),
    "retire": frozenset({"published", "enabled", "paused"}),
}
INVALIDATING_IMPACTS = frozenset({"pending_review", "no_longer_supported"})
NOT_APPLICABLE_IMPACT = "not_applicable"
DISPOSITION_IMPACTS = frozenset(
    {"sustained", NOT_APPLICABLE_IMPACT, *INVALIDATING_IMPACTS}
)
#: The impacts an operator may record explicitly; ``not_applicable`` is derived
#: from the release's artifact, never hand-entered.
DISPOSITION_CHOICES = ("sustained", "pending_review", "no_longer_supported")
DISPOSITION_KIND = "immutable_impact_disposition"
DISPOSITION_RESULT_KIND = "release_impact_disposition_result"
DISPOSITION_VERSION = "impact-disposition-v1"
IMPLICATION_FOR_IMPACT = {
    "sustained": "evidence_still_supported",
    "pending_review": "pending_review_pause_required",
    "no_longer_supported": "no_longer_supported_cannot_resume",
    NOT_APPLICABLE_IMPACT: "release_not_exposed_no_action",
}

ADMISSION_STATEMENT = (
    "This artifact is the human admission decision for this frozen candidate and "
    "release version, recorded over the stored synthetic protected-evaluation "
    "evidence. It is engineering evidence only: it does not prove real-market "
    "performance, does not validate a real strategy, and does not enable "
    "tracking. Enablement is a separate operator lifecycle event registered at "
    "a pre-registered after-close signal date."
)

CONDITIONAL_ADMISSION_STATEMENT = (
    "The reviewer authorized this frozen candidate and its scheduled Shadow Live "
    "enablement before Final Holdout. The trusted passing evidence activated that decision."
)

_RELEASE_ID = re.compile(r"[0-9a-f]{64}")


class ReleaseRejection(Exception):
    """A machine-readable release refusal (never a strategy failure)."""

    def __init__(
        self,
        code: str,
        phase: str,
        next_action: str,
        message: str,
        *,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.details = {
            "code": code,
            "phase": phase,
            "next_action": next_action,
            "message": message,
        }
        self.context = dict(context or {})


def _reject(
    code: str, phase: str, next_action: str, message: str
) -> ReleaseRejection:
    return ReleaseRejection(code, phase, next_action, message)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _hash(value: object) -> str:
    return snapshot._hash(value)


def _bytes(value: object) -> bytes:
    return snapshot._bytes(value)


def _store(path: str | Path) -> Path:
    return protected_eval._store(Path(path))


def _write_immutable(path: Path, record: dict[str, Any], *, mode: int = 0o444) -> bool:
    """Create one immutable file; return False when it already exists."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(dir=path.parent) as staged:
        staged.write(_bytes(record))
        staged.flush()
        os.fsync(staged.fileno())
        os.chmod(staged.name, mode)
        try:
            os.link(staged.name, path)
        except FileExistsError:
            return False
    return True


def _release_id(candidate_hash: object, plan_hash: object) -> str:
    """The unique content address of one (candidate, plan) release version."""
    if not isinstance(candidate_hash, str) or not isinstance(plan_hash, str):
        return ""
    return _hash(
        {
            "candidate_hash": candidate_hash,
            "plan_hash": plan_hash,
            "release": RELEASE_VERSION,
        }
    )


def _check_release_id(release_id: object) -> str:
    if not isinstance(release_id, str) or not _RELEASE_ID.fullmatch(release_id):
        raise _reject(
            "RELEASE_NOT_FOUND",
            "fetch",
            "check_id",
            "Unknown release ID; release IDs are content addresses.",
        )
    return release_id


def _release_dir(store: Path, release_id: str) -> Path:
    return store / "releases" / _check_release_id(release_id)


def _artifact_location(store: Path, release_id: str) -> Path:
    return _release_dir(store, release_id) / "artifact.json"


def _artifact_bytes(artifact: dict[str, Any]) -> str:
    return _hash({key: value for key, value in artifact.items() if key != "artifact_hash"})


def _verified_artifact(path: Path, release_id: str) -> dict[str, Any]:
    """Load and fully verify one artifact; never fall back to another version."""
    try:
        return _load_verified_artifact(path, release_id)
    except ReleaseRejection as error:
        error.context.setdefault("release_id", release_id)
        error.context.setdefault("release_created", False)
        error.context.setdefault("opened_protected_source", False)
        error.context.setdefault("query_debited", False)
        raise


def _load_verified_artifact(path: Path, release_id: str) -> dict[str, Any]:
    _check_release_id(release_id)
    if not path.is_file():
        raise _reject(
            "RELEASE_NOT_FOUND",
            "fetch",
            "check_id",
            "No release artifact is stored for this release ID; a missing "
            "artifact never falls back to another version or a workspace copy.",
        )
    try:
        artifact = snapshot._read_json(path)
    except (OSError, UnicodeError, ValueError) as error:
        raise _reject(
            "RELEASE_INTEGRITY_FAILED",
            "fetch",
            "inspect",
            f"The stored release artifact is unreadable: {error}",
        ) from error
    if not isinstance(artifact, dict):
        raise _reject(
            "RELEASE_INTEGRITY_FAILED",
            "fetch",
            "inspect",
            "The stored release artifact is not an object.",
        )
    stored_hash = artifact.get("artifact_hash")
    if not isinstance(stored_hash, str) or stored_hash != _artifact_bytes(artifact):
        raise _reject(
            "RELEASE_INTEGRITY_FAILED",
            "fetch",
            "inspect",
            "The release artifact failed its content-address check "
            "(artifact_hash missing or mismatched).",
        )
    if artifact.get("release_id") != release_id:
        raise _reject(
            "RELEASE_INTEGRITY_FAILED",
            "fetch",
            "inspect",
            "The release artifact is bound to a different release ID.",
        )
    if artifact.get("release_version") != RELEASE_VERSION:
        raise _reject(
            "RELEASE_INTEGRITY_FAILED",
            "fetch",
            "inspect",
            "The release artifact uses an unsupported release version.",
        )
    if (
        _release_id(artifact.get("candidate_hash"), artifact.get("plan_id"))
        != release_id
    ):
        raise _reject(
            "RELEASE_INTEGRITY_FAILED",
            "fetch",
            "inspect",
            "The release ID is not the content address of the bound candidate "
            "and plan.",
        )
    scope = artifact.get("scope")
    if scope not in shadow_calendar.DATA_KINDS or (
        scope != SCOPE and artifact.get("workflow") != RESEARCH_HOLDOUT
    ):
        raise _reject(
            "RELEASE_INTEGRITY_FAILED", "fetch", "inspect",
            "The stored release uses a data scope unsupported by its admission rules.",
        )
    if artifact.get("workflow") == RESEARCH_HOLDOUT:
        terms = artifact.get("admission")
        try:
            calendar = shadow_calendar.validate_calendar(terms["calendar"])
            if (
                terms["automatic_enable"] is not True
                or calendar["data_kind"] != scope
                or terms["calendar_hash"] != _hash(calendar)
                or terms["effective_signal_date"] not in calendar["sessions"]
                or terms["authorization_id"] != artifact["approval"]["authorization_id"]
            ):
                raise ValueError("Automatic admission bindings differ.")
        except (KeyError, TypeError, ValueError) as error:
            raise _reject(
                "RELEASE_INTEGRITY_FAILED", "fetch", "inspect",
                f"The stored conditional admission is incomplete: {error}",
            ) from error
    return artifact


def _existing_artifact(store: Path, release_id: str) -> Path | None:
    path = _artifact_location(store, release_id)
    return path if path.is_file() else None


# ---------------------------------------------------------------------------
# Explicit human rejection (#30)
# ---------------------------------------------------------------------------


def _rejection_location(store: Path, release_id: str) -> Path:
    _check_release_id(release_id)
    return store / "rejections" / f"{release_id}.json"


def _load_rejection(store: Path, release_id: str) -> dict[str, Any] | None:
    """Load and content-address-check one immutable rejection record."""
    path = _rejection_location(store, release_id)
    if not path.is_file():
        return None
    try:
        record = snapshot._read_json(path)
    except (OSError, UnicodeError, ValueError) as error:
        raise _reject(
            "RELEASE_INTEGRITY_FAILED",
            "rejection",
            "inspect",
            f"The stored rejection record is unreadable: {error}",
        ) from error
    if not isinstance(record, dict):
        raise _reject(
            "RELEASE_INTEGRITY_FAILED",
            "rejection",
            "inspect",
            "The stored rejection record is not an object.",
        )
    body = {key: value for key, value in record.items() if key != "rejection_id"}
    if record.get("rejection_id") != _hash(body):
        raise _reject(
            "RELEASE_INTEGRITY_FAILED",
            "rejection",
            "inspect",
            "The stored rejection record failed its content-address check.",
        )
    if record.get("decision") != "rejected" or record.get("release_id") != release_id:
        raise _reject(
            "RELEASE_INTEGRITY_FAILED",
            "rejection",
            "inspect",
            "The stored rejection record is not bound to this release ID.",
        )
    if (
        _release_id(record.get("candidate_hash"), record.get("plan_id"))
        != release_id
    ):
        raise _reject(
            "RELEASE_INTEGRITY_FAILED",
            "rejection",
            "inspect",
            "The release ID is not the content address of the rejected "
            "candidate and plan.",
        )
    return record


def _rejection_ids(store: Path) -> list[str]:
    directory = store / "rejections"
    if not directory.is_dir():
        return []
    ids = []
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.suffix == ".json" and _RELEASE_ID.fullmatch(path.stem):
            ids.append(path.stem)
    return ids


def _review_references(store: Path, plan: dict[str, Any]) -> dict[str, Any]:
    """Bind the evidence that existed when the human decision was recorded.

    This only reads the trusted plan, query ledger and immutable evaluation
    records.  It opens no sealed partition, debits no query and never turns a
    missing or mismatched record into evidence.
    """
    references: dict[str, Any] = {}
    for stage in STAGES:
        label = "Validation" if stage == protected_eval.VALIDATION_STAGE else "Final Holdout"
        reference = None
        content_hash = plan["partitions"][stage]["content_hash"]
        try:
            row = protected_eval._read_ledger_row(store, stage, content_hash)
        except protected_eval.EvaluationRejection:
            row = None
        if (
            isinstance(row, dict)
            and isinstance(row.get("evaluation_id"), str)
            and row["evaluation_id"]
        ):
            try:
                record = _trusted_record(store, row["evaluation_id"], label)
            except ReleaseRejection:
                record = None
            if isinstance(record, dict) and protected_eval._record_matches(
                record, plan, stage
            ):
                reference = {
                    "evaluation_id": record.get("evaluation_id"),
                    "record_hash": record.get("record_hash"),
                    "outcome": record.get("outcome"),
                    "scoring": record.get("scoring"),
                    "aborted": record.get("aborted"),
                    "partition": stage,
                    "partition_content_hash": content_hash,
                    "authorization_id": (record.get("authorization") or {}).get(
                        "auth_id"
                    ),
                }
        references[stage] = reference
    return references


def _rejection_response(
    record: dict[str, Any], path: Path, *, reused: bool
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "rejection_recorded",
        "scope": SCOPE,
        "release_id": record["release_id"],
        "release_version": record["release_version"],
        "candidate_id": record.get("candidate_id"),
        "candidate_hash": record.get("candidate_hash"),
        "plan_id": record.get("plan_id"),
        "snapshot_id": record.get("snapshot_id"),
        "decision": "rejected",
        "reused": reused,
        "rejection_id": record["rejection_id"],
        "rejection": record,
        "rejection_path": str(path),
        "artifact_created": False,
        "events_created": False,
        "statement": REJECTION_STATEMENT,
    }


def reject(
    store: str | Path,
    plan_id: str,
    reviewer: str,
    reason: str,
) -> dict[str, Any]:
    """Record an immutable human rejection of one frozen candidate+plan (#30).

    The decision is the reviewer's own; it is never inferred from a failed
    threshold.  It binds the plan, candidate, snapshot, both protected
    partition fingerprints and every evaluation record that was available at
    decision time.  It creates no release artifact and no lifecycle event, and
    a later ``approve`` for the same candidate+plan is refused with
    ``PRIOR_REJECTION``.  The first decision is immutable: replaying ``reject``
    returns it unchanged.
    """
    eval_store = _store(store)
    reviewer = reviewer.strip() if isinstance(reviewer, str) else ""
    reason = reason.strip() if isinstance(reason, str) else ""
    context: dict[str, Any] = {"plan_id": plan_id}
    if not reviewer or not reason:
        raise _refusal(
            eval_store,
            context,
            _reject(
                "INVALID_REJECTION",
                "rejection",
                "correct",
                "A rejection requires a non-empty reviewer and reason.",
            ),
            command="release reject",
        )
    try:
        plan = protected_eval.load_plan(eval_store, plan_id)
    except protected_eval.EvaluationRejection as error:
        raise _refusal(
            eval_store,
            context,
            _phase_rejection(error, "rejection"),
            command="release reject",
        ) from error
    if plan.get("workflow") is not None:
        raise _reject(
            "CONDITIONAL_DECISION_REQUIRED", "rejection", "review-holdout",
            "Research Holdout plans use the reviewer's preread conditional decision.",
        )
    release_id = _release_id(plan["candidate_hash"], plan["plan_id"])
    if not release_id:
        raise _refusal(
            eval_store,
            context,
            _reject(
                "FINGERPRINT_MISMATCH",
                "rejection",
                "inspect",
                "The frozen plan does not carry a usable candidate fingerprint.",
            ),
            command="release reject",
        )
    context = {
        "plan_id": plan["plan_id"],
        "release_id": release_id,
        "candidate_id": plan.get("candidate_id"),
        "candidate_hash": plan.get("candidate_hash"),
        "snapshot_id": plan.get("snapshot_id"),
    }
    existing_artifact = _existing_artifact(eval_store, release_id)
    if existing_artifact is not None:
        raise _refusal(
            eval_store,
            context,
            _reject(
                "RELEASE_ALREADY_EXISTS",
                "rejection",
                "stop",
                "An immutable release artifact already exists for this candidate "
                "and plan; an approval cannot be replaced by a rejection. A new "
                "candidate and frozen plan are the only path.",
            ),
            command="release reject",
        )
    existing = _load_rejection(eval_store, release_id)
    if existing is not None:
        # The first explicit decision is the decision; never rewrite it.
        return _rejection_response(
            existing, _rejection_location(eval_store, release_id), reused=True
        )
    references = _review_references(eval_store, plan)
    body = {
        "schema_version": SCHEMA_VERSION,
        "kind": REJECTION_KIND,
        "scope": SCOPE,
        "release_version": RELEASE_VERSION,
        "release_id": release_id,
        "decision": "rejected",
        "candidate_id": plan.get("candidate_id"),
        "candidate_hash": plan.get("candidate_hash"),
        "project_id": plan.get("project_id"),
        "plan_id": plan.get("plan_id"),
        "plan_hash": plan.get("plan_id"),
        "plan_frozen_at": plan.get("frozen_at"),
        "snapshot_id": plan.get("snapshot_id"),
        "partition_fingerprints": {
            stage: plan["partitions"][stage] for stage in STAGES
        },
        "evaluations": references,
        "reviewer": reviewer,
        "reason": reason,
        "recorded_at": _now(),
        "statement": REJECTION_STATEMENT,
        "artifact_created": False,
        "events_created": False,
    }
    rejection_id = _hash(body)
    record = {**body, "rejection_id": rejection_id}
    path = _rejection_location(eval_store, release_id)
    if not _write_immutable(path, record):
        # A concurrent explicit rejection won the write-once race; the first
        # decision is the decision.
        stored = _load_rejection(eval_store, release_id)
        assert stored is not None
        return _rejection_response(stored, path, reused=True)
    return _rejection_response(record, path, reused=False)


# ---------------------------------------------------------------------------
# Trusted evidence for an approval decision
# ---------------------------------------------------------------------------


def _phase_rejection(
    error: protected_eval.EvaluationRejection, phase: str
) -> ReleaseRejection:
    return ReleaseRejection(
        error.details["code"],
        phase,
        error.details["next_action"],
        error.details["message"],
        context=error.context,
    )


def _trusted_record(
    store: Path, evaluation_id: str, label: str
) -> dict[str, Any]:
    path = store / "records" / evaluation_id / "record.json"
    if not path.is_file():
        raise _reject(
            "EVIDENCE_MISSING",
            "approval",
            "run-evaluation",
            f"The stored {label} evaluation record is missing; the release "
            "refuses to fall back to another record.",
        )
    try:
        record = snapshot._read_json(path)
    except (OSError, UnicodeError, ValueError) as error:
        raise _reject(
            "EVIDENCE_INVALID",
            "approval",
            "inspect",
            f"The stored {label} evaluation record is unusable: {error}",
        ) from error
    try:
        protected_eval._verify_record(record)
    except ValueError as error:
        raise _reject(
            "EVIDENCE_INVALID",
            "approval",
            "inspect",
            f"The stored {label} evaluation record failed its integrity check: {error}",
        ) from error
    return record


def _execution_matches(record: dict[str, Any], plan: dict[str, Any]) -> bool:
    execution = record.get("execution")
    if not isinstance(execution, dict):
        return False
    expected = {
        "rules": plan["execution_rules"],
        "initial_cash": plan["initial_cash"],
        "signal_window_sessions": plan["signal_window_sessions"],
        "base_costs": plan["base_costs"],
        "stress_costs": plan["stress_costs"],
    }
    return all(execution.get(key) == value for key, value in expected.items())


def _stage_authorization(
    store: Path, plan: dict[str, Any], stage: str, record: dict[str, Any]
) -> dict[str, Any]:
    try:
        records = protected_eval._authorizations(store, plan["plan_id"], stage)
    except protected_eval.EvaluationRejection as error:
        raise _reject(
            "AUTHORIZATION_INVALID",
            "approval",
            "inspect",
            f"The stored authorization is unusable: {error.details['message']}",
        ) from error
    label = "Validation" if stage == protected_eval.VALIDATION_STAGE else "Final Holdout"
    if not records:
        raise _reject(
            "AUTHORIZATION_REQUIRED",
            "approval",
            "authorize",
            f"No {label} authorization is stored for this frozen plan.",
        )
    if len(records) != 1 or not protected_eval._authorization_matches(
        records[0], plan, stage
    ):
        raise _reject(
            "AUTHORIZATION_INVALID",
            "approval",
            "inspect",
            f"The stored {label} authorization is ambiguous or does not match the "
            "frozen plan, candidate, snapshot and partition fingerprint.",
        )
    authorization = records[0]
    embedded = record.get("authorization")
    if (
        not isinstance(embedded, dict)
        or embedded.get("auth_id") != authorization["auth_id"]
    ):
        raise _reject(
            "AUTHORIZATION_INVALID",
            "approval",
            "inspect",
            f"The stored {label} evaluation was not run under the stored "
            "authorization.",
        )
    return authorization


def _stage_evidence(
    store: Path,
    plan: dict[str, Any],
    stage: str,
    *,
    debit: dict[str, Any] | None = None,
    authorization: dict[str, Any] | None = None,
) -> dict[str, Any]:
    label = "Validation" if stage == protected_eval.VALIDATION_STAGE else "Final Holdout"
    content_hash = plan["partitions"][stage]["content_hash"]
    row = debit if debit is not None else protected_eval._read_ledger_row(store, stage, content_hash)
    if row is None:
        raise _reject(
            "EVIDENCE_MISSING",
            "approval",
            "run-evaluation",
            f"No {label} evaluation was debited for this frozen plan and sealed "
            "range; a release needs both completed stages.",
        )
    if (
        row.get("plan_id") != plan["plan_id"]
        or row.get("candidate_hash") != plan["candidate_hash"]
        or row.get("stage") != stage
        or row.get("partition_name") != stage
        or row.get("partition_content_hash") != content_hash
    ):
        raise _reject(
            "EVIDENCE_INVALID",
            "approval",
            "inspect",
            f"The {label} query-ledger row belongs to another frozen plan, "
            "candidate or sealed range.",
        )
    if row.get("status") != "completed":
        raise _reject(
            "EVIDENCE_INCOMPLETE",
            "approval",
            "run-evaluation",
            f"The {label} evaluation did not complete (ledger status "
            f"{row.get('status')!r}); no release can be approved.",
        )
    evaluation_id = row.get("evaluation_id")
    if not isinstance(evaluation_id, str) or not evaluation_id:
        raise _reject(
            "EVIDENCE_INVALID",
            "approval",
            "inspect",
            f"The {label} ledger row does not reference an evaluation record.",
        )
    record = _trusted_record(store, evaluation_id, label)
    if record.get("evaluation_id") != evaluation_id or not protected_eval._record_matches(
        record, plan, stage
    ):
        raise _reject(
            "EVIDENCE_INVALID",
            "approval",
            "inspect",
            f"The stored {label} record is not bound to this frozen plan, "
            "candidate, snapshot and partition fingerprint.",
        )
    if record.get("scoring") is not True or record.get("aborted") is not False:
        raise _reject(
            "EVIDENCE_INCOMPLETE",
            "approval",
            "inspect",
            f"The {label} evaluation was aborted or unscored; there is no "
            "complete evidence to approve.",
        )
    if record.get("opened_protected_source") is not True:
        raise _reject(
            "EVIDENCE_INVALID",
            "approval",
            "inspect",
            f"The {label} record does not prove that the sealed partition was "
            "opened by the trusted evaluator.",
        )
    if record.get("outcome") != "passed":
        raise _reject(
            "NOT_PASSED",
            "approval",
            "stop",
            f"The {label} evaluation outcome is {record.get('outcome')!r}; only "
            "two passed protected stages can be approved. Hard thresholds are "
            "never overridden.",
        )
    if not _execution_matches(record, plan):
        raise _reject(
            "EVIDENCE_INVALID",
            "approval",
            "inspect",
            f"The {label} record's execution settings differ from the frozen plan.",
        )
    isolation = record.get("isolation")
    if (
        record.get("isolation_verified") is not True
        or not isinstance(isolation, dict)
        or isolation.get("verified") is not True
    ):
        raise _reject(
            "ISOLATION_UNVERIFIED",
            "approval",
            "inspect",
            f"The {label} record does not carry verified OS-isolation evidence.",
        )
    coverage = record.get("coverage")
    if not isinstance(coverage, dict) or coverage.get("complete") is not True:
        raise _reject(
            "COVERAGE_INCOMPLETE",
            "approval",
            "inspect",
            f"The {label} coverage or minimum samples are incomplete.",
        )
    stored_thresholds = {
        item.get("id"): item
        for item in (record.get("thresholds") or [])
        if isinstance(item, dict)
    }
    passed_thresholds: list[str] = []
    for entry in plan["thresholds"]:
        item = stored_thresholds.get(entry["id"])
        if item is None:
            raise _reject(
                "EVIDENCE_INVALID",
                "approval",
                "inspect",
                f"The {label} record is missing the preregistered threshold "
                f"{entry['id']!r}; no threshold is exempt.",
            )
        status = item.get("status")
        if status == "failed":
            raise _reject(
                "THRESHOLD_NOT_MET",
                "approval",
                "stop",
                f"The {label} hard threshold {entry['id']!r} failed; a release is "
                "never approved over a failed hard threshold.",
            )
        if status != "passed":
            raise _reject(
                "EVIDENCE_INVALID",
                "approval",
                "inspect",
                f"The {label} threshold {entry['id']!r} is {status!r}, not "
                "passed; unavailable evidence is never treated as a pass.",
            )
        passed_thresholds.append(entry["id"])
    verdict = record.get("verdict")
    if not isinstance(verdict, dict) or verdict.get("outcome") != "passed":
        raise _reject(
            "NOT_PASSED",
            "approval",
            "inspect",
            f"The {label} record's stored verdict is not a pass.",
        )
    if verdict.get("failed_thresholds") or verdict.get("unavailable_thresholds"):
        raise _reject(
            "EVIDENCE_INVALID",
            "approval",
            "inspect",
            f"The {label} verdict still lists failed or unavailable thresholds.",
        )
    if authorization is None:
        authorization = _stage_authorization(store, plan, stage, record)
    elif (
        not protected_eval._authorization_matches(authorization, plan, stage)
        or record.get("authorization") != authorization
        or record.get("scope") != plan.get("scope")
        or (record.get("query_receipt") or {}).get("debited_at") != row.get("debited_at")
    ):
        raise _reject(
            "AUTHORIZATION_INVALID", "approval", "inspect",
            f"The {label} record is not bound to the trusted plan and authorization.",
        )
    if row.get("authorization_id") != authorization["auth_id"]:
        raise _reject(
            "AUTHORIZATION_INVALID",
            "approval",
            "inspect",
            f"The {label} evaluation was run under a different authorization than "
            "the one stored for this plan.",
        )
    return {
        "evaluation_id": evaluation_id,
        "record_hash": record["record_hash"],
        "outcome": record.get("outcome"),
        "scoring": record.get("scoring"),
        "aborted": record.get("aborted"),
        "authorization": authorization,
        "authorization_id": authorization["auth_id"],
        "partition": stage,
        "partition_content_hash": content_hash,
        "debited_at": row.get("debited_at"),
        "thresholds_passed": passed_thresholds,
        "record": record,
    }


def _approval_evidence(store: Path, plan: dict[str, Any]) -> dict[str, Any]:
    """Verify the complete trusted evidence a human approval must rest on."""
    research = plan.get("workflow") == RESEARCH_HOLDOUT
    scope = plan.get("scope")
    allowed_scopes = shadow_calendar.DATA_KINDS if research else (SCOPE,)
    if scope not in allowed_scopes or plan.get("data_kind") != scope:
        raise _reject(
            "EVIDENCE_INVALID",
            "approval",
            "inspect",
            "The frozen plan has an unsupported data scope.",
        )
    if plan.get("stages") != list(STAGES):
        raise _reject(
            "EVIDENCE_INVALID",
            "approval",
            "inspect",
            "The frozen plan does not fix both protected stages.",
        )
    candidate = plan.get("candidate")
    if not isinstance(candidate, dict):
        raise _reject(
            "EVIDENCE_INVALID", "approval", "inspect", "The frozen plan has no candidate JSON."
        )
    candidate_hash = plan.get("candidate_hash")
    recomputed = _hash(
        {key: value for key, value in candidate.items() if key != "candidate_hash"}
    )
    if candidate.get("candidate_hash") != candidate_hash or recomputed != candidate_hash:
        raise _reject(
            "EVIDENCE_INVALID",
            "approval",
            "inspect",
            "The frozen candidate JSON in the plan does not match its fingerprint.",
        )
    evaluation = candidate.get("evaluation")
    if (
        candidate.get("snapshot_id") != plan.get("snapshot_id")
        or not isinstance(evaluation, dict)
        or evaluation.get("snapshot_id") != plan.get("snapshot_id")
    ):
        raise _reject(
            "EVIDENCE_INVALID",
            "approval",
            "inspect",
            "The frozen candidate is not bound to the plan's sealed snapshot.",
        )
    stages = (
        _research_stage_evidence(store, plan)
        if research else {stage: _stage_evidence(store, plan, stage) for stage in STAGES}
    )
    validation = stages[protected_eval.VALIDATION_STAGE]
    holdout = stages[protected_eval.HOLDOUT_STAGE]

    export = validation["record"].get("prior_export_for_holdout")
    if not isinstance(export, dict):
        raise _reject(
            "EVIDENCE_INVALID",
            "approval",
            "inspect",
            "The Validation record has no derived prior export for Final Holdout.",
        )
    prior = holdout["record"].get("prior_evidence")
    bound = prior.get("validation") if isinstance(prior, dict) else None
    if not isinstance(bound, dict) or (
        bound.get("evaluation_id") != validation["evaluation_id"]
        or bound.get("record_hash") != validation["record_hash"]
        or bound.get("export_hash") != export.get("export_hash")
    ):
        raise _reject(
            "EVIDENCE_INVALID",
            "approval",
            "inspect",
            "The Final Holdout record's prior evidence is not bound to the "
            "Validation evaluation record.",
        )
    binding = holdout["authorization"].get("validation")
    if not isinstance(binding, dict) or (
        binding.get("evaluation_id") != validation["evaluation_id"]
        or binding.get("record_hash") != validation["record_hash"]
        or binding.get("export_hash") != export.get("export_hash")
        or binding.get("outcome") != "passed"
    ):
        raise _reject(
            "AUTHORIZATION_INVALID",
            "approval",
            "inspect",
            "The Final Holdout authorization is not bound to the stored "
            "Validation evaluation record hash.",
        )
    return {
        "stages": stages,
        "validation_export_hash": export.get("export_hash"),
        "holdout_validation": {
            "evaluation_id": validation["evaluation_id"],
            "record_hash": validation["record_hash"],
            "export_hash": export.get("export_hash"),
            "outcome": "passed",
            "partition": protected_eval.VALIDATION_STAGE,
            "partition_content_hash": validation["partition_content_hash"],
        },
    }


def _research_stage_evidence(store: Path, plan: dict[str, Any]) -> dict[str, Any]:
    from prajnatrade import research_holdout

    reference = plan["validation_binding"]
    try:
        selected = protected_eval.selected_validation_evidence(
            store, reference["round_id"], reference["evaluation_id"], plan["candidate"]
        )
        authorization = research_holdout.load_authorization(store, plan["plan_id"])
        claim = research_holdout.load_holdout_claim(store, plan["plan_id"])
    except protected_eval.EvaluationRejection as error:
        raise _phase_rejection(error, "approval") from error
    if authorization is None or claim is None:
        raise _reject(
            "EVIDENCE_MISSING", "approval", "run-holdout",
            "The trusted Holdout authorization and debit are required.",
        )
    actual = {
        "round_id": reference["round_id"],
        "evaluation_id": selected["evaluation_id"],
        "plan_id": selected["plan"]["plan_id"],
        "record_hash": selected["record_hash"],
        "export_hash": selected["export_hash"],
        "partition_content_hash": selected["partition_content_hash"],
    }
    if reference != actual or selected["plan"]["scope"] != plan["scope"]:
        raise _reject(
            "EVIDENCE_INVALID", "approval", "inspect",
            "The Holdout plan differs from the selected Validation evidence.",
        )
    validation_stage, holdout_stage = STAGES
    return {
        validation_stage: _stage_evidence(
            store, selected["plan"], validation_stage,
            debit=selected["debit"], authorization=selected["authorization"],
        ),
        holdout_stage: _stage_evidence(
            store, plan, holdout_stage, debit=claim, authorization=authorization,
        ),
    }


# ---------------------------------------------------------------------------
# The immutable release artifact
# ---------------------------------------------------------------------------


def _artifact(
    plan: dict[str, Any],
    evidence: dict[str, Any],
    reviewer: str,
    reason: str,
    *,
    automatic_admission: dict[str, Any] | None = None,
) -> dict[str, Any]:
    stages = evidence["stages"]
    candidate = plan["candidate"]
    evaluations: dict[str, dict[str, Any]] = {}
    for stage in STAGES:
        entry = stages[stage]
        evaluations[stage] = {
            "evaluation_id": entry["evaluation_id"],
            "record_hash": entry["record_hash"],
            "outcome": entry["outcome"],
            "scoring": entry["scoring"],
            "aborted": entry["aborted"],
            "authorization_id": entry["authorization_id"],
            "partition": stage,
            "partition_content_hash": entry["partition_content_hash"],
            "debited_at": entry["debited_at"],
        }
    evaluations[protected_eval.HOLDOUT_STAGE]["validation"] = evidence[
        "holdout_validation"
    ]
    execution = {
        "strategy": candidate["strategy"],
        "strategy_family": candidate["strategy"]["family"],
        "parameters": candidate["parameters"],
        "initial_cash": plan["initial_cash"],
        "transaction_cost_bps": plan["base_costs"]["transaction_cost_bps"],
        "slippage_bps": plan["base_costs"]["slippage_bps"],
        "base_costs": plan["base_costs"],
        "stress_costs": plan["stress_costs"],
        "signal_window_sessions": plan["signal_window_sessions"],
        "execution_rules": plan["execution_rules"],
        "baseline": plan["baseline"],
    }
    approval = {
        "reviewer": reviewer,
        "decision": "approved",
        "reason": reason,
        "approved_at": _now(),
        "evidence": {
            "evaluations": {
                stage: {
                    key: evaluations[stage][key]
                    for key in (
                        "evaluation_id",
                        "record_hash",
                        "outcome",
                        "authorization_id",
                        "partition",
                        "partition_content_hash",
                    )
                }
                for stage in STAGES
            },
            "query_receipts": {
                stage: {
                    "partition": stage,
                    "partition_content_hash": stages[stage]["partition_content_hash"],
                    "authorization_id": stages[stage]["authorization_id"],
                    "status": "completed",
                    "debited_at": stages[stage]["debited_at"],
                }
                for stage in STAGES
            },
            "isolation_verified": {stage: True for stage in STAGES},
            "thresholds_passed": {
                stage: stages[stage]["thresholds_passed"] for stage in STAGES
            },
            "coverage_complete": {stage: True for stage in STAGES},
            "validation_export_hash": evidence["validation_export_hash"],
        },
    }
    body = {
        "schema_version": SCHEMA_VERSION,
        "kind": ARTIFACT_KIND,
        "scope": plan["scope"],
        "release_version": RELEASE_VERSION,
        "release_id": _release_id(plan["candidate_hash"], plan["plan_id"]),
        "candidate_id": plan["candidate_id"],
        "candidate_hash": plan["candidate_hash"],
        "candidate": candidate,
        "project_id": plan["project_id"],
        "plan_id": plan["plan_id"],
        "plan_hash": plan["plan_id"],
        "plan_frozen_at": plan["frozen_at"],
        "snapshot_id": plan["snapshot_id"],
        "partition_fingerprints": {
            stage: plan["partitions"][stage] for stage in STAGES
        },
        "execution": execution,
        "evaluations": evaluations,
        "approval": approval,
        "admission": {
            "decision": "approved",
            "statement": ADMISSION_STATEMENT,
            "automatic_enable": False,
            "enablement": (
                "a separate operator lifecycle event registered at a "
                "pre-registered after-close signal date"
            ),
        },
    }
    if automatic_admission is not None:
        holdout = stages[protected_eval.HOLDOUT_STAGE]
        approval["approved_at"] = holdout["record"]["completed_at"]
        approval["authorization_id"] = holdout["authorization_id"]
        body["admission"] = automatic_admission
        body["workflow"] = RESEARCH_HOLDOUT
        body["validation_binding"] = plan["validation_binding"]
        body["direction_id"] = holdout["authorization"].get("direction_id")
    return {**body, "artifact_hash": _hash(body)}


def _approval_response(
    artifact: dict[str, Any], path: Path, *, reused: bool
) -> dict[str, Any]:
    approval = artifact.get("approval")
    approval = approval if isinstance(approval, dict) else {}
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "approved",
        "scope": artifact["scope"],
        "release_id": artifact["release_id"],
        "release_version": artifact["release_version"],
        "artifact_hash": artifact["artifact_hash"],
        "release_path": str(path),
        "reused": reused,
        "candidate_id": artifact.get("candidate_id"),
        "candidate_hash": artifact.get("candidate_hash"),
        "plan_id": artifact.get("plan_id"),
        "snapshot_id": artifact.get("snapshot_id"),
        "approval": {
            key: approval.get(key)
            for key in ("reviewer", "decision", "reason", "approved_at")
        },
        "evaluations": artifact.get("evaluations"),
        "artifact": artifact,
    }


def _audit(
    store: Path,
    context: dict[str, Any],
    code: str,
    message: str,
    *,
    command: str,
    phase: str,
    next_action: str = "inspect",
) -> str | None:
    """Persist a refusal without creating any release object."""
    body = {
        "schema_version": SCHEMA_VERSION,
        "scope": context.get("scope", SCOPE),
        "received_at": _now(),
        "command": command,
        **context,
        "error": {
            "code": code,
            "phase": phase,
            "next_action": next_action,
            "message": message,
        },
        "release_created": False,
        "opened_protected_source": False,
        "query_debited": False,
    }
    audit_id = _hash(body)
    record = {**body, "audit_id": audit_id}
    try:
        _write_immutable(store / "audit" / f"{audit_id}.json", record)
    except OSError:
        return None
    return audit_id


def _refusal(
    store: Path,
    context: dict[str, Any],
    error: ReleaseRejection,
    *,
    command: str = "release approve",
) -> ReleaseRejection:
    """Audit one refusal and return the public rejection carrying its audit id."""
    audit_id = _audit(
        store,
        context,
        error.details["code"],
        error.details["message"],
        command=command,
        phase=error.details["phase"],
        next_action=error.details["next_action"],
    )
    return ReleaseRejection(
        error.details["code"],
        error.details["phase"],
        error.details["next_action"],
        error.details["message"],
        context={
            **context,
            "audit_id": audit_id,
            "release_created": False,
            "opened_protected_source": False,
            "query_debited": False,
        },
    )


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------


def approve(
    store: str | Path,
    plan_id: str,
    reviewer: str,
    reason: str,
) -> dict[str, Any]:
    """Record the human approval and create the unique immutable release.

    Approval requires both protected stages of the same frozen plan to be
    completed, scored, verified, ``passed`` with every preregistered threshold
    met and complete coverage/isolation evidence; a failed hard threshold is
    never overridden.  Re-approving the same candidate+plan is idempotent: the
    existing artifact is returned unchanged (original reviewer, reason and
    timestamp) and is never rewritten.
    """
    eval_store = _store(store)
    reviewer = reviewer.strip() if isinstance(reviewer, str) else ""
    reason = reason.strip() if isinstance(reason, str) else ""
    if not reviewer or not reason:
        raise _refusal(
            eval_store,
            {"plan_id": plan_id},
            _reject(
                "INVALID_APPROVAL",
                "approval",
                "correct",
                "Approval requires a non-empty reviewer and reason.",
            ),
        )
    try:
        plan = protected_eval.load_plan(eval_store, plan_id)
    except protected_eval.EvaluationRejection as error:
        raise _refusal(
            eval_store,
            {"plan_id": plan_id},
            _phase_rejection(error, "approval"),
        ) from error
    if plan.get("workflow") is not None:
        raise _reject(
            "CONDITIONAL_DECISION_REQUIRED", "approval", "review-holdout",
            "Research Holdout plans require the stored pre-read conditional authorization and trusted pass.",
        )
    release_id = _release_id(plan["candidate_hash"], plan["plan_id"])
    if not release_id:
        raise _reject(
            "FINGERPRINT_MISMATCH",
            "approval",
            "inspect",
            "The frozen plan does not carry a usable candidate fingerprint.",
        )
    context = {
        "plan_id": plan["plan_id"],
        "release_id": release_id,
        "candidate_id": plan.get("candidate_id"),
        "candidate_hash": plan.get("candidate_hash"),
        "snapshot_id": plan.get("snapshot_id"),
    }
    existing = _existing_artifact(eval_store, release_id)
    if existing is not None:
        return _approval_response(
            _verified_artifact(existing, release_id), existing, reused=True
        )
    prior_rejection = _load_rejection(eval_store, release_id)
    if prior_rejection is not None:
        raise _refusal(
            eval_store,
            {
                **context,
                "rejection_id": prior_rejection["rejection_id"],
                "rejection_path": str(_rejection_location(eval_store, release_id)),
            },
            _reject(
                "PRIOR_REJECTION",
                "approval",
                "new-candidate",
                "A prior immutable human rejection was recorded for this exact "
                "candidate and frozen plan; a later approval is refused. Only a "
                "new candidate and a new frozen plan can be approved.",
            ),
        )
    try:
        evidence = _approval_evidence(eval_store, plan)
    except ReleaseRejection as error:
        raise _refusal(eval_store, context, error) from error
    artifact = _artifact(plan, evidence, reviewer, reason)
    path = _artifact_location(eval_store, release_id)
    written = _write_immutable(path, artifact)
    if not written:
        # A concurrent approval won the write-once race: the first artifact is
        # the release, and its original approval is never overwritten.
        return _approval_response(
            _verified_artifact(path, release_id), path, reused=True
        )
    return _approval_response(artifact, path, reused=False)


def _conditional_admission(plan: dict[str, Any], holdout: dict[str, Any]) -> dict[str, Any]:
    authorization = holdout["authorization"]
    terms = authorization.get("automatic_release")
    try:
        if not isinstance(terms, dict) or set(terms) != {"calendar"}:
            raise ValueError("The preread authorization must bind an automatic release calendar.")
        calendar = shadow_calendar.validate_calendar(terms["calendar"])
        authorized = datetime.fromisoformat(authorization["authorized_at"])
        debited = datetime.fromisoformat(holdout["debited_at"])
        completed = datetime.fromisoformat(holdout["record"]["completed_at"])
        if (
            any(instant.utcoffset() is None for instant in (authorized, debited, completed))
            or not authorized <= debited <= completed
        ):
            raise ValueError("The authorization must precede the trusted Holdout read.")
        if calendar["data_kind"] != plan["data_kind"]:
            raise ValueError("The calendar scope must match the frozen plan.")
    except (KeyError, TypeError, ValueError) as error:
        raise _reject(
            "CONDITIONAL_AUTHORIZATION_INVALID", "approval", "inspect",
            str(error),
        ) from error
    after = max(
        completed.astimezone(CN_TZ).date().isoformat(),
        plan["partitions"][protected_eval.HOLDOUT_STAGE]["end"],
    )
    first = next((session for session in calendar["sessions"] if session > after), None)
    if first is None:
        raise _reject(
            "RELEASE_CALENDAR_EXHAUSTED", "approval", "review-holdout",
            f"The authorized calendar has no scheduled session after {after}.",
        )
    return {
        "decision": "approved",
        "statement": CONDITIONAL_ADMISSION_STATEMENT,
        "automatic_enable": True,
        "authorization_id": authorization["auth_id"],
        "effective_signal_date": first,
        "calendar": calendar,
        "calendar_hash": _hash(calendar),
    }


def settle_authorized_release(store: str | Path, plan_id: str) -> dict[str, Any]:
    """Apply a prior conditional approval using complete, trusted passing evidence."""
    eval_store = _store(store)
    try:
        plan = protected_eval.load_plan(eval_store, plan_id)
    except protected_eval.EvaluationRejection as error:
        raise _phase_rejection(error, "approval") from error
    if plan.get("workflow") != RESEARCH_HOLDOUT:
        raise _reject(
            "CONDITIONAL_DECISION_REQUIRED", "approval", "inspect",
            "An automatic release requires a Research Holdout plan.",
        )
    evidence = _approval_evidence(eval_store, plan)
    holdout = evidence["stages"][protected_eval.HOLDOUT_STAGE]
    admission = _conditional_admission(plan, holdout)
    authorization = holdout["authorization"]
    artifact = _artifact(
        plan, evidence, authorization["reviewer"], authorization["reason"],
        automatic_admission=admission,
    )
    release_id = artifact["release_id"]
    path = _artifact_location(eval_store, release_id)
    written = _write_immutable(path, artifact)
    stored = _verified_artifact(path, release_id)
    if stored != artifact:
        raise _reject(
            "RELEASE_INTEGRITY_FAILED", "approval", "inspect",
            "The stored release differs from the verified conditional decision.",
        )
    event = _authorized_enablement(eval_store, stored)
    return {
        **_approval_response(stored, path, reused=not written),
        "status": "enabled",
        "effective_signal_date": admission["effective_signal_date"],
        "enablement_event": event,
    }


def _authorized_enablement(store: Path, artifact: dict[str, Any]) -> dict[str, Any]:
    release_id = artifact["release_id"]
    terms = artifact["admission"]
    timeline = _timeline(store, release_id)
    if not timeline:
        try:
            return _append_event(
                "enable", store, release_id, terms["effective_signal_date"],
                "Trusted Final Holdout pass activated the reviewer's authorization.",
                authorization_id=terms["authorization_id"],
            )["event"]
        except ReleaseRejection:
            timeline = _timeline(store, release_id)
            if not timeline:
                raise
    _verify_enablement(artifact, timeline)
    return timeline[0]


def _verify_enablement(artifact: dict[str, Any], timeline: list[dict[str, Any]]) -> None:
    if artifact.get("workflow") != RESEARCH_HOLDOUT or not timeline:
        return
    terms = artifact["admission"]
    event = timeline[0]
    if (
        event["action"] != "enable"
        or event.get("authorization_id") != terms["authorization_id"]
        or event["effective_signal_date"] != terms["effective_signal_date"]
        or event.get("scope") != artifact["scope"]
    ):
        raise _reject(
            "RELEASE_INTEGRITY_FAILED", "lifecycle", "inspect",
            "The first lifecycle event does not match the stored conditional decision.",
        )


# ---------------------------------------------------------------------------
# Fetch and operator status
# ---------------------------------------------------------------------------


def fetch(store: str | Path, release_id: str) -> dict[str, Any]:
    """Fetch one release artifact by exact ID and verify its content address."""
    eval_store = _store(store)
    path = _artifact_location(eval_store, release_id)
    artifact = _verified_artifact(path, release_id)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "fetched",
        "scope": artifact["scope"],
        "kind": ARTIFACT_KIND,
        "release_id": artifact["release_id"],
        "artifact_hash": artifact["artifact_hash"],
        "artifact_path": str(path),
        "artifact": artifact,
    }


def _event_paths(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    paths = [
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix == ".json"
    ]
    try:
        return sorted(paths, key=lambda path: int(path.stem))
    except ValueError as error:
        raise _reject(
            "RELEASE_INTEGRITY_FAILED",
            "lifecycle",
            "inspect",
            "The release event filenames are not a contiguous numeric sequence.",
        ) from error


def _timeline(store: Path, release_id: str) -> list[dict[str, Any]]:
    """Read and fully verify the append-only, hash-chained event timeline."""
    directory = _release_dir(store, release_id) / "events"
    events: list[dict[str, Any]] = []
    previous_hash: str | None = None
    previous_date: str | None = None
    state = "published"
    for index, path in enumerate(_event_paths(directory), start=1):
        try:
            event = snapshot._read_json(path)
        except (OSError, UnicodeError, ValueError) as error:
            raise _reject(
                "RELEASE_INTEGRITY_FAILED",
                "lifecycle",
                "inspect",
                f"The release event timeline is unreadable: {error}",
            ) from error
        if not isinstance(event, dict):
            raise _reject(
                "RELEASE_INTEGRITY_FAILED",
                "lifecycle",
                "inspect",
                "A release event is not an object.",
            )
        body = {key: value for key, value in event.items() if key != "event_hash"}
        if event.get("event_hash") != _hash(body):
            raise _reject(
                "RELEASE_INTEGRITY_FAILED",
                "lifecycle",
                "inspect",
                f"Release event {index} failed its content-address check.",
            )
        if (
            event.get("release_id") != release_id
            or event.get("seq") != index
            or int(path.stem) != index
        ):
            raise _reject(
                "RELEASE_INTEGRITY_FAILED",
                "lifecycle",
                "inspect",
                f"Release event {index} is not bound to this release at its sequence.",
            )
        action = event.get("action")
        if action not in ACTIONS:
            raise _reject(
                "RELEASE_INTEGRITY_FAILED",
                "lifecycle",
                "inspect",
                f"Release event {index} has an unknown action.",
            )
        if event.get("prev_event_hash") != previous_hash:
            raise _reject(
                "RELEASE_INTEGRITY_FAILED",
                "lifecycle",
                "inspect",
                f"Release event {index} does not chain to the previous event.",
            )
        try:
            signal_date = snapshot._date(event.get("effective_signal_date"))
        except ValueError as error:
            raise _reject(
                "RELEASE_INTEGRITY_FAILED",
                "lifecycle",
                "inspect",
                f"Release event {index} has an invalid effective signal date: {error}",
            ) from error
        if previous_date is not None and signal_date < previous_date:
            raise _reject(
                "RELEASE_INTEGRITY_FAILED",
                "lifecycle",
                "inspect",
                f"Release event {index} moves the effective signal date backwards.",
            )
        if event.get("state_after") not in (None, STATE_FOR_ACTION[action]):
            raise _reject(
                "RELEASE_INTEGRITY_FAILED",
                "lifecycle",
                "inspect",
                f"Release event {index} records an inconsistent resulting state.",
            )
        if event.get("state_before") not in (None, state) or state not in (
            ALLOWED_FROM[action]
        ):
            raise _reject(
                "RELEASE_INTEGRITY_FAILED",
                "lifecycle",
                "inspect",
                f"Release event {index} records an illegal transition.",
            )
        state = STATE_FOR_ACTION[action]
        previous_hash = event["event_hash"]
        previous_date = signal_date
        events.append(event)
    return events


def _state_at(events: list[dict[str, Any]], as_of: str | None) -> str:
    state = "published"
    for event in events:
        if as_of is not None and event["effective_signal_date"] > as_of:
            break
        state = STATE_FOR_ACTION[event["action"]]
    return state


def _current_state(events: list[dict[str, Any]]) -> str:
    return _state_at(events, None)


def _signal_date(value: object, *, phase: str) -> str:
    try:
        return snapshot._date(value)
    except ValueError as error:
        raise _reject(
            "INVALID_EVENT",
            phase,
            "correct",
            f"effective_signal_date must be an ISO trading date: {error}",
        ) from error


def _as_of_date(value: object) -> str:
    try:
        return snapshot._date(value)
    except ValueError as error:
        raise _reject(
            "INVALID_AS_OF",
            "status",
            "correct",
            f"--as-of must be an ISO trading date: {error}",
        ) from error


# ---------------------------------------------------------------------------
# Admission validity (hook extended by #31 dispositions)
# ---------------------------------------------------------------------------


_DISPOSITION_REVISION = re.compile(r"[0-9a-f]{64}")


def _disposition_order(entry: dict[str, Any]) -> tuple[Any, int, str]:
    """Deterministic latest-wins order: recorded instant, then sequence."""
    recorded_at = entry.get("recorded_at")
    try:
        instant = (
            snapshot._instant(recorded_at, "recorded_at")
            if isinstance(recorded_at, str)
            else None
        )
    except ValueError:
        instant = None
    if instant is None:
        instant = datetime.min.replace(tzinfo=UTC)
    sequence = entry.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        sequence = 0
    return (instant, sequence, str(entry.get("disposition_id") or ""))


def _stem_revision(path: Path) -> str:
    """Recover a revision ID from a disposition filename when it is visible."""
    stem = path.stem
    if _DISPOSITION_REVISION.fullmatch(stem):
        return stem
    head = stem.split("-", 1)[0]
    return head if _DISPOSITION_REVISION.fullmatch(head) else stem


def _invalid_disposition(path: Path, reason: str) -> dict[str, Any]:
    return {
        "revision_id": _stem_revision(path),
        "impact": "pending_review",
        "valid": False,
        "reason": reason,
        "reviewer": None,
        "decided_at": None,
        "evidence_reason": None,
        "recorded_at": None,
        "sequence": None,
        "disposition_id": None,
        "classification": None,
        "source": "unknown",
    }


def _legacy_disposition(
    path: Path, payload: dict[str, Any], release_id: str
) -> dict[str, Any]:
    """A #30 direct ``dispositions/<revision-id>.json`` record."""
    impact = payload.get("impact")
    valid = impact in DISPOSITION_IMPACTS and (
        payload.get("release_id") in (None, release_id)
    )
    recorded_at = payload.get("recorded_at")
    return {
        "revision_id": payload.get("revision_id", path.stem),
        "impact": impact if valid else "pending_review",
        "valid": valid,
        "reason": (
            None
            if valid
            else "the disposition is not bound to this release or impact"
        ),
        "reviewer": payload.get("reviewer"),
        "decided_at": recorded_at,
        "evidence_reason": payload.get("reason"),
        "recorded_at": recorded_at,
        "sequence": None,
        "disposition_id": None,
        "classification": None,
        "source": "legacy",
    }


def _verified_disposition(
    path: Path, payload: dict[str, Any], release_id: str
) -> dict[str, Any]:
    """A #31 write-once impact-disposition record (content-addressed)."""
    revision_id = payload.get("revision_id")
    reason = None
    if payload.get("kind") != DISPOSITION_KIND:
        reason = "the disposition is not an impact-disposition record"
    elif not isinstance(payload.get("disposition_id"), str) or payload[
        "disposition_id"
    ] != _hash(
        {key: value for key, value in payload.items() if key != "disposition_id"}
    ):
        reason = "the disposition failed its content-address check"
    elif payload.get("release_id") != release_id:
        reason = "the disposition is bound to another release"
    elif not isinstance(revision_id, str) or not _DISPOSITION_REVISION.fullmatch(
        revision_id
    ):
        reason = "the disposition does not reference a valid revision"
    elif payload.get("impact") not in DISPOSITION_IMPACTS:
        reason = "the disposition records an unknown impact"
    else:
        try:
            snapshot._instant(payload.get("recorded_at"), "recorded_at")
        except ValueError as error:
            reason = f"the disposition has an invalid recorded_at: {error}"
    valid = reason is None
    impact = payload.get("impact")
    return {
        "revision_id": revision_id if isinstance(revision_id, str) else _stem_revision(path),
        "impact": impact if valid else "pending_review",
        "valid": valid,
        "reason": reason,
        "reviewer": payload.get("reviewer"),
        "decided_at": payload.get("recorded_at"),
        "evidence_reason": payload.get("reason"),
        "recorded_at": payload.get("recorded_at"),
        "sequence": payload.get("sequence"),
        "disposition_id": payload.get("disposition_id"),
        "classification": payload.get("classification"),
        "source": "record",
    }


def _dispositions(store: Path, release_id: str) -> list[dict[str, Any]]:
    """Resolve every stored impact disposition to one record per revision.

    A revision can receive several write-once dispositions over time.  The
    latest by ``recorded_at``/``sequence`` wins, except that once any
    ``no_longer_supported`` record exists for the revision it is final and a
    later ``sustained`` record can never clear it.  An unreadable or unbindable
    record is conservatively invalidating, and the #30 direct-file layout
    (``dispositions/<revision-id>.json``) is still read.
    """
    directory = _release_dir(store, release_id) / "dispositions"
    if not directory.is_dir():
        return []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            payload = snapshot._read_json(path)
        except (OSError, UnicodeError, ValueError) as error:
            entry = _invalid_disposition(path, f"unreadable disposition: {error}")
        else:
            if not isinstance(payload, dict):
                entry = _invalid_disposition(path, "a disposition must be an object")
            elif payload.get("kind") == DISPOSITION_KIND or "disposition_id" in payload:
                entry = _verified_disposition(path, payload, release_id)
            else:
                entry = _legacy_disposition(path, payload, release_id)
        grouped.setdefault(entry["revision_id"], []).append(entry)
    resolved = []
    for revision_id in sorted(grouped):
        entries = grouped[revision_id]
        invalid = [entry for entry in entries if entry["valid"] is not True]
        valid = [entry for entry in entries if entry["valid"] is True]
        if invalid:
            resolved.append(invalid[0])
            continue
        if not valid:
            continue
        final = [
            entry for entry in valid if entry["impact"] == "no_longer_supported"
        ]
        resolved.append(
            max(final or valid, key=_disposition_order)
        )
    return resolved


def _admission(
    store: Path,
    release_id: str,
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    retired = _current_state(events) == "retired"
    dispositions = _dispositions(store, release_id)
    invalidating = [
        item
        for item in dispositions
        if item["valid"] is not True or item["impact"] in INVALIDATING_IMPACTS
    ]
    if retired:
        return {
            "valid": False,
            "reason": "retired",
            "retired": True,
            "dispositions": dispositions,
            "invalidating_dispositions": [item["revision_id"] for item in invalidating],
        }
    if invalidating:
        return {
            "valid": False,
            "reason": invalidating[0]["impact"],
            "retired": False,
            "dispositions": dispositions,
            "invalidating_dispositions": [item["revision_id"] for item in invalidating],
        }
    return {
        "valid": True,
        "reason": None,
        "retired": False,
        "dispositions": dispositions,
        "invalidating_dispositions": [],
    }


def admission_state(store: str | Path, release_id: str) -> dict[str, Any]:
    """The #31 hook: is this release still admissible for new signals?

    Valid unless the release is retired or any stored impact disposition is
    ``pending_review`` / ``no_longer_supported`` (an unreadable or unbound
    disposition is treated conservatively as invalidating).  Several immutable
    dispositions for one revision resolve latest-wins; a
    ``no_longer_supported`` record for the revision is final.
    """
    eval_store = _store(store)
    _verified_artifact(_artifact_location(eval_store, release_id), release_id)
    return _admission(eval_store, release_id, _timeline(eval_store, release_id))


# ---------------------------------------------------------------------------
# Dividend-correction impact re-review (#31)
# ---------------------------------------------------------------------------


def _bound_plan(store: Path, artifact: dict[str, Any]) -> dict[str, Any]:
    """Re-verify that the artifact's frozen plan is the one that used the snapshot."""
    try:
        plan = protected_eval.load_plan(store, artifact.get("plan_id"))
    except protected_eval.EvaluationRejection as error:
        raise _phase_rejection(error, "disposition") from error
    if (
        plan.get("candidate_hash") != artifact.get("candidate_hash")
        or plan.get("snapshot_id") != artifact.get("snapshot_id")
        or plan.get("partitions") != artifact.get("partition_fingerprints")
    ):
        raise _reject(
            "RELEASE_INTEGRITY_FAILED",
            "disposition",
            "inspect",
            "The frozen plan no longer matches the release artifact's candidate, "
            "snapshot and evaluated partition descriptors.",
        )
    return plan


def _plan_freeze(plan: dict[str, Any], artifact: dict[str, Any]) -> tuple[str, Any]:
    """The evidence-freeze instant: the plan's ``frozen_at``, never approval time."""
    frozen_at = plan.get("frozen_at")
    if not isinstance(frozen_at, str) or artifact.get("plan_frozen_at") != frozen_at:
        raise _reject(
            "RELEASE_INTEGRITY_FAILED",
            "disposition",
            "inspect",
            "The artifact's frozen plan timestamp does not match the stored plan; "
            "the evidence-freeze instant cannot be established.",
        )
    try:
        return frozen_at, snapshot._instant(frozen_at, "plan frozen_at")
    except ValueError as error:
        raise _reject(
            "RELEASE_INTEGRITY_FAILED",
            "disposition",
            "inspect",
            f"The frozen plan's timestamp is invalid: {error}",
        ) from error


def _exposed_stages(
    partition_fingerprints: object, ex_date: str
) -> list[str]:
    """Stages whose evaluated protected range contains the revision's ex-date."""
    if not isinstance(partition_fingerprints, dict):
        raise _reject(
            "RELEASE_INTEGRITY_FAILED",
            "disposition",
            "inspect",
            "The release artifact has no evaluated partition descriptors.",
        )
    exposed = []
    for stage in STAGES:
        descriptor = partition_fingerprints.get(stage)
        if not isinstance(descriptor, dict):
            raise _reject(
                "RELEASE_INTEGRITY_FAILED",
                "disposition",
                "inspect",
                f"The release artifact has no {stage} partition descriptor.",
            )
        try:
            start = snapshot._date(descriptor.get("start"))
            end = snapshot._date(descriptor.get("end"))
        except ValueError as error:
            raise _reject(
                "RELEASE_INTEGRITY_FAILED",
                "disposition",
                "inspect",
                f"The release artifact's {stage} range is invalid: {error}",
            ) from error
        if start > end:
            raise _reject(
                "RELEASE_INTEGRITY_FAILED",
                "disposition",
                "inspect",
                f"The release artifact's {stage} range ends before it starts.",
            )
        if start <= ex_date <= end:
            exposed.append(stage)
    return exposed


def _classification(
    revision: dict[str, Any],
    frozen_at: str,
    exposed_stages: list[str],
    explicit_impact: str | None,
    *,
    evidence: str,
    release_snapshot_id: str,
    revision_predecessor_id: str | None,
    release_lineage: list[str],
    range_stages: list[str],
) -> dict[str, Any]:
    """Record why (and on what basis) the impact value was chosen.

    Exposure requires **both** that the release's snapshot is in the revision's
    predecessor lineage and that the ex-date falls inside an evaluated partition
    range.  An explicit impact that overrides the computed default must carry a
    non-empty evidence statement; a bare override never silently erases the
    classification.
    """
    try:
        official = snapshot._instant(
            revision.get("official_available_at"), "official_available_at"
        )
    except ValueError as error:  # pragma: no cover - validated by the journal loader
        raise _reject(
            "REVISION_INVALID", "disposition", "inspect", str(error)
        ) from error
    frozen = snapshot._instant(frozen_at, "plan frozen_at")
    timing_impact = "pending_review" if official <= frozen else "sustained"
    in_lineage = (
        revision_predecessor_id is not None
        and revision_predecessor_id in release_lineage
    )
    computed = timing_impact if exposed_stages else NOT_APPLICABLE_IMPACT
    if not exposed_stages and explicit_impact in INVALIDATING_IMPACTS:
        raise _reject(
            "IMPACT_NOT_APPLICABLE",
            "disposition",
            "correct",
            "The revision is not exposed by this release (its snapshot is not in "
            "the revision's lineage, or the ex-date is outside both evaluated "
            "protected ranges), so this release cannot have used that input; an "
            "unaffected release is never paused or invalidated.",
        )
    if explicit_impact is not None and explicit_impact != computed and not evidence:
        raise _reject(
            "IMPACT_EVIDENCE_REQUIRED",
            "disposition",
            "correct",
            "An explicit --impact that overrides the computed default "
            f"({computed}) must carry a non-empty --evidence statement explaining "
            "the re-review; a bare override never erases the recorded evidence.",
        )
    if not exposed_stages:
        recorded = "sustained" if explicit_impact == "sustained" else NOT_APPLICABLE_IMPACT
        method = (
            "operator_override"
            if explicit_impact
            else (
                "outside_revision_lineage"
                if range_stages
                else "excluded_from_evaluated_ranges"
            )
        )
        if range_stages:
            basis = (
                "The revision's ex-date is inside an evaluated protected range, but "
                f"the release snapshot {release_snapshot_id} is not in the "
                f"revision's predecessor lineage "
                f"({revision_predecessor_id or 'unbound'}); this release's evidence "
                "could not include that corporate action."
            )
        else:
            basis = (
                "The revision's ex-date is outside both evaluated protected ranges, "
                "so this release's evidence could not include that corporate action."
            )
        if explicit_impact == "sustained":
            basis += " The operator recorded that the original evidence still holds."
    else:
        recorded = explicit_impact or timing_impact
        method = (
            "operator_override" if explicit_impact else "official_availability_vs_plan_freeze"
        )
        if recorded == "pending_review":
            basis = (
                "The official amount was available at or before the evidence-freeze "
                "time, so the sealed evaluation may have used an amount that was "
                "already officially wrong at freeze time; admission is pending review."
            )
        elif recorded == "no_longer_supported":
            basis = (
                "The evidence is no longer supported: the revision affected an "
                "evaluated protected range and the operator confirmed the frozen "
                "judgement cannot stand. This release can never resume."
            )
        else:
            basis = (
                "The official correction became available after the plan was "
                "frozen, so the stored report and approval keep their then-evidence "
                "validity."
            )
        if explicit_impact and explicit_impact != timing_impact:
            basis += f" The operator recorded {explicit_impact} explicitly."
    result = {
        "method": method,
        "exposure": "evaluated" if exposed_stages else "not_applicable",
        "exposed_stages": exposed_stages,
        "evaluated_range_stages": range_stages,
        "release_snapshot_id": release_snapshot_id,
        "revision_predecessor_id": revision_predecessor_id,
        "release_snapshot_in_lineage": in_lineage,
        "release_lineage": release_lineage,
        "official_available_at": revision.get("official_available_at"),
        "plan_frozen_at": frozen_at,
        "timing_impact": timing_impact,
        "computed_impact": computed,
        "recorded_impact": recorded,
        "official_available_before_or_at_freeze": official <= frozen,
        "basis": basis,
    }
    if evidence:
        result["evidence"] = evidence
    return result


def _next_disposition_sequence(store: Path, release_id: str) -> int:
    directory = _release_dir(store, release_id) / "dispositions"
    if not directory.is_dir():
        return 1
    sequences = []
    for path in directory.glob("*.json"):
        try:
            payload = snapshot._read_json(path)
        except (OSError, UnicodeError, ValueError):
            continue
        sequence = payload.get("sequence") if isinstance(payload, dict) else None
        if isinstance(sequence, int) and not isinstance(sequence, bool):
            sequences.append(sequence)
    return max(sequences, default=0) + 1


def disposition(
    store: str | Path,
    snapshot_store: str | Path,
    release_id: str,
    revision_id: str,
    reviewer: str,
    reason: str,
    *,
    impact: str | None = None,
    evidence: str | None = None,
    ledger: str | Path | None = None,
    data: str | Path | None = None,
    effective_signal_date: str | None = None,
) -> dict[str, Any]:
    """Record a verified dividend-correction impact and pause new signals (#31).

    Inputs come from the snapshot-store revision journal and the release
    artifact only: no sealed partition payload is opened and the query ledger is
    never debited.  Exposure is the revision's ex-date falling inside an
    evaluated protected range in the artifact; the default classification then
    compares the official availability instant with the *plan's* evidence-freeze
    instant.  An exposed ``pending_review`` / ``no_longer_supported`` release
    that is currently enabled is paused at the first not-yet-committed after-close
    signal point (from the Shadow Live ledger when supplied, otherwise an
    explicit ``--effective-signal-date``).  Unaffected releases are never paused.
    """
    eval_store = _store(store)
    reviewer = reviewer.strip() if isinstance(reviewer, str) else ""
    reason = reason.strip() if isinstance(reason, str) else ""
    if not reviewer or not reason:
        raise ValueError("A disposition requires a non-empty reviewer and reason.")
    if impact is not None and impact not in DISPOSITION_CHOICES:
        raise ValueError(
            "--impact must be one of " + ", ".join(DISPOSITION_CHOICES) + "."
        )
    evidence = evidence.strip() if isinstance(evidence, str) else ""
    if (ledger is None) != (data is None):
        raise ValueError(
            "--ledger and --data must be supplied together to derive the first "
            "uncommitted after-close signal point."
        )
    explicit_date: str | None = None
    if effective_signal_date is not None:
        try:
            explicit_date = snapshot._date(effective_signal_date)
        except ValueError as error:
            raise ValueError(
                f"--effective-signal-date must be an ISO trading date: {error}"
            ) from error

    context: dict[str, Any] = {"release_id": release_id, "revision_id": revision_id}
    try:
        artifact = _verified_artifact(
            _artifact_location(eval_store, release_id), release_id
        )
    except ReleaseRejection as error:
        raise _refusal(
            eval_store, context, error, command="release disposition"
        ) from error
    context = {
        **context,
        "candidate_id": artifact.get("candidate_id"),
        "candidate_hash": artifact.get("candidate_hash"),
        "plan_id": artifact.get("plan_id"),
        "snapshot_id": artifact.get("snapshot_id"),
    }

    try:
        revision_record = dividend.load_verified_revision(snapshot_store, revision_id)
    except dividend.RevisionError as error:
        raise _refusal(
            eval_store,
            context,
            _reject(error.code, "disposition", "inspect", str(error)),
            command="release disposition",
        ) from error
    revision = revision_record["revision"]

    try:
        plan = _bound_plan(eval_store, artifact)
    except ReleaseRejection as error:
        raise _refusal(
            eval_store, context, error, command="release disposition"
        ) from error
    try:
        frozen_at, _ = _plan_freeze(plan, artifact)
    except ReleaseRejection as error:
        raise _refusal(
            eval_store, context, error, command="release disposition"
        ) from error
    release_snapshot_id = artifact.get("snapshot_id")
    try:
        release_lineage = snapshot.lineage_ids(snapshot_store, release_snapshot_id)
    except (OSError, ValueError) as error:
        raise _refusal(
            eval_store,
            context,
            _reject(
                "SNAPSHOT_LINEAGE_UNAVAILABLE",
                "disposition",
                "inspect",
                f"The release snapshot lineage cannot be established without "
                f"opening a sealed payload: {error}",
            ),
            command="release disposition",
        ) from error
    try:
        revision_predecessor_id = dividend.revision_predecessor(
            snapshot_store, revision_id
        )
    except ValueError as error:
        raise _refusal(
            eval_store,
            context,
            _reject(
                "REVISION_INVALID", "disposition", "inspect", str(error)
            ),
            command="release disposition",
        ) from error
    try:
        range_stages = _exposed_stages(
            artifact.get("partition_fingerprints"), revision["ex_date"]
        )
        exposed_stages = (
            range_stages
            if revision_predecessor_id is not None
            and revision_predecessor_id in release_lineage
            else []
        )
        classification = _classification(
            revision,
            frozen_at,
            exposed_stages,
            impact,
            evidence=evidence,
            release_snapshot_id=release_snapshot_id,
            revision_predecessor_id=revision_predecessor_id,
            release_lineage=release_lineage,
            range_stages=range_stages,
        )
    except ReleaseRejection as error:
        raise _refusal(
            eval_store, context, error, command="release disposition"
        ) from error
    recorded_impact = classification["recorded_impact"]
    invalidating = recorded_impact in INVALIDATING_IMPACTS

    events = _timeline(eval_store, release_id)
    _verify_enablement(artifact, events)
    state = _current_state(events)
    pause_date: str | None = None
    if invalidating and state == "enabled":
        last_signal_date = events[-1]["effective_signal_date"]
        derived_date: str | None = None
        if ledger is not None and data is not None:
            from prajnatrade import shadow  # deferred: shadow imports this module

            try:
                derived_date = shadow.first_uncommitted_signal_date(
                    ledger,
                    data,
                    release_id,
                    not_before=last_signal_date,
                    after=revision_record["filed_at"],
                )
            except shadow.ShadowRejection as error:
                raise _refusal(
                    eval_store,
                    context,
                    _reject(
                        error.details["code"],
                        "disposition",
                        error.details["next_action"],
                        error.details["message"],
                    ),
                    command="release disposition",
                ) from error
            if derived_date is None:
                raise _refusal(
                    eval_store,
                    context,
                    _reject(
                        "NO_UNCOMMITTED_SIGNAL_POINT",
                        "disposition",
                        "inspect",
                        "Every session in the supplied feed is already committed; "
                        "there is no uncommitted after-close signal point to pause at.",
                    ),
                    command="release disposition",
                )
        if explicit_date is not None:
            if derived_date is not None and explicit_date != derived_date:
                raise _refusal(
                    eval_store,
                    {
                        **context,
                        "derived_signal_date": derived_date,
                        "effective_signal_date": explicit_date,
                    },
                    _reject(
                        "EFFECTIVE_SIGNAL_DATE_MISMATCH",
                        "disposition",
                        "correct",
                        "The exposed correction must pause at the first "
                        f"not-yet-committed after-close signal point {derived_date}; "
                        f"the explicit date {explicit_date} would leave new signals "
                        "running and is refused.",
                    ),
                    command="release disposition",
                )
            pause_date = explicit_date
        elif derived_date is not None:
            pause_date = derived_date
        else:
            raise _refusal(
                eval_store,
                context,
                _reject(
                    "EFFECTIVE_SIGNAL_DATE_REQUIRED",
                    "disposition",
                    "correct",
                    "An exposed invalidating disposition on an enabled release "
                    "requires --effective-signal-date, or --ledger and --data to "
                    "derive the first uncommitted after-close signal point.",
                ),
                command="release disposition",
            )
        if pause_date < last_signal_date:
            raise _refusal(
                eval_store,
                context,
                _reject(
                    "ILLEGAL_PAUSE",
                    "disposition",
                    "correct",
                    f"The pause effective date {pause_date} precedes the last "
                    f"registered signal date {last_signal_date}; the release "
                    "timeline never moves backwards.",
                ),
                command="release disposition",
            )

    recorded_at = _now()
    sequence = _next_disposition_sequence(eval_store, release_id)
    body = {
        "schema_version": SCHEMA_VERSION,
        "scope": artifact["scope"],
        "kind": DISPOSITION_KIND,
        "disposition_version": DISPOSITION_VERSION,
        "sequence": sequence,
        "release_id": release_id,
        "revision_id": revision_id,
        "revision": {
            key: revision.get(key)
            for key in (
                "symbol",
                "ex_date",
                "old_amount",
                "new_amount",
                "currency",
                "official_available_at",
            )
        },
        "revision_filed_at": revision_record["filed_at"],
        "candidate_id": artifact.get("candidate_id"),
        "candidate_hash": artifact.get("candidate_hash"),
        "plan_id": artifact.get("plan_id"),
        "plan_frozen_at": frozen_at,
        "snapshot_id": artifact.get("snapshot_id"),
        "impact": recorded_impact,
        "implication": IMPLICATION_FOR_IMPACT[recorded_impact],
        "classification": classification,
        "evidence": evidence,
        "reviewer": reviewer,
        "reason": reason,
        "recorded_at": recorded_at,
        "pause_required": bool(invalidating and state == "enabled"),
        "pause_effective_signal_date": pause_date,
    }
    disposition_id = _hash(body)
    record = {**body, "disposition_id": disposition_id}
    disposition_dir = _release_dir(eval_store, release_id) / "dispositions"
    path = disposition_dir / f"{revision_id}-{sequence:04d}-{disposition_id}.json"
    if not _write_immutable(path, record):
        raise _refusal(
            eval_store,
            context,
            _reject(
                "DISPOSITION_CONFLICT",
                "disposition",
                "inspect",
                "A concurrent disposition won this write-once slot; re-read the "
                "release status and retry.",
            ),
            command="release disposition",
        )

    paused_event = None
    if pause_date is not None:
        try:
            pause_response = _append_event(
                "pause",
                eval_store,
                release_id,
                pause_date,
                f"Dividend correction {revision_id} recorded as {recorded_impact}; "
                "pause new signals.",
            )
        except ReleaseRejection as error:
            # The disposition (the conservative record) is already written; the
            # timeline conflict is surfaced instead of silently inventing a pause.
            raise ReleaseRejection(
                error.details["code"],
                error.details["phase"],
                error.details["next_action"],
                error.details["message"],
                context={
                    **error.context,
                    "disposition_id": disposition_id,
                    "disposition_recorded": True,
                },
            ) from error
        paused_event = pause_response["event"]

    events_after = _timeline(eval_store, release_id)
    admission = _admission(eval_store, release_id, events_after)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "recorded",
        "scope": artifact["scope"],
        "kind": DISPOSITION_RESULT_KIND,
        "release_id": release_id,
        "revision_id": revision_id,
        "impact": recorded_impact,
        "implication": IMPLICATION_FOR_IMPACT[recorded_impact],
        "classification": classification,
        "reason": classification["basis"],
        "reviewer": reviewer,
        "disposition": record,
        "disposition_path": str(path),
        "paused_event": paused_event,
        "state": _current_state(events_after),
        "admission": admission,
        "unchanged": {
            "query_ledger": True,
            "sealed_reads": False,
            "artifact": True,
            "evaluation_records": True,
            "snapshot_partitions": True,
        },
    }


# ---------------------------------------------------------------------------
# Lifecycle events
# ---------------------------------------------------------------------------


def _verify_bindings(store: Path, artifact: dict[str, Any]) -> None:
    """Re-verify the artifact's plan and both bound records (immutability check)."""
    plan_id = artifact.get("plan_id")
    try:
        plan = protected_eval.load_plan(store, plan_id)
    except protected_eval.EvaluationRejection as error:
        raise _reject(
            "RELEASE_INTEGRITY_FAILED",
            "lifecycle",
            "inspect",
            f"The frozen plan bound to this release is no longer valid: "
            f"{error.details['message']}",
        ) from error
    if (
        plan.get("candidate_hash") != artifact.get("candidate_hash")
        or plan.get("snapshot_id") != artifact.get("snapshot_id")
        or plan.get("partitions") != artifact.get("partition_fingerprints")
        or plan.get("scope") != artifact.get("scope")
    ):
        raise _reject(
            "RELEASE_INTEGRITY_FAILED",
            "lifecycle",
            "inspect",
            "The frozen plan no longer matches the release artifact's bindings.",
        )
    evaluations = artifact.get("evaluations")
    if not isinstance(evaluations, dict) or set(evaluations) != set(STAGES):
        raise _reject(
            "RELEASE_INTEGRITY_FAILED",
            "lifecycle",
            "inspect",
            "The release artifact does not bind both protected stages.",
        )
    if artifact.get("workflow") == RESEARCH_HOLDOUT:
        verified = _approval_evidence(store, plan)["stages"]
        if any(
            verified[stage][key] != evaluations[stage].get(key)
            for stage in STAGES
            for key in ("evaluation_id", "record_hash", "authorization_id")
        ):
            raise _reject(
                "EVIDENCE_INVALID", "lifecycle", "inspect",
                "The bound research evidence no longer matches the release artifact.",
            )
        return
    for stage in STAGES:
        entry = evaluations[stage]
        label = "Validation" if stage == protected_eval.VALIDATION_STAGE else "Final Holdout"
        if not isinstance(entry, dict):
            raise _reject(
                "RELEASE_INTEGRITY_FAILED",
                "lifecycle",
                "inspect",
                f"The release artifact's {label} binding is malformed.",
            )
        try:
            record = _trusted_record(store, entry.get("evaluation_id"), label)
        except ReleaseRejection as error:
            raise ReleaseRejection(
                error.details["code"],
                "lifecycle",
                error.details["next_action"],
                error.details["message"],
            ) from error
        if (
            record.get("record_hash") != entry.get("record_hash")
            or record.get("evaluation_id") != entry.get("evaluation_id")
            or record.get("outcome") != entry.get("outcome")
            or not protected_eval._record_matches(record, plan, stage)
        ):
            raise _reject(
                "EVIDENCE_INVALID",
                "lifecycle",
                "inspect",
                f"The bound {label} evaluation record no longer matches the "
                "release artifact.",
            )


def _transition_message(action: str, state: str) -> str:
    if state == "retired":
        return (
            "The release is retired; retirement is terminal and cannot be undone "
            "(enable and resume are refused)."
        )
    if action == "enable":
        return (
            "enable is only allowed from the never-enabled published state; this "
            "release already has lifecycle events."
        )
    if action == "pause":
        return "pause is only allowed from the enabled state."
    if action == "resume":
        return "resume is only allowed from the paused state."
    return f"The {action!r} transition is not allowed from state {state!r}."


def _append_event(
    action: str,
    store: str | Path,
    release_id: str,
    effective_signal_date: str,
    reason: str,
    *,
    authorization_id: str | None = None,
) -> dict[str, Any]:
    if action not in ACTIONS:
        raise _reject(
            "UNSUPPORTED_ACTION", "lifecycle", "correct", "Unknown lifecycle action."
        )
    eval_store = _store(store)
    path = _artifact_location(eval_store, release_id)
    artifact = _verified_artifact(path, release_id)
    events = _timeline(eval_store, release_id)
    _verify_enablement(artifact, events)
    state = _current_state(events)
    command = f"release {action}"
    reason = reason.strip() if isinstance(reason, str) else ""

    def refuse(
        code: str,
        message: str,
        *,
        next_action: str = "inspect",
        state_before: str | None = state,
    ) -> None:
        context = {
            "release_id": release_id,
            "action": action,
            "effective_signal_date": effective_signal_date,
            "artifact_hash": artifact["artifact_hash"],
            "state": state_before,
        }
        audit_id = _audit(
            eval_store,
            context,
            code,
            message,
            command=command,
            phase="lifecycle",
            next_action=next_action,
        )
        raise ReleaseRejection(
            code,
            "lifecycle",
            next_action,
            message,
            context={
                **context,
                "audit_id": audit_id,
                "release_created": False,
                "opened_protected_source": False,
                "query_debited": False,
            },
        )

    if not events and artifact.get("workflow") == RESEARCH_HOLDOUT:
        terms = artifact["admission"]
        if (
            action != "enable"
            or authorization_id != terms["authorization_id"]
            or effective_signal_date != terms["effective_signal_date"]
        ):
            refuse(
                "CONDITIONAL_DECISION_REQUIRED",
                "Enablement is bound to the stored conditional authorization and session.",
                next_action="settle-holdout",
            )
    if state not in ALLOWED_FROM[action]:
        refuse("INVALID_TRANSITION", _transition_message(action, state), next_action="stop")
    try:
        signal_date = _signal_date(effective_signal_date, phase="lifecycle")
    except ReleaseRejection as error:
        refuse(
            error.details["code"],
            error.details["message"],
            next_action=error.details["next_action"],
        )
    if not reason:
        refuse(
            "INVALID_EVENT",
            "A lifecycle event requires a non-empty reason.",
            next_action="correct",
        )
    if events and signal_date < events[-1]["effective_signal_date"]:
        refuse(
            "INVALID_EVENT",
            "effective_signal_date must not decrease across the release timeline.",
            next_action="correct",
        )
    if action in ("enable", "resume"):
        admission = _admission(eval_store, release_id, events)
        if admission["valid"] is not True:
            refuse(
                "ADMISSION_INVALID",
                f"{action.capitalize()} requires a currently valid admission; it is "
                f"{admission['reason']!r}. An invalidated evidence set can only be "
                "replaced by a new frozen candidate, unseen range, approval and "
                "release ID.",
                next_action="stop",
            )
    if action == "resume":
        try:
            _verify_bindings(eval_store, artifact)
        except ReleaseRejection as error:
            refuse(
                error.details["code"],
                error.details["message"],
                next_action=error.details["next_action"],
            )
    state_after = STATE_FOR_ACTION[action]
    body = {
        "schema_version": SCHEMA_VERSION,
        "scope": artifact["scope"],
        "release_id": release_id,
        "seq": len(events) + 1,
        "action": action,
        "effective_signal_date": signal_date,
        "recorded_at": _now(),
        "reason": reason,
        "prev_event_hash": events[-1]["event_hash"] if events else None,
        "state_before": state,
        "state_after": state_after,
    }
    if authorization_id is not None:
        body["authorization_id"] = authorization_id
    event = {**body, "event_hash": _hash(body)}
    event_path = _release_dir(eval_store, release_id) / "events" / f"{body['seq']}.json"
    if not _write_immutable(event_path, event):
        refuse(
            "RELEASE_INTEGRITY_FAILED",
            "A concurrent lifecycle event was recorded for this release; re-read "
            "the status and retry.",
            next_action="inspect",
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": state_after,
        "scope": artifact["scope"],
        "action": action,
        "release_id": release_id,
        "artifact_hash": artifact["artifact_hash"],
        "state": state_after,
        "timeline_length": body["seq"],
        "event": event,
    }


def enable(
    store: str | Path, release_id: str, effective_signal_date: str, reason: str
) -> dict[str, Any]:
    """Opt into tracking from a pre-registered after-close signal date."""
    return _append_event("enable", store, release_id, effective_signal_date, reason)


def pause(
    store: str | Path, release_id: str, effective_signal_date: str, reason: str
) -> dict[str, Any]:
    """Stop new signals from a pre-registered after-close signal date."""
    return _append_event("pause", store, release_id, effective_signal_date, reason)


def resume(
    store: str | Path, release_id: str, effective_signal_date: str, reason: str
) -> dict[str, Any]:
    """Resume the same release ID only while its admission is still valid."""
    return _append_event("resume", store, release_id, effective_signal_date, reason)


def retire(
    store: str | Path, release_id: str, effective_signal_date: str, reason: str
) -> dict[str, Any]:
    """Terminal retirement; keeps the artifact and the whole timeline for audit."""
    return _append_event("retire", store, release_id, effective_signal_date, reason)


# ---------------------------------------------------------------------------
# Read model / Shadow Live hooks (#32)
# ---------------------------------------------------------------------------


def release_ids(store: str | Path) -> list[str]:
    """List release artifact addresses without loading individual artifacts."""
    directory = Path(store) / "releases"
    if not directory.is_dir():
        return []
    ids = []
    for path in sorted(directory.iterdir()):
        if not path.is_dir():
            continue
        if not _RELEASE_ID.fullmatch(path.name):
            raise _reject(
                "RELEASE_INTEGRITY_FAILED",
                "status",
                "inspect",
                f"Unknown object in the release store: {path.name!r}.",
            )
        ids.append(path.name)
    return ids


def _rejection_status(
    store: Path, release_id: str, as_of: str | None = None
) -> dict[str, Any]:
    """Operator view of an explicit rejection decision (no artifact exists)."""
    rejection = _load_rejection(store, release_id)
    assert rejection is not None
    as_of_date = _as_of_date(as_of) if as_of is not None else None
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "kind": REJECTION_STATUS_KIND,
        "scope": SCOPE,
        "release_id": rejection["release_id"],
        "release_version": rejection.get("release_version"),
        "artifact_hash": None,
        "release_path": None,
        "candidate_id": rejection.get("candidate_id"),
        "candidate_hash": rejection.get("candidate_hash"),
        "plan_id": rejection.get("plan_id"),
        "plan_hash": rejection.get("plan_hash"),
        "project_id": rejection.get("project_id"),
        "snapshot_id": rejection.get("snapshot_id"),
        "partition_fingerprints": rejection.get("partition_fingerprints"),
        "approval": {
            "reviewer": None,
            "decision": None,
            "reason": None,
            "approved_at": None,
        },
        "evaluations": rejection.get("evaluations"),
        "decision": "rejected",
        "rejection": rejection,
        "rejection_id": rejection["rejection_id"],
        "rejection_path": str(_rejection_location(store, release_id)),
        "admission": {
            "valid": False,
            "reason": "prior_rejection",
            "retired": False,
            "dispositions": [],
            "invalidating_dispositions": [],
        },
        "state": "rejected",
        "current_state": "rejected",
        "active": False,
        "as_of": as_of_date,
        "timeline": [],
    }


def status(
    store: str | Path, release_id: str, as_of: str | None = None
) -> dict[str, Any]:
    """Operator view: artifact fingerprints, approval, admission and timeline."""
    eval_store = _store(store)
    path = _artifact_location(eval_store, release_id)
    if not path.is_file() and _load_rejection(eval_store, release_id) is not None:
        return _rejection_status(eval_store, release_id, as_of)
    artifact = _verified_artifact(path, release_id)
    events = _timeline(eval_store, release_id)
    _verify_enablement(artifact, events)
    current = _current_state(events)
    as_of_date = _as_of_date(as_of) if as_of is not None else None
    state = current if as_of_date is None else _state_at(events, as_of_date)
    admission = _admission(eval_store, release_id, events)
    approval = artifact.get("approval")
    approval = approval if isinstance(approval, dict) else {}
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "kind": STATUS_KIND,
        "scope": artifact["scope"],
        "release_id": artifact["release_id"],
        "release_version": artifact["release_version"],
        "artifact_hash": artifact["artifact_hash"],
        "release_path": str(path),
        "candidate_id": artifact.get("candidate_id"),
        "candidate_hash": artifact.get("candidate_hash"),
        "plan_id": artifact.get("plan_id"),
        "plan_hash": artifact.get("plan_hash"),
        "project_id": artifact.get("project_id"),
        "snapshot_id": artifact.get("snapshot_id"),
        "partition_fingerprints": artifact.get("partition_fingerprints"),
        "approval": {
            key: approval.get(key)
            for key in ("reviewer", "decision", "reason", "approved_at")
        },
        "evaluations": artifact.get("evaluations"),
        "admission": admission,
        "state": state,
        "current_state": current,
        "active": state == "enabled",
        "as_of": as_of_date,
        "timeline": events,
    }


def state_at(store: str | Path, release_id: str, signal_date: str) -> str:
    """The tracked state of one release at an after-close signal date (#32)."""
    eval_store = _store(store)
    artifact = _verified_artifact(_artifact_location(eval_store, release_id), release_id)
    events = _timeline(eval_store, release_id)
    _verify_enablement(artifact, events)
    return _state_at(events, _signal_date(signal_date, phase="status"))


def releases_active_at(store: str | Path, signal_date: str) -> list[dict[str, Any]]:
    """Releases whose state at ``signal_date`` is ``enabled`` (Shadow Live #32).

    ``published``, ``paused`` and ``retired`` are not active.  The caller must
    fetch each artifact by its exact ID and verify the fingerprints again; this
    function never returns artifact contents.
    """
    eval_store = _store(store)
    as_of = _signal_date(signal_date, phase="status")
    active = []
    for release_id in release_ids(eval_store):
        artifact = _verified_artifact(
            _artifact_location(eval_store, release_id), release_id
        )
        events = _timeline(eval_store, release_id)
        _verify_enablement(artifact, events)
        if _state_at(events, as_of) != "enabled":
            continue
        active.append(
            {
                "release_id": release_id,
                "artifact_hash": artifact["artifact_hash"],
                "candidate_id": artifact.get("candidate_id"),
                "candidate_hash": artifact.get("candidate_hash"),
                "plan_id": artifact.get("plan_id"),
                "snapshot_id": artifact.get("snapshot_id"),
                "state": "enabled",
            }
        )
    return active


def list_releases(store: str | Path) -> list[dict[str, Any]]:
    """Every stored release or explicit rejection with its current state."""
    eval_store = _store(store)
    summaries = []
    for release_id in release_ids(eval_store):
        artifact = _verified_artifact(
            _artifact_location(eval_store, release_id), release_id
        )
        events = _timeline(eval_store, release_id)
        _verify_enablement(artifact, events)
        approval = artifact.get("approval")
        approval = approval if isinstance(approval, dict) else {}
        summaries.append(
            {
                "release_id": release_id,
                "release_version": artifact.get("release_version"),
                "artifact_hash": artifact["artifact_hash"],
                "candidate_id": artifact.get("candidate_id"),
                "candidate_hash": artifact.get("candidate_hash"),
                "plan_id": artifact.get("plan_id"),
                "snapshot_id": artifact.get("snapshot_id"),
                "approval": {
                    key: approval.get(key)
                    for key in ("reviewer", "decision", "reason", "approved_at")
                },
                "state": _current_state(events),
                "admission": _admission(eval_store, release_id, events),
                "timeline_length": len(events),
            }
        )
    for release_id in _rejection_ids(eval_store):
        rejection = _load_rejection(eval_store, release_id)
        assert rejection is not None
        summaries.append(
            {
                "release_id": release_id,
                "release_version": rejection.get("release_version"),
                "artifact_hash": None,
                "candidate_id": rejection.get("candidate_id"),
                "candidate_hash": rejection.get("candidate_hash"),
                "plan_id": rejection.get("plan_id"),
                "snapshot_id": rejection.get("snapshot_id"),
                "approval": {
                    "reviewer": rejection.get("reviewer"),
                    "decision": "rejected",
                    "reason": rejection.get("reason"),
                    "approved_at": None,
                },
                "decision": "rejected",
                "rejection_id": rejection["rejection_id"],
                "state": "rejected",
                "admission": {
                    "valid": False,
                    "reason": "prior_rejection",
                    "retired": False,
                    "dispositions": [],
                    "invalidating_dispositions": [],
                },
                "timeline_length": 0,
            }
        )
    return sorted(summaries, key=lambda entry: entry["release_id"])
