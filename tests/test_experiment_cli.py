import json
import signal
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import pytest
from cli_helpers import AGENT_COMMAND, cli, cli_env, ok

from prajnatrade import trading_status
from prajnatrade.experiment import campaign_discovery, runtime_identity, runtime_matches
from prajnatrade.snapshot_baseline import ACTUAL_OPEN_RULES, NEXT_OPEN_RULES, RETRY_INITIAL_RULES


def sealed_snapshot_input(day_count: int = 15) -> dict:
    dates = [date(2024, 1, 1) + timedelta(days=index) for index in range(day_count)]
    evidence = {
        "available_at": "2023-12-31T00:00:00+08:00",
        "availability_evidence": "synthetic release schedule",
        "collected_at": "2024-02-01T00:00:00+08:00",
    }
    symbols = ("511260", "518880", "515080", "513300", "159612")
    return {
        "schema_version": 1,
        "data_kind": "synthetic",
        "sources": {
            name: {
                "source": "synthetic",
                "version": "1",
                "coverage_start": dates[0].isoformat(),
                "coverage_end": dates[-1].isoformat(),
                **evidence,
            }
            for name in ("calendar", "raw_bars", "corporate_actions", "derived")
        },
        "symbol_coverage": {
            symbol: {"start": dates[0].isoformat(), "end": dates[-1].isoformat()}
            for symbol in symbols
        },
        "derivation_rule_version": "synthetic-total-return-v1",
        "calendar": [{"date": day.isoformat(), **evidence} for day in dates],
        "raw_bars": [
            {
                "date": day.isoformat(),
                "symbol": symbol,
                "open": 9.8 if day >= date(2024, 1, 4) and symbol == symbols[0] else 10,
                "high": 10,
                "low": 9.8 if day >= date(2024, 1, 4) and symbol == symbols[0] else 10,
                "close": 9.8 if day >= date(2024, 1, 4) and symbol == symbols[0] else 10,
                "volume": 1000,
                "trading_status": "trading",
                **evidence,
            }
            for day in dates
            for symbol in symbols
        ],
        "corporate_actions": [{
            "date": "2024-01-04",
            "symbol": symbols[0],
            "type": "cash_dividend",
            "cash_per_share": 0.2,
            "pay_date": "2024-01-06",
            "terms_evidence": "synthetic dividend terms",
            **evidence,
        }],
        "derived": [
            {"date": day.isoformat(), "symbol": symbol, "value": 10, **evidence}
            for day in dates
            for symbol in symbols
        ],
    }


def test_project_identity_is_independent_of_directory(tmp_path: Path) -> None:
    project = tmp_path / "strategy-a"
    created = cli("project", "init", "--project", str(project))
    assert created.returncode == 0, created.stderr
    identity = json.loads(created.stdout)
    assert identity["schema_version"] == 1
    assert identity["project_id"]

    moved = tmp_path / "handed-off"
    project.rename(moved)
    shown = cli("project", "status", "--project", str(moved))
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout)["project_id"] == identity["project_id"]


def snapshot_file(path: Path, day_count: int = 12) -> None:
    path.write_text(json.dumps(sealed_snapshot_input(day_count)), encoding="utf-8")


def campaign_options(
    project: Path,
    source: Path,
    *,
    attempts: int = 2,
    max_seconds: float = 30,
    initial_cash: float = 100_000,
) -> tuple[str, ...]:
    store = project.parent / f"{project.name}-snapshot-store"
    sealed = cli("snapshot", "seal", "--input", str(source), "--store", str(store))
    assert sealed.returncode == 0, sealed.stderr
    snapshot_id = json.loads(sealed.stdout)["snapshot_id"]
    return (
        "campaign", "create", "--project", str(project), "--store", str(store),
        "--snapshot-id", snapshot_id, "--attempts", str(attempts),
        "--max-seconds", str(max_seconds), "--initial-cash", str(initial_cash),
    )


def test_operator_creates_locked_synthetic_campaign(tmp_path: Path) -> None:
    project = tmp_path / "strategy-a"
    source = tmp_path / "discovery.json"
    snapshot_file(source)
    assert cli("project", "init", "--project", str(project)).returncode == 0
    result = cli(*campaign_options(project, source))
    assert result.returncode == 0, result.stderr
    campaign = json.loads(result.stdout)
    assert campaign["schema_version"] == 1
    assert campaign["campaign_id"]
    assert campaign["fingerprints"]["snapshot"]
    assert campaign["fingerprints"]["evaluation"]
    assert campaign["budget"]["attempts_remaining"] == 2


