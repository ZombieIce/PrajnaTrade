"""Public-CLI tests for research rounds, locked versions and lightweight Validation.

Ticket #44 (spec #43): ``round open/status/submit/validate/close``.  Every
scenario drives the operator CLI (and the agent CLI where the spec grants it)
through subprocesses, on synthetic sealed snapshots.  Fixture builders are
imported from the modules that own them; nothing here is copied.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from cli_helpers import agent_cli, assert_hex, cli, cli_env, ok, rejected, write_json
from test_experiment_cli import campaign_options, sealed_snapshot_input
from test_protected_eval_cli import SYMBOLS, submit_experiment
VALIDATION_FAILPOINT = "round_validation_after_attempt"
VALIDATION_TERMINAL_FAILPOINT = "round_validation_after_terminal_receipt"


# ---------------------------------------------------------------------------
# Builders and assertions
# ---------------------------------------------------------------------------


def round_input(**overrides: object) -> dict:
    """The round contract a research lead approves before work starts."""
    payload: dict[str, object] = {
        "objective": "Compare short and long synthetic trend candidates on frozen Discovery evidence.",
        "constraints": ["Synthetic evidence only", "One Campaign per round"],
        "comparison": {
            "benchmark": "equal_weight_buy_and_hold",
            "statement": "Equal-weight buy-and-hold with the same cash, costs and interval.",
        },
        "selection_rules": [
            "Prefer a version whose replay matches Discovery and meets every hard condition.",
        ],
        "hard_conditions": [
            {"id": "loss", "metric": "net_return", "operator": ">=", "value": -0.5},
            {"id": "coverage", "metric": "valued_sessions", "operator": ">=", "value": 1},
        ],
        "disclosed_risks": ["Synthetic prices do not establish real-market evidence."],
        "scoring_policy": {
            "mode": "valid_observations",
            "data_availability": "next_open",
            "consecutive_sessions": False,
        },
        "observation": {
            "basis": "rebalance_frequency",
            "minimum_opportunities": 2,
            "review_conditions": ["unfilled intent in two consecutive opportunities"],
        },
        "invalidation": {
            "max_unfilled_opportunities": 3,
            "max_abs_drawdown": 0.25,
            "max_data_gap_sessions": 2,
        },
        "reviewer": "research-lead",
        "approval_reason": "The round may start inside the frozen Campaign budget.",
    }
    payload.update(overrides)
    return payload


def version_input(experiment_id: str, **overrides: object) -> dict:
    payload: dict[str, object] = {
        "experiment_id": experiment_id,
        "change": "Use the frozen trend lookback instead of the previous default.",
        "question": "Does the change survive replay over the frozen Discovery interval?",
        "expected_change": "The fill sequence differs while the interval and cash basis stay fixed.",
        "revisit_condition": "A replay mismatch or a failed hard condition would reject this version.",
        "risks": ["Synthetic prices overfit the declared lookback."],
        "related_experiment_ids": [],
    }
    payload.update(overrides)
    return payload


def campaign_with_warmup(
    root: Path,
    payload: dict,
    *,
    warmup: int,
    name: str = "project",
    attempts: int = 6,
    max_seconds: float = 180,
) -> tuple[Path, Path, dict]:
    """Initialize a project, seal the synthetic snapshot and create the Campaign."""
    project = root / name
    source = write_json(root / f"{name}-snapshot.json", payload)
    ok(cli("project", "init", "--project", str(project)))
    campaign = ok(
        cli(
            *campaign_options(project, source, attempts=attempts, max_seconds=max_seconds),
            "--discovery-warmup-sessions",
            str(warmup),
        )
    )
    snapshots = project.parent / f"{project.name}-snapshot-store"
    return project, snapshots, campaign


def open_round(
    project: Path,
    store: Path,
    campaign_id: str,
    source: Path,
    *,
    direction_key: str = "synthetic-trend",
    name: str = "round-one",
    authorization_id: str = "authorization-1",
) -> object:
    return cli(
        "round",
        "open",
        "--project",
        str(project),
        "--store",
        str(store),
        "--campaign-id",
        campaign_id,
        "--direction-key",
        direction_key,
        "--name",
        name,
        "--authorization-id",
        authorization_id,
        "--input",
        str(source),
    )


def submit_version(
    project: Path,
    round_id: str,
    submission_id: str,
    source: Path,
    *,
    agent: bool = True,
) -> object:
    runner = agent_cli if agent else cli
    return runner(
        "round",
        "submit",
        "--project",
        str(project),
        "--round-id",
        round_id,
        "--submission-id",
        submission_id,
        "--input",
        str(source),
    )


def validate_version(
    project: Path,
    store: Path,
    snapshots: Path,
    round_id: str,
    version_id: str,
    *,
    env: dict[str, str] | None = None,
    timeout: float = 180,
) -> object:
    return cli(
        "round",
        "validate",
        "--project",
        str(project),
        "--store",
        str(store),
        "--snapshot-store",
        str(snapshots),
        "--round-id",
        round_id,
        "--version-id",
        version_id,
        env=env,
        timeout=timeout,
    )


def close_round(project: Path, round_id: str, source: Path) -> object:
    return cli(
        "round",
        "close",
        "--project",
        str(project),
        "--round-id",
        round_id,
        "--reviewer",
        "research-lead",
        "--reason",
        "The round stops with a recorded reason.",
        "--input",
        str(source),
    )


def round_status(project: Path, round_id: str | None = None, *, agent: bool = True) -> object:
    runner = agent_cli if agent else cli
    args = ["round", "status", "--project", str(project)]
    if round_id is not None:
        args += ["--round-id", round_id]
    return runner(*args)


def submit_strategy_result(
    project: Path,
    campaign_id: str,
    source: Path,
    submission_id: str,
    family: str,
    parameters: dict,
    *,
    requirements: list[dict] | None = None,
) -> object:
    strategy: dict[str, object] = {"family": family}
    if requirements is not None:
        strategy["history_requirements"] = requirements
    write_json(
        source,
        {
            "schema_version": 1,
            "campaign_id": campaign_id,
            "submission_id": submission_id,
            "hypothesis": "The frozen history policy admits this synthetic version.",
            "strategy": strategy,
            "parameters": parameters,
        },
    )
    return cli("experiment", "submit", "--project", str(project), "--input", str(source))


def submit_strategy(*args: object, **kwargs: object) -> dict:
    return ok(submit_strategy_result(*args, **kwargs))


def opening(
    root: Path,
    payload: dict,
    *,
    warmup: int,
    round_payload: dict | None = None,
    name: str = "project",
    direction_key: str = "synthetic-trend",
) -> tuple[Path, Path, dict, Path, dict]:
    """Create the Campaign and approve the round; return its common bindings."""
    project, snapshots, campaign = campaign_with_warmup(root, payload, warmup=warmup, name=name)
    store = root / f"{name}-trusted"
    source = write_json(root / f"{name}-round.json", round_payload or round_input())
    opened = ok(open_round(project, store, campaign["campaign_id"], source, direction_key=direction_key))
    return project, snapshots, campaign, store, opened


def lock_version(
    root: Path,
    project: Path,
    round_id: str,
    submission_id: str,
    experiment_id: str,
) -> dict:
    source = write_json(root / f"{submission_id}.json", version_input(experiment_id))
    return ok(submit_version(project, round_id, submission_id, source))


def trending_snapshot(day_count: int) -> dict:
    """Flat raw bars with a rising derived close so trend rules can buy."""
    payload = sealed_snapshot_input(day_count)
    index = {row["date"]: position for position, row in enumerate(payload["calendar"])}
    for row in payload["derived"]:
        row["value"] = 10.0 * (1.0 + 0.001 * index[row["date"]])
    return payload


def suspend_derived(payload: dict, day_index: int) -> str:
    """Make one session's derived close a gap; return the suspended date."""
    suspended = payload["calendar"][day_index]["date"]
    for row in payload["derived"]:
        if row["date"] == suspended:
            row["value"] = None
    return suspended


