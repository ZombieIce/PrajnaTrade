# PrajnaTrade issue #17 — six-scenario acceptance report

**Verdict: NOT PASSED — blocked**

End-to-end MVP is NOT passed: the six-scenario rule was not met (2 of 6 required scenarios verified); 4 scenario(s) are blocked: real_data_mock, controlled_real_llm, independent_validation_reports, immutable_release_forward.

Manifest SHA-256: `a66e499fd9fc5cab97c37f572c9f80cd2290342e0ba6e49a3a91b372db649030`. This report is rendered only from the acceptance manifest. It does not read a trusted store, re-run an evaluation, open a sealed partition or send an order. Verified means verified for the recorded scope; synthetic evidence never satisfies the real-chain gate.

## Delivery

| Field | Value |
| --- | --- |
| version | 0.1.0 (issue #17 synthetic workflow + #24 real snapshot/baseline) |
| branch | zombieice-issue-17-implementation |
| tested_at | 2026-11-22T00:45:26+08:00 |
| environment | macOS 26.2 (arm64), Python 3.12.15, pytest 9.2.0; tests run offline on synthetic fixtures with the macOS sandbox boundary; the #24 real snapshot was acquired from researched vendor/official sources and sealed outside Git. The later #24 reconciliation used a verified Discovery-only Colima container. |
| test_command | PYTHONPATH=src python -m pytest -q |
| test_result | 281 passed in 657.54s (0:10:57), exit 0 |

## Scenario summary

| # | Scenario | Status | Data | Agent | Period |
| --- | --- | --- | --- | --- | --- |
| 1 | `tests_and_cli` — Deterministic tests and public CLI | verified | synthetic | mock | historical |
| 2 | `real_snapshot_baseline` — Real five-ETF snapshot and equal-weight baseline | verified | real | mock | historical |
| 3 | `real_data_mock` — Real-data bounded Mock campaign | blocked | synthetic | mock | historical |
| 4 | `controlled_real_llm` — Controlled real external LLM iteration | blocked | synthetic | mock | historical |
| 5 | `independent_validation_reports` — Independent protected validation and controlled reports | blocked | synthetic | mock | historical |
| 6 | `immutable_release_forward` — Immutable releases and multi-strategy Shadow Live | blocked | synthetic | mock | historical |

Status counts: verified=2, blocked=4, failed=0, not_run=0.

## Scenario details

### 1. Deterministic tests and public CLI (`tests_and_cli`) — VERIFIED

- **Scope**: data=synthetic, agent=mock, period=historical
- **Expected**: The same delivery version runs the deterministic test suite and the public CLI workflows in a clean environment, with the operator/research split enforced and synthetic fixtures covering success, invalid input, replay, budget refusal, failure evidence and interrupted-resume paths.
- **Observed**: The recorded full-suite command passes in this delivery. The operator-only acceptance report validates the strict manifest and renders deterministic Markdown/JSON; prajnatrade-agent rejects the acceptance command. Synthetic scope only: this proves the synthetic test/CLI path, not real market data, real model access, deployed OS isolation, protected real evaluations or real forward tracking.
- **Evidence**:
  - `test_suite` — `PYTHONPATH=src python -m pytest -q`: Full deterministic suite, exit 0; machine result is recorded in delivery.test_result. Synthetic fixtures only, no network and no broker.
  - `test_file` — `tests/test_acceptance_cli.py`: Public-CLI tests for strict manifest validation, stable rejection codes, verdict rules, deterministic rendering, no-store report generation and rejection of the operator command by prajnatrade-agent.
  - `test_file` — `tests/test_experiment_cli.py, tests/test_research_cli.py, tests/test_protected_eval_cli.py, tests/test_holdout_report_cli.py, tests/test_release_cli.py, tests/test_shadow_cli.py, tests/test_compare_cli.py`: Public-CLI workflow tests for the experiment protocol, protected evaluation, release lifecycle, Shadow Live and comparison, all subprocess-based on synthetic fixtures.
- **Blockers**: none

### 2. Real five-ETF snapshot and equal-weight baseline (`real_snapshot_baseline`) — VERIFIED

- **Scope**: data=real, agent=mock, period=historical
- **Expected**: A shared real snapshot for 511260, 518880, 515080, 513300 and 159612 with official exchange calendar, verified corporate actions and price basis, point-in-time availability evidence, frozen 60/20/20 partitions and a Discovery equal-weight baseline that a second researcher can reproduce from the same approved inputs.
- **Observed**: A real snapshot was sealed and baselined outside Git: snapshot ID b07823329f95b2f3d6a8d7e5fb7660941eda25150dc2b7a3ae8086357e02d41d, schema 2, data_kind real, 1,094 sessions 2022-05-20..2026-11-20, partitions 656 Discovery / 218 Validation / 220 Final Holdout, 5,470 raw bars, 5,470 derived values, 20 official cash dividends and 211 hashed evidence items (official calendars, listings, dividends, halts, price archive samples). The Discovery equal-weight baseline (5 bps / 0 slippage, then 20 bps / 10 bps) filled all five ETFs on 2022-05-23 and ended at 155,603.3472 / 155,215.1928 with two declared unknown-NAV days (159612 full halts on 2025-01-10 and 2025-01-24). An independent Agent recomputed all 656 Discovery sessions in a read-only Colima container. @ZombieIce accepted both cost configurations within tolerance and both unknown NAV dates on 2026-11-22. Regular session times follow the agreed 09:30/15:00 assumption subject to actual halts. The daily prices are a vendor reconstruction with seven dated archive samples.
- **Evidence**:
  - `real_snapshot` — `docs/research/real-snapshot-result.md (snapshot b07823329f95b2f3d6a8d7e5fb7660941eda25150dc2b7a3ae8086357e02d41d)`: Sealed schema-2 real snapshot outside Git: 1,094 sessions, 656/218/220 partitions, 5,470 raw bars, 5,470 derived values, 20 official dividends, 211 SHA-256-verified evidence items. Official listing/calendar/dividend/halt sources; prices are a current vendor reconstruction.
  - `real_baseline` — `docs/research/real-discovery-baseline.json; docs/research/real-discovery-baseline-high-cost.json`: Discovery-only equal-weight baseline at 5 bps / 0 and 20 bps / 10 bps: five fills on 2022-05-23, ending NAV 155,603.3472 / 155,215.1928, two declared unknown-NAV days from 159612 full halts, no deferred fills and no stale marks.
  - `machine_cross_check` — `docs/research/real-snapshot-reconciliation.md; docs/research/real-snapshot-reconciliation.json`: Independent standard-library recomputation from the sealed snapshot: derived values, fills, fees, dividend cash, every NAV point and the gap sets match exactly within pre-registered tolerances. This is a machine cross-check, not the independent human researcher #24 requires.
  - `official_source_evidence` — `docs/research/real-etf-data-sources.md; docs/research/real-snapshot-result.md`: Official five listing announcements, five SSE annual notices plus 59 SZSE month calendar responses, 20 dividend PDFs, 118+2 159612 and 43 513300 halt notices, and seven sampled TDX dated packages; all hashed in the snapshot evidence block. Vendor qfq/hfq was not used as the total-return series.
  - `discovery_isolation` — `docs/research/issue-24-researcher-handoff.md`: Trusted operator verified the single read-only Discovery mount and seven inaccessible source or protected paths in the isolated calculation.
  - `human_reconciliation` — `https://github.com/ZombieIce/PrajnaTrade/issues/24#issuecomment-7074089930`: Independent Agent computed the baseline using the approved Discovery input. @ZombieIce accepted the two cost scenarios and the 2025-01-10 and 2025-01-24 halt gaps. Comparison receipt SHA-256 4cde8098f699c0665e6fb17ac9dddaa82c987f28813cdd57784de5059acc574e.
- **Blockers**: none

### 3. Real-data bounded Mock campaign (`real_data_mock`) — BLOCKED

- **Scope**: data=synthetic, agent=mock, period=historical
- **Expected**: A bounded fixed-Mock, fixed-config and fixed-seed campaign on the real Discovery partition with hypotheses, parent/child experiment IDs, receipt-driven iterations, deterministic diagnostics, failure and budget records, and an interrupted-then-resumed run that matches the uninterrupted run without duplicate attempts or lost records.
- **Observed**: The real Discovery snapshot from #24 exists (snapshot b07823329f95b2f3d6a8d7e5fb7660941eda25150dc2b7a3ae8086357e02d41d), but the experiment/campaign protocol still rejects non-synthetic sealed snapshots and the bounded fixed-Mock campaign has only run on synthetic fixtures. No real-data parent/child trace, interruption/resume comparison or real budget reconciliation exists.
- **Evidence**:
  - `synthetic_mechanism` — `tests/test_experiment_cli.py, tests/test_research_cli.py`: Synthetic mechanism coverage only: Mock iterations, idempotent submission, resume, failure evidence and budget accounting. This proves the protocol, not real Discovery execution.
- **Blockers**:
  - Missing: Experiment/campaign support for the real sealed Discovery snapshot (`campaign create` currently requires a synthetic snapshot), and a bounded fixed-Mock/fixed-seed campaign on that real Discovery partition.
    Unblock: Extend the campaign/evaluation path to accept real sealed snapshots without weakening the sealed/Discovery-only rules, then run the bounded fixed-Mock campaign on the #24 snapshot.
  - Missing: A real parent/child experiment trace and an interrupted-then-resumed comparison on real Discovery, including failure and budget records.
    Unblock: Execute the bounded Mock campaign, interrupt and resume one experiment, and record the deterministic diagnostics, IDs, budget ledger and parent/child links.

### 4. Controlled real external LLM iteration (`controlled_real_llm`) — BLOCKED

- **Scope**: data=synthetic, agent=mock, period=historical
- **Expected**: One complete autonomous iteration by an authorized real external LLM on the real Discovery campaign under verified deployment isolation: a falsifiable first hypothesis and strategy, a real receipt read, a self-justified successor experiment, at least one executed model-generated or model-modified Python strategy, and attributable model request/session and usage references.
- **Observed**: Only mock/stub providers, synthetic isolation probes and the disabled trusted-local path exist. The #24 real Discovery snapshot is available, but no authorized real model access, real Discovery campaign or verified deployed OS isolation has been exercised, and no model-generated Python strategy has run.
- **Evidence**:
  - `synthetic_mechanism` — `tests/test_isolation.py, tests/test_openai_provider.py`: Synthetic isolation and provider-contract tests only: allow/deny/resource-abort mechanics and usage accounting against stubs. No real model was called and no deployment isolation was verified.
- **Blockers**:
  - Missing: Authorized real model access (endpoint, credentials and attributable usage records) and a real Discovery campaign whose receipts the model can read; the delivery has neither.
    Unblock: Provide authorized real model access and the real Discovery campaign from issues #24/#26, then retain attributable model request/session references, the two linked submissions and separately accounted platform versus model usage (issue #27).
  - Missing: Verified OS-level deployment isolation for the actual deployment, with allowed, expected-rejection and resource-abort evidence and trusted-side receipts; synthetic probes and trusted-local execution do not count.
    Unblock: Deploy the worker under the verified OS-isolated profile and record allowed, expected-rejection and resource-abort evidence before executing model-generated or model-modified Python in a real campaign (issues #12/#27).

### 5. Independent protected validation and controlled reports (`independent_validation_reports`) — BLOCKED

- **Scope**: data=synthetic, agent=mock, period=historical
- **Expected**: Immutable frozen plan and candidate fingerprints, single human authorizations, debit-before-read, one quota per sealed partition, Validation and separately authorized Final Holdout passes for a real-Agent candidate on real data, and a controlled report whose allowed and rejected paths are independently reviewable.
- **Observed**: Protected evaluation, authorization, debit, rejection and controlled-report mechanics pass only on synthetic snapshots. The #24 real Discovery snapshot exists, but no real-Agent candidate has been validated on real data, no sealed real protected partition has been opened and no real restricted report exists.
- **Evidence**:
  - `synthetic_mechanism` — `tests/test_protected_eval_cli.py, tests/test_holdout_report_cli.py`: Synthetic mechanism coverage only: frozen plans, single-use authorization, debit-before-read, rejection without debit, sealed-partition isolation and controlled report rendering. This does not verify a real protected evaluation.
- **Blockers**:
  - Missing: A real protected Evaluation (Validation, then separately authorized Final Holdout) for a frozen real-Agent candidate on the real snapshot, with immutable plans, authorizations, query ledger and controlled report.
    Unblock: Freeze the candidate and evaluation plan on the real snapshot, authorize and run Validation, obtain separate Final Holdout authorization and retain the immutable trusted records and controlled reports (issues #12/#13/#14).
  - Missing: Real independently validated candidate evidence: no real candidate has passed both protected stages, so there is no real controlled report to review.
    Unblock: Run the real protected evaluation successfully and have an independent reviewer verify frozen inputs, authorization, debit-before-read, coverage gaps, baseline, cost stress, parameter neighbourhood, thresholds and the allowed/rejected paths in the controlled report (issue #13).

### 6. Immutable releases and multi-strategy Shadow Live (`immutable_release_forward`) — BLOCKED

- **Scope**: data=synthetic, agent=mock, period=historical
- **Expected**: At least two real-data independently validated, human-approved immutable release versions, explicitly enabled at pre-registered after-close signal points, tracking at least two consecutive, common, committed real forward trading days, each recorded with its after-close signal_date, whether the resulting orders were persisted before the next session's open, the next_session where those orders reached a terminal state (filled/evidenced_not_filled), and the trusted ledger_day_id/result_hash, plus controlled synthetic boundary coverage.
- **Observed**: Release approval/fetch/lifecycle, explicit immutable human rejection, Shadow Live atomic day commits, committed-time fill eligibility, one-time dividend corrections and multi-version comparison pass on synthetic fixtures. The #24 real snapshot exists, but there is no real approved release, no real forward Shadow Live ledger and no real forward day reconciliation.
- **Evidence**:
  - `synthetic_mechanism` — `tests/test_release_cli.py, tests/test_shadow_cli.py, tests/test_compare_cli.py, tests/test_correction_delta_cli.py, tests/test_impact_cli.py`: Synthetic mechanism coverage only: immutable release identity, explicit human rejection records, lifecycle, atomic Shadow Live days, reruns, missing-price and missed-deadline boundaries, committed-time fill eligibility, corrections and common-interval comparison. Synthetic replay is not real forward evidence.
- **Blockers**:
  - Missing: At least two real candidates that independently passed real protected validation and received explicit human approval as immutable release versions; no real candidate has been approved.
    Unblock: After the real protected evaluation evidence exists, obtain explicit human approval for two distinct real candidates and register their unique immutable release IDs and pre-registered enable times (issue #14).
  - Missing: At least two consecutive, common, committed real forward trading days in a Shadow Live ledger, including one after-close signal submitted pre-open and reconciled to the next session's terminal order state.
    Unblock: Enable the approved releases after the pre-registered signal point, run trusted manual Shadow Live updates on at least two consecutive real sessions and reconcile the forward ledger without reusing synthetic or backtest data (issues #9/#14).

## Real-chain checklist

| Check | Result | Detail |
| --- | --- | --- |
| All six required scenarios are present and verified. (`six_scenarios_verified`) | NOT met | 2/6 verified (verified=2, blocked=4, failed=0, not_run=0). |
| One real-Agent candidate is traceable from experiment → freeze → both protected stages → human approval → immutable release → real forward ledger (non-empty IDs). (`candidate_chain`) | NOT met | real_chain.agent_candidate is empty. |
| The candidate's release is a real-data, human-approved immutable release bound to the same Validation and Final Holdout evidence. (`candidate_release`) | NOT met | No approved release entry matches the candidate release ID. |
| At least two real, independently validated, human-approved immutable releases. (`two_real_approved_releases`) | NOT met | 0 real approved release(s); independent pair: none. |
| At least two consecutive, common, committed real forward trading days, each with an after-close signal persisted pre-open and reconciled to the next session's order terminal state. (`real_forward_days`) | NOT met | 0 forward day(s); 0 reconciled (signal pre-open, terminal state recorded); consecutive real committed common window: none. |
| The approved candidate has committed real forward ledger days. (`candidate_forward_ledger`) | NOT met | candidate forward days: none. |
| The five real-required scenarios are verified with real-scope evidence (real data, controlled real Agent, real forward tracking). (`real_scope_evidence`) | NOT met | real_snapshot_baseline: real-scope verified; real_data_mock: status=blocked, data=synthetic (required real); controlled_real_llm: status=blocked, agent=mock (required real), data=synthetic (required real); independent_validation_reports: status=blocked, data=synthetic (required real); immutable_release_forward: status=blocked, data=synthetic (required real), period=historical (required forward). |

### Unmet real-chain conditions

**`six_scenarios_verified`** — All six required scenarios are present and verified.

- Missing: The six-scenario rule is not met: not verified for real_data_mock, controlled_real_llm, independent_validation_reports, immutable_release_forward.
- Unblock: Provide verifiable evidence for every required scenario; a synthetic-only result marks at most its synthetic scope and never satisfies the real criterion.

**`candidate_chain`** — One real-Agent candidate is traceable from experiment → freeze → both protected stages → human approval → immutable release → real forward ledger (non-empty IDs).

- Missing: An end-to-end real-Agent candidate trace with non-empty experiment, candidate, Validation, Final Holdout, release and forward-ledger IDs.
- Unblock: Run one real-Agent campaign, freeze the candidate, pass both protected stages under separate human authorizations, approve the immutable release and commit its real forward ledger days.

**`candidate_release`** — The candidate's release is a real-data, human-approved immutable release bound to the same Validation and Final Holdout evidence.

- Missing: A human-approved, real-data immutable release whose Validation and Final Holdout IDs match the candidate's.
- Unblock: Obtain explicit human approval for the frozen candidate and register the unique immutable release ID bound to the same protected-stage evidence.

**`two_real_approved_releases`** — At least two real, independently validated, human-approved immutable releases.

- Missing: At least two real, independently validated, human-approved immutable releases, each with its own Validation and Final Holdout evidence.
- Unblock: Independently validate and approve two distinct real candidates; each must consume its own protected intervals and be registered as a unique immutable release.

**`real_forward_days`** — At least two consecutive, common, committed real forward trading days, each with an after-close signal persisted pre-open and reconciled to the next session's order terminal state.

- Missing: At least two consecutive, common, committed real forward trading days in the trusted Shadow Live ledger with pre-open persisted orders and a recorded next-session terminal order state.
- Unblock: Commit the same consecutive real forward sessions for the approved releases in the trusted Shadow Live ledger after a pre-registered enable, then record each day's after-close signal, pre-open persisted orders, next session, order terminal state and ledger day result hash in the acceptance manifest.

**`candidate_forward_ledger`** — The approved candidate has committed real forward ledger days.

- Missing: A non-empty real forward ledger for the approved candidate; every listed forward day must be a committed, common, real session.
- Unblock: Record the candidate's release ID in the trusted forward ledger and commit its real forward days.

**`real_scope_evidence`** — The five real-required scenarios are verified with real-scope evidence (real data, controlled real Agent, real forward tracking).

- Missing: Real-scope verification evidence for: real_data_mock, controlled_real_llm, independent_validation_reports, immutable_release_forward.
- Unblock: Complete scenarios 2–6 with real evidence; synthetic mechanism tests never satisfy the real-chain gate.

## Verdict

- All six scenarios verified: **no**
- Six-scenario rule met: **no**
- End-to-end passed: **no**
- Overall status: **blocked**
- Failures: none

### Blockers

- **`scenario:real_data_mock`**
  - Missing: Experiment/campaign support for the real sealed Discovery snapshot (`campaign create` currently requires a synthetic snapshot), and a bounded fixed-Mock/fixed-seed campaign on that real Discovery partition.
  - Unblock: Extend the campaign/evaluation path to accept real sealed snapshots without weakening the sealed/Discovery-only rules, then run the bounded fixed-Mock campaign on the #24 snapshot.
- **`scenario:real_data_mock`**
  - Missing: A real parent/child experiment trace and an interrupted-then-resumed comparison on real Discovery, including failure and budget records.
  - Unblock: Execute the bounded Mock campaign, interrupt and resume one experiment, and record the deterministic diagnostics, IDs, budget ledger and parent/child links.
- **`scenario:controlled_real_llm`**
  - Missing: Authorized real model access (endpoint, credentials and attributable usage records) and a real Discovery campaign whose receipts the model can read; the delivery has neither.
  - Unblock: Provide authorized real model access and the real Discovery campaign from issues #24/#26, then retain attributable model request/session references, the two linked submissions and separately accounted platform versus model usage (issue #27).
- **`scenario:controlled_real_llm`**
  - Missing: Verified OS-level deployment isolation for the actual deployment, with allowed, expected-rejection and resource-abort evidence and trusted-side receipts; synthetic probes and trusted-local execution do not count.
  - Unblock: Deploy the worker under the verified OS-isolated profile and record allowed, expected-rejection and resource-abort evidence before executing model-generated or model-modified Python in a real campaign (issues #12/#27).
- **`scenario:independent_validation_reports`**
  - Missing: A real protected Evaluation (Validation, then separately authorized Final Holdout) for a frozen real-Agent candidate on the real snapshot, with immutable plans, authorizations, query ledger and controlled report.
  - Unblock: Freeze the candidate and evaluation plan on the real snapshot, authorize and run Validation, obtain separate Final Holdout authorization and retain the immutable trusted records and controlled reports (issues #12/#13/#14).
- **`scenario:independent_validation_reports`**
  - Missing: Real independently validated candidate evidence: no real candidate has passed both protected stages, so there is no real controlled report to review.
  - Unblock: Run the real protected evaluation successfully and have an independent reviewer verify frozen inputs, authorization, debit-before-read, coverage gaps, baseline, cost stress, parameter neighbourhood, thresholds and the allowed/rejected paths in the controlled report (issue #13).
- **`scenario:immutable_release_forward`**
  - Missing: At least two real candidates that independently passed real protected validation and received explicit human approval as immutable release versions; no real candidate has been approved.
  - Unblock: After the real protected evaluation evidence exists, obtain explicit human approval for two distinct real candidates and register their unique immutable release IDs and pre-registered enable times (issue #14).
- **`scenario:immutable_release_forward`**
  - Missing: At least two consecutive, common, committed real forward trading days in a Shadow Live ledger, including one after-close signal submitted pre-open and reconciled to the next session's terminal order state.
  - Unblock: Enable the approved releases after the pre-registered signal point, run trusted manual Shadow Live updates on at least two consecutive real sessions and reconcile the forward ledger without reusing synthetic or backtest data (issues #9/#14).
- **`real_chain:six_scenarios_verified`**
  - Missing: The six-scenario rule is not met: not verified for real_data_mock, controlled_real_llm, independent_validation_reports, immutable_release_forward.
  - Unblock: Provide verifiable evidence for every required scenario; a synthetic-only result marks at most its synthetic scope and never satisfies the real criterion.
- **`real_chain:candidate_chain`**
  - Missing: An end-to-end real-Agent candidate trace with non-empty experiment, candidate, Validation, Final Holdout, release and forward-ledger IDs.
  - Unblock: Run one real-Agent campaign, freeze the candidate, pass both protected stages under separate human authorizations, approve the immutable release and commit its real forward ledger days.
- **`real_chain:candidate_release`**
  - Missing: A human-approved, real-data immutable release whose Validation and Final Holdout IDs match the candidate's.
  - Unblock: Obtain explicit human approval for the frozen candidate and register the unique immutable release ID bound to the same protected-stage evidence.
- **`real_chain:two_real_approved_releases`**
  - Missing: At least two real, independently validated, human-approved immutable releases, each with its own Validation and Final Holdout evidence.
  - Unblock: Independently validate and approve two distinct real candidates; each must consume its own protected intervals and be registered as a unique immutable release.
- **`real_chain:real_forward_days`**
  - Missing: At least two consecutive, common, committed real forward trading days in the trusted Shadow Live ledger with pre-open persisted orders and a recorded next-session terminal order state.
  - Unblock: Commit the same consecutive real forward sessions for the approved releases in the trusted Shadow Live ledger after a pre-registered enable, then record each day's after-close signal, pre-open persisted orders, next session, order terminal state and ledger day result hash in the acceptance manifest.
- **`real_chain:candidate_forward_ledger`**
  - Missing: A non-empty real forward ledger for the approved candidate; every listed forward day must be a committed, common, real session.
  - Unblock: Record the candidate's release ID in the trusted forward ledger and commit its real forward days.
- **`real_chain:real_scope_evidence`**
  - Missing: Real-scope verification evidence for: real_data_mock, controlled_real_llm, independent_validation_reports, immutable_release_forward.
  - Unblock: Complete scenarios 2–6 with real evidence; synthetic mechanism tests never satisfy the real-chain gate.

### Unblock conditions

1. Extend the campaign/evaluation path to accept real sealed snapshots without weakening the sealed/Discovery-only rules, then run the bounded fixed-Mock campaign on the #24 snapshot.
2. Execute the bounded Mock campaign, interrupt and resume one experiment, and record the deterministic diagnostics, IDs, budget ledger and parent/child links.
3. Provide authorized real model access and the real Discovery campaign from issues #24/#26, then retain attributable model request/session references, the two linked submissions and separately accounted platform versus model usage (issue #27).
4. Deploy the worker under the verified OS-isolated profile and record allowed, expected-rejection and resource-abort evidence before executing model-generated or model-modified Python in a real campaign (issues #12/#27).
5. Freeze the candidate and evaluation plan on the real snapshot, authorize and run Validation, obtain separate Final Holdout authorization and retain the immutable trusted records and controlled reports (issues #12/#13/#14).
6. Run the real protected evaluation successfully and have an independent reviewer verify frozen inputs, authorization, debit-before-read, coverage gaps, baseline, cost stress, parameter neighbourhood, thresholds and the allowed/rejected paths in the controlled report (issue #13).
7. After the real protected evaluation evidence exists, obtain explicit human approval for two distinct real candidates and register their unique immutable release IDs and pre-registered enable times (issue #14).
8. Enable the approved releases after the pre-registered signal point, run trusted manual Shadow Live updates on at least two consecutive real sessions and reconcile the forward ledger without reusing synthetic or backtest data (issues #9/#14).
9. Provide verifiable evidence for every required scenario; a synthetic-only result marks at most its synthetic scope and never satisfies the real criterion.
10. Run one real-Agent campaign, freeze the candidate, pass both protected stages under separate human authorizations, approve the immutable release and commit its real forward ledger days.
11. Obtain explicit human approval for the frozen candidate and register the unique immutable release ID bound to the same protected-stage evidence.
12. Independently validate and approve two distinct real candidates; each must consume its own protected intervals and be registered as a unique immutable release.
13. Commit the same consecutive real forward sessions for the approved releases in the trusted Shadow Live ledger after a pre-registered enable, then record each day's after-close signal, pre-open persisted orders, next session, order terminal state and ledger day result hash in the acceptance manifest.
14. Record the candidate's release ID in the trusted forward ledger and commit its real forward days.
15. Complete scenarios 2–6 with real evidence; synthetic mechanism tests never satisfy the real-chain gate.
