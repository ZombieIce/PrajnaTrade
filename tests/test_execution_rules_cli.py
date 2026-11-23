"""Exercise frozen execution rules through the public research flow."""

import pytest
from cli_helpers import agent_cli, cli, ok, write_json
from test_protected_eval_cli import create_campaign, seal_snapshot, submit_experiment, synthetic_snapshot
from test_research_direction_cli import approve_round, kickoff_input, propose, run_validation


def _halted_open_snapshot():
    payload = synthetic_snapshot(25)
    payload["corporate_actions"] = []
    for bar in payload["raw_bars"]:
        bar.update(open=10, high=10, low=10, close=10)
        if bar["date"] in {"2024-01-02", "2024-01-17"} and bar["symbol"] == "511260":
            bar.update(trading_status="suspended", volume=0)
        if bar["date"] in {"2024-01-03", "2024-01-18"}:
            bar.update(open=12, high=12)
    for row in payload["derived"]:
        if row["date"] in {"2024-01-02", "2024-01-17"}:
            # The blocked open is known, but the same day's signal is unavailable
            # at the following open. A fill must carry the original target.
            row["available_at"] = "2024-02-01T00:00:00+08:00"
    return payload


@pytest.mark.parametrize(("version", "first_fill_date"), [
    ("next-open-v1", None),
    ("retry-initial-v1", "2024-01-03"),
])
def test_campaign_freezes_platform_retry_for_discovery(tmp_path, version, first_fill_date):
    sealed = seal_snapshot(tmp_path, _halted_open_snapshot())
    project = tmp_path / "project"
    ok(cli("project", "init", "--project", str(project)))
    campaign = create_campaign(
        project, sealed["store"], sealed["snapshot_id"], "--execution-rules", version,
    )
    assert campaign["execution_rules"]["version"] == version
    receipt = submit_experiment(
        project, campaign["campaign_id"], tmp_path / "experiment.json", "equal-weight",
        "Compare matching through a blocked initial open.", "equal_weight", {},
    )
    baseline = receipt["discovery"]["baseline_comparison"]["baseline"]
    assert baseline["order_attempts"][0] == {
        "signal_date": "2024-01-01", "date": "2024-01-02", "attempts": 1,
        "status": "not_filled", "reason": "missing_or_unconfirmed_open",
        "missing_symbols": ["511260"],
    }
    assert baseline["position_evidence"]["first_fill_date"] == first_fill_date
    if first_fill_date is None:
        assert baseline["fills"] == []
        assert baseline["equity_curve"][-1]["cash"] == 100_000
    else:
        assert [fill["date"] for fill in baseline["fills"]] == [first_fill_date] * 5
        assert [fill["raw_open"] for fill in baseline["fills"]] == [12] * 5
        assert baseline["order_attempts"][1]["signal_date"] == "2024-01-01"
        assert baseline["equity_curve"][-1]["cash"] == pytest.approx(0)


@pytest.mark.isolation
def test_discovery_and_validation_match_the_same_retried_target(tmp_path):
    sealed = seal_snapshot(tmp_path, _halted_open_snapshot())
    project = tmp_path / "project"
    store = tmp_path / "operator"
    ok(cli("project", "init", "--project", str(project)))
    campaign = create_campaign(
        project, sealed["store"], sealed["snapshot_id"],
        "--execution-rules", "retry-initial-v1", "--slippage-bps", "10",
    )
    kickoff = approve_round(
        project, store, campaign, write_json(tmp_path / "kickoff.json", kickoff_input()),
    )
    discovery = submit_experiment(
        project, campaign["campaign_id"], tmp_path / "experiment.json", "equal-weight",
        "Compare matched targets at the opening price.", "equal_weight", {},
    )
    proposal = propose(project, kickoff["round_id"], write_json(tmp_path / "proposal.json", {
        "change": "Use equal ETF weights.",
        "question": "Do execution and costs match in the aligned synthetic sessions?",
        "expected_change": "Matching quantities and costs across partitions.",
        "related_experiment_ids": [],
    }), discovery)
    validation = run_validation(project, store, sealed, proposal)
    plan = ok(cli(
        "evaluation", "status", "--store", str(store), "--plan-id", validation["plan_id"],
    ))["plan"]
    assert plan["execution_rules"] == campaign["execution_rules"]

    detail_flags = (
        "--project", str(project), "--round-id", kickoff["round_id"],
        "--evaluation-id", validation["evaluation_id"],
    )
    date_flags = (
        "--date", "2024-01-18", "--question", "What filled at the first unblocked open?",
    )
    assert ok(agent_cli("validation", "details", *detail_flags, *date_flags))["status"] == "queued"
    ok(cli(
        "validation", "service", "--store", str(store), "--snapshot-store", str(sealed["store"]),
        "--project", str(project), "--round-id", kickoff["round_id"],
    ))
    details = ok(agent_cli("validation", "details", *detail_flags, *date_flags))["details"]
    discovery_run = discovery["discovery"]
    assert discovery_run["order_attempts"][1]["signal_date"] == "2024-01-01"
    assert len(discovery_run["fills"]) == len(details["fills"]) == 5
    assert all(fill["date"] == "2024-01-18" and fill["raw_open"] == 12 for fill in details["fills"])
    assert [{**fill, "date": "2024-01-03"} for fill in details["fills"]] == discovery_run["fills"]
    assert details["cash"] == discovery_run["equity_curve"][2]["cash"]
    assert details["baseline"]["cash"] == details["cash"]
    assert details["position_evidence"] == {"first_fill_date": "2024-01-18"}