def test_actual_open_runtime_identity_binds_the_halt_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    actual_open = {"version": ACTUAL_OPEN_RULES}
    legacy_rules = ({}, {"version": NEXT_OPEN_RULES}, {"version": RETRY_INITIAL_RULES})
    actual_identity = runtime_identity(execution_rules=actual_open)
    legacy_identities = [runtime_identity(execution_rules=rules) for rules in legacy_rules]

    halt_rules_path = Path(trading_status.__file__)
    read_file = Path.read_bytes
    original = read_file(halt_rules_path)
    changed = original.replace(b"hour=15, minute=0", b"hour=14, minute=59")
    assert changed != original

    def read_with_earlier_close(path: Path) -> bytes:
        return changed if path == halt_rules_path else read_file(path)

    monkeypatch.setattr(Path, "read_bytes", read_with_earlier_close)
    assert runtime_identity(execution_rules=actual_open) != actual_identity
    assert not runtime_matches(actual_identity, execution_rules=actual_open)
    for frozen, rules in zip(legacy_identities, legacy_rules, strict=True):
        assert runtime_identity(execution_rules=rules) == frozen
        assert runtime_matches(frozen, execution_rules=rules)


def test_actual_open_campaign_freezes_the_matching_runtime(tmp_path: Path) -> None:
    project = tmp_path / "research"
    source = tmp_path / "snapshot.json"
    snapshot_file(source)
    ok(cli("project", "init", "--project", str(project)))
    options = campaign_options(project, source)
    actual = ok(cli(*options, "--execution-rules", ACTUAL_OPEN_RULES))
    legacy = ok(cli(*options))
    actual_evaluation = campaign_discovery(project, actual["campaign_id"])["evaluation"]
    legacy_evaluation = campaign_discovery(project, legacy["campaign_id"])["evaluation"]

    actual_rules = actual_evaluation["execution_rules"]
    assert actual_evaluation["runtime"] == runtime_identity(execution_rules=actual_rules)
    assert runtime_matches(actual_evaluation["runtime"], execution_rules=actual_rules)
    # The previous fingerprint recipe gave actual-open and legacy Campaigns
    # the same runtime because neither bound the halt predicate source.
    assert not runtime_matches(legacy_evaluation["runtime"], execution_rules=actual_rules)


def test_discovery_warmup_freezes_one_scoring_start_for_versions_and_benchmark(
    tmp_path: Path,
) -> None:
    project = tmp_path / "strategy-a"
    source = tmp_path / "snapshot.json"
    snapshot_file(source, day_count=15)
    assert cli("project", "init", "--project", str(project)).returncode == 0
    result = cli(
        *campaign_options(project, source, attempts=3), "--discovery-warmup-sessions", "4",
        "--transaction-cost-bps", "0",
    )
    assert result.returncode == 0, result.stderr
    campaign = json.loads(result.stdout)
    assert campaign["history_policy"] == {
        "schema_version": 1, "warmup_sessions": 4, "scoring_start": "2024-01-05",
    }

    for lookback in (2, 3, 4):
        request = tmp_path / f"momentum-{lookback}.json"
        request.write_text(json.dumps({
            "schema_version": 1, "campaign_id": campaign["campaign_id"],
            "submission_id": f"momentum-{lookback}",
            "hypothesis": "Score comparable versions from the same opening account.",
            "strategy": {"family": "momentum"},
            "parameters": {"lookback": lookback, "cash_if_negative": False},
        }), encoding="utf-8")
        response = cli("experiment", "submit", "--project", str(project), "--input", str(request))
        experiment = json.loads(response.stdout)
        if lookback == 4:
            assert response.returncode == 2
            assert experiment["status"] == "failed"
            assert experiment["error"]["code"] == "INSUFFICIENT_HISTORY"
            continue
        assert response.returncode == 0, response.stdout
        assert experiment["status"] == "succeeded", experiment
        discovery = experiment["discovery"]
        benchmark = discovery["baseline_comparison"]["baseline"]
        for run in (discovery, benchmark):
            assert run["start_date"] == "2024-01-05"
            assert run["end_date"] == "2024-01-09"
            assert run["initial_cash"] == 100_000
            assert run["fills"][0]["date"] == "2024-01-05"
            assert run["equity_curve"][0]["date"] == "2024-01-05"
            assert run["equity_curve"][0]["nav"] == 100_000
            assert run["equity_curve"][0]["dividend_receivables"] == 0
        assert discovery["signals"][0]["window"][-1] == "2024-01-04"
        assert benchmark["signals"][0]["window"] == []


def test_indicator_free_discovery_can_trade_the_first_open(tmp_path: Path) -> None:
    project = tmp_path / "strategy-a"
    source = tmp_path / "snapshot.json"
    snapshot_file(source)
    assert cli("project", "init", "--project", str(project)).returncode == 0
    response = cli(*campaign_options(project, source), "--discovery-warmup-sessions", "0")
    assert response.returncode == 0, response.stderr
    campaign = json.loads(response.stdout)
    request = tmp_path / "equal-weight.json"
    request.write_text(json.dumps({
        "schema_version": 1, "campaign_id": campaign["campaign_id"],
        "submission_id": "equal-weight", "hypothesis": "Equal weights require no indicators.",
        "strategy": {"family": "equal_weight"}, "parameters": {},
    }), encoding="utf-8")
    submitted = cli("experiment", "submit", "--project", str(project), "--input", str(request))
    assert submitted.returncode == 0, submitted.stdout
    result = json.loads(submitted.stdout)
    assert result["strategy"]["history_requirements"] == []
    assert result["discovery"]["fills"][0]["date"] == "2024-01-01"
    assert result["discovery"]["baseline_comparison"]["baseline"]["fills"][0]["date"] == "2024-01-01"


