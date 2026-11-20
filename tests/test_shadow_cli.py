"""Public-CLI tests for one safely rerunnable Shadow Live trading day (#32).

Everything here is synthetic: a synthetic approved release is produced by the
real protected-evaluation and release flow, then each test copies the approved
release directory into its own trusted store so lifecycle event timelines are
isolated.  The market feed is a synthetic point-in-time JSON document; no
broker, no real orders and no network are used.

Future synthetic sessions (2030) exercise fills because an order persisted now
is still before the execution session's 09:30 deadline; past synthetic sessions
(2024) exercise ``missed_deadline`` because an order persisted now is already
after the execution open.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from test_protected_eval_cli import (
    SYMBOLS,
    authorize,
    cli,
    cli_env,
    create_plan,
    freeze_candidate,
    ok,
    plan_input,
    rejected,
    seal_snapshot,
    submit_experiment,
    synthetic_snapshot,
    write_json,
)


FUTURE = ["2030-01-02", "2030-01-03", "2030-01-04", "2030-01-07", "2030-01-08", "2030-01-09"]
PAST = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"]
DIVIDEND_SYMBOL = SYMBOLS[1]  # 518880
MOMENTUM_SYMBOL = SYMBOLS[0]  # 511260


# ---------------------------------------------------------------------------
# Synthetic approved releases (real evaluation + approval flow, once)
# ---------------------------------------------------------------------------


def _create_campaign(
    project: Path,
    snapshot_store: Path,
    snapshot_id: str,
    *,
    initial_cash: float = 100_000.0,
) -> dict:
    return ok(
        cli(
            "campaign",
            "create",
            "--project",
            str(project),
            "--store",
            str(snapshot_store),
            "--snapshot-id",
            snapshot_id,
            "--attempts",
            "6",
            "--max-seconds",
            "60",
            "--initial-cash",
            format(initial_cash, "g"),
            "--transaction-cost-bps",
            "5",
            "--slippage-bps",
            "10",
        )
    )


def build_release(
    root: Path,
    payload: dict,
    name: str,
    family: str,
    parameters: dict,
    *,
    initial_cash: float = 100_000.0,
) -> dict:
    """Seal a snapshot, run both protected stages and approve one release."""
    root.mkdir(parents=True, exist_ok=True)
    sealed = seal_snapshot(root, payload, f"snapshots-{name}")
    project = root / f"project-{name}"
    ok(cli("project", "init", "--project", str(project)))
    campaign = _create_campaign(
        project,
        sealed["store"],
        sealed["snapshot_id"],
        initial_cash=initial_cash,
    )
    source = root / f"submission-{name}.json"
    submit_experiment(
        project,
        campaign["campaign_id"],
        source,
        "first",
        "Synthetic candidate for Shadow Live accounting.",
        family,
        parameters,
    )
    candidate = freeze_candidate(
        project,
        campaign["campaign_id"],
        "first",
        f"candidate-{name}",
        "Frozen candidate for the Shadow Live ticket.",
    )
    base = {
        "root": root,
        "snapshot_store": sealed["store"],
        "snapshot_id": sealed["snapshot_id"],
        "manifest": sealed["manifest"],
        "partitions": sealed["partitions"],
        "project": project,
        "campaign": campaign,
        "candidate": candidate,
    }
    store = root / f"eval-{name}"
    store.mkdir()
    plan = create_plan(base, store, candidate, plan_input(), name=f"plan-{name}")
    ok(authorize(store, plan["plan_id"]))
    validation = ok(cli_run_evaluation(base, store, plan["plan_id"]))
    ok(
        cli(
            "evaluation", "authorize",
            "--store", str(store),
            "--plan-id", plan["plan_id"],
            "--stage", "final_holdout",
            "--reviewer", "reviewer-2",
            "--reason", "Validation passed with complete evidence.",
        )
    )
    holdout = ok(cli_run_evaluation(base, store, plan["plan_id"], stage="final_holdout"))
    assert validation["outcome"] == "passed", validation
    assert holdout["outcome"] == "passed", holdout
    approved = ok(
        cli(
            "release", "approve",
            "--store", str(store),
            "--plan-id", plan["plan_id"],
            "--reviewer", f"reviewer-{name}",
            "--reason", "Both protected stages passed with complete evidence.",
        )
    )
    return {
        "store": store,
        "release_id": approved["release_id"],
        "artifact": approved["artifact"],
        "plan": plan,
        "base": base,
    }


def cli_run_evaluation(base: dict, store: Path, plan_id: str, *, stage: str = "validation"):
    return cli(
        "evaluation", "run",
        "--store", str(store),
        "--snapshot-store", str(base["snapshot_store"]),
        "--plan-id", plan_id,
        "--stage", stage,
        "--project", str(base["project"]),
    )


@pytest.fixture(scope="module")
def releases(tmp_path_factory: pytest.TempPathFactory) -> dict:
    root = tmp_path_factory.mktemp("shadow-live-releases")
    momentum = build_release(
        root / "momentum", synthetic_snapshot(), "momentum",
        "momentum", {"lookback": 2},
    )
    equal_weight = build_release(
        root / "equal",
        synthetic_snapshot(derived_range=("2024-01-13", "2024-01-20")),
        "equal",
        "equal_weight",
        {},
    )
    return {"momentum": momentum, "equal_weight": equal_weight}


# ---------------------------------------------------------------------------
# Per-test helpers: isolated release store, ledger and synthetic feed
# ---------------------------------------------------------------------------


def isolated_store(root: Path, *entries: dict) -> Path:
    store = root / "store"
    (store / "releases").mkdir(parents=True)
    for entry in entries:
        shutil.copytree(
            entry["store"] / "releases" / entry["release_id"],
            store / "releases" / entry["release_id"],
        )
    return store


def lifecycle(
    action: str,
    store: Path,
    release_id: str,
    effective_signal_date: str,
    *,
    reason: str = "Pre-registered after-close signal point.",
):
    return cli(
        "release", action,
        "--store", str(store),
        "--release-id", release_id,
        "--effective-signal-date", effective_signal_date,
        "--reason", reason,
    )


def shadow_update(
    ledger: Path,
    store: Path,
    data: Path,
    date: str,
    *,
    release_ids: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
):
    arguments = [
        "shadow", "update",
        "--ledger", str(ledger),
        "--store", str(store),
        "--data", str(data),
        "--date", date,
    ]
    for release_id in release_ids:
        arguments += ["--release-id", release_id]
    return cli(*arguments, env=env)


def shadow_report(ledger: Path, release_id: str, output: Path):
    return cli(
        "shadow", "report",
        "--ledger", str(ledger),
        "--version", release_id,
        "--output", str(output),
    )


def write_feed(path: Path, payload: dict) -> Path:
    return write_json(path, payload)


def read_feed(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def report_days(ledger: Path, release_id: str, output: Path) -> list[dict]:
    result = ok(shadow_report(ledger, release_id, output))
    report = json.loads(output.read_text(encoding="utf-8"))
    assert result["integrity"]["verified"] is True, result
    assert report["integrity"]["verified"] is True, report
    assert report["version_id"] == release_id
    return report["days"]


def day_record(ledger: Path, release_id: str, output: Path, date: str) -> dict:
    for entry in report_days(ledger, release_id, output):
        if entry["date"] == date:
            return entry
    raise AssertionError(f"no committed day {date} in the ledger")


def ledger_rows(ledger: Path, table: str) -> list[tuple]:
    database = ledger / "shadow.sqlite"
    if not database.exists():
        return []
    with sqlite3.connect(database) as connection:
        return connection.execute(f"SELECT * FROM {table}").fetchall()


def shadow_feed(
    sessions: list[str],
    *,
    closes: dict[str, list[float | None]] | None = None,
    opens: dict[str, list[float | None]] | None = None,
    derived: dict[str, list[float | None]] | None = None,
    raw_overrides: dict[tuple[str, str], dict] | None = None,
    derived_overrides: dict[tuple[str, str], dict] | None = None,
    actions: list[dict] | None = None,
) -> dict:
    """Build one strictly valid synthetic Shadow Live feed.

    Default prices are flat 10.0 with each session's open equal to the previous
    close, so a buy sized at the signal close exactly fills at the next open.
    Every row carries a timezone-aware ``available_at`` and evidence.
    """
    raw_rows: list[dict] = []
    derived_rows: list[dict] = []
    previous_close: dict[str, float | None] = {}
    for index, session in enumerate(sessions):
        for symbol in SYMBOLS:
            close = closes[symbol][index] if closes and symbol in closes else 10.0
            if opens and symbol in opens:
                open_price = opens[symbol][index]
            elif index > 0 and previous_close.get(symbol) is not None:
                open_price = previous_close[symbol]
            else:
                open_price = close
            if open_price is not None and close is not None:
                high, low = max(open_price, close), min(open_price, close)
            else:
                high = low = open_price if open_price is not None else close
            raw_row = {
                "date": session,
                "symbol": symbol,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": 1000,
                "trading_status": "trading",
                "available_at": f"{session}T09:00:00+08:00",
                "availability_evidence": "synthetic shadow feed",
            }
            raw_row.update((raw_overrides or {}).get((session, symbol), {}))
            raw_rows.append(raw_row)
            value = (
                derived[symbol][index]
                if derived and symbol in derived
                else (close if close is not None else 10.0)
            )
            derived_row = {
                "date": session,
                "symbol": symbol,
                "value": value,
                "available_at": f"{session}T15:00:00+08:00",
                "availability_evidence": "synthetic shadow feed",
            }
            derived_row.update((derived_overrides or {}).get((session, symbol), {}))
            derived_rows.append(derived_row)
            previous_close[symbol] = close
    action_rows = []
    for action in actions or []:
        row = {
            "date": action["date"],
            "symbol": action["symbol"],
            "type": action["type"],
            "available_at": action.get("available_at", f"{action['date']}T08:00:00+08:00"),
            "availability_evidence": "synthetic shadow feed",
            "terms_evidence": action.get("terms_evidence", "synthetic corporate action terms"),
        }
        for key in ("cash_per_share", "pay_date", "ratio", "action_id"):
            if key in action:
                row[key] = action[key]
        if row["type"] == "cash_dividend":
            row.setdefault("cash_per_share", None)
            row.setdefault("pay_date", None)
        else:
            row.setdefault("ratio", None)
        action_rows.append(row)
    return {
        "schema_version": 1,
        "data_kind": "synthetic",
        "symbols": list(SYMBOLS),
        "calendar": [
            {
                "date": session,
                "available_at": f"{session}T08:00:00+08:00",
                "availability_evidence": "synthetic shadow feed",
            }
            for session in sessions
        ],
        "raw_bars": raw_rows,
        "derived": derived_rows,
        "corporate_actions": action_rows,
    }


def execution(entry: dict) -> dict:
    return entry["artifact"]["execution"]


def fee_rate(entry: dict) -> float:
    return execution(entry)["transaction_cost_bps"] / 10_000.0


def slippage_rate(entry: dict) -> float:
    return execution(entry)["slippage_bps"] / 10_000.0


# ---------------------------------------------------------------------------
# 1. Happy path: signal on the first session, fills at the next open, rerun.
# ---------------------------------------------------------------------------


def test_happy_path_fills_at_next_open_and_rerun_is_already_committed(
    releases: dict, tmp_path: Path
) -> None:
    entry = releases["equal_weight"]
    release_id = entry["release_id"]
    root = tmp_path / "happy"
    store = isolated_store(root, entry)
    ledger = root / "ledger"
    sessions = FUTURE[:3]
    feed = write_feed(root / "feed.json", shadow_feed(sessions))
    ok(lifecycle("enable", store, release_id, sessions[0]))

    first = ok(shadow_update(ledger, store, feed, sessions[0]))
    assert first["target_date"] == sessions[0]
    assert first["completed_at"]
    version = first["versions"][0]
    assert version["release_id"] == release_id
    assert version["status"] == "committed"
    assert version["committed_date"] == sessions[0]
    assert version["last_committed_date"] == sessions[0]
    assert version["anomalies"] == []
    assert version["unfilled_orders"] == []

    signal_day = day_record(ledger, release_id, root / "report-1.json", sessions[0])
    result = signal_day["result"]
    assert result["date"] == sessions[0]
    assert result["release_state_at_signal_point"] == "enabled"
    assert result["fills"] == [] and result["orders"] == []
    assert result["signal"]["date"] == sessions[0]
    assert result["signal"]["execution_date"] == sessions[1]
    assert len(result["next_orders"]) == len(SYMBOLS)
    assert {order["side"] for order in result["next_orders"]} == {"buy"}
    assert {order["execution_date"] for order in result["next_orders"]} == {sessions[1]}
    assert result["nav"] == pytest.approx(execution(entry)["initial_cash"])
    assert result["corrections"] == []

    fill_day = ok(shadow_update(ledger, store, feed, sessions[1]))
    assert fill_day["versions"][0]["status"] == "committed"
    assert fill_day["versions"][0]["committed_date"] == sessions[1]
    settled = day_record(ledger, release_id, root / "report-2.json", sessions[1])
    result = settled["result"]
    assert len(result["fills"]) == len(SYMBOLS)
    assert len(result["orders"]) == len(SYMBOLS)
    assert {order["status"] for order in result["orders"]} == {"filled"}
    assert result["signal"] is None  # equal weight holds, so no new signal
    assert result["next_orders"] == []
    fee = fee_rate(entry)
    slip = slippage_rate(entry)
    initial = execution(entry)["initial_cash"]
    for fill in result["fills"]:
        raw_open = fill["raw_open"]
        assert fill["price"] == pytest.approx(raw_open * (1.0 + slip))
        assert fill["fee"] == pytest.approx(fill["quantity"] * fill["price"] * fee)
        assert fill["slippage"] == pytest.approx(fill["quantity"] * raw_open * slip)
    assert result["fees"]["total"] == pytest.approx(
        sum(fill["fee"] for fill in result["fills"])
    )
    assert result["slippage"]["total"] == pytest.approx(
        sum(fill["slippage"] for fill in result["fills"])
    )
    assert result["cash"] == pytest.approx(0.0, abs=1e-6)
    assert result["market_value"] == pytest.approx(
        initial - result["fees"]["total"] - result["slippage"]["total"]
    )
    assert result["nav"] == pytest.approx(
        result["cash"] + result["market_value"] + result["dividend_receivables"]
    )
    assert result["nav"] == pytest.approx(initial - result["fees"]["total"] - result["slippage"]["total"])
    for symbol in SYMBOLS:
        assert result["positions"][symbol]["quantity"] > 0
        assert result["positions"][symbol]["market_value"] > 0
        assert result["positions"][symbol]["close"] == 10.0

    # Rerunning the same session returns the stored day without a second fill.
    rerun = ok(
        shadow_update(ledger, store, feed, sessions[1], release_ids=(release_id,))
    )
    assert rerun["versions"][0]["status"] == "already_committed"
    assert rerun["versions"][0]["result_hash"] == settled["result_hash"]
    again = day_record(ledger, release_id, root / "report-3.json", sessions[1])
    assert again["result"] == result
    assert again["result_hash"] == settled["result_hash"]
    assert len(ledger_rows(ledger, "days")) == 2

    # Committing the next session advances progress and keeps the same account.
    final = ok(shadow_update(ledger, store, feed, sessions[2]))
    assert final["versions"][0]["status"] == "committed"
    assert final["versions"][0]["committed_date"] == sessions[2]
    last = day_record(ledger, release_id, root / "report-4.json", sessions[2])
    assert last["result"]["orders"] == []
    assert last["result"]["nav"] == pytest.approx(result["nav"])
    assert len(ledger_rows(ledger, "days")) == 3


# ---------------------------------------------------------------------------
# 2. A confirmed missing open expires only that order; NAV still commits.
# ---------------------------------------------------------------------------


def test_missing_open_expires_only_that_order_and_does_not_block(
    releases: dict, tmp_path: Path
) -> None:
    entry = releases["equal_weight"]
    release_id = entry["release_id"]
    root = tmp_path / "missing-open"
    store = isolated_store(root, entry)
    ledger = root / "ledger"
    sessions = FUTURE[:2]
    feed_payload = shadow_feed(
        sessions,
        raw_overrides={(sessions[1], DIVIDEND_SYMBOL): {"open": None}},
    )
    feed = write_feed(root / "feed.json", feed_payload)
    ok(lifecycle("enable", store, release_id, sessions[0]))
    ok(shadow_update(ledger, store, feed, sessions[0]))

    result = ok(shadow_update(ledger, store, feed, sessions[1]))
    version = result["versions"][0]
    assert version["status"] == "committed_with_anomalies"
    assert version["unfilled_orders"]
    assert [order["symbol"] for order in version["unfilled_orders"]] == [DIVIDEND_SYMBOL]
    assert version["unfilled_orders"][0]["status"] == "missing_open"
    day = day_record(ledger, release_id, root / "report.json", sessions[1])["result"]
    by_symbol = {order["symbol"]: order for order in day["orders"]}
    assert by_symbol[DIVIDEND_SYMBOL]["status"] == "missing_open"
    assert len(day["fills"]) == len(SYMBOLS) - 1
    # The expired order never became a position, and nothing else was resized.
    assert day["positions"][DIVIDEND_SYMBOL]["quantity"] == 0.0
    assert all(
        day["positions"][symbol]["quantity"] > 0
        for symbol in SYMBOLS
        if symbol != DIVIDEND_SYMBOL
    )
    assert day["nav"] is not None
    assert day["cash"] > 0  # the unfilled order's cash was not spent
    assert all(order["status"] == "filled" for order in day["orders"] if order["symbol"] != DIVIDEND_SYMBOL)
    # No carry-over into the next session: the order is terminal for that day.
    assert all(order["status"] != "pending" for order in day["orders"])


# ---------------------------------------------------------------------------
# 3. A missing held close / unresolved dividend terms blocks one version only.
# ---------------------------------------------------------------------------


def test_blocked_version_does_not_write_a_partial_day_or_redoes_others(
    releases: dict, tmp_path: Path
) -> None:
    equal = releases["equal_weight"]
    momentum = releases["momentum"]
    equal_id, momentum_id = equal["release_id"], momentum["release_id"]
    root = tmp_path / "blocked"
    store = isolated_store(root, equal, momentum)
    ledger = root / "ledger"
    sessions = FUTURE[:5]
    target = sessions[3]
    closes = {MOMENTUM_SYMBOL: [10.0, 10.1, 10.2, 10.3, 10.4]}
    ok(lifecycle("enable", store, equal_id, sessions[0]))
    ok(lifecycle("enable", store, momentum_id, sessions[0]))

    broken = shadow_feed(
        sessions,
        closes=closes,
        raw_overrides={(target, DIVIDEND_SYMBOL): {"close": None}},
    )
    feed = write_feed(root / "broken.json", broken)
    first = shadow_update(ledger, store, feed, target)
    assert first.returncode == 2, first.stdout
    payload = json.loads(first.stdout)
    by_id = {item["release_id"]: item for item in payload["versions"]}
    assert by_id[equal_id]["status"] == "blocked"
    assert by_id[equal_id]["reason_code"] == "missing_close"
    assert by_id[equal_id]["committed_date"] == sessions[2]
    assert by_id[equal_id]["last_committed_date"] == sessions[2]
    assert by_id[momentum_id]["status"] == "committed", by_id[momentum_id]
    assert by_id[momentum_id]["committed_date"] == target

    # No partial day and no advance for the blocked version.
    committed = [
        row[1] for row in ledger_rows(ledger, "days") if row[0] == equal_id
    ]
    assert committed == sessions[:3]
    progress = {
        row[0]: row for row in ledger_rows(ledger, "progress")
    }
    assert progress[equal_id][3] == sessions[2]
    frozen_dates = {
        row[1] for row in ledger_rows(ledger, "input_snapshots") if row[0] == equal_id
    }
    assert target not in frozen_dates
    momentum_hash = {
        row[1]: row[3] for row in ledger_rows(ledger, "days") if row[0] == momentum_id
    }[target]

    # Fix the close but make the dividend terms unavailable by the ex-date open:
    # the same version stays blocked on the terms, the other is not redone.
    late_terms = dict(broken)
    late_terms["corporate_actions"] = [
        {
            "date": target,
            "symbol": DIVIDEND_SYMBOL,
            "type": "cash_dividend",
            "cash_per_share": 0.2,
            "pay_date": sessions[4],
            "terms_evidence": "synthetic dividend terms",
            "available_at": f"{target}T10:00:00+08:00",
            "availability_evidence": "synthetic supplier notice",
        }
    ]
    for row in late_terms["raw_bars"]:
        if row["date"] == target and row["symbol"] == DIVIDEND_SYMBOL:
            row["close"] = 10.0
    feed = write_feed(root / "late-terms.json", late_terms)
    second = shadow_update(ledger, store, feed, target)
    assert second.returncode == 2, second.stdout
    by_id = {item["release_id"]: item for item in json.loads(second.stdout)["versions"]}
    assert by_id[equal_id]["status"] == "blocked"
    assert by_id[equal_id]["reason_code"] == "missing_action_terms"
    assert by_id[momentum_id]["status"] == "already_committed"
    assert by_id[momentum_id]["result_hash"] == momentum_hash
    assert {
        row[1] for row in ledger_rows(ledger, "input_snapshots") if row[0] == equal_id
    } == set(sessions[:3])

    # Fixing the terms lets only the blocked day commit; the other version keeps
    # its committed result byte for byte.
    fixed = json.loads(json.dumps(late_terms))
    fixed["corporate_actions"][0]["available_at"] = f"{target}T08:00:00+08:00"
    feed = write_feed(root / "fixed.json", fixed)
    third = shadow_update(ledger, store, feed, target)
    assert third.returncode == 0, third.stdout
    by_id = {item["release_id"]: item for item in json.loads(third.stdout)["versions"]}
    assert by_id[equal_id]["status"] == "committed"
    assert by_id[equal_id]["committed_date"] == target
    assert by_id[momentum_id]["status"] == "already_committed"
    assert by_id[momentum_id]["result_hash"] == momentum_hash
    day = day_record(ledger, equal_id, root / "equal-report.json", target)["result"]
    entitlement = day["dividends"]["entitlements"][0]
    assert entitlement["action_id"] == f"cash_dividend:{DIVIDEND_SYMBOL}:{target}"
    assert entitlement["pay_date"] == sessions[4]
    assert entitlement["amount"] > 0
    assert day["dividend_receivables"] == pytest.approx(entitlement["amount"])
    # The momentum version committed the missing-close day without needing the
    # unheld symbol at all.
    momentum_day = day_record(ledger, momentum_id, root / "momentum-report.json", target)["result"]
    assert momentum_day["nav"] > 0
    assert momentum_day["positions"][MOMENTUM_SYMBOL]["quantity"] > 0


# ---------------------------------------------------------------------------
# 4. Missed deadline: a backfilled historical order never fills retroactively.
# ---------------------------------------------------------------------------


def test_historical_backfill_records_missed_deadline_and_creates_a_new_order(
    releases: dict, tmp_path: Path
) -> None:
    entry = releases["momentum"]
    release_id = entry["release_id"]
    root = tmp_path / "missed"
    store = isolated_store(root, entry)
    ledger = root / "ledger"
    sessions = PAST[:5]
    feed = write_feed(
        root / "feed.json",
        shadow_feed(
            sessions,
            closes={MOMENTUM_SYMBOL: [10.0, 10.1, 10.2, 10.3, 10.4]},
        ),
    )
    ok(lifecycle("enable", store, release_id, sessions[0]))

    # Backfill the whole past window in one invocation: the signal exists, but
    # the day transaction that would persist the order only happens now, long
    # after the 2024 execution open.
    first = ok(shadow_update(ledger, store, feed, sessions[2]))
    assert first["versions"][0]["status"] == "committed"
    signal_day = day_record(ledger, release_id, root / "report-1.json", sessions[2])
    result = signal_day["result"]
    assert result["release_state_at_signal_point"] == "enabled"
    assert len(result["next_orders"]) == 1
    pending = result["next_orders"][0]
    assert pending["submitted_at"] == f"{sessions[2]}T15:00:00+08:00"
    assert pending["persisted_before_deadline"] is False

    second = ok(shadow_update(ledger, store, feed, sessions[3]))
    version = second["versions"][0]
    assert version["status"] == "committed_with_anomalies"
    day = day_record(ledger, release_id, root / "report-2.json", sessions[3])["result"]
    assert len(day["orders"]) == 1
    assert day["orders"][0]["status"] == "missed_deadline"
    assert day["orders"][0]["order_id"] == pending["order_id"]
    assert day["orders"][0]["fill"] is None
    assert day["fills"] == []
    assert day["positions"][MOMENTUM_SYMBOL]["quantity"] == 0.0
    assert day["cash"] == pytest.approx(execution(entry)["initial_cash"])
    assert day["nav"] == pytest.approx(execution(entry)["initial_cash"])
    # A later signal creates a brand new order instead of retrying the expired one.
    assert day["signal"] is not None
    assert len(day["next_orders"]) == 1
    replacement = day["next_orders"][0]
    assert replacement["order_id"] != pending["order_id"]
    assert replacement["signal_date"] == sessions[3]

    third = ok(shadow_update(ledger, store, feed, sessions[0]))  # no-op/earlier target
    assert third["versions"][0]["status"] == "already_committed"


# ---------------------------------------------------------------------------
# 5. Cash and position rules: no resize, no short, no leverage.
# ---------------------------------------------------------------------------


def test_insufficient_cash_is_terminal_without_resizing_or_leverage(
    releases: dict, tmp_path: Path
) -> None:
    entry = releases["momentum"]
    release_id = entry["release_id"]
    root = tmp_path / "insufficient-cash"
    store = isolated_store(root, entry)
    ledger = root / "ledger"
    sessions = FUTURE[:4]
    closes = {MOMENTUM_SYMBOL: [10.0, 10.1, 10.2, 10.4]}
    opens = {MOMENTUM_SYMBOL: [10.0, 10.0, 10.1, 10.404]}
    feed = write_feed(root / "feed.json", shadow_feed(sessions, closes=closes, opens=opens))
    ok(lifecycle("enable", store, release_id, sessions[2]))

    result = ok(shadow_update(ledger, store, feed, sessions[3]))
    version = result["versions"][0]
    assert version["status"] == "committed_with_anomalies"
    assert version["unfilled_orders"][0]["status"] == "insufficient_cash"
    day = day_record(ledger, release_id, root / "report.json", sessions[3])["result"]
    order = day["orders"][0]
    assert order["status"] == "insufficient_cash"
    assert order["side"] == "buy"
    assert order["fill"] is None
    assert day["fills"] == []
    assert day["cash"] == pytest.approx(execution(entry)["initial_cash"])
    assert day["positions"][MOMENTUM_SYMBOL]["quantity"] == 0.0
    assert day["nav"] == pytest.approx(execution(entry)["initial_cash"])
    assert all(
        position["quantity"] >= 0 for position in day["positions"].values()
    )
    # The frozen quantity is untouched (a cheaper order would have filled).
    assert order["quantity"] > 0


def test_sell_beyond_holdings_is_terminal_without_shorting(
    releases: dict, tmp_path: Path
) -> None:
    entry = releases["momentum"]
    release_id = entry["release_id"]
    root = tmp_path / "insufficient-shares"
    store = isolated_store(root, entry)
    ledger = root / "ledger"
    sessions = FUTURE[:6]
    closes = {MOMENTUM_SYMBOL: [10.0, 10.1, 10.2, 10.3, 9.8, 19.6]}
    opens = {MOMENTUM_SYMBOL: [10.0, 10.0, 10.1, 10.2, 10.3, 19.6]}
    derived = {MOMENTUM_SYMBOL: [10.0, 10.1, 10.2, 10.3, 9.8, 9.8]}
    feed = write_feed(
        root / "feed.json",
        shadow_feed(
            sessions,
            closes=closes,
            opens=opens,
            derived=derived,
            actions=[
                {
                    "date": sessions[5],
                    "symbol": MOMENTUM_SYMBOL,
                    "type": "share_adjustment",
                    "ratio": 0.5,
                }
            ],
        ),
    )
    ok(lifecycle("enable", store, release_id, sessions[2]))
    ok(shadow_update(ledger, store, feed, sessions[4]))

    result = ok(shadow_update(ledger, store, feed, sessions[5]))
    day = day_record(ledger, release_id, root / "report.json", sessions[5])["result"]
    assert result["versions"][0]["status"] == "committed_with_anomalies"
    assert len(day["orders"]) == 1
    order = day["orders"][0]
    assert order["side"] == "sell"
    assert order["status"] == "insufficient_shares"
    assert order["fill"] is None
    assert day["fills"] == []
    assert day["cash"] == pytest.approx(0.0, abs=1e-6)
    held = day["positions"][MOMENTUM_SYMBOL]["quantity"]
    assert held > 0
    assert order["quantity"] > held  # the stale pre-consolidation quantity
    assert day["positions"][MOMENTUM_SYMBOL]["close"] == 19.6
    assert day["nav"] == pytest.approx(held * 19.6)
    assert day["corporate_actions"][0]["status"] == "applied"
    assert day["corporate_actions"][0]["shares_after"] == pytest.approx(held)


# ---------------------------------------------------------------------------
# 6. Corporate actions: receivable on ex-date, cash on pay-date, split value.
# ---------------------------------------------------------------------------


def test_cash_dividend_moves_value_to_receivable_then_cash_without_double_count(
    releases: dict, tmp_path: Path
) -> None:
    entry = releases["equal_weight"]
    release_id = entry["release_id"]
    root = tmp_path / "dividend"
    store = isolated_store(root, entry)
    ledger = root / "ledger"
    sessions = FUTURE[:4]
    closes = {DIVIDEND_SYMBOL: [10.0, 10.0, 9.8, 9.8]}
    feed = write_feed(
        root / "feed.json",
        shadow_feed(
            sessions,
            closes=closes,
            actions=[
                {
                    "date": sessions[2],
                    "symbol": DIVIDEND_SYMBOL,
                    "type": "cash_dividend",
                    "cash_per_share": 0.2,
                    "pay_date": sessions[3],
                }
            ],
        ),
    )
    ok(lifecycle("enable", store, release_id, sessions[0]))
    ok(shadow_update(ledger, store, feed, sessions[1]))
    pre = day_record(ledger, release_id, root / "report-1.json", sessions[1])["result"]
    quantity = pre["positions"][DIVIDEND_SYMBOL]["quantity"]
    assert quantity > 0

    ok(shadow_update(ledger, store, feed, sessions[2]))
    ex = day_record(ledger, release_id, root / "report-2.json", sessions[2])["result"]
    entitlement = ex["dividends"]["entitlements"][0]
    assert entitlement["quantity"] == pytest.approx(quantity)
    assert entitlement["amount"] == pytest.approx(quantity * 0.2)
    assert ex["dividends"]["cash_total"] == 0.0
    assert ex["cash"] == pytest.approx(0.0, abs=1e-6)
    assert ex["dividend_receivables"] == pytest.approx(entitlement["amount"])
    # The ex-date close drop is exactly offset by the receivable: no P&L jump.
    assert ex["nav"] == pytest.approx(pre["nav"])
    assert ex["positions"][DIVIDEND_SYMBOL]["close"] == 9.8

    ok(shadow_update(ledger, store, feed, sessions[3]))
    paid = day_record(ledger, release_id, root / "report-3.json", sessions[3])["result"]
    assert paid["dividends"]["payments"][0]["amount"] == pytest.approx(entitlement["amount"])
    assert paid["dividends"]["receivables"] == []
    assert paid["dividend_receivables"] == 0.0
    assert paid["dividends"]["cash_total"] == pytest.approx(entitlement["amount"])
    assert paid["cash"] == pytest.approx(entitlement["amount"])
    # Payment is only a transfer between accounts: NAV does not increase again.
    assert paid["nav"] == pytest.approx(ex["nav"])
    assert paid["dividends"]["entitled_total"] == pytest.approx(entitlement["amount"])


def test_share_adjustment_doubles_shares_and_halves_price_with_same_value(
    releases: dict, tmp_path: Path
) -> None:
    entry = releases["equal_weight"]
    release_id = entry["release_id"]
    root = tmp_path / "split"
    store = isolated_store(root, entry)
    ledger = root / "ledger"
    sessions = FUTURE[:3]
    closes = {MOMENTUM_SYMBOL: [10.0, 10.0, 5.0]}
    opens = {MOMENTUM_SYMBOL: [10.0, 10.0, 5.0]}
    derived = {MOMENTUM_SYMBOL: [10.0, 10.0, 10.0]}
    feed = write_feed(
        root / "feed.json",
        shadow_feed(
            sessions,
            closes=closes,
            opens=opens,
            derived=derived,
            actions=[
                {
                    "date": sessions[2],
                    "symbol": MOMENTUM_SYMBOL,
                    "type": "share_adjustment",
                    "ratio": 2.0,
                }
            ],
        ),
    )
    ok(lifecycle("enable", store, release_id, sessions[0]))
    ok(shadow_update(ledger, store, feed, sessions[1]))
    pre = day_record(ledger, release_id, root / "report-1.json", sessions[1])["result"]
    before = pre["positions"][MOMENTUM_SYMBOL]
    assert before["quantity"] > 0

    ok(shadow_update(ledger, store, feed, sessions[2]))
    after = day_record(ledger, release_id, root / "report-2.json", sessions[2])["result"]
    position = after["positions"][MOMENTUM_SYMBOL]
    assert position["quantity"] == pytest.approx(before["quantity"] * 2)
    assert position["close"] == 5.0
    assert position["market_value"] == pytest.approx(before["market_value"])
    assert after["market_value"] == pytest.approx(pre["market_value"])
    assert after["nav"] == pytest.approx(pre["nav"])
    action = after["corporate_actions"][0]
    assert action["status"] == "applied"
    assert action["ratio"] == 2.0


# ---------------------------------------------------------------------------
# 7. Interruption before and after the day commit.
# ---------------------------------------------------------------------------


def test_interruption_after_fills_recovers_an_identical_day(
    releases: dict, tmp_path: Path
) -> None:
    entry = releases["equal_weight"]
    release_id = entry["release_id"]
    root = tmp_path / "interrupt"
    store = isolated_store(root, entry)
    sessions = FUTURE[:4]
    feed = write_feed(root / "feed.json", shadow_feed(sessions))
    ok(lifecycle("enable", store, release_id, sessions[0]))

    baseline = root / "baseline"
    ok(shadow_update(baseline, store, feed, sessions[0]))
    crashed = root / "crashed"
    shutil.copytree(baseline, crashed)

    # Uninterrupted reference versus a crash after fills but before the commit.
    ok(shadow_update(baseline, store, feed, sessions[1]))
    interrupted = shadow_update(
        crashed,
        store,
        feed,
        sessions[1],
        env=cli_env(PRAJNATRADE_TEST_FAILPOINT="shadow_after_fills"),
    )
    assert interrupted.returncode != 0, (interrupted.stdout, interrupted.stderr)
    with sqlite3.connect(crashed / "shadow.sqlite") as connection:
        dates = [
            row[0]
            for row in connection.execute(
                "SELECT date FROM days WHERE version_id = ? ORDER BY date", (release_id,)
            )
        ]
        frozen = [
            row[0]
            for row in connection.execute(
                "SELECT date FROM input_snapshots WHERE version_id = ? ORDER BY date",
                (release_id,),
            )
        ]
    assert dates == [sessions[0]]
    assert frozen == [sessions[0], sessions[1]]

    recovered = ok(shadow_update(crashed, store, feed, sessions[1]))
    assert recovered["versions"][0]["status"] == "committed"
    with sqlite3.connect(baseline / "shadow.sqlite") as reference:
        reference_rows = reference.execute(
            "SELECT date, result_json, result_hash FROM days "
            "WHERE version_id = ? ORDER BY date",
            (release_id,),
        ).fetchall()
    with sqlite3.connect(crashed / "shadow.sqlite") as connection:
        crashed_rows = connection.execute(
            "SELECT date, result_json, result_hash FROM days "
            "WHERE version_id = ? ORDER BY date",
            (release_id,),
        ).fetchall()
    assert crashed_rows == reference_rows
    assert len(reference_rows) == 2
    day = json.loads(reference_rows[1][1])
    order_ids = [order["order_id"] for order in day["orders"]]
    assert len(order_ids) == len(set(order_ids)) == len(SYMBOLS)
    assert {order["status"] for order in day["orders"]} == {"filled"}


def test_interruption_after_commit_reports_already_committed(
    releases: dict, tmp_path: Path
) -> None:
    entry = releases["equal_weight"]
    release_id = entry["release_id"]
    root = tmp_path / "interrupt-commit"
    store = isolated_store(root, entry)
    ledger = root / "ledger"
    sessions = FUTURE[:4]
    feed = write_feed(root / "feed.json", shadow_feed(sessions))
    ok(lifecycle("enable", store, release_id, sessions[0]))
    ok(shadow_update(ledger, store, feed, sessions[0]))
    ok(shadow_update(ledger, store, feed, sessions[1]))

    interrupted = shadow_update(
        ledger,
        store,
        feed,
        sessions[2],
        env=cli_env(PRAJNATRADE_TEST_FAILPOINT="shadow_after_commit_before_report"),
    )
    assert interrupted.returncode != 0, (interrupted.stdout, interrupted.stderr)
    with sqlite3.connect(ledger / "shadow.sqlite") as connection:
        before = connection.execute(
            "SELECT date, result_json, result_hash FROM days "
            "WHERE version_id = ? ORDER BY date",
            (release_id,),
        ).fetchall()
    assert [row[0] for row in before] == sessions[:3]

    rerun = ok(shadow_update(ledger, store, feed, sessions[2]))
    assert rerun["versions"][0]["status"] == "already_committed"
    assert rerun["versions"][0]["result_hash"] == before[2][2]
    with sqlite3.connect(ledger / "shadow.sqlite") as connection:
        after = connection.execute(
            "SELECT date, result_json, result_hash FROM days "
            "WHERE version_id = ? ORDER BY date",
            (release_id,),
        ).fetchall()
    assert after == before


# ---------------------------------------------------------------------------
# 8. Pause / retire / not started / invalid or absent artifact.
# ---------------------------------------------------------------------------


def test_pause_settles_pending_orders_but_creates_no_new_signal(
    releases: dict, tmp_path: Path
) -> None:
    entry = releases["momentum"]
    release_id = entry["release_id"]
    root = tmp_path / "paused"
    store = isolated_store(root, entry)
    ledger = root / "ledger"
    sessions = FUTURE[:5]
    feed = write_feed(
        root / "feed.json",
        shadow_feed(sessions, closes={MOMENTUM_SYMBOL: [10.0, 10.1, 10.2, 10.3, 10.4]}),
    )
    ok(lifecycle("enable", store, release_id, sessions[2]))
    ok(shadow_update(ledger, store, feed, sessions[2]))
    ok(lifecycle("pause", store, release_id, sessions[3]))

    result = ok(shadow_update(ledger, store, feed, sessions[3]))
    assert result["versions"][0]["status"] == "committed"
    day = day_record(ledger, release_id, root / "report.json", sessions[3])["result"]
    assert day["release_state_at_signal_point"] == "paused"
    assert len(day["orders"]) == 1
    assert day["orders"][0]["status"] == "filled"
    assert day["signal"] is None
    assert day["next_orders"] == []
    assert day["positions"][MOMENTUM_SYMBOL]["quantity"] > 0


def test_retired_version_commits_nav_only_and_never_signals(
    releases: dict, tmp_path: Path
) -> None:
    entry = releases["momentum"]
    release_id = entry["release_id"]
    root = tmp_path / "retired"
    store = isolated_store(root, entry)
    ledger = root / "ledger"
    sessions = FUTURE[:5]
    feed = write_feed(
        root / "feed.json",
        shadow_feed(sessions, closes={MOMENTUM_SYMBOL: [10.0, 10.1, 10.2, 10.3, 10.4]}),
    )
    ok(lifecycle("enable", store, release_id, sessions[2]))
    ok(shadow_update(ledger, store, feed, sessions[2]))
    ok(lifecycle("retire", store, release_id, sessions[3]))
    ok(shadow_update(ledger, store, feed, sessions[3]))
    result = ok(shadow_update(ledger, store, feed, sessions[4]))
    assert result["versions"][0]["status"] == "committed"
    day = day_record(ledger, release_id, root / "report.json", sessions[4])["result"]
    assert day["release_state_at_signal_point"] == "retired"
    assert day["signal"] is None
    assert day["next_orders"] == []
    assert day["orders"] == []
    assert day["nav"] > 0


def test_not_started_and_retired_without_history_are_reported(
    releases: dict, tmp_path: Path
) -> None:
    entry = releases["momentum"]
    release_id = entry["release_id"]
    sessions = FUTURE[:5]
    feed_payload = shadow_feed(
        sessions, closes={MOMENTUM_SYMBOL: [10.0, 10.1, 10.2, 10.3, 10.4]}
    )

    not_started_root = tmp_path / "not-started"
    store = isolated_store(not_started_root, entry)
    ledger = not_started_root / "ledger"
    feed = write_feed(not_started_root / "feed.json", feed_payload)
    ok(lifecycle("enable", store, release_id, sessions[3]))
    automatic = ok(shadow_update(ledger, store, feed, sessions[2]))
    assert automatic["versions"] == []
    result = ok(
        shadow_update(ledger, store, feed, sessions[2], release_ids=(release_id,))
    )
    assert result["versions"][0]["status"] == "not_started"
    assert result["versions"][0]["reason_code"] == "ENABLED_AFTER_TARGET"
    assert ledger_rows(ledger, "days") == []

    retired_root = tmp_path / "retired-no-history"
    store = isolated_store(retired_root, entry)
    ledger = retired_root / "ledger"
    feed = write_feed(retired_root / "feed.json", feed_payload)
    ok(lifecycle("enable", store, release_id, sessions[2]))
    ok(lifecycle("retire", store, release_id, sessions[3]))
    automatic = ok(shadow_update(ledger, store, feed, sessions[3]))
    assert automatic["versions"] == []
    result = ok(shadow_update(ledger, store, feed, sessions[3], release_ids=(release_id,)))
    assert result["versions"][0]["status"] == "retired"
    assert result["versions"][0]["reason_code"] == "RETIRED_WITHOUT_HISTORY"
    assert ledger_rows(ledger, "days") == []


def test_invalid_or_absent_artifact_blocks_only_that_version(
    releases: dict, tmp_path: Path
) -> None:
    entry = releases["momentum"]
    release_id = entry["release_id"]
    sessions = FUTURE[:4]
    feed_payload = shadow_feed(
        sessions, closes={MOMENTUM_SYMBOL: [10.0, 10.1, 10.2, 10.3]}
    )

    tampered_root = tmp_path / "tampered"
    store = isolated_store(tampered_root, entry)
    ledger = tampered_root / "ledger"
    feed = write_feed(tampered_root / "feed.json", feed_payload)
    ok(lifecycle("enable", store, release_id, sessions[0]))
    artifact = store / "releases" / release_id / "artifact.json"
    artifact.chmod(0o644)
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    payload["approval"]["reason"] = "tampered after approval"
    artifact.write_text(json.dumps(payload), encoding="utf-8")
    result = shadow_update(ledger, store, feed, sessions[0], release_ids=(release_id,))
    assert result.returncode == 2, result.stdout
    version = json.loads(result.stdout)["versions"][0]
    assert version["status"] == "blocked"
    assert version["reason_code"] == "RELEASE_INTEGRITY_FAILED"
    assert ledger_rows(ledger, "days") == []

    absent_root = tmp_path / "absent"
    store = isolated_store(absent_root, entry)
    ledger = absent_root / "ledger"
    feed = write_feed(absent_root / "feed.json", feed_payload)
    ok(lifecycle("enable", store, release_id, sessions[0]))
    (store / "releases" / release_id / "artifact.json").unlink()
    result = shadow_update(ledger, store, feed, sessions[0])
    assert result.returncode == 2, result.stdout
    version = json.loads(result.stdout)["versions"][0]
    assert version["status"] == "blocked"
    assert version["reason_code"] == "RELEASE_NOT_FOUND"


# ---------------------------------------------------------------------------
# 9. Required-input blocking: signal input, suspended closes; no freeze.
# ---------------------------------------------------------------------------


def test_missing_signal_input_blocks_without_freezing_then_recovers(
    releases: dict, tmp_path: Path
) -> None:
    entry = releases["momentum"]
    release_id = entry["release_id"]
    root = tmp_path / "missing-signal"
    store = isolated_store(root, entry)
    ledger = root / "ledger"
    sessions = FUTURE[:4]
    late = {
        (sessions[1], MOMENTUM_SYMBOL): {
            "available_at": f"{sessions[2]}T16:00:00+08:00",
            "availability_evidence": "synthetic late derived publication",
        }
    }
    feed = write_feed(
        root / "late.json",
        shadow_feed(
            sessions,
            closes={MOMENTUM_SYMBOL: [10.0, 10.1, 10.2, 10.3]},
            derived_overrides=late,
        ),
    )
    ok(lifecycle("enable", store, release_id, sessions[2]))
    blocked = shadow_update(ledger, store, feed, sessions[2])
    assert blocked.returncode == 2, blocked.stdout
    version = json.loads(blocked.stdout)["versions"][0]
    assert version["status"] == "blocked"
    assert version["reason_code"] == "missing_signal_input"
    assert version["committed_date"] is None
    assert ledger_rows(ledger, "days") == []
    assert ledger_rows(ledger, "input_snapshots") == []

    fixed = read_feed(root / "late.json")
    for row in fixed["derived"]:
        if row["date"] == sessions[1] and row["symbol"] == MOMENTUM_SYMBOL:
            row["available_at"] = f"{sessions[1]}T15:00:00+08:00"
    feed = write_feed(root / "fixed.json", fixed)
    result = ok(shadow_update(ledger, store, feed, sessions[2]))
    assert result["versions"][0]["status"] == "committed"
    day = day_record(ledger, release_id, root / "report.json", sessions[2])["result"]
    assert day["signal"] is not None
    assert len(day["next_orders"]) == 1


def test_suspended_held_close_blocks_instead_of_valuing_a_stale_price(
    releases: dict, tmp_path: Path
) -> None:
    entry = releases["equal_weight"]
    release_id = entry["release_id"]
    root = tmp_path / "suspended"
    store = isolated_store(root, entry)
    ledger = root / "ledger"
    sessions = FUTURE[:3]
    feed = write_feed(
        root / "feed.json",
        shadow_feed(
            sessions,
            raw_overrides={
                (sessions[2], DIVIDEND_SYMBOL): {"trading_status": "suspended"}
            },
        ),
    )
    ok(lifecycle("enable", store, release_id, sessions[0]))
    ok(shadow_update(ledger, store, feed, sessions[1]))
    blocked = shadow_update(ledger, store, feed, sessions[2])
    assert blocked.returncode == 2, blocked.stdout
    version = json.loads(blocked.stdout)["versions"][0]
    assert version["status"] == "blocked"
    assert version["reason_code"] == "missing_close"
    assert version["committed_date"] == sessions[1]
    assert ledger_rows(ledger, "days") and len(ledger_rows(ledger, "days")) == 2
    assert {
        row[1] for row in ledger_rows(ledger, "input_snapshots")
    } == set(sessions[:2])


# ---------------------------------------------------------------------------
# 10. Ledger integrity: a tampered stored input or result is detected.
# ---------------------------------------------------------------------------


def _tamper(ledger: Path, statement: str, parameters: tuple) -> None:
    with sqlite3.connect(ledger / "shadow.sqlite") as connection:
        connection.execute(statement, parameters)


def test_tampered_day_result_is_detected_on_rerun_and_report(
    releases: dict, tmp_path: Path
) -> None:
    entry = releases["equal_weight"]
    release_id = entry["release_id"]
    root = tmp_path / "tampered-result"
    store = isolated_store(root, entry)
    fed_ledger = root / "fed"
    sessions = FUTURE[:3]
    feed = write_feed(root / "feed.json", shadow_feed(sessions))
    ok(lifecycle("enable", store, release_id, sessions[0]))
    ok(shadow_update(fed_ledger, store, feed, sessions[0]))
    ok(shadow_update(fed_ledger, store, feed, sessions[1]))
    with sqlite3.connect(fed_ledger / "shadow.sqlite") as connection:
        result_json = connection.execute(
            "SELECT result_json FROM days WHERE version_id = ? AND date = ?",
            (release_id, sessions[1]),
        ).fetchone()[0]
    tampered = json.loads(result_json)
    tampered["cash"] = tampered["cash"] + 1.0
    _tamper(
        fed_ledger,
        "UPDATE days SET result_json = ? WHERE version_id = ? AND date = ?",
        (json.dumps(tampered), release_id, sessions[1]),
    )

    rerun = shadow_update(fed_ledger, store, feed, sessions[1])
    assert rerun.returncode == 2, rerun.stdout
    version = json.loads(rerun.stdout)["versions"][0]
    assert version["status"] == "blocked"
    assert version["reason_code"] == "LEDGER_INTEGRITY_FAILED"

    report = shadow_report(fed_ledger, release_id, root / "report.json")
    assert report.returncode == 2, report.stdout
    rejected_payload = json.loads(report.stdout)
    assert rejected_payload["status"] == "rejected"
    assert rejected_payload["error"]["code"] == "LEDGER_INTEGRITY_FAILED"
    assert not (root / "report.json").exists()


def test_tampered_frozen_input_is_detected_on_rerun(
    releases: dict, tmp_path: Path
) -> None:
    entry = releases["equal_weight"]
    release_id = entry["release_id"]
    root = tmp_path / "tampered-input"
    store = isolated_store(root, entry)
    ledger = root / "ledger"
    sessions = FUTURE[:3]
    feed = write_feed(root / "feed.json", shadow_feed(sessions))
    ok(lifecycle("enable", store, release_id, sessions[0]))
    ok(shadow_update(ledger, store, feed, sessions[0]))
    ok(shadow_update(ledger, store, feed, sessions[1]))
    with sqlite3.connect(ledger / "shadow.sqlite") as connection:
        input_json = connection.execute(
            "SELECT input_json FROM input_snapshots WHERE version_id = ? AND date = ?",
            (release_id, sessions[1]),
        ).fetchone()[0]
    frozen = json.loads(input_json)
    frozen["previous"]["cash"] = frozen["previous"]["cash"] + 1.0
    _tamper(
        ledger,
        "UPDATE input_snapshots SET input_json = ? WHERE version_id = ? AND date = ?",
        (json.dumps(frozen), release_id, sessions[1]),
    )
    rerun = shadow_update(ledger, store, feed, sessions[1])
    assert rerun.returncode == 2, rerun.stdout
    version = json.loads(rerun.stdout)["versions"][0]
    assert version["status"] == "blocked"
    assert version["reason_code"] == "LEDGER_INTEGRITY_FAILED"
    # The report refuses to silently present an unverifiable ledger.
    report = shadow_report(ledger, release_id, root / "report.json")
    assert report.returncode == 2
    assert json.loads(report.stdout)["error"]["code"] == "LEDGER_INTEGRITY_FAILED"


def test_report_verifies_every_committed_day_and_summarises_the_ledger(
    releases: dict, tmp_path: Path
) -> None:
    entry = releases["equal_weight"]
    release_id = entry["release_id"]
    root = tmp_path / "report"
    store = isolated_store(root, entry)
    ledger = root / "ledger"
    sessions = FUTURE[:4]
    feed = write_feed(root / "feed.json", shadow_feed(sessions))
    ok(lifecycle("enable", store, release_id, sessions[0]))
    ok(shadow_update(ledger, store, feed, sessions[1]))
    printed = ok(shadow_report(ledger, release_id, root / "ledger-report.json"))
    assert printed["status"] == "written"
    assert printed["days"] == 2
    assert printed["first_committed_date"] == sessions[0]
    assert printed["last_committed_date"] == sessions[1]
    report = json.loads((root / "ledger-report.json").read_text(encoding="utf-8"))
    assert report["kind"] == "shadow_live_ledger_report"
    assert report["scope"] == "synthetic"
    assert [entry["date"] for entry in report["days"]] == sessions[:2]
    assert all(entry["verified"] is True for entry in report["days"])
    assert report["summary"]["fills"] == len(SYMBOLS)
    assert report["summary"]["ending_nav"] == pytest.approx(
        report["days"][-1]["result"]["nav"]
    )
    assert report["progress"]["last_committed_date"] == sessions[1]
    # The hook #33/#34 will use yields the same verified records.
    from prajnatrade import shadow

    hooked = list(shadow.iter_committed_days(ledger, release_id))
    assert [day["date"] for day in hooked] == sessions[:2]
    assert [day["result"] for day in hooked] == [
        entry["result"] for entry in report["days"]
    ]


# ---------------------------------------------------------------------------
# 11. Strict synthetic feed validation and operator-only surface.
# ---------------------------------------------------------------------------


def test_malformed_feed_and_non_session_dates_are_rejected(
    releases: dict, tmp_path: Path
) -> None:
    entry = releases["momentum"]
    release_id = entry["release_id"]
    root = tmp_path / "invalid"
    store = isolated_store(root, entry)
    ledger = root / "ledger"
    sessions = FUTURE[:3]
    ok(lifecycle("enable", store, release_id, sessions[0]))
    valid = shadow_feed(sessions)
    invalid_payloads = {}

    wrong_symbols = json.loads(json.dumps(valid))
    wrong_symbols["symbols"] = list(SYMBOLS[:4])
    invalid_payloads["wrong-symbols"] = wrong_symbols

    missing_evidence = json.loads(json.dumps(valid))
    missing_evidence["raw_bars"][0]["availability_evidence"] = ""
    invalid_payloads["missing-evidence"] = missing_evidence

    unsorted = json.loads(json.dumps(valid))
    unsorted["calendar"] = list(reversed(unsorted["calendar"]))
    invalid_payloads["unsorted-calendar"] = unsorted

    outside = json.loads(json.dumps(valid))
    outside["raw_bars"][0]["date"] = "2031-01-01"
    invalid_payloads["date-outside-calendar"] = outside

    bad_term = json.loads(json.dumps(valid))
    bad_term["corporate_actions"] = [
        {
            "date": sessions[1],
            "symbol": SYMBOLS[0],
            "type": "share_adjustment",
            "ratio": 0,
            "terms_evidence": "synthetic",
            "available_at": f"{sessions[1]}T08:00:00+08:00",
            "availability_evidence": "synthetic",
        }
    ]
    invalid_payloads["zero-ratio"] = bad_term

    naive_timestamp = json.loads(json.dumps(valid))
    naive_timestamp["derived"][0]["available_at"] = f"{sessions[0]}T15:00:00"
    invalid_payloads["naive-timestamp"] = naive_timestamp

    for name, payload in invalid_payloads.items():
        path = root / f"{name}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        response = shadow_update(ledger, store, path, sessions[0])
        assert response.returncode == 2, (name, response.stdout)
        rejected_payload = json.loads(response.stdout)
        assert rejected_payload["status"] == "rejected", name
        assert rejected_payload["error"]["code"] == "INVALID_FEED", name
    # A malformed feed never creates a ledger day or freezes an input.
    assert ledger_rows(ledger, "days") == []
    assert ledger_rows(ledger, "input_snapshots") == []

    response = shadow_update(ledger, store, root / "wrong-symbols.json", sessions[1])
    assert response.returncode == 2
    assert json.loads(response.stdout)["error"]["code"] == "INVALID_FEED"
    response = shadow_update(ledger, store, write_feed(root / "feed.json", valid), "2031-01-01")
    assert response.returncode == 2
    assert json.loads(response.stdout)["error"]["code"] == "NOT_A_SESSION"
    response = shadow_update(
        ledger, store, root / "feed.json", sessions[0], release_ids=("not-a-release",)
    )
    assert response.returncode == 2
    assert json.loads(response.stdout)["error"]["code"] == "INVALID_RELEASE_ID"
    response = shadow_update(ledger, root / "does-not-exist", root / "feed.json", sessions[0])
    assert response.returncode == 2
    assert json.loads(response.stdout)["error"]["code"] == "RELEASE_STORE_UNAVAILABLE"


def test_agent_cli_does_not_expose_shadow(releases: dict, tmp_path: Path) -> None:
    entry = releases["momentum"]
    root = tmp_path / "agent"
    store = isolated_store(root, entry)
    response = cli(
        "shadow", "update",
        "--ledger", str(root / "ledger"),
        "--store", str(store),
        "--data", str(write_feed(root / "feed.json", shadow_feed(FUTURE[:2]))),
        "--date", FUTURE[0],
        agent=True,
    )
    assert response.returncode == 2
    assert "invalid choice" in response.stderr
    assert "shadow" not in response.stdout


# ---------------------------------------------------------------------------
# 12. A frozen input is authoritative: a changed feed is not silently mixed.
# ---------------------------------------------------------------------------


def test_changed_feed_after_freezing_reports_frozen_input_changed(
    releases: dict, tmp_path: Path
) -> None:
    entry = releases["equal_weight"]
    release_id = entry["release_id"]
    root = tmp_path / "frozen-changed"
    store = isolated_store(root, entry)
    ledger = root / "ledger"
    sessions = FUTURE[:3]
    feed = write_feed(root / "feed.json", shadow_feed(sessions))
    ok(lifecycle("enable", store, release_id, sessions[0]))
    ok(shadow_update(ledger, store, feed, sessions[0]))
    crashed = shadow_update(
        ledger,
        store,
        feed,
        sessions[1],
        env=cli_env(PRAJNATRADE_TEST_FAILPOINT="shadow_after_fills"),
    )
    assert crashed.returncode != 0
    with sqlite3.connect(ledger / "shadow.sqlite") as connection:
        frozen_hash = connection.execute(
            "SELECT input_hash FROM input_snapshots WHERE version_id = ? AND date = ?",
            (release_id, sessions[1]),
        ).fetchone()[0]
    assert frozen_hash

    changed_payload = read_feed(root / "feed.json")
    for row in changed_payload["derived"]:
        if row["date"] == sessions[1]:
            row["value"] = 10.05
    changed = write_feed(root / "changed.json", changed_payload)
    blocked = shadow_update(ledger, store, changed, sessions[1])
    assert blocked.returncode == 2, blocked.stdout
    version = json.loads(blocked.stdout)["versions"][0]
    assert version["status"] == "blocked"
    assert version["reason_code"] == "FROZEN_INPUT_CHANGED"
    assert ledger_rows(ledger, "days") and len(ledger_rows(ledger, "days")) == 1
    with sqlite3.connect(ledger / "shadow.sqlite") as connection:
        unchanged_hash = connection.execute(
            "SELECT input_hash FROM input_snapshots WHERE version_id = ? AND date = ?",
            (release_id, sessions[1]),
        ).fetchone()[0]
    assert unchanged_hash == frozen_hash

    # Restoring the original feed lets the rerun reuse the frozen input.
    recovered = ok(shadow_update(ledger, store, feed, sessions[1]))
    assert recovered["versions"][0]["status"] == "committed"


# ---------------------------------------------------------------------------
# 13. Selection: approval never starts tracking, untracked versions are skipped.
# ---------------------------------------------------------------------------


def test_approval_alone_does_not_start_tracking_and_auto_skips_untracked(
    releases: dict, tmp_path: Path
) -> None:
    equal = releases["equal_weight"]
    momentum = releases["momentum"]
    root = tmp_path / "selection"
    store = isolated_store(root, equal, momentum)
    ledger = root / "ledger"
    sessions = FUTURE[:3]
    feed = write_feed(root / "feed.json", shadow_feed(sessions))
    ok(lifecycle("enable", store, equal["release_id"], sessions[0]))

    # Only the enabled version is tracked; the approved-but-published one is
    # neither reported nor run automatically.
    result = ok(shadow_update(ledger, store, feed, sessions[0]))
    assert [version["release_id"] for version in result["versions"]] == [
        equal["release_id"]
    ]
    assert result["versions"][0]["status"] == "committed"

    never_enabled = ok(
        shadow_update(
            ledger,
            store,
            feed,
            sessions[0],
            release_ids=(momentum["release_id"],),
        )
    )
    assert never_enabled["versions"][0]["status"] == "not_started"
    assert never_enabled["versions"][0]["reason_code"] == "NOT_ENABLED"
    assert {
        row[0] for row in ledger_rows(ledger, "days")
    } == {equal["release_id"]}


# ---------------------------------------------------------------------------
# 14. A full switch funds the new position from the same signal's sell proceeds.
# ---------------------------------------------------------------------------


def test_full_switch_rebalance_fills_both_legs_without_resizing(
    releases: dict, tmp_path: Path
) -> None:
    entry = releases["momentum"]
    release_id = entry["release_id"]
    root = tmp_path / "switch"
    store = isolated_store(root, entry)
    ledger = root / "ledger"
    sessions = FUTURE[:5]
    closes = {
        MOMENTUM_SYMBOL: [10.0, 10.1, 10.2, 10.2, 10.2],
        DIVIDEND_SYMBOL: [10.0, 10.0, 10.0, 10.3, 10.3],
    }
    feed = write_feed(root / "feed.json", shadow_feed(sessions, closes=closes))
    ok(lifecycle("enable", store, release_id, sessions[2]))
    ok(shadow_update(ledger, store, feed, sessions[3]))
    day = day_record(ledger, release_id, root / "report-1.json", sessions[3])["result"]
    assert len(day["fills"]) == 1
    assert day["positions"][MOMENTUM_SYMBOL]["quantity"] > 0
    signal = day["signal"]
    assert signal["target_weights"][DIVIDEND_SYMBOL] == pytest.approx(1.0)
    assert [order["side"] for order in day["next_orders"]] == ["sell", "buy"]
    sell = next(order for order in day["next_orders"] if order["side"] == "sell")
    buy = next(order for order in day["next_orders"] if order["side"] == "buy")
    assert sell["quantity"] == pytest.approx(
        day["positions"][MOMENTUM_SYMBOL]["quantity"], rel=1e-9
    )
    assert buy["quantity"] > 0

    result = ok(shadow_update(ledger, store, feed, sessions[4]))
    assert result["versions"][0]["status"] == "committed"
    assert result["versions"][0]["unfilled_orders"] == []
    rebalanced = day_record(ledger, release_id, root / "report-2.json", sessions[4])["result"]
    statuses = {order["side"]: order["status"] for order in rebalanced["orders"]}
    assert statuses == {"sell": "filled", "buy": "filled"}
    assert rebalanced["positions"][MOMENTUM_SYMBOL]["quantity"] == 0.0
    assert rebalanced["positions"][DIVIDEND_SYMBOL]["quantity"] > 0
    assert rebalanced["cash"] == pytest.approx(0.0, abs=1e-6)
    assert rebalanced["nav"] > 0
