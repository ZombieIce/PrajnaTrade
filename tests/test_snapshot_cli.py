import json
import stat
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest

from cli_helpers import cli

SYMBOLS = ("511260", "518880", "515080", "513300", "159612")


def _fixture() -> dict:
    dates = [(date(2024, 1, 1) + timedelta(days=i)).isoformat() for i in range(12)]
    evidence = {
        "available_at": "2024-01-01T00:00:00+08:00",
        "availability_evidence": "synthetic release schedule",
        "collected_at": "2024-02-01T00:00:00+08:00",
    }
    sources = {
        name: {
            "source": "synthetic", "version": "1",
            "coverage_start": dates[0], "coverage_end": dates[-1], **evidence,
        }
        for name in ("calendar", "raw_bars", "corporate_actions", "derived")
    }
    calendar = [{"date": day, **evidence} for day in dates]
    raw_bars, derived = [], []
    for day in dates:
        for symbol in SYMBOLS:
            if (day, symbol) in {
                (dates[0], SYMBOLS[-1]),
                (dates[3], SYMBOLS[0]),
                (dates[-1], SYMBOLS[-1]),
            }:
                continue
            raw_bars.append(
                {"date": day, "symbol": symbol, "open": 10, "high": 10, "low": 10,
                 "close": 10, "volume": 100, "trading_status": "trading", **evidence}
            )
            derived.append({"date": day, "symbol": symbol, "value": 10, **evidence})
    return {
        "schema_version": 1,
        "data_kind": "synthetic",
        "sources": sources,
        "symbol_coverage": {
            symbol: {"start": dates[1] if symbol == SYMBOLS[-1] else dates[0], "end": dates[-1]}
            for symbol in SYMBOLS
        },
        "derivation_rule_version": "synthetic-total-return-v1",
        "calendar": calendar,
        "raw_bars": raw_bars,
        "corporate_actions": [],
        "derived": derived,
    }


