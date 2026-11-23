"""Public workflow checks for the direction-bound Final Holdout."""

from pathlib import Path

import pytest

from cli_helpers import cli, ok, rejected, write_json


def test_holdout_requires_an_existing_frozen_selection(tmp_path: Path) -> None:
    project = tmp_path / "project"
    ok(cli("project", "init", "--project", str(project)))
    calendar = write_json(tmp_path / "calendar.json", {
        "schema_version": 1,
        "data_kind": "synthetic",
        "source": "fixture calendar",
        "sessions": ["2026-12-01", "2026-12-02"],
    })
    result = rejected(cli(
        "holdout", "authorize", "--store", str(tmp_path / "trusted"),
        "--snapshot-store", str(tmp_path / "snapshots"), "--project", str(project),
        "--round-id", "a" * 64, "--validation-evaluation-id", "unknown",
        "--reviewer", "research lead", "--reason", "Review the frozen selection",
        "--selection-independent", "yes", "--independence-reason", "No prior exposure",
        "--calendar", str(calendar),
    ), "SELECTION_REQUIRED")
    assert result["query_debited"] is False
    assert result["opened_protected_source"] is False


def _calendar(root):
    from datetime import datetime, timedelta

    from prajnatrade.snapshot_baseline import CN_TZ

    today = datetime.now(CN_TZ).date()
    return write_json(root / "calendar.json", {
        "schema_version": 1,
        "data_kind": "synthetic",
        "source": "fixture trading calendar",
        "sessions": [(today + timedelta(days=offset)).isoformat() for offset in range(1, 5)],
    })


def _selected_round(root, store, sealed, key, *, independent=True, protocol=None):
    from test_protected_eval_cli import create_campaign, plan_input, submit_experiment

    project = root / "project"
    ok(cli("project", "init", "--project", str(project)))
    campaign = create_campaign(project, sealed["store"], sealed["snapshot_id"])
    kickoff = write_json(root / "kickoff.json", {
        "objective": "Check an equal-weight research direction.",
        "constraints": ["Use the Campaign's fixed fees and sealed snapshot."],
        "validation_budget": 2,
        "reviewer": "Research lead",
        "approval_reason": "Authorize the comparison protocol and budget.",
        "validation_settings": protocol or plan_input(),
        "independence": {"independent": independent, "reason": "Recorded design provenance."},
    })
    approved = ok(cli(
        "direction", "kickoff", "--project", str(project), "--store", str(store),
        "--campaign-id", campaign["campaign_id"], "--direction-key", key,
        "--name", root.name, "--authorization-id", f"review-{root.name}", "--input", str(kickoff),
    ))
    submission = submit_experiment(
        project, campaign["campaign_id"], root / "discovery.json",
        "equal", "Equal weights preserve value in this fixture.", "equal_weight", {},
    )
    proposal = write_json(root / "proposal.json", {
        "change": "Use an equal-weight allocation.",
        "question": "Does the version meet the return and drawdown floors?",
        "expected_change": "The version should match the benchmark.",
        "related_experiment_ids": [],
    })
    ok(cli(
        "validation", "propose", "--project", str(project), "--round-id", approved["round_id"],
        "--submission-id", submission["submission_id"], "--input", str(proposal), agent=True,
    ))
    result = ok(cli(
        "validation", "service", "--store", str(store), "--snapshot-store", str(sealed["store"]),
        "--project", str(project), "--round-id", approved["round_id"],
    ))["results"][0]
    assert result["outcome"] == "passed"
    selection = write_json(root / "selection.json", {
        "selection_reason": "Passed the comparison and meets the simplicity objective.",
        "remaining_risks": ["The benchmark protects no downside."],
        "eliminations": {},
        "independence": {"independent": independent, "reason": "Recorded selection provenance."},
    })
    candidate = ok(cli(
        "direction", "freeze", "--project", str(project), "--round-id", approved["round_id"],
        "--evaluation-id", result["evaluation_id"], "--name", root.name, "--input", str(selection),
        agent=True,
    ))
    return {
        "project": project, "round_id": approved["round_id"],
        "evaluation_id": result["evaluation_id"], "candidate_id": candidate["candidate_id"],
    }


def _authorize(store, sealed, selection, calendar, *, evaluation_id=None, independent=True):
    return cli(
        "holdout", "authorize", "--store", str(store), "--snapshot-store", str(sealed["store"]),
        "--project", str(selection["project"]), "--round-id", selection["round_id"],
        "--validation-evaluation-id", evaluation_id or selection["evaluation_id"],
        "--reviewer", "research lead", "--reason", "Test the frozen selection",
        "--selection-independent", "yes" if independent else "no",
        "--independence-reason", "Recorded lead review of previous results.",
        "--calendar", str(calendar),
    )


