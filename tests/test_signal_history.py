from datetime import date, timedelta

from prajnatrade.data import ETF_SYMBOLS
from prajnatrade.snapshot_baseline import (
    RETRY_INITIAL_RULES,
    first_open_history,
    first_ready_open,
    run_discovery_equal_weight,
    run_partition_strategy,
)


def _sample(sessions: int, *, prior_sessions: int = 0) -> tuple[dict, tuple[list[str], list[dict]]]:
    dates = [
        (date(2024, 1, 1) + timedelta(days=index)).isoformat()
        for index in range(prior_sessions + sessions)
    ]
    trading_dates = dates[prior_sessions:]
    rows = [
        {
            "date": day,
            "symbol": symbol,
            "value": 10.0,
            "available_at": f"{day}T15:00:00+08:00",
            "availability_evidence": "synthetic derived close",
        }
        for day in dates
        for symbol in ETF_SYMBOLS
    ]
    return {
        "calendar": [{"date": day} for day in trading_dates],
        "derived": rows[prior_sessions * len(ETF_SYMBOLS):],
        "corporate_actions": [],
        "raw_bars": [
            {
                "date": day,
                "symbol": symbol,
                "open": 10,
                "close": 10,
                "volume": 100,
                "trading_status": "trading",
                "available_at": f"{day}T09:30:00+08:00",
                "availability_evidence": "synthetic trade",
            }
            for day in trading_dates
            for symbol in ETF_SYMBOLS
        ],
    }, (dates[:prior_sessions], rows[:prior_sessions * len(ETF_SYMBOLS)])


def _requirement(observations: int, mode: str = "valid_observations") -> list[dict]:
    return [{"id": "momentum", "observations": observations, "mode": mode}]


def _run(
    data, prior_history, strategy, requirements, *, partition_name="final_holdout", **kwargs,
) -> dict:
    return run_partition_strategy(
        data,
        "synthetic-snapshot",
        "synthetic-partition",
        strategy,
        partition_name=partition_name,
        signal_window_sessions=max((item["observations"] for item in requirements), default=1),
        history_requirements=requirements,
        prior_history=prior_history,
        initial_cash=5_000,
        transaction_cost_bps=0,
        **kwargs,
    )


def test_first_scoring_open_can_trade_from_authorized_completed_history() -> None:
    data, prior = _sample(3, prior_sessions=60)

    def buy(history, state):
        if state["cash"] == 5_000 and (history["close"] == 10).all():
            return {ETF_SYMBOLS[0]: 1.0}
        return None

    result = _run(data, prior, buy, _requirement(60))
    for row in data["derived"]:
        row["value"] = 99
    revised = _run(data, prior, buy, _requirement(60))

    assert result["start_date"] == "2024-03-01"
    assert result["signals"][0] == revised["signals"][0]
    assert result["signals"][0]["date"] == "2024-03-01"
    assert result["signals"][0]["decision_date"] == "2024-03-01"
    assert result["signals"][0]["window"] == prior[0]
    assert result["fills"][0]["date"] == "2024-03-01"
    assert result["fills"][0]["quantity"] == 500
    assert result["history_readiness"]["first_ready_date"] == "2024-03-01"
    assert [point["date"] for point in result["equity_curve"]] == [
        "2024-03-01", "2024-03-02", "2024-03-03",
    ]


def test_research_window_exposes_dates_selected_for_each_indicator() -> None:
    data, prior = _sample(1, prior_sessions=4)
    # The first scored open can use January 4, or reach past January 3
    # for the second valid observation.
    prior[1][2 * len(ETF_SYMBOLS)]["value"] = None
    requirements = [
        {"id": "valid", "observations": 2, "mode": "valid_observations"},
        {"id": "consecutive", "observations": 1, "mode": "consecutive_sessions"},
    ]

    def buy(history, state):
        assert history["date"].unique().tolist() == ["2024-01-02", "2024-01-04"]
        assert state["history_windows"] == {
            "valid": ["2024-01-02", "2024-01-04"],
            "consecutive": ["2024-01-04"],
        }
        state["history_windows"]["valid"].clear()
        return {ETF_SYMBOLS[0]: 1.0}

    result = _run(data, prior, buy, requirements)
    assert result["signals"][0]["history_windows"] == {
        "valid": ["2024-01-02", "2024-01-04"],
        "consecutive": ["2024-01-04"],
    }