def _cli(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return cli(*args, cwd=cwd)


def test_snapshot_manifest_fixes_common_range_and_partition_contents(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.json"
    source.write_text(json.dumps(_fixture()), encoding="utf-8")
    store = tmp_path / "store"
    result = _cli(tmp_path, "snapshot", "seal", "--input", str(source), "--store", str(store))
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    snapshot_dir = store / receipt["snapshot_id"]
    assert stat.S_IMODE(snapshot_dir.stat().st_mode) == 0o555
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o444 for path in snapshot_dir.iterdir())
    shown = _cli(tmp_path, "snapshot", "manifest", "--store", str(store), "--id", receipt["snapshot_id"])
    assert shown.returncode == 0, shown.stderr
    manifest = json.loads(shown.stdout)
    assert manifest["symbols"] == sorted(SYMBOLS)
    assert manifest["common_range"] == {"start": "2024-01-02", "end": "2024-01-11"}
    assert manifest["gaps"]["raw_bars"] == [{"date": "2024-01-04", "symbol": "511260"}]
    assert [(p["name"], p["sessions"], p["start"], p["end"]) for p in manifest["partitions"]] == [
        ("discovery", 6, "2024-01-02", "2024-01-07"),
        ("validation", 2, "2024-01-08", "2024-01-09"),
        ("final_holdout", 2, "2024-01-10", "2024-01-11"),
    ]
    assert all(len(p["content_hash"]) == 64 for p in manifest["partitions"])
    assert set(manifest["content_hashes"]) == {"calendar", "raw_bars", "corporate_actions", "derived"}
    assert all(len(value) == 64 for value in manifest["content_hashes"].values())


def test_projects_share_discovery_and_cannot_read_sealed_partitions(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.json"
    source.write_text(json.dumps(_fixture()), encoding="utf-8")
    store = tmp_path / "store"
    sealed = _cli(tmp_path, "snapshot", "seal", "--input", str(source), "--store", str(store))
    assert sealed.returncode == 0, sealed.stderr
    snapshot_id = json.loads(sealed.stdout)["snapshot_id"]
    projects = [tmp_path / "project-a", tmp_path / "project-b"]
    for project in projects:
        project.mkdir()
    reads = [
        _cli(project, "snapshot", "read", "--store", str(store), "--id", snapshot_id,
             "--partition", "discovery")
        for project in projects
    ]
    assert all(read.returncode == 0 for read in reads), [read.stderr for read in reads]
    assert json.loads(reads[0].stdout) == json.loads(reads[1].stdout)
    receipt = json.loads(reads[0].stdout)
    assert receipt["snapshot_id"] == snapshot_id
    assert receipt["partition"] == "discovery"
    assert {row["date"] for row in receipt["data"]["calendar"]} == {
        "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-06", "2024-01-07"
    }
    assert all(not list(project.iterdir()) for project in projects)
    for partition in ("validation", "final_holdout"):
        denied = _cli(projects[0], "snapshot", "read", "--store", str(store), "--id", snapshot_id,
                      "--partition", partition)
        assert denied.returncode != 0
        assert not denied.stdout


def test_snapshot_identity_covers_sealed_content_and_detects_tampering(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.json"
    payload = _fixture()
    source.write_text(json.dumps(payload), encoding="utf-8")
    store = tmp_path / "store"

    def seal() -> subprocess.CompletedProcess[str]:
        return _cli(tmp_path, "snapshot", "seal", "--input", str(source), "--store", str(store))

    original = seal()
    assert original.returncode == 0, original.stderr
    snapshot_id = json.loads(original.stdout)["snapshot_id"]
    repeated = seal()
    assert repeated.returncode == 0, repeated.stderr
    assert json.loads(repeated.stdout)["snapshot_id"] == snapshot_id

    row = next(row for row in payload["raw_bars"] if row["date"] == "2024-01-11")
    row["close"] = row["high"] = 11
    source.write_text(json.dumps(payload), encoding="utf-8")
    changed = seal()
    assert changed.returncode == 0, changed.stderr
    assert json.loads(changed.stdout)["snapshot_id"] != snapshot_id

    discovery = store / snapshot_id / "discovery.json"
    discovery.chmod(0o644)
    discovery.write_text("{}", encoding="utf-8")
    read = _cli(tmp_path, "snapshot", "read", "--store", str(store), "--id", snapshot_id,
                "--partition", "discovery")
    assert read.returncode != 0
    assert not read.stdout
    assert "integrity" in read.stderr.lower()
    source.write_text(json.dumps(_fixture()), encoding="utf-8")
    assert seal().returncode != 0


def test_source_coverage_limits_last_complete_session(tmp_path: Path) -> None:
    payload = _fixture()
    payload["sources"]["corporate_actions"]["coverage_end"] = "2024-01-10"
    source = tmp_path / "synthetic.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    store = tmp_path / "store"
    sealed = _cli(tmp_path, "snapshot", "seal", "--input", str(source), "--store", str(store))
    assert sealed.returncode == 0, sealed.stderr
    result = _cli(tmp_path, "snapshot", "manifest", "--store", str(store),
                  "--id", json.loads(sealed.stdout)["snapshot_id"])
    assert json.loads(result.stdout)["common_range"]["end"] == "2024-01-10"


def test_missing_first_common_bar_remains_a_gap(tmp_path: Path) -> None:
    payload = _fixture()
    payload["raw_bars"] = [
        row for row in payload["raw_bars"]
        if not (row["symbol"] == SYMBOLS[-1] and row["date"] == "2024-01-02")
    ]
    source = tmp_path / "synthetic.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    store = tmp_path / "store"
    sealed = _cli(tmp_path, "snapshot", "seal", "--input", str(source), "--store", str(store))
    assert sealed.returncode == 0, sealed.stderr
    result = _cli(tmp_path, "snapshot", "manifest", "--store", str(store),
                  "--id", json.loads(sealed.stdout)["snapshot_id"])
    manifest = json.loads(result.stdout)
    assert manifest["common_range"]["start"] == "2024-01-02"
    assert {"date": "2024-01-02", "symbol": SYMBOLS[-1]} in manifest["gaps"]["raw_bars"]


@pytest.mark.parametrize("kind", ["cash_dividend", "share_adjustment"])
def test_corporate_action_requires_terms_and_evidence(tmp_path: Path, kind: str) -> None:
    payload = _fixture()
    payload["corporate_actions"] = [{
        "date": "2024-01-04", "symbol": SYMBOLS[0], "type": kind,
        "available_at": "2024-01-02T00:00:00+08:00",
        "availability_evidence": "synthetic announcement",
        "collected_at": "2024-02-01T00:00:00+08:00",
    }]
    source = tmp_path / "synthetic.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    rejected = _cli(tmp_path, "snapshot", "seal", "--input", str(source),
                    "--store", str(tmp_path / "store"))
    assert rejected.returncode != 0
    assert "action" in rejected.stderr.lower()


def test_missing_availability_and_git_store_are_rejected(tmp_path: Path) -> None:
    payload = _fixture()
    del payload["raw_bars"][0]["availability_evidence"]
    source = tmp_path / "synthetic.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    invalid = _cli(tmp_path, "snapshot", "seal", "--input", str(source),
                   "--store", str(tmp_path / "store"))
    assert invalid.returncode != 0
    assert "availability_evidence" in invalid.stderr

    source.write_text(json.dumps(_fixture()), encoding="utf-8")
    in_git = Path(__file__).resolve().parents[1] / "snapshot-test-store"
    rejected = _cli(tmp_path, "snapshot", "seal", "--input", str(source), "--store", str(in_git))
    assert rejected.returncode != 0
    assert "outside Git" in rejected.stderr
    assert not in_git.exists()


def test_unknown_availability_is_preserved_as_a_gap(tmp_path: Path) -> None:
    payload = _fixture()
    row = next(row for row in payload["raw_bars"] if row["date"] == "2024-01-04")
    row["available_at"] = None
    row["availability_evidence"] = None
    source = tmp_path / "synthetic.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    store = tmp_path / "store"
    sealed = _cli(tmp_path, "snapshot", "seal", "--input", str(source), "--store", str(store))
    assert sealed.returncode == 0, sealed.stderr
    snapshot_id = json.loads(sealed.stdout)["snapshot_id"]
    shown = _cli(tmp_path, "snapshot", "manifest", "--store", str(store), "--id", snapshot_id)
    assert {"kind": "raw_bars", "date": row["date"], "symbol": row["symbol"]} in json.loads(shown.stdout)["gaps"]["availability"]
    read = _cli(tmp_path, "snapshot", "read", "--store", str(store), "--id", snapshot_id,
                "--partition", "discovery")
    returned = next(item for item in json.loads(read.stdout)["data"]["raw_bars"]
                    if item["date"] == row["date"] and item["symbol"] == row["symbol"])
    assert returned["available_at"] is None
    assert returned["availability_evidence"] is None


def test_missing_raw_trading_status_is_rejected(tmp_path: Path) -> None:
    payload = _fixture()
    del payload["raw_bars"][0]["trading_status"]
    source = tmp_path / "synthetic.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    result = _cli(tmp_path, "snapshot", "seal", "--input", str(source),
                  "--store", str(tmp_path / "store"))
    assert result.returncode != 0
    assert "trading_status" in result.stderr


