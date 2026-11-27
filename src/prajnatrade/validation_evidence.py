"""Interpret verified candidate and benchmark Validation evidence.

Callers own evidence binding, read gates, verdicts and receipt publication.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from prajnatrade.discovery_summary import summarize_signal_pauses

_POSITION_STATES = {"initial_position_established", "intentional_cash"}
_UNFINISHED_BUILD_STATES = {"partial", "unfilled"}


def _gap_summary(gaps: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(gaps),
        "dates": [item.get("date") for item in gaps],
        "first": gaps[0] if gaps else None,
    }


def _position_summary(
    run: Mapping[str, Any], policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    evidence = run["position_evidence"]
    readiness = run.get("history_readiness") or {}
    if policy is None:
        summary = {
            "state": evidence["state"],
            "invested_sessions": evidence["invested_sessions"],
            "first_position_signal_date": evidence["first_position_signal_date"],
            "first_fill_date": evidence["first_fill_date"],
            "fills": run["metrics"]["fills"],
            "unfilled_orders": run["metrics"]["orders_unfilled"],
            "intentional_cash_sessions": list(readiness.get("ready_cash_sessions", [])),
        }
        for key in ("basket_build_state", "unfilled_basket_signals"):
            if key in evidence:
                summary[key] = evidence[key]
        return summary

    required_symbols = list(policy["required_symbols"])
    held_dates = []
    required_hold_dates = []
    daily_account = []
    for point in run.get("equity_curve") or []:
        holdings = point.get("holdings")
        if not isinstance(holdings, Mapping):
            continue
        held_symbols = {
            symbol for symbol, holding in holdings.items()
            if isinstance(holding, Mapping)
            and isinstance(holding.get("quantity"), (int, float))
            and not isinstance(holding.get("quantity"), bool)
            and float(holding["quantity"]) > 1e-12
        }
        if held_symbols:
            held_dates.append(point["date"])
        if (
            all(symbol in held_symbols for symbol in required_symbols)
            if required_symbols else bool(held_symbols)
        ):
            required_hold_dates.append(point["date"])
        daily_account.append({
            "date": point["date"],
            "nav": point.get("nav"),
            "cash": point.get("cash"),
            "holdings": {
                symbol: float(holding["quantity"])
                for symbol, holding in holdings.items()
                if isinstance(holding, Mapping)
                and isinstance(holding.get("quantity"), (int, float))
                and not isinstance(holding.get("quantity"), bool)
            },
        })

    attempts = run.get("order_attempts") or []
    latest = attempts[-1] if attempts else None
    latest_complete = (
        bool(latest["batch_complete"])
        if latest is not None and "batch_complete" in latest
        else latest is not None and latest.get("status") == "filled"
    )
    build_state = (
        "completed" if latest_complete
        else "partial" if latest is not None and latest.get("filled_symbols")
        else "unfilled" if latest is not None
        else "none"
    )
    end_target = {
        "required": policy["require_end_of_period_target"],
        "state": build_state,
        "satisfied": (
            not policy["require_end_of_period_target"] or build_state == "completed"
        ),
    }

    failed_conditions = []
    state = evidence["state"]
    if state not in _POSITION_STATES:
        failed_conditions.append("POSITION_NOT_ESTABLISHED")
    if policy["require_initial_position"] and state != "initial_position_established":
        failed_conditions.append("INITIAL_POSITION_REQUIRED")
    if len(required_hold_dates) < policy["minimum_required_hold_sessions"]:
        failed_conditions.append("REQUIRED_HOLD_SESSIONS_NOT_MET")
    if not end_target["satisfied"]:
        failed_conditions.append("END_OF_PERIOD_TARGET_INCOMPLETE")

    summary = {
        "ok": not failed_conditions,
        "state": state,
        "invested_sessions": evidence["invested_sessions"],
        "first_position_signal_date": evidence["first_position_signal_date"],
        "first_fill_date": evidence["first_fill_date"],
        "held_sessions": len(held_dates),
        "first_held_date": held_dates[0] if held_dates else None,
        "required_symbols": required_symbols,
        "required_hold_dates": required_hold_dates,
        "required_hold_sessions": len(required_hold_dates),
        "first_required_hold_date": (
            required_hold_dates[0] if required_hold_dates else None
        ),
        "daily_account": daily_account,
        "minimum_required_hold_sessions": policy["minimum_required_hold_sessions"],
        "require_initial_position": policy["require_initial_position"],
        "end_of_period_target": end_target,
        "failed_conditions": failed_conditions,
        "fills": run["metrics"]["fills"],
        "unfilled_orders": run["metrics"]["orders_unfilled"],
        "intentional_cash_sessions": list(readiness.get("ready_cash_sessions", [])),
    }
    # Additive #56 basket-build disclosure, published only when the run carries
    # it, so every legacy position-evidence shape stays byte-identical.
    for key in ("basket_build_state", "unfilled_basket_signals"):
        if key in evidence:
            summary[key] = evidence[key]
    return summary


def _scoring_start_check(
    candidate: Mapping[str, Any],
    benchmark: Mapping[str, Any],
    scoring_start: str,
    signal_pauses: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    readiness = candidate.get("history_readiness") or {}
    first_scored_open = readiness.get("first_scored_open")
    signal_pauses = signal_pauses or {}

    def side_starts(run: Mapping[str, Any], label: str) -> bool:
        run_readiness = run.get("history_readiness") or {}
        first_open = run_readiness.get("first_scored_open")
        if (
            run["start_date"] == scoring_start
            and isinstance(first_open, dict)
            and first_open.get("ready") is True
            and run_readiness.get("first_ready_date") == scoring_start
        ):
            return True
        report = signal_pauses.get(label)
        if not isinstance(report, Mapping):
            return False
        return any(
            event.get("date") == scoring_start
            and event.get("rule_compliant") is True
            and event.get("nav_reconciled") is True
            and event.get("prior_orders", {}).get("processing_verified") is True
            for event in report.get("events", [])
            if isinstance(event, Mapping)
        )

    result = {
        "ok": (
            candidate["start_date"] == scoring_start
            and benchmark["start_date"] == scoring_start
            and side_starts(candidate, "candidate")
            and side_starts(benchmark, "benchmark")
        ),
        "expected": scoring_start,
        "candidate": {
            "start_date": candidate["start_date"],
            "first_ready_date": readiness.get("first_ready_date"),
            "first_scored_open": first_scored_open,
        },
        "benchmark": {"start_date": benchmark["start_date"]},
    }
    if signal_pauses is not None:
        result["pause_override"] = {
            "candidate": (
                readiness.get("first_ready_date") != scoring_start
                and side_starts(candidate, "candidate")
            ),
            "benchmark": (
                (benchmark.get("history_readiness") or {}).get("first_ready_date")
                != scoring_start
                and side_starts(benchmark, "benchmark")
            ),
        }
    return result


def _coverage_check(
    candidate: Mapping[str, Any],
    benchmark: Mapping[str, Any],
    scoring_start: str,
    expected: list[str],
) -> dict[str, Any]:
    expected_end = expected[-1] if expected else None

    def covered(run: Mapping[str, Any]) -> bool:
        return (
            run["start_date"] == scoring_start
            and run["end_date"] == expected_end
            and run["metrics"]["sessions"] == len(expected)
            and run["metrics"]["valued_sessions"] == len(expected)
        )

    def covered_summary(run: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "start_date": run["start_date"],
            "end_date": run["end_date"],
            "sessions": run["metrics"]["sessions"],
            "valued_sessions": run["metrics"]["valued_sessions"],
        }

    return {
        "ok": covered(candidate) and covered(benchmark),
        "expected": {
            "start": scoring_start,
            "end": expected_end,
            "sessions": len(expected),
        },
        "candidate": covered_summary(candidate),
        "benchmark": covered_summary(benchmark),
    }


def _data_availability_check(
    candidate: Mapping[str, Any],
    benchmark: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    signal_pauses: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    candidate_valuation = candidate["gaps"]["valuation"]
    benchmark_valuation = benchmark["gaps"]["valuation"]
    if signal_pauses is None:
        return {
            "ok": not (
                candidate["signal_gaps"]
                or candidate_valuation
                or benchmark["signal_gaps"]
                or benchmark_valuation
            ),
            "snapshot": snapshot,
            "candidate": {
                "signal_gaps": _gap_summary(candidate["signal_gaps"]),
                "valuation_gaps": _gap_summary(candidate_valuation),
            },
            "benchmark": {
                "signal_gaps": _gap_summary(benchmark["signal_gaps"]),
                "valuation_gaps": _gap_summary(benchmark_valuation),
            },
        }

    def side_report(label: str, run: Mapping[str, Any]) -> dict[str, Any]:
        report = signal_pauses.get(label, {})
        accepted_dates = {
            event.get("date")
            for event in report.get("events", [])
            if isinstance(event, Mapping) and event.get("rule_compliant") is True
        } if isinstance(report, Mapping) else set()
        unexplained = [
            item for item in run["signal_gaps"]
            if not isinstance(item, Mapping)
            or item.get("date") not in accepted_dates
        ]
        return {
            "signal_gaps": _gap_summary(run["signal_gaps"]),
            "accepted_signal_pause_days": len(accepted_dates),
            "unexplained_signal_gaps": _gap_summary(unexplained),
            "valuation_gaps": _gap_summary(run["gaps"]["valuation"]),
        }

    candidate_report = side_report("candidate", candidate)
    benchmark_report = side_report("benchmark", benchmark)
    return {
        "ok": (
            candidate_report["unexplained_signal_gaps"]["count"] == 0
            and benchmark_report["unexplained_signal_gaps"]["count"] == 0
            and not candidate_valuation
            and not benchmark_valuation
            and all(
                not isinstance(report, Mapping)
                or report.get("status") in {"verified", "not_applicable"}
                for report in signal_pauses.values()
            )
        ),
        "snapshot": snapshot,
        "candidate": candidate_report,
        "benchmark": benchmark_report,
    }


def _position_check(
    candidate: Mapping[str, Any],
    benchmark: Mapping[str, Any],
    frozen_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The formal position prerequisite for both sides of the frozen comparison.

    Actual portfolios are scored by default: a partially filled latest target
    remains visible but is not itself a blocker. An explicit frozen policy may
    require initial investment, simultaneous holdings on a minimum number of
    actual sessions, or a complete end-of-period target.
    """
    if frozen_policy is None:
        def legacy_satisfied(run: Mapping[str, Any]) -> bool:
            evidence = run["position_evidence"]
            if evidence["state"] not in _POSITION_STATES:
                return False
            build_state = evidence.get("basket_build_state")
            return build_state is None or build_state not in _UNFINISHED_BUILD_STATES

        return {
            "ok": legacy_satisfied(candidate) and legacy_satisfied(benchmark),
            "candidate": _position_summary(candidate),
            "benchmark": _position_summary(benchmark),
        }
    policy = frozen_policy
    candidate_report = _position_summary(candidate, policy["candidate"])
    benchmark_report = _position_summary(benchmark, policy["benchmark"])
    return {
        "ok": candidate_report["ok"] and benchmark_report["ok"],
        "candidate": candidate_report,
        "benchmark": benchmark_report,
    }


