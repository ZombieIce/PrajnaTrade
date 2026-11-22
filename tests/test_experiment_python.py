from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pytest
from cli_helpers import cli
from test_experiment_cli import campaign_options, sealed_snapshot_input

from prajnatrade import experiment
from prajnatrade.container_executor import (
    ContainerLimits,
    ContainerStrategyExecutor,
    ExecutionResult,
    executor_from_config,
)
from prajnatrade.data import CASH
from prajnatrade.experiment import (
    ProtocolError,
    campaign_create,
    experiment_resume,
    experiment_status,
    experiment_submit,
    project_init,
)
from prajnatrade.isolation import IsolationError
from prajnatrade.snapshot import seal

SOURCE = "def strategy(history, state):\n    return {CASH: 1.0}\n"
ALT_SOURCE = "def strategy(history, state):\n    return {'511260': 1.0}\n"


def _fake_evidence(index: int) -> dict[str, object]:
    return {
        "run_id": f"fake-run-{index}",
        "image_reference": "fake/issue27:test",
        "image_digest": "sha256:" + "a" * 64,
        "container_id": f"{index:064x}",
        "inspect": {"mount_count": 0, "network_mode": "none"},
        "isolation_checks": {"no_mounts": True, "network_none": True},
        "input_sha256": "0" * 64,
        "input_bytes": 128,
        "output_sha256": "1" * 64,
        "output_bytes": 64,
        "wall_seconds": 0.1,
        "exit_code": 0,
        "source_started": True,
        "strategy_started": True,
        "result": "weights",
        "weights_valid": True,
    }


@dataclass
class FakeExecutor:
    weights: dict[str, float] | None = None
    failure: BaseException | None = None
    calls: list[dict[str, object]] = field(default_factory=list)
    runs: int = 0

    def resolve_configuration(self) -> dict[str, object]:
        return {
            "kind": "container",
            "image": "fake/issue27:test",
            "digest": "sha256:" + "a" * 64,
            "limits": asdict(ContainerLimits()),
        }

    def run(self, source, parameters, history, state):  # noqa: ANN001, ANN201
        self.runs += 1
        self.calls.append(
            {
                "source": source,
                "parameters": dict(parameters),
                "rows": len(history),
                "state": dict(state),
            }
        )
        if self.failure is not None:
            raise self.failure
        weights = self.weights if self.weights is not None else {CASH: 1.0}
        return ExecutionResult(weights=weights, evidence=_fake_evidence(self.runs))


class InterruptingExecutor(FakeExecutor):
    def run(self, source, parameters, history, state):  # noqa: ANN001, ANN201
        self.runs += 1
        if self.runs == 1:
            raise KeyboardInterrupt
        self.calls.append({"source": source, "parameters": dict(parameters)})
        return ExecutionResult(weights={CASH: 1.0}, evidence=_fake_evidence(self.runs))


def _install(monkeypatch: pytest.MonkeyPatch, executor: FakeExecutor) -> None:
    monkeypatch.setattr(experiment, "executor_from_config", lambda config: executor)


def _sealed_project(tmp_path: Path) -> tuple[Path, Path, str]:
    project = tmp_path / "project"
    source = tmp_path / "snapshot.json"
    source.write_text(json.dumps(sealed_snapshot_input()), encoding="utf-8")
    store = tmp_path / "store"
    snapshot = seal(source, store)
    project_init(project)
    return project, store, snapshot["snapshot_id"]


def _campaign(
    tmp_path: Path,
    *,
    executor: FakeExecutor | None = None,
    attempts: int = 2,
    max_seconds: float = 30.0,
    container: bool = True,
) -> tuple[Path, dict]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    project, store, snapshot_id = _sealed_project(tmp_path)
    kwargs = {}
    if container:
        kwargs = {
            "strategy_executor": "container",
            "strategy_image": "fake/issue27:test",
            "executor": executor if executor is not None else FakeExecutor(),
        }
    campaign = campaign_create(
        project, store, snapshot_id, attempts, max_seconds,
        100_000.0, 5.0, 0.0, **kwargs,
    )
    return project, campaign


