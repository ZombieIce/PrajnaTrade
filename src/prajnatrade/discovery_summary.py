"""Quarterly summaries derived from completed Discovery account ledgers."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date
from typing import Any


def summarize_discovery_quarters(
    candidate: Mapping[str, Any],
    benchmark: Mapping[str, Any],
    *,
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Summarize aligned candidate and benchmark ledgers by calendar quarter."""
    candidate_curve = _curve(candidate, "candidate")
    benchmark_curve = _curve(benchmark, "benchmark")
    dates = [point["date"] for point in candidate_curve]
    if dates != [point["date"] for point in benchmark_curve]:
        raise ValueError("Candidate and benchmark equity curves must share trading dates.")
    session_dates = set(dates)
    for label, run, curve in (
        ("candidate", candidate, candidate_curve),
        ("benchmark", benchmark, benchmark_curve),
    ):
        if run.get("start_date") != curve[0]["date"] or run.get("end_date") != curve[-1]["date"]:
            raise ValueError(f"{label} declared interval must match its equity curve.")

    initial_cash = _finite_number(candidate.get("initial_cash"), "candidate initial cash")
    benchmark_cash = _finite_number(benchmark.get("initial_cash"), "benchmark initial cash")
    if initial_cash != benchmark_cash:
        raise ValueError("Candidate and benchmark must share initial cash.")

    quarters: list[dict[str, Any]] = []
    start = 0
    while start < len(dates):
        quarter_date = date.fromisoformat(dates[start])
        quarter = f"{quarter_date.year}-Q{(quarter_date.month - 1) // 3 + 1}"
        end = start + 1
        while end < len(dates):
            current = date.fromisoformat(dates[end])
            if f"{current.year}-Q{(current.month - 1) // 3 + 1}" != quarter:
                break
            end += 1

        prior_nav = initial_cash if start == 0 else candidate_curve[start - 1]["nav"]
        benchmark_prior_nav = benchmark_cash if start == 0 else benchmark_curve[start - 1]["nav"]
        candidate_section = _quarter_side(
            candidate,
            candidate_curve[start:end],
            prior_nav,
            session_dates,
        )
        benchmark_section = _quarter_side(
            benchmark,
            benchmark_curve[start:end],
            benchmark_prior_nav,
            session_dates,
        )
        candidate_return = candidate_section["net_return"]
        benchmark_return = benchmark_section["net_return"]
        missing_reasons = {}
        excess_return = None
        if candidate_return is not None and benchmark_return is not None:
            excess_return = candidate_return - benchmark_return
        else:
            missing_reasons["excess_return"] = "candidate_or_benchmark_return_unavailable"
        quarters.append(
            {
                "quarter": quarter,
                "start_date": dates[start],
                "end_date": dates[end - 1],
                "expected_trading_sessions": end - start,
                "candidate": candidate_section,
                "benchmark": benchmark_section,
                "excess_return": excess_return,
                **({"missing_reasons": missing_reasons} if missing_reasons else {}),
            }
        )
        start = end

    return {
        "schema_version": 1,
        "identity": dict(identity),
        "daily_metrics": {
            "candidate": _daily_metrics(candidate, candidate_curve),
            "benchmark": _daily_metrics(benchmark, benchmark_curve),
        },
        "quarters": quarters,
    }


def _curve(run: Mapping[str, Any], label: str) -> list[dict[str, Any]]:
    raw_curve = run.get("equity_curve")
    if not isinstance(raw_curve, list) or not raw_curve:
        raise ValueError(f"{label} equity curve must contain at least one session.")
    curve: list[dict[str, Any]] = []
    previous_date: str | None = None
    for point in raw_curve:
        if not isinstance(point, Mapping):
            raise ValueError(f"{label} equity curve contains an invalid point.")
        raw_date = point.get("date")
        if not isinstance(raw_date, str):
            raise ValueError(f"{label} equity curve contains a point without a date.")
        date.fromisoformat(raw_date)
        if previous_date is not None and raw_date <= previous_date:
            raise ValueError(f"{label} equity curve dates must be strictly increasing.")
        nav = point.get("nav")
        if nav is not None:
            nav = _finite_number(nav, f"{label} NAV for {raw_date}")
            if nav <= 0:
                raise ValueError(f"{label} NAV for {raw_date} must be positive.")
        curve.append({"date": raw_date, "nav": nav})
        previous_date = raw_date
    metrics = run.get("metrics")
    if isinstance(metrics, Mapping):
        sessions = metrics.get("sessions")
        if sessions is not None and sessions != len(curve):
            raise ValueError(f"{label} equity curve must retain every trading session.")
    return curve