def _run(store, sealed, selection, plan_id):
    return cli(
        "holdout", "run", "--store", str(store), "--snapshot-store", str(sealed["store"]),
        "--project", str(selection["project"]), "--plan-id", plan_id,
    )


def _agent_receipt(selection):
    return ok(cli(
        "evaluation", "status", "--project", str(selection["project"]),
        "--candidate-id", selection["candidate_id"], agent=True,
    ))["final_holdout"]


@pytest.mark.isolation
def test_independent_directions_share_a_range_and_retries_release_once(tmp_path: Path) -> None:
    import json
    from concurrent.futures import ThreadPoolExecutor

    from test_protected_eval_cli import seal_snapshot, synthetic_snapshot

    sealed = seal_snapshot(tmp_path, synthetic_snapshot())
    store = tmp_path / "operator"
    calendar = _calendar(tmp_path)
    first = _selected_round(tmp_path / "first", store, sealed, "Relative Strength")
    second = _selected_round(tmp_path / "second", store, sealed, "Volatility")
    baseline = _agent_receipt(first)

    wrong = rejected(_authorize(
        store, sealed, first, calendar, evaluation_id=second["evaluation_id"],
    ), "VALIDATION_SELECTION_MISMATCH")
    assert wrong["query_debited"] is False
    assert wrong["opened_protected_source"] is False

    with ThreadPoolExecutor(max_workers=2) as jobs:
        approved, repeated = list(jobs.map(
            lambda _: ok(_authorize(store, sealed, first, calendar)), range(2),
        ))
        assert approved["authorization"]["auth_id"] == repeated["authorization"]["auth_id"]
        first_result, retry = list(jobs.map(
            lambda _: ok(_run(store, sealed, first, approved["plan_id"])), range(2),
        ))
    assert first_result["evaluation_id"] == retry["evaluation_id"]
    assert first_result["outcome"] == "passed"
    assert first_result["release"]["release_id"] == retry["release"]["release_id"]
    assert {first_result["query"]["reused"], retry["query"]["reused"]} == {False, True}
    assert _agent_receipt(first) == baseline

    second_approval = ok(_authorize(store, sealed, second, calendar))
    previous, = second_approval["authorization"]["prior_exposures"]
    assert previous["evaluation_id"] == first_result["evaluation_id"]
    second_result = ok(_run(store, sealed, second, second_approval["plan_id"]))
    assert second_result["outcome"] == "passed"
    assert second_result["release"]["release_id"] != first_result["release"]["release_id"]

    output = tmp_path / "controlled.json"
    ok(cli("evaluation", "report", "--store", str(store), "--plan-id", approved["plan_id"],
           "--output", str(output)))
    report = json.loads(output.read_text())
    assert report["workflow"] == "research_holdout"
    assert report["validation_binding"]["evaluation_id"] == first["evaluation_id"]
    assert report["query_receipts"]["final_holdout"]["evaluation_id"] == first_result["evaluation_id"]
    assert report["verdict"]["all_stages_passed"] is True

    renamed = _selected_round(tmp_path / "renamed", store, sealed, "relative-strength!")
    influence = _selected_round(tmp_path / "affected", store, sealed, "Prior feedback", independent=False)
    for attempted, independent, expected in (
        (renamed, True, "DIRECTION_ALREADY_EVALUATED"),
        (influence, False, "UNSEEN_HOLDOUT_REQUIRED"),
    ):
        result = _authorize(store, sealed, attempted, calendar, independent=independent)
        assert result.returncode == 2, result.stderr
        blocked = json.loads(result.stdout)
        assert blocked["status"] == "blocked"
        assert blocked["error"]["code"] == expected
        assert blocked["query_debited"] is False
        assert blocked["opened_protected_source"] is False


@pytest.mark.isolation
def test_holdout_rejection_ends_round_with_retrospective_and_no_release(tmp_path: Path) -> None:
    from test_protected_eval_cli import plan_input, seal_snapshot, synthetic_snapshot

    source = synthetic_snapshot()
    for row in source["raw_bars"]:
        if row["date"] == "2024-01-20":
            row.update({"low": 3, "close": 3})
    sealed = seal_snapshot(tmp_path, source)
    store = tmp_path / "operator"
    selection = _selected_round(tmp_path / "decline", store, sealed, "Absolute floor", protocol=plan_input(
        thresholds=[{"id": "return-floor", "metric": "total_return", "op": ">=", "value": -0.1}],
    ))
    approved = ok(_authorize(store, sealed, selection, _calendar(tmp_path)))
    result = ok(_run(store, sealed, selection, approved["plan_id"]))
    assert result["outcome"] == "rejected"
    assert result["retrospective"]["status"] == "round_ended"
    assert result["retrospective"]["thresholds"][0]["status"] == "failed"
    assert "release" not in result
    assert ok(cli("release", "list", "--store", str(store)))["releases"] == []
