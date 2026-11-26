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
EXECUTION_RULE_VERSIONS = (NEXT_OPEN_RULES, RETRY_INITIAL_RULES, ACTUAL_OPEN_RULES)
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


def halt_conflicts_with_volume(halt: object, day: str, volume: object) -> bool:
    """Return whether reported trades contradict a whole-session halt."""
    return (
        isinstance(volume, (int, float))
        and not isinstance(volume, bool)
        and math.isfinite(volume)
        and volume > 0
        and halt_covers_session(halt, day)
    )
