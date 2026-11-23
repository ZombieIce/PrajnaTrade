"""Research CLI coverage for benchmark failures within a Validation batch."""

from pathlib import Path

import pytest
from cli_helpers import cli, ok, write_json
from test_protected_eval_cli import submit_experiment, synthetic_snapshot
from test_research_direction_cli import create_round, kickoff_input, propose, run_validation

pytestmark = pytest.mark.isolation


def _policy() -> dict:
    policy = kickoff_input()
    policy["validation_settings"]["scoring_prerequisites"] = {
        "candidate": {"require_initial_position": True},
        "baseline": {"require_initial_position": True},
    }
    return policy


def _submit(
    root: Path, project: Path, campaign: dict, round_id: str,
    submission: str, family: str, parameters: dict,
) -> dict:
    experiment = submit_experiment(
        project, campaign["campaign_id"], root / f"experiment-{submission}.json", submission,
        f"Investigate the {family} allocation.", family, parameters,
    )
    source = write_json(root / f"proposal-{submission}.json", {
        "change": f"Run the {family} allocation.",
        "question": "Can the allocation be compared to the common benchmark?",
        "expected_change": "The allocation may change exposure.",
        "selection_impact": "Use the allocation if it can be compared and meets the thresholds.",
        "related_experiment_ids": [],
    })
    return propose(project, round_id, source, experiment)


def _service(project: Path, store: Path, snapshots: Path, round_id: str) -> dict:
    return ok(cli(
        "validation", "service", "--project", str(project), "--store", str(store),
        "--snapshot-store", str(snapshots), "--round-id", round_id,
    ))


def test_failed_common_benchmark_pauses_batch_before_next_debit(tmp_path: Path) -> None:
    snapshot = synthetic_snapshot()
    first_validation_open = snapshot["calendar"][13]["date"]
    for row in snapshot["raw_bars"]:
        if row["date"] == first_validation_open:
            row.update(trading_status="suspended", open=None, high=None, low=None, close=None, volume=None)
    project, store, sealed, campaign, approved = create_round(tmp_path, kickoff_input(), snapshot_input=snapshot)
    round_id = approved["round_id"]
    first = _submit(tmp_path, project, campaign, round_id, "first", "equal_weight", {})
    second = _submit(tmp_path, project, campaign, round_id, "second", "momentum", {
        "lookback": 3, "top_k": 1, "cash_if_negative": False,
    })

    batch = _service(project, store, sealed["store"], round_id)
    evaluated, paused = batch["results"]
    assert batch["status"] == "paused"
    assert evaluated["proposal_id"] == first["proposal_id"]
    assert evaluated["status"] == "completed"
    assert evaluated["outcome"] == "insufficient_evidence"
    assert evaluated["scoring_prerequisites"]["candidate"]["require_initial_position"] is False
    assert evaluated["scoring_evidence"]["candidate"] == {
        "position_state": "initial_position_not_established", "satisfied": False,
        "reason": "INITIAL_POSITION_NOT_ESTABLISHED",
    }
    assert evaluated["scoring_evidence"]["baseline"]["satisfied"] is False
    assert paused["proposal_id"] == second["proposal_id"]
    assert paused["status"] == "paused"
    assert paused["reason"] == "COMMON_BENCHMARK_FAILED"
    assert paused["benchmark_failure"]["evaluation_id"] == evaluated["evaluation_id"]
    assert paused["benchmark_failure"]["baseline"]["method"] == "equal_weight_buy_and_hold"
    assert paused["query_debited"] is False
    assert paused["opened_protected_source"] is False
    assert "evaluation_id" not in paused

    status = ok(cli("validation", "status", "--store", str(store), "--round-id", round_id))
    assert status["budget"] == paused["budget"] == {"limit": 3, "used": 1, "remaining": 2}
    assert status["evaluations"] == [evaluated]
    assert status["benchmark_failures"] == [paused["benchmark_failure"]]
    payload = sealed["store"] / sealed["snapshot_id"] / "validation.json"
    payload.rename(payload.with_suffix(".offline"))
    assert _service(project, store, sealed["store"], round_id)["results"] == [paused]
    assert run_validation(project, store, {"store": tmp_path / "unavailable"}, first) == evaluated


def test_candidate_only_failure_does_not_pause_the_batch(tmp_path: Path) -> None:
    project, store, sealed, campaign, approved = create_round(tmp_path, _policy())
    round_id = approved["round_id"]
    first = _submit(tmp_path, project, campaign, round_id, "first", "momentum", {
        "lookback": 3, "top_k": 1, "cash_if_negative": True,
    })
    second = _submit(tmp_path, project, campaign, round_id, "second", "equal_weight", {})
    batch = _service(project, store, sealed["store"], round_id)
    assert batch["status"] == "completed"
    failed, passed = batch["results"]
    assert failed["proposal_id"] == first["proposal_id"]
    assert failed["outcome"] == "insufficient_evidence"
    assert failed["scoring_evidence"]["candidate"]["satisfied"] is False
    assert failed["scoring_evidence"]["baseline"]["satisfied"] is True
    assert passed["proposal_id"] == second["proposal_id"]
    assert passed["outcome"] == "passed"
    status = ok(cli("validation", "status", "--store", str(store), "--round-id", round_id))
    assert status["status"] == "available"
    assert status["budget"] == {"limit": 3, "used": 2, "remaining": 1}
