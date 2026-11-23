"""Controlled evaluation report rendered from immutable trusted records (MVP-11).

`prajnatrade evaluation report` is operator-only.  It reads the frozen plan,
the stored authorizations, the query ledger, rejected-request audits and the
immutable evaluation records; it never re-runs an evaluation, never opens a
sealed snapshot partition and never debits a query.  The same report data is
rendered to deterministic HTML (inline SVG curves) or JSON.

Missing evidence is rendered as *evidence incomplete*; a changed or missing
record is an explicit error instead of a silent fallback.  The verdict names
``evidence_complete`` and ``all_stages_passed`` explicitly and never claims
admission.
"""

from __future__ import annotations

import html
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from prajnatrade import protected_eval, snapshot

REPORT_SCHEMA_VERSION = 1
REPORT_KIND = "controlled_evaluation_report"
STAGES = protected_eval.STAGES
STAGE_LABELS = {
    protected_eval.VALIDATION_STAGE: "Validation",
    protected_eval.HOLDOUT_STAGE: "Final Holdout",
}
ADMISSION_STATEMENT = (
    "This controlled report is generated only from immutable trusted records. "
    "It does not grant admission, approve a release, or replace human review; "
    "admission requires the separate human release workflow."
)


def _rejection(code: str, message: str) -> protected_eval.EvaluationRejection:
    return protected_eval.EvaluationRejection(code, "report", "inspect", message)


def _ledger_rows(store: Path) -> dict[str, list[dict[str, Any]]]:
    empty: dict[str, list[dict[str, Any]]] = {"consumed": [], "evaluations": []}
    database = store / "queries.sqlite"
    if not database.is_file():
        return empty
    connection = sqlite3.connect(database, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        consumed = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM consumed_queries ORDER BY debited_at, partition_name"
            )
        ]
        evaluations = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM evaluations ORDER BY created_at, evaluation_id"
            )
        ]
    except sqlite3.OperationalError:
        return empty
    finally:
        connection.close()
    return {"consumed": consumed, "evaluations": evaluations}


def _authorizations(store: Path, plan_id: str) -> dict[str, dict[str, Any] | None]:
    result: dict[str, dict[str, Any] | None] = {}
    for stage in STAGES:
        try:
            records = protected_eval._authorizations(store, plan_id, stage)
        except protected_eval.EvaluationRejection as error:
            raise _rejection(error.details["code"], error.details["message"]) from error
        if len(records) > 1:
            raise _rejection(
                "RECORD_INTEGRITY_FAILED",
                f"The {STAGE_LABELS[stage]} authorization is ambiguous.",
            )
        result[stage] = records[0] if records else None
    return result


def _load_record(
    store: Path, plan: dict[str, Any], row: dict[str, Any], stage: str
) -> tuple[dict[str, Any], Path]:
    stored_path = row.get("record_path")
    path = (
        Path(stored_path)
        if isinstance(stored_path, str) and stored_path
        else store / "records" / str(row.get("evaluation_id")) / "record.json"
    )
    if not path.is_file():
        raise _rejection(
            "RECORD_INTEGRITY_FAILED",
            f"The {STAGE_LABELS[stage]} evaluation record is missing "
            f"({row.get('evaluation_id')}); the report refuses to fall back.",
        )
    try:
        record = snapshot._read_json(path)
        protected_eval._verify_record(record)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise _rejection(
            "RECORD_INTEGRITY_FAILED",
            f"The {STAGE_LABELS[stage]} evaluation record is unusable: {error}",
        ) from error
    if record.get("evaluation_id") != row.get("evaluation_id") or not (
        protected_eval._record_matches(record, plan, stage)
    ):
        raise _rejection(
            "RECORD_INTEGRITY_FAILED",
            f"The {STAGE_LABELS[stage]} evaluation record is not bound to this "
            "frozen plan, candidate, snapshot and partition fingerprint.",
        )
    return record, path


def _threshold_rows(
    plan: dict[str, Any], record: dict[str, Any]
) -> list[dict[str, Any]]:
    stored = {
        item.get("id"): item
        for item in (record.get("thresholds") or [])
        if isinstance(item, dict)
    }
    rows = []
    for entry in plan["thresholds"]:
        item = stored.get(entry["id"])
        if item is None:
            rows.append(
                {
                    **entry,
                    "actual": None,
                    "status": "unavailable",
                    "message": "No stored verdict for this preregistered threshold.",
                }
            )
        else:
            rows.append(
                {
                    "id": entry["id"],
                    "metric": entry["metric"],
                    "op": entry["op"],
                    "value": entry["value"],
                    "actual": item.get("actual"),
                    "status": item.get("status"),
                    "message": item.get("message"),
                }
            )
    return rows


