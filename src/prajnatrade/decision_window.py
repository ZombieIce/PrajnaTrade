"""Frozen after-close decision timing and common-valid-close observation selection (Spec #74-C)."""

from __future__ import annotations

import math
import re
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from prajnatrade.trading_status import (
    CASH,
    ETF_SYMBOLS,
    halt_conflicts_with_trade,
)
from prajnatrade.valuation import (
    available_by,
    classify_no_trade_for_valuation,
    close_available,
    parse_instant,
    valid_price,
)

AFTER_CLOSE_COMMON_POLICY = "after-close-common-v1"
DECISION_POLICY_VERSIONS = (AFTER_CLOSE_COMMON_POLICY,)
DEFAULT_SIGNAL_CUTOFF = "15:30"
DEFAULT_SIGNAL_TIMEZONE = "Asia/Shanghai"

_CUTOFF_RE = re.compile(r"^(\d{2}):(\d{2})$")


def _validate_cutoff(raw: object) -> str:
    if not isinstance(raw, str):
        raise ValueError(f"signal_cutoff must be HH:MM, got {raw!r}.")
    match = _CUTOFF_RE.fullmatch(raw.strip())
    if match is None:
        raise ValueError(f"signal_cutoff must be HH:MM, got {raw!r}.")
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"signal_cutoff out of range: {raw!r}.")
    return f"{hour:02d}:{minute:02d}"


def _validate_timezone(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"signal_timezone must be a valid IANA timezone, got {raw!r}.")
    tz_name = raw.strip()
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError(f"Unknown signal_timezone {raw!r}: {error}") from error
    return tz_name


def normalize_decision_policy(
    policy: str | Mapping[str, Any],
    *,
    signal_cutoff: str | None = None,
    signal_timezone: str | None = None,
    monthly_deferral: bool | None = None,
) -> dict[str, Any]:
    """Validate and return a canonical frozen decision policy dict."""
    if isinstance(policy, Mapping):
        version = policy.get("version")
        cutoff_raw = signal_cutoff if signal_cutoff is not None else policy.get("signal_cutoff", DEFAULT_SIGNAL_CUTOFF)
        tz_raw = signal_timezone if signal_timezone is not None else policy.get("signal_timezone", DEFAULT_SIGNAL_TIMEZONE)
        if monthly_deferral is not None:
            extra = {"monthly_deferral": bool(monthly_deferral)}
        elif "monthly_deferral" in policy:
            extra = {"monthly_deferral": bool(policy["monthly_deferral"])}
        else:
            extra = {}
    elif isinstance(policy, str):
        version = policy
        cutoff_raw = signal_cutoff if signal_cutoff is not None else DEFAULT_SIGNAL_CUTOFF
        tz_raw = signal_timezone if signal_timezone is not None else DEFAULT_SIGNAL_TIMEZONE
        extra = {"monthly_deferral": bool(monthly_deferral)} if monthly_deferral is not None else {}
    else:
        raise ValueError(f"Unsupported decision_policy: {policy!r}.")

    if version not in DECISION_POLICY_VERSIONS:
        raise ValueError(f"Unknown decision policy version: {version!r}.")

    return {
        "version": version,
        "signal_cutoff": _validate_cutoff(cutoff_raw),
        "signal_timezone": _validate_timezone(tz_raw),
        "observation_selection": "common_valid_close",
        "execution_earliest": "next_session_open",
        **extra,
    }


def decision_cutoff(day: str, policy: Mapping[str, Any]) -> datetime:
    """Return the timezone-aware decision cutoff instant for ``day``."""
    parsed_day = date.fromisoformat(day)
    cutoff_str = _validate_cutoff(policy.get("signal_cutoff", DEFAULT_SIGNAL_CUTOFF))
    hour, minute = (int(part) for part in cutoff_str.split(":"))
    tz_name = _validate_timezone(policy.get("signal_timezone", DEFAULT_SIGNAL_TIMEZONE))
    return datetime.combine(parsed_day, time(hour, minute), tzinfo=ZoneInfo(tz_name))


