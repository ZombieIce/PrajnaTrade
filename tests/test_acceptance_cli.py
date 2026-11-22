"""Public-CLI tests for the six-scenario acceptance report (#35, synthetic only).

Every fixture in this file is synthetic.  These tests exercise
``prajnatrade acceptance report --input <manifest.json> --output <report.md|json>``
as an operator-only command: the report is rendered from the manifest alone and
never reads a trusted store, re-runs an evaluation or opens a sealed partition.
They do not claim real market data, a real model, OS isolation or end-to-end
acceptance.
"""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

from cli_helpers import cli, ok as generated, rejected as _rejected

ROOT = Path(__file__).resolve().parents[1]
SCENARIO_IDS = (
    "tests_and_cli",
    "real_snapshot_baseline",
    "real_data_mock",
    "controlled_real_llm",
    "independent_validation_reports",
    "immutable_release_forward",
)


def rejected(result: subprocess.CompletedProcess[str], code: str) -> dict:
    """Acceptance reports always reject in the ``acceptance`` phase."""
    return _rejected(result, code, phase="acceptance")


def _scenario(
    scenario_id: str,
    *,
    status: str = "verified",
    data: str = "synthetic",
    agent: str = "mock",
    period: str = "historical",
) -> dict:
    scenario = {
        "id": scenario_id,
        "title": f"Synthetic fixture for {scenario_id}",
        "status": status,
        "scope": {"data": data, "agent": agent, "period": period},
        "expected": f"Expected acceptance criterion for {scenario_id}.",
        "observed": f"Observed behaviour for {scenario_id} on synthetic fixtures.",
        "evidence": [
            {
                "type": "synthetic_fixture",
                "reference": f"tests/synthetic/{scenario_id}.json",
                "description": "Synthetic mechanism fixture only; not real evidence.",
            }
        ],
        "blockers": [],
    }
    if status == "blocked":
        scenario["evidence"] = []
        scenario["blockers"] = [
            {
                "missing": f"Real prerequisite for {scenario_id}.",
                "unblock": f"Provide the real prerequisite for {scenario_id}.",
            }
        ]
    if status == "failed":
        scenario["deviation"] = f"Observed deviation for {scenario_id}."
    if status == "not_run":
        scenario["evidence"] = []
        scenario["observed"] = ""
    return scenario


def manifest() -> dict:
    """A valid six-scenario synthetic manifest with an empty real chain."""
    return {
        "schema_version": 1,
        "delivery": {
            "version": "0.1.0",
            "branch": "fixture-branch",
            "tested_at": "2026-11-20T00:00:00+08:00",
            "environment": "synthetic fixture environment",
            "test_command": "python -m pytest -q tests/",
            "test_result": "999 passed",
        },
        "scenarios": [_scenario(scenario_id) for scenario_id in SCENARIO_IDS],
        "real_chain": {
            "agent_candidate": None,
            "approved_releases": [],
            "forward_days": [],
        },
    }


def _write_manifest(tmp_path: Path, value: dict, *, name: str = "manifest.json") -> Path:
    source = tmp_path / name
    source.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return source


def _run_report(
    tmp_path: Path,
    manifest_value: dict,
    *,
    suffix: str = ".json",
    name: str = "manifest.json",
) -> tuple[dict, Path, Path]:
    source = _write_manifest(tmp_path, manifest_value, name=name)
    output = tmp_path / f"report{suffix}"
    result = cli(
        "acceptance", "report", "--input", str(source), "--output", str(output)
    )
    return generated(result), source, output


