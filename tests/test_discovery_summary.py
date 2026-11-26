from __future__ import annotations

from typing import Any

import pytest

from prajnatrade.discovery_summary import summarize_discovery_quarters

IDENTITY = {
    "strategy_and_parameters": "strategy-hash",
    "snapshot": "snapshot-hash",
    "partition": "partition-hash",
    "evaluation": "evaluation-hash",
}


def _run(
    navs: list[tuple[str, float | None]],
    *,
    fills: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "initial_cash": 100.0,
        "start_date": navs[0][0],
        "end_date": navs[-1][0],
        "equity_curve": [
            {"date": day, "nav": nav, "cash": nav, "market_value": 0} for day, nav in navs
        ],
        "fills": fills or [],
    }


def test_quarter_summary_carries_account_and_season_start_drawdown() -> None:
    dates = [
        ("2024-03-29", 100.0),
        ("2024-04-01", 98.0),
        ("2024-06-28", 105.0),
    ]
    candidate = _run(
        dates,
        fills=[{"date": "2024-04-01", "fee": 0.5}],
    )
    benchmark = _run(
        [("2024-03-29", 100.0), ("2024-04-01", 100.0), ("2024-06-28", 102.0)],
        fills=[{"date": "2024-04-01", "fee": 0.25}],
    )

    summary = summarize_discovery_quarters(candidate, benchmark, identity=IDENTITY)

    assert summary["identity"] == IDENTITY
    assert [quarter["quarter"] for quarter in summary["quarters"]] == ["2024-Q1", "2024-Q2"]
    first = summary["quarters"][0]["candidate"]
    assert first["net_return"] == 0
    assert first["fills"] == 0
    assert first["fees_paid"] == 0
    second = summary["quarters"][1]
    assert (second["start_date"], second["end_date"]) == ("2024-04-01", "2024-06-28")
    assert second["expected_trading_sessions"] == 2
    assert second["candidate"]["net_return"] == pytest.approx(0.05)
    assert second["candidate"]["max_drawdown"] == pytest.approx(-0.02)
    assert second["candidate"]["fees_paid"] == pytest.approx(0.5)
    assert second["candidate"]["fills"] == 1
    assert second["candidate"]["coverage"] == {
        "valued_sessions": 2,
        "expected_sessions": 2,
        "fraction": 1.0,
    }
    assert second["candidate"]["daily_return_coverage"] == {
        "observed_adjacent_returns": 2,
        "expected_adjacent_returns": 2,
        "fraction": 1.0,
    }
    assert second["benchmark"]["net_return"] == pytest.approx(0.02)
    assert second["excess_return"] == pytest.approx(0.03)


def test_quarter_summary_reports_missing_end_nav_without_shifting_dates() -> None:
    dates = [("2024-03-29", 100.0), ("2024-04-01", 98.0), ("2024-04-02", None)]

    summary = summarize_discovery_quarters(
        _run(dates),
        _run(dates),
        identity=IDENTITY,
    )

    quarter = summary["quarters"][1]
    assert (quarter["start_date"], quarter["end_date"]) == ("2024-04-01", "2024-04-02")
    assert quarter["expected_trading_sessions"] == 2
    assert quarter["candidate"]["net_return"] is None
    assert quarter["candidate"]["missing_reasons"]["net_return"] == "missing_end_nav"
    assert quarter["candidate"]["missing_reasons"]["daily_returns"] == "missing_adjacent_nav_pair"
    assert quarter["candidate"]["coverage"] == {
        "valued_sessions": 1,
        "expected_sessions": 2,
        "fraction": 0.5,
    }


def test_quarter_summary_reports_missing_prior_close_without_shifting_start() -> None:
    dates = [
        ("2024-03-29", None),
        ("2024-04-01", 98.0),
        ("2024-04-02", 105.0),
    ]

    summary = summarize_discovery_quarters(
        _run(dates),
        _run(dates),
        identity=IDENTITY,
    )

    quarter = summary["quarters"][1]
    assert (quarter["start_date"], quarter["end_date"]) == ("2024-04-01", "2024-04-02")
    assert quarter["candidate"]["net_return"] is None
    assert quarter["candidate"]["missing_reasons"]["net_return"] == "missing_start_nav"
    assert quarter["candidate"]["coverage"]["valued_sessions"] == 2
