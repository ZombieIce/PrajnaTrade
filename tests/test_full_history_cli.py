"""Public-CLI acceptance for authorized full-history Discovery rounds."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from cli_helpers import cli, ok, rejected, write_json
from test_research_loop_cli import (
    lock_version,
    open_round,
    round_input,
    round_status,
    submit_strategy,
    validate_version,
)

SYMBOLS = ("511260", "518880", "515080", "513300", "159612")
AUTHORIZATION_ID = "synthetic-full-history-authorization"


def seal_full_history(
    root: Path,
    store: Path,
    payload: dict,
    authorization_id: str,
    *,
    source_snapshot_id: str | None = None,
) -> dict:
    dates = [row["date"] for row in payload["calendar"]]
    source = write_json(root / f"{authorization_id}.json", payload)
    arguments = [
        "snapshot",
        "seal",
        "--input",
        str(source),
        "--store",
        str(store),
        "--full-history",
        "--authorization-id",
        authorization_id,
        "--authorized-start",
        dates[0],
        "--authorized-end",
        dates[-1],
    ]
    if source_snapshot_id is not None:
        arguments += ["--source-snapshot-id", source_snapshot_id]
    return ok(cli(*arguments))


def full_history(day_count: int) -> dict:
    dates = []
    current = date(2020, 1, 1)
    while len(dates) < day_count:
        if current.weekday() < 5:
            dates.append(current.isoformat())
        current += timedelta(days=1)
    evidence = {
        "available_at": "2019-12-31T00:00:00+08:00",
        "availability_evidence": "synthetic release schedule",
        "collected_at": "2025-01-01T00:00:00+08:00",
    }
    return {
        "schema_version": 1,
        "data_kind": "synthetic",
        "sources": {
            name: {
                "source": "synthetic",
                "version": "1",
                "coverage_start": dates[0],
                "coverage_end": dates[-1],
                **evidence,
            }
            for name in ("calendar", "raw_bars", "corporate_actions", "derived")
        },
        "symbol_coverage": {
            symbol: {"start": dates[0], "end": dates[-1]} for symbol in SYMBOLS
        },
        "derivation_rule_version": "synthetic-total-return-v1",
        "calendar": [{"date": day, **evidence} for day in dates],
        "raw_bars": [
            {
                "date": day,
                "symbol": symbol,
                "open": 10,
                "high": 10,
                "low": 10,
                "close": 10,
                "volume": 1000,
                "trading_status": "trading",
                **evidence,
            }
            for day in dates
            for symbol in SYMBOLS
        ],
        "corporate_actions": [],
        "derived": [
            {"date": day, "symbol": symbol, "value": 10, **evidence}
            for day in dates
            for symbol in SYMBOLS
        ],
    }


def test_public_cli_seals_and_opens_one_authorized_full_history(tmp_path: Path) -> None:
    payload = full_history(1094)
    dates = [row["date"] for row in payload["calendar"]]
    store = tmp_path / "trusted-snapshots"
    sealed = seal_full_history(tmp_path, store, payload, AUTHORIZATION_ID)
    manifest = ok(
        cli("snapshot", "manifest", "--store", str(store), "--id", sealed["snapshot_id"])
    )
    assert manifest["authorized_history"]["authorization_id"] == AUTHORIZATION_ID
    assert manifest["authorized_history"]["source"]["kind"] == "synthetic_input"
    authorized_range = manifest["authorized_history"]["range"]
    assert (authorized_range["start"], authorized_range["end"], authorized_range["sessions"]) == (
        dates[0],
        dates[-1],
        1094,
    )
    assert [item["name"] for item in manifest["partitions"]] == ["discovery"]
    assert manifest["partitions"][0]["sessions"] == 1094
    protected_read = cli(
        "snapshot",
        "read",
        "--store",
        str(store),
        "--id",
        sealed["snapshot_id"],
        "--partition",
        "validation",
    )
    assert protected_read.returncode != 0
    assert not protected_read.stdout

    project = tmp_path / "research-project"
    ok(cli("project", "init", "--project", str(project)))
    campaign = ok(
        cli(
            "campaign",
            "create",
            "--project",
            str(project),
            "--store",
            str(store),
            "--snapshot-id",
            sealed["snapshot_id"],
            "--attempts",
            "2",
            "--max-seconds",
            "30",
            "--discovery-split",
            "70/30",
            "--discovery-warmup-sessions",
            "40",
        )
    )
    full_history_receipt = campaign["full_history"]
    assert full_history_receipt["authorized_range"] == authorized_range
    assert full_history_receipt["discovery"]["sessions"] == 765
    assert full_history_receipt["validation"]["sessions"] == 329
    assert full_history_receipt["discovery"]["end"] < full_history_receipt["validation"]["start"]
    assert full_history_receipt["warmup"]["sessions"] == 40
    assert full_history_receipt["warmup"]["source"] == "authorized_history"
    assert full_history_receipt["scoring"] == {
        "start": dates[40],
        "end": dates[764],
        "sessions": 725,
    }

    changed_later = full_history(1094)
    last_day = changed_later["calendar"][-1]["date"]
    later_bar = next(
        row for row in changed_later["raw_bars"]
        if row["date"] == last_day and row["symbol"] == SYMBOLS[0]
    )
    later_bar.update({"high": 11, "close": 11})
    successor = seal_full_history(
        tmp_path,
        store,
        changed_later,
        "synthetic-full-history-successor",
        source_snapshot_id=sealed["snapshot_id"],
    )
    successor_campaign = ok(
        cli(
            "campaign",
            "create",
            "--project",
            str(project),
            "--store",
            str(store),
            "--snapshot-id",
            successor["snapshot_id"],
            "--attempts",
            "2",
            "--max-seconds",
            "30",
            "--discovery-split",
            "70/30",
            "--discovery-warmup-sessions",
            "40",
        )
    )
    assert successor["snapshot_id"] != sealed["snapshot_id"]
    assert (
        successor_campaign["full_history"]["discovery"]["content_hash"]
        == full_history_receipt["discovery"]["content_hash"]
    )
    assert (
        successor_campaign["full_history"]["validation"]["content_hash"]
        != full_history_receipt["validation"]["content_hash"]
    )
    first_experiment = submit_strategy(
        project,
        campaign["campaign_id"],
        tmp_path / "first-experiment.json",
        "first-history-result",
        "equal_weight",
        {},
    )
    successor_experiment = submit_strategy(
        project,
        successor_campaign["campaign_id"],
        tmp_path / "successor-experiment.json",
        "successor-history-result",
        "equal_weight",
        {},
    )
    first_run = first_experiment["discovery"]
    successor_run = successor_experiment["discovery"]
    assert first_run["signals"] == successor_run["signals"]
    assert first_run["fills"] == successor_run["fills"]
    assert first_run["order_attempts"] == successor_run["order_attempts"]
    assert first_run["equity_curve"] == successor_run["equity_curve"]
    assert first_run["metrics"] == successor_run["metrics"]
    assert (
        first_run["baseline_comparison"]["baseline"]["metrics"]
        == successor_run["baseline_comparison"]["baseline"]["metrics"]
    )

    round_source = write_json(
        tmp_path / "round.json",
        round_input(validation_policy={"auto_validation_limit": 1}),
    )
    rejected(
        open_round(
            project,
            tmp_path / "trusted-evaluations",
            campaign["campaign_id"],
            round_source,
            direction_key="mismatched-authorization",
            authorization_id="not-the-history-authorization",
        ),
        "AUTHORIZATION_MISMATCH",
    )
    opened = ok(
        open_round(
            project,
            tmp_path / "trusted-evaluations",
            campaign["campaign_id"],
            round_source,
            authorization_id=AUTHORIZATION_ID,
        )
    )
    assert opened["kickoff"]["full_history"] == full_history_receipt
    status = ok(round_status(project, opened["round_id"]))
    assert status["full_history"] == full_history_receipt


def test_new_source_snapshot_discloses_feedback_on_overlapping_history(
    tmp_path: Path,
) -> None:
    payload = full_history(20)
    store = tmp_path / "snapshots"
    first_snapshot = seal_full_history(
        tmp_path, store, payload, "first-history-authorization"
    )
    project = tmp_path / "project"
    ok(cli("project", "init", "--project", str(project)))
    first_campaign = ok(
        cli(
            "campaign",
            "create",
            "--project",
            str(project),
            "--store",
            str(store),
            "--snapshot-id",
            first_snapshot["snapshot_id"],
            "--attempts",
            "2",
            "--max-seconds",
            "30",
            "--discovery-split",
            "70/30",
            "--discovery-warmup-sessions",
            "2",
        )
    )
    trusted = tmp_path / "trusted"
    first_round_input = write_json(
        tmp_path / "first-round.json",
        round_input(validation_policy={"auto_validation_limit": 1}),
    )
    first_round = ok(
        open_round(
            project,
            trusted,
            first_campaign["campaign_id"],
            first_round_input,
            authorization_id="first-history-authorization",
        )
    )
    experiment = submit_strategy(
        project,
        first_campaign["campaign_id"],
        tmp_path / "experiment.json",
        "first-version",
        "equal_weight",
        {},
    )
    version = lock_version(
        tmp_path,
        project,
        first_round["round_id"],
        "first-version",
        experiment["experiment_id"],
    )
    ok(
        validate_version(
            project,
            trusted,
            store,
            first_round["round_id"],
            version["version_id"],
        )
    )
    impact = "The scored interval and its feedback should follow overlapping history."
    feedback = ok(
        cli(
            "round",
            "feedback",
            "--project",
            str(project),
            "--round-id",
            first_round["round_id"],
            "--version-id",
            version["version_id"],
            "--input",
            str(write_json(
                tmp_path / "feedback.json",
                {"impact": impact, "detail_access_id": None, "next_experiment_id": None},
            )),
        )
    )
    assert feedback["status"] == "recorded"

    changed = full_history(20)
    first_day = changed["calendar"][0]["date"]
    changed_bar = next(
        row for row in changed["raw_bars"]
        if row["date"] == first_day and row["symbol"] == SYMBOLS[0]
    )
    changed_bar.update({"high": 11, "close": 11})
    successor = seal_full_history(
        tmp_path,
        store,
        changed,
        "second-history-authorization",
        source_snapshot_id=first_snapshot["snapshot_id"],
    )
    successor_manifest = ok(
        cli(
            "snapshot",
            "manifest",
            "--store",
            str(store),
            "--id",
            successor["snapshot_id"],
        )
    )
    assert (
        successor_manifest["authorized_history"]["source"]["snapshot_id"]
        == first_snapshot["snapshot_id"]
    )
    assert (
        successor_manifest["authorized_history"]["source"]["kind"]
        == "sealed_synthetic_snapshot"
    )

    second_campaign = ok(
        cli(
            "campaign",
            "create",
            "--project",
            str(project),
            "--store",
            str(store),
            "--snapshot-id",
            successor["snapshot_id"],
            "--attempts",
            "2",
            "--max-seconds",
            "30",
            "--discovery-split",
            "70/30",
            "--discovery-warmup-sessions",
            "2",
        )
    )
    second_round_input = write_json(
        tmp_path / "second-round.json",
        round_input(validation_policy={"auto_validation_limit": 1}),
    )
    second_round = ok(
        open_round(
            project,
            trusted,
            second_campaign["campaign_id"],
            second_round_input,
            direction_key="successor-history",
            authorization_id="second-history-authorization",
        )
    )
    disclosure = second_round["reuse_disclosure"]
    assert disclosure["previous_rounds"][0]["match"] == "overlapping_authorized_interval"
    assert disclosure["previous_rounds"][0]["overlap"] is not None
    assert disclosure["feedback"][0]["impact"] == impact
