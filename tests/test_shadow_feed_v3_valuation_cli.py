"""Tests for Spec #74-B (#76): Shadow Feed v3, export-feed, and carry-forward valuation."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cli_helpers import cli, ok
from test_actual_open_cli import (
    PRICE_EVIDENCE,
    STATUS_EVIDENCE,
    day_at,
    make_actual_open_halt,
    make_confirmed_suspension,
    make_open_conflict,
    raw_row,
    schema_v3_fixture,
    seal_v3,
)
from test_shadow_cli import day_record, write_feed

from prajnatrade import release, shadow, snapshot, snapshot_baseline
from prajnatrade.strategies import make_strategy
from prajnatrade.trading_status import (
    ACTUAL_OPEN_RULES,
    CARRY_FORWARD_SUSPENDED_VALUATION,
    STRICT_CLOSE_VALUATION,
)


def _v3_feed_from_fixture(session_count: int = 8, *, data_kind: str = "synthetic") -> dict:
    payload = schema_v3_fixture(session_count)
    payload["data_kind"] = data_kind
    payload.setdefault("symbols", list(shadow.ETF_SYMBOLS))
    return payload


def test_feed_v3_validates_evidence_roles_conflicts_and_action_sequences(
    tmp_path: Path,
) -> None:
    for data_kind in ("synthetic", "real"):
        payload = _v3_feed_from_fixture(8, data_kind=data_kind)
        day2 = payload["calendar"][2]["date"]
        day3 = payload["calendar"][3]["date"]
        make_actual_open_halt(payload, day2, "511260", open_price=10.5)
        make_confirmed_suspension(payload, day3, "159612")
        path = tmp_path / f"feed_{data_kind}_v3.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        feed = shadow.load_feed(path)
        assert feed.schema_version == 3
        assert feed.data_kind == data_kind

    # Reject missing raw_price_basis
    bad_basis = _v3_feed_from_fixture(6)
    del bad_basis["raw_price_basis"]
    bad_basis_path = tmp_path / "bad_basis.json"
    bad_basis_path.write_text(json.dumps(bad_basis), encoding="utf-8")
    with pytest.raises(shadow.ShadowRejection, match="unadjusted"):
        shadow.load_feed(bad_basis_path)

    # Reject using a halt/status notice as open_source (price source)
    bad_price_src = _v3_feed_from_fixture(6)
    day1 = bad_price_src["calendar"][1]["date"]
    row = raw_row(bad_price_src, day1, "511260")
    row["open_source"] = {"source": "halt_notice_feed", "evidence_id": STATUS_EVIDENCE}
    bad_price_path = tmp_path / "bad_price_src.json"
    bad_price_path.write_text(json.dumps(bad_price_src), encoding="utf-8")
    with pytest.raises(shadow.ShadowRejection, match="price_archive"):
        shadow.load_feed(bad_price_path)

    # Reject using a price_archive as status_evidence_id
    bad_status_src = _v3_feed_from_fixture(6)
    make_confirmed_suspension(bad_status_src, day1, "159612")
    raw_row(bad_status_src, day1, "159612")["status_evidence_id"] = PRICE_EVIDENCE
    bad_status_path = tmp_path / "bad_status_src.json"
    bad_status_path.write_text(json.dumps(bad_status_src), encoding="utf-8")
    with pytest.raises(shadow.ShadowRejection, match="trading-status"):
        shadow.load_feed(bad_status_path)

    # Reject open_conflict on suspended row
    bad_conflict = _v3_feed_from_fixture(6)
    make_open_conflict(bad_conflict, day1, "515080")
    raw_row(bad_conflict, day1, "515080")["trading_status"] = "suspended"
    for field in ("high", "low", "close", "volume"):
        raw_row(bad_conflict, day1, "515080")[field] = None
    bad_conflict_path = tmp_path / "bad_conflict.json"
    bad_conflict_path.write_text(json.dumps(bad_conflict), encoding="utf-8")
    with pytest.raises(shadow.ShadowRejection, match="suspended"):
        shadow.load_feed(bad_conflict_path)

    # Corporate actions: canonical sequence sorting & duplicate sequence rejection
    seq_feed = _v3_feed_from_fixture(6)
    seq_feed["corporate_actions"] = [
        {
            "action_id": "act-2",
            "date": day1,
            "symbol": "511260",
            "type": "share_adjustment",
            "ratio": 2.0,
            "sequence": 2,
            "terms_evidence": "split_notice",
            "available_at": f"{day1}T08:00:00+08:00",
            "availability_evidence": "exchange_notice",
        },
        {
            "action_id": "act-1",
            "date": day1,
            "symbol": "511260",
            "type": "cash_dividend",
            "cash_per_share": 0.2,
            "pay_date": day_at(2),
            "sequence": 1,
            "terms_evidence": "div_notice",
            "available_at": f"{day1}T08:00:00+08:00",
            "availability_evidence": "exchange_notice",
        },
    ]
    seq_path = tmp_path / "seq_feed.json"
    seq_path.write_text(json.dumps(seq_feed), encoding="utf-8")
    loaded = shadow.load_feed(seq_path)
    assert [a["action_id"] for a in loaded.actions_by_date[day1]] == ["act-1", "act-2"]

    seq_feed["corporate_actions"][1]["sequence"] = 2
    seq_path.write_text(json.dumps(seq_feed), encoding="utf-8")
    with pytest.raises(shadow.ShadowRejection, match="sequence"):
        shadow.load_feed(seq_path)


def test_export_feed_cli_emits_only_discovery_facts_through_date(tmp_path: Path) -> None:
    payload = schema_v3_fixture(15)
    # Place a corporate action and suspended row before and after through_date
    through_date = payload["calendar"][4]["date"]
    future_date = payload["calendar"][7]["date"]
    make_confirmed_suspension(payload, through_date, "159612")
    make_actual_open_halt(payload, future_date, "511260", open_price=12.0)

    sealed = seal_v3(tmp_path, payload)
    store = sealed["store"]
    snapshot_id = sealed["snapshot_id"]

    out_path = tmp_path / "exported_feed.json"
    result = ok(
        cli(
            "shadow",
            "export-feed",
            "--store",
            str(store),
            "--snapshot-id",
            snapshot_id,
            "--through-date",
            through_date,
            "--output",
            str(out_path),
        )
    )
    assert result["status"] == "ok"
    assert result["schema_version"] == 3
    assert result["through_date"] == through_date
    exported = json.loads(out_path.read_text(encoding="utf-8"))
    assert exported["schema_version"] == 3
    assert exported["data_kind"] == "real"
    assert exported["raw_price_basis"] == "unadjusted"
    assert [row["date"] for row in exported["calendar"]] == [
        row["date"] for row in payload["calendar"][:5]
    ]
    assert all(row["date"] <= through_date for row in exported["raw_bars"])
    assert all(row["date"] <= through_date for row in exported["derived"])
    assert all(row["date"] <= through_date for row in exported["corporate_actions"])
    # Exported feed is directly loadable by shadow.load_feed
    loaded = shadow.load_feed(out_path)
    assert loaded.sessions[-1] == through_date
    assert (through_date, "159612") in loaded.raw
    assert (future_date, "511260") not in loaded.raw


def build_manual_tracking_release(
    store: Path,
    sessions: list[str],
    *,
    valuation_policy: str = CARRY_FORWARD_SUSPENDED_VALUATION,
    execution_rules: str = ACTUAL_OPEN_RULES,
    initial_cash: float = 1000.0,
) -> dict:
    """Create and enable a synthetic manual_tracking release for Shadow tests."""
    strategy = {
        "family": "momentum",
        "parameters": {"lookback": 2, "top_k": 1, "cash_if_negative": False},
    }
    execution_rules_obj = {
        "version": execution_rules,
        "valuation": {"version": valuation_policy},
    }
    calendar = {
        "schema_version": 1,
        "data_kind": "synthetic",
        "source": "synthetic-calendar",
        "sessions": list(sessions),
    }
    cal_hash = snapshot.canonical_hash(calendar)
    version_id = snapshot.canonical_hash({"strategy": strategy, "rules": execution_rules_obj, "cash": initial_cash})
    validation_id = snapshot.canonical_hash({"val": version_id})
    receipt_body = {
        "validation_id": validation_id,
        "version_id": version_id,
        "status": "completed",
        "outcome": "passed",
    }
    receipt_hash = snapshot.canonical_hash(receipt_body)
    receipt = {**receipt_body, "receipt_hash": receipt_hash}
    val_dir = store / "research-loop-validations"
    val_dir.mkdir(parents=True, exist_ok=True)
    (val_dir / f"{validation_id}.json").write_text(json.dumps(receipt), encoding="utf-8")

    release_id = release.tracking_id(version_id, validation_id)
    effective = sessions[0]
    artifact_body = {
        "schema_version": 1,
        "kind": release.ARTIFACT_KIND,
        "scope": "synthetic",
        "data_kind": "synthetic",
        "release_version": release.RELEASE_VERSION,
        "workflow": release.TRACKING_WORKFLOW,
        "simulated_tracking": True,
        "release_id": release_id,
        "candidate_id": "sub-1",
        "candidate_hash": version_id,
        "candidate": {
            "version_id": version_id,
            "strategy": strategy,
            "parameters": strategy["parameters"],
            "evaluation": {
                "strategy_executor": {"kind": "none"},
                "execution_rules": execution_rules_obj,
            },
        },
        "plan_kind": "lightweight_validation",
        "validation_id": validation_id,
        "plan_id": validation_id,
        "plan_hash": validation_id,
        "snapshot_id": "a" * 64,
        "binding": {
            "version_id": version_id,
            "validation_id": validation_id,
            "validation_outcome": "passed",
            "receipt_hash": receipt_hash,
            "initial_cash": initial_cash,
        },
        "execution": {
            "strategy": strategy,
            "strategy_family": strategy["family"],
            "parameters": strategy["parameters"],
            "initial_cash": initial_cash,
            "transaction_cost_bps": 0.0,
            "slippage_bps": 0.0,
            "signal_window_sessions": 3,
            "execution_rules": execution_rules_obj,
        },
        "initial_cash": initial_cash,
        "admission": {
            "decision": "approved",
            "automatic_enable": True,
            "statement": release.TRACKING_ADMISSION_STATEMENT,
            "mode": "simulated_tracking",
            "simulated_tracking": True,
            "protected_evaluation": False,
            "conditional_release": False,
            "effective_signal_date": effective,
            "calendar_hash": cal_hash,
            "calendar": calendar,
        },
        "tracking": {
            "workflow": release.TRACKING_WORKFLOW,
            "simulated_tracking": True,
            "release_id": release_id,
            "enabled_signal_date": effective,
            "calendar_hash": cal_hash,
        },
    }
    artifact = {**artifact_body, "artifact_hash": snapshot.canonical_hash(artifact_body)}
    store.mkdir(parents=True, exist_ok=True)
    rid = release.register_tracking_version(store, artifact, effective, "synthetic test")
    return {"release_id": rid, "artifact": artifact}


def _setup_carry_forward_scenario(tmp_path: Path) -> tuple[dict, list[str]]:
    payload = _v3_feed_from_fixture(11, data_kind="synthetic")
    sessions = [row["date"] for row in payload["calendar"]]
    # Make 511260 trade at 10.0 on days 0..3 and be top momentum on day 2
    for idx, day in enumerate(sessions):
        for sym in shadow.ETF_SYMBOLS:
            row = raw_row(payload, day, sym)
            row["open"] = 10.0
            row["high"] = 10.0
            row["low"] = 10.0
            row["close"] = 10.0
            row["open_available_at"] = f"{day}T09:30:00+08:00"
            row["close_available_at"] = f"{day}T15:00:00+08:00"
            row["available_at"] = f"{day}T09:30:00+08:00"
            d_row = next(
                d for d in payload["derived"] if d["date"] == day and d["symbol"] == sym
            )
            d_row["value"] = 10.0 + (0.5 * idx if sym == "511260" else 0.0)
            d_row["available_at"] = f"{day}T15:00:00+08:00"
    # Day 4: full-day suspended
    make_confirmed_suspension(payload, sessions[4], "511260")
    # Day 5: still suspended + ex-dividend 0.20/share (pay_date = sessions[6])
    make_confirmed_suspension(payload, sessions[5], "511260")
    # Day 6: still suspended + pay_date
    make_confirmed_suspension(payload, sessions[6], "511260")
    # Day 7: still suspended + 1-for-2 split (ratio 2.0)
    make_confirmed_suspension(payload, sessions[7], "511260")
    for idx in (4, 5, 6, 7):
        day = sessions[idx]
        raw_row(payload, day, "511260")["available_at"] = f"{day}T15:00:00+08:00"
    # Day 8: resumes trading with actual open 5.00, close 5.10
    day8_row = raw_row(payload, sessions[8], "511260")
    day8_row["trading_status"] = "trading"
    day8_row["open"] = 5.0
    day8_row["close"] = 5.10
    day8_row["high"] = 5.10
    day8_row["low"] = 5.0
    day8_row["open_available_at"] = f"{sessions[8]}T09:30:00+08:00"
    day8_row["close_available_at"] = f"{sessions[8]}T15:00:00+08:00"
    day8_row["available_at"] = f"{sessions[8]}T09:30:00+08:00"

    payload["corporate_actions"] = [
        {
            "action_id": "div-511260-d5",
            "date": sessions[5],
            "symbol": "511260",
            "type": "cash_dividend",
            "cash_per_share": 0.20,
            "pay_date": sessions[6],
            "sequence": 1,
            "terms_evidence": "div_notice_d5",
            "available_at": f"{sessions[5]}T08:00:00+08:00",
            "availability_evidence": "exchange_notice",
        },
        {
            "action_id": "split-511260-d7",
            "date": sessions[7],
            "symbol": "511260",
            "type": "share_adjustment",
            "ratio": 2.0,
            "sequence": 1,
            "terms_evidence": "split_notice_d7",
            "available_at": f"{sessions[7]}T08:00:00+08:00",
            "availability_evidence": "exchange_notice",
        },
    ]
    return payload, sessions


def test_shadow_carry_forward_valuation_corporate_actions_and_discovery_parity(
    tmp_path: Path,
) -> None:
    payload, sessions = _setup_carry_forward_scenario(tmp_path)
    ledger = tmp_path / "ledger"
    report_out = tmp_path / "report.json"

    assert len(sessions) == 11
    # Re-anchor payload dates to 2030-01-02.. so committed_at < 2030-01-02T09:30+08:00
    base = datetime(2030, 1, 2, tzinfo=UTC).date()
    date_map = {old: (base + timedelta(days=i)).isoformat() for i, old in enumerate(sessions)}
    text = json.dumps(payload)
    for old, new in date_map.items():
        text = text.replace(old, new)
    future_payload = json.loads(text)
    future_sessions = [date_map[s] for s in sessions]
    feed_path = write_feed(tmp_path / "feed_future.json", future_payload)
    store2 = tmp_path / "store2"
    rel2 = build_manual_tracking_release(store2, future_sessions)
    rid2 = rel2["release_id"]

    res3, blocked3 = shadow.update(ledger, store2, feed_path, future_sessions[3], [rid2])
    assert not blocked3, res3
    release.pause(store2, rid2, future_sessions[4], "pause signal generation during suspension")
    res, blocked = shadow.update(ledger, store2, feed_path, future_sessions[8], [rid2])
    assert not blocked, res

    d4 = day_record(ledger, rid2, report_out, future_sessions[4])["result"]
    pos4 = d4["positions"]["511260"]
    assert pos4["quantity"] == pytest.approx(100.0)
    assert pos4["close"] is None
    assert pos4["valuation_price"] == pytest.approx(10.0)
    assert pos4["market_value"] == pytest.approx(1000.0)
    assert pos4["valuation_basis"] == "carried_forward_prior_close"
    assert pos4["carry_forward"]["source_close_date"] == future_sessions[3]
    assert pos4["carry_forward"]["carried_sessions"] == 1
    assert d4["nav"] == pytest.approx(1000.0)

    # Day 5: ex-dividend 0.20/share -> market_value 980, receivable 20, nav 1000
    d5 = day_record(ledger, rid2, report_out, future_sessions[5])["result"]
    pos5 = d5["positions"]["511260"]
    assert pos5["close"] is None
    assert pos5["valuation_price"] == pytest.approx(9.80)
    assert pos5["market_value"] == pytest.approx(980.0)
    assert d5["dividend_receivables"] == pytest.approx(20.0)
    assert d5["nav"] == pytest.approx(1000.0)
    assert pos5["carry_forward"]["carried_sessions"] == 2
    assert len(pos5["carry_forward"]["adjustments"]) == 1

    # Day 6: pay_date -> cash 20, receivables 0, nav 1000
    d6 = day_record(ledger, rid2, report_out, future_sessions[6])["result"]
    assert d6["cash"] == pytest.approx(20.0)
    assert d6["dividend_receivables"] == pytest.approx(0.0)
    assert d6["positions"]["511260"]["valuation_price"] == pytest.approx(9.80)
    assert d6["positions"]["511260"]["market_value"] == pytest.approx(980.0)
    assert d6["nav"] == pytest.approx(1000.0)

    # Day 7: 1-for-2 split -> 200 shares @ 4.90 = 980, nav 1000
    d7 = day_record(ledger, rid2, report_out, future_sessions[7])["result"]
    pos7 = d7["positions"]["511260"]
    assert pos7["quantity"] == pytest.approx(200.0)
    assert pos7["valuation_price"] == pytest.approx(4.90)
    assert pos7["market_value"] == pytest.approx(980.0)
    assert d7["nav"] == pytest.approx(1000.0)
    assert pos7["carry_forward"]["carried_sessions"] == 4
    assert len(pos7["carry_forward"]["adjustments"]) == 2

    # Day 8: resumes with close 5.10 -> 200 * 5.10 + 20 = 1040, no double adjustment
    d8 = day_record(ledger, rid2, report_out, future_sessions[8])["result"]
    pos8 = d8["positions"]["511260"]
    assert pos8["close"] == pytest.approx(5.10)
    assert pos8["valuation_basis"] == "actual_close"
    assert pos8["market_value"] == pytest.approx(1020.0)
    assert d8["nav"] == pytest.approx(1040.0)

    # Discovery parity check on the same payload
    disc = snapshot_baseline.run_discovery_strategy(
        future_payload,
        "a" * 64,
        "b" * 64,
        strategy=make_strategy({"family": "momentum", "parameters": {"lookback": 2, "top_k": 1, "cash_if_negative": False}}),
        initial_cash=1000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
        signal_window_sessions=3,
        execution_rule_version=ACTUAL_OPEN_RULES,
        valuation_policy_version=CARRY_FORWARD_SUSPENDED_VALUATION,
    )
    curve_by_date = {pt["date"]: pt for pt in disc["equity_curve"]}
    for s in future_sessions[4:9]:
        s_day = day_record(ledger, rid2, report_out, s)["result"]
        assert curve_by_date[s]["nav"] == pytest.approx(s_day["nav"])
        assert curve_by_date[s]["market_value"] == pytest.approx(s_day["market_value"])
        assert curve_by_date[s]["cash"] == pytest.approx(s_day["cash"])


def test_shadow_carry_forward_gap_paths_zero_holding_and_strict_close_control(
    tmp_path: Path,
) -> None:
    payload, sessions = _setup_carry_forward_scenario(tmp_path)
    base = datetime(2030, 1, 2, tzinfo=UTC).date()
    date_map = {old: (base + timedelta(days=i)).isoformat() for i, old in enumerate(sessions)}
    text = json.dumps(payload)
    for old, new in date_map.items():
        text = text.replace(old, new)
    future_payload = json.loads(text)
    future_sessions = [date_map[s] for s in sessions]

    # Legacy strict-close-v1 control blocks on day 4 with missing_close
    strict_store = tmp_path / "strict_store"
    strict_rel = build_manual_tracking_release(
        strict_store, future_sessions, valuation_policy=STRICT_CLOSE_VALUATION
    )
    feed_path = write_feed(tmp_path / "feed.json", future_payload)
    res_strict, blocked_strict = shadow.update(
        tmp_path / "strict_ledger", strict_store, feed_path, future_sessions[4], [strict_rel["release_id"]]
    )
    assert blocked_strict
    assert res_strict["versions"][0]["reason_code"] == "missing_close"

    # Late status notice on held 511260 on day 4 -> status_evidence_after_cutoff
    late_payload = json.loads(json.dumps(future_payload))
    raw_row(late_payload, future_sessions[4], "511260")["available_at"] = (
        f"{future_sessions[5]}T09:00:00+08:00"
    )
    late_feed = write_feed(tmp_path / "late_feed.json", late_payload)
    cf_store = tmp_path / "cf_store"
    cf_rel = build_manual_tracking_release(cf_store, future_sessions)
    res_late, blocked_late = shadow.update(
        tmp_path / "late_ledger", cf_store, late_feed, future_sessions[4], [cf_rel["release_id"]]
    )
    assert blocked_late
    assert res_late["versions"][0]["reason_code"] == "missing_close"
    assert "status_evidence_after_cutoff" in res_late["versions"][0]["reason"]

    # Partial halt with open but no close on day 4 (real v3) -> partial_halt_missing_close
    ph_payload = json.loads(json.dumps(future_payload))
    ph_payload["data_kind"] = "real"
    ph_base = raw_row(ph_payload, future_sessions[4], "511260")
    ph_base["high"] = 10.0
    ph_base["low"] = 10.0
    ph_base["volume"] = 100
    ph_row = make_actual_open_halt(ph_payload, future_sessions[4], "511260", open_price=10.0)
    ph_row["close"] = None
    ph_row["close_available_at"] = None
    ph_body = {
        "schema_version": 1,
        "data_kind": "real",
        "version_id": cf_rel["release_id"],
        "date": future_sessions[4],
        "release": {"release_id": cf_rel["release_id"]},
        "artifact_hash": cf_rel["artifact"]["artifact_hash"],
        "execution": cf_rel["artifact"]["execution"],
        "release_state_at_signal_point": "paused",
        "previous": {
            **shadow._initial_prior(shadow._execution_settings(cf_rel["artifact"])),
            "positions": {
                **{s: 0.0 for s in shadow.ETF_SYMBOLS},
                "511260": 100.0,
            },
            "cash": 0.0,
            "nav": 1000.0,
            "carry_states": {
                **{s: {"anchor": None, "chain_broken": False} for s in shadow.ETF_SYMBOLS},
                "511260": {
                    "anchor": {
                        "source_close_date": future_sessions[3],
                        "source_close_price": 10.0,
                        "source_close_evidence": {},
                        "valuation_price": 10.0,
                        "carried_sessions": 0,
                        "adjustments": [],
                    },
                    "chain_broken": False,
                },
            },
        },
        "session": {
            "date": future_sessions[4],
            "next_session": future_sessions[5],
            "calendar": ph_payload["calendar"][4],
            "raw_bars": {s: raw_row(ph_payload, future_sessions[4], s) for s in shadow.ETF_SYMBOLS},
            "corporate_actions": [],
            "derived_window": {"sessions": [], "rows": []},
        },
        "anomalies": [],
        "corrections": [],
    }
    with pytest.raises(shadow._Blocked, match="partial_halt_missing_close"):
        shadow._compute_day(ph_body, shadow._execution_settings(cf_rel["artifact"]))

    # Zero-holding symbol (159612) suspended on day 3 does NOT block carry-forward
    zh_payload = json.loads(json.dumps(future_payload))
    make_confirmed_suspension(zh_payload, future_sessions[3], "159612")
    raw_row(zh_payload, future_sessions[3], "159612")["available_at"] = (
        f"{future_sessions[3]}T15:00:00+08:00"
    )
    zh_d = next(d for d in zh_payload["derived"] if d["date"] == future_sessions[3] and d["symbol"] == "159612")
    zh_d["value"] = 10.0
    zh_d["available_at"] = f"{future_sessions[3]}T15:00:00+08:00"
    zh_feed = write_feed(tmp_path / "zh_feed.json", zh_payload)
    res_zh, blocked_zh = shadow.update(
        tmp_path / "zh_ledger", cf_store, zh_feed, future_sessions[3], [cf_rel["release_id"]]
    )
    assert not blocked_zh, res_zh

    # Conflicting evidence, missing authorized prior close, and broken chain
    conflict_body = json.loads(json.dumps(ph_body))
    conflict_row = dict(raw_row(future_payload, future_sessions[4], "511260"))
    conflict_row["status_conflict"] = {
        "price_evidence_id": PRICE_EVIDENCE,
        "halt": {"start": f"{future_sessions[4]}T09:30:00+08:00", "end": f"{future_sessions[4]}T15:00:00+08:00"},
    }
    conflict_row["trading_status"] = "unknown"
    conflict_body["session"]["raw_bars"]["511260"] = conflict_row
    with pytest.raises(shadow._Blocked, match="conflicting_status_or_trade_evidence"):
        shadow._compute_day(conflict_body, shadow._execution_settings(cf_rel["artifact"]))

    no_prior_body = json.loads(json.dumps(ph_body))
    no_prior_body["session"]["raw_bars"]["511260"] = raw_row(future_payload, future_sessions[4], "511260")
    no_prior_body["previous"]["carry_states"]["511260"] = {"anchor": None, "chain_broken": False}
    with pytest.raises(shadow._Blocked, match="missing_authorized_prior_close"):
        shadow._compute_day(no_prior_body, shadow._execution_settings(cf_rel["artifact"]))

    broken_body = json.loads(json.dumps(no_prior_body))
    broken_body["previous"]["carry_states"]["511260"] = {"anchor": None, "chain_broken": True}
    with pytest.raises(shadow._Blocked, match="carry_forward_chain_broken"):
        shadow._compute_day(broken_body, shadow._execution_settings(cf_rel["artifact"]))


def test_shadow_backfill_never_rewrites_committed_days_and_detects_frozen_input_change(
    tmp_path: Path,
) -> None:
    payload, sessions = _setup_carry_forward_scenario(tmp_path)
    base = datetime(2030, 1, 2, tzinfo=UTC).date()
    date_map = {old: (base + timedelta(days=i)).isoformat() for i, old in enumerate(sessions)}
    text = json.dumps(payload)
    for old, new in date_map.items():
        text = text.replace(old, new)
    future_payload = json.loads(text)
    future_sessions = [date_map[s] for s in sessions]

    store = tmp_path / "store"
    ledger = tmp_path / "ledger"
    report_out = tmp_path / "report.json"
    rel = build_manual_tracking_release(store, future_sessions)
    rid = rel["release_id"]
    feed_path = write_feed(tmp_path / "feed.json", future_payload)

    # Commit through day 3
    res1, blocked1 = shadow.update(ledger, store, feed_path, future_sessions[3], [rid])
    assert not blocked1, res1
    day3_before = day_record(ledger, rid, report_out, future_sessions[3])

    # Backfill revised quote on already-committed day 3 -> day 3 is untouched
    revised = json.loads(json.dumps(future_payload))
    raw_row(revised, future_sessions[3], "511260")["close"] = 99.0
    raw_row(revised, future_sessions[3], "511260")["high"] = 99.0
    revised_feed = write_feed(tmp_path / "revised_feed.json", revised)
    res_rerun, blocked_rerun = shadow.update(ledger, store, revised_feed, future_sessions[3], [rid])
    assert not blocked_rerun
    assert res_rerun["versions"][0]["status"] == "already_committed"
    day3_after = day_record(ledger, rid, report_out, future_sessions[3])
    assert day3_after["result"]["nav"] == day3_before["result"]["nav"]

    # Freeze an uncommitted input on day 4, then mutate day 4 -> FROZEN_INPUT_CHANGED
    db = shadow.Ledger(ledger)
    prior = shadow._prior_from_ledger(db, rid, future_sessions[3])
    artifact = release.fetch(store, rid)["artifact"]
    execution = shadow._execution_settings(artifact)
    feed_obj = shadow.load_feed(feed_path)
    built, _, _ = shadow._build_input(feed_obj, artifact, execution, rid, 4, prior, "enabled")
    db.freeze_input(rid, future_sessions[4], built, shadow._hash(built))
    db.close()

    mutated = json.loads(json.dumps(future_payload))
    raw_row(mutated, future_sessions[4], "518880")["close"] = 12.34
    raw_row(mutated, future_sessions[4], "518880")["high"] = 12.34
    mutated_feed = write_feed(tmp_path / "mutated_feed.json", mutated)
    res_frozen, blocked_frozen = shadow.update(ledger, store, mutated_feed, future_sessions[4], [rid])
    assert blocked_frozen
    assert res_frozen["versions"][0]["reason_code"] == "FROZEN_INPUT_CHANGED"


def test_synthetic_v3_partial_halt_resumed_and_full_day_parity(tmp_path: Path) -> None:
    """Synthetic schema-3 feed supports resumed partial_halt close & full-day partial_halt carry-forward."""
    payload = _v3_feed_from_fixture(8, data_kind="synthetic")
    sessions = [row["date"] for row in payload["calendar"]]
    for idx, day in enumerate(sessions):
        for sym in shadow.ETF_SYMBOLS:
            row = raw_row(payload, day, sym)
            row["open"] = 10.0
            row["high"] = 10.0
            row["low"] = 10.0
            row["close"] = 10.0
            row["open_available_at"] = f"{day}T09:30:00+08:00"
            row["close_available_at"] = f"{day}T15:00:00+08:00"
            row["available_at"] = f"{day}T09:30:00+08:00"
            d_row = next(
                d for d in payload["derived"] if d["date"] == day and d["symbol"] == sym
            )
            d_row["value"] = 10.0 + (0.5 * idx if sym == "511260" else 0.0)
            d_row["available_at"] = f"{day}T15:00:00+08:00"

    # Day 4: resumed partial_halt on held 511260 (halt 09:30-10:30, close=10.20)
    resumed = make_actual_open_halt(payload, sessions[4], "511260", open_price=10.10)
    resumed["high"] = 10.20
    resumed["low"] = 10.10
    resumed["close"] = 10.20
    resumed["volume"] = 500.0
    resumed["close_available_at"] = f"{sessions[4]}T15:00:00+08:00"
    resumed["available_at"] = f"{sessions[4]}T15:00:00+08:00"

    # Day 5: full-day partial_halt on held 511260 (halt 09:30-15:00, null OHLC, volume=0, status_evidence_id)
    full_ph = raw_row(payload, sessions[5], "511260")
    full_ph["trading_status"] = "partial_halt"
    full_ph["halt"] = {
        "start": f"{sessions[5]}T09:30:00+08:00",
        "end": f"{sessions[5]}T15:00:00+08:00",
    }
    full_ph["open"] = None
    full_ph["high"] = None
    full_ph["low"] = None
    full_ph["close"] = None
    full_ph["volume"] = 0.0
    full_ph["open_source"] = None
    full_ph["status_evidence_id"] = STATUS_EVIDENCE
    full_ph["available_at"] = f"{sessions[5]}T15:00:00+08:00"
    full_ph["close_available_at"] = None

    # Day 6: resumed normal trading close 10.50
    raw_row(payload, sessions[6], "511260").update(open=10.40, high=10.50, low=10.40, close=10.50)

    # Re-anchor to future dates so orders persist before 09:30 open deadline
    base = datetime(2031, 1, 2, tzinfo=UTC).date()
    date_map = {old: (base + timedelta(days=i)).isoformat() for i, old in enumerate(sessions)}
    text = json.dumps(payload)
    for old, new in date_map.items():
        text = text.replace(old, new)
    future_payload = json.loads(text)
    future_sessions = [date_map[s] for s in sessions]

    store = tmp_path / "ph_store"
    ledger = tmp_path / "ph_ledger"
    report_out = tmp_path / "ph_report.json"
    rel = build_manual_tracking_release(store, future_sessions)
    rid = rel["release_id"]
    feed_path = write_feed(tmp_path / "ph_feed.json", future_payload)

    res3, blocked3 = shadow.update(ledger, store, feed_path, future_sessions[3], [rid])
    assert not blocked3, res3
    release.pause(store, rid, future_sessions[4], "hold position through partial_halt days")
    res6, blocked6 = shadow.update(ledger, store, feed_path, future_sessions[6], [rid])
    assert not blocked6, res6

    d4 = day_record(ledger, rid, report_out, future_sessions[4])["result"]
    assert d4["positions"]["511260"]["valuation_basis"] == "actual_close"
    assert d4["positions"]["511260"]["close"] == pytest.approx(10.20)
    assert d4["nav"] == pytest.approx(1020.0)

    d5 = day_record(ledger, rid, report_out, future_sessions[5])["result"]
    assert d5["positions"]["511260"]["valuation_basis"] == "carried_forward_prior_close"
    assert d5["positions"]["511260"]["close"] is None
    assert d5["positions"]["511260"]["valuation_price"] == pytest.approx(10.20)
    assert d5["nav"] == pytest.approx(1020.0)

    d6 = day_record(ledger, rid, report_out, future_sessions[6])["result"]
    assert d6["positions"]["511260"]["valuation_basis"] == "actual_close"
    assert d6["nav"] == pytest.approx(1050.0)

    # Discovery parity on identical synthetic schema-3 payload
    disc = snapshot_baseline.run_discovery_strategy(
        future_payload,
        "a" * 64,
        "b" * 64,
        strategy=make_strategy(
            {"family": "momentum", "parameters": {"lookback": 2, "top_k": 1, "cash_if_negative": False}}
        ),
        data_kind="synthetic",
        initial_cash=1000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
        signal_window_sessions=3,
        execution_rule_version=ACTUAL_OPEN_RULES,
        valuation_policy_version=CARRY_FORWARD_SUSPENDED_VALUATION,
    )
    curve_by_date = {pt["date"]: pt for pt in disc["equity_curve"]}
    for s in future_sessions[4:7]:
        s_day = day_record(ledger, rid, report_out, s)["result"]
        assert curve_by_date[s]["nav"] == pytest.approx(s_day["nav"])
        assert curve_by_date[s]["market_value"] == pytest.approx(s_day["market_value"])
        assert curve_by_date[s]["cash"] == pytest.approx(s_day["cash"])

