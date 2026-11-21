#!/usr/bin/env python3
"""Independent machine cross-check of the sealed real Discovery baseline.

This script deliberately does **not** import ``prajnatrade.snapshot_baseline``
(or any other package module): it is a second implementation of the same
economic rules, written from the sealed snapshot JSON so that a shared-code bug
cannot hide in both runs.  It is a machine cross-check, not the independent
human reconciliation that issue #24 requires.

What it does:

1. verifies the snapshot ID and the Discovery partition content hash;
2. re-derives ``tr-reinvest-exclose-v1`` from raw closes + official dividends
   and compares every derived value with the sealed partition (1e-6 relative);
3. recomputes the one equal-weight buy at the 2022-05-23 open, frozen costs,
   unadjusted closes, dividend receivables and suspended NAV gaps, using its own
   implementation;
4. compares trades, fees, dividend cash, the NAV path, gap sets and the ending
   NAV with the sealed baseline JSON, under pre-registered tolerances;
5. optionally (``--input``) repeats the full-window 2022-05-20..2026-11-20 run
   on the pre-seal input, whose sole purpose is to check the research note's
   ~176,741.79 sanity figure and its fee-accounting difference.

Pre-registered tolerances (fixed before the comparison was run):

* derived values: 1e-6 relative;
* NAV path / ending NAV: 1e-4 relative (or 1e-4 CNY absolute for a tiny NAV);
* trade count and traded symbols/dates: exact;
* fill quantity/price/notional: 1e-9 relative; fill fee: 1e-6 CNY absolute;
* dividend cash: 1e-6 CNY absolute;
* NAV gap sets: exact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

CN_TZ = timezone(timedelta(hours=8))
SYMBOLS = ("511260", "518880", "515080", "513300", "159612")
WEIGHTS = {symbol: 1.0 / len(SYMBOLS) for symbol in SYMBOLS}
EPSILON = 1e-12
RESEARCH_FULL_WINDOW_NAV = 176_741.79
TOLERANCES = {
    "derived_relative": 1e-6,
    "nav_relative": 1e-4,
    "nav_absolute_cny": 1e-4,
    "trade_count_and_keys": "exact",
    "fill_quantity_price_notional_relative": 1e-9,
    "fill_fee_absolute_cny": 1e-6,
    "dividend_cash_absolute_cny": 1e-6,
    "nav_gap_sets": "exact",
    "research_sanity_absolute_cny": 0.01,
}


class ReconcileError(RuntimeError):
    """The sealed snapshot or the reconciliation input is inconsistent."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _instant(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _available(value: object, cutoff: datetime) -> bool:
    parsed = _instant(value)
    return parsed is not None and parsed <= cutoff


def _valid(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value > 0
    )


def _open_cutoff(day: str) -> datetime:
    return datetime.combine(datetime.fromisoformat(day).date(), time(9, 30), tzinfo=CN_TZ)


def _close_cutoff(day: str) -> datetime:
    return datetime.combine(datetime.fromisoformat(day).date(), time(23, 59, 59), tzinfo=CN_TZ)


def _index(data: dict[str, Any]) -> tuple[
    list[str], dict[tuple[str, str], dict], dict[tuple[str, str], dict], dict[str, list[dict]]
]:
    sessions = sorted({row["date"] for row in data["calendar"]})
    raw = {(row["date"], row["symbol"]): row for row in data["raw_bars"]}
    derived = {(row["date"], row["symbol"]): row for row in data["derived"]}
    actions: dict[str, list[dict]] = {}
    for row in data["corporate_actions"]:
        actions.setdefault(row["date"], []).append(row)
    return sessions, raw, derived, actions


def _signal(sessions: list[str], derived: dict[tuple[str, str], dict]) -> str:
    """First session whose derived window is fully available before the next open."""
    for index, day in enumerate(sessions[:-1]):
        cutoff = _open_cutoff(sessions[index + 1])
        if all(
            (row := derived.get((day, symbol))) is not None
            and row.get("value") is not None
            and _available(row.get("available_at"), cutoff)
            for symbol in SYMBOLS
        ):
            return sessions[index + 1]
    raise ReconcileError("No executable equal-weight signal was found")


