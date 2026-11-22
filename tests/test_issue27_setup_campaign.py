"""Tests for the trusted #27 campaign setup script.

The real snapshot store is a temporary synthetic store created by the public
CLI, or a stub operator prints the campaign/manifest receipts.  No Docker, no
container image and no real campaign is touched.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

from cli_helpers import cli
from issue27_helpers import (
    build_workspace,
    read_campaign,
    ready_campaign,
    setup_campaign,
    stub_operator,
    trusted_campaign,
)

SNAPSHOT_ID = "a" * 64


def stub_extra(stub: Path) -> tuple[str, ...]:
    return ("--operator-command", shlex.join([sys.executable, str(stub)]))


def test_dry_run_prints_the_plan_and_writes_nothing(tmp_path: Path) -> None:
    store = tmp_path / "store"
    store.mkdir()
    project = tmp_path / "trusted" / "project"
    research = build_workspace(tmp_path)
    stub = stub_operator(tmp_path)
    before = (research / "campaign.json").read_bytes()

    result = setup_campaign(
        store, SNAPSHOT_ID, project, research,
        "--strategy-image", "python:issue27-runner",
        *stub_extra(stub),
        "--dry-run",
    )

    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["dry_run"] is True
    assert plan["would_create"] is True
    assert plan["existing_campaigns"] == []
    create_command = plan["commands"][1]
    assert "--strategy-executor" in create_command
    assert "container" in create_command
    assert "--strategy-image" in create_command
    assert plan["campaign_config"]["strategy_executor"] == {
        "kind": "container",
        "image": "python:issue27-runner",
    }
    assert not project.exists()
    assert (research / "campaign.json").read_bytes() == before


def test_setup_creates_the_container_campaign_and_public_metadata(tmp_path: Path) -> None:
    store = tmp_path / "store"
    store.mkdir()
    project = tmp_path / "trusted" / "project"
    research = build_workspace(tmp_path)
    stub = stub_operator(tmp_path)

    result = setup_campaign(
        store, SNAPSHOT_ID, project, research,
        "--strategy-image", "python:issue27-runner",
        "--attempts", "4", "--max-seconds", "120",
        *stub_extra(stub),
    )

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["action"] == "created"
    assert summary["campaign_id"] == "stub-campaign-0001"
    assert summary["executor"] == "container"
    assert summary["image_digest"] == "sha256:" + "d" * 64
    campaign = read_campaign(research)
    assert campaign["status"] == "ready"
    assert campaign["campaign_id"] == "stub-campaign-0001"
    assert campaign["snapshot_id"] == SNAPSHOT_ID
    assert campaign["data_kind"] == "real"
    assert campaign["budgets"] == {"attempts": 4, "execution_seconds": 120.0}
    assert campaign["discovery"] == {
        "start": "2022-05-20",
        "end": "2025-01-27",
        "sessions": 656,
        "content_hash": "e" * 64,
    }
    assert campaign["executor"] == {
        "kind": "container",
        "image": "python:issue27-runner",
        "digest": "sha256:" + "d" * 64,
        "limits": {"uid": 65534, "network": False, "read_only_root": True},
    }
    text = (research / "campaign.json").read_text(encoding="utf-8")
    assert str(store) not in text
    assert str(project) not in text
    assert (project / ".prajnatrade" / "project.sqlite").is_file()
    # The workspace itself never receives the trusted store or project data.
    for path in research.rglob("*"):
        if path.is_file():
            assert str(store) not in path.read_text(encoding="utf-8", errors="ignore")


def test_setup_refuses_a_different_existing_campaign(tmp_path: Path) -> None:
    project, store, campaign = trusted_campaign(tmp_path)
    research = build_workspace(tmp_path, campaign=ready_campaign(campaign["campaign_id"]))
    stub = stub_operator(tmp_path)
    before = (research / "campaign.json").read_bytes()

    result = setup_campaign(
        store, campaign["snapshot_id"], project, research,
        "--strategy-image", "python:issue27-runner",
        "--attempts", "3", "--max-seconds", "30",
        *stub_extra(stub),
    )

    assert result.returncode == 2
    assert "different config" in result.stderr
    assert "strategy_executor" in result.stderr
    assert (research / "campaign.json").read_bytes() == before


def test_setup_skip_create_adopts_an_existing_declarative_campaign(tmp_path: Path) -> None:
    project, store, campaign = trusted_campaign(tmp_path)
    research = build_workspace(tmp_path)

    result = setup_campaign(
        store, campaign["snapshot_id"], project, research,
        "--skip-create", "--strategy-executor", "none",
        "--attempts", "3", "--max-seconds", "30",
    )

    assert result.returncode == 0, result.stderr
    manifest = cli(
        "snapshot", "manifest", "--store", str(store), "--id", campaign["snapshot_id"]
    )
    assert manifest.returncode == 0, manifest.stderr
    discovery = next(
        partition
        for partition in json.loads(manifest.stdout)["partitions"]
        if partition["name"] == "discovery"
    )
    installed = read_campaign(research)
    assert installed["campaign_id"] == campaign["campaign_id"]
    assert installed["data_kind"] == "synthetic"
    assert installed["executor"]["kind"] == "none"
    assert installed["discovery"] == {
        key: discovery[key] for key in ("start", "end", "sessions", "content_hash")
    }
    assert "data_kind is not 'real'" in result.stderr


def test_setup_force_new_creates_a_fresh_campaign(tmp_path: Path) -> None:
    project, store, campaign = trusted_campaign(tmp_path)
    research = build_workspace(tmp_path)
    stub = stub_operator(tmp_path)

    result = setup_campaign(
        store, campaign["snapshot_id"], project, research,
        "--strategy-image", "python:issue27-runner",
        "--attempts", "3", "--max-seconds", "30",
        "--force-new",
        *stub_extra(stub),
    )

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["action"] == "created"
    assert summary["campaign_id"] == "stub-campaign-0001"
    assert read_campaign(research)["campaign_id"] == "stub-campaign-0001"


def test_setup_requires_the_container_image(tmp_path: Path) -> None:
    store = tmp_path / "store"
    store.mkdir()
    research = build_workspace(tmp_path)

    result = setup_campaign(
        store, SNAPSHOT_ID, tmp_path / "trusted" / "project", research
    )

    assert result.returncode == 2
    assert "--strategy-image is required" in result.stderr
    assert read_campaign(research)["status"] == "pending_operator"


def test_setup_rejects_a_research_dir_inside_the_project(tmp_path: Path) -> None:
    project, store, campaign = trusted_campaign(tmp_path)
    unsafe = project / "research"
    unsafe.mkdir()
    (unsafe / "inbox").mkdir()

    result = setup_campaign(
        store, campaign["snapshot_id"], project, unsafe,
        "--strategy-image", "python:issue27-runner",
    )

    assert result.returncode == 2
    assert "separate from the trusted project" in result.stderr
    assert not (unsafe / "campaign.json").exists()