def _request(campaign: dict, submission_id: str = "first", **overrides: object) -> dict:
    request = {
        "schema_version": 1,
        "campaign_id": campaign["campaign_id"],
        "submission_id": submission_id,
        "hypothesis": "Cash is a valid generated strategy output.",
        "strategy": {"family": "python", "source": SOURCE},
        "parameters": {},
    }
    request.update(overrides)
    return request


def test_python_submission_is_canonicalized_hashed_and_executed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = FakeExecutor()
    _install(monkeypatch, executor)
    project, campaign = _campaign(tmp_path, executor=executor)
    assert campaign["strategy_executor"]["kind"] == "container"
    assert campaign["strategy_executor"]["digest"] == "sha256:" + "a" * 64

    receipt = experiment_submit(project, _request(campaign))

    assert receipt["status"] == "succeeded"
    assert receipt["strategy"]["family"] == "python"
    assert receipt["strategy"]["source"] == SOURCE
    assert receipt["strategy"]["source_sha256"] == hashlib.sha256(SOURCE.encode()).hexdigest()
    assert receipt["parameters"] == {}
    assert receipt["budget"]["attempts_remaining"] == 1
    assert len(executor.calls) > 1
    assert all(call["parameters"] == {} for call in executor.calls)
    assert all(call["rows"] > 0 for call in executor.calls)
    evidence = receipt["executor"]
    assert evidence["kind"] == "container"
    assert evidence["image"] == {
        "reference": "fake/issue27:test", "digest": "sha256:" + "a" * 64,
    }
    assert evidence["limits"] == campaign["strategy_executor"]["limits"]
    assert evidence["invocation_count"] == len(evidence["invocations"]) == len(executor.calls)
    assert evidence["evidence_sha256"]
    first = evidence["invocations"][0]
    assert first["run_id"] == "fake-run-1"
    assert first["container_id"]
    assert first["source_started"] is True
    assert first["strategy_started"] is True
    assert first["input_sha256"] and first["output_sha256"]
    assert receipt["discovery"]["metrics"]["fills"] == 0
    assert receipt["fingerprints"]["strategy_and_parameters"]

    replay = experiment_submit(project, _request(campaign))
    assert replay == receipt
    assert experiment_status(
        project, campaign["campaign_id"], "first",
    ) == receipt


def test_python_submission_requires_a_container_campaign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, FakeExecutor())
    project, campaign = _campaign(tmp_path, container=False)
    assert campaign["strategy_executor"] == {"kind": "none"}

    with pytest.raises(ProtocolError) as rejected:
        experiment_submit(project, _request(campaign))
    assert rejected.value.details["code"] == "INVALID_SUBMISSION"

    declarative = _request(
        campaign,
        submission_id="declarative",
        strategy={"family": "equal_weight"},
    )
    accepted = experiment_submit(project, declarative)
    assert accepted["status"] == "succeeded"
    assert accepted["budget"]["attempts_remaining"] == 1


def test_python_replay_conflict_parent_and_budget_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, FakeExecutor())
    project, campaign = _campaign(tmp_path, attempts=2)

    first = experiment_submit(project, _request(campaign))
    assert first["status"] == "succeeded"
    assert experiment_submit(project, _request(campaign)) == first

    with pytest.raises(ProtocolError) as conflict:
        experiment_submit(
            project,
            _request(campaign, strategy={"family": "python", "source": ALT_SOURCE}),
        )
    assert conflict.value.details["code"] == "SUBMISSION_CONFLICT"
    assert experiment_status(
        project, campaign["campaign_id"], "first",
    )["budget"] == first["budget"]

    child = experiment_submit(
        project,
        _request(
            campaign, submission_id="child",
            parent_experiment_id=first["experiment_id"],
        ),
    )
    assert child["status"] == "succeeded"
    assert child["parent_experiment_id"] == first["experiment_id"]
    assert child["experiment_id"] != first["experiment_id"]
    assert child["budget"]["attempts_remaining"] == 0

    with pytest.raises(ProtocolError) as exhausted:
        experiment_submit(project, _request(campaign, submission_id="third"))
    assert exhausted.value.details["code"] == "BUDGET_EXHAUSTED"
    assert experiment_status(
        project, campaign["campaign_id"], "child",
    )["budget"]["attempts_remaining"] == 0


