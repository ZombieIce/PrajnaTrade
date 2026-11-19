from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping

import numpy as np
import pandas as pd

from prajnatrade.backtest import Strategy, equal_weight_buy_and_hold
from prajnatrade.data import CASH, ETF_SYMBOLS

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
        return {"family": "python", "parameters": parameters, "source": source}
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


def strategy_fingerprint(spec: Mapping[str, object]) -> str:
    normalized = normalize_strategy_spec(spec)
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def require_execution_permission(
    spec: Mapping[str, object], *, trusted_local_python: bool
) -> None:
    if spec.get("family") == "python" and not trusted_local_python:
        raise ValueError(
            "Generated Python requires explicit trusted-local opt-in; it is not sandboxed."
        )


def make_strategy(
    spec: Mapping[str, object], *, trusted_local_python: bool = False
) -> Strategy:
    normalized = normalize_strategy_spec(spec)
    family = normalized["family"]
    params = normalized["parameters"]
    if family == "python":
        require_execution_permission(normalized, trusted_local_python=trusted_local_python)
        namespace = {"ETF_SYMBOLS": ETF_SYMBOLS, "CASH": CASH, "parameters": params}
        try:
            exec(compile(normalized["source"], "<generated-strategy>", "exec"), namespace)
        except Exception as error:
            raise ValueError(f"Generated strategy initialization failed: {error}") from error
        generated = namespace.get("strategy")
        if not callable(generated):
            raise ValueError("Generated Python must define strategy(history, state).")

        def strategy(history: pd.DataFrame, state: Mapping[str, object]):
            try:
                return generated(history, state)
            except Exception as error:
                raise ValueError(f"Generated strategy failed: {error}") from error

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
