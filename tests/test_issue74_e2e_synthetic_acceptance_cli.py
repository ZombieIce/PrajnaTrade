"""Full E2E synthetic acceptance test for Spec #74 (#75-#79) via public CLI."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from cli_helpers import agent_cli, cli, cli_env, ok, write_json
from test_actual_open_cli import (
    OPEN_SOURCE,
    PRICE_EVIDENCE,
    STATUS_EVIDENCE,
    make_confirmed_suspension,
    schema_v3_fixture,
)
from test_evidence_loop_cli import shadow_compare
from test_formal_validation_cli import v2_round_input
from test_research_loop_cli import open_round, round_status, submit_version, version_input
from test_shadow_cli import shadow_update
from test_version_review_cli import approve_version, local_today, observation_input

from prajnatrade.trading_status import CASH, ETF_SYMBOLS

SYM_A, SYM_B, SYM_C = ETF_SYMBOLS[0], ETF_SYMBOLS[1], ETF_SYMBOLS[2]
INITIAL_CASH = 100_000.0
FEE_BPS = 5
FEE_RATE = 0.0005
LOT_SIZE = 100


def _month_boundary_forward_sessions() -> tuple[list[str], list[str]]:
    """Return 2 warmup dates + 7 active forward dates spanning a calendar month boundary.

    Layout of active dates (D1..D7):
      W1, W2  : pre-enablement warmup (month M)
      D1      : M-end-1 (month M)
      D2      : M-end   (month M; month-end monthly signal crosses into M+1)
      D3..D7  : M+1-01 .. M+1-05 (month M+1)
    """
    today = local_today()
    first_next_month = date(today.year + (1 if today.month == 12 else 0), (today.month % 12) + 1, 1)
    # Ensure W1..W2 and D1..D2 are strictly after today so approval always starts at D1.
    while (first_next_month - timedelta(days=4)) <= today:
        first_next_month = date(
            first_next_month.year + (1 if first_next_month.month == 12 else 0),
            (first_next_month.month % 12) + 1,
            1,
        )
    warmup = [
        (first_next_month - timedelta(days=4)).isoformat(),
        (first_next_month - timedelta(days=3)).isoformat(),
    ]
    active = [
        (first_next_month - timedelta(days=2)).isoformat(),  # D1 (M-end-1)
        (first_next_month - timedelta(days=1)).isoformat(),  # D2 (M-end)
        (first_next_month + timedelta(days=0)).isoformat(),  # D3 (M+1-01)
        (first_next_month + timedelta(days=1)).isoformat(),  # D4 (M+1-02)
        (first_next_month + timedelta(days=2)).isoformat(),  # D5 (M+1-03)
        (first_next_month + timedelta(days=3)).isoformat(),  # D6 (M+1-04)
        (first_next_month + timedelta(days=4)).isoformat(),  # D7 (M+1-05)
    ]
    return warmup, active


def _write_stateful_plugin(plugin_dir: Path) -> tuple[Path, Path]:
    """Thin wrapper importing local stateful logic module (dependency-fingerprinted)."""
    plugin_dir.mkdir(parents=True, exist_ok=True)
    helper = plugin_dir / "stateful_logic.py"
    helper.write_text(
        "from prajnatrade.trading_status import CASH, ETF_SYMBOLS\n"
        "SYM_A, SYM_B, SYM_C = ETF_SYMBOLS[0], ETF_SYMBOLS[1], ETF_SYMBOLS[2]\n"
        "\n"
        "def compute_target(history, state):\n"
        "    for k in (\n"
        "        'signal_date', 'month_session', 'week_start', 'rebalance_anchor_date',\n"
        "        'previous_order_attempted', 'last_filled_target', 'last_monthly_filled_target',\n"
        "        'prior_close_weights', 'cash', 'shares', 'equity',\n"
        "    ):\n"
        "        if k not in state:\n"
        "            raise ValueError(f'Missing state field: {k}')\n"
        "    d = state['signal_date']\n"
        "    if state['rebalance_anchor_date'] is None:\n"
        "        return {'weights': {SYM_A: 0.20, CASH: 0.80}, 'purpose': 'initial', 'cycle': d[:7]}\n"
        "    # Month-end signal (fires once after initial fill on next session in same month)\n"
        "    if state['last_filled_target'].get(SYM_B, 0.0) == 0.0 and not state.get('deferred_monthly'):\n"
        "        return {\n"
        "            'weights': {SYM_A: 0.0, SYM_B: 0.50, SYM_C: 0.30, CASH: 0.20},\n"
        "            'purpose': 'monthly',\n"
        "            'cycle': d[:7],\n"
        "        }\n"
        "    # Deferred monthly recovery: wait until month_session >= 3, then emit zero-trade target\n"
        "    if state.get('deferred_monthly') and state['month_session'] >= 3:\n"
        "        return {\n"
        "            'weights': {SYM_B: 0.5005, SYM_C: 0.2995, CASH: 0.20},\n"
        "            'purpose': 'monthly',\n"
        "            'cycle': d[:7],\n"
        "        }\n"
        "    # Daily risk_control after deferred monthly filled (month_session == 4)\n"
        "    if state['month_session'] == 4 and state['last_filled_target'].get(SYM_B, 0.0) > 0.4:\n"
        "        return {\n"
        "            'weights': {SYM_B: 0.30, SYM_C: 0.30, CASH: 0.40},\n"
        "            'purpose': 'risk_control',\n"
        "            'cycle': d[:7],\n"
        "        }\n"
        "    return None\n",
        encoding="utf-8",
    )
    entrypoint = plugin_dir / "wrapper.py"
    entrypoint.write_text(
        "import stateful_logic\n"
        "\n"
        "def make_strategy(params):\n"
        "    def strategy(history, state):\n"
        "        return stateful_logic.compute_target(history, state)\n"
        "    return strategy\n",
        encoding="utf-8",
    )
    config = plugin_dir / "plugins.json"
    write_json(
        config,
        {
            "plugins": [
                {
                    "family": "synth_stateful_monthly",
                    "entrypoint": f"{entrypoint}:make_strategy",
                    "declared_dependencies": [str(helper)],
                }
            ]
        },
    )
    return config, helper


def _build_schema3_e2e_snapshot(session_count: int = 17) -> tuple[dict, list[str]]:
    payload = schema_v3_fixture(session_count)
    payload["corporate_actions"] = []
    sessions = [row["date"] for row in payload["calendar"]]
    # Confirmed suspension of ETF_SYMBOLS[4] in trailing segment (sessions[9])
    make_confirmed_suspension(payload, sessions[9], ETF_SYMBOLS[4])
    for row in payload["raw_bars"]:
        if row.get("trading_status") == "trading":
            row.update(open=10.0, high=10.0, low=10.0, close=10.0)
    return payload, sessions


def _build_forward_schema3_feed(warmup: list[str], active: list[str]) -> dict:
    """Build deterministic forward schema-3 feed covering all Spec #74 positive scenarios."""
    all_days = warmup + active
    d1, d2, d3, d4, d5, d6, d7 = active
    raw_bars: list[dict] = []
    derived: list[dict] = []
    for idx, d in enumerate(all_days):
        for sym in ETF_SYMBOLS:
            # D4 & D5: SYM_C confirmed whole-day suspension with dividend ex_date=D4, pay_date=D5
            if d in (d4, d5) and sym == SYM_C:
                raw_bars.append(
                    {
                        "date": d,
                        "symbol": sym,
                        "open": None,
                        "high": None,
                        "low": None,
                        "close": None,
                        "volume": None,
                        "trading_status": "suspended",
                        "status_evidence_id": STATUS_EVIDENCE,
                        "available_at": f"{d}T15:00:00+08:00",
                        "open_available_at": f"{d}T09:30:00+08:00",
                        "close_available_at": f"{d}T15:00:00+08:00",
                        "availability_evidence": "fixture_synth",
                    }
                )
            # D3: SYM_A resumed from intraday halt with trusted actual open = 12.50
            elif d == d3 and sym == SYM_A:
                raw_bars.append(
                    {
                        "date": d,
                        "symbol": sym,
                        "open": 12.50,
                        "high": 12.50,
                        "low": 12.50,
                        "close": 12.50,
                        "volume": 5000.0,
                        "trading_status": "partial_halt",
                        "halt": {"start": f"{d}T09:30:00+08:00", "end": f"{d}T10:30:00+08:00"},
                        "open_source": {"source": OPEN_SOURCE["source"], "evidence_id": PRICE_EVIDENCE},
                        "status_evidence_id": STATUS_EVIDENCE,
                        "available_at": f"{d}T15:00:00+08:00",
                        "open_available_at": f"{d}T15:05:00+08:00",
                        "close_available_at": f"{d}T15:00:00+08:00",
                        "availability_evidence": "fixture_synth",
                    }
                )
            else:
                # D2: SYM_A gaps up to 12.00 on open & close (proves open-time lot sizing!)
                if d == d2 and sym == SYM_A:
                    px_o, px_c = 12.0, 12.0
                # D6: SYM_C resumes at 9.80 open, closes at 10.00 (no double-adjustment!)
                elif d == d6 and sym == SYM_C:
                    px_o, px_c = 9.80, 10.00
                else:
                    px_o, px_c = 10.0, 10.0
                raw_bars.append(
                    {
                        "date": d,
                        "symbol": sym,
                        "open": px_o,
                        "high": max(px_o, px_c),
                        "low": min(px_o, px_c),
                        "close": px_c,
                        "volume": 10000.0,
                        "trading_status": "trading",
                        "open_source": {"source": OPEN_SOURCE["source"], "evidence_id": PRICE_EVIDENCE},
                        "available_at": f"{d}T15:00:00+08:00",
                        "open_available_at": f"{d}T09:30:00+08:00",
                        "close_available_at": f"{d}T15:00:00+08:00",
                        "availability_evidence": "fixture_synth",
                    }
                )
            # D3: ETF_SYMBOLS[4] derived arrives LATE (16:00 > 15:30 cutoff) -> pauses ONLY D3 new signal!
            drv_ts = (
                f"{d}T16:00:00+08:00"
                if (d == d3 and sym == ETF_SYMBOLS[4])
                else f"{d}T15:00:00+08:00"
            )
            derived.append(
                {
                    "date": d,
                    "symbol": sym,
                    "value": 10.0 + 0.05 * idx + (0.02 if sym == SYM_B else 0.0),
                    "available_at": drv_ts,
                    "availability_evidence": "fixture_synth",
                }
            )
    return {
        "schema_version": 3,
        "kind": "shadow_feed",
        "data_kind": "real",
        "raw_price_basis": "unadjusted",
        "symbols": list(ETF_SYMBOLS),
        "calendar": [
            {
                "date": d,
                "available_at": f"{all_days[0]}T00:00:00+08:00",
                "availability_evidence": "fixture_cal",
            }
            for d in all_days
        ],
        "evidence": {
            "items": [
                {
                    "id": PRICE_EVIDENCE,
                    "kind": "price_archive",
                    "reference": "synthetic:tdx-unadjusted@fwd",
                    "sha256": "b" * 64,
                    "available_at": f"{all_days[0]}T08:00:00+08:00",
                },
                {
                    "id": STATUS_EVIDENCE,
                    "kind": "suspension_notice",
                    "reference": "synthetic:halt-notices@fwd",
                    "sha256": "c" * 64,
                    "available_at": f"{all_days[0]}T08:00:00+08:00",
                },
            ]
        },
        "raw_bars": raw_bars,
        "derived": derived,
        "corporate_actions": [
            {
                "action_id": "act-div-symc-d4",
                "date": d4,
                "symbol": SYM_C,
                "type": "cash_dividend",
                "cash_per_share": 0.20,
                "pay_date": d5,
                "sequence": 1,
                "terms_evidence": "synth_div_notice",
                "available_at": f"{d4}T08:00:00+08:00",
                "availability_evidence": "fixture_synth",
            }
        ],
    }


