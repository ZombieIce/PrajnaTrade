# ADR 0003 — Carry-forward valuation for confirmed full-day suspended holdings

- **Status:** accepted (spec #68, ticket #69)
- **Date:** 2026-11-26
- **Supersedes (opt-in only):** the statements in [ADR 0002](0002-actual-daily-open-execution.md) (Decision 3 and Consequences) that a held symbol with a confirmed full-day no-trade session must always leave close valuation unavailable. Under the legacy default `strict-close-v1`, ADR 0002 remains unchanged; under the opt-in `carry-forward-suspended-v1`, confirmed full-day no-trade holdings carry forward their last authorized trusted unadjusted close.

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
   immutable legacy default) and
   `CARRY_FORWARD_SUSPENDED_VALUATION = "carry-forward-suspended-v1"`.
   `campaign create --valuation-policy` freezes the chosen policy into
   `execution_rules["valuation"]`, `evaluation_hash`, and `runtime_identity`
   (binding `trading_status.py` and `valuation_policy` into
   `runtime_dependencies`). Candidate and equal-weight benchmark runs share the
   exact frozen valuation policy, each accounting from its own holdings.
   Protected Evaluation rejects `carry-forward-suspended-v1` at plan creation
   without reading or debiting any protected partition.

2. **Per-symbol, per-holding, per-session carry-forward rule.**
   Under `carry-forward-suspended-v1`, at each scoring session's close cutoff
   (`23:59:59+08:00`):
   - **Empty position (`shares <= ε`):** never creates a holding valuation gap
     (`valuation_basis: "not_held"`), regardless of whether that symbol was
     suspended or had a price gap.
   - **Trusted confirmed close today:** resets the symbol's carry-forward anchor
     to today's unadjusted close, date, and source evidence, clears any prior
     chain break, and values the holding at `actual_close`.
   - **Confirmed full-day no-trade today** (sealed `suspended` row with all null
     OHLCV and no conflict, or real `partial_halt` with a whole-session halt
     window, zero/null volume, and null OHLC; timely status availability
     evidence by `23:59:59+08:00`, plus `status_evidence_id` on real snapshots):
     - If an unbroken trusted prior close anchor exists for the holding (and no
       unadjusted/unresolved corporate action affects it today), increment
       `carried_sessions` by one trading session (weekends and holidays never
       generate points or increment the count) and value the holding at
       `quantity * valuation_price` (`valuation_basis: "carried_forward_prior_close"`).
       The holding records `close: null` alongside `valuation_price` and a
       `carry_forward` audit block (`source_close_date`, `source_close_price`,
       `source_close_evidence`, `valuation_price`, `carried_sessions`,
       `adjustments`, `status_evidence`).
     - If no authorized prior close exists (`missing_authorized_prior_close`) or
       an earlier gap broke the chain (`carry_forward_chain_broken`), valuation
       stays missing and cannot jump across the gap.
     - Corporate actions affecting a carried-forward holding pass through the
       `_adjust_carried_basis_for_actions` seam; until evidenced adjustment rules
       apply (#70), any corporate action hitting a carried-forward holding fails
       closed (`suspended_corporate_action_unadjusted`) and breaks the chain.
   - **Any other missing/unconfirmed close** (missing bar, `unknown` status,
     notice published after `23:59:59+08:00`, `status_conflict` or whole-day
     halt with positive volume, or resumed `partial_halt` with missing close):
     invalidates the holding's carry-forward anchor, records a specific
     `valuation_gap_reason`, and leaves daily NAV and full-period maximum
     drawdown unavailable until a future session supplies a trusted close.
     Late evidence and resumption prices never backfill prior sessions.

3. **Boundary preservation on confirmed full-day suspensions.**
   `snapshot.py` accepts a confirmed full-day no-trade session (with valid
   availability and, on schema 3, `status_evidence_id` and no conflicts) as a
   valid complete boundary session in `common_range` and `--full-history`
   `authorized_history`. Missing, unknown, unexplained, or conflicting tail rows
   remain incomplete. Frozen authorized, warmup, development, trailing, and
   scoring boundaries are never silently shortened when a confirmed suspension
   lands on a quarter end, scoring end, or authorized end date.

4. **Strict separation from signals, fills, and warmup.**
   Carried-forward valuation prices never enter `open_prices`, `open_equity`,
   `derived` / `history_derived`, or the 61-observation warmup count. A
   confirmed full-day suspension still produces zero fills and zero sell
   proceeds for the suspended ETF; under `actual-open-v1`, a held suspended ETF
   still leaves open equity unknown (`equity_basis_unknown`) for equity-dependent
   orders while explicit exits of priced holdings still execute. Existing
   strategy `state["equity"]` visibility continues to read the known account NAV.

5. **Additive audit fields and legacy immutability.**
   Under `strict-close-v1`, every receipt, metric key set, equity-curve point,
   quarterly summary, and runtime hash stays byte-for-byte compatible with prior
   behavior. Under `carry-forward-suspended-v1`, runs emit additive per-day
   `valuation_basis`/`carried_forward_symbols`, additive metrics
   (`actual_close_sessions`, `carried_forward_sessions`,
   `missing_valuation_sessions`), a top-level `valuation_summary` including
   `ending_carried_forward`, and an additive per-quarter `valuation` block in
   `discovery_summary.py` (without changing `coverage` keys).

## Consequences

- Candidate and equal-weight benchmark ledgers remain continuously valued across
  confirmed full-day suspensions (including multi-day, cross-weekend,
  cross-quarter, and end-of-range suspensions), allowing full-period and
  quarterly return and drawdown computation under a frozen, auditable policy.
- Re-entry / resumption jumps appear solely on the resumption session when a
  trusted unadjusted close arrives; if a suspension persists through the end of
  a quarter or scoring range, `ending_carried_forward` explicitly discloses the
  carried price, source close date/evidence, and elapsed trading sessions so
  book drawdown is never mistaken for an actual-close exit.
- Unexplained gaps, late notices, conflicts, broken chains, resumed opens
  without closes, and unadjusted corporate actions still fail closed with typed
  `valuation_gap_reason` values.
- Ticket #70 builds directly on the `_adjust_carried_basis_for_actions` seam to
  adjust carried valuation basis for evidenced cash dividends and share
  adjustments during suspension, and Ticket #71 builds directly on the frozen
  policy identity and `valuation_summary` to plumb formal trailing Validation
  and allowance accounting.