def test_suspension_separates_optional_older_observation_from_consecutive_gap() -> None:
    data, prior = _sample(1, prior_sessions=61)
    suspended = prior[1][10 * len(ETF_SYMBOLS)]
    suspended["value"] = None
    scoring_day = data["calendar"][0]["date"]

    def wait(history, state):
        return None

    valid_window = _requirement(60)
    consecutive_window = _requirement(60, "consecutive_sessions")
    valid = _run(data, prior, wait, valid_window)
    consecutive = _run(data, prior, wait, consecutive_window)

    assert valid["signal_gaps"] == []
    assert valid["position_evidence"]["state"] == "intentional_cash"
    assert valid["history_readiness"]["ready_cash_sessions"] == [scoring_day]
    assert valid["history_readiness"]["first_scored_open"]["ready"]
    assert consecutive["position_evidence"]["state"] == "no_position_signal"
    assert consecutive["history_readiness"]["ready_cash_sessions"] == []
    assert consecutive["history_readiness"]["first_ready_date"] is None
    assert consecutive["history_readiness"]["first_scored_open"] == {
        "decision_date": scoring_day,
        "ready": False,
        "requirements": [
            {
                "id": "momentum",
                "mode": "consecutive_sessions",
                "required_observations": 60,
                "available_observations": 59,
                "missing_sessions": [
                    {"date": "2024-01-11", "missing_symbols": [ETF_SYMBOLS[0]]},
                ],
            },
        ],
    }
    assert first_open_history(scoring_day, prior, consecutive_window) == (
        consecutive["history_readiness"]["first_scored_open"]
    )


def test_late_previous_close_cannot_change_first_open_signal() -> None:
    data, prior = _sample(2, prior_sessions=3)
    late_close = prior[1][2 * len(ETF_SYMBOLS)]
    late_close["available_at"] = "2024-01-04T09:31:00+08:00"

    def buy(history, state):
        return {ETF_SYMBOLS[0]: 1.0} if (history["close"] == 10).all() else None

    requirements = _requirement(2)
    original = _run(data, prior, buy, requirements)
    late_close["value"] = 99
    revised = _run(data, prior, buy, requirements)
    assert original["signals"][0] == revised["signals"][0]
    assert revised["signals"][0]["window"] == ["2024-01-01", "2024-01-02"]


def test_validation_waiting_cash_and_all_218_scoring_sessions_count() -> None:
    data, prior = _sample(218, prior_sessions=60)
    full_weight = {ETF_SYMBOLS[0]: 1.0}

    def buy_on_tenth_open(history, state):
        if history["date"].max() == "2024-03-09":
            return full_weight
        return None

    short = _run(
        data, prior, lambda *_: full_weight, _requirement(20), partition_name="validation",
    )
    long = _run(
        data, prior, buy_on_tenth_open, _requirement(60), partition_name="validation",
    )
    baseline = _run(data, prior, lambda *_: full_weight, [], partition_name="validation")

    for participant in (short, long, baseline):
        assert participant["start_date"] == "2024-03-01"
        assert participant["initial_cash"] == 5_000
        assert participant["metrics"]["sessions"] == 218
        assert participant["metrics"]["valued_sessions"] == 218
        assert participant["history_readiness"]["first_ready_date"] == "2024-03-01"
    assert long["position_evidence"]["first_position_signal_date"] == "2024-03-10"
    assert long["position_evidence"]["first_fill_date"] == "2024-03-10"
    assert [point["nav"] for point in long["equity_curve"][:9]] == [5_000] * 9
    assert long["history_readiness"]["ready_cash_sessions"] == [
        f"2024-03-0{day}" for day in range(1, 10)
    ]


