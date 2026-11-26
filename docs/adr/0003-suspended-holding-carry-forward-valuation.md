# ADR 0003 — Carry-forward valuation for confirmed full-day suspended holdings

- **Status:** accepted (spec #68: tickets #69, #70, #71)
- **Date:** 2026-11-26
- **Supersedes (opt-in only):** the statements in [ADR 0002](0002-actual-daily-open-execution.md) (Decision 3 and Consequences) that a held symbol with a confirmed full-day no-trade session must always leave close valuation unavailable. Under the legacy default `strict-close-v1`, ADR 0002 remains unchanged; under the opt-in `carry-forward-suspended-v1`, confirmed full-day no-trade holdings carry forward their last authorized trusted unadjusted close (adjusted for evidenced corporate actions).

## Context

In five-ETF daily research, one ETF can be suspended for a full trading session
while the remaining ETFs continue trading normally (for example, `159612` full-day
suspensions while both the candidate and the equal-weight benchmark hold it).
Under `strict-close-v1`, a confirmed full-day no-trade session leaves the entire
account's daily NAV unavailable (`null`), which makes full-period maximum
drawdown unavailable even when every other holding's close and the suspension
notice are fully evidenced. Researchers therefore could not evaluate frozen
drawdown limits against a continuous account ledger.

Two principles constrain the solution:

1. **Distinguish confirmed full-day no-trade from real data gaps.** A session
   with sealed, timely status evidence proving zero trading across the full
   session (09:30–15:00 Asia/Shanghai) is fundamentally different from a missing
   bar, an unknown status, a late notice published after the valuation cutoff, a
   status/trade conflict, or a resumed session whose close is missing. Only the
   first may carry forward a prior close; every other case remains a valuation
   gap.
2. **Isolate valuation from signals, execution, and warmup.** A carried-forward
   valuation price is an end-of-day mark-to-book convention for held shares. It
   is never a real trade, never a signal input, never a valid warmup
   observation, and never an executable open or open-equity sizing input.

## Decision

1. **Opt-in frozen valuation policy (`carry-forward-suspended-v1`).**
   `trading_status.py` defines `STRICT_CLOSE_VALUATION = "strict-close-v1"` (the
   immutable legacy default),
   `CARRY_FORWARD_SUSPENDED_VALUATION = "carry-forward-suspended-v1"`, and the
   shared public predicates `confirmed_no_trade_shape`,
   `halt_conflicts_with_trade`, and `has_status_evidence_id`.
   `campaign create --valuation-policy` freezes the chosen policy into
   `execution_rules["valuation"]`, `evaluation_hash`, and `runtime_identity`
   (binding `trading_status.py` and `valuation_policy` into
   `runtime_dependencies`). Candidate and equal-weight benchmark runs share the
   exact frozen valuation policy across Discovery and formal trailing Validation,
   each accounting from its own holdings. Protected Evaluation rejects
   `carry-forward-suspended-v1` at plan creation without reading or debiting any
   protected partition.

2. **Per-symbol, per-holding, per-session carry-forward rule.**
   Under `carry-forward-suspended-v1`, at each scoring session's close cutoff
   (`23:59:59+08:00`):
   - **Empty position (`shares <= ε`):** never creates a holding valuation gap
     (`valuation_basis: "not_held"`), regardless of whether that symbol was
     suspended or had a price gap—unless an earlier unresolved corporate action
     on that symbol remains outstanding (`unresolved[symbol]`), in which case
     unknown cash/share entitlement keeps daily NAV and derived return/drawdown
     unavailable even after exiting the position.
   - **Trusted confirmed close today:** resets the symbol's carry-forward anchor
     to today's unadjusted close, date, and source evidence (with an empty
     `adjustments` trail), clears any prior chain break, and values the holding
     at `actual_close` (or fails closed if `unresolved[symbol]` is non-empty).
     A `partial_halt` whose halt window covers the full session (`09:30`–`15:00`)
     with any reported trade price (`open`/`high`/`low`/`close`) or positive
     volume is never accepted as a trusted close; it is rejected as
     `conflicting_status_or_trade_evidence`.
   - **Confirmed full-day no-trade today** (classified via the shared
     `trading_status.confirmed_no_trade_shape` predicate: sealed `suspended` row
     with null prices, null/zero volume, and no conflict, or real `partial_halt`
     with a whole-session halt window, zero/null volume, and null OHLC; timely
     status availability evidence by `23:59:59+08:00`; and a non-empty
     `status_evidence_id` on real snapshots, i.e., schema 3+):
     - If an unbroken trusted prior close anchor exists for the holding (and no
       unresolved corporate action affects it), increment `carried_sessions` by
       one trading session (weekends and holidays never generate points or
       increment the count) and value the holding at
       `quantity * valuation_price` (`valuation_basis: "carried_forward_prior_close"`).
       The holding records `close: null` alongside `valuation_price` and a
       `carry_forward` audit block (`source_close_date`, `source_close_price`,
       `source_close_evidence`, `valuation_price`, `carried_sessions`,
       `adjustments`, `status_evidence`).
     - If no authorized prior close exists (`missing_authorized_prior_close`) or
       an earlier gap broke the chain (`carry_forward_chain_broken`), valuation
       stays missing and cannot jump across the gap.
   - **Any other missing/unconfirmed close** (missing bar, `unknown` status,
     missing `status_evidence_id` on a real snapshot such as schema 2, notice
     published after `23:59:59+08:00`, `status_conflict` or whole-session halt
     contradicted by reported trade prices or positive volume, or resumed
     `partial_halt` with missing close): invalidates the holding's carry-forward
     anchor, breaks the chain, records a specific `valuation_gap_reason`, and
     leaves daily NAV and full-period maximum drawdown unavailable until a
     future session supplies a trusted close. Late evidence and resumption
     prices never backfill prior sessions.

