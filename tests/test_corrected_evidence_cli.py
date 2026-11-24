"""Public-CLI tests for corrected-evidence revalidation (ticket #49, spec #43 T5).

Spec #43 L33: a data or platform correction keeps the strategy identity, updates
the evidence source, recomputes the affected Discovery results and permits a
fresh review when the original selection reason still holds.  Every scenario
below drives the operator CLI (``round revalidate --evidence-snapshot-id`` and
``round review``) through subprocesses on synthetic sealed snapshots.

The corrected successor snapshot is produced by the existing trusted
``snapshot correction register|verify|successor`` journal, so the lineage
manifest and its sealed Discovery partition are built by the product's own
sealing code.  Fixture builders are imported from the modules that own them;
none is copied here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from cli_helpers import cli, ok, rejected, write_json
from test_experiment_cli import campaign_options, sealed_snapshot_input
from test_protected_eval_cli import submit_experiment
from test_research_loop_cli import (
    open_round,
    round_input,
    validate_version,
)
from test_version_review_cli import (
    approve_version,
    calendar_file,
    lock,
    observation_input,
    return_input,
    review,
    version_status,
)

HEX = re.compile(r"[0-9a-f]{64}")
EX_DATE = "2024-01-08"
PAY_DATE = "2024-01-10"
OLD_AMOUNT = 0.2
NEW_AMOUNT = 0.18
AVAILABLE_AT = "2024-01-05T09:00:00+08:00"
REASON = "The official cash dividend was corrected; recheck the affected Discovery rows."


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _hex(value: object) -> None:
    assert isinstance(value, str) and HEX.fullmatch(value), value


def corrected_payload(day_count: int = 15) -> dict:
    """A synthetic payload whose one dividend moves inside the scored interval."""
    payload = sealed_snapshot_input(day_count)
    payload["corporate_actions"][0].update(date=EX_DATE, pay_date=PAY_DATE)
    for row in payload["raw_bars"]:
        if row["symbol"] == "511260":
            price = 9.8 if row["date"] >= EX_DATE else 10.0
            row.update(open=price, high=price, low=price, close=price)
    return payload


def open_scenario(
    root: Path,
    *,
    family: str = "equal_weight",
    parameters: dict | None = None,
    hard_conditions: list[dict] | None = None,
    as_of: str | None = None,
    name: str = "project",
) -> dict:
    """Open a round, lock one version and run its first lightweight Validation."""
    payload = corrected_payload()
    project = root / name
    source = write_json(root / f"{name}-snapshot.json", payload)
    ok(cli("project", "init", "--project", str(project)))
    options = (
        *campaign_options(project, source, attempts=6, max_seconds=180),
        "--discovery-warmup-sessions",
        "4",
    )
    if as_of is not None:
        options = (*options, "--as-of", as_of)
    campaign = ok(cli(*options))
    snapshots = project.parent / f"{project.name}-snapshot-store"
    store = root / f"{name}-trusted"
    contract = round_input()
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
    experiment = submit_experiment(
        project,
        campaign["campaign_id"],
        root / f"{name}-experiment.json",
        "first",
        "The corrected dividend still supports the frozen selection reason.",
        family,
        parameters or {},
    )
    version = lock(
        root, project, opened["round_id"], "version-one", experiment["experiment_id"]
    )
    prior = ok(
        validate_version(
            project, store, snapshots, opened["round_id"], version["version_id"]
        )
    )
    return {
        "project": project,
        "snapshots": snapshots,
        "campaign": campaign,
        "store": store,
        "opened": opened,
        "version": version,
        "prior": prior,
    }


def corrected_successor(
    root: Path,
    store: Path,
    snapshot_id: str,
    *,
    old_amount: float = OLD_AMOUNT,
    new_amount: float = NEW_AMOUNT,
    tag: str = "official",
    available_at: str = AVAILABLE_AT,
) -> str:
    """File a verified dividend revision and seal its successor through the CLI."""
    proposal = {
        "schema_version": 1,
        "predecessor_id": snapshot_id,
        "symbol": "511260",
        "ex_date": EX_DATE,
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
            str(store),
        )
    )
    notice = {
        "kind": "synthetic",
        "authority": "fund_manager",
        "issuer": "Synthetic Fund Manager",
        "url": "https://notices.example.invalid/dividend",
        "reference": f"synthetic-notice-{tag}",
        "symbol": "511260",
        "ex_date": EX_DATE,
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
            str(store),
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
            str(store),
            "--proposal-id",
            pending["proposal_id"],
            "--revision-id",
            verified["revision_id"],
        )
    )
    return sealed["snapshot_id"]


def revalidate(
    scenario: dict,
    *,
    reason: str = REASON,
    evidence_snapshot_id: str | None = None,
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
        scenario["opened"]["round_id"],
        "--version-id",
        scenario["version"]["version_id"],
        "--reason",
        reason,
    ]
    if evidence_snapshot_id is not None:
        arguments += ["--evidence-snapshot-id", evidence_snapshot_id]
    return cli(*arguments)


def snapshot_manifest(store: Path, snapshot_id: str) -> dict:
    return ok(cli("snapshot", "manifest", "--store", str(store), "--id", snapshot_id))


def discovery_descriptor(manifest: dict) -> dict:
    return next(
        partition for partition in manifest["partitions"] if partition["name"] == "discovery"
    )


def remove_protected_payloads(store: Path, snapshot_id: str) -> None:
    """Delete the sealed protected payloads so any protected read fails."""
    directory = store / snapshot_id
    directory.chmod(0o755)
    for partition in ("validation", "final_holdout"):
        (directory / f"{partition}.json").unlink()
    directory.chmod(0o555)


def assert_no_protected_access(scenario: dict) -> None:
    store = scenario["store"]
    assert not (store / "queries.sqlite").exists()
    assert not (store / "records").exists()
    current = version_status(
        scenario["project"],
        scenario["opened"]["round_id"],
        scenario["version"]["version_id"],
    )
    assert current["validation"]["validation_id"]
    receipt = json.loads(
        (
            store
            / "research-loop-validations"
            / f"{current['validation']['validation_id']}.json"
        ).read_text(encoding="utf-8")
    )
    assert receipt["opened_protected_source"] is False
    assert receipt["query_debited"] is False


# ---------------------------------------------------------------------------
# 1. Corrected Discovery values recompute the candidate and the benchmark
# ---------------------------------------------------------------------------


def test_corrected_discovery_recomputes_both_runs_and_admits_the_version(
    tmp_path: Path,
) -> None:
    scenario = open_scenario(tmp_path)
    assert scenario["prior"]["outcome"] == "passed"
    prepared = scenario["snapshots"]
    locked = scenario["campaign"]["snapshot_id"]
    successor = corrected_successor(tmp_path, prepared, locked)
    remove_protected_payloads(prepared, locked)

    result = ok(revalidate(scenario, evidence_snapshot_id=successor))
    assert result["status"] == "revalidated"
    assert result["reused"] is False
    assert result["round_id"] == scenario["opened"]["round_id"]
    assert result["version_id"] == scenario["version"]["version_id"]
    assert result["strategy_fingerprint"] == scenario["version"]["strategy_fingerprint"]
    assert result["previous_validation_id"] == scenario["prior"]["validation_id"]
    assert result["validation_id"] != scenario["prior"]["validation_id"]
    assert result["previous_receipt_hash"] == scenario["prior"]["receipt_hash"]
    assert result["previous_outcome"] == "passed"
    assert result["review_state"] == "pending"
    assert result["outcome"] == "passed"
    assert result["reason"] is None
    _hex(result["revalidation_id"])

    evidence = result["evidence"]
    assert evidence["verified"] is True
    assert evidence["successor"] is True
    assert evidence["snapshot_id"] == successor
    assert evidence["previous_snapshot_id"] == locked
    assert evidence["snapshot_changed"] is True
    assert evidence["discovery_changed"] is True
    assert evidence["lineage"] == [successor, locked]
    assert evidence["schema_version"] == 1
    assert evidence["data_kind"] == "synthetic"
    assert evidence["as_of"] == "2024-01-09T23:59:59.999999+08:00"
    assert evidence["partition"] == {
        "start": "2024-01-01",
        "end": "2024-01-09",
        "sessions": 9,
    }
    successor_partition = discovery_descriptor(
        snapshot_manifest(prepared, successor)
    )["content_hash"]
    assert evidence["partition_content_hash"] == successor_partition
    assert successor_partition != scenario["campaign"]["fingerprints"]["partition"]
    assert evidence["snapshot_hash"] == successor_partition
    _hex(evidence["engine_runtime_identity"])
    _hex(evidence["replay_engine_identity"])

    recomputed = result["recomputed_discovery"]
    assert recomputed["snapshot_id"] == successor
    assert recomputed["partition_content_hash"] == successor_partition
    assert recomputed["as_of"] == evidence["as_of"]
    assert recomputed["scoring_start"] == "2024-01-05"
    candidate = recomputed["candidate"]
    benchmark = recomputed["benchmark"]
    assert candidate["start_date"] == "2024-01-05"
    assert candidate["end_date"] == "2024-01-09"
    assert candidate["sessions"] == 5
    assert candidate["valued_sessions"] == 5
    assert candidate["fill_count"] == 5
    assert len(candidate["fills"]) == 5
    assert benchmark["sessions"] == 5
    assert benchmark["valued_sessions"] == 5
    assert candidate["ending_equity"] < result["original_discovery"]["candidate"][
        "ending_equity"
    ]
    assert candidate["ending_cash"] == 0.0
    for field in ("total_return", "max_drawdown", "sharpe", "turnover", "valued_sessions"):
        assert field in candidate["metrics"]
    _hex(recomputed["content_hash"])

    original = result["original_discovery"]
    assert original["snapshot_id"] == locked
    assert original["partition_content_hash"] == scenario["campaign"]["fingerprints"][
        "partition"
    ]
    assert original["candidate"]["fill_count"] == 5
    assert original["candidate"]["ending_equity"] > candidate["ending_equity"]
    _hex(original["content_hash"])

    affected = result["affected"]
    assert affected["changed_rows"] == {
        "calendar": 0,
        "raw_bars": 0,
        "corporate_actions": 1,
        "derived": 9,
    }
    assert affected["changed_nav_sessions"] == ["2024-01-08", "2024-01-09"]
    assert affected["changed_fill_sessions"] == []
    assert affected["fills"] == {"original": 5, "recomputed": 5}
    assert affected["nav_sessions"] == {"original": 5, "recomputed": 5}
    assert affected["ending_equity"]["delta"] < 0
    assert "corporate_actions" in affected["reason"]

    current = version_status(
        scenario["project"],
        scenario["opened"]["round_id"],
        scenario["version"]["version_id"],
    )
    assert current["review_state"] == "pending"
    assert current["validation"]["validation_id"] == result["validation_id"]
    assert current["validation"]["outcome"] == "passed"
    assert current["validation"]["revalidated_from"] == scenario["prior"]["validation_id"]
    assert current["validation"]["revalidation_id"] == result["revalidation_id"]
    history = current["validation"]["history"]
    assert [entry["validation_id"] for entry in history] == [
        scenario["prior"]["validation_id"],
        result["validation_id"],
    ]
    assert current["validation"]["evidence"]["snapshot_id"] == successor

    # The prior receipt is retained byte-for-byte as historical evidence, and
    # the corrected receipt is a new write-once trusted record.
    validations = scenario["store"] / "research-loop-validations"
    assert (validations / f"{scenario['prior']['validation_id']}.json").is_file()
    assert (validations / f"{result['validation_id']}.json").is_file()
    recorded = json.loads(
        (
            scenario["store"]
            / "research-loop-revalidations"
            / f"{result['revalidation_id']}.json"
        ).read_text(encoding="utf-8")
    )
    assert recorded["prior_validation_id"] == scenario["prior"]["validation_id"]
    assert recorded["prior_receipt"]["receipt_hash"] == scenario["prior"]["receipt_hash"]
    assert recorded["validation_id"] == result["validation_id"]
    assert recorded["recomputed_discovery"] == recomputed
    assert recorded["affected"] == affected

    calendar, _ = calendar_file(tmp_path, start_offset=1)
    approved = ok(
        approve_version(
            scenario["project"],
            scenario["store"],
            scenario["opened"]["round_id"],
            scenario["version"]["version_id"],
            calendar,
            write_json(tmp_path / "observation.json", observation_input()),
            reason="The corrected passed receipt still supports the original selection reason.",
        )
    )
    assert approved["validation"]["validation_id"] == result["validation_id"]
    assert approved["validation"]["outcome"] == "passed"
    assert_no_protected_access(scenario)


# ---------------------------------------------------------------------------
# 2. Same corrected evidence replays; prior receipts and decisions stay history
# ---------------------------------------------------------------------------


def test_corrected_replay_is_idempotent_and_keeps_review_history(tmp_path: Path) -> None:
    scenario = open_scenario(tmp_path)
    round_id = scenario["opened"]["round_id"]
    version_id = scenario["version"]["version_id"]
    ok(
        review(
            scenario["project"],
            scenario["store"],
            round_id,
            version_id,
            "return",
            reason="Hold the version until the corrected evidence is rechecked.",
            source=write_json(tmp_path / "return.json", return_input()),
        )
    )
    returned = version_status(scenario["project"], round_id, version_id)
    decision_id = returned["review"]["decision_id"]
    successor = corrected_successor(
        tmp_path, scenario["snapshots"], scenario["campaign"]["snapshot_id"]
    )

    first = ok(revalidate(scenario, evidence_snapshot_id=successor))
    replayed = ok(revalidate(scenario, evidence_snapshot_id=successor))
    assert replayed["reused"] is True
    assert replayed["revalidation_id"] == first["revalidation_id"]
    assert replayed["validation_id"] == first["validation_id"]
    assert replayed["outcome"] == first["outcome"] == "passed"
    assert len(
        list((scenario["store"] / "research-loop-revalidations").iterdir())
    ) == 1
    current = version_status(scenario["project"], round_id, version_id)
    assert current["review_state"] == "returned"
    assert current["review"]["decision_id"] == decision_id
    assert [entry["validation_id"] for entry in current["validation"]["history"]] == [
        scenario["prior"]["validation_id"],
        first["validation_id"],
    ]

    # A different evidence snapshot and a different recorded reason are distinct
    # evidence epochs, not a replay of the first corrected record.
    other = corrected_successor(
        tmp_path,
        scenario["snapshots"],
        scenario["campaign"]["snapshot_id"],
        new_amount=0.19,
        tag="second",
    )
    second = ok(
        revalidate(
            scenario,
            reason="A second official notice refined the same dividend correction.",
            evidence_snapshot_id=other,
        )
    )
    assert second["reused"] is False
    assert second["evidence"]["snapshot_id"] == other
    assert second["validation_id"] != first["validation_id"]
    assert second["revalidation_id"] != first["revalidation_id"]
    assert second["previous_validation_id"] == first["validation_id"]
    status = version_status(scenario["project"], round_id, version_id)
    assert status["review_state"] == "returned"
    assert status["review"]["decision_id"] == decision_id
    assert [entry["validation_id"] for entry in status["validation"]["history"]] == [
        scenario["prior"]["validation_id"],
        first["validation_id"],
        second["validation_id"],
    ]
    approved = ok(
        approve_version(
            scenario["project"],
            scenario["store"],
            round_id,
            version_id,
            calendar_file(tmp_path, start_offset=1)[0],
            write_json(tmp_path / "observation.json", observation_input()),
            reason="The latest corrected receipt still holds the original selection reason.",
        )
    )
    assert approved["validation"]["validation_id"] == second["validation_id"]


# ---------------------------------------------------------------------------
# 3. A correction that breaks a hard condition stays unapprovable
# ---------------------------------------------------------------------------


def test_correction_that_breaks_a_hard_condition_cannot_be_approved(
    tmp_path: Path,
) -> None:
    hard_conditions = [
        {"id": "capital_preservation", "metric": "net_return", "operator": ">=", "value": -0.001},
        {"id": "coverage", "metric": "valued_sessions", "operator": ">=", "value": 1},
    ]
    scenario = open_scenario(tmp_path, hard_conditions=hard_conditions)
    assert scenario["prior"]["outcome"] == "passed"
    successor = corrected_successor(
        tmp_path, scenario["snapshots"], scenario["campaign"]["snapshot_id"], new_amount=0.01
    )

    failed = ok(revalidate(scenario, evidence_snapshot_id=successor))
    assert failed["outcome"] == "failed"
    assert failed["reason"] == "HARD_CONDITION_NOT_MET"
    recorded = json.loads(
        (
            scenario["store"]
            / "research-loop-revalidations"
            / f"{failed['revalidation_id']}.json"
        ).read_text(encoding="utf-8")
    )
    assert recorded["outcome"] == "failed"
    assert recorded["recomputed_discovery"]["candidate"]["metrics"]["total_return"] < -0.001
    assert recorded["original_discovery"]["candidate"]["metrics"]["total_return"] > -0.001

    current = version_status(
        scenario["project"],
        scenario["opened"]["round_id"],
        scenario["version"]["version_id"],
    )
    assert current["validation"]["outcome"] == "failed"
    assert current["validation"]["validation_id"] == failed["validation_id"]
    assert current["validation"]["revalidated_from"] == scenario["prior"]["validation_id"]
    payload = rejected(
        approve_version(
            scenario["project"],
            scenario["store"],
            scenario["opened"]["round_id"],
            scenario["version"]["version_id"],
            calendar_file(tmp_path, start_offset=1)[0],
            write_json(tmp_path / "observation.json", observation_input()),
            reason="A failed hard condition cannot be approved.",
        ),
        "VALIDATION_NOT_PASSED",
        phase="review",
    )
    assert payload["error"]["next_action"] == "new-version"
    # A repeated replay of the failed evidence returns the same failed epoch, and
    # replaying the superseded locked snapshot cannot restore the old receipt.
    replayed = ok(revalidate(scenario, evidence_snapshot_id=successor))
    assert replayed["reused"] is True
    assert replayed["validation_id"] == failed["validation_id"]
    rejected(
        validate_version(
            scenario["project"],
            scenario["store"],
            scenario["snapshots"],
            scenario["opened"]["round_id"],
            scenario["version"]["version_id"],
        ),
        "REVALIDATION_SUPERSEDED",
        phase="validation",
    )


# ---------------------------------------------------------------------------
# 4. A correction that removes the scored history is insufficient evidence
# ---------------------------------------------------------------------------


def test_correction_without_enough_scored_history_is_insufficient_evidence(
    tmp_path: Path,
) -> None:
    scenario = open_scenario(
        tmp_path, family="momentum", parameters={"lookback": 2, "cash_if_negative": False}
    )
    assert scenario["prior"]["outcome"] == "passed"
    successor = corrected_successor(
        tmp_path, scenario["snapshots"], scenario["campaign"]["snapshot_id"]
    )

    insufficient = ok(revalidate(scenario, evidence_snapshot_id=successor))
    assert insufficient["outcome"] == "insufficient_evidence"
    assert insufficient["reason"] == "SCORING_START_INSUFFICIENT"
    assert insufficient["recomputed_discovery"]["candidate"]["signal_gaps"] > 0
    assert insufficient["recomputed_discovery"]["candidate"]["fill_count"] == 0
    assert insufficient["recomputed_discovery"]["candidate"]["ending_equity"] == 100000.0
    rejected(
        approve_version(
            scenario["project"],
            scenario["store"],
            scenario["opened"]["round_id"],
            scenario["version"]["version_id"],
            calendar_file(tmp_path, start_offset=1)[0],
            write_json(tmp_path / "observation.json", observation_input()),
            reason="Insufficient evidence cannot be approved.",
        ),
        "VALIDATION_NOT_PASSED",
        phase="review",
    )


# ---------------------------------------------------------------------------
# 5. Unrelated and non-successor evidence is a typed rejection
# ---------------------------------------------------------------------------


def test_unrelated_and_non_successor_evidence_is_rejected(tmp_path: Path) -> None:
    scenario = open_scenario(tmp_path)
    predecessor = scenario["campaign"]["snapshot_id"]
    unrelated = ok(
        cli(
            "snapshot",
            "seal",
            "--input",
            str(write_json(tmp_path / "unrelated.json", corrected_payload(day_count=13))),
            "--store",
            str(scenario["snapshots"]),
        )
    )["snapshot_id"]

    rejected(
        revalidate(scenario, evidence_snapshot_id=unrelated),
        "EVIDENCE_NOT_SUCCESSOR",
        phase="revalidation",
    )
    rejected(
        revalidate(scenario, evidence_snapshot_id=predecessor),
        "EVIDENCE_NOT_SUCCESSOR",
        phase="revalidation",
    )
    rejected(
        revalidate(scenario, evidence_snapshot_id="0" * 64),
        "EVIDENCE_SNAPSHOT_NOT_FOUND",
        phase="revalidation",
    )

    # A successor of a different sealed snapshot has the wrong lineage for this
    # locked version even though it lives in the same store.
    other_base = corrected_payload()
    other_base["corporate_actions"][0]["cash_per_share"] = 0.3
    wrong_base = ok(
        cli(
            "snapshot",
            "seal",
            "--input",
            str(write_json(tmp_path / "wrong-base.json", other_base)),
            "--store",
            str(scenario["snapshots"]),
        )
    )["snapshot_id"]
    wrong_lineage = corrected_successor(
        tmp_path,
        scenario["snapshots"],
        wrong_base,
        old_amount=0.3,
        new_amount=0.28,
        tag="wrong-lineage",
    )
    rejected(
        revalidate(scenario, evidence_snapshot_id=wrong_lineage),
        "EVIDENCE_NOT_SUCCESSOR",
        phase="revalidation",
    )

    # A store without the locked snapshot cannot reproduce the frozen Discovery
    # partition and holds no successor: the original selection reason cannot be
    # replayed at all.
    other_store = tmp_path / "other-store"
    ok(
        cli(
            "snapshot",
            "seal",
            "--input",
            str(write_json(tmp_path / "other.json", corrected_payload(day_count=13))),
            "--store",
            str(other_store),
        )
    )
    unrelated_scenario = {**scenario, "snapshots": other_store}
    rejected(revalidate(unrelated_scenario), "REPLAY_MISMATCH", phase="revalidation")

    current = version_status(
        scenario["project"],
        scenario["opened"]["round_id"],
        scenario["version"]["version_id"],
    )
    assert current["validation"]["validation_id"] == scenario["prior"]["validation_id"]
    assert "history" not in current["validation"]


# ---------------------------------------------------------------------------
# 6. Ambiguous successors require an explicit evidence snapshot
# ---------------------------------------------------------------------------


def test_ambiguous_successors_require_the_explicit_evidence_snapshot(
    tmp_path: Path,
) -> None:
    scenario = open_scenario(tmp_path)
    first = corrected_successor(
        tmp_path, scenario["snapshots"], scenario["campaign"]["snapshot_id"]
    )
    second = corrected_successor(
        tmp_path,
        scenario["snapshots"],
        scenario["campaign"]["snapshot_id"],
        new_amount=0.19,
        tag="second",
    )
    rejected(
        revalidate(scenario),
        "EVIDENCE_SNAPSHOT_AMBIGUOUS",
        phase="revalidation",
    )
    chosen = ok(revalidate(scenario, evidence_snapshot_id=second))
    assert chosen["outcome"] == "passed"
    assert chosen["evidence"]["snapshot_id"] == second
    assert chosen["evidence"]["snapshot_id"] != first


# ---------------------------------------------------------------------------
# 7. A correction published after the frozen as-of cannot be accepted
# ---------------------------------------------------------------------------


def test_correction_published_after_the_frozen_as_of_is_rejected(
    tmp_path: Path,
) -> None:
    scenario = open_scenario(tmp_path, as_of="2024-01-09T15:00:00+08:00")
    assert scenario["prior"]["outcome"] == "passed"
    successor = corrected_successor(
        tmp_path,
        scenario["snapshots"],
        scenario["campaign"]["snapshot_id"],
        available_at="2024-01-10T09:00:00+08:00",
    )
    rejected(
        revalidate(scenario, evidence_snapshot_id=successor),
        "EVIDENCE_AS_OF_VIOLATION",
        phase="revalidation",
    )
    current = version_status(
        scenario["project"],
        scenario["opened"]["round_id"],
        scenario["version"]["version_id"],
    )
    assert current["validation"]["validation_id"] == scenario["prior"]["validation_id"]

    # The successful correction of the same evidence stays a normal revalidation.
    timely = corrected_successor(
        tmp_path,
        scenario["snapshots"],
        scenario["campaign"]["snapshot_id"],
        new_amount=0.17,
        tag="timely",
    )
    accepted = ok(revalidate(scenario, evidence_snapshot_id=timely))
    assert accepted["outcome"] == "passed"
    assert accepted["evidence"]["as_of"] == "2024-01-09T15:00:00+08:00"