def test_discovery_fixed_start_has_full_cash_and_no_warmup_positions() -> None:
    data, prior = _sample(61)
    scoring_start = first_ready_open(
        data, history_requirements=_requirement(20) + _requirement(60),
    )
    assert scoring_start == "2024-03-01"

    def buy(history, state):
        assert state["cash"] == 5_000
        assert all(quantity == 0 for quantity in state["shares"].values())
        return {ETF_SYMBOLS[0]: 1.0}

    short = _run(
        data, prior, buy, _requirement(20), partition_name="discovery", scoring_start=scoring_start,
    )
    baseline = run_discovery_equal_weight(
        data, "synthetic-snapshot", "synthetic-partition",
        initial_cash=5_000, transaction_cost_bps=0, slippage_bps=0,
        history_requirements=[], scoring_start=scoring_start,
    )

    for participant in (short, baseline):
        assert participant["start_date"] == "2024-03-01"
        assert participant["metrics"]["sessions"] == 1
        assert participant["signals"][0]["decision_date"] == "2024-03-01"
        assert participant["fills"][0]["date"] == "2024-03-01"
        assert participant["equity_curve"][0]["nav"] == 5_000


def test_required_history_gap_during_scoring_keeps_fixed_start_and_nav_dates() -> None:
    data, prior = _sample(3, prior_sessions=2)
    late_close = data["derived"][0]
    late_close["available_at"] = "2024-01-04T09:31:00+08:00"
    requirements = _requirement(2, "consecutive_sessions")

    result = _run(data, prior, lambda *_: None, requirements, scoring_start="2024-01-03")

    assert result["start_date"] == "2024-01-03"
    assert result["history_readiness"]["first_ready_date"] == "2024-01-03"
    assert result["history_readiness"]["ready_cash_sessions"] == ["2024-01-03", "2024-01-05"]
    assert result["history_readiness"]["gaps"][0]["decision_date"] == "2024-01-04"
    assert result["signal_gaps"][0]["missing_sessions"] == [
        {"date": "2024-01-03", "missing_symbols": [ETF_SYMBOLS[0]]},
    ]
    assert [point["date"] for point in result["equity_curve"]] == [
        "2024-01-03", "2024-01-04", "2024-01-05",
    ]
    assert first_ready_open(
        data, history_requirements=requirements, prior_history=prior, scoring_start="2024-01-04",
    ) == "2024-01-05"


def test_first_scoring_open_order_failure_respects_frozen_execution_rules() -> None:
    data, prior = _sample(2, prior_sessions=1)
    data["raw_bars"][0]["open"] = None
    requirements = _requirement(1)

    def buy_once(history, state):
        if history["date"].max() == "2024-01-01":
            return {ETF_SYMBOLS[0]: 1.0}
        return None

    next_open = _run(data, prior, buy_once, requirements)
    retry_initial = _run(
        data, prior, buy_once, requirements, execution_rule_version=RETRY_INITIAL_RULES,
    )

    assert next_open["history_readiness"]["first_scored_open"]["ready"]
    assert next_open["position_evidence"]["state"] == "initial_position_not_established"
    assert next_open["order_attempts"][0]["date"] == "2024-01-02"
    assert next_open["order_attempts"][0]["status"] == "not_filled"
    assert next_open["fills"] == []

    assert retry_initial["position_evidence"]["first_position_signal_date"] == "2024-01-02"
    assert retry_initial["position_evidence"]["first_fill_date"] == "2024-01-03"
    assert [attempt["signal_date"] for attempt in retry_initial["order_attempts"]] == [
        "2024-01-02", "2024-01-02",
    ]
