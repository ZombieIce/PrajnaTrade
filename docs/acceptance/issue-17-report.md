# PrajnaTrade issue #17 — six-scenario acceptance report

**Verdict: NOT PASSED — blocked**

End-to-end MVP is NOT passed: the six-scenario rule was not met (1 of 6 required scenarios verified); 5 scenario(s) are blocked: real_snapshot_baseline, real_data_mock, controlled_real_llm, independent_validation_reports, immutable_release_forward.

Manifest SHA-256: `7dec5953d62f3327b3fc8e0ca08c254cb356cc9f362908458529b44310c8806d`. This report is rendered only from the acceptance manifest. It does not read a trusted store, re-run an evaluation, open a sealed partition or send an order. Verified means verified for the recorded scope; synthetic evidence never satisfies the real-chain gate.

## Delivery

| Field | Value |
| --- | --- |
| version | 0.1.0 (MVP-18 acceptance tooling) |
| branch | ticket-35-acceptance |
| tested_at | 2026-11-20T22:50:09+08:00 |
| environment | macOS 26.2 (arm64), Python 3.12.15, pytest 9.2.0; offline; synthetic fixtures only. macOS sandbox-exec covers the synthetic isolation tests; no real deployment isolation is verified. |
| test_command | PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/yibinzhang/go/src/copilot-worktrees/PrajnaTrade/zombieice-fluffy-giggle/.venv/bin/python -m pytest -q -p no:cacheprovider tests/ |
| test_result | 241 passed in 665.02s (0:11:05), exit 0 |

## Scenario summary

| # | Scenario | Status | Data | Agent | Period |
| --- | --- | --- | --- | --- | --- |
| 1 | `tests_and_cli` — Deterministic tests and public CLI | verified | synthetic | mock | historical |
| 2 | `real_snapshot_baseline` — Real five-ETF snapshot and equal-weight baseline | blocked | synthetic | mock | historical |
| 3 | `real_data_mock` — Real-data bounded Mock campaign | blocked | synthetic | mock | historical |
| 4 | `controlled_real_llm` — Controlled real external LLM iteration | blocked | synthetic | mock | historical |
| 5 | `independent_validation_reports` — Independent protected validation and controlled reports | blocked | synthetic | mock | historical |
| 6 | `immutable_release_forward` — Immutable releases and multi-strategy Shadow Live | blocked | synthetic | mock | historical |

Status counts: verified=1, blocked=5, failed=0, not_run=0.

## Scenario details

### 1. Deterministic tests and public CLI (`tests_and_cli`) — VERIFIED

- **Scope**: data=synthetic, agent=mock, period=historical
- **Expected**: The same delivery version runs the deterministic test suite and the public CLI workflows in a clean environment, with the operator/research split enforced and synthetic fixtures covering success, invalid input, replay, budget refusal, failure evidence and interrupted-resume paths.
- **Observed**: The recorded full-suite command passes in this delivery. The operator-only acceptance report validates the strict manifest and renders deterministic Markdown/JSON; prajnatrade-agent rejects the acceptance command. Synthetic scope only: this proves the synthetic test/CLI path, not real market data, real model access, deployed OS isolation, protected real evaluations or real forward tracking.
- **Evidence**:
  - `test_suite` — `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/yibinzhang/go/src/copilot-worktrees/PrajnaTrade/zombieice-fluffy-giggle/.venv/bin/python -m pytest -q -p no:cacheprovider tests/`: Full deterministic suite, exit 0; machine result is recorded in delivery.test_result. Synthetic fixtures only, no network and no broker.
  - `test_file` — `tests/test_acceptance_cli.py`: Public-CLI tests for strict manifest validation, stable rejection codes, verdict rules, deterministic rendering, no-store report generation and rejection of the operator command by prajnatrade-agent.
  - `test_file` — `tests/test_experiment_cli.py, tests/test_research_cli.py, tests/test_protected_eval_cli.py, tests/test_holdout_report_cli.py, tests/test_release_cli.py, tests/test_shadow_cli.py, tests/test_compare_cli.py`: Public-CLI workflow tests for the experiment protocol, protected evaluation, release lifecycle, Shadow Live and comparison, all subprocess-based on synthetic fixtures.