def test_python_strategy_and_parameters_fingerprint_tracks_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_executor = FakeExecutor()
    _install(monkeypatch, first_executor)
    first_project, first_campaign = _campaign(tmp_path / "first", executor=first_executor)
    first = experiment_submit(first_project, _request(first_campaign))

    _install(monkeypatch, FakeExecutor())
    second_project, second_campaign = _campaign(tmp_path / "second", executor=FakeExecutor())
    second = experiment_submit(second_project, _request(second_campaign))

    _install(monkeypatch, FakeExecutor())
    third_project, third_campaign = _campaign(tmp_path / "third", executor=FakeExecutor())
    third = experiment_submit(
        third_project,
        _request(third_campaign, strategy={"family": "python", "source": ALT_SOURCE}),
    )

    assert (
        first["fingerprints"]["strategy_and_parameters"]
        == second["fingerprints"]["strategy_and_parameters"]
    )
    assert (
        first["fingerprints"]["strategy_and_parameters"]
        != third["fingerprints"]["strategy_and_parameters"]
    )
    assert (
        first["fingerprints"]["evaluation"] == second["fingerprints"]["evaluation"]
    )


def test_python_source_and_parameter_schema_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, FakeExecutor())
    project, campaign = _campaign(tmp_path, attempts=4)
    schema = {
        "parameters": {
            "lookback": {
                "type": "integer", "default": 3, "min": 2, "max": 252, "step": 1,
            }
        },
        "constraints": [],
    }
    with_schema = _request(
        campaign,
        submission_id="schema",
        strategy={
            "family": "python", "source": SOURCE, "parameter_schema": schema,
        },
        parameters={"lookback": 3},
    )
    first = experiment_submit(project, with_schema)
    assert first["status"] == "succeeded"
    assert first["strategy"]["parameter_schema"] == schema
    assert first["strategy"]["source_sha256"] == hashlib.sha256(SOURCE.encode()).hexdigest()

    without_schema = _request(
        campaign, submission_id="no-schema", parameters={"lookback": 3},
    )
    second = experiment_submit(project, without_schema)
    assert second["status"] == "succeeded"
    assert "parameter_schema" not in second["strategy"]
    assert (
        first["fingerprints"]["strategy_and_parameters"]
        != second["fingerprints"]["strategy_and_parameters"]
    )

    invalid_requests = [
        _request(campaign, submission_id="syntax", strategy={
            "family": "python", "source": "def strategy(history, state):\n    return (\n",
        }),
        _request(campaign, submission_id="long", strategy={
            "family": "python", "source": "x = 1\n" * 4000,
        }),
        _request(campaign, submission_id="top-level-source", strategy={
            "family": "python", "source": SOURCE, "parameters": {},
        }),
        _request(campaign, submission_id="bad-schema", strategy={
            "family": "python", "source": SOURCE, "parameter_schema": {"parameters": {}},
        }),
    ]
    for invalid in invalid_requests:
        with pytest.raises(ProtocolError) as rejected:
            experiment_submit(project, invalid)
        assert rejected.value.details["code"] == "INVALID_SUBMISSION"

    assert experiment_status(
        project, campaign["campaign_id"], "schema",
    )["budget"]["attempts_remaining"] == 2


def test_python_failure_is_charged_and_keeps_executor_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    failing = FakeExecutor(
        failure=IsolationError("container exploded", evidence=_fake_evidence(1)),
    )
    _install(monkeypatch, failing)
    project, campaign = _campaign(tmp_path, executor=failing, attempts=2)

    receipt = experiment_submit(project, _request(campaign))

    assert receipt["status"] == "failed"
    assert receipt["error"]["code"] == "EXECUTION_FAILED"
    assert "container exploded" in receipt["error"]["message"]
    assert receipt["budget"]["attempts_remaining"] == 1
    assert receipt["error"]["executor"]["kind"] == "container"
    assert receipt["error"]["executor"]["invocation_count"] == 1
    assert receipt["error"]["executor"]["invocations"][0]["run_id"] == "fake-run-1"
    assert "result" not in receipt


