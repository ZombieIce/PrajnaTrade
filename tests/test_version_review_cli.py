"""Public-CLI tests for version review decisions and simulated-tracking admission.

Ticket #45 (spec #43, T2): ``round review --decision approve|return|end`` and
``round revalidate``.  Every scenario drives the operator CLI (and the agent CLI
where the spec grants it) through subprocesses on synthetic sealed snapshots.
Round, campaign, snapshot and experiment fixtures are imported from the modules
that own them; none is copied here.
"""

from __future__ import annotations

import copy
import json
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from cli_helpers import agent_cli, assert_hex, cli, cli_env, ok, rejected, write_json
from prajnatrade import snapshot as snapshot_module
from test_experiment_cli import sealed_snapshot_input
from test_protected_eval_cli import submit_experiment
from test_research_loop_cli import (
    campaign_with_warmup,
    close_round,
    open_round,
    opening,
    round_input,
    round_status,
    submit_version,
    unexecutable_opens,
    validate_version,
    version_input,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
CONFLICT_RETURN = {
    "change": "Lengthen the lookback so V2 differs from V1 on the same protocol.",
    "comparison": "Compare V2 with V1 on the frozen Discovery interval and identical costs.",
    "revisit_condition": "A V2 replay that does not beat V1 restores the V1 choice.",
}


# ---------------------------------------------------------------------------
# Builders and assertions
# ---------------------------------------------------------------------------


def local_today() -> date:
    return datetime.now(SHANGHAI).date()


def calendar_payload(
    *, data_kind: str = "synthetic", start_offset: int = 0, count: int = 3
) -> dict:
    sessions = [
        (local_today() + timedelta(days=start_offset + index)).isoformat()
        for index in range(count)
    ]
    return {
        "schema_version": 1,
        "data_kind": data_kind,
        "source": "synthetic operator trading calendar",
        "sessions": sessions,
    }


def calendar_file(root: Path, *, name: str = "calendar.json", **overrides: object) -> tuple[Path, dict]:
    payload = calendar_payload(**overrides)
    return write_json(root / name, payload), payload


def observation_input(
    *,
    sessions: int = 20,
    accepted_risks: list[str] | None = None,
    reason: str = "The platform recommendation is accepted for a simulated-tracking term.",
) -> dict:
    disclosed = round_input()["disclosed_risks"]
    return {
        "observation_sessions": sessions,
        "accepted_risks": list(disclosed if accepted_risks is None else accepted_risks),
        "observation_reason": reason,
    }


def return_input(**overrides: object) -> dict:
    payload: dict[str, object] = dict(CONFLICT_RETURN)
    payload.update(overrides)
    return {key: value for key, value in payload.items() if value is not None}


def end_input(**overrides: object) -> dict:
    payload: dict[str, object] = {
        "detail": "The version is closed; the direction may continue with a new version.",
        "continue_direction": True,
    }
    payload.update(overrides)
    return payload


def review(
    project: Path,
    store: Path,
    round_id: str,
    version_id: str,
    decision: str,
    *,
    reason: str,
    reviewer: str = "research-lead",
    source: Path | None = None,
    calendar: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    args = [
        "round",
        "review",
        "--project",
        str(project),
        "--store",
        str(store),
        "--round-id",
        round_id,
        "--version-id",
        version_id,
        "--decision",
        decision,
        "--reviewer",
        reviewer,
        "--reason",
        reason,
    ]
    if source is not None:
        args += ["--input", str(source)]
    if calendar is not None:
        args += ["--calendar", str(calendar)]
    return cli(*args, env=env)


def approve_version(
    project: Path,
    store: Path,
    round_id: str,
    version_id: str,
    calendar: Path,
    source: Path,
    *,
    reason: str = "The passed receipt and explicit observation confirmation admit this version.",
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return review(
        project,
        store,
        round_id,
        version_id,
        "approve",
        reason=reason,
        source=source,
        calendar=calendar,
        env=env,
    )


def revalidate_version(
    project: Path,
    store: Path,
    snapshots: Path,
    round_id: str,
    version_id: str,
    *,
    reason: str,
) -> subprocess.CompletedProcess[str]:
    return cli(
        "round",
        "revalidate",
        "--project",
        str(project),
        "--store",
        str(store),
        "--snapshot-store",
        str(snapshots),
        "--round-id",
        round_id,
        "--version-id",
        version_id,
        "--reason",
        reason,
    )


def artifact(store: Path, release_id: str) -> dict:
    fetched = ok(
        cli("release", "fetch", "--store", str(store), "--release-id", release_id)
    )
    return fetched["artifact"]


def release_status(store: Path, release_id: str, *, as_of: str | None = None) -> dict:
    args = ["release", "status", "--store", str(store), "--release-id", release_id]
    if as_of is not None:
        args += ["--as-of", as_of]
    return ok(cli(*args))


def version_status(project: Path, round_id: str, version_id: str) -> dict:
    status = ok(round_status(project, round_id))
    return next(
        item for item in status["submissions"] if item["version_id"] == version_id
    )


def lock(
    root: Path,
    project: Path,
    round_id: str,
    submission_id: str,
    experiment_id: str,
) -> dict:
    return ok(
        submit_version(
            project,
            round_id,
            submission_id,
            write_json(root / f"{submission_id}.json", version_input(experiment_id)),
        )
    )


def corrected_payload(payload: dict, day_index: int, *, value: float = 11.5) -> dict:
    """Change one day's derived close without touching any other partition row."""
    corrected = copy.deepcopy(payload)
    target = corrected["calendar"][day_index]["date"]
    for row in corrected["derived"]:
        if row["date"] == target:
            row["value"] = value
    return corrected


def seal_payload(root: Path, name: str, payload: dict) -> tuple[Path, dict]:
    source = write_json(root / f"{name}.json", payload)
    store = root / f"{name}-store"
    sealed = ok(cli("snapshot", "seal", "--input", str(source), "--store", str(store)))
    return store, sealed


# ---------------------------------------------------------------------------
# return -> V2 does not improve -> reselect V1 with its stored receipt
# ---------------------------------------------------------------------------


def test_returned_v1_is_reselected_with_its_reused_passed_receipt(tmp_path: Path) -> None:
    project, snapshots, campaign, store, opened = opening(
        tmp_path, sealed_snapshot_input(15), warmup=4
    )
    round_id = opened["round_id"]
    short = submit_experiment(
        project,
        campaign["campaign_id"],
        tmp_path / "short.json",
        "short",
        "A short lookback is a legal synthetic version.",
        "momentum",
        {"lookback": 2, "cash_if_negative": False},
    )
    long = submit_experiment(
        project,
        campaign["campaign_id"],
        tmp_path / "long.json",
        "long",
        "A longer lookback is a legal synthetic version.",
        "momentum",
        {"lookback": 3, "cash_if_negative": False},
    )
    v1 = lock(tmp_path, project, round_id, "version-one", short["experiment_id"])
    v2 = lock(tmp_path, project, round_id, "version-two", long["experiment_id"])
    receipt1 = ok(validate_version(project, store, snapshots, round_id, v1["version_id"]))
    receipt2 = ok(validate_version(project, store, snapshots, round_id, v2["version_id"]))
    assert receipt1["outcome"] == "passed"
    assert receipt2["outcome"] == "passed"
    assert receipt1["version_id"] == v1["version_id"]
    calendar, _ = calendar_file(tmp_path, start_offset=1)

    returned = ok(
        review(
            project,
            store,
            round_id,
            v1["version_id"],
            "return",
            reason="V2 must test a longer lookback before V1 is admitted.",
            source=write_json(
                tmp_path / "return-v1.json",
                return_input(change="Compare a longer lookback against V1.", next_submission_id="version-two"),
            ),
        )
    )
    assert returned["schema_version"] == 1
    assert returned["status"] == "returned"
    assert returned["review_state"] == "returned"
    assert returned["reused"] is False
    assert_hex(returned["decision_id"])
    assert returned["return"]["change"] == "Compare a longer lookback against V1."
    assert returned["return"]["next_submission_id"] == "version-two"
    assert returned["validation"]["validation_id"] == receipt1["validation_id"]
    assert returned["validation"]["reused"] is True

    returned_v2 = ok(
        review(
            project,
            store,
            round_id,
            v2["version_id"],
            "return",
            reason="V2 does not improve the selection basis over V1.",
            source=write_json(
                tmp_path / "return-v2.json",
                return_input(change="V2 must beat V1 on the frozen protocol."),
            ),
        )
    )
    assert returned_v2["review_state"] == "returned"

    reselected = ok(
        approve_version(
            project,
            store,
            round_id,
            v1["version_id"],
            calendar,
            write_json(tmp_path / "approve-v1.json", observation_input()),
            reason="V2's replay does not improve the choice; reselect V1 on the same protocol.",
        )
    )
    assert reselected["status"] == "approved"
    assert reselected["review_state"] == "approved"
    assert reselected["reused"] is False
    assert_hex(reselected["decision_id"])
    assert_hex(reselected["tracking"]["release_id"])
    assert reselected["validation"]["validation_id"] == receipt1["validation_id"]
    assert reselected["validation"]["receipt_hash"] == receipt1["receipt_hash"]
    assert reselected["validation"]["reused"] is True
    assert reselected["observation"]["observation_sessions"] == 20
    assert reselected["observation"]["accepted_risks"] == sorted(round_input()["disclosed_risks"])
    assert reselected["observation"]["recommended_sessions"] == receipt1["observation"]["recommended_sessions"]

    status = ok(round_status(project, round_id))
    versions = {item["version_id"]: item for item in status["submissions"]}
    assert versions[v1["version_id"]]["review_state"] == "approved"
    assert versions[v1["version_id"]]["review"]["decision"] == "approve"
    assert versions[v1["version_id"]]["review"]["decision_id"] == reselected["decision_id"]
    assert (
        versions[v1["version_id"]]["tracking"]["release_id"]
        == reselected["tracking"]["release_id"]
    )
    assert versions[v1["version_id"]]["observation"]["confirmed"]["observation_sessions"] == 20
    assert versions[v2["version_id"]]["review_state"] == "returned"
    assert versions[v2["version_id"]]["tracking"] is None


# ---------------------------------------------------------------------------
# approve requires a passed receipt, an explicit observation and every risk
# ---------------------------------------------------------------------------


def test_failed_and_insufficient_evidence_can_never_be_approved(tmp_path: Path) -> None:
    hard_project, hard_snapshots, hard_campaign = campaign_with_warmup(
        tmp_path / "hard", sealed_snapshot_input(15), warmup=4, name="hard-project"
    )
    hard_store = tmp_path / "hard" / "trusted"
    hard_round_input = round_input(
        hard_conditions=[
            {"id": "impossible", "metric": "net_return", "operator": ">=", "value": 1.0}
        ]
    )
    hard_round = ok(
        open_round(
            hard_project,
            hard_store,
            hard_campaign["campaign_id"],
            write_json(tmp_path / "hard" / "round.json", hard_round_input),
            direction_key="hard-condition-trend",
        )
    )["round_id"]
    hard_experiment = submit_experiment(
        hard_project,
        hard_campaign["campaign_id"],
        tmp_path / "hard" / "experiment.json",
        "short",
        "Short lookback.",
        "momentum",
        {"lookback": 2, "cash_if_negative": False},
    )
    hard_version = lock(
        tmp_path / "hard",
        hard_project,
        hard_round,
        "version-hard",
        hard_experiment["experiment_id"],
    )
    hard_receipt = ok(
        validate_version(
            hard_project, hard_store, hard_snapshots, hard_round, hard_version["version_id"]
        )
    )
    assert hard_receipt["outcome"] == "failed"
    assert hard_receipt["reason"] == "HARD_CONDITION_NOT_MET"
    hard_calendar, _ = calendar_file(tmp_path / "hard", start_offset=1)
    hard_source = write_json(tmp_path / "hard" / "observation.json", observation_input())
    rejected(
        approve_version(
            hard_project,
            hard_store,
            hard_round,
            hard_version["version_id"],
            hard_calendar,
            hard_source,
        ),
        "VALIDATION_NOT_PASSED",
        phase="review",
    )

    insufficient_payload = sealed_snapshot_input(102)
    unexecutable_opens(insufficient_payload, 60)
    insufficient_project, insufficient_snapshots, insufficient_campaign = (
        campaign_with_warmup(
            tmp_path / "insufficient",
            insufficient_payload,
            warmup=60,
            name="insufficient-project",
        )
    )
    insufficient_store = tmp_path / "insufficient" / "trusted"
    insufficient_round = ok(
        open_round(
            insufficient_project,
            insufficient_store,
            insufficient_campaign["campaign_id"],
            write_json(tmp_path / "insufficient" / "round.json", round_input()),
        )
    )["round_id"]
    equal = submit_experiment(
        insufficient_project,
        insufficient_campaign["campaign_id"],
        tmp_path / "insufficient" / "equal.json",
        "equal",
        "Equal weights order once.",
        "equal_weight",
        {},
    )
    insufficient_version = lock(
        tmp_path / "insufficient",
        insufficient_project,
        insufficient_round,
        "version-insufficient",
        equal["experiment_id"],
    )
    insufficient_receipt = ok(
        validate_version(
            insufficient_project,
            insufficient_store,
            insufficient_snapshots,
            insufficient_round,
            insufficient_version["version_id"],
        )
    )
    assert insufficient_receipt["outcome"] == "insufficient_evidence"
    insufficient_calendar, _ = calendar_file(tmp_path / "insufficient", start_offset=1)
    insufficient_source = write_json(
        tmp_path / "insufficient" / "observation.json", observation_input()
    )
    rejected(
        approve_version(
            insufficient_project,
            insufficient_store,
            insufficient_round,
            insufficient_version["version_id"],
            insufficient_calendar,
            insufficient_source,
        ),
        "VALIDATION_NOT_PASSED",
        phase="review",
    )

    for project, round_id, version in (
        (hard_project, hard_round, hard_version),
        (insufficient_project, insufficient_round, insufficient_version),
    ):
        item = version_status(project, round_id, version["version_id"])
        assert item["review_state"] == "pending"
        assert item["review"]["decision"] is None
        assert item["tracking"] is None
    assert not (hard_store / "releases").exists()
    assert not (insufficient_store / "releases").exists()


def test_approve_requires_explicit_observation_and_every_disclosed_risk(tmp_path: Path) -> None:
    project, snapshots, campaign, store, opened = opening(
        tmp_path, sealed_snapshot_input(15), warmup=4
    )
    round_id = opened["round_id"]
    experiment = submit_experiment(
        project,
        campaign["campaign_id"],
        tmp_path / "experiment.json",
        "short",
        "Short lookback.",
        "momentum",
        {"lookback": 2, "cash_if_negative": False},
    )
    version = lock(tmp_path, project, round_id, "version-one", experiment["experiment_id"])
    receipt = ok(validate_version(project, store, snapshots, round_id, version["version_id"]))
    assert receipt["outcome"] == "passed"
    calendar, _ = calendar_file(tmp_path, start_offset=1)

    # The platform shows the recommendation; it must never pre-fill the human's
    # observation confirmation or the accepted risks.
    rejected(
        review(
            project,
            store,
            round_id,
            version["version_id"],
            "approve",
            reason="No explicit observation confirmation supplied.",
            calendar=calendar,
        ),
        "OBSERVATION_REQUIRED",
        phase="review",
    )

    missing_risk = write_json(
        tmp_path / "missing-risk.json", observation_input(accepted_risks=[])
    )
    rejected(
        approve_version(
            project, store, round_id, version["version_id"], calendar, missing_risk
        ),
        "RISK_NOT_ACCEPTED",
        phase="review",
    )

    for sessions in (0, 253):
        invalid = write_json(
            tmp_path / f"observation-{sessions}.json", observation_input(sessions=sessions)
        )
        result = approve_version(
            project, store, round_id, version["version_id"], calendar, invalid
        )
        assert result.returncode == 2, (result.stdout, result.stderr)
        assert "error:" in result.stderr
        assert result.stdout.strip() == ""

    rejected(
        review(
            project,
            store,
            round_id,
            version["version_id"],
            "approve",
            reason="No trading calendar supplied.",
            source=write_json(tmp_path / "observation.json", observation_input()),
        ),
        "CALENDAR_COVERAGE_MISSING",
        phase="review",
    )
    rejected(
        review(
            project,
            store,
            round_id,
            version["version_id"],
            "approve",
            reason="The calendar has no session after the decision date.",
            source=write_json(tmp_path / "observation.json", observation_input()),
            calendar=write_json(
                tmp_path / "past-calendar.json", calendar_payload(start_offset=-5)
            ),
        ),
        "CALENDAR_COVERAGE_MISSING",
        phase="review",
    )
    rejected(
        review(
            project,
            store,
            round_id,
            version["version_id"],
            "approve",
            reason="The calendar data kind differs from the round.",
            source=write_json(tmp_path / "observation.json", observation_input()),
            calendar=calendar_file(tmp_path, name="real-calendar.json", data_kind="real")[0],
        ),
        "CALENDAR_COVERAGE_MISSING",
        phase="review",
    )

    status = version_status(project, round_id, version["version_id"])
    assert status["review_state"] == "pending"
    assert status["review"]["decision"] is None
    assert status["observation"]["confirmed"] is None
    assert not (store / "releases").exists()


# ---------------------------------------------------------------------------
# closed-market approval: one state immediately, tracking only next session
# ---------------------------------------------------------------------------


def test_closed_market_approval_records_one_state_and_enables_next_session(
    tmp_path: Path,
) -> None:
    project, snapshots, campaign, store, opened = opening(
        tmp_path, sealed_snapshot_input(15), warmup=4
    )
    round_id = opened["round_id"]
    experiment = submit_experiment(
        project,
        campaign["campaign_id"],
        tmp_path / "experiment.json",
        "short",
        "Short lookback.",
        "momentum",
        {"lookback": 2, "cash_if_negative": False},
    )
    version = lock(tmp_path, project, round_id, "version-one", experiment["experiment_id"])
    receipt = ok(validate_version(project, store, snapshots, round_id, version["version_id"]))
    assert receipt["outcome"] == "passed"
    calendar, payload = calendar_file(tmp_path, start_offset=0)
    source = write_json(tmp_path / "observation.json", observation_input())
    before = local_today()

    approved = ok(
        approve_version(
            project,
            store,
            round_id,
            version["version_id"],
            calendar,
            source,
            reason="The passed receipt is admitted while the market is closed.",
        )
    )
    after = local_today()
    sessions = [date.fromisoformat(day) for day in payload["sessions"]]
    expected = {
        next(day for day in sessions if day > boundary).isoformat()
        for boundary in (before, after)
    }
    assert approved["status"] == "approved"
    assert approved["review_state"] == "approved"
    assert approved["reused"] is False
    assert approved["tracking"]["enabled_signal_date"] in expected
    assert_hex(approved["tracking"]["release_id"])
    assert_hex(approved["tracking"]["calendar_hash"])
    decided_local = datetime.fromisoformat(approved["decided_at"]).astimezone(SHANGHAI).date()
    assert decided_local in {before, after}
    assert (
        date.fromisoformat(approved["tracking"]["enabled_signal_date"]) > decided_local
    )
    assert approved["decided_at"] != approved["tracking"]["enabled_signal_date"]

    fetched = artifact(store, approved["tracking"]["release_id"])
    assert fetched["workflow"] == "manual_tracking"
    assert fetched["simulated_tracking"] is True
    assert fetched["kind"] == "immutable_release_artifact"
    assert fetched["release_version"] == "release-v1"
    assert fetched["scope"] == "synthetic"
    assert fetched["candidate_hash"] == version["version_id"]
    assert fetched["plan_id"] == receipt["validation_id"]
    # The artifact names the lightweight receipt explicitly; it must not read as
    # a protected evaluation plan.
    assert fetched["plan_kind"] == "lightweight_validation"
    assert fetched["validation_id"] == receipt["validation_id"]
    assert fetched["plan_hash"] == receipt["validation_id"]
    assert "evaluations" not in fetched
    assert fetched["binding"]["version_id"] == version["version_id"]
    assert fetched["binding"]["validation_id"] == receipt["validation_id"]
    assert fetched["binding"]["receipt_hash"] == receipt["receipt_hash"]
    assert fetched["binding"]["submission_id"] == "version-one"
    assert fetched["binding"]["experiment_id"] == experiment["experiment_id"]
    admission = fetched["admission"]
    assert admission["decision"] == "approved"
    assert admission["automatic_enable"] is True
    assert admission["protected_evaluation"] is False
    assert admission["conditional_release"] is False
    assert admission["effective_signal_date"] == approved["tracking"]["enabled_signal_date"]
    assert admission["calendar_hash"] == approved["tracking"]["calendar_hash"]
    assert "not a protected evaluation" in admission["statement"]
    assert fetched["execution"]["initial_cash"] == 100_000.0
    assert fetched["execution"]["strategy_family"] == "momentum"
    assert fetched["execution"]["base_costs"]["transaction_cost_bps"] == 5.0
    assert fetched["observation"]["observation_sessions"] == 20

    current = release_status(store, approved["tracking"]["release_id"])
    assert current["state"] == "enabled"
    assert current["current_state"] == "enabled"
    assert len(current["timeline"]) == 1
    event = current["timeline"][0]
    assert event["action"] == "enable"
    assert event["effective_signal_date"] == approved["tracking"]["enabled_signal_date"]
    before_tracking = release_status(
        store, approved["tracking"]["release_id"], as_of=before.isoformat()
    )
    assert before_tracking["state"] == "published"

    status = version_status(project, round_id, version["version_id"])
    assert status["review_state"] == "approved"
    assert status["review"]["decision"] == "approve"
    # Round status is agent-visible: it never leaks the human reviewer, the
    # decision reason or a fabricated committed-session count.
    assert "reviewer" not in status["review"]
    assert "reason" not in status["review"]
    assert "committed_sessions" not in status["tracking"]
    assert status["review"]["decided_at"] == approved["decided_at"]
    assert status["observation"]["confirmed"]["observation_sessions"] == 20
    assert status["tracking"]["release_id"] == approved["tracking"]["release_id"]
    assert (
        status["tracking"]["enabled_signal_date"]
        == approved["tracking"]["enabled_signal_date"]
    )

    # Decision replay is idempotent: the same decision and the same enable event.
    replayed = ok(
        approve_version(
            project,
            store,
            round_id,
            version["version_id"],
            calendar,
            source,
            reason="The passed receipt is admitted while the market is closed.",
        )
    )
    assert replayed["reused"] is True
    assert replayed["decision_id"] == approved["decision_id"]
    assert replayed["tracking"] == approved["tracking"]
    assert len(release_status(store, approved["tracking"]["release_id"])["timeline"]) == 1

    # The manual_tracking lifecycle branch re-verifies the bound Validation
    # receipt instead of trying to load a protected plan.
    release_id = approved["tracking"]["release_id"]
    enabled = approved["tracking"]["enabled_signal_date"]
    ok(
        cli(
            "release",
            "pause",
            "--store",
            str(store),
            "--release-id",
            release_id,
            "--effective-signal-date",
            enabled,
            "--reason",
            "The operator pauses only this simulated version.",
        )
    )
    resumed = ok(
        cli(
            "release",
            "resume",
            "--store",
            str(store),
            "--release-id",
            release_id,
            "--effective-signal-date",
            enabled,
            "--reason",
            "The bound validation still supports simulated tracking.",
        )
    )
    assert resumed["state"] == "enabled"
    timeline = release_status(store, release_id)["timeline"]
    assert [event["action"] for event in timeline] == ["enable", "pause", "resume"]


# ---------------------------------------------------------------------------
# approval replay is date-independent: it reuses the first stored enable date
# ---------------------------------------------------------------------------


def test_approval_replay_uses_the_stored_effective_session_across_days(
    tmp_path: Path,
) -> None:
    project, snapshots, campaign, store, opened = opening(
        tmp_path, sealed_snapshot_input(15), warmup=4
    )
    round_id = opened["round_id"]
    experiment = submit_experiment(
        project,
        campaign["campaign_id"],
        tmp_path / "experiment.json",
        "short",
        "Short lookback.",
        "momentum",
        {"lookback": 2, "cash_if_negative": False},
    )
    version = lock(tmp_path, project, round_id, "version-one", experiment["experiment_id"])
    receipt = ok(validate_version(project, store, snapshots, round_id, version["version_id"]))
    assert receipt["outcome"] == "passed"
    calendar, _ = calendar_file(tmp_path, start_offset=1, count=3)
    source = write_json(tmp_path / "observation.json", observation_input())
    today = local_today()

    approved = ok(
        approve_version(
            project,
            store,
            round_id,
            version["version_id"],
            calendar,
            source,
            env=cli_env(PRAJNATRADE_TEST_NOW=f"{today.isoformat()}T08:00:00+08:00"),
        )
    )
    assert approved["reused"] is False
    assert approved["tracking"]["enabled_signal_date"] == (
        today + timedelta(days=1)
    ).isoformat()

    # The same human decision is replayed on the next trading day: the key is
    # the decision content, so the stored first-computed enable date wins.
    replayed = ok(
        approve_version(
            project,
            store,
            round_id,
            version["version_id"],
            calendar,
            source,
            env=cli_env(
                PRAJNATRADE_TEST_NOW=f"{(today + timedelta(days=1)).isoformat()}T08:00:00+08:00"
            ),
        )
    )
    assert replayed["reused"] is True
    assert replayed["decision_id"] == approved["decision_id"]
    assert replayed["decided_at"] == approved["decided_at"]
    assert replayed["tracking"] == approved["tracking"]
    release_id = approved["tracking"]["release_id"]
    timeline = release_status(store, release_id)["timeline"]
    assert [event["action"] for event in timeline] == ["enable"]
    assert timeline[0]["effective_signal_date"] == approved["tracking"]["enabled_signal_date"]


# ---------------------------------------------------------------------------
# a release-layer refusal from round review renders as a review ProtocolError
# ---------------------------------------------------------------------------


def test_review_release_refusal_is_rendered_as_a_review_rejection(
    tmp_path: Path,
) -> None:
    project, snapshots, campaign, store, opened = opening(
        tmp_path, sealed_snapshot_input(15), warmup=4
    )
    round_id = opened["round_id"]
    experiment = submit_experiment(
        project, campaign["campaign_id"], tmp_path / "experiment.json", "short",
        "Short lookback.", "momentum", {"lookback": 2, "cash_if_negative": False},
    )
    version = lock(tmp_path, project, round_id, "version-one", experiment["experiment_id"])
    ok(validate_version(project, store, snapshots, round_id, version["version_id"]))
    calendar, _ = calendar_file(tmp_path, start_offset=1)
    source = write_json(tmp_path / "observation.json", observation_input())
    approved = ok(
        approve_version(
            project, store, round_id, version["version_id"], calendar, source
        )
    )
    release_id = approved["tracking"]["release_id"]
    artifact_path = store / "releases" / release_id / "artifact.json"
    original = artifact_path.read_bytes()

    tampered = json.loads(original)
    tampered["observation"]["observation_sessions"] = 1
    artifact_path.chmod(0o600)
    artifact_path.write_text(json.dumps(tampered), encoding="utf-8")

    payload = rejected(
        approve_version(
            project, store, round_id, version["version_id"], calendar, source
        ),
        "RELEASE_INTEGRITY_FAILED",
        phase="review",
    )
    # The protocol context is rendered at the top level, never dropped.
    assert payload["release_id"] == release_id
    assert payload["release_created"] is False
    assert payload["opened_protected_source"] is False
    assert payload["query_debited"] is False
    assert "audit_id" not in payload or isinstance(payload["audit_id"], str)

    artifact_path.chmod(0o600)
    artifact_path.write_bytes(original)
    artifact_path.chmod(0o444)
    replay = ok(
        approve_version(
            project, store, round_id, version["version_id"], calendar, source
        )
    )
    assert replay["reused"] is True
    assert replay["decision_id"] == approved["decision_id"]


# ---------------------------------------------------------------------------
# a tracking release verifies its bound terminal passed Validation receipt
# ---------------------------------------------------------------------------


def _receipt_path(store: Path, validation_id: str) -> Path:
    return store / "research-loop-validations" / f"{validation_id}.json"


def _rewrite_receipt(path: Path, receipt: dict) -> None:
    path.chmod(0o600)
    path.write_text(json.dumps(receipt), encoding="utf-8")
    path.chmod(0o400)


def test_tracking_release_rejects_a_tampered_or_non_passed_receipt(
    tmp_path: Path,
) -> None:
    project, snapshots, campaign, store, opened = opening(
        tmp_path, sealed_snapshot_input(15), warmup=4
    )
    round_id = opened["round_id"]
    experiment = submit_experiment(
        project, campaign["campaign_id"], tmp_path / "experiment.json", "short",
        "Short lookback.", "momentum", {"lookback": 2, "cash_if_negative": False},
    )
    version = lock(tmp_path, project, round_id, "version-one", experiment["experiment_id"])
    receipt = ok(validate_version(project, store, snapshots, round_id, version["version_id"]))
    approved = ok(
        approve_version(
            project,
            store,
            round_id,
            version["version_id"],
            calendar_file(tmp_path, start_offset=1)[0],
            write_json(tmp_path / "observation.json", observation_input()),
        )
    )
    release_id = approved["tracking"]["release_id"]
    enabled = approved["tracking"]["enabled_signal_date"]
    path = _receipt_path(store, receipt["validation_id"])
    original = json.loads(path.read_text(encoding="utf-8"))
    original_bytes = path.read_bytes()

    def rejects_every_reader() -> None:
        rejected(
            cli("release", "fetch", "--store", str(store), "--release-id", release_id),
            "EVIDENCE_INVALID",
        )
        rejected(
            cli("release", "status", "--store", str(store), "--release-id", release_id),
            "EVIDENCE_INVALID",
        )
        rejected(
            cli(
                "release", "resume", "--store", str(store), "--release-id", release_id,
                "--effective-signal-date", enabled,
                "--reason", "A broken receipt must never be resumed.",
            ),
            "EVIDENCE_INVALID",
        )

    # A field changed without recomputing the content address is tampering.
    tampered = copy.deepcopy(original)
    tampered["observation"]["recommended_sessions"] = 1
    _rewrite_receipt(path, tampered)
    rejects_every_reader()

    # A hash-consistent but non-passed receipt is also refused: a manual-tracking
    # release may only bind a passed lightweight Validation.
    failed = copy.deepcopy(original)
    failed["outcome"] = "failed"
    failed["reason"] = "HARD_CONDITION_NOT_MET"
    failed["receipt_hash"] = snapshot_module._hash(
        {key: value for key, value in failed.items() if key != "receipt_hash"}
    )
    _rewrite_receipt(path, failed)
    rejects_every_reader()

    path.chmod(0o600)
    path.write_bytes(original_bytes)
    path.chmod(0o400)
    fetched = ok(
        cli("release", "fetch", "--store", str(store), "--release-id", release_id)
    )
    assert fetched["artifact"]["binding"]["validation_id"] == receipt["validation_id"]


# ---------------------------------------------------------------------------
# end is terminal for one version only; the round keeps accepting versions
# ---------------------------------------------------------------------------


def test_end_is_terminal_for_one_version_and_the_round_continues(tmp_path: Path) -> None:
    project, snapshots, campaign, store, opened = opening(
        tmp_path, sealed_snapshot_input(15), warmup=4
    )
    round_id = opened["round_id"]
    experiments = {
        "one": submit_experiment(
            project, campaign["campaign_id"], tmp_path / "one.json", "one",
            "Short lookback.", "momentum", {"lookback": 2, "cash_if_negative": False},
        ),
        "two": submit_experiment(
            project, campaign["campaign_id"], tmp_path / "two.json", "two",
            "Longer lookback.", "momentum", {"lookback": 3, "cash_if_negative": False},
        ),
        "three": submit_experiment(
            project, campaign["campaign_id"], tmp_path / "three.json", "three",
            "Equal weights.", "equal_weight", {},
        ),
    }
    versions = {
        name: lock(tmp_path, project, round_id, f"version-{name}", experiment["experiment_id"])
        for name, experiment in experiments.items()
    }
    first = ok(validate_version(project, store, snapshots, round_id, versions["one"]["version_id"]))
    assert first["outcome"] == "passed"
    source = write_json(tmp_path / "end.json", end_input())
    ended = ok(
        review(
            project,
            store,
            round_id,
            versions["one"]["version_id"],
            "end",
            reason="The direction continues with a new version.",
            source=source,
        )
    )
    assert ended["status"] == "ended"
    assert ended["review_state"] == "ended"
    assert ended["end"]["detail"] == end_input()["detail"]
    assert ended["end"]["continue_direction"] is True
    assert_hex(ended["decision_id"])

    replayed = ok(
        review(
            project,
            store,
            round_id,
            versions["one"]["version_id"],
            "end",
            reason="The direction continues with a new version.",
            source=source,
        )
    )
    assert replayed["reused"] is True
    assert replayed["decision_id"] == ended["decision_id"]

    calendar, _ = calendar_file(tmp_path, start_offset=1)
    rejected(
        approve_version(
            project,
            store,
            round_id,
            versions["one"]["version_id"],
            calendar,
            write_json(tmp_path / "observation.json", observation_input()),
        ),
        "VERSION_ENDED",
        phase="review",
    )
    rejected(
        review(
            project,
            store,
            round_id,
            versions["one"]["version_id"],
            "return",
            reason="An ended version cannot be returned.",
            source=write_json(tmp_path / "return-ended.json", return_input()),
        ),
        "VERSION_ENDED",
        phase="review",
    )

    second = ok(validate_version(project, store, snapshots, round_id, versions["two"]["version_id"]))
    assert second["outcome"] == "passed"
    approved = ok(
        approve_version(
            project,
            store,
            round_id,
            versions["two"]["version_id"],
            calendar,
            write_json(tmp_path / "observation-two.json", observation_input()),
        )
    )
    assert approved["review_state"] == "approved"

    third = ok(
        submit_version(
            project,
            round_id,
            "version-four",
            write_json(
                tmp_path / "version-four.json",
                version_input(experiments["three"]["experiment_id"]),
            ),
        )
    )
    assert third["status"] == "submitted"
    status = ok(round_status(project, round_id))
    assert status["status"] == "open"
    states = {item["version_id"]: item["review_state"] for item in status["submissions"]}
    assert states[versions["one"]["version_id"]] == "ended"
    assert states[versions["two"]["version_id"]] == "approved"
    assert states[third["version_id"]] == "pending"


# ---------------------------------------------------------------------------
# replay, conflict and typed errors
# ---------------------------------------------------------------------------


def test_review_decisions_replay_and_conflicting_decisions_are_refused(
    tmp_path: Path,
) -> None:
    project, snapshots, campaign, store, opened = opening(
        tmp_path, sealed_snapshot_input(15), warmup=4
    )
    round_id = opened["round_id"]
    experiment = submit_experiment(
        project,
        campaign["campaign_id"],
        tmp_path / "experiment.json",
        "short",
        "Short lookback.",
        "momentum",
        {"lookback": 2, "cash_if_negative": False},
    )
    version = lock(tmp_path, project, round_id, "version-one", experiment["experiment_id"])
    receipt = ok(validate_version(project, store, snapshots, round_id, version["version_id"]))
    assert receipt["outcome"] == "passed"
    calendar, _ = calendar_file(tmp_path, start_offset=1)

    return_source = write_json(tmp_path / "return.json", return_input())
    returned = ok(
        review(
            project,
            store,
            round_id,
            version["version_id"],
            "return",
            reason="V2 must be compared under the same protocol.",
            source=return_source,
        )
    )
    replay = ok(
        review(
            project,
            store,
            round_id,
            version["version_id"],
            "return",
            reason="V2 must be compared under the same protocol.",
            source=return_source,
        )
    )
    assert replay["reused"] is True
    assert replay["decision_id"] == returned["decision_id"]
    rejected(
        review(
            project,
            store,
            round_id,
            version["version_id"],
            "return",
            reason="V2 must be compared under the same protocol.",
            source=write_json(
                tmp_path / "return-conflict.json",
                return_input(change="A different return instruction."),
            ),
        ),
        "REVIEW_CONFLICT",
        phase="review",
    )

    approval_source = write_json(tmp_path / "observation.json", observation_input())
    approved = ok(
        approve_version(
            project,
            store,
            round_id,
            version["version_id"],
            calendar,
            approval_source,
        )
    )
    assert approved["reused"] is False
    replay_approved = ok(
        approve_version(
            project,
            store,
            round_id,
            version["version_id"],
            calendar,
            approval_source,
        )
    )
    assert replay_approved["reused"] is True
    assert replay_approved["decision_id"] == approved["decision_id"]
    assert replay_approved["tracking"] == approved["tracking"]
    rejected(
        review(
            project,
            store,
            round_id,
            version["version_id"],
            "approve",
            reason="A different approval reason is a conflicting decision.",
            source=approval_source,
            calendar=calendar,
        ),
        "REVIEW_CONFLICT",
        phase="review",
    )
    rejected(
        review(
            project,
            store,
            round_id,
            version["version_id"],
            "return",
            reason="An approved version only ends, it cannot return.",
            source=write_json(tmp_path / "return-approved.json", return_input()),
        ),
        "REVIEW_CONFLICT",
        phase="review",
    )

    missing = review(
        project,
        store,
        round_id,
        "a" * 64,
        "return",
        reason="Unknown version.",
        source=write_json(tmp_path / "unknown.json", return_input()),
    )
    rejected(missing, "SUBMISSION_NOT_FOUND", phase="review")

    for decision in ("approve", "return", "end"):
        result = cli(
            "round",
            "review",
            "--project",
            str(project),
            "--store",
            str(store),
            "--round-id",
            round_id,
            "--version-id",
            version["version_id"],
            "--decision",
            decision,
        )
        assert result.returncode == 2
        assert "required" in result.stderr

    close_source = write_json(
        tmp_path / "close.json",
        {
            "stop_reason": "No remaining testable version could change the choice.",
            "selected_version": None,
            "next_experiment": None,
        },
    )
    ok(close_round(project, round_id, close_source))
    # Replaying an already-recorded decision is still idempotent after closure;
    # only a new decision is refused.
    replayed_after_close = ok(
        approve_version(
            project,
            store,
            round_id,
            version["version_id"],
            calendar,
            approval_source,
        )
    )
    assert replayed_after_close["reused"] is True
    assert replayed_after_close["decision_id"] == approved["decision_id"]
    rejected(
        review(
            project,
            store,
            round_id,
            version["version_id"],
            "end",
            reason="A closed round refuses new review decisions.",
            source=write_json(tmp_path / "end-closed.json", end_input()),
        ),
        "ROUND_CLOSED",
        phase="review",
    )


# ---------------------------------------------------------------------------
# platform-fix revalidation
# ---------------------------------------------------------------------------


def test_revalidate_after_a_corrected_snapshot_records_prior_receipt(tmp_path: Path) -> None:
    payload = sealed_snapshot_input(15)
    project, snapshots, campaign, store, opened = opening(tmp_path, payload, warmup=4)
    round_id = opened["round_id"]
    experiment = submit_experiment(
        project,
        campaign["campaign_id"],
        tmp_path / "experiment.json",
        "short",
        "Short lookback.",
        "momentum",
        {"lookback": 2, "cash_if_negative": False},
    )
    version = lock(tmp_path, project, round_id, "version-one", experiment["experiment_id"])
    prior = ok(validate_version(project, store, snapshots, round_id, version["version_id"]))
    assert prior["outcome"] == "passed"
    ok(
        review(
            project,
            store,
            round_id,
            version["version_id"],
            "return",
            reason="Hold the version until the evidence source is rechecked.",
            source=write_json(tmp_path / "return.json", return_input()),
        )
    )

    # A correction inside Discovery is not a platform fix that can be replayed:
    # the original selection evidence no longer holds.
    broken_store, _ = seal_payload(
        tmp_path, "broken-snapshots", corrected_payload(payload, 3)
    )
    rejected(
        revalidate_version(
            project,
            store,
            broken_store,
            round_id,
            version["version_id"],
            reason="A corrected Discovery partition.",
        ),
        "REPLAY_MISMATCH",
        phase="revalidation",
    )
    assert (
        version_status(project, round_id, version["version_id"])["validation"]["validation_id"]
        == prior["validation_id"]
    )

    # Correcting only the later partitions keeps the Discovery replay and version
    # identity, but changes the bound evidence source and forces a fresh receipt.
    # Sealing the corrected snapshot into the same store leaves two snapshots
    # that reproduce the frozen Discovery partition; the locked one is not the
    # fresh evidence source.
    corrected = ok(
        cli(
            "snapshot",
            "seal",
            "--input",
            str(write_json(tmp_path / "corrected.json", corrected_payload(payload, 10))),
            "--store",
            str(snapshots),
        )
    )
    revalidated = ok(
        revalidate_version(
            project,
            store,
            snapshots,
            round_id,
            version["version_id"],
            reason="Platform fix corrected the protected evidence source; Discovery is unchanged.",
        )
    )
    assert revalidated["status"] == "revalidated"
    assert revalidated["reused"] is False
    assert revalidated["round_id"] == round_id
    assert revalidated["version_id"] == version["version_id"]
    assert revalidated["previous_validation_id"] == prior["validation_id"]
    assert revalidated["validation_id"] != prior["validation_id"]
    assert revalidated["previous_receipt_hash"] == prior["receipt_hash"]
    assert revalidated["strategy_fingerprint"] == version["strategy_fingerprint"]
    assert revalidated["outcome"] == "passed"
    assert revalidated["reason"] is None
    assert revalidated["review_state"] == "returned"
    assert_hex(revalidated["revalidation_id"])
    evidence = revalidated["evidence"]
    assert evidence["snapshot_changed"] is True
    assert evidence["snapshot_id"] == corrected["snapshot_id"]
    assert evidence["previous_snapshot_id"] == campaign["snapshot_id"]
    assert evidence["runtime_changed"] is False
    assert_hex(evidence["replay_engine_identity"])

    # The prior receipt is kept as historical evidence in the trusted store.
    prior_copy = store / "research-loop-validations" / f"{prior['validation_id']}.json"
    assert prior_copy.is_file()
    new_copy = store / "research-loop-validations" / f"{revalidated['validation_id']}.json"
    assert new_copy.is_file()
    revalidation_copy = (
        store / "research-loop-revalidations" / f"{revalidated['revalidation_id']}.json"
    )
    assert revalidation_copy.is_file()
    recorded = json.loads(revalidation_copy.read_text(encoding="utf-8"))
    assert recorded["prior_validation_id"] == prior["validation_id"]
    assert recorded["prior_receipt"]["validation_id"] == prior["validation_id"]
    assert recorded["prior_receipt"]["receipt_hash"] == prior["receipt_hash"]
    assert recorded["validation_id"] == revalidated["validation_id"]

    current = version_status(project, round_id, version["version_id"])
    assert current["review_state"] == "returned"
    assert current["validation"]["validation_id"] == revalidated["validation_id"]
    assert current["validation"]["outcome"] == "passed"
    assert current["validation"]["revalidated_from"] == prior["validation_id"]

    replayed = ok(
        revalidate_version(
            project,
            store,
            snapshots,
            round_id,
            version["version_id"],
            reason="Platform fix corrected the protected evidence source; Discovery is unchanged.",
        )
    )
    assert replayed["reused"] is True
    assert replayed["revalidation_id"] == revalidated["revalidation_id"]
    assert replayed["validation_id"] == revalidated["validation_id"]

    calendar, _ = calendar_file(tmp_path, start_offset=1)
    approved = ok(
        approve_version(
            project,
            store,
            round_id,
            version["version_id"],
            calendar,
            write_json(tmp_path / "observation.json", observation_input()),
            reason="The revalidated passed receipt admits the version.",
        )
    )
    assert approved["validation"]["validation_id"] == revalidated["validation_id"]
    assert approved["validation"]["reused"] is True

    # An approved version can still be re-checked after a data/platform fix; a
    # passing corrected receipt keeps the one current review state approved.
    approved_revalidation = ok(
        revalidate_version(
            project,
            store,
            snapshots,
            round_id,
            version["version_id"],
            reason="An approved version is revalidated after the platform fix.",
        )
    )
    assert approved_revalidation["status"] == "revalidated"
    assert approved_revalidation["review_state"] == "approved"
    assert approved_revalidation["outcome"] == "passed"
    assert approved_revalidation["validation_id"] != revalidated["validation_id"]
    current = version_status(project, round_id, version["version_id"])
    assert current["review_state"] == "approved"
    assert current["validation"]["review"] == {
        "required": False,
        "trigger": None,
        "reason": None,
    }

    ok(
        review(
            project,
            store,
            round_id,
            version["version_id"],
            "end",
            reason="Retire the simulated-tracking version after the review.",
            source=write_json(tmp_path / "end.json", end_input()),
        )
    )
    rejected(
        revalidate_version(
            project,
            store,
            snapshots,
            round_id,
            version["version_id"],
            reason="An ended version cannot be revalidated.",
        ),
        "VERSION_ENDED",
        phase="revalidation",
    )


# ---------------------------------------------------------------------------
# agent boundary
# ---------------------------------------------------------------------------


def test_agent_round_surface_stays_status_and_submit_only(tmp_path: Path) -> None:
    project, snapshots, campaign, store, opened = opening(
        tmp_path, sealed_snapshot_input(15), warmup=4
    )
    round_id = opened["round_id"]
    experiment = submit_experiment(
        project,
        campaign["campaign_id"],
        tmp_path / "experiment.json",
        "short",
        "Short lookback.",
        "momentum",
        {"lookback": 2, "cash_if_negative": False},
    )
    version = lock(tmp_path, project, round_id, "version-one", experiment["experiment_id"])
    receipt = ok(validate_version(project, store, snapshots, round_id, version["version_id"]))
    calendar, _ = calendar_file(tmp_path, start_offset=1)
    approved = ok(
        approve_version(
            project,
            store,
            round_id,
            version["version_id"],
            calendar,
            write_json(tmp_path / "observation.json", observation_input()),
        )
    )

    for args in (
        (
            "round", "review", "--project", str(project), "--store", str(store),
            "--round-id", round_id, "--version-id", version["version_id"],
            "--decision", "end", "--reviewer", "lead", "--reason", "stop",
        ),
        (
            "round", "revalidate", "--project", str(project), "--store", str(store),
            "--snapshot-store", str(snapshots), "--round-id", round_id,
            "--version-id", version["version_id"], "--reason", "fix",
        ),
    ):
        result = agent_cli(*args)
        assert result.returncode == 2
        assert "invalid choice" in result.stderr

    result = agent_cli(
        "round", "status", "--project", str(project), "--store", str(store)
    )
    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr

    status = ok(
        agent_cli("round", "status", "--project", str(project), "--round-id", round_id)
    )
    item = status["submissions"][0]
    assert item["review_state"] == "approved"
    assert item["validation"]["validation_id"] == receipt["validation_id"]
    assert item["tracking"]["release_id"] == approved["tracking"]["release_id"]
    assert (
        item["tracking"]["enabled_signal_date"]
        == approved["tracking"]["enabled_signal_date"]
    )
    rendered = json.dumps(status)
    for forbidden in (
        "equity_curve",
        "ledger",
        "artifact_path",
        "release_path",
        "query_debited",
        "opened_protected_source",
        "evaluations",
        "fills",
    ):
        assert forbidden not in rendered
