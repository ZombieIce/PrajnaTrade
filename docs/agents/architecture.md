# Architecture map

A one-page navigation map for PrajnaTrade. Domain terms are defined once in
[`GLOSSARY.md`](../../GLOSSARY.md); the trust/research policy and its rationale
live in [ADR 0001](../adr/0001-synthetic-only-protected-research.md); exact
commands and output examples stay in [`README.md`](../../README.md). This page
only says **where things are and which boundary they cross**.

The end-to-end path:

`snapshot seal` → `campaign create` → `experiment submit` → `candidate freeze`
→ `evaluation plan` → `evaluation authorize`/`run` (Validation, then separately
authorized Final Holdout) → `release approve` → `release enable` →
`shadow update` → `acceptance report`.

Each arrow crosses a trust boundary and adds an immutable record; no later stage
rewrites an earlier one.

## Modules

A public seam is a module-level name without a leading underscore. Cross-module
private reuse (`snapshot._read_json` and friends) is legacy, not precedent.

| Module | Owns | Public seam |
| --- | --- | --- |
| [`snapshot.py`](../../src/prajnatrade/snapshot.py) | Sealed snapshot codec: canonical JSON hashing, seal/verify, frozen partition descriptors, Discovery-only reads, successor lineage. | `seal`, `manifest`, `partition_descriptor`, `read_discovery`, `read_protected`, `lineage_ids` |
| [`snapshot_baseline.py`](../../src/prajnatrade/snapshot_baseline.py) | Point-in-time Discovery accounting: availability by next open, equal-weight comparator, costs, dividends, per-partition strategy runs. | `run_discovery_baseline`, `run_discovery_equal_weight`, `run_discovery_strategy`, `run_partition_strategy`, `available_by`, `CN_TZ` |
| [`experiment.py`](../../src/prajnatrade/experiment.py) | Research project and bounded Campaign protocol: locked config, attempts, receipts, idempotent replay/resume, candidate freeze. | `project_init`, `project_status`, `campaign_create`, `experiment_submit`, `experiment_resume`, `experiment_status`, `freeze_experiment_candidate`, `candidate_status` |
| [`isolation.py`](../../src/prajnatrade/isolation.py) | OS worker boundary for generated Python: CPU/memory/wall/output limits and filesystem/network/process probes. | `run_isolated_strategy`, `probe_isolation_evidence`, `probe_isolation`, `IsolationLimits`, `IsolationError` |
| [`dividend.py`](../../src/prajnatrade/dividend.py) | Trusted cash-dividend correction journal; builds verified successor snapshots without rewriting the predecessor. | `register`, `verify`, `load_verified_revision`, `revision_predecessor`, `successor` |
| [`protected_eval.py`](../../src/prajnatrade/protected_eval.py) | Frozen plan, single human authorization, isolation probe, debit-before-read and immutable record for Validation/Final Holdout; publishes the coarse research receipt. | `create_plan`, `load_plan`, `authorize`, `run_evaluation`, `evaluation_status`, `agent_evaluation_status`, `audit` |
| [`evaluation_report.py`](../../src/prajnatrade/evaluation_report.py) | Operator-only controlled report rendered from stored records; deterministic JSON/HTML; never re-runs, never opens sealed data. | `build_evaluation_report`, `render_report_html`, `write_evaluation_report` |
| [`release.py`](../../src/prajnatrade/release.py) | Human approval/rejection, immutable release artifact, lifecycle timeline, admission and correction disposition. | `approve`, `reject`, `fetch`, `status`, `list_releases`, `enable`/`pause`/`resume`/`retire`, `disposition`, `admission_state`, `state_at`, `releases_active_at` |
| [`shadow.py`](../../src/prajnatrade/shadow.py) | Synthetic forward accounting per release version: frozen per-day inputs, atomic commits, safe reruns, audit dump, common-run comparison. | `update`, `write_report`, `compare`, `load_feed`, `Ledger`, `iter_committed_days`, `first_uncommitted_signal_date` |
| [`acceptance.py`](../../src/prajnatrade/acceptance.py) | Strict six-scenario manifest validation and deterministic report; the verdict cannot over-claim. | `validate_manifest`, `build_report`, `manifest_sha256`, `render_markdown`, `write_acceptance_report` |
| [`cli.py`](../../src/prajnatrade/cli.py) | Argparse surface and dispatch for the operator and research CLIs. | `build_parser`, `main`, `agent_main` |
| [`data.py`](../../src/prajnatrade/data.py), [`backtest.py`](../../src/prajnatrade/backtest.py), [`strategies.py`](../../src/prajnatrade/strategies.py) | Shared bar validation/fingerprints, deterministic engine, declarative strategy registry. | `load_bars`/`validate_calendar`, `run_backtest`/`equal_weight_buy_and_hold`, `make_strategy`/`strategy_fingerprint` |
| [`validation.py`](../../src/prajnatrade/validation.py) (legacy) | Legacy candidate validation/holdout import, plus the shared robustness plan used by protected evaluation. | `freeze_candidate`, `validate_candidate`, `run_holdout`, `make_robustness_plan` |
| [`research.py`](../../src/prajnatrade/research.py), [`report.py`](../../src/prajnatrade/report.py), [`providers.py`](../../src/prajnatrade/providers.py), [`store.py`](../../src/prajnatrade/store.py) (legacy) | Pre-MVP campaign loop, HTML campaign report, LLM provider contracts and legacy SQLite schema. `providers.py` serves the legacy autonomous `research` loop and provider tests; the new experiment protocol expects the external agent to propose, so it has no provider dependency. | `start_campaign`/`resume_campaign`/`campaign_status`, `write_campaign_report`, `ResearchProvider`/`MockProvider`/`OpenAICompatibleProvider`, `SQLiteStore` |

