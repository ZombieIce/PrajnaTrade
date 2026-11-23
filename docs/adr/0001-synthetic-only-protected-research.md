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

Both are behaviour-preserving refactors that need their own review; the
cross-module helper seams (`snapshot_baseline.CN_TZ`/`available_by`,
`validation.make_robustness_plan`, `protected_eval.load_plan`/`audit`,
`snapshot.lineage_ids`) and the removal of trivial middlemen are already done.

## References

- `GLOSSARY.md` for the domain language.
- `docs/acceptance/issue-17-manifest.json` and `issue-17-report.md` for the
  current honest verdict and blockers.
- `AGENTS.md` and `docs/agents/domain.md` for the glossary/ADR layout.
