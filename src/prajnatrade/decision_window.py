"""Frozen after-close decision timing and common-valid-close observation selection (Spec #74-C)."""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from prajnatrade.trading_status import (
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
) -> dict[str, Any]:
    """Validate and return a canonical frozen decision policy dict."""
    if isinstance(policy, Mapping):
        version = policy.get("version")
        cutoff_raw = signal_cutoff if signal_cutoff is not None else policy.get("signal_cutoff", DEFAULT_SIGNAL_CUTOFF)
        tz_raw = signal_timezone if signal_timezone is not None else policy.get("signal_timezone", DEFAULT_SIGNAL_TIMEZONE)
        extra = (
            {"monthly_deferral": bool(policy["monthly_deferral"])}
            if "monthly_deferral" in policy
            else {}
        )
    elif isinstance(policy, str):
        version = policy
        cutoff_raw = signal_cutoff if signal_cutoff is not None else DEFAULT_SIGNAL_CUTOFF
        tz_raw = signal_timezone if signal_timezone is not None else DEFAULT_SIGNAL_TIMEZONE
        extra = {}
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


def select_common_observations(
    sessions: Sequence[str],
    raw_bars: Mapping[tuple[str, str], Mapping[str, Any]],
    derived: Mapping[tuple[str, str], Mapping[str, Any]],
    decision_date: str,
    policy: Mapping[str, Any],
    history_requirements: Sequence[Mapping[str, Any]],
    *,
    data_kind: str = "synthetic",
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
    for symbol in symbols:
        raw = raw_bars.get((decision_date, symbol))
        drv = derived.get((decision_date, symbol))
        confirmed_no_trade, _ = classify_no_trade_for_valuation(
            raw, cutoff, data_kind=data_kind
        )
        if confirmed_no_trade:
            continue
        reason = _check_symbol_observation(decision_date, raw, drv, cutoff, data_kind)
        if reason is not None:
            pause_symbols.append(symbol)
            pause_reasons.append({"symbol": symbol, "reason": reason})

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
            },
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
        "ready": ready,
        "observation_dates": observation_dates,
        "observation_sources": selected_sources,
        "windows": windows,
        "observation_values": selected_values,
        "latest_values": latest_values,
        "requirements": results,
    }
