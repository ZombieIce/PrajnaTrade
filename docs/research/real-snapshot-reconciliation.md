# Real snapshot reconciliation: machine cross-check (MVP-06 / #24)

**This is a machine cross-check, not the independent human reconciliation #24
requires.** It is a second implementation on the same host; it does not replace
a named second researcher, and it reads only the sealed Discovery partition.

## Inputs

| Item | Value |
| --- | --- |
| Snapshot ID | `b07823329f95b2f3d6a8d7e5fb7660941eda25150dc2b7a3ae8086357e02d41d` |
| Store | `~/PrajnaTrade-trusted/real-etf-snapshot/store` |
| Discovery partition | 656 sessions, 2022-05-20..2025-01-27, hash `1fb6d1db77c25c35ded5ea5e82655b81e3581878ee3d74526bdc57d324e75f54` |
| Baseline (base case) | [`real-discovery-baseline.json`](real-discovery-baseline.json) — 100,000 cash, 5 bps, 0 slippage |
| Baseline (high cost) | [`real-discovery-baseline-high-cost.json`](real-discovery-baseline-high-cost.json) — 100,000 cash, 20 bps, 10 bps slippage |
| Script | [`scripts/reconcile_real_etf_baseline.py`](../../scripts/reconcile_real_etf_baseline.py) — standard library only; does **not** import `prajnatrade.snapshot_baseline` (or any package module) |
| Machine output | [`real-snapshot-reconciliation.json`](real-snapshot-reconciliation.json) |

The script verifies that the snapshot ID is the SHA-256 of the canonical
manifest and that the Discovery payload hash equals the manifest partition
hash before recomputing anything.

## Method

1. Re-implement `tr-reinvest-exclose-v1` from the sealed raw closes and the
   official cash-dividend rows and compare all 3,280 Discovery derived values.
2. Re-derive the single equal-weight buy at the 2022-05-23 open from the sealed
   `raw_bars.open` (must be `trading`, have a valid open/volume, and have
   `open_available_at <= 09:30 +08:00`). The buy is computed from a per-symbol
   cash budget — algebraically the same economics as the engine but a different
   order of operations.
3. Walk the 656 Discovery sessions with frozen costs: cash dividends accrue as
   receivables on the ex-date for held shares, transfer to cash on the declared
   pay date, and NAV is unadjusted close marks plus cash plus receivables. A
   held row with no confirmed close (the two `159612` full halts) makes that
   day's NAV unknown instead of using a stale price.
4. Compare fills, fees, dividend cash, the full NAV path, NAV-gap sets and the
   ending NAV with the sealed baseline JSON.

The full-window block (`--input` on the pre-seal `real.json`) recomputes the
same buy over all 1,094 sessions. It exists only to reconcile the research
note's ≈176,741.79 sanity figure; it is **not** a Discovery result and it does
**not** read the sealed Validation or Final Holdout partitions.

### Pre-registered tolerances

These were fixed in the script before the comparison ran:

| Quantity | Tolerance |
| --- | --- |
| Derived value | 1e-6 relative |
| NAV path / ending NAV | 1e-4 relative, or 1e-4 CNY absolute for a tiny NAV |
| Trade count, fill dates and symbols | exact |
| Fill quantity / price / notional | 1e-9 relative |
| Fill fee / slippage | 1e-6 CNY absolute |
| Dividend cash | 1e-6 CNY absolute |
| NAV-gap sets | exact |

## Results

Both scenarios pass every check.

| Check | Base case (5 bps, 0 slippage) | High cost (20 bps, 10 bps slippage) |
| --- | --- | --- |
| Sessions | 656 | 656 |
| Valued sessions | 654 | 654 |
| Unknown-NAV days | `2025-01-10`, `2025-01-24` | `2025-01-10`, `2025-01-24` |
| Fill date | 2022-05-23 | 2022-05-23 |
| Fills | 5 | 5 |
| Fees paid | 49.97501249375313 | 199.60079840319364 |
| Slippage paid | 0.0 | 99.70069850308315 |
| Dividend cash | 2,803.6472647266837 | 2,796.6535344486324 |
| Ending NAV | 155,603.34717297764 | 155,215.19283766544 |
| Fill delta (max relative) | 0.0 | 1.30e-16 |
| NAV delta (max absolute) | 0.0 | 5.82e-11 |
| NAV delta (max relative) | 0.0 | 4.25e-16 |
| Fees delta | 0.0 | 0.0 |
| Dividend delta | 0.0 | 4.55e-13 |
| Ending-NAV delta | 0.0 | 0.0 |
| Gap sets | exact | exact |

Derived-series check: 3,280/3,280 Discovery rows match, 0 mismatches, maximum
relative delta 0.0 (tolerance 1e-6).

Fill details are identical to the sealed baseline, including the five raw opens
`511260 117.512`, `518880 3.875`, `515080 1.426`, `513300 1.022`,
`159612 0.977`, and 9.995002498750626 CNY of fee per symbol in the base case.

The recomputed dividend cash is compared with the baseline's final cash: after
the single buy there are no further trades, so final Discovery cash is exactly
the dividends received (eight `515080` distributions, 2022-06-27..2024-12-02).

## Full-window reconciliation of the research sanity check

The research note's §9.5 probe produced **≈176,741.79**. Recomputing the same
five-ETF equal-weight buy over 2022-05-20..2026-11-20 from the acquisition input
confirms the input reconstruction and explains the difference exactly:

| Model | Ending NAV | Fees |
| --- | --- | --- |
| Sealed-engine accounting (fee debited from cash) | 176,691.81681066097 | 49.97501249375313 |
| Research probe accounting (fee left in cash) | 176,741.7918231547 | 49.97501249375313 (not debited) |
| Research note reference | ≈176,741.79 | — |

- The research-probe model matches the note to **0.001823 CNY** (the note
  quotes a rounded figure; tolerance 0.01 CNY).
- `engine − probe = −49.97501249372726`, which equals the base-case fee
  `49.97501249375313` to 2.6e-11 CNY. The only difference is that the probe
  script did not debit its fee from cash; both models hold identical shares.
- The full-window derived series also matches: 5,470/5,470 rows, 0 mismatches.
- The full window has the same two unknown-NAV days (`2025-01-10`,
  `2025-01-24`).

So the research sanity figure is reproduced, and the sealed Discovery result is
lower than the full-window figure because (a) the Discovery baseline stops at
2025-01-27 and (b) the engine debits the fee.

## Limits

- Same author, same host, same language and the same data files: an independent
  implementation, **not** an independent researcher or an independent data
  capture. #24's human reconciliation is still outstanding.
- The check verifies the sealed numbers against the same raw closes and action
  rows; it does not re-download the evidence PDFs or re-derive the dividend
  terms from them. Evidence integrity is recorded as SHA-256 in the manifest.
- The full-window block uses the pre-seal input, not the sealed protected
  partitions, and must not be quoted as a Discovery or protected-partition
  result.
- The 09:30 open / 15:00 close timestamps remain market-rule assertions, not
  archived point-in-time quotes; see [`real-snapshot-result.md`](real-snapshot-result.md).
