"""Shadow Live: one atomically committed, safely rerunnable trading day (#32).

Shadow Live is **virtual accounting only**.  It never connects to a broker,
never sends a real order. Trading data comes from a scope-matched feed.  A *version* is an
immutable approved release ID from :mod:`prajnatrade.release`; every version has
its own cash, positions, orders, fees, dividends and NAV.  Prices may be shared,
money never is.

The operator runs ``prajnatrade shadow update`` once per session after the
close.  For every tracked version the command backfills the sessions after the
last committed one, one session at a time:

1. Freeze the *complete* per-version/per-day input snapshot in SQLite **before**
   computing.  Nothing is frozen when a required input is missing; the version
   is blocked instead (no partial day, no progress advance).
2. Apply share adjustments before the open, then book cash-dividend
   entitlements for start-of-day holdings as receivables (ex-date), then settle
   the pending orders at the unadjusted open in the deterministic order sells
   before buys, then symbol.  Every order receives exactly one terminal state.
3. Value at the unadjusted close plus cash and unpaid receivables.  A missing
   held close, unresolved held-corporate-action terms or missing signal input
   blocks that version only.
4. When the release is still enabled at the session's after-close signal point,
   generate the next session's signal from data available by that session's
   15:00 Asia/Shanghai close and persist stable order IDs with the
   pre-registered submission time.
5. Commit the whole day (orders, fills, cash, positions, fees, slippage,
   dividends, NAV, signal, next orders, progress, anomalies) in one SQLite
   transaction.

A crash after the frozen input but before the day commit leaves no day row;
a rerun reuses the frozen input and produces an item-identical day.  A crash
after the commit yields ``already_committed`` on rerun and never regenerates a
signal or a fill.

Point-in-time execution rule: an order is fill-eligible only when its recorded
``submitted_at`` (the pre-registered after-close signal point) precedes the
execution session's 09:30 Asia/Shanghai deadline **and** the day transaction
that durably persisted it committed before that deadline in real wall-clock
time (``persisted_before_deadline``).  Historical catch-up therefore records
``missed_deadline`` instead of filling retroactively.

Fault injection (documented, no effect when unset)::

    PRAJNATRADE_TEST_FAILPOINT=shadow_after_fills
    PRAJNATRADE_TEST_FAILPOINT=shadow_after_commit_before_report

Ledger layout (outside Git and outside the research project)::

    <ledger>/shadow.sqlite
        input_snapshots(version_id, date, input_json, input_hash, frozen_at)
        days(version_id, date, result_json, result_hash, committed_at, status)
        progress(version_id, release_id, start_date, last_committed_date, state)

#33 extension: ``shadow update --corrections-store <snapshot-store>`` loads the
verified revisions from the snapshot store's dividend-correction journal
(``dividend.load_verified_revision``; never a sealed partition and never a query
debit).  For every tracked version the applicable revision set is fixed inside
the frozen per-day input *before* the day is computed, then applied exactly once
as ``(new_amount - old_amount) * qualified_shares``: an unpaid entitlement
adjusts the receivable, an already-paid one adjusts cash.  Corrections are
reported separately from strategy P&L and a correction-caused negative cash
balance is recorded as a deficit (never financed, never force-sold).  Dedupe is
by ``(version_id, action_id, revision_id)`` across committed days, so a crash
before the commit and a rerun after it never double-apply a delta.
#34 adds ``shadow.compare(ledger, data, versions)``: a read-only comparison of
the longest common, consecutive run of committed forward sessions.  It reads
only verified committed ledger rows, never interpolates, never mixes backtest
data, rebases every version to the run's first closing NAV and reports the run
fees, dividends, unfilled orders, correction impact and cash deficits next to a
``strategy_return`` that excludes the run's correction impact.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from prajnatrade import dividend, release, shadow_calendar, snapshot
from prajnatrade.backtest import _validate_weights
from prajnatrade.container_executor import executor_from_config
from prajnatrade.data import CASH, ETF_SYMBOLS
from prajnatrade.experiment import runtime_matches
from prajnatrade.isolation import IsolationError
from prajnatrade.snapshot_baseline import (
    _is_executable_open,
    _valid_price,
    available_by,
)
from prajnatrade.strategies import (
    STRATEGY_FAMILIES,
    make_strategy,
    normalize_strategy_spec,
)

SCHEMA_VERSION = 1
SCOPE = "synthetic"
DATA_KIND = "synthetic"
INPUT_KIND = "shadow_live_input"
DAY_KIND = "shadow_live_day"
REPORT_KIND = "shadow_live_ledger_report"
REPORT_SCOPE = "synthetic"
COMPARISON_KIND = "shadow_forward_comparison"
ACTIONS = ("cash_dividend", "share_adjustment")
TRADING_STATUSES = ("trading", "suspended", "unknown")
ORDER_STATUSES = (
    "filled",
    "missing_open",
    "missed_deadline",
    "insufficient_cash",
    "insufficient_shares",
)
FAILPOINTS = ("shadow_after_fills", "shadow_after_commit_before_report")
SCOPE_PAUSE_SCOPES = ("feed", "execution", "ledger", "version")
_FEED_BLOCK_CODES = frozenset(
    {
        "missing_close",
        "missing_signal_input",
        "missing_action_terms",
        "signal_window_incomplete",
        "open_evidence_missing",
        "FEED_SCOPE_MISMATCH",
        "FEED_CALENDAR_MISMATCH",
        "CALENDAR_HORIZON_EXHAUSTED",
        "AUTHORIZED_CALENDAR_MISMATCH",
    }
)
_EXECUTION_BLOCK_CODES = frozenset(
    {"missing_open", "strategy_error", "strategy_isolation_failed"}
)
_LEDGER_BLOCK_CODES = frozenset(
    {
        "FROZEN_INPUT_CHANGED",
        "PROGRESS_CONFLICT",
        "PROGRESS_MISMATCH",
        "DAY_ALREADY_COMMITTED",
        "LEDGER_INTEGRITY_FAILED",
        "CORRECTION_BOOKING_MISMATCH",
    }
)

_EPSILON = 1e-9
_RELEASE_ID = re.compile(r"[0-9a-f]{64}")



class ShadowRejection(Exception):
    """A machine-readable Shadow Live refusal (never a strategy failure)."""

    def __init__(
        self,
        code: str,
        phase: str,
        next_action: str,
        message: str,
        *,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.details = {
            "code": code,
            "phase": phase,
            "next_action": next_action,
            "message": message,
        }
        self.context = dict(context or {})


class ShadowFaultInjection(RuntimeError):
    """Deterministic test failpoint; never raised unless explicitly requested."""


class _Blocked(Exception):
    """A per-version block: the version writes no day row and does not advance."""

    def __init__(self, code: str, reason: str, *, detail: dict[str, Any] | None = None):
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.detail = dict(detail or {})


def _reject(code: str, phase: str, next_action: str, message: str) -> ShadowRejection:
    return ShadowRejection(code, phase, next_action, message)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _hash(value: object) -> str:
    return snapshot._hash(value)


def _failpoint(name: str) -> None:
    if os.environ.get("PRAJNATRADE_TEST_FAILPOINT") == name:
        raise ShadowFaultInjection(f"shadow test failpoint reached: {name}")


# ---------------------------------------------------------------------------
# Feed validation and point-in-time evidence
# ---------------------------------------------------------------------------


def _session_open(session: str) -> datetime:
    return shadow_calendar.session_open(session)


def _session_close(session: str) -> datetime:
    return shadow_calendar.session_close(session)


def _signal_point(session: str) -> str:
    return _session_close(session).isoformat()


def _evidence(row: dict[str, Any], label: str) -> None:
    shadow_calendar.parse_timestamp(row.get("available_at"), f"{label}.available_at")
    evidence = row.get("availability_evidence")
    if not isinstance(evidence, str) or not evidence.strip():
        raise ValueError(f"{label} requires nonempty availability_evidence.")


def _optional_number(value: object, label: str, *, positive: bool) -> None:
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or (value <= 0 if positive else value < 0)
    ):
        raise ValueError(f"{label} must be a {'positive' if positive else 'nonnegative'} finite number or null.")


class Feed:
    """A validated, scope-labelled Shadow Live feed."""

    def __init__(self, payload: object) -> None:
        if not isinstance(payload, dict):
            raise ValueError("The Shadow Live feed must be a JSON object.")
        kind_version = (payload.get("data_kind"), payload.get("schema_version"))
        if kind_version not in ((DATA_KIND, SCHEMA_VERSION), ("real", 2)):
            raise ValueError("The Shadow Live feed must declare synthetic v1 or real v2.")
        self.data_kind = kind_version[0]
        is_real = self.data_kind == "real"
        if is_real and payload.get("raw_price_basis") != "unadjusted":
            raise ValueError("A real Shadow Live feed requires unadjusted raw prices.")
        trading_statuses = snapshot.REAL_STATUSES if is_real else TRADING_STATUSES
        symbols = payload.get("symbols")
        if (
            not isinstance(symbols, list)
            or len(symbols) != len(ETF_SYMBOLS)
            or set(symbols) != set(ETF_SYMBOLS)
            or any(not isinstance(symbol, str) for symbol in symbols)
        ):
            raise ValueError(
                "The Shadow Live feed must declare exactly the five ETF symbols."
            )
        self.symbols = tuple(ETF_SYMBOLS)

        calendar = payload.get("calendar")
        if not isinstance(calendar, list) or not calendar:
            raise ValueError("The Shadow Live feed requires a nonempty calendar.")
        sessions: list[str] = []
        self.calendar: dict[str, dict[str, Any]] = {}
        for row in calendar:
            if not isinstance(row, dict):
                raise ValueError("Calendar rows must be objects.")
            try:
                session = snapshot._date(row.get("date"))
            except ValueError as error:
                raise ValueError(f"Invalid calendar row: {error}") from error
            _evidence(row, f"calendar {session}")
            if sessions and session <= sessions[-1]:
                raise ValueError("Calendar sessions must be unique and strictly increasing.")
            sessions.append(session)
            self.calendar[session] = row
        self.sessions = tuple(sessions)
        self.index = {session: index for index, session in enumerate(sessions)}
        session_set = set(sessions)

        raw_rows = payload.get("raw_bars")
        if not isinstance(raw_rows, list) or not raw_rows:
            raise ValueError("The Shadow Live feed requires raw_bars.")
        self.raw: dict[tuple[str, str], dict[str, Any]] = {}
        for row in raw_rows:
            if not isinstance(row, dict):
                raise ValueError("raw_bars rows must be objects.")
            session = _row_date(row, session_set, "raw_bars")
            symbol = _row_symbol(row, "raw_bars")
            _evidence(row, f"raw_bars {session} {symbol}")
            if row.get("trading_status") not in trading_statuses:
                raise ValueError(
                    f"Invalid trading_status in raw_bars for {session} {symbol}."
                )
            for field in ("open", "high", "low", "close"):
                if field not in row:
                    raise ValueError(
                        f"raw_bars {session} {symbol} is missing {field}; use null for a gap."
                    )
                _optional_number(row[field], f"raw_bars {field} {session} {symbol}", positive=True)
            if "volume" not in row:
                raise ValueError(
                    f"raw_bars {session} {symbol} is missing volume; use null for a gap."
                )
            _optional_number(row["volume"], f"raw_bars volume {session} {symbol}", positive=False)
            prices = [value for value in (row["open"], row["close"]) if value is not None]
            high, low = row["high"], row["low"]
            if (
                (high is not None and any(high < price for price in prices))
                or (low is not None and any(low > price for price in prices))
                or (high is not None and low is not None and high < low)
            ):
                raise ValueError(f"Invalid OHLC values in raw_bars for {session} {symbol}.")
            if is_real:
                _real_raw_timestamps(row)
            key = (session, symbol)
            if key in self.raw:
                raise ValueError(f"Duplicate raw_bars row for {session} {symbol}.")
            self.raw[key] = row

        derived_rows = payload.get("derived")
        if not isinstance(derived_rows, list) or not derived_rows:
            raise ValueError("The Shadow Live feed requires derived rows.")
        self.derived: dict[tuple[str, str], dict[str, Any]] = {}
        for row in derived_rows:
            if not isinstance(row, dict):
                raise ValueError("derived rows must be objects.")
            session = _row_date(row, session_set, "derived")
            symbol = _row_symbol(row, "derived")
            _evidence(row, f"derived {session} {symbol}")
            if "value" not in row:
                raise ValueError(
                    f"derived {session} {symbol} is missing value; use null for a gap."
                )
            _optional_number(row["value"], f"derived value {session} {symbol}", positive=True)
            key = (session, symbol)
            if key in self.derived:
                raise ValueError(f"Duplicate derived row for {session} {symbol}.")
            self.derived[key] = row

        action_rows = payload.get("corporate_actions", [])
        if not isinstance(action_rows, list):
            raise ValueError("corporate_actions must be an array.")
        actions: list[dict[str, Any]] = []
        action_ids: set[str] = set()
        for raw_action in action_rows:
            if not isinstance(raw_action, dict):
                raise ValueError("corporate_actions rows must be objects.")
            session = _row_date(raw_action, session_set, "corporate_actions")
            symbol = _row_symbol(raw_action, "corporate_actions")
            kind = raw_action.get("type")
            if kind not in ACTIONS:
                raise ValueError(f"Invalid corporate action type for {session} {symbol}.")
            _evidence(raw_action, f"corporate_actions {session} {symbol}")
            action_id = raw_action.get("action_id")
            if action_id is None:
                action_id = f"{kind}:{symbol}:{session}"
            if not isinstance(action_id, str) or not action_id.strip():
                raise ValueError("action_id must be a nonempty string when supplied.")
            if action_id in action_ids:
                raise ValueError(f"Duplicate corporate action id {action_id!r}.")
            action_ids.add(action_id)
            action = dict(raw_action)
            action["action_id"] = action_id
            action["date"] = session
            action["symbol"] = symbol
            if kind == "cash_dividend":
                if "cash_per_share" not in action:
                    raise ValueError(f"{action_id} is missing cash_per_share; use null for unknown terms.")
                _optional_number(action["cash_per_share"], f"{action_id} cash_per_share", positive=True)
                if "pay_date" not in action:
                    raise ValueError(f"{action_id} is missing pay_date; use null for unknown terms.")
                pay_date = action.get("pay_date")
                if pay_date is not None:
                    pay_date = snapshot._date(pay_date)
                    if pay_date < session:
                        raise ValueError(f"{action_id} pay_date precedes its ex-date.")
                    action["pay_date"] = pay_date
            else:
                if "ratio" not in action:
                    raise ValueError(f"{action_id} is missing ratio; use null for unknown terms.")
                _optional_number(action["ratio"], f"{action_id} ratio", positive=True)
            evidence = action.get("terms_evidence")
            if evidence is not None and (
                not isinstance(evidence, str) or not evidence.strip()
            ):
                raise ValueError(f"{action_id} has invalid terms_evidence.")
            actions.append(action)
        actions.sort(key=lambda item: (item["date"], item["symbol"], item["type"], item["action_id"]))
        self.actions = tuple(actions)
        self.actions_by_date: dict[str, tuple[dict[str, Any], ...]] = {}
        for action in actions:
            self.actions_by_date.setdefault(action["date"], ())
            self.actions_by_date[action["date"]] = (
                *self.actions_by_date[action["date"]],
                action,
            )

    def next_session(self, session: str) -> str | None:
        index = self.index[session]
        if index + 1 >= len(self.sessions):
            return None
        return self.sessions[index + 1]


def _row_date(row: dict, sessions: set[str], kind: str) -> str:
    try:
        session = snapshot._date(row.get("date"))
    except ValueError as error:
        raise ValueError(f"Invalid {kind} row date: {error}") from error
    if session not in sessions:
        raise ValueError(f"{kind} row date {session} is outside the feed calendar.")
    return session


def _row_symbol(row: dict, kind: str) -> str:
    symbol = row.get("symbol")
    if symbol not in ETF_SYMBOLS:
        raise ValueError(f"Invalid symbol in {kind}.")
    return symbol


def load_feed(path: str | Path) -> Feed:
    try:
        payload = snapshot._read_json(Path(path))
    except (OSError, UnicodeError, ValueError) as error:
        raise _reject(
            "INVALID_FEED",
            "feed",
            "correct",
            f"The Shadow Live feed is unreadable: {error}",
        ) from error
    try:
        return Feed(payload)
    except ValueError as error:
        raise _reject("INVALID_FEED", "feed", "correct", str(error)) from error


def _real_raw_timestamps(row: dict[str, Any]) -> None:
    for field in ("open", "close"):
        timestamp = f"{field}_available_at"
        if timestamp not in row:
            raise ValueError(f"A real raw bar requires {timestamp}.")
        if row[field] is not None or row[timestamp] is not None:
            shadow_calendar.parse_timestamp(row[timestamp], timestamp)


def _before(value: str | None, cutoff: datetime) -> bool:
    if not value:
        return False
    try:
        instant = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    return instant.utcoffset() is not None and instant < cutoff


def _valid_close(
    row: dict | None, cutoff: datetime, *, data_kind: str = DATA_KIND
) -> bool:
    if row is None or not _valid_price(row.get("close")):
        return False
    if data_kind == "real":
        return row.get("trading_status") in ("trading", "partial_halt") and available_by(
            {**row, "available_at": row["close_available_at"]}, cutoff
        )
    return row.get("trading_status") == "trading" and available_by(row, cutoff)


def _terms_valid(action: dict) -> bool:
    evidence = action.get("terms_evidence")
    if not isinstance(evidence, str) or not evidence.strip():
        return False
    if action["type"] == "cash_dividend":
        amount = action.get("cash_per_share")
        pay_date = action.get("pay_date")
        return (
            _valid_price(amount)
            and isinstance(pay_date, str)
            and pay_date >= action["date"]
        )
    return _valid_price(action.get("ratio"))


# ---------------------------------------------------------------------------
# SQLite ledger
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS input_snapshots (
  version_id TEXT NOT NULL,
  date TEXT NOT NULL,
  input_json TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  frozen_at TEXT NOT NULL,
  PRIMARY KEY (version_id, date)
);
CREATE TABLE IF NOT EXISTS days (
  version_id TEXT NOT NULL,
  date TEXT NOT NULL,
  result_json TEXT NOT NULL,
  result_hash TEXT NOT NULL,
  committed_at TEXT NOT NULL,
  status TEXT NOT NULL,
  PRIMARY KEY (version_id, date)
);
CREATE TABLE IF NOT EXISTS progress (
  version_id TEXT PRIMARY KEY,
  release_id TEXT NOT NULL,
  start_date TEXT,
  last_committed_date TEXT,
  state TEXT
);
CREATE TABLE IF NOT EXISTS schedule_notifications (
  scope TEXT NOT NULL,
  release_id TEXT NOT NULL,
  date TEXT NOT NULL,
  code TEXT NOT NULL,
  message TEXT NOT NULL,
  next_action TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  PRIMARY KEY (scope, release_id, date, code)
);
CREATE TABLE IF NOT EXISTS scope_pauses (
  version_id TEXT NOT NULL,
  session TEXT NOT NULL,
  scope TEXT NOT NULL,
  code TEXT NOT NULL,
  message TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  PRIMARY KEY (version_id, session, scope, code)
);
"""


