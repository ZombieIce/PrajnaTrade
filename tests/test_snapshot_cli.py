import json
import os
import stat
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest


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
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    return subprocess.run(
        [sys.executable, "-m", "prajnatrade", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )


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
