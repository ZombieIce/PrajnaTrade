from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd

from prajnatrade.backtest import Strategy, equal_weight_buy_and_hold
from prajnatrade.container_executor import ContainerStrategyExecutor
from prajnatrade.data import CASH, ETF_SYMBOLS
from prajnatrade.isolation import (
    IsolationError,
    IsolationLimits,
    run_isolated_strategy,
)

STRATEGY_FAMILIES = (
    "equal_weight",
    "momentum",
    "trend_filter",
    "inverse_volatility",
)


def normalize_strategy_spec(spec: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(spec, Mapping):
        raise ValueError("Strategy must be a JSON object.")
    family = spec.get("family")
    if family not in (*STRATEGY_FAMILIES, "python"):
        raise ValueError(f"Unsupported strategy family: {family!r}.")
    params = spec.get("parameters", {})
    if not isinstance(params, Mapping):
        raise ValueError("Strategy parameters must be a JSON object.")
    if family == "python":
        source = spec.get("source")
        if not isinstance(source, str) or not source.strip() or len(source) > 20_000:
            raise ValueError("Python strategy source must contain 1 to 20,000 characters.")
        if any(not isinstance(key, str) for key in params):
            raise ValueError("Python strategy parameter keys must be strings.")
        try:
            parameters = json.loads(json.dumps(params, allow_nan=False))
            compile(source, "<generated-strategy>", "exec")
        except (TypeError, ValueError, SyntaxError) as error:
            raise ValueError(f"Invalid Python strategy: {error}") from error
        normalized = {"family": "python", "parameters": parameters, "source": source}
        if "parameter_schema" in spec:
            normalized["parameter_schema"] = _normalize_parameter_schema(
                spec["parameter_schema"], parameters
            )
        return normalized
    normalized_params: dict[str, object] = {}
    if family == "equal_weight":
        if params:
            raise ValueError("equal_weight does not accept parameters.")
    elif family in {"momentum", "trend_filter", "inverse_volatility"}:
        lookback = _integer_parameter(params, "lookback", default=20, minimum=2, maximum=252)
        normalized_params["lookback"] = lookback
        if family == "momentum":
            normalized_params["top_k"] = _integer_parameter(
                params, "top_k", default=1, minimum=1, maximum=len(ETF_SYMBOLS)
            )
            cash_if_negative = params.get("cash_if_negative", True)
            if not isinstance(cash_if_negative, bool):
                raise ValueError("cash_if_negative must be a boolean.")
            normalized_params["cash_if_negative"] = cash_if_negative
    unknown_parameters = set(params) - set(normalized_params)
    if unknown_parameters:
        raise ValueError(
            "Unsupported strategy parameter(s): "
            + ", ".join(sorted(str(key) for key in unknown_parameters))
            + "."
        )
    return {"family": str(family), "parameters": normalized_params}


def _normalize_parameter_schema(schema: object, parameters: dict[str, object]) -> dict[str, object]:
    if (
        not isinstance(schema, Mapping)
        or not {"parameters", "constraints"} <= set(schema)
        or set(schema) - {"parameters", "constraints", "no_tunable_parameters", "joint_variants"}
    ):
        raise ValueError("Parameter schema must contain parameters and constraints.")
    definitions = schema["parameters"]
    constraints = schema["constraints"]
    if not isinstance(definitions, Mapping) or not isinstance(constraints, list):
        raise ValueError("Parameter schema definitions must be an object and constraints a list.")
    numeric = {
        key for key, value in parameters.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    if set(definitions) != numeric:
        raise ValueError("Parameter schema must declare every numeric strategy parameter.")
    if not definitions and not parameters and schema.get("no_tunable_parameters") is not True:
        raise ValueError("An empty parameter schema must explicitly declare no_tunable_parameters.")
    if parameters and schema.get("no_tunable_parameters", False) is not False:
        raise ValueError("A strategy with parameters cannot declare no_tunable_parameters.")
    normalized = {}
    for name, definition in definitions.items():
        if not isinstance(name, str) or not isinstance(definition, Mapping) or set(definition) != {
            "type", "default", "min", "max", "step"
        }:
            raise ValueError("Each numeric parameter requires type, default, min, max and step.")
        kind = definition["type"]
        if kind not in {"integer", "number"}:
            raise ValueError(f"Unsupported numeric parameter type for {name}.")
        if kind == "integer" and not isinstance(parameters[name], int):
            raise ValueError(f"Parameter {name} must be an integer.")
        for field in ("default", "min", "max", "step"):
            value = definition[field]
            if (
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or (kind == "integer" and not isinstance(value, int))
            ):
                raise ValueError(f"Invalid {field} for parameter {name}.")
        minimum, maximum, step = (
            Decimal(str(definition[key])) for key in ("min", "max", "step")
        )
        default = Decimal(str(definition["default"]))
        current = Decimal(str(parameters[name]))
        if (
            minimum > maximum or step <= 0 or default < minimum or default > maximum
            or current < minimum or current > maximum
            or (default - minimum) % step or (current - minimum) % step
        ):
            raise ValueError(f"Parameter {name} has invalid range, step or value.")
        normalized[name] = dict(definition)
    clean_constraints = []
    for constraint in constraints:
        if (
            not isinstance(constraint, Mapping) or set(constraint) != {"left", "op", "right"}
            or not isinstance(constraint["left"], str)
            or not isinstance(constraint["op"], str)
            or constraint["left"] not in definitions
            or constraint["op"] not in {"<", "<=", "==", ">=", ">"}
        ):
            raise ValueError("Invalid parameter constraint.")
        right = constraint["right"]
        if isinstance(right, str):
            if right not in definitions:
                raise ValueError("Constraint refers to an unknown parameter.")
        elif isinstance(right, bool) or not isinstance(right, (int, float)) or not math.isfinite(right):
            raise ValueError("Constraint right side must be a parameter name or finite number.")
        clean_constraints.append(dict(constraint))
    result = {"parameters": normalized, "constraints": clean_constraints}
    if not parameters:
        result["no_tunable_parameters"] = True
    joint_variants = schema.get("joint_variants", [])
    if not isinstance(joint_variants, list) or len(joint_variants) > 10:
        raise ValueError("Joint variants must be a list of at most ten entries.")
    if joint_variants:
        result["joint_variants"] = []
        for variant in joint_variants:
            if (
                not isinstance(variant, Mapping) or set(variant) != {"parameters", "reason"}
                or not isinstance(variant["parameters"], Mapping)
                or len(variant["parameters"]) < 2
                or not set(variant["parameters"]) <= set(definitions)
                or not isinstance(variant["reason"], str) or not variant["reason"].strip()
            ):
                raise ValueError("Joint variants require at least two declared parameters and a reason.")
            for name, value in variant["parameters"].items():
                if (
                    isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or (definitions[name]["type"] == "integer" and not isinstance(value, int))
                ):
                    raise ValueError(f"Invalid joint variant value for {name}.")
            result["joint_variants"].append({
                "parameters": dict(variant["parameters"]), "reason": variant["reason"].strip(),
            })
    if not parameter_constraints_hold(parameters, clean_constraints):
        raise ValueError("Strategy parameters violate their declared joint constraints.")
    return result


def parameter_constraints_hold(parameters: Mapping[str, object], constraints: list) -> bool:
    for constraint in constraints:
        left = parameters[constraint["left"]]
        right = constraint["right"]
        right = parameters[right] if isinstance(right, str) else right
        op = constraint["op"]
        if not {
            "<": left < right, "<=": left <= right, "==": left == right,
            ">=": left >= right, ">": left > right,
        }[op]:
            return False
    return True


def strategy_fingerprint(spec: Mapping[str, object]) -> str:
    normalized = normalize_strategy_spec(spec)
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def require_execution_permission(
    spec: Mapping[str, object], *, isolated_python: bool
) -> None:
    if spec.get("family") == "python" and not isolated_python:
        raise ValueError(
            "Generated Python requires --isolated-generated-python; local execution is disabled."
        )


def make_strategy(
    spec: Mapping[str, object],
    *,
    isolated_python: bool = False,
    isolation_limits: IsolationLimits | None = None,
    evidence_callback: Callable[[dict[str, Any]], None] | None = None,
    container_executor: ContainerStrategyExecutor | None = None,
) -> Strategy:
    normalized = normalize_strategy_spec(spec)
    family = normalized["family"]
    params = normalized["parameters"]
    if family == "python":
        if container_executor is not None:
            return _container_python_strategy(
                str(normalized["source"]),
                dict(params),
                container_executor,
                evidence_callback,
            )
        require_execution_permission(normalized, isolated_python=isolated_python)

        def strategy(history: pd.DataFrame, state: Mapping[str, object]):
            return run_isolated_strategy(
                str(normalized["source"]),
                dict(params),
                history,
                dict(state),
                symbols=ETF_SYMBOLS,
                limits=isolation_limits,
                evidence_callback=evidence_callback,
            )

        return strategy
    if family == "equal_weight":
        return equal_weight_buy_and_hold
    if family == "momentum":
        return _momentum_strategy(
            int(params["lookback"]),
            int(params["top_k"]),
            bool(params["cash_if_negative"]),
        )
    if family == "trend_filter":
        return _trend_filter_strategy(int(params["lookback"]))
    if family == "inverse_volatility":
        return _inverse_volatility_strategy(int(params["lookback"]))
    raise ValueError(f"Unsupported strategy family: {family!r}.")


def _container_python_strategy(
    source: str,
    parameters: dict[str, Any],
    executor: ContainerStrategyExecutor,
    evidence_callback: Callable[[dict[str, Any]], None] | None,
) -> Strategy:
    """Run the generated strategy in the verified container boundary."""

    def strategy(history: pd.DataFrame, state: Mapping[str, object]):
        try:
            result = executor.run(source, parameters, history, dict(state))
        except IsolationError as error:
            if evidence_callback is not None and error.evidence is not None:
                evidence_callback(dict(error.evidence))
            raise
        if evidence_callback is not None:
            evidence_callback(dict(result.evidence))
        return result.weights

    return strategy


def _integer_parameter(
    params: Mapping[str, object],
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = params.get(name, default)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"{name} must be an integer.")
    if not minimum <= raw <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return raw


def _cash() -> dict[str, float]:
    return {**{symbol: 0.0 for symbol in ETF_SYMBOLS}, CASH: 1.0}


def _momentum_strategy(lookback: int, top_k: int, cash_if_negative: bool) -> Strategy:
    def strategy(history: pd.DataFrame, state: Mapping[str, object]):
        scores: list[tuple[str, float]] = []
        for symbol in ETF_SYMBOLS:
            closes = _recent_closes(history, symbol)
            if len(closes) <= lookback:
                continue
            score = float(closes.iloc[-1] / closes.iloc[-lookback - 1] - 1.0)
            scores.append((symbol, score))
        if not scores:
            return _cash()
        selected = sorted(scores, key=lambda item: (-item[1], item[0]))[:top_k]
        if cash_if_negative and all(score <= 0 for _, score in selected):
            return _cash()
        weight = 1.0 / len(selected)
        return {
            **{symbol: weight if symbol in {item[0] for item in selected} else 0.0 for symbol in ETF_SYMBOLS},
            CASH: 0.0,
        }

    return strategy


def _trend_filter_strategy(lookback: int) -> Strategy:
    def strategy(history: pd.DataFrame, state: Mapping[str, object]):
        eligible = []
        for symbol in ETF_SYMBOLS:
            closes = _recent_closes(history, symbol)
            if len(closes) >= lookback and closes.iloc[-1] > float(closes.iloc[-lookback:].mean()):
                eligible.append(symbol)
        if not eligible:
            return _cash()
        weight = 1.0 / len(eligible)
        return {
            **{symbol: weight if symbol in eligible else 0.0 for symbol in ETF_SYMBOLS},
            CASH: 0.0,
        }

    return strategy


def _inverse_volatility_strategy(lookback: int) -> Strategy:
    def strategy(history: pd.DataFrame, state: Mapping[str, object]):
        inverse_volatility: dict[str, float] = {}
        for symbol in ETF_SYMBOLS:
            closes = _recent_closes(history, symbol)
            returns = closes.pct_change().dropna().tail(lookback)
            if len(returns) < lookback:
                continue
            volatility = float(np.std(returns.to_numpy(dtype=float), ddof=1))
            if math.isfinite(volatility) and volatility > 0:
                inverse_volatility[symbol] = 1.0 / volatility
        total = sum(inverse_volatility.values())
        if not total:
            return _cash()
        return {
            **{
                symbol: inverse_volatility.get(symbol, 0.0) / total
                for symbol in ETF_SYMBOLS
            },
            CASH: 0.0,
        }

    return strategy


def _recent_closes(history: pd.DataFrame, symbol: str) -> pd.Series:
    return (
        history.loc[(history["symbol"] == symbol) & history["close"].notna()]
        .sort_values("date")["close"]
        .astype(float)
    )
