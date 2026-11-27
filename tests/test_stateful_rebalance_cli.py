"""Tests for Spec #74-E (#79): stateful rebalancing, intra-month deferral, and tracking recovery."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cli_helpers import cli, cli_env, ok, rejected, write_json
from test_actual_open_cli import OPEN_SOURCE, PRICE_EVIDENCE, STATUS_EVIDENCE, schema_v3_fixture
from test_formal_validation_cli import v2_round_input
from test_research_loop_cli import open_round, submit_version, version_input
from test_shadow_cli import shadow_update
from test_version_review_cli import approve_version, calendar_payload, observation_input

from prajnatrade.decision_window import (
    AFTER_CLOSE_COMMON_POLICY,
    build_strategy_state,
    compute_prior_close_weights,
    normalize_decision_policy,
    parse_strategy_output,
)
from prajnatrade.lot_execution import ACTUAL_OPEN_LOT_RULES
from prajnatrade.shadow import Ledger, ShadowRejection, verify_or_migrate_ledger
from prajnatrade.snapshot_baseline import run_partition_strategy
from prajnatrade.trading_status import CARRY_FORWARD_SUSPENDED_VALUATION, CASH, ETF_SYMBOLS


def test_build_strategy_state_and_prior_close_weights_contract() -> None:
    sym = ETF_SYMBOLS[0]
    sessions = ["2024-01-29", "2024-01-30", "2024-01-31", "2024-02-01", "2024-02-02", "2024-02-05"]
    shares = {symbol: (200.0 if symbol == sym else 0.0) for symbol in ETF_SYMBOLS}

    # Shadow shape ("positions")
    nav_shadow = {
        "date": "2024-02-01",
        "nav": 10000.0,
        "positions": {
            symbol: {"quantity": shares[symbol], "market_value": 2000.0 if symbol == sym else 0.0}
            for symbol in ETF_SYMBOLS
        },
    }
    prior_weights = compute_prior_close_weights(nav_shadow, shares, cash=8000.0, receivables=0.0)
    assert prior_weights is not None
    assert pytest.approx(prior_weights[sym], abs=1e-12) == 0.2
    assert pytest.approx(prior_weights[CASH], abs=1e-12) == 0.8

    # Discovery / Validation shape ("holdings", including carried-forward valuation)
    nav_discovery = {
        "date": "2024-02-01",
        "nav": 10000.0,
        "holdings": {
            symbol: {
                "quantity": shares[symbol],
                "close": None if symbol == sym else 10.0,
                "valuation_price": 9.8 if symbol == sym else 10.0,
                "market_value": 1960.0 if symbol == sym else 0.0,
            }
            for symbol in ETF_SYMBOLS
        },
    }
    prior_weights_cf = compute_prior_close_weights(
        nav_discovery, shares, cash=8000.0, receivables=40.0
    )
    assert prior_weights_cf is not None
    assert pytest.approx(prior_weights_cf[sym], abs=1e-12) == 0.196
    assert pytest.approx(prior_weights_cf[CASH], abs=1e-12) == 0.804

    # Missing NAV or missing held market_value -> prior_close_weights is None
    assert (
        compute_prior_close_weights(
            {"date": "2024-02-01", "nav": None, "positions": {}},
            shares,
            cash=8000.0,
            receivables=0.0,
        )
        is None
    )
    assert (
        compute_prior_close_weights(
            {
                "date": "2024-02-01",
                "nav": 10000.0,
                "holdings": {
                    symbol: {"quantity": shares[symbol], "market_value": None}
                    for symbol in ETF_SYMBOLS
                },
            },
            shares,
            cash=8000.0,
            receivables=0.0,
        )
        is None
    )

    state = build_strategy_state(
        signal_date="2024-02-01",
        sessions=sessions,
        cash=8000.0,
        shares=shares,
        equity=10000.0,
        prior_close_weights=prior_weights,
        history_windows={"lookback": ["2024-01-31", "2024-02-01"]},
    )
    assert state["cash"] == 8000.0
    assert state["shares"][sym] == 200.0
    assert state["equity"] == 10000.0
    assert state["signal_date"] == "2024-02-01"
    assert state["month_session"] == 1
    assert state["week_start"] is False
    assert state["rebalance_anchor_date"] is None
    assert state["previous_order_attempted"] is None
    assert state["last_filled_target"] is None
    assert state["last_monthly_filled_target"] is None
    assert state["prior_close_weights"] == prior_weights

    # Monday 2024-02-05 is week_start=True and month_session=3
    state_mon = build_strategy_state(
        signal_date="2024-02-05",
        sessions=sessions,
        cash=8000.0,
        shares=shares,
        equity=10000.0,
        prior_close_weights=prior_weights,
    )
    assert state_mon["month_session"] == 3
    assert state_mon["week_start"] is True

    policy = normalize_decision_policy(AFTER_CLOSE_COMMON_POLICY, monthly_deferral=True)
    assert policy["monthly_deferral"] is True

    parsed_plain = parse_strategy_output({sym: 1.0}, signal_date="2024-02-01")
    assert parsed_plain is not None
    assert parsed_plain["purpose"] == "rebalance"
    assert parsed_plain["weights"][sym] == 1.0

    parsed_struct = parse_strategy_output(
        {"weights": {sym: 0.5, CASH: 0.5}, "purpose": "monthly", "cycle": "2024-02"},
        signal_date="2024-02-01",
    )
    assert parsed_struct is not None
    assert parsed_struct["purpose"] == "monthly"
    assert parsed_struct["cycle"] == "2024-02"
    assert parsed_struct["weights"][sym] == 0.5
    assert parsed_struct["weights"][CASH] == 0.5


def _make_bars_and_derived(
    sessions: list[str],
    *,
    late_close_days: set[str] | None = None,
    suspended_days: dict[tuple[str, str], bool] | None = None,
) -> dict:
    sym_a, sym_b = ETF_SYMBOLS[0], ETF_SYMBOLS[1]
    late_days = late_close_days or set()
    susp = suspended_days or {}
    raw_bars: list[dict] = []
    derived: list[dict] = []
    for idx, day in enumerate(sessions):
        for symbol in ETF_SYMBOLS:
            if susp.get((day, symbol)):
                raw_bars.append(
                    {
                        "date": day,
                        "symbol": symbol,
                        "open": None,
                        "high": None,
                        "low": None,
                        "close": None,
                        "volume": 0.0,
                        "trading_status": "suspended",
                        "status_evidence_id": STATUS_EVIDENCE,
                        "available_at": f"{day}T15:00:00+08:00",
                        "close_available_at": f"{day}T15:00:00+08:00",
                        "availability_evidence": "fixture_synth",
                    }
                )
            else:
                close_ts = (
                    f"{day}T16:00:00+08:00"
                    if (day in late_days and symbol == sym_a)
                    else f"{day}T15:00:00+08:00"
                )
                raw_bars.append(
                    {
                        "date": day,
                        "symbol": symbol,
                        "open": 10.0,
                        "high": 10.0,
                        "low": 10.0,
                        "close": 10.0,
                        "volume": 100000.0,
                        "trading_status": "trading",
                        "available_at": close_ts,
                        "close_available_at": close_ts,
                        "availability_evidence": "fixture_synth",
                        "open_source": {"source": "synth", "evidence_id": f"ev-{day}-{symbol}"},
                    }
                )
            derived.append(
                {
                    "date": day,
                    "symbol": symbol,
                    "value": 10.0 + (0.1 * idx if symbol == sym_b else 0.0),
                    "available_at": f"{day}T15:00:00+08:00",
                    "availability_evidence": "fixture_synth",
                }
            )
    return {
        "calendar": [{"date": day} for day in sessions],
        "raw_bars": raw_bars,
        "derived": derived,
        "corporate_actions": [],
    }


def test_discovery_stateful_rebalance_anchors_zero_trade_and_deferral() -> None:
    sym_a, sym_b = ETF_SYMBOLS[0], ETF_SYMBOLS[1]
    sessions = [
        "2024-01-30",  # 0: initial signal -> executes 01-31
        "2024-01-31",  # 1: month-end monthly signal -> executes 02-01 (cross-month!)
        "2024-02-01",  # 2: month_session=1, PAUSED (late close) -> triggers monthly deferral
        "2024-02-02",  # 3: month_session=2, RECOVERED -> deferred monthly signal -> executes 02-05
        "2024-02-05",  # 4: zero-trade satisfied target (200 held vs 205 target @ lot=100); emits risk_control
        "2024-02-06",  # 5: settles risk_control -> updates last_filled_target, NOT last_monthly_filled_target
        "2024-02-07",  # 6: quiet day
        "2024-03-01",  # 7: month_session=1, PAUSED
        "2024-03-04",  # 8: month_session=2, PAUSED (whole month unrecovered -> not_executed)
        "2024-04-01",  # 9: final session
    ]
    data = _make_bars_and_derived(
        sessions,
        late_close_days={"2024-02-01", "2024-03-01", "2024-03-04"},
    )
    seen_states: dict[str, dict] = {}

    def stateful_strategy(history, state):
        seen_states[state["signal_date"]] = dict(state)
        d = state["signal_date"]
        if d == "2024-01-30":
            return {"weights": {sym_a: 0.20, CASH: 0.80}, "purpose": "initial", "cycle": "2024-01"}
        if d == "2024-01-31":
            return {"weights": {sym_a: 0.20, CASH: 0.80}, "purpose": "monthly", "cycle": "2024-01"}
        if d == "2024-02-02" and state.get("deferred_monthly"):
            # 205 shares target vs 200 held @ lot=100 -> 0 trade!
            return {"weights": {sym_a: 0.205, CASH: 0.795}, "purpose": "monthly", "cycle": "2024-02"}
        if d == "2024-02-05":
            return {"weights": {sym_b: 0.20, CASH: 0.80}, "purpose": "risk_control"}
        return None

    policy = normalize_decision_policy(AFTER_CLOSE_COMMON_POLICY, monthly_deferral=True)
    result = run_partition_strategy(
        data,
        "snap-test",
        "hash-test",
        stateful_strategy,
        partition_name="discovery",
        initial_cash=10000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
        signal_window_sessions=1,
        execution_rule_version=ACTUAL_OPEN_LOT_RULES,
        decision_policy=policy,
        history_requirements=[{"id": "lookback", "observations": 1, "mode": "valid_observations"}],
        lot_sizes={s: 100 for s in ETF_SYMBOLS},
        symbol_order=ETF_SYMBOLS,
    )

    # 1. All 8 state fields present on every call + prior_close_weights computed from holdings
    for d, st in seen_states.items():
        for key in (
            "signal_date",
            "month_session",
            "week_start",
            "rebalance_anchor_date",
            "previous_order_attempted",
            "last_filled_target",
            "last_monthly_filled_target",
            "prior_close_weights",
        ):
            assert key in st, (d, key)
        assert st["prior_close_weights"] is not None, d
    assert pytest.approx(seen_states["2024-01-30"]["prior_close_weights"][CASH], abs=1e-12) == 1.0
    assert pytest.approx(seen_states["2024-01-31"]["prior_close_weights"][sym_a], abs=1e-12) == 0.20
    assert pytest.approx(seen_states["2024-01-31"]["prior_close_weights"][CASH], abs=1e-12) == 0.80
    assert seen_states["2024-02-02"]["rebalance_anchor_date"] == "2024-01-31"

    # 2. Month-end signal 2024-01-31 executed on 2024-02-01 keeps cycle 2024-01
    sig_jan31 = next(s for s in result["signals"] if s["date"] == "2024-01-31")
    assert sig_jan31["execution_date"] == "2024-02-01"
    assert sig_jan31["cycle"] == "2024-01"
    assert sig_jan31["purpose"] == "monthly"

    # 3. Deferred monthly signal on 2024-02-02 records scheduled_date, actual_signal_date, deferral_reason
    sig_feb02 = next(s for s in result["signals"] if s["date"] == "2024-02-02")
    assert sig_feb02["scheduled_date"] == "2024-02-01"
    assert sig_feb02["actual_signal_date"] == "2024-02-02"
    assert sig_feb02["deferral_reason"]

    # Zero-trade satisfied target on 2024-02-05 open
    att_feb05 = next(a for a in result["order_attempts"] if a["date"] == "2024-02-05")
    assert att_feb05["batch_complete"] is True
    assert att_feb05["zero_trade_satisfied"] is True

    # 4. Risk-control updates last_filled_target without altering last_monthly_filled_target
    final_state = result["strategy_state"]
    assert final_state["rebalance_anchor_date"] == "2024-02-05"
    assert pytest.approx(final_state["last_filled_target"][sym_b], abs=1e-12) == 0.20
    assert pytest.approx(final_state["last_monthly_filled_target"][sym_a], abs=1e-12) == 0.205

    # 5. March whole-month unrecovered -> not_executed
    mar_def = next(d for d in result["monthly_deferrals"] if d["cycle"] == "2024-03")
    assert mar_def["scheduled_date"] == "2024-03-01"
    assert mar_def["actual_signal_date"] is None
    assert mar_def["status"] == "not_executed"


def test_anchor_not_advanced_on_cash_constrained_suspended_partial_and_no_auto_resend() -> None:
    sym_a, sym_b = ETF_SYMBOLS[0], ETF_SYMBOLS[1]
    sessions = [
        "2024-01-30",  # 0: initial build sym_a=20% -> fills 01-31 (anchor = 2024-01-30)
        "2024-01-31",  # 1: cash-constrained fixed_amounts=20000 (> 8000 cash) -> must NOT advance anchor!
        "2024-02-01",  # 2: settles cash_constrained; emits monthly targeting sym_a+sym_b
        "2024-02-02",  # 3: sym_b SUSPENDED on open -> partial fill -> must NOT advance anchor! Also paused on close!
        "2024-02-05",  # 4: recovered -> already-submitted monthly order must NOT trigger deferred_monthly!
        "2024-02-06",  # 5: final session
    ]
    data = _make_bars_and_derived(
        sessions,
        late_close_days={"2024-02-02"},
        suspended_days={("2024-02-02", sym_b): True},
    )
    seen_states: dict[str, dict] = {}

    def strategy(history, state):
        seen_states[state["signal_date"]] = dict(state)
        d = state["signal_date"]
        if d == "2024-01-30":
            return {"weights": {sym_a: 0.20, CASH: 0.80}, "purpose": "initial", "cycle": "2024-01"}
        if d == "2024-01-31":
            # Demand 20,000 > remaining cash 8,000 -> cash_constrained=True, batch_complete=False
            return {
                "weights": {sym_a: 0.20, sym_b: 0.50, CASH: 0.30},
                "purpose": "rebalance",
                "fixed_amounts": {sym_b: 20000.0},
            }
        if d == "2024-02-01":
            # Monthly signal submitted cleanly; on 2024-02-02 open sym_b is suspended -> partial fill
            return {"weights": {sym_a: 0.30, sym_b: 0.30, CASH: 0.40}, "purpose": "monthly", "cycle": "2024-02"}
        return None

    policy = normalize_decision_policy(AFTER_CLOSE_COMMON_POLICY, monthly_deferral=True)
    result = run_partition_strategy(
        data,
        "snap-neg",
        "hash-neg",
        strategy,
        partition_name="discovery",
        initial_cash=10000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
        signal_window_sessions=1,
        execution_rule_version=ACTUAL_OPEN_LOT_RULES,
        valuation_policy_version=CARRY_FORWARD_SUSPENDED_VALUATION,
        decision_policy=policy,
        history_requirements=[{"id": "lookback", "observations": 1, "mode": "valid_observations"}],
        lot_sizes={s: 100 for s in ETF_SYMBOLS},
        symbol_order=ETF_SYMBOLS,
    )

    # 1. Cash-constrained attempt on 2024-02-01 did NOT advance anchor past 2024-01-30
    att_feb01 = next(a for a in result["order_attempts"] if a["date"] == "2024-02-01")
    assert att_feb01["cash_constrained"] is True
    assert att_feb01["batch_complete"] is False
    assert seen_states["2024-02-01"]["rebalance_anchor_date"] == "2024-01-30"
    assert seen_states["2024-02-01"]["previous_order_attempted"]["cash_constrained"] is True

    # 2. Suspended/partial attempt on 2024-02-02 did NOT advance anchor
    att_feb02 = next(a for a in result["order_attempts"] if a["date"] == "2024-02-02")
    assert att_feb02["batch_complete"] is False
    assert any(u["symbol"] == sym_b for u in att_feb02["unfilled"])
    assert result["strategy_state"]["rebalance_anchor_date"] == "2024-01-30"

    # 3. Already-submitted partial monthly order is NEVER auto-resent under deferral
    assert seen_states["2024-02-05"]["deferred_monthly"] is False
    assert result["monthly_deferrals"] == []


def test_shadow_legacy_ledger_migration_reconstructible_and_ambiguous(tmp_path: Path) -> None:
    # 1. Uniquely reconstructible legacy ledger (no strategy_state in day results)
    ledger_ok = tmp_path / "ledger_ok"
    db_ok = Ledger(ledger_ok)
    sym_a = ETF_SYMBOLS[0]
    day1_res = {
        "schema_version": 1,
        "kind": "shadow_day",
        "version_id": "rel-v1",
        "date": "2024-01-30",
        "status": "committed",
        "orders": [],
        "fills": [],
        "cash": 10000.0,
        "positions": {s: {"quantity": 0.0, "market_value": 0.0} for s in ETF_SYMBOLS},
        "nav": 10000.0,
        "signal": {"date": "2024-01-30", "target_weights": {sym_a: 0.2, CASH: 0.8}, "purpose": "initial"},
        "next_orders": [{"order_id": "ord-1", "symbol": sym_a, "target_weight": 0.2, "deadline": "2024-01-31T09:30:00+08:00"}],
    }
    day2_res = {
        "schema_version": 1,
        "kind": "shadow_day",
        "version_id": "rel-v1",
        "date": "2024-01-31",
        "status": "committed",
        "orders": [
            {
                "order_id": "ord-1",
                "symbol": sym_a,
                "signal_date": "2024-01-30",
                "target_weight": 0.2,
                "status": "filled",
            }
        ],
        "fills": [{"symbol": sym_a, "side": "buy", "quantity": 200.0, "price": 10.0}],
        "cash": 8000.0,
        "positions": {s: {"quantity": 200.0 if s == sym_a else 0.0, "market_value": 2000.0 if s == sym_a else 0.0} for s in ETF_SYMBOLS},
        "nav": 10000.0,
        "signal": None,
        "next_orders": [],
    }
    db_ok.commit_day("rel-v1", "rel-v1", "2024-01-30", day1_res, None, "2024-01-30", "enabled", "2024-01-30T16:00:00+08:00")
    db_ok.commit_day("rel-v1", "rel-v1", "2024-01-31", day2_res, "2024-01-30", "2024-01-30", "enabled", "2024-01-31T16:00:00+08:00")
    db_ok.close()

    # Exercise public CLI `shadow migrate-state` for uniquely reconstructible ledger
    mig = ok(cli("shadow", "migrate-state", "--ledger", str(ledger_ok), "--version", "rel-v1"))
    assert mig["status"] == "reconstructed"
    assert mig["strategy_state"]["rebalance_anchor_date"] == "2024-01-30"
    assert pytest.approx(mig["strategy_state"]["last_filled_target"][sym_a], abs=1e-12) == 0.2

    # 2. Non-uniquely reconstructible legacy ledger (committed_with_anomalies)
    ledger_bad = tmp_path / "ledger_bad"
    db_bad = Ledger(ledger_bad)
    bad_res = {
        **day2_res,
        "status": "committed_with_anomalies",
        "orders": [{"order_id": "ord-x", "symbol": sym_a, "status": "unfilled"}],
    }
    db_bad.commit_day("rel-v1", "rel-v1", "2024-01-31", bad_res, None, "2024-01-31", "enabled", "2024-01-31T16:00:00+08:00")

    with pytest.raises(ShadowRejection) as exc_info:
        verify_or_migrate_ledger(ledger_bad, "rel-v1")
    assert exc_info.value.code == "STATE_NOT_UNIQUELY_RECONSTRUCTIBLE"
    # Public CLI also fails closed with STATE_NOT_UNIQUELY_RECONSTRUCTIBLE
    rejected(
        cli("shadow", "migrate-state", "--ledger", str(ledger_bad), "--version", "rel-v1"),
        "STATE_NOT_UNIQUELY_RECONSTRUCTIBLE",
    )
    # Old rows preserved intact
    assert db_bad.get_day_row("rel-v1", "2024-01-31") is not None

    # Explicit clean start point via public CLI preserves old rows and records clean_start
    clean = ok(
        cli(
            "shadow",
            "migrate-state",
            "--ledger",
            str(ledger_bad),
            "--version",
            "rel-v1",
            "--new-start-date",
            "2024-02-01",
            "--initial-cash",
            "15000",
            "--initial-positions",
            json.dumps({sym_a: 100.0}),
        )
    )
    assert clean["status"] == "clean_start"
    assert clean["new_start_date"] == "2024-02-01"
    assert clean["initial_cash"] == 15000.0
    assert clean["initial_positions"][sym_a] == 100.0
    assert db_bad.get_day_row("rel-v1", "2024-01-31") is not None
    db_bad.close()


def test_shadow_own_initial_account_warmup_and_restart_idempotency(tmp_path: Path) -> None:
    sym_a = ETF_SYMBOLS[0]
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir(parents=True)
    entry = plugin_dir / "state_check.py"
    entry.write_text(
        "from prajnatrade.trading_status import CASH, ETF_SYMBOLS\n"
        "SYM_A = ETF_SYMBOLS[0]\n"
        "def make_strategy(params):\n"
        "    def strategy(history, state):\n"
        "        for k in ('signal_date','month_session','week_start','rebalance_anchor_date',\n"
        "                  'previous_order_attempted','last_filled_target',\n"
        "                  'last_monthly_filled_target','prior_close_weights','cash','shares','equity'):\n"
        "            assert k in state, f'missing {k}'\n"
        "        assert state['prior_close_weights'] is not None\n"
        "        if state['rebalance_anchor_date'] is None:\n"
        "            return {'weights': {SYM_A: 0.50, CASH: 0.50}, 'purpose': 'initial'}\n"
        "        return None\n"
        "    return strategy\n",
        encoding="utf-8",
    )
    plugin_cfg = write_json(
        plugin_dir / "plugins.json",
        {"plugins": [{"family": "synth_own_acct", "entrypoint": f"{entry}:make_strategy"}]},
    )

    payload = schema_v3_fixture(17)
    payload["corporate_actions"] = []
    for row in payload["raw_bars"]:
        row.update(open=10.0, high=10.0, low=10.0, close=10.0)
    snap_store = tmp_path / "snaps"
    project = tmp_path / "proj"
    trusted = tmp_path / "trusted"
    sealed = ok(cli("snapshot", "seal", "--input", str(write_json(tmp_path / "snap.json", payload)), "--store", str(snap_store)))
    ok(cli("project", "init", "--project", str(project)))
    ok(cli("plugin", "register", "--project", str(project), "--plugin-config", str(plugin_cfg)))
    camp = ok(
        cli(
            "campaign", "create",
            "--project", str(project),
            "--store", str(snap_store),
            "--snapshot-id", sealed["snapshot_id"],
            "--attempts", "2",
            "--max-seconds", "30",
            "--initial-cash", "10000",
            "--transaction-cost-bps", "0",
            "--slippage-bps", "0",
            "--execution-rules", "actual-open-lot-v1",
            "--valuation-policy", "carry-forward-suspended-v1",
            "--decision-policy", "after-close-common-v1",
            "--lot-size", "100",
            "--discovery-warmup-sessions", "2",
            "--discovery-split", "70/30",
        )
    )
    exp = ok(
        cli(
            "experiment", "submit",
            "--project", str(project),
            "--plugin-config", str(plugin_cfg),
            "--input", str(
                write_json(
                    tmp_path / "exp.json",
                    {
                        "schema_version": 1,
                        "campaign_id": camp["campaign_id"],
                        "submission_id": "sub-own-acct",
                        "hypothesis": "Own account startup & warmup test",
                        "strategy": {"family": "synth_own_acct"},
                        "parameters": {},
                    },
                )
            ),
        )
    )
    opened = ok(open_round(project, trusted, camp["campaign_id"], write_json(tmp_path / "r.json", v2_round_input(limit=1))))
    ver = ok(submit_version(project, opened["round_id"], "v1", write_json(tmp_path / "v1.json", version_input(exp["experiment_id"]))))
    ok(
        cli(
            "round", "validate",
            "--project", str(project),
            "--store", str(trusted),
            "--snapshot-store", str(snap_store),
            "--round-id", opened["round_id"],
            "--version-id", ver["version_id"],
            "--plugin-config", str(plugin_cfg),
        )
    )

    # Calendar has 2 pre-enablement warmup sessions (cal[0], cal[1]); approval enables at cal[2]
    full_cal = calendar_payload(data_kind="real", start_offset=1, count=6)["sessions"]
    approval_cal = {
        "schema_version": 1,
        "data_kind": "real",
        "source": "synthetic operator trading calendar",
        "sessions": full_cal[2:],
    }
    approval = ok(
        approve_version(
            project,
            trusted,
            opened["round_id"],
            ver["version_id"],
            write_json(tmp_path / "cal.json", approval_cal),
            write_json(tmp_path / "obs.json", observation_input()),
        )
    )
    release_id = approval["tracking"]["release_id"]

    # Configure brand-new tracking account with own initial_cash=18000 and initial_positions={sym_a: 200}
    # (starting NAV = 18000 + 200*10 = 20000, distinct from Campaign's 10000!)
    ledger_dir = tmp_path / "ledger"
    ok(
        cli(
            "shadow", "migrate-state",
            "--ledger", str(ledger_dir),
            "--version", release_id,
            "--new-start-date", full_cal[2],
            "--initial-cash", "18000",
            "--initial-positions", json.dumps({sym_a: 200.0}),
        )
    )

    feed = {
        "schema_version": 3,
        "kind": "shadow_feed",
        "data_kind": "real",
        "raw_price_basis": "unadjusted",
        "symbols": list(ETF_SYMBOLS),
        "calendar": [
            {
                "date": d,
                "available_at": f"{full_cal[0]}T00:00:00+08:00",
                "availability_evidence": "fixture_cal",
            }
            for d in full_cal
        ],
        "evidence": {
            "items": [
                {
                    "id": PRICE_EVIDENCE,
                    "kind": "price_archive",
                    "reference": "synthetic:tdx-unadjusted@fwd",
                    "sha256": "b" * 64,
                    "available_at": f"{full_cal[0]}T08:00:00+08:00",
                },
                {
                    "id": STATUS_EVIDENCE,
                    "kind": "suspension_notice",
                    "reference": "synthetic:halt-notices@fwd",
                    "sha256": "c" * 64,
                    "available_at": f"{full_cal[0]}T08:00:00+08:00",
                },
            ]
        },
        "raw_bars": [
            {
                "date": d,
                "symbol": sym,
                "open": 10.0,
                "high": 10.0,
                "low": 10.0,
                "close": 10.0,
                "volume": 1000.0,
                "trading_status": "trading",
                "open_source": {"source": OPEN_SOURCE["source"], "evidence_id": PRICE_EVIDENCE},
                "available_at": f"{d}T15:00:00+08:00",
                "open_available_at": f"{d}T09:30:00+08:00",
                "close_available_at": f"{d}T15:00:00+08:00",
                "availability_evidence": "fixture_synth",
            }
            for d in full_cal
            for sym in ETF_SYMBOLS
        ],
        "derived": [
            {
                "date": d,
                "symbol": sym,
                "value": 10.0 + 0.05 * idx,
                "available_at": f"{d}T15:00:00+08:00",
                "availability_evidence": "fixture_synth",
            }
            for idx, d in enumerate(full_cal)
            for sym in ETF_SYMBOLS
        ],
        "corporate_actions": [],
    }
    feed_path = write_json(tmp_path / "feed.json", feed)
    corr_dir = tmp_path / "corr"
    corr_dir.mkdir()

    plugin_env = cli_env(PRAJNATRADE_PLUGIN_CONFIG=str(plugin_cfg))

    # Pre-enablement warmup day returns not_started and writes zero committed days
    pre = ok(
        shadow_update(
            ledger_dir,
            trusted,
            feed_path,
            full_cal[1],
            release_ids=[release_id],
            corrections_store=corr_dir,
            env=plugin_env,
            at_close=True,
        )
    )
    assert pre["versions"][0]["status"] == "not_started"

    # Day 1 (full_cal[2]): commits with own initial account (200 shares + 18000 cash = 20000 NAV),
    # generates initial 50% target (1000 shares total -> buy 800 more on full_cal[3])
    d1 = ok(
        shadow_update(
            ledger_dir,
            trusted,
            feed_path,
            full_cal[2],
            corrections_store=corr_dir,
            env=plugin_env,
            at_close=True,
        )
    )
    assert d1["versions"][0]["status"] == "committed"

    # Restart / advance to Day 2 (full_cal[3]): settles 800-share buy -> 1000 shares, 10000 cash, anchor=full_cal[2]
    d2 = ok(
        shadow_update(
            ledger_dir,
            trusted,
            feed_path,
            full_cal[3],
            corrections_store=corr_dir,
            env=plugin_env,
            at_close=True,
        )
    )
    assert d2["versions"][0]["status"] == "committed"

    # Replay Day 2 -> already_committed, zero double-fill, zero anchor change
    d2_replay = ok(
        shadow_update(
            ledger_dir,
            trusted,
            feed_path,
            full_cal[3],
            corrections_store=corr_dir,
            env=plugin_env,
            at_close=True,
        )
    )
    assert d2_replay["versions"][0]["status"] == "already_committed"

    rep_path = tmp_path / "rep.json"
    rep = ok(cli("shadow", "report", "--ledger", str(ledger_dir), "--version", release_id, "--output", str(rep_path)))
    assert rep["days"] == 2
    days = json.loads(rep_path.read_text(encoding="utf-8"))["days"]
    assert [d["date"] for d in days] == [full_cal[2], full_cal[3]]
    assert days[0]["result"]["cash"] == pytest.approx(18000.0, abs=1e-8)
    assert days[0]["result"]["positions"][sym_a]["quantity"] == pytest.approx(200.0, abs=1e-8)
    assert days[0]["result"]["nav"] == pytest.approx(20000.0, abs=1e-8)
    assert days[1]["result"]["cash"] == pytest.approx(10000.0, abs=1e-8)
    assert days[1]["result"]["positions"][sym_a]["quantity"] == pytest.approx(1000.0, abs=1e-8)
    assert days[1]["result"]["strategy_state"]["rebalance_anchor_date"] == full_cal[2]
    # Spec #6: lot order submitted_at must match decision_cutoff (15:30:00+08:00), not hardcoded 15:00
    assert days[0]["result"]["next_orders"][0]["submitted_at"] == f"{full_cal[2]}T15:30:00+08:00"


def test_discovery_actual_open_lot_without_decision_policy_supports_structured_output() -> None:
    """Spec #5: actual-open-lot-v1 without decision_policy parses structured dicts & passes state."""
    sym_a = ETF_SYMBOLS[0]
    sessions = ["2024-01-30", "2024-01-31", "2024-02-01"]
    data = _make_bars_and_derived(sessions)
    seen_states: list[dict] = []

    def structured_strategy(history, state):
        seen_states.append(dict(state))
        if state["signal_date"] == "2024-01-30":
            return {
                "weights": {sym_a: 0.20, CASH: 0.80},
                "fixed_amounts": {sym_a: 3000.0},
                "purpose": "monthly",
                "cycle": "2024-01",
            }
        return None

    result = run_partition_strategy(
        data,
        "snap-lot-nodec",
        "hash-lot-nodec",
        structured_strategy,
        partition_name="discovery",
        initial_cash=10000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
        signal_window_sessions=1,
        execution_rule_version=ACTUAL_OPEN_LOT_RULES,
        lot_sizes={s: 100 for s in ETF_SYMBOLS},
        symbol_order=ETF_SYMBOLS,
    )
    assert seen_states and "rebalance_anchor_date" in seen_states[0]
    assert seen_states[0]["prior_close_weights"] is not None
    assert len(result["fills"]) == 1
    assert result["fills"][0]["quantity"] == pytest.approx(300.0)
    assert result["strategy_state"]["rebalance_anchor_date"] == "2024-01-30"
    assert pytest.approx(result["strategy_state"]["last_monthly_filled_target"][sym_a]) == 0.20


