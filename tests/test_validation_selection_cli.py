"""A frozen selection binds leadership approval to the exact evaluated version."""

from pathlib import Path

import pytest

from cli_helpers import agent_cli, ok, rejected, write_json
from test_issue37_holdout_cli import _authorize, _calendar
from test_protected_eval_cli import submit_experiment
from test_real_snapshot import schema_v2_fixture
from test_research_direction_cli import create_round, kickoff_input, propose, run_validation


@pytest.mark.isolation
@pytest.mark.parametrize("data_kind", ["synthetic", "real"])
def test_holdout_rejects_a_when_the_research_round_selected_b(tmp_path: Path, data_kind: str) -> None:
    payload = schema_v2_fixture(18) if data_kind == "real" else None
    policy = kickoff_input()
    if data_kind == "real":
        policy["validation_settings"]["allow_discovery_halt_warmup"] = True
    project, store, sealed, campaign, approved = create_round(tmp_path, policy, snapshot_input=payload)
    round_id = approved["round_id"]
    first = submit_experiment(
        project, campaign["campaign_id"], tmp_path / "a.json", "a",
        "Start with an equally weighted allocation.", "equal_weight", {},
    )
    second = submit_experiment(
        project, campaign["campaign_id"], tmp_path / "b.json", "b",
        "Try a more concentrated allocation.", "momentum",
        {"lookback": 3, "top_k": 1, "cash_if_negative": False},
    )
    a = propose(project, round_id, write_json(tmp_path / "proposal-a.json", {
        "change": "Evaluate the benchmark allocation.",
        "question": "Does the baseline clear the downside threshold?",
        "expected_change": "Comparable costs and returns.",
        "related_experiment_ids": [],
    }), first)
    checked_a = run_validation(project, store, sealed, a)
    replay = run_validation(project, store, sealed, a)
    assert replay["evaluation_id"] == checked_a["evaluation_id"]
    assert replay["budget"]["used"] == 1
    b = propose(project, round_id, write_json(tmp_path / "proposal-b.json", {
        "change": "Concentrate on the strongest recent symbol.",
        "question": "Can concentration improve return enough to justify the simpler allocation?",
        "expected_change": "Comparably low fees with fewer assets.",
        "selection_impact": "Select concentration if it passes and still tracks the benchmark.",
        "related_experiment_ids": [first["experiment_id"]],
    }), second)
    checked_b = run_validation(project, store, sealed, b)
    assert checked_a["outcome"] == checked_b["outcome"] == "passed"

    frozen = ok(agent_cli(
        "direction", "freeze", "--project", str(project), "--round-id", round_id,
        "--evaluation-id", checked_b["evaluation_id"], "--name", "Concentrated version",
        "--input", str(write_json(tmp_path / "selection.json", {
            "selection_reason": "Concentration cleared the agreed thresholds.",
            "remaining_risks": ["Concentration is sensitive to individual price gaps."],
            "eliminations": {first["experiment_id"]: "No single-symbol allocation."},
            "independence": {"independent": True, "reason": "Compared Discovery and Validation only."},
        })),
    ))
    assert frozen["research_selection"]["summary"]["versions_attempted"] == 2
    selection = {"project": project, "round_id": round_id, "evaluation_id": checked_b["evaluation_id"]}
    calendar = _calendar(tmp_path, data_kind)
    refused = rejected(_authorize(
        store, sealed, selection, calendar, evaluation_id=checked_a["evaluation_id"],
    ), "VALIDATION_SELECTION_MISMATCH")
    assert refused["query_debited"] is False
    assert refused["opened_protected_source"] is False
    granted = ok(_authorize(store, sealed, selection, calendar))["authorization"]
    assert granted["candidate_hash"] == frozen["candidate_hash"]
    assert granted["validation"]["evaluation_id"] == checked_b["evaluation_id"]
    assert granted["validation"]["record_hash"] == checked_b["record_hash"]
