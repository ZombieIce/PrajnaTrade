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

The #43 simulated-tracking path is parallel and separate:
`round open` → `round submit` → `round validate` (`round revalidate` after a
correction) → `round review` → `manual_tracking` enable → `shadow update` →
`round evidence status/decide`. It never enters the protected path above.

Each arrow crosses a trust boundary and adds an immutable record; no later stage
rewrites an earlier one.

## Modules

A public seam is a module-level name without a leading underscore. Cross-module
private reuse (`snapshot._read_json` and friends) is legacy, not precedent.

| Module | Owns | Public seam |
| --- | --- | --- |
| [`snapshot.py`](../../src/prajnatrade/snapshot.py) | Sealed snapshot codec: canonical JSON hashing, seal/verify, frozen partition descriptors, Discovery-only reads, successor lineage, and the frozen 70/30 split of the Discovery interval (slice fingerprinting, descriptor freeze and verification; part of the runtime identity hash set). | `seal`, `manifest`, `partition_descriptor`, `read_discovery`, `read_protected`, `lineage_ids`, `partition_slice`, `discovery_split`, `verify_discovery_split` |
| [`snapshot_baseline.py`](../../src/prajnatrade/snapshot_baseline.py) | Point-in-time Discovery accounting: availability by next open, equal-weight comparator, costs, dividends, per-partition strategy runs. | `run_discovery_baseline`, `run_discovery_equal_weight`, `run_discovery_strategy`, `run_partition_strategy`, `available_by`, `first_ready_open`, `CN_TZ` |
| [`experiment.py`](../../src/prajnatrade/experiment.py) | Research project and bounded Campaign protocol: locked config (including the optional container executor and its image digest), the opt-in frozen leading-development split that restricts the Campaign copy to development data, attempts, receipts, idempotent replay/resume, candidate freeze. | `project_init`, `project_status`, `campaign_create`, `experiment_submit`, `experiment_resume`, `experiment_status`, `freeze_experiment_candidate`, `candidate_status`, `runtime_identity`, `runtime_matches`, `signal_window_sessions` |
| [`research_direction.py`](../../src/prajnatrade/research_direction.py) | Direction-bound research round (#37): approved kickoff, proposal lock, published coarse Validation receipt, dated detail request/feedback, selection freeze and retrospective; keeps the research side on the project database. | `direction_id`, `prepare_kickoff`, `kickoff`, `load_kickoff`, `proposal_submit`, `publish_validation`, `validation_details`, `publish_validation_details`, `record_feedback`, `pending_validation_work`, `selected_candidate`, `direction_status`, `list_directions`, `freeze`, `retrospective` |
| [`research_holdout.py`](../../src/prajnatrade/research_holdout.py) | Direction-bound Final Holdout (#37): authorization, independence and overlap blockers, calendar-bound conditional release, scored-failure retrospective. | `authorize`, `run`, `load_authorization`, `load_holdout_claim`, `blockers`, `claim`, `finish` |
| [`research_loop.py`](../../src/prajnatrade/research_loop.py) | Research Loop (#43): approved round contract on one Campaign, the protocol-v1/protocol-v2 round binding (v2 records the frozen 70/30 split, its `split_hash`, the development/trailing segments and the round's automatic Validation limit; the legacy protocol hash and kickoff shape do not move), content-addressed locked versions, Lightweight Validation replay, returned-version reselection, corrected-successor and platform-fix revalidation (new evidence epochs with the original/recomputed Discovery runs and affected diff), immutable per-version approve/return/end decisions, manual simulated-tracking admission, observation recommendation and early closure, and the Evidence Loop's observation, opportunity, scoped-pause and terminal review. Protocol v2 `validate` is the formal trailing-segment Validation: a zero-count sealed-manifest gate, one atomic `research_loop_validation_uses` reservation per version, one counted `read_discovery`, a trailing-only candidate/benchmark run with the development slice as prehistory, a formal receipt and a protocol-aware `revalidate` epoch. Never opens a protected partition or writes a query debit. | `prepare_round`, `open_round`, `load_round`, `round_status`, `submit`, `validate`, `revalidate`, `review`, `close`, `evidence_status`, `evidence_decide`, `observation_recommendation`, `replay_engine_identity` |
| [`isolation.py`](../../src/prajnatrade/isolation.py) | OS worker boundary for generated Python: CPU/memory/wall/output limits and filesystem/network/process probes. | `run_isolated_strategy`, `probe_isolation_evidence`, `probe_isolation`, `IsolationLimits`, `IsolationError` |
| [`container_executor.py`](../../src/prajnatrade/container_executor.py) | Docker/Colima boundary for generated Python: image-digest resolution, one fresh container per strategy call, actual-inspect verification, bounded JSON protocol, trusted weight validation and per-invocation evidence. | `ContainerStrategyExecutor`, `ContainerLimits`, `ExecutionResult`, `ImageIdentity`, `executor_from_config` |
| [`dividend.py`](../../src/prajnatrade/dividend.py) | Trusted cash-dividend correction journal; builds verified successor snapshots without rewriting the predecessor. | `register`, `verify`, `load_verified_revision`, `revision_predecessor`, `successor` |
| [`protected_eval.py`](../../src/prajnatrade/protected_eval.py) | Frozen plan, single human authorization, isolation probe, debit-before-read and immutable record for Validation/Final Holdout; publishes the coarse research receipt. | `create_plan`, `load_plan`, `authorize`, `run_evaluation`, `evaluation_status`, `agent_evaluation_status`, `audit` |
| [`evaluation_report.py`](../../src/prajnatrade/evaluation_report.py) | Operator-only controlled report rendered from stored records; deterministic JSON/HTML; never re-runs, never opens sealed data. | `build_evaluation_report`, `render_report_html`, `write_evaluation_report` |
| [`release.py`](../../src/prajnatrade/release.py) | Human approval/rejection, immutable release artifact, lifecycle timeline, admission and correction disposition; write-once manual simulated-tracking artifacts and their single enable event. | `approve`, `reject`, `fetch`, `status`, `list_releases`, `enable`/`pause`/`resume`/`retire`, `register_tracking_version`, `tracking_id`, `disposition`, `admission_state`, `state_at`, `releases_active_at` |
| [`shadow.py`](../../src/prajnatrade/shadow.py) | Synthetic forward accounting per release version: frozen per-day inputs, atomic commits, safe reruns, audit dump, common-run comparison, and the Evidence Loop's per-day tracking-opportunity, scoped-pause and terminal-ended records. | `update`, `write_report`, `compare`, `evidence`, `mark_version_ended`, `load_feed`, `Ledger`, `iter_committed_days`, `first_uncommitted_signal_date` |
| [`shadow_calendar.py`](../../src/prajnatrade/shadow_calendar.py) | Independent trading-day authority: calendar validation, session open/close instants (09:30/15:00 Asia/Shanghai) and authorized-session matching. | `validate_calendar`, `load_calendar`, `authorized_sessions_match`, `session_open`, `session_close`, `OPEN`, `CLOSE` |
| [`shadow_schedule.py`](../../src/prajnatrade/shadow_schedule.py) | Operator Shadow Live clock: exclusive ledger lock, target-session selection, missed-deadline and blocking alerts, batched per-version update. | `tick`, `notifications` |
| [`acceptance.py`](../../src/prajnatrade/acceptance.py) | Strict six-scenario manifest validation and deterministic report; the verdict cannot over-claim. | `validate_manifest`, `build_report`, `manifest_sha256`, `render_markdown`, `write_acceptance_report` |
| [`cli.py`](../../src/prajnatrade/cli.py) | Argparse surface and dispatch for the operator and research CLIs. | `build_parser`, `main`, `agent_main` |
| [`data.py`](../../src/prajnatrade/data.py), [`backtest.py`](../../src/prajnatrade/backtest.py), [`strategies.py`](../../src/prajnatrade/strategies.py) | Shared bar validation/fingerprints, deterministic engine, declarative strategy registry. | `load_bars`/`validate_calendar`, `run_backtest`/`equal_weight_buy_and_hold`, `make_strategy`/`strategy_fingerprint` |
| [`validation.py`](../../src/prajnatrade/validation.py) (legacy) | Legacy candidate validation/holdout import, plus the shared robustness plan used by protected evaluation. | `freeze_candidate`, `validate_candidate`, `run_holdout`, `make_robustness_plan` |
| [`research.py`](../../src/prajnatrade/research.py), [`report.py`](../../src/prajnatrade/report.py), [`providers.py`](../../src/prajnatrade/providers.py), [`store.py`](../../src/prajnatrade/store.py) (legacy) | Pre-MVP campaign loop, HTML campaign report, LLM provider contracts and legacy SQLite schema. `providers.py` serves the legacy autonomous `research` loop and provider tests; the new experiment protocol expects the external agent to propose, so it has no provider dependency. | `start_campaign`/`resume_campaign`/`campaign_status`, `write_campaign_report`, `ResearchProvider`/`MockProvider`/`OpenAICompatibleProvider`, `SQLiteStore` |

## Stores and trust boundaries

| Store | Layout | Written by | Read by |
| --- | --- | --- | --- |
| Sealed snapshot store (`--snapshot-store`) | `<store>/<snapshot_id>/{manifest,discovery,validation,final_holdout}.json` (0444/0555) and `<store>/.dividend-corrections/` | `snapshot seal`, `snapshot correction register/verify/successor` (journal and successor snapshot) | `campaign create` (Discovery copy; with `--discovery-split` only the frozen development slice is copied), `evaluation run` (protected payload, after debit), `round validate/revalidate` (v2: one counted trailing read of the sealed Discovery payload, or a verified corrected successor; v1: the whole-interval replay; no protected partition), `shadow update`/`release disposition` (corrections journal and lineage metadata only) |
| Trusted evaluation store (`--store`) | `plans/`, `authorizations/<plan_id>/`, `queries.sqlite`, `records/<evaluation_id>/`, `releases/<release_id>/{artifact,events,dispositions}`, `rejections/`, `audit/`, `research-loop-rounds/<round_id>.json`, `research-loop-validations/<validation_id>.json`, `research-loop-revalidations/<revalidation_id>.json`, `research-loop-reviews/<decision_id>.json`, `research-loop-evidence/<decision_id>.json` | `evaluation plan/authorize/run`, `release approve/reject/lifecycle/disposition`, `round open/validate/revalidate/review/evidence decide` (write-once round, receipt, corrected-evidence revalidation and decision copies) | `evaluation status/report`, `release status/fetch/list`, `shadow update` (release and manual-tracking artifacts), `round validate/revalidate/review` (round copy and stored receipts/decisions, including the prior receipt and original/recomputed Discovery history), `round evidence status` |
| Research project (`--project`) | `<project>/.prajnatrade/project.sqlite` plus saved request files; #43 adds `research_loop_rounds`, `_submissions`, `_validations`, `_validation_attempts`, `_reviews`, `_observations`, `_closures`, `_revalidations`, `_evidence_decisions`; #50-T2 adds `research_loop_validation_uses` (the authoritative automatic trailing-read ledger, one row per round/version, unique `(round_id, version_id)` and `(round_id, sequence)`). A split Campaign's `campaigns.snapshot` holds only the development slice; the frozen split descriptor lives in `evaluation_json`. | `project init`, `campaign create` (optionally only the development slice), `experiment submit/resume`, `candidate freeze`, `direction`/`validation` handlers, `round open/submit/validate/revalidate/review/close/evidence decide`, receipts published by the trusted side | `project status`, `experiment status`, agent `evaluation status`, `round status`, `round evidence status`, agent `round status` |
| Shadow Live ledger (`--ledger`) | `<ledger>/shadow.sqlite`: `input_snapshots`, `days`, `progress`, `schedule_notifications`, `scope_pauses` | `shadow update/tick` (day commits and automatic scoped pauses for blocked versions), `round evidence decide` (retire marks the version's ledger progress ended) | `shadow report`, `shadow compare`, `round evidence status`, `release disposition` (pause point) |
| Acceptance input/report | Arbitrary files under `docs/acceptance/` | `acceptance report` (output only) | nothing else — the report is rendered from the manifest alone |

- **Research boundary:** the agent sees the project database (Campaign config,
  experiment receipts/results, candidates), one coarse Validation receipt and
  the constant Final Holdout receipt. For #43, the agent `round status` view
  shows each version's current review state, return instructions and
  observation/tracking summary only; it never exposes the reviewer identity or
  decision reason, ledger rows or accounting detail. For a protocol-v2 round it
  also shows the frozen research partition protocol, the development/trailing
  dates, session counts and content hashes, and the round's automatic
  Validation limit and use counters (`validation_allowance` counters and use
  rows, `lifecycle`) — fingerprints, dates and counters, never a sealed
  payload, per-session curve or run detail. The agent
  never sees a sealed payload, protected outcome/timing/diagnostic, admission or
  release state, or a Shadow Live row.
- **One read per partition:** every protected read follows a debit keyed by
  `(partition name, partition content hash)` and written before the read. A
  copied store is a new deployment with its own ledger; there is no refund.
- **No implicit lookup:** operator commands take explicit paths and exact IDs.
  There is no "latest" resolution, workspace copy or fallback to another stage.

## Command flows and state ownership

| Stage | Operator `prajnatrade` | Research `prajnatrade-agent` | State it owns |
| --- | --- | --- | --- |
| Seal | `snapshot seal`, `snapshot correction register/verify/successor` | — | snapshot content, IDs and correction journal |
| Campaign | `project init`, `campaign create [--discovery-warmup-sessions N] [--discovery-split 70/30]` | `project status` | project ID, locked Campaign config, Discovery copy (only the development slice when split) |
| Research | `experiment submit/resume`, `candidate freeze` | `experiment submit/status/resume` | attempts, hypotheses, receipts, candidates |
| Protected | `evaluation plan/authorize/run`, `evaluation report` | `evaluation status` (coarse only) | plan, authorization, debit, immutable record, controlled report |
| Research loop | `round open/validate/revalidate/review/close` | `round status`, `round submit` | approved round contract (protocol v1, or v2 with the frozen split and auto-validation limit), locked versions, Lightweight Validation or formal trailing Validation receipts (with the per-version trailing-read use), corrected-evidence revalidation epochs (no protected debit), immutable review decisions, `manual_tracking` artifact and enable event, closure |
| Evidence loop | `round evidence status/decide`, `shadow tick/update/report/compare` | — | observation progress, opportunity aggregates, scoped pauses, ledger days, common-interval comparison |
| Release | `release approve/reject`, `enable/pause/resume/retire`, `disposition` | — | artifact, rejection decisions, lifecycle events, dispositions |
| Forward | `shadow update/report/compare` | — | per-version ledger days and comparison documents |
| Acceptance | `acceptance report` | — | nothing persisted; a report file from the manifest |

`build_parser(agent_only=True)` stops after the `evaluation status` parser plus the granted `round status`/`round submit` parsers, so
`project init`, campaign creation, `candidate freeze`, protected run/report,
release, shadow and acceptance are operator-only. `round open`, `validate`,
`revalidate`, `review`, `close`, `evidence status` and `evidence decide` are
also operator-only: the agent gets no `--store`, no ledger, no
Lightweight Validation receipt beyond the round-status allowlist and no
protected read. That allowlist is the agent round view — review state, return
instructions and observation/tracking summary. It may show the round kickoff's
authorizing reviewer and approval reason, but never a per-version review
decision's reviewer/reason or any ledger detail. The manual simulated-tracking admission and
the continue/pause/retire review stay human operator decisions. New research
surfaces require an explicit grant in the spec; sealed partitions, Holdout
diagnostics, admission state and controlled-report internals never move to the
agent CLI.

## Real Discovery and the protected research boundary

Schema-version-2 historical Discovery is supported by snapshot sealing,
baselines and declarative campaigns. Issue #24 verified the real five-ETF
baseline. Issue #26 verified the bounded Mock campaign, including OS isolation
for the actual research run and a reconciled interruption and resume.
Protected evaluation, release and Shadow Live remain synthetic or replay.
The #43 Research Loop and Evidence Loop are synthetic simulated-tracking
mechanisms only; they add no protected read, debit or real-chain evidence.
The overall gate requires real evidence and the complete real chain.

- **#27 — real model and deployment isolation:** authorized real LLM access and
  verified OS deployment isolation with allowed, rejection and resource-abort
  evidence; synthetic probes and trusted-local execution do not count. The
  trusted container boundary and generated-Python submission protocol now
  exist, but the external model session, project-only relay and real-Discovery
  iteration remain outstanding, so the scenario stays blocked.
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
| Discovery split | [`test_discovery_split_cli.py`](../../tests/test_discovery_split_cli.py) | Opt-in 70/30 split arithmetic and fingerprints on a session count not divisible by 10, development-only Campaign copy, identical Discovery results for snapshots differing only in the trailing segment, v2 round open/submit/status protocol binding and validation-policy rejection, and the unchanged v1 kickoff/locked/receipt shape |
| Formal trailing Validation | [`test_formal_validation_cli.py`](../../tests/test_formal_validation_cli.py) | v2 trailing-only receipt interval and cash basis, distinguishable from the v1 replay, zero-count sealed-manifest gap block, one counted use per version across interruption/replay/failed verdicts, exhausted-allowance refusal before the read, two versions racing the last slot, compliant vs short prehistory, first-decision point-in-time, post-reservation partition mismatch, `round status` allowance/lifecycle/summary audit, and corrected-successor revalidation with a trailing-only affected diff and `EVIDENCE_DEVELOPMENT_MISMATCH` |
| Research loop | [`test_research_loop_cli.py`](../../tests/test_research_loop_cli.py) | Round contract freeze/replay, content-addressed version lock, lightweight Validation replay (interval/scoreable days/fills/cash), warmup R1/R2, metrics-not-ready vs intentional cash vs ordered-unfilled, hard-condition and insufficient-evidence verdicts, interruption recovery, early close, agent boundary |
| Version review | [`test_version_review_cli.py`](../../tests/test_version_review_cli.py) | Approve/return/end state machine, risk acceptance and explicit observation, manual simulated-tracking artifact and next-session enable, V1 re-selection with its stored receipt, conflict/replay, platform-fix revalidation with prior-receipt history, agent boundary |
| Corrected evidence | [`test_corrected_evidence_cli.py`](../../tests/test_corrected_evidence_cli.py) | Successor lineage, frozen interval/data-kind/prior-history/as-of verification, corrected Discovery recompute for candidate and benchmark, original vs recomputed records and affected diff, replay and review history, typed `EVIDENCE_*` rejection, `round validate` superseded |
| Evidence loop | [`test_evidence_loop_cli.py`](../../tests/test_evidence_loop_cli.py) | Observation recommendation and researcher confirmation, due/filled/unfilled opportunities, intent-without-fill, `metrics_not_ready` vs intentional cash vs ordered-unfilled, scoped pause while another version continues, trusted-data review trigger, continue/pause/retire, retire terminal per version, dual-version common interval |
| Direction | [`test_research_direction_cli.py`](../../tests/test_research_direction_cli.py), [`test_validation_selection_cli.py`](../../tests/test_validation_selection_cli.py) | Kickoff approval/replay, proposal/detail/feedback, published receipts, selection freeze and retrospective, leadership selection independence |
| Holdout | [`test_issue37_holdout_cli.py`](../../tests/test_issue37_holdout_cli.py), [`test_holdout_recovery_cli.py`](../../tests/test_holdout_recovery_cli.py), [`test_direction_schema_v2_cli.py`](../../tests/test_direction_schema_v2_cli.py) | Separate Holdout authorization, calendar-bound conditional enable, independence/overlap blockers, interrupted/missing-record recovery, no admission on incomplete evidence, schema-v2 protected input |
| Validation reuse | [`test_reusable_validation_cli.py`](../../tests/test_reusable_validation_cli.py), [`test_validation_batch_cli.py`](../../tests/test_validation_batch_cli.py), [`test_validation_scoring_cli.py`](../../tests/test_validation_scoring_cli.py) | One debit per new version, replay/recovery without a second debit, concurrent proposal handling, benchmark-failure pause, preregistered scoring prerequisites |
| Execution rules and warmup | [`test_execution_rules_cli.py`](../../tests/test_execution_rules_cli.py), [`test_signal_history.py`](../../tests/test_signal_history.py) | Frozen `next-open-v1`/`retry-initial-v1` behavior, scored opens, point-in-time indicator history requirements (#39/#41) |
| Generated Python | [`test_experiment_python.py`](../../tests/test_experiment_python.py), [`test_container_executor.py`](../../tests/test_container_executor.py) | Python submission normalization, source hash/fingerprint, executor requirement, replay/conflict/parent/budget/resume with a fake executor, persisted evidence; real Docker/Colima boundary, host/network denial, invalid-weight rejection (Docker-gated) |
| Snapshot | [`test_snapshot_cli.py`](../../tests/test_snapshot_cli.py), [`test_snapshot_baseline.py`](../../tests/test_snapshot_baseline.py), [`test_real_snapshot.py`](../../tests/test_real_snapshot.py), [`test_cli_baseline.py`](../../tests/test_cli_baseline.py), [`test_cli_data.py`](../../tests/test_cli_data.py) | Seal/manifest/integrity, partition split, Discovery-only reads, corrections/successors, point-in-time accounting, schema-v2 real validation and per-field availability/halts, real Discovery baseline, legacy baseline, bar validation |
| Protected evaluation | [`test_protected_eval_cli.py`](../../tests/test_protected_eval_cli.py) | Plan frozen before read, authorization, isolation probe, debit-before-read, single-use ledger, rejection without debit, agent receipt |
| Holdout/report | [`test_holdout_report_cli.py`](../../tests/test_holdout_report_cli.py) | Separate Holdout authorization, no re-read of Validation, controlled-report sections, constant Holdout receipt |
| Release | [`test_release_cli.py`](../../tests/test_release_cli.py) | Approval over passed records, immutable artifact/fetch, explicit rejection, lifecycle timeline |
| Impact | [`test_impact_cli.py`](../../tests/test_impact_cli.py) | Disposition classification, pause point, admission, no sealed read or debit |
| Shadow | [`test_shadow_cli.py`](../../tests/test_shadow_cli.py) | Atomic day, rerun/backfill, deadline eligibility, corporate actions, per-version blocking |
| Scheduler | [`test_shadow_schedule.py`](../../tests/test_shadow_schedule.py) | Operator tick/lock, target session, durable deduped alerts, missed-deadline detection, feed-scope mismatch |
| Corrections | [`test_correction_delta_cli.py`](../../tests/test_correction_delta_cli.py) | One-time delta before/after payment, crash replay, no double-apply, no sealed read or debit |
| Compare | [`test_compare_cli.py`](../../tests/test_compare_cli.py) | Longest common consecutive run, rebasing, incomparable versions, agent has no compare |
| Acceptance | [`test_acceptance_cli.py`](../../tests/test_acceptance_cli.py) | Strict manifest schema, verdict rules, deterministic render, operator-only |
| Supporting | [`test_isolation.py`](../../tests/test_isolation.py), [`test_openai_provider.py`](../../tests/test_openai_provider.py), [`test_backtest.py`](../../tests/test_backtest.py), [`test_research_cli.py`](../../tests/test_research_cli.py) | Worker limits/probes, provider contract/budget, engine, legacy campaign loop |

## Pointers

- Terms and canonical vocabulary: [`GLOSSARY.md`](../../GLOSSARY.md).
- Policy, decision rationale and deferred refactors:
  [ADR 0001](../adr/0001-synthetic-only-protected-research.md).
- Test practice: [`docs/agents/testing.md`](testing.md). Worktree practice:
  [`docs/agents/worktrees.md`](worktrees.md).
- Commands and worked examples: [`README.md`](../../README.md).
- Review judgement calls: [`CODING_STANDARDS.md`](../../CODING_STANDARDS.md).
