from __future__ import annotations

import pytest

from prajnatrade.lot_execution import (
    ACTUAL_OPEN_LOT_RULES,
    DEFAULT_LOT_SIZE,
    match_open_lot_day,
    normalize_lot_sizes,
)
from prajnatrade.trading_status import ETF_SYMBOLS


def _bar(
    day: str,
    symbol: str,
    *,
    open_price: float | None = 10.0,
    close_price: float | None = 10.0,
    status: str = "trading",
    volume: float | None = 1000.0,
    open_source: dict[str, str] | None = None,
    halt: dict[str, str] | None = None,
) -> dict:
    row: dict = {
        "date": day,
        "symbol": symbol,
        "open": open_price,
        "high": open_price,
        "low": open_price,
        "close": close_price,
        "volume": volume,
        "trading_status": status,
    }
    if open_source is not None:
        row["open_source"] = open_source
    if halt is not None:
        row["halt"] = halt
    return row


def test_normalize_lot_sizes_defaults_and_validation() -> None:
    assert ACTUAL_OPEN_LOT_RULES == "actual-open-lot-v1"
    assert DEFAULT_LOT_SIZE == 100
    assert normalize_lot_sizes() == {symbol: 100 for symbol in ETF_SYMBOLS}
    assert normalize_lot_sizes(default_lot_size=200) == {symbol: 200 for symbol in ETF_SYMBOLS}
    custom = normalize_lot_sizes({"511260": 50, "518880": 200})
    assert custom["511260"] == 50
    assert custom["518880"] == 200
    assert custom["515080"] == 100

    with pytest.raises(ValueError):
        normalize_lot_sizes({"511260": 0})
    with pytest.raises(ValueError):
        normalize_lot_sizes({"511260": True})  # type: ignore[dict-item]
    with pytest.raises(ValueError):
        normalize_lot_sizes({"UNKNOWN": 100})


def test_match_open_lot_day_distinguishes_lot_rounding_residual_from_cash_constraint() -> None:
    day = "2026-01-05"
    raw_bars = {
        (day, symbol): _bar(day, symbol, open_price=10.0, close_price=10.0)
        for symbol in ETF_SYMBOLS
    }
    open_prices = {symbol: 10.0 for symbol in ETF_SYMBOLS}
    shares = {symbol: 0.0 for symbol in ETF_SYMBOLS}
    weights = {symbol: (1.0 if symbol == "511260" else 0.0) for symbol in ETF_SYMBOLS}

    attempt, fills, cash, new_shares, costs = match_open_lot_day(
        day,
        {"date": day},
        weights,
        open_prices,
        raw_bars,
        cash=2050.0,
        shares=shares,
        fee_rate=0.0,
        slippage_rate=0.0,
        lot_sizes=normalize_lot_sizes(),
    )
    assert len(fills) == 1
    assert fills[0]["symbol"] == "511260"
    assert fills[0]["quantity"] == 200.0
    assert fills[0]["price"] == 10.0
    assert cash == pytest.approx(50.0)
    assert new_shares["511260"] == 200.0
    assert costs["fees"] == 0.0
    assert attempt["status"] == "filled"
    assert attempt["cash_constrained"] is False
    assert attempt["batch_complete"] is True
    assert attempt["basket_complete"] is True
    assert attempt["lot_rounding_residual"] == pytest.approx(50.0)
    assert attempt["residual_reason"] == "lot_rounding_residual"