def test_all_verified_synthetic_only_is_blocked_not_passed(tmp_path: Path) -> None:
    payload, source, output = _run_report(tmp_path, manifest())
    assert payload["status"] == "generated"
    assert payload["kind"] == "issue_17_acceptance_report"
    assert payload["output"] == str(output)
    assert payload["format"] == "json"
    assert len(payload["manifest_sha256"]) == 64

    report = json.loads(output.read_text(encoding="utf-8"))
    assert {scenario["id"] for scenario in report["scenarios"]} == set(SCENARIO_IDS)
    assert all(scenario["status"] == "verified" for scenario in report["scenarios"])
    assert report["verdict"]["six_scenario_rule_met"] is True
    assert report["verdict"]["end_to_end_passed"] is False
    assert report["verdict"]["overall_status"] == "blocked"
    assert "not passed" in report["verdict"]["statement"].lower()
    assert source.is_file()

    checklist = {item["id"]: item for item in report["real_chain_checklist"]}
    assert checklist["six_scenarios_verified"]["satisfied"] is True
    assert checklist["candidate_chain"]["satisfied"] is False
    assert checklist["two_real_approved_releases"]["satisfied"] is False
    assert checklist["real_forward_days"]["satisfied"] is False
    assert checklist["real_scope_evidence"]["satisfied"] is False

    blockers = report["verdict"]["blockers"]
    assert blockers
    assert all(blocker["missing"].strip() for blocker in blockers)
    assert all(blocker["unblock"].strip() for blocker in blockers)
    assert any("real" in blocker["missing"].lower() for blocker in blockers)
    assert report["verdict"]["unblock_conditions"]


def test_verified_scenario_without_evidence_is_rejected(tmp_path: Path) -> None:
    value = manifest()
    value["scenarios"][0]["evidence"] = []
    source = tmp_path / "manifest.json"
    source.write_text(json.dumps(value), encoding="utf-8")
    result = cli(
        "acceptance", "report", "--input", str(source),
        "--output", str(tmp_path / "report.json"),
    )
    payload = rejected(result, "VERIFIED_WITHOUT_EVIDENCE")
    assert "tests_and_cli" in payload["error"]["message"]
    assert not (tmp_path / "report.json").exists()


def _forward_day(
    day: str,
    *,
    next_session: str,
    terminal_state: str = "filled",
    persisted_before_open: bool = True,
    committed: bool = True,
    common: bool = True,
    real_data: bool = True,
) -> dict:
    """One reconciled real forward ledger day (after-close signal -> next open)."""
    return {
        "date": day,
        "signal_date": day,
        "orders_persisted_before_open": persisted_before_open,
        "next_session": next_session,
        "order_terminal_state": terminal_state,
        "ledger_day_id": f"fixture-ledger-day-{day}",
        "result_hash": "a" * 64,
        "committed": committed,
        "common": common,
        "real_data": real_data,
    }


def _complete_real_chain_manifest() -> dict:
    """A structurally complete real chain in a clearly synthetic mechanism fixture.

    The IDs are deliberately descriptive fixture strings, not real evidence.
    """
    value = manifest()
    scopes = {
        "real_snapshot_baseline": {"data": "real", "agent": "mock", "period": "historical"},
        "real_data_mock": {"data": "real", "agent": "mock", "period": "historical"},
        "controlled_real_llm": {"data": "real", "agent": "real", "period": "historical"},
        "independent_validation_reports": {
            "data": "real", "agent": "real", "period": "historical"
        },
        "immutable_release_forward": {"data": "real", "agent": "real", "period": "forward"},
    }
    for scenario in value["scenarios"]:
        if scenario["id"] in scopes:
            scenario["scope"] = scopes[scenario["id"]]
    value["real_chain"] = {
        "agent_candidate": {
            "experiment_id": "fixture-experiment-001",
            "candidate_id": "fixture-candidate-001",
            "validation_id": "fixture-validation-001",
            "holdout_id": "fixture-holdout-001",
            "release_id": "fixture-release-001",
            "forward_ledger_days": ["2030-01-02", "2030-01-03"],
        },
        "approved_releases": [
            {
                "release_id": "fixture-release-001",
                "real_data": True,
                "validation_id": "fixture-validation-001",
                "holdout_id": "fixture-holdout-001",
                "approved": True,
            },
            {
                "release_id": "fixture-release-002",
                "real_data": True,
                "validation_id": "fixture-validation-002",
                "holdout_id": "fixture-holdout-002",
                "approved": True,
            },
        ],
        "forward_days": [
            _forward_day("2030-01-02", next_session="2030-01-03"),
            _forward_day("2030-01-03", next_session="2030-01-04"),
        ],
    }
    return value


