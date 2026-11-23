"""Research-round Validation compares versions under a single budget authorization."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from cli_helpers import agent_cli, cli, ok, write_json
from test_protected_eval_cli import (
    create_campaign,
    plan_input,
    seal_snapshot,
    submit_experiment,
    synthetic_snapshot,
)


@pytest.mark.isolation
def test_validation_replays_one_version_and_spends_budget_on_another(tmp_path):
    sealed = seal_snapshot(tmp_path, synthetic_snapshot())
    project = tmp_path / "project"
    store = tmp_path / "evaluation-store"
    ok(cli("project", "init", "--project", str(project)))
    campaign_id = create_campaign(project, sealed["store"], sealed["snapshot_id"])["campaign_id"]
    kickoff = write_json(tmp_path / "kickoff.json", {
        "objective": "Compare equal weights with a concentrated momentum strategy.",
        "constraints": ["Use the same fees and equal-weight benchmark for each version."],
        "validation_budget": 2,
        "reviewer": "Research lead",
        "approval_reason": "Approve up to two versions under the frozen comparison protocol.",
        "validation_settings": plan_input(),
        "independence": {"independent": True, "reason": "No prior Holdout results informed the design."},
    })
    started = ok(cli(
        "direction", "kickoff", "--project", str(project), "--store", str(store),
        "--campaign-id", campaign_id, "--direction-key", "momentum-comparison",
        "--name", "Momentum comparison", "--authorization-id", "approved-01",
        "--input", str(kickoff),
    ))
    round_id = started["round_id"]
    submissions = (
        ("a", "equal_weight", {}),
        ("b", "momentum", {"lookback": 3, "top_k": 1, "cash_if_negative": False}),
    )
    proposals = {}
    for submission_id, family, parameters in submissions:
        submit_experiment(
            project, campaign_id, tmp_path / "experiment.json", submission_id,
            f"Test the {family} allocation on Discovery.", family, parameters,
        )
        proposal = write_json(tmp_path / "proposal.json", {
            "change": f"Evaluate the {family} allocation.",
            "question": "Does this allocation meet the preregistered performance thresholds?",
            "expected_change": "A concentrated strategy may differ from the benchmark.",
            "related_experiment_ids": [],
            **({"selection_impact": "Switch to momentum if it improves net return."} if submission_id == "b" else {}),
        })
        proposals[submission_id] = ok(agent_cli(
            "validation", "propose", "--project", str(project), "--round-id", round_id,
            "--submission-id", submission_id, "--input", str(proposal),
        ))["proposal_id"]

    def run_version(submission_id):
        return ok(cli(
            "validation", "run", "--store", str(store), "--snapshot-store", str(sealed["store"]),
            "--project", str(project), "--round-id", round_id, "--submission-id", submission_id,
            "--proposal-id", proposals[submission_id],
        ))

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, replay = executor.map(run_version, ["a", "a"])
    second = run_version("b")
    status = ok(cli("validation", "status", "--store", str(store), "--round-id", round_id))
    assert first["evaluation_id"] == replay["evaluation_id"]
    assert first["evaluation_id"] != second["evaluation_id"]
    assert status["budget"] == {"limit": 2, "used": 2, "remaining": 0}
    assert len(status["evaluations"]) == 2
    assert first["status"] == second["status"] == "completed"
    assert first["metrics"]["total_return"] == pytest.approx(-0.00049975, abs=1e-7)
    assert first["metrics"]["turnover"] == pytest.approx(0.99950025, abs=1e-7)
    assert set(first["metrics"]) >= {"return_delta", "max_drawdown", "fees_paid"}
    assert "record" not in first
    assert "fills" not in first
