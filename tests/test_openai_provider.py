"""OpenAI-compatible provider tests (fake local HTTP server, synthetic data).

The generated-Python variant of
``test_campaign_records_provider_usage_without_persisting_api_credentials`` is
marked ``isolation`` because it executes the generated strategy through the
real macOS sandbox; the equal-weight variant stays in the fast suite.
"""

from __future__ import annotations

import json
import threading
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import prajnatrade.isolation as isolation
from prajnatrade.cli import main
from prajnatrade.data import ETF_SYMBOLS, load_bars
from prajnatrade.providers import (
    OpenAICompatibleProvider,
    ProviderBudgetExceeded,
    ProviderError,
    parse_structured_response,
)
from prajnatrade.research import campaign_status, resume_campaign, start_campaign
from prajnatrade.store import SQLiteStore
from prajnatrade.validation import freeze_candidate


@pytest.fixture(autouse=True)
def use_test_only_memory_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        isolation,
        "DEFAULT_ISOLATION_LIMITS",
        isolation.IsolationLimits(address_space_bytes=1_000_000_000_000_000),
    )


class _ResponseHandler(BaseHTTPRequestHandler):
    responses: list[dict[str, object]] = []

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.requests.append(
            {
                "authorization": self.headers.get("Authorization"),
                "payload": json.loads(body),
            }
        )
        response = self.responses.pop(0)
        encoded = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        return


def _start_server(
    responses: list[dict[str, object]],
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    _ResponseHandler.responses = list(responses)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ResponseHandler)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _chat_response(content: str) -> dict[str, object]:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }


def _best_effort_provider(**kwargs: object) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        **kwargs, budget_mode="best-effort", accept_budget_risk=True,
    )


def test_strict_budget_rejects_unverifiable_provider_before_call() -> None:
    server, thread = _start_server([])
    try:
        provider = OpenAICompatibleProvider(
            endpoint=f"http://127.0.0.1:{server.server_port}/v1",
            model="test-model",
            api_key="local-test-secret",
            input_cost_per_1k_tokens=0.1,
            output_cost_per_1k_tokens=0.2,
        )
        with pytest.raises(ProviderBudgetExceeded, match="upper bound"):
            provider.propose(
                1, {"limits": {"remaining_tokens": 2000, "remaining_cost_usd": 1.0}},
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert server.requests == []


def test_best_effort_requires_explicit_risk_acceptance() -> None:
    with pytest.raises(ValueError, match="explicit acceptance"):
        OpenAICompatibleProvider(
            endpoint="http://127.0.0.1/v1",
            model="test-model",
            api_key="local-test-secret",
            input_cost_per_1k_tokens=0.1,
            output_cost_per_1k_tokens=0.2,
            budget_mode="best-effort",
        )


def test_provider_does_not_forward_credentials_on_redirect() -> None:
    class Destination(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.server.received.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, format: str, *args: object) -> None:
            return

    destination = ThreadingHTTPServer(("127.0.0.1", 0), Destination)
    destination.received = []

    class Redirect(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{destination.server_port}/stolen")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (destination, redirect)
    ]
    for thread in threads:
        thread.start()
    try:
        provider = _best_effort_provider(
            endpoint=f"http://127.0.0.1:{redirect.server_port}/v1", model="test-model",
            api_key="local-test-secret", input_cost_per_1k_tokens=0.1,
            output_cost_per_1k_tokens=0.2,
        )
        with pytest.raises(ProviderError, match="302"):
            provider.propose(1, {"limits": {"remaining_tokens": 5000, "remaining_cost_usd": 1}})
        assert destination.received == []
    finally:
        for server in (redirect, destination):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)


