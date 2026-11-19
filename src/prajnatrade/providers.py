from __future__ import annotations

import json
import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlparse, urlunparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from prajnatrade.strategies import STRATEGY_FAMILIES, normalize_strategy_spec


class ProviderError(RuntimeError):
    """Raised when a research provider fails or returns an invalid contract."""

    def __init__(self, message: str, *, usage: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.usage = dict(usage) if usage is not None else None


class ProviderBudgetExceeded(ProviderError):
    """Raised before a provider request when its estimated cost exceeds budget."""

    def __init__(self, budget_kind: str, message: str) -> None:
        super().__init__(message)
        self.budget_kind = budget_kind


@dataclass(frozen=True)
class Proposal:
    hypothesis: str
    rationale: str
    strategy: dict[str, object]
    usage: dict[str, Any]


@dataclass(frozen=True)
class Reflection:
    decision: str
    text: str
    usage: dict[str, Any]


class ResearchProvider(Protocol):
    name: str
    model: str

    def propose(self, iteration: int, context: Mapping[str, Any]) -> Proposal: ...

    def reflect(
        self,
        proposal: Proposal,
        result: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> Reflection: ...


class MockProvider:
    name = "mock"
    model = "deterministic-mock-v1"

    def __init__(self, seed: int) -> None:
        self.seed = seed

    def propose(self, iteration: int, context: Mapping[str, Any]) -> Proposal:
        if context.get("trusted_local_python"):
            return self._propose_python(iteration, context)
        rng = random.Random(self.seed + iteration)
        family = STRATEGY_FAMILIES[(self.seed + iteration - 1) % len(STRATEGY_FAMILIES)]
        previous = context.get("experiments", [])
        last = previous[-1] if previous else {}
        prior_strategy = last.get("strategy") or {}
        prior_family = prior_strategy.get("family")
        decision = last.get("decision")
        if prior_family in STRATEGY_FAMILIES:
            if last.get("status") == "failed":
                family = STRATEGY_FAMILIES[
                    (STRATEGY_FAMILIES.index(prior_family) + 1) % len(STRATEGY_FAMILIES)
                ]
            elif decision in {"keep", "investigate"}:
                family = prior_family
            elif decision in {"reject", "branch"}:
                family = STRATEGY_FAMILIES[
                    (STRATEGY_FAMILIES.index(prior_family) + 1) % len(STRATEGY_FAMILIES)
                ]
        parameters: dict[str, object] = {}
        if family != "equal_weight":
            if family == prior_family and decision in {"keep", "investigate"}:
                prior_lookback = prior_strategy.get("parameters", {}).get("lookback", 3)
                parameters["lookback"] = 2 if prior_lookback >= 252 else prior_lookback + 1
            else:
                parameters["lookback"] = rng.randint(3, 8)
        if family == "momentum":
            prior_parameters = prior_strategy.get("parameters", {})
            parameters["top_k"] = (
                prior_parameters.get("top_k", 1)
                if family == prior_family and decision in {"keep", "investigate"}
                else rng.randint(1, 3)
            )
            parameters["cash_if_negative"] = prior_parameters.get("cash_if_negative", True)
        strategy = {"family": family, "parameters": parameters}
        if family == "momentum":
            hypothesis = (
                f"ETFs with the strongest positive {parameters['lookback']}-session "
                f"return among the top {parameters['top_k']} will outperform equal weight."
            )
        elif family == "trend_filter":
            hypothesis = (
                f"Equal-weighting ETFs above their {parameters['lookback']}-session "
                "average will reduce drawdown without giving up all return."
            )
        elif family == "inverse_volatility":
            hypothesis = (
                f"Inverse {parameters['lookback']}-session volatility weights will "
                "improve risk-adjusted return versus equal weight."
            )
        else:
            hypothesis = "A simple equal-weight buy-and-hold remains a useful control."
        return Proposal(
            hypothesis=hypothesis,
            rationale=(
                f"Deterministic Mock proposal following {decision or 'initial'} feedback; "
                "it is orchestration test data, not AI research."
            ),
            strategy=strategy,
            usage=_zero_usage(),
        )

    def _propose_python(
        self, iteration: int, context: Mapping[str, Any]
    ) -> Proposal:
        direction = (self.seed + iteration - 1) % 2
        lookback = 3
        previous = context.get("experiments", [])
        if previous:
            last = previous[-1]
            prior_strategy = last.get("strategy") or {}
            source = prior_strategy.get("source", "")
            if prior_strategy.get("family") == "python" and isinstance(source, str):
                prior_direction = 0 if "scores = []" in source else 1
                if last.get("decision") in {"keep", "investigate"}:
                    direction = prior_direction
                    lookback = 4 if "iloc[-3" in source else 3
                else:
                    direction = 1 - prior_direction
        if direction == 0:
            source = (
                "def strategy(history, state):\n"
                "    scores = []\n"
                "    for symbol in ETF_SYMBOLS:\n"
                "        closes = history.loc[(history['symbol'] == symbol) & history['close'].notna(), 'close']\n"
                "        if len(closes) >= 3:\n"
                "            scores.append((float(closes.iloc[-1] / closes.iloc[-3] - 1), symbol))\n"
                "    if not scores or max(scores)[0] <= 0:\n"
                "        return {CASH: 1.0}\n"
                "    return {max(scores)[1]: 1.0}\n"
            )
            hypothesis = f"The strongest positive {lookback - 1}-session ETF momentum outperforms equal weight."
        else:
            source = (
                "def strategy(history, state):\n"
                "    selected = []\n"
                "    for symbol in ETF_SYMBOLS:\n"
                "        closes = history.loc[(history['symbol'] == symbol) & history['close'].notna(), 'close']\n"
                "        if len(closes) >= 3 and closes.iloc[-1] > closes.iloc[-3:].mean():\n"
                "            selected.append(symbol)\n"
                "    if not selected:\n"
                "        return {CASH: 1.0}\n"
                "    return {symbol: 1.0 / len(selected) for symbol in selected}\n"
            )
            hypothesis = f"Equal-weighting ETFs above their {lookback}-session average improves downside control."
        source = source.replace(">= 3", f">= {lookback}").replace(
            "iloc[-3]", f"iloc[-{lookback}]"
        ).replace("iloc[-3:]", f"iloc[-{lookback}:]")
        return Proposal(
            hypothesis=hypothesis,
            rationale=(
                "Deterministic Mock-generated Python based on prior feedback; "
                "this is not AI research."
            ),
            strategy={"family": "python", "parameters": {}, "source": source},
            usage=_zero_usage(),
        )

    def reflect(
        self,
        proposal: Proposal,
        result: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> Reflection:
        if result["status"] == "failed":
            return Reflection(
                decision="investigate",
                text=f"The proposal failed: {result.get('error', 'unknown error')}",
                usage=_zero_usage(),
            )
        score = float(result["metrics"]["total_return"])
        baseline = float(context["baseline"]["total_return"])
        previous = [
            item
            for item in context["experiments"]
            if item["status"] == "succeeded"
        ]
        best_previous = max(
            [baseline]
            + [float(item["metrics"]["total_return"]) for item in previous]
        )
        family_tried = {
            (
                item["strategy"]["family"],
                item["strategy"].get("source")
                if item["strategy"]["family"] == "python"
                else None,
            )
            for item in context["experiments"]
            if isinstance(item.get("strategy"), dict)
            and "family" in item["strategy"]
        }
        family = (
            str(proposal.strategy["family"]),
            proposal.strategy.get("source") if proposal.strategy["family"] == "python" else None,
        )
        if (
            score < baseline
            and len(previous) >= 3
            and all(
                item.get("decision") == "reject"
                and float(item["metrics"]["total_return"]) < baseline
                for item in previous[-3:]
            )
        ):
            decision = "stop"
            text = "Repeated underperformance warrants stopping this research direction."
        elif score > best_previous:
            decision = "keep"
            text = "This result improved the discovery return; retain it as a candidate for independent validation."
        elif family not in family_tried:
            decision = "branch"
            text = "This is a new strategy direction; branch to test its mechanism before tuning it."
        elif score >= baseline:
            decision = "investigate"
            text = "The result is near the baseline; inspect costs and drawdown before spending more attempts."
        else:
            decision = "reject"
            text = "The result did not beat the same-pool baseline on discovery data."
        return Reflection(decision=decision, text=text, usage=_zero_usage())


class OpenAICompatibleProvider:
    name = "openai-compatible"

    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        api_key: str,
        timeout: float = 30.0,
        max_tokens: int = 1024,
        input_cost_per_1k_tokens: float,
        output_cost_per_1k_tokens: float,
    ) -> None:
        if not endpoint.startswith(("https://", "http://")):
            raise ValueError("Provider endpoint must be an HTTP(S) URL.")
        if not model.strip() or not api_key:
            raise ValueError("Provider model and API key must be configured.")
        if not math.isfinite(timeout) or timeout <= 0 or max_tokens <= 0:
            raise ValueError("Provider timeout and max_tokens must be greater than zero.")
        if (
            not math.isfinite(input_cost_per_1k_tokens)
            or not math.isfinite(output_cost_per_1k_tokens)
            or input_cost_per_1k_tokens < 0
            or output_cost_per_1k_tokens < 0
        ):
            raise ValueError("Provider token rates must be finite and nonnegative.")
        if input_cost_per_1k_tokens + output_cost_per_1k_tokens <= 0:
            raise ValueError("Configure a nonzero token rate to enforce the cost budget.")
        self.endpoint = _completion_endpoint(endpoint)
        self.model = model.strip()
        self._api_key = api_key
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.input_cost_per_1k_tokens = input_cost_per_1k_tokens
        self.output_cost_per_1k_tokens = output_cost_per_1k_tokens

    def propose(self, iteration: int, context: Mapping[str, Any]) -> Proposal:
        trusted_local_python = bool(context.get("trusted_local_python"))
        prompt = {
            "task": "Propose one falsifiable ETF strategy research experiment.",
            "iteration": iteration,
            "objective": (
                "Research only the five ETF universe and cash. Generate a Python "
                "strategy(history, state) function. ETF_SYMBOLS, CASH and parameters "
                "are provided as globals. Return ETF/cash weights or None. "
                "This runs only with explicit trusted-local opt-in, without isolation."
                if trusted_local_python
                else "Research only the five ETF universe and cash. Return a declarative "
                "strategy specification; do not return Python or executable code."
            ),
            "allowed_strategy_families": (
                ["python"] if trusted_local_python else list(STRATEGY_FAMILIES)
            ),
            "context": _safe_context(context),
            "response_contract": {
                "schema_version": "1",
                "hypothesis": "string",
                "rationale": "string",
                "strategy": {
                    "family": "python" if trusted_local_python else "one allowed family",
                    "parameters": "JSON object",
                    **({"source": "Python source defining strategy(history, state)"} if trusted_local_python else {}),
                },
            },
        }
        response, usage = self._request(prompt, context)
        try:
            parsed = parse_structured_response(response)
            if _contains_credential(parsed, self._api_key):
                raise ProviderError("Provider response contains the configured credential.")
            proposal = validate_proposal_response(
                parsed, trusted_local_python=trusted_local_python
            )
        except ProviderError as error:
            raise ProviderError(str(error), usage=usage) from error
        return replace(proposal, usage=usage)

    def reflect(
        self,
        proposal: Proposal,
        result: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> Reflection:
        prompt = {
            "task": "Interpret the completed research experiment and decide what to do next.",
            "hypothesis": proposal.hypothesis,
            "strategy": proposal.strategy,
            "result": _safe_context(result),
            "prior_context": _safe_context(context),
            "response_contract": {
                "schema_version": "1",
                "decision": "keep, reject, branch, investigate, or stop",
                "reflection": "string",
            },
        }
        response, usage = self._request(prompt, context)
        try:
            parsed = parse_structured_response(response)
            if _contains_credential(parsed, self._api_key):
                raise ProviderError("Provider response contains the configured credential.")
            reflection = validate_reflection_response(parsed)
        except ProviderError as error:
            raise ProviderError(str(error), usage=usage) from error
        return replace(reflection, usage=usage)

    def _request(
        self, prompt: Mapping[str, Any], context: Mapping[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        limits = context.get("limits", {})
        try:
            remaining_seconds = float(limits.get("remaining_seconds", self.timeout))
        except (TypeError, ValueError) as error:
            raise ProviderError("Remaining provider time budget must be a number.") from error
        if not math.isfinite(remaining_seconds):
            raise ProviderError("Remaining provider time budget must be finite.")
        if remaining_seconds <= 0:
            raise ProviderBudgetExceeded(
                "time_budget", "No campaign time remains for a provider request."
            )
        request_timeout = min(self.timeout, remaining_seconds)
        remaining_tokens = int(limits.get("remaining_tokens", self.max_tokens))
        remaining_cost = float(limits.get("remaining_cost_usd", float("inf")))
        request_payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Return only a JSON object following the supplied versioned "
                        "response contract. Never include credentials. "
                        + (
                            "Python source is permitted only as requested in the contract."
                            if context.get("trusted_local_python")
                            else "Never include executable Python."
                        )
                    ),
                },
                {"role": "user", "content": json.dumps(prompt, sort_keys=True)},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        prompt_text = json.dumps(request_payload, sort_keys=True)
        estimated_prompt_tokens = max(1, math.ceil(len(prompt_text.encode("utf-8")) / 4))
        remaining_completion_tokens = min(
            self.max_tokens, remaining_tokens - estimated_prompt_tokens
        )
        if remaining_completion_tokens <= 0:
            raise ProviderBudgetExceeded(
                "token_budget", "Estimated request exceeds the remaining token budget."
            )

        input_cost = (
            estimated_prompt_tokens * self.input_cost_per_1k_tokens / 1000.0
        )
        remaining_after_input = remaining_cost - input_cost
        if remaining_after_input <= 0:
            raise ProviderBudgetExceeded(
                "cost_budget",
                "Estimated prompt cost would exceed the remaining provider cost budget.",
            )
        if self.output_cost_per_1k_tokens > 0:
            cost_token_limit = int(
                remaining_after_input * 1000.0 / self.output_cost_per_1k_tokens
            )
            remaining_completion_tokens = min(
                remaining_completion_tokens, cost_token_limit
            )
        if remaining_completion_tokens <= 0:
            raise ProviderBudgetExceeded(
                "cost_budget",
                "No completion tokens remain within the provider cost budget.",
            )
        request_payload["max_tokens"] = remaining_completion_tokens
        reserved_usage = {
            "prompt_tokens": estimated_prompt_tokens,
            "completion_tokens": remaining_completion_tokens,
            "total_tokens": estimated_prompt_tokens + remaining_completion_tokens,
            "estimated_cost_usd": (
                estimated_prompt_tokens * self.input_cost_per_1k_tokens
                + remaining_completion_tokens * self.output_cost_per_1k_tokens
            ) / 1000.0,
            "requests": 1,
            "usage_estimated": True,
        }

        request = Request(
            self.endpoint,
            data=json.dumps(request_payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=request_timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            raise ProviderError(
                f"Provider returned HTTP status {error.code}.", usage=reserved_usage
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise ProviderError(
                f"Provider request failed: {type(error).__name__}.", usage=reserved_usage
            ) from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProviderError(
                "Provider returned an invalid response body.", usage=reserved_usage
            ) from error

        if not isinstance(body, dict):
            raise ProviderError("Provider response must be a JSON object.", usage=reserved_usage)
        try:
            content = body["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("response content is not a string")
        except (KeyError, IndexError, TypeError) as error:
            raise ProviderError(
                "Provider response is missing chat completion content.", usage=reserved_usage
            ) from error
        usage_data = body.get("usage", {})
        if not isinstance(usage_data, dict):
            usage_data = {}
        prompt_tokens = _nonnegative_int(usage_data.get("prompt_tokens"), estimated_prompt_tokens)
        completion_tokens = _nonnegative_int(
            usage_data.get("completion_tokens"),
            max(1, math.ceil(len(content.encode("utf-8")) / 4)),
        )
        total_tokens = _nonnegative_int(
            usage_data.get("total_tokens"), prompt_tokens + completion_tokens
        )
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "estimated_cost_usd": (
                prompt_tokens * self.input_cost_per_1k_tokens
                + completion_tokens * self.output_cost_per_1k_tokens
            )
            / 1000.0,
            "requests": 1,
            "usage_estimated": any(
                _nonnegative_int(usage_data.get(name), -1) < 0
                for name in ("prompt_tokens", "completion_tokens", "total_tokens")
            ),
        }
        return content, usage


def _zero_usage() -> dict[str, Any]:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "estimated_cost_usd": 0.0,
        "requests": 0,
    }


def combine_usage(*usages: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "prompt_tokens": sum(int(usage.get("prompt_tokens", 0)) for usage in usages),
        "completion_tokens": sum(
            int(usage.get("completion_tokens", 0)) for usage in usages
        ),
        "total_tokens": sum(int(usage.get("total_tokens", 0)) for usage in usages),
        "estimated_cost_usd": sum(
            float(usage.get("estimated_cost_usd", 0.0)) for usage in usages
        ),
        "requests": sum(int(usage.get("requests", 0)) for usage in usages),
        "usage_estimated": any(bool(usage.get("usage_estimated")) for usage in usages),
    }


def parse_structured_response(content: str, expected_version: str = "1") -> dict[str, Any]:
    try:
        response = json.loads(content)
    except json.JSONDecodeError as error:
        raise ProviderError("Provider returned malformed JSON.") from error
    if not isinstance(response, dict) or response.get("schema_version") != expected_version:
        raise ProviderError(
            f"Provider response must be a JSON object with schema_version {expected_version!r}."
        )
    return response


def validate_proposal_response(
    response: Mapping[str, Any], *, trusted_local_python: bool = False
) -> Proposal:
    if set(response) != {"schema_version", "hypothesis", "rationale", "strategy"}:
        raise ProviderError("Provider proposal does not match the versioned response contract.")
    hypothesis = response.get("hypothesis")
    rationale = response.get("rationale")
    strategy = response.get("strategy")
    if not isinstance(hypothesis, str) or not hypothesis.strip() or len(hypothesis) > 1000:
        raise ProviderError("Provider hypothesis must be a non-empty string of at most 1,000 characters.")
    if not isinstance(rationale, str) or len(rationale) > 2000:
        raise ProviderError("Provider rationale must be a string of at most 2,000 characters.")
    if not isinstance(strategy, dict):
        raise ProviderError("Provider strategy must be a JSON object.")
    family = strategy.get("family")
    if trusted_local_python:
        if family != "python":
            raise ProviderError("Provider strategy must be Python when trusted-local opt-in is enabled.")
        expected_strategy_fields = {"family", "parameters", "source"}
    else:
        if family not in STRATEGY_FAMILIES:
            raise ProviderError(
                "Provider strategy family is unsupported; generated Python requires trusted-local opt-in."
            )
        expected_strategy_fields = {"family", "parameters"}
    if set(strategy) != expected_strategy_fields:
        raise ProviderError("Provider strategy does not match the versioned response contract.")
    try:
        normalized_strategy = normalize_strategy_spec(strategy)
    except (TypeError, ValueError) as error:
        raise ProviderError(f"Provider strategy contract is invalid: {error}") from error
    return Proposal(
        hypothesis=hypothesis.strip(),
        rationale=rationale.strip(),
        strategy=normalized_strategy,
        usage=_zero_usage(),
    )


def validate_reflection_response(response: Mapping[str, Any]) -> Reflection:
    if set(response) != {"schema_version", "decision", "reflection"}:
        raise ProviderError("Provider reflection does not match the versioned response contract.")
    allowed = {"keep", "reject", "branch", "investigate", "stop"}
    decision = response.get("decision")
    text = response.get("reflection")
    if not isinstance(decision, str) or decision not in allowed:
        raise ProviderError(f"Provider decision must be one of {', '.join(sorted(allowed))}.")
    if not isinstance(text, str) or not text.strip() or len(text) > 2000:
        raise ProviderError("Provider reflection must be a non-empty string of at most 2,000 characters.")
    return Reflection(decision=decision, text=text.strip(), usage=_zero_usage())


def recent_experiment_context(experiments: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    summaries = []
    metric_names = (
        "total_return",
        "benchmark_excess_return",
        "cagr",
        "sharpe",
        "max_drawdown",
    )
    for item in experiments[-10:]:
        metrics = item.get("metrics") or {}
        summaries.append(
            {
                "ordinal": item["ordinal"],
                "hypothesis": item["hypothesis"],
                "rationale": item["rationale"],
                "strategy": item["strategy"],
                "status": item["status"],
                "decision": item["decision"],
                "reflection": item["reflection"],
                "metrics": {
                    name: metrics[name] for name in metric_names if name in metrics
                },
                "error": item.get("error"),
            }
        )
    return summaries


def _completion_endpoint(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    if parsed.scheme == "http" and parsed.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise ValueError("Provider credentials require HTTPS except for a loopback test endpoint.")
    if parsed.username or parsed.password:
        raise ValueError("Provider endpoint must not contain embedded credentials.")
    sensitive_query_names = {"api_key", "apikey", "access_token", "authorization", "key", "token"}
    if sensitive_query_names.intersection(
        name.lower() for name, _ in parse_qsl(parsed.query, keep_blank_values=True)
    ):
        raise ValueError("Provider credentials must be supplied through the configured environment variable.")
    path = parsed.path.rstrip("/")
    if not path.endswith("/chat/completions"):
        path = f"{path}/chat/completions"
    return urlunparse(parsed._replace(path=path))


def _safe_context(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _safe_context(item)
            for key, item in value.items()
            if key not in {"api_key", "authorization", "credentials", "secret"}
        }
    if isinstance(value, (list, tuple)):
        return [_safe_context(item) for item in value]
    return value


def _contains_credential(value: Any, credential: str) -> bool:
    if isinstance(value, str):
        return credential in value
    if isinstance(value, Mapping):
        return any(
            _contains_credential(key, credential) or _contains_credential(item, credential)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_credential(item, credential) for item in value)
    return False


def _nonnegative_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default
