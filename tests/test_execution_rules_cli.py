"""Exercise frozen execution rules through the public research flow."""

import pytest
from cli_helpers import agent_cli, cli, ok, validation_service, write_json
from test_actual_open_cli import (
    OPEN_SOURCE,
    day_at,
    derived_row,
    make_actual_open_halt,
    make_confirmed_suspension,
    make_missing_required_open,
    make_unknown_status,
    make_whole_day_halt,
    raw_row,
    schema_v3_fixture,
    seal_v3,
)
from test_protected_eval_cli import create_campaign, seal_snapshot, submit_experiment, synthetic_snapshot
from test_research_direction_cli import approve_round, kickoff_input, propose, run_validation

from prajnatrade.data import ETF_SYMBOLS
from prajnatrade.snapshot_baseline import ACTUAL_OPEN_RULES, EXECUTION_PRICE_BASIS

FEE_RATE = 5 / 10_000
#: The frozen ``experiment._execution_policy`` block of ``actual-open-v1``.
ACTUAL_OPEN_EXECUTION = {
    "source": "sealed_partition.raw_bars.open",
    "basis": "unadjusted",
    "attempts": 1,
    "halted_open": "actual_daily_open_when_evidenced",
    "missing_required_open": "symbol_not_filled",
    "partial_fill": "recorded_per_symbol",
    "funding": "initial_cash_plus_realized_sell_net",
    "order": "sells_before_buys",
    "matching": "per_symbol",
    "partial_halt_open_availability": "not_gated_by_0930",
}
#: The result key sets every legacy rule keeps exactly.
LEGACY_METRICS = {
    "sessions", "valued_sessions", "total_return", "max_drawdown", "sharpe",
    "fees_paid", "slippage_paid", "ending_equity", "fills", "orders_filled",
    "orders_unfilled", "signal_gaps", "valuation_gaps",
}
LEGACY_POSITION_EVIDENCE = {
    "state", "positive_target_signals", "cash_target_signals", "invested_sessions",
    "first_position_signal_date", "first_fill_date",
}


def _v3_run(
    tmp_path, name, payload, *, family="equal_weight", parameters=None,
    execution_rules=ACTUAL_OPEN_RULES, slippage_bps=0,
):
    """Seal a schema-3 payload and run one public-CLI Campaign on it."""
    project = tmp_path / f"{name}-project"
    sealed = seal_v3(tmp_path, payload, store=f"{name}-store", name=name)
    ok(cli("project", "init", "--project", str(project)))
    campaign = create_campaign(
        project, sealed["store"], sealed["snapshot_id"],
        "--execution-rules", execution_rules, "--slippage-bps", str(slippage_bps),
    )
    experiment = submit_experiment(
        project, campaign["campaign_id"], tmp_path / f"{name}-experiment.json", "run",
        "Reconcile the synthetic basket account from the recorded fills.",
        family, dict(parameters or {}),
    )
    return {"sealed": sealed, "campaign": campaign, "experiment": experiment}


def _sides(experiment):
    discovery = experiment["discovery"]
    return discovery, discovery["baseline_comparison"]["baseline"]


def _holdings(run, day):
    point = next(point for point in run["equity_curve"] if point["date"] == day)
    return point["holdings"]


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
    # Both legacy rules keep their exact result key sets: no per-symbol additive
    # metrics or basket-build evidence leaks into them.
    for run in (receipt["discovery"], baseline):
        assert set(run["metrics"]) == LEGACY_METRICS
        assert set(run["position_evidence"]) == LEGACY_POSITION_EVIDENCE
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
    assert validation["scoring_evidence"]["baseline"]["satisfied"] is True
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
    validation_service(project, store, sealed["store"], kickoff["round_id"])
    details = ok(agent_cli("validation", "details", *detail_flags, *date_flags))["details"]
    discovery_run = discovery["discovery"]
    assert discovery_run["order_attempts"][1]["signal_date"] == "2024-01-01"
    assert len(discovery_run["fills"]) == len(details["fills"]) == 5
    assert all(fill["date"] == "2024-01-18" and fill["raw_open"] == 12 for fill in details["fills"])
    assert [{**fill, "date": "2024-01-03"} for fill in details["fills"]] == discovery_run["fills"]
    assert details["cash"] == discovery_run["equity_curve"][2]["cash"]
    assert details["baseline"]["cash"] == details["cash"]
    assert details["position_evidence"] == {"first_fill_date": "2024-01-18"}


# ---------------------------------------------------------------------------
# actual-open-v1: per-symbol matching, sells then buys, realized sell net
# ---------------------------------------------------------------------------

MOMENTUM = {"lookback": 2, "top_k": 1, "cash_if_negative": False}