def test_match_open_lot_day_proportional_allocation_and_fee_cap() -> None:
    day = "2026-01-05"
    raw_bars = {
        (day, symbol): _bar(day, symbol, open_price=10.0, close_price=10.0)
        for symbol in ETF_SYMBOLS
    }
    open_prices = {symbol: 10.0 for symbol in ETF_SYMBOLS}
    shares = {symbol: 0.0 for symbol in ETF_SYMBOLS}
    weights = {symbol: 0.0 for symbol in ETF_SYMBOLS}
    fixed_amounts = {"511260": 10_000.0, "518880": 10_000.0}

    attempt, fills, cash, new_shares, _ = match_open_lot_day(
        day,
        {"date": day},
        weights,
        open_prices,
        raw_bars,
        cash=12_000.0,
        shares=shares,
        fee_rate=0.0,
        slippage_rate=0.0,
        lot_sizes=normalize_lot_sizes(),
        fixed_amounts=fixed_amounts,
    )
    assert len(fills) == 2
    assert {f["symbol"]: f["quantity"] for f in fills} == {"511260": 600.0, "518880": 600.0}
    assert cash == pytest.approx(0.0)
    assert new_shares["511260"] == 600.0
    assert new_shares["518880"] == 600.0
    assert attempt["cash_constrained"] is True
    assert attempt["batch_complete"] is False
    assert attempt["residual_reason"] == "cash_constrained"

    # With non-zero fees + slippage, never overspend available cash
    attempt_fee, fills_fee, cash_fee, _, costs_fee = match_open_lot_day(
        day,
        {"date": day},
        weights,
        open_prices,
        raw_bars,
        cash=12_000.0,
        shares={symbol: 0.0 for symbol in ETF_SYMBOLS},
        fee_rate=0.001,
        slippage_rate=0.001,
        lot_sizes=normalize_lot_sizes(),
        fixed_amounts=fixed_amounts,
    )
    assert cash_fee >= 0.0
    assert costs_fee["fees"] > 0.0
    assert sum(f["quantity"] for f in fills_fee) == 1000.0  # 500 each since 600*10.02001 = 6012 > 6000
    assert attempt_fee["cash_constrained"] is True
    assert attempt_fee["batch_complete"] is False


