"""Public receipts for research direction kickoff and Discovery handoff."""

from pathlib import Path

from cli_helpers import agent_cli, cli, ok, write_json
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