def _check_symbol_observation(
    day: str,
    raw: Mapping[str, Any] | None,
    drv: Mapping[str, Any] | None,
    cutoff: datetime,
    data_kind: str,
) -> str | None:
    """Return None when ``(day, symbol)`` has a timely trusted close & derived observation, else a reason."""
    if raw is None:
        return "missing_bar"
    if (
        raw.get("open_conflict") is not None
        or raw.get("close_conflict") is not None
        or raw.get("status_conflict") is not None
        or halt_conflicts_with_trade(raw.get("halt"), day, raw)
    ):
        return "conflicting_evidence"
    status = raw.get("trading_status")
    if status == "unknown":
        return "unknown_trading_status"
    if status not in ("trading", "partial_halt"):
        return f"non_trading_status:{status}"
    if not valid_price(raw.get("close")):
        return "partial_halt_missing_close" if status == "partial_halt" else "missing_close"
    if not close_available(raw, cutoff, data_kind):
        ts_field = (
            "close_available_at"
            if data_kind == "real" or raw.get("close_available_at") is not None
            else "available_at"
        )
        available_at = parse_instant(raw.get(ts_field))
        if available_at is not None and available_at > cutoff:
            return "close_after_cutoff"
        return "missing_close_availability"
    if drv is None or not valid_price(drv.get("value")):
        return "missing_derived_value"
    if not available_by(drv, cutoff):
        available_at = parse_instant(drv.get("available_at"))
        if available_at is not None and available_at > cutoff:
            return "derived_after_cutoff"
        return "missing_derived_availability"
    return None


def _observation_source(
    raw: Mapping[str, Any] | None,
    drv: Mapping[str, Any],
    *,
    data_kind: str,
) -> dict[str, Any]:
    raw_ts = None
    trading_status = None
    raw_evidence = None
    open_source_id = None
    if raw is not None:
        trading_status = raw.get("trading_status")
        raw_evidence = raw.get("availability_evidence")
        raw_ts = (
            raw.get("close_available_at")
            if data_kind == "real" or raw.get("close_available_at") is not None
            else raw.get("available_at")
        )
        open_source = raw.get("open_source")
        if isinstance(open_source, Mapping):
            open_source_id = open_source.get("evidence_id")
    return {
        "trading_status": trading_status,
        "close_available_at": raw_ts,
        "raw_availability_evidence": raw_evidence,
        **({"open_source_evidence_id": open_source_id} if open_source_id else {}),
        "derived_available_at": drv.get("available_at"),
        "derived_availability_evidence": drv.get("availability_evidence"),
    }


def _pause_evidence(
    symbol: str,
    reason: str,
    raw: Mapping[str, Any] | None,
    drv: Mapping[str, Any] | None,
    *,
    data_kind: str,
) -> dict[str, Any]:
    """Keep the point-in-time evidence that explains a paused decision."""
    raw = raw or {}
    drv = drv or {}
    if reason == "derived_after_cutoff":
        return {
            "symbol": symbol,
            "reason": reason,
            "source": "derived",
            "evidence_available_at": drv.get("available_at"),
            "availability_evidence": drv.get("availability_evidence"),
        }
    if reason == "close_after_cutoff":
        available_at = (
            raw.get("close_available_at")
            if data_kind == "real" or raw.get("close_available_at") is not None
            else raw.get("available_at")
        )
        return {
            "symbol": symbol,
            "reason": reason,
            "source": "raw_close",
            "evidence_available_at": available_at,
            "availability_evidence": raw.get("availability_evidence"),
        }
    return {
        "symbol": symbol,
        "reason": reason,
        "source": "trading_status",
        "evidence_available_at": raw.get("available_at"),
        "availability_evidence": raw.get("availability_evidence"),
        "status_evidence_id": raw.get("status_evidence_id"),
        "trading_status": raw.get("trading_status"),
    }


