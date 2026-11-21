# Real five-ETF data sources for #24

Scope: establish, from primary sources, what is obtainable and verifiable today for a **real** sealed snapshot of `511260`, `518880`, `515080`, `513300`, `159612` plus cash, and for the Discovery equal-weight baseline required by [#24](https://github.com/ZombieIce/PrajnaTrade/issues/24). This note does not change code, does not seal a snapshot, and does not commit raw data. Probe artifacts were kept outside Git in `/tmp/research24`.

Retrieval window: **2026-11-21 22:49–23:35 +08:00** (Saturday; last complete A-share session **2026-11-20**). Every "retrieved" time below is a *collection* time, not evidence that the data was available then; §7 separates the two.

Short answer: **the official calendars, instrument identities, listing dates, dividend terms and trading-halt windows are obtainable and verifiable now; the sealer and halt/availability schema are not ready for real data; two 159612 full-day halts inside Discovery mean the Discovery baseline will have two declared NAV gaps.**

Relation to the prior handoff: two of its four gaps are now closed with primary sources — official venue calendars (§3) and corporate-action histories/terms (§4) are fetchable and complete for this window. Instrument status is documented rather than closed: the halts are officially announced and countable (§6), but the current schema cannot represent them. Historical price availability remains partial: dated vendor archives exist and cross-check exactly, but no public source proves intraday point-in-time availability (§7). The "no real snapshot / no protected partition / no real baseline" statement in the handoff is still accurate: nothing here sealed or baselined real data.

## 1. Verdict summary

| Area | Status | Why |
| --- | --- | --- |
| Instrument identity, exchange, listing date, manager | **verified** | Exchange-hosted listing announcements for all five (§2) |
| SSE/SZSE trading calendar 2022–2026 | **verified** | Five SSE annual holiday notices + SZSE machine calendar; 92/92 weekday non-sessions agree, 0 conflicts (§3) |
| Cash-dividend history and terms | **verified** | 23 official announcements (5 × `511260`, 18 × `515080`); none for the other three (§4) |
| Share adjustments | **verified (none in window)** | Only inception conversions in 2017/2013; complete announcement enumeration shows none after 2022-05-20 (§4.2) |
| Unadjusted daily OHLCV | **verified with gaps** | Tencent/TDX agree exactly on sampled dates; `159612` lacks 2 full-halt sessions (§5) |
| Trading status | **verified; schema cannot encode it** | 118 partial + 2 full `159612` halts; 43 partial `513300` halts in window; official notices give the windows (§6) |
| Point-in-time availability | **partial** | Action terms are date-level; prices have dated TDX archive timestamps but no notarised intraday archive; 09:30 open is a market-rule assertion (§7) |
| Real sealing + Discovery baseline | **blocked in code** | `snapshot.py`, `snapshot_baseline.py`, `experiment.py` hardcode synthetic; per-field availability and halts are not representable (§9, §10) |
| Independent human reconciliation | **blocked (not performed)** | #24 requires a second researcher to recompute the same Discovery inputs; no such receipt exists (§10) |

## 2. Five-ETF identity and listing coverage

All five were confirmed from exchange-hosted **listing announcements** (the fund manager's own notice published by the venue), not from vendor names. Full names, managers, venues and listing dates:

| Code | Full fund name | Exchange | Manager | Listing date | First vendor bar | Announcement |
| --- | --- | --- | --- | --- | --- | --- |
| `511260` | 上证10年期国债交易型开放式指数证券投资基金 | SSE | 国泰基金管理有限公司 | 2017-08-24 | 2017-08-24 | [上市交易公告书](https://www.sse.com.cn/disclosure/fund/announcement/c/2017-08-22/511260_20170821_2.pdf) |
| `518880` | 华安易富黄金交易型开放式证券投资基金 | SSE | 华安基金管理有限公司 | 2013-07-29 | 2013-07-29 | [上市交易公告书](https://www.sse.com.cn/disclosure/fund/announcement/c/2013-07-23/518880_20130724_1.pdf) |
| `515080` | 招商中证红利交易型开放式指数证券投资基金 | SSE | 招商基金管理有限公司 | 2019-12-27 | 2019-12-27 | [上市交易公告书](https://www.sse.com.cn/disclosure/fund/announcement/c/2019-12-24/515080_20191224_1.pdf) |
| `513300` | 华夏纳斯达克100交易型开放式指数证券投资基金（QDII） | SSE | 华夏基金管理有限公司 | 2020-11-05 | 2020-11-05 | [上市交易公告书提示性公告](https://www.sse.com.cn/disclosure/fund/announcement/c/2020-11-02/513300_20201102_3.pdf) |
| `159612` | 国泰标普500交易型开放式指数证券投资基金（QDII） | **SZSE** | 国泰基金管理有限公司 | 2022-05-20 | 2022-05-20 | [上市交易公告书](https://disc.static.szse.cn/download/disc/disk03/finalpage/2022-05-16/d3ebfae4-0dc5-4ed6-acf9-cdba35dbac47.PDF) |

Notes:

- The assumption in the brief is correct: `159612` is SZSE and the other four are SSE. The listing announcements state 上市地点 (上海证券交易所 / 深圳证券交易所) and the manager.
- For each code the first Tencent raw bar equals the official listing date exactly, including `159612` on its 2022-05-20 listing day. This is a useful cross-check but **not** proof of listing: the official announcement is the proof.
- Fund short names changed over time (`十年国债ETF` → `十年国债ETF国泰`, `标普500ETF` → `标普500ETF国泰`). Identity should be keyed by code + full name, not by the vendor short name.
- `513300`'s listing is stated in the 提示性公告: "将于2020年11月5日在上海证券交易所上市". The 上市交易公告书 itself is [here](https://www.sse.com.cn/disclosure/fund/announcement/c/2020-11-02/513300_20201102_1.pdf).
- The manager pages for `518880` ([华安黄金ETF](https://www.huaan.com.cn/funds/518880/index.shtml)) and `513300` ([华夏基金](https://www.chinaamc.com/fund/513300/index.shtml)) were reachable and agree; the corresponding 国泰/招商 product pages were not located at probe time, so identity rests on the exchange-hosted announcements above.

**Common history starts 2022-05-20** because that is `159612`'s listing day. Between 2022-05-20 and 2026-11-20 there are **1,094 SSE/SZSE sessions** (§3). This matches the brief's "roughly 2022-05".

## 3. Official trading calendars

### 3.1 Sources

- **SZSE machine-readable calendar** — one JSON per month, official: `https://www.szse.cn/api/report/exchange/onepersistenthour/monthList?month=YYYY-MM`. Each row has `jyrq` (date) and `jybz` (`"1"` = trading day), e.g. [2022-05](https://www.szse.cn/api/report/exchange/onepersistenthour/monthList?month=2022-05). This is the exchange's own calendar used by its site ([SZSE trading calendar page](http://www.szse.cn/www/English/services/trading/calendar/index.html)).
- **SSE annual holiday notices** — one notice per year, listing every closure range:
  - [2022](https://www.sse.com.cn/disclosure/announcement/general/c/c_20211220_5662606.shtml) (上证公告〔2021〕37号)
  - [2023](https://www.sse.com.cn/disclosure/announcement/general/c/c_20221227_5714458.shtml) (上证公告〔2022〕51号)
  - [2024](https://www.sse.com.cn/disclosure/announcement/general/c/c_20231226_5733939.shtml) (上证公告〔2023〕47号)
  - [2025](https://www.sse.com.cn/disclosure/announcement/general/c/c_20241223_10767108.shtml) (上证公告〔2024〕38号)
  - [2026](https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml) (上证公告〔2025〕45号)
  - Hub: [休市安排](https://www.sse.com.cn/disclosure/dealinstruc/closed/)

### 3.2 What was checked

I fetched all 59 monthly SZSE JSONs from 2022-01 through 2026-11 and all five SSE notices, parsed every closure range from the notices, and cross-checked them:

- 1,188 SZSE sessions from 2022-01-04 through 2026-11-30.
- 92 weekday non-sessions between 2022-01-04 and 2026-11-20; **all 92 are covered by an SSE closure range**.
- **0 SZSE sessions fall on an SSE closure day.**
- No extra SSE closure was found that SZSE treated as a session.

That is sufficient to use the SZSE month JSON as the machine-readable session list while citing SSE notices as the SSE-side authority. It does not prove the two exchanges could never diverge in future; it proves they did not over this window.

Sessions by calendar year from the SZSE feed (2026 is partial through 11-30): 2022: 242, 2023: 242, 2024: 242, 2025: 243, 2026: 219. Common window 2022-05-20..2026-11-20: **1,094**.

### 3.3 Archiving

Store the 59 monthly JSON responses and the five notice pages (HTML or printed PDF) outside Git, with SHA-256 per file, the request URL, the retrieval timestamp, and an `available_at` equal to the notice publication date (e.g. 2025-12-22 for the 2026 notice). The monthly JSON itself is a current-view API; its rows for past months are stable in this probe but the API is not an archive — the notice is the dated publication, the JSON is the machine transcription.

## 4. Corporate actions

### 4.1 Enumeration method

Two official venue feeds were used, paginated to exhaustion:

- SSE fund announcements: `https://query.sse.com.cn/commonQuery.do?sqlId=COMMON_PL_JJXX_JJGG_L&isPagination=true&pageHelp.pageSize=100&...&SECURITY_CODE=<code>` (page through `pageHelp.pageNo`/`pageHelp.pageCount`). Result: 213 rows for `511260` (2017-07-12..2026-11-02), 209 for `518880`, 245 for `515080`, 699 for `513300`; page listing at [SSE 基金公告](https://www.sse.com.cn/disclosure/fund/announcement/).
- SZSE fund announcements: `POST https://www.szse.cn/api/disc/announcement/annList` with `{"channelCode":["fundinfoNotice_disc"],"pageSize":50,"pageNum":N,"stock":["159612"]}`. Result: **525** rows (2022-04-12..2026-11-20); page at [SZSE 基金公告](https://www.szse.cn/disclosure/fund/notice/index.html).

Every announcement PDF cited below was downloaded and converted with `pdftotext -layout`; the terms in the table were read from the PDF, not from a vendor summary.

### 4.2 Share adjustments

The complete enumeration found **no share adjustment in the common window** (2022-05-20 onward). The only conversions are at inception, before the window:

| Code | Event | Effective | Terms | Evidence |
| --- | --- | --- | --- | --- |
| `511260` | 基金份额折算 | 2017-08-04 (contract effective date) | pre 218,647,480 shares at NAV 1.000 → post 2,186,474 at NAV 100.00; ratio 0.01 | [折算公告 2017-07-28](https://www.sse.com.cn/disclosure/fund/announcement/c/2017-07-28/511260_20170728_1.pdf), [结果公告 2017-08-08](https://www.sse.com.cn/disclosure/fund/announcement/c/2017-08-08/511260_20170808_1.pdf) |
| `518880` | 基金份额折算 | 2013-07-24 (registered 2013-07-25) | ratio 0.37761358; pre 1,208,486,400 shares at NAV 1.015 → post 456,340,847 at NAV 2.687 | [折算日公告 2013-07-24](https://www.sse.com.cn/disclosure/fund/announcement/c/2013-07-23/518880_20130724_2.pdf), [结果公告 2013-07-26](https://www.sse.com.cn/disclosure/fund/announcement/c/2013-07-25/518880_20130726_1.pdf) |

Neither conversion touches the 2022-05-20+ series. **Do not** infer "no adjustments" from a clean vendor factor alone; the evidence is the complete official announcement enumeration plus the factor as a secondary check.

### 4.3 Cash dividends

`511260` and `515080` are the only dividend payers. The other three have **zero** dividend or distribution announcements in their complete feeds (0 of 209, 0 of 699, 0 of 525). The table lists every dividend whose ex-date falls after each fund's listing (including three `515080` dividends before the common window, shown for completeness):

| Code | Announced | Record date (场内) | Ex-date | Pay date (场内) | CNY/unit | Official PDF |
| --- | --- | --- | --- | --- | --- | --- |
| 511260 | 2025-09-18 | 2025-09-22 | 2025-09-23 | 2025-09-26 | 1.3600 | [公告](https://www.sse.com.cn/disclosure/fund/announcement/c/new/2025-09-18/511260_20250918_HDYX.pdf) |
| 511260 | 2025-12-23 | 2025-12-25 | 2025-12-26 | 2025-12-31 | 0.8330 | [公告](https://www.sse.com.cn/disclosure/fund/announcement/c/new/2025-12-23/511260_20251223_ZED8.pdf) |
| 511260 | 2026-03-20 | 2026-03-24 | 2026-03-25 | 2026-03-30 | 0.6711 | [公告](https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-03-20/511260_20260320_QYES.pdf) |
| 511260 | 2026-06-22 | 2026-06-24 | 2026-06-25 | 2026-06-30 | 1.2686 | [公告](https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-06-22/511260_20260622_BWI3.pdf) |
| 511260 | 2026-09-15 | 2026-09-17 | 2026-09-18 | 2026-09-23 | 1.2747 | [公告](https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-09-15/511260_20260915_8V2W.pdf) |
| 515080 | 2020-11-24 | 2020-11-27 | 2020-11-30 | 2020-12-03 | 0.0600 | [公告](https://www.sse.com.cn/disclosure/fund/announcement/c/2020-11-24/515080_20201124_1.pdf) |
| 515080 | 2021-06-11 | 2021-06-17 | 2021-06-18 | 2021-06-23 | 0.0300 | [公告](https://www.sse.com.cn/disclosure/fund/announcement/c/new/2021-06-11/515080_20210611_1_CuMELq1I.pdf) |
| 515080 | 2021-12-03 | 2021-12-08 | 2021-12-09 | 2021-12-14 | 0.0300 | [公告](https://www.sse.com.cn/disclosure/fund/announcement/c/new/2021-12-03/515080_20211203_1_cliegDOg.pdf) |
| 515080 | 2022-06-21 | 2022-06-24 | 2022-06-27 | 2022-06-30 | 0.0300 | [公告](https://www.sse.com.cn/disclosure/fund/announcement/c/new/2022-06-21/515080_20220621_1_KcQeLeUI.pdf) |
| 515080 | 2022-11-23 | 2022-11-28 | 2022-11-29 | 2022-12-02 | 0.0300 | [公告](https://www.sse.com.cn/disclosure/fund/announcement/c/new/2022-11-23/515080_20221123_4IE2.pdf) |
| 515080 | 2023-06-12 | 2023-06-15 | 2023-06-16 | 2023-06-21 | 0.0350 | [公告](https://www.sse.com.cn/disclosure/fund/announcement/c/new/2023-06-12/515080_20230612_CKVE.pdf) |
| 515080 | 2023-11-27 | 2023-11-30 | 2023-12-01 | 2023-12-06 | 0.0350 | [公告](https://www.sse.com.cn/disclosure/fund/announcement/c/new/2023-11-27/515080_20231127_Q5BT.pdf) |
| 515080 | 2024-03-22 | 2024-03-27 | 2024-03-28 | 2024-04-02 | 0.0150 | [公告](https://www.sse.com.cn/disclosure/fund/announcement/c/new/2024-03-22/515080_20240322_ZVSO.pdf) |
| 515080 | 2024-06-26 | 2024-06-28 | 2024-07-01 | 2024-07-04 | 0.0200 | [公告](https://www.sse.com.cn/disclosure/fund/announcement/c/new/2024-06-26/515080_20240626_ETCV.pdf) |
| 515080 | 2024-09-12 | 2024-09-19 | 2024-09-20 | 2024-09-25 | 0.0150 | [公告](https://www.sse.com.cn/disclosure/fund/announcement/c/new/2024-09-12/515080_20240912_L2FC.pdf) |
| 515080 | 2024-11-27 | 2024-11-29 | 2024-12-02 | 2024-12-05 | 0.0200 | [公告](https://www.sse.com.cn/disclosure/fund/announcement/c/new/2024-11-27/515080_20241127_JU3W.pdf) |
| 515080 | 2025-03-14 | 2025-03-18 | 2025-03-19 | 2025-03-24 | 0.0150 | [公告](https://www.sse.com.cn/disclosure/fund/announcement/c/new/2025-03-14/515080_20250314_EZST.pdf) |
| 515080 | 2025-06-12 | 2025-06-16 | 2025-06-17 | 2025-06-20 | 0.0150 | [公告](https://www.sse.com.cn/disclosure/fund/announcement/c/new/2025-06-12/515080_20250612_G5EE.pdf) |
| 515080 | 2025-09-12 | 2025-09-16 | 2025-09-17 | 2025-09-22 | 0.0150 | [公告](https://www.sse.com.cn/disclosure/fund/announcement/c/new/2025-09-12/515080_20250912_DDFA.pdf) |
| 515080 | 2025-12-15 | 2025-12-17 | 2025-12-18 | 2025-12-23 | 0.0200 | [公告](https://www.sse.com.cn/disclosure/fund/announcement/c/new/2025-12-15/515080_20251215_W84Y.pdf) |
| 515080 | 2026-03-13 | 2026-03-17 | 2026-03-18 | 2026-03-23 | 0.0150 | [公告](https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-03-13/515080_20260313_6GJG.pdf) |
| 515080 | 2026-06-15 | 2026-06-17 | 2026-06-18 | 2026-06-24 | 0.0200 | [公告](https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-06-15/515080_20260615_PQFR.pdf) |
| 515080 | 2026-09-11 | 2026-09-15 | 2026-09-16 | 2026-09-21 | 0.0150 | [公告](https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-09-11/515080_20260911_FBIW.pdf) |

Within the common window there are **20** dividends: 5 × `511260` and 15 × `515080`. Every announcement is dated **3–8 calendar days before its ex-date**, so terms were public before the ex-date open. For `511260` the announcements separately state 场内 and 场外 record/pay dates; the table uses the 场内 (exchange) terms, which is what the ETF price and the snapshot need.

Cross-check against Sina's fund adjustment factors (secondary, not evidence): the cumulative `u` deltas reproduce every official amount to 1e-6 when the factor feed is complete. It was **not** complete on the second retrieval — see §4.4.

Other observations:

- The `515080` distribution policy changed by holders' meeting and contract amendment ([2023-09-05 meeting result](https://www.sse.com.cn/disclosure/fund/announcement/c/new/2023-09-05/515080_20230905_RFZW.pdf), [2024-03-13 distribution-principle change](https://www.sse.com.cn/disclosure/fund/announcement/c/new/2024-03-13/515080_20240313_RK0B.pdf)). This is context for the dividend frequency, not an action term.
- No dividend correction/amendment was found in either feed (title search for 更正/补充 together with 分红/收益分配 returned no dividend notice; the only hits were contract re-issues).
- The SSE feed can carry cross-fund notices under a code: e.g. `511260` has a 2025-03-25 contract for 上证5年期国债ETF. Acquisition must verify the fund name inside each PDF, not just the code.

### 4.4 Why vendor adjustment factors are not evidence

- Tencent's `qfq` series behaves **additively**, not as a reinvestment series: it subtracts cumulative dividends from prices (Sina's fund factor is `raw / s - u`). For `515080`, with ~0.3 CNY cumulative dividends against a ~1.5 CNY price, the same day's move is reported larger: on 2022-05-20 Tencent raw close returned **+0.992%**, Tencent `qfq` returned **+1.277%**. Across 2022-05-20..2026-11-20, 128 days differ by >0.2pp for this reason, not because of extra actions.
- For `511260`, `qfq` and raw returns diverge materially on exactly the five ex-dates and nowhere else, which is a useful completeness check for the official list — but it does not give reinvestment returns.
- Sina's `qfq.js` feed is **not stable between retrievals**. At ~22:52 +08:00 it returned 6 rows for `511260` (including a 2026-09-18 sentinel row with `u=0`) and 19 for `515080`; at ~23:25 the same URL returned 5 and 18 rows, with the latest sentinel row gone. The missing row makes the most recent dividend's ex-date unrecoverable from the factor alone ([Sina qfq.js for 511260](https://finance.sina.com.cn/realstock/company/sh511260/qfq.js)).
- Consequence: derive the total-return series from **raw closes + official per-unit cash dividends**, reinvested at the ex-date close; never use `qfq`/`hfq` as the signal series or as proof that actions were captured.

## 5. Historical unadjusted prices

### 5.1 Sources probed

| Source | Endpoint | Coverage found | Fields | Result |
| --- | --- | --- | --- | --- |
| Tencent K-line | [`fqkline/get` with empty adjust](https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh511260,day,2022-01-01,2023-06-30,640,) | listing date..2026-11-20 per code | date, open, close, high, low, volume (lots) | Primary reconstruction source; exact match to TDX on sampled dates |
| TDX dated package | [`g4day/YYYYMMDD.zip`](https://www.tdx.com.cn/products/data/data/g4day/20261120.zip) | sampled 12 sessions 2022-05-20..2026-11-20, all HTTP 200 | date, open, high, low, close, volume (shares), amount (CNY) | Best dated archive; includes all five ETFs |
| Sina raw K-line | [`CN_MarketDataService.getKLineData`](https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData?symbol=sh511260&scale=240&ma=no&datalen=1023) | 1,023 bars only, 2022-08-30..2026-11-20 | date, open, high, low, close, volume | Too short for 2022-05-20 start; good overlap cross-check |
| Eastmoney K-line | [`push2his` `fqt=0`](https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.511260&klt=101&fqt=0&beg=0&end=20500101&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61) | matched Tencent date sets for `511260`, `518880`, `515080` | date, OHLC, volume, amount | Usable for 3 codes; returned no usable response for `513300`/`159612` at probe time |
| Tencent real-time quote | [`qt.gtimg.cn`](https://qt.gtimg.cn/q=sh511260) | current snapshot only | name, price, OHLC, volume, amount | Identity/name cross-check only; no history |

### 5.2 Coverage table (common window)

| Code | Bars in 2022-05-20..2026-11-20 | Missing sessions | Bars on non-sessions | First bar |
| --- | --- | --- | --- | --- |
| `511260` | 1,094 | 0 | 0 | 2017-08-24 |
| `518880` | 1,094 | 0 | 0 | 2013-07-29 |
| `515080` | 1,094 | 0 | 0 | 2019-12-27 |
| `513300` | 1,094 | 0 | 0 | 2020-11-05 |
| `159612` | **1,092** | **2** (`2025-01-10`, `2025-01-24`) | 0 | 2022-05-20 |

The two `159612` gaps are **not** vendor errors: both are full-day trading halts with official notices (§6). Tencent omits the bar; TDX writes a record with `prev_close` set and `open=high=low=close=0`, `volume=0`, which my parser drops as a no-trade record. The snapshot should represent them as `trading_status: "suspended"` rows with null OHLCV and the halt notice as evidence, not as silent omissions.

### 5.3 Cross-checks performed

- On 2022-05-20 and 2026-11-20, TDX and Tencent agree **to the last decimal** on open/high/low/close for all five codes (e.g. `159612` 2026-11-20: TDX `1.963/1.970/1.924/1.928`, Tencent `1.963/1.970/1.924/1.928`). Volume differs by the documented unit (TDX shares, Tencent lots ×100).
- Sina and Tencent date sets are identical on the overlap (2022-08-30 onward) for all five; zero Sina-only or Tencent-only dates.
- Eastmoney and Tencent date sets are identical on the overlap for `511260`, `518880`, `515080`; close differences are at most 0.001 CNY on 5–6 dates (vendor rounding).
- The 12 sampled TDX packages span the window and include rule-relevant dates (`2022-05-20`, `2022-05-23`, `2023-01-03`, `2024-03-28`, `2024-12-03`, `2025-01-10`, `2025-01-24`, `2025-09-23`, `2026-03-25`, `2026-09-18`, `2026-11-19`, `2026-11-20`). All returned HTTP 200. This is a **sample**, not an exhaustive 1,094-day check; acquisition must verify every session and record any 404 as a gap.

### 5.4 Data-quality caveats

- Vendor histories are mutable. The Tencent/Sina/Eastmoney responses retrieved today are the vendors' current reconstruction; nothing in them proves the values were visible on the session date. See §7.
- Tencent's `volume` is in lots for these ETFs; TDX's is in shares. Do not mix units in one manifest.
- Tencent history requires date-window pagination (≤640 bars per call); a naive single call truncates silently. The probe used 700-day windows and verified no missing sessions against the official calendar.

## 6. Instrument status: trading halts are the hidden blocker

The two QDII ETFs (`513300`, `159612`) have **repeated temporary trading halts due to secondary-market premium**, announced by the manager and published on the exchange feed. These are *trading* halts, not fund-level subscription/redemption suspensions, and they change what the "open" price means.

### 6.1 What the notices say

- `159612`, announcement 2024-12-30: "本基金将于2024年12月30日开市起至当日10:30停牌" — no 09:30 trade; first trade 10:30.
- `159612`, announcement 2025-01-10: "将于2025年1月10日开市起停牌，复牌时间另行公告" ([notice](https://disc.static.szse.cn/download/disc/disk03/finalpage/2025-01-09/5e5a91e4-47d5-497a-9db4-2f038b746f73.PDF)); the [2025-01-13 resume notice](https://disc.static.szse.cn/download/disc/disk03/finalpage/2025-01-10/c938a2a1-865e-444c-a239-57bc15e2f788.PDF) says trading resumes 2025-01-13. This is a full-day halt.
- `159612`, announcement 2025-01-24: "于2025年1月24日开市起停牌至收市" — full-day halt ([notice](https://disc.static.szse.cn/download/disc/disk03/finalpage/2025-01-23/13d12a73-464d-4f14-93e3-14999e929d17.PDF)).
- `159612`, announcement 2026-11-20: "于2026年11月20日开市起停牌, 自2026年11月20日10:30起复牌" ([notice](https://disc.static.szse.cn/download/disc/disk03/finalpage/2026-11-20/fe9a7c30-bda1-43c5-a9ee-e0dd83c91352.PDF)).
- `513300`, announcement 2026-11-20: "将于2026年11月20日开市起至当日10:30停牌" ([notice](https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-11-20/513300_20261120_KN3R.pdf)).

Exchange trading hours confirm that "开市" is 09:30: opening call auction 09:15–09:25, continuous trading 09:30–11:30 and 13:00–14:57, closing auction 14:57–15:00 for SZSE securities ([SZSE trading rules summary](https://www.szse.cn/www/investor/institute/rules/t20230629_601434.html)); ETFs trade 09:30–11:30 and 13:00–15:00 ([SSE ETF page](http://www.sse.com.cn/assortment/fund/etf/home/c/c_20151111_4011009.shtml)). A 09:30–10:30 halt therefore means **there is no 09:30 opening price**; the vendor daily `open` is the first trade at 10:30.

### 6.2 Counts (all parsed from official notices)

| Code | Full-day halts in window | Partial open halts in window | Range | Examples |
| --- | --- | --- | --- | --- |
| `159612` | **2** (`2025-01-10`, `2025-01-24`) | **118** | 2024-12-03..2026-11-20 | [2024-12-30](https://disc.static.szse.cn/download/disc/disk03/finalpage/2024-12-27/3b80f5ae-eccc-4137-b538-6db6be0d7d52.PDF), [2026-11-20](https://disc.static.szse.cn/download/disc/disk03/finalpage/2026-11-20/fe9a7c30-bda1-43c5-a9ee-e0dd83c91352.PDF) |
| `513300` | 0 | **43** | 2024-12-24..2026-11-20 | [2026-11-20](https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-11-20/513300_20261120_KN3R.pdf) |
| `511260`, `518880`, `515080` | 0 | 0 | — | only fund-level 申赎 suspensions (not trading halts) |

Per partition (using the sealer's floor(3N/5), floor(N/5) rule on 1,094 sessions):

| Partition | Sessions | Dates | `159612` halted sessions | `513300` halted sessions |
| --- | --- | --- | --- | --- |
| Discovery | 656 | 2022-05-20..2025-01-27 | 33 (31 partial + 2 full) | 2 |
| Validation | 218 | 2025-02-05..2025-12-22 | 86 partial | 0 |
| Final Holdout | 220 | 2025-12-23..2026-11-20 | 1 partial | 41 |

Consequences for #24:

1. **The Discovery equal-weight baseline's one-time buy is safe**: the signal is at the 2022-05-20 close and the fill attempt is 2022-05-23, before any halt. All five opens exist that day (checked).
2. **A naive real snapshot would silently lie on 35 Discovery sessions** (33 `159612` + 2 `513300`): the vendor `open` is a 10:30 print, but the baseline's `_is_executable_open` only checks `trading_status == "trading"` and `available_at <= 09:30`. Without a halt field, a strategy that trades on those days would "fill" at a price that did not exist at 09:30.
3. **The Discovery baseline will have two unknown-NAV days** (`2025-01-10`, `2025-01-24`) because it holds `159612` and there is no close either. Per [#10](https://github.com/ZombieIce/PrajnaTrade/issues/10), a held ETF with no verifiable close must not be marked from a stale price; the day must be reported as a gap. The baseline is still computable end-to-end with those two gaps declared, but it cannot claim complete coverage.
4. Fund-level 申赎 suspensions (`518880` gold spot subscription/redemption suspension in 2025; frequent QDII 暂停申购/赎回) are **not** exchange trading halts and must not be encoded as `trading_status: suspended`. They are separate status events; the snapshot may record them as notes, but they do not invalidate the daily bar.

## 7. Availability and point-in-time evidence

The design needs an `available_at` + evidence for every row. What public sources actually establish:

| Item | Earliest defensible availability | Evidence today | Limitation |
| --- | --- | --- | --- |
| Trading calendar | Before the year starts | SSE annual notice dated previous December (e.g. 2025-12-22 for 2026) | Not a per-day publication; the annual notice is the dated instrument |
| Listing/fund name | ~3 days before listing | Listing announcement PDF + date | Date-level only |
| Dividend amount, record, ex, pay | Announcement day (3–8 days before ex-date) | PDF + 公告送出日期 | Exchange APIs expose date, not time; use end-of-announcement-day as conservative |
| Halt window | Announcement day (often same day, before open) | PDF; each notice states the halt date and resume time | Intraday publication time not exposed; Monday halts are often announced Saturday |
| Daily open | 09:25/09:30 on session T by market rule | Exchange trading-hours rules; the day's bar | **No archived 09:30 quote**; the archive only proves the day's OHLC after the close |
| Daily close | After 15:00 on T | TDX zip member timestamps (e.g. 2026-11-20 15:37–15:38), Tencent current history | Vendor build time, not an independent notarisation; no official per-session OHLC archive found |
| Total-return series | After close T, after terms known | Derivation rule + raw bar + official action | Derived, not published |

Specific findings:

- **TDX archive timestamps are the best dated price evidence found.** `unzip -l` on the [2026-11-20 package](https://www.tdx.com.cn/products/data/data/g4day/20261120.zip) shows member timestamps `2026-11-20 15:36–15:38`; the 2022-05-20 package shows `2022-05-20 15:36–15:37`. This is a vendor-side build time attached to a date-named archive, not an exchange publication.
- **No public official per-session per-ETF OHLC archive was found.** SSE's fund announcement API and SZSE's announcement API publish notices, not daily quotes; the SSE fund data pages I probed expose current/aggregate data, not a dated historical per-ETF file. This is marked unverified rather than "does not exist".
- **The 09:30 open is not archived.** For any fill, "the open was available at 09:30" is a market-rule assertion (opening call auction / continuous trading starts 09:30), not an observation. If #24 requires archived proof, no public source satisfies it and the fill rule must be changed or the ticket blocked.
- **Current retrieval is not point-in-time evidence.** Tencent/Sina/Eastmoney return a current reconstruction; their values for 2022 could have been revised. Sina's factor feed demonstrably changed within 30 minutes (§4.4). Do not put the collection time in `available_at`.
- **Official announcement time granularity is one day.** `SSEDATE` (SSE) and `publishTime` with `00:00:00` (SZSE) are date-level. For ex-date decisions this is sufficient because every dividend was announced ≥3 days earlier; for same-day halt notices it is not sufficient to prove pre-open publication, though the notices themselves are dated before the halt.

## 8. Source matrix

| # | Source | URL / API | Type | Coverage | Fields | Availability evidence | Method | Gaps |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | SSE annual holiday notices | [hub](https://www.sse.com.cn/disclosure/dealinstruc/closed/) and the five annual notices ([2022](https://www.sse.com.cn/disclosure/announcement/general/c/c_20211220_5662606.shtml), [2023](https://www.sse.com.cn/disclosure/announcement/general/c/c_20221227_5714458.shtml), [2024](https://www.sse.com.cn/disclosure/announcement/general/c/c_20231226_5733939.shtml), [2025](https://www.sse.com.cn/disclosure/announcement/general/c/c_20241223_10767108.shtml), [2026](https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml)) | Official | 2022–2026 | closure ranges | notice publication date | HTML fetch | no machine API; parse text |
| 2 | SZSE monthly calendar | [`monthList?month=YYYY-MM`](https://www.szse.cn/api/report/exchange/onepersistenthour/monthList?month=2022-05) | Official | 2022-01..2026-11 | `jyrq`, `jybz` | API is current view; annual notice is dated instrument | JSON | current-view, not an archive |
| 3 | SSE fund announcements | [`COMMON_PL_JJXX_JJGG_L`](https://query.sse.com.cn/commonQuery.do?sqlId=COMMON_PL_JJXX_JJGG_L&isPagination=true&pageHelp.pageSize=100&pageHelp.pageNo=1&pageHelp.beginPage=1&pageHelp.cacheSize=1&pageHelp.endPage=1&SECURITY_CODE=511260) | Official venue-hosted manager notices | 2013–2026, per-code start varies (2017 for `511260`, 2019 for `515080`, 2020 for `513300`) | title, date, PDF | `SSEDATE` | JSON + PDF | date-level; cross-fund notices possible |
| 4 | SZSE fund announcements | [`annList`](https://www.szse.cn/api/disc/announcement/annList) POST `fundinfoNotice_disc` | Official venue-hosted manager notices | 2022..2026 (`159612`) | title, `publishTime`, PDF | `publishTime` (date) | JSON + PDF | date-level |
| 5 | CSRC fund disclosure (EID) | [`http://eid.csrc.gov.cn/fund`](http://eid.csrc.gov.cn/fund) | Official regulator venue (referenced by fund announcements) | fund documents | prospectus, announcements | publication | HTML (HTTP 200 meta; HTTPS TLS failed from probe) | not fully probed |
| 6 | TDX dated daily packages | [`g4day/YYYYMMDD.zip`](https://www.tdx.com.cn/products/data/data/g4day/20261120.zip) | Vendor (dated archive) | sampled 12 sessions, all 200 | OHLC, volume (shares), amount | member timestamps on session date 15:36–15:38 | zip + binary parse | vendor, not exchange; 404 possible; full-window check pending |
| 7 | Tencent K-line | [`fqkline/get`](https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh511260,day,2022-01-01,2023-06-30,640,) | Vendor | 2013..2026 | date, OHLC, volume (lots) | none (current reconstruction) | JSON | no PIT; pagination limit |
| 8 | Sina raw K-line | [`getKLineData`](https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData?symbol=sh511260&scale=240&ma=no&datalen=1023) | Vendor | 2022-08-30..2026-11-20 (1,023 bars) | date, OHLC, volume | none | JSON | too short for full window |
| 9 | Sina fund factor | [`qfq.js`](https://finance.sina.com.cn/realstock/company/sh511260/qfq.js) | Vendor | event rows | `s`, `u` | none; observed unstable | JS/JSON | not evidence; feed changed between retrievals |
| 10 | Eastmoney K-line | [`push2his`](https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.511260&klt=101&fqt=0&beg=0&end=20500101&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61) | Vendor | 2013..2026 | date, OHLC, volume, amount | none | JSON | no usable response for 2 codes at probe; rate limits |
| 11 | Exchange trading-hours rules | [SSE ETF](http://www.sse.com.cn/assortment/fund/etf/home/c/c_20151111_4011009.shtml), [SZSE rules summary](https://www.szse.cn/www/investor/institute/rules/t20230629_601434.html) | Official | current rules | session times, call auctions | publication | HTML | rule, not a per-day archive |
| 12 | Fund manager pages | [华安 518880](https://www.huaan.com.cn/funds/518880/index.shtml), [华夏 513300](https://www.chinaamc.com/fund/513300/index.shtml) | Official manager | current | fund identity | page | HTML | 国泰/招商 product pages not located; announcements used instead |

## 9. Acquisition and sealing plan for #24

### 9.1 Decisions that must be made before sealing

1. **Open availability policy.** Recommended: accept the 09:30 open as available at 09:30 by exchange rule, with evidence = trading-hours rule + the session bar + (where relevant) the halt notice; mark it `open_available_at`. Alternative: require an archived 09:30 quote — not publicly available, so #24 would be blocked. This is a policy choice, not a data-fetch problem.
2. **Halt encoding.** Recommended: add `trading_status: "partial_halt"` (or a `halt: {start, end}` object) so the manifest states that no 09:30 open exists; the bar's OHLC then describes the 10:30-onward session. Without this, the snapshot must be declared "open not executable on 35 Discovery sessions" in a free-text evidence field, which is machine-invisible.
3. **Gap policy for the two full halts.** Recommended: include `2025-01-10`/`2025-01-24` as `suspended` rows with null OHLCV and the halt notice as evidence; the Discovery baseline reports two unknown-NAV days. Do not carry a stale close.
4. **Date-level action availability.** Recommended: accept end-of-announcement-day (23:59:59 +08:00) as `available_at`, with the PDF and 公告送出日期 as evidence. This is before every ex-date open in this dataset.
5. **Reconciliation tolerance.** Pre-register e.g. 1e-6 for derived values, 1e-4 for NAV, exact match for trade counts/fees, before the second researcher runs.

### 9.2 Evidence to acquire and archive (outside Git)

| Item | Count | What to capture |
| --- | --- | --- |
| SSE notices | 5 | URL, publication date, SHA-256, parsed closure ranges |
| SZSE month JSON | 59 | URL, `month`, SHA-256, session flags |
| Listing announcements | 5 | PDF, SHA-256, full fund name, manager, venue, listing date |
| Inception conversion notices | 4 | PDF, SHA-256, ratio/effective date (context only) |
| Dividend announcements | 23 | PDF, SHA-256, announced date, record/ex/pay, per-unit amount, fund name inside PDF |
| Halt notices | 165 (`513300` + `159612`) | PDF, SHA-256, halt date, start/end, full vs partial, fund code |
| TDX packages | 1,094 sessions (verify each) | zip URL, SHA-256, member timestamps, extracted rows; record 404 as gap |
| Vendors cross-check | per session | Tencent raw OHLC, Sina overlap, EM where available; keep hashes, not raw dumps in Git |

Store all of this in the trusted read-only store described by [#19](https://github.com/ZombieIce/PrajnaTrade/issues/19), not in the project repo. The snapshot JSON can be generated from the store and then sealed; the store retains the evidence behind each `availability_evidence` string.

### 9.3 Manifest/schema changes needed

The current input schema is synthetic-only and cannot express the real-data facts:

- `src/prajnatrade/snapshot.py` rejects anything except `data_kind == "synthetic"` and hardcodes `data_kind` in the manifest and receipt ([line 97](../../src/prajnatrade/snapshot.py#L97), [line 247](../../src/prajnatrade/snapshot.py#L247), [line 312](../../src/prajnatrade/snapshot.py#L312)).
- `src/prajnatrade/snapshot_baseline.py` emits `"data_kind": "synthetic"`, and its `_is_executable_open` checks the row's single `available_at` against 09:30 ([line 452](../../src/prajnatrade/snapshot_baseline.py#L452), [lines 569–593](../../src/prajnatrade/snapshot_baseline.py#L569-L593)). A row whose `available_at` is the close publication cannot fill at the open; a row whose `available_at` is 09:30 would leak the close into a 09:30 projection.
- `src/prajnatrade/experiment.py` requires synthetic for campaign creation ([line 178](../../src/prajnatrade/experiment.py#L178)); `protected_eval.py`, `release.py`, `shadow.py` also pin synthetic scope and are later tickets.

Recommended minimal schema extension for #24 (a code change, not part of this note):

- `data_kind: "real"` accepted by the sealer, with a mandatory `evidence_bundle` (store-relative IDs + hashes) and no fallback to synthetic.
- `raw_bars`: `open_available_at`, `close_available_at`, `trading_status` ∈ `{trading, partial_halt, suspended, unknown}`, optional `halt: {start, end}` (all timezone-aware).
- `corporate_actions`: keep `date` = ex-date; add `record_date`; keep `pay_date`, `cash_per_share`; require `terms_evidence` to be a source reference (e.g. `sse-fund-announcement:<url>@<date>`).
- `derived`: `available_at = close_available_at`; `derivation_rule_version` e.g. `tr-reinvest-exclose-v1`.
- Manifest: `data_kind: real`, `evidence` block (source hashes), `gaps` including suspended days and missing TDX packages, and partition hashes as today.
- Baseline: `_is_executable_open` uses `open_available_at` and rejects `partial_halt` when the halt covers 09:30; NAV uses `close_available_at`; the two full-halt days become declared NAV gaps.

### 9.4 Steps and expected output

1. Freeze the scope: 1,094 sessions, 2022-05-20..2026-11-20; five codes; 20 in-window dividends.
2. Acquire and hash the evidence in §9.2; build a source manifest.
3. Build `real.json` (schema version 2) with:
   - `symbol_coverage`: each code from listing date to 2026-11-20 (so common start is 2022-05-20);
   - `calendar`: 1,094 session rows with the annual-notice availability;
   - `raw_bars`: 1,094 × 5 rows, with the two `159612` full-halt days included as `suspended` rows with null OHLCV (not omitted);
   - `corporate_actions`: the 20 in-window dividends; the three pre-window `515080` dividends and the two inception conversions are evidence/context, not rows in this calendar;
   - `derived`: total-return base 1.0 at 2022-05-20, reinvesting each official dividend at its ex-date close.
4. Seal into the trusted store; verify manifest common range and partition boundaries:
   - Discovery 656 sessions, 2022-05-20..2025-01-27
   - Validation 218, 2025-02-05..2025-12-22
   - Final Holdout 220, 2025-12-23..2026-11-20
5. Run the Discovery equal-weight baseline (initial cash 100,000; 5 bps cost; 0 slippage as the base case; then the higher-cost scenario).
6. Independent researcher recomputes from the same snapshot ID and Discovery receipt; compare trade count, fees, dividends, NAV path, the two NAV gaps, and end NAV within the pre-registered tolerance.
7. Publish the receipt and the gap list; do not claim complete coverage or OS isolation.

### 9.5 Baseline sanity check (author's, not a deliverable)

A quick end-to-end run on the probe data (Tencent raw + official dividends, 5 bps cost, no slippage, buy at the 2022-05-23 open) filled all five symbols and produced a final NAV of **≈176,741.79** on 100,000 (≈+76.7%) with **2 unknown-NAV days** (`2025-01-10`, `2025-01-24`). This confirms the inputs are internally usable; it is **not** the sealed baseline and must not be quoted as the #24 result.

## 10. Verified / partial / blocked

### Verified now (primary sources)

- Five identities, exchanges, managers and listing dates (§2).
- SSE/SZSE calendar 2022–2026, with 92/92 weekday non-sessions matching and 0 conflicts (§3).
- 23 cash dividends with record/ex/pay/amount and official PDFs; 0 dividends for `518880`, `513300`, `159612`; 0 share adjustments after 2022-05-20 (§4).
- Unadjusted daily OHLCV coverage: 1,094/1,094 for four codes, 1,092/1,094 for `159612`; the 2 gaps are official full-day halts (§5, §6).
- 118 partial + 2 full `159612` halts and 43 partial `513300` halts, parsed from official notices (§6).
- The equal-weight baseline is computable end-to-end with declared gaps (§9.5).

### Partial

- Point-in-time availability is date-level for actions and vendor-timestamp-level for prices; the 09:30 open is rule-based, not archived (§7).
- TDX archive coverage is sampled (12/12), not exhaustively verified for 1,094 sessions.
- Eastmoney cross-check covers 3/5 codes; `513300`/`159612` returned no usable response.
- Sina factor corroboration is incomplete/unstable across retrievals.
- CSRC EID was not fully probed (HTTP 200 meta; HTTPS TLS failure from this environment).
- Manager product pages for 国泰/招商 were not located; exchange-hosted announcements carry the identity.

### Blocked (and unblock conditions)

| Blocker | Impact | Unblock condition |
| --- | --- | --- |
| Sealer/baseline/campaign hardcode synthetic ([snapshot.py](../../src/prajnatrade/snapshot.py#L97), [snapshot_baseline.py](../../src/prajnatrade/snapshot_baseline.py#L452), [experiment.py](../../src/prajnatrade/experiment.py#L178)) | No real snapshot or real Discovery baseline can be sealed/delivered | Code change: accept `data_kind: "real"`, validate evidence, update baseline output |
| Single `available_at` per raw bar, checked at 09:30 | Cannot represent open vs close availability; real rows either block all fills or leak the close | Per-field `open_available_at` / `close_available_at` |
| No halt field; only `trading/suspended/unknown` | 35 Discovery sessions would be treated as normal 09:30 opens (10:30 prints) | Add `partial_halt` + halt window, or explicitly quarantine those sessions |
| Two `159612` full-day halts inside Discovery | Baseline has two unknown-NAV days; cannot claim complete coverage/admission-grade | Declare gaps; human decision on whether the baseline is acceptable with gaps |
| No archived 09:30 open | Cannot *prove* the fill price was available at the fill instant | Human policy decision (accept market-rule assertion) or obtain an intraday archive (not found) |
| Independent human reconciliation not performed | #24 acceptance requires a second researcher on the same inputs | Named second researcher, pre-registered tolerance, comparison receipt |
| Deployment isolation/Discovery-only enforcement not verified (#12/#17) | Research-side access is CLI-checked, not OS-enforced | #12 deployment boundary; not solvable in this ticket |

## 11. Recommended next implementation step

**Extend the sealer for `data_kind: real` before acquiring anything else, and make the open/close/halt semantics explicit.** Concretely:

1. Change `snapshot.py` to accept schema-version-2 real inputs with an evidence bundle, `data_kind: "real"`, per-field availability, `partial_halt`/`suspended` statuses, and a mandatory action-evidence reference.
2. Change `snapshot_baseline.py` to use `open_available_at` at 09:30 and `close_available_at` for NAV, reject `partial_halt` at the 09:30 open, and stop hardcoding `data_kind: "synthetic"`.
3. Acquire the 23 dividend PDFs, 5 listing announcements, 4 conversion notices, 5 SSE notices, 59 SZSE months, 165 halt notices, and the 1,094 TDX packages; hash everything in the trusted store.
4. Seal, run the Discovery baseline, and hand the same snapshot ID to a second researcher for reconciliation.

Do **not** use Tencent `qfq`/Sina factors as the total-return series, and do **not** seal a "real" label over these inputs without the halt/gap declarations: that would reproduce exactly the failure the spec warns about ("real label is not proof"; "a real snapshot must report coverage and gaps").

If the human owner requires archived proof of the 09:30 open rather than the market-rule assertion, stop and ask for an intraday/opening-auction archive; no public source found here provides it, and the baseline's fill rule would need to change first.

## 12. Reproduction references

Probe commands (no secrets; artifacts in `/tmp/research24`, not committed):

```sh
# SZSE calendar (one month)
curl -sS 'https://www.szse.cn/api/report/exchange/onepersistenthour/monthList?month=2022-05' \
  -H 'Referer: https://www.szse.cn/'

# SSE fund announcements (paginate pageHelp.pageNo to pageCount)
curl -sS 'https://query.sse.com.cn/commonQuery.do?sqlId=COMMON_PL_JJXX_JJGG_L&isPagination=true&pageHelp.pageSize=100&pageHelp.pageNo=1&pageHelp.beginPage=1&pageHelp.cacheSize=1&pageHelp.endPage=1&SECURITY_CODE=511260' \
  -H 'Referer: https://www.sse.com.cn/'

# SZSE fund announcements
curl -sS -X POST 'https://www.szse.cn/api/disc/announcement/annList' \
  -H 'Content-Type: application/json' -H 'Referer: https://www.szse.cn/disclosure/fund/notice/index.html' \
  -d '{"channelCode":["fundinfoNotice_disc"],"pageSize":50,"pageNum":1,"stock":["159612"]}'

# Tencent raw daily bars (empty adjust = unadjusted)
curl -sS 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh511260,day,2022-01-01,2023-06-30,640,'

# TDX dated package
curl -sS -o 20261120.zip 'https://www.tdx.com.cn/products/data/data/g4day/20261120.zip'

# PDF terms
pdftotext -layout announcement.pdf -
```

Unverified / not established in this note:

- Whether SSE or SZSE publish a public, dated per-session per-ETF OHLC archive (none found; not proven absent).
- Whether TDX packages exist for every one of the 1,094 sessions (12 sampled; per-session check pending).
- Intraday publication timestamps of exchange announcements (date-level only).
- CSRC EID content (site referenced by fund announcements; HTTPS probe failed from this environment).
- Whether `159612`'s 2025-01-10 halt was announced before the 09:30 open on 2025-01-10 (the PDF is dated 2025-01-10; the notice's own date is the only evidence).
