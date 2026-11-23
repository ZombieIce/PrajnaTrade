"""Public workflow checks for the direction-bound Final Holdout."""

from pathlib import Path

from cli_helpers import cli, ok, rejected, write_json


def test_holdout_requires_an_existing_frozen_selection(tmp_path: Path) -> None:
    project = tmp_path / "project"
    ok(cli("project", "init", "--project", str(project)))
    calendar = write_json(tmp_path / "calendar.json", {
        "schema_version": 1,
        "data_kind": "synthetic",
        "source": "fixture calendar",
        "sessions": ["2026-12-01", "2026-12-02"],
    })
    result = rejected(cli(
        "holdout", "authorize", "--store", str(tmp_path / "trusted"),
        "--snapshot-store", str(tmp_path / "snapshots"), "--project", str(project),
        "--round-id", "a" * 64, "--validation-evaluation-id", "unknown",
        "--reviewer", "research lead", "--reason", "Review the frozen selection",
        "--selection-independent", "yes", "--independence-reason", "No prior exposure",
        "--calendar", str(calendar),
    ), "SELECTION_REQUIRED")
    assert result["query_debited"] is False
    assert result["opened_protected_source"] is False
