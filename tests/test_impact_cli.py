"""Public-CLI tests for dividend-correction impact re-review and signal pause (#31).

Everything here is synthetic: two approved releases are produced by the real
protected-evaluation + approval flow, then each test copies the shared trusted
store so lifecycle and disposition timelines stay isolated.  A verified
revision is filed through the public ``snapshot correction ...`` workflow; the
release side is exercised through ``prajnatrade release disposition ...``.

The classification evidence is the pair (official availability, plan freeze):
the plans here are frozen on a fixed synthetic instant by rewriting the trusted
plan's ``frozen_at`` *before* approval (``frozen_at`` is deliberately outside
``plan_id``), which is the only way to express "the official correction came
later than the evidence freeze" without waiting in real time.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from cli_helpers import cli, ok, rejected, write_json
from test_protected_eval_cli import (
    EVIDENCE,
    SYMBOLS,
    authorize,
    create_plan,
    freeze_candidate,
    plan_input,
    seal_snapshot,
    submit_experiment,
    synthetic_snapshot,
)
from test_shadow_cli import FUTURE, MOMENTUM_SYMBOL, shadow_feed

pytestmark = pytest.mark.isolation

PLAN_FROZEN_AT = "2024-01-10T00:00:00+00:00"
EXPOSED_EX_DATE = "2024-01-15"  # inside the Validation partition (2024-01-13..16)
DISCOVERY_EX_DATE = "2024-01-04"  # inside Discovery (2024-01-01..12)

REVIEWER = "reviewer-impact"
REASON = "Verified official revision reviewed against the frozen evidence."
REVIEW_EVIDENCE = (
    "Re-review evidence: the frozen judgement was compared with the verified "
    "revision and the stored protected record."
)


# ---------------------------------------------------------------------------
# Shared real-flow fixtures: two approved releases + verified revisions
# ---------------------------------------------------------------------------


def _create_campaign(project: Path, snapshot_store: Path, snapshot_id: str) -> dict:
    return ok(
        cli(
            "campaign",
            "create",
            "--project", str(project),
            "--store", str(snapshot_store),
            "--snapshot-id", snapshot_id,
            "--attempts", "6",
            "--max-seconds", "60",
            "--initial-cash", "100000",
            "--transaction-cost-bps", "5",
            "--slippage-bps", "10",
        )
    )


def _set_plan_frozen_at(store: Path, plan: dict, frozen_at: str) -> None:
    """Pin the evidence-freeze instant before approval (excluded from plan_id)."""
    path = store / "plans" / f"{plan['plan_id']}.json"
    path.chmod(0o644)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["frozen_at"] != frozen_at
    payload["frozen_at"] = frozen_at
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o400)


def _build_release(
    root: Path,
    store: Path,
    payload: dict,
    name: str,
    *,
    frozen_at: str = PLAN_FROZEN_AT,
    family: str = "momentum",
    parameters: dict | None = None,
) -> dict:
    """Seal a snapshot, run both protected stages and approve one release."""
    sealed = seal_snapshot(root, payload, f"snapshots-{name}")
    project = root / f"project-{name}"
    ok(cli("project", "init", "--project", str(project)))
    campaign = _create_campaign(project, sealed["store"], sealed["snapshot_id"])
    source = root / f"submission-{name}.json"
    submit_experiment(
        project,
        campaign["campaign_id"],
        source,
        "first",
        "Synthetic candidate for the dividend-correction impact ticket.",
        family,
        {"lookback": 2} if parameters is None else parameters,
    )
    candidate = freeze_candidate(
        project,
        campaign["campaign_id"],
        "first",
        f"candidate-{name}",
        "Frozen candidate for the dividend-correction impact ticket.",
    )
    base = {
        "root": root,
        "snapshot_store": sealed["store"],
        "snapshot_id": sealed["snapshot_id"],
        "manifest": sealed["manifest"],
        "partitions": sealed["partitions"],
        "project": project,
        "campaign": campaign,
        "candidate": candidate,
    }
    plan = create_plan(base, store, candidate, plan_input(), name=f"plan-{name}")
    _set_plan_frozen_at(store, plan, frozen_at)
    ok(
        cli(
            "evaluation", "authorize",
            "--store", str(store),
            "--plan-id", plan["plan_id"],
            "--stage", "validation",
            "--reviewer", "reviewer-1",
            "--reason", "Frozen plan reviewed before Validation.",
        )
    )
    validation = ok(
        cli(
            "evaluation", "run",
            "--store", str(store),
            "--snapshot-store", str(sealed["store"]),
            "--plan-id", plan["plan_id"],
            "--stage", "validation",
            "--project", str(project),
        )
    )
    ok(
        cli(
            "evaluation", "authorize",
            "--store", str(store),
            "--plan-id", plan["plan_id"],
            "--stage", "final_holdout",
            "--reviewer", "reviewer-2",
            "--reason", "Validation passed with complete evidence.",
        )
    )
    holdout = ok(
        cli(
            "evaluation", "run",
            "--store", str(store),
            "--snapshot-store", str(sealed["store"]),
            "--plan-id", plan["plan_id"],
            "--stage", "final_holdout",
            "--project", str(project),
        )
    )
    assert validation["outcome"] == "passed", validation
    assert holdout["outcome"] == "passed", holdout
    approved = ok(
        cli(
            "release", "approve",
            "--store", str(store),
            "--plan-id", plan["plan_id"],
            "--reviewer", f"reviewer-{name}",
            "--reason", "Both protected stages passed with complete evidence.",
        )
    )
    assert approved["artifact"]["plan_frozen_at"] == frozen_at
    return {
        "name": name,
        "store": store,
        "base": base,
        "plan": plan,
        "release_id": approved["release_id"],
        "artifact": approved["artifact"],
        "candidate": candidate,
    }


def _revision(
    root: Path,
    snapshot_store: Path,
    predecessor_id: str,
    *,
    ex_date: str,
    old_amount: float,
    new_amount: float,
    official_available_at: str,
    tag: str,
) -> dict:
    """File a pending observation and verify it into the revision journal."""
    proposal = {
        "schema_version": 1,
        "predecessor_id": predecessor_id,
        "symbol": SYMBOLS[0],
        "ex_date": ex_date,
        "currency": "CNY",
        "old_amount": old_amount,
        "new_amount": new_amount,
        "supplier": {
            "source": "synthetic supplier",
            "archive_reference": f"synthetic-archive:{tag}",
            "observed_at": "2024-02-02T10:00:00+08:00",
        },
    }
    proposal_path = write_json(root / f"proposal-{tag}.json", proposal)
    pending = ok(
        cli(
            "snapshot", "correction", "register",
            "--input", str(proposal_path),
            "--store", str(snapshot_store),
        )
    )
    notice = {
        "kind": "synthetic",
        "authority": "fund_manager",
        "issuer": "Synthetic Fund Manager",
        "url": "https://notices.example.invalid/dividend",
        "reference": f"synthetic-notice-{tag}",
        "symbol": SYMBOLS[0],
        "ex_date": ex_date,
        "currency": "CNY",
        "amount": new_amount,
        "available_at": official_available_at,
        "collected_at": "2024-02-02T09:00:00+08:00",
    }
    notice_path = write_json(root / f"notice-{tag}.json", notice)
    return ok(
        cli(
            "snapshot", "correction", "verify",
            "--input", str(notice_path),
            "--store", str(snapshot_store),
            "--proposal-id", pending["proposal_id"],
        )
    )


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> dict:
    root = tmp_path_factory.mktemp("correction-impact")
    store = root / "eval"
    store.mkdir()

    exposed_payload = synthetic_snapshot()
    exposed_payload["corporate_actions"] = [
        {
            "date": EXPOSED_EX_DATE,
            "symbol": SYMBOLS[0],
            "type": "cash_dividend",
            "cash_per_share": 0.20,
            "pay_date": "2024-01-17",
            "terms_evidence": "synthetic dividend terms",
            **EVIDENCE,
        }
    ]
    exposed = _build_release(root, store, exposed_payload, "exposed")
    # The unaffected release must use different protected content hashes: one
    # sealed range grants exactly one query allowance across the whole store.
    unaffected = _build_release(
        root,
        store,
        synthetic_snapshot(derived_range=("2024-01-13", "2024-01-20")),
        "unaffected",
    )
    assert exposed["release_id"] != unaffected["release_id"]

    # A second frozen candidate for the same exposed snapshot, used to prove a
    # consumed protected range cannot be reused by a new plan (no new allowance).
    exposed_project = exposed["base"]["project"]
    exposed_campaign = exposed["base"]["campaign"]
    submit_experiment(
        exposed_project,
        exposed_campaign["campaign_id"],
        root / "submission-second.json",
        "second",
        "A revised parameter neighbourhood for the no-new-allowance assertion.",
        "momentum",
        {"lookback": 3},
    )
    second_candidate = freeze_candidate(
        exposed_project,
        exposed_campaign["campaign_id"],
        "second",
        "candidate-second",
        "A revised candidate on the already consumed snapshot.",
    )

    # Verified revisions: supplier wrong at freeze time, late official change,
    # and an action outside both evaluated ranges.
    pending_revision = _revision(
        root,
        exposed["base"]["snapshot_store"],
        exposed["base"]["snapshot_id"],
        ex_date=EXPOSED_EX_DATE,
        old_amount=0.20,
        new_amount=0.18,
        official_available_at="2024-01-04T09:00:00+08:00",
        tag="supplier-wrong",
    )
    late_revision = _revision(
        root,
        exposed["base"]["snapshot_store"],
        exposed["base"]["snapshot_id"],
        ex_date=EXPOSED_EX_DATE,
        old_amount=0.20,
        new_amount=0.18,
        official_available_at="2024-01-20T09:00:00+08:00",
        tag="official-late",
    )
    outside_revision = _revision(
        root,
        unaffected["base"]["snapshot_store"],
        unaffected["base"]["snapshot_id"],
        ex_date=DISCOVERY_EX_DATE,
        old_amount=0.20,
        new_amount=0.18,
        official_available_at="2024-01-03T09:00:00+08:00",
        tag="outside-ranges",
    )
    return {
        "root": root,
        "store": store,
        "exposed": exposed,
        "unaffected": unaffected,
        "second_candidate": second_candidate,
        "pending_revision": pending_revision,
        "late_revision": late_revision,
        "outside_revision": outside_revision,
    }


# ---------------------------------------------------------------------------
# Per-test helpers
# ---------------------------------------------------------------------------


def clone_store(world: dict, tmp_path: Path) -> Path:
    target = tmp_path / "store"
    shutil.copytree(world["store"], target)
    return target


def clone_snapshot_store(world: dict, tmp_path: Path, entry: dict) -> Path:
    target = tmp_path / "snapshots"
    shutil.copytree(entry["base"]["snapshot_store"], target)
    return target


def lifecycle(action: str, store: Path, release_id: str, date: str, *, reason: str = "Pre-registered after-close signal point."):
    return cli(
        "release", action,
        "--store", str(store),
        "--release-id", release_id,
        "--effective-signal-date", date,
        "--reason", reason,
    )


def disposition(
    store: Path,
    snapshot_store: Path,
    release_id: str,
    revision_id: str,
    *,
    impact: str | None = None,
    evidence: str | None = None,
    ledger: Path | None = None,
    data: Path | None = None,
    date: str | None = None,
    reviewer: str = REVIEWER,
    reason: str = REASON,
):
    arguments = [
        "release", "disposition",
        "--store", str(store),
        "--snapshot-store", str(snapshot_store),
        "--release-id", release_id,
        "--revision-id", revision_id,
        "--reviewer", reviewer,
        "--reason", reason,
    ]
    if impact is not None:
        arguments += ["--impact", impact]
    if evidence is not None:
        arguments += ["--evidence", evidence]
    if ledger is not None:
        arguments += ["--ledger", str(ledger)]
    if data is not None:
        arguments += ["--data", str(data)]
    if date is not None:
        arguments += ["--effective-signal-date", date]
    return cli(*arguments)


def status(store: Path, release_id: str, *, as_of: str | None = None):
    arguments = ["release", "status", "--store", str(store), "--release-id", release_id]
    if as_of is not None:
        arguments += ["--as-of", as_of]
    return cli(*arguments)


def shadow_update(ledger: Path, store: Path, data: Path, date: str):
    return cli(
        "shadow", "update",
        "--ledger", str(ledger),
        "--store", str(store),
        "--data", str(data),
        "--date", date,
        # These tests exercise #31 pause points, not #33 correction deltas.
        "--corrections-store", str(ledger.parent),
    )


def shadow_report(ledger: Path, release_id: str, output: Path):
    return cli(
        "shadow", "report",
        "--ledger", str(ledger),
        "--version", release_id,
        "--output", str(output),
    )


def day_record(ledger: Path, release_id: str, output: Path, date: str) -> dict:
    report = ok(shadow_report(ledger, release_id, output))
    assert report["integrity"]["verified"] is True
    stored = json.loads(output.read_text(encoding="utf-8"))
    assert stored["integrity"]["verified"] is True
    for entry in stored["days"]:
        if entry["date"] == date:
            return entry
    raise AssertionError(f"no committed day {date}")


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def protected_hashes(store: Path, entry: dict) -> dict:
    """Hashes of the objects a disposition must never rewrite."""
    release_id = entry["release_id"]
    artifact = store / "releases" / release_id / "artifact.json"
    plan = store / "plans" / f"{entry['plan']['plan_id']}.json"
    records = {}
    for stage in ("validation", "final_holdout"):
        evaluation_id = entry["artifact"]["evaluations"][stage]["evaluation_id"]
        path = store / "records" / evaluation_id / "record.json"
        records[stage] = file_hash(path)
    return {
        "artifact": file_hash(artifact),
        "plan": file_hash(plan),
        "records": records,
        "query_ledger": (
            file_hash(store / "queries.sqlite")
            if (store / "queries.sqlite").is_file()
            else None
        ),
    }


# ---------------------------------------------------------------------------
# 1. A late official correction keeps the old evidence valid and never pauses.
# ---------------------------------------------------------------------------


def test_late_official_correction_is_sustained_and_never_pauses(
    world: dict, tmp_path: Path
) -> None:
    entry = world["exposed"]
    release_id = entry["release_id"]
    store = clone_store(world, tmp_path)
    snapshot_store = entry["base"]["snapshot_store"]
    before = protected_hashes(store, entry)

    ok(lifecycle("enable", store, release_id, "2030-02-01"))
    ok(
        lifecycle(
            "pause", store, release_id, "2030-02-05",
            reason="Manual pause for an unrelated operator review.",
        )
    )

    result = ok(
        disposition(
            store,
            snapshot_store,
            release_id,
            world["late_revision"]["revision_id"],
        )
    )
    assert result["schema_version"] == 1
    assert result["status"] == "recorded"
    assert result["release_id"] == release_id
    assert result["revision_id"] == world["late_revision"]["revision_id"]
    assert result["impact"] == "sustained"
    assert result["implication"] == "evidence_still_supported"
    assert result["paused_event"] is None
    assert result["admission"]["valid"] is True
    assert result["admission"]["reason"] is None
    assert result["classification"]["method"] == "official_availability_vs_plan_freeze"
    assert result["classification"]["exposure"] == "evaluated"
    assert result["classification"]["exposed_stages"] == ["validation"]
    assert result["classification"]["official_available_at"] == (
        world["late_revision"]["revision"]["official_available_at"]
    )
    assert result["classification"]["plan_frozen_at"] == PLAN_FROZEN_AT
    # The disposition is stored write-once under the release.
    path = Path(result["disposition_path"])
    assert path.is_file()
    assert path.stat().st_mode & 0o777 == 0o444

    current = ok(status(store, release_id))
    assert [event["action"] for event in current["timeline"]] == ["enable", "pause"]
    assert current["state"] == "paused"
    assert current["admission"]["valid"] is True

    # The manual pause was for another reason, so the same ID resumes normally.
    resumed = ok(lifecycle("resume", store, release_id, "2030-02-10"))
    assert resumed["state"] == "enabled"
    assert resumed["release_id"] == release_id

    # A sustained disposition while enabled still never pauses.
    again = ok(
        disposition(
            store,
            snapshot_store,
            release_id,
            world["late_revision"]["revision_id"],
            reason="Re-checked after the manual resume; evidence is still supported.",
        )
    )
    assert again["impact"] == "sustained"
    assert again["paused_event"] is None
    assert again["admission"]["valid"] is True
    current = ok(status(store, release_id))
    assert [event["action"] for event in current["timeline"]] == [
        "enable",
        "pause",
        "resume",
    ]
    assert current["state"] == "enabled"
    assert current["active"] is True

    # Nothing outside the disposition/event store changed.
    assert protected_hashes(store, entry) == before


# ---------------------------------------------------------------------------
# 2. An ex-date outside both evaluated ranges is not applicable: no pause.
# ---------------------------------------------------------------------------


def test_out_of_range_revision_is_not_applicable_and_other_versions_run(
    world: dict, tmp_path: Path
) -> None:
    exposed_id = world["exposed"]["release_id"]
    unaffected = world["unaffected"]
    unaffected_id = unaffected["release_id"]
    store = clone_store(world, tmp_path)
    sessions = FUTURE[:4]
    feed = write_json(
        tmp_path / "feed.json",
        shadow_feed(
            sessions,
            closes={MOMENTUM_SYMBOL: [10.0, 10.1, 10.2, 10.3]},
        ),
    )
    ok(lifecycle("enable", store, unaffected_id, sessions[0]))
    ok(lifecycle("enable", store, exposed_id, sessions[0]))

    result = ok(
        disposition(
            store,
            unaffected["base"]["snapshot_store"],
            unaffected_id,
            world["outside_revision"]["revision_id"],
            reason="The revised ex-date is outside both evaluated protected ranges.",
        )
    )
    assert result["impact"] == "not_applicable"
    assert result["implication"] == "release_not_exposed_no_action"
    assert result["paused_event"] is None
    assert result["admission"]["valid"] is True
    assert result["classification"]["exposure"] == "not_applicable"
    assert result["classification"]["exposed_stages"] == []
    assert result["unchanged"]["sealed_reads"] is False

    current = ok(status(store, unaffected_id))
    assert [event["action"] for event in current["timeline"]] == ["enable"]
    assert current["state"] == "enabled"
    assert ok(status(store, unaffected_id, as_of=sessions[2]))["state"] == "enabled"

    # The unaffected version and the other enabled version both keep signalling.
    update = ok(shadow_update(tmp_path / "ledger", store, feed, sessions[2]))
    assert update["versions"]
    ledger = tmp_path / "ledger"
    for release_id in (unaffected_id, exposed_id):
        day = day_record(
            ledger, release_id, tmp_path / f"report-{release_id[:8]}.json", sessions[2]
        )
        assert day["result"]["release_state_at_signal_point"] == "enabled"
        assert day["result"]["signal"] is not None


# ---------------------------------------------------------------------------
# 2b. Exposure requires the release snapshot to share the revision's lineage.
# ---------------------------------------------------------------------------


def test_revision_from_another_snapshot_lineage_is_not_applicable(
    world: dict, tmp_path: Path
) -> None:
    entry = world["exposed"]
    release_id = entry["release_id"]
    store = clone_store(world, tmp_path)
    snapshot_store = clone_snapshot_store(world, tmp_path, entry)

    # Seal an unrelated snapshot that carries the same dividend action, then
    # file the verified revision against that other lineage.
    payload = synthetic_snapshot(discovery_marker="2024-01-04")
    payload["corporate_actions"] = [
        {
            "date": EXPOSED_EX_DATE,
            "symbol": SYMBOLS[0],
            "type": "cash_dividend",
            "cash_per_share": 0.20,
            "pay_date": "2024-01-17",
            "terms_evidence": "synthetic dividend terms",
            **EVIDENCE,
        }
    ]
    other = seal_snapshot(tmp_path / "other", payload, "snapshots")
    shutil.copytree(
        other["store"] / other["snapshot_id"],
        snapshot_store / other["snapshot_id"],
    )
    revision = _revision(
        tmp_path / "revisions",
        snapshot_store,
        other["snapshot_id"],
        ex_date=EXPOSED_EX_DATE,
        old_amount=0.20,
        new_amount=0.17,
        official_available_at="2024-01-04T09:00:00+08:00",
        tag="other-lineage",
    )
    assert revision["revision_id"] != world["pending_revision"]["revision_id"]

    result = ok(
        disposition(
            store,
            snapshot_store,
            release_id,
            revision["revision_id"],
            reason="The ex-date is inside the range but the snapshot lineage differs.",
        )
    )
    assert result["impact"] == "not_applicable"
    assert result["paused_event"] is None
    assert result["admission"]["valid"] is True
    classification = result["classification"]
    assert classification["exposure"] == "not_applicable"
    assert classification["method"] == "outside_revision_lineage"
    assert classification["evaluated_range_stages"] == ["validation"]
    assert classification["exposed_stages"] == []
    assert classification["revision_predecessor_id"] == other["snapshot_id"]
    assert classification["release_snapshot_in_lineage"] is False
    assert entry["base"]["snapshot_id"] in classification["release_lineage"]
    assert other["snapshot_id"] not in classification["release_lineage"]
    # The mismatch is recorded, not silently dropped.
    assert "not in the revision's predecessor lineage" in classification["basis"]


# ---------------------------------------------------------------------------
# 2c. An explicit impact override needs recorded re-review evidence.
# ---------------------------------------------------------------------------


def test_impact_override_without_evidence_is_refused(
    world: dict, tmp_path: Path
) -> None:
    entry = world["exposed"]
    release_id = entry["release_id"]
    revision_id = world["pending_revision"]["revision_id"]
    store = clone_store(world, tmp_path)
    ok(lifecycle("enable", store, release_id, "2030-09-01"))

    refused = rejected(
        disposition(
            store,
            entry["base"]["snapshot_store"],
            release_id,
            revision_id,
            impact="sustained",
            reason="A bare sustained override must not erase the classification.",
        ),
        "IMPACT_EVIDENCE_REQUIRED",
    )
    assert "evidence" in refused["error"]["message"].lower()
    assert not (store / "releases" / release_id / "dispositions").exists()
    assert ok(status(store, release_id))["timeline"][-1]["action"] == "enable"

    recorded = ok(
        disposition(
            store,
            entry["base"]["snapshot_store"],
            release_id,
            revision_id,
            impact="sustained",
            evidence="The re-review compared the verified revision with the frozen record.",
            reason="The operator records a sustained override with its evidence.",
        )
    )
    assert recorded["impact"] == "sustained"
    assert recorded["classification"]["method"] == "operator_override"
    assert recorded["classification"]["computed_impact"] == "pending_review"
    assert recorded["classification"]["evidence"] == (
        "The re-review compared the verified revision with the frozen record."
    )
    assert recorded["disposition"]["evidence"] == (
        "The re-review compared the verified revision with the frozen record."
    )
    assert recorded["paused_event"] is None


# ---------------------------------------------------------------------------
# 3. A supplier error already present at freeze time is pending review: pause.
# ---------------------------------------------------------------------------


def test_supplier_error_at_freeze_pauses_at_first_uncommitted_signal_point(
    world: dict, tmp_path: Path
) -> None:
    entry = world["exposed"]
    release_id = entry["release_id"]
    revision_id = world["pending_revision"]["revision_id"]
    store = clone_store(world, tmp_path)
    sessions = FUTURE[:6]
    feed = write_json(
        tmp_path / "feed.json",
        shadow_feed(
            sessions,
            closes={MOMENTUM_SYMBOL: [10.0, 10.1, 10.2, 10.3, 10.4, 10.5]},
        ),
    )
    ledger = tmp_path / "ledger"
    ok(lifecycle("enable", store, release_id, sessions[0]))
    ok(shadow_update(ledger, store, feed, sessions[2]))
    signal_day = day_record(
        ledger, release_id, tmp_path / "report-before.json", sessions[2]
    )
    assert signal_day["result"]["signal"] is not None
    assert len(signal_day["result"]["next_orders"]) > 0

    result = ok(
        disposition(
            store,
            entry["base"]["snapshot_store"],
            release_id,
            revision_id,
            ledger=ledger,
            data=feed,
        )
    )
    assert result["impact"] == "pending_review"
    assert result["implication"] == "pending_review_pause_required"
    assert result["classification"]["method"] == "official_availability_vs_plan_freeze"
    assert result["classification"]["exposure"] == "evaluated"
    assert result["classification"]["official_available_before_or_at_freeze"] is True
    assert result["admission"]["valid"] is False
    assert result["admission"]["reason"] == "pending_review"
    paused = result["paused_event"]
    assert paused is not None
    assert paused["action"] == "pause"
    assert paused["seq"] == 2
    assert paused["effective_signal_date"] == sessions[3]
    assert paused["state_before"] == "enabled"
    assert paused["state_after"] == "paused"
    assert result["state"] == "paused"
    assert result["disposition"]["pause_effective_signal_date"] == sessions[3]
    assert result["disposition"]["pause_required"] is True

    current = ok(status(store, release_id))
    assert [event["action"] for event in current["timeline"]] == ["enable", "pause"]
    assert current["state"] == "paused"
    assert ok(status(store, release_id, as_of=sessions[3]))["state"] == "paused"

    # The pending revision keeps admission invalid: neither resume nor enable may
    # bypass the pause.
    refused = rejected(lifecycle("resume", store, release_id, sessions[4]), "ADMISSION_INVALID")
    assert refused["state"] == "paused"

    # The paused day still settles the already submitted orders and values the
    # positions; it generates no new signal and no new orders.
    paused_day = ok(shadow_update(ledger, store, feed, sessions[3]))
    assert paused_day["versions"][0]["status"] in {"committed", "committed_with_anomalies"}
    paused = day_record(ledger, release_id, tmp_path / "report-paused.json", sessions[3])[
        "result"
    ]
    assert paused["release_state_at_signal_point"] == "paused"
    assert paused["orders"] and all(order["status"] == "filled" for order in paused["orders"])
    assert paused["signal"] is None
    assert paused["next_orders"] == []
    assert paused["positions"][MOMENTUM_SYMBOL]["quantity"] > 0
    assert paused["nav"] is not None

    # A later sustained disposition for the same revision resolves the pending
    # review and allows the same ID to resume at a pre-registered date.
    cleared = ok(
        disposition(
            store,
            entry["base"]["snapshot_store"],
            release_id,
            revision_id,
            impact="sustained",
            evidence=REVIEW_EVIDENCE,
            reason="Re-review proved the frozen judgement does not depend on the amount.",
        )
    )
    assert cleared["impact"] == "sustained"
    assert cleared["classification"]["method"] == "operator_override"
    assert cleared["classification"]["timing_impact"] == "pending_review"
    assert cleared["paused_event"] is None
    assert cleared["admission"]["valid"] is True
    current = ok(status(store, release_id))
    assert [event["action"] for event in current["timeline"]] == ["enable", "pause"]

    resumed = ok(lifecycle("resume", store, release_id, sessions[4]))
    assert resumed["release_id"] == release_id
    assert resumed["state"] == "enabled"

    # The resume date is the first day that may generate a new signal; the paused
    # window is never backfilled.
    ok(shadow_update(ledger, store, feed, sessions[4]))
    resumed_day = day_record(
        ledger, release_id, tmp_path / "report-resumed.json", sessions[4]
    )["result"]
    assert resumed_day["release_state_at_signal_point"] == "enabled"
    assert resumed_day["signal"] is not None
    assert resumed_day["signal"]["date"] == sessions[4]
    assert resumed_day["signal"]["execution_date"] == sessions[5]
    assert paused["signal"] is None  # the paused day stays signal-free


# ---------------------------------------------------------------------------
# 3b. The pause point is the first uncommitted signal point, never a later day.
# ---------------------------------------------------------------------------


def test_ledger_derived_pause_point_rejects_a_later_explicit_date(
    world: dict, tmp_path: Path
) -> None:
    entry = world["exposed"]
    release_id = entry["release_id"]
    revision_id = world["pending_revision"]["revision_id"]
    store = clone_store(world, tmp_path)
    sessions = FUTURE[:6]
    feed = write_json(
        tmp_path / "feed.json",
        shadow_feed(
            sessions,
            closes={MOMENTUM_SYMBOL: [10.0, 10.1, 10.2, 10.3, 10.4, 10.5]},
        ),
    )
    ledger = tmp_path / "ledger"
    ok(lifecycle("enable", store, release_id, sessions[0]))
    ok(shadow_update(ledger, store, feed, sessions[2]))
    assert ok(status(store, release_id))["timeline"][-1]["effective_signal_date"] == (
        sessions[0]
    )

    # The first not-yet-committed after-close signal point is sessions[3]; a
    # later explicit date (or any other date) must not silently pass.
    for later in (sessions[4], sessions[5]):
        refused = rejected(
            disposition(
                store,
                entry["base"]["snapshot_store"],
                release_id,
                revision_id,
                ledger=ledger,
                data=feed,
                date=later,
            ),
            "EFFECTIVE_SIGNAL_DATE_MISMATCH",
        )
        assert refused["derived_signal_date"] == sessions[3]
        assert refused["effective_signal_date"] == later
        assert ok(status(store, release_id))["timeline"] == [
            event
            for event in ok(status(store, release_id))["timeline"]
        ]
        assert not (store / "releases" / release_id / "dispositions").exists()

    # Supplying the derived point explicitly is accepted and pauses there.
    result = ok(
        disposition(
            store,
            entry["base"]["snapshot_store"],
            release_id,
            revision_id,
            ledger=ledger,
            data=feed,
            date=sessions[3],
        )
    )
    assert result["impact"] == "pending_review"
    assert result["paused_event"]["effective_signal_date"] == sessions[3]
    assert result["disposition"]["pause_effective_signal_date"] == sessions[3]

    # Without --ledger/--data an explicit date is still required.
    other_root = tmp_path / "other"
    other_root.mkdir()
    other = clone_store(world, other_root)
    ok(lifecycle("enable", other, release_id, sessions[0]))
    rejected(
        disposition(
            other,
            entry["base"]["snapshot_store"],
            release_id,
            revision_id,
        ),
        "EFFECTIVE_SIGNAL_DATE_REQUIRED",
    )


# ---------------------------------------------------------------------------
# 4. ``no_longer_supported`` is final: no resume, no enable, no reused range.
# ---------------------------------------------------------------------------


def test_no_longer_supported_is_final_and_requires_a_new_release(
    world: dict, tmp_path: Path
) -> None:
    entry = world["exposed"]
    release_id = entry["release_id"]
    revision_id = world["pending_revision"]["revision_id"]
    store = clone_store(world, tmp_path)
    before = protected_hashes(store, entry)
    ok(lifecycle("enable", store, release_id, "2030-05-01"))

    result = ok(
        disposition(
            store,
            entry["base"]["snapshot_store"],
            release_id,
            revision_id,
            impact="no_longer_supported",
            evidence=REVIEW_EVIDENCE,
            date="2030-05-05",
            reason="The frozen judgement depended on an amount that cannot stand.",
        )
    )
    assert result["impact"] == "no_longer_supported"
    assert result["implication"] == "no_longer_supported_cannot_resume"
    assert result["admission"]["valid"] is False
    assert result["admission"]["reason"] == "no_longer_supported"
    assert result["paused_event"]["effective_signal_date"] == "2030-05-05"
    assert result["state"] == "paused"

    rejected(lifecycle("resume", store, release_id, "2030-05-10"), "ADMISSION_INVALID")

    # Even a later sustained record for the same revision cannot clear it.
    later = ok(
        disposition(
            store,
            entry["base"]["snapshot_store"],
            release_id,
            revision_id,
            impact="sustained",
            evidence=REVIEW_EVIDENCE,
            reason="A late re-review claim that must not resurrect a final refusal.",
        )
    )
    assert later["impact"] == "sustained"
    assert later["admission"]["valid"] is False
    assert later["admission"]["reason"] == "no_longer_supported"
    assert later["admission"]["dispositions"][0]["impact"] == "no_longer_supported"
    rejected(lifecycle("resume", store, release_id, "2030-05-10"), "ADMISSION_INVALID")
    assert protected_hashes(store, entry) == before

    # A release whose evidence is no longer supported must go through a new
    # frozen candidate/plan/approval/new ID; the consumed sealed range cannot be
    # reused as a fresh allowance.
    base = entry["base"]
    reuse = create_plan(
        {"root": tmp_path},
        store,
        world["second_candidate"],
        plan_input(),
        name="plan-reuse-consumed",
        project=base["project"],
        snapshot_store=base["snapshot_store"],
    )
    ok(authorize(store, reuse["plan_id"]))
    replay = rejected(
        cli(
            "evaluation", "run",
            "--store", str(store),
            "--snapshot-store", str(base["snapshot_store"]),
            "--plan-id", reuse["plan_id"],
            "--stage", "validation",
            "--project", str(base["project"]),
        ),
        "QUERY_ALREADY_CONSUMED",
    )
    assert replay["opened_protected_source"] is False
    assert replay["query_debited"] is False
    assert protected_hashes(store, entry) == before


def test_published_release_with_invalid_admission_cannot_be_enabled(
    world: dict, tmp_path: Path
) -> None:
    entry = world["exposed"]
    release_id = entry["release_id"]
    store = clone_store(world, tmp_path)
    result = ok(
        disposition(
            store,
            entry["base"]["snapshot_store"],
            release_id,
            world["pending_revision"]["revision_id"],
            impact="pending_review",
            evidence=REVIEW_EVIDENCE,
            reason="Recorded before the release was ever enabled.",
        )
    )
    # Not currently enabled: the disposition is recorded without inventing an event.
    assert result["paused_event"] is None
    assert result["state"] == "published"
    assert result["admission"]["valid"] is False
    assert not (store / "releases" / release_id / "events").exists()

    rejected(lifecycle("enable", store, release_id, "2030-07-01"), "ADMISSION_INVALID")
    assert ok(status(store, release_id))["state"] == "published"


# ---------------------------------------------------------------------------
# 5. Shadow Live: paused version settles, unaffected version keeps signalling.
# ---------------------------------------------------------------------------


def test_paused_version_settles_but_never_backfills_a_resumed_signal(
    world: dict, tmp_path: Path
) -> None:
    exposed = world["exposed"]
    unaffected_id = world["unaffected"]["release_id"]
    release_id = exposed["release_id"]
    revision_id = world["pending_revision"]["revision_id"]
    store = clone_store(world, tmp_path)
    sessions = [*FUTURE, "2030-01-10"]
    feed = write_json(
        tmp_path / "feed.json",
        shadow_feed(
            sessions,
            closes={
                MOMENTUM_SYMBOL: [10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6]
            },
        ),
    )
    ledger = tmp_path / "ledger"
    ok(lifecycle("enable", store, release_id, sessions[0]))
    ok(lifecycle("enable", store, unaffected_id, sessions[0]))
    ok(shadow_update(ledger, store, feed, sessions[2]))
    before = day_record(
        ledger, release_id, tmp_path / "report-signal.json", sessions[2]
    )["result"]
    assert before["release_state_at_signal_point"] == "enabled"
    assert before["signal"] is not None
    submitted = [order["order_id"] for order in before["next_orders"]]
    assert submitted
    nav_before_pause = before["nav"]

    result = ok(
        disposition(
            store,
            exposed["base"]["snapshot_store"],
            release_id,
            revision_id,
            ledger=ledger,
            data=feed,
        )
    )
    assert result["impact"] == "pending_review"
    assert result["paused_event"]["effective_signal_date"] == sessions[3]

    # The pause date settles the orders submitted before the pause, but the
    # version generates no new signal or orders.
    ok(shadow_update(ledger, store, feed, sessions[3]))
    paused = day_record(
        ledger, release_id, tmp_path / "report-paused.json", sessions[3]
    )["result"]
    assert paused["release_state_at_signal_point"] == "paused"
    assert [order["order_id"] for order in paused["orders"]] == submitted
    assert all(order["status"] == "filled" for order in paused["orders"])
    assert len(paused["fills"]) == len(submitted)
    assert paused["signal"] is None
    assert paused["next_orders"] == []
    assert paused["positions"][MOMENTUM_SYMBOL]["quantity"] > 0
    assert paused["nav"] is not None

    ok(shadow_update(ledger, store, feed, sessions[4]))
    still_paused = day_record(
        ledger, release_id, tmp_path / "report-still-paused.json", sessions[4]
    )["result"]
    assert still_paused["release_state_at_signal_point"] == "paused"
    assert still_paused["signal"] is None
    assert still_paused["next_orders"] == []
    assert still_paused["nav"] != nav_before_pause
    assert still_paused["positions"][MOMENTUM_SYMBOL]["quantity"] > 0

    # The other enabled version kept generating signals and orders throughout.
    for date in (sessions[3], sessions[4]):
        other = day_record(
            ledger,
            unaffected_id,
            tmp_path / f"report-other-{date}.json",
            date,
        )["result"]
        assert other["release_state_at_signal_point"] == "enabled"
        assert other["signal"] is not None

    # Clearing the pending review resumes the same ID at the first uncommitted
    # signal point; the paused window is never backfilled.
    cleared = ok(
        disposition(
            store,
            exposed["base"]["snapshot_store"],
            release_id,
            revision_id,
            impact="sustained",
            evidence=REVIEW_EVIDENCE,
            reason="Re-review confirmed the frozen judgement is unaffected.",
        )
    )
    assert cleared["admission"]["valid"] is True
    resumed = ok(lifecycle("resume", store, release_id, sessions[5]))
    assert resumed["release_id"] == release_id
    ok(shadow_update(ledger, store, feed, sessions[5]))
    resumed_day = day_record(
        ledger, release_id, tmp_path / "report-resumed.json", sessions[5]
    )["result"]
    assert resumed_day["release_state_at_signal_point"] == "enabled"
    assert resumed_day["signal"] is not None
    assert resumed_day["signal"]["date"] == sessions[5]
    for date in (sessions[3], sessions[4]):
        assert (
            day_record(
                ledger, release_id, tmp_path / f"report-check-{date}.json", date
            )["result"]["signal"]
            is None
        )


# ---------------------------------------------------------------------------
# 6. No sealed payload read and no query-ledger debit.
# ---------------------------------------------------------------------------


def test_disposition_reads_no_sealed_payload_and_debits_no_query(
    world: dict, tmp_path: Path
) -> None:
    entry = world["exposed"]
    release_id = entry["release_id"]
    store = clone_store(world, tmp_path)
    snapshot_store = clone_snapshot_store(world, tmp_path, entry)
    snapshot_id = entry["base"]["snapshot_id"]
    directory = snapshot_store / snapshot_id
    directory.chmod(0o755)
    sealed_paths = [directory / "validation.json", directory / "final_holdout.json"]
    for path in sealed_paths:
        path.unlink()
    directory.chmod(0o555)

    ok(lifecycle("enable", store, release_id, "2030-06-01"))
    before = protected_hashes(store, entry)
    with sqlite3.connect(store / "queries.sqlite") as connection:
        consumed_before = connection.execute(
            "SELECT COUNT(*) FROM consumed_queries"
        ).fetchone()[0]
    assert consumed_before == 4  # two releases x two protected stages, from the fixture

    result = ok(
        disposition(
            store,
            snapshot_store,
            release_id,
            world["pending_revision"]["revision_id"],
            impact="pending_review",
            evidence=REVIEW_EVIDENCE,
            date="2030-06-05",
            reason="Reviewed from the journal only; the sealed payloads stay closed.",
        )
    )
    assert result["impact"] == "pending_review"
    assert result["unchanged"] == {
        "query_ledger": True,
        "sealed_reads": False,
        "artifact": True,
        "evaluation_records": True,
        "snapshot_partitions": True,
    }
    assert result["admission"]["valid"] is False
    assert protected_hashes(store, entry) == before
    for path in sealed_paths:
        assert not path.exists()
    # The verified revision journal survived; nothing re-created a payload.
    journal = snapshot_store / ".dividend-corrections"
    assert list(journal.glob("revision-*.json"))
    with sqlite3.connect(store / "queries.sqlite") as connection:
        consumed_after = connection.execute(
            "SELECT COUNT(*) FROM consumed_queries"
        ).fetchone()[0]
    assert consumed_after == consumed_before  # no new debit


# ---------------------------------------------------------------------------
# 7. The research/agent surface cannot disposition or read admission.
# ---------------------------------------------------------------------------


def test_agent_surface_cannot_disposition_or_read_admission(
    world: dict, tmp_path: Path
) -> None:
    entry = world["exposed"]
    store = clone_store(world, tmp_path)
    release_id = entry["release_id"]
    attempts = (
        (
            "release", "disposition",
            "--store", str(store),
            "--snapshot-store", str(entry["base"]["snapshot_store"]),
            "--release-id", release_id,
            "--revision-id", world["pending_revision"]["revision_id"],
            "--reviewer", "agent",
            "--reason", "agents cannot disposition",
        ),
        ("release", "status", "--store", str(store), "--release-id", release_id),
        ("release", "fetch", "--store", str(store), "--release-id", release_id),
    )
    for arguments in attempts:
        result = cli(*arguments, agent=True)
        assert result.returncode == 2, (arguments, result.stdout, result.stderr)
        assert "invalid choice" in result.stderr
        assert result.stdout == ""
    help_result = cli("--help", agent=True)
    assert help_result.returncode == 0
    for forbidden in ("release", "disposition", "admission"):
        assert forbidden not in help_result.stdout
    # No disposition or lifecycle event was written by a refused agent attempt.
    assert not (store / "releases" / release_id / "dispositions").exists()
    assert not (store / "releases" / release_id / "events").exists()


# ---------------------------------------------------------------------------
# 8. Immutable duplicate dispositions resolve latest-wins; no_longer is final.
# ---------------------------------------------------------------------------


def canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def test_disposition_records_are_immutable_and_resolved_latest_wins(
    world: dict, tmp_path: Path
) -> None:
    entry = world["exposed"]
    release_id = entry["release_id"]
    revision_id = world["pending_revision"]["revision_id"]
    store = clone_store(world, tmp_path)
    before = protected_hashes(store, entry)
    ok(lifecycle("enable", store, release_id, "2030-04-01"))
    directory = store / "releases" / release_id / "dispositions"

    def stored_records() -> list[dict]:
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(directory.glob("*.json"))
        ]

    first = ok(
        disposition(
            store,
            entry["base"]["snapshot_store"],
            release_id,
            revision_id,
            impact="pending_review",
            evidence=REVIEW_EVIDENCE,
            date="2030-04-05",
        )
    )
    assert first["impact"] == "pending_review"
    assert first["admission"]["valid"] is False
    files = sorted(directory.glob("*.json"))
    assert len(files) == 1
    original_bytes = files[0].read_bytes()
    assert original_bytes

    second = ok(
        disposition(
            store,
            entry["base"]["snapshot_store"],
            release_id,
            revision_id,
            impact="sustained",
            evidence=REVIEW_EVIDENCE,
            reason="A later re-review clears the pending state.",
        )
    )
    assert second["impact"] == "sustained"
    assert second["paused_event"] is None
    assert second["admission"]["valid"] is True
    records = stored_records()
    assert len(records) == 2
    assert len({record["disposition_id"] for record in records}) == 2
    assert [record["sequence"] for record in records] == [1, 2]
    assert sorted(directory.glob("*.json"))[0].read_bytes() == original_bytes
    for path in directory.glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert path.stat().st_mode & 0o777 == 0o444
        assert payload["disposition_id"] == canonical_hash(
            {key: value for key, value in payload.items() if key != "disposition_id"}
        )
    current = ok(status(store, release_id))
    assert current["admission"]["valid"] is True
    assert len(current["admission"]["dispositions"]) == 1
    assert current["admission"]["dispositions"][0]["impact"] == "sustained"

    third = ok(
        disposition(
            store,
            entry["base"]["snapshot_store"],
            release_id,
            revision_id,
            impact="no_longer_supported",
            evidence=REVIEW_EVIDENCE,
            reason="The frozen judgement cannot be supported after all.",
        )
    )
    assert third["impact"] == "no_longer_supported"
    assert third["admission"]["valid"] is False
    assert third["admission"]["reason"] == "no_longer_supported"
    rejected(lifecycle("resume", store, release_id, "2030-04-10"), "ADMISSION_INVALID")

    fourth = ok(
        disposition(
            store,
            entry["base"]["snapshot_store"],
            release_id,
            revision_id,
            impact="sustained",
            evidence=REVIEW_EVIDENCE,
            reason="A late sustained claim must not clear a final refusal.",
        )
    )
    assert fourth["impact"] == "sustained"
    assert fourth["admission"]["valid"] is False
    assert fourth["admission"]["reason"] == "no_longer_supported"
    assert fourth["admission"]["invalidating_dispositions"] == [revision_id]
    assert fourth["admission"]["dispositions"][0]["impact"] == "no_longer_supported"
    assert len(stored_records()) == 4
    assert protected_hashes(store, entry) == before


# ---------------------------------------------------------------------------
# 9. Illegal input is refused without writing a disposition or an event.
# ---------------------------------------------------------------------------


def test_illegal_disposition_inputs_are_rejected_without_partial_mutation(
    world: dict, tmp_path: Path
) -> None:
    entry = world["exposed"]
    release_id = entry["release_id"]
    store = clone_store(world, tmp_path)
    snapshot_store = entry["base"]["snapshot_store"]
    ok(lifecycle("enable", store, release_id, "2030-08-10"))
    timeline_before = ok(status(store, release_id))["timeline"]

    # Unknown verified revision.
    rejected(
        disposition(
            store,
            snapshot_store,
            release_id,
            "f" * 64,
            impact="pending_review",
            evidence=REVIEW_EVIDENCE,
            date="2030-08-15",
        ),
        "REVISION_NOT_FOUND",
    )
    # An invalidating disposition on an enabled release needs a pre-registered
    # date or the Shadow Live ledger + feed.
    rejected(
        disposition(
            store,
            snapshot_store,
            release_id,
            world["pending_revision"]["revision_id"],
        ),
        "EFFECTIVE_SIGNAL_DATE_REQUIRED",
    )
    # The pause point may never move the release timeline backwards.
    rejected(
        disposition(
            store,
            snapshot_store,
            release_id,
            world["pending_revision"]["revision_id"],
            impact="pending_review",
            evidence=REVIEW_EVIDENCE,
            date="2030-08-01",
        ),
        "ILLEGAL_PAUSE",
    )
    # An ex-date outside both evaluated ranges can never invalidate the release.
    unaffected = world["unaffected"]
    rejected(
        disposition(
            store,
            unaffected["base"]["snapshot_store"],
            unaffected["release_id"],
            world["outside_revision"]["revision_id"],
            impact="no_longer_supported",
            evidence=REVIEW_EVIDENCE,
        ),
        "IMPACT_NOT_APPLICABLE",
    )
    # A malformed revision ID is a plain argument error (no JSON, exit 2).
    malformed = disposition(
        store,
        snapshot_store,
        release_id,
        "not-a-revision",
        date="2030-08-15",
    )
    assert malformed.returncode == 2
    assert malformed.stdout == ""
    assert "revision ID" in malformed.stderr

    # Nothing was written for the refused attempts.
    assert not (store / "releases" / release_id / "dispositions").exists()
    assert not (store / "releases" / unaffected["release_id"] / "dispositions").exists()
    assert ok(status(store, release_id))["timeline"] == timeline_before
