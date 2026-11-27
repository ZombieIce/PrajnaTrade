"""Public-CLI tests for the frozen 70/30 Discovery split and the v2 round protocol.

Ticket #51 (spec #50): ``campaign create --discovery-split 70/30`` freezes a
recomputable split of the Campaign's sealed Discovery interval, copies only the
development slice into the research project, and binds the split into a v2
``round open``/``round submit`` protocol.  Legacy campaigns and rounds keep the
protocol v1 shapes.

Every scenario drives the operator (or agent) CLI through subprocesses on
synthetic sealed snapshots.  Fixture builders are imported from the modules
that own them; nothing is copied.  The split rule itself is public library
surface in ``snapshot.py`` (part of the runtime identity hash set), so one
direct unit test covers its verifier.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest
from cli_helpers import assert_hex, cli, digest, ok, rejected, write_json
from test_experiment_cli import campaign_options, sealed_snapshot_input
from test_protected_eval_cli import submit_experiment
from test_research_loop_cli import (
    open_round,
    round_input,
    round_status,
    submit_version,
    trending_snapshot,
    validate_version,
    version_input,
)

KINDS = ("calendar", "raw_bars", "corporate_actions", "derived")
SPLIT = "70/30"
SPLIT_RULE = "leading-development-share"
SPLIT_BOUNDARY = "floor(development_sessions = sessions * 70 / 100)"
V2_PROTOCOL = {
    "schema_version": 1,
    "name": "trailing-70-30",
    "validation_kind": "trailing_validation",
}
V1_PROTOCOL = {
    "schema_version": 1,
    "name": "discovery-replay",
    "validation_kind": "discovery_replay",
}


# ---------------------------------------------------------------------------
# Independent sealed-payload slicing
# ---------------------------------------------------------------------------


def slice_content(content: dict, start: str, end: str) -> dict:
    """The seal's per-partition content hash convention, computed independently."""
    rows = {
        kind: [row for row in content[kind] if start <= row["date"] <= end]
        for kind in KINDS
    }
    sessions = sorted({row["date"] for row in rows["calendar"]})
    return {
        "start": sessions[0],
        "end": sessions[-1],
        "sessions": len(sessions),
        "content_hash": digest(rows),
    }


def manifest(store: Path, snapshot_id: str) -> dict:
    return ok(cli("snapshot", "manifest", "--store", str(store), "--id", snapshot_id))


def discovery_read(store: Path, snapshot_id: str) -> dict:
    return ok(
        cli(
            "snapshot", "read", "--store", str(store), "--id", snapshot_id,
            "--partition", "discovery",
        )
    )


def partition(manifest_body: dict, name: str) -> dict:
    return next(item for item in manifest_body["partitions"] if item["name"] == name)


def stored_campaign(project: Path) -> tuple[bytes, str, dict]:
    with sqlite3.connect(project / ".prajnatrade" / "project.sqlite") as connection:
        blob, snapshot_hash, evaluation_json = connection.execute(
            "SELECT snapshot, snapshot_hash, evaluation_json FROM campaigns"
        ).fetchone()
    return bytes(blob), snapshot_hash, json.loads(evaluation_json)


def split_campaign(
    root: Path,
    payload: dict,
    *,
    warmup: int | None = 3,
    name: str = "project",
    split: str = SPLIT,
    attempts: int = 6,
    max_seconds: float = 180,
    as_of: str | None = None,
    execution_rules: str | None = None,
    valuation_policy: str | None = None,
    decision_policy: dict | None = None,
) -> tuple[Path, Path, dict]:
    """Initialize a project, seal the payload, and freeze a split Campaign.

    ``execution_rules`` and ``valuation_policy`` are forwarded only when given,
    so every existing caller keeps the Campaign's defaults and its frozen
    records unchanged (#59, #71).
    """
    project = root / name
    source = write_json(root / f"{name}-snapshot.json", payload)
    ok(cli("project", "init", "--project", str(project)))
    arguments = list(campaign_options(project, source, attempts=attempts, max_seconds=max_seconds))
    if warmup is not None:
        arguments += ["--discovery-warmup-sessions", str(warmup)]
    arguments += ["--discovery-split", split]
    if as_of is not None:
        arguments += ["--as-of", as_of]
    if execution_rules is not None:
        arguments += ["--execution-rules", execution_rules]
    if valuation_policy is not None:
        arguments += ["--valuation-policy", valuation_policy]
    if decision_policy is not None:
        arguments += [
            "--decision-policy", decision_policy["version"],
            "--signal-cutoff", decision_policy["signal_cutoff"],
            "--signal-timezone", decision_policy["signal_timezone"],
        ]
        if decision_policy.get("monthly_deferral"):
            arguments.append("--monthly-deferral")
    campaign = ok(cli(*arguments))
    snapshots = project.parent / f"{project.name}-snapshot-store"
    return project, snapshots, campaign


