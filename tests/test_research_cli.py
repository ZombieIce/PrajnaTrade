import json
import html
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import prajnatrade.research as research
import pytest
from prajnatrade.data import load_bars
from prajnatrade.providers import MockProvider, Proposal, ProviderBudgetExceeded, ProviderError, Reflection
from prajnatrade.research import campaign_status, resume_campaign, start_campaign
from prajnatrade.store import SQLiteStore
from prajnatrade.strategies import make_strategy
from prajnatrade.validation import freeze_candidate, validate_candidate

SYMBOLS = ("511260", "518880", "515080", "513300", "159612")


def _write_bars(path: Path, day_count: int = 12, start_day: int = 0) -> None:
    lines = ["date,symbol,open,high,low,close,volume,price_basis"]
    for day_index in range(day_count):
        day = (date(2024, 1, 1) + timedelta(days=day_index + start_day)).isoformat()
        for symbol_index, symbol in enumerate(SYMBOLS):
            close = 100 + symbol_index + day_index * (symbol_index + 1)
            open_price = close - 1
            lines.append(
                f"{day},{symbol},{open_price},{close},{open_price},{close},"
                "1000,total_return"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        (str(Path(__file__).parents[1] / "src"), env.get("PYTHONPATH", ""))
    )
    return subprocess.run(
        [sys.executable, "-m", "prajnatrade", *args],
        capture_output=True,
        text=True,
        env=env,
    )


