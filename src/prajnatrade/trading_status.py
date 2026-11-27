"""Trading-session status rules and stdlib constants shared across modules."""

import math
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

#: Fixed five-ETF universe and cash symbol.
ETF_SYMBOLS = ("511260", "518880", "515080", "513300", "159612")
CASH = "CASH"

#: Asia/Shanghai is the trading-time anchor for both baselines and Shadow Live.
CN_TZ = timezone(timedelta(hours=8))
_SHANGHAI = CN_TZ

NEXT_OPEN_RULES = "next-open-v1"
RETRY_INITIAL_RULES = "retry-initial-v1"
#: Fill an order effective before the open at the sealed, evidenced actual daily
#: open, including a halted-at-open session whose bar carries that open (ADR 0002).
ACTUAL_OPEN_RULES = "actual-open-v1"
#: Freeze target weights / fixed amounts pre-open and settle at actual open with
#: whole-lot rounding and proportional cash allocation (Spec #74 / #78).
ACTUAL_OPEN_LOT_RULES = "actual-open-lot-v1"
EXECUTION_RULE_VERSIONS = (
    NEXT_OPEN_RULES,
    RETRY_INITIAL_RULES,
    ACTUAL_OPEN_RULES,
    ACTUAL_OPEN_LOT_RULES,
)
#: The sealed execution basis echoed by every fill and by a round receipt.
EXECUTION_PRICE_BASIS = "unadjusted_open"

#: Default legacy valuation policy: every held symbol needs a confirmed daily close.
STRICT_CLOSE_VALUATION = "strict-close-v1"
#: Opt-in valuation policy (ADR 0003): carry forward the last authorized trusted
#: unadjusted close on a confirmed full-day no-trade session.
CARRY_FORWARD_SUSPENDED_VALUATION = "carry-forward-suspended-v1"
VALUATION_POLICY_VERSIONS = (
    STRICT_CLOSE_VALUATION,
    CARRY_FORWARD_SUSPENDED_VALUATION,
)


def valuation_policy_version(execution_rules: Mapping[str, Any] | None) -> str:
    """Return the frozen valuation policy version, defaulting to strict-close-v1."""
    if not isinstance(execution_rules, Mapping):
        return STRICT_CLOSE_VALUATION
    valuation = execution_rules.get("valuation")
    if isinstance(valuation, Mapping):
        version = valuation.get("version")
        if isinstance(version, str) and version:
            return version
    return STRICT_CLOSE_VALUATION


def halt_covers_session(halt: object, day: str) -> bool:
    """Return whether a halt spans the Shanghai session from 09:30 to 15:00."""
    if not isinstance(halt, dict):
        return False
    start_value, end_value = halt.get("start"), halt.get("end")
    if not isinstance(start_value, str) or not isinstance(end_value, str):
        return False
    try:
        start = datetime.fromisoformat(start_value)
        end = datetime.fromisoformat(end_value)
    except ValueError:
        return False
    if start.tzinfo is None or end.tzinfo is None:
        return False
    session = datetime.fromisoformat(day).replace(tzinfo=_SHANGHAI)
    session_open = session.replace(hour=9, minute=30)
    session_close = session.replace(hour=15, minute=0)
    return start <= session_open and session_close <= end


PRICE_FIELDS = ("open", "high", "low", "close")


def has_status_evidence_id(value: object) -> bool:
    """Return whether a raw bar carries a non-empty status_evidence_id."""
    return isinstance(value, str) and bool(value.strip())


def halt_conflicts_with_volume(halt: object, day: str, volume: object) -> bool:
    """Return whether reported trades contradict a whole-session halt."""
    return (
        isinstance(volume, (int, float))
        and not isinstance(volume, bool)
        and math.isfinite(volume)
        and volume > 0
        and halt_covers_session(halt, day)
    )


def halt_conflicts_with_trade(
    halt: object, day: str, row: Mapping[str, Any]
) -> bool:
    """Return whether reported prices or positive volume contradict a whole-session halt."""
    return halt_covers_session(halt, day) and (
        any(row.get(field) is not None for field in PRICE_FIELDS)
        or halt_conflicts_with_volume(halt, day, row.get("volume"))
    )


def confirmed_no_trade_shape(
    row: Mapping[str, Any],
    *,
    data_kind: str,
    schema_version: int | None = None,
) -> tuple[bool, str | None]:
    """Classify whether a raw bar has a confirmed full-day no-trade row shape (ADR 0003).

    Checks trading status, OHLCV/halt consistency, conflicts, and (for real or
    schema-3+ rows) presence of ``status_evidence_id``. Callers still verify
    availability cutoff evidence as needed. Returns ``(True, None)`` when the
    row shape confirms full-day no-trade, or ``(False, reason_or_none)``
    otherwise.
    """
    if row.get("open_conflict") is not None or row.get("status_conflict") is not None:
        return False, "conflicting_status_or_trade_evidence"
    status = row.get("trading_status")
    day = str(row.get("date"))
    is_real = data_kind == "real"
    allow_v3_status = is_real or (schema_version is not None and schema_version >= 3)
    if status == "unknown":
        return False, "unknown_trading_status"
    if status == "suspended":
        has_price = any(row.get(field) is not None for field in PRICE_FIELDS)
        volume = row.get("volume")
        bad_volume = isinstance(volume, bool) or volume not in (None, 0)
        halt = row.get("halt")
        bad_halt = halt is not None and not halt_covers_session(halt, day)
        if has_price or bad_volume or bad_halt:
            return False, "conflicting_status_or_trade_evidence"
        if allow_v3_status and not has_status_evidence_id(row.get("status_evidence_id")):
            return False, "missing_status_evidence"
        return True, None
    if allow_v3_status and status == "partial_halt":
        halt = row.get("halt")
        volume = row.get("volume")
        if halt_conflicts_with_trade(halt, day, row):
            return False, "conflicting_status_or_trade_evidence"
        all_prices_null = all(row.get(field) is None for field in PRICE_FIELDS)
        no_volume = not isinstance(volume, bool) and volume in (None, 0)
        if halt_covers_session(halt, day) and all_prices_null and no_volume:
            if not has_status_evidence_id(row.get("status_evidence_id")):
                return False, "missing_status_evidence"
            return True, None
        return False, "partial_halt_missing_close"
    return False, None