def test_unverifiable_budget_pauses_campaign_without_losing_progress(tmp_path: Path) -> None:
    server, thread = _start_server([])
    bars_path = tmp_path / "bars.csv"
    database = tmp_path / "research.sqlite"
    lines = ["date,symbol,open,high,low,close,volume,price_basis"]
    for index in range(6):
        day = (date(2024, 1, 1) + timedelta(days=index)).isoformat()
        for symbol in ETF_SYMBOLS:
            lines.append(f"{day},{symbol},100,101,100,101,1000,total_return")
    bars_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        provider = OpenAICompatibleProvider(
            endpoint=f"http://127.0.0.1:{server.server_port}/v1",
            model="test-model", api_key="local-test-secret",
            input_cost_per_1k_tokens=0.1, output_cost_per_1k_tokens=0.2,
        )
        result = start_campaign(
            database=database, dataset=load_bars(bars_path, "total_return"),
            data_path=bars_path, data_kind="synthetic", provider=provider, seed=1,
            experiment_budget=2, max_consecutive_failures=1, max_seconds=60,
            initial_cash=10_000, transaction_cost_bps=0, slippage_bps=0,
            token_budget=5000, cost_budget_usd=1, steps=None,
            holdout_start="2025-01-01", holdout_end="2025-01-31",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert result["status"] == "paused"
    assert result["stop_reason"] == "cost_bound_unverifiable"
    assert SQLiteStore(database).get_campaign(result["campaign_id"])["status"] == "paused"
    assert server.requests == []


def test_interrupted_provider_request_keeps_reservation_and_blocks_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import prajnatrade.providers as providers_module

    proposal = json.dumps({
        "schema_version": "1", "hypothesis": "Control", "rationale": "Reference",
        "strategy": {"family": "equal_weight", "parameters": {}},
    })
    reflection = json.dumps({
        "schema_version": "1", "decision": "keep", "reflection": "Retain control.",
    })
    server, thread = _start_server([
        _chat_response(proposal), _chat_response(reflection), _chat_response(proposal),
    ])
    bars_path = tmp_path / "bars.csv"
    database = tmp_path / "research.sqlite"
    lines = ["date,symbol,open,high,low,close,volume,price_basis"]
    for index in range(6):
        day = (date(2024, 1, 1) + timedelta(days=index)).isoformat()
        for symbol in ETF_SYMBOLS:
            lines.append(f"{day},{symbol},100,101,100,101,1000,total_return")
    bars_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    provider = _best_effort_provider(
        endpoint=f"http://127.0.0.1:{server.server_port}/v1", model="test-model",
        api_key="local-test-secret", input_cost_per_1k_tokens=0.1,
        output_cost_per_1k_tokens=0.2,
    )
    try:
        started = start_campaign(
            database=database, dataset=load_bars(bars_path, "total_return"),
            data_path=bars_path, data_kind="synthetic", provider=provider, seed=1,
            experiment_budget=2, max_consecutive_failures=2, max_seconds=60,
            initial_cash=10_000, transaction_cost_bps=0, slippage_bps=0,
            token_budget=5000, cost_budget_usd=1, steps=1,
            holdout_start="2025-01-01", holdout_end="2025-01-31",
        )
        campaign_id = started["campaign_id"]
        urlopen = providers_module.urlopen

        def interrupted(request, *, timeout):
            with urlopen(request, timeout=timeout) as response:
                response.read()
            raise KeyboardInterrupt

        monkeypatch.setattr(providers_module, "urlopen", interrupted)
        with pytest.raises(KeyboardInterrupt):
            resume_campaign(database=database, campaign_id=campaign_id, provider=provider)
        status = campaign_status(database, campaign_id)
        assert status["provider_requests"][-1]["status"] == "reserved"
        assert status["provider_requests"][-1]["usage"]["estimated_cost_usd"] > 0
        paused = resume_campaign(database=database, campaign_id=campaign_id, provider=provider)
        assert paused["status"] == "paused"
        assert paused["stop_reason"] == "provider_request_unconfirmed"
        assert len(server.requests) == 3
        request_id = status["provider_requests"][-1]["request_id"]
        assert main([
            "research", "reconcile-request", "--db", str(database),
            "--campaign-id", campaign_id, "--request-id", request_id,
            "--prompt-tokens", "200", "--completion-tokens", "100",
            "--actual-cost-usd", "1.01", "--evidence", "Provider invoice",
        ]) == 0
        reconciled = campaign_status(database, campaign_id)["provider_requests"][-1]
        assert reconciled["status"] == "reconciled"
        assert reconciled["usage"]["actual_cost_usd"] == 1.01
        stopped = resume_campaign(database=database, campaign_id=campaign_id, provider=provider)
        assert stopped["stop_reason"] == "cost_budget"
        assert len(server.requests) == 3
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("actual_cost", [0.04, 1.01])
def test_reconciled_reflection_usage_stays_with_completed_experiment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, actual_cost: float,
) -> None:
    import prajnatrade.providers as providers_module

    proposal = json.dumps({
        "schema_version": "1", "hypothesis": "Control", "rationale": "Reference",
        "strategy": {"family": "equal_weight", "parameters": {}},
    })
    reflection = json.dumps({
        "schema_version": "1", "decision": "keep", "reflection": "Retain control.",
    })
    server, thread = _start_server([
        _chat_response(proposal), _chat_response(reflection), _chat_response(proposal),
        _chat_response(reflection), _chat_response(reflection),
    ])
    bars = tmp_path / "bars.csv"
    database = tmp_path / "research.sqlite"
    lines = ["date,symbol,open,high,low,close,volume,price_basis"]
    for index in range(6):
        day = (date(2024, 1, 1) + timedelta(days=index)).isoformat()
        for symbol in ETF_SYMBOLS:
            lines.append(f"{day},{symbol},100,101,100,101,1000,total_return")
    bars.write_text("\n".join(lines) + "\n", encoding="utf-8")
    provider = _best_effort_provider(
        endpoint=f"http://127.0.0.1:{server.server_port}/v1", model="test-model",
        api_key="local-test-secret", input_cost_per_1k_tokens=0.1,
        output_cost_per_1k_tokens=0.2,
    )
    original = providers_module.urlopen

    def interrupt_reflection(request, *, timeout):
        response = original(request, timeout=timeout)
        if len(server.requests) == 4:
            with response:
                response.read()
            raise KeyboardInterrupt
        return response

    try:
        started = start_campaign(
            database=database, dataset=load_bars(bars, "total_return"),
            data_path=bars, data_kind="synthetic", provider=provider, seed=1,
            experiment_budget=2, max_consecutive_failures=2, max_seconds=60,
            initial_cash=10_000, transaction_cost_bps=0, slippage_bps=0,
            token_budget=5000, cost_budget_usd=1, steps=1,
            holdout_start="2025-01-01", holdout_end="2025-01-31",
        )
        campaign_id = started["campaign_id"]
        monkeypatch.setattr(providers_module, "urlopen", interrupt_reflection)
        with pytest.raises(KeyboardInterrupt):
            resume_campaign(database=database, campaign_id=campaign_id, provider=provider)
        request = campaign_status(database, campaign_id)["provider_requests"][-1]
        assert main([
            "research", "reconcile-request", "--db", str(database),
            "--campaign-id", campaign_id, "--request-id", request["request_id"],
            "--prompt-tokens", "200", "--completion-tokens", "100",
            "--actual-cost-usd", str(actual_cost), "--evidence", "Provider invoice",
        ]) == 0
        monkeypatch.setattr(providers_module, "urlopen", original)
        resumed = resume_campaign(database=database, campaign_id=campaign_id, provider=provider)
        experiment = campaign_status(database, campaign_id)["experiments"][1]
        assert experiment["status"] == "succeeded"
        assert experiment["provider_usage"]["actual_cost_usd"] == actual_cost
        if actual_cost > 1:
            assert resumed["stop_reason"] == "cost_budget"
            assert experiment["provider_usage"]["total_tokens"] == 450
            assert experiment["provider_usage"]["requests"] == 2
            assert len(server.requests) == 4
            freeze_candidate(database, campaign_id, experiment["experiment_id"], "candidate", "Frozen")
        else:
            assert experiment["provider_usage"]["total_tokens"] == 600
            assert experiment["provider_usage"]["requests"] == 3
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_reconciled_cost_is_used_when_step_limited_campaign_pauses(tmp_path: Path) -> None:
    proposal = json.dumps({
        "schema_version": "1", "hypothesis": "Control", "rationale": "Reference",
        "strategy": {"family": "equal_weight", "parameters": {}},
    })
    reflection = json.dumps({
        "schema_version": "1", "decision": "keep", "reflection": "Retain control.",
    })
    replies = [_chat_response(proposal), _chat_response(reflection)] * 2
    for response in replies:
        response["usage"] = {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500}
    server, thread = _start_server(replies)
    bars = tmp_path / "bars.csv"
    database = tmp_path / "research.sqlite"
    lines = ["date,symbol,open,high,low,close,volume,price_basis"]
    for index in range(6):
        day = (date(2024, 1, 1) + timedelta(days=index)).isoformat()
        for symbol in ETF_SYMBOLS:
            lines.append(f"{day},{symbol},100,101,100,101,1000,total_return")
    bars.write_text("\n".join(lines) + "\n", encoding="utf-8")
    provider = _best_effort_provider(
        endpoint=f"http://127.0.0.1:{server.server_port}/v1", model="test-model",
        api_key="local-test-secret", input_cost_per_1k_tokens=0.1,
        output_cost_per_1k_tokens=0.2,
    )
    try:
        started = start_campaign(
            database=database, dataset=load_bars(bars, "total_return"),
            data_path=bars, data_kind="synthetic", provider=provider, seed=1,
            experiment_budget=3, max_consecutive_failures=2, max_seconds=60,
            initial_cash=10_000, transaction_cost_bps=0, slippage_bps=0,
            token_budget=10_000, cost_budget_usd=0.6, steps=1,
            holdout_start="2025-01-01", holdout_end="2025-01-31",
        )
        campaign_id = started["campaign_id"]
        for request in campaign_status(database, campaign_id)["provider_requests"]:
            assert main([
                "research", "reconcile-request", "--db", str(database),
                "--campaign-id", campaign_id, "--request-id", request["request_id"],
                "--prompt-tokens", "1000", "--completion-tokens", "500",
                "--actual-cost-usd", "0", "--evidence", "Provider credits",
            ]) == 0
        result = resume_campaign(database=database, campaign_id=campaign_id, provider=provider, steps=1)
        assert result["status"] == "paused"
        assert result["stop_reason"] == "step_limit"
        assert len(server.requests) == 4
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_openai_compatible_provider_validates_proposals_and_tracks_usage() -> None:
    proposal = {
        "schema_version": "1",
        "hypothesis": "Momentum will outperform the equal-weight baseline.",
        "rationale": "Compare trailing returns across the ETF pool.",
        "strategy": {
            "family": "momentum",
            "parameters": {"lookback": 5, "top_k": 2},
        },
    }
    reflection = {
        "schema_version": "1",
        "decision": "branch",
        "reflection": "Test a different lookback before deciding whether to keep it.",
    }
    server, thread = _start_server(
        [_chat_response(json.dumps(proposal)), _chat_response(json.dumps(reflection))]
    )
    try:
        provider = _best_effort_provider(
            endpoint=f"http://127.0.0.1:{server.server_port}/v1",
            model="test-model",
            api_key="local-test-secret",
            input_cost_per_1k_tokens=1.0,
            output_cost_per_1k_tokens=2.0,
        )
        context = {
            "baseline": {"total_return": 0.1},
            "experiments": [],
            "limits": {"remaining_tokens": 2000, "remaining_cost_usd": 1.0},
        }

        generated = provider.propose(1, context)
        evaluated = provider.reflect(
            generated,
            {"status": "succeeded", "metrics": {"total_return": 0.2}},
            context,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert generated.strategy["family"] == "momentum"
    assert generated.usage["total_tokens"] == 150
    assert generated.usage["estimated_cost_usd"] == 0.2
    assert evaluated.decision == "branch"
    assert len(server.requests) == 2
    assert all(
        request["authorization"] == "Bearer local-test-secret"
        for request in server.requests
    )
    assert all(
        "do not return Python" in request["payload"]["messages"][1]["content"]
        for request in server.requests[:1]
    )


def test_openai_provider_can_propose_python_when_trusted_local_enabled() -> None:
    proposal = {
        "schema_version": "1",
        "hypothesis": "Hold cash to avoid downside.",
        "rationale": "Compare a cash-only control to the benchmark.",
        "strategy": {
            "family": "python",
            "parameters": {},
            "parameter_schema": {
                "parameters": {}, "constraints": [], "no_tunable_parameters": True,
            },
            "source": "def strategy(history, state):\n    return {CASH: 1.0}\n",
        },
    }
    server, thread = _start_server([_chat_response(json.dumps(proposal))])
    try:
        provider = _best_effort_provider(
            endpoint=f"http://127.0.0.1:{server.server_port}/v1",
            model="test-model",
            api_key="local-test-secret",
            input_cost_per_1k_tokens=1.0,
            output_cost_per_1k_tokens=2.0,
        )
        generated = provider.propose(
            1, {
                "isolated_generated_python": True,
                "limits": {"remaining_tokens": 2000, "remaining_cost_usd": 1.0},
            }
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert generated.strategy == proposal["strategy"]
    content = server.requests[0]["payload"]["messages"][1]["content"]
    assert "strategy(history, state)" in content
    assert "do not return Python" not in content


def test_openai_provider_stops_before_a_request_outside_token_budget() -> None:
    server, thread = _start_server([])
    try:
        provider = _best_effort_provider(
            endpoint=f"http://127.0.0.1:{server.server_port}/v1",
            model="test-model",
            api_key="local-test-secret",
            input_cost_per_1k_tokens=1.0,
            output_cost_per_1k_tokens=2.0,
        )
        with pytest.raises(ProviderBudgetExceeded, match="token budget"):
            provider.propose(
                1,
                {
                    "limits": {
                        "remaining_tokens": 1,
                        "remaining_cost_usd": 1.0,
                    }
                },
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert server.requests == []


def test_openai_provider_stops_before_estimated_cost_exceeds_budget() -> None:
    server, thread = _start_server([])
    try:
        provider = _best_effort_provider(
            endpoint=f"http://127.0.0.1:{server.server_port}/v1",
            model="test-model",
            api_key="local-test-secret",
            input_cost_per_1k_tokens=1.0,
            output_cost_per_1k_tokens=2.0,
        )
        with pytest.raises(ProviderBudgetExceeded, match="prompt cost"):
            provider.propose(
                1,
                {
                    "limits": {
                        "remaining_tokens": 2000,
                        "remaining_cost_usd": 0.000001,
                    }
                },
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert server.requests == []


def test_openai_provider_requires_tls_for_remote_credentials() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        _best_effort_provider(
            endpoint="http://provider.example/v1",
            model="test-model",
            api_key="local-test-secret",
            input_cost_per_1k_tokens=1.0,
            output_cost_per_1k_tokens=2.0,
        )


def test_structured_contract_rejects_malformed_or_wrong_version_json() -> None:
    with pytest.raises(ProviderError, match="malformed JSON"):
        parse_structured_response("not json")
    with pytest.raises(ProviderError, match="schema_version"):
        parse_structured_response('{"schema_version":"2"}')


def test_openai_provider_rejects_strategy_outside_versioned_contract() -> None:
    proposal = {
        "schema_version": "1",
        "hypothesis": "Try an unsupported strategy.",
        "rationale": "The strategy family is not part of the contract.",
        "strategy": {"family": "parameter_grid", "parameters": {}},
    }
    server, thread = _start_server([_chat_response(json.dumps(proposal))])
    try:
        provider = _best_effort_provider(
            endpoint=f"http://127.0.0.1:{server.server_port}/v1",
            model="test-model",
            api_key="local-test-secret",
            input_cost_per_1k_tokens=1.0,
            output_cost_per_1k_tokens=2.0,
        )
        with pytest.raises(ProviderError, match="strategy"):
            provider.propose(
                1, {"limits": {"remaining_tokens": 2000, "remaining_cost_usd": 1.0}}
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert server.requests[0]["authorization"] == "Bearer local-test-secret"


def test_openai_provider_rejects_python_without_trusted_local_opt_in() -> None:
    proposal = {
        "schema_version": "1",
        "hypothesis": "Try generated Python.",
        "rationale": "Execution is not authorized by the caller.",
        "strategy": {
            "family": "python",
            "parameters": {},
            "source": "def strategy(history, state):\n    return {CASH: 1.0}\n",
        },
    }
    server, thread = _start_server([_chat_response(json.dumps(proposal))])
    try:
        provider = _best_effort_provider(
            endpoint=f"http://127.0.0.1:{server.server_port}/v1",
            model="test-model",
            api_key="local-test-secret",
            input_cost_per_1k_tokens=1.0,
            output_cost_per_1k_tokens=2.0,
        )
        with pytest.raises(ProviderError, match="explicit OS isolation"):
            provider.propose(
                1, {"limits": {"remaining_tokens": 2000, "remaining_cost_usd": 1.0}}
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_openai_provider_bounds_http_timeout_by_remaining_campaign_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import prajnatrade.providers as providers_module

    timeouts: list[float] = []

    def fail_request(request: object, *, timeout: float) -> None:
        timeouts.append(timeout)
        raise TimeoutError

    monkeypatch.setattr(providers_module, "urlopen", fail_request)
    provider = _best_effort_provider(
        endpoint="http://127.0.0.1/v1",
        model="test-model",
        api_key="local-test-secret",
        timeout=30,
        input_cost_per_1k_tokens=1.0,
        output_cost_per_1k_tokens=2.0,
    )
    with pytest.raises(ProviderError, match="TimeoutError"):
        provider.propose(
            1,
            {
                "limits": {
                    "remaining_seconds": 0.25,
                    "remaining_tokens": 2000,
                    "remaining_cost_usd": 1.0,
                }
            },
        )

    assert timeouts == [0.25]


@pytest.mark.parametrize(
    "generated_python",
    [False, pytest.param(True, marks=pytest.mark.isolation)],
)
def test_campaign_records_provider_usage_without_persisting_api_credentials(
    tmp_path: Path, generated_python: bool,
) -> None:
    proposal = {
        "schema_version": "1",
        "hypothesis": "Equal weight is a stable control.",
        "rationale": "Use it as a reference.",
        "strategy": {"family": "equal_weight", "parameters": {}},
    }
    if generated_python:
        proposal["strategy"] = {
            "family": "python",
            "parameters": {},
            "parameter_schema": {
                "parameters": {}, "constraints": [], "no_tunable_parameters": True,
            },
            "source": "def strategy(history, state):\n    return {CASH: 1.0}\n",
        }
    reflection = {
        "schema_version": "1",
        "decision": "keep",
        "reflection": "Retain this control for comparison.",
    }
    server, thread = _start_server(
        [_chat_response(json.dumps(proposal)), _chat_response(json.dumps(reflection))]
    )
    bars_path = tmp_path / "bars.csv"
    database = tmp_path / "research.sqlite"
    lines = ["date,symbol,open,high,low,close,volume,price_basis"]
    for index in range(6):
        day = (date(2024, 1, 1) + timedelta(days=index)).isoformat()
        for symbol_index, symbol in enumerate(ETF_SYMBOLS):
            close = 100 + symbol_index + index
            lines.append(
                f"{day},{symbol},{close - 1},{close},{close - 1},{close},1000,total_return"
            )
    bars_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    secret = "local-test-secret-that-must-not-be-stored"
    endpoint = f"http://127.0.0.1:{server.server_port}/v1"
    provider = _best_effort_provider(
        endpoint=endpoint,
        model="test-model",
        api_key=secret,
        input_cost_per_1k_tokens=0.1,
        output_cost_per_1k_tokens=0.2,
    )
    try:
        result = start_campaign(
            database=database,
            dataset=load_bars(bars_path, "total_return"),
            data_path=bars_path,
            data_kind="synthetic",
            provider=provider,
            seed=1,
            experiment_budget=1,
            max_consecutive_failures=2,
            max_seconds=60,
            initial_cash=10_000,
            transaction_cost_bps=0,
            slippage_bps=0,
            token_budget=5000,
            cost_budget_usd=1,
            steps=None,
            holdout_start="2025-01-01",
            holdout_end="2025-01-31",
            isolated_python=generated_python,
            provider_config={
                "endpoint": endpoint,
                "api_key_env": "TEST_PROVIDER_KEY",
                "timeout": 30,
                "max_tokens": 1024,
                "input_cost_per_1k_tokens": 0.1,
                "output_cost_per_1k_tokens": 0.2,
            },
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    campaign = SQLiteStore(database).get_campaign(result["campaign_id"])
    experiments = SQLiteStore(database).list_experiments(result["campaign_id"])
    assert campaign["status"] == "completed"
    assert campaign["provider_config"]["api_key_env"] == "TEST_PROVIDER_KEY"
    assert secret not in repr(campaign)
    assert experiments[0]["provider_usage"]["total_tokens"] == 300
    assert experiments[0]["provider_usage"]["estimated_cost_usd"] > 0
    assert experiments[0]["data_hash"] == campaign["data_hash"]
    assert experiments[0]["strategy"] == proposal["strategy"]
    persisted_files = [database.read_bytes()]
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{database}{suffix}")
        if sidecar.exists():
            persisted_files.append(sidecar.read_bytes())
    assert all(secret.encode("utf-8") not in content for content in persisted_files)


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps({
            "schema_version": "1",
            "hypothesis": "local-test-secret",
            "rationale": "An echoed credential must never be persisted.",
            "strategy": {"family": "equal_weight", "parameters": {}},
        }),
    ],
)
def test_invalid_provider_response_retains_reported_usage(
    tmp_path: Path, content: str
) -> None:
    server, thread = _start_server([_chat_response(content)])
    bars_path = tmp_path / "bars.csv"
    database = tmp_path / "research.sqlite"
    lines = ["date,symbol,open,high,low,close,volume,price_basis"]
    for index in range(6):
        day = (date(2024, 1, 1) + timedelta(days=index)).isoformat()
        for symbol in ETF_SYMBOLS:
            lines.append(f"{day},{symbol},100,101,100,101,1000,total_return")
    bars_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    provider = _best_effort_provider(
        endpoint=f"http://127.0.0.1:{server.server_port}/v1",
        model="test-model",
        api_key="local-test-secret",
        input_cost_per_1k_tokens=0.1,
        output_cost_per_1k_tokens=0.2,
    )
    try:
        result = start_campaign(
            database=database,
            dataset=load_bars(bars_path, "total_return"),
            data_path=bars_path,
            data_kind="synthetic",
            provider=provider,
            seed=1,
            experiment_budget=2,
            max_consecutive_failures=1,
            max_seconds=60,
            initial_cash=10_000,
            transaction_cost_bps=0,
            slippage_bps=0,
            token_budget=5000,
            cost_budget_usd=1,
            steps=None,
            holdout_start="2025-01-01",
            holdout_end="2025-01-31",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    experiment = SQLiteStore(database).list_experiments(result["campaign_id"])[0]
    assert experiment["status"] == "failed"
    assert experiment["provider_usage"]["total_tokens"] == 150
    assert experiment["provider_usage"]["estimated_cost_usd"] == pytest.approx(0.02)
    assert b"local-test-secret" not in database.read_bytes()


def test_missing_provider_usage_reserves_budget_for_failed_request() -> None:
    server, thread = _start_server([{"choices": []}])
    try:
        provider = _best_effort_provider(
            endpoint=f"http://127.0.0.1:{server.server_port}/v1",
            model="test-model",
            api_key="local-test-secret",
            input_cost_per_1k_tokens=0.1,
            output_cost_per_1k_tokens=0.2,
        )
        with pytest.raises(ProviderError) as failed:
            provider.propose(
                1, {"limits": {"remaining_tokens": 2000, "remaining_cost_usd": 1.0}}
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert failed.value.usage["total_tokens"] > 0
    assert failed.value.usage["estimated_cost_usd"] > 0
    assert failed.value.usage["usage_estimated"] is True
