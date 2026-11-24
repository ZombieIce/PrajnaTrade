"""Public-CLI tests for the Evidence Loop: opportunities, scoped pauses and review decisions.

Ticket #46 (spec #43, T3): ``round evidence status`` / ``round evidence decide``
over the single Shadow Live ledger.  Every scenario drives the operator CLI on
synthetic feeds.  The tracking versions are admitted through the T2
``round review approve`` public CLI, so this file never needs the protected
``release approve`` path or the ``isolation`` marker.  Builders are imported from
the modules that own them; nothing is copied here beyond the small feed helpers.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from cli_helpers import agent_cli, cli, ok, rejected, write_json
from test_experiment_cli import sealed_snapshot_input
from test_protected_eval_cli import SYMBOLS, submit_experiment
from test_research_loop_cli import (
    opening,
    round_status,
    submit_version,
    validate_version,
    version_input,
)
from test_shadow_cli import (
    ledger_rows,
    report_days,
    shadow_feed,
    shadow_update,
    write_feed,
)
from test_version_review_cli import (
    approve_version,
    calendar_file,
    observation_input,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


# ---------------------------------------------------------------------------
# Builders: enabled signal dates, sessions and deterministic synthetic feeds
# ---------------------------------------------------------------------------


def local_today() -> date:
    return datetime.now(SHANGHAI).date()


def remove_tree(path: Path) -> None:
    """Delete a trusted store whose immutable records are mode 0444."""
    for root, directories, files in os.walk(path, topdown=False):
        for name in files:
            os.chmod(os.path.join(root, name), 0o600)
        for name in directories:
            os.chmod(os.path.join(root, name), 0o700)
    os.chmod(path, 0o700)
    shutil.rmtree(path)


def session_sessions() -> list[str]:
    """Sessions around the approval day: warmup before enable and a long horizon."""
    today = local_today()
    return [(today + timedelta(days=offset)).isoformat() for offset in range(-3, 25)]


def session_at(sessions: list[str], offset: int) -> str:
    today = local_today()
    wanted = (today + timedelta(days=offset)).isoformat()
    assert wanted in sessions, (wanted, sessions[:2], sessions[-2:])
    return wanted


def feed_for(
    sessions: list[str],
    *,
    direction: str = "rising",
    raw_overrides: dict[tuple[str, str], dict] | None = None,
    closes: dict[str, list[float | None]] | None = None,
    actions: list[dict] | None = None,
) -> dict:
    """A valid synthetic Shadow Live feed whose derived trend drives momentum."""
    step = 0.002 if direction == "rising" else -0.002
    derived = {
        symbol: [10.0 * (1.0 + step * index) for index in range(len(sessions))]
        for symbol in SYMBOLS
    }
    return shadow_feed(
        sessions,
        derived=derived,
        raw_overrides=raw_overrides,
        closes=closes,
        actions=actions,
    )


def crashed_closes(sessions: list[str], crash_offset: int) -> dict[str, list[float | None]]:
    """Flat closes until ``crash_offset``, then a 40% drawdown for every symbol."""
    crash = session_at(sessions, crash_offset)
    return {
        symbol: [
            10.0 if session < crash else 6.0
            for session in sessions
        ]
        for symbol in SYMBOLS
    }


def evidence_status(
    project: Path,
    store: Path,
    ledger: Path,
    round_id: str,
    *,
    version_id: str | None = None,
) -> subprocess.CompletedProcess[str]:
    arguments = [
        "round",
        "evidence",
        "status",
        "--project",
        str(project),
        "--store",
        str(store),
        "--ledger",
        str(ledger),
        "--round-id",
        round_id,
    ]
    if version_id is not None:
        arguments += ["--version-id", version_id]
    return cli(*arguments)


def evidence_decide(
    project: Path,
    store: Path,
    ledger: Path,
    round_id: str,
    version_id: str,
    decision: str,
    *,
    reason: str,
    reviewer: str = "research-lead",
) -> subprocess.CompletedProcess[str]:
    return cli(
        "round",
        "evidence",
        "decide",
        "--project",
        str(project),
        "--store",
        str(store),
        "--ledger",
        str(ledger),
        "--round-id",
        round_id,
        "--version-id",
        version_id,
        "--decision",
        decision,
        "--reviewer",
        reviewer,
        "--reason",
        reason,
    )


def version_entry(status: dict, version_id: str) -> dict:
    return next(
        item for item in status["versions"] if item["version_id"] == version_id
    )


def clone_environment(root: Path, fixture: dict) -> tuple[Path, Path, Path]:
    """Copy the approved project and trusted store so a test may decide/pause."""
    project = root / "project"
    store = root / "trusted"
    shutil.copytree(fixture["project"], project)
    shutil.copytree(fixture["store"], store)
    ledger = root / "ledger"
    return project, store, ledger


def suspended_opens(sessions: list[str], first_offset: int) -> dict[tuple[str, str], dict]:
    """Make every open non-executable from ``first_offset`` onward."""
    suspended = {session_at(sessions, offset) for offset in range(first_offset, 25)}
    return {
        (session, symbol): {"trading_status": "unknown"}
        for session in sessions
        if session in suspended
        for symbol in SYMBOLS
    }


# ---------------------------------------------------------------------------
# One approved project with V1 (always invested) and V2 (falling, stays cash)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tracking(tmp_path_factory: pytest.TempPathFactory) -> dict:
    root = tmp_path_factory.mktemp("evidence-loop")
    sessions = session_sessions()
    project, snapshots, campaign, store, opened = opening(
        root, sealed_snapshot_input(15), warmup=4, name="project"
    )
    round_id = opened["round_id"]

    first = submit_experiment(
        project,
        campaign["campaign_id"],
        root / "experiment-one.json",
        "first",
        "A fully invested synthetic momentum version.",
        "momentum",
        {"lookback": 2, "top_k": 5, "cash_if_negative": False},
    )
    v1 = ok(
        submit_version(
            project,
            round_id,
            "version-one",
            write_json(
                root / "version-one.json",
                version_input(
                    first["experiment_id"],
                    change="Hold the five synthetic symbols at equal weight when momentum is negative.",
                ),
            ),
        )
    )
    receipt1 = ok(validate_version(project, store, snapshots, round_id, v1["version_id"]))
    assert receipt1["outcome"] == "passed", receipt1
    calendar1, _ = calendar_file(root, name="calendar-one.json", start_offset=1, count=12)
    approved1 = ok(
        approve_version(
            project,
            store,
            round_id,
            v1["version_id"],
            calendar1,
            write_json(
                root / "observation-one.json",
                observation_input(sessions=2, reason="A fast synthetic rebalance cadence needs a short term."),
            ),
        )
    )

    second = submit_experiment(
        project,
        campaign["campaign_id"],
        root / "experiment-two.json",
        "second",
        "A falling synthetic momentum version that stays in cash.",
        "momentum",
        {"lookback": 3, "cash_if_negative": True},
    )
    v2 = ok(
        submit_version(
            project,
            round_id,
            "version-two",
            write_json(
                root / "version-two.json",
                version_input(
                    second["experiment_id"],
                    change="Stay in cash while the synthetic trend is negative.",
                ),
            ),
        )
    )
    receipt2 = ok(validate_version(project, store, snapshots, round_id, v2["version_id"]))
    assert receipt2["outcome"] == "passed", receipt2
    calendar2, _ = calendar_file(root, name="calendar-two.json", start_offset=3, count=12)
    approved2 = ok(
        approve_version(
            project,
            store,
            round_id,
            v2["version_id"],
            calendar2,
            write_json(
                root / "observation-two.json",
                observation_input(sessions=20, reason="A slow synthetic cadence needs a long observation term."),
            ),
        )
    )

    # T3 never needs the sealed snapshot store again; deleting it here proves the
    # Evidence Loop reads only the project, release store and Shadow ledger.
    remove_tree(snapshots)
    return {
        "project": project,
        "store": store,
        "round_id": round_id,
        "sessions": sessions,
        "snapshots": snapshots,
        "v1": {**v1, "receipt": receipt1, "approval": approved1},
        "v2": {**v2, "receipt": receipt2, "approval": approved2},
    }


# ---------------------------------------------------------------------------
# 1. Observation recommendation/confirmation: too few opportunities keeps tracking
# ---------------------------------------------------------------------------


def test_observation_term_with_too_few_opportunities_extends_tracking(
    tracking: dict, tmp_path: Path
) -> None:
    round_id = tracking["round_id"]
    sessions = tracking["sessions"]
    release_id = tracking["v2"]["approval"]["tracking"]["release_id"]
    enabled = tracking["v2"]["approval"]["tracking"]["enabled_signal_date"]
    assert enabled == session_at(sessions, 3)

    # The researcher's confirmed term came from the platform recommendation.
    status = ok(round_status(tracking["project"], round_id))
    submission = next(
        item for item in status["submissions"] if item["version_id"] == tracking["v2"]["version_id"]
    )
    assert submission["observation"]["confirmed"]["observation_sessions"] == 20
    assert submission["observation"]["recommended"]["recommended_sessions"] > 0
    assert submission["observation"]["confirmed"]["minimum_opportunities"] == 2
    assert submission["observation"]["confirmed"]["recommended_sessions"] == (
        submission["observation"]["recommended"]["recommended_sessions"]
    )

    ledger = tmp_path / "ledger"
    feed = write_feed(tmp_path / "feed.json", feed_for(sessions, direction="falling"))
    updated = ok(
        shadow_update(
            ledger,
            tracking["store"],
            feed,
            session_at(sessions, 22),
            release_ids=(release_id,),
        )
    )
    assert updated["versions"][0]["status"] == "committed"

    result = ok(
        evidence_status(
            tracking["project"], tracking["store"], ledger, round_id, version_id=tracking["v2"]["version_id"]
        )
    )
    entry = version_entry(result, tracking["v2"]["version_id"])
    assert entry["review_state"] == "approved"
    assert entry["tracking"]["release_id"] == release_id
    assert entry["tracking"]["enabled_signal_date"] == enabled
    assert entry["tracking"]["committed_sessions"] == 20
    assert entry["tracking"]["first_committed_date"] == enabled
    assert entry["tracking"]["first_signal_date"] == enabled
    assert entry["tracking"]["first_fill_date"] is None
    assert entry["tracking"]["due_opportunities"] == 0
    assert entry["tracking"]["filled_opportunities"] == 0
    assert entry["tracking"]["unfilled_opportunities"] == 0
    assert entry["tracking"]["intent_without_fill"] == 0
    assert entry["tracking"]["metrics_not_ready"]["count"] == 0
    observation = entry["tracking"]["observation"]
    assert observation["confirmed_sessions"] == 20
    assert observation["minimum_opportunities"] == 2
    assert observation["remaining_sessions"] == 0
    assert observation["status"] == "observation_extended"
    assert entry["state"] == "observation_extended"
    assert entry["review"]["trigger"] == "observation_extended"
    assert "opportunit" in entry["review"]["reason"]
    assert entry["pause"] is None
    # A no-order decision point never fabricates an opportunity.
    assert ledger_rows(ledger, "days") != []


# ---------------------------------------------------------------------------
# 2. Opportunities: one per committed decision point with orders; outcomes join
# ---------------------------------------------------------------------------


def test_opportunities_join_execution_fills_and_ignore_pre_approval_history(
    tracking: dict, tmp_path: Path
) -> None:
    round_id = tracking["round_id"]
    sessions = tracking["sessions"]
    release_id = tracking["v1"]["approval"]["tracking"]["release_id"]
    enabled = tracking["v1"]["approval"]["tracking"]["enabled_signal_date"]
    assert enabled == session_at(sessions, 1)

    ledger = tmp_path / "ledger"
    feed = write_feed(tmp_path / "feed.json", feed_for(sessions, direction="rising"))
    ok(
        shadow_update(
            ledger,
            tracking["store"],
            feed,
            session_at(sessions, 3),
            release_ids=(release_id,),
        )
    )

    status = ok(
        evidence_status(tracking["project"], tracking["store"], ledger, round_id)
    )
    entry = version_entry(status, tracking["v1"]["version_id"])
    facts = entry["tracking"]
    assert facts["enabled_signal_date"] == enabled
    assert facts["committed_sessions"] == 3
    assert facts["first_committed_date"] == enabled
    assert facts["first_signal_date"] == enabled
    assert facts["first_fill_date"] == session_at(sessions, 2)
    assert facts["due_opportunities"] == 1
    assert facts["filled_opportunities"] == 1
    assert facts["unfilled_opportunities"] == 0
    assert facts["intent_without_fill"] == 0
    # Every opportunity belongs to a committed decision point at/after enable.
    assert all(
        opportunity["signal_date"] >= enabled
        for opportunity in facts["opportunities"]
    )
    assert [item["signal_date"] for item in facts["opportunities"]] == [
        session_at(sessions, 1)
    ]
    assert facts["opportunities"][0]["due"] is True
    assert len(facts["opportunities"][0]["filled"]) == len(SYMBOLS)
    assert facts["opportunities"][0]["unfilled"] == []
    assert facts["opportunities"][0]["intent_without_fill"] is False

    days = report_days(ledger, release_id, tmp_path / "report.json")
    by_date = {entry["date"]: entry["result"] for entry in days}
    assert sorted(by_date) == [session_at(sessions, offset) for offset in (1, 2, 3)]
    first = by_date[session_at(sessions, 1)]
    assert first["orders"] == [] and first["fills"] == []
    assert first["nav"] == pytest.approx(first["execution"]["initial_cash"])
    assert first["opportunities"] == [
        {
            "signal_date": session_at(sessions, 1),
            "execution_date": session_at(sessions, 2),
            "order_ids": [order["order_id"] for order in first["next_orders"]],
            "due": True,
        }
    ]
    assert by_date[session_at(sessions, 2)]["opportunities"] == []
    assert by_date[session_at(sessions, 2)]["signal"]["opportunity"] == {
        "due": False,
        "reason": "no_target_change",
    }
    summary = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))["summary"]
    assert summary["due_opportunities"] == 1
    assert summary["filled_opportunities"] == 1
    assert summary["unfilled_opportunities"] == 0
    assert summary["intent_without_fill"] == 0
    assert summary["days"] == 3
    assert summary["fills"] == len(SYMBOLS)
    for preserved in (
        "first_committed_date",
        "last_committed_date",
        "ending_nav",
        "fees_total",
        "unfilled_orders",
        "anomalies",
        "corrections",
    ):
        assert preserved in summary


# ---------------------------------------------------------------------------
# 3/4. Intent without fill triggers review; metrics-not-ready stays distinct
# ---------------------------------------------------------------------------


def missing_opens(
    sessions: list[str], first_offset: int,
) -> dict[tuple[str, str], dict]:
    """Make every open a gap from ``first_offset`` on, keeping closes valid."""
    from_offset = {session_at(sessions, offset) for offset in range(first_offset, 25)}
    return {
        (session, symbol): {"open": None}
        for session in sessions
        if session in from_offset
        for symbol in SYMBOLS
    }


def test_metrics_not_ready_is_distinct_from_consecutive_intent_without_fill(
    tracking: dict, tmp_path: Path
) -> None:
    round_id = tracking["round_id"]
    sessions = tracking["sessions"]

    # (a) A ready signal whose orders never fill: consecutive intent-without-fill.
    release_id = tracking["v1"]["approval"]["tracking"]["release_id"]
    ledger = tmp_path / "unfilled-ledger"
    feed = write_feed(
        tmp_path / "unfilled-feed.json",
        feed_for(sessions, direction="rising", raw_overrides=missing_opens(sessions, 2)),
    )
    ok(
        shadow_update(
            ledger,
            tracking["store"],
            feed,
            session_at(sessions, 4),
            release_ids=(release_id,),
        )
    )
    status = ok(evidence_status(tracking["project"], tracking["store"], ledger, round_id))
    entry = version_entry(status, tracking["v1"]["version_id"])
    facts = entry["tracking"]
    assert facts["metrics_not_ready"]["count"] == 0
    assert facts["due_opportunities"] == 4
    assert facts["filled_opportunities"] == 0
    assert facts["unfilled_opportunities"] == 3
    assert facts["intent_without_fill"] == 3
    assert facts["consecutive_intent_without_fill"] == 3
    assert facts["first_fill_date"] is None
    assert entry["state"] == "review_due"
    assert entry["review"]["trigger"] == "max_unfilled_opportunities"
    assert "limit of 3" in entry["review"]["reason"]

    # (b) Too little trailing history: no signal, no order and no opportunity.
    release_id = tracking["v2"]["approval"]["tracking"]["release_id"]
    enabled = tracking["v2"]["approval"]["tracking"]["enabled_signal_date"]
    assert enabled == session_at(sessions, 3)
    short_sessions = [
        session_at(sessions, offset) for offset in range(3, 9)
    ]
    short_ledger = tmp_path / "short-ledger"
    short_feed = write_feed(
        tmp_path / "short-feed.json",
        feed_for(short_sessions, direction="falling"),
    )
    ok(
        shadow_update(
            short_ledger,
            tracking["store"],
            short_feed,
            session_at(sessions, 6),
            release_ids=(release_id,),
        )
    )
    short_status = ok(
        evidence_status(
            tracking["project"],
            tracking["store"],
            short_ledger,
            round_id,
            version_id=tracking["v2"]["version_id"],
        )
    )
    # The filter returns only the requested version.
    assert [item["version_id"] for item in short_status["versions"]] == [
        tracking["v2"]["version_id"]
    ]
    short = version_entry(short_status, tracking["v2"]["version_id"])
    short_facts = short["tracking"]
    assert short_facts["metrics_not_ready"]["count"] == 3
    assert short_facts["metrics_not_ready"]["sessions"] == [
        {
            "date": session_at(sessions, 3),
            "required_sessions": 4,
            "available_sessions": 1,
        },
        {
            "date": session_at(sessions, 4),
            "required_sessions": 4,
            "available_sessions": 2,
        },
        {
            "date": session_at(sessions, 5),
            "required_sessions": 4,
            "available_sessions": 3,
        },
    ]
    assert short_facts["due_opportunities"] == 0
    assert short_facts["filled_opportunities"] == 0
    assert short_facts["unfilled_opportunities"] == 0
    assert short_facts["intent_without_fill"] == 0
    assert short_facts["first_signal_date"] == session_at(sessions, 6)
    assert short["state"] == "tracking"
    assert short["review"]["trigger"] is None


# ---------------------------------------------------------------------------
# 5. A scoped pause stops only the affected version; the other keeps tracking
# ---------------------------------------------------------------------------


def late_dividend_action(
    sessions: list[str], offset: int, symbol: str,
) -> list[dict]:
    session = session_at(sessions, offset)
    return [
        {
            "date": session,
            "symbol": symbol,
            "type": "cash_dividend",
            "cash_per_share": 0.1,
            "pay_date": session_at(sessions, offset + 2),
            "available_at": f"{session}T16:00:00+08:00",
            "terms_evidence": "synthetic dividend terms",
        }
    ]


def test_scoped_pause_stops_only_the_affected_version(tracking: dict, tmp_path: Path) -> None:
    round_id = tracking["round_id"]
    sessions = tracking["sessions"]
    v1_id = tracking["v1"]["version_id"]
    v2_id = tracking["v2"]["version_id"]
    v1_release = tracking["v1"]["approval"]["tracking"]["release_id"]
    v2_release = tracking["v2"]["approval"]["tracking"]["release_id"]
    assert tracking["v1"]["approval"]["tracking"]["enabled_signal_date"] == session_at(sessions, 1)
    assert tracking["v2"]["approval"]["tracking"]["enabled_signal_date"] == session_at(sessions, 3)

    # A falling trend keeps V2 in cash while V1 (cash_if_negative=False) invests.
    ledger = tmp_path / "ledger"
    distorted = write_feed(
        tmp_path / "distorted-feed.json",
        feed_for(
            sessions,
            direction="falling",
            actions=late_dividend_action(sessions, 3, SYMBOLS[0]),
        ),
    )
    blocked = shadow_update(
        ledger,
        tracking["store"],
        distorted,
        session_at(sessions, 4),
        release_ids=(v1_release, v2_release),
    )
    assert blocked.returncode == 2, (blocked.stdout, blocked.stderr)
    blocked_payload = json.loads(blocked.stdout)
    entries = {item["release_id"]: item for item in blocked_payload["versions"]}
    assert entries[v1_release]["status"] == "blocked"
    assert entries[v1_release]["reason_code"] == "missing_action_terms"
    assert entries[v2_release]["status"] == "committed"

    status = ok(evidence_status(tracking["project"], tracking["store"], ledger, round_id))
    paused = version_entry(status, v1_id)
    continuing = version_entry(status, v2_id)
    assert paused["state"] == "paused"
    assert paused["pause"]["scope"] == "feed"
    assert paused["pause"]["code"] == "missing_action_terms"
    assert paused["pause"]["session"] == session_at(sessions, 3)
    assert paused["tracking"]["last_committed_date"] == session_at(sessions, 2)
    assert continuing["state"] == "tracking"
    assert continuing["pause"] is None
    assert continuing["tracking"]["last_committed_date"] == session_at(sessions, 4)
    assert continuing["tracking"]["committed_sessions"] == 2
    rows = ledger_rows(ledger, "scope_pauses")
    assert len(rows) == 1 and rows[0][0] == v1_release

    # Repairing the feed commits the formerly blocked session and clears the pause
    # for that version only.
    repaired = write_feed(
        tmp_path / "repaired-feed.json",
        feed_for(sessions, direction="falling"),
    )
    ok(
        shadow_update(
            ledger,
            tracking["store"],
            repaired,
            session_at(sessions, 4),
            release_ids=(v1_release, v2_release),
        )
    )
    recovered = ok(evidence_status(tracking["project"], tracking["store"], ledger, round_id))
    former = version_entry(recovered, v1_id)
    other = version_entry(recovered, v2_id)
    assert former["pause"] is None
    assert former["tracking"]["last_committed_date"] == session_at(sessions, 4)
    assert former["tracking"]["data_gap_sessions"] == 1
    assert former["state"] != "paused"
    assert other["state"] == "tracking"
    assert other["tracking"]["last_committed_date"] == session_at(sessions, 4)
    # The scoped pause is kept as auditable history, not rewritten away.
    history = ledger_rows(ledger, "scope_pauses")
    assert len(history) == 1
    assert history[0][1] == session_at(sessions, 3)
    assert history[0][2] == "feed"


# ---------------------------------------------------------------------------
# 6. Trusted performance anomaly triggers human review; continue keeps tracking
# ---------------------------------------------------------------------------


def test_trusted_drawdown_requires_human_review_and_continue_keeps_tracking(
    tracking: dict, tmp_path: Path
) -> None:
    project, store, ledger = clone_environment(tmp_path / "clone", tracking)
    round_id = tracking["round_id"]
    sessions = tracking["sessions"]
    release_id = tracking["v1"]["approval"]["tracking"]["release_id"]
    v1_id = tracking["v1"]["version_id"]
    feed = write_feed(
        tmp_path / "feed.json",
        feed_for(sessions, direction="rising", closes=crashed_closes(sessions, 3)),
    )
    ok(
        shadow_update(
            ledger, store, feed, session_at(sessions, 3), release_ids=(release_id,)
        )
    )
    status = ok(evidence_status(project, store, ledger, round_id))
    entry = version_entry(status, v1_id)
    assert entry["state"] == "review_due"
    assert entry["review"]["trigger"] == "max_abs_drawdown"
    assert "drawdown" in entry["review"]["reason"]
    assert entry["tracking"]["max_abs_drawdown"] >= 0.25
    assert entry["tracking"]["metrics_not_ready"]["count"] == 0
    assert entry["pause"] is None

    decided = ok(
        evidence_decide(
            project,
            store,
            ledger,
            round_id,
            v1_id,
            "continue",
            reason="The reviewer accepts the synthetic drawdown and continues the term.",
        )
    )
    assert decided["decision"] == "continue"
    assert decided["effect"]["action"] == "none"
    assert decided["effect"]["state_before"] == "enabled"
    assert decided["facts"]["max_abs_drawdown"] >= 0.25
    _assert_hex(decided["decision_id"])

    # Continue keeps the version tracking; a later session still commits.
    ok(
        shadow_update(
            ledger, store, feed, session_at(sessions, 4), release_ids=(release_id,)
        )
    )
    after = version_entry(
        ok(evidence_status(project, store, ledger, round_id)), v1_id
    )
    assert after["tracking"]["committed_sessions"] == 4
    assert after["state"] == "review_due"
    assert after["review"]["trigger"] == "max_abs_drawdown"


# ---------------------------------------------------------------------------
# 7. continue / pause / retire: pause stops signals, pending orders and
#    valuation continue, continue resumes, retire is terminal and keeps history
# ---------------------------------------------------------------------------


def _assert_hex(value: object) -> None:
    assert isinstance(value, str) and len(value) == 64, value


def test_continue_pause_and_retire_outcomes_keep_the_old_facts(
    tracking: dict, tmp_path: Path
) -> None:
    project, store, ledger = clone_environment(tmp_path / "clone", tracking)
    round_id = tracking["round_id"]
    sessions = tracking["sessions"]
    v1_id = tracking["v1"]["version_id"]
    v2_id = tracking["v2"]["version_id"]
    v1_release = tracking["v1"]["approval"]["tracking"]["release_id"]
    v2_release = tracking["v2"]["approval"]["tracking"]["release_id"]
    feed = write_feed(tmp_path / "feed.json", feed_for(sessions, direction="rising"))
    ok(
        shadow_update(
            ledger,
            store,
            feed,
            session_at(sessions, 3),
            release_ids=(v1_release, v2_release),
        )
    )
    status = ok(evidence_status(project, store, ledger, round_id))
    assert version_entry(status, v2_id)["state"] == "tracking"
    assert version_entry(status, v2_id)["pause"] is None

    continued = ok(
        evidence_decide(
            project, store, ledger, round_id, v2_id, "continue",
            reason="The first synthetic session is unremarkable; continue tracking.",
        )
    )
    assert continued["effect"]["action"] == "none"
    replay = ok(
        evidence_decide(
            project, store, ledger, round_id, v2_id, "continue",
            reason="The first synthetic session is unremarkable; continue tracking.",
        )
    )
    assert replay["reused"] is True
    assert replay["decision_id"] == continued["decision_id"]

    paused = ok(
        evidence_decide(
            project, store, ledger, round_id, v2_id, "pause",
            reason="Pause only V2's new signals while the synthetic data is reviewed.",
        )
    )
    assert paused["decision"] == "pause"
    assert paused["effect"]["action"] == "pause"
    assert paused["effect"]["state_after"] == "paused"
    assert paused["effect"]["effective_signal_date"] == session_at(sessions, 4)
    paused_status = ok(evidence_status(project, store, ledger, round_id))
    paused_entry = version_entry(paused_status, v2_id)
    assert paused_entry["state"] == "paused"
    assert paused_entry["pause"]["code"] == "MANUAL_PAUSE"
    assert paused_entry["pause"]["session"] == session_at(sessions, 4)
    assert version_entry(paused_status, v1_id)["state"] != "paused"

    # Pending orders settle and NAV is valued on the paused day; no new signal.
    ok(
        shadow_update(
            ledger,
            store,
            feed,
            session_at(sessions, 5),
            release_ids=(v1_release, v2_release),
        )
    )
    paused_days = {
        entry["date"]: entry["result"]
        for entry in report_days(ledger, v2_release, tmp_path / "v2-report.json")
    }
    pending_day = paused_days[session_at(sessions, 4)]
    assert pending_day["release_state_at_signal_point"] == "paused"
    assert pending_day["signal"] is None and pending_day["next_orders"] == []
    assert len(pending_day["orders"]) == 1
    assert pending_day["orders"][0]["status"] == "filled"
    assert pending_day["nav"] > 0
    mid = version_entry(
        ok(evidence_status(project, store, ledger, round_id)), v2_id
    )
    assert mid["state"] == "paused"
    assert mid["tracking"]["committed_sessions"] == 3

    resumed = ok(
        evidence_decide(
            project, store, ledger, round_id, v2_id, "continue",
            reason="The review is complete; resume V2's new signals.",
        )
    )
    assert resumed["effect"]["action"] == "resume"
    assert resumed["effect"]["state_after"] == "enabled"
    ok(
        shadow_update(
            ledger,
            store,
            feed,
            session_at(sessions, 7),
            release_ids=(v1_release, v2_release),
        )
    )
    resumed_status = ok(evidence_status(project, store, ledger, round_id))
    resumed_entry = version_entry(resumed_status, v2_id)
    assert resumed_entry["state"] == "tracking"
    assert resumed_entry["pause"] is None
    assert resumed_entry["tracking"]["last_committed_date"] == session_at(sessions, 7)
    resumed_days = report_days(ledger, v2_release, tmp_path / "v2-resumed.json")
    assert resumed_days[-1]["result"]["signal"] is not None
    before_retire = [
        (entry["date"], entry["result_hash"]) for entry in resumed_days
    ]

    retired = ok(
        evidence_decide(
            project, store, ledger, round_id, v2_id, "retire",
            reason="Retire V2; keep its artifact and every committed day.",
        )
    )
    assert retired["decision"] == "retire"
    assert retired["effect"]["action"] == "retire"
    assert retired["effect"]["state_after"] == "retired"
    _assert_hex(retired["decision_id"])
    replay = ok(
        evidence_decide(
            project, store, ledger, round_id, v2_id, "retire",
            reason="Retire V2; keep its artifact and every committed day.",
        )
    )
    assert replay["reused"] is True
    assert replay["decision_id"] == retired["decision_id"]
    lifecycle = release_status(store, v2_release)["timeline"]
    assert [event["action"] for event in lifecycle] == [
        "enable",
        "pause",
        "resume",
        "retire",
    ]

    ended = ok(
        shadow_update(
            ledger,
            store,
            feed,
            session_at(sessions, 9),
            release_ids=(v1_release, v2_release),
        )
    )
    ended_entries = {item["release_id"]: item for item in ended["versions"]}
    assert ended_entries[v2_release]["status"] == "ended"
    assert ended_entries[v2_release]["committed_date"] == session_at(sessions, 7)
    assert ended_entries[v1_release]["status"] in {"committed", "committed_with_anomalies"}
    after_retire = report_days(ledger, v2_release, tmp_path / "v2-frozen.json")
    assert [(entry["date"], entry["result_hash"]) for entry in after_retire] == before_retire
    final = version_entry(
        ok(evidence_status(project, store, ledger, round_id)), v2_id
    )
    assert final["state"] == "ended"
    assert final["review_state"] == "approved"
    assert final["tracking"]["committed_sessions"] == 5
    assert final["tracking"]["last_committed_date"] == session_at(sessions, 7)
    rejected(
        evidence_decide(
            project, store, ledger, round_id, v2_id, "pause",
            reason="A retired version cannot be paused again.",
        ),
        "VERSION_ENDED",
    )


# ---------------------------------------------------------------------------
# 8. Retiring an actively signalling version keeps history and stops signals
# ---------------------------------------------------------------------------


def test_retire_stops_new_signals_and_keeps_committed_history(
    tracking: dict, tmp_path: Path
) -> None:
    project, store, ledger = clone_environment(tmp_path / "clone", tracking)
    round_id = tracking["round_id"]
    sessions = tracking["sessions"]
    v1_id = tracking["v1"]["version_id"]
    release_id = tracking["v1"]["approval"]["tracking"]["release_id"]
    feed = write_feed(tmp_path / "feed.json", feed_for(sessions, direction="rising"))
    ok(
        shadow_update(
            ledger, store, feed, session_at(sessions, 3), release_ids=(release_id,)
        )
    )
    before = report_days(ledger, release_id, tmp_path / "before.json")
    assert before[-1]["result"]["signal"] is not None
    before_hashes = [(entry["date"], entry["result_hash"]) for entry in before]
    artifact_before = ok(
        cli("release", "fetch", "--store", str(store), "--release-id", release_id)
    )["artifact"]

    ok(
        evidence_decide(
            project, store, ledger, round_id, v1_id, "retire",
            reason="The version is retired; the direction may start a new version.",
        )
    )
    artifact_after = ok(
        cli("release", "fetch", "--store", str(store), "--release-id", release_id)
    )["artifact"]
    assert artifact_after == artifact_before
    ended = ok(
        shadow_update(
            ledger, store, feed, session_at(sessions, 6), release_ids=(release_id,)
        )
    )
    assert ended["versions"][0]["status"] == "ended"
    assert ended["versions"][0]["reason_code"] == "VERSION_ENDED"
    after = report_days(ledger, release_id, tmp_path / "after.json")
    assert [(entry["date"], entry["result_hash"]) for entry in after] == before_hashes
    final = version_entry(
        ok(evidence_status(project, store, ledger, round_id)), v1_id
    )
    assert final["state"] == "ended"
    assert final["tracking"]["committed_sessions"] == 3
    assert final["tracking"]["due_opportunities"] == 1


# ---------------------------------------------------------------------------
# 9. Two independent versions: parallel books, common interval, full histories
# ---------------------------------------------------------------------------


def shadow_compare(
    ledger: Path,
    feed: Path,
    versions: tuple[str, ...],
    output: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    arguments = [
        "shadow",
        "compare",
        "--ledger",
        str(ledger),
        "--data",
        str(feed),
        "--versions",
        ",".join(versions),
    ]
    if output is not None:
        arguments += ["--output", str(output)]
    return cli(*arguments)


def release_status(store: Path, release_id: str) -> dict:
    return ok(
        cli("release", "status", "--store", str(store), "--release-id", release_id)
    )


def test_parallel_versions_keep_full_history_and_name_the_common_interval(
    tracking: dict, tmp_path: Path
) -> None:
    round_id = tracking["round_id"]
    sessions = tracking["sessions"]
    v1_release = tracking["v1"]["approval"]["tracking"]["release_id"]
    v2_release = tracking["v2"]["approval"]["tracking"]["release_id"]
    ledger = tmp_path / "ledger"
    feed = write_feed(tmp_path / "feed.json", feed_for(sessions, direction="rising"))
    updated = ok(
        shadow_update(
            ledger,
            tracking["store"],
            feed,
            session_at(sessions, 8),
            release_ids=(v1_release, v2_release),
        )
    )
    entries = {item["release_id"]: item for item in updated["versions"]}
    assert entries[v1_release]["committed_date"] == session_at(sessions, 8)
    assert entries[v2_release]["committed_date"] == session_at(sessions, 8)

    status = ok(
        evidence_status(tracking["project"], tracking["store"], ledger, round_id)
    )
    v1 = version_entry(status, tracking["v1"]["version_id"])
    v2 = version_entry(status, tracking["v2"]["version_id"])
    assert v1["tracking"]["enabled_signal_date"] == session_at(sessions, 1)
    assert v2["tracking"]["enabled_signal_date"] == session_at(sessions, 3)
    assert v1["tracking"]["committed_sessions"] == 8
    assert v2["tracking"]["committed_sessions"] == 6

    output = tmp_path / "comparison.json"
    report = ok(shadow_compare(ledger, feed, (v1_release, v2_release), output))
    assert output.is_file()
    assert report["comparable"] is True
    assert report["reason"] is None
    assert report["versions"] == [v1_release, v2_release]
    assert report["common_interval"] == {
        "start": session_at(sessions, 3),
        "end": session_at(sessions, 8),
        "sessions": 6,
    }
    for release_id, first in ((v1_release, 1), (v2_release, 3)):
        entry = report["per_version"][release_id]
        assert entry["included"] is True
        assert entry["date_range"] == {
            "start": session_at(sessions, 3),
            "end": session_at(sessions, 8),
        }
        full = entry["full_history"]
        assert full["first_committed_date"] == session_at(sessions, first)
        assert full["last_committed_date"] == session_at(sessions, 8)
        assert full["sessions"] == 9 - first
        assert full["ending_nav"] > 0
        assert "due_opportunities" in full
        assert entry["committed_coverage"]["sessions"] == 9 - first

    # An identical rerun is idempotent: already-committed entries, byte-identical
    # comparison document, and every requested version retained.
    again = ok(
        shadow_update(
            ledger,
            tracking["store"],
            feed,
            session_at(sessions, 8),
            release_ids=(v1_release, v2_release),
        )
    )
    assert {item["status"] for item in again["versions"]} == {"already_committed"}
    assert {item["result_hash"] for item in again["versions"]} == {
        item["result_hash"] for item in updated["versions"]
    }
    second = tmp_path / "comparison-again.json"
    repeated = ok(shadow_compare(ledger, feed, (v1_release, v2_release), second))
    assert repeated == report
    assert second.read_bytes() == output.read_bytes()

    # Both versions keep their own full history even when the common run is
    # shorter; a request that shares no run stays incomparable and names why.
    split = tmp_path / "split-ledger"
    ok(
        shadow_update(
            split, tracking["store"], feed, session_at(sessions, 2),
            release_ids=(v1_release,),
        )
    )
    ok(
        shadow_update(
            split, tracking["store"], feed, session_at(sessions, 8),
            release_ids=(v2_release,),
        )
    )
    incomparable = ok(shadow_compare(split, feed, (v1_release, v2_release)))
    assert incomparable["comparable"] is False
    assert incomparable["reason"] == "INSUFFICIENT_COMMON_SESSIONS"
    assert incomparable["comparison"] == {
        "comparable": False,
        "reason": "INSUFFICIENT_COMMON_SESSIONS",
    }
    assert incomparable["common_interval"] == {
        "start": None,
        "end": None,
        "sessions": 0,
    }
    assert set(incomparable["per_version"]) == {v1_release, v2_release}
    for release_id in (v1_release, v2_release):
        entry = incomparable["per_version"][release_id]
        assert entry["included"] is False
        assert entry["reason"] == "INSUFFICIENT_COMMON_SESSIONS"
        assert entry["full_history"]["sessions"] > 0
    assert incomparable["per_version"][v1_release]["full_history"]["sessions"] == 2
    assert incomparable["per_version"][v2_release]["full_history"]["sessions"] == 6


# ---------------------------------------------------------------------------
# 10. The whole Evidence Loop works without the sealed/protected snapshot store
# ---------------------------------------------------------------------------


def test_evidence_loop_never_reads_a_protected_partition(tmp_path: Path, tracking: dict) -> None:
    assert not tracking["snapshots"].exists(), "the sealed snapshot store was removed"
    project, store, ledger = clone_environment(tmp_path / "clone", tracking)
    round_id = tracking["round_id"]
    sessions = tracking["sessions"]
    v1_id = tracking["v1"]["version_id"]
    v1_release = tracking["v1"]["approval"]["tracking"]["release_id"]

    # No committed day yet: report the enabled date and zero counts, never a
    # fabricated opportunity, and never touch the removed snapshot store.
    empty = ok(evidence_status(project, store, ledger, round_id))
    entry = version_entry(empty, v1_id)
    assert entry["tracking"]["enabled_signal_date"] == session_at(sessions, 1)
    assert entry["tracking"]["committed_sessions"] == 0
    assert entry["tracking"]["due_opportunities"] == 0
    assert entry["tracking"]["first_signal_date"] is None
    assert entry["state"] == "tracking"
    rendered = json.dumps(empty)
    for forbidden in (
        "snapshot_store",
        "artifact_path",
        "release_path",
        "opened_protected_source",
        "query_debited",
        "equity_curve",
        "evaluations",
    ):
        assert forbidden not in rendered

    # The evidence loop stays operator-only: the agent CLI gains no ledger,
    # review or pause visibility (spec #43 agent boundary).
    agent = agent_cli(
        "round", "evidence", "status", "--project", str(project),
        "--store", str(store), "--ledger", str(ledger), "--round-id", round_id,
    )
    assert agent.returncode != 0
    assert "invalid choice" in agent.stderr

    # The tracking artifact itself states that it is not a protected evaluation.
    fetched = ok(
        cli("release", "fetch", "--store", str(store), "--release-id", v1_release)
    )
    admission = fetched["artifact"]["admission"]
    assert admission["protected_evaluation"] is False
    assert admission["conditional_release"] is False
    assert fetched["artifact"]["workflow"] == "manual_tracking"

    feed = write_feed(tmp_path / "feed.json", feed_for(sessions, direction="rising"))
    ok(
        shadow_update(
            ledger, store, feed, session_at(sessions, 2), release_ids=(v1_release,)
        )
    )
    decided = ok(
        evidence_decide(
            project, store, ledger, round_id, v1_id, "continue",
            reason="Continue without reading any protected partition.",
        )
    )
    assert decided["reused"] is False
    final = version_entry(
        ok(evidence_status(project, store, ledger, round_id)), v1_id
    )
    assert final["tracking"]["committed_sessions"] == 2
    assert final["tracking"]["due_opportunities"] == 1


# ---------------------------------------------------------------------------
# 11. Data-gap sessions accumulate into the preset invalidation condition
# ---------------------------------------------------------------------------


def test_data_gap_sessions_reach_the_preset_invalidation_condition(
    tracking: dict, tmp_path: Path
) -> None:
    project, store, ledger = clone_environment(tmp_path / "clone", tracking)
    round_id = tracking["round_id"]
    sessions = tracking["sessions"]
    v1_id = tracking["v1"]["version_id"]
    release_id = tracking["v1"]["approval"]["tracking"]["release_id"]

    def feed(action_offset: int | None) -> Path:
        actions = (
            late_dividend_action(sessions, action_offset, SYMBOLS[0])
            if action_offset is not None
            else None
        )
        return write_feed(
            tmp_path / f"feed-{action_offset}.json",
            feed_for(sessions, direction="falling", actions=actions),
        )

    blocked = shadow_update(
        ledger, store, feed(3), session_at(sessions, 3), release_ids=(release_id,)
    )
    assert blocked.returncode == 2
    first = version_entry(
        ok(evidence_status(project, store, ledger, round_id)), v1_id
    )
    assert first["state"] == "paused"
    assert first["tracking"]["data_gap_sessions"] == 1
    ok(
        shadow_update(
            ledger, store, feed(None), session_at(sessions, 4), release_ids=(release_id,)
        )
    )
    recovered = version_entry(
        ok(evidence_status(project, store, ledger, round_id)), v1_id
    )
    assert recovered["pause"] is None
    assert recovered["state"] == "observation_extended"

    blocked = shadow_update(
        ledger, store, feed(5), session_at(sessions, 5), release_ids=(release_id,)
    )
    assert blocked.returncode == 2
    second = version_entry(
        ok(evidence_status(project, store, ledger, round_id)), v1_id
    )
    assert second["tracking"]["data_gap_sessions"] == 2
    assert second["review"]["trigger"] == "max_data_gap_sessions"
    assert "limit of 2" in second["review"]["reason"]
    assert second["review"]["required"] is True

    # Repairing the second gap leaves the historical gap fact and the review
    # requirement standing; the human decides, the platform never auto-retires.
    ok(
        shadow_update(
            ledger, store, feed(None), session_at(sessions, 5), release_ids=(release_id,)
        )
    )
    due = version_entry(
        ok(evidence_status(project, store, ledger, round_id)), v1_id
    )
    assert due["state"] == "review_due"
    assert due["pause"] is None
    assert due["tracking"]["data_gap_sessions"] == 2
    assert due["review"]["trigger"] == "max_data_gap_sessions"