def test_actual_open_ignores_the_published_time_of_a_pre_open_order(tmp_path):
    """B1: the reviewer's 15:05 repro fills 5/5 under either trading status.

    Five ETFs are ordered before the open and one symbol's sealed actual open
    is 10 in both samples, while its quote is only published at 15:05.  The
    order was effective before the open, so ``actual-open-v1`` matches it
    retrospectively: a normal ``trading`` row fills exactly like a resumed
    ``partial_halt`` row, with the same fill date, base price, quantity, fee and
    cash for the candidate and the benchmark.  A ``next-open-v1`` control keeps
    its exact 09:30 publication gate and still refuses that open.
    """
    execution_day = day_at(1)
    late_symbol = ETF_SYMBOLS[0]
    published_at = f"{execution_day}T15:05:00+08:00"
    normal_payload = schema_v3_fixture(8)
    raw_row(normal_payload, execution_day, late_symbol)[
        "open_available_at"
    ] = published_at
    halted_payload = schema_v3_fixture(8)
    halted_row = make_actual_open_halt(halted_payload, execution_day, late_symbol)
    halted_row["open_available_at"] = published_at

    expected_attempt = {
        "signal_date": day_at(0),
        "date": execution_day,
        "attempts": 1,
        "status": "filled",
        "filled_symbols": list(ETF_SYMBOLS),
        "unfilled": [],
        "basket_complete": True,
    }
    observed = {}
    samples = {
        "normal": _v3_run(tmp_path, "late-normal", normal_payload),
        "halted": _v3_run(tmp_path, "late-halted", halted_payload),
    }
    for name, sample in samples.items():
        candidate, benchmark = _sides(sample["experiment"])
        for run in (candidate, benchmark):
            # The same open of 10 fills all five symbols in both samples.
            assert run["order_attempts"] == [expected_attempt]
            assert [fill["date"] for fill in run["fills"]] == (
                [execution_day] * len(ETF_SYMBOLS)
            )
            assert [fill["symbol"] for fill in run["fills"]] == list(ETF_SYMBOLS)
            assert {fill["raw_open"] for fill in run["fills"]} == {10.0}
            assert all(fill["open_evidence"] == OPEN_SOURCE for fill in run["fills"])
        assert candidate["fills"] == benchmark["fills"]
        assert candidate["equity_curve"] == benchmark["equity_curve"]
        assert candidate["metrics"] == benchmark["metrics"]
        observed[name] = {
            "fills": candidate["fills"],
            "ending_cash": candidate["equity_curve"][-1]["cash"],
            "ending_equity": candidate["metrics"]["ending_equity"],
        }
    # Only the trading status differs between the samples: the recorded fills,
    # cash and ending equity are identical.
    assert observed["normal"]["fills"] == observed["halted"]["fills"]
    assert observed["normal"]["ending_cash"] == pytest.approx(
        observed["halted"]["ending_cash"]
    )
    assert observed["normal"]["ending_equity"] == pytest.approx(
        observed["halted"]["ending_equity"]
    )

    # Control: the legacy rule keeps its 09:30 publication gate exactly, so the
    # late quote still leaves the attempt unfilled.
    control = _v3_run(
        tmp_path, "late-control", normal_payload, execution_rules="next-open-v1",
    )
    for run in _sides(control["experiment"]):
        assert run["order_attempts"][0] == {
            "signal_date": day_at(0),
            "date": execution_day,
            "attempts": 1,
            "status": "not_filled",
            "reason": "missing_or_unconfirmed_open",
            "missing_symbols": [late_symbol],
        }
        assert all(fill["date"] != execution_day for fill in run["fills"])


def _engineering_closes(payload):
    """Two symbols whose 2-day momentum hands over A -> B on session four."""
    for symbol, values in (
        (ETF_SYMBOLS[0], (100.0, 100.0, 110.0, 110.0, 110.0, 110.0, 110.0, 110.0, 110.0, 110.0)),
        (ETF_SYMBOLS[1], (100.0, 100.0, 100.0, 120.0, 130.0, 130.0, 130.0, 130.0, 130.0, 130.0)),
    ):
        for index, value in enumerate(values):
            derived_row(payload, day_at(index), symbol)["value"] = value
    return payload


