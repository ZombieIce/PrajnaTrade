#!/usr/bin/env python3
"""Acquire and derive the schema-v3 real five-ETF snapshot input for issue #24.

The probe artifacts used by the research note ``docs/research/real-etf-data-sources.md``
live outside Git (``/tmp/research24`` by default).  This script turns them into the
schema-version-3 ``real.json`` accepted by ``prajnatrade snapshot seal``:

* 1,094 sessions 2022-05-20..2026-11-20 from the official SZSE monthly calendar,
  cross-checked against the five SSE annual holiday notices;
* 5,470 unadjusted vendor raw bars (Tencent ``fqkline`` with an empty adjustment
  parameter) with the official trading halts of the two QDII codes;
* 20 in-window official cash dividends from SSE fund announcements;
* 5,470 derived total-return values under ``tr-reinvest-exclose-v1``;
* a SHA-256 evidence bundle copied into the trusted (non-Git) evidence area.

Schema 3 binds every executable open to sealed evidence (ADR 0002).  A normal
session keeps the vendor's unadjusted daily open, and a halted-at-open session
whose halt ends before the close keeps that day's actual open: the row cites
the vendor price archive as ``open_source`` and the official halt notice as
``status_evidence_id``.  A session halted for the whole day becomes
``suspended`` with null prices and the notice as trading-status evidence.
``convert_raw_bars`` and ``build_snapshot_input`` are the pure, importable
conversion entry points; ``_build`` reads the probe files and delegates to them.

Vendor ``qfq``/``hfq`` series are never used.  The 09:30 open and 15:00 close
timestamps are exchange trading-rule assertions, not archived point-in-time
quotes; every row's ``availability_evidence`` says so, and the derived series
reinvests official cash dividends at the ex-date close.

Run the default (cache-based, reproducible) build::

    python3 scripts/acquire_real_etf_snapshot.py \
        --cache-dir /tmp/research24 \
        --store "$HOME/PrajnaTrade-trusted/real-etf-snapshot/store" \
        --workdir "$WORKDIR" \
        --output "$WORKDIR/real.json"

``--refetch`` downloads fresh copies of the Tencent bars, the 59 SZSE monthly
calendar responses and the announcement PDFs into ``<workdir>/refetch`` and
builds from those instead; the researched cache stays untouched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tempfile
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CN_TZ = timezone(timedelta(hours=8))
SYMBOLS = ("511260", "518880", "515080", "513300", "159612")
MARKETS = {"511260": "sh", "518880": "sh", "515080": "sh", "513300": "sh", "159612": "sz"}
WINDOW_START = "2022-05-20"
WINDOW_END = "2026-11-20"
SESSION_COUNT = 1_094
DERIVED_RULE = "tr-reinvest-exclose-v1"
DERIVATION_METHOD = "cash_dividend_reinvested_at_ex_date_close"
LISTING_DATES = {
    "511260": "2017-08-24",
    "518880": "2013-07-29",
    "515080": "2019-12-27",
    "513300": "2020-11-05",
    "159612": "2022-05-20",
}
# One SSE annual holiday notice per calendar year; the published date is the
# dated availability instrument for that year's session list.
SSE_CALENDAR_NOTICES = {
    "2022": {
        "published": "2021-12-20",
        "url": "https://www.sse.com.cn/disclosure/announcement/general/c/c_20211220_5662606.shtml",
        "file": "sse_c_20211220_5662606.html",
    },
    "2023": {
        "published": "2022-12-27",
        "url": "https://www.sse.com.cn/disclosure/announcement/general/c/c_20221227_5714458.shtml",
        "file": "sse_c_20221227_5714458.html",
    },
    "2024": {
        "published": "2023-12-26",
        "url": "https://www.sse.com.cn/disclosure/announcement/general/c/c_20231226_5733939.shtml",
        "file": "sse_c_20231226_5733939.html",
    },
    "2025": {
        "published": "2024-12-23",
        "url": "https://www.sse.com.cn/disclosure/announcement/general/c/c_20241223_10767108.shtml",
        "file": "sse_c_20241223_10767108.html",
    },
    "2026": {
        "published": "2025-12-22",
        "url": "https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml",
        "file": "sse_c_20251222_10802507.html",
    },
}
LISTING_SPECS = (
    {
        "id": "listing-511260",
        "code": "511260",
        "published": "2017-08-21",
        "title": "上市交易公告书",
        "file": "pdfs/511260_2017-08-21__10_.pdf",
        "url": (
            "https://www.sse.com.cn/disclosure/fund/announcement/c/2017-08-22/"
            "511260_20170821_2.pdf"
        ),
        "name": "listing-511260-2017-08-21.pdf",
    },
    {
        "id": "listing-518880",
        "code": "518880",
        "published": "2013-07-24",
        "title": "上市交易公告书",
        "file": "pdfs/518880_listing_20130724.pdf",
        "url": (
            "https://www.sse.com.cn/disclosure/fund/announcement/c/2013-07-23/"
            "518880_20130724_1.pdf"
        ),
        "name": "listing-518880-2013-07-24.pdf",
    },
    {
        "id": "listing-515080",
        "code": "515080",
        "published": "2019-12-24",
        "title": "上市交易公告书",
        "file": "pdfs/515080_2019-12-24__.pdf",
        "url": (
            "https://www.sse.com.cn/disclosure/fund/announcement/c/2019-12-24/"
            "515080_20191224_1.pdf"
        ),
        "name": "listing-515080-2019-12-24.pdf",
    },
    {
        "id": "listing-513300",
        "code": "513300",
        "published": "2020-11-02",
        "title": "上市交易公告书提示性公告",
        "file": "pdfs/513300_2020-11-02__100_QDII_.pdf",
        "url": (
            "https://www.sse.com.cn/disclosure/fund/announcement/c/2020-11-02/"
            "513300_20201102_3.pdf"
        ),
        "name": "listing-513300-2020-11-02.pdf",
    },
    {
        "id": "listing-159612",
        "code": "159612",
        "published": "2022-05-17",
        "title": "上市交易公告书",
        "file": "pdfs/159612_2022-05-17__500ETF_500_QDII_.pdf",
        "url": (
            "https://disc.static.szse.cn/download/disc/disk03/finalpage/2022-05-16/"
            "d3ebfae4-0dc5-4ed6-acf9-cdba35dbac47.PDF"
        ),
        "name": "listing-159612-2022-05-17.pdf",
    },
)
CONVERSION_SPECS = (
    {
        "id": "conversion-511260-2017-07-28",
        "code": "511260",
        "published": "2017-07-28",
        "title": "基金份额折算公告",
        "file": "pdfs/511260_2017-07-28__10_.pdf",
        "url": (
            "https://www.sse.com.cn/disclosure/fund/announcement/c/2017-07-28/"
            "511260_20170728_1.pdf"
        ),
        "name": "conversion-511260-2017-07-28.pdf",
    },
    {
        "id": "conversion-511260-2017-08-08",
        "code": "511260",
        "published": "2017-08-08",
        "title": "基金份额折算结果公告",
        "file": "pdfs/511260_2017-08-08__10_.pdf",
        "url": (
            "https://www.sse.com.cn/disclosure/fund/announcement/c/2017-08-08/"
            "511260_20170808_1.pdf"
        ),
        "name": "conversion-511260-2017-08-08.pdf",
    },
    {
        "id": "conversion-518880-2013-07-24",
        "code": "518880",
        "published": "2013-07-24",
        "title": "基金份额折算日的公告",
        "file": "pdfs/518880_conversion_date_20130724.pdf",
        "url": (
            "https://www.sse.com.cn/disclosure/fund/announcement/c/2013-07-23/"
            "518880_20130724_2.pdf"
        ),
        "name": "conversion-518880-2013-07-24.pdf",
    },
    {
        "id": "conversion-518880-2013-07-26",
        "code": "518880",
        "published": "2013-07-26",
        "title": "基金份额折算结果公告",
        "file": "pdfs/518880_2013-07-26__.pdf",
        "url": (
            "https://www.sse.com.cn/disclosure/fund/announcement/c/2013-07-25/"
            "518880_20130726_1.pdf"
        ),
        "name": "conversion-518880-2013-07-26.pdf",
    },
)
# 159612's 2025-01-10 halt notice says "复牌时间另行公告"; the 2025-01-13 resume
# notice says trading resumes from the 2025-01-13 open.  The 2025-01-24 notice
# says "停牌至收市".
FULL_HALT_WINDOWS = {
    ("159612", "2025-01-10"): (
        "2025-01-10T09:30:00+08:00",
        "2025-01-13T09:30:00+08:00",
    ),
    ("159612", "2025-01-24"): (
        "2025-01-24T09:30:00+08:00",
        "2025-01-24T15:00:00+08:00",
    ),
}
TDX_SESSIONS = (
    "20220520",
    "20230616",
    "20241127",
    "20250110",
    "20250124",
    "20250923",
    "20261120",
)
MONTHS = tuple(
    f"{year}-{month:02d}"
    for year in range(2022, 2027)
    for month in range(1, 13)
    if "2022-01" <= f"{year}-{month:02d}" <= "2026-11"
)
#: The vendor feed that produced every raw bar (price evidence role).
PRICE_SOURCE = "tencent:unadjusted"
#: The schema-3 evidence item id the vendor daily opens cite.
PRICE_EVIDENCE_ID = "price-tencent-unadjusted"
#: Halt kinds that close the whole session by construction.  ``full_halt_until``
#: is decided by its halt window: a same-day resume leaves an actual open (the
#: vendor's daily bar), while a resume on a later session covers the whole day.
FULL_DAY_HALT_KINDS = ("full_halt", "full_halt_open_end")
#: The three derivation-evidence statements a re-acquired snapshot records.
DERIVATION_EVIDENCE = (
    (
        "Rule tr-reinvest-exclose-v1: TR[2022-05-20]=1.0; "
        "TR[t]=TR[t-1]*(close[t]+D[t])/close[t-1], where D[t] is the "
        "official per-unit cash dividend with ex-date t; null on "
        "suspended sessions; no share adjustment in the window."
    ),
    (
        "20 in-window official SSE dividend announcements "
        "(511260 x5, 515080 x15) are evidence kind dividend."
    ),
    (
        "Unadjusted Tencent daily closes cross-checked against TDX "
        "g4day dated packages on sampled sessions; no vendor qfq/hfq "
        "series is used."
    ),
)


class AcquisitionError(RuntimeError):
    """A probe artifact is missing or contradicts the researched fact set."""


def _iso(moment: datetime) -> str:
    return moment.astimezone(CN_TZ).isoformat(timespec="seconds")


def _at(day: str, hour: int, minute: int = 0) -> str:
    return f"{day}T{hour:02d}:{minute:02d}:00+08:00"


def _day_end(day: str) -> str:
    return f"{day}T23:59:59+08:00"


def _mtime(path: Path) -> str:
    return _iso(datetime.fromtimestamp(path.stat().st_mtime, CN_TZ))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _artifact(cache: Path, refetch: Path | None, relative: str) -> Path:
    """Resolve one artifact, preferring a refetched copy when one exists."""
    if refetch is not None and (refetch / relative).is_file():
        return refetch / relative
    path = cache / relative
    if not path.is_file():
        raise AcquisitionError(f"Missing probe artifact: {path}")
    return path


def _fetch(url: str, *, referer: str | None = None) -> bytes:
    headers = {"User-Agent": "Mozilla/5.0 (PrajnaTrade issue 24 acquisition)"}
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def _fetch_json(url: str, *, referer: str | None = None) -> Any:
    return json.loads(_fetch(url, referer=referer).decode("utf-8"))


def _date_windows(start: str, end: str, days: int) -> list[tuple[str, str]]:
    begin = date.fromisoformat(start)
    last = date.fromisoformat(end)
    windows = []
    while begin <= last:
        finish = min(begin + timedelta(days=days), last)
        windows.append((begin.isoformat(), finish.isoformat()))
        begin = finish + timedelta(days=1)
    return windows


def _fetch_tencent_bars() -> dict[str, dict[str, list[str]]]:
    """Download unadjusted daily bars for the common window."""
    result: dict[str, dict[str, list[str]]] = {}
    for symbol in SYMBOLS:
        market = MARKETS[symbol]
        rows: dict[str, list[str]] = {}
        for begin, finish in _date_windows(WINDOW_START, WINDOW_END, 700):
            param = f"{market}{symbol},day,{begin},{finish},640,"
            payload = _fetch_json(
                "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=" + param,
                referer="https://gu.qq.com/",
            )
            block = payload.get("data", {}).get(f"{market}{symbol}", {})
            for bar in block.get("day", []):
                rows[bar[0]] = [str(value) for value in bar[:6]]
        if not rows:
            raise AcquisitionError(f"Tencent returned no bars for {symbol}")
        result[symbol] = rows
    return result


def _fetch_calendar() -> dict[str, Any]:
    months: dict[str, list[str]] = {}
    for label in MONTHS:
        payload = _fetch_json(
            "https://www.szse.cn/api/report/exchange/onepersistenthour/monthList"
            f"?month={label}",
            referer="https://www.szse.cn/",
        )
        months[label] = sorted(
            row["jyrq"] for row in payload.get("data", []) if row.get("jybz") == "1"
        )
    return {"months": months, "all": sorted(day for days in months.values() for day in days)}


def _halt_announcement_index(announcements: dict[str, Any]) -> dict[tuple[str, str], str]:
    """Map (code, announcement date) to the official halt-notice PDF URL."""
    index: dict[tuple[str, str], list[str]] = {}
    for code, block in announcements["sse"].items():
        for row in block["rows"]:
            if re.search(r"停(复)?牌", row["TITLE"]):
                index.setdefault((code, row["SSEDATE"]), []).append(
                    "https://www.sse.com.cn" + row["URL"]
                )
    for row in announcements["szse"]:
        if re.search(r"停(复)?牌", row["title"]):
            code = row["secCode"][0]
            index.setdefault((code, row["publishTime"][:10]), []).append(
                "https://disc.static.szse.cn/download" + row["attachPath"]
            )
    duplicate = sorted(key for key, urls in index.items() if len(urls) != 1)
    if duplicate:
        raise AcquisitionError(f"Ambiguous halt announcement mapping: {duplicate}")
    return {key: urls[0] for key, urls in index.items()}


def _resume_announcement_url(announcements: dict[str, Any], code: str, day: str) -> str:
    matches = [
        "https://disc.static.szse.cn/download" + row["attachPath"]
        for row in announcements["szse"]
        if row["secCode"] == [code]
        and row["publishTime"][:10] == day
        and "复牌" in row["title"]
    ]
    if len(matches) != 1:
        raise AcquisitionError(f"Expected one {code} resume notice on {day}")
    return matches[0]


def _refetch(cache: Path, target: Path) -> Path:
    """Download fresh price, calendar and announcement artifacts into ``target``."""
    target.mkdir(parents=True, exist_ok=True)
    (target / "pdfs").mkdir(exist_ok=True)
    (target / "pdfs" / "halts").mkdir(exist_ok=True)
    _write_json(target / "tencent_bars.json", _fetch_tencent_bars())
    _write_json(target / "calendar_szse.json", _fetch_calendar())

    def download(url: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(_fetch(url))
        print(f"refetched {url} -> {destination}", file=sys.stderr)

    for meta in _read_json(cache / "pdfmeta.json"):
        name = Path(meta["pdf"]).name
        download(meta["url"], target / "pdfs" / name)
    halts = _read_json(cache / "halts.json")
    announcements = _read_json(cache / "announcements.json")
    index = _halt_announcement_index(announcements)
    for row in halts:
        if not (WINDOW_START <= row["date"] <= WINDOW_END):
            continue
        name = _halt_pdf_name(cache, row["code"], row["announced"])
        url = index.get((row["code"], row["announced"]))
        if url is None:
            raise AcquisitionError(
                f"Missing halt announcement URL for {row['code']} {row['announced']}"
            )
        download(url, target / "pdfs" / "halts" / name)
    resume_name = _halt_pdf_name(cache, "159612", "2025-01-13")
    download(
        _resume_announcement_url(announcements, "159612", "2025-01-13"),
        target / "pdfs" / "halts" / resume_name,
    )
    return target


def _halt_pdf_name(cache: Path, code: str, announced: str) -> str:
    matches = sorted((cache / "pdfs" / "halts").glob(f"{code}_{announced}_*.pdf"))
    if len(matches) != 1:
        raise AcquisitionError(
            f"Expected one cached halt PDF for {code} {announced}, found {len(matches)}"
        )
    return matches[0].name


def _sessions(calendar: dict[str, Any]) -> list[str]:
    sessions = [
        day for day in calendar.get("all", []) if WINDOW_START <= day <= WINDOW_END
    ]
    if sessions != sorted(set(sessions)):
        raise AcquisitionError("Calendar sessions are duplicated or unsorted")
    if len(sessions) != SESSION_COUNT:
        raise AcquisitionError(
            f"Expected {SESSION_COUNT} sessions, found {len(sessions)}"
        )
    if sessions[0] != WINDOW_START or sessions[-1] != WINDOW_END:
        raise AcquisitionError("Calendar window boundaries differ from 2022-05-20/2026-11-20")
    missing_months = sorted(set(MONTHS) - set(calendar.get("months", {})))
    if missing_months:
        raise AcquisitionError(f"Missing SZSE calendar months: {missing_months}")
    return sessions


def _window_dividends(dividends: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [
        row
        for row in dividends
        if WINDOW_START <= row["ex_date"] <= WINDOW_END
    ]
    if len(rows) != 20:
        raise AcquisitionError(f"Expected 20 in-window dividends, found {len(rows)}")
    if {row["code"] for row in rows} - {"511260", "515080"}:
        raise AcquisitionError("Unexpected dividend payer in the common window")
    return sorted(rows, key=lambda row: (row["ex_date"], row["code"]))


def _window_halts(
    halts: list[dict[str, Any]], sessions: list[str]
) -> list[dict[str, Any]]:
    rows = [
        row for row in halts if WINDOW_START <= row["date"] <= WINDOW_END
    ]
    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        if row["date"] not in set(sessions):
            raise AcquisitionError(f"Halt date {row['date']} is not a session")
        counts[(row["code"], row["kind"])] = counts.get((row["code"], row["kind"]), 0) + 1
    expected = {
        ("159612", "open_halt"): 117,
        ("159612", "full_halt_until"): 1,
        ("159612", "full_halt"): 1,
        ("159612", "full_halt_open_end"): 1,
        ("513300", "open_halt"): 43,
    }
    if counts != expected:
        raise AcquisitionError(f"Unexpected in-window halt counts: {counts}")
    return sorted(rows, key=lambda row: (row["date"], row["code"]))


def _halt_window(row: dict[str, Any]) -> dict[str, str] | None:
    if (row["code"], row["date"]) in FULL_HALT_WINDOWS:
        start, end = FULL_HALT_WINDOWS[(row["code"], row["date"])]
        return {"start": start, "end": end}
    resume = row.get("resume")
    if not resume:
        return None
    if re.fullmatch(r"\d{2}:\d{2}", resume):
        return {"start": _at(row["date"], 9, 30), "end": f"{row['date']}T{resume}:00+08:00"}
    parsed = datetime.fromisoformat(f"{resume}:00").replace(tzinfo=CN_TZ)
    return {"start": _at(row["date"], 9, 30), "end": _iso(parsed)}


def _bar_values(bar: list[str]) -> tuple[float, float, float, float, float]:
    """Return (open, close, high, low, volume) from a Tencent row (O/C/H/L/V)."""
    try:
        open_price, close, high, low, volume = (float(value) for value in bar[1:6])
    except (TypeError, ValueError) as error:
        raise AcquisitionError(f"Non-numeric vendor bar: {bar!r}") from error
    if min(open_price, close, high, low) <= 0 or volume < 0:
        raise AcquisitionError(f"Invalid vendor bar: {bar!r}")
    if high < max(open_price, close) or low > min(open_price, close) or high < low:
        raise AcquisitionError(f"Inconsistent vendor OHLC: {bar!r}")
    return open_price, close, high, low, volume


def halt_evidence_id(code: str, announced: str) -> str:
    """The evidence item id of one official halt notice (status evidence role)."""
    return f"halt-{code}-{announced}"


def _window_covers_session(window: dict[str, str], day: str) -> bool:
    """Whether a halt window spans the whole 09:30-15:00 session."""
    session = datetime.fromisoformat(day).replace(tzinfo=CN_TZ)
    start = datetime.fromisoformat(window["start"])
    end = datetime.fromisoformat(window["end"])
    return (
        start <= session.replace(hour=9, minute=30)
        and end >= session.replace(hour=15, minute=0)
    )


def _halt_is_full_day(halt: dict[str, Any], window: dict[str, str] | None) -> bool:
    """Whether the notice closes the whole session, so it has no actual open."""
    if halt["kind"] in FULL_DAY_HALT_KINDS:
        return True
    return window is not None and _window_covers_session(window, halt["date"])


def _halt_evidence(
    halt: dict[str, Any],
    halt_evidence_ids: dict[tuple[str, str], str],
    halt_references: dict[tuple[str, str], str],
) -> tuple[str, str]:
    """Return the halt notice's evidence id and its availability-evidence text."""
    key = (halt["code"], halt["announced"])
    evidence_id = halt_evidence_ids.get(key)
    if evidence_id is None:
        raise AcquisitionError(
            f"Missing halt evidence id for {halt['code']} {halt['announced']}"
        )
    reference = halt_references.get(key, evidence_id)
    return evidence_id, f"official-halt-notice:{reference} (announced {halt['announced']})"


