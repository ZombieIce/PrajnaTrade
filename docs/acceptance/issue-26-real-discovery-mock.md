# Issue #26 real Discovery Mock acceptance

Verified on 2026-11-22. Both runs used the #24 approved historical Discovery
partition, covering 656 sessions from 2022-05-20 through 2025-01-27.
The provider was `deterministic-mock-v1` with seed `1`. Each campaign started
with CNY 100,000, a fee of 5 bps, no slippage, 3 attempts and 90 seconds of
execution budget.

- Sealed snapshot ID `b07823329f95b2f3d6a8d7e5fb7660941eda25150dc2b7a3ae8086357e02d41d`
- Discovery content hash `1fb6d1db77c25c35ded5ea5e82655b81e3581878ee3d74526bdc57d324e75f54`
- Evaluation fingerprint `7dfdc60cbc4ec347430c0fdfd8c64dee889efcba4e48b9fd48f73f0b689c2a70`

## Experiments

Continuous campaign `ca5917e8-789f-4b1d-ab2f-ff083d2193b9`.
Interrupted campaign `77dc06ab-2335-4afd-a883-59581290cc92`.
Each row after the first names the preceding experiment as parent within its
own campaign. The trace retains each submission, the receipt, the Mock
hypothesis and rationale, and the subsequent decision.

| Submission | Hypothesis and settings | Continuous ID | Interrupted run ID | Final NAV (CNY) | Warm-up |
| --- | --- | --- | --- | ---: | ---: |
| mock-01 | The strongest ETF by 3-session momentum can beat the benchmark, with cash when non-positive | `d233d075-2711-44d1-ba4f-8e705f21dcf8` | `ee799c6a-8d1e-44f1-8519-2277da5bb7fd` | 105,482.59 | 4 |
| mock-02 | Equal weight above the 4-session average can reduce drawdown | `477bbade-0ef3-4c09-8181-f083515c8b0d` | `8b571685-2975-4f34-b38c-3fd8e2f526aa` | 106,854.08 | 4 |
| mock-03 | Inverse 4-session volatility weights can improve risk-adjusted return | `9931e8e7-7919-4c76-b1c4-573b1ff0ff21` | `d40b48a4-577d-4da8-838a-4f5caa19bf65` | 124,833.66 | 5 |

The Mock returned `branch` after each result because it saw a new family.
The runner stopped at the fixed attempt limit. The equal-weight comparator
ended at CNY 155,603.35. Its daily NAV matched the accepted #24 baseline
within CNY 5.83e-11, with both halt-related unknown days preserved.

The comparison used the submission key to align the separate campaigns. It
compared proposals, rationale, parent links by submission key, decisions,
diagnostics, fingerprints, fills, orders, valuations and all other Discovery
result fields exactly. Campaign and experiment IDs and measured run times are
recorded independently. The matched content has SHA-256
`74fca7201a6c3f52b7cc5c1d175a4d0f3dee925392144024595094d3cce8d6e9`.

## Interruption and budget

The controller killed the second experiment with SIGKILL after SQLite showed
its committed running row. A fresh client retrieved `EXECUTION_INTERRUPTED`
with `next_action: resume`, replayed the failed receipt without another charge
and confirmed the successful parent remained available. Resume completed the
same experiment ID. The remaining attempt count stayed at 1 across failure and
resume. Remaining execution time went from 88.595 to 87.849 seconds.

Both final ledgers had three successful rows and zero attempts remaining.
Measured execution charges reconciled to SQLite at 2.590 seconds for the
continuous run and 3.309 seconds for the interrupted run. Repeating completed
submissions and resume returned unchanged receipts. Both campaigns refused
a fourth submission with `BUDGET_EXHAUSTED`; the receipt and ledger budget
remained unchanged.

## Isolation and evidence

Colima used the cached Linux ARM64 Python image
`sha256:dbbe4ceb97851e2e5fa83798b239811f871cb743b259ba3563737349f6bcfaa0`
with pinned NumPy 2.2.6, pandas 2.2.3 and PyArrow 20.0.0 in both phases.
The trusted setup container could see the manifest and Discovery payload.
The research container received the initialized projects, read-only source
and read-only dependencies. Both phases ran offline under UID/GID 65534 with
a read-only root and all capabilities dropped. The trusted operator inspected
actual mounts and successful exits. In the research container, source-store,
Validation, Final Holdout, setup Discovery path and Docker socket probes all
returned `ENOENT`. The project bundles contained the approved Discovery copy.

The trusted bundle `~/PrajnaTrade-trusted/issue-26-mock-74fca720` holds the
full traces, SQLite ledgers, JSON summary, environment hashes and container
inspection receipts. The summary SHA-256 is
`bd13bdc8738e148d9c1cdedac0cbcd70d2bef2353e7aab61d6368d134b500120`.

To replay, use [the acceptance script](../../scripts/acceptance_real_discovery_mock.py)
with a fresh output directory inside both containers. The trusted phase runs
`python /mock.py /projects setup --store /snapshot --snapshot-id <approved-snapshot-id>`
with only the two approved files under that snapshot ID. The offline research
phase omits that mount and runs `python /mock.py /projects verify --forbid <path>`.
Repeat `--forbid` for each protected host path and the setup-only Discovery
path. Mount the same Python dependencies and source in both phases so runtime
fingerprints match.

The evidence covers historical real data with a fixed declarative Mock.
Real model autonomy, protected evaluation and forward execution belong to
later acceptance scenarios.
