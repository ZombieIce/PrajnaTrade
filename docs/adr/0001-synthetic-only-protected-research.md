# ADR 0001 — Synthetic-only protected research with a trusted/research boundary

- **Status:** accepted (issue #17, MVP series)
- **Date:** 2026-11-20

## Context

PrajnaTrade runs a research loop over five ETFs and holds out two protected
partitions for honest, single-use evaluation. The repository and its CI have no
real market data, no authorized real model access, no broker and no deployed OS
isolation. The issue #17 handoff therefore requires the six-scenario acceptance
report to stay honest: mechanisms can be verified on synthetic fixtures, but the
real-chain gate must remain blocked until real prerequisites exist.

Two failure modes drove this decision:

1. A research agent that can see protected outcomes, dates or diagnostics can
   overfit or leak the holdout, defeating the purpose of the split.
2. A checklist that treats synthetic mechanism coverage as real evidence can
   claim end-to-end acceptance without any real data, model or forward ledger.

## Direction-bound policy (issue #37)

The following policy applies to research rounds created with an approved
kickoff. The original decision below records the meaning of legacy plans,
authorizations, debits and releases under issue #17. Their original evidence
remains readable under its recorded rules.

1. The research lead confirms the direction, independence declaration,
   Campaign, objective, constraints, evaluation protocol and Validation budget.
   The comparison budget covers one debit per newly evaluated strategy version
   in the round. Discovery and Validation are development evidence. The
   research side receives summary metrics and can request dated Validation
   details by recording a question. Holdout evidence stays operator-only.
2. A frozen Candidate binds the selected passing Validation record and its
   selection rationale. Holdout authorization fixes the candidate, that
   Validation record, the snapshot, the Holdout interval, the evaluation
   protocol and the lead's independence declaration. Reusing a direction key
   across Campaigns or renaming the direction preserves its Holdout history.
3. Different directions can use an overlapping Holdout interval with recorded
   independence at kickoff and selection. Prior use by the same direction or
   declared influence from an overlapping evaluation requires an unseen
   interval. An overlapping legacy debit has unknown direction provenance.
   The evaluator checks exposure atomically before committing a debit.
4. The Holdout authorization gives conditional consent for automatic release.
   A verified pass creates one immutable release and enables the first eligible
   after-close signal session from the authorized calendar. A scored failure
   ends the round with a controlled retrospective. An aborted or interrupted
   read preserves the debit and leaves approval unfulfilled.
5. Recurring trusted jobs can process research requests and update Shadow Live.
   The trading-day clock retains the ledger's order cutoffs and records
   availability, runtime and deadline alerts. Scope follows the documented
   inputs and observed execution. The acceptance requirements for real research
   and reconciled real forward evidence continue to apply.

## Manual simulated-tracking admission (issue #43)

Issue #43 adds a default simulated-tracking loop beside the protected
direction-bound policy above. Its admission is a human review decision, not
protected evidence.

1. **Three decisions, one current state.** A locked research version has exactly
   one current review state: `pending`, `approved`, `returned` or `ended`.
   `approve`, `return` and `end` are mutually exclusive decisions recorded as
   immutable, content-addressed records with a named reviewer and reason; `end`
   is terminal, `returned` may later be approved or ended, and replaying a
   decision returns the stored one. A passed Lightweight Validation never
   pre-fills the human decision.
2. **No protected allowance or debit.** Lightweight Validation replays the
   locked version and benchmark over the frozen Discovery interval using the
   frozen scoring start, execution rules and costs. It does not open a
   Validation or Final Holdout partition, does not spend a Validation
   allowance, and records `opened_protected_source: false` and
   `query_debited: false`. Revalidation keeps the strategy identity and records
   a new evidence epoch: a corrected successor must preserve the frozen
   interval, data kind and prior history, and the original and recomputed
   Discovery runs stay as history. Approval creates a write-once
   `manual_tracking` artifact and one enable event at the first eligible
   trading session; it is not `release approve` and not a Holdout
   authorization.
3. **Synthetic stays labelled.** Simulated-tracking artifacts carry
   `simulated_tracking: true`, and the Shadow Live ledger remains synthetic or
   replay evidence with the same `scope`/`data_kind` labels. A synthetic ledger
   is not forward evidence and never satisfies the real-chain acceptance gate.
4. **Old records keep their protocols.** Issue #37 plans, authorizations, query
   debits, immutable releases, rejections and receipts, and the issue #17
   records, remain governed by the direction-bound policy above and their
   original recorded rules. The new loop does not re-judge or migrate them, and
   a `manual_tracking` version is a different admission source, not a protected
   release.

## Discovery split and trailing Validation (issue #50)

Issue #50 revises the #43 Validation interval: the frozen Discovery interval is
split 70/30 so that a locked version can be checked on a later, unedited part of
Discovery after research has stopped changing it.

1. **The split covers the Campaign's sealed Discovery interval only.** Under
   protocol v2, `development_sessions = N * 70 // 100` (floor) of the Discovery
   sessions and the remainder goes to the trailing side; the two segments are
   contiguous, disjoint and exhaustive. The descriptor records the boundary
   rule, both segments' real dates, session counts and content hashes, the
   sealed full Discovery partition hash and a canonical `split_hash`. The
   sealed 60/20/20 `discovery`/`validation`/`final_holdout` layout is
   unchanged; the split never crosses into the sealed protected partitions.
2. **The Campaign copy holds development data only.** With `campaign create
   --discovery-split 70/30` the research project stores just the development
   slice and computes the baseline, scoring start and every experiment over it,
   so Discovery reads approved development data by construction. The trailing
   segment stays in the sealed snapshot.
3. **No protected read and no query debit.** The trailing segment is part of
   the sealed Discovery payload and is read through `snapshot.read_discovery`
   exactly like the v1 evidence; formal trailing Validation is Discovery
   evidence, not #37 protected Validation, not a protected allowance and not a
   Final Holdout step. Its receipts record `opened_protected_source: false`
   and `query_debited: false`.
   - **One counted read per version, reserved before the read.** A v2 round
     freezes an automatic Validation limit. The first trailing read of a
     distinct locked version reserves one immutable
     `research_loop_validation_uses` row inside the round lock and the same
     SQLite write transaction, before any attempt row, trusted write or sealed
     read; the reservation and the receipt carry it (`usage`). A resume, replay,
     receipt view or revalidation of the same version never consumes again, and
     an interrupted, failed or insufficient read keeps its row. Exhausting the
     limit refuses the next read before it happens, with no attempt and no
     trusted record.
   - **Known gaps are free.** Only sealed manifest metadata is read before the
     reservation: a declared gap inside the trailing segment, or a frozen
     research interval that does not match the sealed Discovery descriptor,
     blocks with `VALIDATION_DATA_GAP` and `counted: false`. History shortfalls
     and gaps discovered by the run are evidence verdicts and count.
   - **Post-read mismatches keep the use.** A payload whose data kind, frozen
     partition hash, prior history, as-of projection or recomputed split fails
     verification is refused with `VALIDATION_PARTITION_MISMATCH` after the
     reservation, so the counted read stays auditable.
   - **Revalidation keeps the version's single use.** A corrected successor
     must preserve the frozen development slice hash, data kind/schema, prior
     history, interval/session dates and as-of; the trailing segment is then
     recomputed and a new formal epoch records the prior receipt, the
     original/recomputed validation records and the trailing affected diff. A
     changed development slice is `EVIDENCE_DEVELOPMENT_MISMATCH`.
   - **The automatic limit pauses; a human admits one version.** Once every
     automatic slot is used, the next automatic trailing read is refused before
     any read, attempt or trusted write (`VALIDATION_ALLOWANCE_EXHAUSTED`); the
     round lifecycle becomes `awaiting_validation_admission` while the ordinary
     `status` stays `open`, and Discovery continues — new experiments and
     locked versions are still accepted. The researcher may instead select an
     already approved passing version or close with no candidate; closing stays
     allowed at the limit or early with Campaign budget remaining, and a new v2
     closure records the allowance and, when a version is selected, its receipt
     (`validation_id`/`receipt_hash`/`validation_kind`/`protocol`).
   - **A manual admission is one version, one read, and granting is not a
     read.** `round admit` records one write-once, content-addressed manual
     Validation admission (reviewer, reason, comparison, selection impact)
     bound to one locked version, in the trusted store plus a project
     projection; identical content replays, a different admission for that
     version is a conflict, and a version whose read is already counted or
     completed cannot be admitted. Granting writes no use row and changes no
     counter. The admitted version's next v2 `validate` consumes it atomically
     as one `manual` use row carrying the `admission_id`; a different version
     can never consume it, and replay, resume or an interruption never consumes
     it twice. The receipt `usage` and `round status` distinguish automatic
     from manual reads; `lifecycle` is `converged` only when the round is
     closed.
4. **Old records keep their protocols.** A round without a frozen split stays
   protocol v1 (`discovery-replay`): its kickoff shape, protocol hash, locked
   version and receipt are unchanged, and it refuses a `validation_policy`.
   A v2 round records `trailing-70-30`/`trailing_validation`, the frozen split
   and the round's automatic Validation limit; its protocol hash folds the
   protocol and split, so its versions trace to the partition protocol they
   were locked under. A v2 round never falls back to the v1 Discovery replay.
   Issue #43 and issue #17 records are read under their original recorded
   protocol and are not re-judged or migrated. A closure stored before #50
   keeps its exact legacy body and replays by its stored `closure_id`; only a
   new closure records the v2 allowance/selection additions.
5. **The formal receipt keeps its own protocol.** A v2 receipt records
   `validation_kind: "trailing_validation"`, the frozen protocol/split, the
   development/trailing/scoring/warmup/provenance records, the counted `usage`
   and the verdict summary. The additive fields are published only when
   present, so a v1 `discovery_replay` receipt keeps its exact #43 shape and is
   still read under its original protocol; `replay` remains a v1-only check.
6. **The receipt summary is a deliberate agent grant.** A v2 `round status`
   publishes the formal receipt's `summary` (candidate and benchmark return,
   drawdown, fees, slippage, turnover, fills, orders and ending equity, the
   delta and the verdict reason) plus the frozen protocol/development/trailing/
   scoring/warmup/provenance/usage blocks. This is intended: the agent must be
   able to propose a testable revision. The grant does not include the
   trusted-only per-session `curves`, any ledger row or price, the
   `query_debited`/`opened_protected_source` internals, sealed payloads or
   protected records, and it does not weaken the human boundary: a per-version
   review decision's reviewer/reason and a manual admission's reviewer/reason
   stay operator-only, as do the protected flows. Legacy v1 summaries keep their
   original shape.
