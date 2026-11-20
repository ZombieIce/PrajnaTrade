"""Public-CLI tests for human approval and the immutable release lifecycle (#30).

Everything here is synthetic and exercises `prajnatrade release ...` (operator)
against the same trusted store as `prajnatrade evaluation ...`.  The passed
evidence is produced by the real protected-evaluation flow (seal snapshot ->
campaign/candidate -> plan -> authorize/run both stages); approval checks are
never weakened with hand-made "passed" records.

This module is meant to be run as a whole: several tests share one trusted
store that holds two approved releases.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from prajnatrade import release

from test_protected_eval_cli import (
    SYMBOLS,
    authorize,
    build_scenario,
    cli,
    cli_env,
    create_plan,
    eval_store,
    freeze_candidate,
    ok,
    plan_input,
    rejected,
    run_evaluation,
    submit_experiment,
    synthetic_snapshot,
    write_json,
)


APPROVAL_REASON = "Both protected stages passed; approve the frozen candidate."
APPROVED_AT_FROM = "reviewer-alpha"


def approve(
    store: Path,
    plan_id: str,
    *,
    reviewer: str = APPROVED_AT_FROM,
    reason: str = APPROVAL_REASON,
    extra: tuple[str, ...] = (),
):
    return cli(
        "release", "approve",
        "--store", str(store),
        "--plan-id", plan_id,
        "--reviewer", reviewer,
        "--reason", reason,
        *extra,
    )


def reject(
    store: Path,
    plan_id: str,
    *,
    reviewer: str = "reviewer-reject",
    reason: str = "Reviewed both passed stages and explicitly reject this candidate.",
):
    return cli(
        "release", "reject",
        "--store", str(store),
        "--plan-id", plan_id,
        "--reviewer", reviewer,
        "--reason", reason,
    )


def fetch(store: Path, release_id: str):
    return cli(
        "release", "fetch",
        "--store", str(store),
        "--release-id", release_id,
    )


def list_releases(store: Path):
    return cli("release", "list", "--store", str(store))


def lifecycle(
    action: str,
    store: Path,
    release_id: str,
    effective_signal_date: str,
    *,
    reason: str = "Pre-registered after-close signal point.",
):
    return cli(
        "release", action,
        "--store", str(store),
        "--release-id", release_id,
        "--effective-signal-date", effective_signal_date,
        "--reason", reason,
    )


def release_status(store: Path, release_id: str, *, as_of: str | None = None):
    arguments = ["release", "status", "--store", str(store), "--release-id", release_id]
    if as_of is not None:
        arguments += ["--as-of", as_of]
    return cli(*arguments)


def authorize_holdout(
    store: Path,
    plan_id: str,
    *,
    reviewer: str = "reviewer-2",
    reason: str = "Validation passed; clear one Holdout read.",
):
    return cli(
        "evaluation", "authorize",
        "--store", str(store),
        "--plan-id", plan_id,
        "--stage", "final_holdout",
        "--reviewer", reviewer,
        "--reason", reason,
    )


def artifact_path(store: Path, release_id: str) -> Path:
    return store / "releases" / release_id / "artifact.json"


def release_dir(store: Path, release_id: str) -> Path:
    return store / "releases" / release_id


def canonical_hash(value: object) -> str:
    """Independent canonical SHA-256 of a JSON value (the documented form)."""
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def clone_store(source: Path, root: Path, name: str) -> Path:
    target = root / name
    shutil.copytree(source, target)
    return target


def rewrite_json(path: Path, mutate) -> None:
    path.chmod(0o644)
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def audit_records(store: Path) -> list[dict]:
    directory = store / "audit"
    if not directory.is_dir():
        return []
    return [json.loads(path.read_text()) for path in sorted(directory.glob("*.json"))]


def _momentum_candidate(scenario: dict, root: Path) -> dict:
    source = write_json(root / "submission-momentum.json", {})
    submit_experiment(
        scenario["project"],
        scenario["campaign"]["campaign_id"],
        source,
        "momentum",
        "Momentum with a short lookback is a legal parameter neighbourhood.",
        "momentum",
        {"lookback": 2},
    )
    return freeze_candidate(
        scenario["project"],
        scenario["campaign"]["campaign_id"],
        "momentum",
        "candidate-momentum",
        "Momentum candidate for the release workflow.",
    )


@pytest.fixture(scope="module")
def evidence(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Real synthetic evidence: two passed plans plus every refused shape."""
    root = tmp_path_factory.mktemp("immutable-release")
    main = build_scenario(root, synthetic_snapshot(), "main")
    alt = build_scenario(
        root,
        synthetic_snapshot(derived_range=("2024-01-13", "2024-01-20")),
        "alt",
    )
    missing_partition = build_scenario(
        root,
        synthetic_snapshot(missing_close=("2024-01-18", SYMBOLS[1])),
        "missing",
    )
    momentum = _momentum_candidate(main, root)

    # One trusted store that will hold two fully passed plans (and two releases).
    store = eval_store(main, "shared")
    plan_main = create_plan(
        main, store, main["candidate"], plan_input(), name="plan-release-main"
    )
    ok(authorize(store, plan_main["plan_id"]))
    validation_main = ok(run_evaluation(main, store, plan_main["plan_id"]))
    ok(authorize_holdout(store, plan_main["plan_id"]))
    holdout_main = ok(
        run_evaluation(main, store, plan_main["plan_id"], stage="final_holdout")
    )
    assert validation_main["outcome"] == "passed"
    assert holdout_main["outcome"] == "passed"

    plan_alt = create_plan(
        alt, store, alt["candidate"], plan_input(), name="plan-release-alt"
    )
    ok(authorize(store, plan_alt["plan_id"]))
    validation_alt = ok(run_evaluation(alt, store, plan_alt["plan_id"]))
    ok(authorize_holdout(store, plan_alt["plan_id"]))
    holdout_alt = ok(
        run_evaluation(alt, store, plan_alt["plan_id"], stage="final_holdout")
    )
    assert validation_alt["outcome"] == "passed"
    assert holdout_alt["outcome"] == "passed"

    # A copy taken before any approval, for evidence-tampering tests that must
    # not be short-circuited by an already existing release artifact.
    pristine = clone_store(store, root, "eval-shared-pristine")

    missing_store = eval_store(main, "missing")
    plan_missing = create_plan(
        main, missing_store, main["candidate"], plan_input(), name="plan-release-missing"
    )

    # Validation passed but the Final Holdout stage was never run: either stage
    # missing is a refusal.
    validation_only_store = eval_store(main, "validation-only")
    plan_validation_only = create_plan(
        main,
        validation_only_store,
        main["candidate"],
        plan_input(),
        name="plan-release-validation-only",
    )
    ok(authorize(validation_only_store, plan_validation_only["plan_id"]))
    validation_only = ok(
        run_evaluation(main, validation_only_store, plan_validation_only["plan_id"])
    )
    assert validation_only["outcome"] == "passed"

    rejected_store = eval_store(main, "rejected")
    plan_rejected = create_plan(
        main,
        rejected_store,
        main["candidate"],
        plan_input(
            thresholds=[
                {"id": "relative", "metric": "return_delta", "op": ">=", "value": 0.5}
            ]
        ),
        name="plan-release-rejected",
    )
    ok(authorize(rejected_store, plan_rejected["plan_id"]))
    rejected_run = ok(run_evaluation(main, rejected_store, plan_rejected["plan_id"]))
    assert rejected_run["outcome"] == "rejected"
    assert rejected_run["reason"] == "THRESHOLD_NOT_MET"

    insufficient_store = eval_store(main, "insufficient")
    plan_insufficient = create_plan(
        main,
        insufficient_store,
        momentum,
        plan_input(
            thresholds=[{"id": "sharpe", "metric": "sharpe", "op": ">=", "value": 0.0}]
        ),
        name="plan-release-insufficient",
    )
    ok(authorize(insufficient_store, plan_insufficient["plan_id"]))
    insufficient_run = ok(
        run_evaluation(main, insufficient_store, plan_insufficient["plan_id"])
    )
    assert insufficient_run["outcome"] == "insufficient_evidence"

    aborted_store = eval_store(main, "aborted")
    plan_aborted = create_plan(
        main, aborted_store, main["candidate"], plan_input(), name="plan-release-aborted"
    )
    ok(authorize(aborted_store, plan_aborted["plan_id"]))
    aborted_run = ok(
        run_evaluation(
            main,
            aborted_store,
            plan_aborted["plan_id"],
            env=cli_env(PRAJNATRADE_TEST_FAILPOINT="evaluation_after_read"),
        )
    )
    assert aborted_run["status"] == "aborted"

    # Validation passes, but a preregistered hard threshold fails only in the
    # Final Holdout: a missing close costs one valued session.
    threshold_store = eval_store(missing_partition, "threshold")
    plan_threshold = create_plan(
        missing_partition,
        threshold_store,
        missing_partition["candidate"],
        plan_input(
            minimum_samples={"sessions": 2, "valued_sessions": 2},
            thresholds=[
                {"id": "valued", "metric": "valued_sessions", "op": ">=", "value": 4}
            ],
        ),
        name="plan-release-threshold",
    )
    ok(authorize(threshold_store, plan_threshold["plan_id"]))
    threshold_validation = ok(
        run_evaluation(missing_partition, threshold_store, plan_threshold["plan_id"])
    )
    ok(authorize_holdout(threshold_store, plan_threshold["plan_id"]))
    threshold_holdout = ok(
        run_evaluation(
            missing_partition,
            threshold_store,
            plan_threshold["plan_id"],
            stage="final_holdout",
        )
    )
    assert threshold_validation["outcome"] == "passed"
    assert threshold_holdout["outcome"] == "rejected"
    assert threshold_holdout["reason"] == "THRESHOLD_NOT_MET"

    return {
        "root": root,
        "main": {
            "scenario": main,
            "store": store,
            "plan": plan_main,
            "validation": validation_main,
            "holdout": holdout_main,
        },
        "alt": {
            "scenario": alt,
            "store": store,
            "plan": plan_alt,
            "validation": validation_alt,
            "holdout": holdout_alt,
        },
        "pristine": pristine,
        "missing_stage": {"store": missing_store, "plan": plan_missing},
        "validation_only": {
            "store": validation_only_store,
            "plan": plan_validation_only,
            "validation": validation_only,
        },
        "rejected": {
            "store": rejected_store,
            "plan": plan_rejected,
            "run": rejected_run,
        },
        "insufficient": {
            "store": insufficient_store,
            "plan": plan_insufficient,
            "run": insufficient_run,
        },
        "aborted": {
            "store": aborted_store,
            "plan": plan_aborted,
            "run": aborted_run,
        },
        "threshold_failure": {
            "store": threshold_store,
            "plan": plan_threshold,
            "validation": threshold_validation,
            "holdout": threshold_holdout,
        },
    }