def test_actual_open_basket_fills_each_symbol_at_its_own_open(tmp_path):
    execution_day = day_at(1)
    suspended, resumed = ETF_SYMBOLS[0], ETF_SYMBOLS[1]
    payload = schema_v3_fixture(8)
    make_confirmed_suspension(payload, execution_day, suspended)
    halted = make_actual_open_halt(payload, execution_day, resumed, open_price=10.5)
    sample = _v3_run(tmp_path, "basket", payload)

    execution = sample["campaign"]["execution_rules"]
    assert execution["version"] == ACTUAL_OPEN_RULES
    assert execution["execution"] == ACTUAL_OPEN_EXECUTION

    candidate, benchmark = _sides(sample["experiment"])
    filled = [symbol for symbol in ETF_SYMBOLS if symbol != suspended]
    expected_attempt = {
        "signal_date": day_at(0),
        "date": execution_day,
        "attempts": 1,
        "status": "partially_filled",
        "filled_symbols": filled,
        "unfilled": [{"symbol": suspended, "reason": "confirmed_no_trade"}],
        "basket_complete": False,
    }
    for run in (candidate, benchmark):
        assert run["order_attempts"] == [expected_attempt]
        fills = run["fills"]
        # Every symbol with an executable open matches once, at its own actual
        # open; the suspended allocation is not spent and stays in cash.
        assert [fill["symbol"] for fill in fills] == filled
        assert [fill["side"] for fill in fills] == ["buy"] * 4
        assert [fill["raw_open"] for fill in fills] == [10.5, 10.0, 10.0, 10.0]
        assert fills[0]["open_evidence"] == halted["open_source"] == OPEN_SOURCE
        assert all(fill["price_basis"] == EXECUTION_PRICE_BASIS for fill in fills)
        for fill in fills:
            assert fill["fee"] == pytest.approx(fill["notional"] * FEE_RATE)
        assert run["metrics"]["fills"] == 4
        assert run["metrics"]["fees_paid"] == pytest.approx(40.0)
        assert run["metrics"]["ending_equity"] == pytest.approx(99007.61904761905)
        assert set(run["metrics"]) == LEGACY_METRICS | {"orders_partially_filled"}
        assert run["metrics"]["orders_filled"] == 0
        assert run["metrics"]["orders_unfilled"] == 1
        assert run["metrics"]["orders_partially_filled"] == 1
        assert set(run["position_evidence"]) == LEGACY_POSITION_EVIDENCE | {
            "basket_build_state", "unfilled_basket_signals",
        }
        assert run["position_evidence"]["state"] == "initial_position_established"
        assert run["position_evidence"]["first_fill_date"] == execution_day
        assert run["position_evidence"]["basket_build_state"] == "partial"
        assert run["position_evidence"]["unfilled_basket_signals"] == 1
        # 100000 initial cash - 4 x (20000 notional + 10 fee) stays liquid.
        assert run["equity_curve"][-1]["cash"] == pytest.approx(19960.0)
        assert run["equity_curve"][-1]["nav"] == pytest.approx(99007.61904761905)
        holdings = _holdings(run, execution_day)
        assert holdings[suspended]["quantity"] == 0.0
        assert holdings[resumed]["quantity"] == pytest.approx(20_000 / 10.5)
        # The resumed halt is valued from its confirmed close, never its open.
        assert holdings[resumed]["market_value"] == pytest.approx(20_000 / 10.5 * 10.0)

    # Candidate and frozen equal-weight benchmark share the same entry point.
    assert candidate["order_attempts"] == benchmark["order_attempts"]
    assert candidate["fills"] == benchmark["fills"]
    assert candidate["equity_curve"] == benchmark["equity_curve"]
    assert candidate["metrics"] == benchmark["metrics"]
    assert candidate["position_evidence"] == benchmark["position_evidence"]

    # Change only the suspended symbol's tradability: it now trades normally at
    # its sealed open of 10, so all five symbols match. The extra parcel buys
    # 1999.0004997501248 shares worth 19990.00499750125 plus a 9.995002498750626
    # fee from the 19960 withheld cash, and funding it scales the whole target
    # by 100000/100050 = 0.9995002498750625. The ending account is:
    #   0 cash + 59970.01499250375 (three normal) + 19038.09999762024 (resumed)
    #   + 19990.00499750125 (new parcel) = 98998.11998762524,
    # i.e. 99007.61904761905 - 9.499059993812615 = 98998.11998762524.
    tradable_payload = schema_v3_fixture(8)
    make_actual_open_halt(tradable_payload, execution_day, resumed, open_price=10.5)
    tradable = _v3_run(tmp_path, "basket-tradable", tradable_payload)
    candidate, benchmark = _sides(tradable["experiment"])
    for run in (candidate, benchmark):
        assert run["order_attempts"] == [{
            "signal_date": day_at(0),
            "date": execution_day,
            "attempts": 1,
            "status": "filled",
            "filled_symbols": list(ETF_SYMBOLS),
            "unfilled": [],
            "basket_complete": True,
        }]
        assert [fill["symbol"] for fill in run["fills"]] == list(ETF_SYMBOLS)
        assert run["metrics"]["orders_filled"] == 1
        assert run["metrics"]["orders_partially_filled"] == 0
        assert run["metrics"]["fees_paid"] == pytest.approx(49.97501249375313)
        assert run["equity_curve"][-1]["cash"] == pytest.approx(0.0, abs=1e-9)
        assert run["equity_curve"][-1]["nav"] == pytest.approx(98998.11998762524)
        assert run["position_evidence"]["basket_build_state"] == "completed"
        assert run["position_evidence"]["unfilled_basket_signals"] == 0
        holdings = _holdings(run, execution_day)
        assert holdings[suspended]["quantity"] == pytest.approx(1999.0004997501248)
        assert holdings[suspended]["market_value"] == pytest.approx(19990.00499750125)
    assert candidate["order_attempts"] == benchmark["order_attempts"]
    assert candidate["fills"] == benchmark["fills"]
    assert candidate["metrics"] == benchmark["metrics"]
    assert candidate["position_evidence"] == benchmark["position_evidence"]
    delta = (
        tradable["experiment"]["discovery"]["metrics"]["ending_equity"]
        - 99007.61904761905
    )
    assert delta == pytest.approx(-9.499059993812615)