## Stores and trust boundaries

| Store | Layout | Written by | Read by |
| --- | --- | --- | --- |
| Sealed snapshot store (`--snapshot-store`) | `<store>/<snapshot_id>/{manifest,discovery,validation,final_holdout}.json` (0444/0555) and `<store>/.dividend-corrections/` | `snapshot seal`, `snapshot correction register/verify/successor` (journal and successor snapshot) | `campaign create` (Discovery copy), `evaluation run` (protected payload, after debit), `shadow update`/`release disposition` (corrections journal and lineage metadata only) |
| Trusted evaluation store (`--store`) | `plans/`, `authorizations/<plan_id>/`, `queries.sqlite`, `records/<evaluation_id>/`, `releases/<release_id>/{artifact,events,dispositions}`, `rejections/`, `audit/` | `evaluation plan/authorize/run`, `release approve/reject/lifecycle/disposition` | `evaluation status/report`, `release status/fetch/list`, `shadow update` (release artifacts) |
| Research project (`--project`) | `<project>/.prajnatrade/project.sqlite` plus saved request files | `project init`, `campaign create`, `experiment submit/resume`, `candidate freeze`, receipts published by the trusted side | `project status`, `experiment status`, agent `evaluation status` |
| Shadow Live ledger (`--ledger`) | `<ledger>/shadow.sqlite`: `input_snapshots`, `days`, `progress` | `shadow update` | `shadow report`, `shadow compare`, `release disposition` (pause point) |
| Acceptance input/report | Arbitrary files under `docs/acceptance/` | `acceptance report` (output only) | nothing else — the report is rendered from the manifest alone |

- **Research boundary:** the agent sees the project database (Campaign config,
  experiment receipts/results, candidates), one coarse Validation receipt and
  the constant Final Holdout receipt. It never sees a sealed payload, protected
  outcome/timing/diagnostic, admission or release state, or a Shadow Live row.
- **One read per partition:** every protected read follows a debit keyed by
  `(partition name, partition content hash)` and written before the read. A
  copied store is a new deployment with its own ledger; there is no refund.
- **No implicit lookup:** operator commands take explicit paths and exact IDs.
  There is no "latest" resolution, workspace copy or fallback to another stage.

## Command flows and state ownership

| Stage | Operator `prajnatrade` | Research `prajnatrade-agent` | State it owns |
| --- | --- | --- | --- |
| Seal | `snapshot seal`, `snapshot correction register/verify/successor` | — | snapshot content, IDs and correction journal |
| Campaign | `project init`, `campaign create` | `project status` | project ID, locked Campaign config, Discovery copy |
| Research | `experiment submit/resume`, `candidate freeze` | `experiment submit/status/resume` | attempts, hypotheses, receipts, candidates |
| Protected | `evaluation plan/authorize/run`, `evaluation report` | `evaluation status` (coarse only) | plan, authorization, debit, immutable record, controlled report |
| Release | `release approve/reject`, `enable/pause/resume/retire`, `disposition` | — | artifact, rejection decisions, lifecycle events, dispositions |
| Forward | `shadow update/report/compare` | — | per-version ledger days and comparison documents |
| Acceptance | `acceptance report` | — | nothing persisted; a report file from the manifest |

`build_parser(agent_only=True)` stops after the `evaluation status` parser, so
`project init`, campaign creation, `candidate freeze`, protected run/report,
release, shadow and acceptance are operator-only. New research surfaces require
an explicit grant in the spec; sealed partitions, Holdout diagnostics, admission
state and controlled-report internals never move to the agent CLI.

