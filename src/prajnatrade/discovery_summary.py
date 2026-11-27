"""Quarterly summaries derived from completed Discovery account ledgers."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date
from typing import Any

from prajnatrade.valuation import parse_instant

_DECISION_STATUSES = {"generated", "no_target", "paused", "insufficient_history"}


def summarize_signal_pauses(run: Mapping[str, Any]) -> dict[str, Any]:
    """Build a ledger-backed audit of frozen decision pauses for one account."""
    records = run.get("decision_records")
    if not isinstance(records, list):
        return {
            "status": "not_applicable",
            "decision_days": 0,
            "paused_signal_days": 0,
            "longest_consecutive_paused_days": 0,
            "events": [],
            "affected_monthly_rebalances": [],
            "unexplained_signal_gaps": list(run.get("signal_gaps") or []),
        }

    start_date, end_date = run.get("start_date"), run.get("end_date")
    decisions = [
        item for item in records
        if isinstance(item, Mapping)
        and isinstance(item.get("date"), str)
        and start_date <= item["date"] <= end_date
        and item.get("signal_status") in _DECISION_STATUSES
    ]
    decisions.sort(key=lambda item: item["date"])
    decision_dates = [item["date"] for item in decisions]
    decision_index = {day: index for index, day in enumerate(decision_dates)}
    by_date: dict[str, Mapping[str, Any]] = {}
    duplicate_dates: set[str] = set()
    for item in decisions:
        day = item["date"]
        if day in by_date:
            duplicate_dates.add(day)
        by_date[day] = item

    signals = run.get("signals") if isinstance(run.get("signals"), list) else []
    attempts = run.get("order_attempts") if isinstance(run.get("order_attempts"), list) else []
    fills = run.get("fills") if isinstance(run.get("fills"), list) else []
    curve = run.get("equity_curve") if isinstance(run.get("equity_curve"), list) else []
    valuation_gaps = (
        run.get("gaps", {}).get("valuation", [])
        if isinstance(run.get("gaps"), Mapping) else []
    )
    signal_gaps = run.get("signal_gaps") if isinstance(run.get("signal_gaps"), list) else []
    paused_records = [item for item in decisions if item.get("signal_status") == "paused"]
    paused_dates = {item["date"] for item in paused_records}
    known_no_trade_sessions = []
    known_no_trade_verified = True
    for decision in decisions:
        cutoff = parse_instant(decision.get("signal_cutoff"))
        confirmed_rows = decision.get("confirmed_no_trade")
        for item in confirmed_rows if isinstance(confirmed_rows, list) else []:
            if not isinstance(item, Mapping):
                known_no_trade_verified = False
                continue
            available_at = parse_instant(item.get("evidence_available_at"))
            valid = (
                cutoff is not None
                and available_at is not None
                and available_at <= cutoff
                and item.get("trading_status") == "suspended"
                and isinstance(item.get("status_evidence_id"), str)
                and bool(item["status_evidence_id"].strip())
                and isinstance(item.get("availability_evidence"), str)
                and bool(item["availability_evidence"].strip())
            )
            known_no_trade_verified = known_no_trade_verified and valid
            known_no_trade_sessions.append(
                {
                    "date": decision["date"],
                    "symbol": item.get("symbol"),
                    "reason": item.get("reason"),
                    "decision_cutoff": decision.get("signal_cutoff"),
                    "evidence_available_at": item.get("evidence_available_at"),
                    "status_evidence_id": item.get("status_evidence_id"),
                    "verified_before_cutoff": valid,
                }
            )
    pause_gaps = {
        item["date"]: item.get("signal_pause")
        for item in paused_records
        if isinstance(item.get("signal_pause"), Mapping)
    }
    unexplained_gaps = [
        item for item in signal_gaps
        if not isinstance(item, Mapping)
        or item.get("date") not in pause_gaps
        or item != pause_gaps.get(item.get("date"))
    ]
    gap_counts: dict[str, int] = {}
    for item in signal_gaps:
        if isinstance(item, Mapping):
            gap_counts[item.get("date")] = gap_counts.get(item.get("date"), 0) + 1
    for day in paused_dates:
        if gap_counts.get(day) != 1:
            unexplained_gaps.append(
                {"date": day, "reason": "pause_gap_ledger_mismatch"}
            )

    events: list[dict[str, Any]] = []
    for decision in paused_records:
        day = decision["date"]
        pause = decision.get("signal_pause")
        if not isinstance(pause, Mapping):
            pause = {}
        reasons = pause.get("reasons") if isinstance(pause.get("reasons"), list) else []
        evidence = pause.get("evidence") if isinstance(pause.get("evidence"), list) else []
        reason_symbols = [
            item.get("symbol") for item in reasons if isinstance(item, Mapping)
        ]
        evidence_symbols = [
            item.get("symbol") for item in evidence if isinstance(item, Mapping)
        ]
        explanations_hold = not (
            pause.get("date") != day
            or len(reason_symbols) != len(reasons)
            or len(evidence_symbols) != len(evidence)
            or len(reason_symbols) != len(evidence_symbols)
            or not all(isinstance(symbol, str) for symbol in reason_symbols + evidence_symbols)
            or len(set(reason_symbols)) != len(reason_symbols)
            or len(set(evidence_symbols)) != len(evidence_symbols)
            or not isinstance(pause.get("symbols"), list)
            or sorted(reason_symbols) != pause.get("symbols")
            or set(reason_symbols) != set(evidence_symbols)
        )
        evidence_by_symbol = {
            item.get("symbol"): item
            for item in evidence
            if isinstance(item, Mapping)
        }
        rendered_reasons = []
        for reason in reasons:
            if not isinstance(reason, Mapping):
                explanations_hold = False
                continue
            symbol = reason.get("symbol")
            code = reason.get("reason")
            source = evidence_by_symbol.get(symbol, {})
            available_at = source.get("evidence_available_at")
            cutoff = parse_instant(decision.get("signal_cutoff"))
            available = parse_instant(available_at)
            end_of_day = cutoff.replace(hour=23, minute=59, second=59, microsecond=0) if cutoff else None
            late_and_reviewable = (
                available is not None
                and cutoff is not None
                and end_of_day is not None
                and cutoff < available <= end_of_day
                and isinstance(source.get("availability_evidence"), str)
                and bool(source["availability_evidence"].strip())
            )
            reason_is_explainable = (
                source.get("reason") == code
                and code == "non_trading_status:suspended"
                and source.get("source") == "trading_status"
                and source.get("trading_status") == "suspended"
                and isinstance(source.get("status_evidence_id"), str)
                and bool(source["status_evidence_id"].strip())
                and late_and_reviewable
            ) or (
                source.get("reason") == code
                and code == "close_after_cutoff"
                and source.get("source") == "raw_close"
                and late_and_reviewable
            ) or (
                source.get("reason") == code
                and code == "derived_after_cutoff"
                and source.get("source") == "derived"
                and late_and_reviewable
            )
            explanations_hold = explanations_hold and reason_is_explainable
            rendered_reasons.append(
                {
                    "symbol": symbol,
                    "reason": code,
                    "evidence_available_at": available_at,
                }
            )

        generated = [item for item in signals if item.get("date") == day]
        expected_orders = [
            item for item in signals if item.get("execution_date") == day
        ]
        daily_attempts = [item for item in attempts if item.get("date") == day]
        order_processing_holds = (
            sorted(item.get("date") for item in expected_orders)
            == sorted(item.get("signal_date") for item in daily_attempts)
            and all(item.get("signal_date") < day for item in daily_attempts)
        )
        daily_fills = [item for item in fills if item.get("date") == day]
        account_points = [
            item for item in curve
            if isinstance(item, Mapping) and item.get("date") == day
        ]
        point = account_points[0] if len(account_points) == 1 else {}
        cash = point.get("cash")
        market_value = point.get("market_value")
        receivables = point.get("dividend_receivables")
        nav = point.get("nav")
        holdings = point.get("holdings")
        holding_market_values = (
            [
                holding.get("market_value")
                for holding in holdings.values()
                if isinstance(holding, Mapping)
            ]
            if isinstance(holdings, Mapping) else []
        )
        nav_components_valid = all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            for value in (cash, market_value, receivables, nav)
        ) and len(holding_market_values) == len(holdings or {}) and all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            for value in holding_market_values
        )
        recomputed_market_value = (
            math.fsum(holding_market_values) if nav_components_valid else None
        )
        recomputed_nav = (
            cash + recomputed_market_value + receivables
            if nav_components_valid else None
        )
        day_valuation_gaps = [
            item for item in valuation_gaps
            if isinstance(item, Mapping) and item.get("date") == day
        ]
        nav_reconciled = (
            nav_components_valid
            and not day_valuation_gaps
            and math.isclose(
                market_value, recomputed_market_value, rel_tol=0.0, abs_tol=1e-8,
            )
            and math.isclose(nav, recomputed_nav, rel_tol=0.0, abs_tol=1e-8)
        )
        events.append(
            {
                "date": day,
                "decision_cutoff": decision.get("signal_cutoff"),
                "reasons": rendered_reasons,
                "evidence": [dict(item) for item in evidence if isinstance(item, Mapping)],
                "new_target_generated": bool(generated),
                "prior_orders": {
                    "expected_signal_dates": sorted(item.get("date") for item in expected_orders),
                    "attempts": [dict(item) for item in daily_attempts],
                    "fills": [dict(item) for item in daily_fills],
                    "processing_verified": order_processing_holds,
                },
                "nav": nav,
                "cash": cash,
                "market_value": market_value,
                "holdings": dict(holdings) if isinstance(holdings, Mapping) else {},
                "recomputed_market_value": recomputed_market_value,
                "dividend_receivables": receivables,
                "recomputed_nav": recomputed_nav,
                "nav_reconciled": nav_reconciled,
                "valuation_gaps": day_valuation_gaps,
                "rule_compliant": (
                    day not in duplicate_dates
                    and not generated
                    and bool(reasons)
                    and explanations_hold
                    and gap_counts.get(day) == 1
                    and any(item == pause for item in signal_gaps)
                    and order_processing_holds
                    and nav_reconciled
                ),
            }
        )

    paused_indices = sorted(decision_index[day] for day in paused_dates if day in decision_index)
    longest = current = 0
    previous = None
    for index in paused_indices:
        current = current + 1 if previous is not None and index == previous + 1 else 1
        longest = max(longest, current)
        previous = index

    monthly_deferrals = run.get("monthly_deferrals")
    affected_rebalances = [
        {
            key: item[key]
            for key in (
                "cycle", "scheduled_date", "actual_signal_date", "execution_date",
                "status", "deferral_reason",
            )
            if key in item
        }
        for item in (monthly_deferrals if isinstance(monthly_deferrals, list) else [])
        if isinstance(item, Mapping)
        and isinstance(item.get("scheduled_date"), str)
        and start_date <= item["scheduled_date"] <= end_date
    ]
    return {
        "status": (
            "verified"
            if (
                all(item["rule_compliant"] for item in events)
                and not unexplained_gaps
                and known_no_trade_verified
            )
            else "insufficient_evidence"
        ),
        "decision_days": len(decisions),
        "decision_dates": decision_dates,
        "paused_signal_days": len(paused_dates),
        "longest_consecutive_paused_days": longest,
        "events": events,
        "known_no_trade_sessions": known_no_trade_sessions,
        "affected_monthly_rebalances": affected_rebalances,
        "unexplained_signal_gaps": unexplained_gaps,
    }

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

    result = {
        "schema_version": 1,
        "identity": dict(identity),
        "daily_metrics": {
            "candidate": _daily_metrics(candidate, candidate_curve),
            "benchmark": _daily_metrics(benchmark, benchmark_curve),
        },
        "quarters": quarters,
    }
    if "decision_records" in candidate or "decision_records" in benchmark:
        result["signal_pauses"] = {
            "candidate": summarize_signal_pauses(candidate),
            "benchmark": summarize_signal_pauses(benchmark),
        }
    return result


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
        entry: dict[str, Any] = {"date": raw_date, "nav": nav}
        if "valuation_basis" in point:
            entry["valuation_basis"] = point["valuation_basis"]
            carried: dict[str, Any] = {}
            holdings = point.get("holdings")
            if isinstance(holdings, Mapping):
                for symbol in sorted(holdings):
                    holding = holdings[symbol]
                    if (
                        isinstance(holding, Mapping)
                        and holding.get("valuation_basis") == "carried_forward_prior_close"
                        and isinstance(holding.get("carry_forward"), Mapping)
                    ):
                        carried[symbol] = dict(holding["carry_forward"])
            entry["ending_carried_forward"] = carried
        curve.append(entry)
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
    valuation = None
    if any("valuation_basis" in point for point in points):
        valuation = {
            "actual_close_sessions": sum(
                point["nav"] is not None and point.get("valuation_basis") == "actual_close"
                for point in points
            ),
            "carried_forward_sessions": sum(
                point["nav"] is not None and point.get("valuation_basis") == "carried_forward"
                for point in points
            ),
            "missing_valuation_sessions": sum(point["nav"] is None for point in points),
            "ending_carried_forward": dict(points[-1].get("ending_carried_forward", {})),
        }
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
        **({"valuation": valuation} if valuation is not None else {}),
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