def test_actual_open_sell_then_buy_uses_realized_sell_net(tmp_path):
    entry_day, switch_day = day_at(3), day_at(4)
    seller, buyer = ETF_SYMBOLS[0], ETF_SYMBOLS[1]
    payload = _engineering_closes(schema_v3_fixture(10))
    sample = _v3_run(
        tmp_path, "switch", payload, family="momentum", parameters=MOMENTUM,
    )
    candidate, _benchmark = _sides(sample["experiment"])
    attempts = [
        attempt for attempt in candidate["order_attempts"]
        if attempt["date"] in {entry_day, switch_day}
    ]
    assert attempts == [
        {
            "signal_date": day_at(2), "date": entry_day, "attempts": 1,
            "status": "filled", "filled_symbols": [seller], "unfilled": [],
            "basket_complete": True,
        },
        {
            "signal_date": day_at(3), "date": switch_day, "attempts": 1,
            "status": "filled", "filled_symbols": [seller, buyer], "unfilled": [],
            "basket_complete": True,
        },
    ]
    entry = [
        fill for fill in candidate["fills"]
        if fill["date"] == entry_day and fill["side"] == "buy"
    ]
    assert [(fill["symbol"], fill["side"]) for fill in entry] == [(seller, "buy")]
    assert entry[0]["quantity"] == pytest.approx(10_000 / 1.0005)
    assert entry[0]["notional"] == pytest.approx(99_950.02498750624)
    assert entry[0]["fee"] == pytest.approx(49.97501249375312)
    # Cash was fully invested, so the switch sells A before it buys B.
    switch = [fill for fill in candidate["fills"] if fill["date"] == switch_day]
    assert [(fill["symbol"], fill["side"]) for fill in switch] == [
        (seller, "sell"), (buyer, "buy"),
    ]
    sell, buy = switch
    assert sell["quantity"] == pytest.approx(entry[0]["quantity"])
    assert sell["notional"] == pytest.approx(99_950.02498750624)
    assert sell["fee"] == pytest.approx(49.97501249375312)
    assert buy["raw_open"] == 10.0
    assert buy["notional"] == pytest.approx(99_850.12491255622)
    assert buy["fee"] == pytest.approx(49.92506245627811)
    # The buy debit is exactly the realized sell net: no other cash existed.
    assert buy["notional"] + buy["fee"] == pytest.approx(
        sell["notional"] - sell["fee"]
    )
    assert candidate["equity_curve"][-1]["cash"] == pytest.approx(0.0, abs=1e-9)
    assert candidate["metrics"]["ending_equity"] == pytest.approx(99_850.1249125562)
    assert candidate["metrics"]["fees_paid"] == pytest.approx(149.87508744378435)
    assert candidate["position_evidence"]["basket_build_state"] == "completed"
    assert candidate["position_evidence"]["unfilled_basket_signals"] == 0
    holdings = _holdings(candidate, switch_day)
    assert holdings[seller]["quantity"] == 0.0
    assert holdings[buyer]["quantity"] == pytest.approx(9985.012491255622)

    # A suspended seller is a held symbol without a trustworthy open, so the
    # day's equity basis is unknowable: no fill is matched at all, A keeps its
    # shares, B is never bought and no fee is charged on the switch.  The
    # priced buyer is still listed unfilled with the equity-basis reason.
    blocked_payload = _engineering_closes(schema_v3_fixture(10))
    make_confirmed_suspension(blocked_payload, switch_day, seller)
    blocked = _v3_run(
        tmp_path, "switch-blocked", blocked_payload,
        family="momentum", parameters=MOMENTUM,
    )
    blocked_run, _benchmark = _sides(blocked["experiment"])
    blocked_attempts = [
        attempt for attempt in blocked_run["order_attempts"]
        if attempt["date"] in {entry_day, switch_day}
    ]
    assert [attempt["status"] for attempt in blocked_attempts] == [
        "filled", "not_filled",
    ]
    assert blocked_attempts[1] == {
        "signal_date": day_at(3), "date": switch_day, "attempts": 1,
        "status": "not_filled", "filled_symbols": [],
        "unfilled": [
            {"symbol": seller, "reason": "confirmed_no_trade"},
            {"symbol": buyer, "reason": "equity_basis_unknown"},
        ],
        "basket_complete": False,
    }
    assert [fill["date"] for fill in blocked_run["fills"]] == [entry_day]
    assert blocked_run["equity_curve"][-1]["cash"] == pytest.approx(0.0, abs=1e-9)
    assert blocked_run["metrics"]["fees_paid"] == pytest.approx(49.97501249375312)
    assert _holdings(blocked_run, switch_day)[seller]["quantity"] == pytest.approx(
        9995.002498750624
    )
    # The last positive target produced no fill at all, so the last basket was
    # not even started: ``unfilled`` is distinct from a partial build.
    assert blocked_run["position_evidence"]["basket_build_state"] == "unfilled"
    assert blocked_run["position_evidence"]["unfilled_basket_signals"] == 1
    # The suspended session has no reliable mark; the retained shares are valued
    # from the next confirmed close only.
    assert _holdings(blocked_run, switch_day)[seller]["close"] is None
    assert blocked_run["equity_curve"][-1]["nav"] == pytest.approx(99950.02498750624)
    assert blocked_run["metrics"]["ending_equity"] == pytest.approx(99950.02498750624)


