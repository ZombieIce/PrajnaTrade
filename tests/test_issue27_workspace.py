"""Tests for the project-only research workspace builder.

The workspace is written to ``tmp_path`` (outside Git) and every assertion is
about public metadata only: no trusted path, no store copy, no project data.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from cli_helpers import repo_root
from issue27_helpers import (
    WORKSPACE,
    build_workspace,
    ready_campaign,
    run_script,
    write_public_metadata,
)

ROOT = repo_root()
EXPECTED_FILES = {
    "README.md",
    "AGENT_PROMPT.md",
    "SUBMISSION_FORMAT.md",
    "campaign.json",
    "notes/000-template.md",
}
EXPECTED_DIRS = {"inbox", "outbox", "processed", "notes", "receipts"}


def all_workspace_text(research: Path) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(research.rglob("*"))
        if path.is_file()
    )


def test_workspace_builds_only_public_files(tmp_path: Path) -> None:
    research = build_workspace(tmp_path)

    assert {path.name for path in research.iterdir() if path.is_dir()} == EXPECTED_DIRS
    assert {
        str(path.relative_to(research)) for path in research.rglob("*") if path.is_file()
    } == EXPECTED_FILES
    campaign = json.loads((research / "campaign.json").read_text(encoding="utf-8"))
    assert campaign == {
        "schema_version": 1,
        "status": "pending_operator",
        "campaign_id": None,
        "snapshot_id": None,
        "data_kind": None,
        "discovery": {
            "start": None,
            "end": None,
            "sessions": None,
            "content_hash": None,
        },
        "budgets": {"attempts": None, "execution_seconds": None},
        "executor": {"kind": None, "image": None, "digest": None, "limits": None},
    }
    text = all_workspace_text(research)
    assert str(ROOT) not in text
    assert str(tmp_path) not in text
    assert "/Users/" not in text
    assert "project.sqlite" not in text
    assert "sealed snapshot store" in text
    assert "inbox/<submission-id>.json" in text
    assert "receipt_sha256" in (research / "README.md").read_text(encoding="utf-8")


def test_workspace_installs_validated_public_metadata(tmp_path: Path) -> None:
    campaign = ready_campaign("campaign-public-1")
    metadata = write_public_metadata(tmp_path, campaign)

    research = build_workspace(tmp_path)
    replaced = run_script(
        WORKSPACE, "--output", str(research), "--campaign-json", str(metadata), "--force"
    )

    assert replaced.returncode == 0, replaced.stderr
    installed = json.loads((research / "campaign.json").read_text(encoding="utf-8"))
    assert installed == campaign
    assert installed["executor"]["digest"] == "sha256:" + "c" * 64
    assert str(tmp_path) not in json.dumps(installed)


def test_workspace_rejects_non_public_campaign_fields(tmp_path: Path) -> None:
    campaign = ready_campaign("campaign-public-1")
    campaign["store"] = "/trusted/snapshot-store"
    metadata = write_public_metadata(tmp_path, campaign)

    result = run_script(
        WORKSPACE,
        "--output", str(tmp_path / "research"),
        "--campaign-json", str(metadata),
    )

    assert result.returncode == 2
    assert "non-public fields" in result.stderr
    assert not (tmp_path / "research").exists()


def test_workspace_rejects_absolute_paths_in_allowed_fields(tmp_path: Path) -> None:
    campaign = ready_campaign("campaign-public-1")
    campaign["executor"]["image"] = "/sealed/example/python-runner"
    metadata = write_public_metadata(tmp_path, campaign)

    result = run_script(
        WORKSPACE,
        "--output", str(tmp_path / "research"),
        "--campaign-json", str(metadata),
    )

    assert result.returncode == 2
    assert "absolute path" in result.stderr
    assert not (tmp_path / "research").exists()


def test_workspace_refuses_a_git_checkout_and_nonempty_directory(tmp_path: Path) -> None:
    forbidden = ROOT / "tmp-issue27-workspace-should-not-exist"
    try:
        result = run_script(WORKSPACE, "--output", str(forbidden))
        assert result.returncode == 2
        assert "outside the Git checkout" in result.stderr
        assert not forbidden.exists()
    finally:
        if forbidden.exists() and forbidden.resolve().is_relative_to(ROOT):
            shutil.rmtree(forbidden)

    research = build_workspace(tmp_path)
    again = run_script(WORKSPACE, "--output", str(research))
    assert again.returncode == 2
    assert "not empty" in again.stderr
    forced = run_script(WORKSPACE, "--output", str(research), "--force")
    assert forced.returncode == 0, forced.stderr


def submission_format_blocks(research: Path) -> list[str]:
    text = (research / "SUBMISSION_FORMAT.md").read_text(encoding="utf-8")
    return re.findall(r"```json\n(.*?)```", text, flags=re.DOTALL)


def test_submission_format_examples_match_the_executor_schema(tmp_path: Path) -> None:
    research = build_workspace(tmp_path)
    blocks = [json.loads(block) for block in submission_format_blocks(research)]
    submissions = [block for block in blocks if "campaign_id" in block]
    assert len(submissions) == 2
    for block in submissions:
        assert set(block) <= {
            "schema_version", "campaign_id", "submission_id", "hypothesis",
            "strategy", "parameters", "parent_experiment_id",
        }
        assert block["schema_version"] == 1
        assert block["campaign_id"]
        assert block["hypothesis"]
        assert isinstance(block["parameters"], dict)
        assert isinstance(block["strategy"], dict)
    declarative, python_strategy = submissions
    assert declarative["strategy"] == {"family": "equal_weight"}
    assert set(python_strategy["strategy"]) == {"family", "source"}
    assert python_strategy["strategy"]["family"] == "python"
    source = python_strategy["strategy"]["source"]
    assert "def strategy(history, state):" in source
    compile(source, "<generated-strategy>", "exec")
    assert "source_sha256" not in json.dumps(submissions)
    status_requests = [block for block in blocks if block.get("request") == "status"]
    assert status_requests == [
        {"schema_version": 1, "request": "status", "submission_id": "exp-001"}
    ]


def test_workspace_readme_records_the_operating_contract(tmp_path: Path) -> None:
    research = build_workspace(tmp_path, campaign=ready_campaign("campaign-public-1"))
    readme = (research / "README.md").read_text(encoding="utf-8")
    prompt = (research / "AGENT_PROMPT.md").read_text(encoding="utf-8")

    for phrase in (
        "Work only inside **this directory**",
        "never access a repository",
        "no API key",
        "The trusted side executes every submission",
        "Only after reading the real receipt",
        "campaign.json",
        "notes/final-summary.md",
    ):
        assert phrase in readme
    assert "parent_experiment_id" in prompt
    assert "notes/final-summary.md" in prompt
    assert str(tmp_path) not in readme + prompt