def _buy(
    day: str,
    raw: dict[tuple[str, str], dict],
    cash: float,
    fee_rate: float,
    slippage_rate: float,
    *,
    fee_from_cash: bool,
) -> tuple[list[dict[str, Any]], float, dict[str, float], float, float]:
    """Equal-weight buy at ``day``'s unadjusted open.

    ``fee_from_cash=True`` is the sealed engine's accounting: the fee leaves the
    cash balance.  ``fee_from_cash=False`` reproduces the research probe script,
    which left the fee in cash; it exists only to reconcile the note's sanity
    number.
    """
    cutoff = _open_cutoff(day)
    # A per-symbol cash budget: notional + fee must fit inside the budget, and
    # the execution price includes slippage.  This is algebraically the same
    # economics as the sealed engine but a different order of operations.
    budget = cash / len(SYMBOLS)
    fills = []
    shares: dict[str, float] = {}
    fees_paid = slippage_paid = total_notional = total_fee = 0.0
    for symbol in SYMBOLS:
        row = raw.get((day, symbol))
        if (
            row is None
            or row.get("trading_status") != "trading"
            or not _valid(row.get("open"))
            or not _valid(row.get("volume"))
            or not _available(row.get("open_available_at"), cutoff)
        ):
            raise ReconcileError(f"Open is not executable for {symbol} on {day}")
        raw_open = float(row["open"])
        execution = raw_open * (1.0 + slippage_rate)
        notional = budget / (1.0 + fee_rate)
        quantity = notional / execution
        fee = notional * fee_rate
        slippage = quantity * (execution - raw_open)
        shares[symbol] = quantity
        total_notional += notional
        total_fee += fee
        fees_paid += fee
        slippage_paid += slippage
        fills.append(
            {
                "date": day,
                "symbol": symbol,
                "quantity": quantity,
                "raw_open": raw_open,
                "price": execution,
                "notional": notional,
                "fee": fee,
                "slippage": slippage,
            }
        )
    cash -= total_notional + (total_fee if fee_from_cash else 0.0)
    if abs(cash) <= EPSILON * max(1.0, total_notional):
        cash = 0.0
    return fills, cash, shares, fees_paid, slippage_paid


