"""Synthetic acceptance for preregistered scoring through the research CLI."""

from pathlib import Path

import pytest
from cli_helpers import agent_cli, cli, ok, write_json
from test_protected_eval_cli import plan_input, submit_experiment
from test_research_direction_cli import create_round, kickoff_input, propose, run_validation

pytestmark = pytest.mark.isolation


def compare(
    tmp_path: Path, *, require_candidate_position: bool, family: str, parameters: dict,
    threshold: float, snapshot_input: dict | None = None,
) -> tuple[dict, tuple[str, ...], Path, dict]:
    policy = kickoff_input()
    prerequisites = {
        "candidate": {"require_initial_position": require_candidate_position},
        "baseline": {"require_initial_position": True},
    }
    policy["validation_settings"] = plan_input(
        scoring_prerequisites=prerequisites,
        thresholds=[{"id": "return", "metric": "total_return", "op": ">=", "value": threshold}],
    )
    project, trusted, snapshot, campaign, kickoff = create_round(
        tmp_path, policy, snapshot_input=snapshot_input,
    )
    assert kickoff["validation_settings"]["scoring_prerequisites"] == prerequisites
    experiment = submit_experiment(
        project, campaign["campaign_id"], tmp_path / "discovery.json", "candidate",
        "Compare position evidence with the common benchmark.", family, parameters,
    )
    proposal = propose(project, kickoff["round_id"], write_json(tmp_path / "proposal.json", {
        "change": "Run the selected allocation under the frozen protocol.",
        "question": "Is there sufficient trading evidence to grade return?",
        "expected_change": "Return is comparable when both scoring prerequisites are satisfied.",
        "related_experiment_ids": [],
    }), experiment)
    receipt = run_validation(project, trusted, snapshot, proposal)
    round_flags = ("--project", str(project), "--round-id", kickoff["round_id"])
    return receipt, round_flags, trusted, kickoff


@pytest.mark.parametrize("threshold", [-0.01, 0.01])
def test_required_position_overrides_financial_threshold(tmp_path: Path, threshold: float) -> None:
    receipt, round_flags, trusted, kickoff = compare(
        tmp_path, require_candidate_position=True, family="momentum",
        parameters={"lookback": 3, "top_k": 1, "cash_if_negative": True}, threshold=threshold,
    )
    assert receipt["status"] == "completed"
    assert receipt["outcome"] == "insufficient_evidence"
    assert receipt["reason"] == "SCORING_PREREQUISITES_NOT_MET"
    assert receipt["metrics"]["total_return"] == 0
    assert receipt["metrics"]["valued_sessions"] > 0
    assert receipt["scoring_prerequisites"] == kickoff["validation_settings"]["scoring_prerequisites"]
    assert receipt["execution_rules"] == kickoff["evaluation"]["execution_rules"]
    assert receipt["scoring_evidence"]["candidate"] == {
        "satisfied": False, "position_state": "intentional_cash", "reason": "INITIAL_POSITION_REQUIRED",
    }
    assert receipt["scoring_evidence"]["baseline"] == {
        "satisfied": True, "position_state": "initial_position_established", "reason": None,
    }
    evaluation_id = receipt["evaluation_id"]
    assert ok(cli(
        "validation", "status", "--store", str(trusted), "--round-id", kickoff["round_id"],
        "--evaluation-id", evaluation_id,
    )) == receipt
    status = ok(agent_cli("direction", "status", *round_flags))
    [published] = status["evaluations"]
    for field in ("evaluation_id", "execution_rules", "scoring_prerequisites", "scoring_evidence"):
        assert published[field] == receipt[field]
    assert status["budget"] == receipt["budget"]
    assert "details" not in published


def test_explicit_cash_can_pass_with_a_valid_benchmark(tmp_path: Path) -> None:
    receipt, _, _, _ = compare(
        tmp_path, require_candidate_position=False, family="momentum",
        parameters={"lookback": 3, "top_k": 1, "cash_if_negative": True}, threshold=0.0,
    )
    assert receipt["outcome"] == "passed"
    assert receipt["metrics"]["total_return"] == 0.0
    assert receipt["scoring_evidence"]["candidate"] == {
        "position_state": "intentional_cash", "satisfied": True, "reason": None,
    }
    assert receipt["scoring_evidence"]["baseline"]["satisfied"] is True


def test_established_positions_can_fail_the_financial_threshold(tmp_path: Path) -> None:
    receipt, _, _, _ = compare(
        tmp_path, require_candidate_position=True, family="equal_weight", parameters={},
        threshold=0.0,
    )
    assert receipt["outcome"] == "rejected"
    assert receipt["reason"] == "THRESHOLD_NOT_MET"
    assert receipt["metrics"]["total_return"] < 0.0
    assert all(evidence["satisfied"] for evidence in receipt["scoring_evidence"].values())
