"""Public-CLI tests for formal trailing Validation and its read allowance.

Ticket #52 (spec #50 T2): ``round validate`` on a protocol-v2 round evaluates a
locked version on the trailing segment only, returns a formal
``validation_kind: trailing_validation`` receipt, and places the first read and
its counted use in one atomic reservation before any attempt or sealed read.
``round revalidate`` recomputes the trailing segment on a corrected successor
without spending another use.

Every scenario drives the operator CLI through subprocesses on synthetic sealed
snapshots.  Fixture builders are imported from the modules that own them.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
from cli_helpers import (
    assert_hex,
    cli,
    cli_env,
    ok,
    rejected,
    round_validate_argv,
    write_json,
)
from test_corrected_evidence_cli import remove_protected_payloads
from test_discovery_split_cli import (
    discovery_read,
    split_campaign,
    stored_campaign,
    v2_round_input,
)
from test_experiment_cli import sealed_snapshot_input
from test_protected_eval_cli import submit_experiment
from test_research_loop_cli import (
    open_round,
    round_input,
    round_status,
    submit_version,
    suspend_derived,
    trending_snapshot,
    validate_version,
    version_input,
)

VALIDATION_FAILPOINT = "round_validation_after_attempt"
VALIDATION_TERMINAL_FAILPOINT = "round_validation_after_terminal_receipt"
V2_PROTOCOL = {
    "schema_version": 1,
    "name": "trailing-70-30",
    "validation_kind": "trailing_validation",
}
INITIAL_CASH = 100_000.0


# ---------------------------------------------------------------------------
# Scenario builders
# ---------------------------------------------------------------------------


def submit_locked_experiment(
    root: Path,
    project: Path,
    campaign_id: str,
    *,
    submission_id: str,
    family: str,
    parameters: dict | None,
    requirements: list[dict] | None = None,
) -> dict:
    """Submit one successful Discovery experiment, optionally with explicit history."""
    if parameters is None:
        parameters = (
            {"lookback": 2, "cash_if_negative": False} if family == "momentum" else {}
        )
    strategy: dict = {"family": family}
    if requirements is not None:
        strategy["history_requirements"] = requirements
    request = write_json(
        root / f"experiment-{submission_id}.json",
        {
            "schema_version": 1,
            "campaign_id": campaign_id,
            "submission_id": submission_id,
            "hypothesis": "The locked candidate is scored on the trailing segment.",
            "strategy": strategy,
            "parameters": parameters,
        },
    )
    result = ok(cli("experiment", "submit", "--project", str(project), "--input", str(request)))
    assert result["status"] == "succeeded", result
    return result


def formal_scenario(
    root: Path,
    *,
    payload: dict | None = None,
    warmup: int = 3,
    limit: int = 2,
    name: str = "project",
    family: str = "momentum",
    parameters: dict | None = None,
    requirements: list[dict] | None = None,
    hard_conditions: list[dict] | None = None,
    as_of: str | None = None,
) -> dict:
    """A split Campaign with one locked version, before any formal Validation."""
    payload = payload if payload is not None else trending_snapshot(15)
    project, snapshots, campaign = split_campaign(
        root, payload, warmup=warmup, name=name, as_of=as_of
    )
    store = root / f"{name}-trusted"
    contract = v2_round_input(limit=limit)
    if hard_conditions is not None:
        contract["hard_conditions"] = hard_conditions
    opened = ok(
        open_round(
            project,
            store,
            campaign["campaign_id"],
            write_json(root / f"{name}-round.json", contract),
        )
    )
    experiment = submit_locked_experiment(
        root,
        project,
        campaign["campaign_id"],
        submission_id="first",
        family=family,
        parameters=parameters,
        requirements=requirements,
    )
    version = ok(
        submit_version(
            project,
            opened["round_id"],
            "v1",
            write_json(
                root / f"{name}-version.json", version_input(experiment["experiment_id"])
            ),
        )
    )
    split = campaign["discovery_split"]
    trailing_dates = trailing_sessions(snapshots, campaign["snapshot_id"], split)
    return {
        "root": root,
        "project": project,
        "snapshots": snapshots,
        "campaign": campaign,
        "store": store,
        "opened": opened,
        "experiment": experiment,
        "version": version,
        "split": split,
        "trailing_dates": trailing_dates,
    }


def add_version(
    scenario: dict,
    *,
    submission_id: str = "v2",
    parameters: dict | None = None,
) -> dict:
    """Lock a second version in the same round (another candidate experiment)."""
    experiment = submit_locked_experiment(
        scenario["root"],
        scenario["project"],
        scenario["campaign"]["campaign_id"],
        submission_id=f"experiment-{submission_id}",
        family="momentum",
        parameters=parameters if parameters is not None else {"lookback": 2, "top_k": 2, "cash_if_negative": False},
    )
    version = ok(
        submit_version(
            scenario["project"],
            scenario["opened"]["round_id"],
            submission_id,
            write_json(
                scenario["root"] / f"version-{submission_id}.json",
                version_input(experiment["experiment_id"]),
            ),
        )
    )
    return {"experiment": experiment, "version": version}


def validate_version_id(scenario: dict, version_id: str, *, env: dict[str, str] | None = None):
    return validate_version(
        scenario["project"],
        scenario["store"],
        scenario["snapshots"],
        scenario["opened"]["round_id"],
        version_id,
        env=env,
    )


def legacy_scenario(
    root: Path,
    *,
    payload: dict | None = None,
    warmup: int = 3,
    name: str = "legacy",
    family: str = "momentum",
    parameters: dict | None = None,
) -> dict:
    """A protocol-v1 Campaign with one locked version (whole-interval replay)."""
    payload = payload if payload is not None else trending_snapshot(15)
    project, snapshots, campaign = _legacy_campaign(root, payload, warmup=warmup, name=name)
    store = root / f"{name}-trusted"
    opened = ok(
        open_round(
            project,
            store,
            campaign["campaign_id"],
            write_json(root / f"{name}-round.json", round_input()),
        )
    )
    experiment = submit_experiment(
        project,
        campaign["campaign_id"],
        root / f"{name}-experiment.json",
        "first",
        "The locked candidate is replayed over the frozen Discovery interval.",
        family,
        parameters if parameters is not None else {"lookback": 2, "cash_if_negative": False},
    )
    assert experiment["status"] == "succeeded", experiment
    version = ok(
        submit_version(
            project,
            opened["round_id"],
            "v1",
            write_json(
                root / f"{name}-version.json", version_input(experiment["experiment_id"])
            ),
        )
    )
    return {
        "root": root,
        "project": project,
        "snapshots": snapshots,
        "campaign": campaign,
        "store": store,
        "opened": opened,
        "experiment": experiment,
        "version": version,
    }


def drifting_snapshot(day_count: int) -> dict:
    """Raw prices and derived values drift up every session (visible in a window)."""
    payload = sealed_snapshot_input(day_count)
    index = {row["date"]: position for position, row in enumerate(payload["calendar"])}
    for row in payload["raw_bars"]:
        factor = 1.0 + 0.005 * index[row["date"]]
        for field in ("open", "high", "low", "close"):
            row[field] = round(row[field] * factor, 6)
    for row in payload["derived"]:
        row["value"] = 10.0 * (1.0 + 0.005 * index[row["date"]])
    return payload


def _legacy_campaign(
    root: Path, payload: dict, *, warmup: int, name: str,
) -> tuple[Path, Path, dict]:
    from test_discovery_split_cli import legacy_campaign

    return legacy_campaign(root, payload, warmup=warmup, name=name)


def trailing_sessions(snapshots: Path, snapshot_id: str, split: dict) -> list[str]:
    content = discovery_read(snapshots, snapshot_id)["data"]
    return sorted(
        {
            row["date"]
            for row in content["calendar"]
            if split["trailing"]["start"] <= row["date"] <= split["trailing"]["end"]
        }
    )


def validate_current(scenario: dict, *, env: dict[str, str] | None = None):
    return validate_version(
        scenario["project"],
        scenario["store"],
        scenario["snapshots"],
        scenario["opened"]["round_id"],
        scenario["version"]["version_id"],
        env=env,
    )


def trusted_receipt(scenario: dict, validation_id: str) -> dict:
    return json.loads(
        (
            scenario["store"]
            / "research-loop-validations"
            / f"{validation_id}.json"
        ).read_text(encoding="utf-8")
    )


def version_entry(scenario: dict) -> dict:
    status = ok(
        round_status(scenario["project"], scenario["opened"]["round_id"], agent=True)
    )
    return next(
        item
        for item in status["submissions"]
        if item["version_id"] == scenario["version"]["version_id"]
    )


def round_uses(scenario: dict) -> list[dict]:
    status = ok(
        round_status(scenario["project"], scenario["opened"]["round_id"], agent=True)
    )
    return status["validation_allowance"]["uses"]


# ---------------------------------------------------------------------------
# 1. The formal trailing receipt
# ---------------------------------------------------------------------------


def test_formal_validation_scores_only_the_trailing_segment(tmp_path: Path) -> None:
    scenario = formal_scenario(tmp_path)
    split = scenario["split"]
    # The formal read is a Discovery read: deleting the protected payloads must
    # not affect it, and it must write no protected query debit.
    remove_protected_payloads(scenario["snapshots"], scenario["campaign"]["snapshot_id"])
    validated = ok(validate_current(scenario))
    assert not (scenario["store"] / "queries.sqlite").exists()
    assert not (scenario["store"] / "records").exists()

    assert validated["status"] == "completed"
    assert validated["validation_kind"] == "trailing_validation"
    assert validated["outcome"] == "passed"
    assert validated["reason"] is None
    assert validated["protocol"] == {
        "schema_version": 1,
        "name": "trailing-70-30",
        "validation_kind": "trailing_validation",
        "split_hash": split["split_hash"],
        "boundary": split["boundary"],
        "research_interval": {
            "start": split["partition"]["start"],
            "end": split["partition"]["end"],
            "sessions": split["partition"]["sessions"],
        },
    }
    assert validated["development"] == split["development"]
    assert validated["trailing"] == split["trailing"]
    assert validated["scoring"] == {
        "start": split["trailing"]["start"],
        "end": split["trailing"]["end"],
        "sessions": split["trailing"]["sessions"],
        "valued_sessions": split["trailing"]["sessions"],
    }
    assert validated["provenance"]["snapshot_id"] == scenario["campaign"]["snapshot_id"]
    assert validated["provenance"]["partition_content_hash"] == (
        split["partition"]["content_hash"]
    )
    assert validated["provenance"]["as_of"] is None
    assert validated["provenance"]["development_content_hash"] == (
        split["development"]["content_hash"]
    )
    assert validated["provenance"]["trailing_content_hash"] == (
        split["trailing"]["content_hash"]
    )
    assert validated["provenance"]["version_id"] == scenario["version"]["version_id"]
    assert validated["provenance"]["experiment_id"] == (
        scenario["experiment"]["experiment_id"]
    )
    assert validated["usage"]["kind"] == "automatic"
    assert validated["usage"]["sequence"] == 1
    assert validated["usage"]["admission_id"] is None
    assert_hex(validated["usage"]["use_id"])

    # No `replay` and no `costs` check on the formal protocol.
    assert set(validated["checks"]) == {
        "data_availability",
        "scoring_start",
        "coverage",
        "position",
        "segment",
        "hard_conditions",
    }
    for name in ("data_availability", "scoring_start", "coverage", "position", "segment"):
        assert validated["checks"][name]["ok"] is True, (name, validated["checks"][name])
    assert validated["checks"]["segment"]["split_hash"] == split["split_hash"]
    assert validated["checks"]["segment"]["development"] == split["development"]
    assert validated["checks"]["segment"]["trailing"] == split["trailing"]
    assert all(
        item["status"] == "met" for item in validated["checks"]["hard_conditions"]
    )

    candidate = validated["runs"]["candidate"]
    benchmark = validated["runs"]["benchmark"]
    for run in (candidate, benchmark):
        assert run["start_date"] == split["trailing"]["start"]
        assert run["end_date"] == split["trailing"]["end"]
        assert run["sessions"] == split["trailing"]["sessions"]
        assert run["valued_sessions"] == split["trailing"]["sessions"]
    assert validated["metrics"]["candidate"]["sessions"] == split["trailing"]["sessions"]
    assert validated["metrics"]["benchmark"]["sessions"] == split["trailing"]["sessions"]

    # Both accounts open from the trailing first session with the frozen cash.
    receipt = trusted_receipt(scenario, validated["validation_id"])
    for side in ("candidate", "benchmark"):
        curve = receipt["curves"][side]
        assert curve[0]["date"] == split["trailing"]["start"]
        assert validated["runs"][side]["initial_cash"] == pytest.approx(INITIAL_CASH)
        assert validated["metrics"][side]["sessions"] == split["trailing"]["sessions"]

    assert scenario["trailing_dates"] == [
        split["trailing"]["start"],
        *scenario["trailing_dates"][1:],
    ]
    assert validated["summary"]["scoring"]["sessions"] == split["trailing"]["sessions"]
    assert validated["summary"]["verdict"] == {"outcome": "passed", "reason": None}
    for key in (
        "total_return", "max_drawdown", "fees_paid", "slippage_paid", "turnover",
        "fills", "orders_filled", "orders_unfilled", "ending_equity",
    ):
        assert key in validated["summary"]["candidate"], key
        assert key in validated["summary"]["benchmark"], key
    assert validated["observation"]["recommended_sessions"] >= 2
    assert validated["opened_protected_source"] is False
    assert validated["query_debited"] is False
    assert validated["reused"] is False
    assert validated["attempts"] == 1
    assert validated["resumed"] is False
    assert validated["allowance"] == {
        "auto_limit": 2,
        "automatic_used": 1,
        "manual_used": 0,
        "total_used": 1,
        "remaining": 1,
    }


def test_formal_validation_is_distinguishable_from_the_v1_replay(tmp_path: Path) -> None:
    formal = formal_scenario(tmp_path, payload=drifting_snapshot(15), name="formal")
    formal_receipt = ok(validate_current(formal))
    legacy = legacy_scenario(tmp_path, payload=drifting_snapshot(15), name="legacy")
    legacy_receipt = ok(validate_current(legacy))

    assert "validation_kind" not in legacy_receipt
    assert "protocol" not in legacy_receipt
    assert "scoring" not in legacy_receipt
    assert "warmup" not in legacy_receipt
    assert "usage" not in legacy_receipt
    assert "allowance" not in legacy_receipt
    assert legacy_receipt["checks"]["replay"]["ok"] is True
    assert "costs" in legacy_receipt["checks"]
    assert legacy_receipt["replay"]["candidate"]["sessions"] == 6

    assert formal_receipt["validation_kind"] == "trailing_validation"
    assert formal_receipt["metrics"]["candidate"]["sessions"] == 3
    # The whole-interval replay and the trailing-only formal score are different
    # windows and produce different returns.
    assert formal_receipt["metrics"]["candidate"]["total_return"] != pytest.approx(
        legacy_receipt["metrics"]["candidate"]["total_return"], abs=1e-9
    )
    assert formal_receipt["summary"]["candidate"]["total_return"] == pytest.approx(
        formal_receipt["metrics"]["candidate"]["total_return"]
    )


# ---------------------------------------------------------------------------
# 2. Round status, allowance and lifecycle audit
# ---------------------------------------------------------------------------


def test_round_status_audits_the_formal_receipt_and_allowance(tmp_path: Path) -> None:
    scenario = formal_scenario(tmp_path, limit=1)
    validated = ok(validate_current(scenario))

    status = ok(
        round_status(scenario["project"], scenario["opened"]["round_id"], agent=True)
    )
    assert status["protocol"] == V2_PROTOCOL
    assert status["research_partition"] == scenario["split"]
    assert status["lifecycle"] == "awaiting_validation_admission"
    allowance = status["validation_allowance"]
    assert allowance["auto_limit"] == 1
    assert allowance["automatic_used"] == 1
    assert allowance["manual_used"] == 0
    assert allowance["total_used"] == 1
    assert allowance["remaining"] == 0
    assert len(allowance["uses"]) == 1
    use = allowance["uses"][0]
    assert use["sequence"] == 1
    assert use["version_id"] == scenario["version"]["version_id"]
    assert use["validation_id"] == validated["validation_id"]
    assert use["kind"] == "automatic"
    assert use["admission_id"] is None
    assert isinstance(use["consumed_at"], str)

    entry = next(
        item
        for item in status["submissions"]
        if item["version_id"] == scenario["version"]["version_id"]
    )
    # Version, development/scoring interval, warmup source, verdict and usage.
    assert entry["protocol"] == V2_PROTOCOL
    assert entry["research_partition"] == scenario["split"]
    summary = entry["validation"]
    assert summary["validation_id"] == validated["validation_id"]
    assert summary["status"] == "completed"
    assert summary["outcome"] == "passed"
    assert summary["reason"] is None
    assert summary["kind"] == "trailing_validation"
    assert summary["protocol"] == validated["protocol"]
    assert summary["scoring"] == validated["scoring"]
    assert summary["warmup"] == validated["warmup"]
    assert summary["warmup"]["source"] == "development"
    assert summary["usage"] == validated["usage"]
    assert summary["attempts"] == 1
    assert "receipt_hash" not in summary
    assert "runs" not in summary

    # A round with a free slot is back to ordinary research after the read.
    open_slot = formal_scenario(tmp_path, limit=2, name="open-slot")
    ok(validate_current(open_slot))
    rest = ok(
        round_status(open_slot["project"], open_slot["opened"]["round_id"], agent=True)
    )
    assert rest["lifecycle"] == "research"
    assert rest["validation_allowance"]["remaining"] == 1


# ---------------------------------------------------------------------------
# 3. Known-gap gate: zero count, no attempt, no receipt
# ---------------------------------------------------------------------------


def test_known_trailing_gap_blocks_before_the_read(tmp_path: Path) -> None:
    payload = trending_snapshot(15)
    split_preview = _split_for_payload(payload)
    suspended = suspend_derived(
        payload,
        next(
            position
            for position, row in enumerate(payload["calendar"])
            if row["date"] == split_preview["trailing"]["start"]
        ),
    )
    scenario = formal_scenario(
        tmp_path,
        payload=payload,
        name="gap",
        # The suspended day must stay inside the trailing segment.
        warmup=3,
    )
    assert scenario["split"]["trailing"]["start"] == suspended

    blocked = rejected(
        validate_current(scenario), "VALIDATION_DATA_GAP", phase="validation"
    )
    assert blocked["counted"] is False
    assert blocked["trailing"]["start"] == suspended
    assert blocked["gaps"]["derived"], blocked["gaps"]
    assert blocked["allowance"]["automatic_used"] == 0
    assert not (scenario["store"] / "research-loop-validations").exists()

    status = ok(
        round_status(scenario["project"], scenario["opened"]["round_id"], agent=True)
    )
    assert status["validation_allowance"]["uses"] == []
    assert status["validation_allowance"]["automatic_used"] == 0
    assert status["lifecycle"] == "research"
    entry = next(
        item
        for item in status["submissions"]
        if item["version_id"] == scenario["version"]["version_id"]
    )
    assert entry["validation"]["status"] == "not_run"
    with sqlite3.connect(
        scenario["project"] / ".prajnatrade" / "project.sqlite"
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM research_loop_validation_attempts"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM research_loop_validation_uses"
        ).fetchone()[0] == 0


def _split_for_payload(payload: dict) -> dict:
    from prajnatrade.snapshot import discovery_split

    return discovery_split(_payload_content(payload))


def _payload_content(payload: dict) -> dict:
    """Project a seal input onto the sealed Discovery content (recomputable)."""
    dates = [row["date"] for row in payload["calendar"]]
    discovery_end = len(dates) * 3 // 5
    discovery = set(dates[:discovery_end])
    return {
        kind: [row for row in payload[kind] if row["date"] in discovery]
        for kind in ("calendar", "raw_bars", "corporate_actions", "derived")
    }


# ---------------------------------------------------------------------------
# 4. Counting: one use per version, resume and replay never add
# ---------------------------------------------------------------------------


def test_first_read_counts_once_across_interruption_and_replay(tmp_path: Path) -> None:
    scenario = formal_scenario(tmp_path)
    interrupted = validate_current(
        scenario, env=cli_env(PRAJNATRADE_TEST_FAILPOINT=VALIDATION_FAILPOINT)
    )
    assert interrupted.returncode == 2, interrupted.stdout
    assert "error:" in interrupted.stderr
    markers = list((scenario["store"] / "research-loop-validations").glob("*.json"))
    assert len(markers) == 1
    assert json.loads(markers[0].read_text(encoding="utf-8"))["status"] == "running"
    assert len(round_uses(scenario)) == 1

    resumed = ok(validate_current(scenario))
    assert resumed["attempts"] == 2
    assert resumed["resumed"] is True
    assert resumed["reused"] is False
    assert resumed["outcome"] == "passed"
    uses = round_uses(scenario)
    assert len(uses) == 1
    assert uses[0]["validation_id"] == resumed["validation_id"]

    replay = ok(validate_current(scenario))
    assert replay["reused"] is True
    assert replay["validation_id"] == resumed["validation_id"]
    assert replay["receipt_hash"] == resumed["receipt_hash"]
    assert replay["attempts"] == 2
    assert len(round_uses(scenario)) == 1

    entry = version_entry(scenario)
    assert entry["validation"]["validation_id"] == resumed["validation_id"]
    assert len(round_uses(scenario)) == 1


def test_terminal_receipt_interruption_recovers_one_use(tmp_path: Path) -> None:
    scenario = formal_scenario(tmp_path)
    interrupted = validate_current(
        scenario,
        env=cli_env(PRAJNATRADE_TEST_FAILPOINT=VALIDATION_TERMINAL_FAILPOINT),
    )
    assert interrupted.returncode == 2, interrupted.stdout
    stored = list((scenario["store"] / "research-loop-validations").glob("*.json"))
    assert len(stored) == 1
    assert len(round_uses(scenario)) == 1

    recovered = ok(validate_current(scenario))
    assert recovered["attempts"] == 2
    assert recovered["resumed"] is True
    assert recovered["reused"] is False
    assert len(round_uses(scenario)) == 1
    assert len(list((scenario["store"] / "research-loop-validations").glob("*.json"))) == 1
    replay = ok(validate_current(scenario))
    assert replay["reused"] is True
    assert replay["receipt_hash"] == recovered["receipt_hash"]
    assert len(round_uses(scenario)) == 1


def test_failed_verdict_keeps_the_counted_use(tmp_path: Path) -> None:
    scenario = formal_scenario(
        tmp_path,
        hard_conditions=[
            {"id": "impossible", "metric": "net_return", "operator": ">=", "value": 10}
        ],
    )
    failed = ok(validate_current(scenario))
    assert failed["status"] == "completed"
    assert failed["outcome"] == "failed"
    assert failed["reason"] == "HARD_CONDITION_NOT_MET"
    assert len(round_uses(scenario)) == 1
    replay = ok(validate_current(scenario))
    assert replay["reused"] is True
    assert replay["outcome"] == "failed"
    assert len(round_uses(scenario)) == 1


def test_insufficient_history_verdict_keeps_the_counted_use(tmp_path: Path) -> None:
    payload = trending_snapshot(15)
    split_preview = _split_for_payload(payload)
    index = {row["date"]: position for position, row in enumerate(payload["calendar"])}
    dev_end = index[split_preview["development"]["end"]]
    for day_index in (dev_end - 1, dev_end):
        suspend_derived(payload, day_index)
    scenario = formal_scenario(
        tmp_path,
        payload=payload,
        name="short-history",
        requirements=[
            {"id": "strategy_lookback", "observations": 3, "mode": "consecutive_sessions"}
        ],
    )
    short = ok(validate_current(scenario))
    assert short["outcome"] == "insufficient_evidence"
    assert short["reason"] == "SCORING_START_INSUFFICIENT"
    assert short["checks"]["scoring_start"]["ok"] is False
    assert short["checks"]["coverage"]["ok"] is True
    assert len(round_uses(scenario)) == 1


# ---------------------------------------------------------------------------
# 5. The automatic allowance: exhaustion and concurrency
# ---------------------------------------------------------------------------


def test_exhausted_allowance_blocks_before_the_read(tmp_path: Path) -> None:
    scenario = formal_scenario(tmp_path, limit=1)
    first = ok(validate_current(scenario))
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
    # No attempt, no trusted write and no read for the refused version.
    assert len(list((scenario["store"] / "research-loop-validations").glob("*.json"))) == 1
    assert len(round_uses(scenario)) == 1
    entry = next(
        item
        for item in ok(
            round_status(scenario["project"], scenario["opened"]["round_id"], agent=True)
        )["submissions"]
        if item["version_id"] == second["version"]["version_id"]
    )
    assert entry["validation"]["status"] == "not_run"
    assert first["usage"]["sequence"] == 1
    status = ok(
        round_status(scenario["project"], scenario["opened"]["round_id"], agent=True)
    )
    assert status["lifecycle"] == "awaiting_validation_admission"


def test_two_versions_racing_the_last_slot_yield_exactly_one_read(
    tmp_path: Path,
) -> None:
    scenario = formal_scenario(tmp_path, limit=1)
    second = add_version(scenario)
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
        for version_id in (
            scenario["version"]["version_id"],
            second["version"]["version_id"],
        )
    ]
    results = [
        (process.wait(timeout=180), process.stdout.read(), process.stderr.read())
        for process in processes
    ]
    successes = [item for item in results if item[0] == 0]
    failures = [item for item in results if item[0] == 2]
    assert len(successes) == 1, results
    assert len(failures) == 1, results
    payload = json.loads(failures[0][1])
    assert payload["status"] == "rejected"
    assert payload["error"]["code"] == "VALIDATION_ALLOWANCE_EXHAUSTED"
    assert payload["round_state"] == "awaiting_validation_admission"
    assert ok(
        round_status(scenario["project"], scenario["opened"]["round_id"], agent=True)
    )["validation_allowance"]["automatic_used"] == 1
    assert len(round_uses(scenario)) == 1
    assert len(list((scenario["store"] / "research-loop-validations").glob("*.json"))) == 1


# ---------------------------------------------------------------------------
# 6. Prehistory and the first trailing decision
# ---------------------------------------------------------------------------


def test_compliant_prehistory_scores_every_trailing_session(tmp_path: Path) -> None:
    scenario = formal_scenario(tmp_path, payload=drifting_snapshot(15))
    validated = ok(validate_current(scenario))
    trailing = scenario["split"]["trailing"]
    assert validated["checks"]["scoring_start"]["ok"] is True
    assert validated["checks"]["coverage"]["ok"] is True
    assert validated["warmup"]["source"] == "development"
    assert validated["warmup"]["first_ready_date"] == trailing["start"]
    assert validated["warmup"]["start"] == scenario["split"]["development"]["start"]
    assert validated["warmup"]["end"] == scenario["split"]["development"]["end"]
    assert validated["warmup"]["sessions"] == scenario["split"]["development"]["sessions"]
    for side in ("candidate", "benchmark"):
        assert validated["runs"][side]["valued_sessions"] == trailing["sessions"]
        assert validated["runs"][side]["sessions"] == trailing["sessions"]


def test_later_trailing_data_does_not_change_the_first_decision(tmp_path: Path) -> None:
    base = drifting_snapshot(15)
    preview = _split_for_payload(base)
    first_trailing = preview["trailing"]["start"]
    variant = deepcopy(base)
    for row in variant["raw_bars"]:
        if row["date"] > first_trailing and row["date"] <= preview["trailing"]["end"]:
            for field in ("open", "high", "low", "close"):
                row[field] = round(row[field] * 3.0, 6)
    for row in variant["derived"]:
        if row["date"] > first_trailing and row["date"] <= preview["trailing"]["end"]:
            row["value"] = round(row["value"] * 3.0, 6)
    sooner = formal_scenario(tmp_path, payload=base, name="base")
    later = formal_scenario(tmp_path, payload=variant, name="variant")
    first = ok(validate_current(sooner))
    second = ok(validate_current(later))
    assert first["scoring"]["start"] == second["scoring"]["start"] == first_trailing
    for side in ("candidate", "benchmark"):
        assert first["runs"][side]["fills"], "the fixture must actually trade"
        assert first["runs"][side]["fills"][0] == second["runs"][side]["fills"][0]
        assert first["runs"][side]["start_date"] == second["runs"][side]["start_date"]
    first_curve = trusted_receipt(sooner, first["validation_id"])["curves"]["candidate"][0]
    second_curve = trusted_receipt(later, second["validation_id"])["curves"]["candidate"][0]
    assert first_curve["date"] == second_curve["date"] == first_trailing
    assert first_curve["nav"] == pytest.approx(second_curve["nav"])
    assert first_curve["holdings"] == second_curve["holdings"]


def test_trailing_cash_only_candidate_opens_with_the_frozen_cash(tmp_path: Path) -> None:
    scenario = formal_scenario(
        tmp_path,
        payload=sealed_snapshot_input(15),
        name="cash",
        parameters={"lookback": 2, "cash_if_negative": True},
    )
    validated = ok(validate_current(scenario))
    assert validated["outcome"] == "passed"
    assert validated["checks"]["position"]["candidate"]["state"] == "intentional_cash"
    assert validated["runs"]["candidate"]["fill_count"] == 0
    receipt = trusted_receipt(scenario, validated["validation_id"])
    trailing = scenario["split"]["trailing"]
    assert receipt["curves"]["candidate"][0]["date"] == trailing["start"]
    assert receipt["curves"]["candidate"][0]["cash"] == pytest.approx(INITIAL_CASH)
    assert validated["runs"]["candidate"]["initial_cash"] == pytest.approx(INITIAL_CASH)
    assert validated["runs"]["benchmark"]["initial_cash"] == pytest.approx(INITIAL_CASH)
    assert receipt["curves"]["benchmark"][0]["date"] == trailing["start"]


# ---------------------------------------------------------------------------
# 7. Only the trailing data changes the formal receipt
# ---------------------------------------------------------------------------


def test_trailing_only_data_changes_the_formal_receipt_not_the_development(
    tmp_path: Path,
) -> None:
    base = drifting_snapshot(15)
    preview = _split_for_payload(base)
    first_trailing = preview["trailing"]["start"]
    variant = deepcopy(base)
    for row in variant["raw_bars"]:
        if row["date"] > first_trailing and row["date"] <= preview["trailing"]["end"]:
            for field in ("open", "high", "low", "close"):
                row[field] = round(row[field] * 3.0, 6)
    for row in variant["derived"]:
        if row["date"] > first_trailing and row["date"] <= preview["trailing"]["end"]:
            row["value"] = round(row["value"] * 3.0, 6)
    first = formal_scenario(tmp_path, payload=base, name="first")
    second = formal_scenario(tmp_path, payload=variant, name="second")
    assert first["split"]["development"] == second["split"]["development"]
    assert first["split"]["trailing"]["content_hash"] != (
        second["split"]["trailing"]["content_hash"]
    )

    # The locked development copy and the Discovery experiment are identical.
    blob_first, hash_first, _ = stored_campaign(first["project"])
    blob_second, hash_second, _ = stored_campaign(second["project"])
    assert blob_first == blob_second
    assert hash_first == hash_second
    discovery_first = first["experiment"]["discovery"]
    discovery_second = second["experiment"]["discovery"]
    assert discovery_first["signals"] == discovery_second["signals"]
    assert discovery_first["fills"] == discovery_second["fills"]
    assert discovery_first["metrics"] == discovery_second["metrics"]
    assert first["version"]["locked"]["snapshot_hash"] == (
        second["version"]["locked"]["snapshot_hash"]
    )

    # The formal receipt changes with the trailing evidence it actually scored.
    receipt_first = ok(validate_current(first))
    receipt_second = ok(validate_current(second))
    assert receipt_first["trailing"] != receipt_second["trailing"]
    assert receipt_first["provenance"]["development_content_hash"] == (
        receipt_second["provenance"]["development_content_hash"]
    )
    assert receipt_first["provenance"]["trailing_content_hash"] != (
        receipt_second["provenance"]["trailing_content_hash"]
    )
    assert receipt_first["metrics"]["candidate"]["total_return"] != pytest.approx(
        receipt_second["metrics"]["candidate"]["total_return"], abs=1e-9
    )


# ---------------------------------------------------------------------------
# 8. Protocol-v2 revalidation on a corrected successor
# ---------------------------------------------------------------------------

REVALIDATION_REASON = "The official trailing cash dividend was corrected."


def trailing_correction_payload() -> dict:
    """Discovery payload with a development and a trailing dividend.

    The corrected successor built from it by the trusted dividend journal
    rewrites the affected symbol's derived rows to ``null`` for every session,
    so the development segment already carries those gaps: a correction of the
    *trailing* dividend therefore leaves the frozen development slice intact,
    which is exactly the trailing-only successor this protocol must accept.
    """
    payload = sealed_snapshot_input(15)
    dates = [row["date"] for row in payload["calendar"]]
    discovery = dates[: len(dates) * 3 // 5]
    development = discovery[: len(discovery) * 7 // 10]
    trailing = discovery[len(discovery) * 7 // 10:]
    dividend = payload["corporate_actions"][0]
    dividend.update(date=trailing[0], pay_date=trailing[1], cash_per_share=0.2)
    dev_action = deepcopy(dividend)
    dev_action.update(
        date=development[2], pay_date=development[3], cash_per_share=0.1
    )
    payload["corporate_actions"].append(dev_action)
    development_dates = set(development)
    for row in payload["derived"]:
        if row["symbol"] == "511260" and row["date"] in development_dates:
            row["value"] = None
    return {
        "payload": payload,
        "development": development,
        "trailing": trailing,
        "dev_ex_date": development[2],
        "dev_amount": 0.1,
        "trailing_ex_date": trailing[0],
        "trailing_amount": 0.2,
    }


def corrected_successor(
    root: Path,
    snapshots: Path,
    snapshot_id: str,
    *,
    ex_date: str,
    old_amount: float,
    new_amount: float,
    tag: str,
    available_at: str = "2024-01-05T09:00:00+08:00",
) -> str:
    proposal = {
        "schema_version": 1,
        "predecessor_id": snapshot_id,
        "symbol": "511260",
        "ex_date": ex_date,
        "currency": "CNY",
        "old_amount": old_amount,
        "new_amount": new_amount,
        "supplier": {
            "source": "synthetic supplier",
            "archive_reference": f"synthetic-archive:{tag}",
            "observed_at": "2024-01-04T10:00:00+08:00",
        },
    }
    pending = ok(
        cli(
            "snapshot",
            "correction",
            "register",
            "--input",
            str(write_json(root / f"proposal-{tag}.json", proposal)),
            "--store",
            str(snapshots),
        )
    )
    notice = {
        "kind": "synthetic",
        "authority": "fund_manager",
        "issuer": "Synthetic Fund Manager",
        "url": "https://notices.example.invalid/dividend",
        "reference": f"synthetic-notice-{tag}",
        "symbol": "511260",
        "ex_date": ex_date,
        "currency": "CNY",
        "amount": new_amount,
        "available_at": available_at,
        "collected_at": "2024-01-11T10:00:00+08:00",
    }
    verified = ok(
        cli(
            "snapshot",
            "correction",
            "verify",
            "--input",
            str(write_json(root / f"notice-{tag}.json", notice)),
            "--store",
            str(snapshots),
            "--proposal-id",
            pending["proposal_id"],
        )
    )
    sealed = ok(
        cli(
            "snapshot",
            "correction",
            "successor",
            "--store",
            str(snapshots),
            "--proposal-id",
            pending["proposal_id"],
            "--revision-id",
            verified["revision_id"],
        )
    )
    return sealed["snapshot_id"]


def revalidate_version(
    scenario: dict,
    evidence_snapshot_id: str | None = None,
    *,
    reason: str = REVALIDATION_REASON,
):
    arguments = [
        "round",
        "revalidate",
        "--project",
        str(scenario["project"]),
        "--store",
        str(scenario["store"]),
        "--snapshot-store",
        str(scenario["snapshots"]),
        "--round-id",
        str(scenario["opened"]["round_id"]),
        "--version-id",
        str(scenario["version"]["version_id"]),
        "--reason",
        reason,
    ]
    if evidence_snapshot_id is not None:
        arguments += ["--evidence-snapshot-id", evidence_snapshot_id]
    return cli(*arguments)


def formal_revalidation_scenario(tmp_path: Path) -> dict:
    fixture = trailing_correction_payload()
    scenario = formal_scenario(
        tmp_path,
        payload=fixture["payload"],
        name="revalidate",
        family="equal_weight",
    )
    scenario["fixture"] = fixture
    scenario["prior"] = ok(validate_current(scenario))
    return scenario


def test_v2_revalidate_on_a_trailing_only_successor(tmp_path: Path) -> None:
    scenario = formal_revalidation_scenario(tmp_path)
    split = scenario["split"]
    fixture = scenario["fixture"]
    prior = scenario["prior"]
    successor = corrected_successor(
        tmp_path,
        scenario["snapshots"],
        scenario["campaign"]["snapshot_id"],
        ex_date=fixture["trailing_ex_date"],
        old_amount=fixture["trailing_amount"],
        new_amount=0.18,
        tag="trailing",
    )
    revalidated = ok(revalidate_version(scenario, successor))

    assert revalidated["status"] == "revalidated"
    assert revalidated["validation_kind"] == "trailing_validation"
    assert revalidated["outcome"] == "passed"
    assert revalidated["previous_validation_id"] == prior["validation_id"]
    assert revalidated["previous_receipt_hash"] == prior["receipt_hash"]
    assert revalidated["previous_outcome"] == prior["outcome"]
    assert revalidated["validation_id"] != prior["validation_id"]
    assert revalidated["evidence"]["snapshot_id"] == successor
    assert revalidated["evidence"]["previous_snapshot_id"] == (
        scenario["campaign"]["snapshot_id"]
    )
    assert revalidated["evidence"]["validation_kind"] == "trailing_validation"
    assert revalidated["evidence"]["development"] == split["development"]
    assert revalidated["evidence"]["trailing"] == split["trailing"]

    original = revalidated["original_validation"]
    recomputed = revalidated["recomputed_validation"]
    assert original["validation_id"] == prior["validation_id"]
    assert original["outcome"] == prior["outcome"]
    assert original["snapshot_id"] == scenario["campaign"]["snapshot_id"]
    assert original["development"] == split["development"]
    assert original["trailing"] == split["trailing"]
    assert recomputed["validation_id"] == revalidated["validation_id"]
    assert recomputed["snapshot_id"] == successor
    assert recomputed["development"] == split["development"]
    assert recomputed["trailing"]["sessions"] == split["trailing"]["sessions"]

    affected = revalidated["affected"]
    assert affected["changed_rows"]["corporate_actions"] >= 1
    assert affected["changed_rows"]["derived"] > 0
    assert affected["changed_sessions"]
    assert affected["changed_nav_sessions"] == []
    assert affected["ending_equity"]["delta"] == pytest.approx(0.0, abs=1e-9)
    assert "corrected Discovery evidence changed" in affected["reason"]

    # The corrected successor keeps the version's one counted use.
    uses = round_uses(scenario)
    assert len(uses) == 1
    assert uses[0]["version_id"] == scenario["version"]["version_id"]
    assert uses[0]["sequence"] == 1

    # The project receipt is the new epoch and links its prior evidence.
    entry = version_entry(scenario)
    summary = entry["validation"]
    assert summary["validation_id"] == revalidated["validation_id"]
    assert summary["kind"] == "trailing_validation"
    assert summary["revalidation_id"] == revalidated["revalidation_id"]
    assert summary["revalidated_from"] == prior["validation_id"]
    assert summary["original_validation"] == original
    assert summary["recomputed_validation"] == recomputed
    assert summary["affected"] == affected

    replay = ok(revalidate_version(scenario, successor))
    assert replay["reused"] is True
    assert replay["revalidation_id"] == revalidated["revalidation_id"]
    assert len(round_uses(scenario)) == 1

    rejected(validate_current(scenario), "REVALIDATION_SUPERSEDED", phase="validation")
    assert len(round_uses(scenario)) == 1


def test_v2_revalidate_rejects_a_changed_development_slice(tmp_path: Path) -> None:
    scenario = formal_revalidation_scenario(tmp_path)
    fixture = scenario["fixture"]
    prior = scenario["prior"]
    successor = corrected_successor(
        tmp_path,
        scenario["snapshots"],
        scenario["campaign"]["snapshot_id"],
        ex_date=fixture["dev_ex_date"],
        old_amount=fixture["dev_amount"],
        new_amount=0.09,
        tag="development",
    )
    blocked = rejected(
        revalidate_version(scenario, successor),
        "EVIDENCE_DEVELOPMENT_MISMATCH",
        phase="revalidation",
    )
    assert blocked["expected"]["content_hash"] == scenario["split"]["development"][
        "content_hash"
    ]
    assert blocked["actual"]["content_hash"] != blocked["expected"]["content_hash"]
    assert len(round_uses(scenario)) == 1
    entry = version_entry(scenario)
    assert entry["validation"]["validation_id"] == prior["validation_id"]
    assert not (scenario["store"] / "research-loop-revalidations").exists()
    receipts = list((scenario["store"] / "research-loop-validations").glob("*.json"))
    assert len(receipts) == 1
    with sqlite3.connect(
        scenario["project"] / ".prajnatrade" / "project.sqlite"
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM research_loop_revalidations"
        ).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# 9. Post-reservation mismatches are typed and keep the counted use
# ---------------------------------------------------------------------------


def test_partition_mismatch_after_the_reserved_read_keeps_the_use(
    tmp_path: Path,
) -> None:
    scenario = formal_scenario(tmp_path)
    snapshot_id = scenario["campaign"]["snapshot_id"]
    payload_path = scenario["snapshots"] / snapshot_id / "discovery.json"
    payload_path.chmod(0o644)
    tampered = json.loads(payload_path.read_text(encoding="utf-8"))
    tampered["derived"][0]["value"] = 999.0
    payload_path.write_text(json.dumps(tampered), encoding="utf-8")
    payload_path.chmod(0o444)

    # The manifest metadata is intact, so the known-gap gate passes; the
    # counted reservation happens before the payload integrity check fails.
    blocked = rejected(
        validate_current(scenario), "VALIDATION_PARTITION_MISMATCH", phase="validation"
    )
    assert blocked["counted"] is True
    uses = round_uses(scenario)
    assert len(uses) == 1
    assert uses[0]["kind"] == "automatic"
    markers = list((scenario["store"] / "research-loop-validations").glob("*.json"))
    assert len(markers) == 1
    assert json.loads(markers[0].read_text(encoding="utf-8"))["status"] == "running"


def test_formal_validation_reads_at_the_campaigns_frozen_as_of(tmp_path: Path) -> None:
    as_of = "2024-01-05T00:00:00+08:00"
    scenario = formal_scenario(
        tmp_path,
        payload=sealed_snapshot_input(15),
        name="as-of",
        family="equal_weight",
        warmup=1,
        as_of=as_of,
    )
    split = scenario["split"]
    assert split["as_of"] == as_of
    assert split["partition"]["sessions"] == 5
    validated = ok(validate_current(scenario))
    assert validated["outcome"] == "passed"
    assert validated["provenance"]["as_of"] == as_of
    assert validated["checks"]["segment"]["as_of"] == as_of
    assert validated["trailing"] == split["trailing"]
    assert validated["development"] == split["development"]
    assert validated["scoring"]["sessions"] == 2
    assert validated["metrics"]["candidate"]["sessions"] == 2
    receipt = trusted_receipt(scenario, validated["validation_id"])
    assert receipt["curves"]["candidate"][0]["date"] == split["trailing"]["start"]