@pytest.fixture(scope="module")
def approved(evidence: dict) -> dict:
    entry = evidence["main"]
    result = ok(approve(entry["store"], entry["plan"]["plan_id"]))
    assert result["reused"] is False, "the shared store was approved before this test"
    return {**entry, "release": result, "release_id": result["release_id"]}


@pytest.fixture(scope="module")
def approved_alt(evidence: dict) -> dict:
    entry = evidence["alt"]
    result = ok(
        approve(
            entry["store"],
            entry["plan"]["plan_id"],
            reviewer="reviewer-alt",
            reason="Second independently passed frozen candidate.",
        )
    )
    assert result["reused"] is False
    return {**entry, "release": result, "release_id": result["release_id"]}


# ---------------------------------------------------------------------------
# 1. Approval refuses incomplete, failed, or tampered evidence.
# ---------------------------------------------------------------------------


def test_approval_refuses_missing_or_failed_stage_evidence(evidence: dict) -> None:
    refused = [
        ("missing_stage", "EVIDENCE_MISSING"),
        ("validation_only", "EVIDENCE_MISSING"),
        ("rejected", "NOT_PASSED"),
        ("insufficient", "NOT_PASSED"),
        ("aborted", "EVIDENCE_INCOMPLETE"),
        ("threshold_failure", "NOT_PASSED"),
    ]
    for name, code in refused:
        entry = evidence[name]
        result = approve(entry["store"], entry["plan"]["plan_id"])
        payload = rejected(result, code)
        assert payload["plan_id"] == entry["plan"]["plan_id"]
        assert payload["audit_id"]
        assert payload["release_created"] is False
        assert payload["opened_protected_source"] is False
        assert not (entry["store"] / "releases").exists()
        audits = audit_records(entry["store"])
        assert [audit["error"]["code"] for audit in audits] == [code]
        audit = audits[0]
        assert audit["audit_id"] == payload["audit_id"]
        assert audit["command"] == "release approve"
        assert audit["release_created"] is False
        assert audit["opened_protected_source"] is False
        assert audit["error"]["code"] == code
        assert audit["error"]["phase"] == "approval"

    # A threshold that fails only in the Final Holdout is still a hard refusal.
    holdout = evidence["threshold_failure"]["holdout"]
    assert holdout["record"]["verdict"]["failed_thresholds"] == ["valued"]
    assert holdout["record"]["outcome"] == "rejected"

    # Explicit human rejection is "do not approve": there is no override flag.
    entry = evidence["missing_stage"]
    no_override = approve(
        entry["store"], entry["plan"]["plan_id"], extra=("--override",)
    )
    assert no_override.returncode == 2
    assert "unrecognized arguments" in no_override.stderr
    assert not (entry["store"] / "releases").exists()


