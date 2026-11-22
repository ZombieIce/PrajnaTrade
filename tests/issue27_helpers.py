"""Shared helpers for the issue #27 public-script tests.

The workspace builder, trusted relay and campaign setup are standalone scripts;
these helpers run them as subprocesses and build synthetic trusted fixtures with
the public CLI.  No container, network, real store or real model is involved.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
import sys
from pathlib import Path

from cli_helpers import cli, cli_env, repo_root
from test_experiment_cli import sealed_snapshot_input

ROOT = repo_root()
SCRIPTS = ROOT / "scripts"
RELAY = SCRIPTS / "issue27_relay.py"
WORKSPACE = SCRIPTS / "issue27_research_workspace.py"
SETUP = SCRIPTS / "issue27_setup_campaign.py"

FAKE_AGENT = '''\
import json
import os
import pathlib
import sys


def option(name):
    return sys.argv[sys.argv.index(name) + 1]


def emit(value, log):
    text = json.dumps(value, sort_keys=True) + "\\n"
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(text)
    print(text, end="")


log = os.environ.get("FAKE_AGENT_LOG")
mode = os.environ.get("FAKE_AGENT_MODE", "ok")
command = sys.argv[1:3]
if command == ["campaign", "create"] and "--help" in sys.argv:
    print("usage: campaign create --strategy-executor none|container --strategy-image IMAGE")
    raise SystemExit(0)
if command == ["project", "init"]:
    project = pathlib.Path(option("--project"))
    (project / ".prajnatrade").mkdir(parents=True, exist_ok=True)
    (project / ".prajnatrade" / "project.sqlite").write_bytes(b"stub")
    print(json.dumps({"schema_version": 1, "project_id": "stub-project"}))
    raise SystemExit(0)
if command == ["snapshot", "manifest"]:
    print(json.dumps({
        "snapshot_id": option("--id"), "data_kind": "real",
        "partitions": [{
            "name": "discovery", "start": "2022-05-20", "end": "2025-01-27",
            "sessions": 656, "content_hash": "e" * 64,
        }],
    }))
    raise SystemExit(0)
if command == ["campaign", "create"]:
    print(json.dumps({
        "schema_version": 1, "project_id": "stub-project",
        "campaign_id": "stub-campaign-0001", "snapshot_id": option("--snapshot-id"),
        "data_kind": "real",
        "fingerprints": {"snapshot": "a" * 64, "partition": "b" * 64, "evaluation": "c" * 64},
        "budget": {"attempts_remaining": 3, "execution_seconds_remaining": 90.0},
        "strategy_executor": {
            "kind": "container", "image": option("--strategy-image"),
            "digest": "sha256:" + "d" * 64,
            "limits": {"uid": 65534, "network": False, "read_only_root": True},
        },
    }))
    raise SystemExit(0)
if mode == "crash":
    trusted = os.environ.get("FAKE_TRUSTED_PATH", "/tmp/trusted")
    print("boom " + trusted + "/.prajnatrade/project.sqlite", file=sys.stderr)
    raise SystemExit(3)
if command == ["experiment", "submit"]:
    request = json.loads(pathlib.Path(option("--input")).read_text())
    submission_id = request["submission_id"]
    if mode == "reject":
        emit({
            "schema_version": 1, "status": "rejected", "submission_id": submission_id,
            "campaign_id": request["campaign_id"],
            "error": {"code": "BUDGET_EXHAUSTED", "phase": "admission",
                      "next_action": "stop", "message": "synthetic rejection"},
        }, log)
        raise SystemExit(2)
    receipt = {
        "schema_version": 1, "status": "succeeded", "submission_id": submission_id,
        "campaign_id": request["campaign_id"],
        "experiment_id": "experiment-" + submission_id,
        "parent_experiment_id": request.get("parent_experiment_id"),
        "budget": {"attempts_remaining": 2, "execution_seconds_remaining": 88.5},
        "discovery": {"metrics": {"total_return": 0.01}},
    }
    if mode == "leak":
        receipt["note"] = (
            os.environ["FAKE_TRUSTED_PATH"] + "/.prajnatrade/project.sqlite"
        )
        if os.environ.get("FAKE_STORE_PATH"):
            receipt["store_note"] = os.environ["FAKE_STORE_PATH"] + "/sealed/discovery.json"
    emit(receipt, log)
    raise SystemExit(0)
if command == ["experiment", "status"]:
    submission_id = option("--submission-id")
    if mode == "not-found":
        emit({
            "schema_version": 1, "status": "rejected", "submission_id": submission_id,
            "campaign_id": option("--campaign-id"),
            "error": {"code": "NOT_FOUND", "phase": "status",
                      "next_action": "check_id", "message": "unknown submission"},
        }, log)
        raise SystemExit(2)
    emit({
        "schema_version": 1, "status": "succeeded", "submission_id": submission_id,
        "campaign_id": option("--campaign-id"),
        "experiment_id": "experiment-" + submission_id,
        "budget": {"attempts_remaining": 1, "execution_seconds_remaining": 70.0},
    }, log)
    raise SystemExit(0)
print("unsupported fake agent invocation", file=sys.stderr)
raise SystemExit(9)
'''


def run_script(
    script: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        env=env or cli_env(),
        cwd=ROOT,
        timeout=120,
        check=False,
    )


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def ready_campaign(campaign_id: str) -> dict:
    return {
        "schema_version": 1,
        "status": "ready",
        "campaign_id": campaign_id,
        "snapshot_id": "a" * 64,
        "data_kind": "real",
        "discovery": {
            "start": "2024-01-02",
            "end": "2024-01-11",
            "sessions": 6,
            "content_hash": "b" * 64,
        },
        "budgets": {"attempts": 3, "execution_seconds": 30},
        "executor": {
            "kind": "container",
            "image": "python:issue27-runner",
            "digest": "sha256:" + "c" * 64,
            "limits": {"uid": 65534, "network": False, "read_only_root": True},
        },
    }


def build_workspace(tmp_path: Path, *, campaign: dict | None = None) -> Path:
    output = tmp_path / "research"
    args = ["--output", str(output)]
    if campaign is not None:
        metadata = tmp_path / "public-campaign.json"
        metadata.write_text(json.dumps(campaign), encoding="utf-8")
        args += ["--campaign-json", str(metadata)]
    result = run_script(WORKSPACE, *args)
    assert result.returncode == 0, result.stderr
    return output


def trusted_campaign(tmp_path: Path) -> tuple[Path, Path, dict]:
    source = tmp_path / "discovery.json"
    source.write_text(json.dumps(sealed_snapshot_input(15)), encoding="utf-8")
    project = tmp_path / "trusted" / "project"
    store = tmp_path / "trusted" / "store"
    initialized = cli("project", "init", "--project", str(project))
    assert initialized.returncode == 0, initialized.stderr
    sealed = cli("snapshot", "seal", "--input", str(source), "--store", str(store))
    assert sealed.returncode == 0, sealed.stderr
    snapshot_id = json.loads(sealed.stdout)["snapshot_id"]
    created = cli(
        "campaign", "create", "--project", str(project), "--store", str(store),
        "--snapshot-id", snapshot_id, "--attempts", "3", "--max-seconds", "30",
    )
    assert created.returncode == 0, created.stderr
    return project, store, json.loads(created.stdout)


def fake_agent(tmp_path: Path) -> Path:
    path = tmp_path / "fake_agent.py"
    path.write_text(FAKE_AGENT, encoding="utf-8")
    return path


def workspace_and_project(tmp_path: Path) -> tuple[Path, Path, dict]:
    project, _store, campaign = trusted_campaign(tmp_path)
    research = build_workspace(tmp_path, campaign=ready_campaign(campaign["campaign_id"]))
    return project, research, campaign


def relay(
    project: Path,
    research: Path,
    fake: Path,
    *extra: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return run_script(
        RELAY,
        "--project", str(project),
        "--research-dir", str(research),
        "--agent-command", shlex.join([sys.executable, str(fake)]),
        *extra,
        env=env,
    )


def submission(
    research: Path,
    campaign_id: str,
    submission_id: str,
    *,
    parent: str | None = None,
) -> tuple[Path, bytes]:
    payload = {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "submission_id": submission_id,
        "hypothesis": "A synthetic hypothesis for the relay test.",
        "strategy": {"family": "equal_weight"},
        "parameters": {},
    }
    if parent is not None:
        payload["parent_experiment_id"] = parent
    path = research / "inbox" / f"{submission_id}.json"
    raw = json.dumps(payload).encode("utf-8")
    path.write_bytes(raw)
    return path, raw


def outbox_text(research: Path) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((research / "outbox").iterdir())
        if path.is_file()
    )




STUB_OPERATOR = '''\
import json
import pathlib
import sys


def option(name):
    return sys.argv[sys.argv.index(name) + 1]


args = sys.argv[1:]
if args[:2] == ["campaign", "create"] and "--help" in args:
    print("usage: campaign create --strategy-executor {none,container} --strategy-image IMAGE")
    raise SystemExit(0)
if args[:2] == ["project", "init"]:
    project = pathlib.Path(option("--project"))
    (project / ".prajnatrade").mkdir(parents=True, exist_ok=True)
    (project / ".prajnatrade" / "project.sqlite").write_bytes(b"stub")
    print(json.dumps({"schema_version": 1, "project_id": "stub-project"}))
    raise SystemExit(0)
if args[:2] == ["campaign", "create"]:
    image = option("--strategy-image") if "--strategy-image" in args else None
    executor = (
        {
            "kind": "container",
            "image": image,
            "digest": "sha256:" + "d" * 64,
            "limits": {"uid": 65534, "network": False, "read_only_root": True},
        }
        if image
        else {"kind": "none"}
    )
    print(json.dumps({
        "schema_version": 1, "project_id": "stub-project",
        "campaign_id": "stub-campaign-0001", "snapshot_id": option("--snapshot-id"),
        "data_kind": "real",
        "fingerprints": {"snapshot": "a" * 64, "partition": "b" * 64, "evaluation": "c" * 64},
        "budget": {"attempts_remaining": 3, "execution_seconds_remaining": 90.0},
        "strategy_executor": executor,
    }))
    raise SystemExit(0)
if args[:2] == ["snapshot", "manifest"]:
    print(json.dumps({
        "snapshot_id": option("--id"), "data_kind": "real",
        "partitions": [{
            "name": "discovery", "start": "2022-05-20", "end": "2025-01-27",
            "sessions": 656, "content_hash": "e" * 64,
        }],
    }))
    raise SystemExit(0)
print("unsupported stub operator invocation", file=sys.stderr)
raise SystemExit(9)
'''


def stub_operator(tmp_path: Path) -> Path:
    path = tmp_path / "stub_operator.py"
    path.write_text(STUB_OPERATOR, encoding="utf-8")
    return path


def setup_campaign(
    store: Path,
    snapshot_id: str,
    project: Path,
    research: Path,
    *extra: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return run_script(
        SETUP,
        "--store", str(store),
        "--snapshot-id", snapshot_id,
        "--trusted-project", str(project),
        "--research-dir", str(research),
        *extra,
        env=env,
    )


def read_campaign(research: Path) -> dict:
    return json.loads((research / "campaign.json").read_text(encoding="utf-8"))


def write_public_metadata(tmp_path: Path, campaign: dict) -> Path:
    path = tmp_path / "public-campaign.json"
    path.write_text(json.dumps(campaign), encoding="utf-8")
    return path
