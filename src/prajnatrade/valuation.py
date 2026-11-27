"""Shared carry-forward valuation primitives for Discovery, Formal Validation, and Shadow Live (ADR 0003)."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any

from prajnatrade.trading_status import (
    CN_TZ,
    ETF_SYMBOLS,
    confirmed_no_trade_shape,
    halt_conflicts_with_trade,
)

EPSILON = 1e-9


def parse_instant(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp with timezone offset, or return None."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def instant_by(value: object, cutoff: datetime) -> bool:
    """Return whether ``value`` is a timezone-aware ISO timestamp at or before ``cutoff``."""
    parsed = parse_instant(value)
    return parsed is not None and parsed <= cutoff


def available_by(row: Mapping[str, Any], cutoff: datetime) -> bool:
    """Return whether ``row`` has non-empty availability_evidence and available_at <= cutoff."""
    evidence = row.get("availability_evidence")
    if not isinstance(evidence, str) or not evidence.strip():
        return False
    return instant_by(row.get("available_at"), cutoff)


def valid_price(value: object) -> bool:
    """Return whether ``value`` is a finite positive number (not a bool)."""
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value > 0
    )


def close_available(row: Mapping[str, Any], cutoff: datetime, data_kind: str) -> bool:
    """Return whether the row's close price is available by ``cutoff``."""
    if data_kind == "real":
        return instant_by(row.get("close_available_at"), cutoff)
    if row.get("close_available_at") is not None:
        evidence = row.get("availability_evidence")
        return (
            isinstance(evidence, str)
            and bool(evidence.strip())
            and instant_by(row.get("close_available_at"), cutoff)
        )
    return available_by(row, cutoff)


def close_source_evidence(row: Mapping[str, Any], *, data_kind: str) -> dict[str, Any]:
    """Extract close-source audit evidence from a raw bar."""
    available_at = (
        row.get("close_available_at")
        if data_kind == "real" or row.get("close_available_at") is not None
        else row.get("available_at")
    )
    evidence: dict[str, Any] = {
        "availability_evidence": row.get("availability_evidence"),
        "available_at": available_at,
    }
    open_source = row.get("open_source")
    if isinstance(open_source, Mapping):
        evidence["source"] = open_source.get("source")
        evidence["evidence_id"] = open_source.get("evidence_id")
    return evidence


def status_source_evidence(row: Mapping[str, Any]) -> dict[str, Any]:
    """Extract trading-status audit evidence from a raw bar."""
    evidence: dict[str, Any] = {
        "trading_status": row.get("trading_status"),
        "availability_evidence": row.get("availability_evidence"),
        "available_at": row.get("available_at"),
    }
    status_id = row.get("status_evidence_id")
    if isinstance(status_id, str) and status_id.strip():
        evidence["status_evidence_id"] = status_id
    halt = row.get("halt")
    if isinstance(halt, Mapping):
        evidence["halt"] = dict(halt)
    return evidence


def classify_no_trade_for_valuation(
    row: Mapping[str, Any] | None,
    close_cutoff: datetime,
    *,
    data_kind: str,
    schema_version: int | None = None,
) -> tuple[bool, str | None]:
    """Return whether ``row`` confirms full-day no-trade by ``close_cutoff``.

    When False, returns a specific ``valuation_gap_reason`` distinguishing
    missing bars, unknown status, late notices, conflicting evidence and
    resumed partial halts without a confirmed close.
    """
    if row is None:
        return False, "missing_bar"
    confirmed_shape, shape_reason = confirmed_no_trade_shape(
        row, data_kind=data_kind, schema_version=schema_version
    )
    if confirmed_shape:
        if not available_by(row, close_cutoff):
            available_at = parse_instant(row.get("available_at"))
            if available_at is not None and available_at > close_cutoff:
                return False, "status_evidence_after_cutoff"
            return False, "missing_status_availability_evidence"
        return True, None
    if shape_reason is not None:
        return False, shape_reason
    if row.get("trading_status") == "trading" and valid_price(row.get("close")):
        ts_field = (
            "close_available_at"
            if data_kind == "real" or row.get("close_available_at") is not None
            else "available_at"
        )
        available_at = parse_instant(row.get(ts_field))
        if available_at is not None and available_at > close_cutoff:
            return False, "close_evidence_after_cutoff"
        return False, "missing_close_availability_evidence"
    return False, "missing_or_unconfirmed_unadjusted_close"