# ---------------------------------------------------------------------------
# 1b. An explicit human rejection is itself the recorded decision.
# ---------------------------------------------------------------------------


def test_explicit_rejection_is_immutable_auditable_and_blocks_approval(
    evidence: dict, tmp_path: Path
) -> None:
    store = clone_store(evidence["pristine"], tmp_path, "reject-store")
    entry = evidence["main"]
    plan = entry["plan"]
    plan_id = plan["plan_id"]
    release_id = canonical_hash(
        {
            "candidate_hash": plan["candidate_hash"],
            "plan_hash": plan_id,
            "release": "release-v1",
        }
    )

    result = ok(
        reject(
            store,
            plan_id,
            reviewer="reviewer-reject",
            reason="Reviewed both passed stages; reject for a reason outside the thresholds.",
        )
    )
    assert result["status"] == "rejection_recorded"
    assert result["decision"] == "rejected"
    assert result["release_id"] == release_id
    assert result["reused"] is False
    assert result["artifact_created"] is False
    assert result["events_created"] is False
    rejection = result["rejection"]
    assert rejection["schema_version"] == 1
    assert rejection["kind"] == "immutable_release_rejection"
    assert rejection["decision"] == "rejected"
    assert rejection["reviewer"] == "reviewer-reject"
    assert "outside the thresholds" in rejection["reason"]
    assert rejection["recorded_at"]
    assert rejection["release_id"] == release_id
    assert rejection["candidate_id"] == plan["candidate_id"]
    assert rejection["candidate_hash"] == plan["candidate_hash"]
    assert rejection["plan_id"] == plan_id
    assert rejection["plan_hash"] == plan_id
    assert rejection["snapshot_id"] == plan["plan"]["snapshot_id"]
    assert rejection["partition_fingerprints"] == {
        "validation": plan["plan"]["partitions"]["validation"],
        "final_holdout": plan["plan"]["partitions"]["final_holdout"],
    }
    runs = {"validation": entry["validation"], "final_holdout": entry["holdout"]}
    for stage in ("validation", "final_holdout"):
        reference = rejection["evaluations"][stage]
        assert reference["evaluation_id"] == runs[stage]["evaluation_id"]
        assert reference["record_hash"] == runs[stage]["record"]["record_hash"]
        assert reference["outcome"] == "passed"
    body = {key: value for key, value in rejection.items() if key != "rejection_id"}
    assert rejection["rejection_id"] == canonical_hash(body)

    # No release artifact, no lifecycle event and no tracking was created.
    assert not (store / "releases" / release_id).exists()
    assert not (store / "releases").exists()
    rejection_path = store / "rejections" / f"{release_id}.json"
    assert rejection_path.is_file()
    assert rejection_path.stat().st_mode & 0o777 == 0o444
    assert json.loads(rejection_path.read_text(encoding="utf-8")) == rejection

    # The decision is visible through status and list_releases, even though no
    # artifact exists.
    current = ok(release_status(store, release_id))
    assert current["state"] == "rejected"
    assert current["decision"] == "rejected"
    assert current["artifact_hash"] is None
    assert current["timeline"] == []
    assert current["rejection"] == rejection
    assert current["admission"]["valid"] is False
    listing = ok(list_releases(store))
    entries = {entry["release_id"]: entry for entry in listing["releases"]}
    assert set(entries) == {release_id}
    assert entries[release_id]["state"] == "rejected"
    assert entries[release_id]["decision"] == "rejected"
    assert entries[release_id]["release_id"] == release_id

    # A later approval for the same candidate+plan refuses explicitly. No
    # artifact is created and the refusal is audited.
    refused = rejected(approve(store, plan_id), "PRIOR_REJECTION")
    assert refused["release_id"] == release_id
    assert refused["release_created"] is False
    assert refused["rejection_id"] == rejection["rejection_id"]
    assert not (store / "releases" / release_id).exists()
    audits = audit_records(store)
    assert [audit["error"]["code"] for audit in audits] == ["PRIOR_REJECTION"]
    assert audits[0]["command"] == "release approve"

    # The decision is immutable: a second reject reuses the first record and
    # never rewrites reviewer, reason or time.
    again = ok(
        reject(
            store,
            plan_id,
            reviewer="reviewer-later",
            reason="A later reviewer tries to replace the decision.",
        )
    )
    assert again["reused"] is True
    assert again["rejection"] == rejection
    assert again["rejection_id"] == rejection["rejection_id"]
    assert json.loads(rejection_path.read_text(encoding="utf-8")) == rejection

    # A new frozen plan for the same candidate is a new decision path.
    other = evidence["alt"]["plan"]
    assert other["plan_id"] != plan_id
    assert other["candidate_hash"] != plan["candidate_hash"]

    # The agent surface cannot reject a candidate.
    agent = cli(
        "release", "reject",
        "--store", str(store),
        "--plan-id", plan_id,
        "--reviewer", "agent",
        "--reason", "agents cannot reject",
        agent=True,
    )
    assert agent.returncode == 2
    assert "invalid choice" in agent.stderr
    assert agent.stdout == ""


