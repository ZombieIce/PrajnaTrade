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
   `open_conflict` with `open: null` and are never silently resolved. The
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
   before this change are read under their recorded shape.

## Consequences

- A bare `{date, symbol}` gap is no longer the only way to describe a
  halted-at-open day: the snapshot distinguishes a trusted actual open from a
  confirmed no-trade and from an unknown or conflicted price.
- Schema 2 remains the acquisition output until the real re-acquisition is done
  separately; no real Validation allowance is consumed by this change, and all
  acceptance tests remain synthetic.
- Per-symbol partial fills, sell-then-buy funding and the formal allocation of
  confirmed no-trade days are separate tickets; the current basket attempt stays
  all-or-nothing, so a required symbol without an executable open still leaves
  the whole attempt unfilled.
- A schema-3 successor of a schema-2 snapshot, protected Evaluation on schema 3,
  and dividend-correction successors of schema-3 snapshots are not supported by
  this decision.