def select_common_observations(
    sessions: Sequence[str],
    raw_bars: Mapping[tuple[str, str], Mapping[str, Any]],
    derived: Mapping[tuple[str, str], Mapping[str, Any]],
    decision_date: str,
    policy: Mapping[str, Any],
    history_requirements: Sequence[Mapping[str, Any]],
    *,
    data_kind: str = "synthetic",
    schema_version: int | None = None,
    symbols: Sequence[str] = ETF_SYMBOLS,
) -> dict[str, Any]:
    """Select common valid close observations up to ``decision_date`` at ``decision_cutoff``."""
    cutoff = decision_cutoff(decision_date, policy)
    cutoff_iso = cutoff.isoformat()

    valid_mode = "valid_observations"
    consecutive_mode = "consecutive_sessions"
    longest = {valid_mode: 0, consecutive_mode: 0}
    for requirement in history_requirements:
        count = requirement["observations"]
        mode = requirement["mode"]
        if mode not in longest or type(count) is not int or count < 1:
            raise ValueError("Each history requirement needs a valid mode and positive observations.")
        longest[mode] = max(longest[mode], count)

    pause_symbols: list[str] = []
    pause_reasons: list[dict[str, str]] = []
    pause_evidence: list[dict[str, Any]] = []
    confirmed_no_trade: list[dict[str, Any]] = []
    for symbol in symbols:
        raw = raw_bars.get((decision_date, symbol))
        drv = derived.get((decision_date, symbol))
        no_trade, _ = classify_no_trade_for_valuation(
            raw, cutoff, data_kind=data_kind, schema_version=schema_version
        )
        if no_trade:
            assert raw is not None
            confirmed_no_trade.append(
                {
                    "symbol": symbol,
                    "reason": "confirmed_full_day_suspension",
                    "evidence_available_at": raw.get("available_at"),
                    "availability_evidence": raw.get("availability_evidence"),
                    "status_evidence_id": raw.get("status_evidence_id"),
                    "trading_status": raw.get("trading_status"),
                }
            )
            continue
        reason = _check_symbol_observation(decision_date, raw, drv, cutoff, data_kind)
        if reason is not None:
            pause_symbols.append(symbol)
            pause_reasons.append({"symbol": symbol, "reason": reason})
            pause_evidence.append(
                _pause_evidence(symbol, reason, raw, drv, data_kind=data_kind)
            )

    if pause_symbols:
        return {
            "decision_date": decision_date,
            "signal_cutoff": cutoff_iso,
            "signal_status": "paused",
            "signal_pause": {
                "code": "SIGNAL_PAUSED_UNTRUSTED_OBSERVATION",
                "date": decision_date,
                "symbols": sorted(pause_symbols),
                "reasons": pause_reasons,
                "evidence": pause_evidence,
                "confirmed_no_trade": confirmed_no_trade,
            },
            "confirmed_no_trade": confirmed_no_trade,
            "ready": False,
            "observation_dates": [],
            "observation_sources": {},
            "windows": {req["id"]: [] for req in history_requirements},
            "observation_values": {},
            "latest_values": {},
            "requirements": [],
        }

    raw_bar_dates = {day for (day, _) in raw_bars}
    earliest_raw_date = min(raw_bar_dates) if raw_bar_dates else None

    panel: list[tuple[str, dict[str, float], dict[str, dict[str, Any]], list[str]]] = []
    valid_indices: list[int] = []
    need_valid = max(longest[valid_mode], 1 if not history_requirements else 0)
    end_idx = bisect_right(sessions, decision_date) - 1
    for index in range(end_idx, -1, -1):
        if len(panel) >= longest[consecutive_mode] and len(valid_indices) >= need_valid:
            break
        day = sessions[index]
        values: dict[str, float] = {}
        sources: dict[str, dict[str, Any]] = {}
        failed: list[str] = []
        has_raw_scope = earliest_raw_date is not None and day >= earliest_raw_date
        for symbol in symbols:
            raw = raw_bars.get((day, symbol))
            drv = derived.get((day, symbol))
            if has_raw_scope or day in raw_bar_dates:
                reason = _check_symbol_observation(day, raw, drv, cutoff, data_kind)
            else:
                if drv is None or not valid_price(drv.get("value")) or not available_by(drv, cutoff):
                    reason = "missing_prior_derived"
                else:
                    reason = None
            if reason is not None:
                failed.append(symbol)
            else:
                assert drv is not None
                values[symbol] = float(drv["value"])
                sources[symbol] = _observation_source(raw, drv, data_kind=data_kind)
        if not failed:
            valid_indices.append(len(panel))
        panel.append((day, values, sources, sorted(failed)))

    results: list[dict[str, Any]] = []
    windows: dict[str, list[str]] = {}
    selected_values: dict[str, dict[str, float]] = {}
    selected_sources: dict[str, dict[str, dict[str, Any]]] = {}

    if not history_requirements:
        if valid_indices:
            day, values, sources, _ = panel[valid_indices[0]]
            selected_values[day] = values
            selected_sources[day] = sources
    else:
        for requirement in history_requirements:
            required = requirement["observations"]
            mode = requirement["mode"]
            if mode == consecutive_mode:
                end = required
            elif len(valid_indices) >= required:
                end = valid_indices[required - 1] + 1
            else:
                end = len(panel)
            valid: list[str] = []
            missing_sessions: list[dict[str, Any]] = []
            for day, values, sources, failed in reversed(panel[:end]):
                if failed:
                    missing_sessions.append({"date": day, "missing_symbols": failed})
                else:
                    valid.append(day)
                    selected_values[day] = values
                    selected_sources[day] = sources
            windows[requirement["id"]] = valid
            results.append({
                "id": requirement["id"],
                "mode": mode,
                "required_observations": required,
                "available_observations": len(valid),
                "missing_sessions": missing_sessions,
            })

    ready = all(
        item["available_observations"] == item["required_observations"] for item in results
    )
    observation_dates = sorted(selected_values)
    latest_values = dict(selected_values[observation_dates[-1]]) if observation_dates else {}
    return {
        "decision_date": decision_date,
        "signal_cutoff": cutoff_iso,
        "signal_status": "ready" if ready else "insufficient_history",
        "signal_pause": None,
        "confirmed_no_trade": confirmed_no_trade,
        "ready": ready,
        "observation_dates": observation_dates,
        "observation_sources": selected_sources,
        "windows": windows,
        "observation_values": selected_values,
        "latest_values": latest_values,
        "requirements": results,
    }