def convert_raw_bars(
    *,
    sessions: list[str],
    bars: dict[str, dict[str, list[str]]],
    halts: list[dict[str, Any]],
    price_evidence_id: str,
    halt_evidence_ids: dict[tuple[str, str], str],
    halt_references: dict[tuple[str, str], str],
    price_collected_at: str,
) -> list[dict[str, Any]]:
    """Convert vendor daily bars and official halt rows to schema-3 raw bars.

    A normal session keeps the vendor's unadjusted daily open and cites the
    price archive.  A halted-at-open session whose halt ends before the close
    (``open_halt``, or a ``full_halt_until`` window that does not cover the
    session) keeps the vendor's actual open as the day's first print after the
    resume, with the price archive as ``open_source`` and the halt notice as a
    distinct ``status_evidence_id``.  A session the notice closes for the whole
    day (``full_halt``, ``full_halt_open_end``, or a covering
    ``full_halt_until`` window) becomes ``suspended`` with null OHLCV and the
    halt notice as status evidence: an actual open means a trade happened, so
    a suspended row must not carry one.
    """
    halts_by_key = {(halt["code"], halt["date"]): halt for halt in halts}
    open_source = {"source": PRICE_SOURCE, "evidence_id": price_evidence_id}
    rows: list[dict[str, Any]] = []
    for day in sessions:
        for symbol in SYMBOLS:
            halt = halts_by_key.get((symbol, day))
            window = _halt_window(halt) if halt is not None else None
            if halt is not None and _halt_is_full_day(halt, window):
                if window is None:
                    raise AcquisitionError(f"Missing full-halt window for {symbol} {day}")
                evidence_id, reference = _halt_evidence(
                    halt, halt_evidence_ids, halt_references
                )
                rows.append(
                    {
                        "date": day,
                        "symbol": symbol,
                        "open": None,
                        "high": None,
                        "low": None,
                        "close": None,
                        "volume": None,
                        "trading_status": "suspended",
                        "halt": window,
                        "status_evidence_id": evidence_id,
                        "open_available_at": None,
                        "close_available_at": None,
                        "available_at": _day_end(halt["announced"]),
                        "availability_evidence": reference,
                        "collected_at": price_collected_at,
                    }
                )
                continue
            bar = bars.get(symbol, {}).get(day)
            if bar is None:
                raise AcquisitionError(f"Missing vendor bar for {symbol} {day}")
            open_price, close, high, low, volume = _bar_values(bar)
            if halt is None:
                rows.append(
                    {
                        "date": day,
                        "symbol": symbol,
                        "open": open_price,
                        "high": high,
                        "low": low,
                        "close": close,
                        "volume": volume,
                        "trading_status": "trading",
                        "halt": None,
                        "open_source": dict(open_source),
                        "open_available_at": _at(day, 9, 30),
                        "close_available_at": _at(day, 15),
                        "available_at": _at(day, 15),
                        "availability_evidence": (
                            f"{PRICE_SOURCE}:{symbol}:{day};"
                            "rule:09:30-open/15:00-close-assertion"
                        ),
                        "collected_at": price_collected_at,
                    }
                )
                continue
            if window is None:
                raise AcquisitionError(f"Missing partial-halt window for {symbol} {day}")
            evidence_id, reference = _halt_evidence(
                halt, halt_evidence_ids, halt_references
            )
            rows.append(
                {
                    "date": day,
                    "symbol": symbol,
                    # The daily bar's first print after the resume is the actual
                    # open an order effective before the open matches at.
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                    "trading_status": "partial_halt",
                    "halt": window,
                    "open_source": dict(open_source),
                    "status_evidence_id": evidence_id,
                    "open_available_at": window["end"],
                    "close_available_at": _at(day, 15),
                    "available_at": _at(day, 15),
                    "availability_evidence": (
                        f"{PRICE_SOURCE}:{symbol}:{day};"
                        "rule:actual-daily-open-after-resume;"
                        f"{reference}"
                    ),
                    "collected_at": price_collected_at,
                }
            )
    return rows


