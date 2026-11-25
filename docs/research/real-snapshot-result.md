# Real five-ETF snapshot: acquisition, seal and Discovery baseline result (MVP-06 / #24)

**Accepted for #24 historical Discovery on 2026-11-22.** An independent
Agent performed the calculation in a Discovery-only Colima container.
@ZombieIce accepted both cost scenarios and the two full-halt valuation
gaps. [Review record](issue-24-researcher-handoff.md)

This note records what was built from the researched probe artifacts, what is
official evidence, what is a market-rule assertion, and what is still missing.
The acquisition input and evidence live outside Git; only the reproducible
scripts, the baseline outputs and the result notes are committed.

## Snapshot

| Item | Value |
| --- | --- |
| Snapshot ID (canonical manifest SHA-256) | `b07823329f95b2f3d6a8d7e5fb7660941eda25150dc2b7a3ae8086357e02d41d` |
| Schema / kind | `schema_version: 2`, `data_kind: "real"`, `raw_price_basis: "unadjusted"` |
| Trusted store | `~/PrajnaTrade-trusted/real-etf-snapshot/store` |
| Evidence archive | `~/PrajnaTrade-trusted/real-etf-snapshot/evidence` (211 files, ~45 MB, outside Git) |
| Acquisition input | `real.json` in the build workdir (5.4 MB, outside Git) |
| Common window | 2022-05-20 .. 2026-11-20, **1,094 sessions** |
| Rows | 1,094 calendar, 5,470 raw bars, 5,470 derived values, 20 corporate actions |
| Sources | 4: calendar, raw_bars, corporate_actions, derived |
| Evidence items | 211, all SHA-256 verified against the copied files |
| Derivation | `tr-reinvest-exclose-v1` / `cash_dividend_reinvested_at_ex_date_close` |
| Symbol coverage | `511260` 2017-08-24, `518880` 2013-07-29, `515080` 2019-12-27, `513300` 2020-11-05, `159612` 2022-05-20 .. 2026-11-20 |

Partition hashes fixed by the sealed manifest:

| Partition | Sessions | Range | Content hash |
| --- | --- | --- | --- |
| Discovery | 656 | 2022-05-20 .. 2025-01-27 | `1fb6d1db77c25c35ded5ea5e82655b81e3581878ee3d74526bdc57d324e75f54` |
| Validation | 218 | 2025-02-05 .. 2025-12-22 | `cecd3e8367066bf0302aedd7a7840df8b11816dc7699543a1b1e179bd730dce5` |
| Final Holdout | 220 | 2025-12-23 .. 2026-11-20 | `fc5afe2eba8a13eb6413468161e01f0787c129ae13156b9f02a96dde85b13f9e` |

Manifest content hashes over the common window: calendar
`d8a8cd3b6c4ec52c597350bfc6093980b5155465df48c5aab4ff9bebe15c5519`,
raw bars `fff79369cecd3f84a4545145dd6379d4e907dab83ae520c2990b6fa4b99cb39b`,
derived `1112ca0c59ec19e2b60629bc556a463b8e92d1ce6a32310733c1662a087fbd8b`,
corporate actions `fc5f334a53c9441f689762a2f6b52725f956e3ff0569717a6a85b2031864a252`.

The evidence block hash (canonical JSON of `manifest.evidence`) is
`c356216e030b248c7c1ec59516e3987eb5dad54e4447a8c17417dc5dbc71ca73`.

## Reproduce the historical schema-2 result