def _simulate(
    sessions: list[str],
    raw: dict[tuple[str, str], dict],
    actions_by_day: dict[str, list[dict]],
    *,
    initial_cash: float,
    transaction_cost_bps: float,
    slippage_bps: float,
    fee_from_cash: bool = True,
) -> dict[str, Any]:
    fee_rate = transaction_cost_bps / 10_000.0
    slippage_rate = slippage_bps / 10_000.0
    fill_day = _signal(sessions, _index_derived_only(sessions, raw, actions_by_day))
    cash = float(initial_cash)
    shares = {symbol: 0.0 for symbol in SYMBOLS}
    receivables: list[dict[str, Any]] = []
    fills: list[dict[str, Any]] = []
    fees_paid = slippage_paid = 0.0
    dividends_paid = 0.0
    equity: list[dict[str, Any]] = []
    nav_gaps: list[str] = []
    raw_gaps: list[dict[str, Any]] = []
    filled = False
    for day in sessions:
        for action in actions_by_day.get(day, []):
            symbol = action["symbol"]
            if shares[symbol] <= EPSILON:
                continue
            if not _available(action.get("available_at"), _open_cutoff(day)):
                raise ReconcileError(f"Corporate action terms unavailable on {day}")
            if not isinstance(action.get("terms_evidence"), str) or not action["terms_evidence"].strip():
                raise ReconcileError(f"Corporate action terms missing on {day}")
            if action["type"] == "cash_dividend":
                amount = action.get("cash_per_share")
                pay_date = action.get("pay_date")
                if not _valid(amount) or not isinstance(pay_date, str) or not pay_date:
                    raise ReconcileError(f"Cash dividend terms invalid on {day}")
                receivables.append(
                    {
                        "symbol": symbol,
                        "ex_date": day,
                        "pay_date": pay_date,
                        "quantity": shares[symbol],
                        "amount": shares[symbol] * float(amount),
                    }
                )
            else:
                raise ReconcileError("Share adjustments are not supported by this check")
        for item in list(receivables):
            if item["pay_date"] <= day:
                cash += item["amount"]
                dividends_paid += item["amount"]
                receivables.remove(item)
        if day == fill_day:
            fills, cash, shares, fees_paid, slippage_paid = _buy(
                day, raw, cash, fee_rate, slippage_rate, fee_from_cash=fee_from_cash
            )
            filled = True
        market_value = 0.0
        valid = True
        for symbol in SYMBOLS:
            row = raw.get((day, symbol))
            quantity = shares[symbol]
            if quantity <= EPSILON:
                continue
            status = row.get("trading_status") if row is not None else None
            close = row.get("close") if row is not None else None
            if (
                row is None
                or status not in ("trading", "partial_halt")
                or not _valid(close)
                or not _available(row.get("close_available_at"), _close_cutoff(day))
            ):
                valid = False
                raw_gaps.append(
                    {
                        "date": day,
                        "symbol": symbol,
                        "trading_status": status,
                        "reason": "missing_or_unconfirmed_unadjusted_close",
                    }
                )
                continue
            market_value += quantity * float(close)
        receivable_total = math.fsum(item["amount"] for item in receivables)
        nav = cash + market_value + receivable_total if valid else None
        if nav is None:
            nav_gaps.append(day)
        equity.append(
            {
                "date": day,
                "nav": nav,
                "cash": cash,
                "market_value": market_value if valid else None,
                "dividend_receivables": receivable_total if valid else None,
            }
        )
    if not filled:
        raise ReconcileError("The equal-weight buy never filled")
    valued = [point["nav"] for point in equity if point["nav"] is not None]
    return {
        "fill_date": fill_day,
        "fills": fills,
        "fees_paid": fees_paid,
        "slippage_paid": slippage_paid,
        "dividend_cash_paid": dividends_paid,
        "equity": equity,
        "nav_gaps": nav_gaps,
        "raw_gaps": raw_gaps,
        "valued_sessions": len(valued),
        "ending_equity": equity[-1]["nav"],
        "total_return": equity[-1]["nav"] / initial_cash - 1.0 if equity[-1]["nav"] else None,
    }


def _index_derived_only(
    sessions: list[str],
    raw: dict[tuple[str, str], dict],
    actions_by_day: dict[str, list[dict]],
) -> dict[tuple[str, str], dict]:
    """Recompute the derived series from the supplied raw bars and actions."""
    closes: dict[str, dict[str, float]] = {symbol: {} for symbol in SYMBOLS}
    for (day, symbol), row in raw.items():
        if _valid(row.get("close")):
            closes[symbol][day] = float(row["close"])
    dividends: dict[tuple[str, str], float] = {}
    for day, rows in actions_by_day.items():
        for row in rows:
            if row["type"] == "cash_dividend":
                dividends[(day, row["symbol"])] = float(row["cash_per_share"])
    derived: dict[tuple[str, str], dict] = {}
    for symbol in SYMBOLS:
        last_value: float | None = None
        last_close: float | None = None
        for day in sessions:
            close = closes[symbol].get(day)
            if close is None:
                value = None
            elif last_value is None or last_close is None:
                value = 1.0
            else:
                value = last_value * (close + dividends.get((day, symbol), 0.0)) / last_close
            if close is not None:
                last_value, last_close = value, close
            derived[(day, symbol)] = {
                "value": value,
                "available_at": f"{day}T15:00:00+08:00",
            }
    return derived


def _rel_delta(actual: float, expected: float) -> float:
    scale = max(abs(expected), 1e-300)
    return abs(actual - expected) / scale


