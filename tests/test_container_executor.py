"""Real Docker/Colima acceptance tests for the generated-Python executor (#27).

These tests are skipped when no Docker daemon is reachable or the runner image
has not been built.  Build it with:

    DOCKER_HOST=unix://$HOME/.colima/default/docker.sock \
        scripts/issue27-runner/build.sh

The image name can be overridden with ``PRAJNA_ISSUE27_IMAGE``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from cli_helpers import cli, cli_env, ok, write_json
from test_experiment_cli import sealed_snapshot_input
from test_holdout_report_cli import HOLDOUT_RECEIPT, authorize_holdout
from test_protected_eval_cli import (
    authorize,
    create_campaign,
    create_plan,
    freeze_candidate,
    plan_input,
    run_evaluation,
    seal_snapshot,
)
from test_real_snapshot import schema_v2_fixture
from test_shadow_cli import (
    FUTURE,
    ledger_rows,
    lifecycle,
    schema_v2_shadow_feed,
    shadow_report,
    shadow_update,
)

IMAGE = os.environ.get("PRAJNA_ISSUE27_IMAGE", "prajnatrade-issue27-runner:local")


def _docker_status() -> tuple[bool, str]:
    docker = shutil.which("docker")
    if docker is None:
        return False, "docker CLI is not installed"
    try:
        daemon = subprocess.run(
            [docker, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=20,
            env=cli_env(),
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return False, f"Docker/Colima daemon is unavailable: {error}"
    if daemon.returncode != 0:
        return False, "Docker/Colima daemon is unavailable"
    try:
        image = subprocess.run(
            [docker, "image", "inspect", IMAGE],
            capture_output=True,
            text=True,
            timeout=20,
            env=cli_env(),
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return False, f"runner image inspection failed: {error}"
    if image.returncode != 0:
        return False, f"runner image {IMAGE!r} is not built"
    return True, ""


_DOCKER_READY, _DOCKER_REASON = _docker_status()

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not _DOCKER_READY, reason=_DOCKER_REASON),
]

EQUAL_WEIGHT_SOURCE = (
    "def strategy(history, state):\n"
    "    return {symbol: 1.0 / len(ETF_SYMBOLS) for symbol in ETF_SYMBOLS}\n"
)


def test_schema_v2_protected_and_shadow_stages_run_frozen_python_in_the_container(
    tmp_path: Path,
) -> None:
    # Model-generated strategy shape and schema-v2 fields, all with synthetic inputs.
    sealed = seal_snapshot(tmp_path, schema_v2_fixture())
    project = tmp_path / "strategy"
    ok(cli("project", "init", "--project", str(project)))
    campaign = create_campaign(
        project, sealed["store"], sealed["snapshot_id"],
        "--strategy-executor", "container", "--strategy-image", IMAGE,
    )
    request = write_json(tmp_path / "python.json", {
        "schema_version": 1,
        "campaign_id": campaign["campaign_id"],
        "submission_id": "python",
        "hypothesis": "The container strategy can target a fixed ETF weight.",
        "strategy": {
            "family": "python",
            "source": "def strategy(history, state): return {ETF_SYMBOLS[0]: 1.0}",
            "parameter_schema": {
                "parameters": {
                    "lookback": {"type": "integer", "default": 2, "min": 2, "max": 3, "step": 1},
                },
                "constraints": [],
            },
        },
        "parameters": {"lookback": 2},
    })
    ok(cli("experiment", "submit", "--project", str(project), "--input", str(request)))
    candidate = freeze_candidate(
        project, campaign["campaign_id"], "python", "candidate", "Evaluate the fixed ETF target.",
    )
    base = {
        "root": tmp_path, "project": project, "snapshot_store": sealed["store"],
    }
    store = tmp_path / "evaluations"
    plan = create_plan(base, store, candidate, plan_input(), name="plan")
    assert plan["signal_window_sessions"] == 2
    ok(authorize(store, plan["plan_id"]))
    validation = ok(run_evaluation(base, store, plan["plan_id"]))
    grant = ok(authorize_holdout(store, plan["plan_id"]))["authorization"]
    assert grant["validation"]["evaluation_id"] == validation["evaluation_id"]
    holdout = ok(run_evaluation(base, store, plan["plan_id"], stage="final_holdout"))
    assert holdout["receipt"] == HOLDOUT_RECEIPT
    for result in (validation, holdout):
        assert result["outcome"] == "passed"
        record = result["record"]
        assert record["data_kind"] == "real"
        isolation = record["isolation"]
        assert isolation["verified"] and isolation["memory_limit_enforced"]
        assert isolation["invocations"]
        for evidence in [isolation["probe"], *isolation["invocations"]]:
            assert evidence["image_digest"] == campaign["strategy_executor"]["digest"]
            assert all(evidence["isolation_checks"].values())
        assert record["candidate_run"]["base"]["fills"][0]["symbol"] == "511260"
        assert record["neighbourhood"]["counts"]["variants"] == 1
        assert record["neighbourhood"]["variants"][0]["parameters"] == {"lookback": 3}

    release = ok(cli(
        "release", "approve", "--store", str(store), "--plan-id", plan["plan_id"],
        "--reviewer", "fixture reviewer", "--reason", "Verify schema v2 paper tracking.",
    ))
    release_id = release["release_id"]
    sessions = FUTURE[:3]
    ledger = tmp_path / "paper-ledger"
    payload = schema_v2_shadow_feed(sessions)
    feed = write_json(tmp_path / "generated-forward-v2.json", payload)
    ok(lifecycle("enable", store, release_id, sessions[0]))
    ok(shadow_update(ledger, store, feed, sessions[0], at_close=True))

    sizing_bar = next(
        row for row in payload["raw_bars"] if row["symbol"] == "511260" and row["date"] == sessions[1]
    )
    sizing_bar.update(close=None, close_available_at=None)
    write_json(feed, payload)
    interrupted = shadow_update(
        ledger, store, feed, sessions[1], at_close=True,
        env={**cli_env(), "PRAJNATRADE_TEST_FAILPOINT": "shadow_after_fills"},
    )
    assert interrupted.returncode != 0
    assert len(ledger_rows(ledger, "days")) == 1
    assert len(ledger_rows(ledger, "input_snapshots")) == 2
    incomplete = shadow_update(ledger, store, feed, sessions[1], at_close=True)
    assert incomplete.returncode != 0
    assert json.loads(incomplete.stdout)["versions"][0]["reason_code"] == "missing_close"
    assert len(ledger_rows(ledger, "input_snapshots")) == 1

    sizing_bar.update(close=10, close_available_at=f"{sessions[1]}T15:00:00+08:00")
    write_json(feed, payload)
    signalled = ok(shadow_update(ledger, store, feed, sessions[1], at_close=True))
    assert signalled["versions"][0]["committed_date"] == sessions[1]
    settled = ok(shadow_update(ledger, store, feed, sessions[2], at_close=True))["versions"][0]
    assert settled["committed_date"] == sessions[2]
    output = tmp_path / "paper-report.json"
    ok(shadow_report(ledger, release_id, output))
    _, signal_day, settlement_day = json.loads(output.read_text())["days"]
    signal = signal_day["result"]["signal"]
    assert signal["target_weights"]["511260"] == 1.0
    invocation, = signal_day["result"]["strategy_execution_evidence"]
    assert invocation["image_digest"] == campaign["strategy_executor"]["digest"]
    assert all(invocation["isolation_checks"].values())
    [order] = settlement_day["result"]["orders"]
    assert order["status"] == "filled" and order["persisted_before_deadline"]
    assert order["order_id"] == signal_day["result"]["next_orders"][0]["order_id"]
    replayed = ok(shadow_update(ledger, store, feed, sessions[2], at_close=True))["versions"][0]
    assert replayed["status"] == "already_committed"
    assert replayed["result_hash"] == settlement_day["result_hash"]


def _campaign(tmp_path: Path, *, attempts: int = 2) -> tuple[Path, dict]:
    project = tmp_path / "strategy-a"
    source = tmp_path / "discovery.json"
    source.write_text(json.dumps(sealed_snapshot_input(day_count=10)), encoding="utf-8")
    store = tmp_path / "sealed-store"
    assert cli("project", "init", "--project", str(project)).returncode == 0
    sealed = cli("snapshot", "seal", "--input", str(source), "--store", str(store))
    assert sealed.returncode == 0, sealed.stderr
    snapshot_id = json.loads(sealed.stdout)["snapshot_id"]
    created = cli(
        "campaign", "create", "--project", str(project), "--store", str(store),
        "--snapshot-id", snapshot_id, "--attempts", str(attempts),
        "--max-seconds", "120", "--initial-cash", "100000",
        "--transaction-cost-bps", "5", "--slippage-bps", "0",
        "--strategy-executor", "container", "--strategy-image", IMAGE,
    )
    assert created.returncode == 0, created.stderr
    return project, json.loads(created.stdout)


def _submit(
    project: Path, campaign: dict, submission_id: str, source: str,
) -> dict:
    request = {
        "schema_version": 1,
        "campaign_id": campaign["campaign_id"],
        "submission_id": submission_id,
        "hypothesis": "Container-isolated generated Python can trade Discovery.",
        "strategy": {"family": "python", "source": source},
        "parameters": {"lookback": 1},
    }
    path = project.parent / f"{submission_id}.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    completed = cli(
        "experiment", "submit", "--project", str(project), "--input", str(path),
        timeout=180,
    )
    assert completed.returncode in {0, 2}, completed.stderr
    return json.loads(completed.stdout)


def _assert_boundary(receipt: dict, campaign: dict) -> None:
    executor = receipt["executor"]
    assert executor["kind"] == "container"
    assert executor["image"] == {
        "reference": campaign["strategy_executor"]["image"],
        "digest": campaign["strategy_executor"]["digest"],
    }
    assert executor["limits"] == campaign["strategy_executor"]["limits"]
    assert executor["invocation_count"] == len(executor["invocations"]) == 5
    assert executor["evidence_sha256"]
    for invocation in executor["invocations"]:
        assert invocation["run_id"]
        assert invocation["image_reference"] == IMAGE
        assert invocation["image_digest"] == campaign["strategy_executor"]["digest"]
        assert len(invocation["container_id"]) == 64
        inspection = invocation["inspect"]
        assert inspection["mount_count"] == 0
        assert inspection["mounts"] == []
        assert inspection["bind_count"] == 0
        assert inspection["network_mode"] == "none"
        assert inspection["port_count"] == 0
        assert inspection["read_only"] is True
        assert inspection["user"] == "65534:65534"
        assert "ALL" in inspection["cap_drop"]
        assert inspection["cap_add"] == []
        assert inspection["privileged"] is False
        assert inspection["memory_bytes"] == 512 * 1024**2
        assert inspection["pids_limit"] == 64
        assert inspection["nano_cpus"] == 1_000_000_000
        assert "/tmp" in inspection["tmpfs"]
        assert all(invocation["isolation_checks"].values())
        assert len(invocation["input_sha256"]) == 64
        assert invocation["input_bytes"] > 0
        assert len(invocation["output_sha256"]) == 64
        assert invocation["output_bytes"] > 0
        assert invocation["wall_seconds"] > 0
        assert invocation["exit_code"] == 0
        assert invocation["source_started"] is True
        assert invocation["strategy_started"] is True


def test_real_container_strategy_runs_discovery_and_records_boundary(
    tmp_path: Path,
) -> None:
    project, campaign = _campaign(tmp_path, attempts=1)
    receipt = _submit(project, campaign, "container-ok", EQUAL_WEIGHT_SOURCE)

    assert receipt["status"] == "succeeded", receipt.get("error")
    assert receipt["strategy"]["source_sha256"] == hashlib.sha256(
        EQUAL_WEIGHT_SOURCE.encode()
    ).hexdigest()
    discovery = receipt["discovery"]
    assert discovery["metrics"]["fills"] >= 5
    assert discovery["metrics"]["orders_filled"] >= 1
    assert discovery["metrics"]["sessions"] == 6
    assert discovery["start_date"] == "2024-01-01"
    assert discovery["end_date"] == "2024-01-06"
    assert discovery["benchmark"]["sessions"] == 6
    _assert_boundary(receipt, campaign)


def test_container_cannot_read_a_host_sentinel_or_open_a_network_socket(
    tmp_path: Path,
) -> None:
    sentinel = tmp_path / "host-only-sentinel.txt"
    sentinel.write_text("synthetic host sentinel", encoding="utf-8")
    source = (
        "def strategy(history, state):\n"
        "    try:\n"
        f"        open({str(sentinel)!r}, encoding='utf-8').read()\n"
        "    except OSError:\n"
        "        pass\n"
        "    else:\n"
        "        return {'511260': 1.0}\n"
        "    import socket\n"
        "    try:\n"
        "        socket.create_connection(('192.0.2.1', 80), timeout=1).close()\n"
        "    except OSError:\n"
        "        return {CASH: 1.0}\n"
        "    return {'518880': 1.0}\n"
    )
    project, campaign = _campaign(tmp_path, attempts=1)
    receipt = _submit(project, campaign, "denied", source)

    assert receipt["status"] == "succeeded", receipt.get("error")
    discovery = receipt["discovery"]
    assert discovery["fills"] == []
    assert discovery["metrics"]["fills"] == 0
    assert discovery["metrics"]["total_return"] == 0
    _assert_boundary(receipt, campaign)


def test_trusted_side_rejects_invalid_container_weights(
    tmp_path: Path,
) -> None:
    project, campaign = _campaign(tmp_path, attempts=1)
    receipt = _submit(
        project, campaign, "bad-weights",
        "def strategy(history, state):\n    return {'511260': 1.5}\n",
    )

    assert receipt["status"] == "failed"
    assert receipt["error"]["code"] == "EXECUTION_FAILED"
    assert "invalid weights" in receipt["error"]["message"]
    assert receipt["budget"]["attempts_remaining"] == 0
    executor = receipt["error"]["executor"]
    assert executor["kind"] == "container"
    assert executor["invocation_count"] == 1
    failed = executor["invocations"][0]
    assert failed["result"] == "error"
    assert failed["reason"] == "invalid_weights"
    assert failed["weights_valid"] is False
    assert failed["strategy_started"] is True
    assert failed["inspect"]["mount_count"] == 0
