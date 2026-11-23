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