def _compare_to_baseline(
    computed: dict[str, Any], baseline: dict[str, Any], label: str
) -> dict[str, Any]:
    baseline_navs = {point["date"]: point["nav"] for point in baseline["equity_curve"]}
    computed_navs = {point["date"]: point["nav"] for point in computed["equity"]}
    if set(baseline_navs) != set(computed_navs):
        raise ReconcileError(f"{label}: NAV dates differ from the sealed baseline")
    nav_deltas = []
    for day, expected in baseline_navs.items():
        actual = computed_navs[day]
        if expected is None or actual is None:
            if expected is not None or actual is not None:
                raise ReconcileError(f"{label}: NAV availability differs on {day}")
            continue
        nav_deltas.append(
            {
                "date": day,
                "delta": actual - expected,
                "relative": _rel_delta(actual, expected),
            }
        )
    max_nav_abs = max((abs(item["delta"]) for item in nav_deltas), default=0.0)
    max_nav_rel = max((item["relative"] for item in nav_deltas), default=0.0)
    baseline_nav_gaps = sorted(
        point["date"] for point in baseline["equity_curve"] if point["nav"] is None
    )
    fill_keys = [(fill["date"], fill["symbol"]) for fill in baseline["fills"]]
    computed_keys = [(fill["date"], fill["symbol"]) for fill in computed["fills"]]
    if len(fill_keys) != len(computed_keys):
        raise ReconcileError(f"{label}: fill counts differ")
    quantity_price_notional_ok = True
    fee_ok = True
    max_fill_rel = 0.0
    max_fee_abs = 0.0
    for expected, actual in zip(baseline["fills"], computed["fills"], strict=True):
        for field in ("quantity", "raw_open", "price", "notional"):
            delta = _rel_delta(float(actual[field]), float(expected[field]))
            max_fill_rel = max(max_fill_rel, delta)
            quantity_price_notional_ok = quantity_price_notional_ok and (
                delta <= TOLERANCES["fill_quantity_price_notional_relative"]
            )
        fee_delta = abs(float(actual["fee"]) - float(expected["fee"]))
        max_fee_abs = max(max_fee_abs, fee_delta)
        fee_ok = fee_ok and fee_delta <= TOLERANCES["fill_fee_absolute_cny"]
    baseline_cash = baseline["equity_curve"][-1]["cash"]
    dividend_delta = abs(computed["dividend_cash_paid"] - baseline_cash)
    end_nav_delta = abs(float(computed["ending_equity"]) - float(baseline["ending_equity"]))
    checks = {
        "fill_date": computed["fill_date"] == "2022-05-23",
        "trade_count_and_keys": fill_keys == computed_keys,
        "fill_values": quantity_price_notional_ok,
        "fill_fees": fee_ok
        and abs(computed["fees_paid"] - baseline["metrics"]["fees_paid"])
        <= TOLERANCES["fill_fee_absolute_cny"],
        "fill_slippage": abs(
            computed["slippage_paid"] - baseline["metrics"]["slippage_paid"]
        )
        <= TOLERANCES["fill_fee_absolute_cny"],
        "dividend_cash": dividend_delta <= TOLERANCES["dividend_cash_absolute_cny"],
        "nav_path": max_nav_abs <= TOLERANCES["nav_absolute_cny"]
        or max_nav_rel <= TOLERANCES["nav_relative"],
        "nav_gap_sets": baseline_nav_gaps == sorted(computed["nav_gaps"]),
        "ending_nav": end_nav_delta <= TOLERANCES["nav_absolute_cny"]
        or _rel_delta(float(computed["ending_equity"]), float(baseline["ending_equity"]))
        <= TOLERANCES["nav_relative"],
    }
    return {
        "label": label,
        "sessions": len(computed["equity"]),
        "baseline": {
            "fill_date": baseline["fills"][0]["date"] if baseline["fills"] else None,
            "fills": len(baseline["fills"]),
            "fees_paid": baseline["metrics"]["fees_paid"],
            "slippage_paid": baseline["metrics"]["slippage_paid"],
            "dividend_cash": baseline_cash,
            "nav_gaps": baseline_nav_gaps,
            "valued_sessions": sum(
                1 for point in baseline["equity_curve"] if point["nav"] is not None
            ),
            "ending_equity": baseline["ending_equity"],
        },
        "recomputed": {
            "fill_date": computed["fill_date"],
            "fills": len(computed["fills"]),
            "fees_paid": computed["fees_paid"],
            "slippage_paid": computed["slippage_paid"],
            "dividend_cash": computed["dividend_cash_paid"],
            "nav_gaps": computed["nav_gaps"],
            "valued_sessions": computed["valued_sessions"],
            "ending_equity": computed["ending_equity"],
        },
        "deltas": {
            "max_nav_absolute_cny": max_nav_abs,
            "max_nav_relative": max_nav_rel,
            "end_nav_absolute_cny": end_nav_delta,
            "fees_absolute_cny": abs(
                computed["fees_paid"] - baseline["metrics"]["fees_paid"]
            ),
            "dividend_cash_absolute_cny": dividend_delta,
            "max_fill_relative": max_fill_rel,
            "max_fill_fee_absolute_cny": max_fee_abs,
        },
        "checks": checks,
        "passed": all(checks.values()),
    }


