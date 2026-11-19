from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import date, timedelta
from pathlib import Path

import pytest

from prajnatrade.data import ETF_SYMBOLS, load_bars
from prajnatrade.providers import (
    OpenAICompatibleProvider,
    ProviderBudgetExceeded,
    ProviderError,
    parse_structured_response,
)
from prajnatrade.research import start_campaign
from prajnatrade.store import SQLiteStore


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
        provider = OpenAICompatibleProvider(
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
            "source": "def strategy(history, state):\n    return {CASH: 1.0}\n",
        },
    }
    server, thread = _start_server([_chat_response(json.dumps(proposal))])
    try:
        provider = OpenAICompatibleProvider(
            endpoint=f"http://127.0.0.1:{server.server_port}/v1",
            model="test-model",
            api_key="local-test-secret",
            input_cost_per_1k_tokens=1.0,
            output_cost_per_1k_tokens=2.0,
        )
        generated = provider.propose(
            1, {
                "trusted_local_python": True,
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
        provider = OpenAICompatibleProvider(
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
        provider = OpenAICompatibleProvider(
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
        OpenAICompatibleProvider(
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


@pytest.mark.parametrize("generated_python", [False, True])
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
    provider = OpenAICompatibleProvider(
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
            trusted_local_python=generated_python,
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
    provider = OpenAICompatibleProvider(
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
        provider = OpenAICompatibleProvider(
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
