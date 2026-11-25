"""Trading-session status rules shared by acquisition and snapshot consumers."""

from datetime import datetime, timedelta, timezone

_SHANGHAI = timezone(timedelta(hours=8))


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