## Synthetic-only boundary and blocked real evidence

Every artifact in this build is synthetic: outputs carry
`scope`/`data_kind: "synthetic"`, and the acceptance gate requires real scope
**and** a complete real chain, so synthetic coverage can never pass it.

- **#24 — real five-ETF snapshot/baseline:** a real sealed snapshot with an
  official exchange calendar, verified corporate actions and point-in-time
  availability evidence, plus an independently reproduced Discovery baseline.
- **#26 — real-data Mock campaign:** a bounded Mock campaign with fixed
  configuration and seed on that real Discovery partition, including
  parent/child and interrupted-then-resumed evidence.
- **#27 — real model and deployment isolation:** authorized real LLM access and
  verified OS deployment isolation with allowed, rejection and resource-abort
  evidence; synthetic probes and trusted-local execution do not count.
- **Also blocked:** real protected evaluation (#12/#13/#14) and real approved
  releases with reconciled forward days (#9/#14).

The checked-in verdict in
[`docs/acceptance/issue-17-report.md`](../acceptance/issue-17-report.md) is
`blocked`, 1/6 verified, `real_chain` empty. Do not unblock by relabelling
synthetic evidence; see [CODING_STANDARDS.md](../../CODING_STANDARDS.md).

## Test map

Flow tests are subprocess public-CLI tests on synthetic fixtures, and each test
owns or copies its store. Module-scoped fixtures (for example the approved
`releases` template in `test_shadow_cli.py`) are immutable builders shared
across files, never mutable state.

| Flow | Test file | Covers |
| --- | --- | --- |
| Experiment | [`test_experiment_cli.py`](../../tests/test_experiment_cli.py) | Campaign lock, submit/status, idempotent replay, resume, budget/timeout, candidate freeze, agent surface |
| Snapshot | [`test_snapshot_cli.py`](../../tests/test_snapshot_cli.py), [`test_snapshot_baseline.py`](../../tests/test_snapshot_baseline.py), [`test_cli_baseline.py`](../../tests/test_cli_baseline.py), [`test_cli_data.py`](../../tests/test_cli_data.py) | Seal/manifest/integrity, partition split, Discovery-only reads, corrections/successors, point-in-time accounting, legacy baseline, bar validation |
| Protected evaluation | [`test_protected_eval_cli.py`](../../tests/test_protected_eval_cli.py) | Plan frozen before read, authorization, isolation probe, debit-before-read, single-use ledger, rejection without debit, agent receipt |
| Holdout/report | [`test_holdout_report_cli.py`](../../tests/test_holdout_report_cli.py) | Separate Holdout authorization, no re-read of Validation, controlled-report sections, constant Holdout receipt |
| Release | [`test_release_cli.py`](../../tests/test_release_cli.py) | Approval over passed records, immutable artifact/fetch, explicit rejection, lifecycle timeline |
| Impact | [`test_impact_cli.py`](../../tests/test_impact_cli.py) | Disposition classification, pause point, admission, no sealed read or debit |
| Shadow | [`test_shadow_cli.py`](../../tests/test_shadow_cli.py) | Atomic day, rerun/backfill, deadline eligibility, corporate actions, per-version blocking |
| Corrections | [`test_correction_delta_cli.py`](../../tests/test_correction_delta_cli.py) | One-time delta before/after payment, crash replay, no double-apply, no sealed read or debit |
| Compare | [`test_compare_cli.py`](../../tests/test_compare_cli.py) | Longest common consecutive run, rebasing, incomparable versions, agent has no compare |
| Acceptance | [`test_acceptance_cli.py`](../../tests/test_acceptance_cli.py) | Strict manifest schema, verdict rules, deterministic render, operator-only |
| Supporting | [`test_isolation.py`](../../tests/test_isolation.py), [`test_openai_provider.py`](../../tests/test_openai_provider.py), [`test_backtest.py`](../../tests/test_backtest.py), [`test_research_cli.py`](../../tests/test_research_cli.py) | Worker limits/probes, provider contract/budget, engine, legacy campaign loop |

## Pointers

- Terms and canonical vocabulary: [`GLOSSARY.md`](../../GLOSSARY.md).
- Policy, decision rationale and deferred refactors:
  [ADR 0001](../adr/0001-synthetic-only-protected-research.md).
- Test practice: [`docs/agents/testing.md`](testing.md). Worktree practice:
  [`docs/agents/worktrees.md`](worktrees.md). Both files arrive with the
  parallel guardrails branch; these links resolve once it merges.
- Commands and worked examples: [`README.md`](../../README.md).
- Review judgement calls: [`CODING_STANDARDS.md`](../../CODING_STANDARDS.md).