def _partial_exit_snapshot():
    held, seller = ETF_SYMBOLS[:2]
    # Nine sessions seal to a five-session Discovery interval: the entry fills
    # on its fourth session and the exit is attempted on its last one.
    payload = schema_v3_fixture(9)
    for symbol, values in (
        (held, (100.0, 100.0, 110.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0)),
        (seller, (100.0, 100.0, 105.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0)),
    ):
        for index, value in enumerate(values):
            derived_row(payload, day_at(index), symbol)["value"] = value
    for symbol in ETF_SYMBOLS[2:]:
        for index in range(9):
            derived_row(payload, day_at(index), symbol)["value"] = 100.0
    make_confirmed_suspension(payload, day_at(4), held)
    return payload


def test_actual_open_explicit_exits_execute_when_another_holding_is_unpriced(
    tmp_path,
):
    """B3: a suspended holding does not block a fully determined exit.

    The reviewer's scenario: hold A and B with both target weights at zero.  A
    is suspended for the day, so its open (and therefore the day's total
    equity) is unknown, but B's exit size is independent of total equity.  B
    sells in full at its own trusted open, cash equals the realized sell net, A
    keeps every share and is recorded with its sealed-row reason.
    """
    entry_day, exit_day = day_at(3), day_at(4)
    seller, retained = ETF_SYMBOLS[:2]
    sample = _v3_run(
        tmp_path, "explicit-exit", _partial_exit_snapshot(),
        family="momentum",
        parameters={"lookback": 2, "top_k": 2, "cash_if_negative": True},
    )
    candidate, benchmark = _sides(sample["experiment"])

    entry_attempt, exit_attempt = [
        attempt for attempt in candidate["order_attempts"]
        if attempt["date"] in {entry_day, exit_day}
    ]
    assert entry_attempt["status"] == "filled"
    assert entry_attempt["filled_symbols"] == [seller, retained]
    # The explicit exit executes; the suspended holding keeps its sealed-row
    # reason and no equity-dependent order is invented.
    assert exit_attempt == {
        "signal_date": day_at(3),
        "date": exit_day,
        "attempts": 1,
        "status": "partially_filled",
        "filled_symbols": [retained],
        "unfilled": [{"symbol": seller, "reason": "confirmed_no_trade"}],
        "basket_complete": False,
    }
    entry_fills = {
        fill["symbol"]: fill for fill in candidate["fills"] if fill["date"] == entry_day
    }
    exit_fills = [fill for fill in candidate["fills"] if fill["date"] == exit_day]
    assert [(fill["symbol"], fill["side"]) for fill in exit_fills] == [
        (retained, "sell")
    ]
    sell = exit_fills[0]
    # The whole retained parcel exits at its actual open; the fee is the frozen
    # 5 bps and cash is exactly the realized sell net.
    assert sell["quantity"] == pytest.approx(entry_fills[retained]["quantity"])
    assert sell["raw_open"] == 10.0
    assert sell["notional"] == pytest.approx(sell["quantity"] * 10.0)
    assert sell["fee"] == pytest.approx(sell["notional"] * FEE_RATE)
    realized_net = sell["notional"] - sell["fee"]

    points = {point["date"]: point for point in candidate["equity_curve"]}
    before, after = points[entry_day], points[exit_day]
    assert before["cash"] == pytest.approx(0.0, abs=1e-9)
    assert after["cash"] == pytest.approx(realized_net)
    assert after["holdings"][seller]["quantity"] == pytest.approx(
        entry_fills[seller]["quantity"]
    )
    assert after["holdings"][seller]["close"] is None
    assert after["holdings"][retained]["quantity"] == 0.0
    # The suspension has no confirmed close, so the day's NAV stays unknown.
    assert after["nav"] is None
    assert not [
        fill for fill in candidate["fills"] if fill["date"] == exit_day
        and fill["side"] == "buy"
    ]
    # The frozen equal-weight benchmark holds and never re-emits, so it has no
    # exit attempt to compare against.
    assert [
        attempt for attempt in benchmark["order_attempts"] if attempt["date"] == exit_day
    ] == []