@dataclass
class CarryState:
    """Per-symbol carry-forward valuation anchor and chain-break status."""

    anchor: dict[str, Any] | None = None
    chain_broken: bool = False

    def reset(self) -> None:
        self.anchor = None
        self.chain_broken = False

    def break_chain(self) -> None:
        if self.anchor is not None:
            self.chain_broken = True
        self.anchor = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "anchor": (
                {
                    **self.anchor,
                    "source_close_evidence": dict(self.anchor["source_close_evidence"]),
                    "adjustments": [dict(item) for item in self.anchor["adjustments"]],
                }
                if self.anchor is not None
                else None
            ),
            "chain_broken": self.chain_broken,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> CarryState:
        if not isinstance(payload, Mapping):
            return cls()
        raw_anchor = payload.get("anchor")
        anchor = (
            {
                **raw_anchor,
                "source_close_evidence": dict(raw_anchor.get("source_close_evidence") or {}),
                "adjustments": [dict(item) for item in raw_anchor.get("adjustments") or []],
            }
            if isinstance(raw_anchor, Mapping)
            else None
        )
        return cls(anchor=anchor, chain_broken=bool(payload.get("chain_broken", False)))


def missing_holding(
    quantity: float, gap_reason: str | None, *, close: float | None = None
) -> dict[str, Any]:
    """Return a holding valuation record for an unvalued position."""
    return {
        "quantity": quantity,
        "close": close,
        "market_value": None,
        "valuation_price": None,
        "valuation_basis": "missing",
        "gap_reason": gap_reason,
    }