def _signal_pause_precheck(
    discovery: Mapping[str, Any],
    benchmark: Mapping[str, Any],
    quarterly: Mapping[str, Any] | None,
    frozen_policy: Mapping[str, Any] | None,
    *,
    require_bound_summary: bool = True,
) -> dict[str, Any]:
    """Verify each account's pause ledger and apply the optional frozen cap."""
    reports = {
        "candidate": summarize_signal_pauses(discovery),
        "benchmark": summarize_signal_pauses(benchmark),
    }
    recorded = quarterly.get("signal_pauses") if quarterly is not None else None
    if isinstance(recorded, Mapping):
        if recorded != reports:
            return {
                "status": "insufficient_evidence",
                "reason": "PAUSE_SUMMARY_MISMATCH",
                **reports,
            }
    elif require_bound_summary and any(
        report["paused_signal_days"] for report in reports.values()
    ):
        # Older summaries did not bind pause evidence.  They remain readable
        # when no pause needs an exception, but cannot authorize one.
        return {
            "status": "insufficient_evidence",
            "reason": "PAUSE_EVIDENCE_NOT_BOUND",
            **reports,
        }

    for report in reports.values():
        if frozen_policy is not None:
            report["limit"] = dict(frozen_policy)
            report["limit_met"] = (
                report["paused_signal_days"] <= frozen_policy["value"]
            )
        else:
            report["limit"] = None
            report["limit_met"] = True
    verified = all(
        report["status"] in {"verified", "not_applicable"}
        for report in reports.values()
    )
    exceeded = [
        side
        for side, report in reports.items()
        if report["limit_met"] is False
    ]
    return {
        "status": "verified" if verified else "insufficient_evidence",
        "policy": dict(frozen_policy) if frozen_policy is not None else None,
        "limit_exceeded": exceeded,
        **reports,
    }