def legacy_campaign(
    root: Path,
    payload: dict,
    *,
    warmup: int = 4,
    name: str = "legacy",
) -> tuple[Path, Path, dict]:
    project = root / name
    source = write_json(root / f"{name}-snapshot.json", payload)
    ok(cli("project", "init", "--project", str(project)))
    campaign = ok(
        cli(
            *campaign_options(project, source, attempts=6, max_seconds=180),
            "--discovery-warmup-sessions",
            str(warmup),
        )
    )
    snapshots = project.parent / f"{project.name}-snapshot-store"
    return project, snapshots, campaign


# ---------------------------------------------------------------------------
# A. The frozen split descriptor
# ---------------------------------------------------------------------------


def test_discovery_split_helper_verifier_rejects_tampering() -> None:
    from prajnatrade.snapshot import discovery_split, verify_discovery_split

    dates = [f"2024-01-{index:02d}" for index in range(1, 11)]
    evidence = {
        "available_at": "2023-12-31T00:00:00+08:00",
        "availability_evidence": "synthetic",
        "collected_at": "2024-02-01T00:00:00+08:00",
    }
    content = {
        "calendar": [{"date": day, **evidence} for day in dates],
        "raw_bars": [
            {"date": day, "symbol": "511260", "close": 10.0, **evidence}
            for day in dates
        ],
        "corporate_actions": [],
        "derived": [
            {"date": day, "symbol": "511260", "value": 10.0, **evidence}
            for day in dates
        ],
    }
    split = discovery_split(content)
    assert split["rule"] == SPLIT_RULE
    assert split["ratio"] == {"development": 70, "validation": 30}
    assert split["boundary"] == SPLIT_BOUNDARY
    assert split["partition"]["sessions"] == 10
    assert split["development"] == slice_content(content, dates[0], dates[6])
    assert split["trailing"] == slice_content(content, dates[7], dates[9])
    body = {key: value for key, value in split.items() if key != "split_hash"}
    assert split["split_hash"] == digest(body)
    verify_discovery_split(split, content=content)

    tampered = deepcopy(split)
    tampered["split_hash"] = digest({**body, "ratio": {"development": 60, "validation": 40}})
    with pytest.raises(ValueError):
        verify_discovery_split(tampered)

    reordered = deepcopy(split)
    reordered["development"]["sessions"] = 8
    with pytest.raises(ValueError):
        verify_discovery_split(reordered)

    changed = deepcopy(content)
    changed["derived"] = changed["derived"][:-1]
    with pytest.raises(ValueError):
        verify_discovery_split(split, content=changed)

    with pytest.raises(ValueError):
        discovery_split(content, development=60)