@pytest.mark.parametrize(
    (
        "target_amounts",
        "initial_cash",
        "cost_bps",
        "slippage_bps",
        "expected_target_lots",
        "expected_filled_lots",
        "complete",
        "expected_ending_cash",
        "missing_open",
        "expected_status",
        "expected_reason",
    ),
    [
        (
            {ETF_SYMBOLS[0]: 2050.0},
            2000.0,
            0,
            0,
            {ETF_SYMBOLS[0]: 2},
            {ETF_SYMBOLS[0]: 2},
            True,
            0.0,
            False,
            "filled",
            None,
        ),
        (
            {ETF_SYMBOLS[0]: 3000.0},
            2000.0,
            0,
            0,
            {ETF_SYMBOLS[0]: 3},
            {ETF_SYMBOLS[0]: 2},
            False,
            0.0,
            False,
            "partially_filled",
            "cash_constrained",
        ),
        ({}, 2000.0, 0, 0, {}, {}, True, 2000.0, False, "filled", None),
        (
            {ETF_SYMBOLS[0]: 2050.0, ETF_SYMBOLS[1]: 2050.0},
            4008.005,
            10,
            10,
            {ETF_SYMBOLS[0]: 2, ETF_SYMBOLS[1]: 2},
            {ETF_SYMBOLS[0]: 2, ETF_SYMBOLS[1]: 2},
            True,
            0.001,
            False,
            "filled",
            None,
        ),
        (
            {ETF_SYMBOLS[0]: 2050.0},
            2000.0,
            0,
            0,
            {ETF_SYMBOLS[0]: None},
            {ETF_SYMBOLS[0]: 0},
            False,
            2000.0,
            True,
            "not_filled",
            "missing_required_open",
        ),
    ],
)
def test_public_discovery_compares_fills_with_preallocation_target_lots(
    tmp_path,
    target_amounts,
    initial_cash,
    cost_bps,
    slippage_bps,
    expected_target_lots,
    expected_filled_lots,
    complete,
    expected_ending_cash,
    missing_open,
    expected_status,
    expected_reason,
) -> None:
    """Whole-lot completion follows executable lots, not nominal target dollars."""
    import json

    from cli_helpers import cli, ok, write_json
    from test_actual_open_cli import (
        make_missing_required_open,
        schema_v3_fixture,
        seal_v3,
    )

    payload = schema_v3_fixture(10)
    payload["corporate_actions"] = []
    for row in payload["raw_bars"]:
        row.update(open=10.0, high=10.0, low=10.0, close=10.0, volume=10_000.0)
    if missing_open:
        for day in [row["date"] for row in payload["calendar"]]:
            make_missing_required_open(payload, day, ETF_SYMBOLS[0])
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "strategy.py").write_text(
        "\n".join(
            [
                "from prajnatrade.data import CASH, ETF_SYMBOLS",
                "def make_plugin(parameters):",
                "    def strategy(history, state):",
                "        if state['previous_order_attempted'] is not None:",
                "            return None",
                "        return {",
                "            'weights': {**{s: 0.0 for s in ETF_SYMBOLS}, CASH: 1.0},",
                "            'purpose': 'initial',",
                f"            'fixed_amounts': {target_amounts!r},",
                "        }",
                "    return strategy",
                "",
            ]
        ),
        encoding="utf-8",
    )
    plugin_config = plugin_dir / "plugins.json"
    plugin_config.write_text(
        json.dumps(
            {"plugins": [{"family": "fixed_budget", "entrypoint": "strategy.py:make_plugin"}]}
        ),
        encoding="utf-8",
    )

    sealed = seal_v3(tmp_path, payload, store="store", name="lot-target")
    project = tmp_path / "project"
    ok(cli("project", "init", "--project", str(project)))
    campaign = ok(
        cli(
            "campaign",
            "create",
            "--project",
            str(project),
            "--store",
            str(sealed["store"]),
            "--snapshot-id",
            sealed["snapshot_id"],
            "--attempts",
            "1",
            "--max-seconds",
            "60",
            "--initial-cash",
            str(initial_cash),
            "--transaction-cost-bps",
            str(cost_bps),
            "--slippage-bps",
            str(slippage_bps),
            "--execution-rules",
            ACTUAL_OPEN_LOT_RULES,
            "--lot-size",
            "100",
            "--discovery-warmup-sessions",
            "3",
        )
    )
    submission = write_json(
        tmp_path / "submission.json",
        {
            "schema_version": 1,
            "campaign_id": campaign["campaign_id"],
            "submission_id": "lot-target",
            "hypothesis": "Compare executable target lots to funded fills.",
            "strategy": {"family": "fixed_budget"},
            "parameters": {},
        },
    )
    result = ok(
        cli(
            "experiment",
            "submit",
            "--project",
            str(project),
            "--plugin-config",
            str(plugin_config),
            "--input",
            str(submission),
        )
    )["discovery"]
    attempt = result["order_attempts"][0]

    lot_orders = {order["symbol"]: order for order in attempt["lot_orders"]}
    assert {
        symbol: order["target_executable_lots"]
        for symbol, order in lot_orders.items()
    } == expected_target_lots
    assert {
        symbol: order["actual_filled_lots"]
        for symbol, order in lot_orders.items()
    } == expected_filled_lots
    assert {
        symbol: order["shortfall_lots"]
        for symbol, order in lot_orders.items()
    } == {
        symbol: (
            expected_target_lots[symbol] - expected_filled_lots[symbol]
            if expected_target_lots[symbol] is not None
            else None
        )
        for symbol in expected_target_lots
    }
    assert attempt["batch_complete"] is complete
    assert attempt["status"] == expected_status
    if expected_reason == "cash_constrained":
        assert attempt["residual_reason"] == "cash_constrained"
    if lot_orders:
        assert lot_orders[ETF_SYMBOLS[0]]["reason"] == expected_reason
    if expected_reason == "missing_required_open":
        assert lot_orders[ETF_SYMBOLS[0]]["target_amount"] == 2050.0
    if target_amounts == {ETF_SYMBOLS[0]: 2050.0} and not missing_open:
        assert attempt["cash_constrained"] is False
        assert attempt["lot_rounding_residual"] == pytest.approx(50.0)
        assert attempt["residual_reason"] == "lot_rounding_residual"
    assert result["metrics"]["orders_filled"] == int(complete)
    assert result["metrics"]["orders_unfilled"] == int(not complete)
    assert result["metrics"]["lot_order_count"] == len(target_amounts)
    assert result["metrics"]["shortfall_lots"] == pytest.approx(
        sum(
            lots for lots in expected_target_lots.values() if lots is not None
        )
        - sum(expected_filled_lots.values())
    )
    assert result["equity_curve"][-1]["cash"] == pytest.approx(expected_ending_cash)
    if cost_bps:
        assert result["metrics"]["fees_paid"] == pytest.approx(4.004)
        assert result["metrics"]["slippage_paid"] == pytest.approx(4.0)
        ratio = initial_cash / sum(target_amounts.values())
        for symbol, order in lot_orders.items():
            assert order["allocated_amount"] == pytest.approx(
                target_amounts[symbol] * ratio
            )
    assert result["strategy_state"]["rebalance_anchor_date"] == (
        attempt["signal_date"] if complete else None
    )
    if not target_amounts:
        assert attempt["zero_trade_satisfied"] is True
        assert attempt["lot_orders"] == []