def test_actual_open_explicit_sell_funds_single_full_target_with_unknown_equity(tmp_path):
    entry_day, switch_day = day_at(3), day_at(4)
    held, seller, buyer = ETF_SYMBOLS[:3]
    payload = _partial_exit_snapshot()
    derived_row(payload, entry_day, buyer)["value"] = 120.0
    sample = _v3_run(
        tmp_path, "cash-funded-switch", payload, family="trend_filter",
        parameters={"lookback": 2}, slippage_bps=10,
    )
    candidate, _benchmark = _sides(sample["experiment"])
    signal = candidate["signals"][-1]
    assert signal["target_weights"][held] == signal["target_weights"][seller] == 0.0
    assert signal["target_weights"][buyer] == 1.0
    assert candidate["order_attempts"][-1] == {
        "signal_date": entry_day,
        "date": switch_day,
        "attempts": 1,
        "status": "partially_filled",
        "filled_symbols": [seller, buyer],
        "unfilled": [{"symbol": held, "reason": "confirmed_no_trade"}],
        "basket_complete": False,
    }

    fills = [fill for fill in candidate["fills"] if fill["date"] == switch_day]
    assert [(fill["symbol"], fill["side"]) for fill in fills] == [
        (seller, "sell"), (buyer, "buy"),
    ]
    sell, buy = fills
    # Each initial parcel cost 50,000 including the 5 bp fee and 10 bp slippage.
    # The open is 10 for both trades. The exit realizes 49,850.22473778096 after
    # costs, buying 4,977.553654519489 shares at 10.01 plus the 5 bp fee.
    assert sell["quantity"] == pytest.approx(4992.508740634678)
    assert sell["price"] == pytest.approx(9.99)
    assert sell["fee"] == pytest.approx(24.937581159470215)
    assert sell["slippage"] == pytest.approx(49.92508740634678)
    assert buy["quantity"] == pytest.approx(4977.553654519489)
    assert buy["price"] == pytest.approx(10.01)
    assert buy["fee"] == pytest.approx(24.912656040870044)
    assert buy["slippage"] == pytest.approx(49.775536545194894)

    before, after = candidate["equity_curve"][-2:]
    assert before["cash"] == pytest.approx(0.0, abs=1e-9)
    assert after["cash"] == pytest.approx(0.0, abs=1e-9)
    assert after["holdings"][held]["quantity"] == before["holdings"][held]["quantity"]
    assert after["holdings"][seller]["quantity"] == 0.0
    assert after["holdings"][buyer]["quantity"] == pytest.approx(4977.553654519489)
    assert after["nav"] is None
    assert candidate["metrics"]["fees_paid"] == pytest.approx(99.82524969409338)
    assert candidate["position_evidence"]["basket_build_state"] == "partial"
    assert candidate["position_evidence"]["unfilled_basket_signals"] == 1


def test_actual_open_cash_capped_buy_requires_spendable_cash(tmp_path):
    entry_day, switch_day = day_at(3), day_at(4)
    held, seller, buyer = ETF_SYMBOLS[:3]
    payload = _partial_exit_snapshot()
    derived_row(payload, entry_day, buyer)["value"] = 120.0
    make_confirmed_suspension(payload, switch_day, seller)
    for symbol in (held, seller):
        # The two cash-capped entries at this price can leave rounding dust.
        raw_row(payload, entry_day, symbol).update(open=11.0, high=11.0)
    sample = _v3_run(
        tmp_path, "cash-funded-switch-blocked", payload, family="trend_filter",
        parameters={"lookback": 2}, slippage_bps=10,
    )
    candidate, _benchmark = _sides(sample["experiment"])
    before, after = candidate["equity_curve"][-2:]
    assert before["cash"] == pytest.approx(0.0, abs=1e-9)
    assert after["cash"] == before["cash"]
    assert after["holdings"][buyer]["quantity"] == 0.0
    assert [fill["date"] for fill in candidate["fills"]] == [entry_day, entry_day]
    assert candidate["order_attempts"][-1]["unfilled"] == [
        {"symbol": held, "reason": "confirmed_no_trade"},
        {"symbol": seller, "reason": "confirmed_no_trade"},
        {"symbol": buyer, "reason": "equity_basis_unknown"},
    ]