def test_campaign_create_freezes_a_floor_split_covering_every_session(tmp_path: Path) -> None:
    project, snapshots, campaign = split_campaign(
        tmp_path, sealed_snapshot_input(15), warmup=3
    )
    manifest_body = manifest(snapshots, campaign["snapshot_id"])
    sealed_discovery = partition(manifest_body, "discovery")
    # 15 sealed sessions are split 9 / 3 / 3, so Discovery has 9 sessions.
    assert sealed_discovery["sessions"] == 9
    read = discovery_read(snapshots, campaign["snapshot_id"])
    content = read["data"]
    sessions = sorted({row["date"] for row in content["calendar"]})
    assert len(sessions) == 9

    split = campaign["discovery_split"]
    assert split["schema_version"] == 1
    assert split["rule"] == SPLIT_RULE
    assert split["ratio"] == {"development": 70, "validation": 30}
    assert split["boundary"] == SPLIT_BOUNDARY
    assert split["as_of"] is None
    assert split["partition"] == {
        "content_hash": sealed_discovery["content_hash"],
        "sessions": 9,
        "start": sessions[0],
        "end": sessions[-1],
    }
    # floor(9 * 70 / 100) = 6 on the development side; the remainder stays trailing.
    assert split["development"] == slice_content(content, sessions[0], sessions[5])
    assert split["trailing"] == slice_content(content, sessions[6], sessions[8])
    assert split["development"]["sessions"] == 6
    assert split["trailing"]["sessions"] == 3
    assert split["development"]["end"] < split["trailing"]["start"]
    assert split["development"]["start"] == split["partition"]["start"]
    assert split["trailing"]["end"] == split["partition"]["end"]
    assert (
        split["development"]["sessions"] + split["trailing"]["sessions"]
        == split["partition"]["sessions"]
    )
    body = {key: value for key, value in split.items() if key != "split_hash"}
    assert split["split_hash"] == digest(body)


def test_campaign_copies_only_development_rows_and_binds_their_hash(tmp_path: Path) -> None:
    project, snapshots, campaign = split_campaign(
        tmp_path, sealed_snapshot_input(15), warmup=3
    )
    split = campaign["discovery_split"]
    sealed_discovery = partition(manifest(snapshots, campaign["snapshot_id"]), "discovery")
    blob, snapshot_hash, evaluation = stored_campaign(project)
    copied = json.loads(blob)
    copied_dates = sorted({row["date"] for row in copied["calendar"]})
    sessions = sorted(
        {row["date"] for row in discovery_read(snapshots, campaign["snapshot_id"])["data"]["calendar"]}
    )
    assert copied_dates == sessions[:6]
    assert split["development"]["start"] <= copied_dates[0]
    assert copied_dates[-1] == split["development"]["end"]
    assert all(kind in copied for kind in KINDS)
    assert hashlib.sha256(blob).hexdigest() == snapshot_hash
    assert snapshot_hash == split["development"]["content_hash"]
    assert campaign["fingerprints"]["snapshot"] == split["development"]["content_hash"]
    assert campaign["research_protocol"] == "trailing-70-30"
    assert evaluation["discovery_split"] == split
    assert evaluation["research_protocol"] == "trailing-70-30"
    assert evaluation["partition_content_hash"] == sealed_discovery["content_hash"]
    assert campaign["history_policy"] == {
        "schema_version": 1,
        "warmup_sessions": 3,
        "scoring_start": sessions[3],
    }


def test_campaign_create_freezes_an_as_of_projection_split(tmp_path: Path) -> None:
    as_of = "2024-01-05T00:00:00+08:00"
    project, snapshots, campaign = split_campaign(
        tmp_path, sealed_snapshot_input(15), warmup=1, as_of=as_of
    )
    split = campaign["discovery_split"]
    assert split["as_of"] == as_of
    # The cutoff projects the Discovery payload to its first five sessions.
    assert split["partition"]["sessions"] == 5
    assert split["development"]["sessions"] == 3
    assert split["trailing"]["sessions"] == 2
    sealed_discovery = partition(manifest(snapshots, campaign["snapshot_id"]), "discovery")
    read = ok(
        cli(
            "snapshot", "read", "--store", str(snapshots),
            "--id", campaign["snapshot_id"], "--partition", "discovery",
            "--as-of", as_of,
        )
    )
    assert split["partition"]["content_hash"] == read["content_hash"]
    assert split["partition"]["content_hash"] != sealed_discovery["content_hash"]

    blob, snapshot_hash, evaluation = stored_campaign(project)
    copied = json.loads(blob)
    copied_dates = sorted({row["date"] for row in copied["calendar"]})
    assert copied_dates == ["2024-01-01", "2024-01-02", "2024-01-03"]
    assert snapshot_hash == split["development"]["content_hash"]
    assert evaluation["partition_content_hash"] == sealed_discovery["content_hash"]
    assert evaluation["as_of"] == as_of

    store = tmp_path / "trusted"
    source = write_json(tmp_path / "round.json", v2_round_input(limit=1))
    opened = ok(open_round(project, store, campaign["campaign_id"], source))
    assert opened["kickoff"]["research_partition"] == split
    assert opened["kickoff"]["validation_policy"] == {"auto_validation_limit": 1}