def interpret_validation_evidence(
    candidate: Mapping[str, Any],
    benchmark: Mapping[str, Any],
    *,
    scoring_start: str,
    expected_sessions: list[str],
    snapshot: Mapping[str, Any],
    position_policy: Mapping[str, Any] | None,
    signal_pause_policy: Mapping[str, Any] | None,
    pause_enabled: bool,
    quarterly: Mapping[str, Any] | None = None,
    require_bound_summary: bool = False,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Interpret both accounts under frozen rules, without stage side effects.

    The Discovery caller supplies its verified quarterly summary even when pause
    disclosures are disabled; the other callers use their actual run ledgers.
    """
    pauses = (
        _signal_pause_precheck(
            candidate, benchmark, quarterly, signal_pause_policy,
            require_bound_summary=require_bound_summary,
        )
        if pause_enabled or require_bound_summary else None
    )
    pause_reports = pauses if pause_enabled else None
    if pause_reports is not None and require_bound_summary:
        pause_reports = {
            side: pauses[side]
            for side in ("candidate", "benchmark")
            if isinstance(pauses.get(side), Mapping)
        }
    checks = {
        "data_availability": _data_availability_check(
            candidate, benchmark, snapshot, pause_reports,
        ),
        "scoring_start": _scoring_start_check(
            candidate, benchmark, scoring_start, pause_reports,
        ),
        "coverage": _coverage_check(candidate, benchmark, scoring_start, expected_sessions),
        "position": _position_check(candidate, benchmark, position_policy),
    }
    return checks, pauses