def _canonical(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


class Ledger:
    """The per-version Shadow Live ledger; days are keyed by version and session."""

    def __init__(self, path: str | Path) -> None:
        self.directory = Path(path)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database = self.directory / "shadow.sqlite"
        self.connection = sqlite3.connect(
            self.database, isolation_level=None, timeout=30.0
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout = 30000")
        self.connection.executescript(_SCHEMA)

    def close(self) -> None:
        self.connection.close()

    def schedule_notifications(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM schedule_notifications ORDER BY observed_at, date, release_id, code"
        )
        return [dict(row) for row in rows]

    def notify_schedule(self, notice: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        inserted = self.connection.execute(
            "INSERT OR IGNORE INTO schedule_notifications "
            "(scope, release_id, date, code, message, next_action, observed_at) "
            "VALUES (:scope, :release_id, :date, :code, :message, :next_action, :observed_at)",
            notice,
        )
        row = self.connection.execute(
            "SELECT * FROM schedule_notifications "
            "WHERE scope = :scope AND release_id = :release_id AND date = :date AND code = :code",
            notice,
        ).fetchone()
        return dict(row), inserted.rowcount == 1

    def scope_pauses(self, version_id: str | None = None) -> list[dict[str, Any]]:
        """Per-version scoped pauses written when a version blocked (T3)."""
        if version_id is None:
            rows = self.connection.execute(
                "SELECT version_id, session, scope, code, message, recorded_at "
                "FROM scope_pauses ORDER BY version_id, session, scope, code"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT version_id, session, scope, code, message, recorded_at "
                "FROM scope_pauses WHERE version_id = ? "
                "ORDER BY session, scope, code",
                (version_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_scope_pause(
        self,
        version_id: str,
        session: str,
        scope: str,
        code: str,
        message: str,
        recorded_at: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Persist one scoped pause; repeated ticks with the same facts dedupe."""
        notice = {
            "version_id": version_id,
            "session": session,
            "scope": scope,
            "code": code,
            "message": message,
            "recorded_at": recorded_at or _now(),
        }
        inserted = self.connection.execute(
            "INSERT OR IGNORE INTO scope_pauses "
            "(version_id, session, scope, code, message, recorded_at) "
            "VALUES (:version_id, :session, :scope, :code, :message, :recorded_at)",
            notice,
        )
        row = self.connection.execute(
            "SELECT version_id, session, scope, code, message, recorded_at "
            "FROM scope_pauses WHERE version_id = :version_id AND session = :session "
            "AND scope = :scope AND code = :code",
            notice,
        ).fetchone()
        return dict(row), inserted.rowcount == 1

    # -- reads ---------------------------------------------------------

    def get_progress(self, version_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT version_id, release_id, start_date, last_committed_date, state "
            "FROM progress WHERE version_id = ?",
            (version_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_day_row(self, version_id: str, session: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT version_id, date, result_json, result_hash, committed_at, status "
            "FROM days WHERE version_id = ? AND date = ?",
            (version_id, session),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_input_row(self, version_id: str, session: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT version_id, date, input_json, input_hash, frozen_at "
            "FROM input_snapshots WHERE version_id = ? AND date = ?",
            (version_id, session),
        ).fetchone()
        return dict(row) if row is not None else None

    def verified_input(self, version_id: str, session: str) -> dict[str, Any] | None:
        row = self.get_input_row(version_id, session)
        if row is None:
            return None
        try:
            body = json.loads(row["input_json"])
        except (TypeError, ValueError) as error:
            raise _integrity(
                f"The frozen Shadow Live input for {session} is unreadable: {error}"
            ) from error
        if not isinstance(body, dict) or _hash(body) != row["input_hash"]:
            raise _integrity(
                f"The frozen Shadow Live input for {session} failed its content-address check."
            )
        if body.get("version_id") != version_id or body.get("date") != session:
            raise _integrity(
                f"The frozen Shadow Live input for {session} is bound to another version or date."
            )
        return {
            "version_id": version_id,
            "date": session,
            "input": body,
            "input_hash": row["input_hash"],
            "frozen_at": row["frozen_at"],
        }

    def verified_day(self, version_id: str, session: str) -> dict[str, Any] | None:
        row = self.get_day_row(version_id, session)
        if row is None:
            return None
        try:
            body = json.loads(row["result_json"])
        except (TypeError, ValueError) as error:
            raise _integrity(
                f"The committed Shadow Live day {session} is unreadable: {error}"
            ) from error
        if not isinstance(body, dict) or _hash(body) != row["result_hash"]:
            raise _integrity(
                f"The committed Shadow Live day {session} failed its content-address check."
            )
        if body.get("version_id") != version_id or body.get("date") != session:
            raise _integrity(
                f"The committed Shadow Live day {session} is bound to another version or date."
            )
        if body.get("status") != row["status"]:
            raise _integrity(
                f"The committed Shadow Live day {session} has an inconsistent status column."
            )
        frozen = self.verified_input(version_id, session)
        if frozen is None:
            raise _integrity(
                f"The committed Shadow Live day {session} has no frozen input snapshot."
            )
        if body.get("input_hash") != frozen["input_hash"]:
            raise _integrity(
                f"The committed Shadow Live day {session} is not bound to its frozen input."
            )
        return {**row, "result": body}

    # -- writes --------------------------------------------------------

    def _begin(self) -> None:
        self.connection.execute("BEGIN IMMEDIATE")

    def _rollback(self) -> None:
        try:
            self.connection.execute("ROLLBACK")
        except sqlite3.OperationalError:  # pragma: no cover - no transaction open
            pass

    def freeze_input(
        self, version_id: str, session: str, body: dict[str, Any], input_hash: str
    ) -> bool:
        """Freeze one complete input snapshot before computing; never overwrite."""
        self._begin()
        try:
            existing = self.connection.execute(
                "SELECT input_hash FROM input_snapshots WHERE version_id = ? AND date = ?",
                (version_id, session),
            ).fetchone()
            if existing is not None:
                self.connection.execute("COMMIT")
                return False
            self.connection.execute(
                "INSERT INTO input_snapshots "
                "(version_id, date, input_json, input_hash, frozen_at) VALUES (?, ?, ?, ?, ?)",
                (version_id, session, _canonical(body), input_hash, _now()),
            )
            self.connection.execute("COMMIT")
            return True
        except BaseException:
            self._rollback()
            raise

    def delete_input(
        self, version_id: str, session: str, input_hash: str | None = None
    ) -> None:
        """Remove a never-committed input snapshot after a compute-time block."""
        self._begin()
        try:
            self.connection.execute(
                "DELETE FROM input_snapshots WHERE version_id = ? AND date = ? "
                "AND (? IS NULL OR input_hash = ?) AND NOT EXISTS "
                "(SELECT 1 FROM days WHERE version_id = ? AND date = ?)",
                (version_id, session, input_hash, input_hash, version_id, session),
            )
            self.connection.execute("COMMIT")
        except BaseException:
            self._rollback()
            raise

    def commit_day(
        self,
        version_id: str,
        release_id: str,
        session: str,
        result: dict[str, Any],
        expected_previous: str | None,
        start_date: str,
        state: str,
        committed_at: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        """Return the committed day with its persistence timestamp and deadline evidence."""
        self._begin()
        try:
            existing = self.connection.execute(
                "SELECT 1 FROM days WHERE version_id = ? AND date = ?",
                (version_id, session),
            ).fetchone()
            if existing is not None:
                raise _Blocked(
                    "DAY_ALREADY_COMMITTED",
                    f"{session} is already committed; a day transaction is never replaced.",
                )
            progress = self.connection.execute(
                "SELECT last_committed_date FROM progress WHERE version_id = ?",
                (version_id,),
            ).fetchone()
            current = progress["last_committed_date"] if progress is not None else None
            if current != expected_previous:
                raise _Blocked(
                    "PROGRESS_CONFLICT",
                    f"Progress for the version is {current!r}, not the expected "
                    f"{expected_previous!r}; refusing to skip or rewrite a session.",
                )
            if progress is None:
                self.connection.execute(
                    "INSERT INTO progress "
                    "(version_id, release_id, start_date, last_committed_date, state) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (version_id, release_id, start_date, session, state),
                )
            else:
                self.connection.execute(
                    "UPDATE progress SET last_committed_date = ?, state = ? "
                    "WHERE version_id = ?",
                    (session, state, version_id),
                )
            persisted_at = committed_at if committed_at is not None else _now()
            committed = {
                **result,
                "next_orders": [
                    {
                        **order,
                        "persisted_before_deadline": _before(
                            persisted_at, datetime.fromisoformat(order["deadline"]),
                        ),
                    }
                    for order in result["next_orders"]
                ],
            }
            self.connection.execute(
                "INSERT INTO days "
                "(version_id, date, result_json, result_hash, committed_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (version_id, session, _canonical(committed), _hash(committed), persisted_at, committed["status"]),
            )
            # Scoped pauses stay as history; a pause is active only while its
            # session is still uncommitted, so a later commit resolves it
            # without rewriting the record (T3).
            self.connection.execute("COMMIT")
            return committed, persisted_at
        except BaseException:
            self._rollback()
            raise

    def end_version(self, version_id: str, release_id: str) -> dict[str, Any] | None:
        """Mark one tracking version's ledger progress ``ended`` (T3 retire).

        The old committed days stay readable and unchanged.  A later tick that
        selects the retired release sees the terminal progress row and never
        commits another day, signal or order for it.
        """
        self._begin()
        try:
            row = self.connection.execute(
                "SELECT 1 FROM progress WHERE version_id = ?", (version_id,)
            ).fetchone()
            if row is None:
                self.connection.execute(
                    "INSERT INTO progress "
                    "(version_id, release_id, start_date, last_committed_date, state) "
                    "VALUES (?, ?, NULL, NULL, 'ended')",
                    (version_id, release_id),
                )
            else:
                self.connection.execute(
                    "UPDATE progress SET state = 'ended' WHERE version_id = ?",
                    (version_id,),
                )
            self.connection.execute("COMMIT")
        except BaseException:
            self._rollback()
            raise
        return self.get_progress(version_id)

    # -- iteration -----------------------------------------------------

    def iter_days(self, version_id: str | None = None) -> Iterator[dict[str, Any]]:
        if version_id is None:
            rows = self.connection.execute(
                "SELECT version_id, date FROM days ORDER BY version_id, date"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT version_id, date FROM days WHERE version_id = ? ORDER BY date",
                (version_id,),
            ).fetchall()
        for row in rows:
            day = self.verified_day(row["version_id"], row["date"])
            assert day is not None  # row was just read
            yield day


def _integrity(message: str) -> ShadowRejection:
    return _reject(
        "LEDGER_INTEGRITY_FAILED",
        "ledger",
        "inspect",
        message,
    )


def iter_committed_days(
    ledger: str | Path, version_id: str | None = None
) -> Iterator[dict[str, Any]]:
    """Yield verified committed day records (the #33/#34 read hook)."""
    return Ledger(ledger).iter_days(version_id)


def first_uncommitted_signal_date(
    ledger: str | Path,
    data: str | Path,
    version_id: str,
    *,
    not_before: str | None = None,
    after: str | None = None,
) -> str | None:
    """First feed session whose after-close signal point is not committed (#31).

    The disposition workflow derives its pause point from this hook instead of
    duplicating the day engine.  With history it is the feed session after the
    version's last committed session; without history it is the first session
    whose after-close point follows ``after`` (the revision filing instant) or
    now, whichever is later.  ``not_before`` (a pre-registered enable/resume
    date) skips sessions that would move the release timeline backwards.
    Returns ``None`` when no session qualifies.  The ledger is never created:
    an absent ledger is simply no history.
    """
    if not isinstance(version_id, str) or not _RELEASE_ID.fullmatch(version_id):
        raise _reject(
            "INVALID_RELEASE_ID",
            "disposition",
            "correct",
            "Release IDs are 64-character lowercase hexadecimal content addresses.",
        )
    feed = load_feed(data)
    bound: str | None = None
    if not_before is not None:
        try:
            bound = snapshot._date(not_before)
        except ValueError as error:
            raise _reject(
                "INVALID_DATE", "disposition", "correct", str(error)
            ) from error
    progress = None
    if (Path(ledger) / "shadow.sqlite").is_file():
        progress = Ledger(ledger).get_progress(version_id)
    last = progress.get("last_committed_date") if progress is not None else None
    if isinstance(last, str) and last:
        if last not in feed.index:
            raise _reject(
                "FEED_CALENDAR_MISMATCH",
                "disposition",
                "inspect",
                f"The last committed session {last} is not in this feed's calendar; "
                "refusing to guess the next session.",
            )
        start = feed.index[last] + 1
    else:
        reference = datetime.now(UTC)
        if after is not None:
            try:
                filed = shadow_calendar.parse_timestamp(after, "filed_at")
            except ValueError as error:
                raise _reject(
                    "INVALID_DATE", "disposition", "correct", str(error)
                ) from error
            reference = max(reference, filed)
        start = next(
            (
                index
                for index, session in enumerate(feed.sessions)
                if _session_close(session) > reference
            ),
            len(feed.sessions),
        )
    for session in feed.sessions[start:]:
        if bound is not None and session < bound:
            continue
        return session
    return None


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def update(
    ledger: str | Path,
    store: str | Path,
    data: str | Path,
    target_date: str,
    release_ids: list[str] | tuple[str, ...] | None = None,
    *,
    corrections_store: str | Path | None = None,
    expected_sessions: tuple[str, ...] | None = None,
    expected_data_kind: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Process every tracked version up to ``target_date``; return (report, blocked).

    ``corrections_store`` is the trusted snapshot store that owns the verified
    dividend-correction journal.  Only ``.dividend-corrections/revision-*.json``
    entries are read, through :func:`prajnatrade.dividend.load_verified_revision`;
    no sealed partition payload is opened and no query is debited.

    The operator CLI requires ``--corrections-store`` so an eligible correction
    cannot be silently skipped and pushed into a later session.  This Python
    entry point keeps ``corrections_store=None`` for trusted callers that
    deliberately run without the journal; such a run applies no correction.
    """
    feed = load_feed(data)
    if expected_data_kind is not None and expected_data_kind != feed.data_kind:
        raise _reject(
            "FEED_SCOPE_MISMATCH", "schedule", "correct",
            f"The trading calendar requires a {expected_data_kind} Shadow Live feed.",
        )
    if expected_sessions:
        expected_start, expected_end = expected_sessions[0], expected_sessions[-1]
        feed_sessions = tuple(
            session for session in feed.sessions if expected_start <= session <= expected_end
        )
        if feed_sessions != expected_sessions:
            raise _reject(
                "FEED_CALENDAR_MISMATCH", "schedule", "correct",
                "The feed must include every scheduled session through the next open "
                "in the operator's trading calendar.",
            )
    try:
        target = snapshot._date(target_date)
    except ValueError as error:
        raise _reject("INVALID_DATE", "select", "correct", str(error)) from error
    if target not in feed.index:
        raise _reject(
            "NOT_A_SESSION",
            "select",
            "correct",
            f"{target} is not a session in the synthetic Shadow Live feed; a "
            "non-session is never invented as a trading day.",
        )
    if feed.data_kind == "real" and datetime.now(UTC) < _session_close(target):
        raise _reject(
            "SESSION_NOT_CLOSED", "select", "wait",
            "The real trading session must close before Shadow Live accounts for it.",
        )
    store_path = Path(store)
    if not store_path.is_dir():
        raise _reject(
            "RELEASE_STORE_UNAVAILABLE",
            "select",
            "correct",
            f"The trusted release store {store_path} is not a directory; Shadow "
            "Live never falls back to a workspace copy or another store.",
        )
    corrections = (
        _corrections_context(corrections_store)
        if corrections_store is not None
        else {"enabled": False, "store": None, "revisions": (), "ignored": []}
    )
    database = Ledger(ledger)
    requested = list(release_ids or [])
    if requested:
        for release_id in requested:
            if not isinstance(release_id, str) or not _RELEASE_ID.fullmatch(release_id):
                raise _reject(
                    "INVALID_RELEASE_ID",
                    "select",
                    "correct",
                    "Release IDs are 64-character lowercase hexadecimal content addresses.",
                )
        candidates = requested
    else:
        candidates = _tracked_release_directories(database, store_path, target)
    versions = [
        _version_result(database, store_path, feed, target, release_id, corrections)
        for release_id in candidates
    ]
    blocked = any(version["status"] == "blocked" for version in versions)
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "scope": feed.data_kind,
        "target_date": target,
        "completed_at": _now(),
        "versions": versions,
    }
    if corrections["enabled"]:
        report["corrections"] = {
            "store": corrections["store"],
            "revisions_loaded": len(corrections["revisions"]),
            "ignored": corrections["ignored"],
        }
    return report, blocked


def _corrections_context(store: str | Path) -> dict[str, Any]:
    """Load verified revisions once per update; never a sealed read or debit.

    ``dividend.load_verified_revision`` is the only reader: it verifies the
    journal record's ``verified`` status and its revision content address.  An
    unreadable, unverified or tampered journal entry is ignored with a stable
    code in the report (it is never evidence and never blocks the ledger); only
    an unavailable corrections store is a request rejection.
    """
    path = Path(store)
    if not path.is_dir():
        raise _reject(
            "CORRECTIONS_STORE_UNAVAILABLE",
            "corrections",
            "correct",
            f"The correction snapshot store {path} is not a directory; verified "
            "revisions are never read from an unverifiable location.",
        )
    journal = path / ".dividend-corrections"
    records: dict[str, dict[str, Any]] = {}
    ignored: list[dict[str, str]] = []
    if journal.is_dir():
        for entry in sorted(journal.iterdir(), key=lambda item: item.name):
            name = entry.name
            if not name.startswith("revision-") or not name.endswith(".json"):
                continue
            token = name[len("revision-") : -len(".json")]
            if not entry.is_file() or not re.fullmatch(r"[0-9a-f]{64}", token):
                ignored.append(
                    {
                        "file": name,
                        "code": "REVISION_INVALID",
                        "message": "Revision journal names are revision-<64 hex>.json.",
                    }
                )
                continue
            try:
                record = dividend.load_verified_revision(path, token)
            except dividend.RevisionError as error:
                ignored.append(
                    {"file": name, "code": error.code, "message": str(error)}
                )
                continue
            except ValueError as error:  # defensive: loader validates its inputs
                ignored.append(
                    {"file": name, "code": "REVISION_INVALID", "message": str(error)}
                )
                continue
            records.setdefault(record["revision_id"], record)
    return {
        "enabled": True,
        "store": str(path),
        "revisions": tuple(records[key] for key in sorted(records)),
        "ignored": ignored,
    }


def write_report(
    ledger: str | Path, version_id: str, output: str | Path
) -> dict[str, Any]:
    """Write the trusted audit dump of every committed day of one version."""
    if not isinstance(version_id, str) or not _RELEASE_ID.fullmatch(version_id):
        raise _reject(
            "INVALID_RELEASE_ID",
            "report",
            "correct",
            "Release IDs are 64-character lowercase hexadecimal content addresses.",
        )
    database = Ledger(ledger)
    days = list(database.iter_days(version_id))
    progress = database.get_progress(version_id)
    entries = [
        {
            "date": day["date"],
            "status": day["status"],
            "committed_at": day["committed_at"],
            "result_hash": day["result_hash"],
            "input_hash": day["result"]["input_hash"],
            "verified": True,
            "result": day["result"],
        }
        for day in days
    ]
    summary = _report_summary(entries, progress)
    scope = days[0]["result"]["scope"] if days else REPORT_SCOPE
    report = {
        "schema_version": SCHEMA_VERSION,
        "kind": REPORT_KIND,
        "scope": scope,
        "version_id": version_id,
        "generated_at": _now(),
        "integrity": {
            "verified": True,
            "days_checked": len(entries),
            "violations": [],
        },
        "progress": progress,
        "summary": summary,
        "days": entries,
    }
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "written",
        "kind": REPORT_KIND,
        "scope": scope,
        "version_id": version_id,
        "output": str(destination),
        "days": summary["days"],
        "first_committed_date": summary["first_committed_date"],
        "last_committed_date": summary["last_committed_date"],
        "ending_nav": summary["ending_nav"],
        "corrections_total": summary["corrections_total"],
        "correction_impact_total": summary["correction_impact_total"],
        "integrity": {
            "verified": True,
            "days_checked": len(entries),
            "violations": [],
        },
        "integrity_verified": True,
    }


def _opportunity_evidence(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Join stored due opportunities to the execution day's terminal orders (T3).

    An opportunity is persisted inside the signal day's atomic body.  Its
    outcome is never written back into that committed day: the fill/unfilled
    sets are joined here from the execution day's order rows.  ``due`` counts
    every recorded intent, ``filled`` counts opportunities whose orders all
    filled, ``unfilled`` counts opportunities with at least one terminal
    non-fill, and ``intent_without_fill`` counts fully resolved opportunities
    with no fill at all (the state that is distinct from ``metrics_not_ready``).
    """
    order_status: dict[str, str] = {}
    for result in results:
        for order in result.get("orders", ()):
            order_status[order["order_id"]] = order["status"]
    ordered = sorted(results, key=lambda result: result["date"])
    records: list[dict[str, Any]] = []
    for result in ordered:
        for stored in result.get("opportunities", ()):
            order_ids = list(stored.get("order_ids", ()))
            filled = [
                order_id
                for order_id in order_ids
                if order_status.get(order_id) == "filled"
            ]
            unfilled = [
                order_id
                for order_id in order_ids
                if order_id in order_status and order_status[order_id] != "filled"
            ]
            unresolved = [
                order_id for order_id in order_ids if order_id not in order_status
            ]
            records.append(
                {
                    "signal_date": stored["signal_date"],
                    "execution_date": stored["execution_date"],
                    "order_ids": order_ids,
                    "due": bool(stored.get("due", True)),
                    "filled": filled,
                    "unfilled": unfilled,
                    "unresolved": unresolved,
                    "intent_without_fill": bool(
                        order_ids and not filled and not unresolved and unfilled
                    ),
                }
            )
    due = sum(1 for record in records if record["due"])
    filled_opportunities = sum(
        1
        for record in records
        if record["due"] and record["filled"] and not record["unfilled"]
        and not record["unresolved"]
    )
    unfilled_opportunities = sum(
        1 for record in records if record["due"] and record["unfilled"]
    )
    intent_without_fill = sum(
        1 for record in records if record["due"] and record["intent_without_fill"]
    )
    consecutive = current = 0
    for record in records:
        if record["intent_without_fill"]:
            current += 1
            consecutive = max(consecutive, current)
        elif record["filled"] or record["unfilled"]:
            current = 0
    not_ready: list[dict[str, Any]] = []
    seen_dates: set[str] = set()
    for result in ordered:
        for anomaly in result.get("anomalies", ()):
            if anomaly.get("code") != "signal_window_incomplete":
                continue
            date = result["date"]
            if date in seen_dates:
                continue
            seen_dates.add(date)
            not_ready.append(
                {
                    "date": date,
                    "required_sessions": anomaly.get("required_sessions"),
                    "available_sessions": anomaly.get("available_sessions"),
                }
            )
    first_signal = next(
        (result["date"] for result in ordered if result.get("signal")), None
    )
    first_fill = next(
        (result["date"] for result in ordered if result.get("fills")), None
    )
    navs = [
        float(result["nav"])
        for result in ordered
        if isinstance(result.get("nav"), (int, float))
    ]
    return {
        "due_opportunities": due,
        "filled_opportunities": filled_opportunities,
        "unfilled_opportunities": unfilled_opportunities,
        "intent_without_fill": intent_without_fill,
        "consecutive_intent_without_fill": consecutive,
        "opportunities": records,
        "metrics_not_ready": {"count": len(not_ready), "sessions": not_ready},
        "first_signal_date": first_signal,
        "first_fill_date": first_fill,
        "max_abs_drawdown": abs(_max_drawdown(navs)) if len(navs) > 1 else 0.0,
    }


def evidence(ledger: str | Path, version_id: str) -> dict[str, Any]:
    """Read-only Evidence Loop seam over one version's Shadow Live ledger (T3).

    Returns committed-session facts, joined opportunity outcomes, the
    metrics-not-ready sessions, the scoped pause history and its still-active
    subset.  It never writes a day, never runs a version and never creates a
    ledger: a version with no committed day reports zero counts instead of a
    fabricated opportunity.
    """
    if not isinstance(version_id, str) or not _RELEASE_ID.fullmatch(version_id):
        raise _reject(
            "INVALID_RELEASE_ID",
            "evidence",
            "correct",
            "Release IDs are 64-character lowercase hexadecimal content addresses.",
        )
    path = Path(ledger)
    database_file = path / "shadow.sqlite"
    if not database_file.is_file():
        joined = _opportunity_evidence([])
        return {
            "schema_version": SCHEMA_VERSION,
            "version_id": version_id,
            "committed_sessions": 0,
            "committed_dates": [],
            "first_committed_date": None,
            "last_committed_date": None,
            **joined,
            "data_gap_sessions": 0,
            "pauses": [],
            "active_pauses": [],
            "progress": None,
        }
    database = Ledger(path)
    days = list(database.iter_days(version_id))
    results = [day["result"] for day in days]
    joined = _opportunity_evidence(results)
    pauses = database.scope_pauses(version_id)
    progress = database.get_progress(version_id)
    last = progress.get("last_committed_date") if progress else None
    active = [
        pause
        for pause in pauses
        if not isinstance(last, str) or pause["session"] > last
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "version_id": version_id,
        "committed_sessions": len(days),
        "committed_dates": [day["date"] for day in days],
        "first_committed_date": days[0]["date"] if days else None,
        "last_committed_date": days[-1]["date"] if days else None,
        **joined,
        "data_gap_sessions": len(
            {pause["session"] for pause in pauses if pause["scope"] == "feed"}
        ),
        "pauses": pauses,
        "active_pauses": active,
        "progress": progress,
    }


def mark_version_ended(
    ledger: str | Path, version_id: str, release_id: str
) -> dict[str, Any] | None:
    """Terminal ledger marker for a retired manual-tracking version (T3)."""
    if not isinstance(version_id, str) or not _RELEASE_ID.fullmatch(version_id):
        raise _reject(
            "INVALID_RELEASE_ID",
            "evidence",
            "correct",
            "Release IDs are 64-character lowercase hexadecimal content addresses.",
        )
    return Ledger(ledger).end_version(version_id, release_id)


def _report_summary(
    entries: list[dict[str, Any]], progress: dict[str, Any] | None
) -> dict[str, Any]:
    first = entries[0]["result"] if entries else None
    last = entries[-1]["result"] if entries else None
    initial_cash = first["execution"]["initial_cash"] if first else None
    ending_nav = last["nav"] if last else None
    corrections = [
        {"date": entry["date"], **correction}
        for entry in entries
        for correction in entry["result"].get("corrections", [])
    ]
    cash_deficits = [
        {"date": entry["date"], **entry["result"]["cash_deficit"]}
        for entry in entries
        if entry["result"].get("cash_deficit")
    ]
    opportunities = _opportunity_evidence([entry["result"] for entry in entries])
    return {
        "days": len(entries),
        "first_committed_date": entries[0]["date"] if entries else None,
        "last_committed_date": entries[-1]["date"] if entries else None,
        "progress": progress,
        "initial_cash": initial_cash,
        "ending_nav": ending_nav,
        "total_return": (
            ending_nav / initial_cash - 1.0
            if ending_nav is not None and initial_cash
            else None
        ),
        "fees_total": last["fees"]["total"] if last else 0.0,
        "slippage_total": last["slippage"]["total"] if last else 0.0,
        "dividends_cash_total": last["dividends"]["cash_total"] if last else 0.0,
        "ending_cash": last["cash"] if last else None,
        "corrections": corrections,
        "corrections_total": len(corrections),
        "correction_impact_total": math.fsum(
            float(correction["amount"]) for correction in corrections
        ),
        "cash_deficits": cash_deficits,
        "fills": sum(len(entry["result"]["fills"]) for entry in entries),
        "unfilled_orders": sum(
            1
            for entry in entries
            for order in entry["result"]["orders"]
            if order["status"] != "filled"
        ),
        "anomalies": sum(len(entry["result"]["anomalies"]) for entry in entries),
        "due_opportunities": opportunities["due_opportunities"],
        "filled_opportunities": opportunities["filled_opportunities"],
        "unfilled_opportunities": opportunities["unfilled_opportunities"],
        "intent_without_fill": opportunities["intent_without_fill"],
        "metrics_not_ready": opportunities["metrics_not_ready"],
    }


# ---------------------------------------------------------------------------
# Multi-version forward comparison (#34): committed rows only, no interpolation
# ---------------------------------------------------------------------------


def _comparison_versions(versions: object) -> list[str]:
    """Normalise the requested version list; a comparison needs two distinct IDs."""
    if isinstance(versions, str):
        raw = versions.split(",")
    elif isinstance(versions, (list, tuple)):
        raw = list(versions)
    else:
        raise _reject(
            "INVALID_VERSIONS",
            "compare",
            "correct",
            "The compared versions must be a comma-separated list of release IDs.",
        )
    requested: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise _reject(
                "INVALID_VERSIONS",
                "compare",
                "correct",
                "The compared versions contain an empty release ID.",
            )
        requested.append(item.strip())
    if len(requested) < 2:
        raise _reject(
            "INVALID_VERSIONS",
            "compare",
            "correct",
            "A forward comparison needs at least two version IDs.",
        )
    if len(set(requested)) != len(requested):
        raise _reject(
            "INVALID_VERSIONS",
            "compare",
            "correct",
            "Each compared version may be requested only once.",
        )
    for version_id in requested:
        if not _RELEASE_ID.fullmatch(version_id):
            raise _reject(
                "INVALID_RELEASE_ID",
                "compare",
                "correct",
                "Release IDs are 64-character lowercase hexadecimal content addresses.",
            )
    return requested


def _consecutive_runs(sessions: list[str], feed: Feed) -> list[list[str]]:
    """Split committed dates into maximal runs of adjacent feed sessions."""
    runs: list[list[str]] = []
    run: list[str] = []
    previous: int | None = None
    for session in sessions:
        index = feed.index[session]
        if previous is not None and index == previous + 1:
            run.append(session)
        else:
            if run:
                runs.append(run)
            run = [session]
        previous = index
    if run:
        runs.append(run)
    return runs


def _max_drawdown(navs: list[float]) -> float:
    """Maximum peak-to-trough drawdown over the run (negative, as backtest.py)."""
    peak: float | None = None
    worst = 0.0
    for nav in navs:
        peak = nav if peak is None else max(peak, nav)
        worst = min(worst, nav / peak - 1.0)
    return worst


def _comparison_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Run metrics for one version, computed only from its committed day rows."""
    start_nav = results[0]["nav"]
    end_nav = results[-1]["nav"]
    if (
        isinstance(start_nav, bool)
        or not isinstance(start_nav, (int, float))
        or not math.isfinite(start_nav)
        or start_nav <= 0
    ):
        raise _reject(
            "INVALID_COMPARISON_BASELINE",
            "compare",
            "inspect",
            "A compared version has no positive finite starting NAV in the common "
            "run; a return cannot be normalised against it.",
        )
    navs = [float(result["nav"]) for result in results]
    fees_paid = math.fsum(float(result["fees"]["day"]) for result in results)
    dividends_paid = math.fsum(
        float(payment["amount"])
        for result in results
        for payment in result["dividends"]["payments"]
    )
    correction_impact = math.fsum(
        float(result.get("correction_impact", 0.0)) for result in results
    )
    corrections = [
        {"date": result["date"], **correction}
        for result in results
        for correction in result.get("corrections", [])
    ]
    cash_deficits = [
        {"date": result["date"], **result["cash_deficit"]}
        for result in results
        if result.get("cash_deficit")
    ]
    unfilled_orders = [
        {
            "date": result["date"],
            "order_id": order["order_id"],
            "symbol": order["symbol"],
            "side": order["side"],
            "quantity": order["quantity"],
            "status": order["status"],
            "reason": order["reason"],
        }
        for result in results
        for order in result["orders"]
        if order["status"] != "filled"
    ]
    opportunities = _opportunity_evidence(results)
    return {
        "starting_nav": float(start_nav),
        "ending_nav": float(end_nav),
        "cumulative_return": float(end_nav) / float(start_nav) - 1.0,
        "max_drawdown": _max_drawdown(navs),
        "fees_paid": fees_paid,
        "dividends_paid": dividends_paid,
        "unfilled_order_count": len(unfilled_orders),
        "unfilled_orders": unfilled_orders,
        "correction_impact": correction_impact,
        "corrections": corrections,
        "cash_deficits": cash_deficits,
        "cash_deficit_total": math.fsum(
            float(deficit["amount"]) for deficit in cash_deficits
        ),
        "strategy_return": (
            (float(end_nav) - correction_impact) / float(start_nav) - 1.0
        ),
        "due_opportunities": opportunities["due_opportunities"],
        "filled_opportunities": opportunities["filled_opportunities"],
        "unfilled_opportunities": opportunities["unfilled_opportunities"],
        "intent_without_fill": opportunities["intent_without_fill"],
    }


def _full_history_entry(days: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Each requested version's own full committed history (T3, never the run)."""
    dates = sorted(days)
    results = [days[session]["result"] for session in dates]
    opportunities = _opportunity_evidence(results)
    navs = [
        float(result["nav"])
        for result in results
        if isinstance(result.get("nav"), (int, float))
    ]
    first_nav = navs[0] if navs else None
    ending_nav = navs[-1] if navs else None
    correction_impact = math.fsum(
        float(result.get("correction_impact", 0.0)) for result in results
    )
    return {
        "sessions": len(dates),
        "first_committed_date": dates[0] if dates else None,
        "last_committed_date": dates[-1] if dates else None,
        "starting_nav": first_nav,
        "ending_nav": ending_nav,
        "total_return": (
            ending_nav / first_nav - 1.0
            if ending_nav is not None and first_nav is not None and first_nav > 0
            else None
        ),
        "strategy_return": (
            (ending_nav - correction_impact) / first_nav - 1.0
            if ending_nav is not None and first_nav is not None and first_nav > 0
            else None
        ),
        "max_drawdown": _max_drawdown(navs) if len(navs) > 1 else 0.0,
        "correction_impact": correction_impact,
        "fees_paid": math.fsum(
            float(result["fees"]["day"]) for result in results
        ),
        "unfilled_order_count": sum(
            1
            for result in results
            for order in result["orders"]
            if order["status"] != "filled"
        ),
        "due_opportunities": opportunities["due_opportunities"],
        "filled_opportunities": opportunities["filled_opportunities"],
        "unfilled_opportunities": opportunities["unfilled_opportunities"],
        "intent_without_fill": opportunities["intent_without_fill"],
    }


def _coverage_entry(
    version_id: str, days: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    dates = sorted(days)
    return {
        "release_id": version_id,
        "committed_coverage": {
            "dates": dates,
            "sessions": len(dates),
            "first_committed_date": dates[0] if dates else None,
            "last_committed_date": dates[-1] if dates else None,
        },
        "full_history": _full_history_entry(days),
    }


def compare(
    ledger: str | Path,
    data: str | Path,
    versions: str | list[str] | tuple[str, ...],
    output: str | Path | None = None,
) -> dict[str, Any]:
    """Compare the longest common, consecutive committed forward interval.

    Only verified committed day rows are read (:meth:`Ledger.iter_days`); a
    tampered row raises ``LEDGER_INTEGRITY_FAILED`` and the comparison never
    re-runs a version, interpolates a session or mixes backtest data.  Every
    version is rebased to the first common session's closing NAV.  Fewer than
    two common consecutive sessions makes the comparison ``comparable: false``
    with a stable reason and each version's committed coverage.  All requested
    versions must share the chosen run; a version with too little common
    history is never silently dropped.
    """
    feed = load_feed(data)
    requested = _comparison_versions(versions)
    path = Path(ledger)
    if not path.is_dir() or not (path / "shadow.sqlite").is_file():
        raise _reject(
            "LEDGER_NOT_FOUND",
            "compare",
            "correct",
            f"The Shadow Live ledger {path} does not exist; a comparison only "
            "reads committed rows and never creates or re-runs a ledger.",
        )
    database = Ledger(path)
    coverage: dict[str, dict[str, dict[str, Any]]] = {}
    for version_id in requested:
        days = {day["date"]: day for day in database.iter_days(version_id)}
        if not days and database.get_progress(version_id) is None:
            raise _reject(
                "VERSION_NOT_FOUND",
                "compare",
                "correct",
                f"The ledger has no committed history or progress for version "
                f"{version_id}; comparison never invents a version.",
            )
        if any(day["result"]["scope"] != feed.data_kind for day in days.values()):
            raise _reject(
                "FEED_SCOPE_MISMATCH", "compare", "correct",
                f"Committed days for {version_id} require a feed of the same data scope.",
            )
        outside = sorted(session for session in days if session not in feed.index)
        if outside:
            raise _reject(
                "FEED_CALENDAR_MISMATCH",
                "compare",
                "inspect",
                f"Committed session(s) {', '.join(outside)} are not in this feed's "
                "calendar; a session is never interpolated or re-dated.",
            )
        coverage[version_id] = days

    common = sorted(set.intersection(*(set(days) for days in coverage.values())))
    runs = _consecutive_runs(common, feed)
    chosen = max(runs, key=lambda run: (len(run), feed.index[run[-1]])) if runs else []
    comparable = len(chosen) >= 2
    reason = None if comparable else "INSUFFICIENT_COMMON_SESSIONS"

    per_version: dict[str, dict[str, Any]] = {}
    for version_id in requested:
        days = coverage[version_id]
        entry = _coverage_entry(version_id, days)
        if comparable:
            entry.update(
                {
                    "included": True,
                    "date_range": {"start": chosen[0], "end": chosen[-1]},
                    "sessions": len(chosen),
                    **_comparison_metrics(
                        [days[session]["result"] for session in chosen]
                    ),
                }
            )
        else:
            entry.update(
                {
                    "included": False,
                    "reason": reason,
                    "date_range": {"start": None, "end": None},
                    "sessions": 0,
                    "starting_nav": None,
                    "ending_nav": None,
                    "cumulative_return": None,
                    "max_drawdown": None,
                    "fees_paid": None,
                    "dividends_paid": None,
                    "unfilled_order_count": None,
                    "unfilled_orders": [],
                    "correction_impact": None,
                    "corrections": [],
                    "cash_deficits": [],
                    "cash_deficit_total": None,
                    "strategy_return": None,
                }
            )
        per_version[version_id] = entry

    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": COMPARISON_KIND,
        "scope": feed.data_kind,
        "versions": requested,
        "comparable": comparable,
        "reason": reason,
        "comparison": {"comparable": comparable, "reason": reason},
        "common_interval": {
            "start": chosen[0] if comparable else None,
            "end": chosen[-1] if comparable else None,
            "sessions": len(chosen) if comparable else 0,
        },
        "per_version": per_version,
    }
    if output is not None:
        destination = Path(output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
        )
    return document


# ---------------------------------------------------------------------------
# Per-version processing
# ---------------------------------------------------------------------------


def _release_directories(store: Path) -> list[str]:
    directory = store / "releases"
    if not directory.is_dir():
        return []
    release_ids: list[str] = []
    for path in sorted(directory.iterdir()):
        if not path.is_dir() or not _RELEASE_ID.fullmatch(path.name):
            raise _reject(
                "RELEASE_INTEGRITY_FAILED",
                "select",
                "inspect",
                f"Unknown object in the release store: {path.name!r}.",
            )
        release_ids.append(path.name)
    return release_ids


def _tracked_release_directories(
    database: Ledger, store: Path, target: str
) -> list[str]:
    """Release IDs tracked at ``target`` (enabled, or paused/retired with history).

    Approval alone never starts tracking.  A version whose state cannot be
    verified is selected anyway and then reported as blocked, never silently
    skipped.
    """
    tracked: list[str] = []
    for release_id in _release_directories(store):
        try:
            state = release.state_at(store, release_id, target)
        except release.ReleaseRejection:
            tracked.append(release_id)
            continue
        progress = database.get_progress(release_id)
        if state == "enabled":
            tracked.append(release_id)
        elif state in ("paused", "retired") and progress is not None:
            tracked.append(release_id)
    return tracked


def _block_scope(code: str) -> str:
    """Classify a per-version block into a scoped pause (T3)."""
    if code in _FEED_BLOCK_CODES:
        return "feed"
    if code in _EXECUTION_BLOCK_CODES:
        return "execution"
    if code in _LEDGER_BLOCK_CODES:
        return "ledger"
    return "version"


def _first_uncommitted_session(
    database: Ledger,
    store: Path,
    feed: Feed,
    release_id: str,
    target: str,
) -> str:
    """The session a scoped pause applies to: the version's first uncommitted one."""
    progress = _progress_or_none(database, release_id)
    last = progress.get("last_committed_date") if progress else None
    if isinstance(last, str) and last:
        if last in feed.index:
            following = feed.next_session(last)
            if following is not None and following <= target:
                return following
        return target
    try:
        for session in feed.sessions:
            if session > target:
                break
            if release.state_at(store, release_id, session) == "enabled":
                return session
    except release.ReleaseRejection:
        pass
    return target


def _record_scope_pause(
    database: Ledger,
    store: Path,
    feed: Feed,
    release_id: str,
    target: str,
    code: str,
    message: str,
) -> dict[str, Any] | None:
    """Persist the scoped pause for a blocked version; other versions continue.

    A failed write is returned as a version anomaly instead of being swallowed:
    the report must never pretend the pause was recorded, and the version entry
    stays ``blocked`` so the operator still sees the distortion.
    """
    session = _first_uncommitted_session(database, store, feed, release_id, target)
    try:
        database.record_scope_pause(
            release_id, session, _block_scope(code), code, message
        )
    except sqlite3.Error as error:
        return {
            "code": "scope_pause_not_recorded",
            "scope": _block_scope(code),
            "session": session,
            "message": (
                f"The scoped pause for {code!r} at {session} could not be "
                f"persisted: {error}"
            ),
        }
    return None


def _version_result(
    database: Ledger,
    store: Path,
    feed: Feed,
    target: str,
    release_id: str,
    corrections: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        return _run_version(database, store, feed, target, release_id, corrections)
    except _Blocked as error:
        progress = _progress_or_none(database, release_id)
        last = progress["last_committed_date"] if progress else None
        pause_failure = _record_scope_pause(
            database, store, feed, release_id, target, error.code, error.reason
        )
        return _version_entry(
            release_id,
            "blocked",
            target_date=target,
            reason=error.reason,
            reason_code=error.code,
            committed_date=last,
            last_committed_date=last,
            anomalies=[pause_failure] if pause_failure is not None else None,
            detail=error.detail,
        )
    except ShadowRejection as error:
        progress = _progress_or_none(database, release_id)
        last = progress["last_committed_date"] if progress else None
        pause_failure = _record_scope_pause(
            database,
            store,
            feed,
            release_id,
            target,
            error.details["code"],
            error.details["message"],
        )
        return _version_entry(
            release_id,
            "blocked",
            target_date=target,
            reason=error.details["message"],
            reason_code=error.details["code"],
            committed_date=last,
            last_committed_date=last,
            anomalies=[pause_failure] if pause_failure is not None else None,
        )


def _progress_or_none(database: Ledger, release_id: str) -> dict[str, Any] | None:
    try:
        return database.get_progress(release_id)
    except sqlite3.Error:  # pragma: no cover - defensive
        return None


def _version_entry(
    release_id: str,
    status: str,
    *,
    target_date: str | None = None,
    committed_at: str | None = None,
    reason: str | None = None,
    reason_code: str | None = None,
    committed_date: str | None = None,
    last_committed_date: str | None = None,
    anomalies: list[dict[str, Any]] | None = None,
    unfilled_orders: list[dict[str, Any]] | None = None,
    result_hash: str | None = None,
    corrections: list[dict[str, Any]] | None = None,
    correction_impact: float = 0.0,
    cash_deficit: dict[str, Any] | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "release_id": release_id,
        "status": status,
        "target_date": target_date,
        "committed_at": committed_at,
        "reason": reason,
        "reason_code": reason_code,
        "committed_date": committed_date,
        "last_committed_date": last_committed_date,
        "result_hash": result_hash,
        "anomalies": list(anomalies or []),
        "unfilled_orders": list(unfilled_orders or []),
        "corrections": list(corrections or []),
        "correction_impact": correction_impact,
        "cash_deficit": cash_deficit,
        **({"detail": detail} if detail else {}),
    }


def _execution_settings(artifact: dict[str, Any]) -> dict[str, Any]:
    execution = artifact.get("execution")
    required = (
        "strategy",
        "parameters",
        "initial_cash",
        "transaction_cost_bps",
        "slippage_bps",
        "signal_window_sessions",
        "execution_rules",
    )
    if not isinstance(execution, dict) or any(key not in execution for key in required):
        raise _Blocked(
            "RELEASE_INVALID",
            "The release artifact's frozen execution settings are incomplete.",
        )
    strategy = execution["strategy"]
    family = strategy.get("family") if isinstance(strategy, dict) else None
    if family not in (*STRATEGY_FAMILIES, "python"):
        raise _Blocked("UNSUPPORTED_STRATEGY", f"The release declares family {family!r}.")
    parameters = execution["parameters"]
    if not isinstance(parameters, dict):
        raise _Blocked(
            "RELEASE_INVALID", "The release artifact's strategy parameters are malformed."
        )
    for name in ("initial_cash", "transaction_cost_bps", "slippage_bps"):
        value = execution[name]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise _Blocked(
                "RELEASE_INVALID", f"The release artifact's {name} is invalid."
            )
    if execution["initial_cash"] <= 0:
        raise _Blocked(
            "RELEASE_INVALID", "The release artifact's initial cash must be positive."
        )
    if execution["transaction_cost_bps"] >= 10_000 or execution["slippage_bps"] >= 10_000:
        raise _Blocked(
            "RELEASE_INVALID", "The release artifact's costs must be below 10,000 bps."
        )
    window = execution["signal_window_sessions"]
    if isinstance(window, bool) or not isinstance(window, int) or window < 1:
        raise _Blocked(
            "RELEASE_INVALID", "The release artifact's signal window must be a positive integer."
        )
    identity = {"family": family, "parameters": dict(parameters)}
    runtime: dict[str, Any] = {}
    try:
        if family == "python":
            evaluation = artifact["candidate"]["evaluation"]
            config = evaluation["strategy_executor"]
            if (
                (artifact.get("workflow") != release.RESEARCH_HOLDOUT and artifact["scope"] != "real")
                or not runtime_matches(
                    evaluation["runtime"], config, execution_rules=evaluation["execution_rules"],
                    history_policy=evaluation.get("history_policy"),
                )
            ):
                raise ValueError("The frozen Python execution runtime is unavailable.")
            version = normalize_strategy_spec({**strategy, "parameters": parameters})
            if hashlib.sha256(version["source"].encode()).hexdigest() != strategy.get("source_sha256"):
                raise ValueError("The frozen Python source fingerprint differs.")
            identity["source_sha256"] = strategy["source_sha256"]
            runtime = {
                "strategy_spec": version,
                "container_executor": executor_from_config(config),
            }
        else:
            normalize_strategy_spec(identity)
    except (KeyError, TypeError, ValueError) as error:
        raise _Blocked(
            "RELEASE_INVALID",
            f"The release artifact's frozen strategy parameters are invalid: {error}",
        ) from error
    return {
        "strategy": identity,
        **runtime,
        "parameters": dict(parameters),
        "initial_cash": float(execution["initial_cash"]),
        "transaction_cost_bps": float(execution["transaction_cost_bps"]),
        "slippage_bps": float(execution["slippage_bps"]),
        "signal_window_sessions": window,
        "execution_rules": execution["execution_rules"],
    }


def _check_authorized_sessions(
    artifact: dict[str, Any],
    feed: Feed,
    target: str,
    target_state: str,
    progress: dict[str, Any] | None,
) -> None:
    terms = artifact["admission"]
    first = terms["effective_signal_date"]
    authorized = terms["calendar"]["sessions"]
    if first > target:
        return
    # Verify the prospective dates bound before the first read, including the
    # deadline date used to size and persist the next orders.
    if not shadow_calendar.authorized_sessions_match(feed.sessions, authorized, first):
        raise _Blocked(
            "AUTHORIZED_CALENDAR_MISMATCH",
            "The feed calendar differs from the trading dates bound to this release.",
        )
    committed = progress is not None and progress["last_committed_date"] >= target
    if target_state == "enabled" and not committed and feed.next_session(target) is None:
        raise _Blocked(
            "CALENDAR_HORIZON_EXHAUSTED",
            f"The feed needs the next scheduled session after {target} to register orders.",
        )


def _run_version(
    database: Ledger,
    store: Path,
    feed: Feed,
    target: str,
    release_id: str,
    corrections: dict[str, Any] | None = None,
) -> dict[str, Any]:
    progress = database.get_progress(release_id)
    if progress is not None and progress.get("state") == "ended":
        last = progress.get("last_committed_date")
        return _version_entry(
            release_id,
            "ended",
            target_date=target,
            reason=(
                "the manual simulated-tracking version was retired by an evidence "
                "decision; its committed history is kept unchanged and no later "
                "day, signal or order is generated"
            ),
            reason_code="VERSION_ENDED",
            committed_date=last,
            last_committed_date=last,
        )
    try:
        fetched = release.fetch(store, release_id)
    except release.ReleaseRejection as error:
        raise _Blocked(error.details["code"], error.details["message"]) from error
    artifact = fetched["artifact"]
    if artifact["scope"] != feed.data_kind:
        raise _Blocked(
            "FEED_SCOPE_MISMATCH",
            f"This release requires a {artifact['scope']} Shadow Live feed.",
        )
    execution = _execution_settings(artifact)
    try:
        states = {
            session: release.state_at(store, release_id, session)
            for session in feed.sessions
        }
    except release.ReleaseRejection as error:
        raise _Blocked(error.details["code"], error.details["message"]) from error
    target_index = feed.index[target]
    target_state = states[target]
    progress = database.get_progress(release_id)
    if (
        progress is not None
        and target_state == "retired"
        and artifact.get("workflow") == release.TRACKING_WORKFLOW
    ):
        # A manual simulated-tracking end appends the terminal release event at
        # the first uncommitted after-close session.  The tick that observes it
        # retires the ledger progress without committing another day, signal or
        # order, so the committed history stays byte-identical (spec #43 L33).
        # A legacy protected release keeps the #32 behavior: it may still commit
        # NAV-only days after retirement, because its ledger is not part of the
        # Research/Evidence Loop.
        last = progress.get("last_committed_date")
        database.end_version(release_id, release_id)
        return _version_entry(
            release_id,
            "ended",
            target_date=target,
            reason=(
                "the manual simulated-tracking version was ended by its review "
                "decision; its committed history is kept unchanged and no later "
                "day, signal or order is generated"
            ),
            reason_code="VERSION_ENDED",
            committed_date=last,
            last_committed_date=last,
        )
    if artifact.get("workflow") == release.RESEARCH_HOLDOUT:
        _check_authorized_sessions(artifact, feed, target, target_state, progress)
    if progress is None:
        start_index = next(
            (
                index
                for index, session in enumerate(feed.sessions)
                if states[session] != "published"
            ),
            None,
        )
        if start_index is None:
            return _version_entry(
                release_id,
                "not_started",
                target_date=target,
                reason=(
                    "the release has no enablement at or before the target date; "
                    "approval alone never starts tracking"
                ),
                reason_code="NOT_ENABLED",
            )
        if start_index > target_index:
            return _version_entry(
                release_id,
                "not_started",
                target_date=target,
                reason=(
                    f"the release is first tracked on {feed.sessions[start_index]}, "
                    f"after the target date {target}"
                ),
                reason_code="ENABLED_AFTER_TARGET",
            )
        if target_state == "retired":
            return _version_entry(
                release_id,
                "retired",
                target_date=target,
                reason=(
                    "the release is retired at the target date and has no ledger "
                    "history to settle"
                ),
                reason_code="RETIRED_WITHOUT_HISTORY",
            )
        prior = _initial_prior(execution)
    else:
        last = progress["last_committed_date"]
        if last == target:
            day = database.verified_day(release_id, target)
            if day is None:
                raise _Blocked(
                    "PROGRESS_MISMATCH",
                    f"The ledger progress points at {target} but no committed day is stored.",
                )
            return _committed_entry(
                release_id, day, status="already_committed", reason=None, target_date=target
            )
        if last not in feed.index:
            raise _Blocked(
                "FEED_CALENDAR_MISMATCH",
                f"The last committed session {last} is not in this feed's calendar; "
                "refusing to guess the next session.",
            )
        if feed.index[last] > target_index:
            day = database.verified_day(release_id, target)
            if day is not None:
                return _committed_entry(
                    release_id, day, status="already_committed", reason=None,
                    target_date=target,
                )
            raise _Blocked(
                "PROGRESS_MISMATCH",
                f"The ledger already advanced past {target} but has no committed day for it.",
            )
        prior = _prior_from_ledger(database, release_id, last)
        start_index = feed.index[last] + 1

    for index in range(start_index, target_index + 1):
        session = feed.sessions[index]
        prior = _process_session(
            database,
            feed,
            artifact,
            execution,
            release_id,
            index,
            session,
            states[session],
            prior,
            target,
            corrections,
        )
    day = database.verified_day(release_id, target)
    if day is None:
        raise _Blocked(
            "PROGRESS_MISMATCH",
            f"The backfill finished but no committed day is stored for {target}.",
        )
    return _committed_entry(
        release_id, day, status=day["result"]["status"], reason=None, target_date=target
    )


def _committed_entry(
    release_id: str,
    day: dict[str, Any],
    *,
    status: str,
    reason: str | None,
    target_date: str | None = None,
) -> dict[str, Any]:
    result = day["result"]
    unfilled = [
        {
            "order_id": order["order_id"],
            "symbol": order["symbol"],
            "side": order["side"],
            "quantity": order["quantity"],
            "status": order["status"],
            "reason": order["reason"],
        }
        for order in result["orders"]
        if order["status"] != "filled"
    ]
    return _version_entry(
        release_id,
        status,
        target_date=target_date,
        committed_at=day["committed_at"],
        reason=reason,
        committed_date=day["date"],
        last_committed_date=day["date"],
        anomalies=result["anomalies"],
        unfilled_orders=unfilled,
        result_hash=day["result_hash"],
        corrections=result.get("corrections", []),
        correction_impact=result.get("correction_impact", 0.0),
        cash_deficit=result.get("cash_deficit"),
    )


def _initial_prior(execution: dict[str, Any]) -> dict[str, Any]:
    initial = float(execution["initial_cash"])
    return {
        "date": None,
        "cash": initial,
        "positions": {symbol: 0.0 for symbol in ETF_SYMBOLS},
        "receivables": [],
        "booked_dividends": [],
        "deficit_revision_ids": [],
        "fees_total": 0.0,
        "slippage_total": 0.0,
        "dividend_entitled_total": 0.0,
        "dividend_cash_total": 0.0,
        "nav": initial,
        "next_orders": [],
        "signal_target": None,
        "start_date": None,
        "last_committed_date": None,
        "committed_at": None,
    }


def _prior_from_ledger(
    database: Ledger, release_id: str, session: str
) -> dict[str, Any]:
    day = database.verified_day(release_id, session)
    if day is None:
        raise _Blocked(
            "PROGRESS_MISMATCH",
            f"The ledger progress points at {session} but no committed day is stored.",
        )
    return _prior_from_result(day["result"], committed_at=day["committed_at"])


def _prior_from_result(
    result: dict[str, Any], *, committed_at: str | None = None
) -> dict[str, Any]:
    return {
        "date": result["date"],
        "cash": float(result["cash"]),
        "positions": {
            symbol: float(result["positions"][symbol]["quantity"])
            for symbol in ETF_SYMBOLS
        },
        "receivables": [dict(item) for item in result["dividends"]["receivables"]],
        "booked_dividends": [
            dict(item) for item in result["dividends"].get("booked", [])
        ],
        "deficit_revision_ids": list(
            (result.get("cash_deficit") or {}).get("revision_ids", [])
        ),
        "fees_total": float(result["fees"]["total"]),
        "slippage_total": float(result["slippage"]["total"]),
        "dividend_entitled_total": float(result["dividends"]["entitled_total"]),
        "dividend_cash_total": float(result["dividends"]["cash_total"]),
        "nav": float(result["nav"]),
        "next_orders": [dict(order) for order in result["next_orders"]],
        # The last signal target, carried across no-signal days.  Old committed
        # rows predate this field; their same-day signal is the best fallback.
        "signal_target": result.get("signal_target")
        or (result.get("signal") or {}).get("target_weights"),
        "start_date": result["progress"]["start_date"],
        "last_committed_date": result["date"],
        "committed_at": committed_at,
    }


# ---------------------------------------------------------------------------
# One session: freeze input -> compute -> deterministic failpoints -> commit
# ---------------------------------------------------------------------------


def _process_session(
    database: Ledger,
    feed: Feed,
    artifact: dict[str, Any],
    execution: dict[str, Any],
    release_id: str,
    index: int,
    session: str,
    state: str,
    prior: dict[str, Any],
    target: str,
    corrections: dict[str, Any] | None = None,
) -> dict[str, Any]:
    frozen = database.verified_input(release_id, session)
    frozen_corrections = (
        frozen["input"].get("corrections") if frozen is not None else None
    )
    built, blockers, _ = _build_input(
        feed,
        artifact,
        execution,
        release_id,
        index,
        prior,
        state,
        corrections=corrections,
        frozen_corrections=frozen_corrections,
    )
    input_hash = _hash(built)
    if frozen is not None:
        if frozen["input_hash"] != input_hash:
            raise _Blocked(
                "FROZEN_INPUT_CHANGED",
                f"The frozen input for {session} no longer matches the feed and "
                f"prior account state (frozen {frozen['input_hash'][:12]}, current "
                f"{input_hash[:12]}); refusing to mix frozen and current inputs.",
                detail={
                    "frozen_input_hash": frozen["input_hash"],
                    "current_input_hash": input_hash,
                },
            )
        body = frozen["input"]
        frozen_now = False
    else:
        if blockers:
            code, reason = blockers[0]
            raise _Blocked(code, reason, detail={"blockers": [b[0] for b in blockers]})
        body = built
        frozen_now = database.freeze_input(release_id, session, body, input_hash)
        if not frozen_now:
            concurrent = database.verified_input(release_id, session)
            if concurrent is None or concurrent["input_hash"] != input_hash:
                raise _Blocked(
                    "FROZEN_INPUT_CHANGED",
                    f"The concurrent input snapshot differs for {session}; retry the update.",
                )

    try:
        result = _compute_day(body, execution)
    except _Blocked as error:
        if frozen_now or error.code == "missing_close":
            database.delete_input(release_id, session, input_hash)
        raise
    result, committed_at = database.commit_day(
        version_id=release_id,
        release_id=release_id,
        session=session,
        result=result,
        expected_previous=prior["last_committed_date"],
        start_date=result["progress"]["start_date"],
        state=state,
    )
    if session == target:
        _failpoint("shadow_after_commit_before_report")
    return _prior_from_result(result, committed_at=committed_at)


def _build_input(
    feed: Feed,
    artifact: dict[str, Any],
    execution: dict[str, Any],
    release_id: str,
    index: int,
    prior: dict[str, Any],
    state: str,
    *,
    corrections: dict[str, Any] | None = None,
    frozen_corrections: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[tuple[str, str]], list[dict[str, Any]]]:
    """Assemble the complete deterministic input body and its completeness report."""
    session = feed.sessions[index]
    next_session = feed.next_session(session)
    raw = {symbol: feed.raw.get((session, symbol)) for symbol in ETF_SYMBOLS}
    actions = list(feed.actions_by_date.get(session, ()))
    window_size = execution["signal_window_sessions"]
    window_sessions = list(feed.sessions[max(0, index - window_size + 1) : index + 1])
    derived_window = [
        {"session": day, "symbol": symbol, "row": feed.derived.get((day, symbol))}
        for day in window_sessions
        for symbol in ETF_SYMBOLS
    ]
    open_cutoff = _session_open(session)
    close_cutoff = _session_close(session)
    held = {
        symbol: quantity
        for symbol, quantity in prior["positions"].items()
        if quantity > _EPSILON
    }
    pending = [dict(order) for order in prior["next_orders"]]
    buy_symbols = {
        order["symbol"]
        for order in pending
        if order.get("side") == "buy" and order.get("symbol") in ETF_SYMBOLS
    }
    blockers: list[tuple[str, str]] = []
    anomalies: list[dict[str, Any]] = []
    if feed.data_kind == "real":
        buy_symbols = set()
        if _before(prior.get("committed_at"), open_cutoff):
            for order in pending:
                if not _before(order.get("submitted_at"), open_cutoff):
                    continue
                symbol = order["symbol"]
                row = raw.get(symbol)
                if row is None or row["trading_status"] == "unknown" or (
                    row["trading_status"] == "trading" and row["open"] is None
                ):
                    blockers.append((
                        "open_evidence_missing",
                        f"{symbol} has no evidence for the {session} opening auction.",
                    ))
                elif order["side"] == "buy" and _is_executable_open(
                    row, open_cutoff, data_kind=feed.data_kind
                ):
                    buy_symbols.add(symbol)

    # #33: fix the applicable correction set *before* computing, deciding only
    # from the verified revisions and the already-committed ledger.  A frozen
    # input is authoritative (a crash before the commit reuses the same set).
    if frozen_corrections is not None:
        correction_decisions = [dict(item) for item in frozen_corrections]
    else:
        correction_decisions, correction_blockers = _correction_decisions(
            corrections, prior, session, close_cutoff
        )
        blockers.extend(correction_blockers)

    for symbol in sorted(set(held) | buy_symbols):
        if not _valid_close(raw.get(symbol), close_cutoff, data_kind=feed.data_kind):
            blockers.append(
                (
                    "missing_close",
                    f"{symbol} has no confirmed unadjusted close available by "
                    f"{close_cutoff.isoformat()}.",
                )
            )
    for action in actions:
        symbol = action["symbol"]
        available = available_by(action, open_cutoff)
        valid = _terms_valid(action)
        if held.get(symbol, 0.0) <= _EPSILON:
            if not available or not valid:
                anomalies.append(
                    {
                        "code": "corporate_action_terms_unavailable",
                        "symbol": symbol,
                        "action_id": action["action_id"],
                        "held": False,
                    }
                )
            continue
        if not available:
            blockers.append(
                (
                    "missing_action_terms",
                    f"{action['action_id']} terms are not available by the "
                    f"{session} open cutoff.",
                )
            )
        elif not valid:
            blockers.append(
                (
                    "missing_action_terms",
                    f"{action['action_id']} has unresolved or incomplete terms.",
                )
            )

    signal_due = state == "enabled" and next_session is not None
    if signal_due:
        if len(window_sessions) < window_size:
            if artifact.get("workflow") == release.RESEARCH_HOLDOUT:
                blockers.append((
                    "signal_window_incomplete",
                    f"The {session} signal requires {window_size} sessions of history. "
                    f"The feed supplies {len(window_sessions)}.",
                ))
            anomalies.append(
                {
                    "code": "signal_window_incomplete",
                    "date": session,
                    "required_sessions": window_size,
                    "available_sessions": len(window_sessions),
                }
            )
        else:
            signal_gap = False
            for entry in derived_window:
                row = entry["row"]
                label = f"derived value for {entry['symbol']} on {entry['session']}"
                if row is None or row.get("value") is None:
                    blockers.append(("missing_signal_input", f"{label} is missing."))
                    signal_gap = True
                elif not available_by(row, close_cutoff):
                    blockers.append(
                        (
                            "missing_signal_input",
                            f"{label} is not available by {close_cutoff.isoformat()}.",
                        )
                    )
                    signal_gap = True
            if not signal_gap:
                history_rows = [
                    {
                        "date": entry["session"],
                        "symbol": entry["symbol"],
                        "close": float(entry["row"]["value"]),
                    }
                    for entry in derived_window
                ]
                sizing_symbols = _possible_target_symbols(
                    execution, history_rows, prior, pending
                )
                for symbol in sorted(sizing_symbols - set(held) - buy_symbols):
                    if not _valid_close(raw.get(symbol), close_cutoff, data_kind=feed.data_kind):
                        blockers.append(
                            (
                                "missing_close",
                                f"{symbol} has no confirmed unadjusted close for the "
                                f"signal's order sizing at {session}.",
                            )
                        )

    body = {
        "schema_version": SCHEMA_VERSION,
        "kind": INPUT_KIND,
        **({"data_kind": feed.data_kind} if feed.data_kind != DATA_KIND else {}),
        "version_id": release_id,
        "date": session,
        "release": {
            "release_id": release_id,
            "artifact_hash": artifact["artifact_hash"],
            "candidate_id": artifact.get("candidate_id"),
            "candidate_hash": artifact.get("candidate_hash"),
            "plan_id": artifact.get("plan_id"),
            "snapshot_id": artifact.get("snapshot_id"),
        },
        "artifact_hash": artifact["artifact_hash"],
        "execution": {
            "strategy": execution["strategy"],
            "parameters": execution["parameters"],
            "initial_cash": execution["initial_cash"],
            "transaction_cost_bps": execution["transaction_cost_bps"],
            "slippage_bps": execution["slippage_bps"],
            "signal_window_sessions": execution["signal_window_sessions"],
            "execution_rules": execution["execution_rules"],
        },
        "release_state_at_signal_point": state,
        "previous": prior,
        "session": {
            "date": session,
            "next_session": next_session,
            "calendar": feed.calendar[session],
            "raw_bars": raw,
            "corporate_actions": actions,
            "derived_window": {
                "sessions": window_sessions,
                "rows": derived_window,
            },
        },
        "anomalies": anomalies,
        "corrections": correction_decisions,
    }
    return body, blockers, anomalies


def _amounts_match(left: object, right: object) -> bool:
    return math.isclose(float(left), float(right), rel_tol=1e-9, abs_tol=1e-9)


def _correction_decisions(
    corrections: dict[str, Any] | None,
    prior: dict[str, Any],
    session: str,
    close_cutoff: datetime,
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """Resolve the verified revisions applicable to one version and session.

    Applicable means: filed by the session's 15:00 Asia/Shanghai after-close cut,
    ex-date not after the session, the ledger booked an entitlement for that
    symbol/ex-date (held through the ex-date), not already applied for
    ``(version_id, action_id, revision_id)``, and the revision's ``old_amount``
    equals the currently booked per-share amount (original entitlement plus prior
    applied deltas).  A mismatch is a version block, never a guessed delta.
    """
    if not corrections or not corrections.get("revisions"):
        return [], []
    booked = [dict(item) for item in prior.get("booked_dividends", [])]
    candidates: list[dict[str, Any]] = []
    filed_instants: dict[str, datetime] = {}
    for record in corrections["revisions"]:
        revision = record["revision"]
        ex_date = revision["ex_date"]
        if ex_date > session:
            continue
        try:
            filed = shadow_calendar.parse_timestamp(record["filed_at"], "filed_at")
        except ValueError:  # pragma: no cover - the loader already validated it
            continue
        if filed > close_cutoff:
            continue
        candidates.append(record)
        filed_instants[record["revision_id"]] = filed

    grouped: dict[str, list[dict[str, Any]]] = {}
    blockers: list[tuple[str, str]] = []
    for record in candidates:
        revision = record["revision"]
        matches = [
            entry
            for entry in booked
            if entry.get("symbol") == revision["symbol"]
            and entry.get("ex_date") == revision["ex_date"]
        ]
        if not matches:
            # No booked entitlement (not held through the ex-date): no correction.
            continue
        if len(matches) > 1:
            blockers.append(
                (
                    "CORRECTION_AMBIGUOUS",
                    f"Revision {record['revision_id']} matches more than one booked "
                    f"entitlement for {revision['symbol']} on {revision['ex_date']}; "
                    "refusing to guess which action to correct.",
                )
            )
            continue
        grouped.setdefault(matches[0]["action_id"], []).append(record)

    decisions: list[dict[str, Any]] = []
    for action_id in sorted(grouped):
        entitlement = next(item for item in booked if item["action_id"] == action_id)
        applied = set(entitlement.get("applied_revisions", []))
        pending = sorted(
            (record for record in grouped[action_id] if record["revision_id"] not in applied),
            key=lambda record: (
                filed_instants[record["revision_id"]],
                record["revision_id"],
            ),
        )
        booked_per_share = float(entitlement["booked_cash_per_share"])
        while pending:
            match = next(
                (
                    record
                    for record in pending
                    if _amounts_match(record["revision"]["old_amount"], booked_per_share)
                ),
                None,
            )
            if match is None:
                first = pending[0]
                blockers.append(
                    (
                        "CORRECTION_AMOUNT_MISMATCH",
                        f"Revision {first['revision_id']} declares old_amount "
                        f"{first['revision']['old_amount']} but the booked per-share "
                        f"amount for {action_id} (ex-date {first['revision']['ex_date']}) "
                        f"is {booked_per_share}; refusing to guess the delta.",
                    )
                )
                break
            revision = match["revision"]
            decisions.append(_correction_decision(match, entitlement))
            entitlement["booked_cash_per_share"] = float(revision["new_amount"])
            booked_per_share = float(revision["new_amount"])
            pending.remove(match)
    return decisions, blockers


def _correction_decision(
    record: dict[str, Any], entitlement: dict[str, Any]
) -> dict[str, Any]:
    revision = record["revision"]
    quantity = float(entitlement["quantity"])
    booked_per_share = float(entitlement["booked_cash_per_share"])
    delta = float(revision["new_amount"]) - booked_per_share
    return {
        "revision_id": record["revision_id"],
        "action_id": entitlement["action_id"],
        "symbol": entitlement["symbol"],
        "ex_date": entitlement["ex_date"],
        "pay_date": entitlement["pay_date"],
        "quantity": quantity,
        "booked_cash_per_share": booked_per_share,
        "old_amount": float(revision["old_amount"]),
        "new_amount": float(revision["new_amount"]),
        "delta": delta,
        "target": (
            "receivable" if entitlement.get("status") == "receivable" else "cash"
        ),
        "status": "applicable",
        "amount": delta * quantity,
        "action_reference": {
            "action_id": entitlement["action_id"],
            "symbol": entitlement["symbol"],
            "ex_date": entitlement["ex_date"],
            "pay_date": entitlement["pay_date"],
            "quantity": quantity,
            "cash_per_share": float(entitlement["cash_per_share"]),
            "amount": quantity * float(entitlement["cash_per_share"]),
        },
        "filed_at": record["filed_at"],
        "official_available_at": revision["official_available_at"],
    }


def _possible_target_symbols(
    execution: dict[str, Any],
    history_rows: list[dict[str, Any]],
    prior: dict[str, Any],
    pending: list[dict[str, Any]],
) -> set[str]:
    """Symbols the frozen strategy could target, so their closes are required.

    The strategy is deterministic; only the post-settlement account state is not
    known before settlement.  Run it at the two extreme states (nothing fills /
    every pending order fills) and require the union of target symbols.  All
    four declarative families either ignore the account state or depend only on
    whether a position is open, so this is exact for them and conservative for
    an unknown (never generated) strategy.
    """
    if "container_executor" in execution:
        # Generated code is called once, against the actual settled account.
        # Price checks for its targets happen when building the orders.
        return set()
    try:
        strategy = make_strategy(
            {
                "family": execution["strategy"]["family"],
                "parameters": dict(execution["parameters"]),
            }
        )
    except ValueError:
        return set(ETF_SYMBOLS)
    base_shares = {
        symbol: float(prior["positions"].get(symbol, 0.0)) for symbol in ETF_SYMBOLS
    }
    filled_shares = dict(base_shares)
    for order in pending:
        symbol = order.get("symbol")
        if symbol not in filled_shares:
            return set(ETF_SYMBOLS)
        quantity = float(order.get("quantity", 0.0))
        if order.get("side") == "buy":
            filled_shares[symbol] += quantity
        else:
            filled_shares[symbol] = max(0.0, filled_shares[symbol] - quantity)
    states = (
        {"cash": float(prior["cash"]), "shares": base_shares, "equity": float(prior["nav"])},
        {
            "cash": float(prior["cash"]),
            "shares": filled_shares,
            "equity": float(prior["nav"]),
        },
    )
    targets: set[str] = set()
    for state in states:
        try:
            weights = strategy(pd.DataFrame(history_rows), state)
        except (ValueError, TypeError):
            return set(ETF_SYMBOLS)
        if weights is None:
            continue
        try:
            normalized = _validate_weights(weights)
        except ValueError:
            return set(ETF_SYMBOLS)
        targets.update(
            symbol for symbol in ETF_SYMBOLS if normalized.get(symbol, 0.0) > _EPSILON
        )
    return targets


def _cash_tolerance(cash: float) -> float:
    return 1e-9 * max(1.0, abs(cash))


def _compute_day(body: dict[str, Any], execution: dict[str, Any]) -> dict[str, Any]:
    data_kind = body.get("data_kind", DATA_KIND)
    session = body["date"]
    prior = body["previous"]
    state = body["release_state_at_signal_point"]
    next_session = body["session"]["next_session"]
    raw = body["session"]["raw_bars"]
    actions = list(body["session"]["corporate_actions"])
    anomalies = [dict(item) for item in body.get("anomalies", [])]
    fee_rate = float(execution["transaction_cost_bps"]) / 10_000.0
    slippage_rate = float(execution["slippage_bps"]) / 10_000.0
    open_cutoff = _session_open(session)
    close_cutoff = _session_close(session)

    cash = float(prior["cash"])
    shares = {
        symbol: float(prior["positions"].get(symbol, 0.0)) for symbol in ETF_SYMBOLS
    }
    receivables = [dict(item) for item in prior["receivables"]]
    fees_total = float(prior["fees_total"])
    slippage_total = float(prior["slippage_total"])
    dividend_entitled_total = float(prior["dividend_entitled_total"])
    dividend_cash_total = float(prior["dividend_cash_total"])
    start_shares = dict(shares)

    # (a) corporate actions: share adjustments first, then cash-dividend
    # entitlements for start-of-day holdings on the ex-date.
    applied: list[dict[str, Any]] = []
    booked = [dict(item) for item in prior.get("booked_dividends", [])]
    adjustments = sorted(
        (action for action in actions if action["type"] == "share_adjustment"),
        key=lambda action: (action["symbol"], action["action_id"]),
    )
    dividends = sorted(
        (action for action in actions if action["type"] == "cash_dividend"),
        key=lambda action: (action["symbol"], action["action_id"]),
    )
    for action in adjustments:
        symbol = action["symbol"]
        quantity = start_shares.get(symbol, 0.0)
        if quantity <= _EPSILON:
            applied.append(
                {
                    "action_id": action["action_id"],
                    "type": action["type"],
                    "symbol": symbol,
                    "ex_date": session,
                    "status": "ignored_not_held",
                }
            )
            continue
        if not available_by(action, open_cutoff) or not _terms_valid(action):
            raise _Blocked(
                "missing_action_terms",
                f"{action['action_id']} has unresolved or unavailable terms.",
            )
        ratio = float(action["ratio"])
        before = shares[symbol]
        shares[symbol] = before * ratio
        applied.append(
            {
                "action_id": action["action_id"],
                "type": action["type"],
                "symbol": symbol,
                "ex_date": session,
                "ratio": ratio,
                "status": "applied",
                "shares_before": before,
                "shares_after": shares[symbol],
            }
        )
    for action in dividends:
        symbol = action["symbol"]
        quantity = start_shares.get(symbol, 0.0)
        if quantity <= _EPSILON:
            applied.append(
                {
                    "action_id": action["action_id"],
                    "type": action["type"],
                    "symbol": symbol,
                    "ex_date": session,
                    "status": "ignored_not_held",
                }
            )
            continue
        if not available_by(action, open_cutoff) or not _terms_valid(action):
            raise _Blocked(
                "missing_action_terms",
                f"{action['action_id']} has unresolved or unavailable terms.",
            )
        amount_per_share = float(action["cash_per_share"])
        amount = quantity * amount_per_share
        entitlement = {
            "action_id": action["action_id"],
            "symbol": symbol,
            "ex_date": session,
            "pay_date": action["pay_date"],
            "quantity": quantity,
            "cash_per_share": amount_per_share,
            "amount": amount,
        }
        receivables.append(entitlement)
        dividend_entitled_total += amount
        booked.append(
            {
                "action_id": action["action_id"],
                "symbol": symbol,
                "ex_date": session,
                "pay_date": action["pay_date"],
                "quantity": quantity,
                "cash_per_share": amount_per_share,
                "booked_cash_per_share": amount_per_share,
                "amount": amount,
                "status": "receivable",
                "applied_revisions": [],
            }
        )
        applied.append({**entitlement, "type": action["type"], "status": "receivable"})

    # (a2) apply the frozen applicable correction set once, before the open:
    # an unpaid entitlement adjusts its receivable, an already-paid one adjusts
    # cash.  Balances and NAV are committed in the same day transaction.
    corrections_applied: list[dict[str, Any]] = []
    correction_impact = 0.0
    for decision in body.get("corrections", []):
        amount = float(decision["amount"])
        revision_id = decision["revision_id"]
        action_id = decision["action_id"]
        entry = {**decision, "status": "applied"}
        booked_entry = next(
            (item for item in booked if item["action_id"] == action_id), None
        )
        if booked_entry is None:
            raise _Blocked(
                "CORRECTION_BOOKING_MISMATCH",
                f"Revision {revision_id} targets {action_id} but the ledger has no "
                "booked entitlement for it; refusing to apply a correction to an "
                "unknown balance.",
            )
        if decision["target"] == "receivable":
            receivable = next(
                (
                    item
                    for item in receivables
                    if item["action_id"] == action_id
                    and item["ex_date"] == decision["ex_date"]
                ),
                None,
            )
            if receivable is None:
                raise _Blocked(
                    "CORRECTION_BOOKING_MISMATCH",
                    f"Revision {revision_id} was frozen as a receivable correction "
                    f"but the {action_id} receivable is no longer unpaid; the day is "
                    "not committed partially.",
                )
            receivable["amount"] = float(receivable["quantity"]) * float(
                decision["new_amount"]
            )
            booked_entry["amount"] = receivable["amount"]
        else:
            cash += amount
            dividend_cash_total += amount
            booked_entry["amount"] = float(booked_entry["quantity"]) * float(
                decision["new_amount"]
            )
        booked_entry["booked_cash_per_share"] = float(decision["new_amount"])
        booked_entry["applied_revisions"] = [
            *booked_entry.get("applied_revisions", []),
            revision_id,
        ]
        corrections_applied.append(entry)
        correction_impact = math.fsum([correction_impact, amount])

    # (b) settle the previous session's pending orders at the unadjusted open,
    # sells before buys and then by symbol; exactly one terminal state each.
    pending = sorted(
        prior["next_orders"],
        key=lambda order: (
            0 if order.get("side") == "sell" else 1,
            order.get("symbol", ""),
            order.get("order_id", ""),
        ),
    )
    orders: list[dict[str, Any]] = []
    fills: list[dict[str, Any]] = []
    day_fees = 0.0
    day_slippage = 0.0
    for order in pending:
        symbol = order["symbol"]
        side = order["side"]
        quantity = float(order["quantity"])
        committed_at = prior.get("committed_at")
        try:
            persisted = (
                datetime.fromisoformat(committed_at)
                if isinstance(committed_at, str) and committed_at
                else None
            )
        except ValueError:
            persisted = None
        persisted_before_deadline = (
            persisted is not None
            and persisted.tzinfo is not None
            and persisted < open_cutoff
        )
        record = {
            "order_id": order["order_id"],
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "signal_date": order.get("signal_date"),
            "execution_date": order.get("execution_date"),
            "submitted_at": order.get("submitted_at"),
            "deadline": order.get("deadline"),
            # The creation-time planning estimate is kept only until settlement;
            # this field records the actual durable persistence time of the
            # signal day that created the order.
            "persisted_before_deadline": persisted_before_deadline,
            "persisted_at": committed_at,
            "target_weight": order.get("target_weight"),
            "date": session,
            "status": None,
            "reason": None,
            "fill": None,
        }
        submitted_at = order.get("submitted_at")
        try:
            submitted = datetime.fromisoformat(submitted_at) if submitted_at else None
        except (TypeError, ValueError):
            submitted = None
        if (
            submitted is None
            or submitted.tzinfo is None
            or submitted >= open_cutoff
            or not persisted_before_deadline
        ):
            record["status"] = "missed_deadline"
            record["reason"] = (
                "the order was not durably submitted before the execution "
                "session's 09:30 Asia/Shanghai deadline; it is never filled "
                "retroactively"
            )
            orders.append(record)
            continue
        row = raw.get(symbol)
        if not _is_executable_open(row, open_cutoff, data_kind=data_kind):
            record["status"] = "missing_open"
            record["reason"] = (
                "no confirmed unadjusted open is available for this symbol; this "
                "order expires without blocking the other orders"
            )
            orders.append(record)
            continue
        raw_open = float(row["open"])
        if side == "buy":
            price = raw_open * (1.0 + slippage_rate)
            fee = quantity * price * fee_rate
            cost = quantity * price + fee
            if cost > cash + _cash_tolerance(cash):
                record["status"] = "insufficient_cash"
                record["reason"] = (
                    "available cash at the open is below the frozen order cost; "
                    "the order is never resized, levered or partially filled"
                )
                orders.append(record)
                continue
            cash -= cost
            shares[symbol] += quantity
            slippage = quantity * (price - raw_open)
        else:
            if quantity > shares[symbol] + _EPSILON:
                record["status"] = "insufficient_shares"
                record["reason"] = (
                    "start-of-settlement holdings are below the frozen sell "
                    "quantity; the order is never resized and shorting is refused"
                )
                orders.append(record)
                continue
            price = raw_open * (1.0 - slippage_rate)
            fee = quantity * price * fee_rate
            cash += quantity * price - fee
            shares[symbol] -= quantity
            if abs(shares[symbol]) <= _EPSILON:
                shares[symbol] = 0.0
            slippage = quantity * (raw_open - price)
        fees_total += fee
        slippage_total += slippage
        day_fees += fee
        day_slippage += slippage
        fill = {
            "date": session,
            "order_id": order["order_id"],
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "raw_open": raw_open,
            "price": price,
            "price_basis": "unadjusted_open",
            "notional": quantity * raw_open,
            "fee": fee,
            "slippage": slippage,
        }
        record["status"] = "filled"
        record["fill"] = fill
        orders.append(record)
        fills.append(fill)
    if abs(cash) <= _cash_tolerance(cash):
        cash = 0.0

    _failpoint("shadow_after_fills")

    # (c) pay-date transfer from receivable to cash; NAV does not jump because
    # the receivable is removed as the cash is added.
    payments: list[dict[str, Any]] = []
    for item in sorted(
        list(receivables),
        key=lambda entry: (
            entry.get("pay_date") or "",
            entry.get("action_id") or "",
            entry.get("symbol") or "",
        ),
    ):
        if item["pay_date"] <= session:
            cash += item["amount"]
            dividend_cash_total += item["amount"]
            payments.append(
                {
                    "action_id": item["action_id"],
                    "symbol": item["symbol"],
                    "ex_date": item["ex_date"],
                    "pay_date": item["pay_date"],
                    "amount": item["amount"],
                    "status": "paid",
                }
            )
            receivables.remove(item)
            paid_entry = next(
                (
                    entry
                    for entry in booked
                    if entry["action_id"] == item["action_id"]
                    and entry["ex_date"] == item["ex_date"]
                ),
                None,
            )
            if paid_entry is not None:
                paid_entry["status"] = "paid"

    # A correction can make cash negative (already-paid cash adjustment with
    # insufficient available cash).  That is recorded honestly as a
    # correction-caused deficit: no financing, no forced sale, no rewrite.
    cash_deficit: dict[str, Any] | None = None
    if cash < -_cash_tolerance(cash):
        revision_ids = sorted(
            {
                entry["revision_id"]
                for entry in corrections_applied
                if entry["target"] == "cash" and entry["amount"] < 0
            }
            | set(prior.get("deficit_revision_ids", []))
        )
        cash_deficit = {
            "amount": -cash,
            "caused_by": "dividend_correction",
            "revision_ids": revision_ids,
            "financed": False,
            "forced_sale": False,
        }
        anomalies.append(
            {
                "code": "cash_deficit",
                "amount": -cash,
                "caused_by": "dividend_correction",
                "revision_ids": revision_ids,
            }
        )

    # (d) value at the unadjusted close plus cash and unpaid receivables.
    positions: dict[str, dict[str, Any]] = {}
    market_value = 0.0
    for symbol in ETF_SYMBOLS:
        quantity = shares[symbol]
        row = raw.get(symbol)
        close: float | None = None
        if quantity > _EPSILON and not _valid_close(row, close_cutoff, data_kind=data_kind):
            raise _Blocked(
                "missing_close",
                f"{symbol} is held but has no confirmed unadjusted close "
                f"available by {close_cutoff.isoformat()}.",
            )
        if _valid_close(row, close_cutoff, data_kind=data_kind):
            close = float(row["close"])
        value = quantity * close if quantity > _EPSILON and close is not None else 0.0
        if quantity > _EPSILON and close is None:  # pragma: no cover - guarded above
            raise _Blocked("missing_close", f"{symbol} cannot be valued.")
        market_value += value
        positions[symbol] = {
            "quantity": quantity,
            "close": close,
            "market_value": value,
        }
    receivable_total = math.fsum(item["amount"] for item in receivables)
    nav = cash + market_value + receivable_total

    # (e) generate the next session's signal only when the release is enabled at
    # this session's after-close signal point.
    signal: dict[str, Any] | None = None
    next_orders: list[dict[str, Any]] = []
    invocations: list[dict[str, Any]] = []
    target_changed = False
    if state == "enabled" and next_session is not None:
        window = body["session"]["derived_window"]
        if len(window["sessions"]) >= execution["signal_window_sessions"]:
            history_rows: list[dict[str, Any]] = []
            latest_values: dict[str, float] = {}
            for entry in window["rows"]:
                row = entry["row"]
                value = None if row is None else row.get("value")
                if value is None:
                    raise _Blocked(
                        "missing_signal_input",
                        f"derived value for {entry['symbol']} on {entry['session']} is missing.",
                    )
                if not available_by(row, close_cutoff):
                    raise _Blocked(
                        "missing_signal_input",
                        f"derived value for {entry['symbol']} on {entry['session']} "
                        f"is not available by {close_cutoff.isoformat()}.",
                    )
                history_rows.append(
                    {
                        "date": entry["session"],
                        "symbol": entry["symbol"],
                        "close": float(value),
                    }
                )
                if entry["session"] == session:
                    latest_values[entry["symbol"]] = float(value)
            try:
                strategy = make_strategy(
                    execution.get("strategy_spec", execution["strategy"]),
                    container_executor=execution.get("container_executor"),
                    evidence_callback=invocations.append,
                )
                weights = strategy(
                    pd.DataFrame(history_rows),
                    {"cash": cash, "shares": dict(shares), "equity": nav},
                )
            except IsolationError as error:
                raise _Blocked(
                    "strategy_isolation_failed", f"The frozen container strategy failed: {error}",
                ) from error
            except (ValueError, TypeError) as error:
                raise _Blocked(
                    "strategy_error",
                    f"The frozen strategy failed on the after-close signal input: {error}",
                ) from error
            if weights is not None:
                try:
                    normalized = _validate_weights(weights)
                except ValueError as error:
                    raise _Blocked(
                        "strategy_error",
                        f"The frozen strategy returned invalid target weights: {error}",
                    ) from error
                signal = {
                    "date": session,
                    "execution_date": next_session,
                    "target_weights": normalized,
                    "total_return_values": latest_values,
                    "window": list(window["sessions"]),
                }
                next_orders = _next_orders(
                    body,
                    normalized,
                    shares,
                    cash,
                    cash + market_value,
                    fee_rate,
                    slippage_rate,
                )
                target_changed = _target_changed(prior.get("signal_target"), normalized)
                if not next_orders and not target_changed:
                    # The target required no order and did not change: record the
                    # reason in the signal diagnostics, never an opportunity.
                    signal["opportunity"] = {
                        "due": False,
                        "reason": "no_target_change",
                    }
                elif next_orders and not target_changed:
                    # Price drift needed rebalancing orders but the strategy's
                    # target is unchanged, so this is not a decision opportunity.
                    signal["opportunity"] = {
                        "due": False,
                        "reason": "target_unchanged",
                    }

    # One due opportunity per decision point whose normalized target weights
    # changed from the version's previous signal target.  Drift-only orders are
    # not opportunities; the outcome is joined later from the execution day's
    # orders, so a committed day is never rewritten.  A metrics-not-ready
    # decision point has no signal at all and creates no opportunity.
    opportunities: list[dict[str, Any]] = (
        [
            {
                "signal_date": session,
                "execution_date": next_session,
                "order_ids": [order["order_id"] for order in next_orders],
                "due": True,
            }
        ]
        if signal is not None and target_changed
        else []
    )

    status = "committed"
    if anomalies or any(order["status"] != "filled" for order in orders):
        status = "committed_with_anomalies"
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": DAY_KIND,
        "scope": data_kind,
        "status": status,
        "version_id": body["version_id"],
        "date": session,
        "release": body["release"],
        "artifact_hash": body["artifact_hash"],
        "input_hash": _hash(body),
        "release_state_at_signal_point": state,
        "execution": {
            "strategy": body["execution"]["strategy"],
            "parameters": body["execution"]["parameters"],
            "initial_cash": body["execution"]["initial_cash"],
            "transaction_cost_bps": body["execution"]["transaction_cost_bps"],
            "slippage_bps": body["execution"]["slippage_bps"],
            "signal_window_sessions": body["execution"]["signal_window_sessions"],
        },
        "previous": {"date": prior["date"], "nav": prior["nav"]},
        "corporate_actions": applied,
        "dividends": {
            "entitlements": [
                item for item in applied if item.get("status") == "receivable"
            ],
            "payments": payments,
            "receivables": [dict(item) for item in receivables],
            "booked": booked,
            "entitled_total": dividend_entitled_total,
            "cash_total": dividend_cash_total,
        },
        "orders": orders,
        "fills": fills,
        "cash": cash,
        "positions": positions,
        "market_value": market_value,
        "dividend_receivables": receivable_total,
        "nav": nav,
        "fees": {"day": day_fees, "total": fees_total},
        "slippage": {"day": day_slippage, "total": slippage_total},
        "signal": signal,
        "next_orders": next_orders,
        "signal_target": (
            normalized if signal is not None else prior.get("signal_target")
        ),
        "opportunities": opportunities,
        **({"strategy_execution_evidence": invocations} if invocations else {}),
        "anomalies": anomalies,
        "corrections": corrections_applied,
        "correction_impact": correction_impact,
        "cash_deficit": cash_deficit,
        "progress": {
            "start_date": prior["start_date"] or session,
            "last_committed_date": session,
        },
    }


def _target_changed(
    previous: Mapping[str, float] | None, current: Mapping[str, float],
) -> bool:
    """Whether a signal is a real target change rather than price drift.

    Drift-only rebalancing orders keep the same normalized target weights; they
    settle normally but are not decision opportunities (spec #43 L34).  The
    first *invested* target counts as a change, and a ``None`` -> weights signal
    counts; an initial all-cash target is not an opportunity to trade.
    """
    if previous is None:
        return any(float(current.get(symbol, 0.0)) > _EPSILON for symbol in ETF_SYMBOLS)
    return any(
        abs(float(current.get(asset, 0.0)) - float(previous.get(asset, 0.0))) > 1e-12
        for asset in (*ETF_SYMBOLS, CASH)
    )


def _next_orders(
    body: dict[str, Any],
    weights: dict[str, float],
    shares: dict[str, float],
    cash: float,
    close_equity: float,
    fee_rate: float,
    slippage_rate: float,
) -> list[dict[str, Any]]:
    """Size the next session's orders at the signal close and persist them.

    Orders are concrete frozen quantities.  Buy quantities include the frozen
    fees and slippage, and the whole buy side is scaled at *creation* time by
    the post-cost budget (cash plus the same signal's net sell proceeds), so a
    full rebalance does not systematically fail on costs.  Settlement never
    resizes an order.
    """
    session = body["date"]
    next_session = body["session"]["next_session"]
    raw = body["session"]["raw_bars"]
    deadline = _session_open(next_session)
    submitted_at = _signal_point(session)
    cost_multiplier = (1.0 + fee_rate) * (1.0 + slippage_rate)
    close_cutoff = _session_close(session)
    intents: list[dict[str, Any]] = []
    sell_value = 0.0
    buy_cost = 0.0
    for symbol in ETF_SYMBOLS:
        weight = float(weights.get(symbol, 0.0))
        quantity_held = shares[symbol]
        if weight <= _EPSILON and quantity_held <= _EPSILON:
            continue
        row = raw.get(symbol)
        if not _valid_close(row, close_cutoff, data_kind=body.get("data_kind", DATA_KIND)):
            raise _Blocked(
                "missing_close",
                f"{symbol} has no confirmed unadjusted close for order sizing.",
            )
        close = float(row["close"])
        current_value = quantity_held * close
        target_value = close_equity * weight
        if target_value < current_value:
            quantity = (current_value - target_value) / close
            if quantity <= _EPSILON:
                continue
            sell_value += current_value - target_value
            intents.append(
                {"symbol": symbol, "side": "sell", "quantity": quantity, "weight": weight}
            )
        else:
            cost = target_value - current_value
            if cost <= _EPSILON:
                continue
            buy_cost += cost
            intents.append(
                {"symbol": symbol, "side": "buy", "cost": cost, "weight": weight}
            )
    budget = cash + sell_value * (1.0 - slippage_rate) * (1.0 - fee_rate)
    scale = min(1.0, budget / buy_cost) if buy_cost > 0 else 1.0
    if scale < 0.0:
        scale = 0.0
    orders: list[dict[str, Any]] = []
    for intent in intents:
        symbol = intent["symbol"]
        if intent["side"] == "sell":
            quantity = intent["quantity"]
        else:
            close = float(raw[symbol]["close"])
            quantity = intent["cost"] * scale / (close * cost_multiplier)
        if quantity <= _EPSILON:
            continue
        identity = {
            "version_id": body["version_id"],
            "signal_date": session,
            "symbol": symbol,
            "side": intent["side"],
            "quantity": quantity,
            "target_weight": intent["weight"],
        }
        orders.append(
            {
                **identity,
                "order_id": "ord-" + _hash(identity)[:32],
                "execution_date": next_session,
                "submitted_at": submitted_at,
                "deadline": deadline.isoformat(),
            }
        )
    return orders