STRATEGY_PURPOSES = ("monthly", "risk_control", "rebalance", "initial")


def compute_prior_close_weights(
    nav_point: Mapping[str, Any] | None,
    shares: Mapping[str, float],
    cash: float,
    receivables: float = 0.0,
    *,
    symbols: Sequence[str] = ETF_SYMBOLS,
) -> dict[str, float] | None:
    """Compute portfolio close weights from ``nav_point``, or ``None`` when NAV is unavailable."""
    if not isinstance(nav_point, Mapping):
        return None
    nav_raw = nav_point.get("nav")
    if nav_raw is None:
        return None
    nav = float(nav_raw)
    if not math.isfinite(nav) or nav <= 0.0:
        return None
    positions = nav_point.get("positions")
    if not isinstance(positions, Mapping):
        positions = nav_point.get("holdings")
    if not isinstance(positions, Mapping):
        return None
    weights: dict[str, float] = {}
    for symbol in symbols:
        held = float(shares.get(symbol, 0.0))
        pos = positions.get(symbol)
        mv = pos.get("market_value") if isinstance(pos, Mapping) else None
        if mv is None:
            if held > 1e-12:
                return None
            weights[symbol] = 0.0
        else:
            weights[symbol] = float(mv) / nav
    weights[CASH] = (float(cash) + float(receivables)) / nav
    return weights


def _month_session_and_week_start(
    signal_date: str,
    sessions: Sequence[str],
) -> tuple[int, bool]:
    """Return 1-based ``month_session`` and ``week_start`` for ``signal_date`` on ``sessions``."""
    month_prefix = signal_date[:7]
    iso_week = date.fromisoformat(signal_date).isocalendar()[:2]
    month_count = 0
    prev_day: str | None = None
    found = False
    for day in sessions:
        if day > signal_date:
            break
        if day[:7] == month_prefix:
            month_count += 1
        if day == signal_date:
            found = True
            break
        prev_day = day
    if not found:
        month_count = 1
    week_start = prev_day is None or date.fromisoformat(prev_day).isocalendar()[:2] != iso_week
    return month_count, week_start