# ---------------------------------------------------------------------------
# 2. A fully passed candidate produces one immutable, idempotent release.
# ---------------------------------------------------------------------------


def test_approval_creates_one_immutable_artifact_and_replays_idempotently(
    evidence: dict, approved: dict
) -> None:
    entry = evidence["main"]
    plan = entry["plan"]["plan"]
    release_id = approved["release_id"]
    result = approved["release"]
    artifact = result["artifact"]

    assert result["status"] == "approved"
    assert result["scope"] == "synthetic"
    assert result["reused"] is False
    assert result["release_version"] == "release-v1"
    assert result["candidate_hash"] == entry["plan"]["candidate_hash"]
    assert result["plan_id"] == entry["plan"]["plan_id"]
    assert result["snapshot_id"] == entry["scenario"]["snapshot_id"]

    # The release ID is the documented content address of candidate+plan.
    assert release_id == canonical_hash(
        {
            "candidate_hash": entry["plan"]["candidate_hash"],
            "plan_hash": entry["plan"]["plan_id"],
            "release": "release-v1",
        }
    )
    path = artifact_path(entry["store"], release_id)
    assert path.is_file()
    assert path.stat().st_mode & 0o777 == 0o444
    assert len(list((entry["store"] / "releases").iterdir())) == 1

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored == artifact
    body = {key: value for key, value in stored.items() if key != "artifact_hash"}
    assert stored["artifact_hash"] == canonical_hash(body)
    assert stored["release_id"] == release_id
    assert stored["plan_hash"] == entry["plan"]["plan_id"]

    # The artifact binds the exact candidate JSON, both partition descriptors,
    # both evaluations/record hashes/authorizations and the execution settings.
    assert stored["candidate"] == entry["scenario"]["candidate"]
    assert stored["candidate_hash"] == entry["scenario"]["candidate"]["candidate_hash"]
    assert stored["snapshot_id"] == entry["scenario"]["snapshot_id"]
    assert stored["partition_fingerprints"] == {
        "validation": plan["partitions"]["validation"],
        "final_holdout": plan["partitions"]["final_holdout"],
    }
    validation = entry["validation"]
    holdout = entry["holdout"]
    validation_entry = stored["evaluations"]["validation"]
    assert validation_entry["evaluation_id"] == validation["evaluation_id"]
    assert validation_entry["record_hash"] == validation["record"]["record_hash"]
    assert validation_entry["outcome"] == "passed"
    assert validation_entry["scoring"] is True
    assert validation_entry["aborted"] is False
    assert validation_entry["authorization_id"] == (
        validation["record"]["authorization"]["auth_id"]
    )
    assert validation_entry["partition_content_hash"] == (
        plan["partitions"]["validation"]["content_hash"]
    )
    holdout_entry = stored["evaluations"]["final_holdout"]
    assert holdout_entry["evaluation_id"] == holdout["evaluation_id"]
    assert holdout_entry["record_hash"] == holdout["record"]["record_hash"]
    assert holdout_entry["outcome"] == "passed"
    assert holdout_entry["authorization_id"] == (
        holdout["record"]["authorization"]["auth_id"]
    )
    assert holdout_entry["partition_content_hash"] == (
        plan["partitions"]["final_holdout"]["content_hash"]
    )
    assert holdout_entry["validation"]["evaluation_id"] == validation["evaluation_id"]
    assert holdout_entry["validation"]["record_hash"] == (
        validation["record"]["record_hash"]
    )
    assert holdout_entry["validation"]["export_hash"] == (
        validation["record"]["prior_export_for_holdout"]["export_hash"]
    )

    approval = stored["approval"]
    assert approval["reviewer"] == APPROVED_AT_FROM
    assert approval["decision"] == "approved"
    assert approval["reason"] == APPROVAL_REASON
    assert isinstance(approval["approved_at"], str) and approval["approved_at"]
    assert approval["evidence"]["isolation_verified"] == {
        "validation": True,
        "final_holdout": True,
    }
    assert approval["evidence"]["coverage_complete"] == {
        "validation": True,
        "final_holdout": True,
    }
    assert set(approval["evidence"]["thresholds_passed"]["validation"]) == {
        item["id"] for item in plan["thresholds"]
    }
    assert stored["admission"]["decision"] == "approved"
    assert stored["admission"]["automatic_enable"] is False
    assert "does not enable" in stored["admission"]["statement"]

    execution = stored["execution"]
    assert execution["strategy_family"] == "equal_weight"
    assert execution["strategy"] == {"family": "equal_weight"}
    assert execution["parameters"] == {}
    assert execution["initial_cash"] == plan["initial_cash"]
    assert execution["transaction_cost_bps"] == plan["base_costs"]["transaction_cost_bps"]
    assert execution["slippage_bps"] == plan["base_costs"]["slippage_bps"]
    assert execution["signal_window_sessions"] == plan["signal_window_sessions"]
    assert execution["execution_rules"] == plan["execution_rules"]
    assert execution["stress_costs"] == plan["stress_costs"]

    # Fetch by the exact ID returns the same verified content.
    fetched = ok(fetch(entry["store"], release_id))
    assert fetched["status"] == "fetched"
    assert fetched["release_id"] == release_id
    assert fetched["artifact_hash"] == stored["artifact_hash"]
    assert fetched["artifact"] == stored

    # A repeat approval is idempotent: same ID and bytes, original approval.
    before = path.read_bytes()
    again = ok(
        approve(
            entry["store"],
            entry["plan"]["plan_id"],
            reviewer="reviewer-beta",
            reason="A later reviewer must never overwrite the original approval.",
        )
    )
    assert again["reused"] is True
    assert again["release_id"] == release_id
    assert again["artifact_hash"] == stored["artifact_hash"]
    assert again["approval"]["reviewer"] == APPROVED_AT_FROM
    assert again["approval"]["approved_at"] == approval["approved_at"]
    assert again["approval"]["reason"] == APPROVAL_REASON
    assert path.read_bytes() == before
    assert len(list((entry["store"] / "releases").iterdir())) == 1