def test_match_open_lot_day_unknown_equity_and_unfilled_sell_no_buying_power() -> None:
    day = "2026-01-05"
    raw_bars = {
        (day, "511260"): _bar(day, "511260", open_price=None, close_price=None, status="suspended", volume=0.0),
        (day, "518880"): _bar(day, "518880", open_price=12.0, close_price=12.0),
        (day, "515080"): _bar(day, "515080", open_price=10.0, close_price=10.0),
        (day, "513300"): _bar(day, "513300", open_price=10.0, close_price=10.0),
        (day, "159612"): _bar(day, "159612", open_price=10.0, close_price=10.0),
    }
    open_prices = {"511260": None, "518880": 12.0, "515080": 10.0, "513300": 10.0, "159612": 10.0}
    shares = {"511260": 500.0, "518880": 200.0, "515080": 0.0, "513300": 0.0, "159612": 0.0}

    # Case 1: explicit full exit of B (518880) executes while weight-dependent C (515080) is blocked
    weights = {"511260": 0.0, "518880": 0.0, "515080": 1.0, "513300": 0.0, "159612": 0.0}
    attempt, fills, cash, new_shares, _ = match_open_lot_day(
        day,
        {"date": day},
        weights,
        open_prices,
        raw_bars,
        cash=0.0,
        shares=dict(shares),
        fee_rate=0.0,
        slippage_rate=0.0,
        lot_sizes=normalize_lot_sizes(),
    )
    assert len(fills) == 1
    assert fills[0]["symbol"] == "518880"
    assert fills[0]["side"] == "sell"
    assert fills[0]["quantity"] == 200.0
    assert cash == pytest.approx(2400.0)
    assert new_shares["518880"] == 0.0
    assert new_shares["511260"] == 500.0
    assert attempt["status"] == "partially_filled"
    assert attempt["batch_complete"] is False
    unfilled_map = {item["symbol"]: item["reason"] for item in attempt["unfilled"]}
    assert unfilled_map["511260"] == "confirmed_no_trade"
    assert unfilled_map["515080"] == "equity_basis_unknown"
    lot_order_map = {item["symbol"]: item for item in attempt["lot_orders"]}
    assert lot_order_map["511260"]["reason"] == "confirmed_no_trade"
    assert lot_order_map["515080"]["reason"] == "equity_basis_unknown"
    assert lot_order_map["515080"]["target_executable_lots"] is None

    # Case 2: unfilled sell of A produces ZERO buying power for fixed-amount buy of C
    attempt2, fills2, cash2, new_shares2, _ = match_open_lot_day(
        day,
        {"date": day},
        {"511260": 0.0, "518880": 0.0, "515080": 0.0, "513300": 0.0, "159612": 0.0},
        open_prices,
        raw_bars,
        cash=0.0,
        shares={"511260": 500.0, "518880": 0.0, "515080": 0.0, "513300": 0.0, "159612": 0.0},
        fee_rate=0.0,
        slippage_rate=0.0,
        lot_sizes=normalize_lot_sizes(),
        fixed_amounts={"515080": 5000.0},
    )
    assert fills2 == []
    assert cash2 == 0.0
    assert new_shares2["515080"] == 0.0
    assert attempt2["cash_constrained"] is True
    assert attempt2["batch_complete"] is False