def test_discovery_freezes_canonical_requirements_with_sealed_prior_history(tmp_path: Path) -> None:
    project = tmp_path / "strategy-a"
    source = tmp_path / "snapshot.json"
    source.write_text(json.dumps({
        **sealed_snapshot_input(), "discovery_start": "2024-01-04",
    }), encoding="utf-8")
    assert cli("project", "init", "--project", str(project)).returncode == 0
    created = cli(*campaign_options(project, source), "--discovery-warmup-sessions", "0")
    assert created.returncode == 0, created.stderr
    campaign = json.loads(created.stdout)
    assert campaign["history_policy"]["scoring_start"] == "2024-01-04"
    assert len(campaign["history_policy"]["prior_history_hash"]) == 64
    (tmp_path / "strategy-a-snapshot-store").rename(tmp_path / "detached-sealed-store")

    requirements = [
        {"id": "momentum", "observations": 3, "mode": "valid_observations"},
        {"id": "streak", "observations": 2, "mode": "consecutive_sessions"},
    ]
    payload = {
        "schema_version": 1, "campaign_id": campaign["campaign_id"],
        "submission_id": "history", "hypothesis": "Use the frozen total-return history.",
        "strategy": {"family": "momentum", "history_requirements": requirements[::-1]},
        "parameters": {"lookback": 2, "cash_if_negative": False},
    }
    request = tmp_path / "history.json"
    submission = ("experiment", "submit", "--project", str(project), "--input", str(request))
    request.write_text(json.dumps(payload), encoding="utf-8")
    submitted = cli(*submission, agent=True)
    assert submitted.returncode == 0, submitted.stdout
    result = json.loads(submitted.stdout)
    assert result["strategy"]["history_requirements"] == requirements
    assert result["discovery"]["start_date"] == "2024-01-04"
    assert result["discovery"]["signals"][0]["window"] == ["2024-01-01", "2024-01-02", "2024-01-03"]

    payload["strategy"]["history_requirements"] = requirements
    request.write_text(json.dumps(payload), encoding="utf-8")
    replay = cli(*submission, agent=True)
    assert replay.returncode == 0, replay.stdout
    assert json.loads(replay.stdout)["experiment_id"] == result["experiment_id"]

    frozen = cli(
        "candidate", "freeze", "--project", str(project),
        "--campaign-id", campaign["campaign_id"], "--submission-id", "history",
        "--name", "momentum-a", "--conclusion", "Retain the frozen history requirements.",
    )
    assert frozen.returncode == 0, frozen.stderr
    candidate = json.loads(frozen.stdout)
    assert candidate["strategy"]["history_requirements"] == requirements
    assert candidate["evaluation"]["history_policy"] == campaign["history_policy"]

    payload["strategy"]["history_requirements"] = [requirements[1]]
    request.write_text(json.dumps(payload), encoding="utf-8")
    rejected = cli(*submission, agent=True)
    assert rejected.returncode == 2
    assert json.loads(rejected.stdout)["error"]["code"] == "INVALID_SUBMISSION"


