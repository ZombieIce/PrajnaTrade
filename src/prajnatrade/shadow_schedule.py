"""Run Shadow Live from a recurring operator job using an independent calendar."""

from __future__ import annotations

import fcntl
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from prajnatrade import release, shadow, shadow_calendar
from prajnatrade.snapshot_baseline import CN_TZ


def notifications(ledger: str | Path) -> list[dict[str, Any]]:
    """Return durable operator alerts in observation order."""
    if not (Path(ledger) / "shadow.sqlite").is_file():
        return []
    database = shadow.Ledger(ledger)
    try:
        return database.schedule_notifications()
    finally:
        database.close()


class _Tick:
    def __init__(self, database: shadow.Ledger, clock: datetime) -> None:
        self.database = database
        self.clock = clock
        self.today = clock.astimezone(CN_TZ).date().isoformat()
        self.scope = "unknown"
        self.alerts: list[dict[str, Any]] = []
        self.blocked = False

    def notify(
        self,
        code: str,
        message: str,
        next_action: str,
        *,
        session: str | None = None,
        release_id: str = "",
        blocked: bool = True,
    ) -> None:
        notice, first_observation = self.database.notify_schedule({
            "scope": self.scope,
            "release_id": release_id,
            "date": session or self.today,
            "code": code,
            "message": message,
            "next_action": next_action,
            "observed_at": self.clock.astimezone(UTC).isoformat(),
        })
        if (first_observation or blocked) and notice not in self.alerts:
            self.alerts.append(notice)
        self.blocked |= blocked

    def error(
        self,
        error: release.ReleaseRejection | shadow.ShadowRejection,
        session: str,
        release_id: str = "",
    ) -> None:
        self.notify(
            error.details["code"], error.details["message"], error.details["next_action"],
            session=session, release_id=release_id,
        )

    def run(
        self,
        store: Path,
        calendar: str | Path,
        data: str | Path,
        corrections_store: str | Path | None,
        *,
        plugin_config: str | Path | None = None,
        plugin_specs: list[str] | tuple[str, ...] = (),
    ) -> dict[str, Any]:
        report: dict[str, Any] = {"target_date": None, "update": None}
        try:
            schedule = shadow_calendar.load_calendar(calendar)
        except (OSError, ValueError) as error:
            self.notify("INVALID_CALENDAR", str(error), "Supply the operator's trading calendar.")
            return report
        self.scope = schedule["data_kind"]
        if not store.is_dir():
            self.notify(
                "RELEASE_STORE_UNAVAILABLE", f"The trusted release store {store} is unavailable.",
                "Check the scheduled job's release store.",
            )
            return report
        sessions = schedule["sessions"]
        closed = [session for session in sessions if self.clock >= shadow_calendar.session_close(session)]
        if not closed:
            return report
        target = closed[-1]
        report["target_date"] = target
        through = len(closed)
        next_session = sessions[through] if through < len(sessions) else None

        try:
            ids = release.release_ids(store)
        except release.ReleaseRejection as error:
            self.error(error, target)
            return report

        requested: list[str] = []
        next_by_release: dict[str, int] = {}
        start_index = through
        for release_id in ids:
            try:
                status = release.status(store, release_id, as_of=target)
                if status["scope"] != self.scope or status["state"] == "published":
                    continue
                progress = self.database.get_progress(release_id)
                if not progress and status["state"] == "retired":
                    continue
                first = next(
                    (event["effective_signal_date"] for event in status["timeline"]
                     if event["action"] == "enable"),
                    None,
                )
                if first is None:
                    continue
                if first not in sessions:
                    self.notify(
                        "CALENDAR_COVERAGE_MISSING", f"The calendar omits the enablement date {first}.",
                        "Restore the trading calendar from the version's first scheduled session.",
                        session=target, release_id=release_id,
                    )
                    continue
                first_index = sessions.index(first)
                self._verify_release_calendar(store, release_id, sessions, first)
                self._record_deadlines(release_id, status["timeline"], sessions, first_index, through)
                last = progress["last_committed_date"] if progress else None
                if last is None or last < target:
                    requested.append(release_id)
                    next_by_release[release_id] = (
                        sessions.index(last) + 1 if last in sessions else first_index
                    )
                    start_index = min(start_index, first_index)
                elif last == target:
                    self._notice_committed_day(release_id, target)
            except (release.ReleaseRejection, shadow.ShadowRejection) as error:
                self.error(error, target, release_id)

        if not requested:
            return report
        if next_session is None:
            self.notify(
                "CALENDAR_HORIZON_EXHAUSTED",
                f"No next trading session is registered after {target}.",
                "Extend the operator's trading calendar before retrying.",
                session=target,
            )
            return report

        try:
            update, _ = shadow.update(
                self.database.directory, store, data, target, requested,
                corrections_store=corrections_store,
                expected_sessions=tuple(sessions[start_index:through + 1]),
                expected_data_kind=self.scope,
                plugin_config=plugin_config,
                plugin_specs=plugin_specs,
            )
        except shadow.ShadowRejection as error:
            self.error(error, target)
            return report
        except (OSError, RuntimeError) as error:
            self.notify(
                "UPDATE_FAILED", str(error), "Inspect the scheduled run, then retry.", session=target
            )
            return report

        report["update"] = update
        for version in update["versions"]:
            release_id = version["release_id"]
            index = next_by_release[release_id]
            for session in sessions[index:through]:
                if not self._notice_committed_day(release_id, session):
                    break
            if version["status"] == "blocked":
                last = version["last_committed_date"]
                if last in sessions and last < target:
                    index = sessions.index(last) + 1
                self.notify(
                    version["reason_code"], version["reason"],
                    "Inspect the version's input availability, then retry.",
                    session=sessions[index], release_id=release_id,
                )
        return report

    def _notice_committed_day(self, release_id: str, session: str) -> bool:
        try:
            day = self.database.verified_day(release_id, session)
        except shadow.ShadowRejection as error:
            self.error(error, session, release_id)
            return False
        if day is None:
            return False
        day_result = day["result"]
        if any(item["code"] == "signal_window_incomplete" for item in day_result["anomalies"]):
            self.notify(
                "SIGNAL_WINDOW_INCOMPLETE", f"The {session} signal had insufficient history.",
                "Load the frozen strategy's required history before the next close.",
                session=session, release_id=release_id, blocked=False,
            )
        if any(order["status"] == "missing_open" for order in day_result["orders"]):
            self.notify(
                "MISSING_EXECUTION_OPEN",
                "An order expired because its unadjusted open was unavailable.",
                "Review the unfilled orders and feed evidence in the Shadow Live ledger.",
                session=session, release_id=release_id, blocked=False,
            )
        return True

    def _verify_release_calendar(
        self, store: Path, release_id: str, sessions: list[str], first: str,
    ) -> None:
        admission = release.fetch(store, release_id)["artifact"]["admission"]
        if not admission["automatic_enable"]:
            return
        if not shadow_calendar.authorized_sessions_match(
            sessions, admission["calendar"]["sessions"], first,
        ):
            raise shadow.ShadowRejection(
                "AUTHORIZED_CALENDAR_MISMATCH", "schedule", "restore-authorized-calendar",
                "The scheduled trading dates differ from the calendar bound to this release.",
            )

    def _record_deadlines(
        self,
        release_id: str,
        timeline: list[dict],
        sessions: list[str],
        start_index: int,
        through: int,
    ) -> None:
        event_index = 0
        state = "published"
        for session, following in zip(sessions[start_index:through], sessions[start_index + 1:], strict=False):
            deadline = shadow_calendar.session_open(following)
            if self.clock < deadline:
                break
            while event_index < len(timeline) and timeline[event_index]["effective_signal_date"] <= session:
                state = timeline[event_index]["state_after"]
                event_index += 1
            if state != "enabled":
                continue
            day = self.database.verified_day(release_id, session)
            if day is None or datetime.fromisoformat(day["committed_at"]) >= deadline:
                self.notify(
                    "MISSED_SIGNAL_DEADLINE",
                    f"The {session} signal day did not commit before {deadline.isoformat()}.",
                    "Review the gap, restore missing data, and resume subsequent trading sessions.",
                    session=session, release_id=release_id, blocked=False,
                )


def tick(
    ledger: str | Path,
    store: str | Path,
    calendar: str | Path,
    data: str | Path,
    *,
    corrections_store: str | Path | None = None,
    now: datetime | None = None,
    plugin_config: str | Path | None = None,
    plugin_specs: list[str] | tuple[str, ...] = (),
) -> tuple[dict[str, Any], bool]:
    """Process due sessions once and return the update and durable operator alerts."""
    clock = now if now is not None else datetime.now(UTC)
    if clock.utcoffset() is None:
        raise ValueError("The scheduler clock requires a timezone.")
    database = shadow.Ledger(ledger)
    try:
        with (database.directory / "schedule.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            run = _Tick(database, clock)
            result = run.run(
                Path(store),
                calendar,
                data,
                corrections_store,
                plugin_config=plugin_config,
                plugin_specs=plugin_specs,
            )
    finally:
        database.close()
    return {
        "schema_version": 1,
        "scope": run.scope,
        "status": "blocked" if run.blocked else "ok",
        **result,
        "notifications": run.alerts,
    }, run.blocked
