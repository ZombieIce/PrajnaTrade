"""Public-CLI tests for the one-time dividend-correction delta (#33).

Everything here is synthetic.  Real approved releases come from the shared
``tests/test_shadow_cli.py`` fixture (the real protected-evaluation + release
flow), plus one rebalancing inverse-volatility release built once through the
same flow because the equal-weight fixture trades only once and the momentum
fixture is single-asset (needed to exercise a pending buy against near-zero
cash).  Each test copies the release directory into an isolated trusted store so
lifecycle timelines do not leak between tests.  A verified correction revision
is built with the real ``snapshot correction register|verify`` journal flow
against a second synthetic snapshot store, so ``dividend.load_verified_revision``
is exercised rather than mocked.

The shadow feed uses future synthetic sessions (2030) so an order persisted now
is still before the execution session's 09:30 Asia/Shanghai deadline.  The
revision journal records the *actual* filing instant; tests that need a filing
after a session's 15:00 cutoff pin ``filed_at`` in the synthetic journal because
the wall clock cannot be moved.  ``revision_id`` is the content address of the
revision itself, so pinning the journal's filing instant does not change the
revision identity or the verified status.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from prajnatrade import snapshot
from test_protected_eval_cli import (
    SYMBOLS,
    cli,
    cli_env,
    ok,
    seal_snapshot,
    synthetic_snapshot,
    write_json,
)
from test_shadow_cli import (
    DIVIDEND_SYMBOL,
    MOMENTUM_SYMBOL,
    build_release,
    day_record,
    isolated_store,
    ledger_rows,
    lifecycle,
    releases,
    shadow_feed,
    shadow_report,
    write_feed,
)


SESSIONS = ["2030-01-02", "2030-01-03", "2030-01-04", "2030-01-07",
            "2030-01-08", "2030-01-09", "2030-01-10", "2030-01-11"]
EX_DATE = SESSIONS[2]
PAY_DATE = SESSIONS[4]
CORRECTION_DAY = SESSIONS[3]
OLD_AMOUNT = 0.20
NEW_AMOUNT = 0.18

NOTICE_AVAILABLE_AT = "2026-01-05T09:00:00+08:00"
NOTICE_COLLECTED_AT = "2026-02-02T09:00:00+08:00"
SUPPLIER_OBSERVED_AT = "2026-02-01T10:00:00+08:00"


# ---------------------------------------------------------------------------
# Synthetic snapshot store that owns the verified-revision journal
# ---------------------------------------------------------------------------


def _journal_evidence(available_at: str = "2029-12-31T00:00:00+08:00") -> dict:
    return {
        "available_at": available_at,
        "availability_evidence": "synthetic correction journal schedule",
        "collected_at": "2029-12-31T12:00:00+08:00",
    }


def correction_snapshot(
    sessions: list[str],
    *,
    symbol: str = DIVIDEND_SYMBOL,
    old_amount: float = OLD_AMOUNT,
    ex_date: str = EX_DATE,
    pay_date: str = PAY_DATE,
) -> dict:
    """One complete synthetic snapshot used only as a revision predecessor."""
    iso_days = list(sessions)
    evidence = _journal_evidence()
    raw_bars = []
    derived = []
    for day in iso_days:
        for day_symbol in SYMBOLS:
            price = 10.0
            raw_bars.append(
                {
                    "date": day,
                    "symbol": day_symbol,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": 1000,
                    "trading_status": "trading",
                    **evidence,
                }
            )
            derived.append(
                {"date": day, "symbol": day_symbol, "value": price, **evidence}
            )
    return {
        "schema_version": 1,
        "data_kind": "synthetic",
        "sources": {
            name: {
                "source": "synthetic",
                "version": "1",
                "coverage_start": iso_days[0],
                "coverage_end": iso_days[-1],
                **evidence,
            }
            for name in ("calendar", "raw_bars", "corporate_actions", "derived")
        },
        "symbol_coverage": {
            day_symbol: {"start": iso_days[0], "end": iso_days[-1]}
            for day_symbol in SYMBOLS
        },
        "derivation_rule_version": "synthetic-total-return-v1",
        "calendar": [{"date": day, **evidence} for day in iso_days],
        "raw_bars": raw_bars,
        "corporate_actions": [
            {
                "date": ex_date,
                "symbol": symbol,
                "type": "cash_dividend",
                "cash_per_share": old_amount,
                "pay_date": pay_date,
                "terms_evidence": "synthetic dividend terms",
                **evidence,
            }
        ],
        "derived": derived,
    }


def correction_journal(
    root: Path,
    sessions: list[str] | None = None,
    *,
    symbol: str = DIVIDEND_SYMBOL,
    old_amount: float = OLD_AMOUNT,
    new_amount: float = NEW_AMOUNT,
    ex_date: str = EX_DATE,
    pay_date: str = PAY_DATE,
    tag: str = "official",
    filed_at: str | None = None,
) -> dict:
    """Seal a predecessor, register a pending observation and verify the revision.

    Returns the store path plus the verified revision record.  ``filed_at`` pins
    the journal filing instant for cutoff tests; the revision ID stays the
    content address of the revision content.
    """
    sessions = sessions or SESSIONS
    root.mkdir(parents=True, exist_ok=True)
    sealed = seal_snapshot(
        root, correction_snapshot(sessions, symbol=symbol, old_amount=old_amount,
                                  ex_date=ex_date, pay_date=pay_date),
        f"corrections-{tag}",
    )
    store = sealed["store"]
    filed = _file_revision(
        root, store, sealed["snapshot_id"],
        symbol=symbol, old_amount=old_amount, new_amount=new_amount,
        ex_date=ex_date, pay_date=pay_date, tag=tag, filed_at=filed_at,
    )
    return {"store": store, "snapshot_id": sealed["snapshot_id"], **filed}


def _file_revision(
    root: Path,
    store: Path,
    snapshot_id: str,
    *,
    symbol: str,
    old_amount: float,
    new_amount: float,
    ex_date: str,
    pay_date: str,
    tag: str,
    filed_at: str | None = None,
) -> dict:
    """Register and verify one revision into an existing journal store."""
    proposal = {
        "schema_version": 1,
        "predecessor_id": snapshot_id,
        "symbol": symbol,
        "ex_date": ex_date,
        "currency": "CNY",
        "old_amount": old_amount,
        "new_amount": new_amount,
        "supplier": {
            "source": "synthetic supplier",
            "archive_reference": f"synthetic-archive:{tag}",
            "observed_at": SUPPLIER_OBSERVED_AT,
        },
    }
    pending = ok(
        cli(
            "snapshot", "correction", "register",
            "--input", str(write_json(root / f"proposal-{tag}.json", proposal)),
            "--store", str(store),
        )
    )
    notice = {
        "kind": "synthetic",
        "authority": "fund_manager",
        "issuer": "Synthetic Fund Manager",
        "url": "https://notices.example.invalid/dividend",
        "reference": f"synthetic-notice-{tag}",
        "symbol": symbol,
        "ex_date": ex_date,
        "currency": "CNY",
        "amount": new_amount,
        "available_at": NOTICE_AVAILABLE_AT,
        "collected_at": NOTICE_COLLECTED_AT,
    }
    verified = ok(
        cli(
            "snapshot", "correction", "verify",
            "--input", str(write_json(root / f"notice-{tag}.json", notice)),
            "--store", str(store),
            "--proposal-id", pending["proposal_id"],
        )
    )
    if filed_at is not None:
        _pin_filed_at(store, verified["revision_id"], filed_at)
    return {"revision_id": verified["revision_id"], "revision": verified["revision"]}


def _pin_filed_at(store: Path, revision_id: str, filed_at: str) -> None:
    """Pin a synthetic journal filing instant (test clock control).

    The journal record is not itself content-addressed; its revision is.  The
    trusted loader verifies ``sha256(revision) == revision_id`` and the verified
    status, so pinning ``filed_at`` simulates when the trusted side filed the
    official revision without changing its identity.
    """
    path = store / ".dividend-corrections" / f"revision-{revision_id}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["revision_id"] == revision_id
    record["filed_at"] = filed_at
    path.chmod(0o644)
    path.write_text(json.dumps(record), encoding="utf-8")
    path.chmod(0o400)


# ---------------------------------------------------------------------------
# Shadow helpers
# ---------------------------------------------------------------------------


def shadow_update_with_corrections(
    ledger: Path,
    store: Path,
    data: Path,
    date: str,
    *,
    corrections: Path | None = None,
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
    if corrections is not None:
        arguments += ["--corrections-store", str(corrections)]
    for release_id in release_ids:
        arguments += ["--release-id", release_id]
    return cli(*arguments, env=env)


def scenario(
    entry: dict,
    root: Path,
    *,
    sessions: list[str] | None = None,
    actions: list[dict] | None = None,
    closes: dict[str, list[float | None]] | None = None,
    opens: dict[str, list[float | None]] | None = None,
    derived: dict[str, list[float | None]] | None = None,
) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    store = isolated_store(root, entry)
    sessions = sessions or SESSIONS
    feed = write_feed(
        root / "feed.json",
        shadow_feed(sessions, actions=actions, closes=closes, opens=opens, derived=derived),
    )
    ledger = root / "ledger"
    ok(lifecycle("enable", store, entry["release_id"], sessions[0]))
    return {"store": store, "feed": feed, "ledger": ledger, "sessions": sessions}


def dividend_action(*, pay_date: str = PAY_DATE, amount: float = OLD_AMOUNT,
                    symbol: str = DIVIDEND_SYMBOL, date: str = EX_DATE) -> dict:
    return {
        "date": date,
        "symbol": symbol,
        "type": "cash_dividend",
        "cash_per_share": amount,
        "pay_date": pay_date,
    }


# ---------------------------------------------------------------------------
# 1. Pre-payment correction: receivable is adjusted once, then paid at 0.18.
# ---------------------------------------------------------------------------


def test_pre_payment_correction_adjusts_receivable_and_payment_is_net(
    releases: dict, tmp_path: Path
) -> None:
    entry = releases["equal_weight"]
    release_id = entry["release_id"]
    context = scenario(entry, tmp_path / "pre-payment", actions=[dividend_action()])
    journal = correction_journal(tmp_path / "journal")

    ok(shadow_update_with_corrections(context["ledger"], context["store"],
                                      context["feed"], EX_DATE))
    ex_date_day = day_record(
        context["ledger"], release_id, tmp_path / "report-ex.json", EX_DATE
    )["result"]
    entitlement = ex_date_day["dividends"]["entitlements"][0]
    quantity = entitlement["quantity"]
    stale_impact = (NEW_AMOUNT - OLD_AMOUNT) * quantity

    corrected = ok(
        shadow_update_with_corrections(
            context["ledger"], context["store"], context["feed"], CORRECTION_DAY,
            corrections=journal["store"],
        )
    )
    assert corrected["versions"][0]["status"] == "committed"
    day = day_record(
        context["ledger"], release_id, tmp_path / "report-correction.json", CORRECTION_DAY
    )["result"]
    assert len(day["corrections"]) == 1
    correction = day["corrections"][0]
    assert correction["revision_id"] == journal["revision_id"]
    assert correction["status"] == "applied"
    assert correction["target"] == "receivable"
    assert correction["action_id"] == entitlement["action_id"]
    assert correction["symbol"] == DIVIDEND_SYMBOL
    assert correction["ex_date"] == EX_DATE
    assert correction["pay_date"] == PAY_DATE
    assert correction["quantity"] == pytest.approx(quantity)
    assert correction["booked_cash_per_share"] == pytest.approx(OLD_AMOUNT)
    assert correction["old_amount"] == pytest.approx(OLD_AMOUNT)
    assert correction["new_amount"] == pytest.approx(NEW_AMOUNT)
    assert correction["delta"] == pytest.approx(NEW_AMOUNT - OLD_AMOUNT)
    assert correction["amount"] == pytest.approx(stale_impact)
    assert correction["action_reference"]["action_id"] == entitlement["action_id"]
    assert correction["action_reference"]["cash_per_share"] == pytest.approx(OLD_AMOUNT)
    assert day["correction_impact"] == pytest.approx(stale_impact)
    assert day["dividend_receivables"] == pytest.approx(
        ex_date_day["dividend_receivables"] + stale_impact
    )
    # The correction is shown separately from strategy P&L: flat prices means the
    # whole one-day NAV change is exactly the correction impact.
    assert day["nav"] == pytest.approx(ex_date_day["nav"] + stale_impact)
    assert day["fills"] == [] and day["signal"] is None

    paid = ok(
        shadow_update_with_corrections(
            context["ledger"], context["store"], context["feed"], PAY_DATE,
            corrections=journal["store"],
        )
    )
    assert paid["versions"][0]["status"] == "committed"
    pay_day = day_record(
        context["ledger"], release_id, tmp_path / "report-paid.json", PAY_DATE
    )["result"]
    payment = pay_day["dividends"]["payments"][0]
    assert payment["amount"] == pytest.approx(NEW_AMOUNT * quantity)
    assert pay_day["dividends"]["receivables"] == []
    assert pay_day["dividend_receivables"] == 0.0
    assert pay_day["dividends"]["cash_total"] == pytest.approx(NEW_AMOUNT * quantity)
    assert pay_day["cash"] == pytest.approx(NEW_AMOUNT * quantity)
    # The original ex-date entitlement is never rewritten and no second
    # deduction happens on payment.
    stored_ex = day_record(
        context["ledger"], release_id, tmp_path / "report-ex-again.json", EX_DATE
    )["result"]
    assert stored_ex["dividends"]["entitlements"][0]["cash_per_share"] == pytest.approx(
        OLD_AMOUNT
    )
    assert stored_ex["dividends"]["entitlements"][0]["amount"] == pytest.approx(
        OLD_AMOUNT * quantity
    )
    assert pay_day["corrections"] == []

    # The audit report carries the correction separately from strategy P&L and
    # keeps the original action reference; no committed ex-date row changed.
    ok(shadow_report(context["ledger"], release_id, tmp_path / "report.json"))
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    summary = report["summary"]
    assert summary["corrections_total"] == 1
    assert summary["corrections"][0]["revision_id"] == journal["revision_id"]
    assert summary["corrections"][0]["action_reference"]["action_id"] == (
        entitlement["action_id"]
    )
    assert summary["corrections"][0]["action_reference"]["cash_per_share"] == (
        pytest.approx(OLD_AMOUNT)
    )
    assert summary["correction_impact_total"] == pytest.approx(stale_impact)
    ex_entry = next(entry for entry in report["days"] if entry["date"] == EX_DATE)
    assert ex_entry["result"]["dividends"]["entitlements"][0]["cash_per_share"] == (
        pytest.approx(OLD_AMOUNT)
    )
    assert ex_entry["result"]["corrections"] == []


# ---------------------------------------------------------------------------
# 2. Post-payment correction: cash is reduced once, committed history is kept.
# ---------------------------------------------------------------------------


def test_post_payment_correction_adjusts_cash_and_keeps_prior_days(
    releases: dict, tmp_path: Path
) -> None:
    entry = releases["equal_weight"]
    release_id = entry["release_id"]
    paid_correction_day = SESSIONS[3]
    context = scenario(
        entry,
        tmp_path / "post-payment",
        actions=[dividend_action(pay_date=paid_correction_day)],
    )
    ok(shadow_update_with_corrections(context["ledger"], context["store"],
                                      context["feed"], paid_correction_day))
    paid_day = day_record(
        context["ledger"], release_id, tmp_path / "report-paid.json", paid_correction_day
    )["result"]
    entitlement = day_record(
        context["ledger"], release_id, tmp_path / "report-ex.json", EX_DATE
    )["result"]["dividends"]["entitlements"][0]
    quantity = entitlement["quantity"]
    assert paid_day["dividends"]["payments"][0]["amount"] == pytest.approx(
        OLD_AMOUNT * quantity
    )
    assert paid_day["dividends"]["cash_total"] == pytest.approx(OLD_AMOUNT * quantity)
    with sqlite3.connect(context["ledger"] / "shadow.sqlite") as connection:
        history_before = connection.execute(
            "SELECT date, result_json, result_hash FROM days "
            "WHERE version_id = ? ORDER BY date",
            (release_id,),
        ).fetchall()

    journal = correction_journal(tmp_path / "journal")
    later = SESSIONS[4]
    corrected = ok(
        shadow_update_with_corrections(
            context["ledger"], context["store"], context["feed"], later,
            corrections=journal["store"],
        )
    )
    assert corrected["versions"][0]["status"] == "committed"
    day = day_record(context["ledger"], release_id, tmp_path / "report-later.json", later)["result"]
    assert len(day["corrections"]) == 1
    correction = day["corrections"][0]
    assert correction["target"] == "cash"
    assert correction["amount"] == pytest.approx(
        (NEW_AMOUNT - OLD_AMOUNT) * quantity
    )
    assert day["correction_impact"] == pytest.approx(
        (NEW_AMOUNT - OLD_AMOUNT) * quantity
    )
    assert day["dividends"]["receivables"] == []
    assert day["cash"] == pytest.approx(NEW_AMOUNT * quantity)
    assert day["dividends"]["cash_total"] == pytest.approx(NEW_AMOUNT * quantity)
    assert day["nav"] == pytest.approx(paid_day["nav"] + day["correction_impact"])
    assert day["fills"] == [] and day["signal"] is None
    # The committed payment, ex-date entitlement and NAV history are untouched.
    with sqlite3.connect(context["ledger"] / "shadow.sqlite") as connection:
        history_after = connection.execute(
            "SELECT date, result_json, result_hash FROM days "
            "WHERE version_id = ? AND date <= ? ORDER BY date",
            (release_id, paid_correction_day),
        ).fetchall()
    assert history_after == history_before
    stored_paid = day_record(
        context["ledger"], release_id, tmp_path / "report-paid-again.json",
        paid_correction_day,
    )["result"]
    assert stored_paid["dividends"]["payments"][0]["amount"] == pytest.approx(
        OLD_AMOUNT * quantity
    )


# ---------------------------------------------------------------------------
# 3. Already applied at 0.18: a rerun and a duplicate notice add nothing.
# ---------------------------------------------------------------------------


def test_duplicate_notice_and_rerun_never_double_apply(
    releases: dict, tmp_path: Path
) -> None:
    entry = releases["equal_weight"]
    release_id = entry["release_id"]
    context = scenario(entry, tmp_path / "duplicate", actions=[dividend_action()])
    ok(shadow_update_with_corrections(context["ledger"], context["store"],
                                      context["feed"], EX_DATE))
    journal = correction_journal(tmp_path / "journal")
    ok(
        shadow_update_with_corrections(
            context["ledger"], context["store"], context["feed"], CORRECTION_DAY,
            corrections=journal["store"],
        )
    )
    first = day_record(
        context["ledger"], release_id, tmp_path / "report-first.json", CORRECTION_DAY
    )["result"]
    assert len(first["corrections"]) == 1
    first_hash = [
        row for row in ledger_rows(context["ledger"], "days")
        if row[1] == CORRECTION_DAY
    ][0][3]

    rerun = ok(
        shadow_update_with_corrections(
            context["ledger"], context["store"], context["feed"], CORRECTION_DAY,
            corrections=journal["store"],
        )
    )
    assert rerun["versions"][0]["status"] == "already_committed"
    assert rerun["versions"][0]["result_hash"] == first_hash
    stored = day_record(
        context["ledger"], release_id, tmp_path / "report-first-again.json",
        CORRECTION_DAY,
    )["result"]
    assert stored == first

    # A second official notice for the same revision is a duplicate notification.
    notice = {
        "kind": "synthetic",
        "authority": "exchange",
        "issuer": "Synthetic Exchange",
        "url": "https://notices.example.invalid/duplicate",
        "reference": "synthetic-notice-duplicate",
        "symbol": DIVIDEND_SYMBOL,
        "ex_date": EX_DATE,
        "currency": "CNY",
        "amount": NEW_AMOUNT,
        "available_at": NOTICE_AVAILABLE_AT,
        "collected_at": NOTICE_COLLECTED_AT,
    }
    # Re-verifying the same proposal with a second notice keeps the same revision.
    proposal = json.loads(
        (tmp_path / "journal" / "proposal-official.json").read_text(encoding="utf-8")
    )
    pending = ok(
        cli(
            "snapshot", "correction", "register",
            "--input", str(tmp_path / "journal" / "proposal-official.json"),
            "--store", str(journal["store"]),
        )
    )
    assert pending["proposal_id"] == snapshot._hash(proposal)
    duplicate = ok(
        cli(
            "snapshot", "correction", "verify",
            "--input", str(write_json(tmp_path / "journal" / "notice-duplicate.json", notice)),
            "--store", str(journal["store"]),
            "--proposal-id", pending["proposal_id"],
        )
    )
    assert duplicate["revision_id"] == journal["revision_id"]

    paid = ok(
        shadow_update_with_corrections(
            context["ledger"], context["store"], context["feed"], PAY_DATE,
            corrections=journal["store"],
        )
    )
    assert paid["versions"][0]["status"] == "committed"
    pay_day = day_record(
        context["ledger"], release_id, tmp_path / "report-pay.json", PAY_DATE
    )["result"]
    assert pay_day["corrections"] == []
    assert pay_day["dividends"]["cash_total"] == pytest.approx(
        NEW_AMOUNT * first["corrections"][0]["quantity"]
    )
    report = shadow_report(context["ledger"], release_id, tmp_path / "report.json")
    ok(report)
    stored_report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert stored_report["summary"]["corrections_total"] == 1
    assert stored_report["summary"]["correction_impact_total"] == pytest.approx(
        first["corrections"][0]["amount"]
    )


# ---------------------------------------------------------------------------
# 4. The after-close cutoff: a later filing waits for the next uncommitted day.
# ---------------------------------------------------------------------------


def test_correction_filed_after_the_close_cutoff_waits_for_the_next_session(
    releases: dict, tmp_path: Path
) -> None:
    entry = releases["equal_weight"]
    release_id = entry["release_id"]
    context = scenario(entry, tmp_path / "cutoff", actions=[dividend_action()])
    ok(shadow_update_with_corrections(context["ledger"], context["store"],
                                      context["feed"], EX_DATE))
    journal = correction_journal(
        tmp_path / "journal",
        filed_at=f"{CORRECTION_DAY}T16:00:00+08:00",
    )

    cutoff_day = ok(
        shadow_update_with_corrections(
            context["ledger"], context["store"], context["feed"], CORRECTION_DAY,
            corrections=journal["store"],
        )
    )
    assert cutoff_day["versions"][0]["status"] == "committed"
    before = day_record(
        context["ledger"], release_id, tmp_path / "report-cutoff.json", CORRECTION_DAY
    )
    assert before["result"]["corrections"] == []
    with sqlite3.connect(context["ledger"] / "shadow.sqlite") as connection:
        cutoff_row = connection.execute(
            "SELECT result_json, result_hash FROM days "
            "WHERE version_id = ? AND date = ?",
            (release_id, CORRECTION_DAY),
        ).fetchone()

    next_day = SESSIONS[4]
    applied = ok(
        shadow_update_with_corrections(
            context["ledger"], context["store"], context["feed"], next_day,
            corrections=journal["store"],
        )
    )
    assert applied["versions"][0]["status"] == "committed"
    day = day_record(
        context["ledger"], release_id, tmp_path / "report-next.json", next_day
    )["result"]
    assert len(day["corrections"]) == 1
    assert day["corrections"][0]["filed_at"] == f"{CORRECTION_DAY}T16:00:00+08:00"
    # The already committed session is never rewritten by the later filing.
    with sqlite3.connect(context["ledger"] / "shadow.sqlite") as connection:
        assert connection.execute(
            "SELECT result_json, result_hash FROM days "
            "WHERE version_id = ? AND date = ?",
            (release_id, CORRECTION_DAY),
        ).fetchone() == cutoff_row


# ---------------------------------------------------------------------------
# 5. Crash before the commit reuses the frozen correction set exactly once.
# ---------------------------------------------------------------------------


def test_crash_before_commit_reuses_the_frozen_correction_and_commits_once(
    releases: dict, tmp_path: Path
) -> None:
    entry = releases["equal_weight"]
    release_id = entry["release_id"]
    root = tmp_path / "crash"
    context = scenario(entry, root, actions=[dividend_action()])
    journal = correction_journal(tmp_path / "journal")
    ok(shadow_update_with_corrections(context["ledger"], context["store"],
                                      context["feed"], EX_DATE))

    baseline = tmp_path / "baseline"
    shutil.copytree(context["ledger"], baseline)
    ok(
        shadow_update_with_corrections(
            baseline, context["store"], context["feed"], CORRECTION_DAY,
            corrections=journal["store"],
        )
    )

    crashed = shadow_update_with_corrections(
        context["ledger"], context["store"], context["feed"], CORRECTION_DAY,
        corrections=journal["store"],
        env=cli_env(PRAJNATRADE_TEST_FAILPOINT="shadow_after_fills"),
    )
    assert crashed.returncode != 0, (crashed.stdout, crashed.stderr)
    with sqlite3.connect(context["ledger"] / "shadow.sqlite") as connection:
        frozen = connection.execute(
            "SELECT input_json FROM input_snapshots WHERE version_id = ? AND date = ?",
            (release_id, CORRECTION_DAY),
        ).fetchone()
        committed = connection.execute(
            "SELECT date FROM days WHERE version_id = ? ORDER BY date", (release_id,)
        ).fetchall()
    assert [row[0] for row in committed] == SESSIONS[:3]
    frozen_input = json.loads(frozen[0])
    assert len(frozen_input["corrections"]) == 1
    assert frozen_input["corrections"][0]["revision_id"] == journal["revision_id"]
    assert frozen_input["corrections"][0]["target"] == "receivable"

    # A revision filed after the input was frozen does not change that decision.
    _file_revision(
        tmp_path / "journal",
        journal["store"],
        journal["snapshot_id"],
        symbol=DIVIDEND_SYMBOL,
        old_amount=0.20,
        new_amount=0.19,
        ex_date=EX_DATE,
        pay_date=PAY_DATE,
        tag="filed-after-freeze",
    )

    recovered = ok(shadow_update_with_corrections(context["ledger"], context["store"],
                                                  context["feed"], CORRECTION_DAY,
                                                  corrections=journal["store"]))
    assert recovered["versions"][0]["status"] == "committed"
    with sqlite3.connect(baseline / "shadow.sqlite") as reference:
        reference_rows = reference.execute(
            "SELECT date, result_json, result_hash FROM days "
            "WHERE version_id = ? ORDER BY date",
            (release_id,),
        ).fetchall()
    with sqlite3.connect(context["ledger"] / "shadow.sqlite") as connection:
        recovered_rows = connection.execute(
            "SELECT date, result_json, result_hash FROM days "
            "WHERE version_id = ? ORDER BY date",
            (release_id,),
        ).fetchall()
    assert recovered_rows == reference_rows
    day = json.loads(reference_rows[-1][1])
    assert len(day["corrections"]) == 1
    assert day["corrections"][0]["amount"] == pytest.approx(
        (NEW_AMOUNT - OLD_AMOUNT) * day["corrections"][0]["quantity"]
    )

    # A rerun after the commit reads the stored day and never double-applies.
    after = ok(
        shadow_update_with_corrections(
            context["ledger"], context["store"], context["feed"], CORRECTION_DAY,
            corrections=journal["store"],
        )
    )
    assert after["versions"][0]["status"] == "already_committed"
    assert after["versions"][0]["corrections"] == day["corrections"]
    with sqlite3.connect(context["ledger"] / "shadow.sqlite") as connection:
        assert connection.execute(
            "SELECT date, result_json, result_hash FROM days "
            "WHERE version_id = ? ORDER BY date",
            (release_id,),
        ).fetchall() == recovered_rows
    stored = shadow_report(context["ledger"], release_id, tmp_path / "report.json")
    ok(stored)
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["summary"]["corrections_total"] == 1


# ---------------------------------------------------------------------------
# 6. No booked entitlement means no correction; an amount mismatch blocks.
# ---------------------------------------------------------------------------


def test_unheld_correction_is_not_applied(releases: dict, tmp_path: Path) -> None:
    entry = releases["momentum"]
    release_id = entry["release_id"]
    rising = [10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7]
    context = scenario(
        entry,
        tmp_path / "unheld",
        closes={MOMENTUM_SYMBOL: rising},
        derived={MOMENTUM_SYMBOL: rising},
        actions=[dividend_action(symbol=DIVIDEND_SYMBOL)],
    )
    journal = correction_journal(tmp_path / "journal", symbol=DIVIDEND_SYMBOL)
    ex_day = ok(
        shadow_update_with_corrections(context["ledger"], context["store"],
                                       context["feed"], EX_DATE)
    )
    assert ex_day["versions"][0]["status"] == "committed"
    booking = day_record(
        context["ledger"], release_id, tmp_path / "report-ex.json", EX_DATE
    )["result"]
    assert booking["corporate_actions"][0]["status"] == "ignored_not_held"
    assert booking["dividends"]["entitlements"] == []

    later = ok(
        shadow_update_with_corrections(
            context["ledger"], context["store"], context["feed"], CORRECTION_DAY,
            corrections=journal["store"],
        )
    )
    assert later["versions"][0]["status"] == "committed"
    day = day_record(
        context["ledger"], release_id, tmp_path / "report-later.json", CORRECTION_DAY
    )["result"]
    assert day["corrections"] == []
    assert day["correction_impact"] == 0.0


def test_booked_amount_mismatch_blocks_without_a_partial_day(
    releases: dict, tmp_path: Path
) -> None:
    entry = releases["equal_weight"]
    release_id = entry["release_id"]
    root = tmp_path / "mismatch"
    context = scenario(
        entry,
        root,
        actions=[dividend_action(amount=0.25)],
    )
    journal = correction_journal(tmp_path / "journal")
    ok(shadow_update_with_corrections(context["ledger"], context["store"],
                                      context["feed"], EX_DATE))

    blocked = shadow_update_with_corrections(
        context["ledger"], context["store"], context["feed"], CORRECTION_DAY,
        corrections=journal["store"],
    )
    assert blocked.returncode == 2, blocked.stdout
    version = json.loads(blocked.stdout)["versions"][0]
    assert version["status"] == "blocked"
    assert version["reason_code"] == "CORRECTION_AMOUNT_MISMATCH"
    assert "old_amount" in version["reason"]
    assert version["committed_date"] == EX_DATE
    with sqlite3.connect(context["ledger"] / "shadow.sqlite") as connection:
        assert [
            row[0]
            for row in connection.execute(
                "SELECT date FROM days WHERE version_id = ? ORDER BY date",
                (release_id,),
            )
        ] == SESSIONS[:3]
        assert connection.execute(
            "SELECT 1 FROM input_snapshots WHERE version_id = ? AND date = ?",
            (release_id, CORRECTION_DAY),
        ).fetchone() is None
    assert ledger_rows(context["ledger"], "progress")[0][3] == EX_DATE

    # Removing the unsatisfiable revision lets the same day commit, proving the
    # block wrote no partial day or frozen input.
    revision_path = (
        journal["store"] / ".dividend-corrections"
        / f"revision-{journal['revision_id']}.json"
    )
    revision_path.chmod(0o644)
    revision_path.unlink()
    recovered = ok(
        shadow_update_with_corrections(
            context["ledger"], context["store"], context["feed"], CORRECTION_DAY,
            corrections=journal["store"],
        )
    )
    assert recovered["versions"][0]["status"] == "committed"
    day = day_record(
        context["ledger"], release_id, tmp_path / "report-recovered.json", CORRECTION_DAY
    )["result"]
    assert day["corrections"] == []


# ---------------------------------------------------------------------------
# 7. A correction-caused cash deficit: no financing, no forced sale.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def inverse_release(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """A real approved inverse-volatility release that rebalances every day."""
    return build_release(
        tmp_path_factory.mktemp("correction-inverse"),
        synthetic_snapshot(derived_range=("2024-01-13", "2024-01-20")),
        "inverse",
        "inverse_volatility",
        {"lookback": 2},
    )


DEFICIT_EX_DATE = SESSIONS[4]
DEFICIT_PAY_DATE = SESSIONS[5]
DEFICIT_CORRECTION_DAY = SESSIONS[7]


def test_correction_caused_negative_cash_is_a_deficit_and_buys_fail(
    inverse_release: dict, tmp_path: Path
) -> None:
    entry = inverse_release
    release_id = entry["release_id"]
    closes = {symbol: [10.0] * len(SESSIONS) for symbol in SYMBOLS}
    closes[DIVIDEND_SYMBOL] = [10.0, 10.0, 10.0, 10.0, 9.8, 9.8, 9.8, 9.8]
    closes["515080"] = [10.0] * len(SESSIONS)
    closes["515080"][6] = 10.01
    calm = [10.0, 10.01, 9.99, 10.01, 9.99, 10.01, 9.99, 10.01]
    volatile = [10.0, 10.02, 10.01, 10.03, 10.02, 10.04, 10.03, 10.05]
    derived = {symbol: list(calm) for symbol in SYMBOLS}
    derived[MOMENTUM_SYMBOL] = list(volatile)
    context = scenario(
        entry,
        tmp_path / "deficit",
        closes=closes,
        derived=derived,
        actions=[
            dividend_action(
                date=DEFICIT_EX_DATE, pay_date=DEFICIT_PAY_DATE, amount=OLD_AMOUNT
            )
        ],
    )
    ok(
        shadow_update_with_corrections(
            context["ledger"], context["store"], context["feed"], SESSIONS[6]
        )
    )
    ex_day = day_record(
        context["ledger"], release_id, tmp_path / "report-ex.json", DEFICIT_EX_DATE
    )["result"]
    entitlement = ex_day["dividends"]["entitlements"][0]
    quantity = entitlement["quantity"]
    before = day_record(
        context["ledger"], release_id, tmp_path / "report-before.json", SESSIONS[6]
    )["result"]
    assert before["cash"] == pytest.approx(0.0, abs=1e-6)
    pending_sells = {
        order["order_id"]: order for order in before["next_orders"]
        if order["side"] == "sell"
    }
    assert pending_sells

    journal = correction_journal(
        tmp_path / "journal",
        ex_date=DEFICIT_EX_DATE,
        pay_date=DEFICIT_PAY_DATE,
    )
    result = shadow_update_with_corrections(
        context["ledger"], context["store"], context["feed"],
        DEFICIT_CORRECTION_DAY, corrections=journal["store"],
    )
    payload = json.loads(result.stdout)
    assert result.returncode == 0, (result.stdout, result.stderr)
    version = payload["versions"][0]
    assert version["status"] == "committed_with_anomalies"
    day = day_record(
        context["ledger"], release_id, tmp_path / "report-deficit.json",
        DEFICIT_CORRECTION_DAY,
    )["result"]
    correction = day["corrections"][0]
    assert correction["target"] == "cash"
    assert correction["amount"] == pytest.approx(
        (NEW_AMOUNT - OLD_AMOUNT) * quantity
    )
    assert day["cash"] < 0
    deficit = day["cash_deficit"]
    assert deficit["amount"] == pytest.approx(-day["cash"])
    assert deficit["caused_by"] == "dividend_correction"
    assert deficit["revision_ids"] == [journal["revision_id"]]
    assert deficit["financed"] is False
    assert deficit["forced_sale"] is False
    assert version["cash_deficit"]["amount"] == pytest.approx(deficit["amount"])
    assert any(anomaly["code"] == "cash_deficit" for anomaly in day["anomalies"])

    # Every buy that was pending at the open fails for insufficient cash; the
    # only sell is the one already submitted at the prior session's signal (a
    # pre-existing order, never a forced sale), and no new orders are invented.
    failed_buys = [order for order in day["orders"] if order["status"] != "filled"]
    assert failed_buys
    assert {order["side"] for order in failed_buys} == {"buy"}
    assert {order["status"] for order in failed_buys} == {"insufficient_cash"}
    filled_sells = [order for order in day["orders"] if order["status"] == "filled"]
    assert {order["order_id"] for order in filled_sells} <= set(pending_sells)
    assert {order["side"] for order in filled_sells} == {"sell"}
    assert day["signal"] is None and day["next_orders"] == []
    assert day["market_value"] > 0  # positions were not liquidated to fund the gap

    report = shadow_report(context["ledger"], release_id, tmp_path / "report.json")
    ok(report)
    stored = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert stored["summary"]["ending_cash"] == pytest.approx(day["cash"])
    assert stored["summary"]["cash_deficits"][0]["amount"] == pytest.approx(
        deficit["amount"]
    )
    assert stored["summary"]["correction_impact_total"] == pytest.approx(
        correction["amount"]
    )


# ---------------------------------------------------------------------------
# 8. A paused version books the correction but still generates no new signal.
# ---------------------------------------------------------------------------


def test_paused_version_books_the_correction_without_new_signals(
    releases: dict, tmp_path: Path
) -> None:
    entry = releases["momentum"]
    release_id = entry["release_id"]
    rising = [10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7]
    context = scenario(
        entry,
        tmp_path / "paused-correction",
        closes={MOMENTUM_SYMBOL: rising},
        derived={MOMENTUM_SYMBOL: rising},
        actions=[
            dividend_action(
                symbol=MOMENTUM_SYMBOL, date=SESSIONS[4], pay_date=SESSIONS[5]
            )
        ],
    )
    ok(shadow_update_with_corrections(context["ledger"], context["store"],
                                      context["feed"], SESSIONS[4]))
    ex_day = day_record(
        context["ledger"], release_id, tmp_path / "report-ex.json", SESSIONS[4]
    )["result"]
    entitlement = ex_day["dividends"]["entitlements"][0]
    assert entitlement["symbol"] == MOMENTUM_SYMBOL
    journal = correction_journal(
        tmp_path / "journal",
        symbol=MOMENTUM_SYMBOL,
        ex_date=SESSIONS[4],
        pay_date=SESSIONS[5],
    )
    ok(lifecycle("pause", context["store"], release_id, SESSIONS[5]))

    paused = shadow_update_with_corrections(
        context["ledger"], context["store"], context["feed"], SESSIONS[5],
        corrections=journal["store"],
    )
    version = json.loads(paused.stdout)["versions"][0]
    assert paused.returncode == 0, paused.stdout
    assert version["status"] == "committed"
    day = day_record(
        context["ledger"], release_id, tmp_path / "report-paused.json", SESSIONS[5]
    )["result"]
    assert day["release_state_at_signal_point"] == "paused"
    assert len(day["corrections"]) == 1
    # Paid during the correction day: the receivable is adjusted before the
    # pay-date transfer, so only the adjusted amount reaches cash.
    assert day["corrections"][0]["target"] == "receivable"
    assert day["dividends"]["payments"][0]["amount"] == pytest.approx(
        NEW_AMOUNT * entitlement["quantity"]
    )
    assert day["cash"] == pytest.approx(NEW_AMOUNT * entitlement["quantity"])
    assert day["dividends"]["receivables"] == []
    # Pause stays in the release timeline: no new signal or orders are created.
    assert day["signal"] is None
    assert day["next_orders"] == []
    assert day["positions"][MOMENTUM_SYMBOL]["quantity"] > 0


# ---------------------------------------------------------------------------
# 9. No sealed payload read, no query debit, and stable ignore reasons.
# ---------------------------------------------------------------------------


def _remove_sealed_payloads(store: Path, snapshot_id: str) -> None:
    directory = store / snapshot_id
    directory.chmod(0o755)
    for partition in ("validation", "final_holdout"):
        path = directory / f"{partition}.json"
        if path.is_file():
            path.chmod(0o644)
            path.unlink()
    assert not (directory / "validation.json").exists()
    assert not (directory / "final_holdout.json").exists()


def _forge_unverified_revision(store: Path) -> tuple[str, dict]:
    revision = {
        "symbol": DIVIDEND_SYMBOL,
        "ex_date": EX_DATE,
        "currency": "CNY",
        "old_amount": OLD_AMOUNT,
        "new_amount": 0.19,
        "official_available_at": NOTICE_AVAILABLE_AT,
    }
    revision_id = snapshot._hash(revision)
    path = store / ".dividend-corrections" / f"revision-{revision_id}.json"
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "data_kind": "synthetic",
                "status": "pending_verification",
                "revision_id": revision_id,
                "revision": revision,
                "filed_at": "2026-02-02T09:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    return revision_id, revision


def test_correction_application_opens_no_sealed_payload_and_debits_no_query(
    releases: dict, tmp_path: Path
) -> None:
    entry = releases["equal_weight"]
    release_id = entry["release_id"]
    context = scenario(entry, tmp_path / "isolated", actions=[dividend_action()])
    journal = correction_journal(tmp_path / "journal")
    forged_id, _ = _forge_unverified_revision(journal["store"])
    missing_id = "f" * 64
    (
        journal["store"] / ".dividend-corrections" / f"revision-{missing_id}.json"
    ).symlink_to(tmp_path / "absent-revision.json")
    _remove_sealed_payloads(journal["store"], journal["snapshot_id"])
    assert not (context["store"] / "queries.sqlite").exists()

    ok(shadow_update_with_corrections(context["ledger"], context["store"],
                                      context["feed"], EX_DATE))
    result = shadow_update_with_corrections(
        context["ledger"], context["store"], context["feed"], CORRECTION_DAY,
        corrections=journal["store"],
    )
    payload = json.loads(result.stdout)
    assert result.returncode == 0, result.stdout
    assert payload["corrections"]["revisions_loaded"] == 1
    ignored = {entry["file"]: entry for entry in payload["corrections"]["ignored"]}
    forged_file = f"revision-{forged_id}.json"
    assert ignored[forged_file]["code"] == "REVISION_NOT_VERIFIED"
    assert ignored[f"revision-{missing_id}.json"]["code"] == "REVISION_INVALID"
    version = payload["versions"][0]
    assert version["status"] == "committed"
    assert len(version["corrections"]) == 1
    assert version["corrections"][0]["revision_id"] == journal["revision_id"]
    # Applying a correction never creates or debits a query ledger, and the
    # sealed payloads were not needed (they were removed before the update).
    assert not (context["store"] / "queries.sqlite").exists()
    assert not (journal["store"] / "queries.sqlite").exists()


def test_unavailable_corrections_store_is_rejected_before_any_day(
    releases: dict, tmp_path: Path
) -> None:
    entry = releases["equal_weight"]
    context = scenario(entry, tmp_path / "unavailable", actions=[dividend_action()])
    missing = tmp_path / "does-not-exist"
    response = shadow_update_with_corrections(
        context["ledger"], context["store"], context["feed"], EX_DATE,
        corrections=missing,
    )
    assert response.returncode == 2
    payload = json.loads(response.stdout)
    assert payload["status"] == "rejected"
    assert payload["error"]["code"] == "CORRECTIONS_STORE_UNAVAILABLE"
    assert ledger_rows(context["ledger"], "days") == []
    assert ledger_rows(context["ledger"], "input_snapshots") == []


def test_journal_without_the_corrections_flag_applies_nothing(
    releases: dict, tmp_path: Path
) -> None:
    entry = releases["equal_weight"]
    release_id = entry["release_id"]
    context = scenario(entry, tmp_path / "no-flag", actions=[dividend_action()])
    journal = correction_journal(tmp_path / "journal")
    ok(shadow_update_with_corrections(context["ledger"], context["store"],
                                      context["feed"], EX_DATE))
    # The journal exists and is verified, but the operator did not opt in.
    ok(shadow_update_with_corrections(context["ledger"], context["store"],
                                      context["feed"], CORRECTION_DAY))
    day = day_record(
        context["ledger"], release_id, tmp_path / "report-no-flag.json", CORRECTION_DAY
    )["result"]
    assert day["corrections"] == []
    # On the next uncommitted session the opted-in run applies it exactly once.
    later = ok(
        shadow_update_with_corrections(
            context["ledger"], context["store"], context["feed"], SESSIONS[4],
            corrections=journal["store"],
        )
    )
    assert later["versions"][0]["status"] == "committed"
    later_day = day_record(
        context["ledger"], release_id, tmp_path / "report-opt-in.json", SESSIONS[4]
    )["result"]
    assert len(later_day["corrections"]) == 1


# ---------------------------------------------------------------------------
# 10. A chained revision must match the amount booked after the first delta.
# ---------------------------------------------------------------------------


def test_chained_revision_matches_the_adjusted_booked_amount(
    releases: dict, tmp_path: Path
) -> None:
    entry = releases["equal_weight"]
    release_id = entry["release_id"]
    chained_ex_date = SESSIONS[4]
    chained_pay_date = SESSIONS[7]
    context = scenario(
        entry,
        tmp_path / "chained",
        actions=[
            dividend_action(date=chained_ex_date, pay_date=chained_pay_date)
        ],
    )
    journal = correction_journal(
        tmp_path / "journal",
        ex_date=chained_ex_date,
        pay_date=chained_pay_date,
    )
    ok(shadow_update_with_corrections(context["ledger"], context["store"],
                                      context["feed"], chained_ex_date))
    quantity = day_record(
        context["ledger"], release_id, tmp_path / "report-ex.json", chained_ex_date
    )["result"]["dividends"]["entitlements"][0]["quantity"]
    first_day = ok(
        shadow_update_with_corrections(
            context["ledger"], context["store"], context["feed"], SESSIONS[5],
            corrections=journal["store"],
        )
    )
    assert first_day["versions"][0]["status"] == "committed"
    first = day_record(
        context["ledger"], release_id, tmp_path / "report-first.json", SESSIONS[5]
    )["result"]["corrections"]
    assert len(first) == 1
    assert first[0]["target"] == "receivable"
    assert first[0]["booked_cash_per_share"] == pytest.approx(OLD_AMOUNT)

    # A later official revision declares old_amount 0.18: it matches the amount
    # booked after the first delta, not the original ex-date amount.
    second = _file_revision(
        tmp_path / "journal",
        journal["store"],
        journal["snapshot_id"],
        symbol=DIVIDEND_SYMBOL,
        old_amount=0.18,
        new_amount=0.17,
        ex_date=chained_ex_date,
        pay_date=chained_pay_date,
        tag="second",
    )
    later = ok(
        shadow_update_with_corrections(
            context["ledger"], context["store"], context["feed"], SESSIONS[6],
            corrections=journal["store"],
        )
    )
    assert later["versions"][0]["status"] == "committed"
    day = day_record(
        context["ledger"], release_id, tmp_path / "report-second.json", SESSIONS[6]
    )["result"]
    assert len(day["corrections"]) == 1
    correction = day["corrections"][0]
    assert correction["revision_id"] == second["revision_id"]
    assert correction["booked_cash_per_share"] == pytest.approx(0.18)
    assert correction["old_amount"] == pytest.approx(0.18)
    assert correction["new_amount"] == pytest.approx(0.17)
    assert correction["amount"] == pytest.approx((0.17 - 0.18) * quantity)

    paid = ok(
        shadow_update_with_corrections(
            context["ledger"], context["store"], context["feed"], chained_pay_date,
            corrections=journal["store"],
        )
    )
    assert paid["versions"][0]["status"] == "committed"
    pay_day = day_record(
        context["ledger"], release_id, tmp_path / "report-paid.json", chained_pay_date
    )["result"]
    assert pay_day["dividends"]["payments"][0]["amount"] == pytest.approx(
        0.17 * quantity
    )
    assert pay_day["dividends"]["cash_total"] == pytest.approx(0.17 * quantity)
    assert pay_day["corrections"] == []
    stored = shadow_report(context["ledger"], release_id, tmp_path / "report.json")
    ok(stored)
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["summary"]["corrections_total"] == 2
    assert report["summary"]["correction_impact_total"] == pytest.approx(
        (0.18 - 0.20) * quantity + (0.17 - 0.18) * quantity
    )