def test_submit_and_status_report_same_discovery_experiment(tmp_path: Path) -> None:
    project = tmp_path / "strategy-a"
    source = tmp_path / "discovery.json"
    snapshot_file(source)
    project_id = json.loads(cli("project", "init", "--project", str(project)).stdout)["project_id"]
    created = cli(*campaign_options(project, source))
    campaign = json.loads(created.stdout)
    source.unlink()
    request = tmp_path / "first.json"
    request.write_text(json.dumps({
        "schema_version": 1, "campaign_id": campaign["campaign_id"],
        "submission_id": "first", "hypothesis": "Equal weights match the benchmark.",
        "strategy": {"family": "equal_weight"}, "parameters": {},
    }), encoding="utf-8")
    submitted = cli("experiment", "submit", "--project", str(project), "--input", str(request))
    assert submitted.returncode == 0, submitted.stderr
    result = json.loads(submitted.stdout)
    assert result["status"] == "succeeded"
    assert result["project_id"] == project_id
    assert result["budget"]["attempts_remaining"] == 1
    assert result["fingerprints"]["snapshot"] == campaign["fingerprints"]["snapshot"]
    assert result["fingerprints"]["evaluation"] == campaign["fingerprints"]["evaluation"]
    assert result["fingerprints"]["strategy_and_parameters"]
    assert result["discovery"]["return_delta"] == 0
    quarterly_summary = result["quarterly_summary"]
    assert quarterly_summary["identity"]["interval"] == {
        "start_date": result["discovery"]["start_date"],
        "end_date": result["discovery"]["end_date"],
    }
    assert quarterly_summary["identity"]["fingerprints"] == result["fingerprints"]
    assert quarterly_summary["identity"]["snapshot_id"] == campaign["snapshot_id"]
    assert quarterly_summary["identity"]["execution_rules"] == campaign["execution_rules"]
    assert quarterly_summary["quarters"][0]["expected_trading_sessions"] == len(
        result["discovery"]["equity_curve"]
    )
    assert isinstance(result["diagnostics"], list)
    assert "holdout" not in submitted.stdout.lower()
    assert "validation" not in submitted.stdout.lower()
    moved = tmp_path / "handed-off"
    project.rename(moved)
    status = cli(
        "experiment", "status", "--project", str(moved),
        "--campaign-id", campaign["campaign_id"], "--submission-id", "first",
    )
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["experiment_id"] == result["experiment_id"]
    assert json.loads(status.stdout)["project_id"] == project_id
    assert json.loads(status.stdout)["discovery"] == result["discovery"]
    assert json.loads(status.stdout)["quarterly_summary"] == quarterly_summary


def test_quarterly_summary_uses_the_frozen_development_interval(tmp_path: Path) -> None:
    project = tmp_path / "strategy-a"
    source = tmp_path / "snapshot.json"
    snapshot_file(source, day_count=15)
    assert cli("project", "init", "--project", str(project)).returncode == 0
    campaign = json.loads(cli(
        *campaign_options(project, source), "--discovery-split", "70/30",
    ).stdout)
    request = tmp_path / "equal-weight.json"
    request.write_text(json.dumps({
        "schema_version": 1,
        "campaign_id": campaign["campaign_id"],
        "submission_id": "development-only",
        "hypothesis": "Summarize only the frozen synthetic development account.",
        "strategy": {"family": "equal_weight"},
        "parameters": {},
    }), encoding="utf-8")

    submitted = cli("experiment", "submit", "--project", str(project), "--input", str(request))
    assert submitted.returncode == 0, submitted.stderr
    result = json.loads(submitted.stdout)
    development = campaign["discovery_split"]["development"]
    summary = result["quarterly_summary"]
    assert result["data_kind"] == "synthetic"
    assert result["discovery"]["start_date"] == development["start"]
    assert result["discovery"]["end_date"] == development["end"]
    assert summary["identity"]["interval"] == {
        "start_date": development["start"],
        "end_date": development["end"],
    }
    assert summary["quarters"][0]["expected_trading_sessions"] == development["sessions"]


def test_experiment_uses_sealed_discovery_accounting_and_same_interval_baseline(
    tmp_path: Path,
) -> None:
    project = tmp_path / "strategy-a"
    source = tmp_path / "snapshot.json"
    store = tmp_path / "sealed-store"
    source.write_text(json.dumps(sealed_snapshot_input()), encoding="utf-8")
    assert cli("project", "init", "--project", str(project)).returncode == 0
    sealed = json.loads(cli("snapshot", "seal", "--input", str(source), "--store", str(store)).stdout)
    manifest = json.loads(cli(
        "snapshot", "manifest", "--store", str(store), "--id", sealed["snapshot_id"],
    ).stdout)
    discovery_hash = next(
        partition["content_hash"]
        for partition in manifest["partitions"]
        if partition["name"] == "discovery"
    )
    campaign_result = cli(
        "campaign", "create", "--project", str(project), "--store", str(store),
        "--snapshot-id", sealed["snapshot_id"], "--attempts", "1",
        "--max-seconds", "30", "--initial-cash", "5000",
        "--transaction-cost-bps", "0", "--slippage-bps", "0",
    )
    assert campaign_result.returncode == 0, campaign_result.stderr
    campaign = json.loads(campaign_result.stdout)
    assert campaign["fingerprints"]["snapshot"] == discovery_hash
    source.unlink()
    store.rename(tmp_path / "detached-sealed-store")

    request = tmp_path / "first.json"
    request.write_text(json.dumps({
        "schema_version": 1,
        "campaign_id": campaign["campaign_id"],
        "submission_id": "first",
        "hypothesis": "Equal weights preserve value across a cash dividend.",
        "strategy": {"family": "equal_weight"},
        "parameters": {},
    }), encoding="utf-8")
    submitted = cli("experiment", "submit", "--project", str(project), "--input", str(request))
    assert submitted.returncode == 0, submitted.stderr
    result = json.loads(submitted.stdout)
    discovery = result["discovery"]
    assert result["fingerprints"]["snapshot"] == discovery_hash
    assert discovery["start_date"] == "2024-01-01"
    assert discovery["end_date"] == "2024-01-09"
    assert discovery["signals"][0]["date"] == "2024-01-01"
    assert discovery["order_attempts"][0]["date"] == "2024-01-02"
    assert len(discovery["fills"]) == 5
    ex_date = next(point for point in discovery["equity_curve"] if point["date"] == "2024-01-04")
    assert ex_date["dividend_receivables"] == 20
    assert ex_date["nav"] == 5000
    comparison = discovery["baseline_comparison"]
    assert comparison["start_date"] == discovery["start_date"]
    assert comparison["end_date"] == discovery["end_date"]
    assert comparison["return_delta"] == 0
    assert comparison["baseline"]["metrics"]["total_return"] == discovery["metrics"]["total_return"]
    assert "holdout" not in submitted.stdout.lower()
    assert "validation" not in submitted.stdout.lower()