def test_complete_real_chain_fixture_is_end_to_end_passed(tmp_path: Path) -> None:
    payload, _, output = _run_report(tmp_path, _complete_real_chain_manifest())
    assert payload["end_to_end_passed"] is True
    assert payload["overall_status"] == "passed"
    assert payload["six_scenario_rule_met"] is True
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["verdict"]["end_to_end_passed"] is True
    assert report["verdict"]["overall_status"] == "passed"
    assert report["verdict"]["blockers"] == []
    assert all(item["satisfied"] for item in report["real_chain_checklist"])
    # The forward-day reconciliation is explicit in the report, not inferred.
    checklist = {item["id"]: item for item in report["real_chain_checklist"]}
    assert "pre-open" in checklist["real_forward_days"]["label"]
    assert "terminal" in checklist["real_forward_days"]["label"]
    assert "2030-01-02" in checklist["real_forward_days"]["detail"]
    assert "2030-01-03" in checklist["real_forward_days"]["detail"]
    # This is a mechanism fixture; the report still never claims real runs.
    assert "no trusted store is read" in report["notes"][0]
    assert all(
        entry["type"] == "synthetic_fixture"
        for scenario in report["scenarios"]
        for entry in scenario["evidence"]
    )


def test_forward_day_reconciliation_fields_are_required(tmp_path: Path) -> None:
    for field in (
        "signal_date",
        "orders_persisted_before_open",
        "next_session",
        "order_terminal_state",
        "ledger_day_id",
        "result_hash",
    ):
        value = _complete_real_chain_manifest()
        del value["real_chain"]["forward_days"][0][field]
        result = cli(
            "acceptance", "report",
            "--input", str(_write_manifest(tmp_path, value)),
            "--output", str(tmp_path / f"report-{field}.json"),
        )
        assert result.returncode == 2, (field, result.stdout)
        payload = json.loads(result.stdout)
        assert payload["status"] == "rejected", (field, payload)
        assert payload["error"]["code"] in {
            "FIELD_MISSING",
            "REAL_CHAIN_INVALID",
        }, (field, payload)
        assert field in payload["error"]["message"], (field, payload)


def test_unreconciled_forward_days_never_satisfy_the_real_chain(
    tmp_path: Path,
) -> None:
    # An order that was not persisted before the next open cannot be counted as
    # a reconciled real forward day, even when the day is committed and common.
    value = _complete_real_chain_manifest()
    value["real_chain"]["forward_days"][0]["orders_persisted_before_open"] = False
    payload, _, output = _run_report(tmp_path, value)
    assert payload["end_to_end_passed"] is False
    report = json.loads(output.read_text(encoding="utf-8"))
    checklist = {item["id"]: item for item in report["real_chain_checklist"]}
    assert checklist["real_forward_days"]["satisfied"] is False
    assert "pre-open" in checklist["real_forward_days"]["missing"]

    # A terminal state that is not a documented evidenced state is rejected by
    # the strict schema rather than being silently counted.
    value = _complete_real_chain_manifest()
    value["real_chain"]["forward_days"][1]["order_terminal_state"] = "unknown"
    rejected(
        cli(
            "acceptance", "report",
            "--input", str(_write_manifest(tmp_path, value)),
            "--output", str(tmp_path / "report-terminal.json"),
        ),
        "REAL_CHAIN_INVALID",
    )

    # The next committed day must be the session in which the first day's
    # pre-open order reached its terminal state.
    value = _complete_real_chain_manifest()
    value["real_chain"]["forward_days"][0]["next_session"] = "2030-01-07"
    payload, _, output = _run_report(tmp_path, value)
    assert payload["end_to_end_passed"] is False
    report = json.loads(output.read_text(encoding="utf-8"))
    checklist = {item["id"]: item for item in report["real_chain_checklist"]}
    assert checklist["real_forward_days"]["satisfied"] is False



