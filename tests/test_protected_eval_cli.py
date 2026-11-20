"""Public-CLI tests for the protected Validation workflow (synthetic only).

These tests exercise `prajnatrade evaluation ...` (operator) and
`prajnatrade-agent evaluation status ...` (research side).  The sealed snapshot
fixtures are synthetic; no real market data is used.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SYMBOLS = ("511260", "518880", "515080", "513300", "159612")
EVIDENCE = {
    "available_at": "2023-12-31T00:00:00+08:00",
    "availability_evidence": "synthetic release schedule",
    "collected_at": "2024-02-01T00:00:00+08:00",
}


def cli_env(**extra: str) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        (str(ROOT / "src"), env.get("PYTHONPATH", ""))
    )
    env.update(extra)
    return env


def cli(
    *args: str, agent: bool = False, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    command = (
        (
            sys.executable,
            "-c",
            "from prajnatrade.cli import agent_main; raise SystemExit(agent_main())",
        )
        if agent
        else (sys.executable, "-m", "prajnatrade")
    )
    return subprocess.run(
        [*command, *args],
        capture_output=True,
        text=True,
        env=env or cli_env(),
        timeout=120,
    )


def ok(result: subprocess.CompletedProcess[str]) -> dict:
    assert result.returncode == 0, (result.stdout, result.stderr)
    return json.loads(result.stdout)


def rejected(result: subprocess.CompletedProcess[str], code: str) -> dict:
    assert result.returncode == 2, (result.returncode, result.stdout, result.stderr)
    payload = json.loads(result.stdout)
    assert payload["status"] == "rejected", payload
    assert payload["error"]["code"] == code, payload
    return payload


def synthetic_snapshot(
    day_count: int = 20,
    *,
    missing_close: tuple[str, str] | None = None,
    discovery_marker: str | None = None,
    derived_range: tuple[str, str] | None = None,
) -> dict:
    """Constant-price synthetic sessions with a Discovery-only dividend.

    Prices are flat so the equal-weight candidate and its baseline both end
    flat after the single rebalance; that makes threshold outcomes deterministic
    without over-fitting to float detail.  `missing_close` removes one close
    inside the protected range.  `discovery_marker` perturbs only Discovery
    derived values, which changes the snapshot ID but preserves the Validation
    and Final Holdout payload hashes (used for the repackaging test).
    `derived_range` perturbs derived values only inside an inclusive date range,
    which changes one partition's content hash while leaving the others intact
    (used by #29 to make the Validation range new but keep the same sealed
    Holdout content).
    """

    days = [date(2024, 1, 1) + timedelta(days=index) for index in range(day_count)]
    iso_days = [day.isoformat() for day in days]

    raw_bars = []
    derived = []
    for day_index, day in enumerate(days):
        for symbol_index, symbol in enumerate(SYMBOLS):
            price = 9.8 if symbol_index == 0 and day >= date(2024, 1, 4) else 10.0
            close = price
            if missing_close == (day.isoformat(), symbol):
                close = None
            raw_bars.append(
                {
                    "date": day.isoformat(),
                    "symbol": symbol,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": close,
                    "volume": 1000,
                    "trading_status": "trading",
                    **EVIDENCE,
                }
            )
            value = 10.0
            if discovery_marker is not None and day.isoformat() <= discovery_marker:
                value = 10.5
            if (
                derived_range is not None
                and derived_range[0] <= day.isoformat() <= derived_range[1]
            ):
                value = 10.5
            derived.append(
                {"date": day.isoformat(), "symbol": symbol, "value": value, **EVIDENCE}
            )

    return {
        "schema_version": 1,
        "data_kind": "synthetic",
        "sources": {
            name: {
                "source": "synthetic",
                "version": "1",
                "coverage_start": iso_days[0],
                "coverage_end": iso_days[-1],
                **EVIDENCE,
            }
            for name in ("calendar", "raw_bars", "corporate_actions", "derived")
        },
        "symbol_coverage": {
            symbol: {"start": iso_days[0], "end": iso_days[-1]} for symbol in SYMBOLS
        },
        "derivation_rule_version": "synthetic-total-return-v1",
        "calendar": [{"date": day, **EVIDENCE} for day in iso_days],
        "raw_bars": raw_bars,
        "corporate_actions": [
            {
                "date": "2024-01-04",
                "symbol": SYMBOLS[0],
                "type": "cash_dividend",
                "cash_per_share": 0.2,
                "pay_date": "2024-01-06",
                "terms_evidence": "synthetic dividend terms",
                **EVIDENCE,
            }
        ],
        "derived": derived,
    }


DEFAULT_PLAN_INPUT = {
    "schema_version": 1,
    "minimum_samples": {"sessions": 2, "valued_sessions": 2},
    "thresholds": [
        {"id": "max_drawdown", "metric": "max_drawdown", "op": ">=", "value": -0.9},
        {"id": "relative_return", "metric": "return_delta", "op": ">=", "value": -0.01},
    ],
    "stress_costs": {"transaction_cost_bps": 20, "slippage_bps": 10},
    "regimes": {
        "method": "trailing_volatility",
        "lookback": 3,
        "volatility_threshold": 0.02,
        "labels": ["calm", "volatile"],
    },
    "stages": ["validation", "final_holdout"],
    "reason": "thresholds and samples were fixed before opening Validation",
}


def plan_input(**overrides: object) -> dict:
    payload = deepcopy(DEFAULT_PLAN_INPUT)
    payload.update(overrides)
    return payload


def write_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def seal_snapshot(root: Path, payload: dict, name: str = "snapshots") -> dict:
    store = root / name
    source = write_json(root / f"{name}-input.json", payload)
    sealed = ok(
        cli("snapshot", "seal", "--input", str(source), "--store", str(store))
    )
    manifest = ok(
        cli("snapshot", "manifest", "--store", str(store), "--id", sealed["snapshot_id"])
    )
    return {
        "store": store,
        "snapshot_id": sealed["snapshot_id"],
        "manifest": manifest,
        "partitions": {
            partition["name"]: partition for partition in manifest["partitions"]
        },
    }


def create_campaign(project: Path, snapshot_store: Path, snapshot_id: str) -> dict:
    return ok(
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
            "6",
            "--max-seconds",
            "60",
            "--initial-cash",
            "100000",
            "--transaction-cost-bps",
            "5",
            "--slippage-bps",
            "0",
        )
    )


def submit_experiment(
    project: Path,
    campaign_id: str,
    source: Path,
    submission_id: str,
    hypothesis: str,
    family: str,
    parameters: dict,
) -> dict:
    path = write_json(
        source,
        {
            "schema_version": 1,
            "campaign_id": campaign_id,
            "submission_id": submission_id,
            "hypothesis": hypothesis,
            "strategy": {"family": family},
            "parameters": parameters,
        },
    )
    return ok(cli("experiment", "submit", "--project", str(project), "--input", str(path)))


def freeze_candidate(
    project: Path, campaign_id: str, submission_id: str, name: str, conclusion: str
) -> dict:
    return ok(
        cli(
            "candidate",
            "freeze",
            "--project",
            str(project),
            "--campaign-id",
            campaign_id,
            "--submission-id",
            submission_id,
            "--name",
            name,
            "--conclusion",
            conclusion,
        )
    )


@pytest.fixture(scope="module")
def base(tmp_path_factory: pytest.TempPathFactory) -> dict:
    root = tmp_path_factory.mktemp("protected-evaluation")
    sealed = seal_snapshot(root, synthetic_snapshot(), "snapshots")
    project = root / "strategy-a"
    ok(cli("project", "init", "--project", str(project)))
    campaign = create_campaign(project, sealed["store"], sealed["snapshot_id"])
    source = root / "submission.json"
    submit_experiment(
        project, campaign["campaign_id"], source, "first",
        "Equal weights preserve value across a cash dividend.",
        "equal_weight", {},
    )
    candidate_a = freeze_candidate(
        project, campaign["campaign_id"], "first", "candidate-a",
        "Retain equal weights as the frozen candidate.",
    )
    submit_experiment(
        project, campaign["campaign_id"], source, "momentum",
        "Momentum with a short lookback is a legal parameter neighbourhood.",
        "momentum", {"lookback": 2},
    )
    candidate_b = freeze_candidate(
        project, campaign["campaign_id"], "momentum", "candidate-b",
        "Check the momentum candidate's frozen neighbourhood.",
    )
    submit_experiment(
        project, campaign["campaign_id"], source, "unused",
        "A candidate that receives no protected evaluation plan.",
        "trend_filter", {"lookback": 2},
    )
    candidate_c = freeze_candidate(
        project, campaign["campaign_id"], "unused", "candidate-c",
        "No protected plan is created for this candidate.",
    )
    return {
        "root": root,
        "snapshot_store": sealed["store"],
        "snapshot_id": sealed["snapshot_id"],
        "manifest": sealed["manifest"],
        "partitions": sealed["partitions"],
        "project": project,
        "campaign": campaign,
        "candidate_a": candidate_a,
        "candidate_b": candidate_b,
        "candidate_c": candidate_c,
    }


def eval_store(base: dict, name: str) -> Path:
    path = base["root"] / f"eval-{name}"
    path.mkdir()
    return path


def create_plan(
    base: dict,
    store: Path,
    candidate: dict,
    payload: dict,
    *,
    name: str,
    project: Path | None = None,
    snapshot_store: Path | None = None,
) -> dict:
    source = write_json(base["root"] / f"{name}-input.json", payload)
    return ok(
        cli(
            "evaluation",
            "plan",
            "--store",
            str(store),
            "--snapshot-store",
            str(snapshot_store or base["snapshot_store"]),
            "--project",
            str(project or base["project"]),
            "--candidate-id",
            candidate["candidate_id"],
            "--input",
            str(source),
        )
    )


def authorize(
    store: Path,
    plan_id: str,
    *,
    reviewer: str = "reviewer-1",
    reason: str = "Frozen plan reviewed before Validation.",
) -> subprocess.CompletedProcess[str]:
    return cli(
        "evaluation",
        "authorize",
        "--store",
        str(store),
        "--plan-id",
        plan_id,
        "--stage",
        "validation",
        "--reviewer",
        reviewer,
        "--reason",
        reason,
    )


def run_evaluation(
    base: dict,
    store: Path,
    plan_id: str,
    *,
    env: dict[str, str] | None = None,
    project: Path | None = None,
    snapshot_store: Path | None = None,
    stage: str = "validation",
) -> subprocess.CompletedProcess[str]:
    return cli(
        "evaluation",
        "run",
        "--store",
        str(store),
        "--snapshot-store",
        str(snapshot_store or base["snapshot_store"]),
        "--plan-id",
        plan_id,
        "--stage",
        stage,
        "--project",
        str(project or base["project"]),
        env=env,
    )


def ledger_rows(store: Path, table: str) -> list[tuple]:
    database = store / "queries.sqlite"
    if not database.exists():
        return []
    with sqlite3.connect(database) as connection:
        return connection.execute(f"SELECT * FROM {table}").fetchall()


def consumed_rows(store: Path) -> list[tuple]:
    return ledger_rows(store, "consumed_queries")


def evaluation_rows(store: Path) -> list[tuple]:
    return ledger_rows(store, "evaluations")


def test_plan_freezes_descriptors_thresholds_and_costs_before_any_read(
    base: dict,
) -> None:
    store = eval_store(base, "plan")
    result = create_plan(base, store, base["candidate_a"], plan_input(), name="plan-a")
    assert result["schema_version"] == 1
    assert result["status"] == "planned"
    assert result["scope"] == "synthetic"
    assert result["plan_id"] == result["plan_hash"]
    assert re.fullmatch(r"[0-9a-f]{64}", result["plan_id"])

    plan_path = store / "plans" / f"{result['plan_id']}.json"
    assert plan_path.is_file()
    assert plan_path.stat().st_mode & 0o777 == 0o400
    plan = json.loads(plan_path.read_text())
    assert plan["plan_id"] == result["plan_id"]
    assert plan["stages"] == ["validation", "final_holdout"]
    assert plan["partitions"]["validation"] == base["partitions"]["validation"]
    assert plan["partitions"]["final_holdout"] == base["partitions"]["final_holdout"]
    assert plan["candidate_hash"] == base["candidate_a"]["candidate_hash"]
    assert plan["candidate"]["strategy"] == {"family": "equal_weight"}
    assert plan["snapshot_id"] == base["snapshot_id"]
    # Costs are copied from the frozen candidate, never restated by the driver.
    assert plan["base_costs"] == {"transaction_cost_bps": 5.0, "slippage_bps": 0.0}
    assert plan["stress_costs"] == {"transaction_cost_bps": 20, "slippage_bps": 10}
    assert plan["signal_window_sessions"] == 1
    assert plan["conclusion"] == base["candidate_a"]["conclusion"]
    assert plan["execution_rules"] == base["candidate_a"]["execution_rules"]
    assert plan["neighborhood"] == {"no_tunable_parameters": True}
    assert plan["data_kind"] == "synthetic"
    assert "validation" in plan["prior_history"]

    invalid_inputs = {
        "restated-base-costs": {**plan_input(), "base_costs": {"transaction_cost_bps": 5, "slippage_bps": 0}},
        "restated-candidate": {**plan_input(), "candidate_hash": "0" * 64},
        "unknown-metric": plan_input(
            thresholds=[{"id": "x", "metric": "yearly_returns", "op": ">=", "value": 0}]
        ),
        "unknown-operator": plan_input(
            thresholds=[{"id": "x", "metric": "total_return", "op": "~=", "value": 0}]
        ),
        "stress-below-base": plan_input(
            stress_costs={"transaction_cost_bps": 4, "slippage_bps": 0}
        ),
        "no-samples": plan_input(minimum_samples={"sessions": 0, "valued_sessions": 0}),
        "one-stage": plan_input(stages=["validation"]),
        "unknown-regime-bottom": plan_input(
            regimes={
                "method": "trailing_volatility",
                "lookback": 3,
                "volatility_threshold": 0.02,
                "labels": ["calm", "volatile", "other"],
            }
        ),
    }
    for name, payload in invalid_inputs.items():
        source = write_json(base["root"] / f"invalid-{name}.json", payload)
        response = cli(
            "evaluation",
            "plan",
            "--store",
            str(store),
            "--snapshot-store",
            str(base["snapshot_store"]),
            "--project",
            str(base["project"]),
            "--candidate-id",
            base["candidate_a"]["candidate_id"],
            "--input",
            str(source),
        )
        rejected(response, "INVALID_PLAN_INPUT")
    assert len(list((store / "plans").glob("*.json"))) == 1

    replanned = create_plan(
        base,
        store,
        base["candidate_a"],
        plan_input(stress_costs={"transaction_cost_bps": 30, "slippage_bps": 10}),
        name="plan-a-replanned",
    )
    assert replanned["plan_id"] != result["plan_id"]
    assert len(list((store / "plans").glob("*.json"))) == 2


def test_plan_is_bound_to_its_project_and_candidate(base: dict) -> None:
    store = eval_store(base, "binding")
    plan = create_plan(base, store, base["candidate_a"], plan_input(), name="plan-bind")
    other_project = base["root"] / "strategy-other"
    ok(cli("project", "init", "--project", str(other_project)))
    result = cli(
        "evaluation",
        "run",
        "--store",
        str(store),
        "--snapshot-store",
        str(base["snapshot_store"]),
        "--plan-id",
        plan["plan_id"],
        "--stage",
        "validation",
        "--project",
        str(other_project),
    )
    payload = rejected(result, "FINGERPRINT_MISMATCH")
    assert payload["opened_protected_source"] is False
    assert payload["query_debited"] is False
    assert consumed_rows(store) == []


def test_run_without_validation_authorization_is_rejected_before_debit_or_read(
    base: dict,
) -> None:
    store = eval_store(base, "unauthorized")
    plan = create_plan(base, store, base["candidate_a"], plan_input(), name="plan-unauth")
    result = run_evaluation(base, store, plan["plan_id"])
    payload = rejected(result, "AUTHORIZATION_REQUIRED")
    assert payload["opened_protected_source"] is False
    assert payload["query_debited"] is False
    audits = list((store / "audit").glob("*.json"))
    assert len(audits) == 1
    audit = json.loads(audits[0].read_text())
    assert audit["opened_protected_source"] is False
    assert audit["error"]["code"] == "AUTHORIZATION_REQUIRED"
    assert consumed_rows(store) == []
    assert evaluation_rows(store) == []


def test_unverified_isolation_rejects_before_the_debit_or_sealed_read(
    base: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prajnatrade import protected_eval

    store = eval_store(base, "isolation")
    plan = create_plan(base, store, base["candidate_a"], plan_input(), name="plan-isolation")
    ok(authorize(store, plan["plan_id"]))
    monkeypatch.setattr(
        protected_eval,
        "probe_isolation_evidence",
        lambda limits=None: {
            "verified": False,
            "failure_reason": "sandbox_install_failed",
            "memory_limit_enforced": False,
            "sandbox_preflight_verified": False,
            "opened_protected_input": False,
        },
    )
    with pytest.raises(protected_eval.EvaluationRejection) as error:
        protected_eval.run_evaluation(
            store,
            base["snapshot_store"],
            base["project"],
            plan["plan_id"],
            "validation",
        )
    assert error.value.details["code"] == "ISOLATION_UNAVAILABLE"
    assert error.value.details["phase"] == "run"
    assert error.value.context["opened_protected_source"] is False
    assert error.value.context["query_debited"] is False
    assert error.value.context["isolation"] == {
        "verified": False,
        "memory_limit_enforced": False,
        "failure_reason": "sandbox_install_failed",
    }
    assert consumed_rows(store) == []
    assert evaluation_rows(store) == []
    audits = list((store / "audit").glob("*.json"))
    assert len(audits) == 1
    audit = json.loads(audits[0].read_text())
    assert audit["error"]["code"] == "ISOLATION_UNAVAILABLE"
    assert audit["opened_protected_source"] is False
    assert audit["query_debited"] is False
    assert audit["isolation"] == {
        "verified": False,
        "memory_limit_enforced": False,
        "failure_reason": "sandbox_install_failed",
    }


def _fake_probe_success(**overrides: object) -> dict:
    evidence = {
        "probe": "generated_python_worker_sandbox",
        "runtime": "synthetic probe result",
        "sandbox_preflight_verified": True,
        "protected_read_denied": True,
        "protected_write_denied": True,
        "network_denied": True,
        "process_creation_denied": True,
        "strategy_started": True,
        "opened_protected_input": False,
        "memory_limit_enforced": False,
        "failure_reason": "memory_limit_unavailable",
        "uid": 501,
        "profile_sha256": "0" * 64,
        "limits": {"wall_seconds": 10.0},
        "verified": True,
    }
    evidence.update(overrides)
    return evidence


def test_boundary_probe_falls_back_when_only_the_memory_limit_is_unenforceable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from prajnatrade import isolation

    attempts: list[bool] = []

    def fake_runner(
        source: str,
        parameters: dict,
        history: object,
        state: dict,
        *,
        symbols: tuple[str, ...],
        limits: object = None,
        evidence_callback=None,
        enforce_memory_limit: bool = True,
    ) -> dict:
        attempts.append(enforce_memory_limit)
        if enforce_memory_limit:
            raise isolation.IsolationError(
                "OS-isolated strategy aborted: memory_limit_unavailable.",
                evidence={
                    "memory_limit_enforced": False,
                    "sandbox_preflight_verified": False,
                },
            )
        assert evidence_callback is not None
        evidence_callback(
            {
                key: value
                for key, value in _fake_probe_success().items()
                if key not in {"verified", "failure_reason"}
            }
        )
        return {"CASH": 1.0}

    monkeypatch.setattr(isolation, "run_isolated_strategy", fake_runner)
    evidence = isolation.probe_isolation_evidence()
    assert attempts == [True, False]
    assert evidence["verified"] is True
    assert evidence["sandbox_preflight_verified"] is True
    assert evidence["memory_limit_enforced"] is False
    assert evidence["failure_reason"] == "memory_limit_unavailable"


def test_boundary_probe_does_not_retry_after_a_sandbox_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from prajnatrade import isolation

    attempts: list[bool] = []

    def fake_runner(*args: object, **kwargs: object) -> dict:
        attempts.append(bool(kwargs["enforce_memory_limit"]))
        raise isolation.IsolationError(
            "OS-isolated strategy aborted: sandbox_install_failed."
        )

    monkeypatch.setattr(isolation, "run_isolated_strategy", fake_runner)
    evidence = isolation.probe_isolation_evidence()
    assert attempts == [True]
    assert evidence["verified"] is False
    assert evidence["failure_reason"] == "sandbox_install_failed"


def test_generated_python_gate_still_refuses_without_the_memory_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from prajnatrade import isolation

    attempts: list[bool] = []

    def fake_runner(*args: object, **kwargs: object) -> dict:
        attempts.append(bool(kwargs["enforce_memory_limit"]))
        raise isolation.IsolationError(
            "OS-isolated strategy aborted: memory_limit_unavailable.",
            evidence={"memory_limit_enforced": False},
        )

    monkeypatch.setattr(isolation, "run_isolated_strategy", fake_runner)
    with pytest.raises(isolation.IsolationError) as error:
        isolation.probe_isolation()
    assert "memory_limit_unavailable" in str(error.value)
    assert attempts == [True]


def test_verified_boundary_without_memory_limit_passes_and_is_recorded(
    base: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prajnatrade import protected_eval

    store = eval_store(base, "boundary-only")
    plan = create_plan(
        base, store, base["candidate_b"], plan_input(), name="plan-boundary-only"
    )
    ok(authorize(store, plan["plan_id"]))
    monkeypatch.setattr(
        protected_eval, "probe_isolation_evidence", lambda limits=None: _fake_probe_success()
    )
    result = protected_eval.run_evaluation(
        store,
        base["snapshot_store"],
        base["project"],
        plan["plan_id"],
        "validation",
    )
    assert result["status"] == "completed"
    assert result["outcome"] == "passed"
    assert result["isolation"] == {
        "verified": True,
        "memory_limit_enforced": False,
        "failure_reason": "memory_limit_unavailable",
    }
    record = result["record"]
    assert record["isolation_verified"] is True
    assert record["memory_limit_enforced"] is False
    assert record["isolation_failure_reason"] == "memory_limit_unavailable"
    assert record["scoring"] is True
    assert record["outcome"] == "passed"


def test_validation_authorization_is_single_and_bound_to_the_plan(base: dict) -> None:
    store = eval_store(base, "authorize")
    plan = create_plan(base, store, base["candidate_a"], plan_input(), name="plan-auth")
    authorized = ok(authorize(store, plan["plan_id"]))
    assert authorized["status"] == "authorized"
    record = authorized["authorization"]
    assert record["plan_id"] == plan["plan_id"]
    assert record["stage"] == "validation"
    assert record["candidate_hash"] == plan["candidate_hash"]
    assert (
        record["partition_content_hash"]
        == plan["partitions"]["validation"]["content_hash"]
    )
    assert record["scope"] == "synthetic"
    files = list((store / "authorizations" / plan["plan_id"]).glob("validation-*.json"))
    assert len(files) == 1
    assert files[0].stat().st_mode & 0o777 == 0o400

    rejected(authorize(store, plan["plan_id"], reviewer="reviewer-2"), "AUTHORIZATION_CONFLICT")
    assert len(list((store / "authorizations" / plan["plan_id"]).glob("validation-*.json"))) == 1

    status = ok(
        cli("evaluation", "status", "--store", str(store), "--plan-id", plan["plan_id"])
    )
    assert status["plan_id"] == plan["plan_id"]
    assert [item["reviewer"] for item in status["authorizations"]] == ["reviewer-1"]
    assert status["stages"]["validation"]["authorization"]["reviewer"] == "reviewer-1"
    assert status["stages"]["validation"]["evaluation"] is None
    assert status["stages"]["final_holdout"]["evaluation"] is None
    assert status["queries"] == []


def test_authorized_validation_records_full_trusted_evidence_and_coarse_receipt(
    base: dict,
) -> None:
    store = eval_store(base, "happy")
    plan = create_plan(base, store, base["candidate_b"], plan_input(), name="plan-happy")
    authorized = ok(authorize(store, plan["plan_id"]))
    assert authorized["status"] == "authorized"
    result = ok(run_evaluation(base, store, plan["plan_id"]))
    assert result["status"] == "completed"
    assert result["outcome"] == "passed"
    assert result["reason"] is None
    assert result["scoring"] is True
    assert result["query"]["reused"] is False
    assert result["query"]["status"] == "completed"
    record = result["record"]

    assert record["schema_version"] == 1
    assert record["scope"] == "synthetic"
    assert record["data_kind"] == "synthetic"
    assert record["not_admission"] is True
    assert record["not_admission_statement"]
    assert record["fingerprints"]["plan"] == plan["plan_id"]
    assert record["fingerprints"]["candidate"] == base["candidate_b"]["candidate_hash"]
    assert record["fingerprints"]["snapshot_manifest"] == base["snapshot_id"]
    assert (
        record["fingerprints"]["evaluated_partition"]
        == base["partitions"]["validation"]["content_hash"]
    )
    assert record["fingerprints"]["partitions"]["final_holdout"] == (
        base["partitions"]["final_holdout"]["content_hash"]
    )
    assert record["partitions"]["validation"] == base["partitions"]["validation"]
    assert record["partitions"]["final_holdout"] == base["partitions"]["final_holdout"]
    assert record["lineage"] == [base["snapshot_id"]]
    assert record["opened_protected_source"] is True
    assert record["authorization"]["reviewer"] == "reviewer-1"
    assert record["query_receipt"]["partition"] == "validation"
    assert record["query_receipt"]["authorization_id"] == record["authorization"]["auth_id"]
    assert record["query_receipt"]["debited_at"]

    isolation = record["isolation"]
    assert isolation["verified"] is True
    assert isolation["sandbox_preflight_verified"] is True
    assert isolation["profile_sha256"]
    assert isolation["uid"] is not None
    assert isinstance(isolation["limits"], dict)
    assert isolation["memory_limit_enforced"] in {True, False}
    if isolation["memory_limit_enforced"]:
        assert isolation["failure_reason"] is None
    else:
        # The boundary is verified; generated-Python memory enforcement is not.
        assert isolation["failure_reason"] == "memory_limit_unavailable"
    assert record["isolation_verified"] is True
    assert record["memory_limit_enforced"] == isolation["memory_limit_enforced"]
    assert record["isolation_failure_reason"] == isolation["failure_reason"]
    assert result["isolation"] == {
        "verified": True,
        "memory_limit_enforced": isolation["memory_limit_enforced"],
        "failure_reason": isolation["failure_reason"],
    }

    availability = record["availability"]
    assert availability["timezone"].startswith("Asia/Shanghai")
    assert availability["signal_cutoff"] == "next_session_open_09:30"
    assert availability["prior_history_rule"]["partitions"] == ["discovery"]

    coverage = record["coverage"]
    assert coverage["complete"] is True
    assert coverage["samples_sufficient"] is True
    assert coverage["valued_sessions"] == coverage["sessions"] == 4
    assert coverage["snapshot_gaps"] == {
        "raw_bars": [],
        "derived": [],
        "corporate_actions": [],
        "availability": [],
    }
    assert coverage["runner_gaps"]["valuation"] == []
    assert coverage["runner_gaps"]["signal_gaps"] == []

    stress = record["cost_stress"]
    assert set(stress) == {"base", "stress", "stress_minus_base"}
    assert stress["base"]["candidate"]["fees_paid"] == 0.0
    assert stress["base"]["candidate"]["total_return"] == 0.0
    assert stress["stress"]["candidate"]["total_return"] == 0.0
    assert stress["base"]["return_delta"] is not None
    assert stress["stress_minus_base"]["candidate_return_delta"] == 0.0
    assert record["candidate_run"]["base"]["metrics"] == stress["base"]["candidate"]
    assert record["candidate_run"]["stress"]["metrics"] == stress["stress"]["candidate"]
    assert record["baseline_run"]["base"]["metrics"] == stress["base"]["baseline"]

    neighbourhood = record["neighbourhood"]
    assert neighbourhood["applicable"] is True
    assert neighbourhood["diagnostic_only"] is True
    assert neighbourhood["counts"]["variants"] == 2
    assert neighbourhood["counts"]["excluded"] == 2
    assert all(
        variant["status"] in {"stable", "degraded", "not_evaluable"}
        for variant in neighbourhood["variants"]
    )
    assert all(
        variant["reason"] == "metric_unavailable"
        for variant in neighbourhood["variants"]
    )
    assert neighbourhood["excluded"]
    # Each variant must be evaluated with the signal window implied by its own
    # parameters, not the original candidate's window (momentum: lookback + 1).
    windows = {
        tuple(variant["changed_parameters"]): variant["signal_window_sessions"]
        for variant in neighbourhood["variants"]
    }
    assert windows[("lookback",)] == 4
    assert windows[("top_k",)] == 3
    assert record["execution"]["signal_window_sessions"] == 3

    regimes = record["regimes"]
    assert regimes["definition"]["method"] == "trailing_volatility"
    groups = {group["label"]: group for group in regimes["groups"]}
    assert set(groups) == {"calm", "volatile", "unclassified"}
    assert groups["calm"]["sessions"] == 4
    assert groups["calm"]["candidate_samples"] == 3
    assert groups["calm"]["baseline_samples"] == 3
    assert groups["volatile"]["sessions"] == 0
    assert groups["unclassified"]["sessions"] == 0
    assert all(
        item["label"] in {"calm", "volatile", None}
        for item in regimes["session_labels"]
    )

    thresholds = {item["id"]: item for item in record["thresholds"]}
    assert set(thresholds) == {"max_drawdown", "relative_return"}
    for item in record["thresholds"]:
        assert item["status"] == "passed"
        assert item["actual"] is not None
    assert record["verdict"]["failed_thresholds"] == []
    assert record["verdict"]["unavailable_thresholds"] == []
    assert record["verdict"]["coverage_complete"] is True

    curves = record["curves"]
    assert curves["candidate"]["base"][0]["nav"] is not None
    for side in ("candidate", "baseline"):
        for label in ("base", "stress"):
            points = curves[side][label]
            assert [point["date"] for point in points] == curves["sessions"]
            assert all(point["nav"] is not None for point in points)
            assert all(point["drawdown"] is not None for point in points)
    assert curves["candidate"]["base"][-1]["drawdown"] == 0.0

    receipt = result["receipt"]
    assert set(receipt) == {"schema_version", "stage", "status", "reason", "message"}
    assert receipt == {
        "schema_version": 1,
        "stage": "validation",
        "status": "passed",
        "reason": None,
        "message": "All preregistered thresholds and minimum samples were met.",
    }
    assert not re.search(r"\d", receipt["message"])

    status = ok(
        cli("evaluation", "status", "--store", str(store), "--plan-id", plan["plan_id"])
    )
    assert status["stages"]["validation"]["evaluation"]["outcome"] == "passed"
    assert status["stages"]["validation"]["query"]["status"] == "completed"
    assert status["stages"]["final_holdout"]["evaluation"] is None

    agent = ok(
        cli(
            "evaluation",
            "status",
            "--project",
            str(base["project"]),
            "--candidate-id",
            base["candidate_b"]["candidate_id"],
            agent=True,
        )
    )
    assert set(agent) == {
        "schema_version",
        "candidate_id",
        "validation",
        "final_holdout",
    }
    assert agent["validation"] == receipt
    assert agent["final_holdout"] == {
        "schema_version": 1,
        "stage": "final_holdout",
        "status": "accepted",
        "message": "Request received.",
    }


def test_replay_reuses_the_record_without_a_second_debit_and_other_candidates_are_blocked(
    base: dict,
) -> None:
    store = eval_store(base, "replay")
    plan = create_plan(base, store, base["candidate_b"], plan_input(), name="plan-replay")
    ok(authorize(store, plan["plan_id"]))
    first = ok(run_evaluation(base, store, plan["plan_id"]))
    assert first["outcome"] == "passed"
    assert first["query"]["reused"] is False
    assert len(consumed_rows(store)) == 1
    assert len(evaluation_rows(store)) == 1

    second = ok(run_evaluation(base, store, plan["plan_id"]))
    assert second["evaluation_id"] == first["evaluation_id"]
    assert second["query"]["reused"] is True
    assert second["record"] == first["record"]
    assert second["outcome"] == "passed"
    assert len(consumed_rows(store)) == 1
    assert len(evaluation_rows(store)) == 1

    other_plan = create_plan(
        base, store, base["candidate_a"], plan_input(), name="plan-replay-other"
    )
    ok(authorize(store, other_plan["plan_id"]))
    payload = rejected(
        run_evaluation(base, store, other_plan["plan_id"]), "QUERY_ALREADY_CONSUMED"
    )
    assert payload["opened_protected_source"] is False
    assert payload["query_debited"] is False
    assert len(consumed_rows(store)) == 1
    assert len(evaluation_rows(store)) == 1
    audits = [
        json.loads(path.read_text())
        for path in (store / "audit").glob("*.json")
    ]
    assert [audit["error"]["code"] for audit in audits] == ["QUERY_ALREADY_CONSUMED"]
    assert audits[0]["opened_protected_source"] is False


def test_repackaged_snapshot_with_the_same_protected_content_cannot_buy_another_read(
    base: dict,
) -> None:
    store = eval_store(base, "repack")
    first_plan = create_plan(
        base, store, base["candidate_b"], plan_input(), name="plan-repack-first"
    )
    ok(authorize(store, first_plan["plan_id"]))
    first = ok(run_evaluation(base, store, first_plan["plan_id"]))
    assert first["outcome"] == "passed"

    repack_root = base["root"] / "repack"
    repack_root.mkdir()
    repacked = seal_snapshot(
        repack_root,
        synthetic_snapshot(discovery_marker="2024-01-12"),
        "snapshots",
    )
    assert repacked["snapshot_id"] != base["snapshot_id"]
    for name in ("validation", "final_holdout"):
        assert (
            repacked["partitions"][name]["content_hash"]
            == base["partitions"][name]["content_hash"]
        )

    project = repack_root / "strategy-repack"
    ok(cli("project", "init", "--project", str(project)))
    campaign = create_campaign(project, repacked["store"], repacked["snapshot_id"])
    source = repack_root / "submission.json"
    submit_experiment(
        project, campaign["campaign_id"], source, "first",
        "Equal weights preserve value across a cash dividend.",
        "equal_weight", {},
    )
    candidate = freeze_candidate(
        project, campaign["campaign_id"], "first", "candidate-repacked",
        "The repackaged snapshot still hits the consumed protected content.",
    )
    second_plan = create_plan(
        base,
        store,
        candidate,
        plan_input(),
        name="plan-repack-second",
        project=project,
        snapshot_store=repacked["store"],
    )
    assert second_plan["plan_id"] != first_plan["plan_id"]
    assert (
        second_plan["partitions"]["validation"]["content_hash"]
        == base["partitions"]["validation"]["content_hash"]
    )
    ok(authorize(store, second_plan["plan_id"]))
    payload = rejected(
        run_evaluation(
            base,
            store,
            second_plan["plan_id"],
            project=project,
            snapshot_store=repacked["store"],
        ),
        "QUERY_ALREADY_CONSUMED",
    )
    assert payload["opened_protected_source"] is False
    assert len(consumed_rows(store)) == 1
    assert len(evaluation_rows(store)) == 1
    assert len(list((store / "records").glob("*/record.json"))) == 1


def build_scenario(root: Path, payload: dict, name: str) -> dict:
    """Seal an alternative synthetic snapshot and freeze an equal-weight candidate."""
    root.mkdir(parents=True, exist_ok=True)
    sealed = seal_snapshot(root, payload, f"snapshots-{name}")
    project = root / f"project-{name}"
    ok(cli("project", "init", "--project", str(project)))
    campaign = create_campaign(project, sealed["store"], sealed["snapshot_id"])
    source = root / f"submission-{name}.json"
    submit_experiment(
        project,
        campaign["campaign_id"],
        source,
        "first",
        "Equal weights preserve value across a cash dividend.",
        "equal_weight",
        {},
    )
    candidate = freeze_candidate(
        project,
        campaign["campaign_id"],
        "first",
        f"candidate-{name}",
        "Frozen candidate for a protected-evidence scenario.",
    )
    return {
        "root": root,
        "snapshot_store": sealed["store"],
        "snapshot_id": sealed["snapshot_id"],
        "manifest": sealed["manifest"],
        "partitions": sealed["partitions"],
        "project": project,
        "campaign": campaign,
        "candidate": candidate,
    }


def test_threshold_failure_is_rejected_and_missing_nav_is_insufficient_evidence(
    base: dict,
) -> None:
    store = eval_store(base, "threshold")
    failing = plan_input(
        thresholds=[
            {"id": "relative", "metric": "return_delta", "op": ">=", "value": 0.5}
        ]
    )
    plan = create_plan(base, store, base["candidate_a"], failing, name="plan-reject")
    ok(authorize(store, plan["plan_id"]))
    rejected_run = ok(run_evaluation(base, store, plan["plan_id"]))
    assert rejected_run["outcome"] == "rejected"
    assert rejected_run["reason"] == "THRESHOLD_NOT_MET"
    assert rejected_run["scoring"] is True
    record = rejected_run["record"]
    threshold = record["thresholds"][0]
    assert threshold["status"] == "failed"
    assert threshold["actual"] == 0.0
    assert record["verdict"]["failed_thresholds"] == ["relative"]
    assert record["verdict"]["coverage_complete"] is True
    assert rejected_run["receipt"]["status"] == "rejected"
    assert rejected_run["receipt"]["reason"] == "THRESHOLD_NOT_MET"

    missing_root = base["root"] / "missing-close"
    scenario = build_scenario(
        missing_root,
        synthetic_snapshot(missing_close=("2024-01-15", SYMBOLS[1])),
        "missing-close",
    )
    missing_store = missing_root / "eval-missing"
    missing_store.mkdir()
    missing_plan = create_plan(
        scenario,
        missing_store,
        scenario["candidate"],
        plan_input(),
        name="plan-missing",
    )
    ok(authorize(missing_store, missing_plan["plan_id"]))
    missing = ok(run_evaluation(scenario, missing_store, missing_plan["plan_id"]))
    assert missing["outcome"] == "insufficient_evidence"
    assert missing["reason"] == "COVERAGE_INSUFFICIENT"
    assert missing["scoring"] is True
    missing_record = missing["record"]
    assert missing_record["coverage"]["complete"] is False
    assert missing_record["coverage"]["valued_sessions"] < missing_record["coverage"]["sessions"]
    assert any(
        gap["date"] == "2024-01-15"
        for gap in missing_record["coverage"]["runner_gaps"]["valuation"]
    )
    assert any(
        gap["date"] == "2024-01-15"
        for gap in missing_record["coverage"]["snapshot_gaps"]["raw_bars"]
    )
    assert missing_record["verdict"]["coverage_complete"] is False
    assert missing["receipt"]["reason"] == "COVERAGE_INSUFFICIENT"


def test_unavailable_threshold_metric_never_passes(base: dict) -> None:
    store = eval_store(base, "unavailable")
    plan = create_plan(
        base,
        store,
        base["candidate_b"],
        plan_input(
            thresholds=[
                {"id": "sharpe", "metric": "sharpe", "op": ">=", "value": 0.0}
            ]
        ),
        name="plan-unavailable",
    )
    ok(authorize(store, plan["plan_id"]))
    result = ok(run_evaluation(base, store, plan["plan_id"]))
    assert result["outcome"] == "insufficient_evidence"
    assert result["reason"] == "EVIDENCE_INCOMPLETE"
    assert result["scoring"] is True
    record = result["record"]
    assert record["coverage"]["complete"] is True
    threshold = record["thresholds"][0]
    assert threshold["status"] == "unavailable"
    assert threshold["actual"] is None
    assert record["verdict"]["unavailable_thresholds"] == ["sharpe"]
    assert record["verdict"]["failed_thresholds"] == []
    assert result["receipt"]["reason"] == "EVIDENCE_INCOMPLETE"


def test_abort_after_protected_read_keeps_the_debit_and_never_retries(base: dict) -> None:
    store = eval_store(base, "abort")
    plan = create_plan(base, store, base["candidate_b"], plan_input(), name="plan-abort")
    ok(authorize(store, plan["plan_id"]))
    failing_env = cli_env(PRAJNATRADE_TEST_FAILPOINT="evaluation_after_read")
    first = ok(run_evaluation(base, store, plan["plan_id"], env=failing_env))
    assert first["status"] == "aborted"
    assert first["outcome"] == "insufficient_evidence"
    assert first["scoring"] is False
    record = first["record"]
    assert record["aborted"] is True
    assert record["scoring"] is False
    assert record["opened_protected_source"] is True
    assert record["failure"]["type"] == "RuntimeError"
    assert "evaluation_after_read" in record["failure"]["message"]
    assert "curves" not in record
    assert "cost_stress" not in record
    assert "neighbourhood" not in record
    assert record["thresholds"] == []
    assert first["receipt"]["status"] == "insufficient_evidence"
    assert first["receipt"]["reason"] == "EVIDENCE_INCOMPLETE"

    rows = consumed_rows(store)
    assert len(rows) == 1
    assert rows[0][6] == "aborted"  # status column
    evaluations = evaluation_rows(store)
    assert len(evaluations) == 1
    assert evaluations[0][5] == "insufficient_evidence"  # outcome column

    second = ok(run_evaluation(base, store, plan["plan_id"]))
    assert second["evaluation_id"] == first["evaluation_id"]
    assert second["query"]["reused"] is True
    assert second["record"] == first["record"]
    assert second["outcome"] == "insufficient_evidence"
    assert len(consumed_rows(store)) == 1
    assert len(evaluation_rows(store)) == 1


def test_tampered_protected_partition_is_detected_without_scoring(base: dict) -> None:
    store = eval_store(base, "tamper")
    plan = create_plan(base, store, base["candidate_a"], plan_input(), name="plan-tamper")
    ok(authorize(store, plan["plan_id"]))
    directory = base["snapshot_store"] / base["snapshot_id"]
    payload_path = directory / "validation.json"
    original_bytes = payload_path.read_bytes()
    directory.chmod(0o755)
    payload_path.chmod(0o644)
    payload = json.loads(payload_path.read_text())
    payload["raw_bars"][0]["close"] = 11.0
    payload_path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    payload_path.chmod(0o444)
    directory.chmod(0o555)
    try:
        result = ok(run_evaluation(base, store, plan["plan_id"]))
        assert result["status"] == "aborted"
        assert result["outcome"] == "insufficient_evidence"
        assert result["scoring"] is False
        record = result["record"]
        assert record["opened_protected_source"] == "unverified"
        assert "integrity check failed" in record["failure"]["message"]
        assert "curves" not in record
        assert consumed_rows(store)[0][6] == "aborted"
        replay = ok(run_evaluation(base, store, plan["plan_id"]))
        assert replay["record"] == record
        assert len(consumed_rows(store)) == 1
    finally:
        directory.chmod(0o755)
        payload_path.chmod(0o644)
        payload_path.write_bytes(original_bytes)
        payload_path.chmod(0o444)
        directory.chmod(0o555)


def test_concurrent_requests_do_not_double_evaluate(base: dict) -> None:
    store = eval_store(base, "concurrent")
    plan = create_plan(base, store, base["candidate_b"], plan_input(), name="plan-concurrent")
    ok(authorize(store, plan["plan_id"]))
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(lambda _: run_evaluation(base, store, plan["plan_id"]), range(2))
        )
    completed = []
    in_progress = []
    for response in responses:
        if response.returncode == 0:
            completed.append(json.loads(response.stdout))
        else:
            in_progress.append(rejected(response, "EVALUATION_IN_PROGRESS"))
    assert completed, [response.stderr for response in responses]
    assert len({item["evaluation_id"] for item in completed}) == 1
    assert completed[0]["outcome"] == "passed"
    assert len(in_progress) <= 1
    assert len(consumed_rows(store)) == 1
    assert len(evaluation_rows(store)) == 1
    assert len(list((store / "records").glob("*/record.json"))) == 1


def test_agent_surface_only_reads_coarse_receipts(base: dict) -> None:
    store = eval_store(base, "agent")
    plan = create_plan(base, store, base["candidate_c"], plan_input(), name="plan-agent")
    project = str(base["project"])
    common = ("--store", str(store), "--plan-id", plan["plan_id"])
    assert cli("evaluation", "plan", *common, agent=True).returncode == 2
    assert (
        cli(
            "evaluation",
            "authorize",
            *common,
            "--stage",
            "validation",
            "--reviewer",
            "agent",
            "--reason",
            "not allowed",
            agent=True,
        ).returncode
        == 2
    )
    assert (
        cli(
            "evaluation",
            "run",
            "--store",
            str(store),
            "--snapshot-store",
            str(base["snapshot_store"]),
            "--plan-id",
            plan["plan_id"],
            "--stage",
            "validation",
            "--project",
            project,
            agent=True,
        ).returncode
        == 2
    )
    status = ok(
        cli(
            "evaluation",
            "status",
            "--project",
            project,
            "--candidate-id",
            base["candidate_c"]["candidate_id"],
            agent=True,
        )
    )
    assert set(status) == {
        "schema_version",
        "candidate_id",
        "validation",
        "final_holdout",
    }
    assert status["validation"] == {"status": "pending"}
    assert status["final_holdout"] == {
        "schema_version": 1,
        "stage": "final_holdout",
        "status": "accepted",
        "message": "Request received.",
    }
    ok(authorize(store, plan["plan_id"]))


def test_protected_reader_walks_dividend_lineage_without_using_research_access(
    base: dict,
) -> None:
    from prajnatrade.snapshot import read_protected

    root = base["root"] / "lineage"
    sealed = seal_snapshot(root, synthetic_snapshot(), "snapshots-lineage")
    snapshot_id = sealed["snapshot_id"]
    proposal = {
        "schema_version": 1,
        "predecessor_id": snapshot_id,
        "symbol": SYMBOLS[0],
        "ex_date": "2024-01-04",
        "currency": "CNY",
        "old_amount": 0.20,
        "new_amount": 0.18,
        "supplier": {
            "source": "synthetic supplier",
            "archive_reference": "synthetic-archive:28",
            "observed_at": "2024-02-02T10:00:00+08:00",
        },
    }
    store = str(sealed["store"])
    pending = ok(
        cli(
            "snapshot",
            "correction",
            "register",
            "--store",
            store,
            "--input",
            str(write_json(root / "proposal.json", proposal)),
        )
    )
    notice = {
        "kind": "synthetic",
        "authority": "fund_manager",
        "issuer": "Synthetic Fund Manager",
        "url": "https://notices.example.invalid/dividend",
        "reference": "synthetic-notice-28",
        "symbol": SYMBOLS[0],
        "ex_date": "2024-01-04",
        "currency": "CNY",
        "amount": 0.18,
        "available_at": "2024-01-06T09:00:00+08:00",
        "collected_at": "2024-02-02T09:00:00+08:00",
    }
    verified = ok(
        cli(
            "snapshot",
            "correction",
            "verify",
            "--store",
            store,
            "--proposal-id",
            pending["proposal_id"],
            "--input",
            str(write_json(root / "notice.json", notice)),
        )
    )
    successor = ok(
        cli(
            "snapshot",
            "correction",
            "successor",
            "--store",
            store,
            "--proposal-id",
            pending["proposal_id"],
            "--revision-id",
            verified["revision_id"],
        )
    )
    assert successor["snapshot_id"] != snapshot_id
    original = read_protected(sealed["store"], snapshot_id, "validation")
    inherited = read_protected(sealed["store"], successor["snapshot_id"], "validation")
    assert inherited["snapshot_id"] == successor["snapshot_id"]
    assert inherited["content_hash"] == original["content_hash"]
    assert inherited["data"] == original["data"]
    assert inherited["lineage"] == [successor["snapshot_id"], snapshot_id]
    assert inherited["descriptor"] == original["descriptor"]
    with pytest.raises(ValueError):
        read_protected(sealed["store"], successor["snapshot_id"], "discovery")