def test_monthly_deferral_non_scheduled_day_and_month_end_not_executed() -> None:
    """Spec #7: pause on non-scheduled day does not trigger deferral; final month day closes not_executed."""
    sessions = [
        "2024-02-01",  # month_session=1 (clean, strategy emits None)
        "2024-02-02",  # month_session=2 (PAUSED -> non-scheduled day, must NOT trigger deferral!)
        "2024-02-05",  # month_session=3 (clean -> deferred_monthly must be False!)
        "2024-03-01",  # month_session=1 (PAUSED -> scheduled day, triggers deferral)
        "2024-03-04",  # month_session=2 (final session in March, PAUSED -> records not_executed on 03-04!)
        "2024-04-01",  # month_session=1 (April)
    ]
    data = _make_bars_and_derived(
        sessions,
        late_close_days={"2024-02-02", "2024-03-01", "2024-03-04"},
    )
    seen_states: dict[str, dict] = {}

    def strategy(history, state):
        seen_states[state["signal_date"]] = dict(state)
        return None

    policy = normalize_decision_policy(AFTER_CLOSE_COMMON_POLICY, monthly_deferral=True)
    result = run_partition_strategy(
        data,
        "snap-def",
        "hash-def",
        strategy,
        partition_name="discovery",
        initial_cash=10000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
        signal_window_sessions=1,
        execution_rule_version=ACTUAL_OPEN_LOT_RULES,
        decision_policy=policy,
        history_requirements=[{"id": "lookback", "observations": 1, "mode": "valid_observations"}],
        lot_sizes={s: 100 for s in ETF_SYMBOLS},
        symbol_order=ETF_SYMBOLS,
    )
    # Feb pause on month_session=2 must NOT trigger deferral
    assert seen_states["2024-02-05"]["deferred_monthly"] is False
    assert all(d["cycle"] != "2024-02" for d in result["monthly_deferrals"])
    # March pause on month_session=1 with unrecovered final March day records exactly one not_executed
    mar_defs = [d for d in result["monthly_deferrals"] if d["cycle"] == "2024-03"]
    assert len(mar_defs) == 1
    assert mar_defs[0]["status"] == "not_executed"
    assert mar_defs[0]["scheduled_date"] == "2024-03-01"