def test_failed_scenario_makes_the_overall_verdict_failed(tmp_path: Path) -> None:
    value = manifest()
    value["scenarios"][0] = _scenario("tests_and_cli", status="failed")
    payload, _, output = _run_report(tmp_path, value)
    assert payload["overall_status"] == "failed"
    assert payload["end_to_end_passed"] is False
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["verdict"]["overall_status"] == "failed"
    assert report["verdict"]["failures"] == [
        {
            "scenario": "tests_and_cli",
            "deviation": "Observed deviation for tests_and_cli.",
        }
    ]
    assert "NOT passed" in report["verdict"]["statement"]
    assert "six-scenario rule was not met" in report["verdict"]["statement"]


def test_not_run_scenario_makes_the_overall_verdict_not_run(tmp_path: Path) -> None:
    value = manifest()
    value["scenarios"][1] = _scenario("real_snapshot_baseline", status="not_run")
    payload, _, output = _run_report(tmp_path, value)
    assert payload["overall_status"] == "not_run"
    assert payload["end_to_end_passed"] is False
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["verdict"]["six_scenario_rule_met"] is False
    assert "six-scenario rule was not met" in report["verdict"]["statement"]
    assert "real_snapshot_baseline" in report["verdict"]["statement"]


def _report_rejected(
    tmp_path: Path, value: dict, code: str
) -> dict:
    source = tmp_path / "manifest.json"
    source.write_text(json.dumps(value), encoding="utf-8")
    result = cli(
        "acceptance", "report", "--input", str(source),
        "--output", str(tmp_path / "report.json"),
    )
    payload = rejected(result, code)
    assert not (tmp_path / "report.json").exists()
    return payload


def test_missing_scenario_is_rejected(tmp_path: Path) -> None:
    value = manifest()
    value["scenarios"] = value["scenarios"][:-1]
    payload = _report_rejected(tmp_path, value, "SCENARIO_MISSING")
    assert "immutable_release_forward" in payload["error"]["message"]


def test_duplicate_scenario_is_rejected(tmp_path: Path) -> None:
    value = manifest()
    value["scenarios"][1] = copy.deepcopy(value["scenarios"][0])
    _report_rejected(tmp_path, value, "SCENARIO_DUPLICATE")


def test_unknown_scenario_is_rejected(tmp_path: Path) -> None:
    value = manifest()
    value["scenarios"][0]["id"] = "not_a_required_scenario"
    _report_rejected(tmp_path, value, "SCENARIO_UNKNOWN")


def test_unknown_manifest_and_scenario_fields_are_rejected(tmp_path: Path) -> None:
    value = manifest()
    value["sealed_diagnostics"] = {"holdout_outcome": "passed"}
    _report_rejected(tmp_path, value, "FIELD_UNKNOWN")

    value = manifest()
    value["scenarios"][0]["credentials"] = "token"
    _report_rejected(tmp_path, value, "FIELD_UNKNOWN")


def test_missing_manifest_field_is_rejected(tmp_path: Path) -> None:
    value = manifest()
    del value["delivery"]["test_command"]
    _report_rejected(tmp_path, value, "FIELD_MISSING")


def test_blocked_scenario_without_blockers_is_rejected(tmp_path: Path) -> None:
    value = manifest()
    value["scenarios"][1] = _scenario("real_snapshot_baseline", status="blocked")
    value["scenarios"][1]["blockers"] = []
    _report_rejected(tmp_path, value, "BLOCKED_WITHOUT_BLOCKERS")


def test_failed_scenario_without_deviation_is_rejected(tmp_path: Path) -> None:
    value = manifest()
    value["scenarios"][0] = _scenario("tests_and_cli", status="failed")
    del value["scenarios"][0]["deviation"]
    _report_rejected(tmp_path, value, "FAILED_WITHOUT_DEVIATION")


