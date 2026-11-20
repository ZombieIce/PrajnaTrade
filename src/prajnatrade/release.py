"""Human approval, immutable release artifacts, and tracked-version lifecycle (#30).

A *release* is the only trusted object that makes a frozen candidate a runnable
version.  It is created by an explicit human `approve` over the two completed,
passed and scored protected-evaluation records of the same frozen plan, and it
is written once as a content-addressed artifact.  It is never created
implicitly, never overwritten, and never replaced by a re-run.

Approval does **not** enable tracking.  Operators opt in through append-only
lifecycle events (`enable`, `pause`, `resume`, `retire`), each effective at a
pre-registered after-close signal date.  State at a signal date is derived from
the event timeline, so a historical replay sees the version and state that were
registered for that date rather than today's state.

Everything in this module is synthetic engineering evidence.  It does not
approve a real strategy, does not prove real-market performance, and does not
implement Shadow Live (#32).

Store layout (the same trusted store used by ``protected_eval``)::

    <store>/releases/<release_id>/artifact.json                  # immutable (0444)
    <store>/releases/<release_id>/events/<seq>.json              # append-only (0444)
    <store>/releases/<release_id>/dispositions/<revision_id>.json  # #31 (immutable)
    <store>/audit/<audit_id>.json                                # refused approvals/events

``release_id = sha256(canonical_json({"candidate_hash", "plan_hash",
"release": "release-v1"}))``; the artifact stores its own ``artifact_hash``
(the SHA-256 of the artifact without that field), which every fetch verifies.
"""

from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from prajnatrade import protected_eval, snapshot


SCHEMA_VERSION = 1
SCOPE = "synthetic"
RELEASE_VERSION = "release-v1"
ARTIFACT_KIND = "immutable_release_artifact"
STATUS_KIND = "immutable_release_status"
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
DISPOSITION_IMPACTS = frozenset({"sustained", *INVALIDATING_IMPACTS})

