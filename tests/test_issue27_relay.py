"""Public-script tests for the #27 trusted relay and research workspace.

Synthetic only: a temporary trusted project and campaign are created with the
public CLI, the agent CLI is replaced with a local fake, and no container,
network, real snapshot store or real model is used.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from cli_helpers import cli_env
from issue27_helpers import (
    build_workspace,
    fake_agent,
    outbox_text,
    ready_campaign,
    relay,
    run_script,
    sha256,
    submission,
    trusted_campaign,
    workspace_and_project,
    RELAY,
)


def test_relay_once_writes_receipt_sidecar_and_processed(tmp_path: Path) -> None:
    project, research, campaign = workspace_and_project(tmp_path)
    fake = fake_agent(tmp_path)
    request, raw = submission(research, campaign["campaign_id"], "exp-001")
    log = tmp_path / "agent.log"
    env = cli_env({"FAKE_AGENT_LOG": str(log), "FAKE_AGENT_SECRET": "do-not-leak"})

    result = relay(project, research, fake, "--once", env=env)

    assert result.returncode == 0, result.stderr
    receipt_path = research / "outbox" / "exp-001.receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "succeeded"
    assert receipt["experiment_id"] == "experiment-exp-001"
    assert receipt_path.read_bytes() == log.read_bytes()
    meta = json.loads((research / "outbox" / "exp-001.meta.json").read_text(encoding="utf-8"))
    assert meta["input_sha256"] == sha256(raw)
    assert meta["receipt_sha256"] == sha256(receipt_path.read_bytes())
    assert meta["returncode"] == 0
    assert meta["kind"] == "submit"
    assert meta["redacted"] is False
    assert not request.exists()
    assert (research / "processed" / "exp-001.json").read_bytes() == raw
    text = outbox_text(research)
    assert str(project) not in text
    assert str(research) not in text
    assert "do-not-leak" not in text
    assert "<trusted>" in json.dumps(meta["command"])
    assert all(not item.startswith("/") for item in meta["command"])
    assert meta["command"][0] == "<operator>"


def test_relay_writes_a_compact_summary_derived_from_the_receipt(tmp_path: Path) -> None:
    project, research, campaign = workspace_and_project(tmp_path)
    fake = fake_agent(tmp_path)
    submission(research, campaign["campaign_id"], "exp-001")

    result = relay(project, research, fake, "--once", env=cli_env())

    assert result.returncode == 0, result.stderr
    summary_path = research / "outbox" / "exp-001.summary.json"
    assert summary_path.is_file()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    meta = json.loads((research / "outbox" / "exp-001.meta.json").read_text(encoding="utf-8"))
    assert summary["kind"] == "receipt_summary"
    assert summary["status"] == "succeeded"
    assert summary["receipt_sha256"] == meta["receipt_sha256"]
    assert summary["discovery"]["metrics"] == {"total_return": 0.01}
    assert summary["discovery"]["equity_curve_points"] == 0
    assert "\"equity_curve\":" not in json.dumps(summary)
    assert str(project) not in summary_path.read_text(encoding="utf-8")


def test_relay_redacts_trusted_paths_from_receipts(tmp_path: Path) -> None:
    project, research, campaign = workspace_and_project(tmp_path)
    fake = fake_agent(tmp_path)
    submission(research, campaign["campaign_id"], "exp-leak")
    sealed_store = tmp_path / "trusted" / "store"
    env = cli_env({
        "FAKE_AGENT_MODE": "leak",
        "FAKE_TRUSTED_PATH": str(project),
        "FAKE_STORE_PATH": str(sealed_store),
    })

    result = relay(
        project, research, fake, "--once", "--redact-path", str(sealed_store), env=env
    )

    assert result.returncode == 0, result.stderr
    text = outbox_text(research)
    assert str(project) not in text
    assert ".prajnatrade" not in text
    assert str(sealed_store) not in text
    assert "<trusted>" in text
    receipt = json.loads((research / "outbox" / "exp-leak.receipt.json").read_text())
    assert "<trusted>" in receipt["store_note"]
    receipt = json.loads((research / "outbox" / "exp-leak.receipt.json").read_text())
    assert "<trusted>" in receipt["note"]
    meta = json.loads((research / "outbox" / "exp-leak.meta.json").read_text())
    assert meta["redacted"] is True
    assert meta["receipt_sha256"] == sha256(
        (research / "outbox" / "exp-leak.receipt.json").read_bytes()
    )


def test_relay_forwards_a_protocol_rejection(tmp_path: Path) -> None:
    project, research, campaign = workspace_and_project(tmp_path)
    fake = fake_agent(tmp_path)
    submission(research, campaign["campaign_id"], "exp-budget")
    env = cli_env({"FAKE_AGENT_MODE": "reject"})

    result = relay(project, research, fake, "--once", env=env)

    assert result.returncode == 0, result.stderr
    receipt = json.loads((research / "outbox" / "exp-budget.receipt.json").read_text())
    assert receipt["status"] == "rejected"
    assert receipt["error"]["code"] == "BUDGET_EXHAUSTED"
    meta = json.loads((research / "outbox" / "exp-budget.meta.json").read_text())
    assert meta["returncode"] == 2
    assert (research / "processed" / "exp-budget.json").is_file()


def test_relay_failure_without_a_json_receipt_does_not_leak_stderr(tmp_path: Path) -> None:
    project, research, campaign = workspace_and_project(tmp_path)
    fake = fake_agent(tmp_path)
    submission(research, campaign["campaign_id"], "exp-crash")
    env = cli_env({"FAKE_AGENT_MODE": "crash", "FAKE_TRUSTED_PATH": str(project)})

    result = relay(project, research, fake, "--once", env=env)

    assert result.returncode == 0, result.stderr
    receipt = json.loads((research / "outbox" / "exp-crash.receipt.json").read_text())
    assert receipt["status"] == "rejected"
    assert receipt["error"]["code"] == "RELAY_AGENT_FAILED"
    meta = json.loads((research / "outbox" / "exp-crash.meta.json").read_text())
    assert meta["returncode"] == 3
    text = outbox_text(research)
    assert str(project) not in text
    assert "boom" not in text


def test_relay_local_rejections_write_receipts_and_archive_requests(tmp_path: Path) -> None:
    project, research, campaign = workspace_and_project(tmp_path)
    fake = fake_agent(tmp_path)
    (research / "inbox" / "bad.json").write_text("not json", encoding="utf-8")
    mismatch = {
        "schema_version": 1, "campaign_id": "other-campaign",
        "submission_id": "exp-mismatch", "hypothesis": "h",
        "strategy": {"family": "equal_weight"}, "parameters": {},
    }
    (research / "inbox" / "mismatch.json").write_text(json.dumps(mismatch), encoding="utf-8")
    (research / "inbox" / "array.json").write_text("[]", encoding="utf-8")
    (research / "inbox" / "status-bad.json").write_text(
        json.dumps({"schema_version": 1, "request": "status"}), encoding="utf-8"
    )
    log = tmp_path / "agent.log"
    env = cli_env({"FAKE_AGENT_LOG": str(log)})

    result = relay(project, research, fake, "--once", env=env)

    assert result.returncode == 0, result.stderr
    assert not log.exists(), "locally rejected requests must not reach the agent CLI"
    bad = json.loads((research / "outbox" / "bad.receipt.json").read_text())
    assert bad["error"]["code"] == "RELAY_INVALID_JSON"
    mismatch_receipt = json.loads(
        (research / "outbox" / "exp-mismatch.receipt.json").read_text()
    )
    assert mismatch_receipt["error"]["code"] == "RELAY_CAMPAIGN_MISMATCH"
    assert mismatch_receipt["expected_campaign_id"] == campaign["campaign_id"]
    array = json.loads((research / "outbox" / "array.receipt.json").read_text())
    assert array["error"]["code"] == "RELAY_INVALID_SUBMISSION"
    status_bad = json.loads((research / "outbox" / "status-bad.receipt.json").read_text())
    assert status_bad["error"]["code"] == "RELAY_INVALID_REQUEST"
    assert (research / "outbox" / "status-bad.status.meta.json").is_file()
    assert len(list((research / "inbox").glob("*.json"))) == 0
    assert len(list((research / "processed").glob("*.json"))) == 4
    assert json.loads((research / "outbox" / "bad.meta.json").read_text())["returncode"] is None


def test_relay_status_mode_answers_by_submission_id(tmp_path: Path) -> None:
    project, research, _campaign = workspace_and_project(tmp_path)
    fake = fake_agent(tmp_path)
    log = tmp_path / "agent.log"
    env = cli_env({"FAKE_AGENT_MODE": "not-found", "FAKE_AGENT_LOG": str(log)})

    result = relay(project, research, fake, "--status", "exp-404", env=env)

    assert result.returncode == 0, result.stderr
    receipt = json.loads((research / "outbox" / "exp-404.receipt.json").read_text())
    assert receipt["error"]["code"] == "NOT_FOUND"
    meta = json.loads((research / "outbox" / "exp-404.status.meta.json").read_text())
    assert meta["kind"] == "status"
    assert "status" in meta["command"]
    assert meta["returncode"] == 2
    assert "status" in log.read_text(encoding="utf-8")


def test_relay_answers_an_inbox_status_request(tmp_path: Path) -> None:
    project, research, campaign = workspace_and_project(tmp_path)
    fake = fake_agent(tmp_path)
    request = {
        "schema_version": 1,
        "request": "status",
        "submission_id": "exp-001",
        "campaign_id": campaign["campaign_id"],
    }
    (research / "inbox" / "status-exp-001.json").write_text(json.dumps(request), encoding="utf-8")

    result = relay(project, research, fake, "--once")

    assert result.returncode == 0, result.stderr
    receipt = json.loads((research / "outbox" / "exp-001.receipt.json").read_text())
    assert receipt["status"] == "succeeded"
    assert (research / "outbox" / "exp-001.status.meta.json").is_file()
    assert not (research / "outbox" / "exp-001.meta.json").exists()
    assert (research / "processed" / "status-exp-001.json").is_file()


def test_relay_max_submissions_bounds_each_process(tmp_path: Path) -> None:
    project, research, campaign = workspace_and_project(tmp_path)
    fake = fake_agent(tmp_path)
    submission(research, campaign["campaign_id"], "a-first")
    submission(research, campaign["campaign_id"], "b-second")
    log = tmp_path / "agent.log"
    env = cli_env({"FAKE_AGENT_LOG": str(log)})

    first = relay(project, research, fake, "--once", "--max-submissions", "1", env=env)

    assert first.returncode == 0, first.stderr
    assert (research / "outbox" / "a-first.receipt.json").is_file()
    assert not (research / "outbox" / "b-second.receipt.json").exists()
    assert (research / "inbox" / "b-second.json").is_file()

    second = relay(project, research, fake, "--once", "--max-submissions", "1", env=env)

    assert second.returncode == 0, second.stderr
    assert (research / "outbox" / "b-second.receipt.json").is_file()
    assert len(list((research / "inbox").glob("*.json"))) == 0
    assert len(log.read_text(encoding="utf-8").splitlines()) == 2


def test_relay_refuses_a_research_dir_inside_the_project(tmp_path: Path) -> None:
    project, _store, campaign = trusted_campaign(tmp_path)
    research = build_workspace(
        tmp_path, campaign=ready_campaign(campaign["campaign_id"])
    )
    unsafe = project / "research"
    unsafe.mkdir()
    (unsafe / "inbox").mkdir()
    fake = fake_agent(tmp_path)
    submission(unsafe, campaign["campaign_id"], "exp-unsafe")

    inside = relay(project, unsafe, fake, "--once")

    assert inside.returncode == 2
    assert "separate" in inside.stderr
    assert not (unsafe / "outbox" / "exp-unsafe.receipt.json").exists()
    assert (unsafe / "inbox" / "exp-unsafe.json").is_file()

    (research / ".prajnatrade").mkdir()
    (research / ".prajnatrade" / "project.sqlite").write_bytes(b"not a database")
    contaminated = relay(project, research, fake, "--once")

    assert contaminated.returncode == 2
    assert "trusted project data" in contaminated.stderr


def test_relay_help_documents_the_contract() -> None:
    result = run_script(RELAY, "--help")
    assert result.returncode == 0, result.stderr
    for option in ("--once", "--serve", "--status", "--max-submissions", "--agent-command"):
        assert option in result.stdout


def test_relay_uses_the_real_agent_cli_on_a_synthetic_campaign(tmp_path: Path) -> None:
    project, research, campaign = workspace_and_project(tmp_path)
    submission(research, campaign["campaign_id"], "exp-real-cli")

    result = run_script(
        RELAY,
        "--project", str(project),
        "--research-dir", str(research),
        "--once",
    )

    assert result.returncode == 0, result.stderr
    receipt = json.loads((research / "outbox" / "exp-real-cli.receipt.json").read_text())
    assert receipt["status"] == "succeeded"
    assert receipt["data_kind"] == "synthetic"
    assert receipt["experiment_id"]
    assert receipt["discovery"]["metrics"]["total_return"] is not None
    meta = json.loads((research / "outbox" / "exp-real-cli.meta.json").read_text())
    assert meta["returncode"] == 0
    assert meta["kind"] == "submit"
    assert "experiment" in meta["command"]
    assert "submit" in meta["command"]


def test_relay_serve_processes_until_the_limit_then_stops(tmp_path: Path) -> None:
    project, research, campaign = workspace_and_project(tmp_path)
    fake = fake_agent(tmp_path)
    submission(research, campaign["campaign_id"], "a-first")
    submission(research, campaign["campaign_id"], "b-second")

    result = relay(
        project, research, fake,
        "--serve", "--poll-interval", "0.01", "--max-submissions", "1",
    )

    assert result.returncode == 0, result.stderr
    assert "reached; stopping" in result.stderr
    assert (research / "outbox" / "a-first.receipt.json").is_file()
    assert not (research / "outbox" / "b-second.receipt.json").exists()
    assert (research / "inbox" / "b-second.json").is_file()


def test_relay_forwards_a_python_rejection_on_a_declarative_campaign(
    tmp_path: Path,
) -> None:
    project, research, campaign = workspace_and_project(tmp_path)
    payload = {
        "schema_version": 1,
        "campaign_id": campaign["campaign_id"],
        "submission_id": "exp-python",
        "hypothesis": "A generated strategy should be rejected without Docker.",
        "strategy": {
            "family": "python",
            "source": "def strategy(history, state):\n    return {CASH: 1.0}\n",
        },
        "parameters": {},
    }
    (research / "inbox" / "exp-python.json").write_text(json.dumps(payload), encoding="utf-8")

    result = run_script(
        RELAY,
        "--project", str(project),
        "--research-dir", str(research),
        "--once",
    )

    assert result.returncode == 0, result.stderr
    receipt = json.loads((research / "outbox" / "exp-python.receipt.json").read_text())
    assert receipt["status"] == "rejected"
    assert receipt["error"]["code"] == "INVALID_SUBMISSION"
    assert "container" in receipt["error"]["message"]
    assert receipt["budget"]["attempts_remaining"] == 3