def test_corporate_action_unknown_terms_are_retained(tmp_path: Path) -> None:
    payload = _fixture()
    payload["corporate_actions"] = [{
        "date": "2024-01-04", "symbol": SYMBOLS[0], "type": "cash_dividend",
        "cash_per_share": None, "pay_date": None, "terms_evidence": "synthetic announcement",
        "available_at": "2024-01-02T00:00:00+08:00",
        "availability_evidence": "synthetic announcement",
        "collected_at": "2024-02-01T00:00:00+08:00",
    }]
    source = tmp_path / "synthetic.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    store = tmp_path / "store"
    sealed = _cli(tmp_path, "snapshot", "seal", "--input", str(source), "--store", str(store))
    assert sealed.returncode == 0, sealed.stderr
    snapshot_id = json.loads(sealed.stdout)["snapshot_id"]
    shown = _cli(tmp_path, "snapshot", "manifest", "--store", str(store), "--id", snapshot_id)
    assert {"date": "2024-01-04", "symbol": SYMBOLS[0], "field": "cash_per_share"} in json.loads(shown.stdout)["gaps"]["corporate_actions"]
    read = _cli(tmp_path, "snapshot", "read", "--store", str(store), "--id", snapshot_id,
                "--partition", "discovery")
    assert json.loads(read.stdout)["data"]["corporate_actions"] == payload["corporate_actions"]