ADMISSION_STATEMENT = (
    "This artifact is the human admission decision for this frozen candidate and "
    "release version, recorded over the stored synthetic protected-evaluation "
    "evidence. It is engineering evidence only: it does not prove real-market "
    "performance, does not validate a real strategy, and does not enable "
    "tracking. Enablement is a separate operator lifecycle event registered at "
    "a pre-registered after-close signal date."
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
    return datetime.now(timezone.utc).isoformat()


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
    return artifact


def _existing_artifact(store: Path, release_id: str) -> Path | None:
    path = _artifact_location(store, release_id)
    return path if path.is_file() else None


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


def _load_plan(store: Path, plan_id: str) -> dict[str, Any]:
    try:
        return protected_eval._load_plan(store, plan_id)
    except protected_eval.EvaluationRejection as error:
        raise _phase_rejection(error, "approval") from error


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
    store: Path, plan: dict[str, Any], stage: str
) -> dict[str, Any]:
    label = "Validation" if stage == protected_eval.VALIDATION_STAGE else "Final Holdout"
    content_hash = plan["partitions"][stage]["content_hash"]
    row = protected_eval._read_ledger_row(store, stage, content_hash)
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
    authorization = _stage_authorization(store, plan, stage, record)
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
    if plan.get("scope") != SCOPE or plan.get("data_kind") != SCOPE:
        raise _reject(
            "EVIDENCE_INVALID",
            "approval",
            "inspect",
            "Only a synthetic frozen plan can produce a release in this build.",
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
    stages = {stage: _stage_evidence(store, plan, stage) for stage in STAGES}
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


# ---------------------------------------------------------------------------
# The immutable release artifact
# ---------------------------------------------------------------------------


def _artifact(
    plan: dict[str, Any],
    evidence: dict[str, Any],
    reviewer: str,
    reason: str,
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
        "scope": SCOPE,
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
    return {**body, "artifact_hash": _hash(body)}


def _approval_response(
    artifact: dict[str, Any], path: Path, *, reused: bool
) -> dict[str, Any]:
    approval = artifact.get("approval")
    approval = approval if isinstance(approval, dict) else {}
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "approved",
        "scope": SCOPE,
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
        "scope": SCOPE,
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
        plan = _load_plan(eval_store, plan_id)
    except ReleaseRejection as error:
        raise _refusal(eval_store, {"plan_id": plan_id}, error) from error
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
        "scope": SCOPE,
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


def _dispositions(store: Path, release_id: str) -> list[dict[str, Any]]:
    directory = _release_dir(store, release_id) / "dispositions"
    if not directory.is_dir():
        return []
    records = []
    for path in sorted(directory.glob("*.json")):
        try:
            payload = snapshot._read_json(path)
        except (OSError, UnicodeError, ValueError) as error:
            records.append(
                {
                    "revision_id": path.stem,
                    "impact": "pending_review",
                    "valid": False,
                    "reason": f"unreadable disposition: {error}",
                }
            )
            continue
        if not isinstance(payload, dict):
            records.append(
                {
                    "revision_id": path.stem,
                    "impact": "pending_review",
                    "valid": False,
                    "reason": "a disposition must be an object",
                }
            )
            continue
        impact = payload.get("impact")
        valid = impact in DISPOSITION_IMPACTS and (
            payload.get("release_id") in (None, release_id)
        )
        records.append(
            {
                "revision_id": payload.get("revision_id", path.stem),
                "impact": impact if valid else "pending_review",
                "valid": valid,
                "reason": (
                    None
                    if valid
                    else "the disposition is not bound to this release or impact"
                ),
                "reviewer": payload.get("reviewer"),
                "decided_at": payload.get("recorded_at"),
                "evidence_reason": payload.get("reason"),
            }
        )
    return records


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
    disposition is treated conservatively as invalidating).
    """
    eval_store = _store(store)
    _verified_artifact(_artifact_location(eval_store, release_id), release_id)
    return _admission(eval_store, release_id, _timeline(eval_store, release_id))


# ---------------------------------------------------------------------------
# Lifecycle events
# ---------------------------------------------------------------------------


def _verify_bindings(store: Path, artifact: dict[str, Any]) -> None:
    """Re-verify the artifact's plan and both bound records (immutability check)."""
    plan_id = artifact.get("plan_id")
    try:
        plan = protected_eval._load_plan(store, plan_id)
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
) -> dict[str, Any]:
    if action not in ACTIONS:
        raise _reject(
            "UNSUPPORTED_ACTION", "lifecycle", "correct", "Unknown lifecycle action."
        )
    eval_store = _store(store)
    path = _artifact_location(eval_store, release_id)
    artifact = _verified_artifact(path, release_id)
    events = _timeline(eval_store, release_id)
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
    if action == "resume":
        admission = _admission(eval_store, release_id, events)
        if admission["valid"] is not True:
            refuse(
                "ADMISSION_INVALID",
                f"Resume requires a currently valid admission; it is "
                f"{admission['reason']!r}.",
                next_action="stop",
            )
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
        "scope": SCOPE,
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
        "scope": SCOPE,
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


def _release_ids(store: Path) -> list[str]:
    directory = store / "releases"
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


def status(
    store: str | Path, release_id: str, as_of: str | None = None
) -> dict[str, Any]:
    """Operator view: artifact fingerprints, approval, admission and timeline."""
    eval_store = _store(store)
    path = _artifact_location(eval_store, release_id)
    artifact = _verified_artifact(path, release_id)
    events = _timeline(eval_store, release_id)
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
        "scope": SCOPE,
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
    _verified_artifact(_artifact_location(eval_store, release_id), release_id)
    return _state_at(
        _timeline(eval_store, release_id),
        _signal_date(signal_date, phase="status"),
    )


def releases_active_at(store: str | Path, signal_date: str) -> list[dict[str, Any]]:
    """Releases whose state at ``signal_date`` is ``enabled`` (Shadow Live #32).

    ``published``, ``paused`` and ``retired`` are not active.  The caller must
    fetch each artifact by its exact ID and verify the fingerprints again; this
    function never returns artifact contents.
    """
    eval_store = _store(store)
    as_of = _signal_date(signal_date, phase="status")
    active = []
    for release_id in _release_ids(eval_store):
        artifact = _verified_artifact(
            _artifact_location(eval_store, release_id), release_id
        )
        if _state_at(_timeline(eval_store, release_id), as_of) != "enabled":
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
    """Every stored release with its current state and admission (operator/#32)."""
    eval_store = _store(store)
    summaries = []
    for release_id in _release_ids(eval_store):
        artifact = _verified_artifact(
            _artifact_location(eval_store, release_id), release_id
        )
        events = _timeline(eval_store, release_id)
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
    return summaries
