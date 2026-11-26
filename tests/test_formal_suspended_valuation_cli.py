"""Public-CLI acceptance tests for formal trailing Validation under frozen
carry-forward suspended valuation (Ticket #71 / #68-C).

Every test drives the public ``prajnatrade`` / ``prajnatrade-agent`` CLI on
synthetic dividend-free fixtures (schema-3 ``data_kind: "real"`` fixtures used
strictly for provenance/evidence semantics, plus schema-1 legacy controls).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest
from cli_helpers import agent_cli, cli, ok, rejected, write_json
from test_actual_open_cli import (
    conflict_references,
    day_at,
    derived_row,
    make_actual_open_halt,
    make_confirmed_suspension,
    make_missing_required_open,
    make_open_conflict,
    make_unknown_status,
    raw_row,
    schema_v3_fixture,
    seal_v3,
)
from test_discovery_split_cli import stored_campaign
from test_experiment_cli import campaign_options
from test_formal_validation_cli import (
    formal_scenario,
    legacy_scenario,
    round_uses,
    trusted_receipt,
    validate_current,
    version_entry,
)
from test_protected_eval_cli import submit_experiment
from test_research_loop_cli import (
    open_round,
    round_input,
    round_status,
    submit_version,
    validate_version,
    version_input,
)
from test_suspended_valuation_cli import (
    init_campaign,
    schema_v3_on_dates,
    set_constant_prices,
    weekday_dates,
)

from prajnatrade.data import ETF_SYMBOLS
from prajnatrade.discovery_summary import summarize_discovery_quarters
from prajnatrade.snapshot_baseline import (
    ACTUAL_OPEN_RULES,
    CARRY_FORWARD_SUSPENDED_VALUATION,
    EXECUTION_PRICE_BASIS,
    STRICT_CLOSE_VALUATION,
)

SYMBOLS = ETF_SYMBOLS
TARGET = SYMBOLS[0]  # 511260
OTHER = SYMBOLS[1]  # 518880
DRAW_DOWN_CAP_15 = [
    {"id": "max-drawdown-cap", "metric": "max_drawdown", "operator": ">=", "value": -0.15}
]
MOMENTUM_TOP1 = {"lookback": 2, "top_k": 1, "cash_if_negative": False}


def _validation_ledger_counts(scenario: dict) -> tuple[int, int]:
    with sqlite3.connect(
        scenario["project"] / ".prajnatrade" / "project.sqlite"
    ) as connection:
        attempts = connection.execute(
            "SELECT count(*) FROM research_loop_validation_attempts"
        ).fetchone()[0]
        uses = connection.execute(
            "SELECT count(*) FROM research_loop_validation_uses"
        ).fetchone()[0]
    return attempts, uses


def _zero_cost_split_campaign(
    root: Path,
    payload: dict,
    *,
    warmup: int = 3,
    name: str = "project",
    initial_cash: float = 1000.0,
    execution_rules: str = ACTUAL_OPEN_RULES,
    valuation_policy: str | None = CARRY_FORWARD_SUSPENDED_VALUATION,
) -> tuple[Path, Path, dict]:
    """Seal a dividend-free payload and freeze a zero-cost 70/30 split Campaign."""
    sealed = seal_v3(root, payload, name=name)
    store = sealed["store"]
    project, campaign = init_campaign(
        root,
        store,
        sealed["snapshot_id"],
        initial_cash=initial_cash,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
        execution_rules=execution_rules,
        valuation_policy=valuation_policy,
        discovery_split="70/30",
        discovery_warmup_sessions=warmup,
        attempts=8,
        max_seconds=180,
        project_name=name,
    )
    return project, store, campaign


def _dividend_free_v3_fixture(
    session_count: int = 30,
    *,
    start_date: date | None = None,
    raw_price: float = 10.0,
) -> dict:
    """Dividend-free schema-3 fixture where SYMBOLS[0] leads momentum(lookback=2, top_k=1)."""
    payload = (
        schema_v3_on_dates(weekday_dates(start_date, session_count))
        if start_date is not None
        else schema_v3_fixture(session_count)
    )
    payload["corporate_actions"] = []
    set_constant_prices(payload, raw_price=raw_price)
    return payload


def _set_symbol_price(payload: dict, day: str, symbol: str, price: float) -> None:
    row = raw_row(payload, day, symbol)
    row["open"] = price
    row["high"] = price
    row["low"] = price
    row["close"] = price


def _split_dates(payload: dict) -> tuple[list[str], list[str]]:
    """Return (development_dates, trailing_dates) for the 60% Discovery / 70-30 split."""
    dates = [row["date"] for row in payload["calendar"]]
    discovery_end = len(dates) * 3 // 5
    dev_end = discovery_end * 70 // 100
    return dates[:dev_end], dates[dev_end:discovery_end]


def _detail_request(
    scenario: dict,
    *,
    question: str,
    records: list[str],
    tag: str,
    agent: bool = True,
) -> dict:
    runner = agent_cli if agent else cli
    path = write_json(
        scenario["root"] / f"detail-{tag}.json",
        {"question": question, "records": records},
    )
    return ok(
        runner(
            "round", "detail",
            "--project", str(scenario["project"]),
            "--round-id", scenario["opened"]["round_id"],
            "--version-id", scenario["version"]["version_id"],
            "--input", str(path),
        )
    )


# ---------------------------------------------------------------------------
# Slice 1: Frozen policy identity, formal receipt, reconciliation & legacy
# ---------------------------------------------------------------------------


def test_frozen_valuation_policy_identity_formal_receipt_and_reconciliation(
    tmp_path: Path,
) -> None:
    """AC1: Frozen valuation policy binds identity, formal receipt & ledgers.

    Spans Q1/Q2 with confirmed full-day suspensions in both Discovery and
    trailing Validation, including a symbol held only by the equal-weight
    benchmark and a trailing-end suspension on TARGET. Verifies:
    - policy freeze across Campaign, Round, Locked Version, Discovery quarterly
      summary, and Formal Validation receipt provenance;
    - per-account holdings valuation (candidate holds TARGET only; benchmark
      holds all 5 ETFs);
    - exact hand-computed NAV, drawdown, and quarterly/full-segment ledger
      reconciliation;
    - agent ``round status`` / ``round detail`` visibility & privacy boundary;
    - legacy v1 & v2 ``strict-close-v1`` receipt shapes remain intact & readable.
    """
    # 40 sessions starting 2024-03-18 -> Discovery = 24 sessions (2024-03-18..2024-04-18)
    # 70/30 split -> development = 16 sessions (Q1 + Q2), trailing = 8 sessions (Q2).
    payload = _dividend_free_v3_fixture(40, start_date=date(2024, 3, 18), raw_price=10.0)
    dev_dates, trailing_dates = _split_dates(payload)
    assert len(dev_dates) == 16
    assert len(trailing_dates) == 8

    # Discovery development: warmup=3 -> scoring starts at dev_dates[3].
    # Candidate buys TARGET @ 10.0 on dev_dates[3] (100 shares, 0 cash).
    # Suspend TARGET on dev_dates[5] (carried @ 10.0), resume @ 9.0 on dev_dates[6].
    make_confirmed_suspension(payload, dev_dates[5], TARGET)
    for d in dev_dates[6:]:
        _set_symbol_price(payload, d, TARGET, 9.0)

    # Trailing Validation: scoring starts at trailing_dates[0].
    # Candidate buys TARGET @ 10.0 on trailing_dates[0] (100 shares, 0 cash).
    # Benchmark buys 20 shares each of all 5 ETFs @ 10.0 on trailing_dates[0].
    # Reset trailing prices to 10.0 on trailing_dates[0..1], then:
    # - trailing_dates[1]: suspend OTHER (benchmark holds OTHER; candidate does NOT!)
    # - trailing_dates[2]: suspend TARGET (both hold TARGET -> carried @ 10.0)
    # - trailing_dates[3..]: TARGET resumes @ 9.5 -> candidate NAV = 950 (-5% drawdown)
    # - trailing_dates[-1]: OTHER confirmed suspended at scoring end (benchmark carries @ 10.0!)
    for d in trailing_dates:
        for sym in SYMBOLS:
            _set_symbol_price(payload, d, sym, 10.0)
    make_confirmed_suspension(payload, trailing_dates[1], OTHER)
    make_confirmed_suspension(payload, trailing_dates[2], TARGET)
    for d in trailing_dates[3:]:
        _set_symbol_price(payload, d, TARGET, 9.5)
    make_confirmed_suspension(payload, trailing_dates[-1], OTHER)

    frozen_cf = _zero_cost_split_campaign(
        tmp_path,
        payload,
        warmup=3,
        name="cf-formal",
        initial_cash=1000.0,
        execution_rules=ACTUAL_OPEN_RULES,
        valuation_policy=CARRY_FORWARD_SUSPENDED_VALUATION,
    )
    scenario = formal_scenario(
        tmp_path,
        frozen_campaign=frozen_cf,
        limit=2,
        name="cf-formal",
        family="momentum",
        parameters=MOMENTUM_TOP1,
        hard_conditions=DRAW_DOWN_CAP_15,
    )

    # 1. Policy frozen before Validation across Campaign, Round, Version, Discovery.
    campaign = scenario["campaign"]
    assert campaign["execution_rules"]["valuation"]["version"] == CARRY_FORWARD_SUSPENDED_VALUATION
    assert campaign["valuation_policy"]["version"] == CARRY_FORWARD_SUSPENDED_VALUATION
    assert scenario["opened"]["kickoff"]["frozen_campaign"]["execution_rules"]["valuation"]["version"] == (
        CARRY_FORWARD_SUSPENDED_VALUATION
    )
    assert scenario["version"]["locked"]["execution_rules"]["valuation"]["version"] == (
        CARRY_FORWARD_SUSPENDED_VALUATION
    )
    assert scenario["experiment"]["quarterly_summary"]["identity"]["execution_rules"]["valuation"]["version"] == (
        CARRY_FORWARD_SUSPENDED_VALUATION
    )
    # Verify Discovery quarterly summary reconciles with full-segment ledger.
    discovery_run = scenario["experiment"]["discovery"]
    discovery_baseline = discovery_run["baseline_comparison"]["baseline"]
    recomputed_q = summarize_discovery_quarters(
        discovery_run,
        discovery_baseline,
        identity=scenario["experiment"]["quarterly_summary"]["identity"],
    )
    assert recomputed_q == scenario["experiment"]["quarterly_summary"]
    assert len(recomputed_q["quarters"]) >= 2
    assert discovery_run["metrics"]["max_drawdown"] == pytest.approx(-0.10, abs=1e-9)

    # Confirm different version_id / evaluation_hash vs strict-close-v1 on same payload.
    frozen_strict = _zero_cost_split_campaign(
        tmp_path,
        payload,
        warmup=3,
        name="strict-control",
        initial_cash=1000.0,
        execution_rules=ACTUAL_OPEN_RULES,
        valuation_policy=STRICT_CLOSE_VALUATION,
    )
    assert frozen_strict[2]["fingerprints"]["evaluation"] != campaign["fingerprints"]["evaluation"]
    assert stored_campaign(frozen_strict[0])[2]["runtime"] != stored_campaign(scenario["project"])[2]["runtime"]

    # 2. Formal trailing Validation runs both accounts under carry-forward-suspended-v1.
    validated = ok(validate_current(scenario))
    assert validated["outcome"] == "passed", validated["checks"]
    assert validated["reason"] is None
    assert validated["usage"]["kind"] == "automatic"
    assert validated["usage"]["sequence"] == 1
    assert len(round_uses(scenario)) == 1
    assert _validation_ledger_counts(scenario) == (1, 1)

    # Provenance & strict legacy-compatible shapes on scoring / snapshot availability.
    assert validated["provenance"]["valuation_policy_version"] == CARRY_FORWARD_SUSPENDED_VALUATION
    assert validated["provenance"]["snapshot_schema_version"] == 3
    assert validated["provenance"]["execution_rule_version"] == ACTUAL_OPEN_RULES
    assert validated["provenance"]["price_basis"] == EXECUTION_PRICE_BASIS
    assert set(validated["scoring"]) == {"start", "end", "sessions", "valued_sessions"}
    assert validated["scoring"] == {
        "start": trailing_dates[0],
        "end": trailing_dates[-1],
        "sessions": len(trailing_dates),
        "valued_sessions": len(trailing_dates),
    }
    assert set(validated["checks"]["data_availability"]["snapshot"]) == {
        "verified", "snapshot_id", "partition_content_hash",
        "as_of", "prior_history_hash", "data_kind", "scope",
    }

    # Candidate vs benchmark own holdings & valuation_summary audit counts:
    # - Candidate held TARGET only -> carried on trailing_dates[2] only (1 session).
    # - Benchmark held all 5 ETFs -> carried on trailing_dates[1] (OTHER), trailing_dates[2] (TARGET),
    #   and trailing_dates[-1] (OTHER) (3 sessions, with OTHER still carried at end).
    cand_run = validated["runs"]["candidate"]
    bench_run = validated["runs"]["benchmark"]
    assert cand_run["valuation_summary"]["policy_version"] == CARRY_FORWARD_SUSPENDED_VALUATION
    assert cand_run["valuation_summary"]["carried_forward_sessions"] == 1
    assert cand_run["valuation_summary"]["actual_close_sessions"] == len(trailing_dates) - 1
    assert cand_run["valuation_summary"]["missing_valuation_sessions"] == 0
    assert cand_run["valuation_summary"]["ending_carried_forward"] == {}

    assert bench_run["valuation_summary"]["policy_version"] == CARRY_FORWARD_SUSPENDED_VALUATION
    assert bench_run["valuation_summary"]["carried_forward_sessions"] == 3
    assert bench_run["valuation_summary"]["actual_close_sessions"] == len(trailing_dates) - 3
    assert bench_run["valuation_summary"]["missing_valuation_sessions"] == 0
    assert list(bench_run["valuation_summary"]["ending_carried_forward"]) == [OTHER]
    assert bench_run["valuation_summary"]["ending_carried_forward"][OTHER]["valuation_price"] == pytest.approx(10.0)
    assert bench_run["valuation_summary"]["ending_carried_forward"][OTHER]["carried_sessions"] == 1

    # Exact hand-computed trailing NAV & drawdown reconciliation from trusted curves.
    assert cand_run["metrics"]["ending_equity"] == pytest.approx(950.0, abs=1e-9)
    assert cand_run["metrics"]["total_return"] == pytest.approx(-0.05, abs=1e-9)
    assert cand_run["metrics"]["max_drawdown"] == pytest.approx(-0.05, abs=1e-9)
    # Benchmark: 4 ETFs * 20 shares * 10 = 800, TARGET 20 shares * 9.5 = 190 -> NAV 990 (-1%).
    assert bench_run["metrics"]["ending_equity"] == pytest.approx(990.0, abs=1e-9)
    assert bench_run["metrics"]["total_return"] == pytest.approx(-0.01, abs=1e-9)
    assert bench_run["metrics"]["max_drawdown"] == pytest.approx(-0.01, abs=1e-9)

    receipt = trusted_receipt(scenario, validated["validation_id"])
    cand_curve = receipt["curves"]["candidate"]
    bench_curve = receipt["curves"]["benchmark"]
    assert len(cand_curve) == len(trailing_dates)
    assert len(bench_curve) == len(trailing_dates)
    assert cand_curve[1]["valuation_basis"] == "actual_close"
    assert bench_curve[1]["valuation_basis"] == "carried_forward"
    assert bench_curve[1]["carried_forward_symbols"] == [OTHER]
    assert cand_curve[2]["valuation_basis"] == "carried_forward"
    assert cand_curve[2]["nav"] == pytest.approx(1000.0, abs=1e-9)
    assert cand_curve[3]["valuation_basis"] == "actual_close"
    assert cand_curve[3]["nav"] == pytest.approx(950.0, abs=1e-9)
    assert cand_curve[-1]["valuation_basis"] == "actual_close"
    assert cand_curve[-1]["nav"] == pytest.approx(950.0, abs=1e-9)
    assert bench_curve[-1]["valuation_basis"] == "carried_forward"
    assert bench_curve[-1]["carried_forward_symbols"] == [OTHER]
    assert bench_curve[-1]["nav"] == pytest.approx(990.0, abs=1e-9)

    # Reconcile trailing continuous ledger via summarize_discovery_quarters.
    trailing_q = summarize_discovery_quarters(
        {
            "start_date": cand_run["start_date"],
            "end_date": cand_run["end_date"],
            "initial_cash": cand_run["initial_cash"],
            "equity_curve": cand_curve,
            "fills": cand_run["fills"],
            "metrics": cand_run["metrics"],
        },
        {
            "start_date": bench_run["start_date"],
            "end_date": bench_run["end_date"],
            "initial_cash": bench_run["initial_cash"],
            "equity_curve": bench_curve,
            "fills": bench_run["fills"],
            "metrics": bench_run["metrics"],
        },
        identity={"provenance": validated["provenance"]},
    )
    assert len(trailing_q["quarters"]) == 1
    q_cand = trailing_q["quarters"][0]["candidate"]
    q_bench = trailing_q["quarters"][0]["benchmark"]
    assert q_cand["net_return"] == pytest.approx(cand_run["metrics"]["total_return"], abs=1e-9)
    assert q_cand["max_drawdown"] == pytest.approx(cand_run["metrics"]["max_drawdown"], abs=1e-9)
    assert q_cand["valuation"]["carried_forward_sessions"] == 1
    assert q_bench["valuation"]["carried_forward_sessions"] == 3

    # 3. Agent round status & detail: policy & valuation_summary visible, curves hidden.
    status = ok(round_status(scenario["project"], scenario["opened"]["round_id"], agent=True))
    sub = next(
        item for item in status["submissions"]
        if item["version_id"] == scenario["version"]["version_id"]
    )
    assert sub["validation"]["provenance"]["valuation_policy_version"] == (
        CARRY_FORWARD_SUSPENDED_VALUATION
    )
    status_text = json.dumps(status)
    assert "equity_curve" not in status_text
    assert '"curves"' not in status_text
    assert str(scenario["store"]) not in status_text

    details = _detail_request(
        scenario,
        question="Verify valuation policy and carried-forward summary.",
        records=["provenance", "runs.candidate", "runs.benchmark", "metrics.candidate", "summary"],
        tag="cf-audit",
        agent=True,
    )
    assert details["details"]["provenance"]["valuation_policy_version"] == (
        CARRY_FORWARD_SUSPENDED_VALUATION
    )
    assert details["details"]["runs.candidate"]["valuation_summary"] == cand_run["valuation_summary"]
    assert details["details"]["runs.benchmark"]["valuation_summary"] == bench_run["valuation_summary"]
    detail_text = json.dumps(details)
    assert "equity_curve" not in detail_text
    assert '"curves"' not in detail_text
    assert str(scenario["store"]) not in detail_text

    # 4. Legacy v1 & v2 strict-close-v1 receipts keep exact legacy shapes and read cleanly.
    v1_legacy = legacy_scenario(tmp_path, name="legacy-v1")
    v1_receipt = ok(
        validate_version(
            v1_legacy["project"],
            v1_legacy["store"],
            v1_legacy["snapshots"],
            v1_legacy["opened"]["round_id"],
            v1_legacy["version"]["version_id"],
        )
    )
    assert v1_receipt["outcome"] == "passed"
    assert "provenance" not in v1_receipt
    assert "valuation_summary" not in v1_receipt
    v1_status = ok(round_status(v1_legacy["project"], v1_legacy["opened"]["round_id"], agent=True))
    assert v1_status["submissions"][0]["validation"]["outcome"] == "passed"

    v2_strict = formal_scenario(
        tmp_path,
        payload=_dividend_free_v3_fixture(15),
        name="legacy-v2-strict",
        execution_rules=ACTUAL_OPEN_RULES,
    )
    v2_receipt = ok(validate_current(v2_strict))
    assert v2_receipt["outcome"] == "passed"
    assert "valuation_policy_version" not in v2_receipt["provenance"]
    assert "valuation_summary" not in v2_receipt["runs"]["candidate"]
    assert "valuation_summary" not in v2_receipt["runs"]["benchmark"]
    v2_detail = _detail_request(
        v2_strict,
        question="Read legacy strict-close-v1 formal provenance.",
        records=["provenance", "runs.candidate"],
        tag="legacy-strict",
        agent=True,
    )
    assert "valuation_policy_version" not in v2_detail["details"]["provenance"]
    assert "valuation_summary" not in v2_detail["details"]["runs.candidate"]

    # 5. Legacy protocol-v1 round under carry-forward-suspended-v1 also records provenance.
    v1_cf_payload = _dividend_free_v3_fixture(15)
    make_confirmed_suspension(v1_cf_payload, day_at(5), TARGET)
    v1_cf_project = tmp_path / "v1-cf"
    v1_cf_source = write_json(tmp_path / "v1-cf-snapshot.json", v1_cf_payload)
    ok(cli("project", "init", "--project", str(v1_cf_project)))
    v1_cf_campaign = ok(
        cli(
            *campaign_options(v1_cf_project, v1_cf_source, attempts=6, max_seconds=180),
            "--discovery-warmup-sessions", "3",
            "--execution-rules", ACTUAL_OPEN_RULES,
            "--valuation-policy", CARRY_FORWARD_SUSPENDED_VALUATION,
        )
    )
    v1_cf_snapshots = v1_cf_project.parent / f"{v1_cf_project.name}-snapshot-store"
    v1_cf_exp = submit_experiment(
        v1_cf_project,
        v1_cf_campaign["campaign_id"],
        tmp_path / "v1-cf-exp.json",
        "mom",
        "v1 carry-forward replay check",
        "momentum",
        MOMENTUM_TOP1,
    )
    v1_cf_store = tmp_path / "v1-cf-trusted"
    v1_cf_opened = ok(
        open_round(
            v1_cf_project,
            v1_cf_store,
            v1_cf_campaign["campaign_id"],
            write_json(tmp_path / "v1-cf-round.json", round_input()),
        )
    )
    v1_cf_ver = ok(
        submit_version(
            v1_cf_project,
            v1_cf_opened["round_id"],
            "v1",
            write_json(tmp_path / "v1-cf-ver.json", version_input(v1_cf_exp["experiment_id"])),
        )
    )
    v1_cf_receipt = ok(
        validate_version(
            v1_cf_project,
            v1_cf_store,
            v1_cf_snapshots,
            v1_cf_opened["round_id"],
            v1_cf_ver["version_id"],
        )
    )
    assert v1_cf_receipt["outcome"] == "passed", v1_cf_receipt["checks"]
    assert v1_cf_receipt["provenance"]["valuation_policy_version"] == (
        CARRY_FORWARD_SUSPENDED_VALUATION
    )


# ---------------------------------------------------------------------------
# Slice 2: Three-way synthetic verdicts (15% drawdown cap & 70/30 boundary)
# ---------------------------------------------------------------------------


def _make_late_confirmed_suspension(payload: dict, day: str, symbol: str) -> None:
    """Confirmed suspension whose notice arrives after the day's 23:59:59+08:00 cutoff.

    ``available_at`` is non-null (so manifest ``gaps.availability`` stays empty),
    but late relative to ``day``'s valuation cutoff.
    """
    make_confirmed_suspension(payload, day, symbol)
    next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    raw_row(payload, day, symbol)["available_at"] = f"{next_day}T10:00:00+08:00"


def _make_resumed_halt_missing_close(
    payload: dict, day: str, symbol: str, *, open_price: float = 10.0,
) -> None:
    """Resumed ``partial_halt`` with trusted actual open but missing close.

    Manifest classifies ``actual_open_after_halt`` (benign, explains ``raw_bars``)
    and ``derived.value`` remains present, so the pre-read gate allows the read
    and the close-valuation deficiency is discovered only post-read if held.
    """
    make_actual_open_halt(payload, day, symbol, open_price=open_price)
    row = raw_row(payload, day, symbol)
    row["close"] = None
    row["close_available_at"] = None


def test_verdict_pass_within_15pct_cap_and_70_30_boundary_preserved(
    tmp_path: Path,
) -> None:
    """AC2 (Case 1 - Pass): Complete NAV via carry-forward, drawdown -10% >= -15%.

    Also verifies an ``equal_weight`` candidate when a confirmed suspension lands
    on the trailing segment's final session: the frozen 70/30 boundary never
    shifts and ``ending_carried_forward`` discloses the carried holding.
    """
    # 30 sessions -> Discovery = 18 -> development = 12 (70% floor), trailing = 6.
    payload = _dividend_free_v3_fixture(30, raw_price=10.0)
    dev_dates, trailing_dates = _split_dates(payload)
    assert len(dev_dates) == 12
    assert len(trailing_dates) == 6

    # Discovery: buy TARGET @ 10.0 on dev_dates[3], suspend dev_dates[5], resume @ 9.0 -> -10%.
    make_confirmed_suspension(payload, dev_dates[5], TARGET)
    for d in dev_dates[6:]:
        _set_symbol_price(payload, d, TARGET, 9.0)

    # Trailing: buy TARGET @ 10.0 on trailing_dates[0], suspend trailing_dates[2],
    # resume @ 9.0 on trailing_dates[3..] -> candidate drawdown = -10% >= -15%.
    for d in trailing_dates:
        for sym in SYMBOLS:
            _set_symbol_price(payload, d, sym, 10.0)
    make_confirmed_suspension(payload, trailing_dates[2], TARGET)
    for d in trailing_dates[3:]:
        _set_symbol_price(payload, d, TARGET, 9.0)

    frozen = _zero_cost_split_campaign(tmp_path, payload, warmup=3, name="pass-10pct")
    scenario = formal_scenario(
        tmp_path,
        frozen_campaign=frozen,
        limit=2,
        name="pass-10pct",
        family="momentum",
        parameters=MOMENTUM_TOP1,
        hard_conditions=DRAW_DOWN_CAP_15,
    )

    assert scenario["split"]["development"] == {
        **scenario["split"]["development"],
        "start": dev_dates[0],
        "end": dev_dates[-1],
        "sessions": 12,
    }
    assert scenario["split"]["trailing"] == {
        **scenario["split"]["trailing"],
        "start": trailing_dates[0],
        "end": trailing_dates[-1],
        "sessions": 6,
    }

    validated = ok(validate_current(scenario))
    assert validated["outcome"] == "passed", validated["checks"]
    assert validated["reason"] is None
    assert validated["metrics"]["candidate"]["max_drawdown"] == pytest.approx(-0.10, abs=1e-9)
    assert validated["metrics"]["benchmark"]["max_drawdown"] == pytest.approx(-0.02, abs=1e-9)
    assert validated["checks"]["hard_conditions"] == [
        {
            "id": "max-drawdown-cap",
            "metric": "max_drawdown",
            "operator": ">=",
            "value": -0.15,
            "observed": pytest.approx(-0.10, abs=1e-9),
            "status": "met",
        }
    ]
    assert validated["usage"]["kind"] == "automatic"
    assert validated["usage"]["sequence"] == 1
    assert len(round_uses(scenario)) == 1

    # Equal-weight candidate with confirmed suspension on trailing_dates[-1]:
    # boundary stays at trailing_dates[-1] and both sides report ending_carried_forward.
    end_payload = _dividend_free_v3_fixture(30, raw_price=10.0)
    _, end_trailing = _split_dates(end_payload)
    make_confirmed_suspension(end_payload, end_trailing[-1], TARGET)
    end_frozen = _zero_cost_split_campaign(tmp_path, end_payload, warmup=3, name="pass-end-halt")
    end_scenario = formal_scenario(
        tmp_path,
        frozen_campaign=end_frozen,
        limit=1,
        name="pass-end-halt",
        family="equal_weight",
        parameters={},
        hard_conditions=DRAW_DOWN_CAP_15,
    )
    assert end_scenario["split"]["trailing"]["end"] == end_trailing[-1]
    assert end_scenario["split"]["trailing"]["sessions"] == 6
    end_validated = ok(validate_current(end_scenario))
    assert end_validated["outcome"] == "passed", end_validated["checks"]
    assert end_validated["scoring"]["end"] == end_trailing[-1]
    for side in ("candidate", "benchmark"):
        summary = end_validated["runs"][side]["valuation_summary"]
        assert summary["carried_forward_sessions"] == 1
        assert list(summary["ending_carried_forward"]) == [TARGET]
        assert summary["ending_carried_forward"][TARGET]["carried_sessions"] == 1


def test_verdict_fail_drawdown_exceeds_15pct_in_precheck_and_formal(
    tmp_path: Path,
) -> None:
    """AC2 (Case 2 - Fail): Book drawdown -20% < -15% cap.

    Verifies BOTH:
    - 2a: Discovery violates 15% cap -> ``VALIDATION_PRECHECK_REJECTED`` with 0 uses.
    - 2b: Discovery passes (-10%), trailing violates (-20%) -> formal ``failed``
      / ``HARD_CONDITION_NOT_MET`` with 1 counted use.
    """
    # --- 2a: Discovery fails 15% cap -> rejected before read, 0 uses ---
    disc_fail_payload = _dividend_free_v3_fixture(30, raw_price=10.0)
    dev_dates, _ = _split_dates(disc_fail_payload)
    make_confirmed_suspension(disc_fail_payload, dev_dates[5], TARGET)
    for d in dev_dates[6:]:
        _set_symbol_price(disc_fail_payload, d, TARGET, 8.0)

    disc_frozen = _zero_cost_split_campaign(
        tmp_path, disc_fail_payload, warmup=3, name="fail-discovery"
    )
    disc_scenario = formal_scenario(
        tmp_path,
        frozen_campaign=disc_frozen,
        limit=1,
        name="fail-discovery",
        family="momentum",
        parameters=MOMENTUM_TOP1,
        hard_conditions=DRAW_DOWN_CAP_15,
    )
    assert disc_scenario["experiment"]["discovery"]["metrics"]["max_drawdown"] == pytest.approx(
        -0.20, abs=1e-9
    )

    blocked = rejected(
        validate_current(disc_scenario),
        "VALIDATION_PRECHECK_REJECTED",
        phase="validation",
    )
    assert blocked["counted"] is False
    assert blocked["outcome"] == "rejected"
    assert blocked["reason"] == "HARD_CONDITION_NOT_MET"
    assert blocked["allowance"]["automatic_used"] == 0
    assert blocked["precheck"]["hard_conditions"][0]["observed"] == pytest.approx(-0.20, abs=1e-9)
    assert blocked["precheck"]["hard_conditions"][0]["status"] == "failed"
    assert _validation_ledger_counts(disc_scenario) == (0, 0)
    assert not (disc_scenario["store"] / "research-loop-validations").exists()
    assert version_entry(disc_scenario)["validation"]["status"] == "not_run"

    # --- 2b: Discovery passes (-10%), Trailing fails (-20%) -> formal failed, 1 use ---
    trail_fail_payload = _dividend_free_v3_fixture(30, raw_price=10.0)
    dev_dates, trailing_dates = _split_dates(trail_fail_payload)
    make_confirmed_suspension(trail_fail_payload, dev_dates[5], TARGET)
    for d in dev_dates[6:]:
        _set_symbol_price(trail_fail_payload, d, TARGET, 9.0)
    for d in trailing_dates:
        for sym in SYMBOLS:
            _set_symbol_price(trail_fail_payload, d, sym, 10.0)
    make_confirmed_suspension(trail_fail_payload, trailing_dates[2], TARGET)
    for d in trailing_dates[3:]:
        _set_symbol_price(trail_fail_payload, d, TARGET, 8.0)

    trail_frozen = _zero_cost_split_campaign(
        tmp_path, trail_fail_payload, warmup=3, name="fail-trailing"
    )
    trail_scenario = formal_scenario(
        tmp_path,
        frozen_campaign=trail_frozen,
        limit=1,
        name="fail-trailing",
        family="momentum",
        parameters=MOMENTUM_TOP1,
        hard_conditions=DRAW_DOWN_CAP_15,
    )
    validated = ok(validate_current(trail_scenario))
    assert validated["outcome"] == "failed"
    assert validated["reason"] == "HARD_CONDITION_NOT_MET"
    assert validated["metrics"]["candidate"]["valued_sessions"] == len(trailing_dates)
    assert validated["metrics"]["candidate"]["max_drawdown"] == pytest.approx(-0.20, abs=1e-9)
    assert validated["checks"]["hard_conditions"][0]["status"] == "failed"
    assert validated["checks"]["hard_conditions"][0]["observed"] == pytest.approx(-0.20, abs=1e-9)
    assert validated["provenance"]["valuation_policy_version"] == CARRY_FORWARD_SUSPENDED_VALUATION
    assert validated["usage"]["kind"] == "automatic"
    assert validated["usage"]["sequence"] == 1
    assert _validation_ledger_counts(trail_scenario) == (1, 1)


def test_verdict_insufficient_evidence_in_precheck_and_formal(tmp_path: Path) -> None:
    """AC2 (Case 3 - Insufficient evidence): Unresolvable valuation gap.

    Verifies BOTH:
    - 3a: Discovery has late suspension notice -> ``VALIDATION_PRECHECK_INSUFFICIENT``
      (``METRICS_NOT_READY``) with 0 uses.
    - 3b: Discovery passes (-10%), trailing has late suspension notice on held
      TARGET -> passes manifest gate, consumes 1 use, returns formal
      ``insufficient_evidence`` / ``COVERAGE_INSUFFICIENT``.
    """
    # --- 3a: Discovery valuation gap -> precheck insufficient, 0 uses ---
    disc_gap_payload = _dividend_free_v3_fixture(30, raw_price=10.0)
    dev_dates, _ = _split_dates(disc_gap_payload)
    _make_late_confirmed_suspension(disc_gap_payload, dev_dates[5], TARGET)

    disc_frozen = _zero_cost_split_campaign(
        tmp_path, disc_gap_payload, warmup=3, name="insuff-discovery"
    )
    disc_scenario = formal_scenario(
        tmp_path,
        frozen_campaign=disc_frozen,
        limit=1,
        name="insuff-discovery",
        family="momentum",
        parameters=MOMENTUM_TOP1,
        hard_conditions=DRAW_DOWN_CAP_15,
    )
    assert disc_scenario["experiment"]["discovery"]["metrics"]["max_drawdown"] is None

    blocked = rejected(
        validate_current(disc_scenario),
        "VALIDATION_PRECHECK_INSUFFICIENT",
        phase="validation",
    )
    assert blocked["counted"] is False
    assert blocked["outcome"] == "insufficient_evidence"
    assert blocked["reason"] == "METRICS_NOT_READY"
    assert blocked["allowance"]["automatic_used"] == 0
    assert _validation_ledger_counts(disc_scenario) == (0, 0)
    assert not (disc_scenario["store"] / "research-loop-validations").exists()

    # --- 3b: Discovery passes (-10%), trailing has late notice -> formal insufficient, 1 use ---
    trail_gap_payload = _dividend_free_v3_fixture(30, raw_price=10.0)
    dev_dates, trailing_dates = _split_dates(trail_gap_payload)
    make_confirmed_suspension(trail_gap_payload, dev_dates[5], TARGET)
    for d in dev_dates[6:]:
        _set_symbol_price(trail_gap_payload, d, TARGET, 9.0)
    for d in trailing_dates:
        for sym in SYMBOLS:
            _set_symbol_price(trail_gap_payload, d, sym, 10.0)
    _make_late_confirmed_suspension(trail_gap_payload, trailing_dates[2], TARGET)

    trail_frozen = _zero_cost_split_campaign(
        tmp_path, trail_gap_payload, warmup=3, name="insuff-trailing"
    )
    trail_scenario = formal_scenario(
        tmp_path,
        frozen_campaign=trail_frozen,
        limit=1,
        name="insuff-trailing",
        family="momentum",
        parameters=MOMENTUM_TOP1,
        hard_conditions=DRAW_DOWN_CAP_15,
    )
    validated = ok(validate_current(trail_scenario))
    assert validated["outcome"] == "insufficient_evidence"
    assert validated["reason"] == "COVERAGE_INSUFFICIENT"
    assert validated["metrics"]["candidate"]["max_drawdown"] is None
    assert validated["metrics"]["benchmark"]["max_drawdown"] is None
    assert validated["checks"]["data_availability"]["ok"] is False
    assert (
        validated["checks"]["data_availability"]["candidate"]["valuation_gaps"]["first"][
            "valuation_gap_reason"
        ]
        == "status_evidence_after_cutoff"
    )
    assert validated["usage"]["kind"] == "automatic"
    assert validated["usage"]["sequence"] == 1
    assert _validation_ledger_counts(trail_scenario) == (1, 1)


# ---------------------------------------------------------------------------
# Slice 3: Pre-read gate & zero-use co-occurring blocking gaps (AC3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "co_gap_kind",
    [
        "same_symbol_availability",
        "other_symbol_missing_required_open",
        "other_symbol_source_conflict",
        "other_symbol_unknown_status",
    ],
)
def test_confirmed_no_trade_never_masks_same_day_blocking_gap_zero_uses(
    tmp_path: Path, co_gap_kind: str,
) -> None:
    """AC3: Confirmed full-day no-trade explains only its own raw_bars/derived gap.

    When an independent blocking gap identifiable from sealed manifest metadata
    co-occurs on the same trailing session, the pre-read gate rejects with
    ``VALIDATION_DATA_GAP``, names the gap, and consumes zero uses.
    """
    payload = _dividend_free_v3_fixture(30, raw_price=10.0)
    _, trailing_dates = _split_dates(payload)
    day = trailing_dates[2]

    # Confirmed full-day no-trade on TARGET on `day`.
    make_confirmed_suspension(payload, day, TARGET)

    disputed_opens: list[float] = []
    if co_gap_kind == "same_symbol_availability":
        derived_row(payload, day, TARGET)["available_at"] = None
    elif co_gap_kind == "other_symbol_missing_required_open":
        make_missing_required_open(payload, day, OTHER)
    elif co_gap_kind == "other_symbol_source_conflict":
        conflict = make_open_conflict(payload, day, OTHER)
        disputed_opens = [item["open"] for item in conflict["open_conflict"]["sources"]]
    else:
        make_unknown_status(payload, day, OTHER)

    frozen = _zero_cost_split_campaign(
        tmp_path, payload, warmup=3, name=f"gate-{co_gap_kind}"
    )
    scenario = formal_scenario(
        tmp_path,
        frozen_campaign=frozen,
        limit=1,
        name=f"gate-{co_gap_kind}",
        family="momentum",
        parameters=MOMENTUM_TOP1,
        hard_conditions=DRAW_DOWN_CAP_15,
    )

    blocked = rejected(
        validate_current(scenario), "VALIDATION_DATA_GAP", phase="validation"
    )
    assert blocked["counted"] is False
    assert blocked["allowance"]["auto_limit"] == 1
    assert blocked["allowance"]["automatic_used"] == 0
    assert blocked["allowance"]["total_used"] == 0
    assert day in blocked["error"]["message"]

    if co_gap_kind == "same_symbol_availability":
        assert blocked["execution_evidence"] == []
        assert "availability" in blocked["gaps"]
        assert any(
            entry["date"] == day and entry["symbol"] == TARGET
            for entry in blocked["gaps"]["availability"]
        )
        assert TARGET in blocked["error"]["message"]
    else:
        expected_cls = {
            "other_symbol_missing_required_open": "missing_required_open",
            "other_symbol_source_conflict": "source_conflict",
            "other_symbol_unknown_status": "unknown_status",
        }[co_gap_kind]
        rows = blocked["execution_evidence"]
        assert len(rows) == 1
        assert rows[0]["date"] == day
        assert rows[0]["symbol"] == OTHER
        assert rows[0]["classification"] == expected_cls
        assert OTHER in blocked["error"]["message"]
        assert expected_cls in blocked["error"]["message"]
        if co_gap_kind == "other_symbol_source_conflict":
            assert rows[0]["conflict_sources"] == conflict_references()
            serialized = json.dumps(rows[0])
            for quote in disputed_opens:
                assert str(quote) not in serialized

    assert not (scenario["store"] / "research-loop-validations").exists()
    assert _validation_ledger_counts(scenario) == (0, 0)
    status = ok(round_status(scenario["project"], scenario["opened"]["round_id"], agent=True))
    assert status["lifecycle"] == "research"
    assert status["validation_allowance"]["automatic_used"] == 0
    assert status["validation_allowance"]["uses"] == []
    assert version_entry(scenario)["validation"]["status"] == "not_run"


# ---------------------------------------------------------------------------
# Slice 4: Post-read holding-dependent insufficiency, replay & policy binding
# ---------------------------------------------------------------------------


def test_post_read_holding_dependent_insufficiency_and_replay_non_recount(
    tmp_path: Path,
) -> None:
    """AC4: Holding-dependent post-read gaps count once; replay never recounts.

    Covers:
    1. Resumed ``partial_halt`` with open & missing close on OTHER (held only
       by equal-weight benchmark, NOT held by momentum candidate) -> candidate
       has 0 valuation gaps while benchmark has 1 (``partial_halt_missing_close``)
       -> formal ``insufficient_evidence`` with 1 counted use; replay reuses
       with 0 additional uses.
    2. Mid-chain break discovered post-read (resumed halt missing close on day t,
       confirmed suspension on day t+1) -> cannot carry across the break
       (``carry_forward_chain_broken``) -> 1 use; replay never recounts.
    3. Legitimate passing formal read consumes 1 use; replay returns
       ``reused: true``, identical ``validation_id``, and 0 additional uses.
    4. Policy mismatch tampered into Discovery quarterly identity is rejected at
       precheck (``VALIDATION_EVIDENCE_MISMATCH``) with 0 uses.
    """
    # --- 1. Asymmetric holdings: resumed partial_halt missing close on OTHER ---
    asym_payload = _dividend_free_v3_fixture(30, raw_price=10.0)
    _, trailing_dates = _split_dates(asym_payload)
    _make_resumed_halt_missing_close(asym_payload, trailing_dates[2], OTHER, open_price=10.0)

    asym_frozen = _zero_cost_split_campaign(tmp_path, asym_payload, warmup=3, name="asym-halt")
    asym_scenario = formal_scenario(
        tmp_path,
        frozen_campaign=asym_frozen,
        limit=2,
        name="asym-halt",
        family="momentum",
        parameters=MOMENTUM_TOP1,
        hard_conditions=DRAW_DOWN_CAP_15,
    )
    asym_val = ok(validate_current(asym_scenario))
    assert asym_val["outcome"] == "insufficient_evidence"
    assert asym_val["reason"] == "COVERAGE_INSUFFICIENT"
    assert asym_val["reused"] is False
    assert asym_val["usage"]["kind"] == "automatic"
    assert asym_val["usage"]["sequence"] == 1

    # Candidate never held OTHER -> zero candidate valuation gaps, full coverage.
    assert asym_val["checks"]["data_availability"]["candidate"]["valuation_gaps"]["count"] == 0
    assert asym_val["runs"]["candidate"]["valued_sessions"] == len(trailing_dates)
    assert asym_val["metrics"]["candidate"]["max_drawdown"] is not None

    # Benchmark held OTHER -> 1 benchmark valuation gap (partial_halt_missing_close).
    bench_gap = asym_val["checks"]["data_availability"]["benchmark"]["valuation_gaps"]
    assert bench_gap["count"] == 1
    assert bench_gap["dates"] == [trailing_dates[2]]
    assert bench_gap["first"]["valuation_gap_reason"] == "partial_halt_missing_close"
    assert asym_val["runs"]["benchmark"]["valued_sessions"] == len(trailing_dates) - 1
    assert asym_val["metrics"]["benchmark"]["max_drawdown"] is None
    assert _validation_ledger_counts(asym_scenario) == (1, 1)

    asym_replay = ok(validate_current(asym_scenario))
    assert asym_replay["reused"] is True
    assert asym_replay["validation_id"] == asym_val["validation_id"]
    assert asym_replay["receipt_hash"] == asym_val["receipt_hash"]
    assert len(round_uses(asym_scenario)) == 1
    assert _validation_ledger_counts(asym_scenario) == (1, 1)

    # --- 2. Post-read mid-chain break on TARGET (day t missing close, day t+1 suspended) ---
    chain_payload = _dividend_free_v3_fixture(30, raw_price=10.0)
    _, chain_trailing = _split_dates(chain_payload)
    _make_resumed_halt_missing_close(chain_payload, chain_trailing[2], TARGET, open_price=10.0)
    make_confirmed_suspension(chain_payload, chain_trailing[3], TARGET)

    chain_frozen = _zero_cost_split_campaign(tmp_path, chain_payload, warmup=3, name="chain-break")
    chain_scenario = formal_scenario(
        tmp_path,
        frozen_campaign=chain_frozen,
        limit=2,
        name="chain-break",
        family="momentum",
        parameters=MOMENTUM_TOP1,
        hard_conditions=DRAW_DOWN_CAP_15,
    )
    chain_val = ok(validate_current(chain_scenario))
    assert chain_val["outcome"] == "insufficient_evidence"
    assert chain_val["reason"] == "COVERAGE_INSUFFICIENT"
    cand_gaps = chain_val["checks"]["data_availability"]["candidate"]["valuation_gaps"]
    assert cand_gaps["count"] == 2
    assert cand_gaps["dates"] == [chain_trailing[2], chain_trailing[3]]
    assert cand_gaps["first"]["valuation_gap_reason"] == "partial_halt_missing_close"
    chain_receipt = trusted_receipt(chain_scenario, chain_val["validation_id"])
    assert chain_receipt["curves"]["candidate"][2]["nav"] is None
    assert chain_receipt["curves"]["candidate"][3]["nav"] is None
    assert _validation_ledger_counts(chain_scenario) == (1, 1)

    chain_replay = ok(validate_current(chain_scenario))
    assert chain_replay["reused"] is True
    assert chain_replay["validation_id"] == chain_val["validation_id"]
    assert _validation_ledger_counts(chain_scenario) == (1, 1)

    # --- 3. Legitimate passing formal read counts 1; replay counts 0 additional ---
    # Also exercises formal_scenario(..., valuation_policy=...) -> split_campaign forwarding.
    pass_payload = _dividend_free_v3_fixture(30, raw_price=10.0)
    _, pass_trailing = _split_dates(pass_payload)
    make_confirmed_suspension(pass_payload, pass_trailing[2], TARGET)
    pass_scenario = formal_scenario(
        tmp_path,
        payload=pass_payload,
        warmup=3,
        limit=2,
        name="pass-replay",
        family="momentum",
        parameters=MOMENTUM_TOP1,
        hard_conditions=DRAW_DOWN_CAP_15,
        execution_rules=ACTUAL_OPEN_RULES,
        valuation_policy=CARRY_FORWARD_SUSPENDED_VALUATION,
    )
    first = ok(validate_current(pass_scenario))
    assert first["outcome"] == "passed"
    assert first["reused"] is False
    assert first["usage"]["sequence"] == 1
    assert first["allowance"]["automatic_used"] == 1
    assert _validation_ledger_counts(pass_scenario) == (1, 1)

    second = ok(validate_current(pass_scenario))
    assert second["reused"] is True
    assert second["validation_id"] == first["validation_id"]
    assert second["receipt_hash"] == first["receipt_hash"]
    assert second["allowance"]["automatic_used"] == 1
    assert len(round_uses(pass_scenario)) == 1
    assert _validation_ledger_counts(pass_scenario) == (1, 1)

    # --- 4. Valuation policy tampering in Discovery summary rejected with 0 uses ---
    tamper_payload = _dividend_free_v3_fixture(30, raw_price=10.0)
    tamper_frozen = _zero_cost_split_campaign(tmp_path, tamper_payload, warmup=3, name="tamper")
    tamper_scenario = formal_scenario(
        tmp_path,
        frozen_campaign=tamper_frozen,
        limit=1,
        name="tamper",
        family="momentum",
        parameters=MOMENTUM_TOP1,
        hard_conditions=DRAW_DOWN_CAP_15,
    )
    db_path = tamper_scenario["project"] / ".prajnatrade" / "project.sqlite"
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT experiment_id, result_json FROM experiments WHERE experiment_id = ?",
            (tamper_scenario["experiment"]["experiment_id"],),
        ).fetchone()
        res = json.loads(row[1])
        res["quarterly_summary"]["identity"]["execution_rules"]["valuation"]["version"] = (
            STRICT_CLOSE_VALUATION
        )
        conn.execute(
            "UPDATE experiments SET result_json = ? WHERE experiment_id = ?",
            (json.dumps(res, sort_keys=True, separators=(",", ":")), row[0]),
        )
    mismatch = rejected(
        validate_current(tamper_scenario),
        "VALIDATION_EVIDENCE_MISMATCH",
        phase="validation",
    )
    assert mismatch["counted"] is False
    assert mismatch["allowance"]["automatic_used"] == 0
    assert _validation_ledger_counts(tamper_scenario) == (0, 0)