def _quarter_side(
    run: Mapping[str, Any],
    points: list[dict[str, Any]],
    prior_nav: float | None,
    session_dates: set[str],
) -> dict[str, Any]:
    navs = [point["nav"] for point in points]
    end_nav = navs[-1]
    missing_reasons: dict[str, str] = {}
    net_return = None
    if prior_nav is None:
        missing_reasons["net_return"] = "missing_start_nav"
    elif end_nav is None:
        missing_reasons["net_return"] = "missing_end_nav"
    else:
        net_return = end_nav / prior_nav - 1.0

    max_drawdown = None
    if any(nav is None for nav in navs):
        missing_reasons["max_drawdown"] = "missing_nav_observation"
    elif prior_nav is None:
        missing_reasons["max_drawdown"] = "missing_start_nav"
    else:
        path = [prior_nav, *(float(nav) for nav in navs)]
        peak = path[0]
        max_drawdown = 0.0
        for nav in path[1:]:
            peak = max(peak, nav)
            max_drawdown = min(max_drawdown, nav / peak - 1.0)

    start_date = points[0]["date"]
    end_date = points[-1]["date"]
    first_session = start_date == run["start_date"]
    expected_daily_returns = len(points) - 1 + int(not first_session)
    observed_daily_returns = 0
    if not first_session and prior_nav is not None and navs[0] is not None:
        observed_daily_returns += 1
    observed_daily_returns += sum(
        left is not None and right is not None for left, right in zip(navs, navs[1:], strict=False)
    )
    daily_return_coverage = {
        "observed_adjacent_returns": observed_daily_returns,
        "expected_adjacent_returns": expected_daily_returns,
        "fraction": (
            observed_daily_returns / expected_daily_returns if expected_daily_returns else None
        ),
    }
    if observed_daily_returns < expected_daily_returns:
        missing_reasons["daily_returns"] = "missing_adjacent_nav_pair"

    fills = run.get("fills")
    if not isinstance(fills, list):
        raise ValueError("Discovery run fills must be a list.")
    segment_fills = []
    for fill in fills:
        if not isinstance(fill, Mapping):
            raise ValueError("Discovery run contains an invalid fill.")
        fill_date = fill.get("date")
        if not isinstance(fill_date, str) or fill_date not in session_dates:
            raise ValueError("Discovery fill date must match an equity-curve session.")
        if start_date <= fill_date <= end_date:
            segment_fills.append(fill)
    fees_paid = math.fsum(
        _finite_number(fill.get("fee"), f"fill fee for {fill.get('date')}")
        for fill in segment_fills
    )
    valued_sessions = sum(nav is not None for nav in navs)
    expected_sessions = len(points)
    return {
        "net_return": net_return,
        "max_drawdown": max_drawdown,
        "fees_paid": fees_paid,
        "fills": len(segment_fills),
        "coverage": {
            "valued_sessions": valued_sessions,
            "expected_sessions": expected_sessions,
            "fraction": valued_sessions / expected_sessions,
        },
        "daily_return_coverage": daily_return_coverage,
        **({"missing_reasons": missing_reasons} if missing_reasons else {}),
    }


def _daily_metrics(run: Mapping[str, Any], curve: list[dict[str, Any]]) -> dict[str, Any]:
    observed = sum(
        left["nav"] is not None and right["nav"] is not None
        for left, right in zip(curve, curve[1:], strict=False)
    )
    expected = max(0, len(curve) - 1)
    coverage = {
        "observed_adjacent_returns": observed,
        "expected_adjacent_returns": expected,
        "fraction": observed / expected if expected else None,
    }
    metrics = run.get("metrics", {})
    if not isinstance(metrics, Mapping):
        raise ValueError("Discovery run metrics must be an object.")
    sharpe = metrics.get("sharpe")
    missing_reasons = {}
    if observed < expected:
        missing_reasons["daily_returns"] = "missing_adjacent_nav_pair"
    if sharpe is None:
        missing_reasons["sharpe"] = (
            "insufficient_adjacent_returns" if observed < 2 else "zero_variance_returns"
        )
    return {
        "sharpe": sharpe,
        "coverage": coverage,
        **({"missing_reasons": missing_reasons} if missing_reasons else {}),
    }


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number.")
    return float(value)