def _stage_section(
    plan: dict[str, Any], record: dict[str, Any], path: Path
) -> dict[str, Any]:
    isolation = record.get("isolation")
    if not isinstance(isolation, dict):
        isolation = None
    coverage = record.get("coverage")
    curves = record.get("curves")
    return {
        "present": True,
        "evaluation_id": record["evaluation_id"],
        "record_hash": record["record_hash"],
        "record_path": str(path),
        "outcome": record.get("outcome"),
        "reason": record.get("reason"),
        "message": record.get("message"),
        "scoring": record.get("scoring"),
        "aborted": record.get("aborted"),
        "opened_protected_source": record.get("opened_protected_source"),
        "isolation": {
            "verified": record.get("isolation_verified") is True,
            "memory_limit_enforced": record.get("memory_limit_enforced"),
            "failure_reason": record.get("isolation_failure_reason"),
        },
        "coverage": coverage,
        "minimum_samples": record.get("minimum_samples"),
        "thresholds": _threshold_rows(plan, record),
        "verdict": record.get("verdict"),
        "cost_stress": record.get("cost_stress"),
        "neighbourhood": record.get("neighbourhood"),
        "regimes": record.get("regimes"),
        "curves": curves,
        "prior_evidence": record.get("prior_evidence"),
        "query_receipt": record.get("query_receipt"),
        "created_at": record.get("created_at"),
        "completed_at": record.get("completed_at"),
    }


def _missing_stage_section(stage: str) -> dict[str, Any]:
    return {
        "present": False,
        "evaluation_id": None,
        "record_hash": None,
        "record_path": None,
        "outcome": None,
        "reason": None,
        "message": f"No {STAGE_LABELS[stage]} evaluation record is stored.",
        "scoring": None,
        "aborted": None,
        "opened_protected_source": None,
        "isolation": None,
        "coverage": None,
        "minimum_samples": None,
        "thresholds": [],
        "verdict": None,
        "cost_stress": None,
        "neighbourhood": None,
        "regimes": None,
        "curves": None,
        "prior_evidence": None,
        "query_receipt": None,
        "created_at": None,
        "completed_at": None,
    }