def test_research_reads_do_not_open_holdout_content(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.json"
    source.write_text(json.dumps(_fixture()), encoding="utf-8")
    store = tmp_path / "store"
    sealed = _cli(tmp_path, "snapshot", "seal", "--input", str(source), "--store", str(store))
    assert sealed.returncode == 0, sealed.stderr
    snapshot_id = json.loads(sealed.stdout)["snapshot_id"]
    directory = store / snapshot_id
    directory.chmod(0o755)
    (directory / "final_holdout.json").unlink()
    directory.chmod(0o555)
    shown = _cli(tmp_path, "snapshot", "manifest", "--store", str(store), "--id", snapshot_id)
    read = _cli(tmp_path, "snapshot", "read", "--store", str(store), "--id", snapshot_id,
                "--partition", "discovery")
    denied = _cli(tmp_path, "snapshot", "read", "--store", str(store), "--id", snapshot_id,
                  "--partition", "final_holdout")
    assert shown.returncode == 0, shown.stderr
    assert read.returncode == 0, read.stderr
    assert denied.returncode != 0 and not denied.stdout
    reseal = _cli(tmp_path, "snapshot", "seal", "--input", str(source), "--store", str(store))
    assert reseal.returncode != 0 and not reseal.stdout


def _correction_case(tmp_path: Path, ex_date: str = "2024-01-04",
                     original_available: str = "2024-01-02T09:00:00+08:00") -> tuple[str, dict]:
    data = _fixture()
    data["corporate_actions"] = [{
        "date": ex_date, "symbol": SYMBOLS[0], "type": "cash_dividend",
        "cash_per_share": 0.20, "pay_date": "2024-01-12",
        "available_at": original_available,
        "collected_at": "2024-02-01T00:00:00+08:00",
        "availability_evidence": "synthetic supplier archive",
        "terms_evidence": "synthetic supplier archive",
    }]
    snapshot_id = _snapshot_command(tmp_path, "seal", payload=data)["snapshot_id"]
    return snapshot_id, {
        "schema_version": 1, "predecessor_id": snapshot_id,
        "symbol": SYMBOLS[0], "ex_date": ex_date, "currency": "CNY",
        "old_amount": 0.20, "new_amount": 0.18,
        "supplier": {
            "source": "synthetic supplier", "archive_reference": "synthetic-archive:123",
            "observed_at": "2024-02-02T10:00:00+08:00",
        },
    }


def _snapshot_command(tmp_path: Path, *args: str, payload: dict | None = None,
                      succeeds: bool = True) -> dict:
    if payload is not None:
        path = tmp_path / "input.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        args = (*args, "--input", str(path))
    result = _cli(tmp_path, "snapshot", *args, "--store", str(tmp_path / "store"))
    if not succeeds:
        assert result.returncode != 0 and not result.stdout
        return {"error": result.stderr}
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_silent_dividend_change_stays_pending(tmp_path: Path) -> None:
    snapshot_id, proposal = _correction_case(tmp_path)
    before = _snapshot_command(tmp_path, "read", "--id", snapshot_id, "--partition", "discovery")
    pending = _snapshot_command(tmp_path, "correction", "register", payload=proposal)
    assert pending["status"] == "pending_verification"
    assert pending["proposal"] == proposal
    assert _snapshot_command(tmp_path, "correction", "register", payload=proposal) == pending
    assert "old amount" in _snapshot_command(
        tmp_path, "correction", "register", payload={**proposal, "old_amount": 0.19}, succeeds=False
    )["error"]
    assert _snapshot_command(tmp_path, "read", "--id", snapshot_id, "--partition", "discovery") == before


def _notice(proposal: dict, available_at: str) -> dict:
    return {
        "kind": "synthetic", "authority": "fund_manager", "issuer": "Synthetic Fund Manager",
        "url": "https://notices.example.invalid/dividend", "reference": "synthetic-notice-1",
        "symbol": proposal["symbol"], "ex_date": proposal["ex_date"], "currency": "CNY",
        "amount": proposal["new_amount"], "available_at": available_at,
        "collected_at": "2024-02-02T09:00:00+08:00",
    }


def test_official_verification_requires_matching_evidence_and_deduplicates_notices(tmp_path: Path) -> None:
    _, proposal = _correction_case(tmp_path)
    pending = _snapshot_command(tmp_path, "correction", "register", payload=proposal)
    args = ("correction", "verify", "--proposal-id", pending["proposal_id"])
    notice = _notice(proposal, "2024-01-03T09:00:00+08:00")
    assert "authority" in _snapshot_command(
        tmp_path, *args, payload={**notice, "authority": "supplier"}, succeeds=False
    )["error"]
    assert "match" in _snapshot_command(
        tmp_path, *args, payload={**notice, "amount": 0.19}, succeeds=False
    )["error"]
    verified = _snapshot_command(tmp_path, *args, payload=notice)
    assert verified["status"] == "verified"
    assert verified["data_kind"] == "synthetic"
    assert verified["evidence"] == notice
    assert verified["revision"]["old_amount"] == 0.20
    assert verified["revision"]["new_amount"] == 0.18
    assert verified["filed_at"] > "2024-02-02"
    duplicate = _snapshot_command(tmp_path, *args, payload={
        **notice, "url": "https://exchange.example.invalid/reprint", "authority": "exchange",
        "issuer": "Synthetic Exchange", "reference": "synthetic-duplicate-2",
    })
    assert duplicate == verified


@pytest.mark.parametrize(("official_at", "on_ex_date"), [
    ("2024-01-06T09:00:00+08:00", 0.20),
    ("2024-01-03T09:00:00+08:00", 0.18),
], ids=["official-later", "supplier-already-wrong"])
def test_dividend_successor_reconstructs_official_availability_without_rewriting_history(
    tmp_path: Path, official_at: str, on_ex_date: float,
) -> None:
    snapshot_id, proposal = _correction_case(tmp_path)
    before_manifest = _snapshot_command(tmp_path, "manifest", "--id", snapshot_id)
    before_discovery = _snapshot_command(tmp_path, "read", "--id", snapshot_id, "--partition", "discovery")
    pending = _snapshot_command(tmp_path, "correction", "register", payload=proposal)
    verified = _snapshot_command(tmp_path, "correction", "verify", "--proposal-id", pending["proposal_id"],
                                 payload=_notice(proposal, official_at))
    args = ("correction", "successor", "--proposal-id", pending["proposal_id"],
            "--revision-id", verified["revision_id"])
    successor = _snapshot_command(tmp_path, *args)
    assert _snapshot_command(tmp_path, *args) == successor
    new_id = successor["snapshot_id"]
    assert new_id != snapshot_id
    new_manifest = _snapshot_command(tmp_path, "manifest", "--id", new_id)
    assert new_manifest["lineage"]["predecessor_id"] == snapshot_id
    assert new_manifest["lineage"]["revision_id"] == verified["revision_id"]
    assert new_manifest["partitions"][1:] == before_manifest["partitions"][1:]
    assert new_manifest["partitions"][0]["content_hash"] != before_manifest["partitions"][0]["content_hash"]
    read = ("read", "--id", new_id, "--partition", "discovery")
    assert "--as-of" in _snapshot_command(tmp_path, *read, succeeds=False)["error"]
    as_of = _snapshot_command(tmp_path, *read, "--as-of", "2024-01-04T15:00:00+08:00")
    assert as_of["data"]["corporate_actions"][0]["cash_per_share"] == on_ex_date
    assert max(r["date"] for r in as_of["data"]["calendar"]) == "2024-01-04"
    after = _snapshot_command(tmp_path, *read, "--as-of", "2024-01-06T01:00:00Z")
    assert after["data"]["corporate_actions"][0]["cash_per_share"] == 0.18
    assert all(r["value"] is None for r in after["data"]["derived"] if r["symbol"] == proposal["symbol"])
    assert _snapshot_command(tmp_path, "manifest", "--id", snapshot_id) == before_manifest
    assert _snapshot_command(tmp_path, "read", "--id", snapshot_id, "--partition", "discovery") == before_discovery


def test_protected_correction_records_lineage_without_opening_sealed_files(tmp_path: Path) -> None:
    snapshot_id, proposal = _correction_case(tmp_path, ex_date="2024-01-10")
    before = _snapshot_command(tmp_path, "manifest", "--id", snapshot_id)
    directory = tmp_path / "store" / snapshot_id
    directory.chmod(0o755)
    for partition in ("validation", "final_holdout"):
        (directory / f"{partition}.json").unlink()
    directory.chmod(0o555)

    pending = _snapshot_command(tmp_path, "correction", "register", payload=proposal)
    proposed = ("--proposal-id", pending["proposal_id"])
    assert "no matching verified revision" in _snapshot_command(
        tmp_path, "correction", "successor", *proposed, "--revision-id", "0" * 64, succeeds=False
    )["error"]
    verified = _snapshot_command(tmp_path, "correction", "verify", *proposed,
                                 payload=_notice(proposal, "2024-01-11T09:00:00+08:00"))
    successor = _snapshot_command(tmp_path, "correction", "successor", *proposed,
                                  "--revision-id", verified["revision_id"])
    after = _snapshot_command(tmp_path, "manifest", "--id", successor["snapshot_id"])
    assert after["partitions"] == before["partitions"]
    assert after["lineage"]["protected_partitions_inherited"] is True
    assert after["lineage"]["revision_id"] == verified["revision_id"]
    assert "discovery_dividend" not in after
    for partition in ("validation", "final_holdout"):
        for content_id in (snapshot_id, successor["snapshot_id"]):
            assert "limited to Discovery" in _snapshot_command(
                tmp_path, "read", "--id", content_id, "--partition", partition, succeeds=False
            )["error"]
    cutoff = ("--partition", "discovery", "--as-of", "2024-02-02T15:00:00+08:00")
    assert _snapshot_command(tmp_path, "read", "--id", successor["snapshot_id"], *cutoff)["data"] == (
        _snapshot_command(tmp_path, "read", "--id", snapshot_id, *cutoff)["data"]
    )
    assert _snapshot_command(tmp_path, "manifest", "--id", snapshot_id) == before


def test_amount_notice_does_not_make_later_payment_terms_available(tmp_path: Path) -> None:
    _, proposal = _correction_case(tmp_path, original_available="2024-01-05T09:00:00+08:00")
    pending = _snapshot_command(tmp_path, "correction", "register", payload=proposal)
    proposed = ("--proposal-id", pending["proposal_id"])
    verified = _snapshot_command(tmp_path, "correction", "verify", *proposed,
                                 payload=_notice(proposal, "2024-01-03T09:00:00+08:00"))
    successor = _snapshot_command(tmp_path, "correction", "successor", *proposed,
                                  "--revision-id", verified["revision_id"])
    args = ("read", "--id", successor["snapshot_id"], "--partition", "discovery", "--as-of")
    early = _snapshot_command(tmp_path, *args, "2024-01-04T15:00:00+08:00")
    assert early["data"]["corporate_actions"][0]["cash_per_share"] == 0.18
    assert early["data"]["corporate_actions"][0]["pay_date"] is None
    late = _snapshot_command(tmp_path, *args, "2024-01-05T15:00:00+08:00")
    assert late["data"]["corporate_actions"][0]["pay_date"] == "2024-01-12"
