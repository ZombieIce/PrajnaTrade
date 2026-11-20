"""Public-CLI tests for the Final Holdout and its controlled report (MVP-11, #29).

Everything here is synthetic and exercises `prajnatrade evaluation ...`
(operator) plus `prajnatrade-agent evaluation status ...` (research side).
The sealed snapshot fixtures are shared with `test_protected_eval_cli`.

Each test owns its own trusted store and candidates.  The receipt-constant test
builds every pending/unauthorized/completed/rejected/replayed state itself, so it
also passes when selected alone with ``pytest -k``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_protected_eval_cli import (
    authorize,
    build_scenario,
    cli,
    cli_env,
    consumed_rows,
    create_plan,
    eval_store,
    evaluation_rows,
    freeze_candidate,
    ok,
    plan_input,
    rejected,
    run_evaluation,
    submit_experiment,
    synthetic_snapshot,
    write_json,
)


HOLDOUT_RECEIPT = {
    "schema_version": 1,
    "stage": "final_holdout",
    "status": "accepted",
    "message": "Request received.",
}


@pytest.fixture(scope="module")
def base(tmp_path_factory: pytest.TempPathFactory) -> dict:
    from test_protected_eval_cli import (
        create_campaign,
        freeze_candidate,
        seal_snapshot,
        write_json,
    )

    root = tmp_path_factory.mktemp("holdout-evaluation")
    sealed = seal_snapshot(root, synthetic_snapshot(), "snapshots")
    project = root / "strategy-a"
    ok(cli("project", "init", "--project", str(project)))
    campaign = create_campaign(project, sealed["store"], sealed["snapshot_id"])
    source = write_json(root / "submission.json", {})
    submit_experiment(
        project,
        campaign["campaign_id"],
        source,
        "equal",
        "Equal weights preserve value across a cash dividend.",
        "equal_weight",
        {},
    )
    candidate_a = freeze_candidate(
        project,
        campaign["campaign_id"],
        "equal",
        "candidate-a",
        "Equal-weight candidate for the Holdout workflow.",
    )
    submit_experiment(
        project,
        campaign["campaign_id"],
        source,
        "momentum",
        "Momentum with a short lookback is a legal parameter neighbourhood.",
        "momentum",
        {"lookback": 2},
    )
    candidate_b = freeze_candidate(
        project,
        campaign["campaign_id"],
        "momentum",
        "candidate-b",
        "Momentum candidate for the Holdout workflow.",
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
    }


@pytest.fixture(scope="module")
def observed() -> dict:
    return {"canonical": None, "payloads": [], "stdouts": []}


def authorize_holdout(
    store: Path,
    plan_id: str,
    *,
    reviewer: str = "reviewer-2",
    reason: str = "Validation passed; the frozen plan is cleared for one Holdout read.",
):
    return cli(
        "evaluation",
        "authorize",
        "--store",
        str(store),
        "--plan-id",
        plan_id,
        "--stage",
        "final_holdout",
        "--reviewer",
        reviewer,
        "--reason",
        reason,
    )


def report(
    store: Path, plan_id: str, output: Path
):
    return cli(
        "evaluation",
        "report",
        "--store",
        str(store),
        "--plan-id",
        plan_id,
        "--output",
        str(output),
    )


def observe(observed: dict, project: Path, candidate_id: str) -> dict:
    """Read the research-side view and enforce the constant Holdout receipt."""
    result = cli(
        "evaluation",
        "status",
        "--project",
        str(project),
        "--candidate-id",
        candidate_id,
        agent=True,
    )
    payload = ok(result)
    assert set(payload) == {
        "schema_version",
        "candidate_id",
        "validation",
        "final_holdout",
    }
    assert payload["candidate_id"] == candidate_id
    assert payload["final_holdout"] == HOLDOUT_RECEIPT
    assert set(payload["final_holdout"]) == {
        "schema_version",
        "stage",
        "status",
        "message",
    }
    assert payload["validation"] == {"status": "pending"} or set(
        payload["validation"]
    ) == {"schema_version", "stage", "status", "reason", "message"}
    canonical = json.dumps(
        payload["final_holdout"], sort_keys=True, separators=(",", ":")
    )
    if observed["canonical"] is None:
        observed["canonical"] = canonical
    assert canonical == observed["canonical"]
    observed["payloads"].append(payload)
    observed["stdouts"].append(result.stdout)
    return payload


def audit_records(store: Path) -> list[dict]:
    return [
        json.loads(path.read_text()) for path in sorted((store / "audit").glob("*.json"))
    ]


def test_holdout_authorization_is_refused_without_a_passed_validation(
    base: dict, observed: dict
) -> None:
    store = eval_store(base, "holdout-auth")
    failing = plan_input(
        thresholds=[
            {"id": "relative", "metric": "return_delta", "op": ">=", "value": 0.5}
        ]
    )
    plan = create_plan(base, store, base["candidate_a"], failing, name="plan-holdout-auth")

    # No Validation evaluation at all.
    refused = rejected(authorize_holdout(store, plan["plan_id"]), "VALIDATION_NOT_PASSED")
    assert refused["opened_protected_source"] is False
    assert refused["query_debited"] is False
    assert consumed_rows(store) == []
    assert not list(
        (store / "authorizations" / plan["plan_id"]).glob("final_holdout-*.json")
    )
    observe(observed, base["project"], base["candidate_a"]["candidate_id"])

    ok(authorize(store, plan["plan_id"]))
    validated = ok(run_evaluation(base, store, plan["plan_id"]))
    assert validated["outcome"] == "rejected"
    assert validated["reason"] == "THRESHOLD_NOT_MET"

    # A rejected Validation can never authorize Final Holdout.
    refused = rejected(authorize_holdout(store, plan["plan_id"]), "VALIDATION_NOT_PASSED")
    assert refused["opened_protected_source"] is False
    assert refused["query_debited"] is False
    assert not list(
        (store / "authorizations" / plan["plan_id"]).glob("final_holdout-*.json")
    )
    observe(observed, base["project"], base["candidate_a"]["candidate_id"])
    audits = audit_records(store)
    assert [audit["error"]["code"] for audit in audits] == [
        "VALIDATION_NOT_PASSED",
        "VALIDATION_NOT_PASSED",
    ]
    assert all(audit["opened_protected_source"] is False for audit in audits)
    assert all(audit["query_debited"] is False for audit in audits)
    assert len(consumed_rows(store)) == 1
    assert len(evaluation_rows(store)) == 1

    # Insufficient evidence is not a pass either.
    store = eval_store(base, "holdout-insufficient")
    uncertain = plan_input(
        thresholds=[{"id": "sharpe", "metric": "sharpe", "op": ">=", "value": 0.0}]
    )
    plan = create_plan(
        base, store, base["candidate_b"], uncertain, name="plan-holdout-insufficient"
    )
    ok(authorize(store, plan["plan_id"]))
    validated = ok(run_evaluation(base, store, plan["plan_id"]))
    assert validated["outcome"] == "insufficient_evidence"
    refused = rejected(authorize_holdout(store, plan["plan_id"]), "VALIDATION_NOT_PASSED")
    assert refused["opened_protected_source"] is False
    assert len(consumed_rows(store)) == 1
    observe(observed, base["project"], base["candidate_b"]["candidate_id"])


def test_authorized_holdout_runs_once_with_the_validation_export_as_prior(
    base: dict, observed: dict
) -> None:
    store = eval_store(base, "holdout-happy")
    plan = create_plan(base, store, base["candidate_b"], plan_input(), name="plan-holdout-happy")
    ok(authorize(store, plan["plan_id"]))
    validation = ok(run_evaluation(base, store, plan["plan_id"]))
    assert validation["outcome"] == "passed"
    validation_record = validation["record"]

    export = validation_record["prior_export_for_holdout"]
    assert export["source_stage"] == "validation"
    assert export["evaluation_id"] == validation["evaluation_id"]
    assert export["plan_id"] == plan["plan_id"]
    assert (
        export["partition_content_hash"]
        == base["partitions"]["validation"]["content_hash"]
    )
    assert export["signal_window_sessions"] == 3
    assert export["regime_lookback"] == 3
    assert len(export["sessions"]) == 4
    assert export["sessions"][0] >= base["partitions"]["validation"]["start"]
    assert export["sessions"][-1] <= base["partitions"]["validation"]["end"]
    assert len(export["derived"]) == 4 * 5
    assert export["export_hash"]

    granted = ok(authorize_holdout(store, plan["plan_id"]))
    authorization = granted["authorization"]
    assert authorization["stage"] == "final_holdout"
    assert authorization["partition_content_hash"] == (
        base["partitions"]["final_holdout"]["content_hash"]
    )
    assert authorization["validation"]["evaluation_id"] == validation["evaluation_id"]
    assert authorization["validation"]["record_hash"] == validation_record["record_hash"]
    assert authorization["validation"]["outcome"] == "passed"
    assert authorization["validation"]["partition_content_hash"] == (
        base["partitions"]["validation"]["content_hash"]
    )

    holdout = ok(run_evaluation(base, store, plan["plan_id"], stage="final_holdout"))
    assert holdout["status"] == "completed"
    assert holdout["outcome"] == "passed"
    assert holdout["reason"] is None
    assert holdout["scoring"] is True
    assert holdout["query"]["reused"] is False
    assert holdout["query"]["partition"] == "final_holdout"
    assert holdout["query"]["status"] == "completed"
    assert holdout["receipt"] == HOLDOUT_RECEIPT
    record = holdout["record"]
    assert record["stage"] == "final_holdout"
    assert record["partition"] == "final_holdout"
    assert record["record_hash"]
    assert record["opened_protected_source"] is True
    assert record["fingerprints"]["evaluated_partition"] == (
        base["partitions"]["final_holdout"]["content_hash"]
    )
    assert record["partition_descriptor"] == {
        key: base["partitions"]["final_holdout"][key]
        for key in ("start", "end", "sessions", "content_hash")
    }
    assert record["partitions"]["validation"] == base["partitions"]["validation"]
    assert record["authorization"]["stage"] == "final_holdout"
    assert record["query_receipt"]["partition"] == "final_holdout"
    assert record["query_receipt"]["status"] == "completed"
    assert record["availability"]["prior_history_rule"] == {
        "partitions": ["discovery", "validation"]
    }
    prior = record["prior_evidence"]
    assert prior["partitions"] == ["discovery", "validation"]
    assert prior["discovery_content_hash"] == (
        base["partitions"]["discovery"]["content_hash"]
    )
    assert prior["validation"]["evaluation_id"] == validation["evaluation_id"]
    assert prior["validation"]["record_hash"] == validation_record["record_hash"]
    assert prior["validation"]["export_hash"] == export["export_hash"]
    assert prior["validation"]["sessions"] == 4
    assert record["coverage"]["complete"] is True
    assert all(item["status"] == "passed" for item in record["thresholds"])
    assert record["cost_stress"]["base"]["candidate"]["total_return"] == 0.0
    assert record["cost_stress"]["stress"]["candidate"]["total_return"] == 0.0
    baseline_base = record["cost_stress"]["base"]["baseline"]["total_return"]
    baseline_stress = record["cost_stress"]["stress"]["baseline"]["total_return"]
    assert baseline_base is not None and baseline_stress is not None
    assert baseline_stress < baseline_base  # higher costs never improve the result
    assert record["neighbourhood"]["counts"]["variants"] == 2
    assert record["neighbourhood"]["excluded"]
    groups = {group["label"]: group for group in record["regimes"]["groups"]}
    assert set(groups) == {"calm", "volatile", "unclassified"}
    assert groups["calm"]["sessions"] == 4
    assert record["curves"]["candidate"]["base"]
    assert record["curves"]["candidate"]["stress"]
    assert record["curves"]["baseline"]["base"]
    assert record["curves"]["baseline"]["stress"]

    rows = consumed_rows(store)
    assert len(rows) == 2
    assert {row[0] for row in rows} == {"validation", "final_holdout"}

    replay = ok(run_evaluation(base, store, plan["plan_id"], stage="final_holdout"))
    assert replay["evaluation_id"] == holdout["evaluation_id"]
    assert replay["query"]["reused"] is True
    assert replay["record"] == record
    assert len(consumed_rows(store)) == 2
    assert len(evaluation_rows(store)) == 2
    assert len(list((store / "records").glob("*/record.json"))) == 2

    status = ok(
        cli("evaluation", "status", "--store", str(store), "--plan-id", plan["plan_id"])
    )
    for stage in ("validation", "final_holdout"):
        entry = status["stages"][stage]
        assert entry["authorization"]["stage"] == stage
        assert entry["query"]["partition"] == stage
        assert entry["query"]["status"] == "completed"
        assert entry["evaluation"]["scoring"] is True
        assert entry["evaluation"]["record_hash"]
        assert Path(entry["evaluation"]["record_path"]).is_file()
    assert status["stages"]["final_holdout"]["authorization"]["validation"][
        "evaluation_id"
    ] == validation["evaluation_id"]

    agent = observe(observed, base["project"], base["candidate_b"]["candidate_id"])
    assert agent["validation"] == validation["receipt"]
    observe(observed, base["project"], base["candidate_b"]["candidate_id"])


def test_abort_after_the_holdout_read_keeps_the_debit_and_never_retries(
    base: dict, observed: dict
) -> None:
    store = eval_store(base, "holdout-abort")
    plan = create_plan(base, store, base["candidate_b"], plan_input(), name="plan-holdout-abort")
    ok(authorize(store, plan["plan_id"]))
    ok(run_evaluation(base, store, plan["plan_id"]))
    ok(authorize_holdout(store, plan["plan_id"]))

    failing = cli_env(PRAJNATRADE_TEST_FAILPOINT="evaluation_after_read")
    aborted = ok(
        run_evaluation(base, store, plan["plan_id"], stage="final_holdout", env=failing)
    )
    assert aborted["status"] == "aborted"
    assert aborted["outcome"] == "insufficient_evidence"
    assert aborted["scoring"] is False
    assert aborted["receipt"] == HOLDOUT_RECEIPT
    record = aborted["record"]
    assert record["aborted"] is True
    assert record["scoring"] is False
    assert record["opened_protected_source"] is True
    assert record["stage"] == "final_holdout"
    assert record["failure"]["type"] == "RuntimeError"
    assert "evaluation_after_read" in record["failure"]["message"]
    for absent in ("curves", "cost_stress", "neighbourhood"):
        assert absent not in record
    assert record["thresholds"] == []
    assert record["query_receipt"]["status"] == "aborted"

    rows = consumed_rows(store)
    assert len(rows) == 2
    holdout_row = next(row for row in rows if row[0] == "final_holdout")
    assert holdout_row[6] == "aborted"
    evaluations = evaluation_rows(store)
    assert len(evaluations) == 2
    assert next(row for row in evaluations if row[2] == "final_holdout")[5] == (
        "insufficient_evidence"
    )

    replay = ok(run_evaluation(base, store, plan["plan_id"], stage="final_holdout"))
    assert replay["evaluation_id"] == aborted["evaluation_id"]
    assert replay["query"]["reused"] is True
    assert replay["record"] == record
    assert len(consumed_rows(store)) == 2
    assert len(list((store / "records").glob("*/record.json"))) == 2
    assert next(
        row for row in consumed_rows(store) if row[0] == "validation"
    )[6] == "completed"

    report_path = base["root"] / "holdout-abort-report.json"
    ok(report(store, plan["plan_id"], report_path))
    data = json.loads(report_path.read_text(encoding="utf-8"))
    holdout_entry = data["stages"]["final_holdout"]
    assert holdout_entry["present"] is True
    assert holdout_entry["scoring"] is False
    assert holdout_entry["aborted"] is True
    assert data["query_receipts"]["final_holdout"]["status"] == "aborted"
    assert data["verdict"]["evidence_complete"] is False
    assert data["verdict"]["all_stages_passed"] is False
    assert data["verdict"]["incomplete"] is True
    assert data["verdict"]["admission"] is False
    assert any("aborted" in blocker for blocker in data["verdict"]["blockers"])
    observe(observed, base["project"], base["candidate_b"]["candidate_id"])


def test_a_different_plan_cannot_reuse_the_consumed_holdout_range(
    base: dict, observed: dict
) -> None:
    store = eval_store(base, "holdout-consumed")
    plan = create_plan(base, store, base["candidate_a"], plan_input(), name="plan-holdout-first")
    ok(authorize(store, plan["plan_id"]))
    first = ok(run_evaluation(base, store, plan["plan_id"]))
    assert first["outcome"] == "passed"
    ok(authorize_holdout(store, plan["plan_id"]))
    holdout = ok(run_evaluation(base, store, plan["plan_id"], stage="final_holdout"))
    assert holdout["outcome"] == "passed"
    assert len(consumed_rows(store)) == 2

    validation_start = base["partitions"]["validation"]["start"]
    validation_end = base["partitions"]["validation"]["end"]
    resealed = build_scenario(
        base["root"] / "holdout-repack",
        synthetic_snapshot(derived_range=(validation_start, validation_end)),
        "holdout-repack",
    )
    # Only the Validation payload changed; the sealed Holdout content is identical.
    assert resealed["snapshot_id"] != base["snapshot_id"]
    assert resealed["partitions"]["final_holdout"]["content_hash"] == (
        base["partitions"]["final_holdout"]["content_hash"]
    )
    assert resealed["partitions"]["validation"]["content_hash"] != (
        base["partitions"]["validation"]["content_hash"]
    )

    second_plan = create_plan(
        resealed,
        store,
        resealed["candidate"],
        plan_input(),
        name="plan-holdout-second",
        project=resealed["project"],
        snapshot_store=resealed["snapshot_store"],
    )
    ok(authorize(store, second_plan["plan_id"]))
    second_validation = ok(
        run_evaluation(
            resealed,
            store,
            second_plan["plan_id"],
            project=resealed["project"],
            snapshot_store=resealed["snapshot_store"],
        )
    )
    assert second_validation["outcome"] == "passed"
    ok(authorize_holdout(store, second_plan["plan_id"]))
    refused = rejected(
        run_evaluation(
            resealed,
            store,
            second_plan["plan_id"],
            stage="final_holdout",
            project=resealed["project"],
            snapshot_store=resealed["snapshot_store"],
        ),
        "QUERY_ALREADY_CONSUMED",
    )
    assert refused["opened_protected_source"] is False
    assert refused["query_debited"] is False
    rows = consumed_rows(store)
    assert len(rows) == 3
    assert len([row for row in rows if row[0] == "final_holdout"]) == 1
    assert len(list((store / "records").glob("*/record.json"))) == 3

    # A revised candidate on the same sealed range cannot reuse Validation either.
    revised = create_plan(
        base, store, base["candidate_b"], plan_input(), name="plan-holdout-revised"
    )
    ok(authorize(store, revised["plan_id"]))
    rejected(
        run_evaluation(base, store, revised["plan_id"]), "QUERY_ALREADY_CONSUMED"
    )
    assert len(consumed_rows(store)) == 3
    codes = [audit["error"]["code"] for audit in audit_records(store)]
    assert codes == ["QUERY_ALREADY_CONSUMED", "QUERY_ALREADY_CONSUMED"]
    assert all(
        audit["opened_protected_source"] is False
        and audit["query_debited"] is False
        for audit in audit_records(store)
    )

    # The controlled report records the rejected Holdout request, not an
    # invented evaluation, and never claims admission.
    rejection_report = base["root"] / "holdout-consumed-report.json"
    generated = ok(report(store, second_plan["plan_id"], rejection_report))
    assert generated["evidence_complete"] is False
    assert generated["all_stages_passed"] is False
    data = json.loads(rejection_report.read_text(encoding="utf-8"))
    assert data["stages"]["final_holdout"]["present"] is False
    assert data["verdict"]["admission"] is False
    assert [item["code"] for item in data["rejected_requests"]] == [
        "QUERY_ALREADY_CONSUMED"
    ]
    observe(observed, resealed["project"], resealed["candidate"]["candidate_id"])


def test_controlled_report_renders_every_section_from_records_only(
    base: dict, tmp_path: Path
) -> None:
    outputs = tmp_path / "reports"
    outputs.mkdir()
    store = eval_store(base, "report-full")
    plan = create_plan(base, store, base["candidate_b"], plan_input(), name="plan-report")
    ok(authorize(store, plan["plan_id"]))
    validation = ok(run_evaluation(base, store, plan["plan_id"]))
    ok(authorize_holdout(store, plan["plan_id"]))
    holdout = ok(run_evaluation(base, store, plan["plan_id"], stage="final_holdout"))
    assert validation["outcome"] == "passed"
    assert holdout["outcome"] == "passed"

    html_path = outputs / "report.html"
    generated = ok(report(store, plan["plan_id"], html_path))
    assert generated["status"] == "generated"
    assert generated["format"] == "html"
    assert generated["evidence_complete"] is True
    assert generated["all_stages_passed"] is True
    assert generated["admission"] is False
    assert html_path.is_file()
    # The report never re-runs an evaluation or debits a query.
    assert len(consumed_rows(store)) == 2
    assert len(list((store / "records").glob("*/record.json"))) == 2

    text = html_path.read_text(encoding="utf-8")
    expected_needles = (
        "PrajnaTrade controlled evaluation report",
        plan["plan_id"],
        base["candidate_b"]["candidate_hash"],
        base["snapshot_id"],
        base["partitions"]["validation"]["content_hash"],
        base["partitions"]["final_holdout"]["content_hash"],
        "Fingerprints",
        "Authorizations and query receipts",
        "reviewer-1",
        "reviewer-2",
        "Coverage gaps",
        "Validation evidence",
        "Final Holdout evidence",
        "Thresholds",
        "max_drawdown",
        "relative_return",
        "Cost stress",
        "stress",
        "Parameter neighbourhood",
        "Market regimes",
        "calm",
        "unclassified",
        "NAV and drawdown curves",
        "<svg",
        "does not grant admission",
    )
    for needle in expected_needles:
        assert needle in text, needle

    regenerated = outputs / "report-again.html"
    ok(report(store, plan["plan_id"], regenerated))
    assert regenerated.read_bytes() == html_path.read_bytes()

    json_path = outputs / "report.json"
    generated_json = ok(report(store, plan["plan_id"], json_path))
    assert generated_json["format"] == "json"
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert data["kind"] == "controlled_evaluation_report"
    assert data["scope"] == "synthetic"
    assert data["plan_id"] == plan["plan_id"]
    assert data["candidate_hash"] == base["candidate_b"]["candidate_hash"]
    assert data["snapshot_id"] == base["snapshot_id"]
    assert data["partition_fingerprints"]["validation"] == (
        base["partitions"]["validation"]
    )
    assert data["partition_fingerprints"]["final_holdout"] == (
        base["partitions"]["final_holdout"]
    )
    for stage in ("validation", "final_holdout"):
        assert data["authorizations"][stage]["present"] is True
        assert data["authorizations"][stage]["authorization"]["reviewer"] in {
            "reviewer-1",
            "reviewer-2",
        }
        receipt = data["query_receipts"][stage]
        assert receipt["debited"] is True
        assert receipt["status"] == "completed"
        entry = data["stages"][stage]
        assert entry["present"] is True
        assert entry["outcome"] == "passed"
        assert entry["scoring"] is True
        assert entry["record_hash"]
        assert entry["coverage"]["complete"] is True
        assert entry["coverage"]["snapshot_gaps"]
        assert {item["id"] for item in entry["thresholds"]} == {
            "max_drawdown",
            "relative_return",
        }
        for threshold in entry["thresholds"]:
            assert threshold["actual"] is not None
            assert threshold["status"] == "passed"
        assert entry["cost_stress"]["base"]["candidate"]["total_return"] == 0.0
        assert entry["cost_stress"]["stress"]["candidate"]["total_return"] == 0.0
        assert (
            entry["cost_stress"]["stress"]["baseline"]["total_return"]
            < entry["cost_stress"]["base"]["baseline"]["total_return"]
        )
        assert entry["neighbourhood"]["counts"]["variants"] == 2
        assert entry["neighbourhood"]["excluded"]
        assert entry["regimes"]["groups"]
        assert entry["curves"]["candidate"]["base"]
        assert entry["curves"]["baseline"]["stress"]
    assert data["verdict"]["evidence_complete"] is True
    assert data["verdict"]["all_stages_passed"] is True
    assert data["verdict"]["incomplete"] is False
    assert data["verdict"]["admission"] is False
    assert data["verdict"]["blockers"] == []
    assert "does not grant admission" in data["verdict"]["statement"]
    assert data["report_hash"] in text

    json_again = outputs / "report-again.json"
    ok(report(store, plan["plan_id"], json_again))
    assert json_again.read_bytes() == json_path.read_bytes()

    incomplete_store = eval_store(base, "report-incomplete")
    incomplete_plan = create_plan(
        base,
        incomplete_store,
        base["candidate_b"],
        plan_input(),
        name="plan-report-incomplete",
    )
    ok(authorize(incomplete_store, incomplete_plan["plan_id"]))
    partial = ok(run_evaluation(base, incomplete_store, incomplete_plan["plan_id"]))
    assert partial["outcome"] == "passed"

    incomplete_html = outputs / "incomplete.html"
    incomplete = ok(report(incomplete_store, incomplete_plan["plan_id"], incomplete_html))
    assert incomplete["evidence_complete"] is False
    assert incomplete["all_stages_passed"] is False
    assert incomplete["admission"] is False
    incomplete_text = incomplete_html.read_text(encoding="utf-8").lower()
    assert "evidence incomplete" in incomplete_text
    assert "not all stages passed" in incomplete_text
    assert "incomplete" in incomplete_text
    assert "does not grant admission" in incomplete_text

    incomplete_json = outputs / "incomplete.json"
    ok(report(incomplete_store, incomplete_plan["plan_id"], incomplete_json))
    incomplete_data = json.loads(incomplete_json.read_text(encoding="utf-8"))
    assert incomplete_data["stages"]["validation"]["present"] is True
    assert incomplete_data["stages"]["final_holdout"]["present"] is False
    assert incomplete_data["authorizations"]["final_holdout"]["present"] is False
    assert incomplete_data["verdict"]["evidence_complete"] is False
    assert incomplete_data["verdict"]["all_stages_passed"] is False
    assert incomplete_data["verdict"]["incomplete"] is True
    assert incomplete_data["verdict"]["admission"] is False
    blockers = " ".join(incomplete_data["verdict"]["blockers"]).lower()
    assert "final holdout" in blockers

    # A changed record is an explicit error, never a silent fallback.
    record_path = (
        incomplete_store / "records" / partial["evaluation_id"] / "record.json"
    )
    original = record_path.read_bytes()
    record_path.chmod(0o644)
    tampered = json.loads(original)
    tampered["outcome"] = "rejected"
    record_path.write_text(
        json.dumps(tampered, sort_keys=True, separators=(",", ":"))
    )
    record_path.chmod(0o444)
    tampered_output = outputs / "tampered.html"
    rejected(
        report(incomplete_store, incomplete_plan["plan_id"], tampered_output),
        "RECORD_INTEGRITY_FAILED",
    )
    assert not tampered_output.exists()

    # A missing record is an explicit error too.
    record_path.chmod(0o644)
    record_path.unlink()
    missing_output = outputs / "missing.html"
    rejected(
        report(incomplete_store, incomplete_plan["plan_id"], missing_output),
        "RECORD_INTEGRITY_FAILED",
    )
    assert not missing_output.exists()


def test_holdout_uses_the_stored_validation_export_and_never_reopens_validation(
    base: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prajnatrade import protected_eval, snapshot

    store = eval_store(base, "holdout-prior")
    plan = create_plan(base, store, base["candidate_b"], plan_input(), name="plan-holdout-prior")
    ok(authorize(store, plan["plan_id"]))
    validation = ok(run_evaluation(base, store, plan["plan_id"]))
    ok(authorize_holdout(store, plan["plan_id"]))

    calls: list[str] = []
    original = snapshot.read_protected

    def spy(snapshot_store: Path, snapshot_id: str, partition: str) -> dict:
        calls.append(partition)
        return original(snapshot_store, snapshot_id, partition)

    monkeypatch.setattr(snapshot, "read_protected", spy)

    directory = base["snapshot_store"] / base["snapshot_id"]
    payload_path = directory / "validation.json"
    payload_bytes = payload_path.read_bytes()
    directory.chmod(0o755)
    payload_path.chmod(0o644)
    payload_path.unlink()
    directory.chmod(0o555)
    try:
        result = protected_eval.run_evaluation(
            store,
            base["snapshot_store"],
            base["project"],
            plan["plan_id"],
            "final_holdout",
        )
    finally:
        directory.chmod(0o755)
        payload_path.write_bytes(payload_bytes)
        payload_path.chmod(0o444)
        directory.chmod(0o555)

    assert result["status"] == "completed"
    assert result["outcome"] == "passed"
    # Only the sealed Holdout payload was opened; Validation was never re-read.
    assert calls == ["final_holdout"]
    prior = result["record"]["prior_evidence"]
    assert prior["validation"]["evaluation_id"] == validation["evaluation_id"]
    rows = consumed_rows(store)
    assert len(rows) == 2
    assert next(row for row in rows if row[0] == "validation")[6] == "completed"


def test_a_validation_record_without_the_prior_export_is_an_explicit_error(
    base: dict,
) -> None:
    store = eval_store(base, "holdout-legacy")
    plan = create_plan(base, store, base["candidate_a"], plan_input(), name="plan-holdout-legacy")
    ok(authorize(store, plan["plan_id"]))
    validation = ok(run_evaluation(base, store, plan["plan_id"]))
    assert validation["outcome"] == "passed"
    record_path = store / "records" / validation["evaluation_id"] / "record.json"
    original = record_path.read_bytes()

    def write_legacy_record() -> None:
        legacy = json.loads(original)
        del legacy["prior_export_for_holdout"]
        del legacy["record_hash"]
        record_path.chmod(0o644)
        record_path.write_text(
            json.dumps(legacy, sort_keys=True, separators=(",", ":"))
        )
        record_path.chmod(0o444)

    write_legacy_record()
    refused = rejected(
        authorize_holdout(store, plan["plan_id"]), "VALIDATION_EVIDENCE_INVALID"
    )
    assert refused["opened_protected_source"] is False
    assert refused["query_debited"] is False
    assert "record" in refused["error"]["message"].lower()
    assert not list(
        (store / "authorizations" / plan["plan_id"]).glob("final_holdout-*.json")
    )
    assert len(consumed_rows(store)) == 1

    record_path.chmod(0o644)
    record_path.write_bytes(original)
    record_path.chmod(0o444)
    ok(authorize_holdout(store, plan["plan_id"]))
    write_legacy_record()
    refused = rejected(
        run_evaluation(base, store, plan["plan_id"], stage="final_holdout"),
        "VALIDATION_EVIDENCE_INVALID",
    )
    assert refused["opened_protected_source"] is False
    assert refused["query_debited"] is False
    assert len(consumed_rows(store)) == 1
    assert len(list((store / "records").glob("*/record.json"))) == 1
    codes = [audit["error"]["code"] for audit in audit_records(store)]
    assert codes == ["VALIDATION_EVIDENCE_INVALID", "VALIDATION_EVIDENCE_INVALID"]

    record_path.chmod(0o644)
    record_path.write_bytes(original)
    record_path.chmod(0o444)



def test_agent_surface_cannot_authorize_run_or_report(
    base: dict, tmp_path: Path
) -> None:
    common = ("--store", str(tmp_path / "trusted"), "--plan-id", "0" * 64)
    output = str(tmp_path / "agent-report.html")
    assert (
        cli(
            "evaluation", "authorize", *common, "--stage", "final_holdout",
            "--reviewer", "agent", "--reason", "not allowed", agent=True,
        ).returncode
        == 2
    )
    assert (
        cli(
            "evaluation", "run", *common, "--snapshot-store", str(tmp_path / "snapshots"),
            "--stage", "final_holdout", "--project", str(tmp_path / "project"),
            agent=True,
        ).returncode
        == 2
    )
    assert (
        cli("evaluation", "report", *common, "--output", output, agent=True).returncode
        == 2
    )
    assert not (tmp_path / "trusted").exists()
    assert not Path(output).exists()


def test_holdout_receipt_is_byte_identical_across_every_state(
    base: dict, tmp_path: Path
) -> None:
    """The constant Holdout receipt is identical across every research state.

    The test is self-contained: it builds a pending candidate, a rejected
    Validation plan, a passed-but-not-yet-authorized state, the completed
    Holdout state and a replay inside this test instead of relying on
    observations accumulated by earlier tests.
    """
    observations: list[dict] = []
    stdouts: list[str] = []

    def observe(project: Path, candidate_id: str) -> dict:
        result = cli(
            "evaluation",
            "status",
            "--project",
            str(project),
            "--candidate-id",
            candidate_id,
            agent=True,
        )
        payload = ok(result)
        observations.append(payload)
        stdouts.append(result.stdout)
        return payload

    assert base["project"].is_dir()

    # 1. Pending: a frozen candidate with no evaluation plan yet.
    source = write_json(tmp_path / "submission-receipts.json", {})
    submit_experiment(
        base["project"],
        base["campaign"]["campaign_id"],
        source,
        "receipt-pending",
        "A candidate that has no protected evaluation plan yet.",
        "equal_weight",
        {},
    )
    pending_candidate = freeze_candidate(
        base["project"],
        base["campaign"]["campaign_id"],
        "receipt-pending",
        "candidate-receipt-pending",
        "Pending state for the constant receipt test.",
    )
    pending = observe(base["project"], pending_candidate["candidate_id"])
    assert pending["validation"] == {"status": "pending"}

    # 2. Rejected Validation: a plan whose hard threshold fails.
    rejected_store = eval_store(base, "receipt-rejected")
    rejected_plan = create_plan(
        base,
        rejected_store,
        pending_candidate,
        plan_input(
            thresholds=[
                {"id": "relative", "metric": "return_delta", "op": ">=", "value": 0.5}
            ]
        ),
        name="plan-receipt-rejected",
    )
    ok(authorize(rejected_store, rejected_plan["plan_id"]))
    rejected_run = ok(run_evaluation(base, rejected_store, rejected_plan["plan_id"]))
    assert rejected_run["outcome"] == "rejected"
    rejected_payload = observe(base["project"], pending_candidate["candidate_id"])
    assert rejected_payload["validation"]["status"] == "rejected"

    # 3/4/5. Unauthorized, completed and replayed Holdout states.
    submit_experiment(
        base["project"],
        base["campaign"]["campaign_id"],
        source,
        "receipt-complete",
        "A candidate whose Validation passes before Holdout authorization.",
        "momentum",
        {"lookback": 2},
    )
    completed_candidate = freeze_candidate(
        base["project"],
        base["campaign"]["campaign_id"],
        "receipt-complete",
        "candidate-receipt-complete",
        "Completed state for the constant receipt test.",
    )
    completed_store = eval_store(base, "receipt-completed")
    completed_plan = create_plan(
        base,
        completed_store,
        completed_candidate,
        plan_input(),
        name="plan-receipt-completed",
    )
    ok(authorize(completed_store, completed_plan["plan_id"]))
    validation = ok(run_evaluation(base, completed_store, completed_plan["plan_id"]))
    assert validation["outcome"] == "passed"

    unauthorized = observe(base["project"], completed_candidate["candidate_id"])
    assert unauthorized["validation"]["status"] == "passed"

    ok(authorize_holdout(completed_store, completed_plan["plan_id"]))
    holdout = ok(
        run_evaluation(
            base, completed_store, completed_plan["plan_id"], stage="final_holdout"
        )
    )
    assert holdout["outcome"] == "passed"
    completed = observe(base["project"], completed_candidate["candidate_id"])
    assert completed["validation"]["status"] == "passed"
    replayed = observe(base["project"], completed_candidate["candidate_id"])
    assert replayed == completed

    # The receipt bytes are constant across all five states.
    assert len(observations) == 5
    serialized = {
        json.dumps(payload["final_holdout"], sort_keys=True, separators=(",", ":"))
        for payload in observations
    }
    assert serialized == {
        json.dumps(HOLDOUT_RECEIPT, sort_keys=True, separators=(",", ":"))
    }
    statuses = {payload["validation"].get("status") for payload in observations}
    assert {"pending", "rejected", "passed"} <= statuses
    for payload in observations:
        assert "validation" not in json.dumps(payload["final_holdout"])
        assert "holdout_outcome" not in payload
        assert "admission" not in payload
    for stdout in stdouts:
        assert "2024-" not in stdout
        assert "evaluation_id" not in stdout
        assert "auth_id" not in stdout
        assert "debited_at" not in stdout
        assert "record_path" not in stdout
        assert "scoring" not in stdout
        assert "curves" not in stdout
        assert "skipped" not in stdout
        assert "outcome" not in stdout
        assert "aborted" not in stdout
        assert "duration" not in stdout