Run the acquisition, seal, baseline and reconciliation at
[the original schema-2 revision](https://github.com/ZombieIce/PrajnaTrade/commit/8a6fbcdfb15c3d1819c499c72fd18c22b9ebac06).
The current acquisition script emits schema 3. Use the original probe cache
with its file modification times preserved, since those timestamps enter the
evidence and row metadata. The commands below use a temporary checkout, evidence
directory and store. Install the dependencies from the historical checkout's
`pyproject.toml` before sealing or running the baselines.

```sh
REPLAY_DIR="$(mktemp -d)"
WORKDIR="$REPLAY_DIR/build"
STORE="$REPLAY_DIR/store"
SNAPSHOT_ID=b07823329f95b2f3d6a8d7e5fb7660941eda25150dc2b7a3ae8086357e02d41d
git worktree add --detach "$REPLAY_DIR/checkout" 8a6fbcdfb15c3d1819c499c72fd18c22b9ebac06
cd "$REPLAY_DIR/checkout"

# 1. Build real.json from the archived probe cache.
python3 scripts/acquire_real_etf_snapshot.py \
  --cache-dir /tmp/research24 \
  --store "$STORE" \
  --workdir "$WORKDIR" \
  --output "$WORKDIR/real.json"

# 2. Seal, verify, and read only the approved Discovery partition.
PYTHONPATH=src python3 -m prajnatrade snapshot seal \
  --input "$WORKDIR/real.json" --store "$STORE"
PYTHONPATH=src python3 -m prajnatrade snapshot manifest --store "$STORE" --id "$SNAPSHOT_ID"
PYTHONPATH=src python3 -m prajnatrade snapshot read \
  --store "$STORE" --id "$SNAPSHOT_ID" --partition discovery

# 3. Discovery equal-weight baseline (5 bps / 0 slippage) and the high-cost scenario.
PYTHONPATH=src python3 -m prajnatrade baseline --store "$STORE" --snapshot-id "$SNAPSHOT_ID" \
  --initial-cash 100000 --transaction-cost-bps 5 --slippage-bps 0 \
  --output docs/research/real-discovery-baseline.json
PYTHONPATH=src python3 -m prajnatrade baseline --store "$STORE" --snapshot-id "$SNAPSHOT_ID" \
  --initial-cash 100000 --transaction-cost-bps 20 --slippage-bps 10 \
  --output docs/research/real-discovery-baseline-high-cost.json

# 4. Independent machine cross-check (sealed Discovery + full-window input sanity).
python3 scripts/reconcile_real_etf_baseline.py --store "$STORE" --snapshot-id "$SNAPSHOT_ID" \
  --baseline docs/research/real-discovery-baseline.json \
  --baseline-high-cost docs/research/real-discovery-baseline-high-cost.json \
  --input "$WORKDIR/real.json" \
  --json-output docs/research/real-snapshot-reconciliation.json

# 5. Lint and tests.
ruff check .
python3 -m pytest -q -m "not isolation"
```

`--refetch` re-downloads the Tencent bars, the 59 SZSE month responses and the
announcement PDFs into `<workdir>/refetch`; the reproducible default run uses
only the researched cache. With the original cache and pinned revision, step 1
reproduces `real.json` byte-for-byte (SHA-256
`1fe96ff206aeb1c333d6f916cae5b39333458572e58397707e153ca799438b64`),
and step 2 returns the snapshot ID shown above.

## What is real, asserted and missing

**Real (primary-source evidence hashed in the snapshot):**

- Five instrument identities and listing dates from exchange-hosted listing
  announcements; `159612` is SZSE, the other four are SSE.
- Trading calendar: five SSE annual holiday notices plus the SZSE monthly
  `monthList` transcription; 1,094 common sessions, 92/92 weekday non-sessions
  matched and 0 conflicts as recorded in the research note.
- 20 in-window cash dividends (5 × `511260`, 15 × `515080`) with record date,
  ex-date, pay date and per-unit amount read from official SSE PDFs. No share
  adjustments inside the window (complete official enumeration, research note
  §4.2); the four inception conversions are archived as context evidence only.
- 118 partial + 2 full `159612` halts and 43 in-window partial `513300` halts,
  parsed from official notices. The 44th `513300` halt row is 2026-11-23,
  outside the window, and is not in the snapshot.
- Unadjusted daily OHLCV from Tencent (`fqkline`, empty adjustment); the
  research note's sampled TDX overlap is exact, and seven dated TDX packages
  are archived as evidence.

**Asserted (not archived point-in-time evidence):**

For #24 Discovery the owner accepts 09:30 and 15:00 Asia/Shanghai as the
normal session's open and close. The trading calendar, instrument halts and
confirmed prices govern exceptions. A delayed first print cannot fill the
09:30 attempt. A full suspension yields no fill and missing held closes
remain unknown for valuation.

- `open_available_at = 09:30 +08:00`: exchange trading rules say continuous
  trading starts at 09:30; no archived 09:30 quote exists. This is a
  **market-rule assertion**, recorded per row and in `availability_evidence`.
- `close_available_at = 15:00 +08:00`: likewise a market-rule assertion. The
  seven archived TDX `g4day` packages carry vendor build timestamps around
  15:36–15:38 on their session date; they are sampled vendor-side evidence,
  not an exchange notarisation, and only 7 of 1,094 sessions were archived.
- Dividend and halt terms are `available_at` the announcement day at
  `23:59:59 +08:00` because the exchange feeds expose a date, not a time. Every
  in-window dividend was announced 3–8 calendar days before its ex-date, so
  the terms were public before the ex-date open.
- Prices and the total-return series are a **current vendor reconstruction**
  (`collected_at` 2026-11-21 22:53:50–22:57:37 +08:00, inside the research
  retrieval window 22:49–23:35). Every `collected_at` is the artifact file
  mtime (or the maximum of the row's input artifacts), not the script run time.
  The snapshot does **not** claim the values were visible on their session
  dates. Source-level `available_at` for `raw_bars`
  and `derived` is the retrieval time for that reason, while row-level
  per-field timestamps carry the market-rule assertion.

**Missing / not established:**

- The Colima access receipt covers the #24 Discovery calculation. Deployment
  evidence for the external model and protected evaluation belongs to later stages.
- No archived 09:30 opening quote or notarised intraday archive was found.
- TDX archive coverage is a 7-session sample, not the full 1,094 sessions.
- The SZSE calendar evidence is the probe's machine transcription
  (`calendar_szse.json`) of the 59 month responses; the raw HTTP responses were
  not retained. The five SSE notices are archived in full HTML.
- `513300`'s listing evidence is the official 上市交易公告书提示性公告 (which
  states the 2020-11-05 listing); only that PDF was retained, not the separate
  full 上市交易公告书.

## Bars and halts

- Regular sessions: `trading_status: "trading"`, Tencent unadjusted
  O/H/L/C/volume, `open_available_at` 09:30, `close_available_at` 15:00.
- Partial halts (161 in window): `trading_status: "partial_halt"`,
  `open: null` — the vendor daily "open" is the 10:30 resume print and is not
  stored as the 09:30 open — while high/low/close/volume describe the resumed
  session. `halt` is 09:30–10:30 and `open_available_at` is the 10:30 resume.
- Full halts: `trading_status: "suspended"`, all OHLCV null, with the halt
  window. `159612` on **2025-01-10** was suspended from the open with the
  resumption announced for the 2025-01-13 open; `159612` on **2025-01-24** was
  suspended "至收市" (to the close). Both notices and the 2025-01-13 resume
  notice are hashed evidence.
- **Trailing partial halt:** on **2026-11-20** both QDII codes halted
  09:30–10:30. `159612` closed at 1.928 (derived 1.9754098360655674) and
  `513300` at 2.523 (derived 2.468688845401173). The close is confirmed, so the
  day counts as complete and the common window ends there; its missing open is
  still listed as a gap.
- Manifest gap counts: `raw_bars` 163 rows (161 partial-halt null opens + 2
  suspended), `derived` 2 (the two suspended `159612` days),
  `corporate_actions` 0, `availability` 0.
- Vendor volume is stored as delivered by Tencent (lots); it is not combined
  with TDX's share counts.

## Dividends and derived total return

- 20 in-window actions: `date` = ex-date, `record_date` from the exchange
  (场内) record date, `pay_date` from the exchange pay date,
  `cash_per_share` = official CNY per unit, `terms_evidence` = the official SSE
  PDF URL plus announcement date, `available_at` = announcement day
  `23:59:59 +08:00`. The three pre-window `515080` dividends are context only.
- Derived rule `tr-reinvest-exclose-v1`: base 1.0 on 2022-05-20;
  `TR[t] = TR[t-1] * (close[t] + D[t]) / close[t-1]`, where `D[t]` is the
  official per-unit dividend whose ex-date is `t`; `null` on suspended days; no
  share adjustments in the window. Vendor `qfq`/`hfq` series are never used
  (the `qfq` probe is archived only as a negative cross-check).

## Discovery equal-weight baseline

Outputs: [`real-discovery-baseline.json`](real-discovery-baseline.json)
(5 bps, 0 slippage) and
[`real-discovery-baseline-high-cost.json`](real-discovery-baseline-high-cost.json)
(20 bps, 10 bps).

| Metric | Base case | High cost |
| --- | --- | --- |
| Initial cash | 100,000.00 | 100,000.00 |
| Signal / fill | 2022-05-20 signal, 2022-05-23 open | same |
| Fills | 5 (all five ETFs, equal weight) | 5 |
| Fees paid | 49.97501249375313 | 199.60079840319364 |
| Slippage paid | 0.0 | 99.70069850308315 |
| Dividends paid (8 × `515080`) | 2,803.6472647266837 | 2,796.6535344486324 |
| Ending NAV (2025-01-27) | **155,603.34717297764** | **155,215.19283766544** |
| Total return | +55.6033% | +55.2152% |
| Valued sessions | 654 of 656 | 654 of 656 |

The single base-case buy on 2022-05-23 filled 170.11032913660947 shares of
`511260` at 117.512, 5,158.710967097098 of `518880` at 3.875,
14,018.236323633417 of `515080` at 1.426, 19,559.691778376957 of `513300` at
1.022 and 20,460.598769192682 of `159612` at 0.977, each with 19,990.00499750125
notional and 9.995002498750626 fee. The single order attempt is `filled`; there
are no deferred or repeated attempts and no signal gaps.

Dividend cash in the base case comes from eight `515080` distributions:
2022-06-27 (0.030), 2022-11-29 (0.030), 2023-06-16 (0.035), 2023-12-01
(0.035), 2024-03-28 (0.015), 2024-07-01 (0.020), 2024-09-20 (0.015) and
2024-12-02 (0.020); amounts are 420.5471, 420.5471, 490.6383, 490.6383,
210.2735, 280.3647, 210.2735 and 280.3647 CNY respectively.

### Declared gaps in the Discovery baseline

`gaps.valuation` (and `gaps.derived`) contain the two `159612` full halts;
`gaps.raw_bars` contains 35 entries (33 partial-halt null opens + the same 2
suspended days):

| Symbol | Status | Count | Dates |
| --- | --- | --- | --- |
| `159612` | partial_halt | 31 | 2024-12-03, 12-04, 12-12, 12-13, 12-16, 12-17, 12-18, 12-19, 12-20, 12-23, 12-24, 12-25, 12-26, 12-27, 12-30, 12-31, 2025-01-02, 01-03, 01-06, 01-07, 01-08, 01-09, 01-14, 01-15, 01-16, 01-17, 01-20, 01-21, 01-22, 01-23, 01-27 |
| `513300` | partial_halt | 2 | 2024-12-24, 2025-01-07 |
| `159612` | suspended | 2 | 2025-01-10, 2025-01-24 |

The two **unknown-NAV days** are 2025-01-10 (halt 09:30 → 2025-01-13 09:30) and
2025-01-24 (halt 09:30 → 15:00). NAV is `null` on both days; no stale close is
carried. NAV is 157,646.0394 on 2025-01-09, unknown on 2025-01-10, 154,002.0494
on 2025-01-13 (the underlying resumed lower), unknown on 2025-01-24 and
155,603.3472 on 2025-01-27. The Discovery baseline therefore cannot claim
complete coverage; the two gaps are declared rather than filled.

### Relation to the research sanity check (≈176,741.79)

The research note's §9.5 probe figure is a **full-window**
(2022-05-20..2026-11-20) computation, not the sealed Discovery result. The
sealed Discovery baseline stops at 2025-01-27 by construction (it reads only
the Discovery partition), which is why its ending NAV is 155,603.35.

The full-window figure was verified from the acquisition input separately in
the reconciliation script:

| Full-window model | Ending NAV | Fee treatment |
| --- | --- | --- |
| Sealed-engine accounting | 176,691.81681066097 | fee debited from cash |
| Research probe accounting | 176,741.7918231547 | fee left in cash (not debited) |
| Research note | ≈176,741.79 | — |

The probe model matches the note to 0.0018 CNY (rounding); the engine model is
lower by exactly the base-case fee, 49.9750124937 CNY, because the engine
debits it. The inputs therefore reproduce the research check, and the sealed
result's lower figure is the documented fee accounting plus the Discovery
cut-off — not a data discrepancy.

## Machine cross-check

[`scripts/reconcile_real_etf_baseline.py`](../../scripts/reconcile_real_etf_baseline.py)
is a standard-library-only second implementation that does not import
`snapshot_baseline`. It verified the snapshot ID and Discovery hash, recomputed
the derived series, the 2022-05-23 buy, fees, receivables, NAV path and gaps,
and compared them with both sealed baseline JSONs. All checks pass:
3,280/3,280 Discovery derived values (0 mismatches) and 5,470/5,470 full-window
derived values; 5/5 fills with exact dates and symbols; base-case fees, dividend
cash, ending NAV and gap sets exact; the high-cost scenario's largest NAV delta
is 5.82e-11 CNY. Details and pre-registered tolerances are in
[`real-snapshot-reconciliation.md`](real-snapshot-reconciliation.md).

This is a machine cross-check on the same host, by the same author, from the
same source files. It is **not** the independent human researcher that #24
requires.

## Evidence summary

211 evidence items, 211 distinct SHA-256 values, 211 copied files, ~45 MB.
Per-kind aggregate digest = SHA-256 of the canonical JSON list of
`{"id", "sha256"}` sorted by `id`:

| Kind | Count | Aggregate SHA-256 |
| --- | --- | --- |
| `halt` | 164 (163 in-window halt notices + 1 resume notice) | `a5599ad454966dc33956862fa787c6e3432537e4fb768ed0bb6c1ad0519c6ae7` |
| `dividend` | 20 | `e959605254f61c94a6743a0599158256bd82e7621aff666cecf0cbb5b8321bd3` |
| `price_archive` | 8 (7 TDX zips + Tencent unadjusted bars) | `39b7f04a55ab07e6d8b1d58d4cafe880dee81a68fa8c2e0c95781be6d51ff898` |
| `calendar` | 6 (5 SSE notices + SZSE month bundle) | `7b381ce4766dac6a249062f70336a28a58e544e2840022cb55e6c53ee0c48a27` |
| `listing` | 5 | `7d5bd97f60e97a4ca5a3ab0e48afa3727a950764888224a7b07e1003054ae24f` |
| `conversion` | 4 | `6e4620068638f5325065bdc3fc33a8b8eefcbe67c06564e0d010e02c6be675f8` |
| `vendor_cross_check` | 1 (`qfq`, recorded as not used) | `f4c8f20776dc4e6af780362fc08ed6e3c4b955874fb10a3aff4e88411a3e13a7` |
| `announcement_index` | 1 | `feba5d4bcb82edd8be98d524dffc2c91e64f78fdcc428a7252ecf17ae8a987c8` |
| `dividend_index` | 1 | `d2f39cadc91cfa45f560afd6ec8e161bc9e8f8b5f4d3e42a4192eeabde828603` |
| `halt_index` | 1 | `e71848882e77d6b690b75e9b3479aaa21f63b3a48e328f7dc2faa3474e2d008e` |

Selected primary-source hashes (full):

| Evidence id | SHA-256 | `available_at` |
| --- | --- | --- |
| `calendar-sse-2022` | `e79c13dcb309e01a3615d0dd96f34ef9bc1bb520ffb368f06c429dfb2d6d61b8` | 2021-12-20 23:59:59 +08:00 |
| `calendar-sse-2023` | `3bc21d9ff7e8b359afe3e9336138bed20aa5f9f14517c04bbfa125416333d785` | 2022-12-27 23:59:59 +08:00 |
| `calendar-sse-2024` | `eeb592d56b0e87f1cc7c5493768c5bb029bc83d4d7e7f755ca3c90fc84634ea8` | 2023-12-26 23:59:59 +08:00 |
| `calendar-sse-2025` | `9faf2f456f5276de263e4234abb42e4e2e1e7665a951fcd885dd87d80ef191e4` | 2024-12-23 23:59:59 +08:00 |
| `calendar-sse-2026` | `6414ab8f4316c318d816b68aefc4843a5ea87ec75d543f614e3e53aa7688f33c` | 2025-12-22 23:59:59 +08:00 |
| `calendar-szse-months` | `b5c27d0cae442c85cf6680860a4fa77811ba9b7a78cc39e723787e475611b9b9` | 2026-11-21 22:56:22 +08:00 |
| `listing-511260` | `1e792cc79fcf1992e7b431f618620d9cde911f834f2495d511dcf01c120d1147` | 2017-08-21 23:59:59 +08:00 |
| `listing-518880` | `53eee78fcd2b67993c79eed9c9df5dd10c86d2583d7343e7fcf77d4bb7325205` | 2013-07-24 23:59:59 +08:00 |
| `listing-515080` | `86a43829886f3c7b5c1a5cb9d42c6f0a02daa403d51c79ab0f3cf112b66d4144` | 2019-12-24 23:59:59 +08:00 |
| `listing-513300` | `a0b3f87ead897c2aec202e7239088faf0f6382364a75852f74127838d906a8ab` | 2020-11-02 23:59:59 +08:00 |
| `listing-159612` | `23c4871b1babfd9a5318cea84f51cdfc5619d0cdddc8167abb18984f0bbd8382` | 2022-05-17 23:59:59 +08:00 |
| `halt-159612-2025-01-10` | `5569577d51e7cb993067aa05e3946d4096f26549d537bf2fed297670810be842` | 2025-01-10 23:59:59 +08:00 |
| `halt-159612-2025-01-13-resume` | `07981133b284ec66bf44276b3f03a6bb5302366166d8803c8925629d52abf8de` | 2025-01-13 23:59:59 +08:00 |
| `halt-159612-2025-01-24` | `0c2303dd1c0b7abb942090b1203bfb982b74bbb0a3ed48a8bc65fe545fdfcb33` | 2025-01-24 23:59:59 +08:00 |
| `halt-513300-2026-11-20` | `4daa4bc77532ad9233056f9b78d754e45a083fbcff7993eba2f15314defe3859` | 2026-11-20 23:59:59 +08:00 |
| `price-tencent-unadjusted` | `0e347a243206cbfdce757e1f4f7776c8c18c4e75a9329b932bb56f94ce83f6d4` | 2026-11-21 22:57:37 +08:00 |
| `price-tdx-20220520` | `3457e29b57c52b99ac7ab8e21f194ab79cbb6c437c41d5746af2c80f8be4dcd7` | 2022-05-20 15:38:00 +08:00 |
| `price-tdx-20261120` | `2329d86a99cb74a6bd4c30a6314c39096fe3cd559397dcaffaf0de8f88804a6d` | 2026-11-20 15:38:00 +08:00 |

The dividend PDFs include `196b4161ac1f27f88b5a7281527715c01894e0e379110677bf346a3842c08956`
(`dividend-511260-2025-09-18`) and
`255e9990752ec6e2d3452a6296e87b316e6bff0665f8e24ab43f208972341c9f`
(`dividend-515080-2026-09-11`); the per-kind aggregate above covers all 20.
Inception conversions: `conversion-511260-2017-07-28`
`a322e469f3364526fbe98a94e97df4c789027e54c30e882b93ce0ed65878133e`,
`conversion-511260-2017-08-08`
`c4d8031fabdc73309567f04d6513816e2c024835e9bbd3a7ea550795e2c78505`,
`conversion-518880-2013-07-24`
`fcc5a323031547aaffdc612bb69045688c6e9dbe4c77b6adeb9cd66adfaaffa2`,
`conversion-518880-2013-07-26`
`58295d4576b17ff847e9fa377d3a3f439d574f5662e0e805ecad584cb5399ba0`.

Evidence `available_at` values are the dated publication instruments (SSE
notice dates, announcement dates, TDX session build dates). The three
`index-*` files, `calendar-szse-months`,
`vendor_cross_check`/`price-tencent-unadjusted` items carry their retrieval
timestamps because they are current-view transcriptions, not dated
publications.

## Reproducibility and verification

- Rebuilding `real.json` from the same cache is byte-identical; re-sealing is
  idempotent and returns the same snapshot ID.
- `snapshot manifest` reports `data_kind: "real"`, 1,094 sessions, partitions
  656/218/220, the common range 2022-05-20..2026-11-20 and the gap counts
  above.
- `snapshot read --partition discovery` returns the partition with its content
  hash; `snapshot read --partition validation` is refused with
  `Research access is limited to Discovery.` (process-level, not OS-level).
- `ruff check .` passes.
- The fast pytest suite passes (`163 passed, 118 deselected in 198.85s`; the
  `isolation`-marked tests need a host sandbox and were deselected). The sandbox
  needs pre-installed dependencies; `docs/agents/testing.md` documents that
  pip/venv creation is unreliable inside this sandbox, which is an environment
  limitation, not a change to the package.
- `--refetch` was smoke-tested end-to-end: it re-downloaded the Tencent bars,
  the 59 SZSE months and the announcement PDFs and rebuilt 1,094 sessions,
  5,470 bars, 5,470 derived rows and 211 evidence items (with `collected_at`
  values from the refetch time, so it was not sealed).

## Acceptance scope

The signed review covers the 656 Discovery sessions. The two missing NAVs
remain null. The normal 09:30/15:00 availability times are exchange-rule
assumptions subject to documented halts. The vendor reconstructed the daily
history, and dated price packages cover seven sessions.

## What is committed and what is not

Committed: the acquisition script, the reconciliation script, the two Discovery
baseline JSONs, the reconciliation JSON and this note. Not committed: `real.json`
(5.4 MB input), the sealed store, the 211 evidence files (~45 MB), the probe
cache, or any raw market data. The baseline JSONs are the only derived market
data outputs committed, as allowed by the ticket.
