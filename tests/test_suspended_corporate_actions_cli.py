"""Public-CLI acceptance tests for corporate actions during suspension (#70 / #68-B).

All fixtures are synthetic JSON (including schema-3 ``data_kind: "real"`` inputs
used strictly to exercise real-schema provenance and evidence validation).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cli_helpers import cli, ok, write_json
from test_actual_open_cli import (
    day_at,
    make_confirmed_suspension,
    raw_row,
    schema_v3_fixture,
    seal_v3,
)
from test_full_history_cli import full_history
from test_suspended_valuation_cli import (
    SYMBOLS,
    TARGET,
    init_campaign,
    set_constant_prices,
    submit_strategy,
)


def _action(
    *,
    date: str,
    symbol: str,
    action_type: str,
    available_at: str | None = None,
    availability_evidence: str | None = "synthetic:official-ca-notice",
    terms_evidence: str | None = "synthetic:official-ca-terms",
    cash_per_share: float | None = None,
    pay_date: str | None = None,
    ratio: float | None = None,
    sequence: int | None = None,
) -> dict:
    row: dict = {
        "date": date,
        "symbol": symbol,
        "type": action_type,
        "available_at": available_at if available_at is not None else "2024-01-01T08:00:00+08:00",
        "availability_evidence": availability_evidence,
        "collected_at": "2024-02-01T00:00:00+08:00",
        "terms_evidence": terms_evidence,
    }
    if action_type == "cash_dividend":
        row["cash_per_share"] = cash_per_share
        row["pay_date"] = pay_date
    else:
        row["ratio"] = ratio
    if sequence is not None:
        row["sequence"] = sequence
    return row


def test_ex_dividend_and_payment_during_suspension_and_resumption(tmp_path: Path) -> None:
    """AC1: 100 shares @ 10, ex-div 0.20 -> MV 980, rec 20, NAV 1000; pay -> cash 20, NAV 1000."""
    # 15 sessions -> Discovery is indices 0..8.
    # Day 0..2: warmup/signal; Day 3: candidate buys 100 shares of TARGET @ 10.0 (cash 0).
    # Equal-weight benchmark buys 20 shares of each ETF @ 10.0 on day 1 (cash 0).
    # Days 4, 5, 6: TARGET confirmed full-day suspended.
    #   Day 4: ex-dividend 0.20/share, pay_date = Day 6 (still suspended!).
    #   Day 5: still suspended, receivable still 20.0 (no re-application!).
    #   Day 6: still suspended, payment date -> receivable moves to cash (+20.0), NAV 1000.0.
    # Days 7..8: TARGET resumes trading with real unadjusted close 9.50 (no double-adjustment!).
    payload = schema_v3_fixture(15)
    set_constant_prices(payload, raw_price=10.0)
    buy_day = day_at(3)
    ex_day = day_at(4)
    mid_susp_day = day_at(5)
    pay_day = day_at(6)
    resume_day = day_at(7)
    for d in (ex_day, mid_susp_day, pay_day):
        make_confirmed_suspension(payload, d, TARGET)
    for idx in range(7, 9):
        raw_row(payload, day_at(idx), TARGET).update(open=9.5, high=9.5, low=9.5, close=9.5)

    payload["corporate_actions"] = [
        _action(
            date=ex_day,
            symbol=TARGET,
            action_type="cash_dividend",
            available_at=f"{buy_day}T15:05:00+08:00",
            cash_per_share=0.20,
            pay_date=pay_day,
        ),
    ]

    sealed = seal_v3(tmp_path, payload, store="store-div", name="div")
    project, created = init_campaign(
        tmp_path,
        sealed["store"],
        sealed["snapshot_id"],
        initial_cash=1000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
        execution_rules="actual-open-v1",
        valuation_policy="carry-forward-suspended-v1",
        project_name="div-proj",
    )
    receipt = submit_strategy(tmp_path, project, created["campaign_id"], "div-sub")
    disc = receipt["discovery"]
    curve = {p["date"]: p for p in disc["equity_curve"]}

    # Day 4 (ex-dividend while suspended): MV 980, receivable 20, cash 0, NAV 1000.
    p_ex = curve[ex_day]
    assert p_ex["valuation_basis"] == "carried_forward"
    assert p_ex["cash"] == pytest.approx(0.0)
    assert p_ex["dividend_receivables"] == pytest.approx(20.0)
    assert p_ex["market_value"] == pytest.approx(980.0)
    assert p_ex["nav"] == pytest.approx(1000.0)
    h_ex = p_ex["holdings"][TARGET]
    assert h_ex["quantity"] == pytest.approx(100.0)
    assert h_ex["close"] is None
    assert h_ex["valuation_price"] == pytest.approx(9.80)
    assert h_ex["market_value"] == pytest.approx(980.0)
    assert h_ex["valuation_basis"] == "carried_forward_prior_close"
    cf_ex = h_ex["carry_forward"]
    assert cf_ex["source_close_date"] == buy_day
    assert cf_ex["source_close_price"] == pytest.approx(10.0)
    assert cf_ex["valuation_price"] == pytest.approx(9.80)
    assert cf_ex["carried_sessions"] == 1
    assert len(cf_ex["adjustments"]) == 1
    assert cf_ex["adjustments"][0]["date"] == ex_day
    assert cf_ex["adjustments"][0]["type"] == "cash_dividend"
    assert cf_ex["adjustments"][0]["cash_per_share"] == pytest.approx(0.20)
    assert cf_ex["adjustments"][0]["pay_date"] == pay_day
    assert cf_ex["adjustments"][0]["valuation_price_before"] == pytest.approx(10.0)
    assert cf_ex["adjustments"][0]["valuation_price_after"] == pytest.approx(9.80)

    # Day 5 (still suspended): adjustment applied only once; MV 980, rec 20, NAV 1000.
    p_mid = curve[mid_susp_day]
    assert p_mid["cash"] == pytest.approx(0.0)
    assert p_mid["dividend_receivables"] == pytest.approx(20.0)
    assert p_mid["market_value"] == pytest.approx(980.0)
    assert p_mid["nav"] == pytest.approx(1000.0)
    assert p_mid["holdings"][TARGET]["valuation_price"] == pytest.approx(9.80)
    assert p_mid["holdings"][TARGET]["carry_forward"]["carried_sessions"] == 2
    assert len(p_mid["holdings"][TARGET]["carry_forward"]["adjustments"]) == 1

    # Day 6 (payment date while still suspended): receivable -> cash, NAV stays 1000!
    p_pay = curve[pay_day]
    assert p_pay["valuation_basis"] == "carried_forward"
    assert p_pay["cash"] == pytest.approx(20.0)
    assert p_pay["dividend_receivables"] == pytest.approx(0.0)
    assert p_pay["receivables"] == []
    assert p_pay["market_value"] == pytest.approx(980.0)
    assert p_pay["nav"] == pytest.approx(1000.0)
    assert p_pay["holdings"][TARGET]["valuation_price"] == pytest.approx(9.80)
    assert p_pay["holdings"][TARGET]["carry_forward"]["carried_sessions"] == 3
    assert len(p_pay["holdings"][TARGET]["carry_forward"]["adjustments"]) == 1

    # Day 7 (resumes with actual unadjusted close 9.50): MV = 100 * 9.50 = 950, cash = 20, NAV = 970.
    p_res = curve[resume_day]
    assert p_res["valuation_basis"] == "actual_close"
    assert p_res["cash"] == pytest.approx(20.0)
    assert p_res["dividend_receivables"] == pytest.approx(0.0)
    assert p_res["market_value"] == pytest.approx(950.0)
    assert p_res["nav"] == pytest.approx(970.0)
    assert p_res["holdings"][TARGET]["close"] == pytest.approx(9.50)
    assert p_res["holdings"][TARGET]["valuation_price"] == pytest.approx(9.50)
    assert "carry_forward" not in p_res["holdings"][TARGET]

    assert disc["metrics"]["valuation_gaps"] == 0
    assert disc["metrics"]["valued_sessions"] == 9
    assert disc["metrics"]["max_drawdown"] == pytest.approx(-0.03)

    # Benchmark (equal_weight: 20 shares of each ETF @ 10.0) reconciles its own holdings!
    bm_curve = {p["date"]: p for p in disc["baseline_comparison"]["baseline"]["equity_curve"]}
    bm_ex = bm_curve[ex_day]
    assert bm_ex["holdings"][TARGET]["quantity"] == pytest.approx(20.0)
    assert bm_ex["holdings"][TARGET]["valuation_price"] == pytest.approx(9.80)
    assert bm_ex["holdings"][TARGET]["market_value"] == pytest.approx(196.0)
    assert bm_ex["dividend_receivables"] == pytest.approx(4.0)
    assert bm_ex["market_value"] == pytest.approx(996.0)
    assert bm_ex["nav"] == pytest.approx(1000.0)
    bm_pay = bm_curve[pay_day]
    assert bm_pay["cash"] == pytest.approx(4.0)
    assert bm_pay["dividend_receivables"] == pytest.approx(0.0)
    assert bm_pay["nav"] == pytest.approx(1000.0)
    bm_res = bm_curve[resume_day]
    assert bm_res["holdings"][TARGET]["market_value"] == pytest.approx(190.0)
    assert bm_res["cash"] == pytest.approx(4.0)
    assert bm_res["nav"] == pytest.approx(994.0)
    assert len(SYMBOLS) == 5


def test_share_split_and_multi_day_sequential_actions_with_resumption(tmp_path: Path) -> None:
    """AC2: 1-for-2 split -> 200 shares @ 5.0 (MV unchanged); sequential days preserve trail."""
    # Part 1: Standalone 1-for-2 split during suspension (100 @ 10 -> 200 @ 5, MV 1000).
    p1 = schema_v3_fixture(15)
    set_constant_prices(p1, raw_price=10.0)
    buy_day = day_at(3)
    split_day = day_at(4)
    resume_day = day_at(5)
    make_confirmed_suspension(p1, split_day, TARGET)
    for idx in range(5, 9):
        raw_row(p1, day_at(idx), TARGET).update(open=5.0, high=5.0, low=5.0, close=5.0)
    p1["corporate_actions"] = [
        _action(
            date=split_day,
            symbol=TARGET,
            action_type="share_adjustment",
            available_at=f"{buy_day}T15:05:00+08:00",
            ratio=2.0,
        ),
    ]
    s1 = seal_v3(tmp_path, p1, store="store-split", name="split")
    proj1, camp1 = init_campaign(
        tmp_path, s1["store"], s1["snapshot_id"], project_name="split-proj",
    )
    rec1 = submit_strategy(tmp_path, proj1, camp1["campaign_id"], "split-sub")
    disc1 = rec1["discovery"]
    c1 = {p["date"]: p for p in disc1["equity_curve"]}

    h_split = c1[split_day]["holdings"][TARGET]
    assert h_split["quantity"] == pytest.approx(200.0)
    assert h_split["close"] is None
    assert h_split["valuation_price"] == pytest.approx(5.0)
    assert h_split["market_value"] == pytest.approx(1000.0)
    assert c1[split_day]["nav"] == pytest.approx(1000.0)
    cf1 = h_split["carry_forward"]
    assert cf1["source_close_date"] == buy_day
    assert cf1["source_close_price"] == pytest.approx(10.0)
    assert cf1["valuation_price"] == pytest.approx(5.0)
    assert cf1["carried_sessions"] == 1
    assert len(cf1["adjustments"]) == 1
    assert cf1["adjustments"][0]["type"] == "share_adjustment"
    assert cf1["adjustments"][0]["ratio"] == pytest.approx(2.0)
    assert cf1["adjustments"][0]["valuation_price_before"] == pytest.approx(10.0)
    assert cf1["adjustments"][0]["valuation_price_after"] == pytest.approx(5.0)

    # Benchmark also splits 20 -> 40 @ 5.0 (MV 200, NAV 1000).
    bm1 = {p["date"]: p for p in disc1["baseline_comparison"]["baseline"]["equity_curve"]}
    assert bm1[split_day]["holdings"][TARGET]["quantity"] == pytest.approx(40.0)
    assert bm1[split_day]["holdings"][TARGET]["valuation_price"] == pytest.approx(5.0)
    assert bm1[split_day]["holdings"][TARGET]["market_value"] == pytest.approx(200.0)
    assert bm1[split_day]["nav"] == pytest.approx(1000.0)

    # Resumption @ 5.0 keeps 200 shares and MV 1000 without double-splitting.
    assert c1[resume_day]["holdings"][TARGET]["quantity"] == pytest.approx(200.0)
    assert c1[resume_day]["holdings"][TARGET]["valuation_price"] == pytest.approx(5.0)
    assert c1[resume_day]["holdings"][TARGET]["valuation_basis"] == "actual_close"
    assert c1[resume_day]["nav"] == pytest.approx(1000.0)

    # Part 2: Multi-day sequential actions across continuous suspension + second suspension:
    # Day 4 (susp): cash_dividend 0.20 (pay_date=Day 6) -> 100 @ 9.80, rec 20, NAV 1000
    # Day 5 (susp): share_adjustment 2.0 -> 200 @ 4.90, rec 20, NAV 1000
    # Day 6 (susp): payment -> cash 20, rec 0, 200 @ 4.90, NAV 1000
    # Day 7 (resumed @ 5.10): 200 * 5.10 + 20 = 1040 (actual_close)
    # Day 8 (suspended again!): carries 5.10 with empty adjustments (prior events NOT re-applied!)
    p2 = schema_v3_fixture(15)
    set_constant_prices(p2, raw_price=10.0)
    ex_day, split2_day, pay_day, res_day, susp2_day = (
        day_at(4), day_at(5), day_at(6), day_at(7), day_at(8),
    )
    for d in (ex_day, split2_day, pay_day, susp2_day):
        make_confirmed_suspension(p2, d, TARGET)
    raw_row(p2, res_day, TARGET).update(open=5.1, high=5.1, low=5.1, close=5.1)
    p2["corporate_actions"] = [
        _action(
            date=ex_day,
            symbol=TARGET,
            action_type="cash_dividend",
            available_at=f"{buy_day}T15:05:00+08:00",
            cash_per_share=0.20,
            pay_date=pay_day,
        ),
        _action(
            date=split2_day,
            symbol=TARGET,
            action_type="share_adjustment",
            available_at=f"{ex_day}T15:05:00+08:00",
            ratio=2.0,
        ),
    ]
    s2 = seal_v3(tmp_path, p2, store="store-seq-days", name="seq-days")
    proj2, camp2 = init_campaign(
        tmp_path, s2["store"], s2["snapshot_id"], project_name="seq-days-proj",
    )
    rec2 = submit_strategy(tmp_path, proj2, camp2["campaign_id"], "seq-days-sub")
    disc2 = rec2["discovery"]
    c2 = {p["date"]: p for p in disc2["equity_curve"]}

    # Day 5: both events in trail, original source close preserved!
    h5 = c2[split2_day]["holdings"][TARGET]
    assert h5["quantity"] == pytest.approx(200.0)
    assert h5["valuation_price"] == pytest.approx(4.90)
    assert h5["market_value"] == pytest.approx(980.0)
    assert c2[split2_day]["dividend_receivables"] == pytest.approx(20.0)
    assert c2[split2_day]["nav"] == pytest.approx(1000.0)
    cf5 = h5["carry_forward"]
    assert cf5["source_close_date"] == buy_day
    assert cf5["source_close_price"] == pytest.approx(10.0)
    assert cf5["carried_sessions"] == 2
    assert [a["type"] for a in cf5["adjustments"]] == ["cash_dividend", "share_adjustment"]
    assert cf5["adjustments"][0]["valuation_price_after"] == pytest.approx(9.80)
    assert cf5["adjustments"][1]["valuation_price_before"] == pytest.approx(9.80)
    assert cf5["adjustments"][1]["valuation_price_after"] == pytest.approx(4.90)

    # Day 6: payment date -> cash 20, rec 0, adjustments still length 2.
    assert c2[pay_day]["cash"] == pytest.approx(20.0)
    assert c2[pay_day]["dividend_receivables"] == pytest.approx(0.0)
    assert c2[pay_day]["nav"] == pytest.approx(1000.0)
    assert len(c2[pay_day]["holdings"][TARGET]["carry_forward"]["adjustments"]) == 2

    # Day 7: resumption @ 5.10 -> 200 * 5.10 + 20 = 1040.
    assert c2[res_day]["valuation_basis"] == "actual_close"
    assert c2[res_day]["market_value"] == pytest.approx(1020.0)
    assert c2[res_day]["cash"] == pytest.approx(20.0)
    assert c2[res_day]["nav"] == pytest.approx(1040.0)

    # Day 8: suspended again -> carries Day 7's 5.10 with empty adjustments!
    cf8 = c2[susp2_day]["holdings"][TARGET]["carry_forward"]
    assert cf8["source_close_date"] == res_day
    assert cf8["source_close_price"] == pytest.approx(5.10)
    assert cf8["valuation_price"] == pytest.approx(5.10)
    assert cf8["carried_sessions"] == 1
    assert cf8["adjustments"] == []
    assert c2[susp2_day]["nav"] == pytest.approx(1040.0)
    assert disc2["valuation_summary"]["ending_carried_forward"][TARGET] == cf8


@pytest.mark.parametrize("bad_seq", [0, -1, 1.5, "1", True, False, None])
def test_snapshot_seal_rejects_invalid_and_duplicate_corporate_action_sequence(
    tmp_path: Path, bad_seq: object,
) -> None:
    """Snapshot sealing rejects invalid sequence values and duplicate same-(date,symbol) sequence."""
    payload = schema_v3_fixture(15)
    set_constant_prices(payload, raw_price=10.0)
    action = _action(
        date=day_at(4),
        symbol=TARGET,
        action_type="share_adjustment",
        available_at=f"{day_at(3)}T15:05:00+08:00",
        ratio=2.0,
    )
    action["sequence"] = bad_seq
    payload["corporate_actions"] = [action]
    source = write_json(tmp_path / "bad-seq.json", payload)
    res = cli("snapshot", "seal", "--input", str(source), "--store", str(tmp_path / "store"))
    assert res.returncode != 0
    assert "sequence" in res.stderr.lower()


def test_snapshot_seal_rejects_duplicate_sequence_same_date_symbol(tmp_path: Path) -> None:
    payload = schema_v3_fixture(15)
    set_constant_prices(payload, raw_price=10.0)
    payload["corporate_actions"] = [
        _action(
            date=day_at(4),
            symbol=TARGET,
            action_type="cash_dividend",
            available_at=f"{day_at(3)}T15:05:00+08:00",
            cash_per_share=0.20,
            pay_date=day_at(6),
            sequence=1,
        ),
        _action(
            date=day_at(4),
            symbol=TARGET,
            action_type="share_adjustment",
            available_at=f"{day_at(3)}T15:05:00+08:00",
            ratio=2.0,
            sequence=1,
        ),
    ]
    source = write_json(tmp_path / "dup-seq.json", payload)
    res = cli("snapshot", "seal", "--input", str(source), "--store", str(tmp_path / "store"))
    assert res.returncode != 0
    assert "duplicate" in res.stderr.lower() and "sequence" in res.stderr.lower()


def test_same_day_sequenced_actions_follow_evidenced_order_and_unsequenced_fails_closed(
    tmp_path: Path,
) -> None:
    """AC2+AC3: same-day seq 1->2 applies exact order (div->split vs split->div); missing seq fails."""
    buy_day = day_at(3)
    susp_day = day_at(4)
    pay_day = day_at(5)
    resume_day = day_at(6)

    def _run_same_day(
        name: str, div_seq: int | None, split_seq: int | None,
    ) -> tuple[dict, dict]:
        payload = schema_v3_fixture(15)
        set_constant_prices(payload, raw_price=10.0)
        make_confirmed_suspension(payload, susp_day, TARGET)
        make_confirmed_suspension(payload, pay_day, TARGET)
        for idx in range(6, 9):
            raw_row(payload, day_at(idx), TARGET).update(open=5.0, high=5.0, low=5.0, close=5.0)
        # Deliberately put share_adjustment BEFORE cash_dividend in raw input list
        # to prove sequence (not input list or JSON byte order) controls application!
        payload["corporate_actions"] = [
            _action(
                date=susp_day,
                symbol=TARGET,
                action_type="share_adjustment",
                available_at=f"{buy_day}T15:05:00+08:00",
                ratio=2.0,
                sequence=split_seq,
            ),
            _action(
                date=susp_day,
                symbol=TARGET,
                action_type="cash_dividend",
                available_at=f"{buy_day}T15:05:00+08:00",
                cash_per_share=0.20,
                pay_date=pay_day,
                sequence=div_seq,
            ),
        ]
        sealed = seal_v3(tmp_path, payload, store=f"store-{name}", name=name)
        proj, camp = init_campaign(
            tmp_path, sealed["store"], sealed["snapshot_id"], project_name=f"proj-{name}",
        )
        rec = submit_strategy(tmp_path, proj, camp["campaign_id"], f"sub-{name}")
        read = ok(
            cli(
                "snapshot", "read",
                "--store", str(sealed["store"]),
                "--id", sealed["snapshot_id"],
                "--partition", "discovery",
            )
        )
        return rec["discovery"], read["data"]

    # Order A: seq=1 cash_dividend (0.20), seq=2 share_adjustment (2.0)
    # 100 @ 10 -> div 20.0, price 9.80 -> split 200 @ 4.90, MV 980, rec 20, NAV 1000.
    disc_a, data_a = _run_same_day("order-div-split", div_seq=1, split_seq=2)
    assert [a["sequence"] for a in data_a["corporate_actions"]] == [1, 2]
    assert [a["type"] for a in data_a["corporate_actions"]] == [
        "cash_dividend", "share_adjustment",
    ]
    ca = {p["date"]: p for p in disc_a["equity_curve"]}
    assert ca[susp_day]["holdings"][TARGET]["quantity"] == pytest.approx(200.0)
    assert ca[susp_day]["holdings"][TARGET]["valuation_price"] == pytest.approx(4.90)
    assert ca[susp_day]["holdings"][TARGET]["market_value"] == pytest.approx(980.0)
    assert ca[susp_day]["dividend_receivables"] == pytest.approx(20.0)
    assert ca[susp_day]["nav"] == pytest.approx(1000.0)
    adj_a = ca[susp_day]["holdings"][TARGET]["carry_forward"]["adjustments"]
    assert [(x["sequence"], x["type"]) for x in adj_a] == [
        (1, "cash_dividend"), (2, "share_adjustment"),
    ]
    assert ca[pay_day]["cash"] == pytest.approx(20.0)
    assert ca[pay_day]["dividend_receivables"] == pytest.approx(0.0)
    assert ca[pay_day]["nav"] == pytest.approx(1000.0)
    assert ca[resume_day]["nav"] == pytest.approx(1020.0)  # 200 * 5.0 + 20 cash

    # Order B: seq=1 share_adjustment (2.0), seq=2 cash_dividend (0.20)
    # 100 @ 10 -> split 200 @ 5.0 -> div 200*0.20=40.0, price 4.80, MV 960, rec 40, NAV 1000.
    disc_b, data_b = _run_same_day("order-split-div", div_seq=2, split_seq=1)
    assert [a["type"] for a in data_b["corporate_actions"]] == [
        "share_adjustment", "cash_dividend",
    ]
    cb = {p["date"]: p for p in disc_b["equity_curve"]}
    assert cb[susp_day]["holdings"][TARGET]["quantity"] == pytest.approx(200.0)
    assert cb[susp_day]["holdings"][TARGET]["valuation_price"] == pytest.approx(4.80)
    assert cb[susp_day]["holdings"][TARGET]["market_value"] == pytest.approx(960.0)
    assert cb[susp_day]["dividend_receivables"] == pytest.approx(40.0)
    assert cb[susp_day]["nav"] == pytest.approx(1000.0)
    adj_b = cb[susp_day]["holdings"][TARGET]["carry_forward"]["adjustments"]
    assert [(x["sequence"], x["type"]) for x in adj_b] == [
        (1, "share_adjustment"), (2, "cash_dividend"),
    ]
    assert cb[pay_day]["cash"] == pytest.approx(40.0)
    assert cb[pay_day]["dividend_receivables"] == pytest.approx(0.0)
    assert cb[pay_day]["nav"] == pytest.approx(1000.0)
    assert cb[resume_day]["nav"] == pytest.approx(1040.0)  # 200 * 5.0 + 40 cash

    # Ambiguous order: neither action has sequence (and partial sequence: only one has sequence).
    for case_name, d_seq, s_seq in (
        ("ambig-none", None, None),
        ("ambig-partial", 1, None),
    ):
        disc_amb, _ = _run_same_day(case_name, div_seq=d_seq, split_seq=s_seq)
        camb = {p["date"]: p for p in disc_amb["equity_curve"]}
        assert camb[susp_day]["nav"] is None
        assert camb[susp_day]["valuation_basis"] == "missing"
        assert (
            camb[susp_day]["holdings"][TARGET]["gap_reason"]
            == "corporate_action_order_ambiguous"
        )
        # No partial mutation of shares or receivables!
        assert camb[susp_day]["holdings"][TARGET]["quantity"] == pytest.approx(100.0)
        assert camb[susp_day]["receivables"] == []
        assert disc_amb["metrics"]["max_drawdown"] is None
        assert any(
            g["symbol"] == TARGET and g["reason"] == "corporate_action_order_ambiguous"
            for g in disc_amb["gaps"]["valuation"]
        )


@pytest.mark.parametrize(
    ("case", "action_kwargs", "expected_reason"),
    [
        (
            "missing-cash-per-share",
            {"action_type": "cash_dividend", "cash_per_share": None, "pay_date": "2020-01-10"},
            "cash_dividend_terms_unknown",
        ),
        (
            "missing-pay-date",
            {"action_type": "cash_dividend", "cash_per_share": 0.20, "pay_date": None},
            "cash_dividend_terms_unknown",
        ),
        (
            "missing-terms-evidence",
            {
                "action_type": "cash_dividend",
                "cash_per_share": 0.20,
                "pay_date": "2020-01-10",
                "terms_evidence": None,
            },
            "corporate_action_terms_or_availability_unknown",
        ),
        (
            "missing-ratio",
            {"action_type": "share_adjustment", "ratio": None},
            "share_adjustment_ratio_unknown",
        ),
    ],
)
def test_insufficient_synthetic_corporate_action_terms_fail_closed(
    tmp_path: Path, case: str, action_kwargs: dict, expected_reason: str,
) -> None:
    """AC3: null/unknown terms on a suspended holding fail closed with typed gap reasons."""
    payload = full_history(15)
    set_constant_prices(payload, raw_price=10.0)
    days = [row["date"] for row in payload["calendar"]]
    susp_day = days[4]
    raw_row(payload, susp_day, TARGET).update(
        open=None, high=None, low=None, close=None, volume=None, trading_status="suspended",
    )
    payload["corporate_actions"] = [
        _action(
            date=susp_day,
            symbol=TARGET,
            available_at=f"{days[3]}T15:05:00+08:00",
            **action_kwargs,
        ),
    ]
    source = write_json(tmp_path / f"{case}.json", payload)
    store = tmp_path / f"store-{case}"
    sealed = ok(cli("snapshot", "seal", "--input", str(source), "--store", str(store)))
    proj, camp = init_campaign(
        tmp_path,
        store,
        sealed["snapshot_id"],
        execution_rules="next-open-v1",
        valuation_policy="carry-forward-suspended-v1",
        project_name=f"proj-{case}",
    )
    rec = submit_strategy(tmp_path, proj, camp["campaign_id"], f"sub-{case}")
    disc = rec["discovery"]
    curve = {p["date"]: p for p in disc["equity_curve"]}

    assert curve[susp_day]["nav"] is None
    assert curve[susp_day]["valuation_basis"] == "missing"
    assert curve[susp_day]["holdings"][TARGET]["gap_reason"] == expected_reason
    assert disc["metrics"]["max_drawdown"] is None
    assert any(
        g["symbol"] == TARGET and g["reason"] == expected_reason
        for g in disc["gaps"]["valuation"]
    )


def test_late_timing_bad_pay_date_atomic_batch_and_non_positive_price_fail_closed(
    tmp_path: Path,
) -> None:
    """AC3: late notice, pay_date < ex_date, partial-invalid batch, and price <= 0 fail closed."""
    buy_day = day_at(3)
    susp_day = day_at(4)
    susp_day_2 = day_at(5)
    resume_day = day_at(6)

    # 1. pay_date precedes ex_date -> fails closed with typed reason.
    p_bad_pay = schema_v3_fixture(15)
    set_constant_prices(p_bad_pay, raw_price=10.0)
    make_confirmed_suspension(p_bad_pay, susp_day, TARGET)
    p_bad_pay["corporate_actions"] = [
        _action(
            date=susp_day,
            symbol=TARGET,
            action_type="cash_dividend",
            available_at=f"{buy_day}T15:05:00+08:00",
            cash_per_share=0.20,
            pay_date=buy_day,  # precedes ex-date!
        ),
    ]
    s_bad_pay = seal_v3(tmp_path, p_bad_pay, store="store-bad-pay", name="bad-pay")
    proj_bp, camp_bp = init_campaign(
        tmp_path, s_bad_pay["store"], s_bad_pay["snapshot_id"], project_name="proj-bp",
    )
    rec_bp = submit_strategy(tmp_path, proj_bp, camp_bp["campaign_id"], "sub-bp")
    c_bp = {p["date"]: p for p in rec_bp["discovery"]["equity_curve"]}
    assert c_bp[susp_day]["nav"] is None
    assert (
        c_bp[susp_day]["holdings"][TARGET]["gap_reason"]
        == "cash_dividend_pay_date_precedes_ex_date"
    )
    assert c_bp[susp_day]["receivables"] == []

    # 2. Same-day sequenced batch where seq=1 is valid and seq=2 is late (09:31):
    #    atomic rejection — seq=1 MUST NOT mutate shares or receivables!
    p_atomic = schema_v3_fixture(15)
    set_constant_prices(p_atomic, raw_price=10.0)
    make_confirmed_suspension(p_atomic, susp_day, TARGET)
    p_atomic["corporate_actions"] = [
        _action(
            date=susp_day,
            symbol=TARGET,
            action_type="share_adjustment",
            available_at=f"{buy_day}T15:05:00+08:00",
            ratio=2.0,
            sequence=1,
        ),
        _action(
            date=susp_day,
            symbol=TARGET,
            action_type="cash_dividend",
            available_at=f"{susp_day}T09:31:00+08:00",
            cash_per_share=0.20,
            pay_date=day_at(6),
            sequence=2,
        ),
    ]
    s_at = seal_v3(tmp_path, p_atomic, store="store-atomic", name="atomic")
    proj_at, camp_at = init_campaign(
        tmp_path, s_at["store"], s_at["snapshot_id"], project_name="proj-at",
    )
    rec_at = submit_strategy(tmp_path, proj_at, camp_at["campaign_id"], "sub-at")
    c_at = {p["date"]: p for p in rec_at["discovery"]["equity_curve"]}
    assert c_at[susp_day]["nav"] is None
    assert (
        c_at[susp_day]["holdings"][TARGET]["gap_reason"]
        == "corporate_action_terms_or_availability_unknown"
    )
    assert c_at[susp_day]["holdings"][TARGET]["quantity"] == pytest.approx(100.0)
    assert c_at[susp_day]["receivables"] == []

    # 3. Non-positive adjusted carried price (10.0 - 10.0 == 0.0 <= 0):
    #    Day 4 fails with carried_valuation_price_non_positive,
    #    Day 5 (still suspended) fails with carry_forward_chain_broken,
    #    Day 6 (resumed @ 0.50 real close) recovers actual close valuation!
    p_nonpos = schema_v3_fixture(15)
    set_constant_prices(p_nonpos, raw_price=10.0)
    make_confirmed_suspension(p_nonpos, susp_day, TARGET)
    make_confirmed_suspension(p_nonpos, susp_day_2, TARGET)
    for idx in range(6, 9):
        raw_row(p_nonpos, day_at(idx), TARGET).update(open=0.5, high=0.5, low=0.5, close=0.5)
    p_nonpos["corporate_actions"] = [
        _action(
            date=susp_day,
            symbol=TARGET,
            action_type="cash_dividend",
            available_at=f"{buy_day}T15:05:00+08:00",
            cash_per_share=10.0,
            pay_date=day_at(7),
        ),
    ]
    s_np = seal_v3(tmp_path, p_nonpos, store="store-nonpos", name="nonpos")
    proj_np, camp_np = init_campaign(
        tmp_path, s_np["store"], s_np["snapshot_id"], project_name="proj-np",
    )
    rec_np = submit_strategy(tmp_path, proj_np, camp_np["campaign_id"], "sub-np")
    disc_np = rec_np["discovery"]
    c_np = {p["date"]: p for p in disc_np["equity_curve"]}
    assert c_np[susp_day]["nav"] is None
    assert (
        c_np[susp_day]["holdings"][TARGET]["gap_reason"]
        == "carried_valuation_price_non_positive"
    )
    assert c_np[susp_day_2]["nav"] is None
    assert c_np[susp_day_2]["holdings"][TARGET]["gap_reason"] == "carry_forward_chain_broken"
    assert disc_np["metrics"]["max_drawdown"] is None
    # Resumption on day 6 recovers: 100 * 0.50 + 1000.0 receivable = 1050.0.
    assert c_np[resume_day]["valuation_basis"] == "actual_close"
    assert c_np[resume_day]["market_value"] == pytest.approx(50.0)
    assert c_np[resume_day]["dividend_receivables"] == pytest.approx(1000.0)
    assert c_np[resume_day]["nav"] == pytest.approx(1050.0)


def test_account_isolation_replay_idempotency_and_strict_close_control(tmp_path: Path) -> None:
    """AC4: actions apply only to actual holdings; replay is idempotent; strict-close unchanged."""
    buy_day = day_at(3)
    susp_day = day_at(4)
    pay_day = day_at(5)
    resume_day = day_at(6)
    unheld = SYMBOLS[4]  # Candidate holds only TARGET (SYMBOLS[0]); benchmark holds all 5 ETFs!

    # Part 1: Valid dividend + split on unheld SYMBOLS[4] during suspension.
    p_iso = schema_v3_fixture(15)
    set_constant_prices(p_iso, raw_price=10.0)
    make_confirmed_suspension(p_iso, susp_day, unheld)
    make_confirmed_suspension(p_iso, pay_day, unheld)
    for idx in range(6, 9):
        raw_row(p_iso, day_at(idx), unheld).update(open=4.9, high=4.9, low=4.9, close=4.9)
    p_iso["corporate_actions"] = [
        _action(
            date=susp_day,
            symbol=unheld,
            action_type="cash_dividend",
            available_at=f"{buy_day}T15:05:00+08:00",
            cash_per_share=0.20,
            pay_date=pay_day,
            sequence=1,
        ),
        _action(
            date=susp_day,
            symbol=unheld,
            action_type="share_adjustment",
            available_at=f"{buy_day}T15:05:00+08:00",
            ratio=2.0,
            sequence=2,
        ),
    ]
    s_iso = seal_v3(tmp_path, p_iso, store="store-iso", name="iso")
    proj_iso, camp_iso = init_campaign(
        tmp_path, s_iso["store"], s_iso["snapshot_id"], project_name="proj-iso",
    )
    rec_iso = submit_strategy(tmp_path, proj_iso, camp_iso["campaign_id"], "sub-iso")
    cand_iso = rec_iso["discovery"]
    bm_iso = cand_iso["baseline_comparison"]["baseline"]

    # Candidate holds 0 of unheld -> zero receivables, zero cash change, zero share change!
    cand_curve = {p["date"]: p for p in cand_iso["equity_curve"]}
    assert all(p["valuation_basis"] == "actual_close" for p in cand_iso["equity_curve"])
    assert cand_curve[susp_day]["dividend_receivables"] == pytest.approx(0.0)
    assert cand_curve[pay_day]["cash"] == pytest.approx(0.0)
    assert cand_curve[susp_day]["holdings"][unheld]["quantity"] == pytest.approx(0.0)
    assert cand_curve[susp_day]["nav"] == pytest.approx(1000.0)

    # Benchmark holds 20 shares of unheld -> applies div (4.0) then split (40 @ 4.90)!
    bm_curve = {p["date"]: p for p in bm_iso["equity_curve"]}
    assert bm_curve[susp_day]["valuation_basis"] == "carried_forward"
    assert bm_curve[susp_day]["holdings"][unheld]["quantity"] == pytest.approx(40.0)
    assert bm_curve[susp_day]["holdings"][unheld]["valuation_price"] == pytest.approx(4.90)
    assert bm_curve[susp_day]["dividend_receivables"] == pytest.approx(4.0)
    assert bm_curve[susp_day]["nav"] == pytest.approx(1000.0)
    assert bm_curve[pay_day]["cash"] == pytest.approx(4.0)
    assert bm_curve[pay_day]["dividend_receivables"] == pytest.approx(0.0)
    assert bm_curve[pay_day]["nav"] == pytest.approx(1000.0)
    assert bm_curve[resume_day]["nav"] == pytest.approx(1000.0)  # 40*4.9 + 800 + 4

    # Part 2: Ambiguous unsequenced actions on unheld SYMBOLS[4]:
    # Candidate stays 100% clean; benchmark fails closed!
    p_amb_u = schema_v3_fixture(15)
    set_constant_prices(p_amb_u, raw_price=10.0)
    make_confirmed_suspension(p_amb_u, susp_day, unheld)
    p_amb_u["corporate_actions"] = [
        _action(
            date=susp_day,
            symbol=unheld,
            action_type="cash_dividend",
            available_at=f"{buy_day}T15:05:00+08:00",
            cash_per_share=0.20,
            pay_date=pay_day,
        ),
        _action(
            date=susp_day,
            symbol=unheld,
            action_type="share_adjustment",
            available_at=f"{buy_day}T15:05:00+08:00",
            ratio=2.0,
        ),
    ]
    s_au = seal_v3(tmp_path, p_amb_u, store="store-amb-u", name="amb-u")
    proj_au, camp_au = init_campaign(
        tmp_path, s_au["store"], s_au["snapshot_id"], project_name="proj-au",
    )
    rec_au = submit_strategy(tmp_path, proj_au, camp_au["campaign_id"], "sub-au")
    assert rec_au["discovery"]["metrics"]["valuation_gaps"] == 0
    assert rec_au["discovery"]["metrics"]["max_drawdown"] == pytest.approx(0.0)
    bm_au = rec_au["discovery"]["baseline_comparison"]["baseline"]
    assert bm_au["metrics"]["max_drawdown"] is None
    assert any(
        g["symbol"] == unheld and g["reason"] == "corporate_action_order_ambiguous"
        for g in bm_au["gaps"]["valuation"]
    )

    # Part 3: Replay idempotency & strict-close-v1 control on TARGET div+split+payment+resume.
    p_rep = schema_v3_fixture(15)
    set_constant_prices(p_rep, raw_price=10.0)
    make_confirmed_suspension(p_rep, susp_day, TARGET)
    make_confirmed_suspension(p_rep, pay_day, TARGET)
    for idx in range(6, 9):
        raw_row(p_rep, day_at(idx), TARGET).update(open=5.0, high=5.0, low=5.0, close=5.0)
    p_rep["corporate_actions"] = [
        _action(
            date=susp_day,
            symbol=TARGET,
            action_type="cash_dividend",
            available_at=f"{buy_day}T15:05:00+08:00",
            cash_per_share=0.20,
            pay_date=pay_day,
            sequence=1,
        ),
        _action(
            date=susp_day,
            symbol=TARGET,
            action_type="share_adjustment",
            available_at=f"{buy_day}T15:05:00+08:00",
            ratio=2.0,
            sequence=2,
        ),
    ]
    s_rep = seal_v3(tmp_path, p_rep, store="store-rep", name="rep")
    proj_rep, camp_rep = init_campaign(
        tmp_path, s_rep["store"], s_rep["snapshot_id"], project_name="proj-rep",
    )
    first = submit_strategy(tmp_path, proj_rep, camp_rep["campaign_id"], "replay-1")
    same_id_replay = submit_strategy(tmp_path, proj_rep, camp_rep["campaign_id"], "replay-1")
    second_fresh = submit_strategy(tmp_path, proj_rep, camp_rep["campaign_id"], "replay-2")
    status = ok(
        cli(
            "experiment", "status",
            "--project", str(proj_rep),
            "--campaign-id", camp_rep["campaign_id"],
            "--submission-id", "replay-1",
        )
    )
    assert same_id_replay["experiment_id"] == first["experiment_id"]
    assert same_id_replay["discovery"] == first["discovery"]
    assert same_id_replay["quarterly_summary"] == first["quarterly_summary"]
    assert status["discovery"] == first["discovery"]
    assert status["quarterly_summary"] == first["quarterly_summary"]
    assert second_fresh["experiment_id"] != first["experiment_id"]
    assert second_fresh["discovery"] == first["discovery"]
    assert second_fresh["quarterly_summary"]["quarters"] == first["quarterly_summary"]["quarters"]
    assert (
        second_fresh["quarterly_summary"]["daily_metrics"]
        == first["quarterly_summary"]["daily_metrics"]
    )

    # strict-close-v1 control on the exact same snapshot keeps legacy behavior!
    proj_strict, camp_strict = init_campaign(
        tmp_path,
        s_rep["store"],
        s_rep["snapshot_id"],
        valuation_policy="strict-close-v1",
        project_name="proj-strict",
    )
    strict_rec = submit_strategy(tmp_path, proj_strict, camp_strict["campaign_id"], "strict-1")
    strict_disc = strict_rec["discovery"]
    strict_curve = {p["date"]: p for p in strict_disc["equity_curve"]}
    assert strict_curve[susp_day]["nav"] is None
    assert strict_curve[pay_day]["nav"] is None
    assert "valuation_basis" not in strict_curve[susp_day]
    assert "valuation_summary" not in strict_disc
    assert strict_disc["metrics"]["max_drawdown"] is None
    assert strict_curve[resume_day]["nav"] == pytest.approx(1020.0)