def test_holdout_is_imported_only_after_confirmation(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    development = tmp_path / "development.csv"
    holdout = tmp_path / "holdout.csv"
    database = tmp_path / "research.sqlite"
    _write_bars(source, day_count=24)
    header, *rows = source.read_text(encoding="utf-8").splitlines()
    development.write_text(
        "\n".join([header, *(row for row in rows if row[:10] <= "2024-01-18")]) + "\n",
        encoding="utf-8",
    )
    holdout_rows = [row for row in rows if row[:10] >= "2024-01-19"]
    source.unlink()

    started = _run_cli(
        "research", "run", "--db", str(database), "--data", str(development),
        "--price-basis", "total_return", "--data-kind", "synthetic",
        "--holdout-start", "2024-01-19", "--holdout-end", "2024-01-24",
        "--provider", "mock", "--seed", "11", "--experiments", "1",
    )
    assert started.returncode == 0, started.stderr
    campaign_id = json.loads(started.stdout)["campaign_id"]
    status = _run_cli(
        "research", "status", "--db", str(database), "--campaign-id", campaign_id,
    )
    campaign = json.loads(status.stdout)
    assert campaign["campaign"]["split"]["holdout_start"] == "2024-01-19"
    experiment_id = campaign["experiments"][0]["experiment_id"]
    frozen = _run_cli(
        "research", "freeze", "--db", str(database), "--campaign-id", campaign_id,
        "--experiment-id", experiment_id, "--name", "candidate",
        "--conclusion", "Frozen before independent validation.",
    )
    assert frozen.returncode == 0, frozen.stderr
    candidate_id = json.loads(frozen.stdout)["candidate_id"]
    validation = _run_cli(
        "research", "validate", "--db", str(database), "--candidate-id", candidate_id,
        "--data", str(development), "--price-basis", "total_return",
        "--data-kind", "synthetic",
    )
    assert validation.returncode == 0, validation.stderr

    args = [
        "research", "holdout", "--db", str(database), "--candidate-id", candidate_id,
        "--data", str(holdout), "--price-basis", "total_return",
        "--data-kind", "synthetic",
    ]
    denied = _run_cli(*args, "--confirmation", "not confirmed")
    assert denied.returncode == 2
    assert "confirmation" in denied.stderr.lower()
    assert not holdout.exists()

    holdout.write_text("\n".join([header, *holdout_rows]) + "\n", encoding="utf-8")
    evaluated = _run_cli(
        *args, "--confirmation", "I CONFIRM THE CANDIDATE AND CONCLUSIONS ARE FROZEN",
    )
    assert evaluated.returncode == 0, evaluated.stderr
    result = json.loads(evaluated.stdout)
    assert result["start_date"] == "2024-01-19"
    assert result["end_date"] == "2024-01-24"
    assert result["result"]["source_data_hash"] != campaign["campaign"]["data_hash"]


def test_campaign_rejects_development_data_overlapping_holdout(tmp_path: Path) -> None:
    bars = tmp_path / "bars.csv"
    _write_bars(bars, day_count=24)
    started = _run_cli(
        "research", "run", "--db", str(tmp_path / "research.sqlite"),
        "--data", str(bars), "--price-basis", "total_return",
        "--data-kind", "synthetic", "--holdout-start", "2024-01-19",
        "--holdout-end", "2024-01-24",
    )
    assert started.returncode == 2
    assert "development data must end before" in started.stderr.lower()


def test_mock_campaign_can_pause_resume_and_preserve_every_attempt(
    tmp_path: Path,
) -> None:
    bars = tmp_path / "bars.csv"
    database = tmp_path / "research.sqlite"
    _write_bars(bars, day_count=24)
    common = [
        "--db",
        str(database),
        "--data",
        str(bars),
        "--price-basis",
        "total_return",
        "--data-kind",
        "synthetic",
        "--provider",
        "mock",
        "--seed",
        "1",
        "--holdout-start",
        "2025-01-01",
        "--holdout-end",
        "2025-01-31",
    ]

    first = _run_cli(
        "research",
        "run",
        *common,
        "--experiments",
        "4",
        "--steps",
        "2",
    )

    assert first.returncode == 0, first.stderr
    first_result = json.loads(first.stdout)
    assert first_result["status"] == "paused"
    assert first_result["completed_experiments"] == 2
    campaign_id = first_result["campaign_id"]

    status = _run_cli(
        "research",
        "status",
        "--db",
        str(database),
        "--campaign-id",
        campaign_id,
    )
    assert status.returncode == 0, status.stderr
    status_result = json.loads(status.stdout)
    assert len(status_result["experiments"]) == 2
    assert all(
        item["data_hash"] == status_result["campaign"]["data_hash"]
        for item in status_result["experiments"]
    )

    # A process interrupted between checkpoints leaves the persisted status running.
    SQLiteStore(database).update_campaign(campaign_id, status="running")
    resumed = _run_cli(
        "research",
        "resume",
        "--db",
        str(database),
        "--campaign-id",
        campaign_id,
        "--steps",
        "2",
    )
    assert resumed.returncode == 0, resumed.stderr
    assert json.loads(resumed.stdout)["status"] == "completed"

    final_status = _run_cli(
        "research",
        "status",
        "--db",
        str(database),
        "--campaign-id",
        campaign_id,
    )
    experiments = json.loads(final_status.stdout)["experiments"]
    assert len(experiments) == 4
    assert len({item["experiment_id"] for item in experiments}) == 4
    assert len({item["strategy"]["family"] for item in experiments}) > 1
    assert {"keep", "reject", "branch"} <= {item["decision"] for item in experiments}
    for previous, following in zip(experiments, experiments[1:]):
        if previous["decision"] in {"keep", "investigate"}:
            assert following["strategy"]["family"] == previous["strategy"]["family"]
            if previous["strategy"]["family"] != "equal_weight":
                assert following["parameters"] != previous["parameters"]
        elif previous["decision"] in {"reject", "branch"}:
            assert following["strategy"]["family"] != previous["strategy"]["family"]

    uninterrupted = _run_cli(
        "research", "run", "--db", str(tmp_path / "second.sqlite"),
        "--data", str(bars), "--price-basis", "total_return",
        "--data-kind", "synthetic", "--provider", "mock", "--seed", "1",
        "--experiments", "4", "--holdout-start", "2025-01-01",
        "--holdout-end", "2025-01-31",
    )
    assert uninterrupted.returncode == 0, uninterrupted.stderr
    second_id = json.loads(uninterrupted.stdout)["campaign_id"]
    second_status = _run_cli(
        "research", "status", "--db", str(tmp_path / "second.sqlite"),
        "--campaign-id", second_id,
    )
    second = json.loads(second_status.stdout)["experiments"]
    assert [(e["strategy"], e["decision"]) for e in experiments] == [
        (e["strategy"], e["decision"]) for e in second
    ]


def test_mock_investigates_a_recorded_control_result(tmp_path: Path) -> None:
    bars = tmp_path / "bars.csv"
    database = tmp_path / "research.sqlite"
    _write_bars(bars, day_count=24)
    started = _run_cli(
        "research", "run", "--db", str(database), "--data", str(bars),
        "--price-basis", "total_return", "--data-kind", "synthetic",
        "--provider", "mock", "--seed", "0", "--experiments", "8",
        "--holdout-start", "2025-01-01", "--holdout-end", "2025-01-31",
    )
    assert started.returncode == 0, started.stderr
    status = _run_cli(
        "research", "status", "--db", str(database), "--campaign-id",
        json.loads(started.stdout)["campaign_id"],
    )
    experiments = json.loads(status.stdout)["experiments"]
    assert experiments[6]["decision"] == "investigate"
    assert experiments[7]["strategy"]["family"] == experiments[6]["strategy"]["family"]


def test_provider_failure_is_retained_and_stops_at_the_failure_budget(
    tmp_path: Path,
) -> None:
    bars = tmp_path / "bars.csv"
    database = tmp_path / "research.sqlite"
    _write_bars(bars)
    dataset = load_bars(bars, "total_return")

    class FailingProvider:
        name = "test-provider"
        model = "failing-v1"

        def propose(self, iteration, context):
            raise ProviderError("deterministic test failure")

        def reflect(self, proposal, result, context):
            raise AssertionError("reflection must not run when proposal fails")

    result = start_campaign(
        database=database,
        dataset=dataset,
        data_path=bars,
        data_kind="synthetic",
        provider=FailingProvider(),
        seed=3,
        experiment_budget=3,
        max_consecutive_failures=1,
        max_seconds=60,
        initial_cash=10_000,
        transaction_cost_bps=0,
        slippage_bps=0,
        token_budget=100,
        cost_budget_usd=1,
        steps=None,
        holdout_start="2025-01-01",
        holdout_end="2025-01-31",
    )

    status = campaign_status(database, result["campaign_id"])
    assert result["status"] == "stopped"
    assert result["stop_reason"] == "consecutive_failures"
    assert len(status["experiments"]) == 1
    assert status["experiments"][0]["status"] == "failed"
    assert status["experiments"][0]["error"] == "deterministic test failure"
    assert status["experiments"][0]["data_hash"] == status["campaign"]["data_hash"]


def test_interruption_during_reflection_preserves_completed_backtest(tmp_path: Path) -> None:
    bars = tmp_path / "bars.csv"
    database = tmp_path / "research.sqlite"
    _write_bars(bars, day_count=24)
    started = _run_cli(
        "research", "run", "--db", str(database), "--data", str(bars),
        "--price-basis", "total_return", "--data-kind", "synthetic",
        "--provider", "mock", "--seed", "1", "--experiments", "2", "--steps", "1",
        "--holdout-start", "2025-01-01", "--holdout-end", "2025-01-31",
    )
    assert started.returncode == 0, started.stderr
    campaign_id = json.loads(started.stdout)["campaign_id"]

    class InterruptedProvider:
        name = "mock"
        model = "deterministic-mock-v1"

        def __init__(self) -> None:
            self.proposals = 0
            self.interrupted = False
            self.mock = MockProvider(1)

        def propose(self, iteration, context):
            self.proposals += 1
            proposal = self.mock.propose(iteration, context)
            return Proposal(
                hypothesis=proposal.hypothesis,
                rationale=proposal.rationale,
                strategy=proposal.strategy,
                usage={"requests": 1, "total_tokens": 10, "estimated_cost_usd": 0.01},
            )

        def reflect(self, proposal, result, context):
            if not self.interrupted:
                self.interrupted = True
                raise KeyboardInterrupt
            return self.mock.reflect(proposal, result, context)

    provider = InterruptedProvider()
    with pytest.raises(KeyboardInterrupt):
        resume_campaign(database=database, campaign_id=campaign_id, provider=provider)

    interrupted = campaign_status(database, campaign_id)["experiments"]
    assert len(interrupted) == 2
    pending = interrupted[1]
    assert pending["status"] == "running"
    assert pending["metrics"]["ending_equity"] > 0
    assert pending["provider_usage"]["total_tokens"] == 10

    resumed = resume_campaign(database=database, campaign_id=campaign_id, provider=provider)
    attempts = campaign_status(database, campaign_id)["experiments"]
    assert resumed["status"] == "completed"
    assert provider.proposals == 1
    assert attempts[1]["experiment_id"] == pending["experiment_id"]
    assert attempts[1]["metrics"] == pending["metrics"]
    assert attempts[1]["provider_usage"]["total_tokens"] == 10


def test_repeated_reflection_failures_stop_at_consecutive_failure_budget(
    tmp_path: Path,
) -> None:
    bars = tmp_path / "bars.csv"
    database = tmp_path / "research.sqlite"
    _write_bars(bars)

    class FailingReflectionProvider:
        name = "test-provider"
        model = "failing-reflection-v1"

        def propose(self, iteration, context):
            return Proposal(
                hypothesis="Equal weights track the baseline.",
                rationale="Use a known valid strategy.",
                strategy={"family": "equal_weight", "parameters": {}},
                usage={"requests": 0, "total_tokens": 0, "estimated_cost_usd": 0.0},
            )

        def reflect(self, proposal, result, context):
            raise ProviderError("deterministic reflection failure")

    result = start_campaign(
        database=database,
        dataset=load_bars(bars, "total_return"),
        data_path=bars,
        data_kind="synthetic",
        provider=FailingReflectionProvider(),
        seed=3,
        experiment_budget=5,
        max_consecutive_failures=2,
        max_seconds=60,
        initial_cash=10_000,
        transaction_cost_bps=0,
        slippage_bps=0,
        token_budget=100,
        cost_budget_usd=1,
        steps=1,
        holdout_start="2025-01-01",
        holdout_end="2025-01-31",
    )

    first = campaign_status(database, result["campaign_id"])["experiments"]
    assert result["status"] == "paused"
    assert len(first) == 1
    result = resume_campaign(
        database=database,
        campaign_id=result["campaign_id"],
        provider=FailingReflectionProvider(),
    )
    experiments = campaign_status(database, result["campaign_id"])["experiments"]
    assert result["stop_reason"] == "consecutive_failures"
    assert len(experiments) == 2
    assert experiments[0]["experiment_id"] == first[0]["experiment_id"]
    assert all(item["status"] == "failed" for item in experiments)
    assert all(item["error"] == "deterministic reflection failure" for item in experiments)


def test_time_budget_stops_between_attempts(tmp_path: Path, monkeypatch) -> None:
    bars = tmp_path / "bars.csv"
    database = tmp_path / "research.sqlite"
    _write_bars(bars)
    clock = {"seconds": 0.0}
    monkeypatch.setattr(
        research, "time", SimpleNamespace(monotonic=lambda: clock["seconds"])
    )

    class SlowProvider:
        name = "test-provider"
        model = "slow-v1"

        def propose(self, iteration, context):
            clock["seconds"] = 2.0
            return Proposal(
                hypothesis="Equal weights track the baseline.",
                rationale="Use a known valid strategy.",
                strategy={"family": "equal_weight", "parameters": {}},
                usage={"requests": 0, "total_tokens": 0, "estimated_cost_usd": 0.0},
            )

        def reflect(self, proposal, result, context):
            return Reflection(
                decision="keep", text="Retain result.",
                usage={"requests": 0, "total_tokens": 0, "estimated_cost_usd": 0.0},
            )

    result = start_campaign(
        database=database,
        dataset=load_bars(bars, "total_return"),
        data_path=bars,
        data_kind="synthetic",
        provider=SlowProvider(),
        seed=3,
        experiment_budget=5,
        max_consecutive_failures=2,
        max_seconds=1,
        initial_cash=10_000,
        transaction_cost_bps=0,
        slippage_bps=0,
        token_budget=100,
        cost_budget_usd=1,
        steps=None,
        holdout_start="2025-01-01",
        holdout_end="2025-01-31",
    )

    assert result["stop_reason"] == "time_budget"
    assert len(campaign_status(database, result["campaign_id"])["experiments"]) == 1


def test_generated_python_requires_explicit_opt_in_before_execution(tmp_path: Path) -> None:
    marker = tmp_path / "executed"
    spec = {
        "family": "python",
        "parameters": {},
        "source": (
            f"open({str(marker)!r}, 'w').write('executed')\n"
            "def strategy(history, state):\n"
            "    return {'CASH': 1.0}\n"
        ),
    }

    with pytest.raises(ValueError, match="trusted-local"):
        make_strategy(spec)
    assert not marker.exists()
    strategy = make_strategy(spec, trusted_local_python=True)
    assert marker.read_text(encoding="utf-8") == "executed"
    assert strategy(None, {}) == {"CASH": 1.0}


def test_exhausted_reflection_budget_retains_newly_completed_backtest(tmp_path: Path) -> None:
    class BudgetProvider(MockProvider):
        def reflect(self, proposal, result, context):
            raise ProviderBudgetExceeded("token_budget", "No reflection budget remains.")

    bars = tmp_path / "bars.csv"
    database = tmp_path / "research.sqlite"
    _write_bars(bars, day_count=20)
    started = start_campaign(
        database=database, dataset=load_bars(bars, "total_return"), data_path=bars,
        data_kind="synthetic", provider=BudgetProvider(1), seed=1,
        experiment_budget=1, max_consecutive_failures=1, max_seconds=60,
        initial_cash=10_000, transaction_cost_bps=0, slippage_bps=0,
        token_budget=5000, cost_budget_usd=1, steps=None,
        holdout_start="2025-01-01", holdout_end="2025-01-31",
    )
    experiment = campaign_status(database, started["campaign_id"])["experiments"][0]
    assert started["stop_reason"] == "token_budget"
    assert experiment["status"] == "succeeded"
    assert experiment["metrics"] is not None
    assert "Reflection not completed" in experiment["reflection"]
    freeze_candidate(database, started["campaign_id"], experiment["experiment_id"], "candidate", "Frozen")


def test_generated_strategy_cannot_declare_non_numeric_parameters_absent() -> None:
    with pytest.raises(ValueError, match="no_tunable_parameters"):
        make_strategy({
            "family": "python",
            "parameters": {"enabled": True},
            "parameter_schema": {
                "parameters": {}, "constraints": [], "no_tunable_parameters": True,
            },
            "source": "def strategy(history, state):\n    return {CASH: 1.0}\n",
        }, trusted_local_python=True)


@pytest.mark.parametrize("fail_variant,minimum", [(False, 2), (True, 2), (False, 10)])
def test_generated_strategy_freezes_and_evaluates_declared_parameter_plan(
    tmp_path: Path, fail_variant: bool, minimum: int,
) -> None:
    class ParameterProvider(MockProvider):
        def propose(self, iteration, context):
            return Proposal(
                hypothesis="Test a declared parameter", rationale="Frozen sensitivity",
                strategy={
                    "family": "python", "parameters": {"window": 10},
                    "parameter_schema": {
                        "parameters": {
                            "window": {
                                "type": "integer", "default": 10,
                                "min": minimum, "max": 20, "step": 1,
                            }
                        },
                        "constraints": [],
                    },
                    "source": (
                        "def strategy(history, state):\n"
                        + ("    if parameters['window'] == 11: raise ValueError('bad variant')\n"
                           if fail_variant else "")
                        +
                        "    return {ETF_SYMBOLS[parameters['window'] % 5]: 1.0}\n"
                    ),
                },
                usage={"total_tokens": 0, "estimated_cost_usd": 0, "requests": 0},
            )

    bars = tmp_path / "bars.csv"
    database = tmp_path / "research.sqlite"
    _write_bars(bars, day_count=40)
    lines = bars.read_text(encoding="utf-8").splitlines()
    shock = (date(2024, 1, 1) + timedelta(days=35)).isoformat()
    for index, line in enumerate(lines):
        if line.startswith(shock):
            fields = line.split(",")
            fields[2:6] = [str(float(value) * 0.5) for value in fields[2:6]]
            lines[index] = ",".join(fields)
    bars.write_text("\n".join(lines) + "\n", encoding="utf-8")
    started = start_campaign(
        database=database, dataset=load_bars(bars, "total_return"), data_path=bars,
        data_kind="synthetic", provider=ParameterProvider(1), seed=1,
        experiment_budget=1, max_consecutive_failures=1, max_seconds=60,
        initial_cash=10_000, transaction_cost_bps=5, slippage_bps=0,
        token_budget=5000, cost_budget_usd=1, steps=None,
        holdout_start="2025-01-01", holdout_end="2025-01-31",
        trusted_local_python=True, max_drawdown_limit=0.0,
    )
    experiment = campaign_status(database, started["campaign_id"])["experiments"][0]
    frozen = freeze_candidate(database, started["campaign_id"], experiment["experiment_id"],
                              "candidate", "Fixed before validation")
    result = validate_candidate(
        database, frozen["candidate_id"], bars, "synthetic", "total_return",
        trusted_local_python=True,
    )["result"]["parameter_robustness"]
    assert frozen["robustness_plan"]["hard_risk_limit"] == {"max_drawdown": 0.0}
    assert [item["parameters"]["window"] for item in result["variants"]] == (
        [11] if minimum == 10 else [9, 11]
    )
    if minimum == 10:
        assert result["plan"]["excluded"][0]["reason"] == "outside_declared_range"
    if fail_variant:
        assert [item["status"] for item in result["variants"]] == ["evaluated", "execution_failed"]
        assert result["coverage"] == 0.5
        assert result["status"] == "inconclusive"
    else:
        assert all(item["status"] == "evaluated" for item in result["variants"])
        assert result["status"] in {"passed", "failed"}
        assert all(item["risk_limit_breach"] for item in result["variants"])


@pytest.mark.parametrize("operator", ["<=", "=="])
def test_joint_parameter_perturbation_is_frozen_before_validation(
    tmp_path: Path, operator: str,
) -> None:
    class JointProvider(MockProvider):
        def propose(self, iteration, context):
            return Proposal(
                hypothesis="Joint perturbation", rationale="Keep the ordering",
                strategy={
                    "family": "python",
                    "parameters": {"fast": 10, "slow": 10},
                    "parameter_schema": {
                        "parameters": {
                            name: {"type": "integer", "default": 10, "min": 2, "max": 20, "step": 1}
                            for name in ("fast", "slow")
                        },
                        "constraints": [{"left": "fast", "op": operator, "right": "slow"}],
                        "joint_variants": [
                            {"parameters": {"fast": 11, "slow": 11}, "reason": "Preserve ordering"}
                        ],
                    },
                    "source": (
                        "def strategy(history, state):\n"
                        "    return {ETF_SYMBOLS[(parameters['fast'] + parameters['slow']) % 5]: 1.0}\n"
                    ),
                },
                usage={"total_tokens": 0, "estimated_cost_usd": 0, "requests": 0},
            )

    bars = tmp_path / "bars.csv"
    database = tmp_path / "research.sqlite"
    _write_bars(bars, day_count=40)
    started = start_campaign(
        database=database, dataset=load_bars(bars, "total_return"), data_path=bars,
        data_kind="synthetic", provider=JointProvider(1), seed=1,
        experiment_budget=1, max_consecutive_failures=1, max_seconds=60,
        initial_cash=10_000, transaction_cost_bps=0, slippage_bps=0,
        token_budget=5000, cost_budget_usd=1, steps=None,
        holdout_start="2025-01-01", holdout_end="2025-01-31",
        trusted_local_python=True,
    )
    experiment = campaign_status(database, started["campaign_id"])["experiments"][0]
    frozen = freeze_candidate(database, started["campaign_id"], experiment["experiment_id"],
                              "candidate", "Fixed before validation")
    plan = frozen["robustness_plan"]
    joint = [variant for variant in plan["variants"] if len(variant["changed_parameters"]) == 2]
    assert len(joint) == 1
    assert joint[0]["reason"] is None
    assert joint[0]["selection_reason"] == "Preserve ordering"
    result = validate_candidate(
        database, frozen["candidate_id"], bars, "synthetic", "total_return",
        trusted_local_python=True,
    )["result"]["parameter_robustness"]
    assert result["plan"] == plan
    assert len(result["variants"]) == (3 if operator == "<=" else 1)


def test_frozen_plan_covers_all_six_parameters_before_second_perturbations(tmp_path: Path) -> None:
    class SixParameterProvider(MockProvider):
        def propose(self, iteration, context):
            names = [f"p{i}" for i in range(6)]
            return Proposal(
                hypothesis="Six parameters", rationale="Cover each first",
                strategy={
                    "family": "python",
                    "parameters": {name: 10 for name in names},
                    "parameter_schema": {
                        "parameters": {
                            name: {"type": "integer", "default": 10, "min": 2, "max": 20, "step": 1}
                            for name in names
                        },
                        "constraints": [],
                    },
                    "source": (
                        "def strategy(history, state):\n"
                        "    return {ETF_SYMBOLS[sum(parameters.values()) % 5]: 1.0}\n"
                    ),
                },
                usage={"total_tokens": 0, "estimated_cost_usd": 0, "requests": 0},
            )

    bars = tmp_path / "bars.csv"
    database = tmp_path / "research.sqlite"
    _write_bars(bars, day_count=40)
    started = start_campaign(
        database=database, dataset=load_bars(bars, "total_return"), data_path=bars,
        data_kind="synthetic", provider=SixParameterProvider(1), seed=1,
        experiment_budget=1, max_consecutive_failures=1, max_seconds=60,
        initial_cash=10_000, transaction_cost_bps=0, slippage_bps=0,
        token_budget=5000, cost_budget_usd=1, steps=None,
        holdout_start="2025-01-01", holdout_end="2025-01-31",
        trusted_local_python=True,
    )
    experiment = campaign_status(database, started["campaign_id"])["experiments"][0]
    plan = freeze_candidate(
        database, started["campaign_id"], experiment["experiment_id"], "candidate", "Frozen",
    )["robustness_plan"]
    assert len(plan["variants"]) == 10
    assert {name for item in plan["variants"] for name in item["changed_parameters"]} == {
        f"p{i}" for i in range(6)
    }


def test_mock_generated_revision_uses_recorded_feedback() -> None:
    provider = MockProvider(seed=0)
    original = provider.propose(1, {"trusted_local_python": True})
    kept = provider.propose(
        2, {
            "trusted_local_python": True,
            "experiments": [{"strategy": original.strategy, "decision": "keep"}],
        },
    )
    rejected = provider.propose(
        2, {
            "trusted_local_python": True,
            "experiments": [{"strategy": original.strategy, "decision": "reject"}],
        },
    )

    assert "scores = []" in original.strategy["source"]
    assert "scores = []" in kept.strategy["source"]
    assert kept.strategy["parameters"]["lookback"] != original.strategy["parameters"]["lookback"]
    assert "scores = []" not in rejected.strategy["source"]
    assert provider.propose(
        2, {
            "trusted_local_python": True,
            "experiments": [{"strategy": original.strategy, "decision": "keep"}],
        },
    ) == kept
    stopped = provider.reflect(
        original,
        {"status": "succeeded", "metrics": {"total_return": -0.1}},
        {
            "baseline": {"total_return": 0.1},
            "experiments": [
                {
                    "status": "succeeded",
                    "metrics": {"total_return": -0.1},
                    "decision": "reject",
                    "strategy": original.strategy,
                }
                for _ in range(3)
            ],
        },
    )
    assert stopped.decision == "stop"


def test_mock_generated_code_is_persisted_and_can_be_evaluated_only_with_opt_in(
    tmp_path: Path,
) -> None:
    bars = tmp_path / "bars.csv"
    holdout = tmp_path / "holdout.csv"
    database = tmp_path / "research.sqlite"
    _write_bars(bars, day_count=18)
    _write_bars(holdout, day_count=6, start_day=18)
    started = _run_cli(
        "research", "run", "--db", str(database), "--data", str(bars),
        "--price-basis", "total_return", "--data-kind", "synthetic",
        "--provider", "mock", "--seed", "11", "--experiments", "2",
        "--trusted-local-generated-python",
        "--holdout-start", "2024-01-19", "--holdout-end", "2024-01-24",
    )
    assert started.returncode == 0, started.stderr
    campaign_id = json.loads(started.stdout)["campaign_id"]
    experiments = campaign_status(database, campaign_id)["experiments"]
    assert len(experiments) == 2
    assert all(item["status"] == "succeeded" for item in experiments)
    assert all(item["strategy"]["family"] == "python" for item in experiments)
    assert len({item["strategy_hash"] for item in experiments}) == 2

    frozen = _run_cli(
        "research", "freeze", "--db", str(database), "--campaign-id", campaign_id,
        "--experiment-id", experiments[0]["experiment_id"], "--name", "candidate",
        "--conclusion", "Frozen before validation.",
    )
    assert frozen.returncode == 0, frozen.stderr
    frozen_result = json.loads(frozen.stdout)
    candidate_id = frozen_result["candidate_id"]
    assert frozen_result["candidate_hash"]
    args = [
        "research", "validate", "--db", str(database), "--candidate-id", candidate_id,
        "--data", str(bars), "--price-basis", "total_return", "--data-kind", "synthetic",
    ]
    denied = _run_cli(*args)
    assert denied.returncode == 2
    assert "trusted-local" in denied.stderr
    allowed = _run_cli(*args, "--trusted-local-generated-python")
    assert allowed.returncode == 0, allowed.stderr
    robustness = json.loads(allowed.stdout)["result"]["parameter_robustness"]
    assert robustness["plan"]["parameters"] == ["lookback"]
    assert robustness["status"] in {"passed", "failed", "inconclusive"}
    holdout_args = [
        "research", "holdout", "--db", str(database), "--candidate-id", candidate_id,
        "--data", str(holdout), "--price-basis", "total_return", "--data-kind", "synthetic",
        "--confirmation", "I CONFIRM THE CANDIDATE AND CONCLUSIONS ARE FROZEN",
    ]
    denied_holdout = _run_cli(*holdout_args)
    assert denied_holdout.returncode == 2
    assert "trusted-local" in denied_holdout.stderr
    assert SQLiteStore(database).get_holdout_receipt(campaign_id) is None
    allowed_holdout = _run_cli(*holdout_args, "--trusted-local-generated-python")
    assert allowed_holdout.returncode == 0, allowed_holdout.stderr
    report_path = tmp_path / "report.html"
    rendered = _run_cli(
        "research", "report", "--db", str(database), "--campaign-id", campaign_id,
        "--output", str(report_path),
    )
    assert rendered.returncode == 0, rendered.stderr
    report = report_path.read_text(encoding="utf-8")
    assert "not sandboxed" in report
    assert "No generated Python is executed" not in report
    assert experiments[0]["strategy_hash"] in report
    assert html.escape(json.dumps(experiments[0]["strategy"]["source"])) in report
    assert html.escape(experiments[0]["reflection"]) in report
    assert "provider_usage" in report


def test_frozen_candidate_validation_holdout_gate_and_report(
    tmp_path: Path,
) -> None:
    bars = tmp_path / "bars.csv"
    holdout_bars = tmp_path / "holdout.csv"
    database = tmp_path / "research.sqlite"
    report_path = tmp_path / "report.html"
    _write_bars(bars, day_count=18)
    _write_bars(holdout_bars, day_count=6, start_day=18)

    started = _run_cli(
        "research",
        "run",
        "--db",
        str(database),
        "--data",
        str(bars),
        "--price-basis",
        "total_return",
        "--data-kind",
        "synthetic",
        "--provider",
        "mock",
        "--seed",
        "11",
        "--experiments",
        "1",
        "--steps",
        "1",
        "--holdout-start",
        "2024-01-19",
        "--holdout-end",
        "2024-01-24",
    )
    assert started.returncode == 0, started.stderr
    campaign_id = json.loads(started.stdout)["campaign_id"]
    status = _run_cli(
        "research", "status", "--db", str(database), "--campaign-id", campaign_id
    )
    experiment_id = json.loads(status.stdout)["experiments"][0]["experiment_id"]
    frozen = _run_cli(
        "research",
        "freeze",
        "--db",
        str(database),
        "--campaign-id",
        campaign_id,
        "--experiment-id",
        experiment_id,
        "--name",
        "Candidate A",
        "--conclusion",
        "Freeze this strategy before independent validation.",
    )
    assert frozen.returncode == 0, frozen.stderr
    frozen_result = json.loads(frozen.stdout)
    candidate_id = frozen_result["candidate_id"]
    assert frozen_result["candidate_hash"]

    altered_bars = tmp_path / "altered.csv"
    altered_bars.write_text(
        bars.read_text(encoding="utf-8").replace(
            ",1000,total_return\n", ",1001,total_return\n", 1
        ),
        encoding="utf-8",
    )
    for data, kind in ((bars, "real"), (altered_bars, "synthetic")):
        rejected = _run_cli(
            "research",
            "validate",
            "--db",
            str(database),
            "--candidate-id",
            candidate_id,
            "--data",
            str(data),
            "--data-kind",
            kind,
            "--price-basis",
            "total_return",
        )
        assert rejected.returncode == 2, rejected.stdout
        assert "campaign" in rejected.stderr.lower()

    validation = _run_cli(
        "research",
        "validate",
        "--db",
        str(database),
        "--candidate-id",
        candidate_id,
        "--data",
        str(bars),
        "--data-kind",
        "synthetic",
        "--price-basis",
        "total_return",
    )
    assert validation.returncode == 0, validation.stderr
    validation_result = json.loads(validation.stdout)
    assert validation_result["kind"] == "validation"
    evidence = validation_result["result"]
    assert validation_result["data_hash"] == evidence["data_hash"]
    assert validation_result["data_hash"] == evidence["candidate"]["data_hash"]
    assert validation_result["data_hash"] != evidence["source_data_hash"]
    assert "benchmark_excess_return" in evidence
    assert evidence["candidate"]["metrics"]["max_drawdown"] is not None
    assert evidence["cost_sensitivity"]["metrics"]["total_return"] is not None
    assert evidence["parameter_robustness"]["variants"]
    assert evidence["market_period_breakdown"]

    wrong_confirmation = _run_cli(
        "research",
        "holdout",
        "--db",
        str(database),
        "--candidate-id",
        candidate_id,
        "--data",
        str(tmp_path / "not-read.csv"),
        "--data-kind",
        "synthetic",
        "--price-basis",
        "total_return",
        "--confirmation",
        "not frozen",
    )
    assert wrong_confirmation.returncode == 2
    assert "confirmation" in wrong_confirmation.stderr.lower()
    assert "not-read.csv" not in wrong_confirmation.stderr

    confirmation = "I CONFIRM THE CANDIDATE AND CONCLUSIONS ARE FROZEN"
    for data, kind in ((holdout_bars, "real"), (altered_bars, "synthetic")):
        rejected = _run_cli(
            "research",
            "holdout",
            "--db",
            str(database),
            "--candidate-id",
            candidate_id,
            "--data",
            str(data),
            "--data-kind",
            kind,
            "--price-basis",
            "total_return",
            "--confirmation",
            confirmation,
        )
        assert rejected.returncode == 2, rejected.stdout
        assert "campaign" in rejected.stderr.lower() or "interval" in rejected.stderr.lower()

    holdout = _run_cli(
        "research",
        "holdout",
        "--db",
        str(database),
        "--candidate-id",
        candidate_id,
        "--data",
        str(holdout_bars),
        "--data-kind",
        "synthetic",
        "--price-basis",
        "total_return",
        "--confirmation",
        confirmation,
    )
    assert holdout.returncode == 0, holdout.stderr
    holdout_response = json.loads(holdout.stdout)
    assert holdout_response["kind"] == "holdout"
    holdout_result = holdout_response["result"]
    assert holdout_response["data_hash"] == holdout_result["data_hash"]
    assert holdout_response["data_hash"] == holdout_result["candidate"]["data_hash"]
    assert holdout_response["data_hash"] == holdout_result["source_data_hash"]
    assert holdout_result["unlocked_at"]

    repeated = _run_cli(
        "research",
        "holdout",
        "--db",
        str(database),
        "--candidate-id",
        candidate_id,
        "--data",
        str(holdout_bars),
        "--data-kind",
        "synthetic",
        "--price-basis",
        "total_return",
        "--confirmation",
        confirmation,
    )
    assert repeated.returncode == 2
    assert "already consumed" in repeated.stderr.lower()

    second_candidate = _run_cli(
        "research",
        "freeze",
        "--db",
        str(database),
        "--campaign-id",
        campaign_id,
        "--experiment-id",
        experiment_id,
        "--name",
        "Candidate B",
        "--conclusion",
        "A second frozen strategy cannot reuse the final holdout.",
    )
    second_candidate_id = json.loads(second_candidate.stdout)["candidate_id"]
    second_candidate_holdout = _run_cli(
        "research",
        "holdout",
        "--db",
        str(database),
        "--candidate-id",
        second_candidate_id,
        "--data",
        str(holdout_bars),
        "--data-kind",
        "synthetic",
        "--price-basis",
        "total_return",
        "--confirmation",
        confirmation,
    )
    assert second_candidate_holdout.returncode == 2
    assert "already consumed" in second_candidate_holdout.stderr.lower()

    for name in ("Candidate C", "Candidate D"):
        extra = _run_cli(
            "research", "freeze", "--db", str(database),
            "--campaign-id", campaign_id, "--experiment-id", experiment_id,
            "--name", name, "--conclusion", "Frozen before validation.",
        )
        if name == "Candidate C":
            assert extra.returncode == 0, extra.stderr
        else:
            assert extra.returncode == 2
            assert "limit" in extra.stderr.lower()
            assert "3" in extra.stderr

    bars.unlink()
    report = _run_cli(
        "research",
        "report",
        "--db",
        str(database),
        "--campaign-id",
        campaign_id,
        "--output",
        str(report_path),
    )
    assert report.returncode == 0, report.stderr
    html = report_path.read_text(encoding="utf-8")
    assert "synthetic" in html
    assert "Objective" in html
    assert f"<dt>Data source path</dt><dd>{bars.resolve()}</dd>" in html
    assert "<dt>Data date range</dt><dd>2024-01-01 — 2024-01-18</dd>" in html
    assert "<dt>Data rows</dt><dd>90</dd>" in html
    assert "Candidate A" in html
    assert frozen_result["candidate_hash"] in html
    assert "<dt>Total research attempts</dt><dd>1</dd>" in html
    assert "Frozen data split" in html
    assert "Market-period breakdown" in html
    assert "<td>2024</td>" in html
    assert "Cost sensitivity" in html
    assert "Holdout" in html
    assert holdout_result["unlocked_at"] in html
    assert "drawdown" in html.lower()
    repeated_report = _run_cli(
        "research", "report", "--db", str(database),
        "--campaign-id", campaign_id, "--output", str(report_path),
    )
    assert repeated_report.returncode == 0, repeated_report.stderr
    assert report_path.read_text(encoding="utf-8") == html
