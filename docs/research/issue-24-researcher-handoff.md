# Issue #24 independent Discovery reconciliation

The project owner designated an independent Agent to perform the calculation
and retained the human review. The technical results below are ready for that review.

## Inputs for the researcher

| Item | Pinned value |
| --- | --- |
| Baseline reference commit | `1379d135e2a73f370059468a0afbcb4fd489958e` |
| Sealed snapshot ID | `b07823329f95b2f3d6a8d7e5fb7660941eda25150dc2b7a3ae8086357e02d41d` |
| Discovery content SHA-256 | `1fb6d1db77c25c35ded5ea5e82655b81e3581878ee3d74526bdc57d324e75f54` |
| Sessions | 656, from 2022-05-20 through 2025-01-27 |
| Starting cash | CNY 100,000 in each run |
| Fees and slippage | Base 5 / 0 bps. High cost 20 / 10 bps. |

The trusted operator verifies the canonical manifest against the snapshot ID and supplies the sealed `discovery.json` alone through the approved research environment. Verify its content hash using SHA-256 over UTF-8 JSON serialized with sorted keys, compact separators and unescaped Unicode. Keep the source store, full-window evidence and acquisition input on the trusted side.

Recompute the total-return series from unadjusted closes and official ex-date dividends. For the baseline, signal equal weights after the first session and attempt the basket once at the following session's 09:30 open, accounting for each run's costs. If any constituent lacks an executable open then, record the entire attempt as unfilled. Accrue dividend receivables on the ex-date and move them to cash on the pay date. Value held shares at confirmed closes and record unknown NAV when a held close is missing. Retain your dated output and method so someone else can reproduce the comparison.

After computing both runs, compare against the Discovery references at the pinned commit. The hashes below cover the files as stored.

| Reference | File SHA-256 |
| --- | --- |
| [Base case](real-discovery-baseline.json) | `41de2205c10bcc9657c1e0c01476cdf447dc04633c504bf285f490a3cf48699d` |
| [High cost](real-discovery-baseline-high-cost.json) | `c28aead84ea6b0406accaa494d353c9fa0c8e6279e5a13120be7e4a40e20b5fd` |

## Independent Agent comparison, 2026-11-22

The author of the independent implementation received the field definitions,
calculation rules and approved Discovery fingerprint. Reference outputs and the
other implementation were withheld. The submitted program used 50-digit
decimal arithmetic, reconstructed each symbol's return index, and determined
dividend entitlements from record-date positions. Its SHA-256 is
`e878e73a8c0cc7c8750df3b0768247c70464e555d70f9e2148871d2ea914d1d3`.
The operator ran it in the isolated container and compared its output with
the two pinned references after exit. Numeric differences below are rounded up.

| Comparison | Pre-registered tolerance | Base case | High cost |
| --- | --- | --- | --- |
| Derived values and missing values | 1e-6 relative, matching missing values | 3,280 compared, max 4.02e-15 | same input |
| Fill count, dates and symbols | Exact | All five on 2022-05-23 | All five on 2022-05-23 |
| Fill quantity, price and notional | 1e-9 relative | ≤ 2.69e-16 | ≤ 2.52e-16 |
| Fee and slippage per fill | 1e-6 CNY absolute | ≤ 1.32e-15 | ≤ 3.06e-12 |
| Dividend cash and daily cash balances | 1e-6 CNY absolute | ≤ 7.59e-13 | ≤ 9.09e-13 |
| Daily NAV over all 656 sessions and ending NAV | 1e-4 relative or 1e-4 CNY absolute | ≤ 6.48e-11 CNY | ≤ 6.00e-11 CNY |
| Unknown-NAV dates | Exact set | 2025-01-10 and 2025-01-24 | same dates |

| Recomputed amount (CNY) | Base case | High cost |
| --- | ---: | ---: |
| Ending NAV | 155,603.347173 | 155,215.192838 |
| Fees | 49.975012 | 199.600798 |
| Slippage | 0.000000 | 99.700699 |
| Cash from eight distributions | 2,803.647265 | 2,796.653534 |

The trusted-side bundle `~/PrajnaTrade-trusted/issue-24-independent-acf54e5b`
holds the independent source, per-session outputs, container inspection,
comparison code and replay command. Receipt SHA-256:
`4cde8098f699c0665e6fb17ac9dddaa82c987f28813cdd57784de5059acc574e`.
The trusted-side comparison checked seven inaccessible paths, the single
read-only Discovery mount, exit code zero, UID 65534, both reference
fingerprints and the pre-registered tolerances.

**Human review:** the project owner requested this independent calculation
and will review the results. The human conclusion and attribution are pending.

## Operator and owner decisions

- **OS isolation verified for the #24 calculation on 2026-11-22.** The trusted operator checked the manifest and Discovery hashes and confirmed that the protected files existed on the host. Colima ran the calculation offline as UID/GID 65534 with a read-only root, dropped capabilities, no privilege escalation and one read-only bind of `discovery.json`. The approved read succeeded. All seven checks for direct source access and protected siblings returned `ENOENT` inside the container. The operator inspected its actual mounts and exit status and replayed the trusted-side comparison. The private evidence bundle is `~/PrajnaTrade-trusted/issue-24-review-c56e103b`, receipt SHA-256 `237bc2f86f6c56a61a0f68cd8871ed1ad9d8c94269655a236a9e1d9cf289a0f1`. This receipt covers the #24 research process. [Issue #12](https://github.com/ZombieIce/PrajnaTrade/issues/12)
- **Price timing — owner decision, 2026-11-22, #24 Discovery.** Use 09:30 and 15:00 Asia/Shanghai as the default open and close on a scheduled trading day. A documented delayed open or full suspension overrides the default for the affected instrument. A delayed opening print cannot fill the 09:30 order. On a full suspension there is no fill, and a missing confirmed close for a holding leaves portfolio NAV unknown. The times are modeling assumptions. The daily prices are a vendor reconstruction with seven dated archive samples.