def test_actual_open_held_no_trade_sells_only_the_explicit_exit(tmp_path):
    """#56 A1: an unpriced holding forbids equity-dependent orders only.

    A whole-day halt removes the day's total equity basis, so the priced holding
    whose target still asks for a positive weight is not bought or retargeted.
    The other holding's target is an explicit exit, sized independently of
    equity: it sells in full at its open and its cash funds the next priced
    decision.  The next priced decision rebalances normally.
    """
    entry_day, frozen_day, rebalance_day = day_at(3), day_at(4), day_at(5)
    held, retained, entering = ETF_SYMBOLS[0], ETF_SYMBOLS[1], ETF_SYMBOLS[2]
    payload = schema_v3_fixture(10)
    for symbol, values in (
        (held, (100.0, 100.0, 110.0, 110.0, 110.0, 110.0, 110.0, 110.0, 110.0, 110.0)),
        (retained, (100.0, 100.0, 120.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0)),
        (entering, (100.0, 100.0, 100.0, 120.0, 120.0, 120.0, 120.0, 120.0, 120.0, 120.0)),
    ):
        for index, value in enumerate(values):
            derived_row(payload, day_at(index), symbol)["value"] = value
    # A whole-day halt is a confirmed no-trade: no executable open, but a
    # confirmed close still values the retained shares.
    make_whole_day_halt(payload, frozen_day, held)
    sample = _v3_run(
        tmp_path, "held-halt", payload,
        family="momentum",
        parameters={"lookback": 2, "top_k": 2, "cash_if_negative": False},
    )
    candidate, benchmark = _sides(sample["experiment"])

    attempts = {
        attempt["date"]: attempt
        for attempt in candidate["order_attempts"]
        if attempt["date"] in {entry_day, frozen_day}
    }
    assert list(attempts) == [entry_day, frozen_day]
    assert attempts[entry_day]["filled_symbols"] == [held, retained]
    # The unpriced holding keeps its sealed-row reason; the priced explicit exit
    # executes, and the order that depends on the unknown equity basis is
    # recorded unfilled.
    assert attempts[frozen_day] == {
        "signal_date": entry_day,
        "date": frozen_day,
        "attempts": 1,
        "status": "partially_filled",
        "filled_symbols": [retained],
        "unfilled": [
            {"symbol": held, "reason": "confirmed_no_trade"},
            {"symbol": entering, "reason": "equity_basis_unknown"},
        ],
        "basket_complete": False,
    }
    entry_fills = {
        fill["symbol"]: fill for fill in candidate["fills"] if fill["date"] == entry_day
    }
    frozen_fills = [
        fill for fill in candidate["fills"] if fill["date"] == frozen_day
    ]
    assert [(fill["symbol"], fill["side"]) for fill in frozen_fills] == [
        (retained, "sell")
    ]
    sell = frozen_fills[0]
    assert sell["quantity"] == pytest.approx(entry_fills[retained]["quantity"])
    assert sell["raw_open"] == 10.0
    assert sell["notional"] == pytest.approx(sell["quantity"] * 10.0)
    assert sell["fee"] == pytest.approx(sell["notional"] * FEE_RATE)

    points = {point["date"]: point for point in candidate["equity_curve"]}
    before, frozen = points[entry_day], points[frozen_day]
    # The explicit exit's cash is real; the halted holding keeps its parcel and
    # is marked from its confirmed close, so the day's NAV drops only by the fee.
    assert frozen["cash"] == pytest.approx(sell["notional"] - sell["fee"])
    assert frozen["nav"] == pytest.approx(before["nav"] - sell["fee"])
    assert frozen["holdings"][held] == before["holdings"][held]
    assert frozen["holdings"][held]["close"] == 10.0
    assert frozen["holdings"][retained]["quantity"] == 0.0
    assert not [fill for fill in frozen_fills if fill["side"] == "buy"]

    # The next priced decision sizes and rebalances on a known equity basis
    # again: the halted holding is sold and the new target is bought.  The
    # explicit exit already left the account, so it has no second sell here.
    rebalance = [fill for fill in candidate["fills"] if fill["date"] == rebalance_day]
    assert [(fill["side"], fill["symbol"]) for fill in rebalance] == [
        ("sell", held), ("buy", entering), ("buy", "159612"),
    ]
    assert points[rebalance_day]["cash"] == pytest.approx(0.0, abs=1e-9)
    assert points[rebalance_day]["holdings"][held]["quantity"] == 0.0
    assert points[rebalance_day]["holdings"][retained]["quantity"] == 0.0
    assert points[rebalance_day]["holdings"][entering]["quantity"] > 0
    # The candidate and the frozen benchmark share the same entry point: the
    # equal-weight benchmark holds and never re-emits, so it never freezes.
    assert [
        attempt for attempt in benchmark["order_attempts"] if attempt["date"] == frozen_day
    ] == []