def _derived_values(
    sessions: list[str],
    closes: dict[str, dict[str, float]],
    dividends: list[dict[str, Any]],
) -> dict[str, dict[str, float | None]]:
    """tr-reinvest-exclose-v1: reinvest each official dividend at the ex-date close."""
    by_day: dict[str, dict[str, float]] = {symbol: {} for symbol in SYMBOLS}
    for row in dividends:
        by_day[row["code"]][row["ex_date"]] = row["per_unit"]
    series: dict[str, dict[str, float | None]] = {}
    for symbol in SYMBOLS:
        values: dict[str, float | None] = {}
        last_close: float | None = None
        last_value: float | None = None
        for day in sessions:
            close = closes[symbol].get(day)
            if close is None:
                values[day] = None
                continue
            if last_value is None or last_close is None:
                value = 1.0
            else:
                value = last_value * (close + by_day[symbol].get(day, 0.0)) / last_close
            values[day] = value
            last_value, last_close = value, close
        if values[sessions[0]] != 1.0:
            raise AcquisitionError(f"Missing base close for {symbol} on {sessions[0]}")
        series[symbol] = values
    return series


def _build_evidence(
    *,
    cache: Path,
    refetch: Path | None,
    evidence_dir: Path,
    dividends: list[dict[str, Any]],
    halts: list[dict[str, Any]],
    halt_urls: dict[tuple[str, str], str],
    resume_url: str,
) -> list[dict[str, Any]]:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, Any]] = []

    def add(
        item_id: str,
        kind: str,
        relative: str,
        reference: str,
        available_at: str | None,
        name: str,
    ) -> None:
        source = _artifact(cache, refetch, relative)
        destination = evidence_dir / name
        digest = _sha256(source)
        if not destination.is_file() or _sha256(destination) != digest:
            shutil.copy2(source, destination)
        items.append(
            {
                "id": item_id,
                "kind": kind,
                "reference": reference,
                "sha256": digest,
                "available_at": available_at or _mtime(source),
            }
        )

    for year, notice in SSE_CALENDAR_NOTICES.items():
        add(
            f"calendar-sse-{year}",
            "calendar",
            notice["file"],
            notice["url"],
            _day_end(notice["published"]),
            f"sse-calendar-{year}.html",
        )
    add(
        "calendar-szse-months",
        "calendar",
        "calendar_szse.json",
        "https://www.szse.cn/api/report/exchange/onepersistenthour/monthList "
        "(59 months, 2022-01..2026-11)",
        None,
        "szse-calendar-months.json",
    )
    for spec in LISTING_SPECS:
        add(
            spec["id"],
            "listing",
            spec["file"],
            spec["url"],
            _day_end(spec["published"]),
            spec["name"],
        )
    for spec in CONVERSION_SPECS:
        add(
            spec["id"],
            "conversion",
            spec["file"],
            spec["url"],
            _day_end(spec["published"]),
            spec["name"],
        )
    for row in dividends:
        add(
            f"dividend-{row['code']}-{row['announced']}",
            "dividend",
            f"pdfs/{_dividend_pdf_name(cache, row)}",
            row["url"],
            _day_end(row["announced"]),
            f"dividend-{row['code']}-{row['announced']}.pdf",
        )
    for row in halts:
        url = halt_urls.get((row["code"], row["announced"]))
        if url is None:
            raise AcquisitionError(
                f"Missing halt notice URL for {row['code']} {row['announced']}"
            )
        add(
            halt_evidence_id(row["code"], row["announced"]),
            "halt",
            f"pdfs/halts/{_halt_pdf_name(cache, row['code'], row['announced'])}",
            url,
            _day_end(row["announced"]),
            f"halt-{row['code']}-{row['announced']}.pdf",
        )
    add(
        "halt-159612-2025-01-13-resume",
        "halt",
        f"pdfs/halts/{_halt_pdf_name(cache, '159612', '2025-01-13')}",
        resume_url,
        _day_end("2025-01-13"),
        "halt-159612-2025-01-13-resume.pdf",
    )
    for session in TDX_SESSIONS:
        add(
            f"price-tdx-{session}",
            "price_archive",
            f"tdx_{session}.zip",
            f"https://www.tdx.com.cn/products/data/data/g4day/{session}.zip",
            f"{session[:4]}-{session[4:6]}-{session[6:]}T15:38:00+08:00",
            f"tdx-{session}.zip",
        )
    add(
        PRICE_EVIDENCE_ID,
        "price_archive",
        "tencent_bars.json",
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get (empty adjust; sh/sz "
        "daily bars, 2022-05-20..2026-11-20)",
        None,
        "tencent-unadjusted-bars.json",
    )
    add(
        "vendor-tencent-qfq",
        "vendor_cross_check",
        "tencent_qfq.json",
        "Tencent qfq series (retrieved for the research note; not used by this snapshot)",
        None,
        "tencent-qfq-reference.json",
    )
    add(
        "index-announcements",
        "announcement_index",
        "announcements.json",
        "SSE COMMON_PL_JJXX_JJGG_L and SZSE annList enumeration (probe transcription)",
        None,
        "announcements-index.json",
    )
    add(
        "index-dividends",
        "dividend_index",
        "dividends.json",
        "Parsed official dividend announcements (probe transcription)",
        None,
        "dividends-index.json",
    )
    add(
        "index-halts",
        "halt_index",
        "halts.json",
        "Parsed official halt notices (probe transcription)",
        None,
        "halts-index.json",
    )
    return items


def _dividend_pdf_name(cache: Path, dividend: dict[str, Any]) -> str:
    meta = [
        row
        for row in _read_json(cache / "pdfmeta.json")
        if row["code"] == dividend["code"]
        and row["date"] == dividend["announced"]
        and row["url"] == dividend["url"]
    ]
    if len(meta) != 1:
        raise AcquisitionError(
            f"Expected one dividend PDF for {dividend['code']} {dividend['announced']}"
        )
    return Path(meta[0]["pdf"]).name


def build_snapshot_input(
    *,
    sessions: list[str],
    bars: dict[str, dict[str, list[str]]],
    halts: list[dict[str, Any]],
    dividends: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    price_evidence_id: str,
    halt_evidence_ids: dict[tuple[str, str], str],
    halt_references: dict[tuple[str, str], str],
    price_collected_at: str,
    calendar_collected_at: str,
    action_collected_at: str,
    derived_collected_at: str,
    dividend_collected_at: dict[tuple[str, str], str] | None = None,
    derivation_evidence: tuple[str, ...] | list[str] = DERIVATION_EVIDENCE,
) -> dict[str, Any]:
    """Assemble a schema-3 real snapshot input from converted acquisition facts.

    Pure conversion: it takes the vendor bars, the official halt and dividend
    rows, the already-hashed evidence bundle and the collection timestamps, and
    returns the document ``snapshot seal`` accepts.  ``_build`` validates the
    probe window and hashes the evidence files, then delegates here; the #57
    acceptance test drives this same entry point with synthetic bars and
    notices.
    """
    closes: dict[str, dict[str, float]] = {symbol: {} for symbol in SYMBOLS}
    for symbol in SYMBOLS:
        code_bars = bars.get(symbol)
        if not isinstance(code_bars, dict):
            raise AcquisitionError(f"Missing vendor bars for {symbol}")
        for day in sessions:
            bar = code_bars.get(day)
            if bar is not None:
                closes[symbol][day] = _bar_values(bar)[1]
    derived = _derived_values(sessions, closes, dividends)
    raw_bars = convert_raw_bars(
        sessions=sessions,
        bars=bars,
        halts=halts,
        price_evidence_id=price_evidence_id,
        halt_evidence_ids=halt_evidence_ids,
        halt_references=halt_references,
        price_collected_at=price_collected_at,
    )

    calendar_rows = []
    for day in sessions:
        notice = SSE_CALENDAR_NOTICES[day[:4]]
        calendar_rows.append(
            {
                "date": day,
                "available_at": _day_end(notice["published"]),
                "availability_evidence": (
                    f"sse-holiday-notice:{notice['url']} (published {notice['published']});"
                    f"szse-monthList:{day[:7]}"
                ),
                "collected_at": calendar_collected_at,
            }
        )

    collected_by_dividend = dividend_collected_at or {}
    corporate_actions = [
        {
            "date": row["ex_date"],
            "symbol": row["code"],
            "type": "cash_dividend",
            "record_date": row["record_exchange"],
            "pay_date": row["pay_exchange"],
            "cash_per_share": float(row["per_unit"]),
            "terms_evidence": (
                f"sse-dividend-announcement:{row['url']} (announced {row['announced']})"
            ),
            "available_at": _day_end(row["announced"]),
            "availability_evidence": (
                f"sse-dividend-announcement:{row['url']} (announced {row['announced']})"
            ),
            "collected_at": collected_by_dividend.get(
                (row["code"], row["announced"]), action_collected_at
            ),
        }
        for row in dividends
    ]

    derived_rows = []
    for symbol in SYMBOLS:
        for day in sessions:
            derived_rows.append(
                {
                    "date": day,
                    "symbol": symbol,
                    "value": derived[symbol][day],
                    "available_at": _at(day, 15),
                    "availability_evidence": (
                        f"derived:{DERIVED_RULE}:{symbol}:{day};"
                        "inputs:tencent:unadjusted-close+official-cash-dividend"
                    ),
                    "collected_at": derived_collected_at,
                }
            )

    source_calendar = {
        "source": (
            "SSE annual holiday notices (5) + SZSE monthly onepersistenthour calendar (59)"
        ),
        "version": "cny-2022-2026-v1",
        "coverage_start": WINDOW_START,
        "coverage_end": WINDOW_END,
        "available_at": _day_end(SSE_CALENDAR_NOTICES["2022"]["published"]),
        "collected_at": calendar_collected_at,
        "availability_evidence": (
            "sse-holiday-notices:"
            + ",".join(notice["url"] for notice in SSE_CALENDAR_NOTICES.values())
            + ";szse-monthList:59-months"
        ),
    }
    source_raw = {
        "source": "Tencent fqkline unadjusted daily bars (empty adjustment)",
        "version": "tencent-unadjusted-2026-11-21",
        "coverage_start": WINDOW_START,
        "coverage_end": WINDOW_END,
        "available_at": price_collected_at,
        "collected_at": price_collected_at,
        "availability_evidence": (
            "tencent:unadjusted-bars;cross-check:tdx-g4day-samples;"
            "09:30/15:00-are-market-rule-assertions-not-archived-quotes"
        ),
    }
    source_actions = {
        "source": "SSE official cash-dividend announcements (20 in-window)",
        "version": "official-sse-dividends-2026-11-21",
        "coverage_start": WINDOW_START,
        "coverage_end": WINDOW_END,
        "available_at": (
            _day_end(dividends[0]["announced"])
            if dividends
            else calendar_collected_at
        ),
        "collected_at": action_collected_at,
        "availability_evidence": "sse-dividend-announcements:20-pdfs",
    }
    source_derived = {
        "source": "Derived total return from unadjusted closes + official cash dividends",
        "version": DERIVED_RULE,
        "coverage_start": WINDOW_START,
        "coverage_end": WINDOW_END,
        "available_at": derived_collected_at,
        "collected_at": derived_collected_at,
        "availability_evidence": (
            "derivation:tr-reinvest-exclose-v1;inputs:tencent-unadjusted+20-official-dividends"
        ),
    }

    return {
        "schema_version": 3,
        "data_kind": "real",
        "raw_price_basis": "unadjusted",
        "derivation_rule_version": DERIVED_RULE,
        "sources": {
            "calendar": source_calendar,
            "raw_bars": source_raw,
            "corporate_actions": source_actions,
            "derived": source_derived,
        },
        "symbol_coverage": {
            symbol: {"start": LISTING_DATES[symbol], "end": WINDOW_END}
            for symbol in SYMBOLS
        },
        "calendar": calendar_rows,
        "raw_bars": raw_bars,
        "corporate_actions": corporate_actions,
        "derived": derived_rows,
        "evidence": {"items": list(evidence)},
        "derivation": {
            "rule_version": DERIVED_RULE,
            "method": DERIVATION_METHOD,
            "evidence": list(derivation_evidence),
        },
    }


def _build(
    *,
    cache: Path,
    refetch: Path | None,
    evidence_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read and validate the probe artifacts, then build the schema-3 input.

    The window checks below are acquisition-fact validation (session count,
    dividend and halt windows, vendor coverage); the conversion itself is the
    pure ``convert_raw_bars``/``build_snapshot_input`` pair.
    """
    bars_path = _artifact(cache, refetch, "tencent_bars.json")
    calendar_path = _artifact(cache, refetch, "calendar_szse.json")
    bars = _read_json(bars_path)
    calendar = _read_json(calendar_path)
    dividends_all = _read_json(cache / "dividends.json")
    halts_all = _read_json(cache / "halts.json")
    announcements = _read_json(cache / "announcements.json")

    sessions = _sessions(calendar)
    session_set = set(sessions)
    dividends = _window_dividends(dividends_all)
    halts = _window_halts(halts_all, sessions)
    halt_urls = _halt_announcement_index(announcements)
    resume_url = _resume_announcement_url(announcements, "159612", "2025-01-13")

    for row in dividends:
        if row["ex_date"] not in session_set:
            raise AcquisitionError(f"Dividend ex-date {row['ex_date']} is not a session")
        if row["record_exchange"] not in session_set:
            raise AcquisitionError(f"Record date {row['record_exchange']} is not a session")
        if row["pay_exchange"] not in session_set:
            raise AcquisitionError(f"Pay date {row['pay_exchange']} is not a session")

    closes: dict[str, dict[str, float]] = {symbol: {} for symbol in SYMBOLS}
    for symbol in SYMBOLS:
        code_bars = bars.get(symbol)
        if not isinstance(code_bars, dict):
            raise AcquisitionError(f"Missing vendor bars for {symbol}")
        for day, bar in code_bars.items():
            if day in session_set:
                closes[symbol][day] = _bar_values(bar)[1]
        unexpected = sorted(
            day
            for day in code_bars
            if WINDOW_START <= day <= WINDOW_END and day not in session_set
        )
        if unexpected:
            raise AcquisitionError(f"{symbol} has non-session bars: {unexpected[:3]}")
    expected_missing = {"159612": {"2025-01-10", "2025-01-24"}}
    for symbol in SYMBOLS:
        missing = session_set - set(closes[symbol])
        if missing != expected_missing.get(symbol, set()):
            raise AcquisitionError(f"Unexpected missing sessions for {symbol}: {missing}")

    price_collected = _mtime(bars_path)
    calendar_collected = _mtime(calendar_path)
    action_collected = max(
        _mtime(_artifact(cache, refetch, f"pdfs/{_dividend_pdf_name(cache, row)}"))
        for row in dividends
    )
    derived_collected = max(price_collected, action_collected)

    evidence = _build_evidence(
        cache=cache,
        refetch=refetch,
        evidence_dir=evidence_dir,
        dividends=dividends,
        halts=halts,
        halt_urls=halt_urls,
        resume_url=resume_url,
    )
    payload = build_snapshot_input(
        sessions=sessions,
        bars=bars,
        halts=halts,
        dividends=dividends,
        evidence=evidence,
        price_evidence_id=PRICE_EVIDENCE_ID,
        halt_evidence_ids={
            (row["code"], row["announced"]): halt_evidence_id(
                row["code"], row["announced"]
            )
            for row in halts
        },
        halt_references=halt_urls,
        price_collected_at=price_collected,
        calendar_collected_at=calendar_collected,
        action_collected_at=action_collected,
        derived_collected_at=derived_collected,
        dividend_collected_at={
            (row["code"], row["announced"]): _mtime(
                _artifact(cache, refetch, f"pdfs/{_dividend_pdf_name(cache, row)}")
            )
            for row in dividends
        },
    )
    raw_bars = payload["raw_bars"]
    return payload, {
        "sessions": len(sessions),
        "raw_bars": len(raw_bars),
        "derived": len(payload["derived"]),
        "corporate_actions": len(payload["corporate_actions"]),
        "evidence": len(payload["evidence"]["items"]),
        "partial_halts": sum(
            1 for row in raw_bars if row["trading_status"] == "partial_halt"
        ),
        "suspended": sum(
            1 for row in raw_bars if row["trading_status"] == "suspended"
        ),
        "first_session": sessions[0],
        "last_session": sessions[-1],
        "price_collected_at": price_collected,
        "calendar_collected_at": calendar_collected,
        "action_collected_at": action_collected,
    }


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=Path("/tmp/research24"))
    parser.add_argument(
        "--store",
        type=Path,
        default=Path("~/PrajnaTrade-trusted/real-etf-snapshot/store").expanduser(),
        help="trusted sealed-snapshot store (outside Git; used to locate evidence dir)",
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        help="directory for hashed evidence copies (default: <store>/../evidence)",
    )
    parser.add_argument("--workdir", type=Path, help="temporary working directory")
    parser.add_argument("--output", type=Path, help="real.json path (default: <workdir>/real.json)")
    parser.add_argument(
        "--refetch",
        action="store_true",
        help="re-download Tencent bars, the 59 SZSE months and the announcement PDFs",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    cache = args.cache_dir.expanduser().resolve()
    if not cache.is_dir():
        print(f"error: cache directory does not exist: {cache}", file=sys.stderr)
        return 2
    workdir = (
        args.workdir.expanduser().resolve()
        if args.workdir is not None
        else Path(tempfile.mkdtemp(prefix="prajna-real-acquisition-"))
    )
    workdir.mkdir(parents=True, exist_ok=True)
    output = (
        args.output.expanduser().resolve()
        if args.output is not None
        else workdir / "real.json"
    )
    store = args.store.expanduser().resolve()
    evidence_dir = (
        args.evidence_dir.expanduser().resolve()
        if args.evidence_dir is not None
        else store.parent / "evidence"
    )
    refetch = _refetch(cache, workdir / "refetch") if args.refetch else None
    payload, summary = _build(cache=cache, refetch=refetch, evidence_dir=evidence_dir)
    _write_json(output, payload)
    summary.update(
        {
            "output": str(output),
            "output_sha256": _sha256(output),
            "evidence_dir": str(evidence_dir),
            "refetch": refetch is not None,
        }
    )
    _write_json(workdir / "build-summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
