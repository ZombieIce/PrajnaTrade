"""Replay #35 from one synthetic snapshot to two reconciled Shadow Live accounts."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from cli_helpers import ok, rejected, write_json
from test_compare_cli import expected_metrics
from test_evidence_loop_cli import evidence_status, shadow_compare
from test_formal_validation_cli import (
    add_version,
    formal_scenario,
    trusted_receipt,
    validate_version_id,
)
from test_protected_eval_cli import SYMBOLS
from test_research_loop_cli import round_status, trending_snapshot
from test_shadow_cli import report_days, shadow_feed, shadow_update
from test_validation_admission_cli import admission_input, admit
from test_version_review_cli import (
    SHANGHAI,
    approve_version,
    artifact,
    calendar_payload,
    local_today,
    observation_input,
    release_status,
)

CASH = 100_000
FEE = 0.0005
OPEN = 10
FOCUS, MISSING = SYMBOLS[:2]
DISCOVERY = ("2024-01-01", "2024-01-07", 7)
TRAILING = ("2024-01-08", "2024-01-10", 3)
EXECUTION_COSTS = {"initial_cash": CASH, "transaction_cost_bps": 5, "slippage_bps": 0}


def acceptance_snapshot() -> dict:
    # 17 total sessions leave 10 in sealed Discovery and split exactly 7/3.
    payload = trending_snapshot(17)
    payload["corporate_actions"] = []
    for row in payload["raw_bars"]:
        close = OPEN
        if row["symbol"] == FOCUS:
            close = {DISCOVERY[1]: 10.5, TRAILING[1]: 11}.get(row["date"], OPEN)
        row.update(open=OPEN, high=close, low=OPEN, close=close)
    for row in payload["derived"]:
        if row["symbol"] == FOCUS:
            row["value"] = OPEN + int(row["date"][-2:]) * 0.1
    return payload


def assert_scored_run(metrics: dict, sessions: int, fills: int, focus_gain: float) -> None:
    # All cash buys at 10 with a 5 bps fee. Only FOCUS gains at the final close.
    invested = CASH / (1 + FEE)
    nav = invested + invested / fills / OPEN * focus_gain
    assert metrics["sessions"] == metrics["valued_sessions"] == sessions
    assert metrics["fills"] == fills
    assert metrics["ending_equity"] == pytest.approx(nav, abs=1e-8, rel=0)
    assert metrics["total_return"] == pytest.approx(nav / CASH - 1, abs=1e-12)
    assert metrics["fees_paid"] == pytest.approx(invested * FEE)
    assert metrics["slippage_paid"] == 0


def assert_research_results(
    scenario: dict, version: dict, experiment: dict, receipt: dict, candidate_fills: int,
) -> None:
    locked = version["locked"]
    split = scenario["split"]
    assert locked["base_costs"] == EXECUTION_COSTS
    assert locked["research_partition"] == split
    assert locked["protocol"]["name"] == "trailing-70-30"
    assert receipt["protocol"] == {
        **locked["protocol"],
        "boundary": split["boundary"],
        "research_interval": {key: split["partition"][key] for key in ("start", "end", "sessions")},
        "split_hash": split["split_hash"],
    }
    assert receipt["validation_kind"] == "trailing_validation"
    assert receipt["outcome"] == "passed"
    assert receipt["opened_protected_source"] is receipt["query_debited"] is False
    provenance = receipt["provenance"]
    assert provenance["snapshot_id"] == locked["snapshot_id"] == scenario["campaign"]["snapshot_id"]
    assert provenance["round_id"] == version["round_id"] == scenario["opened"]["round_id"]
    assert provenance["version_id"] == version["version_id"]
    assert provenance["experiment_id"] == version["experiment_id"] == experiment["experiment_id"]
    assert provenance["execution_rule_version"] == "next-open-v1"
    assert provenance["price_basis"] == "unadjusted_open"
    for key, expected in (("development", DISCOVERY), ("trailing", TRAILING)):
        segment = receipt[key]
        assert segment == split[key]
        assert (segment["start"], segment["end"], segment["sessions"]) == expected
        assert provenance[f"{key}_content_hash"] == segment["content_hash"]
    assert receipt["scoring"] == {
        "start": TRAILING[0], "end": TRAILING[1], "sessions": 3, "valued_sessions": 3,
    }
    assert receipt["warmup"] == {
        "source": "development", "start": DISCOVERY[0], "end": DISCOVERY[1],
        "sessions": 7, "first_ready_date": TRAILING[0], "prior_history_hash": None,
    }
    discovery = experiment["discovery"]
    stored = trusted_receipt(scenario, receipt["validation_id"])
    assert stored["receipt_hash"] == receipt["receipt_hash"]
    for side, run, fills in (
        ("candidate", discovery, candidate_fills),
        ("benchmark", discovery["baseline_comparison"]["baseline"], 5),
    ):
        assert (run["start_date"], run["end_date"]) == ("2024-01-05", DISCOVERY[1])
        assert_scored_run(run["metrics"], 3, fills, 0.5)
        assert_scored_run(receipt["metrics"][side], 3, fills, 1)
        assert [row["date"] for row in stored["curves"][side]] == [
            TRAILING[0], "2024-01-09", TRAILING[1],
        ]


def assert_settlement(signal: dict, settlement: dict, expected: dict[str, str]) -> None:
    scheduled = {order["order_id"]: order for order in signal["result"]["next_orders"]}
    terminal = {order["order_id"]: order for order in settlement["result"]["orders"]}
    assert len(scheduled) == len(terminal) == len(expected)
    assert scheduled.keys() == terminal.keys()
    assert {order["symbol"]: order["status"] for order in terminal.values()} == expected
    filled = {order_id for order_id, order in terminal.items() if order["status"] == "filled"}
    assert {fill["order_id"] for fill in settlement["result"]["fills"]} == filled
    for order_id, order in scheduled.items():
        actual = terminal[order_id]
        assert order["signal_date"] == actual["signal_date"] == signal["date"]
        assert order["execution_date"] == actual["execution_date"] == settlement["date"]
        assert order["quantity"] == actual["quantity"]
        assert order["persisted_before_deadline"] is actual["persisted_before_deadline"] is True
        assert actual["persisted_at"] == signal["committed_at"]
        assert (
            datetime.fromisoformat(order["submitted_at"])
            < datetime.fromisoformat(actual["persisted_at"])
            < datetime.fromisoformat(order["deadline"])
        )


def assert_book(day: dict, positions: dict[str, float], cash: float, nav: float) -> None:
    result = day["result"]
    assert {symbol: row["quantity"] for symbol, row in result["positions"].items()} == pytest.approx(
        {symbol: positions.get(symbol, 0) for symbol in SYMBOLS}, abs=1e-8, rel=0,
    )
    assert result["cash"] == pytest.approx(cash, abs=1e-8, rel=0)
    assert result["nav"] == pytest.approx(nav, abs=1e-8, rel=0)


def test_synthetic_dual_loop_acceptance(tmp_path: Path) -> None:
    scenario = formal_scenario(
        tmp_path, payload=acceptance_snapshot(), warmup=4, limit=1, family="equal_weight",
    )
    project, store = scenario["project"], scenario["store"]
    round_id = scenario["opened"]["round_id"]
    v1_id = scenario["version"]["version_id"]
    second = add_version(
        scenario, parameters={"lookback": 3, "top_k": 1, "cash_if_negative": False},
    )
    v2_id = second["version"]["version_id"]
    assert v1_id != v2_id
    write_json(tmp_path / "round.json", scenario["opened"])
    write_json(tmp_path / "campaign.json", scenario["campaign"])

    # One automatic read, then one separately authorized read for V2.
    receipts = {}
    for number, entry, fills, kind in (
        (1, scenario, 5, "automatic"), (2, second, 1, "manual"),
    ):
        version = entry["version"]
        version_id = version["version_id"]
        admission_id = None
        if number == 2:
            blocked = rejected(
                validate_version_id(scenario, version_id), "VALIDATION_ALLOWANCE_EXHAUSTED",
            )
            assert blocked["allowance"]["total_used"] == 1
            write_json(tmp_path / "validation-v2-blocked.json", blocked)
            grant = ok(admit(
                project, store, round_id, version_id,
                write_json(tmp_path / "admission-input.json", admission_input()),
                reviewer="synthetic-operator",
            ))
            admission_id = grant["admission"]["admission_id"]
            write_json(tmp_path / "admission.json", grant)
        receipt = ok(validate_version_id(scenario, version_id))
        assert receipt["reused"] is False
        assert receipt["usage"]["kind"] == kind
        assert receipt["usage"]["admission_id"] == admission_id
        assert receipt["usage"]["sequence"] == receipt["allowance"]["total_used"] == number
        replay = ok(validate_version_id(scenario, version_id))
        assert replay["reused"] is True
        for key in ("validation_id", "receipt_hash", "usage", "allowance"):
            assert replay[key] == receipt[key]
        assert_research_results(scenario, version, entry["experiment"], receipt, fills)
        receipts[version_id] = receipt
        write_json(tmp_path / f"experiment-v{number}.json", entry["experiment"])
        write_json(tmp_path / f"locked-v{number}.json", version)
        write_json(tmp_path / f"validation-v{number}.json", receipt)
        write_json(tmp_path / f"validation-v{number}-replay.json", replay)

    status = ok(round_status(project, round_id))
    allowance = status["validation_allowance"]
    assert (allowance["automatic_used"], allowance["manual_used"], allowance["total_used"]) == (1, 1, 2)
    assert [(row["version_id"], row["validation_id"]) for row in allowance["uses"]] == [
        (version_id, receipt["validation_id"]) for version_id, receipt in receipts.items()
    ]
    (admission,) = status["admissions"]
    assert admission["used"] is True
    assert admission["admission_id"] == receipts[v2_id]["usage"]["admission_id"]
    assert admission["consumed_validation_id"] == receipts[v2_id]["validation_id"]
    write_json(tmp_path / "round-status.json", status)
    assert not (store / "queries.sqlite").exists()

    # Each approval selects the next session in its approved calendar window.
    today = local_today()
    sessions = [(today + timedelta(days=offset)).isoformat() for offset in (-1, 2, 3, 4, 5, 6)]
    prior, d1, d2, d3, d4, _ = sessions
    observation = write_json(tmp_path / "observation.json", observation_input())
    release_ids = []
    decision_ids = []
    for number, version_id in enumerate((v1_id, v2_id), start=1):
        calendar = write_json(
            tmp_path / f"calendar-v{number}.json",
            {**calendar_payload(), "sessions": sessions[number:]},
        )
        approval = ok(approve_version(project, store, round_id, version_id, calendar, observation))
        assert approval["status"] == "approved"
        assert approval["version_id"] == version_id and approval["round_id"] == round_id
        enabled = sessions[number]
        decided = datetime.fromisoformat(approval["decided_at"]).astimezone(SHANGHAI)
        assert prior < decided.date().isoformat() < enabled
        assert approval["tracking"]["enabled_signal_date"] == enabled
        release_id = approval["tracking"]["release_id"]
        release_ids.append(release_id)
        decision_ids.append(approval["decision_id"])
        tracking = artifact(store, release_id)
        assert tracking["scope"] == "synthetic" and tracking["simulated_tracking"] is True
        assert tracking["decision"]["decision_id"] == approval["decision_id"]
        assert tracking["decision"]["decided_at"] == approval["decided_at"]
        binding = tracking["binding"]
        for key in ("version_id", "experiment_id", "snapshot_id", "round_id"):
            assert binding[key] == receipts[version_id]["provenance"][key]
        for key in ("validation_id", "receipt_hash"):
            assert binding[key] == approval["validation"][key] == receipts[version_id][key]
        state = release_status(store, release_id)
        (event,) = state["timeline"]
        assert state["state"] == "enabled" and event["action"] == "enable"
        assert event["effective_signal_date"] == enabled
        assert datetime.fromisoformat(event["recorded_at"]) >= decided
        write_json(tmp_path / f"approval-v{number}.json", approval)
        write_json(tmp_path / f"release-v{number}.json", tracking)
        write_json(tmp_path / f"lifecycle-v{number}.json", state)
    assert len(set(release_ids)) == len(set(decision_ids)) == 2

    # Provide observations through each target close. Only the calendar reaches
    # ahead, so today's order knows the next session but cannot see its prices.
    feed = shadow_feed(
        sessions,
        closes={FOCUS: [10, 10, 10, 10, 11, 11]},
        derived={FOCUS: [10, 11, 12, 13, 14, 15]},
        raw_overrides={(d2, MISSING): {"open": None}},
    )
    ledger = tmp_path / "ledger"
    release_pair = tuple(release_ids)
    updates = {}
    for session in sessions[:-1]:
        daily = write_json(tmp_path / f"feed-{session}.json", {
            **feed,
            **{
                key: [row for row in feed[key] if row["date"] <= session]
                for key in ("raw_bars", "derived")
            },
        })
        update = ok(shadow_update(ledger, store, daily, session, release_ids=release_pair, at_close=True))
        updates[session] = {row["release_id"]: row for row in update["versions"]}
        write_json(tmp_path / f"update-{session}.json", update)

    reports = []
    for number, release_id in enumerate(release_ids, start=1):
        for session in sessions[:number]:
            assert updates[session][release_id]["status"] == "not_started"
        days = report_days(ledger, release_id, tmp_path / f"ledger-v{number}.json")
        assert [day["date"] for day in days] == sessions[number:-1]
        for day in days:
            update = updates[day["date"]][release_id]
            assert update["committed_date"] == day["date"]
            assert update["result_hash"] == day["result_hash"]
        reports.append({day["date"]: day for day in days})

    v1, v2 = reports
    assert_settlement(v1[d1], v1[d2], {
        symbol: "missing_open" if symbol == MISSING else "filled" for symbol in SYMBOLS
    })
    assert_settlement(v2[d3], v2[d4], {FOCUS: "filled"})
    assert v2[d2]["result"]["signal"] is None
    assert v2[d2]["result"]["next_orders"] == v2[d2]["result"]["opportunities"] == []
    quantity = CASH / (OPEN * (1 + FEE))
    four_positions = {symbol: quantity / 5 for symbol in SYMBOLS if symbol != MISSING}
    partial_nav = CASH / 5 + quantity * OPEN * 4 / 5
    assert_book(v1[d1], {}, CASH, CASH)
    assert_book(v1[d2], four_positions, CASH / 5, partial_nav)
    assert_book(v1[d3], four_positions, CASH / 5, partial_nav)
    assert_book(v1[d4], four_positions, CASH / 5, partial_nav + quantity / 5)
    assert_book(v2[d2], {}, CASH, CASH)
    assert_book(v2[d3], {}, CASH, CASH)
    assert_book(v2[d4], {FOCUS: quantity}, 0, quantity * 11)

    evidence = ok(evidence_status(project, store, ledger, round_id))
    write_json(tmp_path / "evidence-status.json", evidence)
    by_version = {row["version_id"]: row["tracking"] for row in evidence["versions"]}
    for version_id, report, signal, execution, filled, unfilled in (
        (v1_id, v1, d1, d2, 0, 1), (v2_id, v2, d3, d4, 1, 0),
    ):
        facts = by_version[version_id]
        assert (facts["due_opportunities"], facts["filled_opportunities"], facts["unfilled_opportunities"]) == (
            1, filled, unfilled,
        )
        (opportunity,) = facts["opportunities"]
        assert (opportunity["signal_date"], opportunity["execution_date"]) == (signal, execution)
        orders = report[execution]["result"]["orders"]
        assert set(opportunity["filled"]) == {
            order["order_id"] for order in orders if order["status"] == "filled"
        }
        assert set(opportunity["unfilled"]) == {
            order["order_id"] for order in orders if order["status"] == "missing_open"
        }
        assert opportunity["unresolved"] == []
    assert by_version[v1_id]["metrics_not_ready"]["count"] == 0
    assert by_version[v2_id]["metrics_not_ready"] == {
        "count": 1, "sessions": [{"date": d2, "required_sessions": 4, "available_sessions": 3}],
    }

    comparison = ok(shadow_compare(ledger, daily, release_pair, tmp_path / "comparison.json"))
    common = [d2, d3, d4]
    assert comparison["scope"] == "synthetic" and comparison["comparable"] is True
    assert comparison["common_interval"] == {"start": d2, "end": d4, "sessions": 3}
    for release_id, report in zip(release_ids, reports, strict=True):
        entry = comparison["per_version"][release_id]
        days = {session: row["result"] for session, row in report.items()}
        assert entry["committed_coverage"]["dates"] == list(days)
        assert entry["date_range"] == {"start": d2, "end": d4}
        common_metrics = expected_metrics(days, common)
        assert {key: entry[key] for key in common_metrics} == pytest.approx(common_metrics)
        full = entry["full_history"]
        assert full["sessions"] == len(days)
        assert (full["first_committed_date"], full["last_committed_date"]) == (next(iter(days)), d4)
        full_metrics = expected_metrics(days, list(days))
        assert full["total_return"] == pytest.approx(full_metrics.pop("cumulative_return"))
        full_metrics.pop("dividends_paid")
        assert {key: full[key] for key in full_metrics} == pytest.approx(full_metrics)
    equal_weight, momentum = (comparison["per_version"][release_id] for release_id in release_ids)
    assert equal_weight["due_opportunities"] == 0
    assert equal_weight["full_history"]["due_opportunities"] == 1
    assert equal_weight["unfilled_order_count"] == equal_weight["full_history"]["unfilled_order_count"] == 1
    assert momentum["due_opportunities"] == momentum["full_history"]["due_opportunities"] == 1
