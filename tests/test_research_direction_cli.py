"""Public receipts for research direction kickoff and Discovery handoff."""

from pathlib import Path

import pytest
from cli_helpers import agent_cli, cli, ok, rejected, write_json
from test_protected_eval_cli import (
    create_campaign,
    plan_input,
    seal_snapshot,
    submit_experiment,
    synthetic_snapshot,
)


def kickoff_input() -> dict:
    return {
        "objective": "Compare defensive allocation against the equally weighted baseline.",
        "constraints": ["Long-only ETF positions."],
        "validation_budget": 3,
        "validation_settings": plan_input(),
        "independence": {"independent": True, "reason": "No prior Holdout results used."},
        "reviewer": "research lead",
        "approval_reason": "Reviewed the baseline, thresholds, costs and budget.",
    }


def approve_round(project: Path, store: Path, campaign: dict, source: Path) -> dict:
    return ok(cli(
        "direction", "kickoff", "--project", str(project), "--store", str(store),
        "--campaign-id", campaign["campaign_id"], "--direction-key", "defensive allocation",
        "--name", "Defensive allocation", "--input", str(source),
        "--authorization-id", campaign["campaign_id"],
    ))


def test_kickoff_identity_survives_rename_and_campaign_change(tmp_path: Path) -> None:
    project = tmp_path / "project"
    ok(cli("project", "init", "--project", str(project)))
    snapshot = seal_snapshot(tmp_path, synthetic_snapshot())
    first = create_campaign(project, snapshot["store"], snapshot["snapshot_id"])
    second = create_campaign(project, snapshot["store"], snapshot["snapshot_id"])
    source = write_json(tmp_path / "kickoff.json", kickoff_input())

    def kickoff(campaign: dict, key: str, name: str) -> dict:
        return ok(cli(
            "direction", "kickoff", "--project", str(project), "--store", str(tmp_path / "trusted"),
            "--campaign-id", campaign["campaign_id"], "--direction-key", key, "--name", name,
            "--input", str(source), "--authorization-id", campaign["campaign_id"],
        ))

    approved = kickoff(first, " Defensive Allocation ", "Original display name")
    assert kickoff(first, "Defensive-allocation", "Original display name") == approved
    renamed = kickoff(second, "defensive_allocation", "Updated display name")
    assert renamed["direction_id"] == approved["direction_id"]
    assert renamed["round_id"] != approved["round_id"]

    reopened = tmp_path / "reopened-project"
    project.rename(reopened)
    status = ok(agent_cli(
        "direction", "status", "--project", str(reopened), "--round-id", approved["round_id"],
    ))
    assert status["kickoff"] == approved
    assert status["status"] == "open"
    assert status["budget"] == {"limit": 3, "used": 0, "remaining": 3}


def test_proposal_recovers_discovery_inputs_without_freezing_a_candidate(tmp_path: Path) -> None:
    project = tmp_path / "project"
    ok(cli("project", "init", "--project", str(project)))
    snapshot = seal_snapshot(tmp_path, synthetic_snapshot())
    campaign = create_campaign(project, snapshot["store"], snapshot["snapshot_id"])
    kickoff = approve_round(
        project, tmp_path / "trusted", campaign, write_json(tmp_path / "kickoff.json", kickoff_input()),
    )
    experiment = submit_experiment(
        project, campaign["campaign_id"], tmp_path / "discovery.json", "a",
        "Equal weights keep exposure balanced.", "equal_weight", {},
    )
    source = write_json(tmp_path / "proposal.json", {
        "change": "Start from a long-only equally weighted allocation.",
        "question": "Does net return track the baseline out of sample?",
        "expected_change": "Tracking difference stays within one percentage point.",
        "related_experiment_ids": [],
    })
    request = (
        "validation", "propose", "--project", str(project), "--round-id", kickoff["round_id"],
        "--submission-id", experiment["submission_id"], "--input", str(source),
    )
    proposal = ok(agent_cli(*request))
    assert proposal["experiment_id"] == experiment["experiment_id"]
    assert proposal["kickoff_id"] == kickoff["kickoff_id"]
    assert ok(agent_cli(*request)) == proposal
    status = ok(agent_cli(
        "direction", "status", "--project", str(project), "--round-id", kickoff["round_id"],
    ))
    assert status["proposals"] == [proposal]
    assert status["budget"] == {"limit": 3, "used": 0, "remaining": 3}
    assert status["experiments"][0]["experiment_id"] == experiment["experiment_id"]
    assert cli(
        "candidate", "status", "--project", str(project), "--candidate-id", experiment["experiment_id"],
    ).returncode != 0