7. **A detail query is a controlled projection, not a read.** `round detail`
   (agent + operator, project database only, no `--store`) records a non-empty
   question and a fixed allowlist of records over the version's **published**
   project receipt, under a content-addressed `detail_access_id`; identical
   requests replay the stored resolution. Unknown or unavailable records are
   typed rejections, and a version without a terminal receipt has nothing to
   project. It never projects `curves` (trusted-only per-session evidence),
   never re-reads a sealed payload or ledger, never consumes a Validation use or
   an admission, and never changes the allowance. Feedback is the recorded
   impact linked to that receipt (and optionally one detail access and the next
   known Campaign experiment); it is content-addressed, replayable and not a
   read. A later submission may cite a prior version (`revision_of`) and prior
   feedback (`feedback_ids`) as validated provenance in its body; these links
   are deliberately outside `version_id`, because a revision is the same
   experiment content whether or not its provenance is cited.
8. **Reused evidence is disclosed across rounds.** A v2 round open on a
   snapshot that an earlier round of the same project already used records one
   `reuse_disclosure`: each earlier round's protocol and the interval its
   published receipt actually used (a formal `trailing` segment or the legacy
   replay's scoring interval), its intersection with the new trailing segment,
   and the feedback recorded on those versions. The disclosure is derived once
   from the project's own published receipts and feedback rows, stored outside
   `kickoff_hash` in the trusted round record and its project copy, and returned
   unchanged on replay, including when the trusted record exists but its project
   projection crashed. Protocol-v1 rounds get no disclosure; old receipts keep
   being read under their original protocol rather than re-judged.
9. **The selection basis is traceable from the public CLI.** A synthetic
   mechanism test follows snapshot → round → version → experiment → locked
   protocol → actual development and scoring dates → warmup source →
   usage/allowance (including a manual admission read) → feedback → review state
   → tracking using only `round status` and one `round detail` with
   `records: ["provenance"]`, without re-reading sealed data. This proves the
   mechanism #35 needs; the real receipt linking those facts and real forward
   acceptance remain #35's gate and are not claimed here.
10. **Synthetic stays labelled.** The split, formal-Validation, summary/detail/
    feedback, reuse-disclosure and traceability tests are mechanism tests on
    synthetic sealed snapshots; they do not establish real forward evidence and
    do not change the #17 acceptance verdict.

## Original decision (issue #17)

1. **Synthetic only.** Every sealed snapshot, evaluation, release, forward
   ledger and acceptance scenario in this build is synthetic or a replay.
   Outputs carry `scope: "synthetic"` and `data_kind: "synthetic"`; nothing in
   the delivery claims real market performance, real order execution or
   deployed isolation.
2. **Trusted/research boundary.** Sealed partitions, evaluation records,
   controlled reports, release artifacts and Shadow Live ledgers live outside
   Git and outside the research project. The operator CLI (`prajnatrade`) owns
   plan/authorize/run/report/approve/disposition/update; the research CLI
   (`prajnatrade-agent`) can read at most a coarse Validation receipt and the
   constant Final Holdout receipt, plus `evaluation status`. It cannot read a
   protected partition, a Holdout outcome/status/timing/diagnostic, admission
   state, a release artifact or a Shadow Live ledger.
3. **One read per partition.** The debit is keyed by *partition name and
   partition content hash* and is written before the sealed read. The same
   plan/candidate/stage retry returns the stored record without another debit;
   anything else against a consumed partition is a request rejection with no
   read. Repackaging identical content under a new snapshot ID cannot buy a new
   allowance.
4. **Human decisions are explicit and immutable.** A release requires two
   passed protected stages and an explicit human `approve`; an explicit
   `reject` is itself an immutable decision that blocks a later approval for
   the same candidate+plan. Approval does not enable tracking. Lifecycle events
   and correction dispositions are write-once and effective at pre-registered
   after-close signal dates, never at wall-clock approval time.
5. **The acceptance report cannot over-claim.** The strict manifest is the only
   input. `end_to_end_passed` requires all six scenarios verified with real
   scope **and** a real forward chain: one real-Agent candidate from experiment
   and freeze through both protected stages, human approval, immutable release
   and a reconciled real forward ledger, plus at least two real approved
   releases and two consecutive common committed real forward days whose
   after-close signals were persisted pre-open and reconciled to the next
   session's order terminal state. The checked-in verdict is
   `overall_status: blocked` and `end_to_end_passed: false`.

## Consequences

- Synthetic mechanism tests can be thorough and fast, but they never count as
  real evidence. Scenario statuses are always read together with their scope.
- The operator must run specific commands with the trusted store; there is no
  convenience fallback, "latest" lookup, workspace copy or automatic debit
  refund.
- Immutable decisions mean a rejected candidate cannot be re-approved; a new
  candidate and frozen plan are the only path forward.

### Blocked real-evidence items

The following remain blocked in the checked-in acceptance manifest and require
real prerequisites outside this repository: a real five-ETF sealed snapshot
with official calendar/corporate actions/point-in-time evidence; a bounded Mock
campaign on that real Discovery partition; authorized real LLM access under
verified OS deployment isolation; a real protected evaluation (and independent
review of its controlled report); and two real human-approved releases with at
least two consecutive reconciled real forward Shadow Live days. Until then the
acceptance report must not claim end-to-end acceptance.

## Deferred structural refactors

Recorded here rather than silently performed in the #17 implementation:

- Splitting the 500+ line functions in `shadow.py`, `release.py` and
  `protected_eval.py` into smaller deep modules.
- Refactoring the `cli.py` dispatch into a command registry.
- Splitting `research_loop.py` (round, lightweight Validation, review,
  revalidation and the Evidence Loop read model in one module) and bundling the
  loose scoring-history parameters from #41 into one history-policy type.

These are behaviour-preserving refactors that need their own review; the
cross-module helper seams (`snapshot_baseline.CN_TZ`/`available_by`,
`validation.make_robustness_plan`, `protected_eval.load_plan`/`audit`,
`snapshot.lineage_ids`) and the removal of trivial middlemen are already done.

## References

- `GLOSSARY.md` for the domain language.
- `docs/acceptance/issue-17-manifest.json` and `issue-17-report.md` for the
  current honest verdict and blockers.
- `AGENTS.md` and `docs/agents/domain.md` for the glossary/ADR layout.