- **Blockers**: none

### 2. Real five-ETF snapshot and equal-weight baseline (`real_snapshot_baseline`) — BLOCKED

- **Scope**: data=synthetic, agent=mock, period=historical
- **Expected**: A shared real snapshot for 511260, 518880, 515080, 513300 and 159612 with official exchange calendar, verified corporate actions and price basis, point-in-time availability evidence, frozen 60/20/20 partitions and a Discovery equal-weight baseline that a second researcher can reproduce from the same approved inputs.
- **Observed**: Only the synthetic snapshot-sealing, partition and equal-weight baseline mechanisms have been exercised. No real five-ETF snapshot has been acquired, sealed, partitioned or independently recomputed in this delivery.
- **Evidence**:
  - `synthetic_mechanism` — `tests/test_snapshot_cli.py, tests/test_snapshot_baseline.py, tests/test_cli_baseline.py`: Synthetic mechanism coverage only: sealing, content fingerprints, Discovery-only reads, partition freezing and baseline reproduction on synthetic fixtures. This is not real market evidence.
- **Blockers**:
  - Missing: A real five-ETF snapshot (511260, 518880, 515080, 513300, 159612) with official exchange calendar, verified corporate actions/price basis and point-in-time availability evidence; no real data has been acquired or sealed.
    Unblock: Acquire and seal the real shared snapshot with official calendars, corporate actions and point-in-time availability evidence, freeze the 60/20/20 partition fingerprints, and keep the sealed partitions outside the research project (issue #24).
  - Missing: A frozen Discovery content fingerprint and a second researcher's independent re-computation of the equal-weight baseline trades, fees and NAV on the same approved inputs.
    Unblock: Publish the Discovery snapshot manifest and baseline configuration, then have an independent reviewer reproduce the Discovery equal-weight baseline from the same sealed inputs within the predeclared tolerance (issue #24).

### 3. Real-data bounded Mock campaign (`real_data_mock`) — BLOCKED

- **Scope**: data=synthetic, agent=mock, period=historical
- **Expected**: A bounded fixed-Mock, fixed-config and fixed-seed campaign on the real Discovery partition with hypotheses, parent/child experiment IDs, receipt-driven iterations, deterministic diagnostics, failure and budget records, and an interrupted-then-resumed run that matches the uninterrupted run without duplicate attempts or lost records.
- **Observed**: The experiment protocol, idempotent replay/resume and budget accounting are exercised only on synthetic fixtures. No bounded Mock campaign has run on a real Discovery partition, so real-data flow evidence is missing.
- **Evidence**:
  - `synthetic_mechanism` — `tests/test_experiment_cli.py, tests/test_research_cli.py`: Synthetic mechanism coverage only: Mock iterations, idempotent submission, resume, failure evidence and budget accounting. This proves the protocol, not real Discovery execution.
- **Blockers**:
  - Missing: The real Discovery partition from issue #24; no approved real snapshot exists for a bounded Mock campaign.
    Unblock: After the real Discovery snapshot is sealed and partitioned, run the bounded fixed-Mock, fixed-config and fixed-seed campaign on it and reconcile attempts, budget, diagnostics and receipts (issue #26).
  - Missing: A real parent/child experiment trace and an interrupted-then-resumed comparison on real Discovery, including failure records and budget debit/refund accounting.
    Unblock: Retain the real campaign's structured hypotheses, submissions, parent/child experiment IDs, deterministic diagnostics, failures and budget reconciliation, including the interrupted-run resume, for independent review (issue #26).

### 4. Controlled real external LLM iteration (`controlled_real_llm`) — BLOCKED

- **Scope**: data=synthetic, agent=mock, period=historical
- **Expected**: One complete autonomous iteration by an authorized real external LLM on the real Discovery campaign under verified deployment isolation: a falsifiable first hypothesis and strategy, a real receipt read, a self-justified successor experiment, at least one executed model-generated or model-modified Python strategy, and attributable model request/session and usage references.
- **Observed**: Only mock/stub providers, synthetic isolation probes and the disabled trusted-local path exist. No authorized real model access, real Discovery campaign or verified deployed OS isolation has been exercised, and no model-generated Python strategy has run.
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
- **Observed**: Protected evaluation, authorization, debit, rejection and controlled-report mechanics pass only on synthetic snapshots. No real-Agent candidate has been validated on real data, no sealed real partition has been opened and no real restricted report exists.
- **Evidence**:
  - `synthetic_mechanism` — `tests/test_protected_eval_cli.py, tests/test_holdout_report_cli.py`: Synthetic mechanism coverage only: frozen plans, single-use authorization, debit-before-read, rejection without debit, sealed-partition isolation and controlled report rendering. This does not verify a real protected evaluation.
- **Blockers**:
  - Missing: A real protected Evaluation (Validation, then separately authorized Final Holdout) for a frozen real-Agent candidate on the real snapshot, with immutable plans, authorizations, query ledger and controlled report.
    Unblock: Freeze the candidate and evaluation plan on the real snapshot, authorize and run Validation, obtain separate Final Holdout authorization and retain the immutable trusted records and controlled reports (issues #12/#13/#14).
  - Missing: Real independently validated candidate evidence: no real candidate has passed both protected stages, so there is no real controlled report to review.
    Unblock: Run the real protected evaluation successfully and have an independent reviewer verify frozen inputs, authorization, debit-before-read, coverage gaps, baseline, cost stress, parameter neighbourhood, thresholds and the allowed/rejected paths in the controlled report (issue #13).

### 6. Immutable releases and multi-strategy Shadow Live (`immutable_release_forward`) — BLOCKED

- **Scope**: data=synthetic, agent=mock, period=historical
- **Expected**: At least two real-data independently validated, human-approved immutable release versions, explicitly enabled at pre-registered after-close signal points, tracking at least two consecutive, common, committed real forward trading days, with one after-close signal submitted pre-open and reconciled to the next session's terminal order state, plus controlled synthetic boundary coverage.
- **Observed**: Release approval/fetch/lifecycle, Shadow Live atomic day commits, one-time dividend corrections and multi-version comparison pass on synthetic fixtures. There is no real approved release, no real forward ledger and no real multi-version tracking.
- **Evidence**:
  - `synthetic_mechanism` — `tests/test_release_cli.py, tests/test_shadow_cli.py, tests/test_compare_cli.py, tests/test_correction_delta_cli.py, tests/test_impact_cli.py`: Synthetic mechanism coverage only: immutable release identity, lifecycle, atomic Shadow Live days, reruns, missing-price and missed-deadline boundaries, corrections and common-interval comparison. Synthetic replay is not real forward evidence.
- **Blockers**:
  - Missing: At least two real candidates that independently passed real protected validation and received explicit human approval as immutable release versions; no real candidate has been approved.
    Unblock: After the real protected evaluation evidence exists, obtain explicit human approval for two distinct real candidates and register their unique immutable release IDs and pre-registered enable times (issue #14).
  - Missing: At least two consecutive, common, committed real forward trading days in a Shadow Live ledger, including one after-close signal submitted pre-open and reconciled to the next session's terminal order state.
    Unblock: Enable the approved releases after the pre-registered signal point, run trusted manual Shadow Live updates on at least two consecutive real sessions and reconcile the forward ledger without reusing synthetic or backtest data (issues #9/#14).

## Real-chain checklist

| Check | Result | Detail |
| --- | --- | --- |
| All six required scenarios are present and verified. (`six_scenarios_verified`) | NOT met | 1/6 verified (verified=1, blocked=5, failed=0, not_run=0). |
| One real-Agent candidate is traceable from experiment → freeze → both protected stages → human approval → immutable release → real forward ledger (non-empty IDs). (`candidate_chain`) | NOT met | real_chain.agent_candidate is empty. |
| The candidate's release is a real-data, human-approved immutable release bound to the same Validation and Final Holdout evidence. (`candidate_release`) | NOT met | No approved release entry matches the candidate release ID. |
| At least two real, independently validated, human-approved immutable releases. (`two_real_approved_releases`) | NOT met | 0 real approved release(s); independent pair: none. |
| At least two consecutive, common, committed real forward trading days. (`real_forward_days`) | NOT met | 0 forward day(s); consecutive real committed common window: none. |
| The approved candidate has committed real forward ledger days. (`candidate_forward_ledger`) | NOT met | candidate forward days: none. |
| The five real-required scenarios are verified with real-scope evidence (real data, controlled real Agent, real forward tracking). (`real_scope_evidence`) | NOT met | real_snapshot_baseline: status=blocked, data=synthetic (required real); real_data_mock: status=blocked, data=synthetic (required real); controlled_real_llm: status=blocked, agent=mock (required real), data=synthetic (required real); independent_validation_reports: status=blocked, data=synthetic (required real); immutable_release_forward: status=blocked, data=synthetic (required real), period=historical (required forward). |

### Unmet real-chain conditions

**`six_scenarios_verified`** — All six required scenarios are present and verified.

- Missing: The six-scenario rule is not met: not verified for real_snapshot_baseline, real_data_mock, controlled_real_llm, independent_validation_reports, immutable_release_forward.
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

**`real_forward_days`** — At least two consecutive, common, committed real forward trading days.

- Missing: At least two consecutive, common, committed real forward trading days in the trusted Shadow Live ledger.
- Unblock: Commit the same consecutive real forward sessions for the approved releases in the trusted Shadow Live ledger after a pre-registered enable.

**`candidate_forward_ledger`** — The approved candidate has committed real forward ledger days.

- Missing: A non-empty real forward ledger for the approved candidate; every listed forward day must be a committed, common, real session.
- Unblock: Record the candidate's release ID in the trusted forward ledger and commit its real forward days.

**`real_scope_evidence`** — The five real-required scenarios are verified with real-scope evidence (real data, controlled real Agent, real forward tracking).

- Missing: Real-scope verification evidence for: real_snapshot_baseline, real_data_mock, controlled_real_llm, independent_validation_reports, immutable_release_forward.
- Unblock: Complete scenarios 2–6 with real evidence; synthetic mechanism tests never satisfy the real-chain gate.

## Verdict

- All six scenarios verified: **no**
- Six-scenario rule met: **no**
- End-to-end passed: **no**
- Overall status: **blocked**
- Failures: none

### Blockers

- **`scenario:real_snapshot_baseline`**
  - Missing: A real five-ETF snapshot (511260, 518880, 515080, 513300, 159612) with official exchange calendar, verified corporate actions/price basis and point-in-time availability evidence; no real data has been acquired or sealed.
  - Unblock: Acquire and seal the real shared snapshot with official calendars, corporate actions and point-in-time availability evidence, freeze the 60/20/20 partition fingerprints, and keep the sealed partitions outside the research project (issue #24).
- **`scenario:real_snapshot_baseline`**
  - Missing: A frozen Discovery content fingerprint and a second researcher's independent re-computation of the equal-weight baseline trades, fees and NAV on the same approved inputs.
  - Unblock: Publish the Discovery snapshot manifest and baseline configuration, then have an independent reviewer reproduce the Discovery equal-weight baseline from the same sealed inputs within the predeclared tolerance (issue #24).
- **`scenario:real_data_mock`**
  - Missing: The real Discovery partition from issue #24; no approved real snapshot exists for a bounded Mock campaign.
  - Unblock: After the real Discovery snapshot is sealed and partitioned, run the bounded fixed-Mock, fixed-config and fixed-seed campaign on it and reconcile attempts, budget, diagnostics and receipts (issue #26).
- **`scenario:real_data_mock`**
  - Missing: A real parent/child experiment trace and an interrupted-then-resumed comparison on real Discovery, including failure records and budget debit/refund accounting.
  - Unblock: Retain the real campaign's structured hypotheses, submissions, parent/child experiment IDs, deterministic diagnostics, failures and budget reconciliation, including the interrupted-run resume, for independent review (issue #26).
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
  - Missing: The six-scenario rule is not met: not verified for real_snapshot_baseline, real_data_mock, controlled_real_llm, independent_validation_reports, immutable_release_forward.
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
  - Missing: At least two consecutive, common, committed real forward trading days in the trusted Shadow Live ledger.
  - Unblock: Commit the same consecutive real forward sessions for the approved releases in the trusted Shadow Live ledger after a pre-registered enable.
- **`real_chain:candidate_forward_ledger`**
  - Missing: A non-empty real forward ledger for the approved candidate; every listed forward day must be a committed, common, real session.
  - Unblock: Record the candidate's release ID in the trusted forward ledger and commit its real forward days.
- **`real_chain:real_scope_evidence`**
  - Missing: Real-scope verification evidence for: real_snapshot_baseline, real_data_mock, controlled_real_llm, independent_validation_reports, immutable_release_forward.
  - Unblock: Complete scenarios 2–6 with real evidence; synthetic mechanism tests never satisfy the real-chain gate.

### Unblock conditions

1. Acquire and seal the real shared snapshot with official calendars, corporate actions and point-in-time availability evidence, freeze the 60/20/20 partition fingerprints, and keep the sealed partitions outside the research project (issue #24).
2. Publish the Discovery snapshot manifest and baseline configuration, then have an independent reviewer reproduce the Discovery equal-weight baseline from the same sealed inputs within the predeclared tolerance (issue #24).
3. After the real Discovery snapshot is sealed and partitioned, run the bounded fixed-Mock, fixed-config and fixed-seed campaign on it and reconcile attempts, budget, diagnostics and receipts (issue #26).
4. Retain the real campaign's structured hypotheses, submissions, parent/child experiment IDs, deterministic diagnostics, failures and budget reconciliation, including the interrupted-run resume, for independent review (issue #26).
5. Provide authorized real model access and the real Discovery campaign from issues #24/#26, then retain attributable model request/session references, the two linked submissions and separately accounted platform versus model usage (issue #27).
6. Deploy the worker under the verified OS-isolated profile and record allowed, expected-rejection and resource-abort evidence before executing model-generated or model-modified Python in a real campaign (issues #12/#27).
7. Freeze the candidate and evaluation plan on the real snapshot, authorize and run Validation, obtain separate Final Holdout authorization and retain the immutable trusted records and controlled reports (issues #12/#13/#14).
8. Run the real protected evaluation successfully and have an independent reviewer verify frozen inputs, authorization, debit-before-read, coverage gaps, baseline, cost stress, parameter neighbourhood, thresholds and the allowed/rejected paths in the controlled report (issue #13).
9. After the real protected evaluation evidence exists, obtain explicit human approval for two distinct real candidates and register their unique immutable release IDs and pre-registered enable times (issue #14).
10. Enable the approved releases after the pre-registered signal point, run trusted manual Shadow Live updates on at least two consecutive real sessions and reconcile the forward ledger without reusing synthetic or backtest data (issues #9/#14).
11. Provide verifiable evidence for every required scenario; a synthetic-only result marks at most its synthetic scope and never satisfies the real criterion.
12. Run one real-Agent campaign, freeze the candidate, pass both protected stages under separate human authorizations, approve the immutable release and commit its real forward ledger days.
13. Obtain explicit human approval for the frozen candidate and register the unique immutable release ID bound to the same protected-stage evidence.
14. Independently validate and approve two distinct real candidates; each must consume its own protected intervals and be registered as a unique immutable release.
15. Commit the same consecutive real forward sessions for the approved releases in the trusted Shadow Live ledger after a pre-registered enable.
16. Record the candidate's release ID in the trusted forward ledger and commit its real forward days.
17. Complete scenarios 2–6 with real evidence; synthetic mechanism tests never satisfy the real-chain gate.