def test_match_open_lot_day_discloses_late_resume_sell_funding_same_day_buy() -> None:
    day = "2026-01-05"
    raw_bars = {
        (day, "511260"): _bar(
            day,
            "511260",
            open_price=12.0,
            close_price=12.0,
            status="partial_halt",
            volume=5000.0,
            open_source={"source": "vendor", "evidence_id": "ev-open"},
            halt={"start": f"{day}T09:30:00+08:00", "end": f"{day}T10:30:00+08:00"},
        ),
        (day, "518880"): _bar(day, "518880", open_price=10.0, close_price=10.0),
        (day, "515080"): _bar(day, "515080", open_price=10.0, close_price=10.0),
        (day, "513300"): _bar(day, "513300", open_price=10.0, close_price=10.0),
        (day, "159612"): _bar(day, "159612", open_price=10.0, close_price=10.0),
    }
    open_prices = {"511260": 12.0, "518880": 10.0, "515080": 10.0, "513300": 10.0, "159612": 10.0}
    shares = {"511260": 100.0, "518880": 0.0, "515080": 0.0, "513300": 0.0, "159612": 0.0}
    weights = {"511260": 0.0, "518880": 1.0, "515080": 0.0, "513300": 0.0, "159612": 0.0}

    attempt, fills, cash, new_shares, _ = match_open_lot_day(
        day,
        {"date": day},
        weights,
        open_prices,
        raw_bars,
        cash=0.0,
        shares=shares,
        fee_rate=0.0,
        slippage_rate=0.0,
        lot_sizes=normalize_lot_sizes(),
    )
    assert len(fills) == 2
    assert fills[0]["symbol"] == "511260" and fills[0]["side"] == "sell"
    assert fills[1]["symbol"] == "518880" and fills[1]["side"] == "buy"
    assert fills[1]["quantity"] == 100.0
    assert cash == pytest.approx(200.0)
    assert new_shares["518880"] == 100.0
    assumptions = attempt["daily_simulation_assumptions"]
    assert assumptions["late_resume_sell_funds_same_day_buy"] is True
    assert assumptions["triggered"] is True
    assert assumptions["late_resume_sell_symbols"] == ["511260"]
    assert assumptions["funded_buy_symbols"] == ["518880"]


