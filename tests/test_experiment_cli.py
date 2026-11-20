import json
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path


def cli_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        (str(Path(__file__).parents[1] / "src"), env.get("PYTHONPATH", ""))
    )
    return env


AGENT_CLI = (
    sys.executable, "-c", "from prajnatrade.cli import agent_main; raise SystemExit(agent_main())",
)


def cli(*args: str, agent: bool = False) -> subprocess.CompletedProcess[str]:
    command = AGENT_CLI if agent else (sys.executable, "-m", "prajnatrade")
    return subprocess.run(
        [*command, *args], capture_output=True, text=True, env=cli_env(), timeout=45,
    )


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


def bars(path: Path, day_count: int = 12) -> None:
    lines = ["date,symbol,open,high,low,close,volume,price_basis"]
    for day in range(day_count):
        for index, symbol in enumerate(("511260", "518880", "515080", "513300", "159612")):
            price = 100 + (index + 1) * day
            lines.append(
                f"{date(2024, 1, 1) + timedelta(days=day)},{symbol},"
                f"{price},{price},{price},{price},1000,total_return"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_operator_creates_locked_synthetic_campaign(tmp_path: Path) -> None:
    project = tmp_path / "strategy-a"
    source = tmp_path / "discovery.csv"
    bars(source)
    assert cli("project", "init", "--project", str(project)).returncode == 0
    result = cli(
        "campaign", "create", "--project", str(project), "--data", str(source), "--data-kind", "synthetic",
        "--attempts", "2", "--max-seconds", "30",
    )
    assert result.returncode == 0, result.stderr
    campaign = json.loads(result.stdout)
    assert campaign["schema_version"] == 1
    assert campaign["campaign_id"]
    assert campaign["fingerprints"]["snapshot"]
    assert campaign["fingerprints"]["evaluation"]
    assert campaign["budget"]["attempts_remaining"] == 2


def test_submit_and_status_report_same_discovery_experiment(tmp_path: Path) -> None:
    project = tmp_path / "strategy-a"
    source = tmp_path / "discovery.csv"
    bars(source)
    project_id = json.loads(cli("project", "init", "--project", str(project)).stdout)["project_id"]
    created = cli(
        "campaign", "create", "--project", str(project), "--data", str(source), "--data-kind", "synthetic",
        "--attempts", "2", "--max-seconds", "30",
    )
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


def test_concurrent_replay_conflict_and_invalid_submissions_do_not_double_charge(tmp_path: Path) -> None:
    project = tmp_path / "strategy-a"
    source = tmp_path / "discovery.csv"
    bars(source)
    assert cli("project", "init", "--project", str(project)).returncode == 0
    campaign = json.loads(cli(
        "campaign", "create", "--project", str(project), "--data", str(source), "--data-kind", "synthetic",
        "--attempts", "1", "--max-seconds", "30",
    ).stdout)
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
    source = tmp_path / "discovery.csv"
    bars(source)
    assert cli("project", "init", "--project", str(project)).returncode == 0
    campaign = json.loads(cli(
        "campaign", "create", "--project", str(project), "--data", str(source), "--data-kind", "synthetic",
        "--attempts", "2", "--max-seconds", "0.000001",
    ).stdout)
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
        [*AGENT_CLI, *submit_args], stdout=subprocess.PIPE,
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
    source = tmp_path / "discovery.csv"
    bars(source, day_count=600)
    identity = json.loads(cli("project", "init", "--project", str(project)).stdout)
    campaign_args = (
        "campaign", "create", "--project", str(project), "--data", str(source), "--data-kind", "synthetic",
        "--attempts", "1", "--max-seconds", "30",
    )
    campaign = json.loads(cli(*campaign_args).stdout)
    control_campaign = json.loads(cli(*campaign_args).stdout)
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
    source = tmp_path / "discovery.csv"
    bars(source, day_count=600)
    assert cli("project", "init", "--project", str(project)).returncode == 0
    campaign = json.loads(cli(
        "campaign", "create", "--project", str(project), "--data", str(source), "--data-kind", "synthetic",
        "--attempts", "3", "--max-seconds", "8",
    ).stdout)
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
    source = tmp_path / "discovery.csv"
    bars(source)
    init = ("project", "init", "--project", str(project))
    assert cli(*init, agent=True).returncode == 2
    assert not project.exists()
    identity = json.loads(cli(*init).stdout)
    assert json.loads(cli("project", "status", "--project", str(project), agent=True).stdout) == identity
    for action in ("create", "reset", "increase-budget"):
        assert cli("campaign", action, "--project", str(project), "--data", str(source),
                   "--data-kind", "synthetic", "--attempts", "2", "--max-seconds", "30",
                   agent=True).returncode == 2


def test_parent_and_campaign_configuration_are_checked_before_admission(tmp_path: Path) -> None:
    project = tmp_path / "strategy-a"
    source = tmp_path / "discovery.csv"
    bars(source)
    assert cli("project", "init", "--project", str(project)).returncode == 0
    campaign_args = (
        "campaign", "create", "--project", str(project), "--data", str(source), "--data-kind", "synthetic",
        "--attempts", "2", "--max-seconds", "30",
    )
    first_campaign = json.loads(cli(*campaign_args).stdout)
    other_campaign = json.loads(cli(*campaign_args).stdout)
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