def test_candidate_freeze_is_explicit_success_only_and_immutable(tmp_path: Path) -> None:
    project = tmp_path / "strategy-a"
    source = tmp_path / "snapshot.json"
    source.write_text(json.dumps(sealed_snapshot_input()), encoding="utf-8")
    assert cli("project", "init", "--project", str(project)).returncode == 0
    campaign = json.loads(cli(*campaign_options(project, source, attempts=2)).stdout)
    request = tmp_path / "first.json"
    payload = {
        "schema_version": 1,
        "campaign_id": campaign["campaign_id"],
        "submission_id": "first",
        "hypothesis": "Equal weights preserve value across a cash dividend.",
        "strategy": {"family": "equal_weight"},
        "parameters": {},
    }
    request.write_text(json.dumps(payload), encoding="utf-8")
    submitted = json.loads(cli(
        "experiment", "submit", "--project", str(project), "--input", str(request),
    ).stdout)
    assert submitted["status"] == "succeeded"

    frozen = cli(
        "candidate", "freeze", "--project", str(project),
        "--campaign-id", campaign["campaign_id"], "--submission-id", "first",
        "--name", "candidate-a", "--conclusion", "Retain as a research candidate.",
    )
    assert frozen.returncode == 0, frozen.stderr
    candidate = json.loads(frozen.stdout)
    assert candidate["candidate_id"] != submitted["experiment_id"]
    assert candidate["experiment_id"] == submitted["experiment_id"]
    assert candidate["strategy"] == {"family": "equal_weight"}
    assert candidate["parameters"] == {}
    assert candidate["fingerprints"]["snapshot"] == submitted["fingerprints"]["snapshot"]
    assert candidate["snapshot_id"] == campaign["snapshot_id"]
    assert candidate["evaluation"]["engine"] == "sealed-discovery-v1"
    assert candidate["execution_rules"]["execution"]["basis"] == "unadjusted"
    assert candidate["runtime_dependencies"]["snapshot_baseline.py"]

    payload.update(
        hypothesis="A changed workspace submission.",
        strategy={"family": "momentum"},
        parameters={"lookback": 2},
    )
    request.write_text(json.dumps(payload), encoding="utf-8")
    shown = cli(
        "candidate", "status", "--project", str(project),
        "--candidate-id", candidate["candidate_id"],
    )
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout) == candidate
    assert cli(
        "candidate", "status", "--project", str(project),
        "--candidate-id", candidate["candidate_id"], agent=True,
    ).returncode == 2

    short_campaign = json.loads(cli(*campaign_options(
        project, source, attempts=1, max_seconds=0.000001,
    )).stdout)
    payload.update(
        campaign_id=short_campaign["campaign_id"],
        submission_id="timed-out",
        strategy={"family": "equal_weight"},
        parameters={},
    )
    request.write_text(json.dumps(payload), encoding="utf-8")
    failed = json.loads(cli(
        "experiment", "submit", "--project", str(project), "--input", str(request),
    ).stdout)
    assert failed["status"] == "failed"
    rejected = cli(
        "candidate", "freeze", "--project", str(project),
        "--campaign-id", short_campaign["campaign_id"], "--submission-id", "timed-out",
        "--name", "failed-candidate", "--conclusion", "Must not freeze.",
    )
    assert rejected.returncode == 2
    assert "only a successful experiment" in rejected.stderr.lower()


