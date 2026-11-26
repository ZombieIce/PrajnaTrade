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
   `status_evidence_id`, `open_conflict` and `status_conflict` to raw bars. An open is executable
   only with the price archive that produced it; the resumption notice is a
   separate evidence id that proves trading status only and must differ from the
   open's price source. Evidence has roles, and the seal checks them:
   the evidence ID for an open or a conflicting quote must name a
   `price_archive` item, and `status_evidence_id` must be a string naming a
   halt/suspension/resumption notice
   (`halt`, `halt_notice`, `suspension_notice`, `resumption_notice`,
   `trading_status_notice`). An announcement therefore cannot be used as a
   price, nor a price archive as a status proof. A `partial_halt` row may carry
   the day's actual unadjusted daily open, but an actual open is a real trade:
   the row needs positive `volume` and a halt window that does not cover the
   whole session. The seal rejects incompatible quoted rows. A whole-day halt
   with positive reported volume is a status conflict even with a null open.
   A normal trading row with zero volume has no executable open. Two sealed
   opens that disagree are stored as an
   `open_conflict` with `open: null` and are never silently resolved; a
   `suspended` row cannot carry one, because a confirmed no-trade day has no
   open to disagree about, and an explicit `open_conflict: null` is absent
   rather than a conflict. The manifest adds a schema-3-only
   `execution_evidence` block that classifies non-trading rows and rows with
   a known execution gap as `actual_open_after_halt`, `confirmed_no_trade`,
   `missing_required_open`, `source_conflict` or `unknown_status`. That block is
   a pre-read classification, not a price record: each row carries the
   classification, date, symbol, status, halt/reason and evidence *references*
   (`open_source`, `status_evidence_id`, `conflict_sources`, `status_conflict`) only. No actual
   open and no conflict quote from any sealed partition appears in the manifest,
   so inspecting it before the counted read cannot expose a price. `gaps` keeps
   its historical shape so old consumers are unchanged.

2. **`actual-open-v1` matches at the actual daily open.** Under the opt-in
   execution rule, an order effective before the open fills at the sealed actual
   unadjusted daily open, including on a halted-at-open session whose bar carries
   that open and its price source. Neither trading status is gated by the
   open's publication time: a daily bar's actual open is only knowable after the
   close, and the fill is retrospective matching of an order that already
   existed. The predicate is therefore uniform — `trading` needs a valid open
   and positive volume; `partial_halt` additionally needs the sealed price
   source and a halt window that does not cover the session. The same open
   therefore fills the same basket whether the session is marked normal or
   halted, and the two samples produce the same fill date, base price,
   quantity, fee and cash. Signal inputs keep their own point-in-time cutoffs,
   and the daily receipt never needs to infer the first trade minute. The
   legacy `next-open-v1`/`retry-initial-v1` rules keep their 09:30 per-field
   availability cutoffs exactly.

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
  evidence explains only the gap kinds it proves: `actual_open_after_halt`
  explains its `raw_bars` gap, `confirmed_no_trade` explains its `raw_bars` and
  `derived` gaps, and `availability` or `corporate_actions` gaps are never
  explained by execution evidence. This scoping matters: a trusted halted open
  must not mask a same-day gap in another field, which would let a counted read
  proceed on incomplete data. Trailing `missing_required_open`,
  `source_conflict` and `unknown_status` rows reject `VALIDATION_DATA_GAP` with
  zero uses, naming the first date/symbol/classification, and so does a trailing
  gap that no evidence row explains for its kind; a blocking row suppresses only
  its own entries so they are not reported twice. The gate is metadata-only and
  never turns a confirmed no-trade day into a valuation input: a held symbol
  with no confirmed close remains a valuation gap in the counted run.
  A new schema-3 formal read requires the current execution classification
  policy. Reseal provisional schema-3 inputs before starting a new formal
  read. Schema 1 and 2 keep the legacy gaps-only gate. Existing receipts remain
  available for inspection, and replay of the same frozen identity keeps its
  original count.
- The acquisition script emits schema 3. `convert_raw_bars` keeps a resumed
  session's evidenced actual open when there is a usable vendor price and
  positive volume. A missing resume open remains `missing_required_open` at
  sealing. The halt window determines whether a `full_halt_until` notice covers
  the session. A confirmed whole-day halt is `suspended`. Conflicting positive
  vendor volume yields `unknown_status`, with the notice and archive references
  in the sealed manifest for formal preflight. `build_snapshot_input` derives
  total returns from the accepted closes. The 45-row real-data verification
  remains tracked in #35. Acceptance tests use synthetic fixtures.
- Under `actual-open-v1` a basket no longer fills all-or-nothing: every
  required symbol (a buy with `weight > ε`, a sell with `shares > ε`) matches
  separately at its own sealed executable open. Sells match before buys in
  frozen symbol order; buys are funded by initial available cash plus realized
  sell net, and a symbol without an executable open is recorded unfilled with a
  reason derived from its sealed row (`confirmed_no_trade`,
  `missing_required_open`, `unknown_trading_status`, `missing_bar`) while the
  other required symbols still fill.
- A held symbol without an executable open makes total equity unknown.
  Match explicit exits at their own usable opens. For a sole 100% target
  with an executable open, the available cash after those sales determines
  the buy quantity, including fees and slippage. The unknown holding adds
  nonnegative value to the target equity, so the cash cap determines the
  affordable quantity. Leave orders whose quantities still depend on
  total equity unfilled with `equity_basis_unknown`. Unpriced symbols keep
  their sealed-row reason, and the basket stays incomplete while any
  required symbol is unfilled.
- A partially filled plan is recorded as `partially_filled` with
  `basket_complete: false` and marked not yet built in `position_evidence`;
  unfilled buy cash and unsold shares stay in the account. The attempt states
  are mirrored by `position_evidence.basket_build_state`: `completed`,
  `partial` (that attempt filled at least one symbol and left at least one
  required symbol unfilled), `unfilled` (that attempt produced no fill at all)
  or `none` (no positive target). A formal Validation receipt publishes each
  side's build state in `checks.position` when the run carries it, and the
  formal position prerequisite is unmet while either side's latest build is
  `partial` or `unfilled`: four of five required fills are accounted honestly,
  but a plan that required the whole basket is not a satisfied scoring premise.
  `actual-open-v1` never retries automatically: a later strategy decision emits
  a new target, or the plan expires at the partition end. `next-open-v1` and
  `retry-initial-v1` keep the all-or-nothing attempt, their exact result keys
  and the legacy initial-entry retry.
- The shared matching primitive skips a symbol whose open is absent or not a
  positive price rather than trading at price 0, so an unfilled symbol cannot
  corrupt the ledger. Without a missing price its result is unchanged.
- A schema-3 successor of a schema-2 snapshot, protected Evaluation on schema 3,
  and dividend-correction successors of schema-3 snapshots are not supported by
  this decision.
