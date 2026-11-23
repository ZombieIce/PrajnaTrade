"""Synthetic fixtures exercise the schema-v2 protected input contract."""

import json
from pathlib import Path

import pytest

from cli_helpers import cli, ok, write_json
from test_issue37_holdout_cli import _authorize, _calendar, _run, _selected_round
from test_protected_eval_cli import seal_snapshot
from test_real_snapshot import _real_input


@pytest.mark.isolation
def test_schema_v2_reports_historical_scope_and_separate_automatic_admission(tmp_path: Path) -> None:
    sealed = seal_snapshot(tmp_path, _real_input())
    store = tmp_path / "operator"
    calendar_path = _calendar(tmp_path)
    calendar = json.loads(calendar_path.read_text())
    calendar.update({"data_kind": "real", "source": "fabricated calendar for testing schema v2"})
    write_json(calendar_path, calendar)
    selected = _selected_round(tmp_path / "schema-v2", store, sealed, "Schema v2 fixture")

    approval = ok(_authorize(store, sealed, selected, calendar_path))
    result = ok(_run(store, sealed, selected, approval["plan_id"]))
    assert result["record"]["data_kind"] == "real"
    assert result["outcome"] == "passed"
    assert result["release"]["status"] == "enabled"
    assert result["release"]["artifact"]["scope"] == "real"

    report_path = tmp_path / "controlled.json"
    ok(cli("evaluation", "report", "--store", str(store), "--plan-id", approval["plan_id"],
           "--output", str(report_path)))
    report = json.loads(report_path.read_text())
    assert report["scope"] == "real"
    assert report["workflow"] == "research_holdout"
    assert report["validation_binding"]["evaluation_id"] == selected["evaluation_id"]