def test_experiment_does_not_substitute_missing_open_or_close_prices(tmp_path: Path) -> None:
    project = tmp_path / "strategy-a"
    source = tmp_path / "snapshot.json"
    payload = sealed_snapshot_input()
    for row in payload["raw_bars"]:
        if row["date"] == "2024-01-02" and row["symbol"] == "159612":
            row["volume"] = 0
        if row["date"] == "2024-01-04" and row["symbol"] == "511260":
            row["close"] = None
        if row["date"] >= "2024-01-05":
            row["high"] = 11
            row["close"] = 11
    source.write_text(json.dumps(payload), encoding="utf-8")
    assert cli("project", "init", "--project", str(project)).returncode == 0
    campaign = json.loads(cli(
        *campaign_options(project, source, attempts=1), "--transaction-cost-bps", "0",
    ).stdout)
    request = tmp_path / "first.json"
    request.write_text(json.dumps({
        "schema_version": 1,
        "campaign_id": campaign["campaign_id"],
        "submission_id": "missing-prices",
        "hypothesis": "Missing prices prevent fabricated fills and NAV.",
        "strategy": {"family": "equal_weight"},
        "parameters": {},
    }), encoding="utf-8")
    submitted = cli("experiment", "submit", "--project", str(project), "--input", str(request))
    assert submitted.returncode == 0, submitted.stderr
    result = json.loads(submitted.stdout)
    assert result["data_kind"] == "synthetic"
    discovery = result["discovery"]
    first_attempt = discovery["order_attempts"][0]
    assert first_attempt["date"] == "2024-01-02"
    assert first_attempt["status"] == "not_filled"
    assert first_attempt["missing_symbols"] == ["159612"]
    assert not any(fill["date"] == "2024-01-02" for fill in discovery["fills"])
    missing_nav = next(
        point for point in discovery["equity_curve"] if point["date"] == "2024-01-04"
    )
    assert missing_nav["nav"] is None
    assert any(
        gap["date"] == "2024-01-04"
        and gap["symbol"] == "511260"
        and gap["reason"] == "missing_or_unconfirmed_unadjusted_close"
        for gap in discovery["gaps"]["valuation"]
    )
    curve = discovery["equity_curve"]
    expected_adjacent_observations = sum(
        left["nav"] is not None and right["nav"] is not None
        for left, right in zip(curve, curve[1:], strict=False)
    )
    assert discovery["metrics"]["sharpe"] is None
    quarterly_metrics = result["quarterly_summary"]["quarters"][0]["candidate"]
    assert quarterly_metrics["daily_return_coverage"] == {
        "observed_adjacent_returns": expected_adjacent_observations,
        "expected_adjacent_returns": len(curve) - 1,
        "fraction": expected_adjacent_observations / (len(curve) - 1),
    }
    assert quarterly_metrics["missing_reasons"]["daily_returns"] == "missing_adjacent_nav_pair"


def test_concurrent_replay_conflict_and_invalid_submissions_do_not_double_charge(tmp_path: Path) -> None:
    project = tmp_path / "strategy-a"
    source = tmp_path / "discovery.json"
    snapshot_file(source)
    assert cli("project", "init", "--project", str(project)).returncode == 0
    campaign = json.loads(cli(*campaign_options(project, source, attempts=1)).stdout)
    payload = {
        "schema_version": 1, "campaign_id": campaign["campaign_id"],
        "submission_id": "same", "hypothesis": "Equal weights match benchmark.",
        "strategy": {"family": "equal_weight"}, "parameters": {},
    }
    request = tmp_path / "request.json"
    request.write_text(json.dumps(payload), encoding="utf-8")
    args = ("experiment", "submit", "--project", str(project), "--input", str(request))
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: cli(*args), range(4)))
    assert all(response.returncode == 0 for response in responses)
    receipts = [json.loads(response.stdout) for response in responses]
    assert len({receipt["experiment_id"] for receipt in receipts}) == 1
    assert all(receipt["budget"]["attempts_remaining"] == 0 for receipt in receipts)

    payload["hypothesis"] = "A different hypothesis."
    request.write_text(json.dumps(payload), encoding="utf-8")
    conflict = json.loads(cli(*args).stdout)
    assert conflict["error"]["code"] == "SUBMISSION_CONFLICT"
    assert conflict["budget"]["attempts_remaining"] == 0
    payload["submission_id"] = "new"
    payload["parameters"] = {"invalid": 1}
    request.write_text(json.dumps(payload), encoding="utf-8")
    invalid = json.loads(cli(*args).stdout)
    assert invalid["error"]["code"] == "INVALID_SUBMISSION"
    assert invalid["budget"]["attempts_remaining"] == 0
    payload["parameters"] = {}
    request.write_text(json.dumps(payload), encoding="utf-8")
    exhausted = json.loads(cli(*args).stdout)
    assert exhausted["error"]["code"] == "BUDGET_EXHAUSTED"


