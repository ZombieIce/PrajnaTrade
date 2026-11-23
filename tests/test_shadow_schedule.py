"""Scheduled Shadow Live behavior at the operator tick and ledger seams."""

from datetime import datetime

import pytest

from cli_helpers import ok, write_json
from test_shadow_cli import PAST, isolated_store, lifecycle, releases, shadow_feed  # noqa: F401

pytestmark = pytest.mark.isolation


def test_unavailable_feed_leaves_one_alert_and_records_actual_deadline_gap(
    releases, tmp_path
):
    from prajnatrade import shadow_schedule

    entry = releases["equal_weight"]
    store = isolated_store(tmp_path, entry)
    version = entry["release_id"]
    first, second, *_ = PAST
    ok(lifecycle("enable", store, version, first))
    calendar = write_json(
        tmp_path / "calendar.json",
        {
            "schema_version": 1,
            "data_kind": "synthetic",
            "source": "Synthetic exchange calendar",
            "sessions": PAST,
        },
    )
    ledger = tmp_path / "ledger"
    feed = tmp_path / "feed.json"

    def tick(at):
        return shadow_schedule.tick(
            ledger, store, calendar, feed, now=datetime.fromisoformat(at)
        )

    first_result, blocked = tick("2024-01-02T16:00:00+08:00")
    assert blocked
    assert [(event["date"], event["code"]) for event in first_result["notifications"]] == [
        (first, "INVALID_FEED"),
    ]
    second_result, blocked = tick("2024-01-02T17:00:00+08:00")
    assert blocked
    assert second_result["notifications"] == first_result["notifications"]

    write_json(feed, shadow_feed(PAST))
    recovered, blocked = tick("2024-01-03T16:00:00+08:00")
    assert not blocked, recovered
    assert [(item["release_id"], item["date"], item["code"])
            for item in recovered["notifications"]] == [(version, first, "MISSED_SIGNAL_DEADLINE")]
    assert recovered["update"]["versions"][0]["committed_date"] == second
    assert [item["code"] for item in shadow_schedule.notifications(ledger)] == [
        "INVALID_FEED", "MISSED_SIGNAL_DEADLINE",
    ]


def test_release_cannot_use_a_feed_with_a_different_data_scope(releases, tmp_path):
    from prajnatrade import shadow

    entry = releases["equal_weight"]
    store = isolated_store(tmp_path, entry)
    version = entry["release_id"]
    ok(lifecycle("enable", store, version, PAST[0]))
    payload = shadow_feed(PAST)
    payload.update(schema_version=2, data_kind="real", raw_price_basis="unadjusted")
    for row in payload["raw_bars"]:
        row["open_available_at"] = f"{row['date']}T09:30:00+08:00"
        row["close_available_at"] = f"{row['date']}T15:00:00+08:00"
    feed = write_json(tmp_path / "feed.json", payload)

    ledger = tmp_path / "ledger"
    result, blocked = shadow.update(ledger, store, feed, PAST[0], [version])
    assert blocked
    assert result["versions"][0]["reason_code"] == "FEED_SCOPE_MISMATCH"
    assert list(shadow.iter_committed_days(ledger, version)) == []


def test_scheduler_reports_a_signal_gap_even_when_the_day_can_commit(releases, tmp_path):
    from prajnatrade import shadow_schedule

    entry = releases["momentum"]
    store = isolated_store(tmp_path, entry)
    version = entry["release_id"]
    first = PAST[0]
    ok(lifecycle("enable", store, version, first))
    calendar = write_json(
        tmp_path / "calendar.json",
        {
            "schema_version": 1,
            "data_kind": "synthetic",
            "source": "Synthetic exchange calendar",
            "sessions": PAST,
        },
    )
    feed = write_json(tmp_path / "feed.json", shadow_feed(PAST))

    report, blocked = shadow_schedule.tick(
        tmp_path / "ledger", store, calendar, feed,
        now=datetime.fromisoformat(f"{first}T16:00:00+08:00"),
    )
    assert not blocked
    assert report["update"]["versions"][0]["committed_date"] == first
    assert [(event["release_id"], event["date"], event["code"])
            for event in report["notifications"]] == [(version, first, "SIGNAL_WINDOW_INCOMPLETE")]
