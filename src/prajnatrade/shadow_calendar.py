"""Trading dates authorized independently of daily market data."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from prajnatrade.snapshot_baseline import CN_TZ

OPEN = time(9, 30)
CLOSE = time(15, 0)
SCHEMA_VERSION = 1
DATA_KINDS = ("synthetic", "real")


def validate_calendar(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version", "data_kind", "source", "sessions"
    }:
        raise ValueError("Supply schema_version, data_kind, source and sessions in the calendar.")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != SCHEMA_VERSION:
        raise ValueError("The trading calendar must declare schema_version 1.")
    if payload["data_kind"] not in DATA_KINDS:
        raise ValueError("The calendar data_kind must be synthetic or real.")
    if not isinstance(payload["source"], str) or not payload["source"].strip():
        raise ValueError("The calendar requires a source.")
    sessions = payload["sessions"]
    if not isinstance(sessions, list) or not sessions:
        raise ValueError("The calendar requires at least one trading session.")
    for session in sessions:
        if not isinstance(session, str) or date.fromisoformat(session).isoformat() != session:
            raise ValueError("Calendar sessions must use YYYY-MM-DD.")
    if any(earlier >= later for earlier, later in zip(sessions, sessions[1:], strict=False)):
        raise ValueError("Calendar sessions must be unique and strictly increasing.")
    return payload


def load_calendar(path: str | Path) -> dict[str, Any]:
    """Read a trading calendar from an operator-provided JSON file."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return validate_calendar(payload)


def authorized_sessions_match(
    sessions: Sequence[str], authorized: Sequence[str], first: str,
) -> bool:
    """Compare trading dates from enablement through the shared calendar horizon."""
    through = min(sessions[-1], authorized[-1])
    return [day for day in sessions if first <= day <= through] == [
        day for day in authorized if first <= day <= through
    ]


def parse_timestamp(value: object, field: str) -> datetime:
    """Read a timezone-aware timestamp from Shadow Live operator data."""
    if not isinstance(value, str):
        raise ValueError(f"Missing or invalid {field}; expected an ISO timestamp.")
    try:
        instant = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"Invalid {field}.") from error
    if instant.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone.")
    return instant


def session_open(session: str) -> datetime:
    return datetime.combine(date.fromisoformat(session), OPEN, tzinfo=CN_TZ)


def session_close(session: str) -> datetime:
    return datetime.combine(date.fromisoformat(session), CLOSE, tzinfo=CN_TZ)