def unexecutable_opens(payload: dict, day_index: int) -> str:
    """Keep the session complete but make every open non-executable."""
    halted = payload["calendar"][day_index]["date"]
    for row in payload["raw_bars"]:
        if row["date"] == halted:
            row["trading_status"] = "unknown"
    return halted


def assert_checks_met(receipt: dict) -> None:
    checks = receipt["checks"]
    for name in ("data_availability", "scoring_start", "coverage", "position", "costs", "replay"):
        assert checks[name]["ok"] is True, (name, checks[name])
    assert all(item["status"] == "met" for item in checks["hard_conditions"]), checks["hard_conditions"]


def assert_replay_matches_discovery(receipt: dict, experiment: dict) -> None:
    discovery = experiment["discovery"]
    baseline = discovery["baseline_comparison"]["baseline"]
    replay = receipt["replay"]
    assert replay["matches_discovery"] is True, replay["mismatches"]
    assert replay["mismatches"] == []
    for label, original in (("candidate", discovery), ("benchmark", baseline)):
        summary = replay[label]
        assert summary["start_date"] == original["start_date"]
        assert summary["end_date"] == original["end_date"]
        assert summary["sessions"] == original["metrics"]["sessions"]
        assert summary["valued_sessions"] == original["metrics"]["valued_sessions"]
        assert summary["fills"] == original["metrics"]["fills"]
        assert summary["ending_equity"] == pytest.approx(
            original["metrics"]["ending_equity"], abs=1e-9
        )


# ---------------------------------------------------------------------------
# round open / status
# ---------------------------------------------------------------------------


def test_round_open_freezes_the_contract_and_replays(tmp_path: Path) -> None:
    project, _, campaign = campaign_with_warmup(tmp_path, sealed_snapshot_input(15), warmup=4)
    store = tmp_path / "trusted"
    source = write_json(tmp_path / "round.json", round_input())

    opened = ok(open_round(project, store, campaign["campaign_id"], source))
    assert opened["schema_version"] == 1
    assert opened["status"] == "opened"
    assert opened["reused"] is False
    assert_hex(opened["round_id"])
    assert_hex(opened["direction_id"])
    assert_hex(opened["kickoff_hash"])
    assert opened["project_id"] == campaign["project_id"]
    assert opened["campaign_id"] == campaign["campaign_id"]
    assert opened["scope"] == "synthetic"
    assert opened["data_kind"] == "synthetic"

    kickoff = opened["kickoff"]
    assert kickoff["objective"] == round_input()["objective"]
    assert kickoff["constraints"] == round_input()["constraints"]
    assert kickoff["selection_rules"] == round_input()["selection_rules"]
    assert kickoff["hard_conditions"] == round_input()["hard_conditions"]
    assert kickoff["disclosed_risks"] == round_input()["disclosed_risks"]
    assert kickoff["scoring_policy"]["frozen"] == campaign["history_policy"]
    assert kickoff["frozen_campaign"]["snapshot_id"] == campaign["snapshot_id"]
    assert (
        kickoff["frozen_campaign"]["campaign_evaluation_hash"]
        == campaign["fingerprints"]["evaluation"]
    )
    assert kickoff["frozen_campaign"]["snapshot_hash"] == campaign["fingerprints"]["snapshot"]
    assert kickoff["frozen_campaign"]["initial_cash"] == 100_000.0
    assert kickoff["frozen_campaign"]["transaction_cost_bps"] == 5.0
    assert kickoff["frozen_campaign"]["execution_rules"] == campaign["execution_rules"]
    assert kickoff["budget"] == {"attempts": 6, "max_seconds": 180}

    round_copy = store / "research-loop-rounds" / f"{opened['round_id']}.json"
    assert round_copy.is_file()
    assert json.loads(round_copy.read_text(encoding="utf-8"))["kickoff_hash"] == opened["kickoff_hash"]
    assert not round_copy.is_relative_to(project)
    round_copy_bytes = round_copy.read_bytes()

    # Identical content replays; a renamed direction key keeps the same identity.
    replayed = ok(
        open_round(
            project,
            store,
            campaign["campaign_id"],
            source,
            direction_key="  Synthetic Trend ",
        )
    )
    assert replayed["reused"] is True
    assert replayed["round_id"] == opened["round_id"]
    assert replayed["kickoff_hash"] == opened["kickoff_hash"]
    assert replayed["status"] == "opened"
    assert round_copy.read_bytes() == round_copy_bytes

    # Different approved content for the same Campaign conflicts.
    other = write_json(tmp_path / "round-other.json", round_input(objective="A different objective."))
    rejected(open_round(project, store, campaign["campaign_id"], other), "ROUND_ALREADY_OPENED")

    status = ok(round_status(project, opened["round_id"]))
    assert status["status"] == "open"
    assert status["round_id"] == opened["round_id"]
    assert status["budget"] == {"attempts": 6, "max_seconds": 180}
    assert status["submissions"] == []
    assert status["observation"] == {"recommended": None, "confirmed": None}
    assert "closure" not in status
    listed = ok(round_status(project))
    assert [item["round_id"] for item in listed["rounds"]] == [opened["round_id"]]