def test_actual_open_unfilled_orders_do_not_retry(tmp_path):
    blocked_day, retry_day, next_signal_day = day_at(1), day_at(2), day_at(3)
    payload = schema_v3_fixture(8)
    make_whole_day_halt(payload, blocked_day, ETF_SYMBOLS[0])
    make_missing_required_open(payload, blocked_day, ETF_SYMBOLS[1])
    make_unknown_status(payload, blocked_day, ETF_SYMBOLS[2])
    make_confirmed_suspension(payload, blocked_day, ETF_SYMBOLS[3])
    make_confirmed_suspension(payload, blocked_day, ETF_SYMBOLS[4])
    reasons = {
        ETF_SYMBOLS[0]: "confirmed_no_trade",
        ETF_SYMBOLS[1]: "missing_required_open",
        ETF_SYMBOLS[2]: "unknown_trading_status",
        ETF_SYMBOLS[3]: "confirmed_no_trade",
        ETF_SYMBOLS[4]: "confirmed_no_trade",
    }
    sample = _v3_run(tmp_path, "no-retry", payload)
    candidate, benchmark = _sides(sample["experiment"])
    blocked_attempt = {
        "signal_date": day_at(0),
        "date": blocked_day,
        "attempts": 1,
        "status": "not_filled",
        "filled_symbols": [],
        "unfilled": [
            {"symbol": symbol, "reason": reasons[symbol]}
            for symbol in ETF_SYMBOLS
        ],
        "basket_complete": False,
    }
    # The equal-weight candidate keeps asking (it holds no shares), so the next
    # attempt comes from a new decision point at the next ready open, never from
    # a same-signal automatic retry.
    assert candidate["order_attempts"] == [
        blocked_attempt,
        {
            "signal_date": day_at(2), "date": next_signal_day, "attempts": 1,
            "status": "filled", "filled_symbols": list(ETF_SYMBOLS),
            "unfilled": [], "basket_complete": True,
        },
    ]
    assert [fill["date"] for fill in candidate["fills"]] == [next_signal_day] * 5
    points = {point["date"]: point for point in candidate["equity_curve"]}
    assert points[blocked_day]["cash"] == pytest.approx(100_000.0)
    assert points[retry_day]["cash"] == pytest.approx(100_000.0)
    assert candidate["metrics"]["orders_filled"] == 1
    assert candidate["metrics"]["orders_unfilled"] == 1
    assert candidate["metrics"]["orders_partially_filled"] == 0
    assert candidate["position_evidence"]["unfilled_basket_signals"] == 1
    assert candidate["position_evidence"]["basket_build_state"] == "completed"
    assert candidate["position_evidence"]["first_fill_date"] == next_signal_day

    # The frozen equal-weight benchmark submits once: its blocked basket is
    # recorded and never retried automatically, so no cash or shares move.
    assert benchmark["order_attempts"] == [blocked_attempt]
    assert benchmark["fills"] == []
    assert benchmark["equity_curve"][-1]["cash"] == pytest.approx(100_000.0)
    assert benchmark["metrics"]["orders_unfilled"] == 1
    assert benchmark["metrics"]["orders_partially_filled"] == 0
    assert benchmark["position_evidence"]["basket_build_state"] == "unfilled"
    assert benchmark["position_evidence"]["unfilled_basket_signals"] == 1

    # The legacy retry rule does reuse the blocked entry signal on the next
    # open and keeps its exact old attempt shape and result key sets.
    retry = _v3_run(
        tmp_path, "retry", payload, execution_rules="retry-initial-v1",
    )
    retried, _ = _sides(retry["experiment"])
    assert [attempt["date"] for attempt in retried["order_attempts"]] == [
        blocked_day, retry_day,
    ]
    assert retried["order_attempts"][0] == {
        "signal_date": day_at(0), "date": blocked_day, "attempts": 1,
        "status": "not_filled", "reason": "missing_or_unconfirmed_open",
        "missing_symbols": sorted(ETF_SYMBOLS),
    }
    assert retried["order_attempts"][1] == {
        "signal_date": day_at(0), "date": retry_day, "attempts": 1,
        "status": "filled",
    }
    assert set(retried["metrics"]) == LEGACY_METRICS
    assert set(retried["position_evidence"]) == LEGACY_POSITION_EVIDENCE