def _verdict(
    plan: dict[str, Any],
    stages: dict[str, dict[str, Any]],
    authorizations: dict[str, dict[str, Any] | None],
    query_receipts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    incomplete: list[str] = []
    failed: list[str] = []
    for stage in STAGES:
        label = STAGE_LABELS[stage]
        section = stages[stage]
        if not section["present"]:
            incomplete.append(f"{label} evaluation record is missing.")
            continue
        if section["scoring"] is not True or section["aborted"] is not False:
            incomplete.append(
                f"{label} evaluation was aborted or unscored; no metrics are available."
            )
        elif section["outcome"] != "passed":
            failed.append(
                f"{label} evaluation outcome is {section['outcome']!r}, not 'passed'."
            )
        coverage = section["coverage"] or {}
        if coverage.get("complete") is not True:
            incomplete.append(f"{label} coverage or minimum samples are incomplete.")
        for threshold in section["thresholds"]:
            if threshold["status"] == "unavailable":
                incomplete.append(
                    f"{label} threshold {threshold['id']!r} is unavailable."
                )
            elif threshold["status"] != "passed":
                failed.append(
                    f"{label} threshold {threshold['id']!r} was {threshold['status']}."
                )
        if section["isolation"] is None or section["isolation"]["verified"] is not True:
            incomplete.append(f"{label} isolation evidence is not verified.")
        curves = section["curves"] or {}
        if not all(
            isinstance(curves.get(side, {}).get(cost), list)
            and curves[side][cost]
            for side in ("candidate", "baseline")
            for cost in ("base", "stress")
        ):
            incomplete.append(f"{label} candidate/baseline NAV and drawdown curves are missing.")
        if not section["cost_stress"]:
            incomplete.append(f"{label} base and stress cost results are missing.")
        if not section["regimes"] or not section["regimes"].get("groups"):
            incomplete.append(f"{label} market-regime samples are missing.")
        if not section["neighbourhood"]:
            incomplete.append(f"{label} parameter-neighbourhood evidence is missing.")
        if authorizations[stage] is None:
            incomplete.append(f"{label} authorization is missing.")
        if query_receipts[stage].get("debited") is not True:
            incomplete.append(f"{label} query debit receipt is missing.")
    blockers = [*incomplete, *failed]
    all_stages_passed = all(
        stages[stage]["present"] is True
        and stages[stage]["scoring"] is True
        and stages[stage]["aborted"] is False
        and stages[stage]["outcome"] == "passed"
        for stage in STAGES
    )
    return {
        "evidence_complete": not blockers,
        "all_stages_passed": all_stages_passed,
        "incomplete": bool(incomplete),
        "admission": False,
        "blockers": blockers,
        "statement": ADMISSION_STATEMENT,
    }


def _legacy_report_evidence(eval_store: Path, plan: dict) -> tuple[dict, dict, dict]:
    plan_id = plan["plan_id"]
    ledger = _ledger_rows(eval_store)
    authorizations = _authorizations(eval_store, plan_id)

    plan_evaluations = [
        row for row in ledger["evaluations"] if row.get("plan_id") == plan_id
    ]
    stages: dict[str, dict[str, Any]] = {}
    for stage in STAGES:
        candidates = sorted(
            (row for row in plan_evaluations if row.get("stage") == stage),
            key=lambda row: (str(row.get("created_at") or ""), str(row.get("evaluation_id"))),
        )
        if not candidates:
            stages[stage] = _missing_stage_section(stage)
            continue
        latest = candidates[-1]
        record, path = _load_record(eval_store, plan, latest, stage)
        stages[stage] = _stage_section(plan, record, path)

    debits = {}
    for stage in STAGES:
        matching = [
            row
            for row in ledger["consumed"]
            if row.get("plan_id") == plan_id and row.get("partition_name") == stage
        ]
        debits[stage] = matching[0] if matching else None
    return stages, authorizations, debits


def _research_report_evidence(store: Path, plan: dict) -> tuple[dict, dict, dict, dict]:
    from prajnatrade import research_holdout

    validation_stage = protected_eval.VALIDATION_STAGE
    holdout_stage = protected_eval.HOLDOUT_STAGE
    selected = plan["candidate"]["research_selection"]
    proof = protected_eval.selected_validation_evidence(
        store, selected["round_id"], selected["validation_evaluation_id"], plan["candidate"],
    )
    validation = proof["record"]
    authorization = research_holdout.load_authorization(store, plan["plan_id"])
    claim = research_holdout.load_holdout_claim(store, plan["plan_id"])
    stages = {
        validation_stage: _stage_section(
            proof["plan"], validation, store / "records" / validation["evaluation_id"] / "record.json",
        ),
        holdout_stage: _missing_stage_section(holdout_stage),
    }
    if claim:
        record_path = store / "records" / claim["evaluation_id"] / "record.json"
        if record_path.is_file() or claim["status"] in {"completed", "aborted"}:
            record, path = _load_record(store, plan, claim, holdout_stage)
            if (
                authorization is None
                or record["authorization"] != authorization
                or claim["authorization_id"] != authorization["auth_id"]
                or record["query_receipt"]["debited_at"] != claim["debited_at"]
            ):
                raise _rejection("RECORD_INTEGRITY_FAILED", "The Holdout authorization and debit differ.")
            stages[holdout_stage] = _stage_section(plan, record, path)
    authorizations = {validation_stage: validation["authorization"], holdout_stage: authorization}
    debits = {validation_stage: proof["debit"], holdout_stage: claim}
    context = {
        "workflow": research_holdout.WORKFLOW,
        "direction_id": selected["direction_id"],
        "round_id": selected["round_id"],
        "selection_summary": selected["summary"],
        "validation_binding": plan["validation_binding"],
        "holdout_blockers": research_holdout.blockers(store, plan["plan_id"]),
    }
    return stages, authorizations, debits, context


def _query_receipt(plan: dict, stage: str, debit: dict | None) -> dict:
    row = debit or {}
    return {
        "debited": debit is not None,
        "partition": stage,
        "partition_content_hash": plan["partitions"][stage]["content_hash"],
        "status": row.get("status"),
        "debited_at": row.get("debited_at"),
        "evaluation_id": row.get("evaluation_id"),
        "authorization_id": row.get("authorization_id"),
    }


def build_evaluation_report(store: str | Path, plan_id: str) -> dict[str, Any]:
    """Assemble the controlled report data from immutable stored evidence."""
    eval_store = protected_eval._store(store)
    plan = protected_eval.load_plan(eval_store, plan_id)
    context = {}
    if plan.get("workflow") == "research_holdout":
        stages, authorizations, debits, context = _research_report_evidence(eval_store, plan)
    else:
        stages, authorizations, debits = _legacy_report_evidence(eval_store, plan)
    query_receipts = {stage: _query_receipt(plan, stage, debits[stage]) for stage in STAGES}

    rejected_requests = []
    audit_directory = eval_store / "audit"
    if audit_directory.is_dir():
        for path in sorted(audit_directory.glob("*.json")):
            try:
                audit = snapshot._read_json(path)
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(audit, dict) or audit.get("plan_id") != plan_id:
                continue
            error = audit.get("error") if isinstance(audit.get("error"), dict) else {}
            rejected_requests.append(
                {
                    "audit_id": audit.get("audit_id"),
                    "command": audit.get("command"),
                    "stage": audit.get("stage"),
                    "code": error.get("code"),
                    "message": error.get("message"),
                    "received_at": audit.get("received_at"),
                    "opened_protected_source": audit.get("opened_protected_source"),
                    "query_debited": audit.get("query_debited"),
                }
            )
    rejected_requests.sort(
        key=lambda item: (
            str(item.get("received_at") or ""),
            str(item.get("audit_id") or ""),
        )
    )

    authorization_sections = {}
    for stage in STAGES:
        record = authorizations[stage]
        authorization_sections[stage] = {
            "present": record is not None,
            "authorization": record,
        }

    verdict = _verdict(plan, stages, authorizations, query_receipts)
    if context:
        verdict["evidence_complete"] = not verdict["incomplete"]
        verdict["statement"] = (
            "The report records the selected Validation and direction-bound Holdout. "
            "The pre-read human authorization governs release on a trusted pass."
        )
    body: dict[str, Any] = {
        **context,
        "schema_version": 2 if context else REPORT_SCHEMA_VERSION,
        "kind": REPORT_KIND,
        "scope": plan["scope"],
        "plan_id": plan["plan_id"],
        "plan_hash": plan["plan_id"],
        "candidate_id": plan["candidate_id"],
        "candidate_hash": plan["candidate_hash"],
        "snapshot_id": plan["snapshot_id"],
        "project_id": plan["project_id"],
        "partition_fingerprints": {
            stage: plan["partitions"][stage] for stage in STAGES
        },
        "execution": {
            "initial_cash": plan["initial_cash"],
            "base_costs": plan["base_costs"],
            "stress_costs": plan["stress_costs"],
            "signal_window_sessions": plan["signal_window_sessions"],
            "execution_rules": plan["execution_rules"],
            "minimum_samples": plan["minimum_samples"],
            "regimes": plan["regimes"],
        },
        "authorizations": authorization_sections,
        "query_receipts": query_receipts,
        "rejected_requests": rejected_requests,
        "stages": stages,
        "verdict": verdict,
    }
    body["report_hash"] = snapshot._hash(body)
    return body


# ---------------------------------------------------------------------------
# HTML rendering (inline SVG, deterministic)
# ---------------------------------------------------------------------------


def _esc(value: object) -> str:
    return html.escape(str(value))


def _format(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.6f}"
    if isinstance(value, (dict, list)):
        return _esc(json.dumps(value, sort_keys=True, allow_nan=False))
    return _esc(value)


def _definition_list(items: list[tuple[str, Any]]) -> str:
    rows = "".join(
        f"<dt>{_esc(label)}</dt><dd>{_format(value)}</dd>" for label, value in items
    )
    return f"<dl>{rows}</dl>"


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return '<p class="muted">No stored rows.</p>'
    head = "".join(f"<th>{_esc(header)}</th>" for header in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{_format(cell)}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _svg_plot(points: list[tuple[str, float]], color: str, label: str) -> str:
    values = [(date, float(value)) for date, value in points if value is not None]
    if len(values) < 2:
        return (
            f'<p class="muted">Insufficient points for the {_esc(label)} curve.</p>'
        )
    width, height, margin = 700, 180, 12
    numbers = [value for _, value in values]
    low, high = min(numbers), max(numbers)
    spread = high - low or 1.0
    plotted = []
    for index, (_date, value) in enumerate(values):
        x = margin + (width - 2 * margin) * index / max(1, len(values) - 1)
        y = height - margin - (height - 2 * margin) * (value - low) / spread
        plotted.append(f"{x:.1f},{y:.1f}")
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{_esc(label)}">'
        f'<polyline points="{" ".join(plotted)}" fill="none" '
        f'stroke="{color}" stroke-width="2.5"/>'
        "</svg>"
    )


def _curve_sections(stage: str, section: dict[str, Any]) -> str:
    label = STAGE_LABELS[stage]
    curves = section.get("curves")
    if not isinstance(curves, dict):
        return (
            f'<h3>NAV and drawdown curves — {_esc(label)}</h3>'
            f'<p class="muted">{_esc(label)} NAV/drawdown curves are missing.</p>'
        )
    parts = [f"<h3>NAV and drawdown curves — {_esc(label)}</h3>"]
    for side, side_label in (
        ("candidate", "Candidate"),
        ("baseline", "Equal-weight baseline"),
    ):
        for cost in ("base", "stress"):
            points = curves.get(side, {}).get(cost)
            if not isinstance(points, list) or not points:
                parts.append(
                    f'<p class="muted">The {_esc(side_label)} {_esc(cost)} curve is missing.</p>'
                )
                continue
            nav = [(point.get("date"), point.get("nav")) for point in points]
            drawdown = [
                (point.get("date"), point.get("drawdown")) for point in points
            ]
            parts.append(f"<h5>{_esc(side_label)} {_esc(cost)} NAV</h5>")
            parts.append(
                _svg_plot(
                    nav,
                    "#2665a8",
                    f"{side_label} {cost} NAV over {label} sessions",
                )
            )
            parts.append(f"<h5>{_esc(side_label)} {_esc(cost)} drawdown</h5>")
            parts.append(
                _svg_plot(
                    drawdown,
                    "#c14444",
                    f"{side_label} {cost} drawdown over {label} sessions",
                )
            )
    return "".join(parts)


def _coverage_section(stage: str, section: dict[str, Any]) -> str:
    coverage = section.get("coverage")
    if not isinstance(coverage, dict):
        return (
            f'<h4>Coverage gaps — {_esc(STAGE_LABELS[stage])}</h4>'
            f'<p class="muted">{_esc(STAGE_LABELS[stage])} coverage evidence is missing.</p>'
        )
    snapshot_gaps = coverage.get("snapshot_gaps") or {}
    runner_gaps = coverage.get("runner_gaps") or {}
    parts = [
        f"<h4>Coverage gaps — {_esc(STAGE_LABELS[stage])}</h4>",
        _definition_list(
            [
                ("Sessions", coverage.get("sessions")),
                ("Valued sessions", coverage.get("valued_sessions")),
                ("Minimum samples", coverage.get("minimum_samples")),
                ("Samples sufficient", coverage.get("samples_sufficient")),
                ("Coverage complete", coverage.get("complete")),
                ("Candidate signals", coverage.get("candidate_signals")),
                ("Baseline signals", coverage.get("baseline_signals")),
                ("Snapshot raw-bar gaps", snapshot_gaps.get("raw_bars")),
                ("Snapshot derived gaps", snapshot_gaps.get("derived")),
                ("Snapshot corporate-action gaps", snapshot_gaps.get("corporate_actions")),
                ("Snapshot availability gaps", snapshot_gaps.get("availability")),
                ("Runner raw-bar gaps", runner_gaps.get("raw_bars")),
                ("Runner derived gaps", runner_gaps.get("derived")),
                ("Runner valuation gaps", runner_gaps.get("valuation")),
                ("Runner signal gaps", runner_gaps.get("signal_gaps")),
            ]
        ),
    ]
    return "".join(parts)


def _threshold_section(stage: str, section: dict[str, Any]) -> str:
    rows = [
        [
            item.get("id"),
            item.get("metric"),
            item.get("op"),
            item.get("value"),
            item.get("actual"),
            item.get("status"),
            item.get("message"),
        ]
        for item in section.get("thresholds") or []
    ]
    heading = f"<h3>Thresholds — {_esc(STAGE_LABELS[stage])}</h3>"
    if not rows:
        return heading + '<p class="muted">No preregistered threshold verdicts are stored.</p>'
    return heading + _table(
        ["Id", "Metric", "Op", "Threshold", "Actual", "Verdict", "Message"], rows
    )


def _cost_stress_section(stage: str, section: dict[str, Any]) -> str:
    stress = section.get("cost_stress")
    heading = f"<h3>Cost stress — {_esc(STAGE_LABELS[stage])}</h3>"
    if not isinstance(stress, dict):
        return heading + '<p class="muted">Cost-stress evidence is missing.</p>'
    rows = []
    for label in ("base", "stress"):
        block = stress.get(label) or {}
        candidate = block.get("candidate") or {}
        baseline = block.get("baseline") or {}
        rows.append(
            [
                label,
                candidate.get("total_return"),
                baseline.get("total_return"),
                block.get("return_delta"),
                candidate.get("max_drawdown"),
                baseline.get("max_drawdown"),
                candidate.get("fees_paid"),
                candidate.get("slippage_paid"),
            ]
        )
    delta = stress.get("stress_minus_base") or {}
    return (
        heading
        + _table(
            [
                "Costs",
                "Candidate return",
                "Baseline return",
                "Relative return",
                "Candidate drawdown",
                "Baseline drawdown",
                "Candidate fees",
                "Candidate slippage",
            ],
            rows,
        )
        + _definition_list(
            [
                ("Stress minus base candidate return", delta.get("candidate_return_delta")),
                ("Stress minus base baseline return", delta.get("baseline_return_delta")),
            ]
        )
    )


def _neighbourhood_section(stage: str, section: dict[str, Any]) -> str:
    neighbourhood = section.get("neighbourhood")
    heading = f"<h3>Parameter neighbourhood — {_esc(STAGE_LABELS[stage])}</h3>"
    if not isinstance(neighbourhood, dict):
        return heading + '<p class="muted">Parameter-neighbourhood evidence is missing.</p>'
    counts = neighbourhood.get("counts") or {}
    rows = [
        [
            variant.get("changed_parameters"),
            variant.get("parameters"),
            variant.get("signal_window_sessions"),
            variant.get("status"),
            variant.get("reason"),
            variant.get("sharpe_drop"),
            variant.get("drawdown_increase"),
        ]
        for variant in neighbourhood.get("variants") or []
    ]
    excluded = [
        [
            exclusion.get("changed_parameters"),
            exclusion.get("parameters"),
            exclusion.get("reason"),
            exclusion.get("exclusion_reason"),
        ]
        for exclusion in neighbourhood.get("excluded") or []
    ]
    return (
        heading
        + _definition_list(
            [
                ("Applicable", neighbourhood.get("applicable")),
                ("Diagnostic only", neighbourhood.get("diagnostic_only")),
                ("No tunable parameters", (neighbourhood.get("definition") or {}).get("no_tunable_parameters")),
                ("Variants", counts.get("variants")),
                ("Stable", counts.get("stable")),
                ("Degraded", counts.get("degraded")),
                ("Not evaluable", counts.get("not_evaluable")),
                ("Excluded", counts.get("excluded")),
            ]
        )
        + "<h5>Variants</h5>"
        + _table(
            [
                "Changed",
                "Parameters",
                "Signal window",
                "Status",
                "Reason",
                "Sharpe drop",
                "Drawdown increase",
            ],
            rows,
        )
        + "<h5>Exclusions</h5>"
        + _table(["Changed", "Parameters", "Reason", "Exclusion"], excluded)
    )


def _regimes_section(stage: str, section: dict[str, Any]) -> str:
    regimes = section.get("regimes")
    heading = f"<h3>Market regimes — {_esc(STAGE_LABELS[stage])}</h3>"
    if not isinstance(regimes, dict):
        return heading + '<p class="muted">Market-regime evidence is missing.</p>'
    rows = [
        [
            group.get("label"),
            group.get("sessions"),
            group.get("candidate_samples"),
            group.get("baseline_samples"),
            group.get("candidate_mean_return"),
            group.get("baseline_mean_return"),
            group.get("mean_relative_return"),
        ]
        for group in regimes.get("groups") or []
    ]
    return (
        heading
        + _definition_list(
            [
                ("Regime definition", regimes.get("definition")),
                ("Diagnostic only", regimes.get("diagnostic_only")),
            ]
        )
        + _table(
            [
                "Label",
                "Sessions",
                "Candidate samples",
                "Baseline samples",
                "Candidate mean return",
                "Baseline mean return",
                "Mean relative return",
            ],
            rows,
        )
    )


def _evidence_section(stage: str, section: dict[str, Any]) -> str:
    label = STAGE_LABELS[stage]
    heading = f"<h2>{_esc(label)} evidence</h2>"
    if not section.get("present"):
        return (
            heading
            + '<p class="missing">Evidence incomplete / not admitted: '
            + _esc(section.get("message") or "no evaluation record is stored.")
            + "</p>"
        )
    parts = [
        heading,
        _definition_list(
            [
                ("Outcome", section.get("outcome")),
                ("Reason", section.get("reason")),
                ("Message", section.get("message")),
                ("Scoring", section.get("scoring")),
                ("Aborted", section.get("aborted")),
                ("Sealed source opened", section.get("opened_protected_source")),
                ("Evaluation id", section.get("evaluation_id")),
                ("Record hash", section.get("record_hash")),
                ("Record path", section.get("record_path")),
                ("Completed at", section.get("completed_at")),
                ("Isolation verified", (section.get("isolation") or {}).get("verified")),
                (
                    "Isolation memory limit enforced",
                    (section.get("isolation") or {}).get("memory_limit_enforced"),
                ),
                (
                    "Isolation failure reason",
                    (section.get("isolation") or {}).get("failure_reason"),
                ),
                ("Prior evidence", section.get("prior_evidence")),
            ]
        ),
        _coverage_section(stage, section),
        _threshold_section(stage, section),
        _cost_stress_section(stage, section),
        _neighbourhood_section(stage, section),
        _regimes_section(stage, section),
        _curve_sections(stage, section),
    ]
    return "".join(parts)


def _authorization_section(
    stage: str,
    section: dict[str, Any],
    receipt: dict[str, Any],
) -> str:
    label = STAGE_LABELS[stage]
    heading = f"<h3>{_esc(label)}</h3>"
    authorization = section.get("authorization")
    if not isinstance(authorization, dict):
        authorization_html = (
            '<p class="missing">No authorization is stored for this stage.</p>'
        )
    else:
        authorization_html = _definition_list(
            [
                ("Reviewer", authorization.get("reviewer")),
                ("Reason", authorization.get("reason")),
                ("Authorized at", authorization.get("authorized_at")),
                ("Authorization id", authorization.get("auth_id")),
                ("Partition content hash", authorization.get("partition_content_hash")),
                ("Validation binding", authorization.get("validation")),
            ]
        )
    receipt_html = _definition_list(
        [
            ("Query debited", receipt.get("debited")),
            ("Debited at", receipt.get("debited_at")),
            ("Ledger status", receipt.get("status")),
            ("Evaluation id", receipt.get("evaluation_id")),
            ("Authorization id", receipt.get("authorization_id")),
            ("Partition content hash", receipt.get("partition_content_hash")),
        ]
    )
    return (
        heading
        + "<h4>Authorization</h4>"
        + authorization_html
        + "<h4>Query receipt</h4>"
        + receipt_html
    )


def _style() -> str:
    return (
        "<style>"
        "body{font-family:system-ui,sans-serif;margin:0;color:#1d2733;background:#f7f8fa}"
        "main{max-width:980px;margin:0 auto;padding:2rem}"
        "h1{font-size:1.6rem}h2{margin-top:2.2rem;border-bottom:1px solid #d7dbe0}"
        "h3,h4,h5{margin-bottom:.4rem}"
        "table{border-collapse:collapse;width:100%;margin:.6rem 0;font-size:.85rem}"
        "th,td{border:1px solid #d7dbe0;padding:.35rem .5rem;text-align:left;vertical-align:top}"
        "th{background:#eef1f4}"
        "dl{display:grid;grid-template-columns:18rem 1fr;gap:.2rem .8rem;margin:.4rem 0}"
        "dt{font-weight:600}dd{margin:0;overflow-wrap:anywhere}"
        ".badge{display:inline-block;background:#1d2733;color:#fff;padding:.15rem .5rem;border-radius:.2rem}"
        ".statement{background:#fff4d6;border-left:4px solid #b8860b;padding:.6rem .8rem}"
        ".verdict{font-weight:600}.missing{color:#a12a2a}"
        ".muted{color:#5a6672}pre{white-space:pre-wrap;overflow-wrap:anywhere}"
        "svg{width:100%;height:auto;background:#fff;border:1px solid #e2e6ea}"
        "</style>"
    )


def render_report_html(report: dict[str, Any]) -> str:
    verdict = report["verdict"]
    state = (
        "Evidence complete"
        if verdict["evidence_complete"]
        else "Evidence incomplete"
    )
    stages_state = (
        "All stages passed"
        if verdict["all_stages_passed"]
        else "Not all stages passed"
    )
    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>PrajnaTrade controlled evaluation report — {_esc(report['plan_id'])}</title>",
        _style(),
        "</head><body><main>",
        "<header>",
        "<h1>PrajnaTrade controlled evaluation report</h1>",
        '<p class="badge">SYNTHETIC DATA — ENGINEERING EVIDENCE ONLY</p>'
        if report["scope"] == "synthetic" else '<p class="badge">REAL HISTORICAL DATA</p>',
        f'<p class="statement">{_esc(report["verdict"]["statement"])}</p>',
        f'<p class="verdict">{state} · {stages_state} · '
        "Admission: not granted</p>",
        "</header>",
        "<section><h2>Verdict</h2>",
        _definition_list(
            [
                ("Evidence complete", verdict["evidence_complete"]),
                ("All stages passed", verdict["all_stages_passed"]),
                ("Evidence incomplete", verdict["incomplete"]),
                ("Admission", verdict["admission"]),
                ("Report hash", report.get("report_hash")),
                ("Scope", report.get("scope")),
                ("Kind", report.get("kind")),
            ]
        ),
    ]
    blockers = report["verdict"]["blockers"]
    if blockers:
        parts.append("<h3>Blockers</h3><ul>")
        parts.extend(f"<li>{_esc(blocker)}</li>" for blocker in blockers)
        parts.append("</ul>")
    else:
        parts.append("<p>No blocking evidence gaps were found in the stored records.</p>")
    parts.append("</section>")

    if report.get("workflow") == "research_holdout":
        parts.extend((
            "<section><h2>Research selection</h2>",
            _definition_list([
                ("Workflow", report["workflow"]),
                ("Direction ID", report["direction_id"]),
                ("Round ID", report["round_id"]),
                ("Selection summary", report["selection_summary"]),
                ("Validation binding", report["validation_binding"]),
                ("Holdout eligibility blockers", report["holdout_blockers"]),
            ]),
            "</section>",
        ))

    parts.append("<section><h2>Fingerprints</h2>")
    fingerprint_items = [
        ("Plan id", report["plan_id"]),
        ("Plan hash", report["plan_hash"]),
        ("Candidate id", report["candidate_id"]),
        ("Candidate hash", report["candidate_hash"]),
        ("Snapshot manifest", report["snapshot_id"]),
        ("Project id", report["project_id"]),
    ]
    for stage in STAGES:
        descriptor = report["partition_fingerprints"][stage]
        fingerprint_items.extend(
            [
                (f"{STAGE_LABELS[stage]} partition", stage),
                (f"{STAGE_LABELS[stage]} start", descriptor.get("start")),
                (f"{STAGE_LABELS[stage]} end", descriptor.get("end")),
                (f"{STAGE_LABELS[stage]} sessions", descriptor.get("sessions")),
                (
                    f"{STAGE_LABELS[stage]} content hash",
                    descriptor.get("content_hash"),
                ),
            ]
        )
    execution = report.get("execution") or {}
    fingerprint_items.extend(
        [
            ("Initial cash", execution.get("initial_cash")),
            ("Base costs", execution.get("base_costs")),
            ("Stress costs", execution.get("stress_costs")),
            ("Signal window sessions", execution.get("signal_window_sessions")),
            ("Execution rules", execution.get("execution_rules")),
            ("Minimum samples", execution.get("minimum_samples")),
            ("Regime definition", execution.get("regimes")),
        ]
    )
    parts.append(_definition_list(fingerprint_items))
    parts.append("</section>")

    parts.append("<section><h2>Authorizations and query receipts</h2>")
    for stage in STAGES:
        parts.append(
            _authorization_section(
                stage,
                report["authorizations"][stage],
                report["query_receipts"][stage],
            )
        )
    parts.append("<h3>Rejected requests</h3>")
    rejected = report.get("rejected_requests") or []
    if rejected:
        parts.append(
            _table(
                [
                    "Received at",
                    "Command",
                    "Stage",
                    "Code",
                    "Message",
                    "Sealed source opened",
                    "Query debited",
                ],
                [
                    [
                        item.get("received_at"),
                        item.get("command"),
                        item.get("stage"),
                        item.get("code"),
                        item.get("message"),
                        item.get("opened_protected_source"),
                        item.get("query_debited"),
                    ]
                    for item in rejected
                ],
            )
        )
    else:
        parts.append(
            '<p class="muted">No rejected requests are recorded for this plan.</p>'
        )
    parts.append("</section>")

    for stage in STAGES:
        parts.append(
            f"<section>{_evidence_section(stage, report['stages'][stage])}</section>"
        )

    parts.append("<section><h2>Limitations</h2><ul>")
    parts.append(
        "<li>Synthetic data is engineering evidence only and never market performance.</li>"
    )
    parts.append(
        "<li>The report is rendered from immutable stored records; no evaluation was re-run.</li>"
    )
    parts.append(
        "<li>Holdout evidence is reviewed on the trusted side only; the research side "
        "receives a constant receipt.</li>"
    )
    parts.append("</ul></section>")
    parts.append("</main></body></html>")
    return "\n".join(parts) + "\n"


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as staged:
        staged.write(text)
        staged.flush()
        os.fsync(staged.fileno())
        staged_name = staged.name
    os.replace(staged_name, path)


def write_evaluation_report(
    store: str | Path, plan_id: str, output: str | Path
) -> dict[str, Any]:
    """Render and write the controlled report; return an operator summary."""
    output = Path(output)
    suffix = output.suffix.lower()
    if suffix == ".html":
        report_format = "html"
    elif suffix == ".json":
        report_format = "json"
    else:
        raise ValueError("Report output must end with .html or .json.")
    report = build_evaluation_report(store, plan_id)
    if report_format == "html":
        text = render_report_html(report)
    else:
        text = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    _write_text(output, text)
    return {
        "schema_version": report["schema_version"],
        "status": "generated",
        "scope": report["scope"],
        "plan_id": report["plan_id"],
        "output": str(output),
        "format": report_format,
        "report_hash": report["report_hash"],
        "evidence_complete": report["verdict"]["evidence_complete"],
        "all_stages_passed": report["verdict"]["all_stages_passed"],
        "incomplete": report["verdict"]["incomplete"],
        "admission": False,
    }
