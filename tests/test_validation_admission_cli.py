"""Public-CLI tests for manual Validation admission and the round lifecycle.

Ticket #53 (spec #50 T3): once a protocol-v2 round has used every automatic
trailing Validation slot, the next automatic read pauses before the sealed read.
The researcher can select an existing passing version, close with no candidate,
or record why the next comparison changes the choice and admit one version for
one manual read.  Discovery keeps working while Validation waits.

Every scenario drives the public CLI through subprocesses on synthetic sealed
snapshots.  Fixture builders are imported from the modules that own them.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

from cli_helpers import (
    agent_cli,
    assert_hex,
    cli,
    cli_env,
    digest,
    ok,
    rejected,
    round_validate_argv,
    write_json,
)
from test_formal_validation_cli import (
    VALIDATION_FAILPOINT,
    add_version,
    formal_scenario,
    legacy_scenario,
    round_uses,
    submit_locked_experiment,
    validate_version_id,
)
from test_research_loop_cli import (
    close_round,
    round_status,
    submit_version,
    version_input,
)
from test_version_review_cli import (
    approve_version,
    calendar_file,
    end_input,
    observation_input,
    review,
)

V2_PROTOCOL = {
    "schema_version": 1,
    "name": "trailing-70-30",
    "validation_kind": "trailing_validation",
}
REVIEWER = "research-lead"
ADMISSION_REASON = "The next trailing comparison is worth one manual read."
COMPARISON = "Compare V2 with the passed V1 on the same trailing segment and costs."
SELECTION_IMPACT = (
    "If V2 improves the trailing return without a deeper drawdown, it replaces V1; "
    "otherwise V1 stays the selected version."
)


# ---------------------------------------------------------------------------
# Scenario builders and assertions
# ---------------------------------------------------------------------------


def admission_input(**overrides: object) -> dict:
    payload: dict[str, object] = {
        "comparison": COMPARISON,
        "selection_impact": SELECTION_IMPACT,
    }
    payload.update(overrides)
    return payload


def admit(
    project: Path,
    store: Path,
    round_id: str,
    version_id: str,
    source: Path,
    *,
    reviewer: str = REVIEWER,
    reason: str = ADMISSION_REASON,
) -> subprocess.CompletedProcess[str]:
    return cli(
        "round",
        "admit",
        "--project",
        str(project),
        "--store",
        str(store),
        "--round-id",
        round_id,
        "--version-id",
        version_id,
        "--reviewer",
        reviewer,
        "--reason",
        reason,
        "--input",
        str(source),
    )


def admission_identity(admission: dict) -> str:
    """Independent content address of the immutable admission body."""
    return digest(
        {
            "schema_version": admission.get("schema_version", 1),
            **{
                field: admission[field]
                for field in (
                    "round_id",
                    "version_id",
                    "reviewer",
                    "reason",
                    "comparison",
                    "selection_impact",
                )
            },
        }
    )


def status(scenario: dict) -> dict:
    return ok(
        round_status(scenario["project"], scenario["opened"]["round_id"], agent=True)
    )


ALLOWANCE_FIELDS = (
    "auto_limit", "automatic_used", "manual_used", "total_used", "remaining",
)


def allowance(scenario: dict) -> dict:
    return status(scenario)["validation_allowance"]


def counters(scenario: dict) -> dict:
    """The allowance summary without the use rows (the validate output shape)."""
    body = allowance(scenario)
    return {field: body[field] for field in ALLOWANCE_FIELDS}


def admission_entry(scenario: dict, version_id: str) -> dict:
    status_body = status(scenario)
    return next(
        item for item in status_body["admissions"] if item["version_id"] == version_id
    )


def version_entry_for(scenario: dict, version_id: str) -> dict:
    return next(
        item
        for item in status(scenario)["submissions"]
        if item["version_id"] == version_id
    )


def read_admission(scenario: dict, admission_id: str) -> dict:
    return json.loads(
        (
            scenario["store"]
            / "research-loop-admissions"
            / f"{admission_id}.json"
        ).read_text(encoding="utf-8")
    )


def excluded_scenario(tmp_path: Path, *, limit: int = 1) -> dict:
    """A v2 round whose single automatic slot is used, plus a pending V2."""
    scenario = formal_scenario(tmp_path, limit=limit, name="excluded")
    scenario["first"] = ok(validate_version_id(scenario, scenario["version"]["version_id"]))
    scenario["second"] = add_version(scenario)
    return scenario


def close_input(*, selected: str | None = None) -> dict:
    return {
        "stop_reason": "No remaining testable version could change the choice.",
        "selected_version": selected,
        "next_experiment": None,
    }


# ---------------------------------------------------------------------------
# 1. The automatic limit pauses before the read
# ---------------------------------------------------------------------------


def test_automatic_limit_pauses_before_the_read(tmp_path: Path) -> None:
    scenario = formal_scenario(tmp_path, limit=1)
    first = ok(validate_version_id(scenario, scenario["version"]["version_id"]))
    assert first["usage"]["kind"] == "automatic"
    second = add_version(scenario)

    blocked = rejected(
        validate_version_id(scenario, second["version"]["version_id"]),
        "VALIDATION_ALLOWANCE_EXHAUSTED",
        phase="validation",
    )
    assert blocked["round_state"] == "awaiting_validation_admission"
    assert blocked["allowance"] == {
        "auto_limit": 1,
        "automatic_used": 1,
        "manual_used": 0,
        "total_used": 1,
        "remaining": 0,
    }
    # The refused version got no receipt, no running marker and no use row.
    markers = list((scenario["store"] / "research-loop-validations").glob("*.json"))
    assert len(markers) == 1
    assert len(round_uses(scenario)) == 1
    entry = next(
        item
        for item in status(scenario)["submissions"]
        if item["version_id"] == second["version"]["version_id"]
    )
    assert entry["validation"]["status"] == "not_run"

    round_body = status(scenario)
    assert round_body["lifecycle"] == "awaiting_validation_admission"
    counters = round_body["validation_allowance"]
    assert counters["auto_limit"] == 1
    assert counters["automatic_used"] == 1
    assert counters["manual_used"] == 0
    assert counters["total_used"] == 1
    assert counters["remaining"] == 0
    assert round_body["validation_policy"] == {"auto_validation_limit": 1}
    assert round_body["admissions"] == []


# ---------------------------------------------------------------------------
# 2. Discovery continues while Validation waits
# ---------------------------------------------------------------------------


def test_discovery_continues_after_the_automatic_limit(tmp_path: Path) -> None:
    scenario = formal_scenario(tmp_path, limit=1)
    ok(validate_version_id(scenario, scenario["version"]["version_id"]))
    assert status(scenario)["lifecycle"] == "awaiting_validation_admission"

    # A new experiment submit and a new locked version both still succeed.
    second = add_version(scenario)
    assert second["version"]["status"] == "submitted"
    another = submit_locked_experiment(
        scenario["root"],
        scenario["project"],
        scenario["campaign"]["campaign_id"],
        submission_id="third",
        family="momentum",
        parameters={"lookback": 2, "cash_if_negative": True},
    )
    assert another["status"] == "succeeded"
    assert another["budget"]["attempts_remaining"] >= 1

    round_body = status(scenario)
    assert {item["version_id"] for item in round_body["submissions"]} == {
        scenario["version"]["version_id"],
        second["version"]["version_id"],
    }
    # Discovery work is not a Validation read.
    assert len(round_uses(scenario)) == 1
    assert round_body["lifecycle"] == "awaiting_validation_admission"


# ---------------------------------------------------------------------------
# 3. round admit records one write-once admission
# ---------------------------------------------------------------------------


def test_round_admit_records_and_replays_one_admission(tmp_path: Path) -> None:
    scenario = formal_scenario(tmp_path, limit=1)
    ok(validate_version_id(scenario, scenario["version"]["version_id"]))
    second = add_version(scenario)
    version_id = second["version"]["version_id"]
    before = counters(scenario)

    source = write_json(tmp_path / "admission.json", admission_input())
    admitted = ok(
        admit(
            scenario["project"],
            scenario["store"],
            scenario["opened"]["round_id"],
            version_id,
            source,
        )
    )
    assert admitted["schema_version"] == 1
    assert admitted["status"] == "admitted"
    assert admitted["reused"] is False
    admission = admitted["admission"]
    assert set(admission) == {
        "admission_id",
        "round_id",
        "version_id",
        "reviewer",
        "reason",
        "comparison",
        "selection_impact",
        "granted_at",
        "used",
        "consumed_validation_id",
    }
    assert admission["admission_id"] == admission_identity(admission)
    assert_hex(admission["admission_id"])
    assert admission["round_id"] == scenario["opened"]["round_id"]
    assert admission["version_id"] == version_id
    assert admission["reviewer"] == REVIEWER
    assert admission["reason"] == ADMISSION_REASON
    assert admission["comparison"] == COMPARISON
    assert admission["selection_impact"] == SELECTION_IMPACT
    assert isinstance(admission["granted_at"], str) and admission["granted_at"]
    assert admission["used"] is False
    assert admission["consumed_validation_id"] is None
    # Granting is not a read: the counters are unchanged.
    assert admitted["allowance"] == before
    after = status(scenario)
    assert counters(scenario) == before
    assert after["lifecycle"] == "awaiting_validation_admission"
    assert len(after["admissions"]) == 1
    listed = after["admissions"][0]
    assert listed["admission_id"] == admission["admission_id"]
    assert listed["version_id"] == version_id
    assert listed["used"] is False
    assert listed["consumed_validation_id"] is None
    assert listed["comparison"] == COMPARISON
    assert listed["selection_impact"] == SELECTION_IMPACT
    # The human reviewer identity and decision reason stay out of the agent view.
    assert "reviewer" not in listed
    assert "reason" not in listed
    # The operator and the agent share the same derived round view.
    operator = ok(round_status(scenario["project"], scenario["opened"]["round_id"], agent=False))
    assert operator["admissions"] == after["admissions"]
    assert operator["lifecycle"] == after["lifecycle"]

    # The trusted artifact is write-once and content-addressed.
    stored = read_admission(scenario, admission["admission_id"])
    assert stored == {
        "schema_version": 1,
        "admission_id": admission["admission_id"],
        "round_id": admission["round_id"],
        "version_id": version_id,
        "reviewer": REVIEWER,
        "reason": ADMISSION_REASON,
        "comparison": COMPARISON,
        "selection_impact": SELECTION_IMPACT,
        "granted_at": admission["granted_at"],
    }
    assert stored["admission_id"] == admission_identity(stored)
    with sqlite3.connect(
        scenario["project"] / ".prajnatrade" / "project.sqlite"
    ) as connection:
        rows = connection.execute(
            "SELECT admission_id, round_id, version_id, admission_json "
            "FROM research_loop_admissions"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == admission["admission_id"]
    assert json.loads(rows[0][3]) == stored

    # Identical content replays the stored admission; nothing new is written.
    replay = ok(
        admit(
            scenario["project"],
            scenario["store"],
            scenario["opened"]["round_id"],
            version_id,
            source,
        )
    )
    assert replay["reused"] is True
    assert replay["admission"] == admission
    assert replay["allowance"] == before
    assert len(list((scenario["store"] / "research-loop-admissions").glob("*.json"))) == 1

    # Different content for the same version is a conflict, not a second grant.
    conflicting = write_json(
        tmp_path / "admission-conflict.json",
        admission_input(selection_impact="A different selection impact."),
    )
    rejected(
        admit(
            scenario["project"],
            scenario["store"],
            scenario["opened"]["round_id"],
            version_id,
            conflicting,
        ),
        "ADMISSION_CONFLICT",
        phase="admission",
    )
    assert len(list((scenario["store"] / "research-loop-admissions").glob("*.json"))) == 1
    assert counters(scenario) == before

    # The agent may only read the round; admitting is an operator decision.
    agent_result = agent_cli(
        "round",
        "admit",
        "--project",
        str(scenario["project"]),
        "--store",
        str(scenario["store"]),
        "--round-id",
        scenario["opened"]["round_id"],
        "--version-id",
        version_id,
        "--reviewer",
        REVIEWER,
        "--reason",
        ADMISSION_REASON,
        "--input",
        str(source),
    )
    assert agent_result.returncode == 2
    assert "invalid choice" in agent_result.stderr


def test_admission_requires_the_automatic_allowance_to_be_exhausted(
    tmp_path: Path,
) -> None:
    scenario = formal_scenario(tmp_path, limit=2)
    project, store = scenario["project"], scenario["store"]
    round_id = scenario["opened"]["round_id"]
    first = scenario["version"]["version_id"]
    ok(validate_version_id(scenario, first))
    second = add_version(scenario)
    third = add_version(
        scenario,
        submission_id="v3",
        parameters={"lookback": 2, "top_k": 2, "cash_if_negative": True},
    )
    source = write_json(tmp_path / "admission.json", admission_input())

    # One automatic slot is still open: a manual admission would bypass the
    # limit, so the round refuses it before writing anything.
    blocked = rejected(
        admit(project, store, round_id, second["version"]["version_id"], source),
        "VALIDATION_ALLOWANCE_REMAINING",
        phase="admission",
    )
    assert blocked["error"]["next_action"] == "validate"
    assert blocked["allowance"] == {
        "auto_limit": 2,
        "automatic_used": 1,
        "manual_used": 0,
        "total_used": 1,
        "remaining": 1,
    }
    assert blocked["round_state"] == "research"
    assert "automatic" in blocked["error"]["message"]
    assert not (store / "research-loop-admissions").exists()
    assert status(scenario)["admissions"] == []
    assert len(round_uses(scenario)) == 1

    # The researcher must let the second automatic slot be used first.
    second_read = ok(validate_version_id(scenario, second["version"]["version_id"]))
    assert second_read["usage"]["kind"] == "automatic"
    assert len(round_uses(scenario)) == 2
    assert status(scenario)["lifecycle"] == "awaiting_validation_admission"

    # Now the limit is exhausted, so the identical admission request grants the
    # extra version and its read is counted manual.
    admitted = ok(admit(project, store, round_id, third["version"]["version_id"], source))
    assert admitted["status"] == "admitted"
    assert admitted["admission"]["version_id"] == third["version"]["version_id"]
    assert admitted["admission"]["used"] is False
    assert admitted["allowance"] == {
        "auto_limit": 2,
        "automatic_used": 2,
        "manual_used": 0,
        "total_used": 2,
        "remaining": 0,
    }
    manual = ok(validate_version_id(scenario, third["version"]["version_id"]))
    assert manual["usage"]["kind"] == "manual"
    assert manual["usage"]["admission_id"] == admitted["admission"]["admission_id"]
    assert admission_entry(scenario, third["version"]["version_id"])["used"] is True
    assert counters(scenario) == {
        "auto_limit": 2,
        "automatic_used": 2,
        "manual_used": 1,
        "total_used": 3,
        "remaining": 0,
    }


def test_round_admit_recovers_a_trusted_grant_without_a_project_row(
    tmp_path: Path,
) -> None:
    scenario = formal_scenario(tmp_path, limit=1)
    ok(validate_version_id(scenario, scenario["version"]["version_id"]))
    second = add_version(scenario)
    version_id = second["version"]["version_id"]
    round_id = scenario["opened"]["round_id"]
    source = write_json(tmp_path / "admission.json", admission_input())

    # Simulate a crash after the write-once trusted grant but before the
    # project projection is published.
    body = {
        "schema_version": 1,
        "round_id": round_id,
        "version_id": version_id,
        "reviewer": REVIEWER,
        "reason": ADMISSION_REASON,
        "comparison": COMPARISON,
        "selection_impact": SELECTION_IMPACT,
    }
    admission_id = digest(body)
    granted_at = "2024-01-02T03:04:05+00:00"
    directory = scenario["store"] / "research-loop-admissions"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{admission_id}.json").write_text(
        json.dumps({**body, "admission_id": admission_id, "granted_at": granted_at}),
        encoding="utf-8",
    )

    recovered = ok(admit(scenario["project"], scenario["store"], round_id, version_id, source))
    assert recovered["reused"] is True
    assert recovered["admission"]["admission_id"] == admission_id
    assert recovered["admission"]["granted_at"] == granted_at
    assert recovered["admission"]["used"] is False
    listed = admission_entry(scenario, version_id)
    assert listed["admission_id"] == admission_id
    assert listed["used"] is False
    with sqlite3.connect(
        scenario["project"] / ".prajnatrade" / "project.sqlite"
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM research_loop_admissions"
        ).fetchone()[0] == 1


def test_round_admit_refuses_read_ended_approved_unknown_and_closed(
    tmp_path: Path,
) -> None:
    scenario = formal_scenario(tmp_path, limit=1)
    project, store = scenario["project"], scenario["store"]
    round_id = scenario["opened"]["round_id"]
    first = ok(validate_version_id(scenario, scenario["version"]["version_id"]))
    first_version = scenario["version"]["version_id"]
    source = write_json(tmp_path / "admission.json", admission_input())

    # A version with a terminal receipt needs no admission; refusing avoids a
    # stranded grant.
    rejected(
        admit(project, store, round_id, first_version, source),
        "VALIDATION_ALREADY_COMPLETED",
        phase="admission",
    )
    assert not (store / "research-loop-admissions").exists()

    # An unknown version is not admissible.
    rejected(
        admit(project, store, round_id, "a" * 64, source),
        "SUBMISSION_NOT_FOUND",
        phase="admission",
    )

    # An ended version is terminal.
    second = add_version(scenario)
    ended = write_json(tmp_path / "end.json", end_input())
    ok(
        review(
            project, store, round_id, second["version"]["version_id"], "end",
            reason="This version is closed.", source=ended,
        )
    )
    rejected(
        admit(project, store, round_id, second["version"]["version_id"], source),
        "VERSION_ENDED",
        phase="admission",
    )

    # An approved version keeps its admitted receipt; re-admission is a conflict.
    calendar = calendar_file(tmp_path, name="calendar.json")[0]
    observation = write_json(tmp_path / "observation.json", observation_input())
    ok(
        approve_version(
            project, store, round_id, first_version, calendar, observation
        )
    )
    rejected(
        admit(project, store, round_id, first_version, source),
        "REVIEW_CONFLICT",
        phase="admission",
    )

    # A closed round accepts no new admission.
    closed = ok(
        close_round(
            project, round_id, write_json(tmp_path / "close.json", close_input())
        )
    )
    assert closed["status"] == "closed"
    rejected(
        admit(project, store, round_id, first_version, source),
        "ROUND_CLOSED",
        phase="admission",
    )
    assert first["status"] == "completed"


# ---------------------------------------------------------------------------
# 4. An admitted version is read exactly once, and only by its own version
# ---------------------------------------------------------------------------


def test_admitted_version_reads_once_and_a_different_version_cannot_consume_it(
    tmp_path: Path,
) -> None:
    scenario = formal_scenario(tmp_path, limit=1)
    project, store = scenario["project"], scenario["store"]
    round_id = scenario["opened"]["round_id"]
    first = ok(validate_version_id(scenario, scenario["version"]["version_id"]))
    second = add_version(scenario)
    version_id = second["version"]["version_id"]
    admitted = ok(
        admit(
            project,
            store,
            round_id,
            version_id,
            write_json(tmp_path / "admission.json", admission_input()),
        )
    )
    admission_id = admitted["admission"]["admission_id"]

    # The admission is bound to V2; another version is still refused before the
    # read and the admission stays unconsumed.
    third = add_version(
        scenario,
        submission_id="v3",
        parameters={"lookback": 2, "cash_if_negative": True},
    )
    rejected(
        validate_version_id(scenario, third["version"]["version_id"]),
        "VALIDATION_ALLOWANCE_EXHAUSTED",
        phase="validation",
    )
    assert counters(scenario) == {
        "auto_limit": 1,
        "automatic_used": 1,
        "manual_used": 0,
        "total_used": 1,
        "remaining": 0,
    }
    listed = admission_entry(scenario, version_id)
    assert listed["used"] is False
    assert listed["consumed_validation_id"] is None

    # The admitted version consumes the admission as its one manual read.
    validated = ok(validate_version_id(scenario, version_id))
    assert validated["usage"] == {
        "kind": "manual",
        "admission_id": admission_id,
        "sequence": 2,
        "use_id": validated["usage"]["use_id"],
    }
    assert_hex(validated["usage"]["use_id"])
    assert validated["allowance"] == {
        "auto_limit": 1,
        "automatic_used": 1,
        "manual_used": 1,
        "total_used": 2,
        "remaining": 0,
    }
    uses = round_uses(scenario)
    assert [(use["kind"], use["version_id"]) for use in uses] == [
        ("automatic", scenario["version"]["version_id"]),
        ("manual", version_id),
    ]
    assert uses[0]["validation_id"] == first["validation_id"]
    assert uses[1]["validation_id"] == validated["validation_id"]
    assert uses[1]["admission_id"] == admission_id
    assert uses[1]["sequence"] == 2
    listed = admission_entry(scenario, version_id)
    assert listed["used"] is True
    assert listed["consumed_validation_id"] == validated["validation_id"]
    summary = version_entry_for(scenario, version_id)["validation"]
    assert summary["usage"] == validated["usage"]
    # The formal receipt itself records the manual usage.
    receipt = json.loads(
        (
            store
            / "research-loop-validations"
            / f"{validated['validation_id']}.json"
        ).read_text(encoding="utf-8")
    )
    assert receipt["usage"] == validated["usage"]
    # A manual read is still a sealed-Discovery read: no protected partition and
    # no query debit.
    assert not (store / "queries.sqlite").exists()
    assert not (store / "records").exists()

    # Replaying the same version neither reads nor consumes again.
    replay = ok(validate_version_id(scenario, version_id))
    assert replay["reused"] is True
    assert replay["validation_id"] == validated["validation_id"]
    assert len(round_uses(scenario)) == 2
    assert admission_entry(scenario, version_id)["consumed_validation_id"] == (
        validated["validation_id"]
    )


# ---------------------------------------------------------------------------
# 5. Concurrency: one admission, one slot, one counted read
# ---------------------------------------------------------------------------


def _race_validations(scenario: dict, version_ids: tuple[str, str]) -> list[tuple]:
    command = round_validate_argv(
        scenario["project"],
        scenario["store"],
        scenario["snapshots"],
        scenario["opened"]["round_id"],
    )
    environment = cli_env()
    processes = [
        subprocess.Popen(
            [*command, version_id],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        for version_id in version_ids
    ]
    return [
        (process.wait(timeout=180), process.stdout.read(), process.stderr.read())
        for process in processes
    ]


def test_two_processes_consuming_the_same_admission_read_once(tmp_path: Path) -> None:
    scenario = formal_scenario(tmp_path, limit=1)
    ok(validate_version_id(scenario, scenario["version"]["version_id"]))
    second = add_version(scenario)
    version_id = second["version"]["version_id"]
    admitted = ok(
        admit(
            scenario["project"],
            scenario["store"],
            scenario["opened"]["round_id"],
            version_id,
            write_json(tmp_path / "admission.json", admission_input()),
        )
    )

    results = _race_validations(scenario, (version_id, version_id))
    assert [item[0] for item in results] == [0, 0], results
    validation_ids = {
        json.loads(stdout)["validation_id"] for code, stdout, _ in results
    }
    assert len(validation_ids) == 1
    uses = round_uses(scenario)
    assert len(uses) == 2
    manual = [use for use in uses if use["kind"] == "manual"]
    assert len(manual) == 1
    assert manual[0]["admission_id"] == admitted["admission"]["admission_id"]
    assert manual[0]["validation_id"] in validation_ids
    assert len(
        list((scenario["store"] / "research-loop-validations").glob("*.json"))
    ) == 2  # the automatic V1 receipt plus the one manual receipt
    assert counters(scenario)["manual_used"] == 1
    assert admission_entry(scenario, version_id)["consumed_validation_id"] in (
        validation_ids
    )


def test_race_between_an_admitted_and_an_unadmitted_version_yields_one_read(
    tmp_path: Path,
) -> None:
    scenario = formal_scenario(tmp_path, limit=1)
    ok(validate_version_id(scenario, scenario["version"]["version_id"]))
    second = add_version(scenario)
    third = add_version(
        scenario,
        submission_id="v3",
        parameters={"lookback": 2, "cash_if_negative": True},
    )
    admitted_version = second["version"]["version_id"]
    admitted = ok(
        admit(
            scenario["project"],
            scenario["store"],
            scenario["opened"]["round_id"],
            admitted_version,
            write_json(tmp_path / "admission.json", admission_input()),
        )
    )
    results = _race_validations(
        scenario, (admitted_version, third["version"]["version_id"])
    )
    successes = [item for item in results if item[0] == 0]
    failures = [item for item in results if item[0] == 2]
    assert len(successes) == 1, results
    assert len(failures) == 1, results
    assert json.loads(successes[0][1])["usage"]["kind"] == "manual"
    payload = json.loads(failures[0][1])
    assert payload["status"] == "rejected"
    assert payload["error"]["code"] == "VALIDATION_ALLOWANCE_EXHAUSTED"
    assert payload["round_state"] == "awaiting_validation_admission"
    uses = round_uses(scenario)
    assert len(uses) == 2
    assert [use["kind"] for use in uses] == ["automatic", "manual"]
    assert admission_entry(scenario, admitted_version)["used"] is True
    assert counters(scenario) == {
        "auto_limit": 1,
        "automatic_used": 1,
        "manual_used": 1,
        "total_used": 2,
        "remaining": 0,
    }


# ---------------------------------------------------------------------------
# 7. An interrupted manual read keeps its admission-bound use
# ---------------------------------------------------------------------------


def test_interrupted_manual_read_keeps_the_admission_and_resume_converges(
    tmp_path: Path,
) -> None:
    scenario = formal_scenario(tmp_path, limit=1)
    ok(validate_version_id(scenario, scenario["version"]["version_id"]))
    second = add_version(scenario)
    version_id = second["version"]["version_id"]
    admitted = ok(
        admit(
            scenario["project"],
            scenario["store"],
            scenario["opened"]["round_id"],
            version_id,
            write_json(tmp_path / "admission.json", admission_input()),
        )
    )
    admission_id = admitted["admission"]["admission_id"]

    interrupted = validate_version_id(
        scenario, version_id, env=cli_env(PRAJNATRADE_TEST_FAILPOINT=VALIDATION_FAILPOINT)
    )
    assert interrupted.returncode == 2, interrupted.stdout
    assert "error:" in interrupted.stderr
    markers = list((scenario["store"] / "research-loop-validations").glob("*.json"))
    assert len(markers) == 2  # the V1 automatic receipt plus the V2 running marker
    marker = next(
        json.loads(path.read_text(encoding="utf-8")) for path in markers
        if json.loads(path.read_text(encoding="utf-8"))["status"] == "running"
    )
    manual = [use for use in round_uses(scenario) if use["kind"] == "manual"]
    assert len(manual) == 1
    assert manual[0]["admission_id"] == admission_id
    assert manual[0]["validation_id"] == marker["validation_id"]
    assert counters(scenario)["manual_used"] == 1
    assert admission_entry(scenario, version_id)["used"] is True
    assert admission_entry(scenario, version_id)["consumed_validation_id"] == (
        marker["validation_id"]
    )
    # Replaying the grant after the counted read keeps the stored record and
    # exposes the consumed validation instead of granting again.
    replayed_grant = ok(
        admit(
            scenario["project"],
            scenario["store"],
            scenario["opened"]["round_id"],
            version_id,
            write_json(tmp_path / "later-admission.json", admission_input()),
        )
    )
    assert replayed_grant["reused"] is True
    assert replayed_grant["admission"]["used"] is True
    assert replayed_grant["admission"]["consumed_validation_id"] == (
        marker["validation_id"]
    )

    resumed = ok(validate_version_id(scenario, version_id))
    assert resumed["attempts"] == 2
    assert resumed["resumed"] is True
    assert resumed["usage"]["kind"] == "manual"
    assert resumed["usage"]["admission_id"] == admission_id
    assert resumed["validation_id"] == marker["validation_id"]
    assert len(round_uses(scenario)) == 2
    replay = ok(validate_version_id(scenario, version_id))
    assert replay["reused"] is True
    assert replay["validation_id"] == resumed["validation_id"]
    assert len(round_uses(scenario)) == 2


# ---------------------------------------------------------------------------
# 6. Closing at the limit or early, with or without a selected version
# ---------------------------------------------------------------------------


def test_close_at_the_limit_records_the_selected_validation_and_allowance(
    tmp_path: Path,
) -> None:
    scenario = formal_scenario(tmp_path, limit=1)
    project, store = scenario["project"], scenario["store"]
    round_id = scenario["opened"]["round_id"]
    version_id = scenario["version"]["version_id"]
    validated = ok(validate_version_id(scenario, version_id))
    calendar = calendar_file(tmp_path, name="calendar.json")[0]
    approved = ok(
        approve_version(
            project,
            store,
            round_id,
            version_id,
            calendar,
            write_json(tmp_path / "observation.json", observation_input()),
        )
    )
    assert approved["status"] == "approved"

    source = write_json(tmp_path / "close.json", close_input(selected=version_id))
    closed = ok(close_round(project, round_id, source))
    assert closed["status"] == "closed"
    assert closed["reused"] is False
    assert closed["candidate_selected"] is True
    assert closed["selected_version"] == version_id
    assert closed["validation_allowance"] == {
        "auto_limit": 1,
        "automatic_used": 1,
        "manual_used": 0,
        "total_used": 1,
        "remaining": 0,
    }
    assert closed["selected_validation"] == {
        "validation_id": validated["validation_id"],
        "receipt_hash": validated["receipt_hash"],
        "validation_kind": "trailing_validation",
        "protocol": validated["protocol"],
    }
    assert_hex(closed["selected_validation"]["receipt_hash"])

    status_body = status(scenario)
    assert status_body["status"] == "closed"
    assert status_body["lifecycle"] == "converged"
    stored_closure = {
        key: value for key, value in closed.items() if key not in {"status", "reused"}
    }
    assert status_body["closure"] == stored_closure

    replay = ok(close_round(project, round_id, source))
    assert replay["reused"] is True
    assert replay["closure_id"] == closed["closure_id"]

    # A closed round refuses new validation and submission work.
    rejected(validate_version_id(scenario, version_id), "ROUND_CLOSED")
    rejected(
        submit_version(
            project,
            round_id,
            "v2",
            write_json(
                tmp_path / "version-two.json",
                version_input(scenario["experiment"]["experiment_id"]),
            ),
        ),
        "ROUND_CLOSED",
    )


def test_early_close_with_budget_remaining_records_no_candidate(
    tmp_path: Path,
) -> None:
    scenario = formal_scenario(tmp_path, limit=2)
    project = scenario["project"]
    round_id = scenario["opened"]["round_id"]
    version_id = scenario["version"]["version_id"]
    # One experiment has run and the Campaign budget still has attempts left.
    assert scenario["experiment"]["budget"]["attempts_remaining"] >= 1

    closed = ok(
        close_round(
            project, round_id, write_json(tmp_path / "close.json", close_input())
        )
    )
    assert closed["status"] == "closed"
    assert closed["candidate_selected"] is False
    assert closed["selected_version"] is None
    assert "selected_validation" not in closed
    assert closed["validation_allowance"] == {
        "auto_limit": 2,
        "automatic_used": 0,
        "manual_used": 0,
        "total_used": 0,
        "remaining": 2,
    }
    assert closed["budget"] == {"attempts": 6, "max_seconds": 180}
    status_body = status(scenario)
    assert status_body["lifecycle"] == "converged"
    assert status_body["closure"]["candidate_selected"] is False

    rejected(validate_version_id(scenario, version_id), "ROUND_CLOSED")
    rejected(
        submit_version(
            project,
            round_id,
            "v2",
            write_json(
                tmp_path / "version-two.json",
                version_input(scenario["experiment"]["experiment_id"]),
            ),
        ),
        "ROUND_CLOSED",
    )


def test_a_legacy_stored_closure_still_replays_its_exact_shape(tmp_path: Path) -> None:
    scenario = formal_scenario(tmp_path, limit=2)
    project = scenario["project"]
    round_id = scenario["opened"]["round_id"]
    status_body = status(scenario)
    payload = close_input()
    legacy_body = {
        "schema_version": 1,
        "round_id": round_id,
        "direction_id": status_body["direction_id"],
        "project_id": status_body["project_id"],
        "campaign_id": status_body["campaign_id"],
        "scope": status_body["scope"],
        "data_kind": status_body["data_kind"],
        "reviewer": "research-lead",
        "reason": "The round stops with a recorded reason.",
        "stop_reason": payload["stop_reason"],
        "candidate_selected": False,
        "selected_version": None,
        "next_experiment": None,
        "budget": status_body["budget"],
        "versions_attempted": len(status_body["submissions"]),
    }
    closure_id = digest(legacy_body)
    stored = {**legacy_body, "closure_id": closure_id}
    with sqlite3.connect(project / ".prajnatrade" / "project.sqlite") as connection:
        connection.execute(
            "INSERT INTO research_loop_closures (round_id, closure_json) VALUES (?, ?)",
            (round_id, json.dumps(stored)),
        )

    # A closure recorded by the pre-admission code keeps its exact body and
    # content-addressed replay instead of becoming a ROUND_CLOSED conflict.
    replayed = ok(
        close_round(
            project, round_id, write_json(tmp_path / "close.json", payload)
        )
    )
    assert replayed["status"] == "closed"
    assert replayed["reused"] is True
    assert replayed["closure_id"] == closure_id
    assert "validation_allowance" not in replayed
    assert "selected_validation" not in replayed
    status_body = status(scenario)
    assert status_body["closure"] == stored
    assert status_body["lifecycle"] == "converged"


def test_v1_round_has_no_admission_surface_and_keeps_the_legacy_shapes(
    tmp_path: Path,
) -> None:
    scenario = legacy_scenario(tmp_path)
    project, store = scenario["project"], scenario["store"]
    round_id = scenario["opened"]["round_id"]
    status_body = status(scenario)
    assert "admissions" not in status_body
    assert "validation_allowance" not in status_body
    assert "validation_policy" not in status_body
    assert status_body["lifecycle"] == "research"

    # A v1 round has no automatic limit to admit past.
    source = write_json(tmp_path / "admission.json", admission_input())
    rejected(
        admit(project, store, round_id, scenario["version"]["version_id"], source),
        "VALIDATION_POLICY_REQUIRED",
        phase="admission",
    )

    validated = ok(validate_version_id(scenario, scenario["version"]["version_id"]))
    assert "usage" not in validated
    assert "allowance" not in validated
    assert "validation_kind" not in validated
    assert validated["replay"]["matches_discovery"] is True

    closed = ok(
        close_round(project, round_id, write_json(tmp_path / "close.json", close_input()))
    )
    assert "validation_allowance" not in closed
    assert "selected_validation" not in closed
    assert closed["candidate_selected"] is False
    assert status(scenario)["lifecycle"] == "converged"
    assert "admissions" not in status(scenario)


def test_v1_closure_with_a_selected_version_keeps_the_legacy_body(
    tmp_path: Path,
) -> None:
    scenario = legacy_scenario(tmp_path)
    project, store = scenario["project"], scenario["store"]
    round_id = scenario["opened"]["round_id"]
    version_id = scenario["version"]["version_id"]
    validated = ok(validate_version_id(scenario, version_id))
    assert validated["status"] == "completed"
    ok(
        approve_version(
            project,
            store,
            round_id,
            version_id,
            calendar_file(tmp_path, name="v1-calendar.json")[0],
            write_json(tmp_path / "v1-observation.json", observation_input()),
        )
    )
    status_body = status(scenario)
    payload = close_input(selected=version_id)
    closed = ok(
        close_round(project, round_id, write_json(tmp_path / "v1-close.json", payload))
    )

    # A protocol-v1 closure keeps the exact pre-#50 body and identity: selecting
    # an approved version adds no v2-only `selected_validation` or allowance.
    legacy_body = {
        "schema_version": 1,
        "round_id": round_id,
        "direction_id": status_body["direction_id"],
        "project_id": status_body["project_id"],
        "campaign_id": status_body["campaign_id"],
        "scope": status_body["scope"],
        "data_kind": status_body["data_kind"],
        "reviewer": "research-lead",
        "reason": "The round stops with a recorded reason.",
        "stop_reason": payload["stop_reason"],
        "candidate_selected": True,
        "selected_version": version_id,
        "next_experiment": None,
        "budget": status_body["budget"],
        "versions_attempted": len(status_body["submissions"]),
    }
    assert closed == {
        **legacy_body,
        "closure_id": digest(legacy_body),
        "status": "closed",
        "reused": False,
    }
    stored_status = status(scenario)
    assert stored_status["closure"] == {
        **legacy_body,
        "closure_id": digest(legacy_body),
    }
    assert "validation_allowance" not in stored_status["closure"]
    assert "selected_validation" not in stored_status["closure"]
    assert stored_status["lifecycle"] == "converged"