def test_execution_timeout_is_charged_and_blocks_new_attempts(tmp_path: Path) -> None:
    project = tmp_path / "strategy-a"
    source = tmp_path / "discovery.json"
    snapshot_file(source)
    assert cli("project", "init", "--project", str(project)).returncode == 0
    campaign = json.loads(cli(*campaign_options(
        project, source, attempts=2, max_seconds=0.000001,
    )).stdout)
    payload = {
        "schema_version": 1, "campaign_id": campaign["campaign_id"],
        "submission_id": "timed", "hypothesis": "Equal weights match benchmark.",
        "strategy": {"family": "equal_weight"}, "parameters": {},
    }
    request = tmp_path / "request.json"
    request.write_text(json.dumps(payload), encoding="utf-8")
    args = ("experiment", "submit", "--project", str(project), "--input", str(request))
    result = json.loads(cli(*args).stdout)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "BUDGET_EXHAUSTED"
    assert result["budget"]["attempts_remaining"] == 1
    assert result["budget"]["execution_seconds_remaining"] == 0
    payload["submission_id"] = "next"
    request.write_text(json.dumps(payload), encoding="utf-8")
    result = json.loads(cli(*args).stdout)
    assert result["status"] == "rejected"
    assert result["error"]["code"] == "BUDGET_EXHAUSTED"
    assert result["budget"]["attempts_remaining"] == 1
    terminal = json.loads(cli(
        "experiment", "resume", "--project", str(project), "--campaign-id",
        campaign["campaign_id"], "--submission-id", "timed", agent=True,
    ).stdout)
    assert terminal["status"] == "failed"
    assert terminal["error"]["phase"] == "discovery"
    assert terminal["budget"] == result["budget"]


def kill_after_admission(submit_args: tuple[str, ...], status_args: tuple[str, ...]) -> dict:
    with subprocess.Popen(
        [*AGENT_COMMAND, *submit_args], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env=cli_env(),
    ) as process:
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                status = json.loads(cli(*status_args, agent=True).stdout)
                if status["status"] == "running":
                    process.kill()
                    assert process.wait(timeout=10) == -signal.SIGKILL
                    return status
                assert process.poll() is None
            raise AssertionError("Experiment did not start")
        finally:
            if process.poll() is None:
                process.kill()


def test_replacement_client_resumes_once_and_matches_uninterrupted_run(tmp_path: Path) -> None:
    project = tmp_path / "strategy-a"
    source = tmp_path / "discovery.json"
    snapshot_file(source, day_count=600)
    identity = json.loads(cli("project", "init", "--project", str(project)).stdout)
    campaign = json.loads(cli(*campaign_options(project, source, attempts=1)).stdout)
    control_campaign = json.loads(cli(*campaign_options(project, source, attempts=1)).stdout)
    payload = {
        "schema_version": 1, "campaign_id": campaign["campaign_id"],
        "submission_id": "interrupted", "hypothesis": "Momentum beats the benchmark.",
        "strategy": {"family": "momentum"}, "parameters": {"lookback": 20},
    }
    request = tmp_path / "request.json"
    request.write_text(json.dumps(payload), encoding="utf-8")
    submit_args = ("experiment", "submit", "--project", str(project), "--input", str(request))
    lookup = (
        "--project", str(project),
        "--campaign-id", campaign["campaign_id"], "--submission-id", "interrupted",
    )
    live = kill_after_admission(submit_args, ("experiment", "status", *lookup))
    interrupted = json.loads(cli("experiment", "status", *lookup, agent=True).stdout)
    assert interrupted["status"] == "failed"
    assert interrupted["error"]["code"] == "EXECUTION_INTERRUPTED"
    assert interrupted["error"]["next_action"] == "resume"
    assert interrupted["experiment_id"] == live["experiment_id"]
    assert interrupted["budget"]["attempts_remaining"] == 0
    assert 0 < interrupted["budget"]["execution_seconds_remaining"] < 30
    replay = json.loads(cli(*submit_args, agent=True).stdout)
    assert replay["experiment_id"] == live["experiment_id"]
    assert replay["status"] == "failed"
    assert replay["budget"] == interrupted["budget"]

    payload["submission_id"] = "new"
    request.write_text(json.dumps(payload), encoding="utf-8")
    denied = json.loads(cli(*submit_args, agent=True).stdout)
    assert denied["status"] == "rejected"
    assert denied["error"]["code"] == "BUDGET_EXHAUSTED"
    assert denied["budget"] == interrupted["budget"]
    request.unlink()

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: cli("experiment", "resume", *lookup, agent=True), range(2)))
    assert all(response.returncode == 0 for response in responses)
    resumed, concurrent = [json.loads(response.stdout) for response in responses]
    assert resumed == concurrent
    assert resumed["status"] == "succeeded"
    assert resumed["project_id"] == identity["project_id"]
    assert resumed["experiment_id"] == live["experiment_id"]
    assert resumed["submission_id"] == "interrupted"
    assert "error" not in resumed
    assert resumed["budget"]["attempts_remaining"] == 0
    assert 0 < resumed["budget"]["execution_seconds_remaining"] < interrupted["budget"]["execution_seconds_remaining"]

    payload.update(campaign_id=control_campaign["campaign_id"], submission_id="interrupted")
    request.write_text(json.dumps(payload), encoding="utf-8")
    control = json.loads(cli(*submit_args, agent=True).stdout)
    assert control["status"] == "succeeded"
    for field in ("fingerprints", "discovery", "diagnostics"):
        assert resumed[field] == control[field]


