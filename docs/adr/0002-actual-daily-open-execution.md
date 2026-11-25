# ADR 0002 — Actual daily open execution for orders effective before the open

- **Status:** accepted (spec #56, ticket #57)
- **Date:** 2026-11-25

## Context

PrajnaTrade studies five ETFs on sealed daily bars. A `partial_halt` session is
one where the ETF was halted at the opening auction and resumed later that day.
The vendor's daily bar for such a day still has a real open — the first print
after the resume — but the schema-2 snapshot contract required that `open` be
`null`, because it could not be known at 09:30 and was treated as "no
executable 09:30 open". An order that was already effective before the open
therefore could never be filled on a halted-at-open day, and the formal
trailing pre-read gate reported every such day as a missing required price.
Spec #56 records a real 70/30 trailing run that returned `VALIDATION_DATA_GAP`
over 44 sessions with 45 such open gaps, producing no return result.

Two failure modes drove this decision:

1. Treating a known daily open as missing conflates three different situations:
   a trustworthy open after a resume, a confirmed whole-day no-trade, and an
   open whose value is genuinely unknown or disputed. Only the last two are
   price gaps.
2. Inferring an "executable 09:30 open" for a halted session would fabricate a
   price that never traded. The alternative is not to guess 09:30, but to use
   the day's actual daily open as the matching base price for an order that was
   already effective before the open.

## Decision

1. **Schema 3 binds every executable open to sealed evidence.** A real schema-3
   snapshot declares `raw_price_basis: "unadjusted"` and adds `open_source`,
   `status_evidence_id` and `open_conflict` to raw bars. An open is executable
   only with the price archive that produced it; the resumption notice is a
   separate evidence id that proves trading status only and must differ from the
   open's price source. A `partial_halt` row may carry the day's actual
   unadjusted daily open. Two sealed opens that disagree are stored as an
   `open_conflict` with `open: null` and are never silently resolved; a
   `suspended` row cannot carry one, because a confirmed no-trade day has no
   open to disagree about. The
   manifest adds a schema-3-only `execution_evidence` block that classifies every
   non-trading sealed session/symbol as `actual_open_after_halt`,
   `confirmed_no_trade`, `missing_required_open`, `source_conflict` or
   `unknown_status`; `gaps` keeps its historical shape so old consumers are
   unchanged.

2. **`actual-open-v1` matches at the actual daily open.** Under the opt-in
   execution rule, an order effective before the open fills at the sealed actual
   unadjusted daily open, including on a halted-at-open session whose bar carries
   that open and its price source. The 09:30 per-field availability cutoff does
   not gate that fill: a daily bar's actual open is only knowable after the
   close, and the fill is retrospective matching of an order that already
   existed. Signal inputs keep their own point-in-time cutoffs, and the daily
   receipt never needs to infer the first trade minute. A normal open and a
   resumed-halt open with the same daily open produce the same fill date, base
   price, quantity, fee and cash.

3. **The fill price is never a signal input.** The actual open is used only for
   matching; using a value published after the close to decide the same session's
   order would be lookahead. Orders remain generated from the declared history at
   the frozen decision cutoff (the prior session's open for the legacy path), and
   the daily open participates in no indicator, weight or valuation rule.
   Valuation keeps using confirmed unadjusted closes.

4. **Legacy stays immutable and new behaviour is opt-in.** Schema 1 and 2
   manifests keep their exact bytes; schema 2 continues to forbid an `open` on
   `partial_halt`. `next-open-v1` and `retry-initial-v1` keep their matching
   bodies and remain the default. A v1 receipt computed on a schema-3 snapshot
   or under `actual-open-v1` identifies the sealed snapshot schema version,
   execution rule version and `unadjusted_open` price basis; a legacy schema-1/2
   receipt under a legacy rule keeps its exact old shape, and receipts stored
   before this change are read under their recorded shape. A formal trailing
   receipt carries the same flat identity in its `provenance`, read from the
   sealed read receipt rather than from the availability `snapshot` block,
   which keeps its pre-#56 shape.

## Consequences

- A bare `{date, symbol}` gap is no longer the only way to describe a
  halted-at-open day: the snapshot distinguishes a trusted actual open from a
  confirmed no-trade and from an unknown or conflicted price.
- The formal trailing pre-read gate (#59) classifies the trailing segment from
  the sealed `execution_evidence` before it reads the payload. Trailing
  `actual_open_after_halt` and `confirmed_no_trade` rows are benign and explain
  their `(date, symbol)` gap; trailing `missing_required_open`, `source_conflict`
  and `unknown_status` rows reject `VALIDATION_DATA_GAP` with zero uses, naming
  the first date/symbol/classification, and so does a trailing gap that no
  evidence row classifies. The gate is metadata-only and never turns a
  confirmed no-trade day into a valuation input: a held symbol with no confirmed
  close remains a valuation gap in the counted run. Schema-1/2 manifests, and
  successors without the block, keep the legacy gaps-only gate exactly.
- Schema 2 remains the acquisition output of the checked-in script until the
  real re-acquisition (#35) is done separately; schema 3 is the new real
  contract a re-acquired snapshot must declare. No real Validation allowance is
  consumed by this change, and all acceptance tests remain synthetic.
- Under `actual-open-v1` a basket no longer fills all-or-nothing: every
  required symbol (a buy with `weight > ε`, a sell with `shares > ε`) matches
  separately at its own sealed executable open. Sells match before buys in
  frozen symbol order; buys are funded by initial available cash plus realized
  sell net, and a symbol without an executable open is recorded unfilled with a
  reason derived from its sealed row (`confirmed_no_trade`,
  `missing_required_open`, `unknown_trading_status`, `missing_bar`) while the
  other required symbols still fill.
- A *held* symbol without an executable open makes the day's total equity
  unknowable: its open would size the other holdings' targets, and excluding it
  would force sales of priced holdings the strategy never asked for. The whole
  day is then recorded as `not_filled` with no fills and zero costs, every
  required symbol in `unfilled` (the unpriced ones with their sealed-row reason,
  the priced ones as `equity_basis_unknown`), and the account unchanged. An
  entry basket (no holdings) keeps filling its priced symbols per symbol.
- A partially filled plan is recorded as `partially_filled` with
  `basket_complete: false` and marked not yet built in `position_evidence`;
  unfilled buy cash and unsold shares stay in the account. The attempt states
  are mirrored by `position_evidence.basket_build_state`: `completed`,
  `partial` (that attempt filled at least one symbol and left at least one
  required symbol unfilled), `unfilled` (that attempt produced no fill at all)
  or `none` (no positive target). `actual-open-v1` never retries automatically:
  a later strategy decision emits a new target, or the plan expires at the
  partition end. `next-open-v1` and `retry-initial-v1` keep the all-or-nothing
  attempt, their exact result keys and the legacy initial-entry retry.
- The shared matching primitive skips a symbol whose open is absent or not a
  positive price rather than trading at price 0, so an unfilled symbol cannot
  corrupt the ledger. Without a missing price its result is unchanged.
- A schema-3 successor of a schema-2 snapshot, protected Evaluation on schema 3,
  and dividend-correction successors of schema-3 snapshots are not supported by
  this decision.
