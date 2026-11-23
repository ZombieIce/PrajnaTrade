# Coding standards

Review-axis standards for PrajnaTrade: the judgement calls a reviewer must make,
not the mechanical ones. Formatting, import order and lint belong to `ruff` and
CI; this file does not restate them. Where a rule has a mechanical check, it
names the check instead. Architecture and the test map live in
[`docs/agents/architecture.md`](docs/agents/architecture.md).

1. **Synthetic evidence stays labelled synthetic.** Keep `scope`/`data_kind` on
   every artifact, name commands and reports after what they do (`synthetic`,
   `replay`, `mock`), and never describe a synthetic or mock run as real,
   forward or market evidence. *Why:* scope is what separates a verified
   mechanism from an acceptance claim, and a mislabel reads as a false claim.
2. **Blocked stays blocked.** A scenario, blocker or verdict may become
   verified/passed only in the change that supplies the named real
   prerequisite; deleting or softening a blocker is not progress. *Why:* the
   #24/#26/#27 prerequisites are external, so the honest record is the only
   guard against over-claiming end-to-end acceptance.
3. **Checked-in generated artifacts are deterministic and regenerate from
   source.** Re-render and commit artifacts such as
   `docs/acceptance/issue-17-report.md` in the same change, with no timestamps,
   randomness, wall-clock values or machine-specific paths. *Why:* the
   acceptance test byte-compares the checked-in report with a fresh render, and
   a machine path leaks an environment and breaks reproducibility.
4. **Tests must run alone and own their state.** Every test must pass when
   selected individually; a module-scoped fixture may build an immutable
   template, but each test copies it so no test depends on another test's
   writes. Shared CLI/process/environment helpers belong in the test helper
   module (see [`docs/agents/testing.md`](docs/agents/testing.md)), not copied
   into each file. *Why:* order dependence hides failures, and copied helpers
   drift while the CLI is the contract under test.
5. **Cross-module access uses public seams.** Import only non-underscore names
   across modules; if a private helper is genuinely shared, promote it to a
   public alias (as `snapshot_baseline.CN_TZ` did) instead of reaching into
   another module's internals. *Why:* private reuse couples modules and blocks
   the deferred refactors; `ruff` rule `PLC2701` (private-name import) flags it
   mechanically.
6. **The agent CLI grows only by explicit spec grant.** Add a
   `prajnatrade-agent` command, flag or field only when the issue/spec grants
   research visibility, and never expose sealed partitions, Holdout
   outcome/status/timing/diagnostics, admission or release state, or
   controlled-report internals there. *Why:* visible protected outcomes invite
   overfitting and can leak the holdout.
7. **New trust boundaries record fingerprints, authorization and query
   receipts.** A command that opens sealed content or moves trusted data to the
   research side must persist content fingerprints, the human authorization ID
   and a receipt, with any debit written before the read. *Why:* those fields
   are what let a later reviewer reproduce and bound the access.
8. **No real broker, orders, credentials or network in tests.** Tests and
   fixtures stay synthetic and hermetic: no broker connection, no real order,
   no required API key and no external network, only local stubs. *Why:* the
   suite must stay deterministic and offline; a real credential or order is
   irreversible and cannot be replayed.

Reviewed against these plus
[ADR 0001](docs/adr/0001-synthetic-only-protected-research.md) and the
[glossary](GLOSSARY.md). Exact commands and formats stay in
[`README.md`](README.md); test practice stays in
[`docs/agents/testing.md`](docs/agents/testing.md).