def test_fetch_refuses_missing_or_tampered_artifacts_without_fallback(
    evidence: dict, approved: dict
) -> None:
    store = approved["store"]
    release_id = approved["release_id"]

    tampered = clone_store(store, evidence["root"], "artifact-tampered")
    rewrite_json(
        artifact_path(tampered, release_id),
        lambda payload: payload["approval"].__setitem__("reason", "tampered"),
    )
    refused = rejected(fetch(tampered, release_id), "RELEASE_INTEGRITY_FAILED")
    assert refused["release_id"] == release_id

    missing = clone_store(store, evidence["root"], "artifact-missing")
    artifact_path(missing, release_id).unlink()
    rejected(fetch(missing, release_id), "RELEASE_NOT_FOUND")

    # No fallback to any other version, `latest`, or the workspace.
    rejected(fetch(store, "0" * 64), "RELEASE_NOT_FOUND")
    rejected(fetch(store, "../../etc/passwd"), "RELEASE_NOT_FOUND")


# ---------------------------------------------------------------------------
# 3. Approval does not enable the version.
# ---------------------------------------------------------------------------


def test_approval_does_not_enable_or_change_any_tracking_state(approved: dict) -> None:
    store = approved["store"]
    release_id = approved["release_id"]
    payload = ok(release_status(store, release_id))
    assert payload["status"] == "ok"
    assert payload["state"] == "published"
    assert payload["current_state"] == "published"
    assert payload["active"] is False
    assert payload["timeline"] == []
    assert payload["admission"]["valid"] is True
    assert payload["as_of"] is None
    assert not (release_dir(store, release_id) / "events").exists()
    assert not (store / "shadow").exists()

    for as_of in ("2024-01-01", "2030-01-02", "2099-12-31"):
        historical = ok(release_status(store, release_id, as_of=as_of))
        assert historical["state"] == "published"
        assert historical["current_state"] == "published"
        assert historical["active"] is False
        assert historical["timeline"] == []

    assert release.state_at(store, release_id, "2030-01-02") == "published"
    assert release.releases_active_at(store, "2030-01-02") == []
    summaries = release.list_releases(store)
    assert [entry["release_id"] for entry in summaries] == [release_id]
    assert summaries[0]["state"] == "published"
    assert summaries[0]["timeline_length"] == 0


# ---------------------------------------------------------------------------
# 4. Enable takes effect exactly at the pre-registered signal date.
# ---------------------------------------------------------------------------


def test_enable_takes_effect_exactly_at_the_preregistered_signal_date(
    evidence: dict, approved: dict
) -> None:
    release_id = approved["release_id"]
    store = clone_store(approved["store"], evidence["root"], "lifecycle-enable")
    enabled = ok(
        lifecycle(
            "enable", store, release_id, "2030-01-02",
            reason="Track from the 2030-01-02 after-close signal point.",
        )
    )
    assert enabled["status"] == "enabled"
    assert enabled["state"] == "enabled"
    assert enabled["timeline_length"] == 1
    event = enabled["event"]
    assert event["action"] == "enable"
    assert event["seq"] == 1
    assert event["effective_signal_date"] == "2030-01-02"
    assert event["reason"] == "Track from the 2030-01-02 after-close signal point."
    assert event["prev_event_hash"] is None
    assert event["state_before"] == "published"
    assert event["state_after"] == "enabled"
    assert isinstance(event["recorded_at"], str) and event["recorded_at"]
    assert event["event_hash"] == canonical_hash(
        {key: value for key, value in event.items() if key != "event_hash"}
    )
    event_path = release_dir(store, release_id) / "events" / "1.json"
    assert event_path.is_file()
    assert event_path.stat().st_mode & 0o777 == 0o444
    assert json.loads(event_path.read_text(encoding="utf-8")) == event

    before = ok(release_status(store, release_id, as_of="2030-01-01"))
    assert before["state"] == "published"
    assert before["active"] is False
    assert before["current_state"] == "enabled"
    on_the_day = ok(release_status(store, release_id, as_of="2030-01-02"))
    assert on_the_day["state"] == "enabled"
    assert on_the_day["active"] is True
    later = ok(release_status(store, release_id, as_of="2030-06-30"))
    assert later["state"] == "enabled"
    assert [entry["effective_signal_date"] for entry in later["timeline"]] == [
        "2030-01-02"
    ]
    assert release.state_at(store, release_id, "2030-01-01") == "published"
    assert release.state_at(store, release_id, "2030-01-02") == "enabled"
    assert release.releases_active_at(store, "2030-01-02")[0]["release_id"] == release_id

    # The timeline never moves backwards; an earlier date is refused.
    refused = rejected(
        lifecycle("pause", store, release_id, "2030-01-01"), "INVALID_EVENT"
    )
    assert refused["state"] == "enabled"
    assert refused["audit_id"]
    # enable is only allowed once, from the never-enabled published state.
    rejected(lifecycle("enable", store, release_id, "2030-01-03"), "INVALID_TRANSITION")
    assert len(ok(release_status(store, release_id))["timeline"]) == 1

    # pause and resume before/without the required state are refused.
    other = clone_store(approved["store"], evidence["root"], "lifecycle-early")
    rejected(lifecycle("pause", other, release_id, "2030-01-02"), "INVALID_TRANSITION")
    rejected(lifecycle("resume", other, release_id, "2030-01-02"), "INVALID_TRANSITION")

    # retire is legal from published, is terminal, and keeps the artifact.
    retired = ok(
        lifecycle("retire", other, release_id, "2030-01-05", reason="Retire before tracking.")
    )
    assert retired["state"] == "retired"
    rejected(lifecycle("enable", other, release_id, "2030-01-06"), "INVALID_TRANSITION")
    rejected(lifecycle("resume", other, release_id, "2030-01-06"), "INVALID_TRANSITION")
    rejected(lifecycle("retire", other, release_id, "2030-01-06"), "INVALID_TRANSITION")
    final = ok(release_status(other, release_id))
    assert final["state"] == "retired"
    assert final["active"] is False
    assert len(final["timeline"]) == 1
    assert artifact_path(other, release_id).is_file()


