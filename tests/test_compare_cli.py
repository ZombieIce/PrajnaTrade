"""Public-CLI tests for multi-version Shadow Live update and comparison (#34).

Everything here is synthetic.  Approved releases are produced by the real
protected-evaluation + release flow in ``tests/test_shadow_cli.py`` (the
module-scoped ``releases`` fixture, built once), plus one inverse-volatility
release for the correction-deficit scenario.  Each test copies the release
directories into its own trusted store so lifecycle timelines are isolated.
The market feed is synthetic point-in-time JSON: no broker, no real orders, no
real market data and no network.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from cli_helpers import cli, ok
from test_correction_delta_cli import (
    SESSIONS as CORRECTION_SESSIONS,
    correction_journal,
    shadow_update_with_corrections,
)
from test_protected_eval_cli import (
    SYMBOLS,
    synthetic_snapshot,
)
from test_shadow_cli import (
    DIVIDEND_SYMBOL,
    FUTURE,
    MOMENTUM_SYMBOL,
    build_release,
    execution,
    isolated_store,
    ledger_rows,
    lifecycle,
    releases,
    shadow_feed,
    shadow_report,
    shadow_update,
    write_feed,
)

pytestmark = pytest.mark.isolation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def shadow_compare(
    ledger: Path,
    data: Path,
    versions: tuple[str, ...],
    *,
    output: Path | None = None,
    env: dict[str, str] | None = None,
):
    arguments = [
        "shadow", "compare",
        "--ledger", str(ledger),
        "--data", str(data),
        "--versions", ",".join(versions),
    ]
    if output is not None:
        arguments += ["--output", str(output)]
    return cli(*arguments, env=env)


def day_row(ledger: Path, version_id: str, session: str) -> dict:
    with sqlite3.connect(ledger / "shadow.sqlite") as connection:
        row = connection.execute(
            "SELECT committed_at, status, result_hash FROM days "
            "WHERE version_id = ? AND date = ?",
            (version_id, session),
        ).fetchone()
    if row is None:
        raise AssertionError(f"no committed day row for {version_id} {session}")
    return {"committed_at": row[0], "status": row[1], "result_hash": row[2]}


def stored_results(ledger: Path, version_id: str, report_path: Path) -> dict:
    """Every verified committed day result of one version, keyed by date."""
    ok(shadow_report(ledger, version_id, report_path))
    days = json.loads(report_path.read_text(encoding="utf-8"))["days"]
    return {entry["date"]: entry["result"] for entry in days}


# ---------------------------------------------------------------------------
# 1. Multi-version update: independent accounts, actual commit timestamps,
#    a blocked version beside a committed one.
# ---------------------------------------------------------------------------


def test_update_reports_actual_commit_timestamps_and_blocks_one_version_only(
    releases: dict, tmp_path: Path
) -> None:
    equal = releases["equal_weight"]
    momentum = releases["momentum"]
    equal_id, momentum_id = equal["release_id"], momentum["release_id"]
    root = tmp_path / "multi-update"
    store = isolated_store(root, equal, momentum)
    ledger = root / "ledger"
    sessions = FUTURE[:4]
    target = sessions[3]
    ok(lifecycle("enable", store, equal_id, sessions[0]))
    ok(lifecycle("enable", store, momentum_id, sessions[0]))
    feed = write_feed(
        root / "feed.json",
        shadow_feed(
            sessions,
            raw_overrides={(target, DIVIDEND_SYMBOL): {"close": None}},
        ),
    )

    response = shadow_update(ledger, store, feed, target)
    assert response.returncode == 2, response.stdout
    payload = json.loads(response.stdout)
    assert payload["target_date"] == target
    by_id = {entry["release_id"]: entry for entry in payload["versions"]}
    assert set(by_id) == {equal_id, momentum_id}

    blocked = by_id[equal_id]
    assert blocked["status"] == "blocked"
    assert blocked["reason_code"] == "missing_close"
    assert blocked["target_date"] == target
    assert blocked["committed_at"] is None
    assert blocked["committed_date"] == sessions[2]
    assert blocked["last_committed_date"] == sessions[2]
    assert len([row for row in ledger_rows(ledger, "days") if row[0] == equal_id]) == 3

    committed = by_id[momentum_id]
    assert committed["status"] == "committed", committed
    assert committed["target_date"] == target
    assert committed["committed_date"] == target
    assert committed["last_committed_date"] == target
    assert committed["committed_at"] == day_row(ledger, momentum_id, target)["committed_at"]
    # The actual posting time is a real timezone-aware timestamp, not the target.
    datetime.fromisoformat(committed["committed_at"])

    # Re-running the target is already_committed with the stored commit time.
    rerun = ok(shadow_update(ledger, store, feed, target, release_ids=(momentum_id,)))
    again = rerun["versions"][0]
    assert again["status"] == "already_committed"
    assert again["committed_at"] == committed["committed_at"]
    assert again["target_date"] == target


# ---------------------------------------------------------------------------
# 2. Comparison: longest common consecutive run, normalisation and the maths.
# ---------------------------------------------------------------------------


def comparison_feed(sessions: list[str]) -> dict:
    """A feed where the two versions trade, pay a dividend and miss one open."""
    return shadow_feed(
        sessions,
        closes={
            MOMENTUM_SYMBOL: [10.0, 10.0, 10.25, 10.4, 10.5, 10.2],
            DIVIDEND_SYMBOL: [10.0, 10.0, 10.0, 9.8, 9.8, 9.8],
        },
        derived={
            MOMENTUM_SYMBOL: [10.0, 10.1, 10.2, 10.4, 10.6, 10.8],
            DIVIDEND_SYMBOL: [10.0, 10.0, 10.0, 10.8, 10.9, 11.0],
        },
        raw_overrides={(sessions[4], DIVIDEND_SYMBOL): {"open": None}},
        actions=[
            {
                "date": sessions[3],
                "symbol": DIVIDEND_SYMBOL,
                "type": "cash_dividend",
                "cash_per_share": 0.2,
                "pay_date": sessions[5],
            }
        ],
    )


def expected_metrics(days: dict[str, dict], run: list[str]) -> dict:
    """Hand-computed run metrics straight from the stored committed day rows."""
    results = [days[session] for session in run]
    start_nav = results[0]["nav"]
    end_nav = results[-1]["nav"]
    peak = None
    maximum_drawdown = 0.0
    for result in results:
        peak = result["nav"] if peak is None else max(peak, result["nav"])
        maximum_drawdown = min(maximum_drawdown, result["nav"] / peak - 1.0)
    fees = sum(result["fees"]["day"] for result in results)
    dividends = sum(
        payment["amount"]
        for result in results
        for payment in result["dividends"]["payments"]
    )
    correction_impact = sum(
        result.get("correction_impact", 0.0) for result in results
    )
    return {
        "starting_nav": start_nav,
        "ending_nav": end_nav,
        "cumulative_return": end_nav / start_nav - 1.0,
        "max_drawdown": maximum_drawdown,
        "fees_paid": fees,
        "dividends_paid": dividends,
        "correction_impact": correction_impact,
        "strategy_return": (end_nav - correction_impact) / start_nav - 1.0,
    }


def test_comparison_uses_the_longest_common_run_and_reports_run_metrics(
    releases: dict, tmp_path: Path
) -> None:
    equal = releases["equal_weight"]
    momentum = releases["momentum"]
    equal_id, momentum_id = equal["release_id"], momentum["release_id"]
    root = tmp_path / "compare"
    store = isolated_store(root, equal, momentum)
    ledger = root / "ledger"
    sessions = FUTURE[:6]
    feed = write_feed(root / "feed.json", comparison_feed(sessions))
    ok(lifecycle("enable", store, equal_id, sessions[0]))
    ok(lifecycle("enable", store, momentum_id, sessions[2]))

    updated = ok(shadow_update(ledger, store, feed, sessions[-1]))
    by_id = {entry["release_id"]: entry for entry in updated["versions"]}
    assert by_id[equal_id]["status"] == "committed"
    assert by_id[momentum_id]["status"] in {"committed", "committed_with_anomalies"}

    equal_days = stored_results(ledger, equal_id, root / "equal-report.json")
    momentum_days = stored_results(ledger, momentum_id, root / "momentum-report.json")
    assert sorted(equal_days) == sessions
    assert sorted(momentum_days) == sessions[2:]
    # Independent accounts: the equal-weight version holds five symbols while the
    # momentum version starts from its own initial cash and holds none yet.
    assert all(
        equal_days[sessions[2]]["positions"][symbol]["quantity"] > 0
        for symbol in SYMBOLS
    )
    assert momentum_days[sessions[2]]["positions"][MOMENTUM_SYMBOL]["quantity"] == 0.0
    assert momentum_days[sessions[2]]["cash"] == pytest.approx(
        execution(momentum)["initial_cash"]
    )

    report_path = root / "comparison.json"
    report = ok(
        shadow_compare(
            ledger,
            feed,
            (equal_id, momentum_id),
            output=report_path,
        )
    )
    assert report["schema_version"] == 1
    assert report["kind"] == "shadow_forward_comparison"
    assert report["scope"] == "synthetic"
    assert report["versions"] == [equal_id, momentum_id]
    assert report["comparable"] is True
    assert report["reason"] is None
    run = sessions[2:]
    assert report["common_interval"] == {
        "start": sessions[2],
        "end": sessions[-1],
        "sessions": len(run),
    }
    assert report["comparison"] == {"comparable": True, "reason": None}
    assert set(report["per_version"]) == {equal_id, momentum_id}

    equal_expected = expected_metrics(equal_days, run)
    momentum_expected = expected_metrics(momentum_days, run)
    for version_id, expected in (
        (equal_id, equal_expected),
        (momentum_id, momentum_expected),
    ):
        entry = report["per_version"][version_id]
        assert entry["release_id"] == version_id
        assert entry["date_range"] == {"start": run[0], "end": run[-1]}
        assert entry["sessions"] == len(run)
        assert entry["starting_nav"] == pytest.approx(expected["starting_nav"])
        assert entry["ending_nav"] == pytest.approx(expected["ending_nav"])
        assert entry["cumulative_return"] == pytest.approx(
            expected["cumulative_return"]
        )
        assert entry["max_drawdown"] == pytest.approx(expected["max_drawdown"])
        assert entry["fees_paid"] == pytest.approx(expected["fees_paid"])
        assert entry["dividends_paid"] == pytest.approx(expected["dividends_paid"])
        assert entry["correction_impact"] == pytest.approx(0.0)
        assert entry["strategy_return"] == pytest.approx(
            expected["cumulative_return"]
        )
        days = equal_days if version_id == equal_id else momentum_days
        expected_unfilled = [
            order
            for session in run
            for order in days[session]["orders"]
            if order["status"] != "filled"
        ]
        assert entry["unfilled_order_count"] == len(expected_unfilled)
        assert entry["committed_coverage"]["sessions"] == len(
            equal_days if version_id == equal_id else momentum_days
        )

    # The values are real, not zeros: the momentum version pays fees and leaves
    # one evidenced non-fill in the run; the equal-weight version is paid a
    # dividend in the run and has a real drawdown.
    momentum_entry = report["per_version"][momentum_id]
    assert momentum_entry["fees_paid"] > 0
    assert momentum_entry["max_drawdown"] < 0
    assert len(momentum_entry["unfilled_orders"]) == 1
    unfilled = momentum_entry["unfilled_orders"][0]
    assert unfilled["date"] == sessions[4]
    assert unfilled["symbol"] == DIVIDEND_SYMBOL
    assert unfilled["status"] == "missing_open"
    assert momentum_entry["unfilled_order_count"] == 1
    equal_entry = report["per_version"][equal_id]
    assert equal_entry["dividends_paid"] > 0
    assert equal_entry["max_drawdown"] < 0
    assert equal_entry["fees_paid"] == pytest.approx(0.0)

    # The written document is byte-identical for identical inputs (no generated
    # timestamps in the compared data).
    written = report_path.read_bytes()
    again = ok(
        shadow_compare(ledger, feed, (equal_id, momentum_id), output=root / "again.json")
    )
    assert (root / "again.json").read_bytes() == written
    assert again == report


# ---------------------------------------------------------------------------
# 3. A third version with too little common history: every requested version
#    must share the run, so the comparison is explicitly incomparable.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def late_release(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """A real approved inverse-volatility release with its own initial cash."""
    return build_release(
        tmp_path_factory.mktemp("compare-late"),
        synthetic_snapshot(derived_range=("2024-01-13", "2024-01-20")),
        "inverse",
        "inverse_volatility",
        {"lookback": 2},
        initial_cash=50_000,
    )


def test_third_version_without_a_common_run_makes_the_comparison_incomparable(
    releases: dict, late_release: dict, tmp_path: Path
) -> None:
    equal = releases["equal_weight"]
    momentum = releases["momentum"]
    late = late_release
    equal_id, momentum_id, late_id = (
        equal["release_id"],
        momentum["release_id"],
        late["release_id"],
    )
    root = tmp_path / "three-versions"
    store = isolated_store(root, equal, momentum, late)
    ledger = root / "ledger"
    sessions = FUTURE[:4]
    feed = write_feed(root / "feed.json", shadow_feed(sessions))
    ok(lifecycle("enable", store, equal_id, sessions[0]))
    ok(lifecycle("enable", store, momentum_id, sessions[0]))
    ok(lifecycle("enable", store, late_id, sessions[-1]))
    updated = ok(shadow_update(ledger, store, feed, sessions[-1]))
    by_id = {entry["release_id"]: entry for entry in updated["versions"]}
    assert set(by_id) == {equal_id, momentum_id, late_id}
    assert by_id[late_id]["committed_date"] == sessions[-1]

    # The late version has a different initial cash and its own account: a flat
    # session cannot inherit the other versions' positions or NAV.
    late_days = stored_results(ledger, late_id, root / "late-report.json")
    assert sorted(late_days) == [sessions[-1]]
    assert execution(late)["initial_cash"] == 50_000.0
    assert late_days[sessions[-1]]["nav"] == pytest.approx(50_000.0)
    assert late_days[sessions[-1]]["positions"] == {
        symbol: {"quantity": 0.0, "close": 10.0, "market_value": 0.0}
        for symbol in SYMBOLS
    }

    report = ok(shadow_compare(ledger, feed, (equal_id, momentum_id, late_id)))
    assert report["versions"] == [equal_id, momentum_id, late_id]
    assert report["comparable"] is False
    assert report["reason"] == "INSUFFICIENT_COMMON_SESSIONS"
    assert report["comparison"] == {
        "comparable": False,
        "reason": "INSUFFICIENT_COMMON_SESSIONS",
    }
    assert report["common_interval"] == {"start": None, "end": None, "sessions": 0}
    assert set(report["per_version"]) == {equal_id, momentum_id, late_id}
    assert report["per_version"][late_id]["committed_coverage"] == {
        "dates": [sessions[-1]],
        "sessions": 1,
        "first_committed_date": sessions[-1],
        "last_committed_date": sessions[-1],
    }
    for version_id in (equal_id, momentum_id, late_id):
        entry = report["per_version"][version_id]
        assert entry["included"] is False
        assert entry["reason"] == "INSUFFICIENT_COMMON_SESSIONS"
        assert entry["sessions"] == 0
        assert entry["cumulative_return"] is None
        assert entry["strategy_return"] is None
    assert report["per_version"][equal_id]["committed_coverage"]["sessions"] == 4
    assert report["per_version"][momentum_id]["committed_coverage"]["sessions"] == 4

    # The same two versions that do share a run remain comparable: the third
    # version is never silently dropped from a request that names it.
    pair = ok(shadow_compare(ledger, feed, (equal_id, momentum_id)))
    assert pair["comparable"] is True
    assert pair["common_interval"] == {
        "start": sessions[0],
        "end": sessions[-1],
        "sessions": 4,
    }


# ---------------------------------------------------------------------------
# 4. A committed gap in the comparison feed splits runs: the longest run wins
#    (ties go to the latest) and dates outside it are never used.
# ---------------------------------------------------------------------------


def test_nonconsecutive_common_dates_split_runs_and_only_the_chosen_run_is_used(
    releases: dict, tmp_path: Path
) -> None:
    equal = releases["equal_weight"]
    momentum = releases["momentum"]
    equal_id, momentum_id = equal["release_id"], momentum["release_id"]
    root = tmp_path / "gap"
    store = isolated_store(root, equal, momentum)
    ledger = root / "ledger"
    committed_sessions = ["2030-01-02", "2030-01-03", "2030-01-06", "2030-01-07"]
    update_feed = write_feed(
        root / "update-feed.json", shadow_feed(committed_sessions)
    )
    ok(lifecycle("enable", store, equal_id, committed_sessions[0]))
    ok(lifecycle("enable", store, momentum_id, committed_sessions[0]))
    ok(shadow_update(ledger, store, update_feed, committed_sessions[-1]))

    equal_days = stored_results(ledger, equal_id, root / "equal-report.json")
    assert sorted(equal_days) == committed_sessions

    # The comparison feed inserts one uncommitted session between 01-03 and
    # 01-06: the committed dates now form two 2-session runs and the tie goes to
    # the latest one.
    compare_sessions = [
        "2030-01-02", "2030-01-03", "2030-01-04", "2030-01-06", "2030-01-07",
    ]
    compare_feed = write_feed(
        root / "compare-feed.json", shadow_feed(compare_sessions)
    )
    report = ok(shadow_compare(ledger, compare_feed, (equal_id, momentum_id)))
    assert report["comparable"] is True
    assert report["common_interval"] == {
        "start": "2030-01-06",
        "end": "2030-01-07",
        "sessions": 2,
    }
    entry = report["per_version"][equal_id]
    assert entry["date_range"] == {"start": "2030-01-06", "end": "2030-01-07"}
    assert entry["sessions"] == 2
    # Normalisation happens at the chosen run's first day, not at the earliest
    # committed date, and the earlier run's NAV is never used.
    assert entry["starting_nav"] == pytest.approx(equal_days["2030-01-06"]["nav"])
    assert entry["starting_nav"] != pytest.approx(equal_days["2030-01-02"]["nav"])
    assert entry["ending_nav"] == pytest.approx(equal_days["2030-01-07"]["nav"])
    assert entry["cumulative_return"] == pytest.approx(
        equal_days["2030-01-07"]["nav"] / equal_days["2030-01-06"]["nav"] - 1.0
    )
    # Every requested version must cover the chosen run.
    assert report["per_version"][momentum_id]["date_range"] == {
        "start": "2030-01-06",
        "end": "2030-01-07",
    }

    # A strictly longer earlier run wins over a shorter later run (only a tie
    # goes to the latest run).
    longest_root = tmp_path / "gap-longest"
    longest_store = isolated_store(longest_root, equal, momentum)
    longest_ledger = longest_root / "ledger"
    longest_sessions = [
        "2030-01-02", "2030-01-03", "2030-01-04", "2030-01-07", "2030-01-08",
    ]
    longest_update_feed = write_feed(
        longest_root / "update-feed.json", shadow_feed(longest_sessions)
    )
    ok(lifecycle("enable", longest_store, equal_id, longest_sessions[0]))
    ok(lifecycle("enable", longest_store, momentum_id, longest_sessions[0]))
    ok(shadow_update(longest_ledger, longest_store, longest_update_feed, longest_sessions[-1]))
    longest_compare_feed = write_feed(
        longest_root / "compare-feed.json",
        shadow_feed(
            ["2030-01-02", "2030-01-03", "2030-01-04", "2030-01-05",
             "2030-01-07", "2030-01-08"]
        ),
    )
    longest = ok(
        shadow_compare(longest_ledger, longest_compare_feed, (equal_id, momentum_id))
    )
    assert longest["common_interval"] == {
        "start": "2030-01-02",
        "end": "2030-01-04",
        "sessions": 3,
    }


# ---------------------------------------------------------------------------
# 5. One blocked version beside committed ones; the rerun only adds the
#    missing version/day and every order keeps exactly one terminal state.
# ---------------------------------------------------------------------------


def test_blocked_version_does_not_disturb_committed_ones_and_rerun_only_fills_it(
    releases: dict, late_release: dict, tmp_path: Path
) -> None:
    equal = releases["equal_weight"]
    momentum = releases["momentum"]
    late = late_release
    equal_id, momentum_id, late_id = (
        equal["release_id"],
        momentum["release_id"],
        late["release_id"],
    )
    root = tmp_path / "blocked-multi"
    store = isolated_store(root, equal, momentum, late)
    ledger = root / "ledger"
    sessions = FUTURE[:4]
    target = sessions[-1]
    feed_payload = shadow_feed(
        sessions,
        closes={
            MOMENTUM_SYMBOL: [10.0, 10.0, 10.25, 10.4],
            DIVIDEND_SYMBOL: [10.0, 10.0, 10.0, 9.8],
        },
        derived={
            MOMENTUM_SYMBOL: [10.0, 10.1, 10.2, 10.4],
            DIVIDEND_SYMBOL: [10.0, 10.0, 10.0, 10.8],
        },
        raw_overrides={(target, DIVIDEND_SYMBOL): {"close": None}},
        actions=[
            {
                "date": target,
                "symbol": DIVIDEND_SYMBOL,
                "type": "cash_dividend",
                "cash_per_share": 0.2,
                "pay_date": FUTURE[4],
            }
        ],
    )
    feed = write_feed(root / "broken.json", feed_payload)
    for release_id in (equal_id, momentum_id, late_id):
        ok(lifecycle("enable", store, release_id, sessions[0]))

    blocked = shadow_update(ledger, store, feed, target)
    assert blocked.returncode == 2, blocked.stdout
    by_id = {
        entry["release_id"]: entry
        for entry in json.loads(blocked.stdout)["versions"]
    }
    assert by_id[equal_id]["status"] == "blocked"
    assert by_id[equal_id]["reason_code"] == "missing_close"
    assert by_id[equal_id]["committed_date"] == sessions[2]
    assert by_id[equal_id]["committed_at"] is None
    assert by_id[momentum_id]["status"] == "committed"
    assert by_id[momentum_id]["committed_at"] == day_row(
        ledger, momentum_id, target
    )["committed_at"]
    assert by_id[late_id]["status"] == "committed"
    assert [
        row[1] for row in ledger_rows(ledger, "days") if row[0] == equal_id
    ] == sessions[:3]

    momentum_before = [
        row for row in ledger_rows(ledger, "days") if row[0] == momentum_id
    ]
    late_before = [
        row for row in ledger_rows(ledger, "days") if row[0] == late_id
    ]
    equal_before = [
        row for row in ledger_rows(ledger, "days") if row[0] == equal_id
    ]

    # The two committed versions remain comparable and their run data is whole
    # even though a third version is blocked on the same target day.
    report = ok(shadow_compare(ledger, feed, (momentum_id, late_id)))
    assert report["comparable"] is True
    assert report["common_interval"] == {
        "start": sessions[0],
        "end": target,
        "sessions": len(sessions),
    }
    momentum_days = stored_results(
        ledger, momentum_id, root / "momentum-report.json"
    )
    expected = expected_metrics(momentum_days, sessions)
    entry = report["per_version"][momentum_id]
    assert entry["sessions"] == len(sessions)
    assert entry["starting_nav"] == pytest.approx(expected["starting_nav"])
    assert entry["ending_nav"] == pytest.approx(expected["ending_nav"])
    assert entry["cumulative_return"] == pytest.approx(expected["cumulative_return"])
    assert entry["fees_paid"] == pytest.approx(expected["fees_paid"])
    assert entry["fees_paid"] > 0
    late_days = stored_results(ledger, late_id, root / "late-report.json")
    late_expected = expected_metrics(late_days, sessions)
    late_entry = report["per_version"][late_id]
    assert late_entry["starting_nav"] == pytest.approx(50_000.0)
    assert late_entry["cumulative_return"] == pytest.approx(
        late_expected["cumulative_return"]
    )
    assert late_entry["fees_paid"] == pytest.approx(late_expected["fees_paid"])

    # Fix the missing close: only the blocked version/day is added.
    fixed_payload = json.loads(json.dumps(feed_payload))
    for row in fixed_payload["raw_bars"]:
        if row["date"] == target and row["symbol"] == DIVIDEND_SYMBOL:
            row["close"] = 9.8
    fixed = write_feed(root / "fixed.json", fixed_payload)
    recovered = ok(shadow_update(ledger, store, fixed, target))
    by_id = {entry["release_id"]: entry for entry in recovered["versions"]}
    assert by_id[equal_id]["status"] == "committed"
    assert by_id[equal_id]["committed_date"] == target
    assert by_id[momentum_id]["status"] == "already_committed"
    assert by_id[late_id]["status"] == "already_committed"
    assert by_id[momentum_id]["committed_at"] == next(
        row[4] for row in momentum_before if row[1] == target
    )
    assert [
        row for row in ledger_rows(ledger, "days") if row[0] == momentum_id
    ] == momentum_before
    assert [
        row for row in ledger_rows(ledger, "days") if row[0] == late_id
    ] == late_before
    assert [
        row for row in ledger_rows(ledger, "days") if row[0] == equal_id
    ][:3] == equal_before
    assert [
        row[1] for row in ledger_rows(ledger, "days") if row[0] == equal_id
    ] == sessions

    # Each order has exactly one terminal state across the week, every pending
    # order is settled on the next committed session, and no order is lost or
    # duplicated.
    terminal = {
        "filled", "missing_open", "missed_deadline",
        "insufficient_cash", "insufficient_shares",
    }
    for version_id in (equal_id, momentum_id, late_id):
        days = stored_results(
            ledger, version_id, root / f"report-{version_id[:8]}.json"
        )
        ordered = sorted(days)
        settled_ids: list[str] = []
        for session in ordered:
            settled = days[session]["orders"]
            assert all(order["status"] in terminal for order in settled)
            settled_ids.extend(order["order_id"] for order in settled)
        assert len(settled_ids) == len(set(settled_ids))
        for earlier, later in zip(ordered, ordered[1:]):
            pending = {order["order_id"] for order in days[earlier]["next_orders"]}
            assert pending <= {order["order_id"] for order in days[later]["orders"]}
        final_pending = {
            order["order_id"] for order in days[ordered[-1]]["next_orders"]
        }
        assert final_pending.isdisjoint(set(settled_ids))


# ---------------------------------------------------------------------------
# 6. Correction impact is shown separately from the strategy return and a
#    correction-caused cash deficit is surfaced in the comparison.
# ---------------------------------------------------------------------------


def test_comparison_separates_correction_impact_from_strategy_return_and_deficits(
    releases: dict, late_release: dict, tmp_path: Path
) -> None:
    equal = releases["equal_weight"]
    inverse = late_release
    equal_id, inverse_id = equal["release_id"], inverse["release_id"]
    sessions = CORRECTION_SESSIONS
    deficit_ex_date = sessions[4]
    deficit_pay_date = sessions[5]
    correction_day = sessions[7]
    root = tmp_path / "corrections-compare"
    store = isolated_store(root, equal, inverse)
    ledger = root / "ledger"
    ok(lifecycle("enable", store, equal_id, sessions[0]))
    ok(lifecycle("enable", store, inverse_id, sessions[0]))

    closes = {symbol: [10.0] * len(sessions) for symbol in SYMBOLS}
    closes[DIVIDEND_SYMBOL] = [10.0, 10.0, 10.0, 10.0, 9.8, 9.8, 9.8, 9.8]
    closes["515080"] = [10.0] * len(sessions)
    closes["515080"][6] = 10.01
    calm = [10.0, 10.01, 9.99, 10.01, 9.99, 10.01, 9.99, 10.01]
    volatile = [10.0, 10.02, 10.01, 10.03, 10.02, 10.04, 10.03, 10.05]
    derived = {symbol: list(calm) for symbol in SYMBOLS}
    derived[MOMENTUM_SYMBOL] = list(volatile)
    feed = write_feed(
        root / "feed.json",
        shadow_feed(
            sessions,
            closes=closes,
            derived=derived,
            actions=[
                {
                    "date": deficit_ex_date,
                    "symbol": DIVIDEND_SYMBOL,
                    "type": "cash_dividend",
                    "cash_per_share": 0.20,
                    "pay_date": deficit_pay_date,
                }
            ],
        ),
    )
    ok(shadow_update(ledger, store, feed, sessions[6]))
    journal = correction_journal(
        tmp_path / "journal",
        ex_date=deficit_ex_date,
        pay_date=deficit_pay_date,
    )
    corrected = shadow_update_with_corrections(
        ledger, store, feed, correction_day, corrections=journal["store"]
    )
    assert corrected.returncode == 0, (corrected.stdout, corrected.stderr)
    by_id = {
        entry["release_id"]: entry
        for entry in json.loads(corrected.stdout)["versions"]
    }
    assert by_id[inverse_id]["status"] == "committed_with_anomalies"
    assert by_id[inverse_id]["cash_deficit"]["amount"] > 0

    equal_days = stored_results(ledger, equal_id, root / "equal-report.json")
    inverse_days = stored_results(ledger, inverse_id, root / "inverse-report.json")
    inverse_correction_day = inverse_days[correction_day]
    assert inverse_correction_day["cash"] < 0
    assert inverse_correction_day["cash_deficit"]["revision_ids"] == [
        journal["revision_id"]
    ]
    assert len(inverse_correction_day["corrections"]) == 1
    assert equal_days[correction_day]["cash_deficit"] is None

    report = ok(shadow_compare(ledger, feed, (equal_id, inverse_id)))
    assert report["comparable"] is True
    assert report["common_interval"] == {
        "start": sessions[0],
        "end": correction_day,
        "sessions": len(sessions),
    }
    equal_expected = expected_metrics(equal_days, sessions)
    inverse_expected = expected_metrics(inverse_days, sessions)
    assert equal_expected["correction_impact"] < 0
    assert inverse_expected["correction_impact"] < 0

    for version_id, days, expected in (
        (equal_id, equal_days, equal_expected),
        (inverse_id, inverse_days, inverse_expected),
    ):
        entry = report["per_version"][version_id]
        assert entry["correction_impact"] == pytest.approx(
            expected["correction_impact"]
        )
        assert entry["strategy_return"] == pytest.approx(
            expected["strategy_return"]
        )
        # The correction is not strategy P&L: the two returns differ by exactly
        # the correction impact normalised at the run's first NAV.
        assert entry["strategy_return"] != pytest.approx(
            entry["cumulative_return"]
        )
        assert (
            entry["strategy_return"] - entry["cumulative_return"]
        ) == pytest.approx(-entry["correction_impact"] / entry["starting_nav"])
        assert [correction["revision_id"] for correction in entry["corrections"]] == [
            journal["revision_id"]
        ]
        expected_unfilled = [
            order
            for session in sessions
            for order in days[session]["orders"]
            if order["status"] != "filled"
        ]
        assert entry["unfilled_order_count"] == len(expected_unfilled)

    equal_entry = report["per_version"][equal_id]
    assert equal_entry["cash_deficits"] == []
    assert equal_entry["cash_deficit_total"] == pytest.approx(0.0)
    inverse_entry = report["per_version"][inverse_id]
    assert len(inverse_entry["cash_deficits"]) == 1
    deficit = inverse_entry["cash_deficits"][0]
    assert deficit["date"] == correction_day
    assert deficit["amount"] == pytest.approx(
        inverse_correction_day["cash_deficit"]["amount"]
    )
    assert deficit["caused_by"] == "dividend_correction"
    assert deficit["financed"] is False
    assert deficit["forced_sale"] is False
    assert inverse_entry["cash_deficit_total"] == pytest.approx(deficit["amount"])
    assert any(
        order["status"] == "insufficient_cash"
        for order in inverse_entry["unfilled_orders"]
    )


# ---------------------------------------------------------------------------
# 7. Integrity and input failures: no fallback, no re-run, stable rejection.
# ---------------------------------------------------------------------------


def test_compare_rejects_tampered_ledgers_and_missing_inputs(
    releases: dict, tmp_path: Path
) -> None:
    equal = releases["equal_weight"]
    momentum = releases["momentum"]
    equal_id, momentum_id = equal["release_id"], momentum["release_id"]
    versions = (equal_id, momentum_id)
    root = tmp_path / "integrity"
    store = isolated_store(root, equal, momentum)
    healthy = root / "healthy"
    sessions = FUTURE[:3]
    feed = write_feed(root / "feed.json", shadow_feed(sessions))
    ok(lifecycle("enable", store, equal_id, sessions[0]))
    ok(lifecycle("enable", store, momentum_id, sessions[0]))
    ok(shadow_update(healthy, store, feed, sessions[1]))

    # A tampered committed result is an explicit integrity failure: no output
    # file, no fallback and no re-run.
    tampered_result = root / "tampered-result"
    shutil.copytree(healthy, tampered_result)
    with sqlite3.connect(tampered_result / "shadow.sqlite") as connection:
        result_json = connection.execute(
            "SELECT result_json FROM days WHERE version_id = ? AND date = ?",
            (equal_id, sessions[1]),
        ).fetchone()[0]
    body = json.loads(result_json)
    body["nav"] = body["nav"] + 1.0
    with sqlite3.connect(tampered_result / "shadow.sqlite") as connection:
        connection.execute(
            "UPDATE days SET result_json = ? WHERE version_id = ? AND date = ?",
            (json.dumps(body), equal_id, sessions[1]),
        )
    output = root / "tampered-report.json"
    response = shadow_compare(tampered_result, feed, versions, output=output)
    assert response.returncode == 2, response.stdout
    payload = json.loads(response.stdout)
    assert payload["status"] == "rejected"
    assert payload["error"]["code"] == "LEDGER_INTEGRITY_FAILED"
    assert not output.exists()

    # A tampered frozen input is detected too (the day is bound to its input).
    tampered_input = root / "tampered-input"
    shutil.copytree(healthy, tampered_input)
    with sqlite3.connect(tampered_input / "shadow.sqlite") as connection:
        input_json = connection.execute(
            "SELECT input_json FROM input_snapshots WHERE version_id = ? AND date = ?",
            (momentum_id, sessions[1]),
        ).fetchone()[0]
    frozen = json.loads(input_json)
    frozen["previous"]["cash"] = frozen["previous"]["cash"] + 1.0
    with sqlite3.connect(tampered_input / "shadow.sqlite") as connection:
        connection.execute(
            "UPDATE input_snapshots SET input_json = ? "
            "WHERE version_id = ? AND date = ?",
            (json.dumps(frozen), momentum_id, sessions[1]),
        )
    response = shadow_compare(tampered_input, feed, versions)
    assert response.returncode == 2
    assert json.loads(response.stdout)["error"]["code"] == "LEDGER_INTEGRITY_FAILED"

    # A committed date missing from the comparison feed calendar is refused
    # rather than interpolated or re-dated.
    outside_feed = write_feed(
        root / "outside.json", shadow_feed([sessions[0], sessions[2]])
    )
    response = shadow_compare(healthy, outside_feed, versions)
    assert response.returncode == 2
    assert json.loads(response.stdout)["error"]["code"] == "FEED_CALENDAR_MISMATCH"

    # Missing ledger, feed and version, plus malformed/single version lists,
    # are stable rejections.
    response = shadow_compare(root / "absent-ledger", feed, versions)
    assert response.returncode == 2
    assert json.loads(response.stdout)["error"]["code"] == "LEDGER_NOT_FOUND"
    response = shadow_compare(healthy, root / "absent-feed.json", versions)
    assert response.returncode == 2
    assert json.loads(response.stdout)["error"]["code"] == "INVALID_FEED"
    response = shadow_compare(healthy, feed, (equal_id, "a" * 64))
    assert response.returncode == 2
    assert json.loads(response.stdout)["error"]["code"] == "VERSION_NOT_FOUND"
    response = shadow_compare(healthy, feed, (equal_id,))
    assert response.returncode == 2
    assert json.loads(response.stdout)["error"]["code"] == "INVALID_VERSIONS"
    response = shadow_compare(healthy, feed, (equal_id, "not-a-release"))
    assert response.returncode == 2
    assert json.loads(response.stdout)["error"]["code"] == "INVALID_RELEASE_ID"
    response = shadow_compare(healthy, feed, (equal_id, equal_id))
    assert response.returncode == 2
    assert json.loads(response.stdout)["error"]["code"] == "INVALID_VERSIONS"

    # The healthy ledger is untouched by every rejected request.
    assert len(ledger_rows(healthy, "days")) == 4


def test_agent_cli_does_not_expose_shadow_compare(
    releases: dict, tmp_path: Path
) -> None:
    equal = releases["equal_weight"]
    momentum = releases["momentum"]
    root = tmp_path / "agent"
    response = cli(
        "shadow", "compare",
        "--ledger", str(root / "ledger"),
        "--data", str(write_feed(root / "feed.json", shadow_feed(FUTURE[:2]))),
        "--versions", f"{equal['release_id']},{momentum['release_id']}",
        agent=True,
    )
    assert response.returncode == 2
    assert "invalid choice" in response.stderr
    assert "shadow" not in response.stdout