def test_python_interruption_resumes_the_same_charged_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = InterruptingExecutor()
    _install(monkeypatch, executor)
    project, campaign = _campaign(tmp_path, executor=executor, attempts=1)

    with pytest.raises(KeyboardInterrupt):
        experiment_submit(project, _request(campaign))

    interrupted = experiment_status(project, campaign["campaign_id"], "first")
    assert interrupted["status"] == "failed"
    assert interrupted["error"]["code"] == "EXECUTION_INTERRUPTED"
    assert interrupted["error"]["next_action"] == "resume"
    assert interrupted["budget"]["attempts_remaining"] == 0

    resumed = experiment_resume(project, campaign["campaign_id"], "first")

    assert resumed["status"] == "succeeded"
    assert resumed["experiment_id"] == interrupted["experiment_id"]
    assert resumed["budget"]["attempts_remaining"] == 0
    assert resumed["executor"]["invocation_count"] == len(executor.calls)
    assert experiment_resume(project, campaign["campaign_id"], "first") == resumed


def test_python_and_campaign_validation_error_paths(tmp_path: Path) -> None:
    project, store, snapshot_id = _sealed_project(tmp_path)

    with pytest.raises(ValueError, match="strategy-image"):
        campaign_create(
            project, store, snapshot_id, 1, 30.0, 100_000.0, 5.0, 0.0,
            strategy_executor="container",
        )
    with pytest.raises(ValueError, match="strategy-image"):
        campaign_create(
            project, store, snapshot_id, 1, 30.0, 100_000.0, 5.0, 0.0,
            strategy_image="python:3.12-bookworm",
        )
    with pytest.raises(IsolationError, match="Docker CLI"):
        ContainerStrategyExecutor(
            "python:3.12-bookworm", docker_binary="prajnatrade-missing-docker",
        ).resolve_image()
    with pytest.raises(ValueError, match="digest"):
        executor_from_config(
            {
                "kind": "container", "image": "x", "digest": "bad",
                "limits": asdict(ContainerLimits()),
            }
        )


def _cli_python_request(campaign: dict, source: str = SOURCE) -> dict:
    return _request(campaign, strategy={"family": "python", "source": source})


def test_cli_requires_strategy_image_and_rejects_python_without_container(
    tmp_path: Path,
) -> None:
    project = tmp_path / "strategy-a"
    source = tmp_path / "discovery.json"
    source.write_text(json.dumps(sealed_snapshot_input()), encoding="utf-8")
    assert cli("project", "init", "--project", str(project)).returncode == 0
    options = campaign_options(project, source)

    missing_image = cli(*options, "--strategy-executor", "container")
    assert missing_image.returncode == 2
    assert "strategy-image" in missing_image.stderr

    image_without_container = cli(*options, "--strategy-image", "python:3.12-bookworm")
    assert image_without_container.returncode == 2
    assert "strategy-image" in image_without_container.stderr

    invalid = cli(*options, "--strategy-executor", "docker")
    assert invalid.returncode == 2

    created = cli(*options)
    assert created.returncode == 0, created.stderr
    campaign = json.loads(created.stdout)
    assert campaign["strategy_executor"] == {"kind": "none"}
    request = tmp_path / "python.json"
    request.write_text(json.dumps(_cli_python_request(campaign)), encoding="utf-8")
    rejected = cli(
        "experiment", "submit", "--project", str(project), "--input", str(request),
    )
    assert rejected.returncode == 2
    payload = json.loads(rejected.stdout)
    assert payload["status"] == "rejected"
    assert payload["error"]["code"] == "INVALID_SUBMISSION"
    assert payload["budget"]["attempts_remaining"] == 2