def test_spec74_full_e2e_synthetic_acceptance_cli(tmp_path: Path) -> None:
    plugin_config, helper_path = _write_stateful_plugin(tmp_path / "plugins")
    snap_payload, sessions = _build_schema3_e2e_snapshot(17)
    snap_input = write_json(tmp_path / "snap-input.json", snap_payload)
    snap_store = tmp_path / "snap-store"
    project = tmp_path / "project"
    trusted = tmp_path / "trusted"

    # 1. Seal schema-3 snapshot
    sealed = ok(cli("snapshot", "seal", "--input", str(snap_input), "--store", str(snap_store)))
    snapshot_id = sealed["snapshot_id"]

    # 2. Init project & register local plugin (operator only; agent forbidden)
    ok(cli("project", "init", "--project", str(project)))
    agent_reg = agent_cli(
        "plugin", "register", "--project", str(project), "--plugin-config", str(plugin_config),
    )
    assert agent_reg.returncode != 0
    reg = ok(
        cli(
            "plugin", "register",
            "--project", str(project),
            "--plugin-config", str(plugin_config),
        )
    )
    assert reg["plugins"][0]["family"] == "synth_stateful_monthly"

    # 3. Create Campaign with all Spec #74 opt-in policies (lot=100, fee=5bps)
    campaign = ok(
        cli(
            "campaign", "create",
            "--project", str(project),
            "--store", str(snap_store),
            "--snapshot-id", snapshot_id,
            "--attempts", "4",
            "--max-seconds", "30",
            "--initial-cash", str(int(INITIAL_CASH)),
            "--transaction-cost-bps", str(FEE_BPS),
            "--slippage-bps", "0",
            "--execution-rules", "actual-open-lot-v1",
            "--valuation-policy", "carry-forward-suspended-v1",
            "--decision-policy", "after-close-common-v1",
            "--monthly-deferral",
            "--lot-size", str(LOT_SIZE),
            "--discovery-warmup-sessions", "2",
            "--discovery-split", "70/30",
        )
    )
    campaign_id = campaign["campaign_id"]

    # Negative control: unregistered plugin family rejected before execution
    unreg_req = write_json(
        tmp_path / "exp-unreg.json",
        {
            "schema_version": 1,
            "campaign_id": campaign_id,
            "submission_id": "sub-unreg",
            "hypothesis": "Unregistered family must fail",
            "strategy": {"family": "nonexistent_plugin"},
            "parameters": {},
        },
    )
    unreg_res = cli("experiment", "submit", "--project", str(project), "--input", str(unreg_req))
    assert unreg_res.returncode != 0

    # 4. Submit Discovery experiments: V1 (local stateful plugin) and V2 (built-in equal_weight comparator)
    exp_v1 = ok(
        cli(
            "experiment", "submit",
            "--project", str(project),
            "--plugin-config", str(plugin_config),
            "--input", str(
                write_json(
                    tmp_path / "exp-v1.json",
                    {
                        "schema_version": 1,
                        "campaign_id": campaign_id,
                        "submission_id": "sub-e2e-v1",
                        "hypothesis": "Spec 74 E2E stateful monthly plugin",
                        "strategy": {"family": "synth_stateful_monthly"},
                        "parameters": {},
                    },
                )
            ),
        )
    )
    assert exp_v1["status"] == "succeeded"

    exp_v2 = ok(
        cli(
            "experiment", "submit",
            "--project", str(project),
            "--input", str(
                write_json(
                    tmp_path / "exp-v2.json",
                    {
                        "schema_version": 1,
                        "campaign_id": campaign_id,
                        "submission_id": "sub-e2e-v2",
                        "hypothesis": "Spec 74 built-in equal_weight control",
                        "strategy": {"family": "equal_weight"},
                        "parameters": {},
                    },
                )
            ),
        )
    )
    assert exp_v2["status"] == "succeeded"

    # 5. Open round (auto limit=2), submit V1 & V2, formal validate, platform-fix revalidate V1
    opened = ok(
        open_round(
            project,
            trusted,
            campaign_id,
            write_json(tmp_path / "round.json", v2_round_input(limit=2)),
        )
    )
    round_id = opened["round_id"]
    ver_v1 = ok(submit_version(project, round_id, "ver-1", write_json(tmp_path / "ver-1.json", version_input(exp_v1["experiment_id"]))))
    ver_v2 = ok(submit_version(project, round_id, "ver-2", write_json(tmp_path / "ver-2.json", version_input(exp_v2["experiment_id"]))))
    v1_id, v2_id = ver_v1["version_id"], ver_v2["version_id"]

    val_v1 = ok(
        cli(
            "round", "validate",
            "--project", str(project),
            "--store", str(trusted),
            "--snapshot-store", str(snap_store),
            "--round-id", round_id,
            "--version-id", v1_id,
            "--plugin-config", str(plugin_config),
        )
    )
    assert val_v1["outcome"] == "passed"
    assert val_v1["usage"]["sequence"] == 1

    # Platform-fix revalidation with identical plugin & frozen rules -> zero allowance increment
    reval_ok = ok(
        cli(
            "round", "revalidate",
            "--project", str(project),
            "--store", str(trusted),
            "--snapshot-store", str(snap_store),
            "--round-id", round_id,
            "--version-id", v1_id,
            "--reason", "platform_fix_check",
            "--plugin-config", str(plugin_config),
        )
    )
    assert reval_ok["outcome"] == "passed"
    assert ok(round_status(project, round_id))["validation_allowance"]["automatic_used"] == 1

    val_v2 = ok(
        cli(
            "round", "validate",
            "--project", str(project),
            "--store", str(trusted),
            "--snapshot-store", str(snap_store),
            "--round-id", round_id,
            "--version-id", v2_id,
        )
    )
    assert val_v2["outcome"] == "passed"
    assert val_v2["usage"]["sequence"] == 2

    # 6. Approve V1 & V2 for manual_tracking on forward calendar spanning month boundary
    warmup, active = _month_boundary_forward_sessions()
    d1, d2, d3, d4, d5, d6, d7 = active
    cal_obj = {
        "schema_version": 1,
        "data_kind": "real",
        "source": "synthetic operator trading calendar",
        "sessions": active,
    }
    cal_path = write_json(tmp_path / "calendar.json", cal_obj)
    obs_path = write_json(tmp_path / "obs.json", observation_input())
    app_v1 = ok(approve_version(project, trusted, round_id, v1_id, cal_path, obs_path))
    app_v2 = ok(approve_version(project, trusted, round_id, v2_id, cal_path, obs_path))
    rel_v1 = app_v1["tracking"]["release_id"]
    rel_v2 = app_v2["tracking"]["release_id"]

    # 7. Verify shadow export-feed from Discovery payload (zero future leakage)
    exported_path = tmp_path / "exported-feed.json"
    exported = ok(
        cli(
            "shadow", "export-feed",
            "--store", str(snap_store),
            "--snapshot-id", snapshot_id,
            "--through-date", sessions[5],
            "--output", str(exported_path),
        )
    )
    assert exported["schema_version"] == 3
    exported_body = json.loads(exported_path.read_text(encoding="utf-8"))
    assert max(row["date"] for row in exported_body["calendar"]) == sessions[5]

    # 8. Day-by-day Shadow Live updates on forward schema-3 feed
    fwd_feed = _build_forward_schema3_feed(warmup, active)
    feed_path = write_json(tmp_path / "shadow-feed.json", fwd_feed)
    ledger_dir = tmp_path / "ledger"
    corrections_dir = tmp_path / "corrections"
    corrections_dir.mkdir(exist_ok=True)

    plugin_env = cli_env(PRAJNATRADE_PLUGIN_CONFIG=str(plugin_config))

    # Pre-enablement warmup day W2 -> not_started (never produces pre-approval committed days)
    pre = ok(
        shadow_update(
            ledger_dir,
            trusted,
            feed_path,
            warmup[1],
            release_ids=[rel_v1, rel_v2],
            corrections_store=corrections_dir,
            env=plugin_env,
            at_close=True,
        )
    )
    assert len(pre["versions"]) == 2
    assert all(r["status"] == "not_started" for r in pre["versions"])

    # Step through D1..D7 day by day (realistic daily delivery & clock advancement)
    for session_day in active:
        upd = ok(
            shadow_update(
                ledger_dir,
                trusted,
                feed_path,
                session_day,
                corrections_store=corrections_dir,
                env=plugin_env,
                at_close=True,
            )
        )
        assert len(upd["versions"]) == 2
        assert all(r["target_date"] == session_day for r in upd["versions"])

    # Replay D7 -> already_committed, zero double-fill, zero double anchor advance
    replay = ok(
        shadow_update(
            ledger_dir,
            trusted,
            feed_path,
            d7,
            corrections_store=corrections_dir,
            env=plugin_env,
            at_close=True,
        )
    )
    assert all(r["status"] == "already_committed" for r in replay["versions"])

    # 9. Hand-reconcile V1 day-by-day report against exact arithmetic
    rep_v1_path = tmp_path / "report-v1.json"
    rep_v2_path = tmp_path / "report-v2.json"
    rep_v1 = ok(cli("shadow", "report", "--ledger", str(ledger_dir), "--version", rel_v1, "--output", str(rep_v1_path)))
    rep_v2 = ok(cli("shadow", "report", "--ledger", str(ledger_dir), "--version", rel_v2, "--output", str(rep_v2_path)))
    assert rep_v1["days"] == 7
    assert rep_v2["days"] == 7

    days_v1 = {d["date"]: d["result"] for d in json.loads(rep_v1_path.read_text(encoding="utf-8"))["days"]}

    # D1 (M-end-1): initial signal emitted after close; no fills yet; NAV = 100,000
    assert days_v1[d1]["nav"] == pytest.approx(100_000.0, abs=1e-8)
    assert days_v1[d1]["signal"]["purpose"] == "initial"
    assert days_v1[d1]["next_orders"][0]["order_sizing_mode"] == "open_weight"
    assert days_v1[d1]["next_orders"][0]["submitted_at"] == f"{d1}T15:30:00+08:00"
    assert days_v1[d1]["strategy_state"]["rebalance_anchor_date"] is None

    # D2 (M-end): SYM_A gaps to 12.00 on open!
    # 20% of 100,000 = 20,000; lot_cost = 100 * 12.0 * 1.0005 = 1200.60 -> 16 lots = 1600 shares!
    # notional = 19,200.0, fee = 9.60, cash = 80,790.40, SYM_A MV = 19,200.0, NAV = 99,990.40
    assert days_v1[d2]["positions"][SYM_A]["quantity"] == pytest.approx(1600.0, abs=1e-8)
    assert days_v1[d2]["fees"]["day"] == pytest.approx(9.60, abs=1e-8)
    assert days_v1[d2]["cash"] == pytest.approx(80_790.40, abs=1e-8)
    assert days_v1[d2]["nav"] == pytest.approx(99_990.40, abs=1e-8)
    assert days_v1[d2]["batch_complete"] is True
    assert days_v1[d2]["residual_reason"] == "lot_rounding_residual"
    assert days_v1[d2]["strategy_state"]["rebalance_anchor_date"] == d1
    # D2 close emits month-end monthly signal crossing into M+1
    assert days_v1[d2]["signal"]["purpose"] == "monthly"
    assert days_v1[d2]["signal"]["cycle"] == d2[:7]
    assert days_v1[d2]["signal"]["execution_date"] == d3

    # D3 (M+1-01):
    # Open: SYM_A resumed from partial_halt at 12.50 -> sells 1600 shares FIRST!
    # open_equity = 80,790.40 + 1600 * 12.50 = 100,790.40
    # sell proceeds = 20,000 - 10.0 fee = 19,990.0 -> available cash = 100,780.40
    # SYM_B target = 0.50 * 100,790.40 = 50,395.20 -> lot_cost = 1000.5 -> 50 lots = 5000 shares (fee 25.0)
    # SYM_C target = 0.30 * 100,790.40 = 30,237.12 -> lot_cost = 1000.5 -> 30 lots = 3000 shares (fee 15.0)
    # cash = 100,780.40 - 50,025.0 - 30,015.0 = 20,740.40; total day fee = 50.0; NAV = 100,740.40
    assert days_v1[d3]["positions"][SYM_A]["quantity"] == pytest.approx(0.0, abs=1e-8)
    assert days_v1[d3]["positions"][SYM_B]["quantity"] == pytest.approx(5000.0, abs=1e-8)
    assert days_v1[d3]["positions"][SYM_C]["quantity"] == pytest.approx(3000.0, abs=1e-8)
    assert days_v1[d3]["fees"]["day"] == pytest.approx(50.0, abs=1e-8)
    assert days_v1[d3]["cash"] == pytest.approx(20_740.40, abs=1e-8)
    assert days_v1[d3]["nav"] == pytest.approx(100_740.40, abs=1e-8)
    assert days_v1[d3]["daily_simulation_assumptions"]["late_resume_sell_funds_same_day_buy"] is True
    # Month-end signal attribution kept at d2 (month M)
    assert days_v1[d3]["strategy_state"]["rebalance_anchor_date"] == d2
    # D3 close: SYM_B close arrived at 16:00 > 15:30 -> signal paused, triggering deferral!
    assert days_v1[d3]["signal_status"] == "paused"
    assert days_v1[d3]["signal"] is None
    assert days_v1[d3]["strategy_state"]["active_deferral"]["scheduled_date"] == d3

    # D4 (M+1-02): SYM_C confirmed whole-day suspension + ex_date cash dividend 0.20/share
    # SYM_C close=None, valuation_price=9.80, MV=29,400.0, receivable=600.0 -> NAV preserved at 100,740.40!
    assert days_v1[d4]["positions"][SYM_C]["close"] is None
    assert days_v1[d4]["positions"][SYM_C]["valuation_price"] == pytest.approx(9.80, abs=1e-8)
    assert days_v1[d4]["positions"][SYM_C]["market_value"] == pytest.approx(29_400.0, abs=1e-8)
    assert days_v1[d4]["valuation_basis"] == "carried_forward"
    assert days_v1[d4]["cash"] == pytest.approx(20_740.40, abs=1e-8)
    assert days_v1[d4]["nav"] == pytest.approx(100_740.40, abs=1e-8)

    # D5 (M+1-03): SYM_C STILL suspended, pay_date transfers 600.0 receivable -> cash!
    # cash = 21,340.40, SYM_C MV = 29,400.0, NAV = 100,740.40!
    # After-close: deferred monthly signal emitted (scheduled_date=D3, actual_signal_date=D5)
    assert days_v1[d5]["positions"][SYM_C]["close"] is None
    assert days_v1[d5]["positions"][SYM_C]["valuation_price"] == pytest.approx(9.80, abs=1e-8)
    assert days_v1[d5]["cash"] == pytest.approx(21_340.40, abs=1e-8)
    assert days_v1[d5]["nav"] == pytest.approx(100_740.40, abs=1e-8)
    assert days_v1[d5]["signal"]["purpose"] == "monthly"
    assert days_v1[d5]["signal"]["scheduled_date"] == d3
    assert days_v1[d5]["signal"]["actual_signal_date"] == d5
    assert days_v1[d5]["signal"]["deferral_reason"]

    # D6 (M+1-04): SYM_C resumes at 9.80 open, closes at 10.00 (no double-adjustment!)
    # Deferred monthly target {SYM_B: 0.5005, SYM_C: 0.2995} vs held 5000@10.0 & 3000@9.80:
    # open_equity = 21,340.40 + 50,000 + 29,400 = 100,740.40
    # SYM_B target_val = 50,420.57 (delta +420.57 < 1000.5 lot -> 0 lots!)
    # SYM_C target_val = 30,171.75 (delta +771.75 < 980.49 lot -> 0 lots!)
    # Zero-trade satisfied target -> advances anchor to D5!
    assert days_v1[d6]["zero_trade_satisfied"] is True
    assert days_v1[d6]["batch_complete"] is True
    assert days_v1[d6]["fills"] == []
    assert days_v1[d6]["positions"][SYM_C]["close"] == pytest.approx(10.0, abs=1e-8)
    assert days_v1[d6]["valuation_basis"] == "actual_close"
    assert days_v1[d6]["nav"] == pytest.approx(101_340.40, abs=1e-8)
    assert days_v1[d6]["strategy_state"]["rebalance_anchor_date"] == d5
    assert pytest.approx(days_v1[d6]["strategy_state"]["last_monthly_filled_target"][SYM_B], abs=1e-12) == 0.5005
    # D6 close emits risk_control switch
    assert days_v1[d6]["signal"]["purpose"] == "risk_control"

    # D7 (M+1-05): risk_control fills at open:
    # open_equity = 21,340.40 + 50,000 + 30,000 = 101,340.40
    # SYM_B target_val = 0.30 * 101,340.40 = 30,402.12; delta = -19,597.88 -> floor(1959.788/100) = 19 lots = 1900 shares
    # sells 1900 shares @ 10.0 (notional 19,000, fee 9.50) -> 3100 shares held, cash = 40,330.90, NAV = 101,330.90
    # Updates rebalance_anchor_date=D6 and last_filled_target[SYM_B]=0.30,
    # while last_monthly_filled_target STAYS at 0.5005!
    assert days_v1[d7]["positions"][SYM_B]["quantity"] == pytest.approx(3100.0, abs=1e-8)
    assert days_v1[d7]["positions"][SYM_C]["quantity"] == pytest.approx(3000.0, abs=1e-8)
    assert days_v1[d7]["fees"]["day"] == pytest.approx(9.50, abs=1e-8)
    assert days_v1[d7]["cash"] == pytest.approx(40_330.90, abs=1e-8)
    assert days_v1[d7]["nav"] == pytest.approx(101_330.90, abs=1e-8)
    assert days_v1[d7]["strategy_state"]["rebalance_anchor_date"] == d6
    assert pytest.approx(days_v1[d7]["strategy_state"]["last_filled_target"][SYM_B], abs=1e-12) == 0.30
    assert pytest.approx(days_v1[d7]["strategy_state"]["last_monthly_filled_target"][SYM_B], abs=1e-12) == 0.5005

    # 10. Reuse existing shadow compare across V1 & V2 on their common 7-day interval
    cmp_path = tmp_path / "compare.json"
    ok(shadow_compare(ledger_dir, feed_path, (rel_v1, rel_v2), cmp_path))
    cmp_doc = json.loads(cmp_path.read_text(encoding="utf-8"))
    assert cmp_doc["common_interval"]["sessions"] == 7
    assert rel_v1 in cmp_doc["per_version"]
    assert rel_v2 in cmp_doc["per_version"]

    # 11. Single-mutation negative counterexamples:
    # (a) Tampering ONLY imported implementation file behind thin wrapper -> rejected, committed ledger untouched
    original_helper = helper_path.read_text(encoding="utf-8")
    helper_path.write_text(original_helper + "\n# tampered\n", encoding="utf-8")
    tampered = cli(
        "round", "revalidate",
        "--project", str(project),
        "--store", str(trusted),
        "--snapshot-store", str(snap_store),
        "--round-id", round_id,
        "--version-id", v1_id,
        "--reason", "tampered_check",
        "--plugin-config", str(plugin_config),
    )
    assert tampered.returncode != 0
    tampered_body = json.loads(tampered.stdout)
    assert (
        "PLUGIN_CONTENT_CHANGED" in tampered_body["error"]["code"]
        or "REVALIDATION_INELIGIBLE" in tampered_body["error"]["code"]
    )
    # Committed Shadow days remain byte-for-byte verifiable even after plugin tampering
    rep_after = ok(cli("shadow", "report", "--ledger", str(ledger_dir), "--version", rel_v1, "--output", str(tmp_path / "report-after.json")))
    assert rep_after["days"] == 7

    # (b) Unresolved valuation gap (single mutation: remove SYM_C's status_evidence_id on D4 in a fresh ledger)
    helper_path.write_text(original_helper, encoding="utf-8")
    bad_val_feed = json.loads(json.dumps(fwd_feed))
    for row in bad_val_feed["raw_bars"]:
        if row["date"] == d4 and row["symbol"] == SYM_C:
            row["trading_status"] = "unknown"
            row.pop("status_evidence_id", None)
    bad_val_path = write_json(tmp_path / "bad-val-feed.json", bad_val_feed)
    bad_ledger = tmp_path / "ledger-bad-val"
    for day in (d1, d2, d3):
        ok(
            shadow_update(
                bad_ledger,
                trusted,
                bad_val_path,
                day,
                corrections_store=corrections_dir,
                env=plugin_env,
                at_close=True,
            )
        )
    blocked_d4_res = shadow_update(
        bad_ledger,
        trusted,
        bad_val_path,
        d4,
        corrections_store=corrections_dir,
        env=plugin_env,
        at_close=True,
    )
    assert blocked_d4_res.returncode != 0
    blocked_d4 = json.loads(blocked_d4_res.stdout)
    v1_d4_res = next(r for r in blocked_d4["versions"] if r["release_id"] == rel_v1)
    assert v1_d4_res["status"] == "blocked"
    assert v1_d4_res["reason_code"] == "missing_close"