def valuation_gap(
    day: str,
    symbol: str,
    gap_reason: str | None,
    *,
    status: str | None = None,
    row: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a structured valuation gap record."""
    gap: dict[str, Any] = {
        "date": day,
        "symbol": symbol,
        "reason": "missing_or_unconfirmed_unadjusted_close",
        "valuation_gap_reason": gap_reason,
    }
    if status is not None:
        gap["trading_status"] = status
    if row is not None and isinstance(row.get("halt"), dict):
        gap["halt"] = row["halt"]
    return gap


def adjust_carried_basis_for_actions(
    day: str,
    anchor: dict[str, Any],
    applied_actions: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str | None]:
    """Adjust a carried-forward valuation anchor for corporate actions on ``day``."""
    if not applied_actions:
        return anchor, None
    price = float(anchor["valuation_price"])
    adjustments = [dict(item) for item in anchor["adjustments"]]
    for action in applied_actions:
        action_type = action.get("type")
        if action_type == "cash_dividend":
            amount = float(action["cash_per_share"])
            new_price = price - amount
            extra: dict[str, Any] = {
                "cash_per_share": amount,
                "pay_date": action["pay_date"],
            }
        elif action_type == "share_adjustment":
            ratio = float(action["ratio"])
            new_price = price / ratio
            extra = {"ratio": ratio}
        else:
            return None, "suspended_corporate_action_unadjusted"
        if not valid_price(new_price) or new_price <= EPSILON:
            return None, "carried_valuation_price_non_positive"
        entry: dict[str, Any] = {
            "date": day,
            "type": action_type,
            **extra,
            "terms_evidence": action["terms_evidence"],
            "available_at": action["available_at"],
            "availability_evidence": action["availability_evidence"],
            "valuation_price_before": price,
            "valuation_price_after": new_price,
        }
        if "sequence" in action:
            entry["sequence"] = action["sequence"]
        adjustments.append(entry)
        price = new_price
    updated = dict(anchor)
    updated["valuation_price"] = price
    updated["adjustments"] = adjustments
    return updated, None


def value_carry_forward_session(
    day: str,
    raw_bars: Mapping[tuple[str, str], Mapping[str, Any]] | Mapping[str, Mapping[str, Any] | None],
    shares: Mapping[str, float],
    cash: float,
    receivables: list[dict[str, Any]],
    unresolved: Mapping[str, list[dict[str, str]]],
    carry_states: dict[str, CarryState],
    applied_actions_today: Mapping[str, list[dict[str, Any]]],
    *,
    data_kind: str = "synthetic",
    schema_version: int | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Value holdings under ``carry-forward-suspended-v1`` (ADR 0003).

    Accepts ``raw_bars`` keyed by either ``(day, symbol)`` (Discovery/Formal) or
    ``symbol`` (Shadow Live single-session).
    """
    holdings: dict[str, Any] = {}
    gaps: list[dict[str, Any]] = []
    carried_symbols: list[str] = []
    market_value = 0.0
    valid = True
    allow_partial_halt = data_kind == "real" or (
        schema_version is not None and schema_version >= 3
    )
    close_cutoff = datetime.combine(
        datetime.fromisoformat(day).date(), time(23, 59, 59), tzinfo=CN_TZ
    )
    for symbol in ETF_SYMBOLS:
        quantity = float(shares[symbol])
        state = carry_states[symbol]
        row = raw_bars.get((day, symbol))  # type: ignore[arg-type]
        if row is None and symbol in raw_bars:
            row = raw_bars.get(symbol)  # type: ignore[arg-type]
        status = row.get("trading_status") if row is not None else None
        markable = status == "trading" or (
            allow_partial_halt and status == "partial_halt"
        )
        close_conflict = row is not None and (
            row.get("status_conflict") is not None
            or row.get("close_conflict") is not None
            or (
                allow_partial_halt
                and status == "partial_halt"
                and halt_conflicts_with_trade(row.get("halt"), day, row)
            )
        )
        raw_close = row.get("close") if row is not None else None
        has_trusted_close = (
            row is not None
            and markable
            and not close_conflict
            and valid_price(raw_close)
            and close_available(row, close_cutoff, data_kind)
        )

        if quantity <= EPSILON:
            state.reset()
            trusted_close = float(raw_close) if has_trusted_close else None
            if unresolved.get(symbol):
                valid = False
                for action_gap in unresolved[symbol]:
                    gaps.append({"symbol": symbol, **action_gap})
                holdings[symbol] = missing_holding(
                    quantity, unresolved[symbol][0]["reason"], close=trusted_close
                )
            else:
                holdings[symbol] = {
                    "quantity": quantity,
                    "close": trusted_close,
                    "market_value": 0.0,
                    "valuation_price": None,
                    "valuation_basis": "not_held",
                }
            continue

        if has_trusted_close:
            actual_close = float(raw_close)
            state.anchor = {
                "source_close_date": day,
                "source_close_price": actual_close,
                "source_close_evidence": close_source_evidence(row, data_kind=data_kind),
                "valuation_price": actual_close,
                "carried_sessions": 0,
                "adjustments": [],
            }
            state.chain_broken = False
            if unresolved.get(symbol):
                valid = False
                for action_gap in unresolved[symbol]:
                    gaps.append({"symbol": symbol, **action_gap})
                holdings[symbol] = missing_holding(
                    quantity, unresolved[symbol][0]["reason"], close=actual_close
                )
            else:
                value = quantity * actual_close
                market_value += value
                holdings[symbol] = {
                    "quantity": quantity,
                    "close": actual_close,
                    "market_value": value,
                    "valuation_price": actual_close,
                    "valuation_basis": "actual_close",
                }
            continue

        confirmed_no_trade, non_trade_reason = classify_no_trade_for_valuation(
            row, close_cutoff, data_kind=data_kind, schema_version=schema_version
        )
        if confirmed_no_trade:
            if unresolved.get(symbol):
                valid = False
                state.break_chain()
                for action_gap in unresolved[symbol]:
                    gaps.append({"symbol": symbol, **action_gap})
                holdings[symbol] = missing_holding(
                    quantity, unresolved[symbol][0]["reason"]
                )
                continue

            anchor = state.anchor
            if anchor is None:
                valid = False
                gap_reason = (
                    "carry_forward_chain_broken"
                    if state.chain_broken
                    else "missing_authorized_prior_close"
                )
                gaps.append(valuation_gap(day, symbol, gap_reason, status=status))
                holdings[symbol] = missing_holding(quantity, gap_reason)
                continue

            day_actions = applied_actions_today.get(symbol, [])
            if day_actions:
                adjusted, adj_gap_reason = adjust_carried_basis_for_actions(
                    day, anchor, day_actions
                )
                if adjusted is None:
                    valid = False
                    state.break_chain()
                    gaps.append(valuation_gap(day, symbol, adj_gap_reason, status=status))
                    holdings[symbol] = missing_holding(quantity, adj_gap_reason)
                    continue
                anchor = adjusted
                state.anchor = anchor

            anchor["carried_sessions"] += 1
            valuation_price = float(anchor["valuation_price"])
            value = quantity * valuation_price
            market_value += value
            carried_symbols.append(symbol)
            holdings[symbol] = {
                "quantity": quantity,
                "close": None,
                "market_value": value,
                "valuation_price": valuation_price,
                "valuation_basis": "carried_forward_prior_close",
                "carry_forward": {
                    "source_close_date": anchor["source_close_date"],
                    "source_close_price": anchor["source_close_price"],
                    "source_close_evidence": dict(anchor["source_close_evidence"]),
                    "valuation_price": valuation_price,
                    "carried_sessions": anchor["carried_sessions"],
                    "adjustments": [dict(item) for item in anchor["adjustments"]],
                    "status_evidence": status_source_evidence(row),
                },
            }
            continue

        # Not a trusted close and not a confirmed full-day no-trade: gap & break chain.
        valid = False
        state.break_chain()
        gaps.append(valuation_gap(day, symbol, non_trade_reason, status=status, row=row))
        if unresolved.get(symbol):
            for action_gap in unresolved[symbol]:
                gaps.append({"symbol": symbol, **action_gap})
        holdings[symbol] = missing_holding(quantity, non_trade_reason)

    receivable_total = math.fsum(item["amount"] for item in receivables)
    nav = cash + market_value + receivable_total if valid else None
    day_basis = (
        "missing"
        if not valid
        else "carried_forward"
        if carried_symbols
        else "actual_close"
    )
    return {
        "date": day,
        "nav": nav,
        "cash": cash,
        "market_value": market_value if valid else None,
        "dividend_receivables": receivable_total if valid else None,
        "receivables": [dict(item) for item in receivables],
        "holdings": holdings,
        "valuation_basis": day_basis,
        "carried_forward_symbols": sorted(carried_symbols),
    }, gaps
