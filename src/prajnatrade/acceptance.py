"""Six-scenario acceptance report for issue #17 (MVP-18, #35).

``prajnatrade acceptance report`` is **operator-only**.  It validates one strict,
versioned evidence manifest and renders a deterministic Markdown or JSON report
from that manifest alone.  It never reads a trusted evaluation/release/Shadow
Live store, never re-runs an evaluation, never opens a sealed partition and
never debits a query.  The report is a reviewable checklist, not an admission
or a strategy-performance claim.

The core rule is that the tool cannot over-claim:

* all six required scenarios must be present and status-valid;
* a ``verified`` scenario needs evidence and non-empty expected/observed;
* a ``blocked`` scenario needs explicit missing/unblock conditions;
* a ``failed`` scenario needs evidence and a recorded deviation;
* ``end_to_end_passed`` requires all six scenarios to be verified **and** the
  real chain to prove one real-Agent candidate from experiment and freeze
  through both protected stages, human approval, an immutable release and a real
  forward ledger, plus at least two real independently validated approved
  releases and at least two consecutive, common, committed real forward trading
  days;
* synthetic-only evidence marks a scenario verified only for its synthetic
  scope and never satisfies the real-chain gate.

The manifest intentionally has no store paths, credentials or sealed values:
unknown fields are rejected so diagnostics cannot be smuggled into the public
checklist.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import date
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
REPORT_KIND = "issue_17_acceptance_report"
REPORT_SCHEMA_VERSION = 1

#: The required scenarios, in canonical report order.
SCENARIO_IDS = (
    "tests_and_cli",
    "real_snapshot_baseline",
    "real_data_mock",
    "controlled_real_llm",
    "independent_validation_reports",
    "immutable_release_forward",
)

SCENARIO_STATUSES = ("verified", "blocked", "failed", "not_run")
DATA_SCOPES = ("synthetic", "real")
AGENT_SCOPES = ("mock", "real")
PERIOD_SCOPES = ("historical", "forward")

#: The real-chain gate requires these verified scenarios to carry real-scope
#: evidence.  Synthetic mechanism coverage never satisfies the real criterion.
REAL_SCOPE_REQUIREMENTS: dict[str, dict[str, str]] = {
    "real_snapshot_baseline": {"data": "real"},
    "real_data_mock": {"data": "real", "agent": "mock"},
    "controlled_real_llm": {"data": "real", "agent": "real"},
    "independent_validation_reports": {"data": "real"},
    "immutable_release_forward": {"data": "real", "period": "forward"},
}

_DELIVERY_KEYS = {
    "version",
    "branch",
    "tested_at",
    "environment",
    "test_command",
    "test_result",
}
_SCENARIO_REQUIRED_KEYS = {
    "id",
    "title",
    "status",
    "scope",
    "expected",
    "observed",
    "evidence",
    "blockers",
}
_SCENARIO_KEYS = _SCENARIO_REQUIRED_KEYS | {"deviation"}
_SCOPE_KEYS = {"data", "agent", "period"}
_EVIDENCE_KEYS = {"type", "reference", "description"}
_BLOCKER_KEYS = {"missing", "unblock"}
_REAL_CHAIN_KEYS = {"agent_candidate", "approved_releases", "forward_days"}
_CANDIDATE_KEYS = {
    "experiment_id",
    "candidate_id",
    "validation_id",
    "holdout_id",
    "release_id",
    "forward_ledger_days",
}
_RELEASE_KEYS = {
    "release_id",
    "real_data",
    "validation_id",
    "holdout_id",
    "approved",
}
#: A forward day must record the reconciliation the real-chain rule requires:
#: the after-close signal date, the next session whose open executes the signal,
#: whether the resulting orders were durably persisted before that open, the
#: terminal state those orders reached, and the trusted ledger day identity.
_FORWARD_DAY_KEYS = {
    "date",
    "signal_date",
    "orders_persisted_before_open",
    "next_session",
    "order_terminal_state",
    "ledger_day_id",
    "result_hash",
    "committed",
    "common",
    "real_data",
}
#: The documented evidenced terminal outcomes for a persisted forward order.
FORWARD_ORDER_TERMINAL_STATES = ("filled", "evidenced_not_filled")
_RESULT_HASH = re.compile(r"[0-9a-f]{64}")

NOT_A_REAL_CLAIM = (
    "This report is rendered only from the acceptance manifest. It does not "
    "read a trusted store, re-run an evaluation, open a sealed partition or "
    "send an order. Verified means verified for the recorded scope; synthetic "
    "evidence never satisfies the real-chain gate."
)


class AcceptanceRejection(Exception):
    """A machine-readable manifest refusal (never a scenario verdict)."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.details = {
            "code": code,
            "phase": "acceptance",
            "next_action": "correct_manifest",
            "message": message,
        }
        self.context = dict(context or {})


def _reject(code: str, message: str) -> AcceptanceRejection:
    return AcceptanceRejection(code, message)


# ---------------------------------------------------------------------------
# Strict manifest validation
# ---------------------------------------------------------------------------