def test_failed_scenario_without_evidence_is_rejected(tmp_path: Path) -> None:
    value = manifest()
    value["scenarios"][0] = _scenario("tests_and_cli", status="failed")
    value["scenarios"][0]["evidence"] = []
    _report_rejected(tmp_path, value, "FAILED_WITHOUT_EVIDENCE")


def test_verified_scenario_without_findings_is_rejected(tmp_path: Path) -> None:
    value = manifest()
    value["scenarios"][0]["expected"] = ""
    _report_rejected(tmp_path, value, "VERIFIED_WITHOUT_EXPECTED_OBSERVED")


def test_agent_cli_rejects_the_acceptance_operator_command(tmp_path: Path) -> None:
    value = manifest()
    source = tmp_path / "manifest.json"
    source.write_text(json.dumps(value), encoding="utf-8")
    result = cli(
        "acceptance", "report", "--input", str(source),
        "--output", str(tmp_path / "report.json"), agent=True,
    )
    assert result.returncode == 2
    assert "invalid choice" in result.stderr
    assert "acceptance" in result.stderr
    assert not (tmp_path / "report.json").exists()


def test_report_rendering_covers_every_scenario_and_the_real_chain_checklist(
    tmp_path: Path,
) -> None:
    value = manifest()
    value["scenarios"][1] = _scenario("real_snapshot_baseline", status="blocked")
    value["scenarios"][2] = _scenario("real_data_mock", status="failed")
    value["scenarios"][3] = _scenario("controlled_real_llm", status="not_run")
    source = tmp_path / "manifest.json"
    source.write_text(json.dumps(value), encoding="utf-8")
    json_output = tmp_path / "report.json"
    markdown_output = tmp_path / "report.md"
    for output in (json_output, markdown_output):
        result = cli(
            "acceptance", "report", "--input", str(source),
            "--output", str(output),
        )
        assert result.returncode == 0, (result.stdout, result.stderr)

    report = json.loads(json_output.read_text(encoding="utf-8"))
    statuses = {scenario["id"]: scenario["status"] for scenario in report["scenarios"]}
    assert statuses["real_snapshot_baseline"] == "blocked"
    assert statuses["real_data_mock"] == "failed"
    assert statuses["controlled_real_llm"] == "not_run"
    assert report["verdict"]["overall_status"] == "failed"

    markdown = markdown_output.read_text(encoding="utf-8")
    for scenario_id, status in statuses.items():
        assert f"`{scenario_id}`" in markdown
        assert status in markdown
    assert "## Real-chain checklist" in markdown
    for item in report["real_chain_checklist"]:
        assert item["id"] in markdown
    assert "## Verdict" in markdown
    assert "NOT PASSED" in markdown
    assert "### Unblock conditions" in markdown


def test_identical_manifests_render_identical_bytes(tmp_path: Path) -> None:
    value = manifest()
    text = json.dumps(value, indent=2, sort_keys=True) + "\n"
    source_a = tmp_path / "a.json"
    source_b = tmp_path / "b.json"
    source_a.write_text(text, encoding="utf-8")
    source_b.write_text(text, encoding="utf-8")
    for suffix in (".md", ".json"):
        first = tmp_path / f"first{suffix}"
        second = tmp_path / f"second{suffix}"
        for source, output in ((source_a, first), (source_b, second)):
            result = cli(
                "acceptance", "report", "--input", str(source),
                "--output", str(output),
            )
            assert result.returncode == 0, (result.stdout, result.stderr)
        assert first.read_bytes() == second.read_bytes()