# ---------------------------------------------------------------------------
# 5. Pause, resume the same ID, and terminal retirement keep the history.
# ---------------------------------------------------------------------------


def test_pause_resume_and_retire_keep_the_same_id_and_full_history(
    evidence: dict, approved: dict
) -> None:
    release_id = approved["release_id"]
    store = clone_store(approved["store"], evidence["root"], "lifecycle-full")
    artifact_before = artifact_path(store, release_id).read_bytes()

    ok(lifecycle("enable", store, release_id, "2030-02-01"))
    paused = ok(
        lifecycle("pause", store, release_id, "2030-02-05", reason="Operator pause.")
    )
    assert paused["release_id"] == release_id
    assert paused["state"] == "paused"
    paused_timeline = ok(release_status(store, release_id))["timeline"]
    assert [entry["action"] for entry in paused_timeline] == ["enable", "pause"]
    assert paused_timeline[1]["prev_event_hash"] == paused_timeline[0]["event_hash"]
    assert (
        paused_timeline[0]["effective_signal_date"]
        <= paused_timeline[1]["effective_signal_date"]
    )
    assert ok(release_status(store, release_id, as_of="2030-02-04"))["state"] == "enabled"
    assert ok(release_status(store, release_id, as_of="2030-02-05"))["state"] == "paused"
    assert ok(release_status(store, release_id, as_of="2030-02-05"))["active"] is False
    assert release.releases_active_at(store, "2030-02-05") == []
    assert release.state_at(store, release_id, "2030-02-05") == "paused"

    resumed = ok(
        lifecycle(
            "resume", store, release_id, "2030-02-10",
            reason="Admission is still valid; resume the same ID.",
        )
    )
    assert resumed["release_id"] == release_id
    assert resumed["artifact_hash"] == approved["release"]["artifact_hash"]
    assert resumed["state"] == "enabled"
    assert resumed["timeline_length"] == 3
    assert ok(release_status(store, release_id, as_of="2030-02-09"))["state"] == "paused"
    assert ok(release_status(store, release_id, as_of="2030-02-10"))["state"] == "enabled"

    retired = ok(
        lifecycle("retire", store, release_id, "2030-02-15", reason="Version retired.")
    )
    assert retired["state"] == "retired"
    rejected(lifecycle("resume", store, release_id, "2030-02-20"), "INVALID_TRANSITION")
    rejected(lifecycle("enable", store, release_id, "2030-02-20"), "INVALID_TRANSITION")

    history = ok(release_status(store, release_id))
    assert [entry["action"] for entry in history["timeline"]] == [
        "enable",
        "pause",
        "resume",
        "retire",
    ]
    assert [entry["seq"] for entry in history["timeline"]] == [1, 2, 3, 4]
    for previous, following in zip(history["timeline"], history["timeline"][1:]):
        assert following["prev_event_hash"] == previous["event_hash"]
        assert previous["effective_signal_date"] <= following["effective_signal_date"]
    assert history["state"] == "retired"
    assert history["active"] is False
    assert ok(release_status(store, release_id, as_of="2030-02-12"))["state"] == "enabled"
    assert ok(release_status(store, release_id, as_of="2030-02-15"))["state"] == "retired"
    assert release.releases_active_at(store, "2030-02-15") == []
    assert release.state_at(store, release_id, "2030-02-15") == "retired"

    # Pause and retire never alter or delete the artifact, and the ID is reused.
    assert artifact_path(store, release_id).read_bytes() == artifact_before
    assert history["artifact_hash"] == approved["release"]["artifact_hash"]
    admission = release.admission_state(store, release_id)
    assert admission["valid"] is False
    assert admission["reason"] == "retired"
    assert admission["retired"] is True


# ---------------------------------------------------------------------------
# 6. Admission hook: #31 dispositions, and immutability re-checks on resume.
# ---------------------------------------------------------------------------


def _write_disposition(
    store: Path,
    release_id: str,
    revision_id: str,
    impact: str,
    *,
    recorded_at: str = "2030-03-06T00:00:00+00:00",
) -> Path:
    directory = release_dir(store, release_id) / "dispositions"
    directory.mkdir(parents=True, exist_ok=True)
    return write_json(
        directory / f"{revision_id}.json",
        {
            "schema_version": 1,
            "release_id": release_id,
            "revision_id": revision_id,
            "impact": impact,
            "reviewer": "synthetic-reviewer",
            "reason": f"synthetic {impact} revision",
            "recorded_at": recorded_at,
        },
    )