def create_round(tmp_path: Path, policy: dict) -> tuple[Path, Path, dict, dict, dict]:
    project = tmp_path / "project"
    trusted = tmp_path / "trusted"
    ok(cli("project", "init", "--project", str(project)))
    snapshot = seal_snapshot(tmp_path, synthetic_snapshot())
    campaign = create_campaign(project, snapshot["store"], snapshot["snapshot_id"])
    kickoff = approve_round(project, trusted, campaign, write_json(tmp_path / "kickoff.json", policy))
    return project, trusted, snapshot, campaign, kickoff


def propose(project: Path, round_id: str, source: Path, experiment: dict) -> dict:
    return ok(agent_cli(
        "validation", "propose", "--project", str(project), "--round-id", round_id,
        "--submission-id", experiment["submission_id"], "--input", str(source),
    ))


def run_validation(
    project: Path, trusted: Path, snapshot: dict, proposal: dict,
) -> dict:
    return ok(cli(
        "validation", "run", "--project", str(project), "--store", str(trusted),
        "--snapshot-store", str(snapshot["store"]), "--round-id", proposal["round_id"],
        "--submission-id", proposal["submission_id"], "--proposal-id", proposal["proposal_id"],
    ))


@pytest.mark.isolation
def test_validation_details_and_feedback_are_recoverable_by_question(tmp_path: Path) -> None:
    project, trusted, snapshot, campaign, approved = create_round(tmp_path, kickoff_input())
    experiment = submit_experiment(
        project, campaign["campaign_id"], tmp_path / "discovery.json", "base",
        "Equal weights reproduce the baseline.", "equal_weight", {},
    )
    proposal = propose(project, approved["round_id"], write_json(tmp_path / "proposal.json", {
        "change": "Use equal weights across all eligible ETFs.",
        "question": "Does the change preserve baseline-like return?",
        "expected_change": "Match the equal-weight benchmark.",
        "related_experiment_ids": [],
    }), experiment)
    run_validation(project, trusted, snapshot, proposal)
    round_flags = ("--project", str(project), "--round-id", approved["round_id"])
    status = ok(agent_cli("direction", "status", *round_flags))
    evaluation = status["evaluations"][0]
    assert evaluation["outcome"] == "passed"
    assert "details" not in evaluation
    evaluation_flags = ("--evaluation-id", evaluation["evaluation_id"])
    question = "Which positions and trades explain baseline tracking on the first Validation date?"
    detail_flags = ("--date", "2024-01-13", "--question", question)
    requested = ok(agent_cli("validation", "details", *round_flags, *evaluation_flags, *detail_flags))
    assert requested["status"] == "queued"
    outside_flags = ("--date", "2024-01-17", "--question", "Check an adjacent session.")
    assert ok(agent_cli(
        "validation", "details", *round_flags, *evaluation_flags, *outside_flags,
    ))["status"] == "queued"
    ok(cli(
        "validation", "service", *round_flags, "--store", str(trusted),
        "--snapshot-store", str(snapshot["store"]),
    ))
    delivered = ok(agent_cli("validation", "details", *round_flags, *evaluation_flags, *detail_flags))
    assert delivered["status"] == "completed"
    assert delivered["detail_access_id"] == requested["detail_access_id"]
    assert set(delivered["details"]) == {"signals", "positions", "fills"}
    assert delivered["question"] == question
    outside = ok(agent_cli("validation", "details", *round_flags, *evaluation_flags, *outside_flags))
    assert outside["status"] == "unavailable"
    assert "details" not in outside

    revision = submit_experiment(
        project, campaign["campaign_id"], tmp_path / "revised.json", "revision",
        "Retain the benchmark schedule while testing concentration.", "momentum",
        {"lookback": 3, "top_k": 1, "cash_if_negative": False},
    )
    impact = "Matching weights and fills support retaining the original rebalance schedule."
    recorded = ok(agent_cli(
        "validation", "feedback", *round_flags, *evaluation_flags,
        "--detail-access-id", delivered["detail_access_id"], "--impact", impact,
        "--next-experiment-id", revision["experiment_id"],
    ))
    recovered = ok(agent_cli("direction", "status", *round_flags))
    assert recovered["feedback"] == [recorded]
    assert recorded["next_experiment_id"] == revision["experiment_id"]
    assert recovered["detail_requests"][0]["question"] == question