def test_discovery_and_formal_validation_freeze_lot_rules_and_size_at_actual_open(tmp_path) -> None:
    import json

    from cli_helpers import cli, ok, write_json
    from test_actual_open_cli import schema_v3_fixture, seal_v3
    from test_formal_validation_cli import formal_scenario
    from test_protected_eval_cli import create_campaign, submit_experiment

    from prajnatrade.experiment import _runtime_components, runtime_identity
    from prajnatrade.trading_status import NEXT_OPEN_RULES

    # Guardrail: legacy runtime_identity must not include lot_execution.py
    assert "lot_execution.py" not in _runtime_components(execution_rules={"version": NEXT_OPEN_RULES})
    assert "lot_execution.py" in _runtime_components(execution_rules={"version": ACTUAL_OPEN_LOT_RULES})
    assert runtime_identity() == runtime_identity(execution_rules={"version": NEXT_OPEN_RULES})

    # Build a 15-session schema-3 fixture (development=6, trailing=3) with close=10, open=12
    payload = schema_v3_fixture(15)
    for row in payload["raw_bars"]:
        row["open"] = 12.0
        row["high"] = 12.0
        row["low"] = 10.0
        row["close"] = 10.0
        row["volume"] = 10_000.0
    sealed = seal_v3(tmp_path, payload, store="lot-store", name="lot-gap")
    project = tmp_path / "lot-project"
    ok(cli("project", "init", "--project", str(project)))

    campaign = create_campaign(
        project,
        sealed["store"],
        sealed["snapshot_id"],
        "--execution-rules",
        ACTUAL_OPEN_LOT_RULES,
        "--lot-size",
        "100",
        "--initial-cash",
        "100000",
        "--transaction-cost-bps",
        "0",
        "--slippage-bps",
        "0",
        "--discovery-warmup-sessions",
        "3",
        "--discovery-split",
        "70/30",
    )
    rules = campaign["execution_rules"]
    assert rules["version"] == ACTUAL_OPEN_LOT_RULES
    assert rules["execution"]["lot_sizes"] == {symbol: 100 for symbol in ETF_SYMBOLS}
    assert rules["execution"]["symbol_order"] == list(ETF_SYMBOLS)

    # Discovery candidate & benchmark: equal_weight matches benchmark exactly,
    # 100,000 / 5 = 20,000 per ETF at open=12.0 -> floor(20000 / 1200) = 16 lots = 1600 shares
    sub = submit_experiment(
        project,
        campaign["campaign_id"],
        tmp_path / "exp.json",
        "s1",
        "equal weight lot test",
        "equal_weight",
        {},
    )
    assert len(sub["discovery"]["fills"]) == 5
    for fill in sub["discovery"]["fills"]:
        assert fill["raw_open"] == 12.0
        assert fill["quantity"] == 1600.0
    # 5 * 1600 * 12 = 96,000 invested, 4,000 cash residual, close=10 -> NAV = 4000 + 8000*10 = 84000
    assert sub["discovery"]["metrics"]["ending_equity"] == pytest.approx(84_000.0)
    assert sub["discovery"]["return_delta"] == pytest.approx(0.0)
    assert sub["discovery"]["baseline_comparison"]["baseline"]["metrics"]["fills"] == 5

    # Formal validation on trailing segment also freezes lot_sizes & sizes at open=12.0
    from test_formal_validation_cli import formal_scenario

    scenario = formal_scenario(
        tmp_path,
        name="lot-formal",
        execution_rules=ACTUAL_OPEN_LOT_RULES,
        frozen_campaign=(project, sealed["store"], campaign),
        family="equal_weight",
        parameters={},
    )
    receipt = ok(
        cli(
            "round",
            "validate",
            "--project",
            str(project),
            "--store",
            str(scenario["store"]),
            "--snapshot-store",
            str(sealed["store"]),
            "--round-id",
            scenario["opened"]["round_id"],
            "--version-id",
            scenario["version"]["version_id"],
        )
    )
    assert receipt["provenance"]["execution_rule_version"] == ACTUAL_OPEN_LOT_RULES
    assert receipt["provenance"]["lot_sizes"] == {symbol: 100 for symbol in ETF_SYMBOLS}
    assert receipt["provenance"]["daily_simulation_assumptions"]["late_resume_sell_funds_same_day_buy"] is True
    assert receipt["summary"]["candidate"]["total_return"] == pytest.approx(-0.16)
    assert receipt["summary"]["benchmark"]["total_return"] == pytest.approx(-0.16)
    for side in ("candidate", "benchmark"):
        formal_run = receipt["runs"][side]
        assert formal_run["metrics"]["lot_order_count"] == 5
        entry_attempt = next(
            attempt for attempt in formal_run["order_attempts"] if attempt["lot_orders"]
        )
        assert {
            order["target_executable_lots"] for order in entry_attempt["lot_orders"]
        } == {16}
        assert {
            order["actual_filled_lots"] for order in entry_attempt["lot_orders"]
        } == {16}
        assert entry_attempt["batch_complete"] is True


