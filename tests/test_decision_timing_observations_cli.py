"""Public-CLI and unit tests for frozen after-close decision timing and common valid observations.

Ticket #77 (Spec #74-C):
- Opt-in ``after-close-common-v1`` decision policy with frozen cutoff and timezone
- ``history_requirements`` forwarded through ``locked`` and ``manual_tracking`` artifacts
- Common-valid-close observation selection across Discovery, Formal Validation, and Shadow Live
- Pause-on-anomaly with independent settlement of T-1 orders and automatic T+1 recovery
- Anti-lookahead, warmup insufficiency, pre-enablement warmup, and legacy fixed-window regression
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from cli_helpers import cli, ok, rejected, write_json
from test_actual_open_cli import (
    OPEN_SOURCE,
    day_at,
    make_actual_open_halt,
    make_confirmed_suspension,
    make_missing_required_open,
    make_open_conflict,
    raw_row,
    schema_v3_fixture,
    seal_v3,
)
from test_research_loop_cli import (
    open_round,
    round_input,
    submit_version,
    validate_version,
    version_input,
)
from test_version_review_cli import local_today, observation_input
from test_version_review_cli import review as review_version

from prajnatrade.data import ETF_SYMBOLS
from prajnatrade.decision_window import (
    AFTER_CLOSE_COMMON_POLICY,
    decision_cutoff,
    normalize_decision_policy,
    select_common_observations,
)


def test_decision_policy_normalization_and_cutoff() -> None:
    policy = normalize_decision_policy(AFTER_CLOSE_COMMON_POLICY)
    assert policy["version"] == AFTER_CLOSE_COMMON_POLICY
    assert policy["signal_cutoff"] == "15:30"
    assert policy["signal_timezone"] == "Asia/Shanghai"

    custom = normalize_decision_policy(
        AFTER_CLOSE_COMMON_POLICY,
        signal_cutoff="15:45",
        signal_timezone="Asia/Shanghai",
    )
    cutoff = decision_cutoff("2024-01-15", custom)
    assert cutoff == datetime(2024, 1, 15, 15, 45, tzinfo=ZoneInfo("Asia/Shanghai"))

    with pytest.raises(ValueError):
        normalize_decision_policy("unknown-policy-v1")
    with pytest.raises(ValueError):
        normalize_decision_policy(AFTER_CLOSE_COMMON_POLICY, signal_cutoff="25:00")
    with pytest.raises(ValueError):
        normalize_decision_policy(AFTER_CLOSE_COMMON_POLICY, signal_timezone="Invalid/Zone")


def _index_fixture_with_cutoffs(
    fixture: dict,
    *,
    close_time: str = "15:10:00+08:00",
    derived_time: str = "15:15:00+08:00",
) -> tuple[list[str], dict[tuple[str, str], dict], dict[tuple[str, str], dict]]:
    sessions = [row["date"] for row in fixture["calendar"]]
    raw_bars: dict[tuple[str, str], dict] = {}
    for row in fixture["raw_bars"]:
        item = dict(row)
        day = item["date"]
        item["available_at"] = f"{day}T{close_time}"
        if item.get("close") is not None:
            item["close_available_at"] = f"{day}T{close_time}"
        raw_bars[(day, item["symbol"])] = item
    derived: dict[tuple[str, str], dict] = {}
    for row in fixture["derived"]:
        item = dict(row)
        day = item["date"]
        item["available_at"] = f"{day}T{derived_time}"
        derived[(day, item["symbol"])] = item
    return sessions, raw_bars, derived


def test_select_common_observations_61_staggered_halts_pause_and_recovery() -> None:
    # 66 sessions: enough for 61 common valid close observations even with staggered halts + T+1 recovery
    fixture = schema_v3_fixture(66)
    all_sessions = [row["date"] for row in fixture["calendar"]]
    d10, d20, d30, d40 = all_sessions[10], all_sessions[20], all_sessions[30], all_sessions[40]

    # Staggered confirmed whole-day suspensions:
    # - session[10]: ETF 0 suspended
    # - session[20]: ETF 1 suspended
    # - session[30]: ETF 2 partial_halt with timely close -> MUST be included!
    # - session[40]: ETF 3 partial_halt open-only (close=None) -> historical anomaly, skipped!
    make_confirmed_suspension(fixture, d10, ETF_SYMBOLS[0])
    make_confirmed_suspension(fixture, d20, ETF_SYMBOLS[1])
    make_actual_open_halt(fixture, d30, ETF_SYMBOLS[2], open_price=10.5)
    open_only = make_actual_open_halt(fixture, d40, ETF_SYMBOLS[3], open_price=10.5)
    open_only["close"] = None
    open_only["close_available_at"] = None

    sessions, raw_bars, derived = _index_fixture_with_cutoffs(fixture)
    policy = normalize_decision_policy(AFTER_CLOSE_COMMON_POLICY)
    reqs = [{"id": "strategy_lookback", "observations": 61, "mode": "valid_observations"}]

    # On day 63 (sessions[63]), there are 64 total days [0..63], minus 3 skipped (d10, d20, d40) = 61!
    t_day = sessions[63]
    res = select_common_observations(
        sessions, raw_bars, derived, t_day, policy, reqs, data_kind="real"
    )
    assert res["signal_status"] == "ready"
    assert res["ready"] is True
    assert res["signal_pause"] is None
    assert len(res["observation_dates"]) == 61
    assert d10 not in res["observation_dates"]
    assert d20 not in res["observation_dates"]
    assert d40 not in res["observation_dates"]
    assert d30 in res["observation_dates"]
    assert t_day in res["observation_dates"]
    assert res["observation_dates"][0] == sessions[0]

    # One day earlier (sessions[62]), only 60 common valid dates exist -> insufficient_history
    res_short = select_common_observations(
        sessions, raw_bars, derived, sessions[62], policy, reqs, data_kind="real"
    )
    assert res_short["signal_status"] == "insufficient_history"
    assert res_short["ready"] is False
    assert res_short["signal_pause"] is None
    assert len(res_short["observation_dates"]) == 60

    # On sessions[64], make ETF 4 close arrive AFTER 15:30 cutoff (15:31) -> PAUSE on T!
    t_late = sessions[64]
    raw_bars[(t_late, ETF_SYMBOLS[4])]["close_available_at"] = f"{t_late}T15:31:00+08:00"
    res_paused = select_common_observations(
        sessions, raw_bars, derived, t_late, policy, reqs, data_kind="real"
    )
    assert res_paused["signal_status"] == "paused"
    assert res_paused["ready"] is False
    assert res_paused["signal_pause"]["date"] == t_late
    assert res_paused["signal_pause"]["symbols"] == [ETF_SYMBOLS[4]]
    assert res_paused["signal_pause"]["reasons"] == [
        {"symbol": ETF_SYMBOLS[4], "reason": "close_after_cutoff"}
    ]

    # On sessions[65] with timely data -> T+1 recovers automatically!
    # At T+1 (15:30), t_late's 15:31 close is now point-in-time available, AND if t_late was
    # permanently missing/conflicted, T+1 skips t_late and backfills from earlier valid dates.
    next_day = sessions[65]
    res_recovered = select_common_observations(
        sessions, raw_bars, derived, next_day, policy, reqs, data_kind="real"
    )
    assert res_recovered["signal_status"] == "ready"
    assert res_recovered["ready"] is True
    assert len(res_recovered["observation_dates"]) == 61
    assert next_day in res_recovered["observation_dates"]

    # Now make t_late permanently missing close -> t_late paused on t_late AND skipped on next_day!
    raw_bars[(t_late, ETF_SYMBOLS[4])]["close"] = None
    raw_bars[(t_late, ETF_SYMBOLS[4])]["close_available_at"] = None
    res_skipped = select_common_observations(
        sessions, raw_bars, derived, next_day, policy, reqs, data_kind="real"
    )
    assert res_skipped["signal_status"] == "ready"
    assert res_skipped["ready"] is True
    assert len(res_skipped["observation_dates"]) == 61
    assert t_late not in res_skipped["observation_dates"]
    assert next_day in res_skipped["observation_dates"]
    assert res_skipped["observation_dates"][0] == sessions[1]


def _make_cutoff_fixture(
    num_sessions: int,
    *,
    close_time: str = "15:00:00+08:00",
    derived_time: str = "15:15:00+08:00",
    synthetic_future: bool = False,
) -> dict:
    fixture = schema_v3_fixture(num_sessions)
    if synthetic_future:
        # Shift dates so Discovery trailing_start is local_today() + 1 day,
        # enabling round review + Shadow Live on the exact same trailing dates.
        anchor = local_today() + timedelta(days=1)
        disc_count = (3 * num_sessions) // 5
        trailing_start_idx = (disc_count * 70) // 100
        old_days = [row["date"] for row in fixture["calendar"]]
        day_map = {
            old_day: (anchor + timedelta(days=i - trailing_start_idx)).isoformat()
            for i, old_day in enumerate(old_days)
        }
        fixture["schema_version"] = 1
        fixture["data_kind"] = "synthetic"
        fixture.pop("evidence", None)
        fixture.pop("raw_price_basis", None)
        for src in fixture["sources"].values():
            src["coverage_start"] = day_map[old_days[0]]
            src["coverage_end"] = day_map[old_days[-1]]
            src["available_at"] = f"{day_map[old_days[0]]}T00:00:00+08:00"
            src["collected_at"] = f"{day_map[old_days[-1]]}T23:59:59+08:00"
        for interval in fixture["symbol_coverage"].values():
            interval["start"] = day_map[old_days[0]]
            interval["end"] = day_map[old_days[-1]]
        for row in fixture["calendar"]:
            row["date"] = day_map[row["date"]]
            row["available_at"] = f"{row['date']}T00:00:00+08:00"
        for row in fixture["raw_bars"]:
            row["date"] = day_map[row["date"]]
            for k in (
                "open_source", "status_evidence_id", "open_conflict", "status_conflict",
                "open_available_at", "close_available_at",
            ):
                row.pop(k, None)
        for row in fixture["derived"]:
            row["date"] = day_map[row["date"]]
        fixture["corporate_actions"] = []
    for row in fixture["raw_bars"]:
        day = row["date"]
        row["available_at"] = f"{day}T{close_time}"
        if not synthetic_future and row.get("close") is not None:
            row["close_available_at"] = f"{day}T{close_time}"
    for row in fixture["derived"]:
        day = row["date"]
        row["available_at"] = f"{day}T{derived_time}"
    # Deterministic upward drift differentiated by symbol so momentum picks a unique winner
    sessions = [row["date"] for row in fixture["calendar"]]
    day_index = {d: i for i, d in enumerate(sessions)}
    for row in fixture["derived"]:
        idx = day_index[row["date"]]
        sym_idx = ETF_SYMBOLS.index(row["symbol"])
        row["value"] = round(100.0 * (1.0 + (0.002 + 0.001 * sym_idx) * idx), 6)
    return fixture


def test_cross_stage_parity_discovery_formal_validation_and_shadow_cli(tmp_path: Path) -> None:
    # 250 sessions -> Discovery 150 -> 70/30 split gives 105 development / 45 trailing.
    # Strategy requires 61 valid observations -> ready in development AND trailing!
    fixture = _make_cutoff_fixture(250, close_time="09:30:00+08:00", synthetic_future=True)
    sessions = [row["date"] for row in fixture["calendar"]]
    snapshot_store = tmp_path / "snapshots"
    eval_store = tmp_path / "eval_store"
    project = tmp_path / "project"
    ledger = tmp_path / "ledger"

    sealed = seal_v3(tmp_path, fixture, store="snapshots")
    snapshot_id = sealed["snapshot_id"]

    ok(cli("project", "init", "--project", str(project)))
    campaign = ok(
        cli(
            "campaign",
            "create",
            "--project",
            str(project),
            "--store",
            str(snapshot_store),
            "--snapshot-id",
            snapshot_id,
            "--attempts",
            "3",
            "--max-seconds",
            "60",
            "--initial-cash",
            "100000",
            "--transaction-cost-bps",
            "0",
            "--slippage-bps",
            "0",
            "--execution-rules",
            "actual-open-v1",
            "--discovery-split",
            "70/30",
            "--discovery-warmup-sessions",
            "61",
            "--decision-policy",
            AFTER_CLOSE_COMMON_POLICY,
            "--signal-cutoff",
            "15:30",
            "--signal-timezone",
            "Asia/Shanghai",
        )
    )
    campaign_id = campaign["campaign_id"]
    assert campaign["decision_policy"]["version"] == AFTER_CLOSE_COMMON_POLICY
    assert campaign["decision_policy"]["signal_cutoff"] == "15:30"
    assert campaign["decision_policy"]["signal_timezone"] == "Asia/Shanghai"

    # Submit a momentum strategy declaring 61 valid observations
    exp_req = {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "submission_id": "sub-mom-61",
        "hypothesis": "61-observation momentum under after-close-common-v1",
        "strategy": {
            "family": "momentum",
            "history_requirements": [
                {"id": "strategy_lookback", "observations": 61, "mode": "valid_observations"}
            ],
        },
        "parameters": {"lookback": 20, "top_k": 1, "cash_if_negative": True},
    }
    exp_file = write_json(tmp_path / "exp.json", exp_req)
    exp_receipt = ok(
        cli("experiment", "submit", "--project", str(project), "--input", str(exp_file))
    )
    assert exp_receipt["status"] == "succeeded"

    # Protected evaluation must reject decision_policy
    cand = ok(
        cli(
            "candidate",
            "freeze",
            "--project",
            str(project),
            "--campaign-id",
            campaign_id,
            "--submission-id",
            "sub-mom-61",
            "--name",
            "cand-mom-61",
            "--conclusion",
            "test",
        )
    )
    plan_file = write_json(
        tmp_path / "plan.json",
        {
            "schema_version": 1,
            "candidate_id": cand["candidate_id"],
            "snapshot_id": snapshot_id,
            "thresholds": {
                "min_return": -1.0,
                "max_drawdown": 1.0,
                "min_trades": 0,
            },
        },
    )
    plan_res = cli(
        "evaluation",
        "plan",
        "--project",
        str(project),
        "--store",
        str(eval_store),
        "--snapshot-store",
        str(snapshot_store),
        "--input",
        str(plan_file),
    )
    assert plan_res.returncode != 0

    # Formal Validation through Research Loop
    round_file = write_json(
        tmp_path / "round.json",
        round_input(validation_policy={"auto_validation_limit": 2}),
    )
    round_id = ok(open_round(project, eval_store, campaign_id, round_file))["round_id"]
    ver_file = write_json(
        tmp_path / "ver.json",
        version_input(exp_receipt["experiment_id"]),
    )
    ver = ok(submit_version(project, round_id, "v1", ver_file))
    version_id = ver["version_id"]
    assert ver["locked"]["decision_policy"] == campaign["decision_policy"]
    assert ver["locked"]["history_requirements"] == [
        {"id": "strategy_lookback", "observations": 61, "mode": "valid_observations"}
    ]

    val_receipt = ok(validate_version(project, eval_store, snapshot_store, round_id, version_id))
    assert val_receipt["outcome"] == "passed"
    assert val_receipt["provenance"]["decision_policy"] == campaign["decision_policy"]
    assert val_receipt["protocol"]["decision_policy"] == campaign["decision_policy"]

    # Review approve -> manual_tracking artifact
    trailing_start = campaign["discovery_split"]["trailing"]["start"]
    trailing_end = campaign["discovery_split"]["trailing"]["end"]
    today_str = local_today().isoformat()
    cal_sessions = [d for d in sessions if trailing_start <= d <= trailing_end and d > today_str]
    calendar_file = write_json(
        tmp_path / "calendar.json",
        {
            "schema_version": 1,
            "data_kind": "synthetic",
            "source": "synthetic",
            "sessions": cal_sessions,
        },
    )
    review_file = write_json(
        tmp_path / "review.json",
        observation_input(sessions=len(cal_sessions)),
    )
    review_res = ok(
        review_version(
            project,
            eval_store,
            round_id,
            version_id,
            "approve",
            reason="synthetic acceptance",
            reviewer="operator",
            source=review_file,
            calendar=calendar_file,
        )
    )
    release_id = review_res["tracking"]["release_id"]
    artifact_path = eval_store / "releases" / release_id / "artifact.json"
    artifact = json.loads(artifact_path.read_text())
    assert artifact["execution"]["decision_policy"] == campaign["decision_policy"]
    assert artifact["execution"]["history_requirements"] == [
        {"id": "strategy_lookback", "observations": 61, "mode": "valid_observations"}
    ]

    # Export daily feed from sealed snapshot up to cal_sessions[2]
    feed_path = tmp_path / "shadow_feed.json"
    ok(
        cli(
            "shadow",
            "export-feed",
            "--store",
            str(snapshot_store),
            "--snapshot-id",
            snapshot_id,
            "--through-date",
            cal_sessions[2],
            "--output",
            str(feed_path),
        )
    )

    # Update Shadow Live on cal_sessions[0] and cal_sessions[1]
    shadow_res = ok(
        cli(
            "shadow",
            "update",
            "--ledger",
            str(ledger),
            "--store",
            str(eval_store),
            "--corrections-store",
            str(snapshot_store),
            "--data",
            str(feed_path),
            "--date",
            cal_sessions[1],
            "--release-id",
            release_id,
        )
    )
    assert shadow_res["versions"][0]["status"] in ("committed", "committed_with_anomalies")

    # Pre-enablement sessions (sessions[0..83]) must NOT be committed in shadow.sqlite
    with sqlite3.connect(ledger / "shadow.sqlite") as conn:
        committed_dates = [
            row[0]
            for row in conn.execute(
                "SELECT date FROM days WHERE version_id = ? ORDER BY date",
                (release_id,),
            ).fetchall()
        ]
        day0_json = json.loads(
            conn.execute(
                "SELECT result_json FROM days WHERE version_id = ? AND date = ?",
                (release_id, cal_sessions[0]),
            ).fetchone()[0]
        )
    assert committed_dates == [cal_sessions[0], cal_sessions[1]]
    assert day0_json["signal"] is not None
    assert day0_json["signal"]["date"] == cal_sessions[0]
    assert day0_json["signal"]["execution_date"] == cal_sessions[1]
    assert len(day0_json["signal"]["observation_dates"]) == 61
    assert day0_json["signal"]["observation_dates"][-1] == cal_sessions[0]
    assert day0_json["signal_status"] == "generated"

    # Compare Formal Validation and Shadow Live on cal_sessions[0] -> cal_sessions[1]
    val_record = json.loads(
        (eval_store / "research-loop-validations" / f"{val_receipt['validation_id']}.json").read_text()
    )
    formal_day1_fills = [
        f for f in val_record["runs"]["candidate"]["fills"] if f["date"] == cal_sessions[1]
    ]
    with sqlite3.connect(ledger / "shadow.sqlite") as conn:
        day1_json = json.loads(
            conn.execute(
                "SELECT result_json FROM days WHERE version_id = ? AND date = ?",
                (release_id, cal_sessions[1]),
            ).fetchone()[0]
        )
    assert len(formal_day1_fills) == 1
    assert len(day1_json["fills"]) == 1
    assert formal_day1_fills[0]["symbol"] == day1_json["fills"][0]["symbol"] == ETF_SYMBOLS[4]
    assert formal_day1_fills[0]["side"] == day1_json["fills"][0]["side"] == "buy"
    assert formal_day1_fills[0]["price"] == day1_json["fills"][0]["price"]

    # Also verify Discovery experiment receipt ran with the frozen decision_policy
    exp_status = ok(
        cli(
            "experiment",
            "status",
            "--project",
            str(project),
            "--campaign-id",
            campaign_id,
            "--submission-id",
            "sub-mom-61",
        )
    )
    assert exp_status["status"] == "succeeded"


def _write_tracking_release(
    eval_store: Path,
    *,
    tag: str,
    sessions: list[str],
    enabled_date: str,
    decision_policy: dict | None = None,
    history_requirements: list[dict] | None = None,
    signal_window_sessions: int = 21,
) -> str:
    """Write a minimal valid manual_tracking release artifact + events for Shadow CLI tests."""
    import hashlib
    from prajnatrade import release as release_mod

    cand_hash = hashlib.sha256(f"cand-{tag}".encode("utf-8")).hexdigest()
    plan_id = hashlib.sha256(f"plan-{tag}".encode("utf-8")).hexdigest()
    release_id = release_mod.tracking_id(cand_hash, plan_id)
    val_receipt = {
        "validation_id": plan_id,
        "version_id": cand_hash,
        "status": "completed",
        "outcome": "passed",
    }
    receipt_hash = hashlib.sha256(
        json.dumps(val_receipt, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    val_receipt["receipt_hash"] = receipt_hash
    val_dir = eval_store / "research-loop-validations"
    val_dir.mkdir(parents=True, exist_ok=True)
    write_json(val_dir / f"{plan_id}.json", val_receipt)

    cal = {
        "schema_version": 1,
        "data_kind": "synthetic",
        "source": "synthetic",
        "sessions": sessions,
    }
    cal_hash = hashlib.sha256(
        json.dumps(cal, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    execution: dict = {
        "strategy": {"family": "momentum", "parameters": {"lookback": 20, "top_k": 1, "cash_if_negative": True}},
        "strategy_family": "momentum",
        "parameters": {"lookback": 20, "top_k": 1, "cash_if_negative": True},
        "initial_cash": 100_000.0,
        "transaction_cost_bps": 0.0,
        "slippage_bps": 0.0,
        "signal_window_sessions": signal_window_sessions,
        "execution_rules": {"version": "actual-open-v1"},
    }
    if decision_policy is not None:
        execution["decision_policy"] = decision_policy
    if history_requirements is not None:
        execution["history_requirements"] = history_requirements

    artifact: dict = {
        "schema_version": 1,
        "kind": release_mod.ARTIFACT_KIND,
        "scope": "synthetic",
        "data_kind": "synthetic",
        "release_version": release_mod.RELEASE_VERSION,
        "workflow": release_mod.TRACKING_WORKFLOW,
        "simulated_tracking": True,
        "release_id": release_id,
        "candidate_id": "cand-1",
        "candidate_hash": cand_hash,
        "plan_id": plan_id,
        "plan_hash": plan_id,
        "snapshot_id": "c" * 64,
        "binding": {
            "version_id": cand_hash,
            "validation_id": plan_id,
            "receipt_hash": receipt_hash,
        },
        "admission": {
            "decision": "approved",
            "automatic_enable": True,
            "protected_evaluation": False,
            "conditional_release": False,
            "effective_signal_date": enabled_date,
            "calendar": cal,
            "calendar_hash": cal_hash,
        },
        "execution": execution,
    }
    artifact["artifact_hash"] = hashlib.sha256(
        json.dumps(artifact, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    release_mod.register_tracking_version(eval_store, artifact, enabled_date, "synthetic test")
    return release_id


def _shadow_feed_from_fixture(fixture: dict, *, through_date: str) -> dict:
    sessions = [row["date"] for row in fixture["calendar"] if row["date"] <= through_date]
    session_set = set(sessions)
    return {
        "schema_version": 1,
        "kind": "shadow_live_feed",
        "data_kind": "synthetic",
        "symbols": list(ETF_SYMBOLS),
        "calendar": [row for row in fixture["calendar"] if row["date"] in session_set],
        "raw_bars": [row for row in fixture["raw_bars"] if row["date"] in session_set],
        "derived": [row for row in fixture["derived"] if row["date"] in session_set],
        "corporate_actions": [],
    }


def test_shadow_pause_recovery_lookahead_warmup_and_legacy_regression(tmp_path: Path) -> None:
    fixture = _make_cutoff_fixture(80, close_time="09:30:00+08:00", synthetic_future=True)
    sessions = [row["date"] for row in fixture["calendar"]]
    # Enable on sessions[60]:
    # - sessions[60]: only 61 sessions [0..60]. If we suspend sessions[10], only 60 valid -> warmup insufficient!
    # - sessions[61]: 61 valid observations -> signal generated for sessions[62] open!
    # - sessions[62]: T-1 order settles at sessions[62] open, AND sessions[62] has late derived (15:35 > 15:30)
    #                 -> sessions[62] commits, T-1 order settles, sessions[62] signal is PAUSED!
    # - sessions[63]: timely data -> Shadow recovers and generates signal!
    make_confirmed_suspension(fixture, sessions[10], ETF_SYMBOLS[0])
    # Restore synthetic availability for the suspended row
    for row in fixture["raw_bars"]:
        if row["date"] == sessions[10] and row["symbol"] == ETF_SYMBOLS[0]:
            row["available_at"] = f"{sessions[10]}T09:30:00+08:00"
            row["availability_evidence"] = "ev-susp-10"

    # Make sessions[62] derived for ETF_SYMBOLS[1] arrive AFTER 15:30 cutoff (15:35)
    for row in fixture["derived"]:
        if row["date"] == sessions[62] and row["symbol"] == ETF_SYMBOLS[1]:
            row["available_at"] = f"{sessions[62]}T15:35:00+08:00"

    eval_store = tmp_path / "eval_store"
    snapshots_store = tmp_path / "snapshots"
    snapshots_store.mkdir(parents=True, exist_ok=True)
    ledger = tmp_path / "ledger"
    policy = normalize_decision_policy(AFTER_CLOSE_COMMON_POLICY)
    new_rel_id = _write_tracking_release(
        eval_store,
        tag="new",
        sessions=sessions[60:],
        enabled_date=sessions[60],
        decision_policy=policy,
        history_requirements=[
            {"id": "strategy_lookback", "observations": 61, "mode": "valid_observations"}
        ],
    )
    legacy_rel_id = _write_tracking_release(
        eval_store,
        tag="legacy",
        sessions=sessions[60:],
        enabled_date=sessions[60],
        decision_policy=None,
        history_requirements=None,
        signal_window_sessions=21,
    )

    feed_path = write_json(
        tmp_path / "feed.json",
        _shadow_feed_from_fixture(fixture, through_date=sessions[64]),
    )

    # Update new release through sessions[63]
    res = ok(
        cli(
            "shadow",
            "update",
            "--ledger",
            str(ledger),
            "--store",
            str(eval_store),
            "--corrections-store",
            str(snapshots_store),
            "--data",
            str(feed_path),
            "--date",
            sessions[63],
            "--release-id",
            new_rel_id,
        )
    )
    assert res["versions"][0]["status"] in ("committed", "committed_with_anomalies")

    with sqlite3.connect(ledger / "shadow.sqlite") as conn:
        rows = {
            row[0]: json.loads(row[1])
            for row in conn.execute(
                "SELECT date, result_json FROM days WHERE version_id = ? ORDER BY date",
                (new_rel_id,),
            ).fetchall()
        }

    # Pre-enablement sessions[0..59] must not be committed
    assert sorted(rows) == [sessions[60], sessions[61], sessions[62], sessions[63]]

    # sessions[60]: warmup insufficient (60 valid < 61 required) -> no signal, committed cleanly
    d60 = rows[sessions[60]]
    assert d60["signal"] is None
    assert d60["signal_status"] == "insufficient_history"
    assert d60["signal_pause"] is None

    # sessions[61]: 61 valid observations -> signal generated for sessions[62]
    d61 = rows[sessions[61]]
    assert d61["signal"] is not None
    assert d61["signal"]["execution_date"] == sessions[62]
    assert d61["signal_status"] == "generated"
    assert len(d61["observation_dates"]) == 61
    assert sessions[10] not in d61["observation_dates"]

    # sessions[62]: T-1 order STILL settles at sessions[62] open, while sessions[62] signal is PAUSED!
    d62 = rows[sessions[62]]
    assert len(d62["fills"]) == 1
    assert d62["fills"][0]["symbol"] == ETF_SYMBOLS[4]
    assert d62["signal"] is None
    assert d62["signal_status"] == "paused"
    assert d62["signal_pause"]["date"] == sessions[62]
    assert d62["signal_pause"]["symbols"] == [ETF_SYMBOLS[1]]
    assert d62["signal_pause"]["reasons"] == [
        {"symbol": ETF_SYMBOLS[1], "reason": "derived_after_cutoff"}
    ]

    # sessions[63]: Shadow recovers and generates signal!
    d63 = rows[sessions[63]]
    assert d63["signal"] is not None
    assert d63["signal_status"] == "generated"
    assert d63["signal_pause"] is None

    # Legacy release regression: runs with fixed signal_window_sessions=21 when derived is available by 15:00,
    # and keeps legacy day schema without decision_policy/signal_status/signal_cutoff.
    legacy_fixture = _make_cutoff_fixture(
        80, close_time="09:30:00+08:00", derived_time="15:00:00+08:00", synthetic_future=True,
    )
    legacy_feed_path = write_json(
        tmp_path / "legacy_feed.json",
        _shadow_feed_from_fixture(legacy_fixture, through_date=sessions[64]),
    )
    res_leg = ok(
        cli(
            "shadow",
            "update",
            "--ledger",
            str(ledger),
            "--store",
            str(eval_store),
            "--corrections-store",
            str(snapshots_store),
            "--data",
            str(legacy_feed_path),
            "--date",
            sessions[61],
            "--release-id",
            legacy_rel_id,
        )
    )
    assert res_leg["versions"][0]["status"] == "committed"
    with sqlite3.connect(ledger / "shadow.sqlite") as conn:
        leg_d60 = json.loads(
            conn.execute(
                "SELECT result_json FROM days WHERE version_id = ? AND date = ?",
                (legacy_rel_id, sessions[60]),
            ).fetchone()[0]
        )
    assert "decision_policy" not in leg_d60
    assert "signal_status" not in leg_d60
    assert "signal_cutoff" not in leg_d60
    assert leg_d60["signal"] is not None
    assert len(leg_d60["signal"]["window"]) == 21