def test_report_generation_reads_only_the_manifest(tmp_path: Path) -> None:
    value = manifest()
    # Evidence references are never opened: a trusted-store path in a reference
    # must not be read, created or contacted by the report command.
    value["scenarios"][0]["evidence"].append(
        {
            "type": "trusted_reference",
            "reference": "/nonexistent/trusted/store/records/evaluation-001/record.json",
            "description": "Never opened by the report command.",
        }
    )
    source = tmp_path / "manifest.json"
    source.write_text(json.dumps(value), encoding="utf-8")
    before = {path.name for path in tmp_path.iterdir()}
    output = tmp_path / "report.md"
    result = cli(
        "acceptance", "report", "--input", str(source), "--output", str(output),
        cwd=tmp_path,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    after = {path.name for path in tmp_path.iterdir()}
    assert after == before | {"report.md"}
    assert not list(tmp_path.glob("*.sqlite"))
    assert not (tmp_path / ".prajnatrade").exists()

    with_store = cli(
        "acceptance", "report", "--input", str(source), "--output", str(output),
        "--store", str(tmp_path / "store"), cwd=tmp_path,
    )
    assert with_store.returncode == 2
    assert "unrecognized arguments" in with_store.stderr


def test_report_output_format_must_be_markdown_or_json(tmp_path: Path) -> None:
    value = manifest()
    source = tmp_path / "manifest.json"
    source.write_text(json.dumps(value), encoding="utf-8")
    result = cli(
        "acceptance", "report", "--input", str(source),
        "--output", str(tmp_path / "report.txt"),
    )
    assert result.returncode == 2
    assert "must end with .md or .json" in result.stderr
    assert not (tmp_path / "report.txt").exists()


DOCS = ROOT / "docs" / "acceptance"


def test_checked_in_manifest_is_honest_and_the_report_is_current(
    tmp_path: Path,
) -> None:
    manifest_path = DOCS / "issue-17-manifest.json"
    report_path = DOCS / "issue-17-report.md"
    assert manifest_path.is_file()
    assert report_path.is_file()
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    scenarios = {scenario["id"]: scenario for scenario in value["scenarios"]}
    assert set(scenarios) == set(SCENARIO_IDS)
    assert scenarios["tests_and_cli"]["status"] == "verified"
    assert all(
        scenarios[scenario_id]["status"] == "blocked"
        for scenario_id in SCENARIO_IDS
        if scenario_id not in {"tests_and_cli", "real_snapshot_baseline", "real_data_mock"}
    )

    real_snapshot = scenarios["real_snapshot_baseline"]
    assert real_snapshot["scope"] == {
        "data": "real",
        "agent": "mock",
        "period": "historical",
    }
    assert real_snapshot["status"] == "verified"
    assert real_snapshot["blockers"] == []
    evidence = {entry["type"]: entry for entry in real_snapshot["evidence"]}
    assert {"real_snapshot", "real_baseline", "machine_cross_check", "discovery_isolation"} <= evidence.keys()
    assert evidence["human_reconciliation"]["reference"].startswith(
        "https://github.com/ZombieIce/PrajnaTrade/issues/24#issuecomment-"
    )
    real_mock = scenarios["real_data_mock"]
    assert real_mock["status"] == "verified"
    assert real_mock["scope"] == {"data": "real", "agent": "mock", "period": "historical"}
    assert real_mock["blockers"] == []
    assert any(
        entry["type"] == "real_discovery_mock" for entry in real_mock["evidence"]
    )
    for scenario in scenarios.values():
        if scenario["id"] in {"real_snapshot_baseline", "real_data_mock"}:
            continue
        assert scenario["scope"] == {
            "data": "synthetic",
            "agent": "mock",
            "period": "historical",
        }
        for entry in scenario["evidence"]:
            assert entry["type"] in {"test_suite", "test_file", "synthetic_mechanism"}
    for scenario in scenarios.values():
        for blocker in scenario["blockers"]:
            assert blocker["missing"].strip()
            assert blocker["unblock"].strip()
    assert value["real_chain"] == {
        "agent_candidate": None,
        "approved_releases": [],
        "forward_days": [],
    }
    assert "pending" not in value["delivery"]["test_result"]
    assert "passed" in value["delivery"]["test_result"]

    # The exact missing prerequisites from #24/#26/#27 and this handoff are
    # explicit in the checked-in manifest.
    manifest_text = manifest_path.read_text(encoding="utf-8").lower()
    for phrase in (
        "official exchange calendar",
        "point-in-time availability",
        "os-level deployment isolation",
        "authorized real model access",
        "real protected evaluation",
        "at least two real candidates",
        "at least two consecutive, common, committed real forward trading days",
    ):
        assert phrase in manifest_text, phrase

    # The checked-in Markdown report is exactly what the current CLI renders.
    output = tmp_path / "report.md"
    result = cli(
        "acceptance", "report", "--input", str(manifest_path),
        "--output", str(output),
    )
    generated_payload = generated(result)
    assert generated_payload["overall_status"] == "blocked"
    assert generated_payload["end_to_end_passed"] is False
    assert output.read_bytes() == report_path.read_bytes()

    json_output = tmp_path / "report.json"
    result = cli(
        "acceptance", "report", "--input", str(manifest_path),
        "--output", str(json_output),
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    report = json.loads(json_output.read_text(encoding="utf-8"))
    assert report["verdict"]["overall_status"] == "blocked"
    assert report["verdict"]["end_to_end_passed"] is False
    assert report["verdict"]["six_scenario_rule_met"] is False
    assert "NOT passed" in report["verdict"]["statement"]
    assert "six-scenario rule was not met" in report["verdict"]["statement"]
    assert report["scenario_status_counts"] == {
        "verified": 3, "blocked": 3, "failed": 0, "not_run": 0
    }


def test_empty_or_false_real_chain_ids_never_pass(tmp_path: Path) -> None:
    value = manifest()
    value["real_chain"] = {
        "agent_candidate": {
            "experiment_id": "",
            "candidate_id": None,
            "validation_id": "",
            "holdout_id": None,
            "release_id": "",
            "forward_ledger_days": [],
        },
        "approved_releases": [
            {
                "release_id": "fixture-release-a",
                "real_data": False,
                "validation_id": "",
                "holdout_id": "",
                "approved": False,
            }
        ],
        "forward_days": [
            _forward_day(
                "2030-01-02",
                next_session="2030-01-03",
                committed=False,
                common=False,
                real_data=False,
            ),
            _forward_day("2030-01-03", next_session="2030-01-04"),
        ],
    }
    payload, _, output = _run_report(tmp_path, value)
    assert payload["end_to_end_passed"] is False
    assert payload["overall_status"] == "blocked"
    report = json.loads(output.read_text(encoding="utf-8"))
    checklist = {item["id"]: item for item in report["real_chain_checklist"]}
    assert checklist["candidate_chain"]["satisfied"] is False
    assert checklist["candidate_release"]["satisfied"] is False
    assert checklist["two_real_approved_releases"]["satisfied"] is False
    assert checklist["real_forward_days"]["satisfied"] is False
    assert checklist["candidate_forward_ledger"]["satisfied"] is False
    assert "NOT passed" in report["verdict"]["statement"]


def test_synthetic_scope_verified_scenarios_never_satisfy_the_real_chain(
    tmp_path: Path,
) -> None:
    value = _complete_real_chain_manifest()
    for scenario in value["scenarios"]:
        scenario["scope"] = {
            "data": "synthetic",
            "agent": "mock",
            "period": "historical",
        }
    payload, _, output = _run_report(tmp_path, value)
    assert payload["end_to_end_passed"] is False
    assert payload["overall_status"] == "blocked"
    assert payload["six_scenario_rule_met"] is True
    report = json.loads(output.read_text(encoding="utf-8"))
    checklist = {item["id"]: item for item in report["real_chain_checklist"]}
    assert checklist["six_scenarios_verified"]["satisfied"] is True
    assert checklist["real_scope_evidence"]["satisfied"] is False
    assert "synthetic" in checklist["real_scope_evidence"]["detail"]
    assert "NOT passed" in report["verdict"]["statement"]
    assert "real-chain gate" in report["verdict"]["statement"]