def test_shadow_live_freezes_pre_open_weights_and_reconciles_with_discovery(tmp_path) -> None:
    import json

    from cli_helpers import cli, ok
    from test_actual_open_cli import schema_v3_fixture
    from test_shadow_cli import day_record, shadow_report
    from test_shadow_feed_v3_valuation_cli import _build_manual_tracking_release

    from prajnatrade.trading_status import STRICT_CLOSE_VALUATION

    from datetime import date, timedelta

    payload = schema_v3_fixture(8)
    payload["data_kind"] = "synthetic"
    payload["symbols"] = list(ETF_SYMBOLS)
    old_sessions = [row["date"] for row in payload["calendar"]]
    date_map = {
        old: (date(2099, 1, 1) + timedelta(days=i)).isoformat()
        for i, old in enumerate(old_sessions)
    }
    for row in payload["calendar"]:
        row["date"] = date_map[row["date"]]
    sessions = [row["date"] for row in payload["calendar"]]
    for row in payload["raw_bars"]:
        row["date"] = date_map[row["date"]]
        row["open"] = 12.0
        row["high"] = 12.0
        row["low"] = 10.0
        row["close"] = 10.0
        row["volume"] = 10_000.0
        row["open_available_at"] = f"{row['date']}T09:30:00+08:00"
        row["close_available_at"] = f"{row['date']}T15:00:00+08:00"
        row["available_at"] = f"{row['date']}T09:30:00+08:00"
    for d_row in payload["derived"]:
        d_row["date"] = date_map[d_row["date"]]
        idx = sessions.index(d_row["date"])
        d_row["value"] = 10.0 + (0.5 * idx if d_row["symbol"] == "511260" else 0.0)
        d_row["available_at"] = f"{d_row['date']}T15:00:00+08:00"
    payload["corporate_actions"] = []

    feed_path = tmp_path / "shadow_feed_v3.json"
    feed_path.write_text(json.dumps(payload), encoding="utf-8")

    store = tmp_path / "release-store"
    ledger = tmp_path / "shadow-ledger"
    reg = _build_manual_tracking_release(
        store,
        sessions,
        valuation_policy=STRICT_CLOSE_VALUATION,
        execution_rules=ACTUAL_OPEN_LOT_RULES,
        initial_cash=100_000.0,
    )
    release_id = reg["release_id"]

    corrections = tmp_path / "corrections"
    corrections.mkdir(parents=True, exist_ok=True)
    # Run through sessions[2] (signal day: close=10.0, emits pre-open weights for 511260=1.0)
    ok(
        cli(
            "shadow",
            "update",
            "--store",
            str(store),
            "--data",
            str(feed_path),
            "--ledger",
            str(ledger),
            "--corrections-store",
            str(corrections),
            "--date",
            sessions[2],
        )
    )
    report_out = tmp_path / "shadow-report"
    day2 = day_record(ledger, release_id, report_out, sessions[2])["result"]
    assert len(day2["next_orders"]) == 1
    assert day2["next_orders"][0]["symbol"] == "511260"
    assert day2["next_orders"][0]["order_sizing_mode"] == "open_weight"
    assert day2["next_orders"][0]["target_weight"] == pytest.approx(1.0)
    assert "quantity" not in day2["next_orders"][0]

    # Run through sessions[3] (execution day: open=12.0, settles whole lots at 12.0!)
    # 100,000 / (100 * 12.0) = 83.33 -> 83 lots = 8300 shares, cost = 99,600, cash = 400
    ok(
        cli(
            "shadow",
            "update",
            "--store",
            str(store),
            "--data",
            str(feed_path),
            "--ledger",
            str(ledger),
            "--corrections-store",
            str(corrections),
            "--date",
            sessions[3],
        )
    )
    day3 = day_record(ledger, release_id, report_out, sessions[3])["result"]
    assert len(day3["fills"]) == 1
    assert day3["fills"][0]["symbol"] == "511260"
    assert day3["fills"][0]["raw_open"] == 12.0
    assert day3["fills"][0]["quantity"] == 8300.0
    assert day3["cash"] == pytest.approx(400.0)
    assert day3["nav"] == pytest.approx(83_400.0)  # 400 + 8300 * 10
    assert day3["batch_complete"] is True
    assert day3["cash_constrained"] is False
    lot_order = next(
        order for order in day3["lot_orders"] if order["symbol"] == "511260"
    )
    assert lot_order["target_executable_lots"] == 83
    assert lot_order["actual_filled_lots"] == 83
    assert lot_order["shortfall_lots"] == 0
    assert lot_order["status"] == "filled"
    assert day3["daily_simulation_assumptions"]["late_resume_sell_funds_same_day_buy"] is True
    ok(shadow_report(ledger, release_id, report_out))
    shadow_receipt = json.loads(report_out.read_text(encoding="utf-8"))
    assert shadow_receipt["summary"]["lot_order_count"] == 1
    assert shadow_receipt["summary"]["lot_orders_unfilled"] == 0
    assert shadow_receipt["summary"]["shortfall_lots"] == 0

    # Reconcile Discovery on the same data + momentum strategy
    from prajnatrade.snapshot_baseline import run_discovery_strategy
    from prajnatrade.strategies import make_strategy

    strat = make_strategy(
        {"family": "momentum", "parameters": {"lookback": 2, "top_k": 1, "cash_if_negative": False}}
    )
    disc = run_discovery_strategy(
        payload,
        "a" * 64,
        "b" * 64,
        strat,
        data_kind="synthetic",
        signal_window_sessions=3,
        initial_cash=100_000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
        execution_rule_version=ACTUAL_OPEN_LOT_RULES,
        lot_sizes={s: 100 for s in ETF_SYMBOLS},
    )
    assert disc["fills"][0]["date"] == day3["fills"][0]["date"]
    assert disc["fills"][0]["symbol"] == day3["fills"][0]["symbol"]
    assert disc["fills"][0]["quantity"] == day3["fills"][0]["quantity"]
    assert disc["fills"][0]["raw_open"] == day3["fills"][0]["raw_open"]
    assert disc["equity_curve"][3]["nav"] == pytest.approx(day3["nav"])
    discovery_order = next(
        order
        for attempt in disc["order_attempts"]
        if attempt["date"] == sessions[3]
        for order in attempt["lot_orders"]
        if order["symbol"] == "511260"
    )
    assert {
        key: discovery_order[key]
        for key in (
            "target_executable_lots",
            "actual_filled_lots",
            "shortfall_lots",
            "reason",
        )
    } == {
        key: lot_order[key]
        for key in (
            "target_executable_lots",
            "actual_filled_lots",
            "shortfall_lots",
            "reason",
        )
    }

    # Legacy next-open-v1 Shadow release still freezes concrete share quantities
    store_legacy = tmp_path / "release-store-legacy"
    ledger_legacy = tmp_path / "shadow-ledger-legacy"
    reg_legacy = _build_manual_tracking_release(
        store_legacy,
        sessions,
        valuation_policy=STRICT_CLOSE_VALUATION,
        execution_rules="next-open-v1",
        initial_cash=100_000.0,
    )
    ok(
        cli(
            "shadow",
            "update",
            "--store",
            str(store_legacy),
            "--data",
            str(feed_path),
            "--ledger",
            str(ledger_legacy),
            "--corrections-store",
            str(corrections),
            "--date",
            sessions[2],
        )
    )
    day2_legacy = day_record(ledger_legacy, reg_legacy["release_id"], report_out, sessions[2])["result"]
    assert "order_sizing_mode" not in day2_legacy["next_orders"][0]
    assert day2_legacy["next_orders"][0]["quantity"] == pytest.approx(10_000.0)