def test_round_open_requires_a_frozen_scoring_policy(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = write_json(tmp_path / "snapshot.json", sealed_snapshot_input(15))
    ok(cli("project", "init", "--project", str(project)))
    campaign = ok(cli(*campaign_options(project, source)))
    store = tmp_path / "trusted"
    round_source = write_json(tmp_path / "round.json", round_input())

    rejected(
        open_round(project, store, campaign["campaign_id"], round_source),
        "SCORING_POLICY_NOT_FROZEN",
    )

    # The schema is strict: unknown keys and unsupported scoring modes are refused.
    frozen_project, _, frozen_campaign = campaign_with_warmup(
        tmp_path / "frozen", sealed_snapshot_input(15), warmup=4
    )
    unknown = write_json(tmp_path / "round-unknown.json", round_input(budget=10))
    result = open_round(frozen_project, store, frozen_campaign["campaign_id"], unknown)
    assert result.returncode == 2
    assert "error:" in result.stderr
    assert result.stdout.strip() == ""

    bad_mode = write_json(
        tmp_path / "round-mode.json",
        round_input(
            scoring_policy={
                "mode": "rolling_windows",
                "data_availability": "next_open",
                "consecutive_sessions": False,
            }
        ),
    )
    result = open_round(frozen_project, store, frozen_campaign["campaign_id"], bad_mode)
    assert result.returncode == 2
    assert "error:" in result.stderr


# ---------------------------------------------------------------------------
# round submit
# ---------------------------------------------------------------------------


def test_round_submit_locks_a_successful_discovery_version(tmp_path: Path) -> None:
    project, _, campaign = campaign_with_warmup(tmp_path, sealed_snapshot_input(15), warmup=4)
    store = tmp_path / "trusted"
    opened = ok(
        open_round(
            project,
            store,
            campaign["campaign_id"],
            write_json(tmp_path / "round.json", round_input()),
        )
    )
    round_id = opened["round_id"]
    experiment = submit_experiment(
        project,
        campaign["campaign_id"],
        tmp_path / "experiment.json",
        "short",
        "A short lookback is a legal synthetic version.",
        "momentum",
        {"lookback": 2, "cash_if_negative": False},
    )
    source = write_json(tmp_path / "version.json", version_input(experiment["experiment_id"]))

    submitted = ok(submit_version(project, round_id, "submission-1", source))
    assert submitted["schema_version"] == 1
    assert submitted["status"] == "submitted"
    assert submitted["reused"] is False
    assert submitted["round_id"] == round_id
    assert submitted["submission_id"] == "submission-1"
    assert submitted["experiment_id"] == experiment["experiment_id"]
    assert_hex(submitted["version_id"])
    assert (
        submitted["strategy_fingerprint"]
        == experiment["fingerprints"]["strategy_and_parameters"]
    )
    locked = submitted["locked"]
    assert locked["snapshot_id"] == campaign["snapshot_id"]
    assert locked["snapshot_hash"] == campaign["fingerprints"]["snapshot"]
    assert locked["evaluation_hash"] == campaign["fingerprints"]["evaluation"]
    assert locked["execution_rules"] == campaign["execution_rules"]
    assert locked["base_costs"] == {
        "initial_cash": 100_000.0,
        "transaction_cost_bps": 5.0,
        "slippage_bps": 0.0,
    }
    assert locked["history_policy"] == campaign["history_policy"]
    assert locked["signal_window_sessions"] == 3
    assert_hex(locked["protocol_hash"])

    replayed = ok(submit_version(project, round_id, "submission-1", source))
    assert replayed["reused"] is True
    assert replayed["version_id"] == submitted["version_id"]

    conflict_source = write_json(
        tmp_path / "version-conflict.json",
        version_input(experiment["experiment_id"], change="A different change."),
    )
    rejected(
        submit_version(project, round_id, "submission-1", conflict_source),
        "SUBMISSION_CONFLICT",
    )

    # Content-addressed: identical content under a new submission ID is the same version.
    duplicate = ok(submit_version(project, round_id, "submission-2", source))
    assert duplicate["version_id"] == submitted["version_id"]
    assert duplicate["reused"] is True

    missing = write_json(
        tmp_path / "version-missing.json",
        {key: value for key, value in version_input(experiment["experiment_id"]).items() if key != "revisit_condition"},
    )
    result = submit_version(project, round_id, "submission-3", missing)
    assert result.returncode == 2
    assert "error:" in result.stderr

    unknown = write_json(tmp_path / "version-unknown.json", version_input("exp-unknown"))
    rejected(
        submit_version(project, round_id, "submission-4", unknown),
        "EXPERIMENT_NOT_ELIGIBLE",
    )

    failed_experiment = json.loads(
        submit_strategy_result(
            project,
            campaign["campaign_id"],
            tmp_path / "failed.json",
            "too-long",
            "momentum",
            {"lookback": 4, "cash_if_negative": False},
        ).stdout
    )
    assert failed_experiment["status"] == "failed"
    assert failed_experiment["error"]["code"] == "INSUFFICIENT_HISTORY"
    failed_source = write_json(
        tmp_path / "version-failed.json", version_input(failed_experiment["experiment_id"])
    )
    rejected(
        submit_version(project, round_id, "submission-5", failed_source),
        "EXPERIMENT_NOT_ELIGIBLE",
    )

    status = ok(round_status(project, round_id))
    assert len(status["submissions"]) == 1
    assert status["submissions"][0]["version_id"] == submitted["version_id"]
    assert status["submissions"][0]["validation"] == {"status": "not_run", "attempts": 0}
    assert status["submissions"][0]["review_state"] == "pending"


# ---------------------------------------------------------------------------
# round validate: warmup regression R1 (218 scoring days, generous prior history)
# ---------------------------------------------------------------------------


def test_round_validate_replays_218_scoring_days_without_protected_access(
    tmp_path: Path,
) -> None:
    payload = sealed_snapshot_input(425)
    payload["discovery_start"] = payload["calendar"][61]["date"]
    project, snapshots, campaign = campaign_with_warmup(
        tmp_path, payload, warmup=0, attempts=6, max_seconds=300
    )
    scoring_start = campaign["history_policy"]["scoring_start"]
    assert campaign["history_policy"]["warmup_sessions"] == 0
    assert campaign["history_policy"]["prior_history_hash"]
    short = submit_strategy(
        project,
        campaign["campaign_id"],
        tmp_path / "short.json",
        "short-20",
        "momentum",
        {"lookback": 20, "cash_if_negative": False},
    )
    long = submit_strategy(
        project,
        campaign["campaign_id"],
        tmp_path / "long.json",
        "long-60",
        "momentum",
        {"lookback": 60, "cash_if_negative": False},
    )
    for experiment in (short, long):
        discovery = experiment["discovery"]
        assert discovery["start_date"] == scoring_start
        assert discovery["end_date"] == payload["calendar"][278]["date"]
        assert discovery["metrics"]["sessions"] == 218
        assert discovery["metrics"]["valued_sessions"] == 218
        assert discovery["baseline_comparison"]["baseline"]["start_date"] == scoring_start
        assert discovery["metrics"]["fills"] > 0

    opened = ok(
        open_round(
            project,
            (tmp_path / "trusted"),
            campaign["campaign_id"],
            write_json(tmp_path / "round.json", round_input()),
        )
    )
    round_id = opened["round_id"]
    versions = [
        lock_version(tmp_path, project, round_id, f"version-{name}", experiment["experiment_id"])
        for name, experiment in (("short", short), ("long", long))
    ]

    # No protected partition may be opened: make every sealed protected payload
    # unreadable and still expect a completed lightweight Validation.
    snapshot_dir = snapshots / campaign["snapshot_id"]
    protected = [snapshot_dir / f"{name}.json" for name in ("validation", "final_holdout")]
    for path in protected:
        path.chmod(0o000)
    try:
        receipts = []
        for version, experiment in zip(versions, (short, long), strict=True):
            receipt = ok(
                validate_version(
                    project,
                    tmp_path / "trusted",
                    snapshots,
                    round_id,
                    version["version_id"],
                )
            )
            receipts.append((receipt, experiment))
    finally:
        for path in protected:
            path.chmod(0o444)

    for (receipt, experiment), version in zip(receipts, versions, strict=True):
        assert receipt["schema_version"] == 1
        assert receipt["status"] == "completed"
        assert receipt["outcome"] == "passed"
        assert receipt["reason"] is None
        assert receipt["round_id"] == round_id
        assert receipt["version_id"] == version["version_id"]
        assert_hex(receipt["validation_id"])
        assert_hex(receipt["receipt_hash"])
        assert receipt["opened_protected_source"] is False
        assert receipt["query_debited"] is False
        assert receipt["attempts"] == 1
        assert receipt["resumed"] is False
        assert_checks_met(receipt)
        assert receipt["checks"]["coverage"]["expected"]["sessions"] == 218
        assert receipt["checks"]["coverage"]["expected"]["start"] == scoring_start
        assert receipt["checks"]["scoring_start"]["candidate"]["first_scored_open"]["ready"] is True
        assert_replay_matches_discovery(receipt, experiment)
        assert receipt["metrics"]["candidate"]["sessions"] == 218
        assert receipt["metrics"]["candidate"]["turnover"] > 0
        observation = receipt["observation"]
        assert observation["recommended_sessions"] == 252
        assert observation["basis"] == {
            "opportunities": 1,
            "rebalance_sessions": [scoring_start],
            "sessions": 218,
            "minimum_opportunities": 2,
            "formula": (
                "ceil(sessions / max(1, opportunities)) * minimum_opportunities, "
                "clamped to [minimum_opportunities, 252]"
            ),
        }
        assert observation["review_conditions"] == round_input()["observation"]["review_conditions"]
        trusted_copy = (
            tmp_path / "trusted" / "research-loop-validations" / f"{receipt['validation_id']}.json"
        )
        stored = json.loads(trusted_copy.read_text(encoding="utf-8"))
        assert stored["receipt_hash"] == receipt["receipt_hash"]
        # Mutable attempt provenance is not part of the immutable receipt content.
        assert "attempts" not in stored and "resumed" not in stored

    # Lightweight Validation never debits a protected query or writes a record.
    trusted = tmp_path / "trusted"
    assert not (trusted / "queries.sqlite").exists()
    assert not (trusted / "records").exists()

    # The same validation identity returns the stored terminal receipt, unchanged.
    version_id = versions[0]["version_id"]
    trusted_copy = (
        trusted / "research-loop-validations" / f"{receipts[0][0]['validation_id']}.json"
    )
    before = trusted_copy.read_bytes()
    replayed = ok(
        validate_version(
            project, trusted, snapshots, round_id, version_id
        )
    )
    assert replayed["reused"] is True
    assert replayed["validation_id"] == receipts[0][0]["validation_id"]
    assert replayed["receipt_hash"] == receipts[0][0]["receipt_hash"]
    assert replayed["attempts"] == 1
    assert trusted_copy.read_bytes() == before

    status = ok(round_status(project, round_id))
    assert status["status"] == "open"
    assert len(status["submissions"]) == 2
    for item in status["submissions"]:
        assert item["validation"]["status"] == "completed"
        assert item["validation"]["outcome"] == "passed"
        assert item["validation"]["attempts"] == 1


# ---------------------------------------------------------------------------
# round validate: interruption recovery and write-once identity
# ---------------------------------------------------------------------------


def test_round_validate_resumes_an_interrupted_attempt_without_a_new_identity(
    tmp_path: Path,
) -> None:
    project, snapshots, campaign = campaign_with_warmup(tmp_path, sealed_snapshot_input(15), warmup=4)
    trusted = tmp_path / "trusted"
    opened = ok(
        open_round(
            project,
            trusted,
            campaign["campaign_id"],
            write_json(tmp_path / "round.json", round_input()),
        )
    )
    round_id = opened["round_id"]
    short = submit_experiment(
        project, campaign["campaign_id"], tmp_path / "short.json", "short",
        "Short lookback.", "momentum", {"lookback": 2, "cash_if_negative": False},
    )
    long = submit_experiment(
        project, campaign["campaign_id"], tmp_path / "long.json", "long",
        "Longer lookback.", "momentum", {"lookback": 3, "cash_if_negative": False},
    )
    first = lock_version(tmp_path, project, round_id, "version-first", short["experiment_id"])
    second = lock_version(tmp_path, project, round_id, "version-second", long["experiment_id"])

    completed = ok(
        validate_version(project, trusted, snapshots, round_id, first["version_id"])
    )
    assert completed["outcome"] == "passed"
    assert completed["attempts"] == 1

    interrupted = validate_version(
        project,
        trusted,
        snapshots,
        round_id,
        second["version_id"],
        env=cli_env(PRAJNATRADE_TEST_FAILPOINT=VALIDATION_FAILPOINT),
    )
    assert interrupted.returncode == 2, (interrupted.stdout, interrupted.stderr)
    assert "error:" in interrupted.stderr
    assert interrupted.stdout.strip() == ""

    status = ok(round_status(project, round_id))
    running = next(
        item for item in status["submissions"] if item["version_id"] == second["version_id"]
    )
    assert running["validation"]["status"] == "running"
    assert running["validation"]["attempts"] == 1
    assert_hex(running["validation"]["validation_id"])

    resumed = ok(validate_version(project, trusted, snapshots, round_id, second["version_id"]))
    assert resumed["status"] == "completed"
    assert resumed["outcome"] == "passed"
    assert resumed["reused"] is False
    assert resumed["validation_id"] == running["validation"]["validation_id"]
    assert resumed["attempts"] == 2
    assert resumed["resumed"] is True

    replay = ok(validate_version(project, trusted, snapshots, round_id, second["version_id"]))
    assert replay["reused"] is True
    assert replay["validation_id"] == resumed["validation_id"]
    assert replay["receipt_hash"] == resumed["receipt_hash"]
    assert replay["attempts"] == 2


def test_round_validate_recovers_a_terminal_receipt_without_a_second_record(
    tmp_path: Path,
) -> None:
    project, snapshots, campaign = campaign_with_warmup(
        tmp_path, sealed_snapshot_input(15), warmup=4
    )
    trusted = tmp_path / "trusted"
    opened = ok(
        open_round(
            project,
            trusted,
            campaign["campaign_id"],
            write_json(tmp_path / "round.json", round_input()),
        )
    )
    round_id = opened["round_id"]
    experiment = submit_experiment(
        project, campaign["campaign_id"], tmp_path / "short.json", "short",
        "Short lookback.", "momentum", {"lookback": 2, "cash_if_negative": False},
    )
    version = lock_version(
        tmp_path, project, round_id, "version-first", experiment["experiment_id"]
    )

    crashed = validate_version(
        project,
        trusted,
        snapshots,
        round_id,
        version["version_id"],
        env=cli_env(PRAJNATRADE_TEST_FAILPOINT=VALIDATION_TERMINAL_FAILPOINT),
    )
    assert crashed.returncode == 2, (crashed.stdout, crashed.stderr)
    assert "error:" in crashed.stderr
    validations = trusted / "research-loop-validations"
    stored_files = list(validations.glob("*.json"))
    assert len(stored_files) == 1
    stored = json.loads(stored_files[0].read_text(encoding="utf-8"))
    assert stored["status"] == "completed"
    assert "attempts" not in stored and "resumed" not in stored
    first_validation_id = stored["validation_id"]
    first_receipt_hash = stored["receipt_hash"]
    running = next(
        item
        for item in ok(round_status(project, round_id))["submissions"]
        if item["version_id"] == version["version_id"]
    )
    assert running["validation"]["status"] == "running"
    assert running["validation"]["attempts"] == 1

    recovered = ok(
        validate_version(project, trusted, snapshots, round_id, version["version_id"])
    )
    assert recovered["validation_id"] == first_validation_id
    assert recovered["receipt_hash"] == first_receipt_hash
    assert recovered["attempts"] == 2
    assert recovered["resumed"] is True
    assert recovered["reused"] is False
    assert recovered["outcome"] == stored["outcome"]
    # The trusted record is never duplicated or rewritten by the recovery.
    assert list(validations.glob("*.json")) == stored_files
    assert json.loads(stored_files[0].read_text(encoding="utf-8")) == stored
    published = next(
        item
        for item in ok(round_status(project, round_id))["submissions"]
        if item["version_id"] == version["version_id"]
    )
    assert published["validation"]["status"] == "completed"
    assert published["validation"]["attempts"] == 2
    assert published["validation"]["validation_id"] == first_validation_id

    replay = ok(
        validate_version(project, trusted, snapshots, round_id, version["version_id"])
    )
    assert replay["reused"] is True
    assert replay["attempts"] == 2
    assert replay["validation_id"] == first_validation_id
    assert replay["receipt_hash"] == first_receipt_hash


# ---------------------------------------------------------------------------
# round validate: warmup regression R2 (no prior history, unified start at day 61)
# ---------------------------------------------------------------------------


def test_round_validate_starts_short_long_and_benchmark_at_day_61(tmp_path: Path) -> None:
    payload = trending_snapshot(102)
    project, snapshots, campaign = campaign_with_warmup(tmp_path, payload, warmup=60)
    scoring_start = campaign["history_policy"]["scoring_start"]
    assert scoring_start == payload["calendar"][60]["date"]
    short = submit_strategy(
        project, campaign["campaign_id"], tmp_path / "short.json", "short-20",
        "momentum", {"lookback": 20, "cash_if_negative": False},
    )
    long = submit_strategy(
        project, campaign["campaign_id"], tmp_path / "long.json", "long-60",
        "trend_filter", {"lookback": 60},
    )
    for experiment in (short, long):
        discovery = experiment["discovery"]
        assert discovery["start_date"] == scoring_start
        assert discovery["metrics"]["sessions"] == 1
        assert discovery["baseline_comparison"]["baseline"]["start_date"] == scoring_start
        assert discovery["baseline_comparison"]["baseline"]["metrics"]["sessions"] == 1
        assert discovery["history_readiness"]["first_ready_date"] == scoring_start

    trusted = tmp_path / "trusted"
    opened = ok(
        open_round(
            project,
            trusted,
            campaign["campaign_id"],
            write_json(tmp_path / "round.json", round_input()),
        )
    )
    round_id = opened["round_id"]
    for name, experiment in (("short", short), ("long", long)):
        version = lock_version(tmp_path, project, round_id, f"version-{name}", experiment["experiment_id"])
        receipt = ok(validate_version(project, trusted, snapshots, round_id, version["version_id"]))
        assert receipt["outcome"] == "passed", receipt["checks"]
        assert receipt["replay"]["candidate"]["start_date"] == scoring_start
        assert receipt["replay"]["benchmark"]["start_date"] == scoring_start
        assert receipt["replay"]["candidate"]["sessions"] == 1
        assert receipt["checks"]["scoring_start"]["ok"] is True
        assert receipt["checks"]["position"]["ok"] is True
        assert_replay_matches_discovery(receipt, experiment)


def test_round_validate_backfills_valid_observations_where_consecutive_sessions_gap(
    tmp_path: Path,
) -> None:
    payload = sealed_snapshot_input(107)
    suspended = suspend_derived(payload, 9)
    project, snapshots, campaign = campaign_with_warmup(tmp_path, payload, warmup=61)
    scoring_start = campaign["history_policy"]["scoring_start"]
    assert scoring_start == payload["calendar"][61]["date"]

    valid = submit_strategy(
        project, campaign["campaign_id"], tmp_path / "valid.json", "valid-60",
        "trend_filter", {"lookback": 60},
        requirements=[{"id": "trend", "observations": 60, "mode": "valid_observations"}],
    )
    assert valid["discovery"]["start_date"] == scoring_start
    assert valid["discovery"]["history_readiness"]["first_scored_open"]["ready"] is True

    consecutive = json.loads(
        submit_strategy_result(
            project, campaign["campaign_id"], tmp_path / "consecutive.json", "consecutive-60",
            "trend_filter", {"lookback": 60},
            requirements=[{"id": "trend", "observations": 60, "mode": "consecutive_sessions"}],
        ).stdout
    )
    assert consecutive["status"] == "failed"
    assert consecutive["error"]["code"] == "INSUFFICIENT_HISTORY"

    trusted = tmp_path / "trusted"
    opened = ok(
        open_round(
            project,
            trusted,
            campaign["campaign_id"],
            write_json(tmp_path / "round.json", round_input()),
        )
    )
    version = lock_version(
        tmp_path, project, opened["round_id"], "version-valid", valid["experiment_id"]
    )
    receipt = ok(
        validate_version(project, trusted, snapshots, opened["round_id"], version["version_id"])
    )
    assert receipt["outcome"] == "passed", receipt["checks"]
    readiness = receipt["checks"]["scoring_start"]["candidate"]["first_scored_open"]
    requirement = readiness["requirements"][0]
    assert requirement["mode"] == "valid_observations"
    assert requirement["required_observations"] == 60
    assert requirement["available_observations"] == 60
    assert requirement["missing_sessions"] == [
        {"date": suspended, "missing_symbols": sorted(SYMBOLS)}
    ]


# ---------------------------------------------------------------------------
# round validate: metrics-not-ready vs intentional cash vs ordered-unfilled
# ---------------------------------------------------------------------------


def test_round_validate_reports_metrics_not_ready_after_a_scoring_gap(
    tmp_path: Path,
) -> None:
    payload = sealed_snapshot_input(200)
    suspended = suspend_derived(payload, 70)
    project, snapshots, campaign = campaign_with_warmup(tmp_path, payload, warmup=61)
    experiment = submit_strategy(
        project, campaign["campaign_id"], tmp_path / "consecutive.json", "consecutive-60",
        "trend_filter", {"lookback": 60},
        requirements=[{"id": "trend", "observations": 60, "mode": "consecutive_sessions"}],
    )
    assert experiment["status"] == "succeeded"
    trusted = tmp_path / "trusted"
    opened = ok(
        open_round(
            project,
            trusted,
            campaign["campaign_id"],
            write_json(
                tmp_path / "round.json",
                round_input(
                    scoring_policy={
                        "mode": "consecutive_sessions",
                        "data_availability": "next_open",
                        "consecutive_sessions": True,
                    }
                ),
            ),
        )
    )
    version = lock_version(
        tmp_path, project, opened["round_id"], "version-gap", experiment["experiment_id"]
    )
    receipt = ok(
        validate_version(project, trusted, snapshots, opened["round_id"], version["version_id"])
    )
    assert receipt["status"] == "completed"
    assert receipt["outcome"] == "insufficient_evidence"
    assert receipt["reason"] == "COVERAGE_INSUFFICIENT"
    assert receipt["checks"]["scoring_start"]["ok"] is True
    availability = receipt["checks"]["data_availability"]
    assert availability["ok"] is False
    gaps = availability["candidate"]["signal_gaps"]
    assert gaps["count"] >= 1
    assert gaps["dates"][0] == payload["calendar"][71]["date"]
    first = gaps["first"]
    assert first["reason"] == "required_total_return_window_unavailable"
    requirement = first["requirements"][0]
    assert requirement["mode"] == "consecutive_sessions"
    assert requirement["available_observations"] < requirement["required_observations"]
    assert suspended in [item["date"] for item in first["missing_sessions"]]


def test_round_validate_treats_intentional_cash_as_position_evidence(
    tmp_path: Path,
) -> None:
    project, snapshots, campaign = campaign_with_warmup(tmp_path, sealed_snapshot_input(15), warmup=4)
    experiment = submit_experiment(
        project, campaign["campaign_id"], tmp_path / "cash.json", "cash",
        "Flat synthetic prices keep the momentum rule in cash.", "momentum",
        {"lookback": 2, "cash_if_negative": True},
    )
    trusted = tmp_path / "trusted"
    opened = ok(
        open_round(
            project,
            trusted,
            campaign["campaign_id"],
            write_json(tmp_path / "round.json", round_input()),
        )
    )
    version = lock_version(
        tmp_path, project, opened["round_id"], "version-cash", experiment["experiment_id"]
    )
    receipt = ok(
        validate_version(project, trusted, snapshots, opened["round_id"], version["version_id"])
    )
    assert receipt["outcome"] == "passed", receipt["checks"]
    assert receipt["reason"] is None
    position = receipt["checks"]["position"]
    assert position["ok"] is True
    assert position["candidate"]["state"] == "intentional_cash"
    sessions = receipt["metrics"]["candidate"]["sessions"]
    assert sessions == 5
    assert len(position["candidate"]["intentional_cash_sessions"]) == sessions
    assert position["candidate"]["fills"] == 0
    assert receipt["metrics"]["candidate"]["fills"] == 0


def test_round_validate_records_ready_cash_then_a_day_ten_buy(tmp_path: Path) -> None:
    """Spec #43 L57: ready but cash-scored for nine days, then it actively buys."""
    payload = sealed_snapshot_input(40)
    index = {
        row["date"]: position for position, row in enumerate(payload["calendar"])
    }
    for row in payload["derived"]:
        position = index[row["date"]]
        row["value"] = 10.0 if position < 12 else 10.5 + 0.5 * (position - 12)
    project, snapshots, campaign = campaign_with_warmup(tmp_path, payload, warmup=4)
    scoring_start = campaign["history_policy"]["scoring_start"]
    dates = [row["date"] for row in payload["calendar"]]
    assert dates.index(scoring_start) == 4
    experiment = submit_experiment(
        project, campaign["campaign_id"], tmp_path / "late.json", "late",
        "Flat synthetic prices keep momentum in cash until the tenth scored session.",
        "momentum", {"lookback": 2, "cash_if_negative": True},
    )
    discovery = experiment["discovery"]
    assert discovery["metrics"]["sessions"] == 20
    assert discovery["metrics"]["valued_sessions"] == 20
    assert discovery["metrics"]["fills"] == 1
    assert discovery["position_evidence"]["state"] == "initial_position_established"
    assert len(discovery["history_readiness"]["ready_cash_sessions"]) == 9

    trusted = tmp_path / "trusted"
    opened = ok(
        open_round(
            project,
            trusted,
            campaign["campaign_id"],
            write_json(tmp_path / "round.json", round_input()),
        )
    )
    version = lock_version(
        tmp_path, project, opened["round_id"], "version-late", experiment["experiment_id"]
    )
    receipt = ok(
        validate_version(project, trusted, snapshots, opened["round_id"], version["version_id"])
    )
    assert receipt["outcome"] == "passed", receipt["checks"]
    assert receipt["reason"] is None
    assert receipt["metrics"]["candidate"]["sessions"] == 20
    assert receipt["metrics"]["candidate"]["valued_sessions"] == 20
    assert receipt["metrics"]["candidate"]["fills"] == 1
    position = receipt["checks"]["position"]["candidate"]
    assert position["state"] == "initial_position_established"
    cash_sessions = position["intentional_cash_sessions"]
    assert cash_sessions == [
        dates[4],
        dates[5],
        dates[6],
        dates[7],
        dates[8],
        dates[9],
        dates[10],
        dates[11],
        dates[12],
    ]
    # The tenth scored session decides at its open from the prior close history
    # and buys in that same session; the first nine scored sessions hold cash.
    assert position["first_position_signal_date"] == dates[13]
    assert position["first_fill_date"] == dates[13]
    assert receipt["replay"]["matches_discovery"] is True


def test_round_validate_marks_ordered_unfilled_as_insufficient_evidence(
    tmp_path: Path,
) -> None:
    payload = sealed_snapshot_input(102)
    halted = unexecutable_opens(payload, 60)
    project, snapshots, campaign = campaign_with_warmup(tmp_path, payload, warmup=60)
    scoring_start = campaign["history_policy"]["scoring_start"]
    assert halted == scoring_start
    experiment = submit_experiment(
        project, campaign["campaign_id"], tmp_path / "equal.json", "equal",
        "Equal weights order once.", "equal_weight", {},
    )
    discovery = experiment["discovery"]
    assert discovery["metrics"]["fills"] == 0
    assert discovery["position_evidence"]["state"] == "initial_position_not_established"
    assert discovery["order_attempts"][0]["status"] == "not_filled"

    trusted = tmp_path / "trusted"
    opened = ok(
        open_round(
            project,
            trusted,
            campaign["campaign_id"],
            write_json(tmp_path / "round.json", round_input()),
        )
    )
    version = lock_version(
        tmp_path, project, opened["round_id"], "version-unfilled", experiment["experiment_id"]
    )
    receipt = ok(
        validate_version(project, trusted, snapshots, opened["round_id"], version["version_id"])
    )
    assert receipt["outcome"] == "insufficient_evidence"
    assert receipt["reason"] == "POSITION_EVIDENCE_INSUFFICIENT"
    position = receipt["checks"]["position"]
    assert position["ok"] is False
    assert position["candidate"]["state"] == "initial_position_not_established"
    assert position["candidate"]["fills"] == 0
    assert position["candidate"]["unfilled_orders"] == 1
    assert position["candidate"]["first_fill_date"] is None
    assert receipt["replay"]["matches_discovery"] is True


# ---------------------------------------------------------------------------
# round validate: ending cash is part of the Discovery replay comparison
# ---------------------------------------------------------------------------


def test_round_validate_compares_end_cash_and_rejects_a_cash_only_mismatch(
    tmp_path: Path,
) -> None:
    project, snapshots, campaign = campaign_with_warmup(
        tmp_path, sealed_snapshot_input(15), warmup=4
    )
    experiment = submit_experiment(
        project, campaign["campaign_id"], tmp_path / "short.json", "short",
        "Short lookback.", "momentum", {"lookback": 2, "cash_if_negative": False},
    )
    trusted = tmp_path / "trusted"
    opened = ok(
        open_round(
            project,
            trusted,
            campaign["campaign_id"],
            write_json(tmp_path / "round.json", round_input()),
        )
    )
    version = lock_version(
        tmp_path, project, opened["round_id"], "version-cash", experiment["experiment_id"]
    )

    # Tamper only the stored Discovery ending cash (its ending equity, fills and
    # metrics stay identical), so a cash-blind replay would still pass.
    database = project / ".prajnatrade" / "project.sqlite"
    with sqlite3.connect(database) as connection:
        raw = connection.execute(
            "SELECT result_json FROM experiments WHERE experiment_id = ?",
            (experiment["experiment_id"],),
        ).fetchone()[0]
        stored = json.loads(raw)
        curve = stored["discovery"]["equity_curve"]
        assert curve[-1]["cash"] is not None
        curve[-1]["cash"] = float(curve[-1]["cash"]) + 1.0
        connection.execute(
            "UPDATE experiments SET result_json = ? WHERE experiment_id = ?",
            (json.dumps(stored), experiment["experiment_id"]),
        )

    receipt = ok(
        validate_version(
            project, trusted, snapshots, opened["round_id"], version["version_id"]
        )
    )
    assert receipt["outcome"] == "insufficient_evidence"
    assert receipt["reason"] == "REPLAY_MISMATCH"
    assert receipt["checks"]["replay"]["ok"] is False
    assert receipt["replay"]["mismatches"] == ["candidate.ending_cash"]
    for label in ("candidate", "benchmark"):
        summary = receipt["replay"][label]
        assert "ending_cash" in summary
        assert summary["ending_cash"] is not None
    assert receipt["replay"]["candidate"]["ending_cash"] != pytest.approx(
        curve[-1]["cash"], abs=1e-9
    )


# ---------------------------------------------------------------------------
# round validate: hard conditions and verdicts
# ---------------------------------------------------------------------------


def test_round_validate_marks_a_hard_condition_failure(tmp_path: Path) -> None:
    project, snapshots, campaign = campaign_with_warmup(tmp_path, sealed_snapshot_input(15), warmup=4)
    experiment = submit_experiment(
        project, campaign["campaign_id"], tmp_path / "short.json", "short",
        "Short lookback.", "momentum", {"lookback": 2, "cash_if_negative": False},
    )
    trusted = tmp_path / "trusted"
    conditions = [
        {"id": "impossible-return", "metric": "net_return", "operator": ">=", "value": 1.0},
    ]
    opened = ok(
        open_round(
            project,
            trusted,
            campaign["campaign_id"],
            write_json(tmp_path / "round.json", round_input(hard_conditions=conditions)),
        )
    )
    version = lock_version(
        tmp_path, project, opened["round_id"], "version-failed", experiment["experiment_id"]
    )
    receipt = ok(
        validate_version(project, trusted, snapshots, opened["round_id"], version["version_id"])
    )
    assert receipt["status"] == "completed"
    assert receipt["outcome"] == "failed"
    assert receipt["reason"] == "HARD_CONDITION_NOT_MET"
    [condition] = receipt["checks"]["hard_conditions"]
    assert condition["id"] == "impossible-return"
    assert condition["status"] == "failed"
    assert condition["operator"] == ">="
    assert condition["value"] == 1.0
    assert condition["observed"] < 1.0
    assert receipt["checks"]["replay"]["ok"] is True


def test_round_validate_reports_an_unavailable_hard_metric_as_insufficient_evidence(
    tmp_path: Path,
) -> None:
    project, snapshots, campaign = campaign_with_warmup(
        tmp_path, trending_snapshot(102), warmup=60
    )
    experiment = submit_strategy(
        project, campaign["campaign_id"], tmp_path / "short.json", "short-20",
        "momentum", {"lookback": 20, "cash_if_negative": False},
    )
    trusted = tmp_path / "trusted"
    opened = ok(
        open_round(
            project,
            trusted,
            campaign["campaign_id"],
            write_json(
                tmp_path / "round.json",
                round_input(
                    hard_conditions=[
                        {"id": "sharpe", "metric": "sharpe", "operator": ">=", "value": 0.0},
                    ]
                ),
            ),
        )
    )
    version = lock_version(
        tmp_path, project, opened["round_id"], "version-sharpe", experiment["experiment_id"]
    )
    receipt = ok(
        validate_version(project, trusted, snapshots, opened["round_id"], version["version_id"])
    )
    assert receipt["outcome"] == "insufficient_evidence"
    assert receipt["reason"] == "METRICS_NOT_READY"
    [condition] = receipt["checks"]["hard_conditions"]
    assert condition["status"] == "unavailable"
    assert condition["observed"] is None


# ---------------------------------------------------------------------------
# round validate: data visible only after the first decision
# ---------------------------------------------------------------------------


def _late_visibility_campaign(
    root: Path, *, late_value: float, name: str
) -> tuple[Path, Path, dict]:
    payload = sealed_snapshot_input(70)
    scoring_day = payload["calendar"][22]["date"]
    for row in payload["derived"]:
        if row["date"] == scoring_day and row["symbol"] == SYMBOLS[1]:
            row["value"] = late_value
            row["available_at"] = f"{scoring_day}T15:00:00+08:00"
    return campaign_with_warmup(root, payload, warmup=22, name=name)


def test_round_validate_ignores_data_visible_only_after_the_first_decision(
    tmp_path: Path,
) -> None:
    receipts = []
    experiments = []
    for name, value in (("baseline", 10.0), ("late", 99.0)):
        project, snapshots, campaign = _late_visibility_campaign(
            tmp_path, late_value=value, name=name
        )
        experiment = submit_strategy(
            project, campaign["campaign_id"], tmp_path / f"{name}-experiment.json", name,
            "momentum", {"lookback": 20, "cash_if_negative": False},
        )
        trusted = tmp_path / f"{name}-trusted"
        opened = ok(
            open_round(
                project,
                trusted,
                campaign["campaign_id"],
                write_json(tmp_path / f"{name}-round.json", round_input()),
            )
        )
        version = lock_version(
            tmp_path, project, opened["round_id"], f"{name}-version", experiment["experiment_id"]
        )
        receipt = ok(
            validate_version(project, trusted, snapshots, opened["round_id"], version["version_id"])
        )
        assert receipt["outcome"] == "passed", receipt["checks"]
        receipts.append(receipt)
        experiments.append(experiment)

    first, second = receipts
    assert first["replay"]["candidate"]["first_decision"] == second["replay"]["candidate"]["first_decision"]
    assert first["replay"]["candidate"]["first_fill"] == second["replay"]["candidate"]["first_fill"]
    # The difference is visible from the next decision onward, so the runs diverge.
    assert first["replay"]["candidate"]["fills"] != second["replay"]["candidate"]["fills"]
    assert first["replay"]["candidate"]["ending_equity"] != second["replay"]["candidate"]["ending_equity"]
    for receipt in receipts:
        assert receipt["checks"]["replay"]["ok"] is True


# ---------------------------------------------------------------------------
# round close
# ---------------------------------------------------------------------------


def test_round_close_records_an_early_stop_and_blocks_new_work(tmp_path: Path) -> None:
    project, snapshots, campaign = campaign_with_warmup(tmp_path, sealed_snapshot_input(15), warmup=4)
    experiment = submit_experiment(
        project, campaign["campaign_id"], tmp_path / "short.json", "short",
        "Short lookback.", "momentum", {"lookback": 2, "cash_if_negative": False},
    )
    another = submit_experiment(
        project, campaign["campaign_id"], tmp_path / "other.json", "other",
        "Another short lookback.", "momentum", {"lookback": 3, "cash_if_negative": False},
    )
    assert another["budget"]["attempts_remaining"] == 4
    trusted = tmp_path / "trusted"
    opened = ok(
        open_round(
            project,
            trusted,
            campaign["campaign_id"],
            write_json(tmp_path / "round.json", round_input()),
        )
    )
    round_id = opened["round_id"]
    version = lock_version(tmp_path, project, round_id, "version-one", experiment["experiment_id"])
    close_input = {
        "stop_reason": "No remaining testable version could change the choice.",
        "selected_version": None,
        "next_experiment": None,
    }
    source = write_json(tmp_path / "close.json", close_input)
    closed = ok(close_round(project, round_id, source))
    assert closed["schema_version"] == 1
    assert closed["status"] == "closed"
    assert closed["reused"] is False
    assert closed["round_id"] == round_id
    assert_hex(closed["closure_id"])
    assert closed["candidate_selected"] is False
    assert closed["selected_version"] is None
    assert closed["stop_reason"] == close_input["stop_reason"]
    assert closed["budget"] == {"attempts": 6, "max_seconds": 180}

    replayed = ok(close_round(project, round_id, source))
    assert replayed["reused"] is True
    assert replayed["closure_id"] == closed["closure_id"]

    conflict = write_json(
        tmp_path / "close-other.json", {**close_input, "stop_reason": "A different stop reason."}
    )
    rejected(close_round(project, round_id, conflict), "ROUND_CLOSED")

    status = ok(round_status(project, round_id))
    assert status["status"] == "closed"
    assert status["closure"]["closure_id"] == closed["closure_id"]
    assert status["closure"]["candidate_selected"] is False

    source_version = write_json(
        tmp_path / "version-two.json", version_input(another["experiment_id"])
    )
    rejected(submit_version(project, round_id, "version-two", source_version), "ROUND_CLOSED")
    rejected(
        validate_version(project, trusted, snapshots, round_id, version["version_id"]),
        "ROUND_CLOSED",
    )


# ---------------------------------------------------------------------------
# agent boundary
# ---------------------------------------------------------------------------


def test_agent_round_surface_is_status_and_submit_only(tmp_path: Path) -> None:
    project, _, campaign = campaign_with_warmup(tmp_path, sealed_snapshot_input(15), warmup=4)
    trusted = tmp_path / "trusted"
    source = write_json(tmp_path / "round.json", round_input())

    for args in (
        (
            "round", "open", "--project", str(project), "--store", str(trusted),
            "--campaign-id", campaign["campaign_id"], "--direction-key", "synthetic-trend",
            "--name", "round-one", "--authorization-id", "authorization-1",
            "--input", str(source),
        ),
        (
            "round", "validate", "--project", str(project), "--store", str(trusted),
            "--snapshot-store", str(tmp_path), "--round-id", "a" * 64,
            "--version-id", "b" * 64,
        ),
        (
            "round", "close", "--project", str(project), "--round-id", "a" * 64,
            "--reviewer", "lead", "--reason", "stop", "--input", str(source),
        ),
    ):
        result = agent_cli(*args)
        assert result.returncode == 2
        assert "invalid choice" in result.stderr

    # The agent round status takes no trusted store and still reports the round.
    operator = ok(open_round(project, trusted, campaign["campaign_id"], source))
    result = agent_cli(
        "round", "status", "--project", str(project), "--store", str(trusted)
    )
    assert result.returncode == 2
    status = ok(round_status(project, operator["round_id"]))
    assert status["status"] == "open"
    rendered = json.dumps(status)
    assert "equity_curve" not in rendered
    assert "query_debited" not in rendered
    assert "opened_protected_source" not in rendered
