"""Public-CLI acceptance tests for full-day suspended holding valuation (#69 / #68-A).

All fixtures are synthetic JSON (including schema-3 ``data_kind: "real"`` inputs
used strictly to exercise real-schema provenance and evidence validation).
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from cli_helpers import cli, ok, rejected, write_json
from test_actual_open_cli import (
    OPEN_SOURCE,
    PRICE_EVIDENCE,
    STATUS_EVIDENCE,
    day_at,
    derived_row,
    make_actual_open_halt,
    make_confirmed_suspension,
    make_unknown_status,
    make_whole_day_halt,
    raw_row,
    schema_v3_fixture,
    seal_v3,
)
from test_full_history_cli import full_history, seal_full_history
from test_protected_eval_cli import plan_input

from prajnatrade.data import ETF_SYMBOLS

SYMBOLS = ETF_SYMBOLS
TARGET = SYMBOLS[0]  # 511260


def _set_constant_prices(
    payload: dict,
    *,
    raw_price: float = 10.0,
    derived_base: float = 10.0,
) -> None:
    """Set uniform raw OHLC prices and deterministic derived series for momentum."""
    days = [row["date"] for row in payload["calendar"]]
    for index, day in enumerate(days):
        for sym_idx, symbol in enumerate(SYMBOLS):
            row = raw_row(payload, day, symbol)
            row["open"] = raw_price
            row["high"] = raw_price
            row["low"] = raw_price
            row["close"] = raw_price
            drow = derived_row(payload, day, symbol)
            # Make SYMBOLS[0] strictly lead momentum(lookback=2, top_k=1)
            slope = 0.05 if sym_idx == 0 else 0.001 * (len(SYMBOLS) - sym_idx)
            drow["value"] = round(derived_base * ((1.0 + slope) ** index), 6)


def _init_campaign(
    root: Path,
    store: Path,
    snapshot_id: str,
    *,
    initial_cash: float = 1000.0,
    transaction_cost_bps: float = 0.0,
    slippage_bps: float = 0.0,
    execution_rules: str = "actual-open-v1",
    valuation_policy: str | None = "carry-forward-suspended-v1",
    discovery_split: str | None = None,
    discovery_warmup_sessions: int | None = None,
    project_name: str = "project",
) -> tuple[Path, dict]:
    project = root / project_name
    ok(cli("project", "init", "--project", str(project)))
    args = [
        "campaign", "create",
        "--project", str(project),
        "--store", str(store),
        "--snapshot-id", snapshot_id,
        "--attempts", "10",
        "--max-seconds", "30",
        "--initial-cash", str(initial_cash),
        "--transaction-cost-bps", str(transaction_cost_bps),
        "--slippage-bps", str(slippage_bps),
        "--execution-rules", execution_rules,
    ]
    if valuation_policy is not None:
        args += ["--valuation-policy", valuation_policy]
    if discovery_split is not None:
        args += ["--discovery-split", discovery_split]
    if discovery_warmup_sessions is not None:
        args += ["--discovery-warmup-sessions", str(discovery_warmup_sessions)]
    return project, ok(cli(*args))


def _submit_strategy(
    root: Path,
    project: Path,
    campaign_id: str,
    submission_id: str,
    *,
    family: str = "momentum",
    parameters: dict | None = None,
    history_requirements: list[dict] | None = None,
) -> dict:
    strategy: dict = {"family": family}
    if history_requirements is not None:
        strategy["history_requirements"] = history_requirements
    request = {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "submission_id": submission_id,
        "hypothesis": f"synthetic suspended valuation check {submission_id}",
        "strategy": strategy,
        "parameters": parameters if parameters is not None else {
            "lookback": 2,
            "top_k": 1,
            "cash_if_negative": False,
        },
    }
    path = write_json(root / f"{submission_id}.json", request)
    return ok(cli("experiment", "submit", "--project", str(project), "--input", str(path)))


def test_core_zero_cost_carry_forward_valuation_drawdown_and_policy_freeze(
    tmp_path: Path,
) -> None:
    """AC1: 100 shares @ 10 -> 1,000 suspended -> 900 resumed -> -10% drawdown."""
    # 15 sessions -> Discovery is first 9 sessions (indices 0..8).
    # momentum(lookback=2, top_k=1) needs 3 sessions (0,1,2) -> first signal at
    # day 2, fills at day 3 open = 10.0 (100 shares of TARGET, 0 cash).
    # Day 4: confirmed full-day suspension -> carried forward at 10.0 (NAV 1000).
    # Day 5: resumes trading, closes at 9.0 -> NAV 900 (-10% drawdown).
    # Days 6..8: stay at 9.0.
    payload = schema_v3_fixture(15)
    _set_constant_prices(payload, raw_price=10.0)
    buy_day = day_at(3)
    suspended_day = day_at(4)
    resume_day = day_at(5)
    make_confirmed_suspension(payload, suspended_day, TARGET)
    for idx in range(5, 9):
        d = day_at(idx)
        row = raw_row(payload, d, TARGET)
        row["open"] = 9.0
        row["high"] = 9.0
        row["low"] = 9.0
        row["close"] = 9.0

    sealed = seal_v3(tmp_path, payload, name="core-carry")
    store = sealed["store"]
    snapshot_id = sealed["snapshot_id"]

    # Carry-forward policy campaign
    project, created = _init_campaign(
        tmp_path,
        store,
        snapshot_id,
        initial_cash=1000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
        execution_rules="actual-open-v1",
        valuation_policy="carry-forward-suspended-v1",
        project_name="carry-project",
    )
    assert created["execution_rules"]["valuation"]["version"] == "carry-forward-suspended-v1"
    assert created["valuation_policy"]["version"] == "carry-forward-suspended-v1"
    assert (
        created["execution_rules"]["valuation"]["confirmed_suspended_close"]
        == "carry_forward_prior_trusted_unadjusted_close"
    )

    receipt = _submit_strategy(
        tmp_path, project, created["campaign_id"], "core-carry-sub",
    )
    assert receipt["status"] == "succeeded"
    assert receipt["runtime_dependencies"]["valuation_policy"] == "carry-forward-suspended-v1"
    discovery = receipt["discovery"]
    curve = {point["date"]: point for point in discovery["equity_curve"]}

    # Day 3: bought 100 shares @ 10.0, closed @ 10.0 -> actual_close NAV 1000.
    buy_point = curve[buy_day]
    assert buy_point["nav"] == pytest.approx(1000.0)
    assert buy_point["valuation_basis"] == "actual_close"
    assert buy_point["carried_forward_symbols"] == []
    assert buy_point["holdings"][TARGET]["quantity"] == pytest.approx(100.0)
    assert buy_point["holdings"][TARGET]["valuation_basis"] == "actual_close"
    assert buy_point["holdings"][TARGET]["valuation_price"] == pytest.approx(10.0)
    assert buy_point["holdings"][TARGET]["close"] == pytest.approx(10.0)

    # Day 4: confirmed full-day suspension -> carried forward @ 10.0 -> NAV 1000.
    susp_point = curve[suspended_day]
    assert susp_point["nav"] == pytest.approx(1000.0)
    assert susp_point["cash"] == pytest.approx(0.0)
    assert susp_point["market_value"] == pytest.approx(1000.0)
    assert susp_point["valuation_basis"] == "carried_forward"
    assert susp_point["carried_forward_symbols"] == [TARGET]
    holding = susp_point["holdings"][TARGET]
    assert holding["quantity"] == pytest.approx(100.0)
    assert holding["close"] is None
    assert holding["valuation_price"] == pytest.approx(10.0)
    assert holding["market_value"] == pytest.approx(1000.0)
    assert holding["valuation_basis"] == "carried_forward_prior_close"
    audit = holding["carry_forward"]
    assert audit["source_close_date"] == buy_day
    assert audit["source_close_price"] == pytest.approx(10.0)
    assert audit["valuation_price"] == pytest.approx(10.0)
    assert audit["carried_sessions"] == 1
    assert audit["source_close_evidence"]["source"] == OPEN_SOURCE["source"]
    assert audit["source_close_evidence"]["evidence_id"] == OPEN_SOURCE["evidence_id"]
    assert audit["status_evidence"]["status_evidence_id"] == STATUS_EVIDENCE
    assert audit["adjustments"] == []

    # Day 5: resumes trading and closes @ 9.0 -> NAV 900, actual close restored.
    res_point = curve[resume_day]
    assert res_point["nav"] == pytest.approx(900.0)
    assert res_point["valuation_basis"] == "actual_close"
    assert res_point["carried_forward_symbols"] == []
    assert res_point["holdings"][TARGET]["close"] == pytest.approx(9.0)
    assert res_point["holdings"][TARGET]["valuation_price"] == pytest.approx(9.0)
    assert res_point["holdings"][TARGET]["valuation_basis"] == "actual_close"
    assert "carry_forward" not in res_point["holdings"][TARGET]

    # Metrics and valuation summary distinguish actual close days vs valued days.
    metrics = discovery["metrics"]
    assert metrics["sessions"] == 9
    assert metrics["valued_sessions"] == 9
    assert metrics["actual_close_sessions"] == 8
    assert metrics["carried_forward_sessions"] == 1
    assert metrics["missing_valuation_sessions"] == 0
    assert metrics["valuation_gaps"] == 0
    assert metrics["total_return"] == pytest.approx(-0.10)
    assert metrics["max_drawdown"] == pytest.approx(-0.10)

    summary = discovery["valuation_summary"]
    assert summary["policy_version"] == "carry-forward-suspended-v1"
    assert summary["sessions"] == 9
    assert summary["valued_sessions"] == 9
    assert summary["actual_close_sessions"] == 8
    assert summary["carried_forward_sessions"] == 1
    assert summary["missing_valuation_sessions"] == 0
    assert summary["ending_carried_forward"] == {}

    # Control: strict-close-v1 keeps legacy behavior (day 4 NAV None, drawdown None,
    # no new valuation_summary or carry-forward metrics keys).
    strict_project, strict_created = _init_campaign(
        tmp_path,
        store,
        snapshot_id,
        initial_cash=1000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
        execution_rules="actual-open-v1",
        valuation_policy="strict-close-v1",
        project_name="strict-project",
    )
    assert "version" not in strict_created["execution_rules"]["valuation"]
    assert "valuation_policy" not in strict_created
    assert strict_created["fingerprints"]["evaluation"] != created["fingerprints"]["evaluation"]

    strict_receipt = _submit_strategy(
        tmp_path, strict_project, strict_created["campaign_id"], "core-strict-sub",
    )
    strict_disc = strict_receipt["discovery"]
    strict_curve = {p["date"]: p for p in strict_disc["equity_curve"]}
    assert strict_curve[suspended_day]["nav"] is None
    assert "valuation_basis" not in strict_curve[suspended_day]
    assert strict_disc["metrics"]["valued_sessions"] == 8
    assert strict_disc["metrics"]["max_drawdown"] is None
    assert "actual_close_sessions" not in strict_disc["metrics"]
    assert "valuation_summary" not in strict_disc

    # Protected Evaluation rejects carry-forward-suspended-v1 cleanly before any debit.
    frozen = ok(
        cli(
            "candidate", "freeze",
            "--project", str(project),
            "--campaign-id", created["campaign_id"],
            "--submission-id", "core-carry-sub",
            "--name", "carry-cand",
            "--conclusion", "test protected rejection",
        )
    )
    store_eval = tmp_path / "eval-store"
    p_input = write_json(tmp_path / "plan.json", plan_input())
    plan_res = cli(
        "evaluation", "plan",
        "--store", str(store_eval),
        "--snapshot-store", str(store),
        "--project", str(project),
        "--candidate-id", frozen["candidate_id"],
        "--input", str(p_input),
    )
    assert plan_res.returncode != 0

    # Schema-1 + next-open-v1 + carry-forward-suspended-v1 specifically hits the
    # valuation-policy rejection in Protected Evaluation.
    syn_payload = full_history(15)
    syn_source = write_json(tmp_path / "syn.json", syn_payload)
    syn_store = tmp_path / "syn-store"
    syn_sealed = ok(
        cli("snapshot", "seal", "--input", str(syn_source), "--store", str(syn_store))
    )
    syn_project, syn_created = _init_campaign(
        tmp_path,
        syn_store,
        syn_sealed["snapshot_id"],
        initial_cash=1000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
        execution_rules="next-open-v1",
        valuation_policy="carry-forward-suspended-v1",
        project_name="syn-carry-project",
    )
    _submit_strategy(
        tmp_path,
        syn_project,
        syn_created["campaign_id"],
        "syn-carry-sub",
        family="equal_weight",
        parameters={},
    )
    syn_frozen = ok(
        cli(
            "candidate", "freeze",
            "--project", str(syn_project),
            "--campaign-id", syn_created["campaign_id"],
            "--submission-id", "syn-carry-sub",
            "--name", "syn-carry-cand",
            "--conclusion", "reject carry-forward in protected eval",
        )
    )
    syn_plan_res = cli(
        "evaluation", "plan",
        "--store", str(store_eval),
        "--snapshot-store", str(syn_store),
        "--project", str(syn_project),
        "--candidate-id", syn_frozen["candidate_id"],
        "--input", str(p_input),
    )
    syn_rej = rejected(syn_plan_res, "INVALID_PLAN_INPUT", phase="plan")
    assert "carry-forward-suspended-v1" in syn_rej["error"]["message"]


def test_candidate_benchmark_parity_differing_holdings_staggered_halts_and_unheld_gap(
    tmp_path: Path,
) -> None:
    """AC2: equal-weight parity, differing holdings, staggered multi-ETF halts, unheld gap."""
    # 15 sessions -> Discovery is indices 0..8 (9 sessions).
    # Equal-weight benchmark & candidate buy 200 each of 5 ETFs @ 10.0 on day 1 (20 shares each).
    # Staggered halts & price moves:
    # - Day 2: SYMBOLS[0] confirmed suspended (carried @ 10.0, day 1);
    #          SYMBOLS[1] closes @ 11.0 (+10%); others @ 10.0.
    #          Equal-weight NAV = 20*10 + 20*11 + 20*10*3 = 1020.0
    # - Day 3: SYMBOLS[0] still suspended (carried @ 10.0, day 2);
    #          SYMBOLS[1] confirmed suspended (carried @ 11.0, day 1);
    #          SYMBOLS[2] closes @ 9.0 (-10%); others @ 10.0.
    #          Equal-weight NAV = 20*10 + 20*11 + 20*9 + 20*10*2 = 1000.0
    # - Day 4: SYMBOLS[0] resumes @ 12.0; SYMBOLS[1] still suspended (carried @ 11.0, day 2);
    #          SYMBOLS[2] closes @ 9.5; others @ 10.0.
    #          Equal-weight NAV = 20*12 + 20*11 + 20*9.5 + 20*10*2 = 1050.0
    # - Day 5: SYMBOLS[1] resumes @ 8.0; SYMBOLS[0]=12.0; SYMBOLS[2]=9.5; others=10.0.
    #          Equal-weight NAV = 20*12 + 20*8 + 20*9.5 + 20*10*2 = 990.0
    # - Days 6..8: stay at day 5 closes.
    payload = schema_v3_fixture(15)
    _set_constant_prices(payload, raw_price=10.0)
    make_confirmed_suspension(payload, day_at(2), SYMBOLS[0])
    raw_row(payload, day_at(2), SYMBOLS[1]).update(open=11.0, high=11.0, low=11.0, close=11.0)

    make_confirmed_suspension(payload, day_at(3), SYMBOLS[0])
    make_confirmed_suspension(payload, day_at(3), SYMBOLS[1])
    raw_row(payload, day_at(3), SYMBOLS[2]).update(open=9.0, high=9.0, low=9.0, close=9.0)

    raw_row(payload, day_at(4), SYMBOLS[0]).update(open=12.0, high=12.0, low=12.0, close=12.0)
    make_confirmed_suspension(payload, day_at(4), SYMBOLS[1])
    raw_row(payload, day_at(4), SYMBOLS[2]).update(open=9.5, high=9.5, low=9.5, close=9.5)

    for idx in range(5, 9):
        d = day_at(idx)
        raw_row(payload, d, SYMBOLS[0]).update(open=12.0, high=12.0, low=12.0, close=12.0)
        raw_row(payload, d, SYMBOLS[1]).update(open=8.0, high=8.0, low=8.0, close=8.0)
        raw_row(payload, d, SYMBOLS[2]).update(open=9.5, high=9.5, low=9.5, close=9.5)

    sealed = seal_v3(tmp_path, payload, name="staggered")
    project, created = _init_campaign(
        tmp_path,
        sealed["store"],
        sealed["snapshot_id"],
        initial_cash=1000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
        execution_rules="actual-open-v1",
        valuation_policy="carry-forward-suspended-v1",
        project_name="staggered-project",
    )

    # 1. Equal-weight candidate vs equal-weight benchmark: identical holdings & valuation.
    ew_receipt = _submit_strategy(
        tmp_path,
        project,
        created["campaign_id"],
        "ew-parity",
        family="equal_weight",
        parameters={},
    )
    cand = ew_receipt["discovery"]
    bench = cand["baseline_comparison"]["baseline"]
    assert cand["equity_curve"] == bench["equity_curve"]
    assert cand["valuation_summary"] == bench["valuation_summary"]
    assert cand["metrics"] == bench["metrics"]

    curve = {p["date"]: p for p in cand["equity_curve"]}
    assert curve[day_at(2)]["nav"] == pytest.approx(1020.0)
    assert curve[day_at(2)]["carried_forward_symbols"] == [SYMBOLS[0]]
    assert curve[day_at(2)]["holdings"][SYMBOLS[0]]["carry_forward"]["carried_sessions"] == 1
    assert curve[day_at(2)]["holdings"][SYMBOLS[0]]["carry_forward"]["source_close_date"] == day_at(1)

    assert curve[day_at(3)]["nav"] == pytest.approx(1000.0)
    assert curve[day_at(3)]["carried_forward_symbols"] == sorted([SYMBOLS[0], SYMBOLS[1]])
    assert curve[day_at(3)]["holdings"][SYMBOLS[0]]["carry_forward"]["carried_sessions"] == 2
    assert curve[day_at(3)]["holdings"][SYMBOLS[1]]["carry_forward"]["carried_sessions"] == 1
    assert curve[day_at(3)]["holdings"][SYMBOLS[1]]["carry_forward"]["source_close_date"] == day_at(2)
    assert curve[day_at(3)]["holdings"][SYMBOLS[1]]["carry_forward"]["source_close_price"] == pytest.approx(11.0)

    assert curve[day_at(4)]["nav"] == pytest.approx(1050.0)
    assert curve[day_at(4)]["carried_forward_symbols"] == [SYMBOLS[1]]
    assert curve[day_at(4)]["holdings"][SYMBOLS[0]]["valuation_basis"] == "actual_close"
    assert curve[day_at(4)]["holdings"][SYMBOLS[1]]["carry_forward"]["carried_sessions"] == 2

    assert curve[day_at(5)]["nav"] == pytest.approx(990.0)
    assert curve[day_at(5)]["valuation_basis"] == "actual_close"
    assert curve[day_at(5)]["carried_forward_symbols"] == []
    # Peak NAV was 1050.0 (day 4), trough 990.0 (day 5) -> drawdown = 990/1050 - 1
    assert cand["metrics"]["max_drawdown"] == pytest.approx(990.0 / 1050.0 - 1.0)
    assert cand["metrics"]["carried_forward_sessions"] == 3
    assert cand["metrics"]["actual_close_sessions"] == 6

    # 2. Differing holdings & unheld symbol gap:
    # Build a snapshot where SYMBOLS[4] (unheld by momentum top_k=1) has an
    # UNKNOWN status (unexplained gap!) on day 5, while TARGET (SYMBOLS[0])
    # trades normally. Candidate holding only TARGET must have 0 valuation gaps
    # and complete NAV/drawdown; benchmark holding all 5 ETFs must have a gap on day 5!
    payload2 = schema_v3_fixture(15)
    _set_constant_prices(payload2, raw_price=10.0)
    make_unknown_status(payload2, day_at(5), SYMBOLS[4])
    sealed2 = seal_v3(tmp_path, payload2, store="store-unheld", name="unheld-gap")
    proj2, created2 = _init_campaign(
        tmp_path,
        sealed2["store"],
        sealed2["snapshot_id"],
        initial_cash=1000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
        execution_rules="actual-open-v1",
        valuation_policy="carry-forward-suspended-v1",
        project_name="unheld-project",
    )
    mom_receipt = _submit_strategy(
        tmp_path, proj2, created2["campaign_id"], "mom-unheld-gap",
    )
    mom_disc = mom_receipt["discovery"]
    mom_bench = mom_disc["baseline_comparison"]["baseline"]
    # Candidate only holds SYMBOLS[0], NOT SYMBOLS[4] -> no valuation gap!
    assert mom_disc["metrics"]["valuation_gaps"] == 0
    assert mom_disc["metrics"]["valued_sessions"] == 9
    assert mom_disc["metrics"]["actual_close_sessions"] == 9
    assert mom_disc["metrics"]["carried_forward_sessions"] == 0
    assert mom_disc["metrics"]["max_drawdown"] == pytest.approx(0.0)
    mom_day5 = next(p for p in mom_disc["equity_curve"] if p["date"] == day_at(5))
    assert mom_day5["nav"] == pytest.approx(1000.0)
    assert mom_day5["holdings"][SYMBOLS[4]]["valuation_basis"] == "not_held"
    # Benchmark holds SYMBOLS[4] -> day 5 has an unknown-status valuation gap!
    assert mom_bench["metrics"]["valuation_gaps"] == 1
    assert mom_bench["metrics"]["valued_sessions"] == 8
    assert mom_bench["metrics"]["max_drawdown"] is None
    assert mom_bench["gaps"]["valuation"][0]["valuation_gap_reason"] == "unknown_trading_status"


def _weekday_dates(start: date, count: int) -> list[str]:
    days: list[str] = []
    cur = start
    while len(days) < count:
        if cur.weekday() < 5:
            days.append(cur.isoformat())
        cur += timedelta(days=1)
    return days


def _schema_v3_on_dates(days: list[str]) -> dict:
    """Build a schema-3 fixture on exact trading dates (e.g. across weekend/quarter)."""
    payload = schema_v3_fixture(len(days))
    old_to_new = dict(zip([row["date"] for row in payload["calendar"]], days, strict=True))
    for source in payload["sources"].values():
        source["coverage_start"] = days[0]
        source["coverage_end"] = days[-1]
    for cov in payload["symbol_coverage"].values():
        cov["start"] = days[0]
        cov["end"] = days[-1]
    for row in payload["calendar"]:
        row["date"] = old_to_new[row["date"]]
    for row in payload["raw_bars"]:
        new_day = old_to_new[row["date"]]
        row["date"] = new_day
        row["open_available_at"] = f"{new_day}T09:30:00+08:00"
        row["close_available_at"] = f"{new_day}T15:05:00+08:00"
        row["available_at"] = f"{new_day}T15:05:00+08:00"
        row["availability_evidence"] = f"synthetic:tdx:{new_day}:close-batch"
    for row in payload["derived"]:
        new_day = old_to_new[row["date"]]
        row["date"] = new_day
        row["available_at"] = f"{new_day}T15:05:00+08:00"
        row["availability_evidence"] = f"synthetic:derivation:{new_day}"
    return payload


def test_continuous_suspension_across_weekend_quarter_and_full_history_end(
    tmp_path: Path,
) -> None:
    """AC3: multi-day suspension across weekend, Q1->Q2, and authorized/scoring end."""
    # Start 2024-03-18 (Mon): 20 weekday sessions -> spans 2024-Q1 (Mar 18..29, 10 days)
    # and 2024-Q2 (Apr 1..12, 10 days).
    # With --full-history --discovery-split 70/30:
    #   20 sessions -> development = 14 (indices 0..13: 2024-03-18..2024-04-04),
    #                  trailing = 6 (indices 14..19: 2024-04-05..2024-04-12).
    # With --discovery-warmup-sessions 4:
    #   warmup = indices 0..3 (4 sessions), scoring = indices 4..13 (10 sessions).
    # Place TWO continuous suspensions on TARGET (SYMBOLS[0]):
    # 1. Cross-weekend & cross-quarter: Fri 2024-03-29 (idx 9, last day of Q1)
    #    and Mon 2024-04-01 (idx 10, first day of Q2), resuming Tue 2024-04-02 (idx 11) @ 9.5.
    # 2. Ending on the LAST session of development (idx 13: 2024-04-04) AND ALSO
    #    ending on the LAST session of the authorized full-history range (idx 18..19: 2024-04-11..12)!
    days = _weekday_dates(date(2024, 3, 18), 20)
    payload = _schema_v3_on_dates(days)
    _set_constant_prices(payload, raw_price=10.0)

    # Cross-weekend + cross-quarter suspension (Mar 29 Fri -> Apr 1 Mon)
    q1_end = days[9]   # 2024-03-29 (Friday, end of Q1)
    q2_start = days[10]  # 2024-04-01 (Monday, start of Q2)
    assert q1_end == "2024-03-29"
    assert q2_start == "2024-04-01"
    make_confirmed_suspension(payload, q1_end, TARGET)
    make_confirmed_suspension(payload, q2_start, TARGET)
    # Resume on Apr 2 (idx 11) at 9.5, stay 9.5 on Apr 3 (idx 12)
    for idx in (11, 12):
        raw_row(payload, days[idx], TARGET).update(open=9.5, high=9.5, low=9.5, close=9.5)
    # Development scoring end (idx 13: Apr 4) is confirmed suspended!
    dev_end = days[13]
    make_confirmed_suspension(payload, dev_end, TARGET)
    # Authorized full-history end (indices 18..19: Apr 11..12) is ALSO confirmed suspended!
    make_confirmed_suspension(payload, days[18], TARGET)
    make_confirmed_suspension(payload, days[19], TARGET)

    store = tmp_path / "fh-store"
    sealed = seal_full_history(tmp_path, store, payload, "auth-end-suspended")
    snapshot_id = sealed["snapshot_id"]
    manifest = ok(cli("snapshot", "manifest", "--store", str(store), "--id", snapshot_id))

    # Authorized & common_range boundaries must NOT move earlier despite trailing suspension!
    assert manifest["common_range"] == {"start": days[0], "end": days[-1]}
    assert manifest["authorized_history"]["range"]["start"] == days[0]
    assert manifest["authorized_history"]["range"]["end"] == days[-1]
    assert manifest["authorized_history"]["range"]["sessions"] == 20

    project, created = _init_campaign(
        tmp_path,
        store,
        snapshot_id,
        initial_cash=1000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
        execution_rules="actual-open-v1",
        valuation_policy="carry-forward-suspended-v1",
        discovery_split="70/30",
        discovery_warmup_sessions=4,
        project_name="fh-project",
    )
    # Verify warmup, scoring, discovery, validation boundaries did NOT shift!
    fh = created["full_history"]
    assert fh["warmup"] == {
        "source": "authorized_history",
        "start": days[0],
        "end": days[3],
        "sessions": 4,
    }
    assert fh["scoring"] == {"start": days[4], "end": days[13], "sessions": 10}
    assert fh["discovery"]["start"] == days[0]
    assert fh["discovery"]["end"] == days[13]
    assert fh["validation"]["start"] == days[14]
    assert fh["validation"]["end"] == days[19]

    receipt = _submit_strategy(
        tmp_path,
        project,
        created["campaign_id"],
        "fh-carry-sub",
        family="momentum",
        parameters={"lookback": 2, "top_k": 1, "cash_if_negative": False},
    )
    assert receipt["status"] == "succeeded"
    disc = receipt["discovery"]
    curve = {p["date"]: p for p in disc["equity_curve"]}

    # No weekend points generated! Only exact scoring trading sessions days[4..13].
    assert [p["date"] for p in disc["equity_curve"]] == days[4:14]
    # Mar 29 (Fri): carried session 1 from Mar 28 @ 10.0
    assert curve[q1_end]["nav"] == pytest.approx(1000.0)
    assert curve[q1_end]["holdings"][TARGET]["carry_forward"]["carried_sessions"] == 1
    assert curve[q1_end]["holdings"][TARGET]["carry_forward"]["source_close_date"] == days[8]
    # Apr 1 (Mon): carried session 2 (weekend NOT counted!) from Mar 28 @ 10.0
    assert curve[q2_start]["nav"] == pytest.approx(1000.0)
    assert curve[q2_start]["holdings"][TARGET]["carry_forward"]["carried_sessions"] == 2
    assert curve[q2_start]["holdings"][TARGET]["carry_forward"]["source_close_date"] == days[8]
    # Apr 2 (Tue): resumes @ 9.5 -> NAV 950.0 (-5% drawdown)
    assert curve[days[11]]["nav"] == pytest.approx(950.0)
    assert curve[days[11]]["valuation_basis"] == "actual_close"
    # Apr 4 (scoring end): suspended -> carried session 1 from Apr 3 @ 9.5 -> NAV 950.0
    assert curve[dev_end]["nav"] == pytest.approx(950.0)
    assert curve[dev_end]["valuation_basis"] == "carried_forward"
    assert curve[dev_end]["holdings"][TARGET]["carry_forward"]["source_close_date"] == days[12]
    assert curve[dev_end]["holdings"][TARGET]["carry_forward"]["source_close_price"] == pytest.approx(9.5)
    assert curve[dev_end]["holdings"][TARGET]["carry_forward"]["carried_sessions"] == 1

    # Ending carried-forward status is disclosed in valuation_summary!
    vs = disc["valuation_summary"]
    assert vs["sessions"] == 10
    assert vs["valued_sessions"] == 10
    assert vs["actual_close_sessions"] == 7
    assert vs["carried_forward_sessions"] == 3
    assert vs["missing_valuation_sessions"] == 0
    assert set(vs["ending_carried_forward"]) == {TARGET}
    assert vs["ending_carried_forward"][TARGET]["source_close_date"] == days[12]
    assert vs["ending_carried_forward"][TARGET]["carried_sessions"] == 1
    assert disc["metrics"]["max_drawdown"] == pytest.approx(-0.05)

    # Quarterly summary reconciles continuous ledger across Q1 and Q2 and discloses
    # quarter-end carry-forward status without altering coverage keys.
    qs = receipt["quarterly_summary"]
    quarters = {q["quarter"]: q for q in qs["quarters"]}
    assert list(quarters) == ["2024-Q1", "2024-Q2"]
    q1 = quarters["2024-Q1"]
    q2 = quarters["2024-Q2"]
    assert q1["start_date"] == days[4]
    assert q1["end_date"] == q1_end
    assert q1["candidate"]["net_return"] == pytest.approx(0.0)
    assert q1["candidate"]["max_drawdown"] == pytest.approx(0.0)
    assert set(q1["candidate"]["coverage"]) == {"valued_sessions", "expected_sessions", "fraction"}
    assert q1["candidate"]["valuation"]["carried_forward_sessions"] == 1
    assert q1["candidate"]["valuation"]["actual_close_sessions"] == 5
    assert set(q1["candidate"]["valuation"]["ending_carried_forward"]) == {TARGET}
    assert q1["candidate"]["valuation"]["ending_carried_forward"][TARGET]["carried_sessions"] == 1

    assert q2["start_date"] == q2_start
    assert q2["end_date"] == dev_end
    assert q2["candidate"]["net_return"] == pytest.approx(-0.05)
    assert q2["candidate"]["max_drawdown"] == pytest.approx(-0.05)
    assert q2["candidate"]["valuation"]["carried_forward_sessions"] == 2
    assert q2["candidate"]["valuation"]["actual_close_sessions"] == 2
    assert set(q2["candidate"]["valuation"]["ending_carried_forward"]) == {TARGET}
    assert q2["candidate"]["valuation"]["ending_carried_forward"][TARGET]["source_close_date"] == days[12]

    # Negative boundary check: an unexplained unknown status on the authorized
    # end date must STILL be rejected as an incomplete boundary!
    bad_payload = _schema_v3_on_dates(days)
    _set_constant_prices(bad_payload, raw_price=10.0)
    make_unknown_status(bad_payload, days[-1], TARGET)
    bad_source = write_json(tmp_path / "bad-tail.json", bad_payload)
    bad_res = cli(
        "snapshot", "seal",
        "--input", str(bad_source),
        "--store", str(tmp_path / "bad-fh-store"),
        "--full-history",
        "--authorization-id", "auth-bad-tail",
        "--authorized-start", days[0],
        "--authorized-end", days[-1],
    )
    assert bad_res.returncode != 0
    assert "complete common trading-session boundaries" in bad_res.stderr


def test_gap_missing_authorized_prior_close_and_recovery(tmp_path: Path) -> None:
    """AC4: held symbol without authorized prior close stays a gap, recovers on trusted close."""
    payload = schema_v3_fixture(15)
    _set_constant_prices(payload, raw_price=10.0)
    # Day 3: strategy buys 100 shares of TARGET @ 10.0, but day 3 close is only
    # available AFTER day 3's valuation cutoff (late close_available_at).
    # Day 4: confirmed full-day suspension -> NO authorized prior close exists!
    # Day 5: resumes trading with timely close @ 9.0 -> recovers actual close!
    buy_day = day_at(3)
    susp_day = day_at(4)
    resume_day = day_at(5)
    raw_row(payload, buy_day, TARGET)["close_available_at"] = f"{day_at(6)}T15:05:00+08:00"
    make_confirmed_suspension(payload, susp_day, TARGET)
    for idx in range(5, 9):
        raw_row(payload, day_at(idx), TARGET).update(open=9.0, high=9.0, low=9.0, close=9.0)

    sealed = seal_v3(tmp_path, payload, name="missing-prior-close")
    project, created = _init_campaign(
        tmp_path,
        sealed["store"],
        sealed["snapshot_id"],
        initial_cash=1000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
        execution_rules="actual-open-v1",
        valuation_policy="carry-forward-suspended-v1",
        project_name="missing-prior-project",
    )
    receipt = _submit_strategy(tmp_path, project, created["campaign_id"], "missing-prior-sub")
    disc = receipt["discovery"]
    curve = {p["date"]: p for p in disc["equity_curve"]}
    gaps_by_day = {g["date"]: g for g in disc["gaps"]["valuation"]}

    assert curve[buy_day]["nav"] is None
    assert gaps_by_day[buy_day]["valuation_gap_reason"] == "close_evidence_after_cutoff"
    assert curve[susp_day]["nav"] is None
    assert gaps_by_day[susp_day]["valuation_gap_reason"] == "missing_authorized_prior_close"
    assert curve[resume_day]["nav"] == pytest.approx(900.0)
    assert curve[resume_day]["valuation_basis"] == "actual_close"
    assert disc["metrics"]["max_drawdown"] is None
    assert disc["metrics"]["missing_valuation_sessions"] == 2
    assert disc["metrics"]["valued_sessions"] == 7


@pytest.mark.parametrize(
    ("case_name", "expected_reason"),
    [
        ("unknown", "unknown_trading_status"),
        ("late_notice", "status_evidence_after_cutoff"),
        ("status_conflict", "conflicting_status_or_trade_evidence"),
        ("halt_volume_conflict", "conflicting_status_or_trade_evidence"),
    ],
)
def test_gap_unexplained_late_notice_and_conflicting_evidence(
    tmp_path: Path, case_name: str, expected_reason: str,
) -> None:
    """AC4: unknown gap, late status notice, and conflicting evidence stay gaps and recover."""
    payload = schema_v3_fixture(15)
    _set_constant_prices(payload, raw_price=10.0)
    gap_day = day_at(4)
    resume_day = day_at(5)
    if case_name == "unknown":
        make_unknown_status(payload, gap_day, TARGET)
    elif case_name == "late_notice":
        row = make_confirmed_suspension(payload, gap_day, TARGET)
        # Notice published AFTER day 4's 23:59:59+08:00 valuation cutoff!
        row["available_at"] = f"{resume_day}T09:00:00+08:00"
    elif case_name == "status_conflict":
        row = make_unknown_status(payload, gap_day, TARGET)
        row["status_evidence_id"] = STATUS_EVIDENCE
        row["status_conflict"] = {
            "price_evidence_id": PRICE_EVIDENCE,
            "halt": {
                "start": f"{gap_day}T09:30:00+08:00",
                "end": f"{gap_day}T15:00:00+08:00",
            },
        }
    else:
        make_whole_day_halt(payload, gap_day, TARGET, volume=100)

    for idx in range(5, 9):
        raw_row(payload, day_at(idx), TARGET).update(open=9.0, high=9.0, low=9.0, close=9.0)

    sealed = seal_v3(tmp_path, payload, store=f"store-{case_name}", name=case_name)
    project, created = _init_campaign(
        tmp_path,
        sealed["store"],
        sealed["snapshot_id"],
        initial_cash=1000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
        execution_rules="actual-open-v1",
        valuation_policy="carry-forward-suspended-v1",
        project_name=f"proj-{case_name}",
    )
    receipt = _submit_strategy(tmp_path, project, created["campaign_id"], f"sub-{case_name}")
    disc = receipt["discovery"]
    curve = {p["date"]: p for p in disc["equity_curve"]}

    assert curve[gap_day]["nav"] is None
    assert curve[gap_day]["valuation_basis"] == "missing"
    assert curve[gap_day]["holdings"][TARGET]["gap_reason"] == expected_reason
    assert len(disc["gaps"]["valuation"]) == 1
    assert disc["gaps"]["valuation"][0]["date"] == gap_day
    assert disc["gaps"]["valuation"][0]["valuation_gap_reason"] == expected_reason
    assert disc["metrics"]["max_drawdown"] is None
    assert disc["metrics"]["missing_valuation_sessions"] == 1
    assert disc["metrics"]["valued_sessions"] == 8
    # Recovers from resumption day onward; no retroactive backfill to gap_day!
    assert curve[resume_day]["nav"] == pytest.approx(900.0)
    assert curve[resume_day]["valuation_basis"] == "actual_close"


def test_gap_mid_chain_break_and_resumed_open_without_close(tmp_path: Path) -> None:
    """AC4: mid-chain break blocks later carry; resumed open without close fills but lacks EOD NAV."""
    # Part 1: Mid-chain break + recovery
    # Day 3: bought @ 10.0, trusted close 10.0
    # Day 4: confirmed suspended -> carried forward (NAV 1000, carried_sessions=1)
    # Day 5: unknown status -> gap (unknown_trading_status), breaks chain!
    # Day 6: confirmed suspended -> CANNOT cross day 5 gap! gap (carry_forward_chain_broken)
    # Day 7: resumes with trusted close 9.0 -> recovers actual close (NAV 900)
    payload = schema_v3_fixture(15)
    _set_constant_prices(payload, raw_price=10.0)
    make_confirmed_suspension(payload, day_at(4), TARGET)
    make_unknown_status(payload, day_at(5), TARGET)
    make_confirmed_suspension(payload, day_at(6), TARGET)
    for idx in (7, 8):
        raw_row(payload, day_at(idx), TARGET).update(open=9.0, high=9.0, low=9.0, close=9.0)

    sealed = seal_v3(tmp_path, payload, store="store-chain", name="chain-break")
    project, created = _init_campaign(
        tmp_path,
        sealed["store"],
        sealed["snapshot_id"],
        initial_cash=1000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
        execution_rules="actual-open-v1",
        valuation_policy="carry-forward-suspended-v1",
        project_name="chain-project",
    )
    receipt = _submit_strategy(tmp_path, project, created["campaign_id"], "chain-sub")
    disc = receipt["discovery"]
    curve = {p["date"]: p for p in disc["equity_curve"]}
    gaps_by_day = {g["date"]: g for g in disc["gaps"]["valuation"]}

    assert curve[day_at(4)]["nav"] == pytest.approx(1000.0)
    assert curve[day_at(4)]["valuation_basis"] == "carried_forward"
    assert curve[day_at(5)]["nav"] is None
    assert gaps_by_day[day_at(5)]["valuation_gap_reason"] == "unknown_trading_status"
    assert curve[day_at(6)]["nav"] is None
    assert gaps_by_day[day_at(6)]["valuation_gap_reason"] == "carry_forward_chain_broken"
    assert curve[day_at(7)]["nav"] == pytest.approx(900.0)
    assert curve[day_at(7)]["valuation_basis"] == "actual_close"
    assert disc["metrics"]["max_drawdown"] is None
    assert disc["metrics"]["carried_forward_sessions"] == 1
    assert disc["metrics"]["missing_valuation_sessions"] == 2

    # Part 2: Resumed open without close (coexistence of trusted open fill + missing EOD valuation)
    # Day 3: partial_halt with executable open=10.0, volume=1000, but close=None!
    # Strategy buys 100 shares at open=10.0 on day 3 (FILL SUCCEEDS!), while day 3
    # close valuation is missing (partial_halt_missing_close), then recovers on day 4!
    payload2 = schema_v3_fixture(15)
    _set_constant_prices(payload2, raw_price=10.0)
    resumed_no_close = make_actual_open_halt(payload2, day_at(3), TARGET, open_price=10.0)
    resumed_no_close["close"] = None
    resumed_no_close["close_available_at"] = None
    derived_row(payload2, day_at(3), TARGET)["value"] = None
    for idx in range(4, 9):
        raw_row(payload2, day_at(idx), TARGET).update(open=9.5, high=9.5, low=9.5, close=9.5)

    sealed2 = seal_v3(tmp_path, payload2, store="store-open-noclose", name="open-noclose")
    proj2, created2 = _init_campaign(
        tmp_path,
        sealed2["store"],
        sealed2["snapshot_id"],
        initial_cash=1000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
        execution_rules="actual-open-v1",
        valuation_policy="carry-forward-suspended-v1",
        project_name="open-noclose-project",
    )
    rec2 = _submit_strategy(tmp_path, proj2, created2["campaign_id"], "open-noclose-sub")
    disc2 = rec2["discovery"]
    curve2 = {p["date"]: p for p in disc2["equity_curve"]}

    # Trusted open fill succeeded on day 3 even though close was missing!
    day3_fills = [f for f in disc2["fills"] if f["date"] == day_at(3) and f["symbol"] == TARGET]
    assert len(day3_fills) == 1
    assert day3_fills[0]["quantity"] == pytest.approx(100.0)
    assert day3_fills[0]["raw_open"] == pytest.approx(10.0)
    assert curve2[day_at(3)]["nav"] is None
    assert curve2[day_at(3)]["holdings"][TARGET]["gap_reason"] == "partial_halt_missing_close"
    assert disc2["metrics"]["max_drawdown"] is None
    # Recovers on day 4 with real close 9.5 -> NAV 950.0
    assert curve2[day_at(4)]["nav"] == pytest.approx(950.0)
    assert curve2[day_at(4)]["valuation_basis"] == "actual_close"


def test_warmup_61_observations_order_independence_no_lookahead_and_isolation(
    tmp_path: Path,
) -> None:
    """AC5: 61-obs warmup never counts carried days; no fills/sizing/lookahead/cross-account leak."""
    # Part 1: 61-observation warmup independence
    # 100 weekday sessions -> full-history 70/30 split gives 70 development sessions (0..69).
    # Put 2 confirmed suspensions inside warmup (indices 10, 11) so:
    # - After 62 warmup sessions (indices 0..61), only 60 valid observations exist!
    #   momentum(lookback=60) needs 61 valid observations -> MUST reject INSUFFICIENT_HISTORY!
    # - After 63 warmup sessions (indices 0..62), exactly 61 valid observations exist -> SUCCEEDS!
    days100 = _weekday_dates(date(2024, 1, 2), 100)
    fh_payload = _schema_v3_on_dates(days100)
    _set_constant_prices(fh_payload, raw_price=10.0)
    make_confirmed_suspension(fh_payload, days100[10], TARGET)
    make_confirmed_suspension(fh_payload, days100[11], TARGET)
    fh_store = tmp_path / "warmup-store"
    fh_sealed = seal_full_history(tmp_path, fh_store, fh_payload, "auth-warmup-61")

    # 62 warmup sessions -> only 60 valid observations -> rejected!
    proj_short, camp_short = _init_campaign(
        tmp_path,
        fh_store,
        fh_sealed["snapshot_id"],
        initial_cash=1000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
        execution_rules="actual-open-v1",
        valuation_policy="carry-forward-suspended-v1",
        discovery_split="70/30",
        discovery_warmup_sessions=62,
        project_name="warmup-short",
    )
    req_short = write_json(
        tmp_path / "req-short.json",
        {
            "schema_version": 1,
            "campaign_id": camp_short["campaign_id"],
            "submission_id": "mom-60-short",
            "hypothesis": "60 valid obs + 2 suspended must not satisfy 61-obs warmup",
            "strategy": {"family": "momentum"},
            "parameters": {"lookback": 60, "top_k": 1, "cash_if_negative": False},
        },
    )
    short_res = cli(
        "experiment", "submit", "--project", str(proj_short), "--input", str(req_short)
    )
    assert short_res.returncode == 2
    short_payload = json.loads(short_res.stdout)
    assert short_payload["status"] == "failed"
    assert short_payload["error"]["code"] == "INSUFFICIENT_HISTORY"

    # 63 warmup sessions -> 61 valid observations + 2 suspended -> succeeds!
    proj_ok, camp_ok = _init_campaign(
        tmp_path,
        fh_store,
        fh_sealed["snapshot_id"],
        initial_cash=1000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
        execution_rules="actual-open-v1",
        valuation_policy="carry-forward-suspended-v1",
        discovery_split="70/30",
        discovery_warmup_sessions=63,
        project_name="warmup-ok",
    )
    ok_receipt = _submit_strategy(
        tmp_path,
        proj_ok,
        camp_ok["campaign_id"],
        "mom-60-ok",
        family="momentum",
        parameters={"lookback": 60, "top_k": 1, "cash_if_negative": False},
    )
    assert ok_receipt["status"] == "succeeded"
    assert ok_receipt["discovery"]["history_readiness"]["first_ready_date"] == days100[63]

    # Part 2: No fills, no sell proceeds, no equity-dependent sizing from carried price!
    # Strategy: momentum(lookback=2, top_k=2, cash_if_negative=False)
    # Days 0..2: SYMBOLS[0] and SYMBOLS[1] lead -> buys 50 shares each @ 10.0 on day 3 (cash 0).
    # Day 3 close: SYMBOLS[0]=10.0, SYMBOLS[1]=10.0.
    # Make SYMBOLS[2] & SYMBOLS[3] spike on day 3 derived so signal at day 3 close
    # targets (SYMBOLS[2]: 0.5, SYMBOLS[3]: 0.5) for day 4 open!
    # On day 4: SYMBOLS[0] is confirmed suspended! SYMBOLS[1], [2], [3] trade normally @ 10.0!
    # Expected on day 4:
    # - SYMBOLS[0] (held, suspended): NO fill (confirmed_no_trade), NO sell proceeds, keeps 50 shares!
    # - SYMBOLS[1] (held, target 0 -> explicit exit): SELLS 50 shares @ 10.0 -> cash = 500.0!
    # - SYMBOLS[2] & SYMBOLS[3] (positive targets, equity-dependent): UNFILLED with
    #   equity_basis_unknown (carried price of SYMBOLS[0] MUST NOT be used as open equity!).
    # - Day 4 close valuation: cash 500.0 + 50 shares SYMBOLS[0] carried @ 10.0 = NAV 1000.0!
    # Also test no future lookahead by varying day 5 resumption close (9.0 vs 15.0).
    def _build_switch_snapshot(resume_close: float) -> dict:
        p = schema_v3_fixture(15)
        days = [row["date"] for row in p["calendar"]]
        for idx, day in enumerate(days):
            for sym_idx, sym in enumerate(SYMBOLS):
                raw_row(p, day, sym).update(open=10.0, high=10.0, low=10.0, close=10.0)
                # Days 0..2: SYMBOLS[0] & SYMBOLS[1] lead
                if idx <= 2:
                    val = 10.0 + (0.5 * idx if sym_idx in (0, 1) else 0.05 * idx)
                else:
                    # Day 3+: SYMBOLS[2] & SYMBOLS[3] surge to top_k=2
                    val = 10.0 + (2.0 * idx if sym_idx in (2, 3) else 0.1 * idx)
                derived_row(p, day, sym)["value"] = val
        make_confirmed_suspension(p, day_at(4), SYMBOLS[0])
        for idx in range(5, 9):
            raw_row(p, day_at(idx), SYMBOLS[0]).update(
                open=resume_close, high=resume_close, low=resume_close, close=resume_close,
            )
        return p

    sealed_a = seal_v3(tmp_path, _build_switch_snapshot(9.0), store="store-sw-a", name="sw-a")
    sealed_b = seal_v3(tmp_path, _build_switch_snapshot(15.0), store="store-sw-b", name="sw-b")

    proj_a, camp_a = _init_campaign(
        tmp_path,
        sealed_a["store"],
        sealed_a["snapshot_id"],
        initial_cash=1000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
        execution_rules="actual-open-v1",
        valuation_policy="carry-forward-suspended-v1",
        project_name="switch-a",
    )
    rec_a = _submit_strategy(
        tmp_path,
        proj_a,
        camp_a["campaign_id"],
        "switch-sub-a",
        family="momentum",
        parameters={"lookback": 2, "top_k": 2, "cash_if_negative": False},
    )
    disc_a = rec_a["discovery"]
    day4_attempt = next(a for a in disc_a["order_attempts"] if a["date"] == day_at(4))
    assert day4_attempt["status"] == "partially_filled"
    assert day4_attempt["filled_symbols"] == [SYMBOLS[1]]
    unfilled_map = {item["symbol"]: item["reason"] for item in day4_attempt["unfilled"]}
    assert unfilled_map[SYMBOLS[0]] == "confirmed_no_trade"
    assert unfilled_map[SYMBOLS[2]] == "equity_basis_unknown"
    assert unfilled_map[SYMBOLS[3]] == "equity_basis_unknown"
    # No fills on suspended SYMBOLS[0] on day 4!
    assert not any(
        f["date"] == day_at(4) and f["symbol"] == SYMBOLS[0] for f in disc_a["fills"]
    )
    day4_point_a = next(p for p in disc_a["equity_curve"] if p["date"] == day_at(4))
    assert day4_point_a["cash"] == pytest.approx(500.0)
    assert day4_point_a["holdings"][SYMBOLS[0]]["quantity"] == pytest.approx(50.0)
    assert day4_point_a["holdings"][SYMBOLS[1]]["quantity"] == pytest.approx(0.0)
    assert day4_point_a["nav"] == pytest.approx(1000.0)
    assert day4_point_a["valuation_basis"] == "carried_forward"

    # No future lookahead: snapshot B with resume_close=15.0 yields IDENTICAL day 4 point!
    proj_b, camp_b = _init_campaign(
        tmp_path,
        sealed_b["store"],
        sealed_b["snapshot_id"],
        initial_cash=1000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
        execution_rules="actual-open-v1",
        valuation_policy="carry-forward-suspended-v1",
        project_name="switch-b",
    )
    rec_b = _submit_strategy(
        tmp_path,
        proj_b,
        camp_b["campaign_id"],
        "switch-sub-b",
        family="momentum",
        parameters={"lookback": 2, "top_k": 2, "cash_if_negative": False},
    )
    day4_point_b = next(p for p in rec_b["discovery"]["equity_curve"] if p["date"] == day_at(4))
    assert day4_point_b == day4_point_a

    # Account isolation: another experiment in the SAME campaign (equal_weight) has
    # completely independent holdings (20 shares each, cash 0) from switch-sub-a!
    ew_same_camp = _submit_strategy(
        tmp_path,
        proj_a,
        camp_a["campaign_id"],
        "ew-isolated",
        family="equal_weight",
        parameters={},
    )
    ew_day4 = next(p for p in ew_same_camp["discovery"]["equity_curve"] if p["date"] == day_at(4))
    assert ew_day4["cash"] == pytest.approx(0.0)
    assert ew_day4["holdings"][SYMBOLS[0]]["quantity"] == pytest.approx(20.0)
    assert ew_day4["holdings"][SYMBOLS[1]]["quantity"] == pytest.approx(20.0)


@pytest.mark.parametrize("action_type", ["cash_dividend", "share_adjustment"])
def test_corporate_action_during_suspension_fails_closed_until_explained(
    tmp_path: Path, action_type: str,
) -> None:
    """AC6: unadjusted corporate action on a suspended holding yields a gap; unheld has no gap."""
    payload = schema_v3_fixture(15)
    _set_constant_prices(payload, raw_price=10.0)
    susp_day = day_at(4)
    resume_day = day_at(5)
    make_confirmed_suspension(payload, susp_day, TARGET)
    action: dict = {
        "date": susp_day,
        "symbol": TARGET,
        "type": action_type,
        "available_at": f"{day_at(3)}T15:05:00+08:00",
        "availability_evidence": "synthetic:official-ca-notice",
        "collected_at": "2024-02-01T00:00:00+08:00",
        "terms_evidence": "synthetic:official-ca-terms",
    }
    if action_type == "cash_dividend":
        action["cash_per_share"] = 0.20
        action["pay_date"] = day_at(6)
    else:
        action["ratio"] = 2.0
    payload["corporate_actions"] = [action]
    for idx in range(5, 9):
        raw_row(payload, day_at(idx), TARGET).update(open=5.0, high=5.0, low=5.0, close=5.0)

    sealed = seal_v3(tmp_path, payload, store=f"store-ca-{action_type}", name=f"ca-{action_type}")
    project, created = _init_campaign(
        tmp_path,
        sealed["store"],
        sealed["snapshot_id"],
        initial_cash=1000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
        execution_rules="actual-open-v1",
        valuation_policy="carry-forward-suspended-v1",
        project_name=f"ca-proj-{action_type}",
    )
    receipt = _submit_strategy(tmp_path, project, created["campaign_id"], f"ca-sub-{action_type}")
    disc = receipt["discovery"]
    curve = {p["date"]: p for p in disc["equity_curve"]}

    # Day 4: corporate action hits carried-forward holding -> fails closed in #69!
    assert curve[susp_day]["nav"] is None
    assert curve[susp_day]["valuation_basis"] == "missing"
    assert (
        curve[susp_day]["holdings"][TARGET]["gap_reason"]
        == "suspended_corporate_action_unadjusted"
    )
    assert disc["metrics"]["max_drawdown"] is None
    assert disc["metrics"]["missing_valuation_sessions"] == 1
    # Day 5: resumes with real close -> recovers actual close valuation!
    assert curve[resume_day]["nav"] is not None
    assert curve[resume_day]["valuation_basis"] == "actual_close"

    # Control: an unheld symbol having a corporate action on a suspended day does
    # NOT produce a valuation gap for an account that does not hold it.
    unheld = SYMBOLS[4]
    payload_unheld = schema_v3_fixture(15)
    _set_constant_prices(payload_unheld, raw_price=10.0)
    make_confirmed_suspension(payload_unheld, susp_day, unheld)
    unheld_action = dict(action, symbol=unheld)
    payload_unheld["corporate_actions"] = [unheld_action]
    sealed_u = seal_v3(
        tmp_path, payload_unheld, store=f"store-ca-unheld-{action_type}", name=f"ca-u-{action_type}"
    )
    proj_u, camp_u = _init_campaign(
        tmp_path,
        sealed_u["store"],
        sealed_u["snapshot_id"],
        initial_cash=1000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
        execution_rules="actual-open-v1",
        valuation_policy="carry-forward-suspended-v1",
        project_name=f"ca-u-proj-{action_type}",
    )
    rec_u = _submit_strategy(tmp_path, proj_u, camp_u["campaign_id"], f"ca-u-sub-{action_type}")
    assert rec_u["discovery"]["metrics"]["valuation_gaps"] == 0
    assert rec_u["discovery"]["metrics"]["valued_sessions"] == 9
    assert rec_u["discovery"]["metrics"]["max_drawdown"] == pytest.approx(0.0)