def test_carry_forward_valuation_never_enters_lot_sizing(tmp_path) -> None:
    """Changing carry-forward valuation on suspended A must not alter order sizing."""
    day = "2026-01-05"
    raw_bars = {
        (day, "511260"): _bar(day, "511260", open_price=None, close_price=None, status="suspended", volume=0.0),
        (day, "518880"): _bar(day, "518880", open_price=12.0, close_price=12.0),
        (day, "515080"): _bar(day, "515080", open_price=10.0, close_price=10.0),
        (day, "513300"): _bar(day, "513300", open_price=10.0, close_price=10.0),
        (day, "159612"): _bar(day, "159612", open_price=10.0, close_price=10.0),
    }
    open_prices = {"511260": None, "518880": 12.0, "515080": 10.0, "513300": 10.0, "159612": 10.0}
    shares = {"511260": 500.0, "518880": 200.0, "515080": 0.0, "513300": 0.0, "159612": 0.0}
    weights = {"511260": 0.5, "518880": 0.5, "515080": 0.0, "513300": 0.0, "159612": 0.0}

    # Even if signal carries a prior_nav / valuation hint, match_open_lot_day ignores it
    attempt_a, fills_a, cash_a, shares_a, _ = match_open_lot_day(
        day,
        {"date": day, "prior_nav": 50_000.0},
        weights,
        open_prices,
        raw_bars,
        cash=10_000.0,
        shares=dict(shares),
        fee_rate=0.0,
        slippage_rate=0.0,
        lot_sizes=normalize_lot_sizes(),
    )
    attempt_b, fills_b, cash_b, shares_b, _ = match_open_lot_day(
        day,
        {"date": day, "prior_nav": 500_000.0},
        weights,
        open_prices,
        raw_bars,
        cash=10_000.0,
        shares=dict(shares),
        fee_rate=0.0,
        slippage_rate=0.0,
        lot_sizes=normalize_lot_sizes(),
    )
    assert fills_a == fills_b == []
    assert cash_a == cash_b == 10_000.0
    assert shares_a == shares_b == shares
    assert attempt_a["unfilled"] == attempt_b["unfilled"]
    assert {item["symbol"]: item["reason"] for item in attempt_a["unfilled"]} == {
        "511260": "confirmed_no_trade",
        "518880": "equity_basis_unknown",
    }
    _ = tmp_path
