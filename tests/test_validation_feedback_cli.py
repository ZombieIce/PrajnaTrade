"""Public-CLI tests for Validation feedback, revisions and the selection basis.

Ticket #54 (spec #50 T4): a protocol-v2 ``round status`` publishes the formal
receipt's summary (return, drawdown, costs, fills and verdict reason) to the
agent, while still hiding curves, ledger rows, prices, human reviewer/reason,
sealed payloads and protected internals.  The agent can ask a specific question
with ``round detail`` over the *published* receipt, record the resulting
``round feedback``, and link both into the V2 submission with
``revision_of``/``feedback_ids``.  A v2 ``round open`` discloses the intervals
and feedback a previous round already used on the same snapshot, and the
return -> V2 does not improve -> re-select V1 path keeps V1's stored receipt.

Every scenario drives the public CLI through subprocesses on synthetic sealed
snapshots.  Fixture builders are imported from the modules that own them.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

from cli_helpers import agent_cli, assert_hex, cli, ok, rejected, write_json
from test_discovery_split_cli import V1_PROTOCOL, V2_PROTOCOL, v2_round_input
from test_experiment_cli import campaign_options
from test_formal_validation_cli import (
    _split_for_payload,
    add_version,
    formal_scenario,
    legacy_scenario,
    round_uses,
    submit_locked_experiment,
    validate_current,
    validate_version_id,
)
from test_protected_eval_cli import submit_experiment
from test_research_loop_cli import (
    close_round,
    open_round,
    round_input,
    round_status,
    submit_version,
    suspend_derived,
    trending_snapshot,
    validate_version,
    version_input,
)
from test_validation_admission_cli import admission_input, admit
from test_version_review_cli import (
    approve_version,
    calendar_file,
    end_input,
    observation_input,
    return_input,
    review,
)

REVIEWER = "research-lead"
REVIEW_REASON = "The passed receipt and explicit observation confirmation admit this version."


# ---------------------------------------------------------------------------
# Command wrappers
# ---------------------------------------------------------------------------


def round_detail(
    project: Path,
    round_id: str,
    version_id: str,
    source: Path,
    *,
    agent: bool = True,
) -> subprocess.CompletedProcess[str]:
    runner = agent_cli if agent else cli
    return runner(
        "round",
        "detail",
        "--project",
        str(project),
        "--round-id",
        round_id,
        "--version-id",
        version_id,
        "--input",
        str(source),
    )


def round_feedback(
    project: Path,
    round_id: str,
    version_id: str,
    source: Path,
    *,
    agent: bool = True,
) -> subprocess.CompletedProcess[str]:
    runner = agent_cli if agent else cli
    return runner(
        "round",
        "feedback",
        "--project",
        str(project),
        "--round-id",
        round_id,
        "--version-id",
        version_id,
        "--input",
        str(source),
    )


def detail_input(question: str, records: list[str]) -> dict:
    return {"question": question, "records": records}


def feedback_input(
    impact: str,
    *,
    detail_access_id: str | None = None,
    next_experiment_id: str | None = None,
) -> dict:
    return {
        "impact": impact,
        "detail_access_id": detail_access_id,
        "next_experiment_id": next_experiment_id,
    }


def agent_status(scenario: dict) -> dict:
    return ok(
        round_status(scenario["project"], scenario["opened"]["round_id"], agent=True)
    )


def version_entry_for(scenario: dict, version_id: str) -> dict:
    return next(
        item
        for item in agent_status(scenario)["submissions"]
        if item["version_id"] == version_id
    )


# ---------------------------------------------------------------------------
# 1. The T4 agent grant: the formal summary in status, never the internals
# ---------------------------------------------------------------------------


def test_formal_summary_reaches_status_and_the_agent_view(tmp_path: Path) -> None:
    scenario = formal_scenario(tmp_path, name="summary", limit=2)
    validated = ok(validate_current(scenario))
    assert validated["outcome"] == "passed"
    assert validated["summary"]["verdict"] == {"outcome": "passed", "reason": None}

    status = agent_status(scenario)
    entry = version_entry_for(scenario, scenario["version"]["version_id"])
    summary = entry["validation"]
    # T4 publishes the receipt summary plus its frozen protocol context.
    assert summary["summary"] == validated["summary"]
    assert summary["development"] == validated["development"]
    assert summary["trailing"] == validated["trailing"]
    assert summary["provenance"] == validated["provenance"]
    assert summary["protocol"] == validated["protocol"]
    assert summary["scoring"] == validated["scoring"]
    assert summary["warmup"] == validated["warmup"]
    assert summary["usage"] == validated["usage"]
    candidate = summary["summary"]["candidate"]
    assert candidate["fills"] >= 1
    for field in (
        "total_return",
        "max_drawdown",
        "fees_paid",
        "slippage_paid",
        "turnover",
        "fills",
        "orders_filled",
        "orders_unfilled",
        "ending_equity",
    ):
        assert field in candidate, field
    assert summary["summary"]["verdict"]["reason"] is None

    # The deliberate grant: the numbers are agent-visible.  Everything that is
    # trusted-only, protected or human-attributed stays out.
    rendered = json.dumps(status)
    assert "fills" in rendered
    assert "total_return" in rendered
    for forbidden in (
        "equity_curve",
        "curves",
        "ledger",
        "artifact_path",
        "release_path",
        "query_debited",
        "opened_protected_source",
        "evaluations",
    ):
        assert forbidden not in rendered, forbidden
    # The pending review summary keeps its recorded null placeholders but never
    # a human identity or reason value.
    assert entry["review"]["reviewer"] is None
    assert entry["review"]["reason"] is None
    assert set(entry["review"]) == {
        "state", "decision", "decision_id", "reviewer", "reason", "decided_at", "summary",
    }
    assert status["admissions"] == []


def test_legacy_status_summary_keeps_its_v1_shape(tmp_path: Path) -> None:
    scenario = legacy_scenario(tmp_path, name="legacy-summary")
    ok(validate_current(scenario))
    entry = version_entry_for(scenario, scenario["version"]["version_id"])
    summary = entry["validation"]
    assert summary["status"] == "completed"
    for absent in ("summary", "development", "trailing", "provenance", "kind", "protocol"):
        assert absent not in summary, absent
    rendered = json.dumps(agent_status(scenario))
    for forbidden in ("equity_curve", "fills", "query_debited", "opened_protected_source"):
        assert forbidden not in rendered, forbidden


# ---------------------------------------------------------------------------
# 2. Controlled detail queries over the published receipt
# ---------------------------------------------------------------------------


def formal_detail_scenario(tmp_path: Path) -> dict:
    scenario = formal_scenario(tmp_path, name="detail", limit=2)
    scenario["validated"] = ok(validate_current(scenario))
    return scenario


def detail_rows(scenario: dict) -> list[dict]:
    project = scenario["project"] / ".prajnatrade" / "project.sqlite"
    with sqlite3.connect(project) as connection:
        return connection.execute(
            "SELECT detail_access_id, round_id, version_id, validation_id, "
            "request_json, resolution_json FROM research_loop_details ORDER BY rowid"
        ).fetchall()


def test_detail_query_records_the_question_and_returns_only_requested_blocks(
    tmp_path: Path,
) -> None:
    scenario = formal_detail_scenario(tmp_path)
    validated = scenario["validated"]
    version_id = scenario["version"]["version_id"]
    round_id = scenario["opened"]["round_id"]
    uses_before = round_uses(scenario)

    # Record names are normalized to a sorted, de-duplicated list.
    records = ["checks.position", "fills.candidate", "provenance", "runs.candidate", "summary"]
    source = write_json(
        tmp_path / "detail.json",
        detail_input("Which trailing sessions carried the drawdown?", records),
    )
    resolved = ok(round_detail(scenario["project"], round_id, version_id, source))
    assert resolved["status"] == "completed"
    assert resolved["reused"] is False
    assert_hex(resolved["detail_access_id"])
    assert resolved["round_id"] == round_id
    assert resolved["version_id"] == version_id
    assert resolved["validation_id"] == validated["validation_id"]
    assert resolved["receipt_hash"] == validated["receipt_hash"]
    assert resolved["question"] == "Which trailing sessions carried the drawdown?"
    assert resolved["records"] == records
    assert set(resolved["details"]) == set(records)
    assert resolved["details"]["summary"] == validated["summary"]
    assert resolved["details"]["checks.position"] == validated["checks"]["position"]
    assert resolved["details"]["provenance"] == validated["provenance"]
    assert resolved["details"]["runs.candidate"] == validated["runs"]["candidate"]
    assert resolved["details"]["fills.candidate"] == validated["runs"]["candidate"]["fills"]
    assert isinstance(resolved["recorded_at"], str) and resolved["recorded_at"]
    # The trusted-only per-session curves are never part of a detail resolution.
    rendered = json.dumps(resolved)
    assert "equity_curve" not in rendered
    assert '"curves"' not in rendered

    # The query is recorded once and never consumes a Validation use.
    assert round_uses(scenario) == uses_before
    allowance_before = agent_status(scenario)["validation_allowance"]
    rows = detail_rows(scenario)
    assert len(rows) == 1
    assert rows[0][0] == resolved["detail_access_id"]
    assert rows[0][1] == round_id
    assert rows[0][2] == version_id
    assert rows[0][3] == validated["validation_id"]
    request = json.loads(rows[0][4])
    assert request["question"] == resolved["question"]
    assert request["records"] == records
    assert request["receipt_hash"] == validated["receipt_hash"]
    resolution = json.loads(rows[0][5])
    assert resolution["details"] == resolved["details"]
    assert resolution["recorded_at"] == resolved["recorded_at"]

    entry = version_entry_for(scenario, version_id)
    assert entry["details"] == [
        {
            "detail_access_id": resolved["detail_access_id"],
            "question": resolved["question"],
            "records": records,
            "recorded_at": resolved["recorded_at"],
        }
    ]
    status_after = agent_status(scenario)
    assert status_after["validation_allowance"] == allowance_before
    assert len(round_uses(scenario)) == len(uses_before)

    # Identical content replays the stored resolution; an operator sees the
    # same answer (the detail command is agent + operator, project DB only).
    replay = ok(round_detail(scenario["project"], round_id, version_id, source))
    assert replay["reused"] is True
    assert replay["detail_access_id"] == resolved["detail_access_id"]
    assert replay["recorded_at"] == resolved["recorded_at"]
    assert replay["details"] == resolved["details"]
    assert len(detail_rows(scenario)) == 1
    operator = ok(
        round_detail(scenario["project"], round_id, version_id, source, agent=False)
    )
    assert operator["reused"] is True
    assert operator["detail_access_id"] == resolved["detail_access_id"]

    # The same records in another order are the same request; another question
    # is a new recorded access with its own ID.
    reordered = write_json(
        tmp_path / "detail-reordered.json",
        detail_input(resolved["question"], list(reversed(records))),
    )
    shuffled = ok(round_detail(scenario["project"], round_id, version_id, reordered))
    assert shuffled["reused"] is True
    assert shuffled["detail_access_id"] == resolved["detail_access_id"]
    other = ok(
        round_detail(
            scenario["project"],
            round_id,
            version_id,
            write_json(
                tmp_path / "detail-other.json",
                detail_input("Which fills carried the return?", ["summary"]),
            ),
        )
    )
    assert other["reused"] is False
    assert other["detail_access_id"] != resolved["detail_access_id"]
    assert len(detail_rows(scenario)) == 2
    listed = version_entry_for(scenario, version_id)["details"]
    assert [item["detail_access_id"] for item in listed] == [
        resolved["detail_access_id"],
        other["detail_access_id"],
    ]


def test_detail_query_rejects_unknown_unavailable_and_receiptless_versions(
    tmp_path: Path,
) -> None:
    scenario = formal_detail_scenario(tmp_path)
    round_id = scenario["opened"]["round_id"]
    version_id = scenario["version"]["version_id"]

    unknown = round_detail(
        scenario["project"],
        round_id,
        version_id,
        write_json(tmp_path / "unknown.json", detail_input("What?", ["cash"])),
    )
    rejected(unknown, "DETAIL_RECORD_UNKNOWN", phase="detail")
    assert "cash" in json.loads(unknown.stdout)["records"]

    unknown_check = round_detail(
        scenario["project"],
        round_id,
        version_id,
        write_json(tmp_path / "unknown-check.json", detail_input("What?", ["checks.alpha"])),
    )
    rejected(unknown_check, "DETAIL_RECORD_UNKNOWN", phase="detail")

    # `replay` is a legacy receipt record; a formal receipt has no replay check.
    for record in ("replay", "checks.replay"):
        result = round_detail(
            scenario["project"],
            round_id,
            version_id,
            write_json(tmp_path / f"unavailable-{record}.json", detail_input("What?", [record])),
        )
        rejected(result, "DETAIL_RECORD_UNAVAILABLE", phase="detail")
    assert detail_rows(scenario) == []

    # A version without a terminal receipt has no published record to project.
    second = add_version(scenario)
    no_receipt = round_detail(
        scenario["project"],
        round_id,
        second["version"]["version_id"],
        write_json(tmp_path / "no-receipt.json", detail_input("What?", ["summary"])),
    )
    blocked = rejected(no_receipt, "VALIDATION_NOT_PASSED", phase="detail")
    assert "summary" not in json.dumps(blocked.get("details", {}))
    assert detail_rows(scenario) == []
    # A v1 (legacy) receipt exposes `replay` and keeps its published fields.
    legacy = legacy_scenario(tmp_path, name="detail-legacy")
    legacy_validated = ok(validate_current(legacy))
    legacy_details = ok(
        round_detail(
            legacy["project"],
            legacy["opened"]["round_id"],
            legacy["version"]["version_id"],
            write_json(
                tmp_path / "legacy-detail.json",
                detail_input("Which replay interval was scored?", ["replay", "metrics.candidate"]),
            ),
        )
    )
    assert legacy_details["validation_id"] == legacy_validated["validation_id"]
    assert legacy_details["details"]["replay"] == legacy_validated["replay"]
    assert legacy_details["details"]["metrics.candidate"] == (
        legacy_validated["metrics"]["candidate"]
    )
    unavailable = round_detail(
        legacy["project"],
        legacy["opened"]["round_id"],
        legacy["version"]["version_id"],
        write_json(tmp_path / "legacy-unavailable.json", detail_input("Runs?", ["runs.candidate"])),
    )
    rejected(unavailable, "DETAIL_RECORD_UNAVAILABLE", phase="detail")


def test_detail_input_requires_a_question_and_at_least_one_record(tmp_path: Path) -> None:
    scenario = formal_detail_scenario(tmp_path)
    round_id = scenario["opened"]["round_id"]
    version_id = scenario["version"]["version_id"]
    for index, payload in enumerate(
        (
            {"question": "", "records": ["summary"]},
            {"question": "   ", "records": ["summary"]},
            {"question": "What?", "records": []},
            {"question": "What?", "records": "summary"},
            {"question": "What?"},
            {"records": ["summary"]},
        )
    ):
        result = round_detail(
            scenario["project"],
            round_id,
            version_id,
            write_json(tmp_path / f"invalid-{index}.json", payload),
        )
        assert result.returncode == 2, result.stdout
        assert result.stdout == ""
        assert "error:" in result.stderr
    assert detail_rows(scenario) == []


# ---------------------------------------------------------------------------
# 3. Feedback and the V2 revision link
# ---------------------------------------------------------------------------


def feedback_rows(scenario: dict) -> list[tuple]:
    project = scenario["project"] / ".prajnatrade" / "project.sqlite"
    with sqlite3.connect(project) as connection:
        return connection.execute(
            "SELECT feedback_id, round_id, version_id, feedback_json "
            "FROM research_loop_feedback ORDER BY rowid"
        ).fetchall()


def test_v1_detail_and_feedback_drive_a_linked_v2_revision(tmp_path: Path) -> None:
    scenario = formal_scenario(tmp_path, name="revision", limit=2)
    project = scenario["project"]
    round_id = scenario["opened"]["round_id"]
    v1_id = scenario["version"]["version_id"]
    first = ok(validate_current(scenario))

    detail = ok(
        round_detail(
            project,
            round_id,
            v1_id,
            write_json(
                tmp_path / "revision-detail.json",
                detail_input(
                    "Which drawdown was carried by the trailing segment?",
                    ["summary", "provenance"],
                ),
            ),
        )
    )
    second_experiment = submit_locked_experiment(
        scenario["root"],
        project,
        scenario["campaign"]["campaign_id"],
        submission_id="second-experiment",
        family="momentum",
        parameters={"lookback": 2, "top_k": 2, "cash_if_negative": False},
    )
    assert second_experiment["status"] == "succeeded"
    source = write_json(
        tmp_path / "revision-feedback.json",
        feedback_input(
            "The trailing drawdown says the slower lookback should be tried next.",
            detail_access_id=detail["detail_access_id"],
            next_experiment_id=second_experiment["experiment_id"],
        ),
    )
    recorded = ok(round_feedback(project, round_id, v1_id, source))
    assert recorded["status"] == "recorded"
    assert recorded["reused"] is False
    body = recorded["feedback"]
    assert_hex(body["feedback_id"])
    assert body["round_id"] == round_id
    assert body["version_id"] == v1_id
    assert body["validation_id"] == first["validation_id"]
    assert body["receipt_hash"] == first["receipt_hash"]
    assert body["impact"].startswith("The trailing drawdown")
    assert body["detail_access_id"] == detail["detail_access_id"]
    assert body["next_experiment_id"] == second_experiment["experiment_id"]
    assert isinstance(body["recorded_at"], str) and body["recorded_at"]
    # Recording feedback is not a Validation read.
    assert len(round_uses(scenario)) == 1
    assert len(feedback_rows(scenario)) == 1

    replay = ok(round_feedback(project, round_id, v1_id, source))
    assert replay["reused"] is True
    assert replay["feedback"] == body
    assert len(feedback_rows(scenario)) == 1
    assert len(round_uses(scenario)) == 1
    operator = ok(round_feedback(project, round_id, v1_id, source, agent=False))
    assert operator["reused"] is True
    assert operator["feedback"] == body

    entry = version_entry_for(scenario, v1_id)
    assert entry["feedback"] == [body]
    assert entry["details"][0]["detail_access_id"] == detail["detail_access_id"]

    # V2 links the prior version and the recorded feedback as provenance only.
    submitted = ok(
        submit_version(
            project,
            round_id,
            "v2",
            write_json(
                tmp_path / "revision-v2.json",
                version_input(
                    second_experiment["experiment_id"],
                    revision_of=v1_id,
                    feedback_ids=[body["feedback_id"]],
                ),
            ),
        )
    )
    assert submitted["version_id"] != v1_id
    assert submitted["revision_of"] == v1_id
    assert submitted["feedback_ids"] == [body["feedback_id"]]
    v2_entry = version_entry_for(scenario, submitted["version_id"])
    assert v2_entry["revision_of"] == v1_id
    assert v2_entry["feedback_ids"] == [body["feedback_id"]]

    # Linkage is provenance: the same experiment without it resolves to the
    # same content-addressed version (the version identity is unchanged).
    plain = ok(
        submit_version(
            project,
            round_id,
            "v2-plain",
            write_json(
                tmp_path / "revision-v2-plain.json",
                version_input(second_experiment["experiment_id"]),
            ),
        )
    )
    assert plain["reused"] is True
    assert plain["version_id"] == submitted["version_id"]
    assert plain["requested_submission_id"] == "v2-plain"

    # V2 is scored on the same trailing segment, benchmark, cash, costs and
    # hard-condition thresholds as V1; only the candidate changed.
    second = ok(validate_version_id(scenario, submitted["version_id"]))
    assert second["validation_kind"] == first["validation_kind"] == "trailing_validation"
    assert second["protocol"] == first["protocol"]
    assert second["development"] == first["development"]
    assert second["trailing"] == first["trailing"]
    assert second["scoring"] == first["scoring"]
    assert second["checks"]["segment"] == first["checks"]["segment"]
    assert [item["id"] for item in second["checks"]["hard_conditions"]] == [
        item["id"] for item in first["checks"]["hard_conditions"]
    ]
    assert second["runs"]["candidate"]["initial_cash"] == (
        first["runs"]["candidate"]["initial_cash"]
    )
    assert second["runs"]["benchmark"]["initial_cash"] == (
        first["runs"]["benchmark"]["initial_cash"]
    )
    assert second["usage"]["sequence"] == 2
    assert len(round_uses(scenario)) == 2


def test_feedback_and_revision_links_reject_self_and_foreign_references(
    tmp_path: Path,
) -> None:
    scenario = formal_scenario(tmp_path, name="links", limit=2)
    project = scenario["project"]
    round_id = scenario["opened"]["round_id"]
    v1_id = scenario["version"]["version_id"]
    first = ok(validate_current(scenario))
    detail = ok(
        round_detail(
            project,
            round_id,
            v1_id,
            write_json(tmp_path / "links-detail.json", detail_input("What?", ["summary"])),
        )
    )
    second = add_version(scenario, submission_id="v2")
    v2_id = second["version"]["version_id"]

    # A version with no terminal receipt has no evidence to give feedback on.
    rejected(
        round_feedback(
            project,
            round_id,
            v2_id,
            write_json(
                tmp_path / "no-receipt-feedback.json",
                feedback_input("No receipt yet."),
            ),
        ),
        "VALIDATION_NOT_PASSED",
        phase="feedback",
    )

    # A detail access from another version's receipt is not this version's.
    v2_receipt = ok(validate_version_id(scenario, v2_id))
    v2_detail = ok(
        round_detail(
            project,
            round_id,
            v2_id,
            write_json(tmp_path / "v2-detail.json", detail_input("What?", ["summary"])),
        )
    )
    assert v2_detail["validation_id"] == v2_receipt["validation_id"]
    rejected(
        round_feedback(
            project,
            round_id,
            v1_id,
            write_json(
                tmp_path / "foreign-detail.json",
                feedback_input(
                    "The V2 detail cannot explain V1.",
                    detail_access_id=v2_detail["detail_access_id"],
                ),
            ),
        ),
        "DETAIL_NOT_FOUND",
        phase="feedback",
    )

    # The next experiment must be another known experiment of the Campaign.
    rejected(
        round_feedback(
            project,
            round_id,
            v1_id,
            write_json(
                tmp_path / "self-experiment.json",
                feedback_input("Self link.", next_experiment_id=scenario["experiment"]["experiment_id"]),
            ),
        ),
        "FEEDBACK_LINK_INVALID",
        phase="feedback",
    )
    rejected(
        round_feedback(
            project,
            round_id,
            v1_id,
            write_json(
                tmp_path / "unknown-experiment.json",
                feedback_input("Unknown link.", next_experiment_id="exp-unknown"),
            ),
        ),
        "FEEDBACK_LINK_INVALID",
        phase="feedback",
    )

    recorded = ok(
        round_feedback(
            project,
            round_id,
            v1_id,
            write_json(
                tmp_path / "links-feedback.json",
                feedback_input(
                    "The trailing detail supports the next revision.",
                    detail_access_id=detail["detail_access_id"],
                ),
            ),
        )
    )
    assert recorded["feedback"]["validation_id"] == first["validation_id"]
    assert len(round_uses(scenario)) == 2

    # `revision_of` must be another version already locked in this round.
    rejected(
        submit_version(
            project,
            round_id,
            "missing-revision",
            write_json(
                tmp_path / "missing-revision.json",
                version_input(
                    second["experiment"]["experiment_id"], revision_of="a" * 64
                ),
            ),
        ),
        "REVISION_NOT_FOUND",
        phase="submission",
    )
    # A version cannot revise itself.
    rejected(
        submit_version(
            project,
            round_id,
            "self-revision",
            write_json(
                tmp_path / "self-revision.json",
                version_input(scenario["experiment"]["experiment_id"], revision_of=v1_id),
            ),
        ),
        "REVISION_CONFLICT",
        phase="submission",
    )
    # A submission cannot cite feedback recorded on itself.
    rejected(
        submit_version(
            project,
            round_id,
            "self-feedback",
            write_json(
                tmp_path / "self-feedback.json",
                version_input(
                    scenario["experiment"]["experiment_id"],
                    feedback_ids=[recorded["feedback"]["feedback_id"]],
                ),
            ),
        ),
        "FEEDBACK_LINK_INVALID",
        phase="submission",
    )
    rejected(
        submit_version(
            project,
            round_id,
            "unknown-feedback",
            write_json(
                tmp_path / "unknown-feedback.json",
                version_input(
                    second["experiment"]["experiment_id"], feedback_ids=["b" * 64]
                ),
            ),
        ),
        "FEEDBACK_LINK_INVALID",
        phase="submission",
    )


# ---------------------------------------------------------------------------
# 4. Cross-round reuse disclosure on the same snapshot
# ---------------------------------------------------------------------------


def add_campaign(
    project: Path, source: Path, *, split: bool = False, warmup: int = 3
) -> dict:
    """Create another Campaign in the same project from the same sealed source."""
    arguments = list(campaign_options(project, source, attempts=6, max_seconds=180))
    arguments += ["--discovery-warmup-sessions", str(warmup)]
    if split:
        arguments += ["--discovery-split", "70/30"]
    return ok(cli(*arguments))


def same_project_rounds(tmp_path: Path) -> dict:
    """A legacy round with a receipt + feedback, then a v2 round on the snapshot."""
    payload = trending_snapshot(15)
    project = tmp_path / "reuse"
    source = write_json(tmp_path / "reuse-snapshot.json", payload)
    ok(cli("project", "init", "--project", str(project)))
    first_campaign = add_campaign(project, source)
    snapshots = project.parent / f"{project.name}-snapshot-store"
    first_store = tmp_path / "reuse-trusted-first"
    first_opened = ok(
        open_round(
            project,
            first_store,
            first_campaign["campaign_id"],
            write_json(tmp_path / "first-round.json", round_input()),
        )
    )
    experiment = submit_experiment(
        project,
        first_campaign["campaign_id"],
        tmp_path / "first-experiment.json",
        "first",
        "The legacy replay establishes the first selection basis.",
        "momentum",
        {"lookback": 2, "cash_if_negative": False},
    )
    version = ok(
        submit_version(
            project,
            first_opened["round_id"],
            "v1",
            write_json(
                tmp_path / "first-version.json",
                version_input(experiment["experiment_id"]),
            ),
        )
    )
    validated = ok(
        validate_version(
            project,
            first_store,
            snapshots,
            first_opened["round_id"],
            version["version_id"],
        )
    )
    detail = ok(
        round_detail(
            project,
            first_opened["round_id"],
            version["version_id"],
            write_json(
                tmp_path / "first-detail.json",
                detail_input("Which interval did the replay score?", ["replay"]),
            ),
        )
    )
    impact = "The legacy interval's drawdown motivates a revision on the same snapshot."
    recorded = ok(
        round_feedback(
            project,
            first_opened["round_id"],
            version["version_id"],
            write_json(
                tmp_path / "first-feedback.json",
                feedback_input(impact, detail_access_id=detail["detail_access_id"]),
            ),
        )
    )
    second_campaign = add_campaign(project, source, split=True)
    second_store = tmp_path / "reuse-trusted-second"
    second_round_source = write_json(
        tmp_path / "second-round.json", v2_round_input(limit=2)
    )
    second_opened = ok(
        open_round(
            project,
            second_store,
            second_campaign["campaign_id"],
            second_round_source,
        )
    )
    return {
        "project": project,
        "source": source,
        "snapshots": snapshots,
        "first_campaign": first_campaign,
        "first_store": first_store,
        "first_opened": first_opened,
        "experiment": experiment,
        "version": version,
        "validated": validated,
        "detail": detail,
        "feedback": recorded["feedback"],
        "impact": impact,
        "second_campaign": second_campaign,
        "second_store": second_store,
        "second_round_source": second_round_source,
        "second_opened": second_opened,
    }


def add_split_round(
    tmp_path: Path, project: Path, source: Path, *, name: str, limit: int = 2
) -> tuple[dict, Path, dict]:
    """Freeze a split Campaign in an existing project and open its v2 round."""
    campaign = add_campaign(project, source, split=True)
    store = tmp_path / f"{name}-trusted"
    opened = ok(
        open_round(
            project,
            store,
            campaign["campaign_id"],
            write_json(tmp_path / f"{name}-round.json", v2_round_input(limit=limit)),
        )
    )
    return campaign, store, opened


def lock_and_read(
    tmp_path: Path,
    project: Path,
    campaign: dict,
    store: Path,
    round_id: str,
    *,
    submission_id: str,
) -> tuple[dict, dict, dict]:
    """Lock one momentum version in a v2 round and spend its first automatic read."""
    experiment = submit_experiment(
        project,
        campaign["campaign_id"],
        tmp_path / f"{submission_id}-experiment.json",
        submission_id,
        "The frozen development segment determines the Discovery result.",
        "momentum",
        {"lookback": 2, "cash_if_negative": False},
    )
    version = ok(
        submit_version(
            project,
            round_id,
            submission_id,
            write_json(
                tmp_path / f"{submission_id}-version.json",
                version_input(experiment["experiment_id"]),
            ),
        )
    )
    snapshots = project.parent / f"{project.name}-snapshot-store"
    validated = ok(
        validate_version(project, store, snapshots, round_id, version["version_id"])
    )
    return experiment, version, validated


def trailing_only_successor_payload() -> dict:
    """The trending fixture with corrected trailing Discovery rows only.

    Discovery is the first 9 of the 15 sessions and the frozen split gives
    development 6 / trailing 3.  Changing only the trailing derived values keeps
    the development content hash (and every Campaign copy) identical while the
    sealed snapshot identity changes.
    """
    payload = trending_snapshot(15)
    trailing = _split_for_payload(payload)["trailing"]
    trailing_dates = {
        row["date"]
        for row in payload["calendar"]
        if trailing["start"] <= row["date"] <= trailing["end"]
    }
    for row in payload["derived"]:
        if row["date"] in trailing_dates:
            row["value"] = round(row["value"] * 1.01, 6)
    return payload


def test_v2_open_discloses_a_corrected_successor_on_the_same_development_slice(
    tmp_path: Path,
) -> None:
    base = trending_snapshot(15)
    successor = trailing_only_successor_payload()
    first_split = _split_for_payload(base)
    second_split = _split_for_payload(successor)
    assert second_split["development"] == first_split["development"]
    assert second_split["trailing"] != first_split["trailing"]

    project = tmp_path / "successor-reuse"
    ok(cli("project", "init", "--project", str(project)))
    first_campaign, first_store, first_opened = add_split_round(
        tmp_path, project, write_json(tmp_path / "base-snapshot.json", base),
        name="base-round",
    )
    _, version, validated = lock_and_read(
        tmp_path,
        project,
        first_campaign,
        first_store,
        first_opened["round_id"],
        submission_id="base-version",
    )
    assert validated["usage"]["kind"] == "automatic"

    second_campaign, _, second_opened = add_split_round(
        tmp_path,
        project,
        write_json(tmp_path / "successor-snapshot.json", successor),
        name="successor-round",
    )
    # The successor is a different sealed snapshot with the same development
    # prehistory, so the disclosure matches it without a duplicate entry.
    assert second_campaign["snapshot_id"] != first_campaign["snapshot_id"]
    disclosure = second_opened["reuse_disclosure"]
    assert disclosure["snapshot_id"] == second_campaign["snapshot_id"]
    assert disclosure["previous_rounds"] == [
        {
            "round_id": first_opened["round_id"],
            "campaign_id": first_campaign["campaign_id"],
            "protocol": "trailing-70-30",
            "match": "same_development",
            "used": {
                "start": first_split["trailing"]["start"],
                "end": first_split["trailing"]["end"],
            },
            "overlap": {
                "start": first_split["trailing"]["start"],
                "end": first_split["trailing"]["end"],
            },
            # The prior round's counted read history is carried across.
            "automatic_used": 1,
            "manual_used": 0,
            "total_used": 1,
            "uses": [
                {
                    "version_id": version["version_id"],
                    "validation_id": validated["validation_id"],
                    "kind": "automatic",
                    "admission_id": None,
                    "sequence": 1,
                }
            ],
        }
    ]
    assert disclosure["feedback"] == []
    assert disclosure["overlapping"] is True
    status_body = ok(round_status(project, second_opened["round_id"], agent=True))
    assert status_body["reuse_disclosure"] == disclosure


def test_v2_open_discloses_the_prior_rounds_used_interval_and_feedback(
    tmp_path: Path,
) -> None:
    fixture = same_project_rounds(tmp_path)
    snapshot_id = fixture["first_campaign"]["snapshot_id"]
    assert fixture["second_campaign"]["snapshot_id"] == snapshot_id
    trailing = fixture["second_campaign"]["discovery_split"]["trailing"]
    # The legacy receipt records the replay's actual scoring interval (after
    # the Campaign warmup), not the whole Discovery partition.
    replay_candidate = fixture["validated"]["replay"]["candidate"]
    used = {
        "start": replay_candidate["start_date"],
        "end": replay_candidate["end_date"],
    }
    assert used["start"] < trailing["start"]

    disclosure = fixture["second_opened"]["reuse_disclosure"]
    assert disclosure["snapshot_id"] == snapshot_id
    assert disclosure["previous_rounds"] == [
        {
            "round_id": fixture["first_opened"]["round_id"],
            "campaign_id": fixture["first_campaign"]["campaign_id"],
            "protocol": "discovery-replay",
            "match": "same_snapshot",
            "used": used,
            "overlap": {"start": trailing["start"], "end": trailing["end"]},
            # A v1 round counts no Validation use, so its history is empty.
            "automatic_used": 0,
            "manual_used": 0,
            "total_used": 0,
            "uses": [],
        }
    ]
    assert disclosure["feedback"] == [
        {
            "feedback_id": fixture["feedback"]["feedback_id"],
            "round_id": fixture["first_opened"]["round_id"],
            "version_id": fixture["version"]["version_id"],
            "impact": fixture["impact"],
        }
    ]
    assert disclosure["overlapping"] is True

    second_status = ok(
        round_status(fixture["project"], fixture["second_opened"]["round_id"], agent=True)
    )
    assert second_status["reuse_disclosure"] == disclosure
    # The disclosure is written once: replaying the open returns the stored value.
    replay = ok(
        open_round(
            fixture["project"],
            fixture["second_store"],
            fixture["second_campaign"]["campaign_id"],
            fixture["second_round_source"],
        )
    )
    assert replay["reused"] is True
    assert replay["reuse_disclosure"] == disclosure

    # The old receipt keeps reading under its original protocol, with no
    # fabricated disclosure on the legacy round.
    first_status = ok(
        round_status(fixture["project"], fixture["first_opened"]["round_id"], agent=True)
    )
    assert first_status["protocol"] == V1_PROTOCOL
    assert "reuse_disclosure" not in first_status
    first_entry = next(
        item
        for item in first_status["submissions"]
        if item["version_id"] == fixture["version"]["version_id"]
    )
    assert "kind" not in first_entry["validation"]
    assert "protocol" not in first_entry["validation"]
    old_detail = ok(
        round_detail(
            fixture["project"],
            fixture["first_opened"]["round_id"],
            fixture["version"]["version_id"],
            write_json(
                tmp_path / "old-receipt-detail.json",
                detail_input("Show the original protocol's replay.", ["replay"]),
            ),
        )
    )
    assert old_detail["details"]["replay"] == fixture["validated"]["replay"]

    # Feedback recorded in one round cannot be cited by another round.
    second_experiment = submit_locked_experiment(
        tmp_path,
        fixture["project"],
        fixture["second_campaign"]["campaign_id"],
        submission_id="second-campaign-experiment",
        family="momentum",
        parameters={"lookback": 2, "cash_if_negative": False},
    )
    rejected(
        submit_version(
            fixture["project"],
            fixture["second_opened"]["round_id"],
            "cross-round",
            write_json(
                tmp_path / "cross-round.json",
                version_input(
                    second_experiment["experiment_id"],
                    feedback_ids=[fixture["feedback"]["feedback_id"]],
                ),
            ),
        ),
        "FEEDBACK_LINK_INVALID",
        phase="submission",
    )


def test_open_reuses_the_stored_disclosure_across_the_trusted_write_crash_window(
    tmp_path: Path,
) -> None:
    fixture = same_project_rounds(tmp_path)
    project = fixture["project"]
    second_round = fixture["second_opened"]["round_id"]
    original = fixture["second_opened"]["reuse_disclosure"]

    # Simulate the crash window: the trusted round exists but its project row
    # was not published.
    database = project / ".prajnatrade" / "project.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "DELETE FROM research_loop_rounds WHERE round_id = ?", (second_round,)
        )

    # A prior round opened meanwhile would change a re-derived disclosure.
    third_campaign = add_campaign(project, fixture["source"], split=True)
    third_opened = ok(
        open_round(
            project,
            tmp_path / "reuse-trusted-third",
            third_campaign["campaign_id"],
            write_json(tmp_path / "third-round.json", v2_round_input(limit=2)),
        )
    )
    third_prior = [
        item["round_id"] for item in third_opened["reuse_disclosure"]["previous_rounds"]
    ]
    assert third_prior == [fixture["first_opened"]["round_id"]]

    retried = ok(
        open_round(
            project,
            fixture["second_store"],
            fixture["second_campaign"]["campaign_id"],
            fixture["second_round_source"],
        )
    )
    # The stored disclosure is reused, not recomputed to include the new round.
    assert retried["reuse_disclosure"] == original
    assert second_round not in [
        item["round_id"] for item in retried["reuse_disclosure"]["previous_rounds"]
    ]
    status = ok(round_status(project, second_round, agent=True))
    assert status["reuse_disclosure"] == original


# ---------------------------------------------------------------------------
# 5. #35-synthetic traceability and the final selection basis
# ---------------------------------------------------------------------------


def test_synthetic_traceability_chain_links_every_selection_basis(tmp_path: Path) -> None:
    scenario = formal_scenario(tmp_path, name="trace", limit=1)
    project = scenario["project"]
    round_id = scenario["opened"]["round_id"]
    v1_id = scenario["version"]["version_id"]
    first = ok(validate_version_id(scenario, v1_id))
    calendar = calendar_file(tmp_path, name="trace-calendar.json")[0]
    approved = ok(
        approve_version(
            project,
            scenario["store"],
            round_id,
            v1_id,
            calendar,
            write_json(tmp_path / "trace-observation.json", observation_input()),
        )
    )
    second = add_version(scenario, submission_id="v2-trace")
    impact = "The trailing drawdown justifies one manual read of V2."
    recorded = ok(
        round_feedback(
            project,
            round_id,
            v1_id,
            write_json(
                tmp_path / "trace-feedback.json",
                feedback_input(
                    impact, next_experiment_id=second["experiment"]["experiment_id"]
                ),
            ),
        )
    )
    admitted = ok(
        admit(
            project,
            scenario["store"],
            round_id,
            second["version"]["version_id"],
            write_json(tmp_path / "trace-admission.json", admission_input()),
        )
    )
    v2 = ok(validate_version_id(scenario, second["version"]["version_id"]))
    assert v2["usage"]["kind"] == "manual"
    assert v2["usage"]["admission_id"] == admitted["admission"]["admission_id"]

    status = agent_status(scenario)
    assert status["round_id"] == round_id
    assert status["campaign_id"] == scenario["campaign"]["campaign_id"]
    assert status["protocol"] == V2_PROTOCOL
    partition = status["research_partition"]
    assert partition["development"]["start"] == scenario["split"]["development"]["start"]
    assert partition["development"]["end"] == scenario["split"]["development"]["end"]
    assert partition["trailing"]["start"] == scenario["split"]["trailing"]["start"]
    assert partition["trailing"]["end"] == scenario["split"]["trailing"]["end"]

    entry_v1 = version_entry_for(scenario, v1_id)
    assert entry_v1["submission_id"] == scenario["version"]["submission_id"]
    assert entry_v1["experiment_id"] == scenario["experiment"]["experiment_id"]
    assert entry_v1["protocol"] == V2_PROTOCOL
    assert entry_v1["research_partition"] == scenario["split"]
    summary = entry_v1["validation"]
    assert summary["kind"] == "trailing_validation"
    assert summary["provenance"]["snapshot_id"] == scenario["campaign"]["snapshot_id"]
    assert summary["development"] == scenario["split"]["development"]
    assert summary["trailing"] == scenario["split"]["trailing"]
    assert summary["scoring"]["start"] == scenario["split"]["trailing"]["start"]
    assert summary["scoring"]["end"] == scenario["split"]["trailing"]["end"]
    assert summary["warmup"]["source"] == "development"
    assert summary["summary"]["verdict"] == {"outcome": "passed", "reason": None}
    assert summary["usage"] == first["usage"]
    assert entry_v1["review_state"] == "approved"
    assert entry_v1["feedback"] == [recorded["feedback"]]
    assert entry_v1["tracking"]["release_id"] == approved["tracking"]["release_id"]
    assert entry_v1["tracking"]["enabled_signal_date"] == (
        approved["tracking"]["enabled_signal_date"]
    )
    assert status["tracking"]["status"] == "tracking"
    assert status["tracking"]["release_ids"] == [approved["tracking"]["release_id"]]
    # The ordinary simulated-tracking artifact still enables on the calendar.
    release = ok(
        cli(
            "release", "status", "--store", str(scenario["store"]),
            "--release-id", approved["tracking"]["release_id"],
        )
    )
    assert release["state"] == "enabled"

    entry_v2 = version_entry_for(scenario, second["version"]["version_id"])
    assert entry_v2["validation"]["usage"]["kind"] == "manual"
    assert entry_v2["validation"]["usage"]["admission_id"] == (
        admitted["admission"]["admission_id"]
    )
    manual_use = next(
        use for use in status["validation_allowance"]["uses"] if use["kind"] == "manual"
    )
    assert manual_use["version_id"] == second["version"]["version_id"]
    assert manual_use["validation_id"] == v2["validation_id"]
    listed_admission = next(
        item
        for item in status["admissions"]
        if item["admission_id"] == admitted["admission"]["admission_id"]
    )
    assert listed_admission["used"] is True
    assert listed_admission["consumed_validation_id"] == v2["validation_id"]
    assert "reviewer" not in listed_admission
    assert "reason" not in listed_admission

    # One provenance query closes the chain without a sealed read: snapshot,
    # round, version, experiment, both slice dates, warmup source and usage.
    provenance = ok(
        round_detail(
            project,
            round_id,
            v1_id,
            write_json(
                tmp_path / "trace-provenance.json",
                detail_input(
                    "Show the full selection basis of V1.", ["provenance", "summary", "usage"]
                ),
            ),
        )
    )
    assert provenance["details"]["provenance"] == summary["provenance"]
    assert provenance["details"]["provenance"]["snapshot_id"] == (
        scenario["campaign"]["snapshot_id"]
    )
    assert provenance["details"]["provenance"]["round_id"] == round_id
    assert provenance["details"]["provenance"]["version_id"] == v1_id
    assert provenance["details"]["provenance"]["experiment_id"] == (
        scenario["experiment"]["experiment_id"]
    )
    assert provenance["details"]["provenance"]["development_content_hash"] == (
        scenario["split"]["development"]["content_hash"]
    )
    assert provenance["details"]["provenance"]["trailing_content_hash"] == (
        scenario["split"]["trailing"]["content_hash"]
    )
    assert provenance["details"]["summary"] == summary["summary"]
    assert provenance["details"]["usage"] == first["usage"]
    assert "equity_curve" not in json.dumps(provenance)


# ---------------------------------------------------------------------------
# 6. V2 does not improve: re-select V1 on its stored receipt
# ---------------------------------------------------------------------------


def test_returned_v1_is_reselected_on_its_reused_formal_receipt(tmp_path: Path) -> None:
    scenario = formal_scenario(tmp_path, name="reselect", limit=2)
    project = scenario["project"]
    round_id = scenario["opened"]["round_id"]
    v1_id = scenario["version"]["version_id"]
    first = ok(validate_current(scenario))
    returned = ok(
        review(
            project,
            scenario["store"],
            round_id,
            v1_id,
            "return",
            reason="Ask for a slower lookback under the same trailing protocol.",
            source=write_json(tmp_path / "reselect-return.json", return_input()),
        )
    )
    assert returned["status"] == "returned"

    second = add_version(scenario, submission_id="v2")
    v2 = ok(validate_version_id(scenario, second["version"]["version_id"]))
    assert v2["outcome"] in {"passed", "failed", "insufficient_evidence"}
    # V2 is scored on the same protocol and segment as V1.
    assert v2["protocol"] == first["protocol"]
    assert v2["trailing"] == first["trailing"]
    assert v2["scoring"] == first["scoring"]
    # The V1 evidence is still the current receipt: approval reuses it instead
    # of re-running Validation or spending a new use.
    calendar = calendar_file(tmp_path, name="reselect-calendar.json")[0]
    reselected = ok(
        approve_version(
            project,
            scenario["store"],
            round_id,
            v1_id,
            calendar,
            write_json(tmp_path / "reselect-observation.json", observation_input()),
            reason="V2 did not improve; the stored V1 receipt still applies.",
        )
    )
    assert reselected["status"] == "approved"
    assert reselected["validation"]["validation_id"] == first["validation_id"]
    assert reselected["validation"]["receipt_hash"] == first["receipt_hash"]
    assert reselected["validation"]["reused"] is True
    assert len(round_uses(scenario)) == 2
    entry = version_entry_for(scenario, v1_id)
    assert entry["review_state"] == "approved"
    assert entry["validation"]["validation_id"] == first["validation_id"]
    assert entry["tracking"]["release_id"] == reselected["tracking"]["release_id"]

    # An ended V2 is terminal and the round still selects V1.
    ended = ok(
        review(
            project,
            scenario["store"],
            round_id,
            second["version"]["version_id"],
            "end",
            reason="V2 does not improve the trailing selection.",
            source=write_json(tmp_path / "reselect-end.json", end_input()),
        )
    )
    assert ended["status"] == "ended"
    assert version_entry_for(scenario, second["version"]["version_id"])[
        "review_state"
    ] == "ended"

    closed = ok(
        close_round(
            project,
            round_id,
            write_json(
                tmp_path / "reselect-close.json",
                {
                    "stop_reason": "V2 does not improve; V1 remains selected.",
                    "selected_version": v1_id,
                    "next_experiment": None,
                },
            ),
        )
    )
    assert closed["status"] == "closed"
    assert closed["selected_version"] == v1_id
    assert closed["selected_validation"]["validation_id"] == first["validation_id"]
    assert closed["selected_validation"]["receipt_hash"] == first["receipt_hash"]
    assert closed["selected_validation"]["validation_kind"] == "trailing_validation"


def test_failed_and_insufficient_formal_receipts_cannot_be_approved(
    tmp_path: Path,
) -> None:
    failed_scenario = formal_scenario(
        tmp_path,
        name="failed-approval",
        hard_conditions=[
            {"id": "impossible", "metric": "net_return", "operator": ">=", "value": 10}
        ],
    )
    failed = ok(validate_current(failed_scenario))
    assert failed["outcome"] == "failed"
    failed_calendar = calendar_file(tmp_path, name="failed-calendar.json")[0]
    rejected(
        approve_version(
            failed_scenario["project"],
            failed_scenario["store"],
            failed_scenario["opened"]["round_id"],
            failed_scenario["version"]["version_id"],
            failed_calendar,
            write_json(tmp_path / "failed-observation.json", observation_input()),
        ),
        "VALIDATION_NOT_PASSED",
        phase="review",
    )

    payload = trending_snapshot(15)
    preview = _split_for_payload(payload)
    index = {row["date"]: position for position, row in enumerate(payload["calendar"])}
    dev_end = index[preview["development"]["end"]]
    for day_index in (dev_end - 1, dev_end):
        suspend_derived(payload, day_index)
    insufficient_scenario = formal_scenario(
        tmp_path,
        payload=payload,
        name="insufficient-approval",
        requirements=[
            {"id": "strategy_lookback", "observations": 3, "mode": "consecutive_sessions"}
        ],
    )
    insufficient = ok(validate_current(insufficient_scenario))
    assert insufficient["outcome"] == "insufficient_evidence"
    assert insufficient["reason"] == "SCORING_START_INSUFFICIENT"
    insufficient_calendar = calendar_file(tmp_path, name="insufficient-calendar.json")[0]
    rejected(
        approve_version(
            insufficient_scenario["project"],
            insufficient_scenario["store"],
            insufficient_scenario["opened"]["round_id"],
            insufficient_scenario["version"]["version_id"],
            insufficient_calendar,
            write_json(tmp_path / "insufficient-observation.json", observation_input()),
        ),
        "VALIDATION_NOT_PASSED",
        phase="review",
    )


# ---------------------------------------------------------------------------
# 7. The agent surface: detail/feedback granted, admission still operator-only
# ---------------------------------------------------------------------------


def test_agent_surface_grants_detail_and_feedback_but_not_admission(
    tmp_path: Path,
) -> None:
    scenario = formal_scenario(tmp_path, name="agent-surface", limit=2)
    project = scenario["project"]
    round_id = scenario["opened"]["round_id"]
    v1_id = scenario["version"]["version_id"]
    ok(validate_version_id(scenario, v1_id))
    source = write_json(tmp_path / "agent-detail.json", detail_input("What?", ["summary"]))
    assert ok(round_detail(project, round_id, v1_id, source))["status"] == "completed"

    # The agent cannot pass an operator-only trusted store to either command.
    for action, args in (
        (
            "detail",
            ("--version-id", v1_id, "--input", str(source)),
        ),
        (
            "feedback",
            (
                "--version-id",
                v1_id,
                "--input",
                str(
                    write_json(
                        tmp_path / "agent-feedback.json",
                        feedback_input("What the receipt changed."),
                    )
                ),
            ),
        ),
    ):
        result = agent_cli(
            "round",
            action,
            "--project",
            str(project),
            "--store",
            str(scenario["store"]),
            "--round-id",
            round_id,
            *args,
        )
        assert result.returncode == 2
        assert "unrecognized arguments" in result.stderr

    # Admission remains an operator decision.
    result = agent_cli(
        "round",
        "admit",
        "--project",
        str(project),
        "--store",
        str(scenario["store"]),
        "--round-id",
        round_id,
        "--version-id",
        v1_id,
        "--reviewer",
        REVIEWER,
        "--reason",
        REVIEW_REASON,
        "--input",
        str(write_json(tmp_path / "agent-admit.json", admission_input())),
    )
    assert result.returncode == 2
    assert "invalid choice" in result.stderr

    # A recorded review decision never exposes the human reviewer or reason.
    returned = ok(
        review(
            project,
            scenario["store"],
            round_id,
            v1_id,
            "return",
            reason="Return for a recorded reason.",
            source=write_json(tmp_path / "agent-return.json", return_input()),
        )
    )
    assert returned["status"] == "returned"
    entry = version_entry_for(scenario, v1_id)
    assert entry["review"]["decision"] == "return"
    assert "reviewer" not in entry["review"]
    assert "reason" not in entry["review"]
    rendered = json.dumps(agent_status(scenario))
    for forbidden in ("equity_curve", "curves", "ledger", "sealed"):
        assert forbidden not in rendered, forbidden