3. **Evidenced corporate action adjustments during suspension (#70).**
   - `snapshot.py` validates optional positive integer `sequence` fields on
     `corporate_actions` (rejecting non-integer/non-positive values and
     duplicate `(date, symbol, sequence)` pairs) and sorts sealed actions by
     `(date, symbol, sequence)` ahead of canonical bytes.
   - Under `carry-forward-suspended-v1`, `_apply_actions(..., enforce_action_order=True)`
     validates same-day corporate actions per held symbol atomically: multiple
     same-day events on one held symbol require explicit unique positive integer
     `sequence` numbers; if order is ambiguous (`corporate_action_order_ambiguous`),
     terms or pre-open (`09:30+08:00`) availability evidence are missing/late
     (`corporate_action_terms_or_availability_unknown`,
     `cash_dividend_terms_unknown`, `share_adjustment_ratio_unknown`), or
     `pay_date < ex_date` (`cash_dividend_pay_date_precedes_ex_date`), no event
     in that symbol's day batch mutates shares or receivables and
     `unresolved[symbol]` records the gap permanently for the run.
   - For evidenced events on a carried-forward holding, `_adjust_carried_basis_for_actions`
     applies each event once in evidenced order and appends an audit record
     (`date`, `type`, terms/evidence/timestamps, `valuation_price_before`,
     `valuation_price_after`, and optional `sequence`) to
     `carry_forward.adjustments` while preserving the original
     `source_close_date`, `source_close_price`, and `source_close_evidence`:
     - **Cash dividend:** reduces carried `valuation_price` by `cash_per_share`
       on ex-date, accrues `quantity * cash_per_share` in `receivables`, and
       moves the receivable into `cash` on `pay_date` (even if still suspended).
     - **Share adjustment:** multiplies `shares[symbol]` by `ratio` and divides
       carried `valuation_price` by `ratio` (keeping market value invariant).
     - If an adjustment produces a non-positive valuation price (`<= ε`),
       valuation fails closed (`carried_valuation_price_non_positive`) and
       breaks the carry chain.
   - Returning to a trusted unadjusted close resets the anchor and clears
     `adjustments`, ensuring prior corporate actions are never double-applied on
     resumption or replay. Unheld symbols' corporate actions never affect an
     account that holds zero shares.

4. **Boundary preservation on confirmed full-day suspensions.**
   `snapshot.py` delegates to the same `trading_status.confirmed_no_trade_shape`
   predicate so a confirmed full-day no-trade session (with valid availability
   and, on real snapshots, schema-3+ `status_evidence_id` and no conflicts)
   qualifies as a complete boundary session in `common_range` and
   `--full-history` `authorized_history`. Schema-2 real snapshots (which forbid
   `status_evidence_id`) and missing, unknown, unexplained, or conflicting tail
   rows remain incomplete, keeping seal boundaries and carry-forward valuation
   in exact agreement. Frozen authorized, warmup, development, trailing, and
   scoring boundaries are never silently shortened when a confirmed suspension
   lands on a quarter end, scoring end, or authorized end date.

5. **Strict separation from signals, fills, and warmup.**
   Carried-forward valuation prices never enter `open_prices`, `open_equity`,
   `derived` / `history_derived`, or the 61-observation warmup count. A
   confirmed full-day suspension still produces zero fills and zero sell
   proceeds for the suspended ETF; under `actual-open-v1`, a held suspended ETF
   still leaves open equity unknown (`equity_basis_unknown`) for equity-dependent
   orders while explicit exits of priced holdings still execute. Existing
   strategy `state["equity"]` visibility continues to read the known account NAV.

6. **Additive audit fields, formal trailing Validation integration (#71), and legacy immutability.**
   - Under `strict-close-v1`, every receipt, metric key set, equity-curve point,
     quarterly summary, and runtime hash stays byte-for-byte compatible with
     prior behavior, and previously stored receipts remain readable unchanged.
   - Under `carry-forward-suspended-v1`, runs emit additive per-day
     `valuation_basis`/`carried_forward_symbols`, additive metrics
     (`actual_close_sessions`, `carried_forward_sessions`,
     `missing_valuation_sessions`), a top-level `valuation_summary` including
     `ending_carried_forward`, and an additive per-quarter `valuation` block in
     `discovery_summary.py` (without changing `coverage` keys).
   - `research_loop.py` plumbs the frozen `valuation_policy_version` through
     `_evidence_runs` and `_formal_runs` for both candidate and equal-weight
     benchmark, records additive `provenance.valuation_policy_version` on v1 and
     formal v2 receipts whenever the policy is non-legacy, and includes additive
     `runs.<side>.valuation_summary` on formal run records (while keeping
     research-side `round status` / `round detail` allowlists curve- and
     ledger-free).
   - Formal precheck and pre-read manifest gates preserve #50/#56/#63 allowance
     semantics: Discovery hard-condition/precheck failures or manifest-level
     trailing gaps (`missing_required_open`, `source_conflict`, `unknown_status`,
     or unexplained gaps) reject before reading with zero Validation uses;
     holdings-dependent trailing valuation gaps (missing authorized prior close,
     broken carry chain, late status notice) consume one counted use upon
     legitimate trailing read and report insufficient evidence; replaying an
     identical evaluation never consumes an additional use.

## Consequences

- Candidate and equal-weight benchmark ledgers remain continuously valued across
  confirmed full-day suspensions (including multi-day, cross-weekend,
  cross-quarter, corporate-action, and end-of-range suspensions) in both
  Discovery and formal trailing Validation, allowing full-period and quarterly
  return and drawdown evaluation under a frozen, auditable policy.
- Re-entry / resumption jumps appear solely on the resumption session when a
  trusted unadjusted close arrives; if a suspension persists through the end of
  a quarter or scoring range, `ending_carried_forward` explicitly discloses the
  carried price, source close date/evidence, adjustment trail, and elapsed
  trading sessions so book drawdown is never mistaken for an actual-close exit.
- Unexplained gaps, missing schema-3 status evidence, late notices,
  status/trade conflicts (including whole-session `partial_halt` rows carrying
  contradictory trade prices or positive volume), broken chains, resumed opens
  without closes, and ambiguous/missing/late/non-positive corporate actions fail
  closed with typed `valuation_gap_reason` values, and unresolved corporate
  action gaps persist even after exiting the position.
- Legacy `strict-close-v1` campaigns, schema-2 real boundaries, and historical
  receipts remain immutable and interpretable under their original rules.