def test_admission_blocks_resume_for_invalid_dispositions_or_tampered_evidence(
    evidence: dict, approved: dict
) -> None:
    release_id = approved["release_id"]

    pending = clone_store(approved["store"], evidence["root"], "admission-pending")
    ok(lifecycle("enable", pending, release_id, "2030-03-01"))
    ok(lifecycle("pause", pending, release_id, "2030-03-05"))
    admission = release.admission_state(pending, release_id)
    assert admission["valid"] is True
    assert admission["reason"] is None
    assert admission["dispositions"] == []
    _write_disposition(pending, release_id, "revision-pending", "pending_review")
    admission = release.admission_state(pending, release_id)
    assert admission["valid"] is False
    assert admission["reason"] == "pending_review"
    assert admission["invalidating_dispositions"] == ["revision-pending"]
    refused = rejected(
        lifecycle("resume", pending, release_id, "2030-03-10"), "ADMISSION_INVALID"
    )
    assert refused["state"] == "paused"
    assert refused["release_id"] == release_id
    assert ok(release_status(pending, release_id))["state"] == "paused"
    # A sustained disposition cannot clear an invalidating revision.
    _write_disposition(pending, release_id, "revision-sustained", "sustained")
    assert release.admission_state(pending, release_id)["valid"] is False

    # A sustained-only release resumes at its pre-registered signal date.
    sustained = clone_store(approved["store"], evidence["root"], "admission-sustained")
    ok(lifecycle("enable", sustained, release_id, "2030-03-01"))
    ok(lifecycle("pause", sustained, release_id, "2030-03-05"))
    _write_disposition(sustained, release_id, "revision-late", "sustained")
    assert release.admission_state(sustained, release_id)["valid"] is True
    resumed = ok(lifecycle("resume", sustained, release_id, "2030-03-10"))
    assert resumed["state"] == "enabled"
    assert resumed["timeline_length"] == 3

    # no_longer_supported can never resume this ID.
    unsupported = clone_store(
        approved["store"], evidence["root"], "admission-unsupported"
    )
    ok(lifecycle("enable", unsupported, release_id, "2030-03-01"))
    ok(lifecycle("pause", unsupported, release_id, "2030-03-05"))
    _write_disposition(unsupported, release_id, "revision-gone", "no_longer_supported")
    rejected(
        lifecycle("resume", unsupported, release_id, "2030-03-10"),
        "ADMISSION_INVALID",
    )

    # A tampered or deleted bound evaluation record blocks resume.
    record_store = clone_store(approved["store"], evidence["root"], "admission-record")
    ok(lifecycle("enable", record_store, release_id, "2030-03-01"))
    ok(lifecycle("pause", record_store, release_id, "2030-03-05"))
    record_path = (
        record_store
        / "records"
        / approved["holdout"]["evaluation_id"]
        / "record.json"
    )
    rewrite_json(record_path, lambda payload: payload.__setitem__("message", "tampered"))
    rejected(
        lifecycle("resume", record_store, release_id, "2030-03-10"), "EVIDENCE_INVALID"
    )
    record_path.unlink()
    rejected(
        lifecycle("resume", record_store, release_id, "2030-03-10"), "EVIDENCE_MISSING"
    )

    # A tampered artifact blocks resume even with a valid timeline.
    artifact_store = clone_store(
        approved["store"], evidence["root"], "admission-artifact"
    )
    ok(lifecycle("enable", artifact_store, release_id, "2030-03-01"))
    ok(lifecycle("pause", artifact_store, release_id, "2030-03-05"))
    rewrite_json(
        artifact_path(artifact_store, release_id),
        lambda payload: payload["approval"].__setitem__("reviewer", "intruder"),
    )
    rejected(
        lifecycle("resume", artifact_store, release_id, "2030-03-10"),
        "RELEASE_INTEGRITY_FAILED",
    )


# ---------------------------------------------------------------------------
# 7. Multiple releases coexist and keep independent bindings and states.
# ---------------------------------------------------------------------------


def test_multiple_release_versions_coexist_and_fetch_by_exact_id(
    evidence: dict, approved: dict, approved_alt: dict
) -> None:
    store = clone_store(approved["store"], evidence["root"], "multi-release")
    first = approved["release_id"]
    second = approved_alt["release_id"]
    assert first != second
    first_artifact = artifact_path(store, first).read_bytes()
    second_artifact = artifact_path(store, second).read_bytes()
    assert first_artifact != second_artifact

    # Only the enabled version is active; the other keeps its own binding.
    ok(lifecycle("enable", store, first, "2030-04-01"))
    ok(lifecycle("enable", store, second, "2030-04-05"))
    first_status = ok(release_status(store, first, as_of="2030-04-01"))
    second_before = ok(release_status(store, second, as_of="2030-04-01"))
    assert first_status["state"] == "enabled"
    assert second_before["state"] == "published"
    assert second_before["active"] is False
    assert artifact_path(store, second).read_bytes() == second_artifact
    assert ok(release_status(store, second, as_of="2030-04-05"))["state"] == "enabled"

    active_ids = [item["release_id"] for item in release.releases_active_at(store, "2030-04-01")]
    assert active_ids == [first]
    active_ids = [item["release_id"] for item in release.releases_active_at(store, "2030-04-05")]
    assert set(active_ids) == {first, second}
    assert release.state_at(store, second, "2030-04-01") == "published"
    assert release.state_at(store, first, "2030-04-01") == "enabled"

    summaries = {item["release_id"]: item for item in release.list_releases(store)}
    assert set(summaries) == {first, second}
    assert summaries[first]["state"] == "enabled"
    assert summaries[second]["state"] == "enabled"
    assert summaries[first]["artifact_hash"] == approved["release"]["artifact_hash"]
    assert (
        summaries[second]["artifact_hash"] == approved_alt["release"]["artifact_hash"]
    )
    assert summaries[first]["candidate_id"] != summaries[second]["candidate_id"]

    # Each version fetches its own exact content; no `latest` fallback.
    fetched_first = ok(fetch(store, first))
    fetched_second = ok(fetch(store, second))
    assert fetched_first["artifact"]["candidate_id"] == (
        approved["scenario"]["candidate"]["candidate_id"]
    )
    assert fetched_second["artifact"]["candidate_id"] == (
        approved_alt["scenario"]["candidate"]["candidate_id"]
    )
    assert fetched_first["artifact_hash"] != fetched_second["artifact_hash"]
    rejected(fetch(store, "a" * 64), "RELEASE_NOT_FOUND")