def test_time_exhaustion_blocks_interrupted_resume_but_keeps_prior_result(tmp_path: Path) -> None:
    project = tmp_path / "strategy-a"
    source = tmp_path / "discovery.json"
    snapshot_file(source, day_count=600)
    assert cli("project", "init", "--project", str(project)).returncode == 0
    campaign = json.loads(cli(*campaign_options(project, source, attempts=3, max_seconds=8)).stdout)
    lookup = ("--project", str(project), "--campaign-id", campaign["campaign_id"], "--submission-id")
    path = tmp_path / "request.json"
    submit_args = ("experiment", "submit", "--project", str(project), "--input", str(path))
    payload = {
        "schema_version": 1, "campaign_id": campaign["campaign_id"],
        "submission_id": "first", "hypothesis": "Equal weights match benchmark.",
        "strategy": {"family": "equal_weight"}, "parameters": {},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    first = json.loads(cli(*submit_args, agent=True).stdout)
    assert first["status"] == "succeeded"
    payload.update(submission_id="pending", hypothesis="Momentum beats benchmark.",
                   strategy={"family": "momentum"}, parameters={"lookback": 20})
    path.write_text(json.dumps(payload), encoding="utf-8")
    live = kill_after_admission(submit_args, ("experiment", "status", *lookup, "pending"))
    time.sleep(live["budget"]["execution_seconds_remaining"])
    denied = json.loads(cli("experiment", "resume", *lookup, "pending", agent=True).stdout)
    assert denied["status"] == "rejected"
    assert denied["error"]["code"] == "BUDGET_EXHAUSTED"
    assert denied["experiment_id"] == live["experiment_id"]
    assert denied["budget"] == {"attempts_remaining": 1, "execution_seconds_remaining": 0}
    interrupted = json.loads(cli("experiment", "status", *lookup, "pending", agent=True).stdout)
    assert interrupted["error"]["code"] == "EXECUTION_INTERRUPTED"
    assert interrupted["error"]["next_action"] == "stop"
    prior = json.loads(cli("experiment", "resume", *lookup, "first", agent=True).stdout)
    assert prior == {**first, "budget": denied["budget"]}


def test_agent_surface_cannot_initialize_or_create_campaigns(tmp_path: Path) -> None:
    project = tmp_path / "strategy-a"
    source = tmp_path / "discovery.json"
    snapshot_file(source)
    init = ("project", "init", "--project", str(project))
    assert cli(*init, agent=True).returncode == 2
    assert not project.exists()
    identity = json.loads(cli(*init).stdout)
    assert json.loads(cli("project", "status", "--project", str(project), agent=True).stdout) == identity
    for action in ("create", "reset", "increase-budget"):
        assert cli("campaign", action, "--project", str(project), "--store", str(source),
                   "--snapshot-id", "unused", "--attempts", "2", "--max-seconds", "30",
                   agent=True).returncode == 2


def test_parent_and_campaign_configuration_are_checked_before_admission(tmp_path: Path) -> None:
    project = tmp_path / "strategy-a"
    source = tmp_path / "discovery.json"
    snapshot_file(source)
    assert cli("project", "init", "--project", str(project)).returncode == 0
    first_campaign = json.loads(cli(*campaign_options(project, source)).stdout)
    other_campaign = json.loads(cli(*campaign_options(project, source)).stdout)
    payload = {
        "schema_version": 1, "campaign_id": first_campaign["campaign_id"],
        "submission_id": "first", "hypothesis": "Equal weights match benchmark.",
        "strategy": {"family": "equal_weight"}, "parameters": {},
    }
    request = tmp_path / "request.json"
    args = ("experiment", "submit", "--project", str(project), "--input", str(request))
    request.write_text(json.dumps(payload), encoding="utf-8")
    first = json.loads(cli(*args).stdout)
    payload.update(submission_id="child", parent_experiment_id=first["experiment_id"])
    request.write_text(json.dumps(payload), encoding="utf-8")
    child = json.loads(cli(*args).stdout)
    assert child["status"] == "succeeded"
    assert child["experiment_id"] != first["experiment_id"]
    assert child["parent_experiment_id"] == first["experiment_id"]
    payload.update(campaign_id=other_campaign["campaign_id"], submission_id="cross")
    request.write_text(json.dumps(payload), encoding="utf-8")
    cross = json.loads(cli(*args).stdout)
    assert cross["error"]["code"] == "INVALID_SUBMISSION"
    assert cross["budget"]["attempts_remaining"] == 2
    del payload["parent_experiment_id"]
    payload["max_seconds"] = 100
    request.write_text(json.dumps(payload), encoding="utf-8")
    override = json.loads(cli(*args).stdout)
    assert override["error"]["code"] == "INVALID_SUBMISSION"
    assert override["budget"]["attempts_remaining"] == 2