def test_legacy_migration_rejects_multiple_untyped_rebalances_and_zero_trade(tmp_path: Path) -> None:
    """Spec #8: multiple untyped legacy rebalances (filled or zero-trade) fail closed."""
    sym_a, sym_b = ETF_SYMBOLS[0], ETF_SYMBOLS[1]

    # Case 1: two filled legacy signals without purpose -> ambiguous last_monthly_filled_target
    ledger_multi = tmp_path / "ledger_multi"
    db = Ledger(ledger_multi)
    base_day = {
        "schema_version": 1,
        "kind": "shadow_day",
        "version_id": "rel-m",
        "status": "committed",
        "fills": [],
        "cash": 10000.0,
        "positions": {s: {"quantity": 0.0, "market_value": 0.0} for s in ETF_SYMBOLS},
        "nav": 10000.0,
        "signal": None,
        "next_orders": [],
        "orders": [],
    }
    db.commit_day(
        "rel-m", "rel-m", "2024-01-30",
        {**base_day, "date": "2024-01-30", "signal": {"date": "2024-01-30", "target_weights": {sym_a: 0.2, CASH: 0.8}},
         "next_orders": [{"order_id": "o1", "symbol": sym_a, "target_weight": 0.2, "deadline": "2024-01-31T09:30:00+08:00"}]},
        None, "2024-01-30", "enabled", "2024-01-30T16:00:00+08:00",
    )
    db.commit_day(
        "rel-m", "rel-m", "2024-01-31",
        {**base_day, "date": "2024-01-31",
         "orders": [{"order_id": "o1", "symbol": sym_a, "signal_date": "2024-01-30", "target_weight": 0.2, "status": "filled"}],
         "signal": {"date": "2024-01-31", "target_weights": {sym_b: 0.3, CASH: 0.7}},
         "next_orders": [{"order_id": "o2", "symbol": sym_b, "target_weight": 0.3, "deadline": "2024-02-01T09:30:00+08:00"}]},
        "2024-01-30", "2024-01-30", "enabled", "2024-01-31T16:00:00+08:00",
    )
    db.commit_day(
        "rel-m", "rel-m", "2024-02-01",
        {**base_day, "date": "2024-02-01",
         "orders": [{"order_id": "o2", "symbol": sym_b, "signal_date": "2024-01-31", "target_weight": 0.3, "status": "filled"}]},
        "2024-01-31", "2024-01-30", "enabled", "2024-02-01T16:00:00+08:00",
    )
    db.close()

    with pytest.raises(ShadowRejection) as exc_info:
        verify_or_migrate_ledger(ledger_multi, "rel-m")
    assert exc_info.value.code == "STATE_NOT_UNIQUELY_RECONSTRUCTIBLE"

    # Case 2: one untyped fill + one untyped zero-trade signal -> also fails closed
    ledger_zt = tmp_path / "ledger_zt"
    db2 = Ledger(ledger_zt)
    db2.commit_day(
        "rel-z", "rel-z", "2024-01-30",
        {**base_day, "version_id": "rel-z", "date": "2024-01-30",
         "signal": {"date": "2024-01-30", "target_weights": {sym_a: 0.2, CASH: 0.8}},
         "next_orders": [{"order_id": "o1", "symbol": sym_a, "target_weight": 0.2, "deadline": "2024-01-31T09:30:00+08:00"}]},
        None, "2024-01-30", "enabled", "2024-01-30T16:00:00+08:00",
    )
    db2.commit_day(
        "rel-z", "rel-z", "2024-01-31",
        {**base_day, "version_id": "rel-z", "date": "2024-01-31",
         "orders": [{"order_id": "o1", "symbol": sym_a, "signal_date": "2024-01-30", "target_weight": 0.2, "status": "filled"}],
         "signal": {"date": "2024-01-31", "target_weights": {sym_a: 0.205, CASH: 0.795}},
         "next_orders": []},
        "2024-01-30", "2024-01-30", "enabled", "2024-01-31T16:00:00+08:00",
    )
    db2.commit_day(
        "rel-z", "rel-z", "2024-02-01",
        {**base_day, "version_id": "rel-z", "date": "2024-02-01", "orders": []},
        "2024-01-31", "2024-01-30", "enabled", "2024-02-01T16:00:00+08:00",
    )
    db2.close()

    with pytest.raises(ShadowRejection) as exc_info2:
        verify_or_migrate_ledger(ledger_zt, "rel-z")
    assert exc_info2.value.code == "STATE_NOT_UNIQUELY_RECONSTRUCTIBLE"