def test_event_timeline_tampering_is_detected_explicitly(
    evidence: dict, approved: dict
) -> None:
    release_id = approved["release_id"]
    store = clone_store(approved["store"], evidence["root"], "timeline-tampered")
    ok(lifecycle("enable", store, release_id, "2030-05-01"))
    ok(lifecycle("pause", store, release_id, "2030-05-05"))
    events = release_dir(store, release_id) / "events"
    rewrite_json(
        events / "1.json",
        lambda payload: payload.__setitem__("effective_signal_date", "2030-05-09"),
    )
    rejected(release_status(store, release_id), "RELEASE_INTEGRITY_FAILED")
    with pytest.raises(release.ReleaseRejection) as error:
        release.state_at(store, release_id, "2030-05-10")
    assert error.value.details["code"] == "RELEASE_INTEGRITY_FAILED"

    # A deleted middle event breaks the sequence and is never silently ignored.
    # (Truncating only the newest event is undetectable without an external
    # anchor; the Shadow Live ledger provides that anchor in #32.)
    other = clone_store(approved["store"], evidence["root"], "timeline-gap")
    ok(lifecycle("enable", other, release_id, "2030-05-01"))
    ok(lifecycle("pause", other, release_id, "2030-05-05"))
    ok(lifecycle("resume", other, release_id, "2030-05-10"))
    (release_dir(other, release_id) / "events" / "2.json").unlink()
    rejected(release_status(other, release_id), "RELEASE_INTEGRITY_FAILED")


# ---------------------------------------------------------------------------
# 8. The research/agent surface has no release or admission command.
# ---------------------------------------------------------------------------


def test_agent_surface_cannot_approve_or_manage_releases(
    evidence: dict, approved: dict
) -> None:
    store = approved["store"]
    release_id = approved["release_id"]
    attempts = (
        ("release", "status", "--store", str(store), "--release-id", release_id),
        (
            "release", "approve", "--store", str(store),
            "--plan-id", approved["plan"]["plan_id"],
            "--reviewer", "agent", "--reason", "agents cannot approve",
        ),
        ("release", "fetch", "--store", str(store), "--release-id", release_id),
        (
            "release", "enable", "--store", str(store), "--release-id", release_id,
            "--effective-signal-date", "2030-06-01", "--reason", "no",
        ),
        (
            "release", "pause", "--store", str(store), "--release-id", release_id,
            "--effective-signal-date", "2030-06-01", "--reason", "no",
        ),
        (
            "release", "resume", "--store", str(store), "--release-id", release_id,
            "--effective-signal-date", "2030-06-01", "--reason", "no",
        ),
        (
            "release", "retire", "--store", str(store), "--release-id", release_id,
            "--effective-signal-date", "2030-06-01", "--reason", "no",
        ),
    )
    for arguments in attempts:
        result = cli(*arguments, agent=True)
        assert result.returncode == 2, (arguments, result.stdout, result.stderr)
        assert "invalid choice" in result.stderr
        assert result.stdout == ""

    help_result = cli("--help", agent=True)
    assert help_result.returncode == 0
    assert "release" not in help_result.stdout

    # No lifecycle event was written by any refused agent attempt.
    assert not (release_dir(store, release_id) / "events").exists()
    assert ok(release_status(store, release_id))["timeline"] == []


def test_approval_refuses_tampered_or_unbound_evidence(evidence: dict) -> None:
    source = evidence["pristine"]
    plan_id = evidence["main"]["plan"]["plan_id"]
    validation_id = evidence["main"]["validation"]["evaluation_id"]
    holdout_id = evidence["main"]["holdout"]["evaluation_id"]

    def clone(name: str) -> Path:
        return clone_store(source, evidence["root"], name)

    def refuse(store: Path, code: str) -> dict:
        payload = rejected(approve(store, plan_id), code)
        assert payload["release_created"] is False
        assert payload["audit_id"]
        assert payload["opened_protected_source"] is False
        assert not (store / "releases").exists()
        assert [audit["error"]["code"] for audit in audit_records(store)] == [code]
        return payload

    # A changed record fails its content address.
    store = clone("refuse-tampered-record")
    rewrite_json(
        store / "records" / validation_id / "record.json",
        lambda payload: payload.__setitem__("message", "tampered"),
    )
    refuse(store, "EVIDENCE_INVALID")

    # A missing record never falls back to another evaluation.
    store = clone("refuse-missing-record")
    (store / "records" / holdout_id / "record.json").unlink()
    refuse(store, "EVIDENCE_MISSING")

    # A valid record from the other stage is not this stage's evidence.
    store = clone("refuse-swapped-record")
    swapped = store / "records" / holdout_id / "record.json"
    swapped.chmod(0o644)
    swapped.write_bytes(
        (store / "records" / validation_id / "record.json").read_bytes()
    )
    refuse(store, "EVIDENCE_INVALID")

    # A changed plan no longer matches its content address.
    store = clone("refuse-tampered-plan")
    rewrite_json(
        store / "plans" / f"{plan_id}.json",
        lambda payload: payload.__setitem__("reason", "tampered"),
    )
    refuse(store, "FINGERPRINT_MISMATCH")

    # A changed authorization fails its own content address.
    store = clone("refuse-tampered-authorization")
    authorization = next(
        (store / "authorizations" / plan_id).glob("validation-*.json")
    )
    rewrite_json(
        authorization, lambda payload: payload.__setitem__("reviewer", "intruder")
    )
    refuse(store, "AUTHORIZATION_INVALID")

    # Without an authorization there is no approval at all.
    store = clone("refuse-missing-authorization")
    shutil.rmtree(store / "authorizations" / plan_id)
    refuse(store, "AUTHORIZATION_REQUIRED")