def test_campaign_create_rejects_an_unsupported_discovery_split(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = write_json(tmp_path / "snapshot.json", sealed_snapshot_input(15))
    ok(cli("project", "init", "--project", str(project)))
    arguments = list(campaign_options(project, source))
    for ratio in ("60/40", "70/25", "0/100", "70/30/1"):
        result = cli(*arguments, "--discovery-split", ratio)
        assert result.returncode == 2, result.stdout
        assert result.stdout == ""
        assert "error:" in result.stderr
    with sqlite3.connect(project / ".prajnatrade" / "project.sqlite") as connection:
        assert connection.execute("SELECT count(*) FROM campaigns").fetchone()[0] == 0


def test_development_only_campaign_results_ignore_the_trailing_segment(tmp_path: Path) -> None:
    first, snapshots_first, campaign_first = split_campaign(
        tmp_path, trending_snapshot(15), warmup=3, name="first"
    )
    split_first = campaign_first["discovery_split"]
    trailing_dates = {
        row["date"]
        for row in discovery_read(snapshots_first, campaign_first["snapshot_id"])["data"]["calendar"]
        if split_first["trailing"]["start"] <= row["date"] <= split_first["trailing"]["end"]
    }
    assert trailing_dates
    different = trending_snapshot(15)
    for row in different["derived"]:
        if row["date"] in trailing_dates:
            row["value"] = round(row["value"] * 2.5, 6)
    for row in different["raw_bars"]:
        if row["date"] in trailing_dates:
            row.update({"open": 12.0, "high": 12.5, "low": 11.5, "close": 12.0})
    second, _, campaign_second = split_campaign(
        tmp_path, different, warmup=3, name="second"
    )
    split_second = campaign_second["discovery_split"]
    assert split_first["development"] == split_second["development"]
    assert split_first["split_hash"] != split_second["split_hash"]
    assert split_first["trailing"]["content_hash"] != split_second["trailing"]["content_hash"]
    assert split_first["trailing"]["start"] == split_second["trailing"]["start"]
    assert split_first["trailing"]["sessions"] == split_second["trailing"]["sessions"]

    blob_first, _, _ = stored_campaign(first)
    blob_second, _, _ = stored_campaign(second)
    assert blob_first == blob_second

    parameters = {"lookback": 2, "cash_if_negative": False}
    result_first = submit_experiment(
        first,
        campaign_first["campaign_id"],
        tmp_path / "request-first.json",
        "momentum",
        "The frozen development slice should determine every result.",
        "momentum",
        parameters,
    )
    result_second = submit_experiment(
        second,
        campaign_second["campaign_id"],
        tmp_path / "request-second.json",
        "momentum",
        "The frozen development slice should determine every result.",
        "momentum",
        parameters,
    )
    assert result_first["status"] == "succeeded", result_first
    assert result_second["status"] == "succeeded", result_second
    discovery_first = result_first["discovery"]
    discovery_second = result_second["discovery"]
    assert discovery_first["signals"], "the fixture must actually trade"
    assert discovery_first["signals"] == discovery_second["signals"]
    assert discovery_first["fills"] == discovery_second["fills"]
    assert discovery_first["order_attempts"] == discovery_second["order_attempts"]
    assert discovery_first["equity_curve"] == discovery_second["equity_curve"]
    assert discovery_first["metrics"] == discovery_second["metrics"]
    assert (
        discovery_first["baseline_comparison"]["baseline"]["metrics"]
        == discovery_second["baseline_comparison"]["baseline"]["metrics"]
    )
    assert discovery_first["metrics"]["sessions"] == 3


# ---------------------------------------------------------------------------
# C. The v2 round protocol: open, submit, status
# ---------------------------------------------------------------------------


def v2_round_input(limit: int = 2) -> dict:
    return round_input(validation_policy={"auto_validation_limit": limit})


def open_v2_round(
    project: Path, store: Path, campaign_id: str, source: Path
) -> dict:
    return ok(open_round(project, store, campaign_id, source))


def lock_momentum_version(
    root: Path,
    project: Path,
    store: Path,
    snapshots: Path,
    campaign: dict,
    round_id: str,
) -> tuple[dict, dict]:
    experiment = submit_experiment(
        project,
        campaign["campaign_id"],
        root / "round-request.json",
        "momentum",
        "The frozen development slice should determine every result.",
        "momentum",
        {"lookback": 2, "cash_if_negative": False},
    )
    source = write_json(
        root / "round-version.json", version_input(experiment["experiment_id"])
    )
    submitted = ok(submit_version(project, round_id, "v1", source))
    assert snapshots.is_dir()
    return experiment, submitted


def test_round_open_v2_binds_protocol_split_and_auto_limit(tmp_path: Path) -> None:
    project, _, campaign = split_campaign(tmp_path, sealed_snapshot_input(15), warmup=3)
    store = tmp_path / "trusted"
    source = write_json(tmp_path / "round.json", v2_round_input(limit=3))

    opened = ok(open_round(project, store, campaign["campaign_id"], source))
    kicked = opened["kickoff"]
    assert kicked["protocol"] == V2_PROTOCOL
    assert kicked["research_partition"] == campaign["discovery_split"]
    assert kicked["validation_policy"] == {"auto_validation_limit": 3}
    assert kicked["frozen_campaign"]["snapshot_hash"] == (
        campaign["discovery_split"]["development"]["content_hash"]
    )
    assert kicked["frozen_campaign"]["partition_content_hash"] == (
        campaign["discovery_split"]["partition"]["content_hash"]
    )

    replay = ok(open_round(project, store, campaign["campaign_id"], source))
    assert replay["reused"] is True
    assert replay["round_id"] == opened["round_id"]
    assert replay["kickoff_hash"] == opened["kickoff_hash"]
    assert replay["kickoff"] == kicked


def test_round_open_requires_the_policy_for_v2_and_refuses_it_for_v1(tmp_path: Path) -> None:
    project, _, campaign = split_campaign(tmp_path, sealed_snapshot_input(15), warmup=3)
    store = tmp_path / "trusted"
    missing = write_json(tmp_path / "missing-policy.json", round_input())
    rejected(open_round(project, store, campaign["campaign_id"], missing), "VALIDATION_POLICY_REQUIRED", phase="round")
    assert not (store / "research-loop-rounds").exists()

    for invalid in (0, -1, "2", True):
        source = write_json(
            tmp_path / f"invalid-{invalid}.json",
            round_input(validation_policy={"auto_validation_limit": invalid}),
        )
        rejected(
            open_round(project, store, campaign["campaign_id"], source),
            "VALIDATION_POLICY_REQUIRED",
            phase="round",
        )
    malformed = write_json(
        tmp_path / "malformed-policy.json", round_input(validation_policy={})
    )
    rejected(
        open_round(project, store, campaign["campaign_id"], malformed),
        "VALIDATION_POLICY_REQUIRED",
        phase="round",
    )

    legacy_project, _, legacy = legacy_campaign(tmp_path, sealed_snapshot_input(15))
    legacy_store = tmp_path / "legacy-trusted"
    source = write_json(tmp_path / "legacy-policy.json", v2_round_input())
    rejected(
        open_round(legacy_project, legacy_store, legacy["campaign_id"], source),
        "DISCOVERY_SPLIT_NOT_FROZEN",
        phase="round",
    )
    assert not (legacy_store / "research-loop-rounds").exists()


def test_round_submit_locks_the_v2_protocol_and_agent_status_shows_the_split(
    tmp_path: Path,
) -> None:
    project, snapshots, campaign = split_campaign(
        tmp_path, sealed_snapshot_input(15), warmup=3
    )
    store = tmp_path / "trusted"
    source = write_json(tmp_path / "round.json", v2_round_input(limit=2))
    opened = ok(open_round(project, store, campaign["campaign_id"], source))
    split = campaign["discovery_split"]

    experiment, submitted = lock_momentum_version(
        tmp_path, project, store, snapshots, campaign, opened["round_id"]
    )
    assert submitted["status"] == "submitted"
    assert_hex(submitted["version_id"])
    locked = submitted["locked"]
    assert locked["protocol"] == V2_PROTOCOL
    assert locked["research_partition"] == split
    assert locked["snapshot_hash"] == split["development"]["content_hash"]
    protocol_body = {
        "schema_version": 1,
        "comparison": opened["kickoff"]["comparison"],
        "hard_conditions": opened["kickoff"]["hard_conditions"],
        "selection_rules": opened["kickoff"]["selection_rules"],
        "protocol": V2_PROTOCOL,
        "position_policy": opened["kickoff"]["position_policy"],
        "research_partition": {
            key: split[key] for key in ("split_hash", "development", "trailing")
        },
    }
    assert locked["protocol_hash"] == digest(protocol_body)

    replay = ok(
        submit_version(
            project, opened["round_id"], "v1", write_json(
                tmp_path / "round-version-replay.json",
                version_input(experiment["experiment_id"]),
            )
        )
    )
    assert replay["reused"] is True
    assert replay["version_id"] == submitted["version_id"]
    assert replay["locked"] == locked

    status = ok(round_status(project, opened["round_id"], agent=True))
    assert status["protocol"] == V2_PROTOCOL
    assert status["research_partition"] == split
    assert status["validation_policy"] == {"auto_validation_limit": 2}
    assert status["research_partition"]["development"]["start"] == (
        split["development"]["start"]
    )
    assert status["research_partition"]["development"]["sessions"] == 6
    assert status["research_partition"]["trailing"]["start"] == split["trailing"]["start"]
    assert status["research_partition"]["trailing"]["sessions"] == 3
    assert status["submissions"][0]["version_id"] == submitted["version_id"]
    assert status["submissions"][0]["protocol"] == V2_PROTOCOL
    assert status["submissions"][0]["research_partition"] == split


def test_legacy_round_keeps_the_v1_kickoff_locked_shape_and_receipt(tmp_path: Path) -> None:
    project, snapshots, campaign = legacy_campaign(
        tmp_path, sealed_snapshot_input(15), warmup=4
    )
    # A new Campaign always records its research protocol, including v1 for a
    # Campaign created without the split flag.
    assert campaign["research_protocol"] == "discovery-replay"
    _, _, legacy_evaluation = stored_campaign(project)
    assert legacy_evaluation["research_protocol"] == "discovery-replay"
    assert "discovery_split" not in legacy_evaluation
    store = tmp_path / "trusted"
    source = write_json(tmp_path / "round.json", round_input())
    opened = ok(open_round(project, store, campaign["campaign_id"], source))
    kicked = opened["kickoff"]
    assert "protocol" not in kicked
    assert "research_partition" not in kicked
    assert "validation_policy" not in kicked
    assert set(kicked) == {
        "objective", "constraints", "comparison", "selection_rules",
        "hard_conditions", "disclosed_risks", "scoring_policy", "observation",
        "invalidation", "reviewer", "approval_reason", "frozen_campaign", "budget",
    }

    experiment, submitted = lock_momentum_version(
        tmp_path, project, store, snapshots, campaign, opened["round_id"]
    )
    locked = submitted["locked"]
    assert "protocol" not in locked
    assert "research_partition" not in locked
    assert locked["protocol_hash"] == digest(
        {
            "schema_version": 1,
            "comparison": kicked["comparison"],
            "hard_conditions": kicked["hard_conditions"],
            "selection_rules": kicked["selection_rules"],
        }
    )

    status = ok(round_status(project, opened["round_id"], agent=True))
    assert status["protocol"] == V1_PROTOCOL
    assert "research_partition" not in status
    assert "validation_policy" not in status

    validated = ok(
        validate_version(
            project, store, snapshots, opened["round_id"], submitted["version_id"]
        )
    )
    assert validated["status"] == "completed"
    assert validated["outcome"] == "passed"
    assert "protocol" not in validated
    assert "validation_kind" not in validated
    assert "research_partition" not in validated
    assert validated["opened_protected_source"] is False
    assert validated["query_debited"] is False
    assert validated["replay"]["matches_discovery"] is True
    assert validated["replay"]["candidate"]["sessions"] == (
        experiment["discovery"]["metrics"]["sessions"]
    )