def _derived_check(
    sessions: list[str],
    sealed: dict[tuple[str, str], dict],
    raw: dict[tuple[str, str], dict],
    actions_by_day: dict[str, list[dict]],
) -> dict[str, Any]:
    recomputed = _index_derived_only(sessions, raw, actions_by_day)
    mismatches = 0
    max_relative = 0.0
    for key, row in sealed.items():
        expected = row.get("value")
        actual = recomputed[key]["value"]
        if expected is None or actual is None:
            if expected is not None or actual is not None:
                mismatches += 1
            continue
        relative = _rel_delta(float(actual), float(expected))
        max_relative = max(max_relative, relative)
        if relative > TOLERANCES["derived_relative"]:
            mismatches += 1
    return {
        "rows": len(sealed),
        "mismatches": mismatches,
        "max_relative_delta": max_relative,
        "tolerance": TOLERANCES["derived_relative"],
        "passed": mismatches == 0,
    }


def _load_snapshot(store: Path, snapshot_id: str) -> tuple[dict, dict]:
    if not (isinstance(snapshot_id, str) and len(snapshot_id) == 64):
        raise ReconcileError("Invalid snapshot ID")
    manifest_path = store / snapshot_id / "manifest.json"
    manifest = _read_json(manifest_path)
    if _hash(manifest) != snapshot_id:
        raise ReconcileError("Snapshot manifest hash does not match the snapshot ID")
    if manifest.get("data_kind") != "real" or manifest.get("schema_version") != 2:
        raise ReconcileError("This check requires a schema-v2 real snapshot")
    descriptor = next(
        item for item in manifest["partitions"] if item["name"] == "discovery"
    )
    discovery = _read_json(store / snapshot_id / "discovery.json")
    if _hash(discovery) != descriptor["content_hash"]:
        raise ReconcileError("Discovery partition content hash does not match the manifest")
    return manifest, discovery


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--baseline", type=Path, help="sealed Discovery baseline JSON")
    parser.add_argument(
        "--baseline-high-cost", type=Path, help="higher-cost baseline JSON to check"
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="pre-seal real.json; enables the full-window research sanity check",
    )
    parser.add_argument("--json-output", type=Path, help="write the comparison JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    store = args.store.expanduser().resolve()
    manifest, discovery = _load_snapshot(store, args.snapshot_id)
    sessions, raw, sealed_derived, actions_by_day = _index(discovery)
    result: dict[str, Any] = {
        "schema_version": 1,
        "snapshot_id": args.snapshot_id,
        "data_kind": manifest["data_kind"],
        "common_range": manifest["common_range"],
        "partition": "discovery",
        "partition_content_hash": _hash(discovery),
        "integrity": {"manifest": True, "discovery_partition": True},
        "tolerances": TOLERANCES,
        "derived_check": _derived_check(
            sessions, sealed_derived, raw, actions_by_day
        ),
        "cost_scenarios": [],
    }
    scenarios = [(args.baseline, 5.0, 0.0)]
    if args.baseline_high_cost is not None:
        scenarios.append((args.baseline_high_cost, 20.0, 10.0))
    for path, cost, slip in scenarios:
        if path is None:
            result["cost_scenarios"].append(
                {
                    "label": f"recomputed-{cost:g}bps-{slip:g}bps-slippage",
                    "recomputed": _simulate(
                        sessions,
                        raw,
                        actions_by_day,
                        initial_cash=100_000.0,
                        transaction_cost_bps=cost,
                        slippage_bps=slip,
                    ),
                    "baseline": None,
                    "passed": None,
                }
            )
            continue
        baseline = _read_json(path)
        simulated = _simulate(
            sessions,
            raw,
            actions_by_day,
            initial_cash=float(baseline["initial_cash"]),
            transaction_cost_bps=cost,
            slippage_bps=slip,
        )
        result["cost_scenarios"].append(
            _compare_to_baseline(
                simulated, baseline, f"discovery-{cost:g}bps-{slip:g}bps-slippage"
            )
        )

    if args.input is not None:
        full = _read_json(args.input)
        full_sessions, full_raw, _, full_actions = _index(full)
        engine = _simulate(
            full_sessions,
            full_raw,
            full_actions,
            initial_cash=100_000.0,
            transaction_cost_bps=5.0,
            slippage_bps=0.0,
        )
        probe = _simulate(
            full_sessions,
            full_raw,
            full_actions,
            initial_cash=100_000.0,
            transaction_cost_bps=5.0,
            slippage_bps=0.0,
            fee_from_cash=False,
        )
        if engine["fill_date"] != "2022-05-23" or probe["fill_date"] != "2022-05-23":
            raise ReconcileError("Full-window fill date is not 2022-05-23")
        research_delta = abs(float(probe["ending_equity"]) - RESEARCH_FULL_WINDOW_NAV)
        engine_probe_delta = float(engine["ending_equity"]) - float(probe["ending_equity"])
        full_derived = _derived_check(
            full_sessions, _index(full)[2], full_raw, full_actions
        )
        result["full_window"] = {
            "input": Path(args.input).name,
            "sessions": len(full_sessions),
            "range": [full_sessions[0], full_sessions[-1]],
            "engine_model": {
                "ending_equity": engine["ending_equity"],
                "fees_paid": engine["fees_paid"],
                "nav_gaps": engine["nav_gaps"],
            },
            "research_probe_model": {
                "ending_equity": probe["ending_equity"],
                "fees_paid": probe["fees_paid"],
                "nav_gaps": probe["nav_gaps"],
            },
            "research_reference_nav": RESEARCH_FULL_WINDOW_NAV,
            "research_reference_delta_cny": research_delta,
            "engine_minus_probe_cny": engine_probe_delta,
            "derived_check": full_derived,
            "research_reference_check": research_delta
            <= TOLERANCES["research_sanity_absolute_cny"],
            "fee_accounting_explains_difference": abs(
                engine_probe_delta + engine["fees_paid"]
            )
            <= TOLERANCES["research_sanity_absolute_cny"],
        }
        result["full_window"]["passed"] = bool(
            result["full_window"]["research_reference_check"]
            and result["full_window"]["fee_accounting_explains_difference"]
            and result["full_window"]["derived_check"]["passed"]
        )

    result["passed"] = bool(
        result["derived_check"]["passed"]
        and all(
            scenario.get("passed") is not False
            for scenario in result["cost_scenarios"]
        )
        and (result.get("full_window") is None or result["full_window"]["passed"])
    )
    if args.json_output is not None:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