def _mapping(
    value: object, label: str, *, allowed: set[str], required: set[str]
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _reject("FIELD_INVALID", f"{label} must be a JSON object.")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise _reject(
            "FIELD_UNKNOWN",
            f"{label} has unknown field(s): {', '.join(unknown)}.",
        )
    missing = sorted(required - set(value))
    if missing:
        raise _reject(
            "FIELD_MISSING",
            f"{label} is missing field(s): {', '.join(missing)}.",
        )
    return value


def _text(value: object, label: str, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise _reject("FIELD_INVALID", f"{label} must be a string.")
    if required and not value.strip():
        raise _reject("FIELD_INVALID", f"{label} must be a non-empty string.")
    return value


def _iso_date(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise _reject("REAL_CHAIN_INVALID", f"{label} must be an ISO date string.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise _reject(
            "REAL_CHAIN_INVALID", f"{label} must be a YYYY-MM-DD date."
        ) from error
    if parsed.isoformat() != value:
        raise _reject("REAL_CHAIN_INVALID", f"{label} must be a YYYY-MM-DD date.")
    return value


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise _reject("REAL_CHAIN_INVALID", f"{label} must be a boolean.")
    return value


def _validate_delivery(value: object) -> dict[str, Any]:
    delivery = _mapping(
        value, "delivery", allowed=_DELIVERY_KEYS, required=_DELIVERY_KEYS
    )
    return {
        key: _text(delivery[key], f"delivery.{key}") for key in _DELIVERY_KEYS
    }


def _validate_scope(value: object, label: str) -> dict[str, str]:
    scope = _mapping(value, label, allowed=_SCOPE_KEYS, required=_SCOPE_KEYS)
    data = scope["data"]
    agent = scope["agent"]
    period = scope["period"]
    if data not in DATA_SCOPES:
        raise _reject(
            "SCOPE_INVALID",
            f"{label}.data must be one of {', '.join(DATA_SCOPES)}.",
        )
    if agent not in AGENT_SCOPES:
        raise _reject(
            "SCOPE_INVALID",
            f"{label}.agent must be one of {', '.join(AGENT_SCOPES)}.",
        )
    if period not in PERIOD_SCOPES:
        raise _reject(
            "SCOPE_INVALID",
            f"{label}.period must be one of {', '.join(PERIOD_SCOPES)}.",
        )
    return {"data": data, "agent": agent, "period": period}


def _validate_evidence(value: object, label: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise _reject("FIELD_INVALID", f"{label} must be a list.")
    evidence: list[dict[str, str]] = []
    for index, entry in enumerate(value):
        item_label = f"{label}[{index}]"
        item = _mapping(
            entry, item_label, allowed=_EVIDENCE_KEYS, required=_EVIDENCE_KEYS
        )
        record = {
            key: _text(item[key], f"{item_label}.{key}") for key in _EVIDENCE_KEYS
        }
        evidence.append(
            {
                "type": record["type"],
                "reference": record["reference"],
                "description": record["description"],
            }
        )
    return evidence


def _validate_blockers(value: object, label: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise _reject("FIELD_INVALID", f"{label} must be a list.")
    blockers: list[dict[str, str]] = []
    for index, entry in enumerate(value):
        item_label = f"{label}[{index}]"
        item = _mapping(
            entry, item_label, allowed=_BLOCKER_KEYS, required=_BLOCKER_KEYS
        )
        missing = _text(item["missing"], f"{item_label}.missing")
        unblock = _text(item["unblock"], f"{item_label}.unblock")
        if not missing or not unblock:
            raise _reject(
                "BLOCKER_INVALID",
                f"{item_label} requires non-empty missing and unblock text.",
            )
        blockers.append({"missing": missing, "unblock": unblock})
    return blockers


def _validate_scenario(value: object, index: int) -> dict[str, Any]:
    label = f"scenarios[{index}]"
    scenario = _mapping(
        value, label, allowed=_SCENARIO_KEYS, required=_SCENARIO_REQUIRED_KEYS
    )
    scenario_id = _text(scenario["id"], f"{label}.id")
    if scenario_id not in SCENARIO_IDS:
        raise _reject(
            "SCENARIO_UNKNOWN",
            f"{label}.id {scenario_id!r} is not one of the six required scenarios.",
        )
    status = scenario["status"]
    if status not in SCENARIO_STATUSES:
        raise _reject(
            "STATUS_INVALID",
            f"{label}.status must be one of {', '.join(SCENARIO_STATUSES)}.",
        )
    result: dict[str, Any] = {
        "id": scenario_id,
        "title": _text(scenario["title"], f"{label}.title"),
        "status": status,
        "scope": _validate_scope(scenario["scope"], f"{label}.scope"),
        "expected": _text(scenario["expected"], f"{label}.expected", required=False),
        "observed": _text(scenario["observed"], f"{label}.observed", required=False),
        "evidence": _validate_evidence(scenario["evidence"], f"{label}.evidence"),
        "blockers": _validate_blockers(scenario["blockers"], f"{label}.blockers"),
    }
    deviation = scenario.get("deviation")
    if deviation is not None:
        result["deviation"] = _text(deviation, f"{label}.deviation", required=True)
    else:
        result["deviation"] = None
    if status == "verified":
        if not result["evidence"]:
            raise _reject(
                "VERIFIED_WITHOUT_EVIDENCE",
                f"Scenario {scenario_id!r} is verified but has no evidence reference.",
            )
        if not result["expected"].strip() or not result["observed"].strip():
            raise _reject(
                "VERIFIED_WITHOUT_EXPECTED_OBSERVED",
                f"Scenario {scenario_id!r} is verified but expected/observed is empty.",
            )
    elif status == "blocked":
        if not result["blockers"]:
            raise _reject(
                "BLOCKED_WITHOUT_BLOCKERS",
                f"Scenario {scenario_id!r} is blocked but lists no missing/unblock prerequisite.",
            )
    elif status == "failed":
        if not result["evidence"]:
            raise _reject(
                "FAILED_WITHOUT_EVIDENCE",
                f"Scenario {scenario_id!r} failed but has no evidence reference.",
            )
        if not result["deviation"] or not result["deviation"].strip():
            raise _reject(
                "FAILED_WITHOUT_DEVIATION",
                f"Scenario {scenario_id!r} failed but records no deviation.",
            )
    return result


def _validate_scenarios(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise _reject("FIELD_INVALID", "scenarios must be a list.")
    scenarios: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(value):
        scenario = _validate_scenario(entry, index)
        if scenario["id"] in seen:
            raise _reject(
                "SCENARIO_DUPLICATE",
                f"Scenario {scenario['id']!r} appears more than once.",
            )
        seen.add(scenario["id"])
        scenarios.append(scenario)
    missing = [scenario_id for scenario_id in SCENARIO_IDS if scenario_id not in seen]
    if missing:
        raise _reject(
            "SCENARIO_MISSING",
            "Missing required scenario(s): " + ", ".join(missing) + ".",
        )
    order = {scenario_id: index for index, scenario_id in enumerate(SCENARIO_IDS)}
    return sorted(scenarios, key=lambda scenario: order[scenario["id"]])


def _validate_candidate(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    candidate = _mapping(
        value,
        "real_chain.agent_candidate",
        allowed=_CANDIDATE_KEYS,
        required=_CANDIDATE_KEYS,
    )
    result: dict[str, Any] = {}
    for key in (
        "experiment_id",
        "candidate_id",
        "validation_id",
        "holdout_id",
        "release_id",
    ):
        item = candidate[key]
        if item is None:
            result[key] = None
        else:
            # Empty strings are allowed and count as "missing" in the verdict.
            result[key] = _text(item, f"real_chain.agent_candidate.{key}", required=False)
    days = candidate["forward_ledger_days"]
    if days is None:
        result["forward_ledger_days"] = None
    elif isinstance(days, list):
        parsed_days = []
        for index, item in enumerate(days):
            label = f"real_chain.agent_candidate.forward_ledger_days[{index}]"
            if not isinstance(item, str):
                raise _reject("REAL_CHAIN_INVALID", f"{label} must be a date string.")
            parsed_days.append(_iso_date(item, label) if item.strip() else "")
        result["forward_ledger_days"] = parsed_days
    else:
        raise _reject(
            "REAL_CHAIN_INVALID",
            "real_chain.agent_candidate.forward_ledger_days must be a list or null.",
        )
    return result


def _validate_releases(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise _reject("REAL_CHAIN_INVALID", "real_chain.approved_releases must be a list.")
    releases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(value):
        label = f"real_chain.approved_releases[{index}]"
        release = _mapping(entry, label, allowed=_RELEASE_KEYS, required=_RELEASE_KEYS)
        result = {
            "release_id": _text(
                release["release_id"], f"{label}.release_id", required=False
            ),
            "real_data": _boolean(release["real_data"], f"{label}.real_data"),
            "validation_id": _text(
                release["validation_id"], f"{label}.validation_id", required=False
            ),
            "holdout_id": _text(
                release["holdout_id"], f"{label}.holdout_id", required=False
            ),
            "approved": _boolean(release["approved"], f"{label}.approved"),
        }
        if result["release_id"] and result["release_id"] in seen:
            raise _reject(
                "REAL_CHAIN_INVALID",
                f"Duplicate approved release ID {result['release_id']!r}.",
            )
        if result["release_id"]:
            seen.add(result["release_id"])
        releases.append(result)
    return releases


def _validate_forward_days(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise _reject("REAL_CHAIN_INVALID", "real_chain.forward_days must be a list.")
    days: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(value):
        label = f"real_chain.forward_days[{index}]"
        day = _mapping(entry, label, allowed=_FORWARD_DAY_KEYS, required=_FORWARD_DAY_KEYS)
        result = {
            "date": _iso_date(day["date"], f"{label}.date"),
            "signal_date": _iso_date(day["signal_date"], f"{label}.signal_date"),
            "orders_persisted_before_open": _boolean(
                day["orders_persisted_before_open"],
                f"{label}.orders_persisted_before_open",
            ),
            "next_session": _iso_date(
                day["next_session"], f"{label}.next_session"
            ),
            "order_terminal_state": _text(
                day["order_terminal_state"], f"{label}.order_terminal_state"
            ),
            "ledger_day_id": _text(day["ledger_day_id"], f"{label}.ledger_day_id"),
            "result_hash": _text(day["result_hash"], f"{label}.result_hash"),
            "committed": _boolean(day["committed"], f"{label}.committed"),
            "common": _boolean(day["common"], f"{label}.common"),
            "real_data": _boolean(day["real_data"], f"{label}.real_data"),
        }
        if result["signal_date"] != result["date"]:
            raise _reject(
                "REAL_CHAIN_INVALID",
                f"{label}.signal_date must equal the committed ledger day "
                f"{result['date']!r}; the after-close signal is the day's own signal.",
            )
        if result["next_session"] <= result["date"]:
            raise _reject(
                "REAL_CHAIN_INVALID",
                f"{label}.next_session must be a later trading session than the "
                f"signal date {result['date']!r}.",
            )
        if result["order_terminal_state"] not in FORWARD_ORDER_TERMINAL_STATES:
            raise _reject(
                "REAL_CHAIN_INVALID",
                f"{label}.order_terminal_state must be one of "
                + ", ".join(FORWARD_ORDER_TERMINAL_STATES)
                + ".",
            )
        if not _RESULT_HASH.fullmatch(result["result_hash"]):
            raise _reject(
                "REAL_CHAIN_INVALID",
                f"{label}.result_hash must be a 64-character lowercase SHA-256 "
                "content address of the committed ledger day.",
            )
        if result["date"] in seen:
            raise _reject(
                "REAL_CHAIN_INVALID",
                f"Duplicate forward day {result['date']!r}.",
            )
        seen.add(result["date"])
        days.append(result)
    return days


def _validate_real_chain(value: object) -> dict[str, Any]:
    chain = _mapping(
        value,
        "real_chain",
        allowed=_REAL_CHAIN_KEYS,
        required=_REAL_CHAIN_KEYS,
    )
    return {
        "agent_candidate": _validate_candidate(chain["agent_candidate"]),
        "approved_releases": _validate_releases(chain["approved_releases"]),
        "forward_days": _validate_forward_days(chain["forward_days"]),
    }


def validate_manifest(value: object) -> dict[str, Any]:
    """Validate and canonicalise one acceptance manifest."""
    manifest = _mapping(
        value,
        "manifest",
        allowed={"schema_version", "delivery", "scenarios", "real_chain"},
        required={"schema_version", "delivery", "scenarios", "real_chain"},
    )
    schema_version = manifest["schema_version"]
    if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
        raise _reject(
            "SCHEMA_VERSION_UNSUPPORTED",
            f"schema_version must be the integer {SCHEMA_VERSION}.",
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "delivery": _validate_delivery(manifest["delivery"]),
        "scenarios": _validate_scenarios(manifest["scenarios"]),
        "real_chain": _validate_real_chain(manifest["real_chain"]),
    }


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


def _present(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _candidate_ids(candidate: dict[str, Any] | None) -> dict[str, Any]:
    if candidate is None:
        return {}
    return candidate


def _checklist_item(
    item_id: str,
    label: str,
    satisfied: bool,
    detail: str,
    missing: str,
    unblock: str,
) -> dict[str, Any]:
    return {
        "id": item_id,
        "label": label,
        "satisfied": satisfied,
        "detail": detail,
        "missing": None if satisfied else missing,
        "unblock": None if satisfied else unblock,
    }


def _six_scenarios_item(
    scenarios: list[dict[str, Any]], counts: dict[str, int]
) -> dict[str, Any]:
    unverified = [
        scenario["id"] for scenario in scenarios if scenario["status"] != "verified"
    ]
    counts_text = ", ".join(
        f"{status}={counts[status]}" for status in SCENARIO_STATUSES
    )
    return _checklist_item(
        "six_scenarios_verified",
        "All six required scenarios are present and verified.",
        not unverified,
        f"{counts['verified']}/6 verified ({counts_text}).",
        "The six-scenario rule is not met: not verified for "
        + ", ".join(unverified)
        + ".",
        "Provide verifiable evidence for every required scenario; a synthetic-only "
        "result marks at most its synthetic scope and never satisfies the real criterion.",
    )


def _approved_releases_are_real(entry: dict[str, Any]) -> bool:
    return (
        _present(entry["release_id"])
        and entry["real_data"] is True
        and entry["approved"] is True
        and _present(entry["validation_id"])
        and _present(entry["holdout_id"])
    )


def _real_release_pair(releases: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    real = [entry for entry in releases if _approved_releases_are_real(entry)]
    for index, first in enumerate(real):
        for second in real[index + 1 :]:
            if first["release_id"] == second["release_id"]:
                continue
            if first["validation_id"] == second["validation_id"]:
                continue
            if first["holdout_id"] == second["holdout_id"]:
                continue
            return first, second
    return None


def _forward_day_reconciled(day: dict[str, Any]) -> bool:
    """A committed real day whose after-close signal is reconciled to its order."""
    return (
        day["committed"] is True
        and day["common"] is True
        and day["real_data"] is True
        and day["orders_persisted_before_open"] is True
        and day["order_terminal_state"] in FORWARD_ORDER_TERMINAL_STATES
    )


def _consecutive_real_forward_days(
    days: list[dict[str, Any]],
) -> tuple[str, str] | None:
    # The manifest lists forward trading days; adjacency is the operator's
    # attested session order, so weekends and holidays are not calendar gaps.
    # The pair is only reconciled when the first day's after-close signal was
    # persisted before, and reached its terminal order state on, the second
    # day's session (``next_session``).
    ordered = sorted(days, key=lambda day: day["date"])
    for first, second in zip(ordered, ordered[1:]):
        if (
            _forward_day_reconciled(first)
            and _forward_day_reconciled(second)
            and first["next_session"] == second["date"]
        ):
            return first["date"], second["date"]
    return None


def _real_chain_checklist(
    manifest: dict[str, Any],
    scenarios: list[dict[str, Any]],
    counts: dict[str, int],
) -> list[dict[str, Any]]:
    chain = manifest["real_chain"]
    candidate = chain["agent_candidate"]
    candidate_ids = _candidate_ids(candidate)
    releases = chain["approved_releases"]
    forward_days = chain["forward_days"]

    checklist = [_six_scenarios_item(scenarios, counts)]

    id_fields = (
        "experiment_id",
        "candidate_id",
        "validation_id",
        "holdout_id",
        "release_id",
    )
    candidate_days = candidate_ids.get("forward_ledger_days") or []
    candidate_days = [day for day in candidate_days if _present(day)]
    if candidate is None:
        chain_detail = "real_chain.agent_candidate is empty."
    else:
        chain_detail = (
            "experiment={experiment_id}, candidate={candidate_id}, "
            "validation={validation_id}, holdout={holdout_id}, "
            "release={release_id}, forward_ledger_days={days}."
        ).format(days=candidate_days or "[]", **candidate_ids)
    chain_satisfied = (
        candidate is not None
        and all(_present(candidate_ids.get(field)) for field in id_fields)
        and bool(candidate_days)
    )
    checklist.append(
        _checklist_item(
            "candidate_chain",
            "One real-Agent candidate is traceable from experiment → freeze → "
            "both protected stages → human approval → immutable release → real "
            "forward ledger (non-empty IDs).",
            chain_satisfied,
            chain_detail,
            "An end-to-end real-Agent candidate trace with non-empty experiment, "
            "candidate, Validation, Final Holdout, release and forward-ledger IDs.",
            "Run one real-Agent campaign, freeze the candidate, pass both protected "
            "stages under separate human authorizations, approve the immutable "
            "release and commit its real forward ledger days.",
        )
    )

    matching_release = None
    if candidate is not None and _present(candidate_ids.get("release_id")):
        for entry in releases:
            if entry["release_id"] != candidate_ids["release_id"]:
                continue
            matching_release = entry
            break
    release_satisfied = (
        matching_release is not None
        and matching_release["real_data"] is True
        and matching_release["approved"] is True
        and matching_release["validation_id"] == candidate_ids.get("validation_id")
        and matching_release["holdout_id"] == candidate_ids.get("holdout_id")
        and _present(matching_release["validation_id"])
        and _present(matching_release["holdout_id"])
    )
    if matching_release is None:
        release_detail = "No approved release entry matches the candidate release ID."
    else:
        release_detail = (
            "release={release_id}, approved={approved}, real_data={real_data}, "
            "validation={validation_id}, holdout={holdout_id}."
        ).format(**matching_release)
    checklist.append(
        _checklist_item(
            "candidate_release",
            "The candidate's release is a real-data, human-approved immutable "
            "release bound to the same Validation and Final Holdout evidence.",
            release_satisfied,
            release_detail,
            "A human-approved, real-data immutable release whose Validation and "
            "Final Holdout IDs match the candidate's.",
            "Obtain explicit human approval for the frozen candidate and register "
            "the unique immutable release ID bound to the same protected-stage "
            "evidence.",
        )
    )

    pair = _real_release_pair(releases)
    real_releases = [entry for entry in releases if _approved_releases_are_real(entry)]
    checklist.append(
        _checklist_item(
            "two_real_approved_releases",
            "At least two real, independently validated, human-approved "
            "immutable releases.",
            pair is not None,
            f"{len(real_releases)} real approved release(s); independent pair: "
            + (
                f"{pair[0]['release_id']} and {pair[1]['release_id']}."
                if pair
                else "none."
            ),
            "At least two real, independently validated, human-approved immutable "
            "releases, each with its own Validation and Final Holdout evidence.",
            "Independently validate and approve two distinct real candidates; "
            "each must consume its own protected intervals and be registered as a "
            "unique immutable release.",
        )
    )

    window = _consecutive_real_forward_days(forward_days)
    reconciled_days = [
        day for day in forward_days if _forward_day_reconciled(day)
    ]
    checklist.append(
        _checklist_item(
            "real_forward_days",
            "At least two consecutive, common, committed real forward trading "
            "days, each with an after-close signal persisted pre-open and "
            "reconciled to the next session's order terminal state.",
            window is not None,
            f"{len(forward_days)} forward day(s); "
            f"{len(reconciled_days)} reconciled (signal pre-open, terminal state "
            "recorded); consecutive real committed common "
            + (f"window: {window[0]} → {window[1]}." if window else "window: none."),
            "At least two consecutive, common, committed real forward trading days "
            "in the trusted Shadow Live ledger with pre-open persisted orders and "
            "a recorded next-session terminal order state.",
            "Commit the same consecutive real forward sessions for the approved "
            "releases in the trusted Shadow Live ledger after a pre-registered "
            "enable, then record each day's after-close signal, pre-open persisted "
            "orders, next session, order terminal state and ledger day result hash "
            "in the acceptance manifest.",
        )
    )

    qualifying_days = {
        day["date"] for day in forward_days if _forward_day_reconciled(day)
    }
    candidate_days_satisfied = (
        bool(candidate_days) and all(day in qualifying_days for day in candidate_days)
    )
    checklist.append(
        _checklist_item(
            "candidate_forward_ledger",
            "The approved candidate has committed real forward ledger days.",
            candidate_days_satisfied,
            "candidate forward days: "
            + (", ".join(candidate_days) if candidate_days else "none")
            + ".",
            "A non-empty real forward ledger for the approved candidate; every "
            "listed forward day must be a committed, common, real session.",
            "Record the candidate's release ID in the trusted forward ledger and "
            "commit its real forward days.",
        )
    )

    unmet_scope: list[str] = []
    scope_details: list[str] = []
    for scenario in scenarios:
        requirements = REAL_SCOPE_REQUIREMENTS.get(scenario["id"])
        if not requirements:
            continue
        scope = scenario["scope"]
        problems = [
            f"{key}={scope[key]} (required {required})"
            for key, required in sorted(requirements.items())
            if scope[key] != required
        ]
        if scenario["status"] != "verified":
            problems.insert(0, f"status={scenario['status']}")
        if problems:
            unmet_scope.append(scenario["id"])
            scope_details.append(f"{scenario['id']}: " + ", ".join(problems))
        else:
            scope_details.append(f"{scenario['id']}: real-scope verified")
    checklist.append(
        _checklist_item(
            "real_scope_evidence",
            "The five real-required scenarios are verified with real-scope "
            "evidence (real data, controlled real Agent, real forward tracking).",
            not unmet_scope,
            "; ".join(scope_details) + ".",
            "Real-scope verification evidence for: " + ", ".join(unmet_scope) + ".",
            "Complete scenarios 2–6 with real evidence; synthetic mechanism tests "
            "never satisfy the real-chain gate.",
        )
    )
    return checklist


def build_report(manifest: dict[str, Any], *, manifest_sha256: str) -> dict[str, Any]:
    """Build the deterministic report document from one validated manifest."""
    scenarios = manifest["scenarios"]
    counts = {status: 0 for status in SCENARIO_STATUSES}
    for scenario in scenarios:
        counts[scenario["status"]] += 1
    checklist = _real_chain_checklist(manifest, scenarios, counts)
    end_to_end_passed = all(item["satisfied"] for item in checklist)
    six_scenario_rule_met = checklist[0]["satisfied"]
    failed = [
        scenario["id"] for scenario in scenarios if scenario["status"] == "failed"
    ]
    blocked = [
        scenario["id"] for scenario in scenarios if scenario["status"] == "blocked"
    ]
    not_run = [
        scenario["id"] for scenario in scenarios if scenario["status"] == "not_run"
    ]

    if end_to_end_passed:
        overall_status = "passed"
        statement = (
            "End-to-end MVP is PASSED: all six required scenarios are verified "
            "and the real chain proves a real-Agent candidate from experiment and "
            "freeze through both protected stages, human approval, an immutable "
            "release and a real forward ledger, with at least two real "
            "independently validated human-approved releases and at least two "
            "consecutive, common, committed real forward trading days."
        )
    elif failed:
        overall_status = "failed"
        statement = (
            f"End-to-end MVP is NOT passed: the six-scenario rule was not met "
            f"({counts['verified']} of 6 required scenarios verified) and "
            f"{len(failed)} scenario(s) failed: {', '.join(failed)}."
        )
    elif blocked:
        overall_status = "blocked"
        statement = (
            f"End-to-end MVP is NOT passed: the six-scenario rule was not met "
            f"({counts['verified']} of 6 required scenarios verified); "
            f"{len(blocked)} scenario(s) are blocked: {', '.join(blocked)}."
        )
    elif not six_scenario_rule_met:
        overall_status = "not_run"
        statement = (
            f"End-to-end MVP is NOT passed: the six-scenario rule was not met "
            f"({counts['verified']} of 6 required scenarios verified); "
            f"not run: {', '.join(not_run)}."
        )
    else:
        # All six are verified but at least one real-chain check still fails.
        overall_status = "blocked"
        unmet = [item["id"] for item in checklist if not item["satisfied"]]
        statement = (
            "End-to-end MVP is NOT passed: all six required scenarios are "
            "verified, but the real-chain gate is not satisfied ("
            + ", ".join(unmet)
            + ")."
        )

    blockers: list[dict[str, str]] = []
    for scenario in scenarios:
        if scenario["status"] == "blocked":
            for entry in scenario["blockers"]:
                blockers.append(
                    {
                        "source": f"scenario:{scenario['id']}",
                        "missing": entry["missing"],
                        "unblock": entry["unblock"],
                    }
                )
    for item in checklist:
        if not item["satisfied"]:
            blockers.append(
                {
                    "source": f"real_chain:{item['id']}",
                    "missing": item["missing"],
                    "unblock": item["unblock"],
                }
            )
    unblock_conditions = list(dict.fromkeys(blocker["unblock"] for blocker in blockers))

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "kind": REPORT_KIND,
        "manifest_sha256": manifest_sha256,
        "delivery": manifest["delivery"],
        "scenarios": scenarios,
        "scenario_status_counts": counts,
        "real_chain": manifest["real_chain"],
        "real_chain_checklist": checklist,
        "verdict": {
            "overall_status": overall_status,
            "end_to_end_passed": end_to_end_passed,
            "six_scenario_rule_met": six_scenario_rule_met,
            "statement": statement,
            "blockers": blockers,
            "failures": [
                {
                    "scenario": scenario["id"],
                    "deviation": scenario["deviation"],
                }
                for scenario in scenarios
                if scenario["status"] == "failed"
            ],
            "unblock_conditions": unblock_conditions,
        },
        "notes": [
            "Rendered from the acceptance manifest alone: no trusted store is "
            "read, no evaluation is re-run and no sealed partition is opened.",
            "Scope records the evidence range reviewed for the scenario. A "
            "blocked scenario may cite synthetic mechanism tests in its evidence, "
            "but its status and scope stay non-real; synthetic-only evidence never "
            "satisfies the real-chain gate or the real criterion.",
            "The end-to-end verdict is a checklist conclusion, not an investment "
            "or performance claim; no real order is ever sent.",
        ],
    }


# ---------------------------------------------------------------------------
# Deterministic rendering
# ---------------------------------------------------------------------------


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def manifest_sha256(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _md(value: object) -> str:
    text = str(value)
    return text.replace("|", "\\|").replace("\r\n", "<br>").replace("\n", "<br>")


def _md_or_dash(value: object) -> str:
    text = str(value)
    return text if text.strip() else "—"


def render_markdown(report: dict[str, Any]) -> str:
    """Render one deterministic Markdown report from the report document."""
    verdict = report["verdict"]
    checklist = report["real_chain_checklist"]
    counts = report["scenario_status_counts"]
    lines: list[str] = []
    lines.append("# PrajnaTrade issue #17 — six-scenario acceptance report")
    lines.append("")
    if verdict["end_to_end_passed"]:
        lines.append("**Verdict: PASSED**")
    else:
        lines.append(f"**Verdict: NOT PASSED — {verdict['overall_status']}**")
    lines.append("")
    lines.append(verdict["statement"])
    lines.append("")
    lines.append(
        f"Manifest SHA-256: `{report['manifest_sha256']}`. "
        + NOT_A_REAL_CLAIM
    )
    lines.append("")
    lines.append("## Delivery")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("| --- | --- |")
    delivery = report["delivery"]
    for key in (
        "version",
        "branch",
        "tested_at",
        "environment",
        "test_command",
        "test_result",
    ):
        lines.append(f"| {key} | {_md(delivery[key])} |")
    lines.append("")
    lines.append("## Scenario summary")
    lines.append("")
    lines.append("| # | Scenario | Status | Data | Agent | Period |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for index, scenario in enumerate(report["scenarios"], start=1):
        lines.append(
            "| {index} | `{scenario_id}` — {title} | {status} | {data} | "
            "{agent} | {period} |".format(
                index=index,
                scenario_id=scenario["id"],
                title=_md(scenario["title"]),
                status=scenario["status"],
                data=scenario["scope"]["data"],
                agent=scenario["scope"]["agent"],
                period=scenario["scope"]["period"],
            )
        )
    lines.append("")
    lines.append(
        "Status counts: "
        + ", ".join(f"{status}={counts[status]}" for status in SCENARIO_STATUSES)
        + "."
    )
    lines.append("")
    lines.append("## Scenario details")
    for index, scenario in enumerate(report["scenarios"], start=1):
        lines.append("")
        lines.append(
            f"### {index}. {scenario['title']} (`{scenario['id']}`) — "
            f"{scenario['status'].upper()}"
        )
        lines.append("")
        lines.append(
            f"- **Scope**: data={scenario['scope']['data']}, "
            f"agent={scenario['scope']['agent']}, "
            f"period={scenario['scope']['period']}"
        )
        lines.append(f"- **Expected**: {_md_or_dash(scenario['expected'])}")
        lines.append(f"- **Observed**: {_md_or_dash(scenario['observed'])}")
        if scenario["evidence"]:
            lines.append("- **Evidence**:")
            for entry in scenario["evidence"]:
                lines.append(
                    f"  - `{_md(entry['type'])}` — `{_md(entry['reference'])}`: "
                    f"{_md(entry['description'])}"
                )
        else:
            lines.append("- **Evidence**: none")
        if scenario["blockers"]:
            lines.append("- **Blockers**:")
            for entry in scenario["blockers"]:
                lines.append(f"  - Missing: {_md(entry['missing'])}")
                lines.append(f"    Unblock: {_md(entry['unblock'])}")
        else:
            lines.append("- **Blockers**: none")
        if scenario["deviation"]:
            lines.append(f"- **Deviation**: {_md(scenario['deviation'])}")
    lines.append("")
    lines.append("## Real-chain checklist")
    lines.append("")
    lines.append("| Check | Result | Detail |")
    lines.append("| --- | --- | --- |")
    for item in checklist:
        result = "met" if item["satisfied"] else "NOT met"
        lines.append(
            f"| {_md(item['label'])} (`{item['id']}`) | {result} | "
            f"{_md(item['detail'])} |"
        )
    unmet = [item for item in checklist if not item["satisfied"]]
    lines.append("")
    if unmet:
        lines.append("### Unmet real-chain conditions")
        for item in unmet:
            lines.append("")
            lines.append(f"**`{item['id']}`** — {_md(item['label'])}")
            lines.append("")
            lines.append(f"- Missing: {_md(item['missing'])}")
            lines.append(f"- Unblock: {_md(item['unblock'])}")
    else:
        lines.append(
            "All real-chain conditions are met: the verdict is a positive "
            "six-scenario end-to-end conclusion."
        )
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    lines.append(
        "- All six scenarios verified: **yes**"
        if verdict["six_scenario_rule_met"]
        else "- All six scenarios verified: **no**"
    )
    lines.append(
        "- Six-scenario rule met: **yes**"
        if verdict["six_scenario_rule_met"]
        else "- Six-scenario rule met: **no**"
    )
    lines.append(
        "- End-to-end passed: **yes**"
        if verdict["end_to_end_passed"]
        else "- End-to-end passed: **no**"
    )
    lines.append(f"- Overall status: **{verdict['overall_status']}**")
    if verdict["failures"]:
        lines.append(
            "- Failures: "
            + "; ".join(
                f"`{failure['scenario']}` — {_md(failure['deviation'])}"
                for failure in verdict["failures"]
            )
        )
    else:
        lines.append("- Failures: none")
    lines.append("")
    lines.append("### Blockers")
    lines.append("")
    if verdict["blockers"]:
        for blocker in verdict["blockers"]:
            lines.append(f"- **`{blocker['source']}`**")
            lines.append(f"  - Missing: {_md(blocker['missing'])}")
            lines.append(f"  - Unblock: {_md(blocker['unblock'])}")
    else:
        lines.append("None.")
    lines.append("")
    lines.append("### Unblock conditions")
    lines.append("")
    if verdict["unblock_conditions"]:
        for index, condition in enumerate(verdict["unblock_conditions"], start=1):
            lines.append(f"{index}. {_md(condition)}")
    else:
        lines.append("None.")
    lines.append("")
    return "\n".join(lines)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as staged:
        staged.write(text)
        staged.flush()
        os.fsync(staged.fileno())
        staged_name = staged.name
    os.replace(staged_name, path)


def _load_manifest(path: Path) -> tuple[dict[str, Any], str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise _reject(
            "MANIFEST_UNREADABLE", f"Cannot read acceptance manifest: {error}"
        ) from error

    def _invalid_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant {value!r}")

    try:
        value = json.loads(text, parse_constant=_invalid_constant)
    except (json.JSONDecodeError, ValueError) as error:
        raise _reject(
            "MANIFEST_INVALID_JSON",
            f"Acceptance manifest is not valid JSON: {error}",
        ) from error
    if not isinstance(value, dict):
        raise _reject("MANIFEST_INVALID", "Acceptance manifest must be a JSON object.")
    manifest = validate_manifest(value)
    return manifest, manifest_sha256(value)


def build_acceptance_report(path: str | Path) -> dict[str, Any]:
    """Validate the manifest at *path* and return the report document."""
    manifest, digest = _load_manifest(Path(path))
    return build_report(manifest, manifest_sha256=digest)


def write_acceptance_report(
    input_path: str | Path, output_path: str | Path
) -> dict[str, Any]:
    """Validate the manifest, render the report, write it; return the summary."""
    output = Path(output_path)
    suffix = output.suffix.lower()
    if suffix == ".md":
        report_format = "markdown"
    elif suffix == ".json":
        report_format = "json"
    else:
        raise ValueError("Acceptance report output must end with .md or .json.")
    report = build_acceptance_report(input_path)
    if report_format == "markdown":
        text = render_markdown(report)
    else:
        text = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    _write_text(output, text)
    return {
        "schema_version": 1,
        "status": "generated",
        "kind": REPORT_KIND,
        "output": str(output),
        "format": report_format,
        "manifest_sha256": report["manifest_sha256"],
        "overall_status": report["verdict"]["overall_status"],
        "end_to_end_passed": report["verdict"]["end_to_end_passed"],
        "six_scenario_rule_met": report["verdict"]["six_scenario_rule_met"],
        "scenario_status_counts": report["scenario_status_counts"],
    }