def build_strategy_state(
    *,
    signal_date: str,
    sessions: Sequence[str],
    cash: float,
    shares: Mapping[str, float],
    equity: float | None,
    prior_close_weights: Mapping[str, float] | None,
    rebalance_anchor_date: str | None = None,
    previous_order_attempted: Mapping[str, Any] | None = None,
    last_filled_target: Mapping[str, float] | None = None,
    last_monthly_filled_target: Mapping[str, float] | None = None,
    history_windows: Mapping[str, Sequence[str]] | None = None,
    monthly_deferral: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical point-in-time strategy state dict shared across stages."""
    month_session, week_start = _month_session_and_week_start(signal_date, sessions)
    state: dict[str, Any] = {
        "cash": float(cash),
        "shares": {symbol: float(shares.get(symbol, 0.0)) for symbol in ETF_SYMBOLS},
        "equity": float(equity) if equity is not None else None,
        "signal_date": signal_date,
        "month_session": month_session,
        "week_start": week_start,
        "rebalance_anchor_date": rebalance_anchor_date,
        "previous_order_attempted": dict(previous_order_attempted) if previous_order_attempted is not None else None,
        "last_filled_target": dict(last_filled_target) if last_filled_target is not None else None,
        "last_monthly_filled_target": (
            dict(last_monthly_filled_target) if last_monthly_filled_target is not None else None
        ),
        "prior_close_weights": dict(prior_close_weights) if prior_close_weights is not None else None,
    }
    if history_windows is not None:
        state["history_windows"] = {k: list(v) for k, v in history_windows.items()}
    if monthly_deferral is not None:
        state["deferred_monthly"] = True
        state["monthly_deferral"] = dict(monthly_deferral)
    else:
        state["deferred_monthly"] = False
    return state


def _validate_target_weights(weights: Mapping[str, Any]) -> dict[str, float]:
    if not isinstance(weights, Mapping):
        raise ValueError("Strategy must return a mapping of asset weights or None.")
    unknown = sorted(set(weights) - set((*ETF_SYMBOLS, CASH)))
    if unknown:
        raise ValueError(f"Strategy returned unsupported assets: {', '.join(unknown)}.")
    normalized: dict[str, float] = {}
    for symbol in (*ETF_SYMBOLS, CASH):
        raw = weights.get(symbol, 0.0)
        try:
            weight = float(raw)
        except (TypeError, ValueError) as error:
            raise ValueError(f"Weight for {symbol} must be numeric.") from error
        if not math.isfinite(weight) or weight < 0 or weight > 1:
            raise ValueError(f"Weight for {symbol} must be finite and between 0 and 1.")
        normalized[symbol] = weight
    if not math.isclose(sum(normalized.values()), 1.0, rel_tol=0.0, abs_tol=1e-8):
        raise ValueError("ETF and CASH weights must sum to 1.")
    return normalized


def parse_strategy_output(
    raw_result: Any,
    *,
    signal_date: str,
) -> dict[str, Any] | None:
    """Parse plain-weight or structured strategy output into a canonical target record."""
    if raw_result is None:
        return None
    if isinstance(raw_result, Mapping) and "weights" in raw_result:
        raw_weights = raw_result.get("weights")
        if raw_weights is None:
            return None
        normalized = _validate_target_weights(raw_weights)
        purpose = str(raw_result.get("purpose", "rebalance"))
        if purpose not in STRATEGY_PURPOSES:
            raise ValueError(
                f"Unsupported strategy purpose {purpose!r}; expected one of {STRATEGY_PURPOSES}."
            )
        cycle = raw_result.get("cycle")
        if cycle is not None and not isinstance(cycle, str):
            raise ValueError(f"Strategy cycle must be a string, got {cycle!r}.")
        raw_fixed = raw_result.get("fixed_amounts")
        fixed_amounts: dict[str, float] | None = None
        if raw_fixed is not None:
            if not isinstance(raw_fixed, Mapping):
                raise ValueError("fixed_amounts must be a mapping of symbol to non-negative float.")
            unknown_fixed = sorted(set(raw_fixed) - set(ETF_SYMBOLS))
            if unknown_fixed:
                raise ValueError(f"Unsupported fixed_amounts symbols: {', '.join(unknown_fixed)}.")
            fixed_amounts = {}
            for sym, val in raw_fixed.items():
                amount = float(val)
                if not math.isfinite(amount) or amount < 0.0:
                    raise ValueError(f"fixed_amounts[{sym}] must be finite and non-negative.")
                if amount > 0.0:
                    fixed_amounts[sym] = amount
        return {
            "weights": normalized,
            "purpose": purpose,
            "cycle": cycle if cycle is not None else signal_date[:7],
            "fixed_amounts": fixed_amounts or None,
        }
    normalized = _validate_target_weights(raw_result)
    return {
        "weights": normalized,
        "purpose": "rebalance",
        "cycle": signal_date[:7],
        "fixed_amounts": None,
    }


def initial_strategy_state() -> dict[str, Any]:
    """Return the canonical initial persisted stateful-rebalance tracking state."""
    return {
        "rebalance_anchor_date": None,
        "last_filled_target": None,
        "last_monthly_filled_target": None,
        "previous_order_attempted": None,
        "monthly_signal_cycle": None,
        "active_deferral": None,
        "monthly_deferrals": [],
    }


def scheduled_monthly_session(policy: Mapping[str, Any] | None) -> int:
    """Return the 1-based monthly trading session index scheduled for monthly decisions."""
    if isinstance(policy, Mapping) and "monthly_decision_session" in policy:
        raw = policy["monthly_decision_session"]
        if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 1:
            return raw
    return 1


def close_expired_deferral(
    active_deferral: dict[str, Any] | None,
    monthly_deferrals: Sequence[Mapping[str, Any]],
    *,
    current_cycle: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Record ``not_executed`` and clear ``active_deferral`` once the calendar enters a new month."""
    updated = [dict(item) for item in monthly_deferrals]
    if active_deferral is not None and active_deferral["cycle"] != current_cycle:
        updated.append(
            {
                "cycle": active_deferral["cycle"],
                "scheduled_date": active_deferral["scheduled_date"],
                "actual_signal_date": None,
                "status": "not_executed",
                "deferral_reason": active_deferral["deferral_reason"],
            }
        )
        return None, updated
    return active_deferral, updated


def close_month_end_deferral(
    active_deferral: dict[str, Any] | None,
    monthly_deferrals: Sequence[Mapping[str, Any]],
    *,
    session: str,
    next_session: str | None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Record ``not_executed`` on a month's final trading session when no same-month session remains."""
    updated = [dict(item) for item in monthly_deferrals]
    if (
        active_deferral is not None
        and active_deferral["cycle"] == session[:7]
        and next_session is not None
        and next_session[:7] != session[:7]
    ):
        updated.append(
            {
                "cycle": active_deferral["cycle"],
                "scheduled_date": active_deferral["scheduled_date"],
                "actual_signal_date": None,
                "status": "not_executed",
                "deferral_reason": active_deferral["deferral_reason"],
            }
        )
        return None, updated
    return active_deferral, updated


def maybe_trigger_monthly_deferral(
    *,
    enabled: bool,
    active_deferral: dict[str, Any] | None,
    monthly_signal_cycle: str | None,
    signal_date: str,
    sessions: Sequence[str],
    pause_info: Mapping[str, Any],
    scheduled_month_session: int = 1,
    warmup_ready: bool = True,
) -> dict[str, Any] | None:
    """Start an intra-month deferral ONLY when paused on the scheduled monthly decision session."""
    if not enabled or not warmup_ready or active_deferral is not None:
        return active_deferral
    if monthly_signal_cycle == signal_date[:7]:
        return active_deferral
    month_session, _ = _month_session_and_week_start(signal_date, sessions)
    if month_session != scheduled_month_session:
        return active_deferral
    return {
        "cycle": signal_date[:7],
        "scheduled_date": signal_date,
        "deferral_reason": pause_info.get("reasons") or pause_info.get("code"),
    }


def consume_monthly_deferral(
    *,
    active_deferral: dict[str, Any] | None,
    monthly_deferrals: Sequence[Mapping[str, Any]],
    purpose: str,
    signal_date: str,
    execution_date: str,
) -> tuple[dict[str, Any], dict[str, Any] | None, list[dict[str, Any]]]:
    """Consume an active intra-month deferral when a deferred monthly signal is emitted."""
    updated = [dict(item) for item in monthly_deferrals]
    if (
        active_deferral is not None
        and active_deferral["cycle"] == signal_date[:7]
        and purpose == "monthly"
    ):
        deferral_fields = {
            "scheduled_date": active_deferral["scheduled_date"],
            "actual_signal_date": signal_date,
            "deferral_reason": active_deferral["deferral_reason"],
        }
        updated.append(
            {
                "cycle": signal_date[:7],
                "scheduled_date": active_deferral["scheduled_date"],
                "actual_signal_date": signal_date,
                "execution_date": execution_date,
                "status": "executed",
                "deferral_reason": active_deferral["deferral_reason"],
            }
        )
        return deferral_fields, None, updated
    return {}, active_deferral, updated


def advance_rebalance_anchor(
    *,
    rebalance_anchor_date: str | None,
    last_filled_target: dict[str, float] | None,
    last_monthly_filled_target: dict[str, float] | None,
    attempt: Mapping[str, Any],
    batch_ok: bool,
    signal_date: str,
    target_weights: Mapping[str, float],
    purpose: str,
) -> tuple[str | None, dict[str, float] | None, dict[str, float] | None, dict[str, Any]]:
    """Advance rebalance anchor and target history iff the settled order batch completed."""
    previous_order_attempted = dict(attempt)
    if not batch_ok:
        return (
            rebalance_anchor_date,
            last_filled_target,
            last_monthly_filled_target,
            previous_order_attempted,
        )
    new_target = dict(target_weights)
    new_monthly = (
        dict(target_weights)
        if last_monthly_filled_target is None or purpose == "monthly"
        else last_monthly_filled_target
    )
    return signal_date, new_target, new_monthly, previous_order_attempted