@pytest.mark.isolation
def test_round_freezes_with_remaining_budget_after_comparison_question(tmp_path: Path) -> None:
    project, trusted, snapshot, campaign, approved = create_round(tmp_path, kickoff_input())
    round_flags = ("--project", str(project), "--round-id", approved["round_id"])
    selected = submit_experiment(
        project, campaign["campaign_id"], tmp_path / "discovery.json", "a",
        "Equal weights track the baseline.", "equal_weight", {},
    )
    challenger = submit_experiment(
        project, campaign["campaign_id"], tmp_path / "discovery.json", "b",
        "Concentrated momentum might improve return.", "momentum",
        {"lookback": 3, "top_k": 1, "cash_if_negative": False},
    )
    source = {
        "change": "Check if the allocation can clear the fixed screens.",
        "question": "What is the return advantage after costs?",
        "expected_change": "Track or outperform the baseline.",
        "related_experiment_ids": [],
    }
    first = propose(project, approved["round_id"], write_json(tmp_path / "a.json", source), selected)
    draft = write_json(tmp_path / "b.json", source)
    queued = propose(project, approved["round_id"], draft, challenger)
    passed = run_validation(project, trusted, snapshot, first)
    assert passed["outcome"] == "passed"

    stale = cli(
        "validation", "run", *round_flags, "--store", str(trusted),
        "--snapshot-store", str(snapshot["store"]),
        "--submission-id", challenger["submission_id"], "--proposal-id", queued["proposal_id"],
    )
    rejected(stale, "VALIDATION_SELECTION_QUESTION_REQUIRED", phase="run")
    assert ok(agent_cli("direction", "status", *round_flags))["budget"]["used"] == 1
    comparative = {**source, "selection_impact": (
        "Switch to the momentum challenger if its net return improves while it meets drawdown limits."
    ), "related_experiment_ids": [selected["experiment_id"]]}
    propose(project, approved["round_id"], write_json(draft, comparative), challenger)

    choice = write_json(tmp_path / "selection.json", {
        "selection_reason": "The baseline passed and concentration risk still outweighs the upgrade.",
        "remaining_risks": ["Performance in a falling market remains uncertain."],
        "eliminations": {challenger["experiment_id"]: "Discovery showed unstable concentration."},
        "independence": {"independent": True, "reason": "No Holdout feedback was used to choose."},
    })
    freeze_flags = (
        "direction", "freeze", *round_flags, "--evaluation-id", passed["evaluation_id"],
        "--name", "Baseline candidate", "--input", str(choice),
    )
    frozen = ok(agent_cli(*freeze_flags))
    assert ok(agent_cli(*freeze_flags)) == frozen
    status = ok(agent_cli("direction", "status", *round_flags))
    assert status["status"] == "frozen"
    assert status["budget"]["remaining"] == 2
    selection = status["selection"]
    assert selection["candidate_id"] == frozen["candidate_id"]
    assert selection["validation_evaluation_id"] == passed["evaluation_id"]
    assert selection["validation_evaluation_hash"] == passed["record_hash"]
    assert selection["validation_plan_id"] == passed["plan_id"]
    assert selection["summary"]["versions_attempted"] == 2
    assert selection["summary"]["eliminations"] == {
        challenger["experiment_id"]: "Discovery showed unstable concentration."
    }


@pytest.mark.isolation
def test_exhaustion_without_passing_version_records_retrospective(tmp_path: Path) -> None:
    policy = kickoff_input()
    policy["validation_budget"] = 1
    policy["validation_settings"] = plan_input(thresholds=[
        {"id": "required_gain", "metric": "total_return", "op": ">=", "value": 10.0},
    ])
    project, trusted, snapshot, campaign, approved = create_round(tmp_path, policy)
    experiment = submit_experiment(
        project, campaign["campaign_id"], tmp_path / "discovery.json", "failed",
        "Measure equal-weight performance against a strict target.", "equal_weight", {},
    )
    proposal = propose(project, approved["round_id"], write_json(tmp_path / "proposal.json", {
        "change": "Start with an equal-weight design.",
        "question": "Does the allocation meet the strict return target?",
        "expected_change": "Net return meets the fixed target.",
        "related_experiment_ids": [],
    }), experiment)
    outcome = run_validation(project, trusted, snapshot, proposal)
    assert outcome["outcome"] == "rejected"
    round_flags = ("--project", str(project), "--round-id", approved["round_id"])
    assert ok(agent_cli("direction", "status", *round_flags))["status"] == "retrospective_required"
    source = write_json(tmp_path / "retrospective.json", {
        "findings": "The fixed target was never reached within the approved Validation budget.",
        "next_step": "Ask the research lead whether to reformulate the objective.",
    })
    command = ("direction", "retrospective", *round_flags, "--input", str(source))
    review = ok(agent_cli(*command))
    assert ok(agent_cli(*command)) == review
    final = ok(agent_cli("direction", "status", *round_flags))
    assert final["status"] == "retrospective_submitted"
    assert final["retrospective"] == review
    assert review["evaluations"][0]["evaluation_id"] == outcome["evaluation_id"]
    assert "selection" not in final
