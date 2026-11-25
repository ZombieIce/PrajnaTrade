# PrajnaTrade

PrajnaTrade is a local-first research CLI for a fixed portfolio universe:
ETFs `511260`, `518880`, `515080`, `513300`, `159612`, plus cash.

## Development

Start with the map before changing the code:

- **Tests:** [`docs/agents/testing.md`](docs/agents/testing.md) — how the suite is organized and run.
- **Architecture:** [`docs/agents/architecture.md`](docs/agents/architecture.md) — modules, trusted stores, CLI flows and the test map.
- **Standards:** [`CODING_STANDARDS.md`](CODING_STANDARDS.md) — review judgement calls; mechanical rules live in ruff/CI.
- **Worktrees:** [`docs/agents/worktrees.md`](docs/agents/worktrees.md) — parallel worktree workflow.

## Data format

CSV and Parquet inputs use one row per symbol and date with these columns:
`date,symbol,open,high,low,close,volume,price_basis`. Prices must consistently
use the documented `total_return` adjustment basis, and every row must declare
that basis so the loader can reject mixed or mismatched data. Missing opens or
closes are retained and reported rather than silently filled.
Timezone-aware timestamps are converted to Asia/Shanghai before extracting the
trading date; timezone-naive dates are used as supplied. Calendar dates follow
the same rule.

## First workflow

Create and activate a virtual environment, then install the package:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[test]"
```

Validate a legacy CSV/Parquet dataset before running that baseline mode:

```sh
prajnatrade data validate \
  --data bars.csv --price-basis total_return --data-kind real

prajnatrade baseline \
  --data bars.csv --price-basis total_return --data-kind real \
  --initial-cash 100000 --transaction-cost-bps 5 --slippage-bps 0 \
  --output results/baseline.json
```

The `--data` form is the legacy CSV/Parquet baseline. It retains the behavior
described below and does not read sealed snapshots.

`--data-kind` is required so synthetic fixtures cannot be mistaken for real
market evidence. The baseline uses equal initial weights and then holds. A
signal made from date T's close can only fill at a later available open. Missing
opens defer the whole pending target; missing closes are marked at the last
available close and reported. If no earlier close exists for a newly held ETF,
its executable open is used as the first mark and identified as
`mark_open_fallback`; subsequent missing closes use the last known mark.
Missing-symbol coverage is assessed against dates present in the input, not an
external exchange calendar. Without `--calendar`, `data validate` reports
missing weekdays within the observed date range as `inferred_weekdays`; exchange
holidays can therefore appear as possible gaps. For exact session validation,
pass `--calendar sessions.csv`; the CSV must have a `date` column listing
expected sessions for the period under review. The report then lists both
missing calendar dates and input dates outside the supplied calendar.

The baseline CLI reports total and yearly returns relative to an all-cash
benchmark. The Python backtest interface provides
`benchmark_relative_returns(result, benchmark)` for return differences
(strategy return minus benchmark return); both runs must use the same dataset
and sessions.

Dataset fingerprints preserve the full precision of floating-point values.

### Synthetic shared snapshots (MVP-01)

`snapshot seal` accepts version-1 **synthetic** JSON by default, plus the
version-2 **real** input described in the next subsection for the MVP-06
mechanism. It writes a shared, content-addressed snapshot outside any Git
repository. The synthetic path does not acquire or verify real market data. A
trusted operator prepares the input and store:

```sh
prajnatrade snapshot seal --input synthetic.json --store /trusted/snapshots
prajnatrade snapshot manifest --store /trusted/snapshots --id <snapshot-id>
prajnatrade snapshot read --store /trusted/snapshots --id <snapshot-id> --partition discovery
```

Different project directories can refer to the same snapshot ID and read the
same Discovery receipt. `read` rejects Validation and Final Holdout; their
actual contents are already sealed and fingerprinted, not merely reserved
dates. The CLI does not export sealed files or authorize protected evaluation.
The store is read-only by file permissions and each read verifies the requested
content's fingerprint. Those permissions and CLI checks are **not OS isolation**:
any process with the trusted operator's filesystem permissions can bypass them.
Protect the store with a separately enforced deployment boundary before using
untrusted agents; this ticket does not verify that boundary.

Set `discovery_start` in the seal input to reserve earlier common sessions for
indicator input. Use a trading date in the declared common range. The usual
partition split applies from that date. Discovery reads return the earlier
calendar and derived observations as `prior_history` with a fingerprint of the
authorized view. With `--as-of`, values unavailable at the cutoff remain gaps.

The input object has `schema_version: 1`, `data_kind: "synthetic"`,
`derivation_rule_version`, `sources`, `symbol_coverage`, and arrays `calendar`,
`raw_bars`, `corporate_actions`, and `derived`. `tests/test_snapshot_cli.py`
contains a complete generated example. Each source in `sources` has `source`,
`version`, `coverage_start`, `coverage_end`, `available_at`,
`availability_evidence`, and `collected_at`. `symbol_coverage` maps each of
`511260`, `518880`, `515080`, `513300`, and `159612` to an independently
declared `{"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}` history; do not infer
listing history from the first observed bar. The coverage declarations and
calendar are synthetic fixtures, not proof of actual listings or exchange days.

Every array entry includes `date`, timezone-aware ISO `available_at` and
`collected_at`, and `availability_evidence`. Use explicit `null` for unknown
availability or evidence; it is recorded as a gap, never replaced with the
collection time. Non-calendar entries include `symbol`. Raw bars require
`open`, `high`, `low`, `close`, `volume`, and `trading_status` (`trading`,
`suspended`, or `unknown`); derived rows require `value`. Missing numeric
values are explicit `null`, and unknown trading status is a gap. Corporate
actions use `type: "cash_dividend"` with
`cash_per_share`, `pay_date`, and `terms_evidence`, or
`type: "share_adjustment"` with `ratio` and `terms_evidence`; unknown terms
are explicit `null` and recorded as gaps. An empty actions array means the
source's declared coverage asserts no actions. Source-level availability and
collection evidence are required, not inferred.

The common start is the latest declared symbol/source coverage start, aligned
to the supplied calendar; the end is the last calendar session within all
coverage with complete raw/derived values and availability evidence for all
five ETFs and no incomplete action on that session. Intermediate gaps remain
visible. At least five sessions are required. The first `floor(3N/5)` sessions
are Discovery, the next `floor(N/5)` are Validation, and the rest are Final
Holdout. The manifest includes source metadata, coverage, gaps, per-source and
per-partition hashes, and the derivation version. The snapshot ID hashes the
canonical manifest, including all three partition fingerprints, not paths.
Changing fixed content produces a new ID; repeat sealing verifies existing
content rather than overwriting it.

### Real schema-version-2 snapshots (MVP-06)

`snapshot seal` also accepts `{"schema_version": 2, "data_kind": "real"}` input.
The [#24 result note](docs/research/real-snapshot-result.md) records the sealed
real snapshot, independent reconciliation and human acceptance. The test suite
uses synthetic fixtures. The sealer rejects a mismatched schema version and
data kind.

Real input adds a top-level `evidence` block and a `derivation` block:

- `evidence` has exactly the key `items`; each item has exactly `id`, `kind`,
  `reference`, `sha256` (64 lowercase hex characters), and timezone-aware
  `available_at`. The sealer validates structure only and never opens the
  referenced files; the manifest records the block.
- `derivation` has exactly `rule_version`, `method`, and `evidence`.
  `rule_version` must equal the top-level `derivation_rule_version`, `method`
  must be `cash_dividend_reinvested_at_ex_date_close`, and `evidence` is a
  non-empty string or a non-empty list of strings. The sealer does not
  recompute the total-return series; it records the declared rule and evidence.
- If `raw_price_basis` is present it must be `unadjusted`; the real manifest
  records `raw_price_basis: "unadjusted"`.

Real `raw_bars` rows keep the v1 fields and add timezone-aware
`open_available_at` and `close_available_at`. A `null` per-field timestamp is
allowed only where the corresponding price is absent (`partial_halt`'s
`open_available_at` still records the resume availability). `trading_status`
extends to `trading`, `partial_halt`, `suspended`, and `unknown`:

- `trading`: non-null open/high/low/close/volume, positive OHLC, non-negative
  volume, and both per-field timestamps present. The 09:30 open and the close
  are treated as available at those declared timestamps by market rule.
- `partial_halt` (schema 2): there is no executable 09:30 open, so `open` must
  be `null` and a `halt: {"start": ..., "end": ...}` window is required. `high`,
  `low`, `close`, and `volume` may describe the resumed session;
  `open_available_at` is the resume availability, and a present close needs
  `close_available_at`. The vendor's post-resume first trade must not be stored
  in `open`; schema 3 below replaces this rule with the evidenced actual daily
  open.
- `suspended`: open/high/low/close/volume are all `null`; `halt` is optional.
  This represents full-day halts such as `159612` on 2025-01-10 and 2025-01-24.
- `unknown`: nulls are allowed and the row is treated as a gap.

For any real row, `available_at` still needs the documented evidence, but it is
not the open or close availability; the per-field timestamps are authoritative.
That keeps a close-publication timestamp from gating a 09:30 fill, and keeps a
later close from being backdated.

Real `corporate_actions` rows keep the v1 fields, may add `record_date`, and
require non-null `cash_per_share` (cash dividend) or `ratio` (share
adjustment), `pay_date` for cash dividends, and non-empty `terms_evidence`.
Real `derived` rows must carry a non-null value on `trading` sessions and `null`
on `suspended` sessions. The real manifest adds `schema_version: 2`,
`data_kind: "real"`, `raw_price_basis`, `evidence`, and `derivation`; the
synthetic manifest is unchanged.

The 09:30 open is a market-rule assertion, not an archived quote: exchange rules
say continuous trading starts at 09:30, while the archived bar only proves the
day's OHLC after the close. A real snapshot must therefore report coverage and
gaps, and a real label is not proof of point-in-time availability. A real
`partial_halt` session with a confirmed close still counts toward the common
range (for example a window ending on 2026-11-20). Under schema 2 its missing
open and any suspended session remain listed gaps; under schema 3 an evidenced
actual open is not a gap. Both `seal` and `snapshot read` receipts report
`schema_version` and `data_kind`, so the baseline takes the kind from the sealed
receipt instead of assuming synthetic.

### Real schema-version-3 snapshots (actual daily open, #56)

`snapshot seal` also accepts `{"schema_version": 3, "data_kind": "real"}` input.
Schema 3 is the new real contract for research snapshots: it is what a
re-acquired snapshot must declare for actual-open execution, while the
checked-in acquisition script still emits schema 2 until the real
re-acquisition tracked by #35 (out of scope here), so no checked-in real
snapshot changes. A schema-2 manifest and every receipt stored under it keep
their exact historical bytes, shape and meaning. The test suite uses synthetic
fixtures. The [ADR](docs/adr/0002-actual-daily-open-execution.md) records why
the fill price is a matching base price and never a signal input.

Schema 3 keeps the schema-2 `evidence`, `derivation`, per-field availability and
corporate-action rules, requires `raw_price_basis: "unadjusted"` to be
**declared** (there is no absent default), and adds three raw-bar keys:

- `open_source: {"source": ..., "evidence_id": ...}` is required exactly when
  `open` is not `null`, on a normal `trading` session and on a `partial_halt`
  session alike. It names the sealed price archive (a non-empty vendor/feed
  name) and its `evidence.items[].id`, binding every executable open to its
  sealed price source. A row without an open cannot carry one.
- `status_evidence_id` is the `evidence.items[].id` of the official
  suspension/resumption notice. It is required for `partial_halt` and
  `suspended` rows and must differ from that row's `open_source.evidence_id`: a
  resumption notice proves trading status, never a price.
- `open_conflict: {"sources": [...]}` records two or more sealed vendor opens
  that disagree. It requires `open: null`, at least two entries with distinct
  non-empty `source` names, every `evidence_id` sealed, positive finite opens
  and at least two distinct open values. A conflict is never silently resolved.
  It may appear on a `trading` or `partial_halt` row: a `trading` row that
  carries one stores `open: null`, keeps high/low/close/volume and carries no
  halt window, so a disputed opening print is never forced into an executable
  open. A `suspended` row cannot carry one — a confirmed no-trade day has no
  open to disagree about.

A schema-3 `partial_halt` row **may** carry the day's actual unadjusted daily
open — the first print after a resume on a halted-at-open day — together with
its `open_source`. A halt window is still required, and a confirmed close still
counts toward the common range. `trading`, `suspended` and `unknown` keep their
schema-2 meaning, and schema 2 still rejects all three new keys and still
forbids an `open` on `partial_halt`.

The schema-3 manifest adds one additive, schema-3-only block,
`execution_evidence`:

```json
{"policy": {"price_basis": "unadjusted_open",
            "price_source": "raw_bars[].open_source -> evidence.items",
            "status_evidence": "raw_bars[].status_evidence_id -> evidence.items",
            "halted_open": "the day's actual unadjusted daily open for an order effective before the open"},
 "rows": [{"date": "...", "symbol": "159612", "trading_status": "partial_halt",
           "classification": "actual_open_after_halt",
           "halt": {"start": "...", "end": "..."},
           "status_evidence_id": "...", "open": 3.301,
           "open_source": {"source": "...", "evidence_id": "..."}}],
 "counts": {"actual_open_after_halt": 1, "confirmed_no_trade": 0,
            "missing_required_open": 0, "source_conflict": 0, "unknown_status": 0}}
```

It has one row per sealed session/symbol whose `trading_status` is not
`trading` or that carries an `open_conflict`, sorted by date and symbol, with
classification:

- `actual_open_after_halt`: `partial_halt` with a valid `open` and
  `open_source`.
- `confirmed_no_trade`: `suspended`, or `partial_halt` with `open: null`, no
  conflict, whose halt window covers the whole session (`start <= 09:30` and
  `15:00 <= end`).
- `missing_required_open`: `partial_halt` with `open: null`, no conflict and a
  halt that does not cover the session (`reason:
  "partial_halt_without_actual_open"`).
- `source_conflict`: the row carries `open_conflict`.
- `unknown_status`: `trading_status: "unknown"`.

`gaps` keeps its exact historical shape and population for every schema
version, so old consumers are unchanged: an evidenced actual open is no longer
declared as a raw-bar gap, while a confirmed no-trade day, a missing required
open and a source conflict still are. The formal trailing pre-read gate reads
`execution_evidence` when the sealed manifest claims schema 3 (see
[the gate](#formal-trailing-validation-protocol-v2)): a trailing
`actual_open_after_halt` or `confirmed_no_trade` row explains its gap, while
`missing_required_open`, `source_conflict` and `unknown_status` block the read
with zero uses. A trailing gap no evidence row classifies is still unexplained
and blocks. Schema-1/2 manifests, and successors without the block, keep the
legacy gaps-only gate exactly.

### Discovery snapshot equal-weight baseline (MVP-04)

The snapshot baseline reads only the sealed Discovery partition. It treats
`derived.value` as the total-return signal series, raw unadjusted opens as
execution prices, and raw unadjusted closes as NAV marks. A signal is emitted
on the first Discovery session with all five derived values available before
the next session's open; it
attempts the equal-weight buy once at the next session's open. If any ETF has a
missing, unconfirmed, or not-yet-available-by-09:30 open, the entire attempt
is recorded as not filled and is never deferred. Fees and slippage are fixed
basis-point inputs.

```sh
prajnatrade baseline \
  --store /trusted/snapshots --snapshot-id <snapshot-id> \
  --initial-cash 100000 --transaction-cost-bps 5 --slippage-bps 0 \
  --output results/snapshot-baseline.json
```

NAV uses unadjusted closes, cash, and dividend receivables. Cash dividends
accrue only for shares held through the ex-date and transfer to cash on the
declared pay date; dividend terms and share-adjustment ratios must be evidenced
by the ex-date open. Share-adjustment ratios change held quantities before
trading on their effective dates. A missing, unconfirmed, unavailable-by-end-
of-day, or non-trading close for a held ETF, or unresolved held corporate
action terms, makes that day's NAV unknown and is listed as a gap rather than
filled from a stale price or guessed terms. The output records the snapshot
and Discovery content hashes, price roles, signal/fill details, costs, and
gaps for reproducibility. This interface accepts both synthetic and real sealed
snapshots. The #24 result note and its signed review document the real baseline
and the isolation evidence for the reconciliation.

For a schema-v2 real snapshot the output carries `data_kind: "real"` from the
sealed receipt. Execution compares `raw_bars.open_available_at` with the 09:30
cutoff and does not use `available_at`; a `trading` row needs a valid open and
volume, and `partial_halt`/`suspended` rows are never executable. If any
required open is missing or unconfirmed, the single attempt is recorded as
`not_filled` and is never deferred. Valuation compares `close_available_at`
with the end-of-day cutoff (`available_at` is the fallback only for v1 synthetic
rows). A held ETF that is `partial_halt` with a confirmed close is marked at
that close; a held `partial_halt` or `suspended` row with no confirmed close
makes that NAV `null`, with a `missing_or_unconfirmed_unadjusted_close` gap
that also records the trading status and halt window. A stale close is never
carried. Full-day halts therefore produce unknown-NAV days, partial halts
produce no executable 09:30 open, and the one-time buy rule still applies.

The legacy `research` and validation commands do not consume sealed snapshots
or inherit their checks. Campaigns created before this change may have an
incompatible fingerprint and must be recreated rather than resumed or
validated.

### Cash dividend correction successors (MVP-15)

The trusted operator registers a supplier's changed **CNY per-unit cash
dividend amount**, reviews exchange or fund-manager evidence, then seals a
successor. This workflow inherits MVP-01's synthetic-only scope: the notices
are explicitly synthetic and no actual ETF announcement is fetched or verified.
The CLI validates the submitted evidence and records the operator's filing; it
does not authenticate a publisher or inspect a supplier archive.

```sh
prajnatrade snapshot correction register --store /trusted/snapshots --input observation.json
prajnatrade snapshot correction verify --store /trusted/snapshots --proposal-id <proposal-id> --input notice.json
prajnatrade snapshot correction successor --store /trusted/snapshots --proposal-id <proposal-id> --revision-id <revision-id>
prajnatrade snapshot read --store /trusted/snapshots --id <successor-id> --partition discovery --as-of 2024-01-04T15:00:00+08:00
```

An observation has `schema_version: 1`, `predecessor_id`, `symbol`, `ex_date`,
`currency: "CNY"`, `old_amount`, `new_amount`, and `supplier` with `source`,
`archive_reference`, and timezone-aware `observed_at`. Registration records
`pending_verification` and changes no snapshot. For a Discovery date, the CLI
checks the old amount against the unique cash dividend for that ETF and ex-date.
For protected dates, the trusted operator must check the old value and event
against an independently archived supplier record and provide its reference;
the CLI deliberately does not compare it with a sealed payload.

The notice has `kind: "synthetic"`, `authority` (`exchange` or `fund_manager`),
`issuer`, `url` (HTTPS under `.invalid`), `reference`, matching `symbol`,
`ex_date`, `currency` and `amount`, and timezone-aware `available_at` and
`collected_at`. `available_at` is when the amount was originally public, not
when a duplicate notice appeared or when the operator collected it. The CLI
records actual `filed_at` in UTC. The same ETF, ex-date, currency, old/new
amounts and original official availability instant identify one revision.
Repeating a notice returns the original revision and filing time; additional
reviewed sources are retained separately in the trusted journal.

The successor manifest binds the predecessor ID and verified revision into a
new content ID. It keeps original whole-snapshot hashes and gaps under
`base_content_hashes` and `base_gaps`. Validation and Final Holdout retain
their original descriptors and fingerprints; the correction workflow never
opens or copies their payloads, grants evaluation access, or changes any
query budget. A correction inside one of those ranges is recorded as lineage
only, with no protected action data in the research receipt.

For Discovery, `--as-of` uses official availability, not filing time: a
synthetic 0.20 → 0.18 notice published after the ex-date leaves the original
amount in an ex-date reconstruction; a notice already public at that time
reconstructs 0.18 without claiming that the operator had filed or used it.
Reads omit rows dated after the cutoff's China-market day or lacking evidence
of availability by that instant. An amount correction does not make other
terms (such as payment date) available early. Once a correction applies,
the affected ETF's derived values are `null` rather than stale total-return
inputs. `partition_content_hash` identifies the fixed Discovery descriptor;
`content_hash` identifies only the requested historical projection.

## Research, validation, and reports

### External Discovery experiment protocol

The operator seals a snapshot, initializes a project, and creates a Campaign
against its Discovery partition. The protocol accepts synthetic and real
snapshots. Campaign creation reads Discovery
only, copies that partition into the project, and locks its snapshot ID,
evaluation rules, runtime fingerprints, and attempt/time budgets in the
project's `.prajnatrade` directory. Move that directory with the project to
preserve its identity; `--project` is just its location.

The [#26 acceptance record](docs/acceptance/issue-26-real-discovery-mock.md)
documents the real-data Mock run, budget reconciliation and interrupted
experiment recovery.

```sh
prajnatrade snapshot seal --input synthetic-snapshot.json --store ./sealed-data
prajnatrade project init --project ./strategy-A
prajnatrade campaign create --project ./strategy-A --store ./sealed-data \
  --snapshot-id <snapshot-id> --attempts 20 --max-seconds 3600
prajnatrade-agent project status --project ./strategy-A
prajnatrade-agent experiment submit --project ./strategy-A --input first.json
prajnatrade-agent experiment status --project ./strategy-A \
  --campaign-id <campaign-id> --submission-id first
```

`first.json` contains:

```json
{
  "schema_version": 1,
  "campaign_id": "<campaign-id>",
  "submission_id": "first",
  "hypothesis": "Equal weights match the Discovery benchmark.",
  "strategy": {"family": "equal_weight"},
  "parameters": {}
}
```

Supported declarative families are `equal_weight`, `momentum`, `trend_filter`
and `inverse_volatility`; parameters follow the existing research strategy
definitions. A follow-up uses a fresh `submission_id` and may include a
`parent_experiment_id` from the same Campaign. Submissions cannot override
Campaign settings. Submit/status/resume print versioned JSON on stdout; rejected and
failed requests return a nonzero exit code with a stable error and next action.
Results include only Discovery. Signals use the sealed total-return series and
must be available before the next open. Execution uses unadjusted opens and
applies the Campaign's all-or-none execution rule. NAV uses unadjusted closes,
cash, and dividend receivables. Missing required inputs produce signal gaps,
unfilled orders, or unavailable NAV rather than substituted prices. Each
experiment includes fills, order attempts, the NAV curve, fingerprints, and an
equal-weight buy-and-hold comparison over the same Discovery interval and
evaluation settings. An experiment result also echoes `snapshot_schema_version`
(additive #56 identity), so a stored receipt shows which sealed contract
produced it; results stored before this change have no such key.

Create a Campaign with `--discovery-warmup-sessions N` to freeze one scoring
start for every strategy version and the equal-weight benchmark. The first
`N` Discovery sessions supply indicator observations. At the next opening,
each account starts with the same initial cash and empty holdings. `N=0` scores
from the first Discovery opening. History-dependent strategies need sufficient
sealed pre-Discovery observations at that opening. The Campaign receipt reports
`history_policy.scoring_start` and the fingerprint of any sealed pre-Discovery
history used in the Campaign.

**New research Campaigns must be created with `--discovery-split 70/30`.**
The flag is opt-in only for backward compatibility: Campaigns, rounds and
receipts created without it are protocol v1 (`discovery-replay`) and remain
readable for historical compatibility, but they are not the protocol new
research uses. The flag freezes a leading-development split of the Campaign's
sealed Discovery interval before any experiment can run:

```sh
prajnatrade campaign create --project ./strategy-A --store ./sealed-data \
  --snapshot-id "$SNAPSHOT" --attempts 20 --max-seconds 3600 \
  --discovery-warmup-sessions 40 --discovery-split 70/30
```

The Campaign copy then holds only the development slice
(`floor(Discovery sessions * 70 / 100)` sessions), so every Discovery
experiment, baseline and comparison reads approved development data by
construction. The receipt reports `discovery_split` with the frozen rule, both
segments' real dates, session counts and content hashes, the sealed full
Discovery partition hash and a `split_hash`; `fingerprints.snapshot` is the
development slice hash that the experiment integrity check verifies. The
Campaign output and its stored evaluation also record `research_protocol`
(`"trailing-70-30"` with the flag, `"discovery-replay"` without); a Campaign
stored before #50 lacks the field, so readers use `.get("research_protocol",
"discovery-replay")`. Absent the flag, the Campaign and its stored records keep
their unchanged protocol-v1 shapes. A split Campaign that will open a round
also needs `--discovery-warmup-sessions`, because a round requires the frozen
scoring policy and the warmup must leave at least one development scoring
session.

In this mode, `strategy.history_requirements` can declare each required history
window. For example, a momentum strategy with `parameters.lookback: 20` can
declare `{"id": "momentum", "observations": 21, "mode": "valid_observations"}`
in that array. Each entry has a unique ID and requests a positive number of
completed, available total-return observations across the universe.
`valid_observations` looks back through gaps and `consecutive_sessions` requires
the immediately preceding sessions. When the array is omitted, the Campaign
derives the window from the strategy family and parameters. Equal weight needs
no observations. Submitted
requirements are canonicalized by ID and included in the strategy fingerprint
and frozen Candidate. An experiment that cannot satisfy its declaration at the
fixed scoring start returns `INSUFFICIENT_HISTORY`.

The same Campaign/submission ID and normalized content reuse one experiment;
changed content conflicts, and invalid or conflicting requests do not consume
an attempt. Accepted failures do consume one. Only
`EXECUTION_INTERRUPTED` can be resumed. A replacement client checks status
with the same project, Campaign, and submission key. If the receipt has
`error.next_action: resume`, it can run:

```sh
prajnatrade-agent experiment resume --project ./strategy-A \
  --campaign-id <campaign-id> --submission-id first
```

Resume restarts deterministic evaluation from the stored request under the
same experiment ID, not from an in-memory checkpoint. It acquires the same
Campaign lock as submit, does not debit another attempt, and accumulates
execution time. After a hard kill, time until interruption detection is
conservatively charged. Zero attempts remaining blocks new keys, not resumes;
zero time remaining blocks all execution without discarding earlier receipts.
Resume on a completed or terminally failed experiment returns its existing
receipt. A runtime fingerprint change fails execution rather than changing the
Campaign.

Success does not automatically create a Candidate. The operator must explicitly
freeze one successful experiment with its research conclusion:

```sh
prajnatrade candidate freeze --project ./strategy-A \
  --campaign-id <campaign-id> --submission-id first \
  --name candidate-1 --conclusion "Retain for independent review."
prajnatrade candidate status --project ./strategy-A --candidate-id <candidate-id>
```

The frozen record has an independent Candidate ID and preserves the exact
strategy, parameters, hypothesis, conclusion, snapshot/evaluation
fingerprints, runtime dependencies, and execution rules. It is immutable;
freezing is not validation, approval, or a release.

#### Container-isolated generated Python (#18/#27)

Generated Python is accepted only by a Campaign created with a container
executor. Build the runner image once, then create the Campaign:

```sh
DOCKER_HOST=unix://$HOME/.colima/default/docker.sock scripts/issue27-runner/build.sh
# prints the local image digest, e.g. sha256:<image-id>

prajnatrade campaign create --project ./strategy-A --store ./sealed-data \
  --snapshot-id <snapshot-id> --attempts 20 --max-seconds 3600 \
  --strategy-executor container --strategy-image prajnatrade-issue27-runner:local
```

`--strategy-executor` defaults to `none`, so existing declarative Campaigns are
unchanged. `--strategy-image` is required for `container`; the image digest is
resolved with `docker image inspect` at creation and frozen into the Campaign
evaluation config and runtime fingerprint. `DOCKER_HOST` (or
`PRAJNA_DOCKER_HOST`, which takes precedence) selects the daemon and
`PRAJNA_DOCKER_BIN` the Docker CLI binary. If Docker/Colima is unavailable, the
image cannot be resolved, or its digest later changes, execution fails; there
is no trusted-local fallback.

A Python submission uses the same envelope with `source` inside `strategy` and
the top-level `parameters`:

```json
{
  "schema_version": 1,
  "campaign_id": "<campaign-id>",
  "submission_id": "generated-1",
  "hypothesis": "A generated momentum variant can beat equal weight.",
  "strategy": {
    "family": "python",
    "source": "def strategy(history, state):\n    return {CASH: 1.0}\n",
    "parameter_schema": {
      "parameters": {
        "lookback": {"type": "integer", "default": 3, "min": 2, "max": 252, "step": 1}
      },
      "constraints": []
    }
  },
  "parameters": {"lookback": 3},
  "parent_experiment_id": "<optional parent experiment id>"
}
```

`parameter_schema` is optional. The trusted side compiles and normalizes the
source (1-20,000 characters), validates the schema and parameters, stores the
canonical source with `strategy.source_sha256`, and includes that hash in the
`strategy_and_parameters` fingerprint. A Python submission to a Campaign
without the container executor is rejected at admission without consuming an
attempt or execution time. Declarative families keep their existing schema,
replay/conflict, parent/child, budget, interruption and resume semantics; a
Python attempt consumes an attempt and elapsed time exactly like a declarative
one.

Each strategy call starts one fresh container with the verified boundary:

```
docker run --rm -i --network none --read-only --user 65534:65534 \
  --cap-drop ALL --security-opt no-new-privileges --pids-limit 64 \
  --memory 512m --cpus 1 --tmpfs /tmp:rw,size=16m \
  -e PYTHONDONTWRITEBYTECODE=1 <image> python -I -c <worker>
```

There are no host mounts, no container environment secrets and no network. The
worker builds the pandas history frame, exposes `ETF_SYMBOLS`, `CASH` and
`parameters`, executes the source, and calls `strategy(history, state)`; its
stdout is bounded. Before the strategy is allowed to start, the trusted
controller inspects the running container and fails the attempt unless the
actual mounts, network mode, read-only root, UID/GID, dropped capabilities,
`no-new-privileges`, memory, PID and CPU limits match. Returned weights are
validated again in the trusted process (finite, non-negative, allowed assets,
sum 1). A strategy that reads a host path or opens a socket cannot return a
usable result; container failures, timeouts, output overruns and invalid
weights fail the attempt with no trusted-local retry, while the attempt and
elapsed time remain charged.

Each experiment receipt includes `strategy.source_sha256` and an `executor`
object with:

- `kind`, `image.reference`, `image.digest` and the frozen `limits`;
- `invocation_count`, `invocations` and `evidence_sha256`;
- per invocation: `run_id`, `image_reference`, `image_digest`, `container_id`,
  the actual `inspect` summary (`mounts`, `network_mode`, `read_only`, `user`,
  `cap_drop`, `security_opt`, memory/PIDs/CPU/tmpfs), `input_sha256` and
  `input_bytes`, `output_sha256` and `output_bytes`, `wall_seconds`,
  `exit_code`, `source_started`, `strategy_started`, `result` and
  `weights_valid`.

No secrets are recorded (no environment values, credential paths or host mount
sources). Failed attempts persist the same evidence under `error.executor`.

The runner image is built from `scripts/issue27-runner/Dockerfile`:
`python:3.12-bookworm` pinned by digest, with `numpy==2.2.6` and
`pandas==2.2.3` (the #26 versions) and nothing else. The build needs network
for those wheels; execution is offline. The observed digest and Docker-gated
test commands are recorded in
[docs/acceptance/issue-27-python-executor.md](docs/acceptance/issue-27-python-executor.md).
The external model session, project-only relay and generated model strategy are
the remaining #27 integration; this change provides only the trusted execution
boundary and submission protocol and does not run a model. Frozen Python
candidates are not yet executed by protected evaluation.

Share [the `prajnatrade-research` Skill](.github/skills/prajnatrade-research/SKILL.md)
with both clients (register that Skill directory in clients that do not
discover `.github/skills`). Keep the objective, constraints, and saved request
files with the handoff so the replacement client can query before deciding.
The `prajnatrade-agent` entry point exposes only project status and experiment
operations; use `prajnatrade` for operator setup. Expose only the Agent tool
against the assigned project; the restricted entry point does not protect the
ledger from a client with arbitrary shell or filesystem access under the
operator's identity. OS-enforced isolation is tracked separately in
[#18](https://github.com/ZombieIce/PrajnaTrade/issues/18).

This synthetic protocol is separate from `research freeze`; the scripted
handoff tests are not evidence of real Agent research or deployment isolation.

Mock campaigns are deterministic orchestration checks, not AI research. They
use recorded decisions to refine a retained or investigated direction or
switch after a rejection or branch. The unparameterized equal-weight control
can only be retested, not tuned. Mock proposals remain limited to the supported
declarative strategy families unless isolated generated Python is requested. Campaigns
split the supplied development sessions into discovery and validation (75% /
25%). The separately stored final holdout interval is fixed at campaign start.
Campaigns require at least five development sessions and retain every attempt
in SQLite:

```sh
prajnatrade research run \
  --db research.sqlite --data development.csv --price-basis total_return \
  --data-kind real --holdout-start 2025-01-02 --holdout-end 2025-12-31 \
  --provider mock --seed 7 --experiments 20 --steps 5

prajnatrade research status --db research.sqlite --campaign-id <campaign-id>
prajnatrade research resume --db research.sqlite --campaign-id <campaign-id>
```

Paused campaigns and campaigns left in `running` state by an interrupted
process can be resumed from their persisted experiment history. Resume a
`running` campaign only after its original process has stopped; concurrent
workers on the same campaign are not supported. Time spent before a handled
interruption is recorded. After an abrupt termination, the time since the last
checkpoint is conservatively charged on resume, including downtime; a missing
or invalid checkpoint or a backwards system clock prevents unsafe recovery.
Older `running` campaigns use their last update time as the checkpoint. The
campaign time budget is checked between stages; it does not interrupt a backtest already
in progress. If provider token or cost budget is exhausted after a proposal is
saved, its pending backtest may finish without another provider call; if the
time budget is exhausted before that backtest, the attempt is marked failed.

Freeze a successful experiment before evaluating it on the independent
validation interval. Each campaign permits at most three frozen candidates.
The final holdout can be consumed only once, after
validation and an explicit confirmation. Validation requires the original
development dataset; holdout requires a separate file containing only the
frozen holdout interval. Both require the original price basis and data-kind
label. The development fingerprint is checked before both evaluations:

```sh
prajnatrade research freeze \
  --db research.sqlite --campaign-id <campaign-id> \
  --experiment-id <experiment-id> --name candidate-1 \
  --conclusion "Candidate and conclusions are frozen before validation."

prajnatrade research validate \
  --db research.sqlite --candidate-id <candidate-id> \
  --data development.csv --price-basis total_return --data-kind real

prajnatrade research holdout \
  --db research.sqlite --candidate-id <candidate-id> \
  --data holdout.csv --price-basis total_return --data-kind real \
  --confirmation "I CONFIRM THE CANDIDATE AND CONCLUSIONS ARE FROZEN"

prajnatrade research report \
  --db research.sqlite --campaign-id <campaign-id> \
  --output report.html
```

Reports include the recorded development source path, date range and row count.
Older campaigns that did not persist the latter fields report them as
unavailable; reporting does not reread the source file. Evaluation `data_hash`
identifies the evaluated interval, while `source_data_hash` identifies the full
input for that evaluation. Campaigns created with the earlier combined-data
format must be recreated; their holdout was already accessible during research.
Only the holdout dates, not the contents of the separate holdout file, are
committed at freeze time. Independently archive or precommit the holdout file
if protection against changing it after validation is required.

For an OpenAI-compatible provider, configure `PRAJNATRADE_OPENAI_ENDPOINT`,
`PRAJNATRADE_OPENAI_MODEL`, and the environment variable named by
`--api-key-env` (default `OPENAI_API_KEY`). Supply
`--input-cost-per-1k-tokens` and `--output-cost-per-1k-tokens`. Credentials are
read from the environment and are not persisted. Default `--budget-mode strict`
pauses before requests because a generic OpenAI-compatible endpoint does not
provide a verifiable cost ceiling. To accept the risk of exceeding the budget,
explicitly provide `--budget-mode best-effort --accept-budget-overrun-risk`
on run and on each resume. Best-effort reduces output tokens to fit the
estimated remaining budget, but input tokenization, provider usage and actual
charges can differ. The rates are estimates, not invoices.
Provider redirects are rejected, including same-origin redirects; configure the
final endpoint directly so credentials are never forwarded by a redirect.

Each request is reserved in SQLite before sending. `research status` and the
report show the ledger; an interrupted request or a response without complete
usage stays unconfirmed and blocks further calls. After checking the provider's
records, reconcile a request with actual usage and charges (the evidence text is
persisted; do not include credentials):

```sh
prajnatrade research reconcile-request \
  --db research.sqlite --campaign-id <campaign-id> --request-id <request-id> \
  --prompt-tokens 100 --completion-tokens 50 --actual-cost-usd 0.02 \
  --evidence "Provider invoice reference"
```

Reconciliation is a human assertion, not automated invoice verification.
Reported usage and configured rates remain estimated costs until reconciled;
subsequent requests are blocked when recorded costs or tokens exhaust the budget.

By default, strategies are declarative specifications (`equal_weight`,
`momentum`, `trend_filter`, and `inverse_volatility`); generated Python is not
executed. To request generated Python, explicitly pass
`--isolated-generated-python` to each Discovery `research run` or `resume`
command that executes it. Generated Python validation and holdout are refused
by the public CLI until their detailed results can be kept outside the research
side. The generated source and parameters are retained with the experiment.
Source defines `strategy(history, state)` and may use the globals
`ETF_SYMBOLS`, `CASH`, and `parameters`.

Generated Python runs only in the supported macOS sandbox worker. Its
preflight verifies denial of protected-file reads and writes, network access,
and process creation; it also applies the configured resource and I/O limits.
The worker shares the caller's OS UID, so this is a macOS sandbox-policy
boundary, not a separate-account or container boundary. Campaign status and
reports retain per-run IDs, identity, sandbox-policy hash, configured limits,
measured wall/input/output usage, start state, protected-input status, and
output destination. CPU time and peak resident memory are not measured yet.
Raw logs and per-invocation weights remain in the trusted controller, while
Discovery metrics follow the research feedback contract.
Unsupported platforms or unavailable limits stop execution before strategy
code runs. There is no trusted-local fallback. The current 2 GiB address-space
limit is not enforceable on every macOS setup, including the environment used
to validate this release; generated execution is refused there. These checks
are not a claim of isolation on other operating systems or deployment
boundaries.

Generated proposals must declare each numeric parameter in `parameter_schema`:
`{"parameters":{"window":{"type":"integer","default":10,"min":2,"max":20,"step":1}},"constraints":[]}`.
Types are `integer` or `number`; constraints compare a named parameter to a
number or another parameter using `<`, `<=`, `==`, `>=`, or `>`, for example
`{"left":"fast","op":"<","right":"slow"}`. Use
`{"parameters":{},"constraints":[],"no_tunable_parameters":true}` only for a
strategy with no parameters. A strategy with only non-numeric parameters is
inconclusive. This declaration cannot prove that
generated source has no hidden tuning knobs; review the source. Legacy Python
strategies without a schema are inconclusive, not robust by default.

At freeze, the application records the schema, legal one-at-a-time ±10%
perturbations rounded to declared steps, exclusions, and fixed thresholds in
the candidate fingerprint. Optional `joint_variants` entries contain a
`parameters` object with at least two changed parameter values and a `reason`;
their range, step and joint constraints are checked as part of the frozen plan.
At most two variants per parameter and ten in total are planned. A variant is
degraded if Sharpe falls by at least the greater of 0.30 or 25% of positive
original Sharpe, or absolute maximum drawdown rises by at least the greater of
3 percentage points or 25% of original drawdown. A result requires at least 80%
of planned variants to be evaluable and each declared parameter to be covered;
at least 80% of evaluable variants must be stable to pass. Invalid, failed, and
non-evaluable variants are retained. The plan is diagnostic only and does not
replace independent out-of-sample evaluation or significance testing.
An optional `research run --max-drawdown-limit 0.2` freezes a hard limit as a
positive proportion; the original and perturbation results record breaches.
No hard risk limit is assumed when the flag is omitted.

The Mock provider can emit deterministic Python when opted in, but a Mock
campaign is never evidence of model novelty or investable performance. Results
are research evidence, not investment advice or a guarantee of future returns.

### Research Loop and Evidence Loop (#43, #50)

Issue #43 is the default simulated-tracking workflow, extended by #50 with a
frozen 70/30 Discovery-to-Validation protocol. A **Research Loop** runs one
approved round over a Campaign: approve the round contract, lock successful
Discovery versions, score them as lightweight Validation (protocol v1) or
formal trailing-segment Validation (protocol v2), read the published receipt's
summary and ask specific questions of its published detail, record the feedback
that a revision links back to, then record one current human review state per
version from immutable decisions. An **Evidence Loop** is the independent
Shadow Live account of a version admitted to simulated tracking and the
observation review that follows.

Admission here is a **manual simulated-tracking decision**. It is not the
protected `evaluation`/Final Holdout flow and not a conditional `release`: it
never opens a sealed Validation/Final Holdout partition, never writes a query
debit, never creates a protected immutable release and never enables a release
conditionally. The protected flows and
[`#37`](#direction-bound-research-37) keep their own recorded protocols, and
existing plans, authorizations, debits, releases and receipts are still read
under their original rules.

#### 1. Open the round contract

The research lead approves the round contract before work starts. One round is
bound to one Campaign and one stable direction key.

```sh
prajnatrade round open --project ./strategy-A --store /trusted/evaluations \
  --campaign-id "$CAMPAIGN" --direction-key "etf-relative-strength" \
  --name "ETF relative strength" --authorization-id "$KICKOFF_APPROVAL" \
  --input round.json
```

`round.json` fixes everything the agent may work within:

```json
{
  "objective": "Prefer a version that keeps drawdown below the benchmark.",
  "constraints": ["Long-only.", "At most one rebalance per session."],
  "comparison": {
    "benchmark": "equal_weight_buy_and_hold",
    "statement": "Same Discovery interval, initial cash and costs."
  },
  "selection_rules": ["A lower unfilled-order count wins a tie."],
  "hard_conditions": [
    {"id": "drawdown", "metric": "max_drawdown", "operator": ">=", "value": -0.20}
  ],
  "disclosed_risks": ["Momentum may stay in cash for long stretches."],
  "scoring_policy": {
    "mode": "valid_observations",
    "data_availability": "next_open",
    "consecutive_sessions": false
  },
  "observation": {
    "basis": "rebalance_frequency",
    "minimum_opportunities": 2,
    "review_conditions": ["two consecutive opportunities with intent but no fill"]
  },
  "invalidation": {
    "max_unfilled_opportunities": 3,
    "max_abs_drawdown": 0.25,
    "max_data_gap_sessions": 2
  },
  "reviewer": "research-lead",
  "approval_reason": "The contract and disclosed risks are approved."
}
```

Hard-condition metrics are `net_return`, `max_drawdown`, `sharpe`, `turnover`,
`unfilled_orders` and `valued_sessions`; operators are `>=`, `<=`, `>`, `<` and
`==`. `scoring_policy.mode` is `valid_observations` or
`consecutive_sessions`, `data_availability` is `next_open`, and
`consecutive_sessions` must agree with the mode. The Campaign must already carry
its frozen Discovery scoring policy (`campaign create
--discovery-warmup-sessions`); otherwise the round is refused with
`SCORING_POLICY_NOT_FROZEN`. Replaying identical content returns the stored
round with `reused: true`; different content for the same Campaign is
`ROUND_ALREADY_OPENED`. Changing the objective or the evaluation standard opens
a new round; changed strategy experiments are charged to the Campaign budget.

**Protocol v2: a frozen 70/30 split.** A Campaign created with
`--discovery-split 70/30` (see the
[Campaign section](#external-discovery-experiment-protocol)) freezes a v2
round. Its split covers the Campaign's sealed **Discovery interval only** — never
the sealed Validation or Final Holdout partitions — and is recorded before any
experiment runs:

- `development_sessions = N * 70 // 100` (floor) of the Discovery sessions; the
  remainder goes to the trailing side. Both segments are non-empty,
  contiguous, disjoint and exhaustive.
- The Campaign copy holds only the development slice. `research_partition`
  records the frozen descriptor: the sealed full Discovery partition hash and
  session bounds, and each segment's `start`, `end`, `sessions` and
  `content_hash`, plus the canonical `split_hash` over the descriptor body.
- A v2 round input must also freeze this round's automatic Validation limit
  with `"validation_policy": {"auto_validation_limit": 2}`. The limit is a
  positive integer. A v2 campaign's round without it is refused with
  `VALIDATION_POLICY_REQUIRED`; supplying `validation_policy` to a v1 Campaign
  is `DISCOVERY_SPLIT_NOT_FROZEN`. The kickoff gains `protocol`
  (`{"schema_version": 1, "name": "trailing-70-30", "validation_kind":
  "trailing_validation"}`), the frozen `research_partition` descriptor and
  `validation_policy`; a v1 kickoff keeps its exact old shape and hash.
- Opening a v2 round computes one **reuse disclosure** (see
  [cross-round reuse disclosure](#cross-round-reuse-disclosure)) for every
  earlier round in the same project whose Campaign used the same sealed
  `snapshot_id` or the same frozen development content hash (a corrected
  successor): the earlier round's protocol and `match`, the interval its
  published receipt actually used, the intersection with this round's trailing
  segment, the counted Validation use history and the feedback recorded against
  that round's versions. It is stored once with the round record, outside
  `kickoff_hash`, so a replay returns the stored disclosure.

`round status` exposes the same split to the operator and the agent:
top-level `protocol` (a v1 round defaults to `discovery-replay`),
`research_partition` and `validation_policy` for v2, and per-version `protocol`
and `research_partition` on every locked submission. That is how the operator
verifies the development dates to research within and the trailing dates that
will be scored without opening any payload. `round validate` on a v2 round runs
the [formal trailing-segment Validation](#formal-trailing-validation-protocol-v2);
it never falls back to the v1 full-interval replay.

`round status` is granted to the agent and reads the project only:

```sh
prajnatrade-agent round status --project ./strategy-A [--round-id "$ROUND"]
```

Without `--round-id` it lists every round. Each round reports `status` (`open`
or `closed`), the approved kickoff, the budget, and for each locked submission
its Validation summary, the recorded detail accesses and feedback, the V2
`revision_of`/`feedback_ids` links, current `review_state`, the return
instructions recorded when a version was returned, the recommended and
confirmed observation and a tracking summary, plus any closure. A protocol-v2
round also reports the frozen split, the automatic Validation limit and use
counters, the recorded manual admissions (IDs, version, granted-at and derived
use state), `lifecycle` and the `reuse_disclosure` stored at open. The round
kickoff keeps the authorizing reviewer and approval reason; the agent view never
exposes a per-version review decision's reviewer or reason, a manual
admission's reviewer or reason, any ledger row, ledger price or per-session
curve, `query_debited`/`opened_protected_source` internals, sealed payloads or
protected outcomes. The deliberate v2 grant is the receipt **summary** —
return, drawdown, costs, fills and the verdict reason — while legacy v1
summaries keep their original shape.

#### 2. Lock a version for review

After a successful Discovery experiment the agent submits the version with its
testable question and the condition that would change the choice.

```sh
prajnatrade-agent round submit --project ./strategy-A --round-id "$ROUND" \
  --submission-id v1 --input submit.json
```

```json
{
  "experiment_id": "exp-...",
  "change": "Replace the 20-session lookback with a 60-session one.",
  "question": "Does the slower signal reduce drawdown without losing return?",
  "expected_change": "Fewer rebalances and a smaller drawdown.",
  "revisit_condition": "A lower return without the drawdown improvement.",
  "risks": ["A slow signal can miss a regime change."],
  "related_experiment_ids": ["exp-..."]
}
```

`revisit_condition` is required for every submission, and the experiment must be
a successful attempt in the round's Campaign. The receipt locks the strategy
(with parameters and any generated source), snapshot, Discovery evaluation,
execution rules, costs, protocol, signal window and frozen scoring policy into
one content-addressed `version_id`; `locked` repeats those bindings (for a v2
round it also records the round `protocol` and the frozen `research_partition`,
so the version traces to the partition protocol it will be scored under). The
same `submission_id` with the same content returns the stored version with
`reused: true`; the same ID with different content is `SUBMISSION_CONFLICT`; the
same content under another submission ID resolves to the same `version_id`.

A revision links its provenance without changing the version identity. The
optional `revision_of` names a prior version already locked in this round
(never the version being submitted, `REVISION_NOT_FOUND`/`REVISION_CONFLICT`),
and `feedback_ids` names feedback recorded on prior versions of this round
(never another round or the version being locked, `FEEDBACK_LINK_INVALID`). Both
are stored in the submission body and shown by `round status`; they are
deliberately excluded from `version_id`, so a V2 experiment submits to the same
content address with or without the link. The ordinary idempotency and conflict
rules compare them while they are part of the submission content.

#### 3. Lightweight Validation (no protected allowance)

```sh
prajnatrade round validate --project ./strategy-A --store /trusted/evaluations \
  --snapshot-store /trusted/snapshots --round-id "$ROUND" \
  --version-id "$VERSION"
```

`round validate` follows the protocol recorded in the round kickoff. A
**protocol v1** round (`discovery-replay`) uses the full-interval Discovery
replay described in this section. A **protocol v2** round
(`trailing_validation`) uses the
[formal trailing-segment Validation](#formal-trailing-validation-protocol-v2)
below; it never falls back to the v1 replay.

Protocol v1 replays the locked candidate and the equal-weight
benchmark over the Campaign's frozen Discovery interval, using the same sealed
snapshot, point-in-time cutoffs, execution rules, costs, initial cash and
scoring start as Discovery. It reuses the Discovery evidence and introduces no
new historical scoring window keyed to the submission date. It never opens a
protected partition and never writes a query debit; the receipt records
`opened_protected_source: false` and `query_debited: false`.

The replay must match Discovery on the scoring interval, scoreable sessions,
fills and ending cash within the `1e-9` tolerance. The receipt's check groups
are `data_availability`, `scoring_start`, `coverage`, `position`, `costs`,
`replay` and `hard_conditions`. A receipt created after the actual-open work
computed on a schema-3 snapshot or under `actual-open-v1` also carries an
additive `provenance` block naming the sealed input and the frozen execution
protocol: `snapshot_id`, `snapshot_schema_version`, `execution_rule_version`
and `price_basis: "unadjusted_open"`. A legacy schema-1/2 receipt under a
legacy execution rule keeps its exact old shape (no `provenance` block), and so
does a receipt stored before this change. The verdict is:

| `outcome` | `reason` | meaning |
| --- | --- | --- |
| `passed` | `null` | Every check matched and every hard condition was met. |
| `failed` | `HARD_CONDITION_NOT_MET` | At least one round hard condition failed; the version cannot be approved. |
| `insufficient_evidence` | `REPLAY_MISMATCH` | The replay is not byte-for-value equal to Discovery; a platform or data change needs `round revalidate`. |
| `insufficient_evidence` | `SCORING_START_INSUFFICIENT` | The frozen scoring start is not ready for the candidate or the benchmark. |
| `insufficient_evidence` | `COVERAGE_INSUFFICIENT` | Signal/valuation gaps or an incomplete scoring interval; `checks.data_availability` names the exact missing requirement. |
| `insufficient_evidence` | `POSITION_EVIDENCE_INSUFFICIENT` | The account never established the preregistered position and did not explicitly target cash. |
| `insufficient_evidence` | `COST_EVIDENCE_INSUFFICIENT` | Replayed fees or slippage do not match Discovery. |
| `insufficient_evidence` | `METRICS_NOT_READY` | A hard-condition metric is unavailable; the condition records `status: unavailable`. |

The position-evidence states stay distinct: no usable indicator/valuation
input is `metrics_not_ready` (reported through the coverage checks), an
explicit cash target is `intentional_cash` (valid position evidence), and a
candidate whose orders were intended but never filled reports
`initial_position_not_established` (`POSITION_EVIDENCE_INSUFFICIENT`, not a
missing metric). A candidate that established its first position reports
`initial_position_established`.

A completed receipt is published to the project with the research-visible
allowlist and kept write-once in the trusted store. Replaying the same
validation identity returns the stored receipt with `reused: true`. If the
process is interrupted, the retry keeps the same identity, records a new
`attempt` row and reports `attempts`/`resumed: true`; it does not spend a
protected allowance or a new attempt.

##### Formal trailing Validation (protocol v2)

A v2 round scores the locked version and the equal-weight benchmark on the
**trailing segment only**. The development segment is the prehistory, so the
first trailing session can score immediately; the candidate and the benchmark
each open with the frozen `initial_cash` and the same costs, execution rules,
signal window and locked `history_requirements` as Discovery. Nothing after the
decision date is visible to that decision, and no protected partition is opened
(`opened_protected_source: false`, `query_debited: false`).

The trusted read is counted once per locked version. `round validate`:

1. **Pre-read gate (zero count).** Reads only the sealed `manifest.json`
   metadata, never the Discovery payload, and refuses before any count when the
   frozen research interval does not match the sealed Discovery descriptor.
   - On a schema-3 manifest that carries the `execution_evidence` block, the
     trailing `actual_open_after_halt` and `confirmed_no_trade` rows are benign
     and explain their `(date, symbol)`: a trusted halted open and a proven
     no-trade day do not block the read. A trailing `missing_required_open`,
     `source_conflict` or `unknown_status` row blocks, and so does any trailing
     `raw_bars`, `derived`, `corporate_actions` or `availability` gap whose
     `(date, symbol)` no evidence row classifies (an unexplained gap).
   - Otherwise (schema 1/2, or a successor manifest without the block) any gap
     in those four kinds inside the trailing date range blocks, exactly as
     before.
   The rejection is `VALIDATION_DATA_GAP` (exit 2, `counted: false`,
   `next_action: "inspect"`) with `trailing`, `allowance`, the blocking
   `execution_evidence` rows as sealed (date, symbol, trading status,
   classification, reason, halt and evidence ids) and the per-kind `gaps` that
   stayed unexplained; the message names the first blocking date, symbol and
   classification. The ledger stays empty and the payload is not read. The gate
   is a read gate only: valuation still follows the confirmed-close rules, so a
   held `confirmed_no_trade` symbol with no close remains a valuation gap and
   the counted run can end `insufficient_evidence`/`COVERAGE_INSUFFICIENT`.
2. **Atomic reservation (one use per version).** Under the round lock and one
   SQLite write transaction, and before any attempt row, trusted write or sealed
   read: an existing use for this version is reused (resume or revalidation);
   otherwise an unused [manual admission](#manual-validation-admission-protocol-v2)
   bound to this version is consumed as a `manual` row; otherwise, when
   `count(automatic uses) < auto_validation_limit`, the next `automatic` row is
   inserted; otherwise the command is refused with
   `VALIDATION_ALLOWANCE_EXHAUSTED` (exit 2, `next_action: "admit"`, context
   `round_state: "awaiting_validation_admission"` and the allowance). That
   refusal writes no attempt, no trusted record and reads nothing. Two versions
   racing the last slot serialize: exactly one reserves the use and reads.
3. **One sealed read.** The Discovery payload is read once with the Campaign's
   frozen `as_of`. Its data kind, sealed full Discovery partition hash and
   prior-history hash must match the frozen record, and recomputing
   `snapshot.discovery_split` from the same projection must reproduce the frozen
   development and trailing slice hashes. A post-read mismatch is
   `VALIDATION_PARTITION_MISMATCH` (exit 2) and the reserved use stays counted.
4. **Trailing-only scoring.** The candidate and benchmark run over the trailing
   segment from its first session; every trailing session is expected to be
   scored (a valuation or signal gap is `COVERAGE_INSUFFICIENT`).

The formal receipt is write-once in the trusted store and published to the
project with the additive allowlist. Its fields are `schema_version`,
`round_id`, `version_id`, `validation_id`, `status`, `validation_kind:
"trailing_validation"`, `outcome`, `reason`, `protocol` (split hash, boundary
rule, research interval), `development` and `trailing` (start, end, sessions,
content hash), `scoring` (start, end, sessions, valued sessions), `warmup`
(source, development bounds, prior-history hash, first ready date),
`provenance` (snapshot, both slice hashes, as-of, round/version/submission/
experiment, plus the additive #56 identity `snapshot_schema_version`,
`execution_rule_version` and `price_basis: "unadjusted_open"`), `usage` (kind,
sequence, use ID, admission ID), `summary`
(scoring, candidate, benchmark, delta, verdict), `metrics`, `checks`, `runs`
(candidate and benchmark records with fills and metrics), `observation`,
`opened_protected_source`, `query_debited` and `receipt_hash`. The `usage` block
is deterministic from the immutable use row, so a retry reproduces a
byte-identical receipt without consuming again. The three #56 identity keys are
flat additive members of the already-published `provenance` block; a receipt
stored before the actual-open work has no such keys and keeps its recorded shape
(readers use `.get`). `round validate` output adds an
`allowance` summary (`auto_limit`, `automatic_used`, `manual_used`,
`total_used`, `remaining`).

The checks are `data_availability`, `scoring_start`, `coverage`, `position`,
`segment` (the verified frozen descriptor, hashes and as-of) and
`hard_conditions`; there is no `replay` check and no `costs` check (costs are
reported in the summary). The verdict precedence is:

| `outcome` | `reason` | meaning |
| --- | --- | --- |
| `failed` | `HARD_CONDITION_NOT_MET` | At least one round hard condition failed. |
| `insufficient_evidence` | `SCORING_START_INSUFFICIENT` | The required history is not ready at the trailing first session. |
| `insufficient_evidence` | `COVERAGE_INSUFFICIENT` | Signal/valuation gaps or an incomplete trailing window. |
| `insufficient_evidence` | `POSITION_EVIDENCE_INSUFFICIENT` | The account neither established the preregistered position nor explicitly targeted cash. |
| `insufficient_evidence` | `METRICS_NOT_READY` | A hard-condition metric is unavailable. |
| `passed` | `null` | Every check holds and every hard condition is met. |

A `failed` or `insufficient_evidence` verdict is evidence, not a protocol
rejection: the command exits 0, the receipt records the reason and the reserved
use stays counted.

**Interruption and replay.** An interrupted run keeps its `running` attempt and
its use; a retry records the next attempt, reuses the same `validation_id` and
use, and completes with the identical receipt. A terminal replay returns the
stored receipt (`reused: true`) without reading or counting again. A known-gap
block, an exhausted allowance and a partition mismatch are protocol rejections
and write no receipt.

**Corrected successors.** A v2 `round revalidate` verifies that the successor
preserves the frozen development slice hash, data kind and schema, prior
history, research interval/session dates and as-of, then recomputes the
trailing candidate and benchmark with the same protocol, prehistory, costs and
thresholds. It records a new formal epoch with `evidence`,
`prior_validation_id`, `prior_receipt_hash`, `prior_outcome`,
`original_validation`, `recomputed_validation` and `affected` (changed
rows/sessions, NAV changes, fill changes, ending-equity delta and a summary
reason). The version's single use is kept (no new count), replaying the
revalidation returns the stored record, and `round validate` on that version is
`REVALIDATION_SUPERSEDED`. A successor that changed the development slice is
refused with `EVIDENCE_DEVELOPMENT_MISMATCH` (the other checks keep the
`EVIDENCE_*` family).

**Status.** For a v2 round, `round status` adds the per-round
`validation_allowance` (`auto_limit`, `automatic_used`, `manual_used`,
`total_used`, `remaining` and the `uses` rows with sequence, version, validation
ID, kind, admission ID and consumed-at), the recorded `admissions` (admission ID,
round, version, granted-at, the recorded comparison and selection impact, and
the derived `used`/`consumed_validation_id`; the reviewer identity and reason
stay out of the agent view) and `lifecycle` (`research`,
`awaiting_validation_admission` once every automatic slot is used, or
`converged` when the round is closed). A version's `validation` summary keeps
its legacy fields and adds `kind`, `protocol`, `development`, `trailing`,
`scoring`, `warmup`, `provenance`, `usage` and the receipt `summary` when the
stored receipt has them. Each submission also lists its recorded `details`
(access ID, question, records, recorded-at) and `feedback` rows, plus the
`revision_of`/`feedback_ids` it linked. The agent sees the summary numbers,
these counters, dates and the admission IDs; it never sees a sealed payload,
the per-session `curves`, ledger rows/prices, `query_debited`/
`opened_protected_source` internals, the review decision's reviewer/reason or
the admission's reviewer/reason.

##### Manual Validation admission (protocol v2)

When the automatic limit is used, the next automatic `round validate` pauses
before the sealed read: it rejects with `VALIDATION_ALLOWANCE_EXHAUSTED` (exit
2, `next_action: "admit"`, context `round_state:
"awaiting_validation_admission"` and the allowance) and writes no attempt,
trusted record or use row. Discovery does not pause: new experiments and new
locked versions are submitted while the round lifecycle is
`awaiting_validation_admission`.

The researcher decides between an existing passing version, closing with no
candidate, or one extra read:

```sh
prajnatrade round admit --project ./strategy-A --store /trusted/evaluations \
  --round-id "$ROUND" --version-id "$VERSION" \
  --reviewer "$REVIEWER" --reason "$ADMISSION_REASON" --input admit.json
```

```json
{
  "comparison": "Compare V2 with the passed V1 on the same trailing segment and costs.",
  "selection_impact": "If V2 improves the trailing return without a deeper drawdown, it replaces V1; otherwise V1 stays selected."
}
```

`round admit` is operator-only. It records one write-once, content-addressed
`research-loop-admissions/<admission_id>.json` and a project row:
`admission_id` addresses the immutable body (`schema_version`, round, version,
reviewer, reason, comparison, selection_impact); `granted_at` is stored
alongside it. The output is `{"schema_version", "status": "admitted", "reused",
"admission": {... "granted_at", "used", "consumed_validation_id"},
"allowance": {...}}`. `used`/`consumed_validation_id` are derived from the
validation-use ledger, never written into the admission. **Granting is not a
read:** it writes no use row and leaves `automatic_used`/`manual_used`/
`total_used` unchanged.

**The automatic allowance must be exhausted first.** A manual admission is the
researcher's extra read *after* the limit, never a way to bypass it: while
`automatic_used < auto_limit` (lifecycle `research`) `round admit` rejects with
`VALIDATION_ALLOWANCE_REMAINING` (exit 2, `next_action: "validate"`, the
allowance and `round_state: "research"`) and writes nothing. The researcher
must let the automatic slots be used, after which the lifecycle is
`awaiting_validation_admission` and the same request is accepted.

One admission is bound to one version. Identical request content replays the
stored admission (`reused: true`, including its original `granted_at`); a
different admission for the same version is `ADMISSION_CONFLICT`. A closed round
is `ROUND_CLOSED`, an ended version `VERSION_ENDED`, an approved version
`REVIEW_CONFLICT` and an unknown version `SUBMISSION_NOT_FOUND`. A version that
already has a counted or completed trailing read needs no admission and is
refused with `VALIDATION_ALREADY_COMPLETED`, so no grant is left stranded. A
protocol-v1 round has no automatic limit and refuses an admission with
`VALIDATION_POLICY_REQUIRED`. A crash after the trusted write converges on retry:
the stored grant is published and returned.

The admitted version's next `round validate` consumes the admission atomically
inside the same round-lock/SQLite reservation, before any attempt or read: it
inserts one `manual` use row (with `admission_id` and the next round sequence)
instead of an automatic row. The manual branch is checked before the automatic
one, so a granted manual read can never silently become automatic (the
reservation remains defensive about a grant recorded before this gate). A
different version can never consume it; a second validate of the same version
reuses the existing use and terminal receipt; an interrupted read keeps the
manual use. The formal receipt's `usage` block records
`{"kind": "manual", "admission_id": ..., "sequence": ..., "use_id": ...}`, and
`round status` shows `manual_used` and the use row's `kind`/`admission_id`
beside the automatic counters (`remaining` is always the automatic remainder).
With every automatic slot used, `lifecycle` stays
`awaiting_validation_admission` even after a manual read, exactly as frozen in
the round policy; it becomes `converged` only when the round closes.

##### Validation detail and feedback (protocol v2 and legacy)

The summary is the entry point; a specific question is answered by projecting
the **published** project receipt. `round detail` is granted to the agent and
the operator and takes no `--store`:

```sh
prajnatrade-agent round detail --project ./strategy-A --round-id "$ROUND" \
  --version-id "$VERSION" --input detail.json
```

```json
{"question": "Which trailing sessions carried the drawdown?",
 "records": ["summary", "checks.position", "provenance"]}
```

`records` is a fixed allowlist over the published receipt: `summary`,
`metrics.candidate`, `metrics.benchmark`, `checks.<group>` for every published
check group (`data_availability`, `scoring_start`, `coverage`, `position`,
`costs`, `replay`, `segment`, `hard_conditions`), `runs.candidate`,
`runs.benchmark`, `fills.candidate`, `fills.benchmark`, `observation`,
`protocol`, `provenance`, `usage`, `development`, `trailing`, `scoring`,
`warmup`, and `replay` for a legacy receipt. A record name outside the allowlist
is `DETAIL_RECORD_UNKNOWN`; an allowed name that this receipt does not publish
is `DETAIL_RECORD_UNAVAILABLE`; a version without a terminal receipt is
`VALIDATION_NOT_PASSED`. The resolution is never derived from the trusted
per-session `curves`, a sealed payload or a ledger.

`detail_access_id` is a canonical hash of the round, version, validation ID,
`receipt_hash`, question and normalized (sorted, de-duplicated) records. The
output is `{"schema_version", "status": "completed", "reused",
"detail_access_id", "round_id", "version_id", "validation_id", "receipt_hash",
"question", "records", "details", "recorded_at"}`. Identical requests replay the
stored resolution with `reused: true`; another question is another recorded
access. A detail query is **not** a Validation read: it writes no use row,
never consumes an admission and leaves the allowance unchanged. `round status`
lists each version's `details` as access ID, question, records and recorded-at
without duplicating the stored resolution.

`round feedback` links that evidence to what it changed:

```sh
prajnatrade-agent round feedback --project ./strategy-A --round-id "$ROUND" \
  --version-id "$VERSION" --input feedback.json
```

```json
{"impact": "The trailing drawdown says the slower lookback should be tried next.",
 "detail_access_id": "…", "next_experiment_id": "exp-…"}
```

Feedback requires a terminal receipt for that version. A supplied
`detail_access_id` must belong to this round, version and receipt
(`DETAIL_NOT_FOUND`); a supplied `next_experiment_id` must be another known
experiment of the same Campaign (`FEEDBACK_LINK_INVALID`). The record keeps
`feedback_id`, round, version, `validation_id`, `receipt_hash`, impact, both
links and `recorded_at`. `feedback_id` content-addresses the immutable body
(the timestamp is stored alongside), so identical content replays the stored
row with `reused: true`, and recording feedback is never a Validation read.
`round status` shows each version's `feedback` rows.

A V2 submission then cites the prior version and the feedback with
`revision_of` and `feedback_ids` (see [lock a version](#2-lock-a-version-for-review)):

```json
{"experiment_id": "exp-…", "change": "…", "question": "…",
 "expected_change": "…", "revisit_condition": "…", "risks": ["…"],
 "revision_of": "…V1 version_id…", "feedback_ids": ["…feedback_id…"]}
```

The links are provenance, not identity: they are validated, stored in the
submission body and shown in `round status`, but excluded from `version_id`.
V2 is scored by the same round protocol on the same trailing segment, benchmark,
initial cash, costs and hard-condition thresholds as V1.

##### Cross-round reuse disclosure

A Discovery interval may be reused by a new Campaign (one Campaign per round),
including a **corrected successor** whose re-sealed snapshot preserves the
approved development prehistory but changes trailing data. When a v2 round
opens, the kickoff records one top-level `reuse_disclosure` (outside
`kickoff_hash`) for every earlier round in the same project whose Campaign
either used the **same sealed `snapshot_id`** or froze the **same development
content hash** (`evaluation_json.discovery_split.development.content_hash`):

```json
{"snapshot_id": "…",
 "previous_rounds": [{"round_id": "…", "campaign_id": "…",
                      "protocol": "discovery-replay",
                      "match": "same_snapshot",
                      "used": {"start": "2024-01-04", "end": "2024-01-09"},
                      "overlap": {"start": "2024-01-07", "end": "2024-01-09"},
                      "automatic_used": 1, "manual_used": 0, "total_used": 1,
                      "uses": [{"version_id": "…", "validation_id": "…",
                                "kind": "automatic", "admission_id": null,
                                "sequence": 1}]}],
 "feedback": [{"feedback_id": "…", "round_id": "…", "version_id": "…",
               "impact": "…"}],
 "overlapping": true}
```

`match` is `"same_snapshot"` (the same sealed snapshot) or
`"same_development"` (a different, typically corrected successor, snapshot
whose frozen development slice is byte-identical). A round is listed once, with
the snapshot match taking precedence, so the two criteria never duplicate an
entry. The previously used interval comes from the prior round's published
receipt: a formal trailing receipt uses its `trailing` segment, and a legacy
receipt uses the **replayed scoring interval**
(`replay.candidate.start_date`/`end_date`, after the Campaign warmup).
`overlap` is the intersection with this round's trailing segment (or `null`).
Each entry also carries the prior round's counted Validation use history —
`automatic_used`, `manual_used`, `total_used` and the `uses` rows (version,
validation ID, `kind`, `admission_id`, round sequence) — so a reused interval
discloses which versions were already read; a protocol-v1 prior round has no
use ledger, so its counters are zero and `uses` is empty. `feedback` lists the
prior round's feedback rows. The value is computed once on the first open,
stored in the trusted round record and its project copy, and returned unchanged
on replay — even if another prior round opens meanwhile. If the trusted round
file exists without its project row (a crash between the two writes), the
stored disclosure is reused rather than re-derived. Protocol-v1 rounds get no
disclosure, and old receipts keep being read under their original protocol.

##### Traceability of the selection basis (#35, synthetic)

`round status` plus one `round detail` with `records: ["provenance"]` traces a
version without re-reading sealed data:

```sh
prajnatrade-agent round detail --project ./strategy-A --round-id "$ROUND" \
  --version-id "$VERSION" --input provenance.json
# {"question": "Show the full selection basis of V1.",
#  "records": ["provenance", "summary", "usage"]}
```

The chain is snapshot → round → version → experiment → locked protocol →
development and scoring dates → warmup source → usage/allowance → feedback →
review state → tracking. `round status` carries the round/protocol/split,
version, submission, experiment, `validation` (kind/protocol/development/
trailing/scoring/warmup/provenance/usage/summary), `details`, `feedback`,
`review_state` and `tracking`; a manual admission read appears as a `manual`
use row and a consumed admission; the published `provenance` block names the
snapshot, the frozen partition and both slice content hashes. This is a
synthetic mechanism check: it does not collect the real #35 receipt or claim
real-chain evidence.

After a corrected data source or a platform fix, use `round revalidate` when
the original selection reason still holds:

```sh
prajnatrade round revalidate --project ./strategy-A --store /trusted/evaluations \
  --snapshot-store /trusted/snapshots --round-id "$ROUND" \
  --version-id "$VERSION" --reason "Corrected official dividend evidence." \
  --evidence-snapshot-id "$CORRECTED_SNAPSHOT"
```

`--evidence-snapshot-id` names the sealed corrected successor snapshot
explicitly and is optional. Without it, the command uses the supplied snapshot
store's only valid successor of the locked snapshot; if no valid successor
exists it takes the platform-fix path and reproduces the frozen partition. A
successor must descend from the locked snapshot, keep the frozen data
kind/schema and Discovery interval, keep the frozen prior history, and be
readable at the recorded as-of cutoff. The command then recomputes the locked
candidate and the equal-weight benchmark over the corrected Discovery partition
with the same execution rules, costs, cash and scoring start.

Revalidation keeps the strategy identity (the same `version_id`) and publishes
a new validation epoch. The result keeps the prior receipt as history
(`previous_validation_id`, `previous_receipt_hash`, `previous_outcome`) and
records `evidence` (source snapshot, lineage, and whether the snapshot or
runtime changed), `original_discovery` and `recomputed_discovery` (the two
content-addressed runs with fills and ending cash) and `affected` (changed
rows/sessions, NAV and fill changes, ending-equity delta and a summary
reason). Replaying the same revalidation returns the stored record with
`reused: true`.

The locked strategy and every earlier receipt stay untouched. An approved
version may be revalidated: a corrected pass keeps the review state `approved`;
a failed or insufficient corrected receipt records the new receipt and raises a
concrete review trigger for the operator, without silently revoking the
approval or auto-pausing tracking. An `ended` version remains terminal and is
refused (`VERSION_ENDED`).

Corrected-evidence failures are typed rejections (exit 2, no new receipt):
`EVIDENCE_SNAPSHOT_NOT_FOUND` (the ID is not a sealed snapshot in the store or
its manifest is incomplete), `EVIDENCE_NOT_SUCCESSOR` (it does not descend from
the locked snapshot), `EVIDENCE_DATA_KIND_MISMATCH` (data kind or schema
differs from the frozen Campaign or prior snapshot),
`EVIDENCE_INTERVAL_MISMATCH` (the frozen Discovery interval changed or the
frozen scoring start is not a session in the corrected evidence),
`EVIDENCE_PRIOR_HISTORY_MISMATCH` (the frozen prior history changed),
`EVIDENCE_AS_OF_VIOLATION` (the corrected payload only became available after
the recorded point-in-time cutoff) and `EVIDENCE_INTEGRITY_VIOLATION` (the
sealed manifest or partition integrity check failed). If more than one valid
successor exists, pass `--evidence-snapshot-id` (`EVIDENCE_SNAPSHOT_AMBIGUOUS`).
A completed revalidation supersedes the locked replay, so `round validate` on
that version is refused with `REVALIDATION_SUPERSEDED`.

#### 4. Review decisions and the current state

Every version has exactly one current review state: `pending` (no decision yet),
`approved`, `returned` or `ended`. The reviewer records one immutable,
content-addressed decision; both `--reviewer` and `--reason` are required. The
platform never pre-fills the decision — a passed receipt is evidence, not an
approval, and a failed or insufficient receipt cannot be approved.

```sh
prajnatrade round review --project ./strategy-A --store /trusted/evaluations \
  --round-id "$ROUND" --version-id "$VERSION" --decision approve \
  --reviewer "$REVIEWER" --reason "$APPROVAL_REASON" \
  --input observation.json --calendar trading-calendar.json
```

`approve` requires a terminal `passed` receipt for exactly this version
(`VALIDATION_NOT_PASSED` otherwise) and the reviewer's explicit observation
confirmation:

```json
{
  "observation_sessions": 20,
  "accepted_risks": ["Momentum may stay in cash for long stretches."],
  "observation_reason": "The recommended term plus two extra sessions."
}
```

`observation_sessions` is a positive integer no greater than 252, and
`accepted_risks` must list every round `disclosed_risks` entry the reviewer
accepts; a missing one is `RISK_NOT_ACCEPTED`, and omitting the input is
`OBSERVATION_REQUIRED`. The approved decision binds the locked version,
submission, experiment, Validation receipt, question, revisit condition and the
confirmed observation. It is recorded immediately even when the market is
closed, and tracking starts at the **first eligible trading session after the
decision's Asia/Shanghai date** from the supplied approved calendar
(`CALENDAR_COVERAGE_MISSING` when the calendar does not cover it). The tracking
artifact is write-once with `workflow: manual_tracking` and
`simulated_tracking: true`, and exactly one `enable` event takes effect on that
session; `release status` shows `published` before it and `enabled` from it.

`return` sends the version back to research:

```sh
prajnatrade round review ... --decision return --input return.json
```

```json
{
  "change": "What V2 must change.",
  "comparison": "How V2 is compared with V1 under the same protocol.",
  "revisit_condition": "The result that would select this version again.",
  "next_submission_id": "v2"
}
```

The version's current state becomes `returned`, and its passed receipt stays
usable. V2 is an ordinary `round submit` with a new `submission_id`; `round
status` compares it with V1 under the same round and scoring policy. If V2 does
not improve and V1's receipt still applies, approve V1 again with a recorded
reason: the stored passed receipt is reused (`validation.reused: true`) instead
of being recomputed. Re-selection is a new review decision, not a new
Validation.

`end` closes one version terminally:

```sh
prajnatrade round review ... --decision end --input end.json
```

```json
{"detail": "Why this version is closed.", "continue_direction": true}
```

An `ended` version cannot be revalidated, re-approved or continued, but the
round stays open and a new version may be submitted for the same direction.

`round close` records an early or exhausted stop:

```sh
prajnatrade round close --project ./strategy-A --round-id "$ROUND" \
  --reviewer "$REVIEWER" --reason "$REASON" --input close.json
```

```json
{"stop_reason": "No remaining testable experiment could change the choice.",
 "selected_version": "v1", "next_experiment": null}
```

A close is allowed with Campaign budget remaining, after the automatic limit is
exhausted and while the lifecycle waits for a manual admission. It records
`candidate_selected` (whether `selected_version` is non-null) and the stop
reason; a selected version must be locked and approved. A v2 closure also
freezes `validation_allowance` (the allowance counters at the stop) and, when a
version is selected, a `selected_validation` block with its `validation_id`,
`receipt_hash`, `validation_kind` and `protocol`, so the closure can be traced
to the receipt it was selected on without a second read. A v1 closure keeps its
#43 body. A closed round refuses new submissions, admissions, validations,
revalidations and review decisions. The same closure replays by its stored
`closure_id` (a closure recorded before #50 keeps its exact legacy body and
identity); a different one is `ROUND_CLOSED`.

#### 5. Evidence Loop: observation, opportunities and pauses

An approved version is tracked in the existing Shadow Live ledger as its own
account; there is no second accounting engine. Tracking starts at the enable
event's effective signal session. The first decision point may use compliant
history before that session to initialise indicators, but nothing before
enable — simulated fills, holdings, P&L or opportunity counts — is forward
performance. If the required history is incomplete at the first decision point,
the ledger records `metrics_not_ready` with the specific missing requirement
rather than inventing an order. The ledger keeps three states distinct:
`metrics_not_ready` (a required input or trailing window is missing),
intentional cash (the inputs are ready and the strategy targets cash) and
ordered-unfilled (an order was intended but did not fill).

Each committed session records one **tracking opportunity** when the strategy's
normalized target weights changed: its signal and execution dates, order IDs,
and separately which orders are due, filled and unfilled. Orders produced only
by price drift under an unchanged target still settle, but they are not a
decision opportunity. An opportunity with intent but no fill still counts as
due. `round evidence status` reports, per
version, the enable date, the committed range, first signal and first fill
dates, `due_opportunities` / `filled_opportunities` / `unfilled_opportunities` /
`intent_without_fill` (and the longest consecutive run of intent-without-fill),
observation progress, `metrics_not_ready` and data-gap counts, any review
trigger, any pause and the version state (`tracking`, `review_due`,
`observation_extended`, `paused` or `ended`). `shadow report` and
`shadow compare` aggregate the same opportunity counts; `shadow compare` keeps
each requested version's `full_history` even when the common run is shorter.

The platform recommends an observation term from the candidate replay's
rebalance cadence:

```text
recommended_sessions = ceil(sessions / max(1, opportunities)) * minimum_opportunities
```

clamped to `[minimum_opportunities, 252]`, with a `basis` record. The researcher
confirms or adjusts it in the approval input. When the confirmed term is reached
and due opportunities meet `minimum_opportunities`, the version is reviewed. A
term reached with too few opportunities becomes `observation_extended`: tracking
continues and the reason is recorded. A preset invalidation condition (too many
unfilled opportunities, an absolute drawdown limit or a data gap) also triggers
human review; the platform never changes the verdict automatically.

`round evidence status` and `round evidence decide` are operator-only:

```sh
prajnatrade round evidence status --project ./strategy-A \
  --store /trusted/evaluations --ledger /trusted/shadow-ledger \
  --round-id "$ROUND" [--version-id "$VERSION"]

prajnatrade round evidence decide --project ./strategy-A \
  --store /trusted/evaluations --ledger /trusted/shadow-ledger \
  --round-id "$ROUND" --version-id "$VERSION" \
  --decision continue|pause|retire --reviewer "$REVIEWER" --reason "$REASON"
```

`continue` records the decision and keeps tracking (it resumes a manually
paused version); it is the human answer to a review trigger or an extended
term. `pause` stops only that version's new signals at its first uncommitted
after-close session, keeps pending orders and valuation, and records the pause
scope (default `version`) and reason. A data/execution/ledger distortion pauses
only the affected version's processing and is recorded as a scope pause on that
version; other versions keep committing. `retire` ends that version terminally,
keeps its artifact and full ledger history, marks its ledger progress ended and
stops later signals; the round and any other version continue.

Two versions may be approved independently and track in parallel. Each has its
own immutable tracking artifact, ledger account, enable date and full history.
`shadow compare` reports only the common consecutive forward interval and never
mixes the two accounts; V2's forward evidence begins at its own enable, and an
incomparable pair states why instead of dropping a version. Ending a version is
terminal for that version only.

```sh
prajnatrade shadow tick --ledger /trusted/shadow-ledger \
  --store /trusted/evaluations --calendar trading-calendar.json \
  --data "$DAILY_FEED" --corrections-store /trusted/snapshots
prajnatrade shadow compare --ledger /trusted/shadow-ledger \
  --data "$DAILY_FEED" --versions "$V1_RELEASE,$V2_RELEASE"
```

`round status` and `round evidence status` summarize stored records and
ledger-derived counts; the ledger remains the accounting source of truth, and
the agent CLI never sees it.

#### 6. Command statuses and exit codes

Every workflow command prints one versioned JSON document. Protocol rejections
use `{"schema_version", "status": "rejected", "error": {code, phase,
next_action, message}, ...context}` and exit 2; plain path/SQLite/value errors
print `error: ...` on stderr and exit 2.

| Command | Side | Success `status` | Evidence-bearing statuses (exit 0) | Rejection (exit 2) |
| --- | --- | --- | --- | --- |
| `round open` | operator | `opened` (v2 adds the stored `reuse_disclosure`) | — | `ROUND_ALREADY_OPENED`, `SCORING_POLICY_NOT_FROZEN`, `VALIDATION_POLICY_REQUIRED`, `DISCOVERY_SPLIT_NOT_FROZEN` |
| `round status` | operator, agent | `ok` (or `open`/`closed` for one round); per version adds `details` and `feedback` and v2 adds the receipt `summary`, `validation_allowance`, `validation_policy`, `admissions`, `lifecycle` and `reuse_disclosure` | — | `ROUND_NOT_FOUND` |
| `round submit` | agent | `submitted` (with the optional `revision_of`/`feedback_ids` provenance links) | `reused: true` replay | `ROUND_NOT_FOUND`, `ROUND_CLOSED`, `SUBMISSION_CONFLICT`, `EXPERIMENT_NOT_ELIGIBLE`, `REVISION_NOT_FOUND`, `REVISION_CONFLICT`, `FEEDBACK_LINK_INVALID` |
| `round validate` | operator | `completed` (v2 adds `validation_kind`/`protocol`/`development`/`trailing`/`scoring`/`warmup`/`provenance`/`usage`/`summary`/`allowance`) | `passed`, `failed`, `insufficient_evidence` (exit 0; a v2 `failed`/`insufficient_evidence` keeps its counted use) | `ROUND_NOT_FOUND`, `SUBMISSION_NOT_FOUND`, `ROUND_CLOSED`, `VERSION_ENDED`, `REVIEW_CONFLICT`, `REVALIDATION_SUPERSEDED`, `VALIDATION_DATA_GAP`, `VALIDATION_ALLOWANCE_EXHAUSTED`, `VALIDATION_PARTITION_MISMATCH` |
| `round revalidate` | operator | `revalidated` (v2 adds `validation_kind`/`evidence`/`original_validation`/`recomputed_validation`/`affected`) | `passed`, `failed`, `insufficient_evidence` | `ROUND_NOT_FOUND`, `SUBMISSION_NOT_FOUND`, `VALIDATION_NOT_PASSED`, `ROUND_CLOSED`, `VERSION_ENDED`, `REPLAY_MISMATCH`, `EVIDENCE_SNAPSHOT_NOT_FOUND`, `EVIDENCE_NOT_SUCCESSOR`, `EVIDENCE_DATA_KIND_MISMATCH`, `EVIDENCE_INTERVAL_MISMATCH`, `EVIDENCE_DEVELOPMENT_MISMATCH`, `EVIDENCE_PRIOR_HISTORY_MISMATCH`, `EVIDENCE_AS_OF_VIOLATION`, `EVIDENCE_INTEGRITY_VIOLATION`, `EVIDENCE_SNAPSHOT_AMBIGUOUS` |
| `round admit` | operator | `admitted` | `reused: true` replay (`admission.used` shows a consumed grant) | `ROUND_NOT_FOUND`, `SUBMISSION_NOT_FOUND`, `ROUND_CLOSED`, `VERSION_ENDED`, `REVIEW_CONFLICT`, `ADMISSION_CONFLICT`, `VALIDATION_ALREADY_COMPLETED`, `VALIDATION_POLICY_REQUIRED`, `VALIDATION_ALLOWANCE_REMAINING` |
| `round detail` | operator, agent | `completed` (`reused: true` for an identical request) | — | `ROUND_NOT_FOUND`, `SUBMISSION_NOT_FOUND`, `DETAIL_RECORD_UNKNOWN`, `DETAIL_RECORD_UNAVAILABLE`, `VALIDATION_NOT_PASSED` |
| `round feedback` | operator, agent | `recorded` (`reused: true` for identical content) | — | `ROUND_NOT_FOUND`, `SUBMISSION_NOT_FOUND`, `VALIDATION_NOT_PASSED`, `DETAIL_NOT_FOUND`, `FEEDBACK_LINK_INVALID` |
| `round review` | operator | `approved` / `returned` / `ended` | `reused: true` replay | `ROUND_NOT_FOUND`, `SUBMISSION_NOT_FOUND`, `ROUND_CLOSED`, `VALIDATION_NOT_PASSED`, `RISK_NOT_ACCEPTED`, `OBSERVATION_REQUIRED`, `REVIEW_CONFLICT`, `VERSION_ENDED`, `CALENDAR_COVERAGE_MISSING`, `RELEASE_NOT_FOUND`, `RELEASE_INTEGRITY_FAILED`, `INVALID_TRANSITION`, `ADMISSION_INVALID` |
| `round close` | operator | `closed` (v2 adds `validation_allowance` and, when selected, `selected_validation`) | `reused: true` replay | `ROUND_NOT_FOUND`, `ROUND_CLOSED`, `VALIDATION_NOT_PASSED`, `VERSION_ENDED` |
| `round evidence status` | operator | `ok` (per-version `state`: `tracking` / `review_due` / `observation_extended` / `paused` / `ended`) | — | `ROUND_NOT_FOUND`, unknown version |
| `round evidence decide` | operator | `ok` (fields `decision`, `state`, `effect`) | `reused: true` replay | `ROUND_NOT_FOUND`, `SUBMISSION_NOT_FOUND`, `REVIEW_CONFLICT`, `VERSION_ENDED` |
| `shadow tick`, `shadow update` | operator | `committed` / `committed_with_anomalies` / `already_committed` | — | `blocked` (exit 2 when any version blocks) |

A Validation `failed` or `insufficient_evidence` verdict is evidence, not a
protocol rejection, so `round validate` and `round revalidate` exit 0 and the
receipt records the `outcome` and `reason`; approval is refused later.

**Synthetic scope.** This is a synthetic mechanism test: synthetic snapshots,
synthetic prices, a synthetic feed and no broker, real order, credential or
network. The public-CLI tests demonstrate the #43 round, receipt, review and
observation rules and the #50 split, formal Validation, admission, detail,
feedback, reuse-disclosure and traceability mechanisms; they are not evidence of
real forward performance, do not satisfy the #35 real-chain gate (which needs a
real receipt linking the snapshot, round, version, warmup source, actual
development and scoring dates, usage and feedback) and do not change the #17
acceptance verdict.

### Direction-bound research (#37)

The research lead creates a Campaign on a sealed snapshot, then approves its
direction, objective, constraints, Validation protocol, and maximum number of
new Validation evaluations. Use one stable direction key for the same design
across display-name changes, Campaigns, and parameter revisions. The kickoff
declares whether prior Holdout feedback influenced the design.

```sh
prajnatrade direction kickoff --project "$PROJECT" --store "$EVAL_STORE" \
  --campaign-id "$CAMPAIGN" --direction-key "cross-sectional-relative-strength" \
  --name "ETF relative strength" --authorization-id "$KICKOFF_APPROVAL" \
  --input kickoff.json
```

The kickoff JSON contains `objective`, `constraints` (a list of strings),
`validation_budget` (a positive integer), `validation_settings` (the evaluation
plan input below), `independence` (an `independent` boolean and a `reason`),
`reviewer`, and `approval_reason`. The Campaign fixes the snapshot, transaction
cost, slippage, initial cash, and Discovery execution budget. The Validation
protocol fixes the comparator and scoring rules for the round.

Select the sealed backtest rule when creating the Campaign. Both the candidate
and the equal-weight benchmark use the selected rule in Discovery and
Validation. Strategies supply target weights to the shared accounting engine.

| `campaign create --execution-rules` | Initial entry | Subsequent rebalances |
| --- | --- | --- |
| `next-open-v1` (default) | Attempt the whole target once at the next session's open. A required unavailable open leaves it unfilled. | Attempt once at the next session's open. |
| `retry-initial-v1` | Retry a blocked positive target at subsequent opens until the first position is established, the partition ends, or a new target replaces it. An explicit cash target cancels a pending entry. | Attempt once at the next session's open. |
| `actual-open-v1` | Match each required symbol once at the next session's open: a buy is a target weight above zero and a sell is a held position. A symbol with an executable open fills at the sealed actual unadjusted daily open, including a halted-at-open session whose bar carries that open and its price source (the 09:30 availability cutoff does not gate that fill). A symbol without one is recorded unfilled with a reason derived from its sealed row and keeps its cash or shares. Sells match before buys in frozen symbol order; buys are funded by initial cash plus realized sell net, and a proportional scale keeps them within that cash. | Same per-symbol rule: sells, then buys, funded by initial cash plus realized sell net. |

Campaigns created before the version flag keep the `next-open-v1` behavior.
`actual-open-v1` fills a halted-at-open session only from a schema-3 row that
seals the actual daily open and its price source; a schema-2 row keeps the older
behaviour. `campaign create` freezes the final policy block for this rule:
`source: sealed_partition.raw_bars.open`, `basis: unadjusted`, `attempts: 1`,
`halted_open: actual_daily_open_when_evidenced`,
`partial_halt_open_availability: not_gated_by_0930`,
`missing_required_open: symbol_not_filled`,
`partial_fill: recorded_per_symbol`,
`funding: initial_cash_plus_realized_sell_net`, `order: sells_before_buys` and
`matching: per_symbol`. It records one attempt per target with `status`
`filled`, `partially_filled` or `not_filled`, the `filled_symbols` that matched,
the `unfilled` symbols with their sealed-row reason (`confirmed_no_trade`,
`missing_required_open`, `unknown_trading_status` or `missing_bar`) and a
`basket_complete` flag. `unfilled` is about the missing open, not about whether a
trade was needed: a required symbol whose open is missing is recorded unfilled
even if its target would have equalled its holding, while a required symbol that
does have an executable open but needs no trade emits no fill row and stays out
of both lists. A symbol whose row is `suspended` or whose `partial_halt` window
covers the whole session is a confirmed no-trade day; a `partial_halt` without a
trusted actual open or a conflicted/missing price is a missing required open.

A held symbol (`shares > ε`) without an executable open makes the day's total
equity basis unknowable: sizing the priced holdings against a partial open
equity would force sales the strategy never asked for. The whole day is then
recorded as `not_filled` with `filled_symbols: []`, `basket_complete: false`
and every required symbol in `unfilled` — the unpriced ones with their
sealed-row reason and the priced ones with `equity_basis_unknown` — while cash,
shares and costs stay untouched. An entry basket (no holdings yet) keeps
per-symbol matching, so its priced symbols still fill.

A partially filled plan is recorded as it happened and marked not yet built:
unfilled buy cash stays in cash and unsold shares stay in the account.
`actual-open-v1` never retries automatically — a later strategy decision
re-emits a target, or the plan expires at the partition end;
`retry-initial-v1` keeps the legacy retry and is not extended. Discovery metrics
add `orders_partially_filled` for this rule (a partial attempt still counts in
`orders_unfilled`), and `position_evidence` adds `basket_build_state` and
`unfilled_basket_signals` (positive-target attempts with an unfilled required
symbol). `basket_build_state` is `completed` when the most recent
positive-target attempt completed, `partial` only when that attempt filled at
least one symbol and left at least one required symbol unfilled, `unfilled`
when that attempt produced no fill at all, and `none` when there was no positive
target. Legacy rules keep their exact old attempt, metric and
position-evidence shapes. The selected rule is part of the Campaign evaluation
fingerprint and is copied into the approved Research round and protected
evaluation plan.

To preregister separate scoring prerequisites, include this object under
`validation_settings` in the kickoff JSON.

```json
{
  "scoring_prerequisites": {
    "candidate": {"require_initial_position": false},
    "baseline": {"require_initial_position": true}
  }
}
```

`true` requires an established position. `false` also accepts explicit intent
to hold cash. A strategy that requests entry and never establishes a position,
or never emits a target, provides insufficient evidence under either setting.
A completed Validation keeps the calculated metrics and reports
`insufficient_evidence` when either
side fails its preregistered requirement. Once both sides qualify, the existing
sample and financial thresholds apply. A new kickoff uses the defaults shown
above when the object is omitted. Rounds approved before this change keep
their recorded scoring rules.

After a successful Discovery experiment, the research agent submits a
Validation proposal. Its JSON records `change`, `question`, `expected_change`,
and `related_experiment_ids`. Once a version meets the thresholds, any new
evaluation needs `selection_impact` explaining how the answer could change the
selection.

```sh
prajnatrade-agent validation propose --project "$PROJECT" --round-id "$ROUND" \
  --submission-id "$SUBMISSION" --input proposal.json
prajnatrade validation service --project "$PROJECT" --store "$EVAL_STORE" \
  --snapshot-store "$SNAPSHOTS" --round-id "$ROUND"
prajnatrade-agent direction status --project "$PROJECT" --round-id "$ROUND"
```

The trusted operator can run `validation service` as a recurring job. It
processes proposals and detail requests under the kickoff approval. The
operator can also run one exact proposal with `validation run --project
"$PROJECT" --store "$EVAL_STORE" --snapshot-store "$SNAPSHOTS" --round-id
"$ROUND" --submission-id "$SUBMISSION" --proposal-id "$PROPOSAL"`. A new version
debits once in the trusted ledger before the sealed read. Checking status,
retrying the same evaluation, and querying stored details preserve the budget.
The agent sees default summaries including net return, return relative to the
equal-weight baseline, drawdown, turnover, and fees. With preregistered scoring
prerequisites, the receipt also summarizes each side's position state and which
requirement was unmet. Dated execution evidence stays in the requested
Validation details.

When a completed Validation shows that the common benchmark failed its scoring
prerequisite, the service pauses new evaluations in that round with the same
partition, benchmark, and execution rules. A pause (`COMMON_BENCHMARK_FAILED`)
identifies the triggering evaluation and leaves affected proposals queued before
they can incur a debit or open protected data. The trusted `validation status
--store "$EVAL_STORE" --round-id "$ROUND"` lists the benchmark failure and
remaining budget with the evaluations already debited. Existing receipts and
detail requests stay available. A candidate's own evidence failure or financial
rejection allows the next comparable version to proceed.

```sh
prajnatrade-agent validation details --project "$PROJECT" --round-id "$ROUND" \
  --evaluation-id "$VALIDATION" --date "$TRADING_DATE" --question "$QUESTION"
prajnatrade-agent validation feedback --project "$PROJECT" --round-id "$ROUND" \
  --evaluation-id "$VALIDATION" --detail-access-id "$DETAIL" --impact "$IMPACT"
prajnatrade-agent direction freeze --project "$PROJECT" --round-id "$ROUND" \
  --evaluation-id "$VALIDATION" --name "ETF relative strength v2" --input selection.json
```

Repeat the detail request after the trusted service has run to retrieve its
signals, positions, and fills. Feedback can reference `--next-experiment-id`.
The selection JSON contains `selection_reason`, a `remaining_risks` list,
an `eliminations` map keyed by experiment ID, and the agent's `independence`
declaration. A passed version can be frozen before the budget is exhausted.
If the budget runs out without one, use `direction retrospective` with a JSON
`findings` and `next_step` for the lead to review. If the lead wants further
research after a freeze, open another approved round using the same direction
key.

The lead authorizes one frozen selection and reaffirms whether selection
remained independent. The trusted authorization verifies the exact selected
Validation ID and records consent to automatically release and enable the
frozen version on a trusted Holdout pass.

```sh
prajnatrade holdout authorize --project "$PROJECT" --store "$EVAL_STORE" \
  --snapshot-store "$SNAPSHOTS" --round-id "$ROUND" \
  --validation-evaluation-id "$VALIDATION" --reviewer "$REVIEWER" \
  --reason "$APPROVAL_REASON" --selection-independent yes \
  --independence-reason "$INDEPENDENCE_REASON" --calendar trading-calendar.json
prajnatrade holdout run --project "$PROJECT" --store "$EVAL_STORE" \
  --snapshot-store "$SNAPSHOTS" --plan-id "$HOLDOUT_PLAN"
prajnatrade evaluation report --store "$EVAL_STORE" \
  --plan-id "$HOLDOUT_PLAN" --output controlled-report.html
```

The calendar JSON includes `schema_version: 1`, `data_kind` matching the
snapshot, a `source`, and an ordered list of ISO trading dates in `sessions`.
Include the planned first eligible after-close signal date and subsequent
trading days. An overlapping Holdout range can be used for another direction
when both declarations and the recorded selection support independence.
Prior use by the same direction, an overlapping legacy Holdout with unknown
direction, or declared influence on an overlapping range records an
eligibility blocker. A scored Holdout rejection ends the round and returns a
controlled retrospective. Aborted or incomplete evidence leaves the release
disabled.

On a pass, the release begins with the first authorized trading session after
the Holdout interval and completion date in Asia/Shanghai. Use the operator's
recurring job to run the trading-day clock with the current feed. For example,
a cron job can call this command every few minutes and route its output and
nonzero exit to the operator's job monitor.

```sh
prajnatrade shadow tick --ledger "$LEDGER" --store "$EVAL_STORE" \
  --calendar trading-calendar.json --data "$DAILY_FEED" \
  --corrections-store "$SNAPSHOTS"
```

The clock records blocked inputs, failures, and missed next-open deadlines in
the ledger. Use `prajnatrade shadow alerts --ledger "$LEDGER"` to inspect
durable alerts. Its retry reuses existing day commits and records missed fills
according to the actual order persistence time.

For real feeds, use `schema_version: 2`, `data_kind: "real"`,
`raw_price_basis: "unadjusted"`, and timezone-aware `open_available_at` and
`close_available_at` on the raw-bar rows. Retain the approved exchange
calendar, contemporaneous feed, scheduler receipts, and reconciled forward
ledger as real acceptance evidence.

### Legacy protected Validation and Final Holdout (MVP-10/MVP-11)

`prajnatrade evaluation ...` runs preregistered, human-authorized, OS-isolated
Validation and Final Holdout for a frozen candidate on sealed synthetic or
schema-v2 real snapshots. Final Holdout requires its own human authorization
after the same frozen plan's Validation passes. The controlled report is
rendered from immutable records. Synthetic runs verify the mechanism. Admission
requires both protected stages and a separate human decision.

The trusted store is one directory outside Git (the same store is reused by
later release/Shadow Live workflows). The evaluation ledger in
`queries.sqlite` is the budget boundary. All projects and operators with access
to a given sealed interval must use the same canonical trusted evaluation
store and ledger.

```text
<store>/
  plans/<plan_id>.json                                        # frozen plan (0400)
  authorizations/<plan_id>/validation-<auth_id>.json           # one Validation authorization
  authorizations/<plan_id>/validation.authorized.json          # which one is used
  authorizations/<plan_id>/final_holdout-<auth_id>.json        # one separate Holdout authorization
  authorizations/<plan_id>/final_holdout.authorized.json       # which one is used
  audit/<audit_id>.json                                       # rejected requests
  queries.sqlite                                              # debit ledger
  records/<evaluation_id>/record.json                         # trusted record (0400)
```

Both partitions are fixed in the plan and are separate global one-use
budgets: the debit keys are `("validation", content_hash)` and
`("final_holdout", content_hash)`.

#### 1. Freeze the plan before reading anything

```sh
prajnatrade evaluation plan --store /trusted/evaluations \
  --snapshot-store /trusted/snapshots --project ./strategy-A \
  --candidate-id <candidate-id> --input plan-input.json
```

`plan-input.json` can only declare the preregistered decision rules:

```json
{
  "schema_version": 1,
  "minimum_samples": {"sessions": 3, "valued_sessions": 3},
  "thresholds": [
    {"id": "drawdown", "metric": "max_drawdown", "op": ">=", "value": -0.25},
    {"id": "relative", "metric": "return_delta", "op": ">=", "value": 0.0}
  ],
  "stress_costs": {"transaction_cost_bps": 20, "slippage_bps": 10},
  "regimes": {"method": "trailing_volatility", "lookback": 5,
              "volatility_threshold": 0.02, "labels": ["calm", "volatile"]},
  "stages": ["validation", "final_holdout"],
  "reason": "why these thresholds and samples were fixed in advance"
}
```

Base costs, initial cash, signal window, execution rules, candidate JSON and
dependencies, project ID, snapshot ID, and both protected partition descriptors
are copied from the frozen candidate and sealed manifest; the plan input may
not restate them (`base_costs`, `candidate_hash` and other bound fields are
rejected). Metrics are a fixed registry: `sessions`, `valued_sessions`,
`total_return`, `max_drawdown`, `sharpe`, `return_delta`, `stress_total_return`,
`stress_return_delta`, `fees_paid`, `slippage_paid`, `unfilled_orders`.
Operators are `>=`, `<=`, `>`, `<`, `==`. Stress costs must be at least the
frozen base costs, `minimum_samples.sessions >= 1`, and `stages` must be exactly
`["validation", "final_holdout"]`. Real Validation requires at least one hard
threshold. The plan freezes the legal parameter neighbourhood with its variants,
exclusions, and thresholds. A Python candidate supplies its frozen parameter
schema, including an explicit `no_tunable_parameters` declaration if applicable.

`plan_id` is the SHA-256 of the canonical plan without `plan_id`/`frozen_at`.
Re-planning the same candidate with different rules creates a new plan; the
previous immutable plan remains. The mapping `(plan_id, candidate_id, plan_hash,
created_at)` is also stored in the project database so the research-side command
can resolve a candidate's receipt.

#### 2. Record the single human Validation authorization

```sh
prajnatrade evaluation authorize --store /trusted/evaluations \
  --plan-id <plan-id> --stage validation \
  --reviewer "reviewer name" --reason "frozen plan reviewed before Validation"
```

The authorization is an immutable record bound to the plan, candidate hash,
snapshot, stage, and Validation partition fingerprint. A second authorization
for the same stage is a conflict: the first is the one used, and
`evaluation status` shows it. Final Holdout needs its own authorization (below).

#### 3. Run the one debited Validation evaluation

```sh
prajnatrade evaluation run --store /trusted/evaluations \
  --snapshot-store /trusted/snapshots --plan-id <plan-id> \
  --stage validation --project ./strategy-A
```

Checks run **before** the sealed read: the plan's content address, the frozen
candidate (re-read and hash-checked from the project), the sealed manifest and
both protected descriptors, the project binding, the matching authorization,
the source's presence, and the isolation probe. On any failure the request is rejected with
a stable `error.code` (`FINGERPRINT_MISMATCH`, `AUTHORIZATION_REQUIRED`,
`ISOLATION_UNAVAILABLE`, ...), a matching `audit/<audit_id>.json` with
`opened_protected_source: false`, and no debit. There is no fallback to local
execution and no bypass flag.

Declarative strategies run in the trusted controller and use the macOS sandbox
boundary probe. If the probe has to retry without RLIMIT_AS, the record includes
`memory_limit_enforced: false` and `failure_reason: "memory_limit_unavailable"`.
Generated Python uses the container runner, limits, and image digest from the
frozen candidate. The runner is probed with empty input before the debit, and
the trusted record includes boundary checks for each strategy invocation.

Only then is the debit written: one SQLite `BEGIN IMMEDIATE` inserts the
`(partition_name, partition_content_hash)` row and the evaluation row, keyed by
the **content hash**, not by project, candidate, operator, or snapshot
identity. Consequently:

- the same plan/stage re-run returns the stored record and receipt, with no
  second debit and no second read;
- another candidate or plan against the same Validation content hash is
  rejected (`QUERY_ALREADY_CONSUMED`) before reading;
- re-packaging the identical protected content under a new snapshot ID cannot
  buy another read;
- concurrent requests serialise on the ledger; a request that loses the race is
  rejected (`EVALUATION_IN_PROGRESS`) and never double-evaluates.

After a successful read the engine accounts only over the Validation calendar,
starting from the frozen initial cash with no holdings. The candidate and the
same-basis `equal_weight_buy_and_hold` baseline run at base and stress costs;
each uses the same frozen signal window, with Discovery as prior history for the
point-in-time signal window (still enforced against the next session's 09:30
Asia/Shanghai availability cutoff). The record contains the fingerprints,
authorization and query receipts, isolation evidence, opened-source flag,
availability rules, coverage and valuation/signal gaps, per-item threshold
verdicts, base vs stress results, parameter neighbourhood, point-in-time market
regimes with sample counts, and candidate/baseline NAV and drawdown curves. It
is written immutably and is sufficient for the later controlled report. Any
abort after the read keeps the debit, scores nothing, writes an
`insufficient_evidence` record with `scoring: false`, and never retries. A
documented synthetic fault hook, `PRAJNATRADE_TEST_FAILPOINT=evaluation_after_read`,
exercises that path and has no effect when unset.

Thresholds are evaluated item by item (`passed`, `failed`, `unavailable`). A
single failed hard threshold means `rejected` even if other evidence is
incomplete. With no failure, missing coverage, valuation gaps, signal gaps,
sample counts below the frozen minimum, or an unavailable threshold metric mean
`insufficient_evidence`. Only complete evidence with every threshold met means
`passed`. The parameter neighbourhood is diagnostic, not a hard threshold, and
no plan can add a generic return threshold after the fact.

For `total_return`, `sharpe` and `max_drawdown`, a threshold can set
`benchmark_multiplier`. The comparison target is that multiplier times the
same-stage base-cost equal-weight metric plus `value`. The controlled report
records the benchmark metric and the computed target.

`allow_discovery_halt_warmup: true` preregisters cash-only Validation sessions
caused exclusively by confirmed full halts in the approved Discovery tail.
The plan binds the exact affected inputs and initial window count. All days
remain in the NAV and metric calculations for both candidate and benchmark.
Additional signal gaps still make coverage incomplete.

New schema-v2 Validation plans treat a documented halt covering 09:30 as
evidence of a non-executable open when the resumed session has a confirmed close
and complete remaining bar fields. An order attempted at that open remains
unfilled, and the controlled report retains the halt and its source evidence.
An unconfirmed open, missing close or missing required input still affects the
coverage verdict.

#### 4. Authorize Final Holdout separately

```sh
prajnatrade evaluation authorize --store /trusted/evaluations \
  --plan-id <plan-id> --stage final_holdout \
  --reviewer "reviewer name" --reason "Validation passed; one Holdout read cleared"
```

The authorization is refused unless the **same** frozen plan already has a
completed Validation evaluation whose record is bound to this plan, candidate,
snapshot and Validation partition fingerprint and whose `outcome` is `passed`
with `scoring: true` (an aborted or insufficient Validation is never a pass).
A second Holdout authorization is a conflict. Refusals happen before any debit
or sealed read: they write an `audit/<audit_id>.json` with
`opened_protected_source: false` and `query_debited: false`, exit non-zero, and
the authorization record itself carries the Validation evaluation id, record
hash and export hash it was granted against.

#### 5. Run the one debited Final Holdout evaluation

```sh
prajnatrade evaluation run --store /trusted/evaluations \
  --snapshot-store /trusted/snapshots --plan-id <plan-id> \
  --stage final_holdout --project ./strategy-A
```

This repeats every Validation gate (plan/candidate/runtime/manifest/descriptor
fingerprints, project binding, authorization, verified OS isolation probe),
re-verifies that the Validation evidence the authorization was granted against
is still intact and passing, and only then atomically debits
`("final_holdout", content_hash)` before the sealed Holdout read. Evaluation is
the same candidate + equal-weight baseline matrix at base and stress costs with
the frozen thresholds, minimum samples, parameter neighbourhood and point-in-time
regimes.

**Warm-up history without a second sealed read.** Holdout signals need trailing
total-return values from earlier partitions. The Validation record exports the
trailing sessions needed by the frozen signal window, legal parameter variants
and regime lookback, bound to the Validation evaluation id, plan, candidate,
snapshot and partition hash. Holdout can prepend Discovery when the export
covers all Validation sessions. The Holdout run loads that export
from the stored Validation record and verifies its hash; it never re-opens the
sealed Validation payload. A Validation record that predates the export (or a
changed record) is rejected with `VALIDATION_EVIDENCE_INVALID` rather than
silently re-reading Validation. Both records also carry a `record_hash`
content address that later workflows verify.

The same global rules apply: a replay of the same plan/candidate/stage returns
the stored record without a second debit or read; a different plan or candidate
using the same sealed Holdout content hash is rejected
(`QUERY_ALREADY_CONSUMED`) before reading. An abort after the Holdout read keeps
the debit, writes `scoring: false`, no metrics and no retry/refund; the
`PRAJNATRADE_TEST_FAILPOINT=evaluation_after_read` hook exercises that path for
either stage.

#### 6. What the research side may see for both stages

The research-side command is read-only and cannot plan, authorize, run or
report:

```sh
prajnatrade-agent evaluation status --project ./strategy-A --candidate-id <id>
```

It returns exactly
`{"schema_version": 1, "candidate_id": ..., "validation": <coarse receipt or
{"status": "pending"}>, "final_holdout": <constant>}` and nothing else. The
`validation` value is either `{"status": "pending"}` or exactly this coarse
receipt (the latest completed evaluation of that frozen plan; replaying in the
same trusted store returns the same payload):

```json
{"schema_version": 1, "stage": "validation",
 "status": "passed|rejected|insufficient_evidence",
 "reason": "THRESHOLD_NOT_MET|COVERAGE_INSUFFICIENT|EVIDENCE_INCOMPLETE",
 "message": "..."}
```

It contains no numbers, dates, metrics, fills, curves, variants, logs,
durations, or admission state (a passing receipt has `reason: null`). The Final
Holdout value is the constant, outcome-independent document:

```json
{"schema_version": 1, "stage": "final_holdout",
 "status": "accepted", "message": "Request received."}
```

It is byte-identical for completed, rejected, aborted, unauthorized and
re-submitted requests: no timestamps, IDs, durations, counts, diagnostics,
admission state or anything from the trusted Holdout record is exposed.

The Validation `validation` value contains no numbers, dates, metrics, fills,
curves, variants, logs, durations, or admission state (a passing receipt has
`reason: null`). A rejected **request** (permissions, budget, fingerprint,
isolation) is not a strategy failure and publishes no strategy receipt.

#### 7. Controlled report (operator only)

```sh
prajnatrade evaluation report --store /trusted/evaluations \
  --plan-id <plan-id> --output report.html   # or report.json
```

The report is rendered **only** from the frozen plan, the stored
authorizations, the query ledger, rejected-request audits and the immutable
evaluation records; it never re-runs an evaluation, opens a sealed partition or
debits a query. A changed or missing record is an explicit error
(`RECORD_INTEGRITY_FAILED`), not a fallback. It shows the candidate / plan /
snapshot / partition fingerprints, both authorizations and query receipts
(including rejected and aborted requests), coverage gaps, every preregistered
threshold with actual value and verdict, base-vs-stress candidate and
equal-weight baseline results, the legal parameter neighbourhood with
exclusions, point-in-time market regimes with sample counts, and inline SVG NAV
and drawdown curves for candidate and baseline at both cost scenarios. Missing
evidence is rendered as *not approved / incomplete*; the report always states
that it does not grant admission. `.html` and `.json` contain the same
report data and are deterministic for the same stored records (the report has a
content hash and no wall-clock timestamps).

The operator view is `prajnatrade evaluation status --store <store>
--plan-id <plan-id>`: the frozen plan, both authorizations, per-stage query
debit state, evaluation outcome/`scoring` and the record path.

**Implementation decisions for the strict reading of #12/#13/#29:**

- The Validation record keeps only the derived-row tail needed by the frozen
  signal window and regime lookback (`prior_export_for_holdout`), not the whole
  sealed partition. A Validation record without that export, or with a changed
  hash, is rejected (`VALIDATION_EVIDENCE_INVALID`) instead of re-reading
  Validation or recomputing the export from sealed data.
- The Holdout run re-verifies the Validation evaluation id and record hash that
  are bound into its authorization, so a later change to the Validation
  evidence is `AUTHORIZATION_REQUIRED`, never a silent pass.
- Trusted records carry a `record_hash` content address; replay, operator
  status and the report verify it (`RECORD_INTEGRITY_FAILED` on mismatch).
- The controlled report is written to the operator-selected `--output` path
  rather than copied into the trusted store; it is still derived only from the
  immutable plan/records/ledger/audit files.
- The constant Holdout receipt is stored in the trusted record's
  `research_receipt` field and in `evaluation_receipts(plan_id,
  'final_holdout')` for audit, but it is never derived from the outcome.

### Legacy immutable release and lifecycle (MVP-12)

`prajnatrade release ...` turns an explicitly human-approved frozen candidate
into a uniquely identified, immutable release version and lets an operator
enable, pause, resume and retire tracking of that version at pre-registered
after-close signal dates. Synthetic v1 and real v2 plans use the same evidence
and human approval requirements. Release artifacts inherit the frozen plan's
scope. Shadow Live accounting is built on top of these artifacts. The research
side (`prajnatrade-agent`) has
no release, admission or lifecycle command at all, so admission state cannot
flow back to a research Agent.

The release objects live in the same trusted store as the evaluation evidence
(outside Git and outside the research project):

```text
<store>/
  releases/<release_id>/artifact.json                     # immutable release (0444)
  releases/<release_id>/events/<seq>.json                 # append-only lifecycle events (0444)
  releases/<release_id>/dispositions/<revision-id>.json   # #30 direct impact disposition (immutable)
  releases/<release_id>/dispositions/<revision-id>-<seq>-<id>.json  # #31 write-once dispositions
  audit/<audit_id>.json                                  # refused approvals/events
```

`release_id = sha256(canonical JSON {"candidate_hash", "plan_hash",
"release": "release-v1"})`. The artifact stores `artifact_hash`, the SHA-256 of
the artifact without that field, and every fetch re-computes it.

#### 1. Approve a frozen candidate (the only admission path)

```sh
prajnatrade release approve --store /trusted/evaluations \
  --plan-id <plan-id> --reviewer "reviewer name" \
  --reason "why this evidence is sufficient for admission"
```

Approval re-loads the frozen plan and both protected-evaluation records from the
trusted store and refuses (exit 2, stable `error.code`, `releases/` untouched,
`audit/<audit_id>.json` with `release_created: false`) unless all of the
following hold for the **same** frozen plan, candidate, snapshot and the two
partition descriptors:

- both stages have a `completed` query-ledger row and a stored record whose
  `record_hash` verifies and whose `_record_matches` binding is intact
  (`EVIDENCE_INVALID` otherwise; missing rows/records are `EVIDENCE_MISSING`);
- both records are scored and not aborted (`EVIDENCE_INCOMPLETE`);
- both `outcome` values are `passed` (`NOT_PASSED`); a rejected or
  insufficient stage can never be approved and there is no override flag;
- every preregistered hard threshold is `passed` (`THRESHOLD_NOT_MET` for a
  failed one, `EVIDENCE_INVALID` for a missing or unavailable one — no
  threshold is exempt and no post-hoc threshold is added);
- coverage/minimum samples are complete (`COVERAGE_INCOMPLETE`) and the
  record's `isolation_verified` evidence is true (`ISOLATION_UNVERIFIED`);
- the stored authorizations match the plan and the ledger rows were run under
  exactly those authorizations (`AUTHORIZATION_REQUIRED` /
  `AUTHORIZATION_INVALID`); the Final Holdout authorization must be bound to the
  stored Validation record hash and export hash, and the Holdout record's
  `prior_evidence` must point at the same Validation evaluation;
- both records' execution settings (rules, initial cash, costs, signal window)
  equal the frozen plan (`EVIDENCE_INVALID`);
- the frozen candidate JSON still hashes to `candidate_hash` and is bound to the
  plan's snapshot.

On success the artifact is written once (`0444`, write-temp + `os.link`) and
binds the candidate JSON/hash, `plan_hash`, project and snapshot IDs, both
partition descriptors, both evaluation IDs with their record hashes, outcomes
and authorization IDs, the approval reviewer/time/decision/reason, and the exact
execution settings (strategy family, parameters, initial cash,
`transaction_cost_bps`, `slippage_bps`, base and stress costs,
`signal_window_sessions`, execution rules, baseline definition). The artifact
**is** the admission decision record: its `admission.statement` says that
approval is separate from evidence performance and never enables tracking
automatically (`admission.automatic_enable: false`). The controlled evaluation
report remains a readable presentation and never grants admission itself.

Approval is idempotent for one `(candidate, plan)` pair: a repeat — even with a
different reviewer or reason — still verifies the frozen plan and the existing
artifact's content address, then returns that artifact unchanged with
`reused: true` and the original approval (it does not re-derive the evidence);
the file is never rewritten and no second ID exists. A candidate whose parameters change is a different frozen
candidate and needs its own plan, evidence, approval and release ID; old
releases are never replaced.

#### 2. Fetch by exact ID

```sh
prajnatrade release fetch --store /trusted/evaluations --release-id <release_id>
```

Fetch verifies the stored `artifact_hash`, the `release_id` binding and the
content-address derivation from `candidate_hash` + `plan_hash`. A missing or
tampered artifact fails explicitly (`RELEASE_NOT_FOUND` /
`RELEASE_INTEGRITY_FAILED`); there is no fallback to the workspace, the
project, another version or "latest".

#### 3. Lifecycle events at pre-registered signal dates

```sh
prajnatrade release enable  --store ... --release-id <id> \
  --effective-signal-date 2030-01-02 --reason "..."
prajnatrade release pause   --store ... --release-id <id> \
  --effective-signal-date 2030-02-05 --reason "..."
prajnatrade release resume  --store ... --release-id <id> \
  --effective-signal-date 2030-02-10 --reason "..."
prajnatrade release retire  --store ... --release-id <id> \
  --effective-signal-date 2030-02-15 --reason "..."
```

Approval writes **no** event: a freshly approved release is `published` and
inactive at every date. Each lifecycle event is one immutable
`events/<seq>.json`:

```json
{"schema_version": 1, "scope": "synthetic", "release_id": "...", "seq": 1,
 "action": "enable", "effective_signal_date": "2030-01-02",
 "recorded_at": "2030-01-01T03:00:00+00:00", "reason": "...",
 "prev_event_hash": null, "state_before": "published",
 "state_after": "enabled", "event_hash": "..."}
```

`event_hash` is the SHA-256 of the event without that field and
`prev_event_hash` chains to the previous event, so removing a middle event or
editing any field is detected (`RELEASE_INTEGRITY_FAILED`) by `status`, the
hooks, and every later lifecycle command. (Truncating only the newest event
needs an external anchor; the Shadow Live ledger supplies one for a tracked
version.)
`effective_signal_date` is an ISO date and must not decrease across the
timeline; it is the pre-registered after-close signal point, not the approval or
run time, so a historical replay sees the version and state registered for that
date. Trading-session validation of the date belongs to the Shadow Live feed
(#32); the release store deliberately does not open a sealed snapshot.

Allowed transitions, and nothing else (`INVALID_TRANSITION`, terminal
retirement, exit 2 with an audit record):

| action | allowed from | resulting state |
| --- | --- | --- |
| `enable` | `published` (never enabled) | `enabled` |
| `pause` | `enabled` | `paused` |
| `resume` | `paused` with valid admission + intact artifact/records | `enabled` |
| `retire` | `published`, `enabled` or `paused` | `retired` (terminal) |

Pause stops new signals from its effective signal date and never alters or
deletes the artifact or earlier events. Resume reuses the same ID and does not
backfill the paused window. An invalidated admission (`ADMISSION_INVALID`) or a
deleted/tampered bound record (`EVIDENCE_MISSING` / `EVIDENCE_INVALID`) blocks
resume; a retired release can never be enabled or resumed again.

#### 4. Operator status and the Shadow Live hooks

```sh
prajnatrade release status --store /trusted/evaluations \
  --release-id <release_id> [--as-of 2030-02-05]
```

The status prints the artifact fingerprints, approval, admission state, the
full hash-chained timeline and the state derived at `--as-of` (or the current
state): the last event with `effective_signal_date <= as-of`, mapping
`enable`/`resume` → `enabled`, `pause` → `paused`, `retire` → `retired`, and no
event → `published`. `active` is true only when that state is `enabled`.

Shadow Live (#32) uses these hooks: `release.fetch`, `release.state_at(store,
release_id, signal_date)`, `release.releases_active_at(store, signal_date)`,
`release.list_releases(store)` and `release.admission_state(store, release_id)`.
`admission_state` resolves the stored impact dispositions of the release (see
the dividend-correction section below): valid unless the release is retired or
the resolved disposition of any revision is `pending_review` /
`no_longer_supported` (unreadable or unbound dispositions are treated
conservatively as invalidating). `releases_active_at` returns only IDs,
fingerprints and bindings — the caller fetches each artifact by exact ID and
verifies it again.

**Implementation decisions and limits for #30:**

- Only an explicit `approve` creates a release. "Human rejection" is expressed
  as not approving: this ticket adds no separate rejection-decision record and
  no override flag. A refused approval is still auditable and can never produce
  a runnable version.
- The release artifact records admission. Both protected evaluations must pass
  before the reviewer can approve a release. The delivery has exercised this
  gate with generated inputs in both formats and has no approved real release.
- The store's `queries.sqlite` budget boundary is unchanged: approval, fetch,
  lifecycle and status never read a sealed partition and never debit a query.

#### 5. Dividend-correction impact re-review (#31, synthetic only)

A supplier can silently change an amount, and a trusted operator can verify the
change against an exchange/fund-manager notice. `prajnatrade release
disposition` records what that verified revision means for one already-approved
release version **without re-running, rewriting or re-approving anything**:

```sh
prajnatrade release disposition --store /trusted/evaluations \
  --snapshot-store /trusted/snapshots \
  --release-id <release_id> --revision-id <revision_id> \
  --reviewer "reviewer name" --reason "why this impact was recorded" \
  [--impact sustained|pending_review|no_longer_supported] \
  [--ledger /trusted/shadow --data synthetic-feed.json] \
  [--effective-signal-date 2030-03-05]
```

The revision is read from the snapshot store's write-once journal
(`<snapshot-store>/.dividend-corrections/revision-<revision-id>.json`) and must
still be `status: "verified"` with `sha256(revision) == revision_id`; a
proposal, an unverified observation or a tampered record is refused
(`REVISION_NOT_FOUND` / `REVISION_NOT_VERIFIED` / `REVISION_INVALID`). No sealed
partition payload is opened and `queries.sqlite` is never debited — the command
also succeeds after the sealed `validation.json` / `final_holdout.json`
payloads have been removed.

**Exposure and the default classification.** Exposure comes from the release
artifact only: the revision's `ex_date` must fall inside the `validation` or
`final_holdout` range stored in the artifact's `partition_fingerprints` (the
frozen plan bound to the artifact is re-verified). If it falls outside both
evaluated ranges the release could not have used that input: the recorded
impact is `not_applicable` (or, if the operator explicitly passes `sustained`,
`sustained`) and the release is never paused or invalidated. An invalidating
explicit impact for an out-of-range revision is refused
(`IMPACT_NOT_APPLICABLE`).

For an exposed release the classification compares
`official_available_at` with the **plan's `frozen_at`** — the moment the
evidence was fixed *before* the first protected read. Approval time is never
used (it is always "now"). The two evidence cases are:

| evidence | default impact | meaning |
| --- | --- | --- |
| official availability **after** the plan freeze | `sustained` | the report and approval keep their then-evidence validity: the old evaluation did not have the corrected amount at freeze time |
| official availability **at/before** the plan freeze | `pending_review` | the sealed evaluation may have used an amount that was already officially wrong when the evidence was frozen; admission needs a re-review |

A later verified re-review can record `sustained` for the same revision and
clear a `pending_review`. `no_longer_supported` is **final**: no later
`sustained` record can clear it, the release can never resume (nor be enabled),
and a replacement needs a new frozen candidate, a qualified unseen protected
range, fresh approval and a **new release ID**. The consumed sealed range is
never re-issued (`QUERY_ALREADY_CONSUMED`).

**Pause point.** An exposed `pending_review` / `no_longer_supported` release
that is currently `enabled` gets a real append-only `pause` lifecycle event at
the first not-yet-committed after-close signal point following the disposition:
with `--ledger` + `--data` it is the feed session after the version's last
committed session (or, with no ledger history, the first session whose
after-close point follows the revision filing/now); otherwise an explicit
`--effective-signal-date` is required. The date must not precede the last
registered signal date (`ILLEGAL_PAUSE`), and a release that is not currently
enabled (`published`, `paused`, `retired`) gets the disposition only — no
invented event. A refused input writes no disposition and no event. Pending
orders submitted before the pause keep their fill eligibility, positions and
NAV keep settling in Shadow Live, and the version generates no new
signals/orders while paused; unaffected versions keep running. Resume reuses
the same ID at a pre-registered date and never backfills the paused window.
The one-time accounting delta for the corrected amount itself is **#33**; #31
deliberately changes no booked cash, receivable or NAV.

**Disposition records and resolution.** Every disposition is one write-once,
content-addressed file under
`releases/<release_id>/dispositions/<revision-id>-<seq>-<id>.json` (`0444`,
write-temp + `os.link`), carrying the release/revision/candidate/plan/snapshot
IDs, the frozen plan timestamp, the impact, the classification method/basis,
the reviewer, the operator reason, `recorded_at`, and the planned pause point.
Repeating a disposition appends a new record; the old bytes are never rewritten.
For admission, the records of one revision resolve **latest-wins** by
`recorded_at`/`sequence`, except that a `no_longer_supported` record is final;
an unreadable or unbound record is conservatively invalidating. The #30 direct
file `dispositions/<revision-id>.json` is still read. Both `resume` and `enable`
refuse while the resolved admission is invalid (`ADMISSION_INVALID`).

Command output is versioned JSON:
`{schema_version, status: "recorded", release_id, revision_id, impact,
implication, classification, admission, paused_event|null, disposition,
disposition_path, unchanged:{query_ledger, sealed_reads: false, artifact,
evaluation_records, snapshot_partitions}}`; the `unchanged` block records that
the artifact, both evaluation records, the sealed partitions and the query
ledger were not modified. The research side (`prajnatrade-agent`) has no
`release`/`disposition`/admission command at all, so disposition details never
flow back to a research Agent.

*Limitation:* the revision journal does not store its predecessor snapshot ID,
so exposure is conservatively derived from the release artifact's evaluated
ranges (its pre-registered input scope) rather than by walking snapshot
lineage. If lineage later proves the action never entered the evaluated
evidence, an operator records `sustained` for that revision.

### Shadow Live one-day ledger (MVP-13)

`prajnatrade shadow update` accounts **one manually triggered paper trading
day** for every tracked immutable release version, `prajnatrade shadow report`
dumps one version's committed ledger for trusted audit, and `prajnatrade shadow
compare` compares the common, consecutive committed forward interval of two or
more versions. Shadow Live is **virtual accounting only**: it never connects to
a broker or sends a real order. The operator supplies a point-in-time feed.
The examples below use generated prices and record engineering evidence.

A release ID is the account boundary. Each version keeps its own cash,
positions, orders, fees, slippage, dividends and NAV; prices may be shared,
money never is. `prajnatrade-agent` has no `shadow` command at all, so no
Shadow Live state can flow back to a research Agent.

```text
prajnatrade shadow update --ledger /trusted/shadow-ledger \
  --store /trusted/evaluations --data synthetic-feed.json --date 2030-01-07 \
  [--corrections-store /trusted/snapshots] \
  [--release-id <exact-release-id> ...]
prajnatrade shadow report --ledger /trusted/shadow-ledger \
  --version <exact-release-id> --output ledger-report.json
prajnatrade shadow compare --ledger /trusted/shadow-ledger \
  --data synthetic-feed.json \
  --versions <release-id>,<release-id>[,<release-id>...] \
  [--output comparison.json]
```

#### 1. The point-in-time feed (`--data`)

The feed is versioned JSON. Every row carries a timezone-aware
`available_at` and non-empty `availability_evidence`. Use an explicit
`null` for missing prices. The example uses the synthetic v1 format.

```json
{"schema_version": 1, "data_kind": "synthetic",
 "symbols": ["511260", "518880", "515080", "513300", "159612"],
 "calendar": [{"date": "2030-01-02",
               "available_at": "2030-01-02T08:00:00+08:00",
               "availability_evidence": "synthetic calendar"},
              {"date": "2030-01-07",
               "available_at": "2030-01-07T08:00:00+08:00",
               "availability_evidence": "synthetic calendar"}],
 "raw_bars": [{"date": "2030-01-02", "symbol": "511260",
               "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0,
               "volume": 1000, "trading_status": "trading",
               "available_at": "2030-01-02T09:00:00+08:00",
               "availability_evidence": "synthetic bars"}],
 "derived": [{"date": "2030-01-02", "symbol": "511260", "value": 10.0,
              "available_at": "2030-01-02T15:00:00+08:00",
              "availability_evidence": "synthetic total-return value"}],
 "corporate_actions": [{"action_id": "cash_dividend:511260:2030-01-07",
   "date": "2030-01-07", "symbol": "511260", "type": "cash_dividend",
   "cash_per_share": 0.2, "pay_date": "2030-01-09",
   "terms_evidence": "synthetic terms",
   "available_at": "2030-01-07T08:00:00+08:00",
   "availability_evidence": "synthetic notice"}]}
```

Validation is strict and a malformed feed is rejected with exit 2,
`{"schema_version":1,"status":"rejected","error":{"code":"INVALID_FEED", ...}}`
and a stable `error.phase:"feed"`; nothing is frozen and no ledger day is
written. The rules:

- The feed declares schema version 1 with `data_kind: synthetic`, or
  version 2 with `data_kind: real` and `raw_price_basis: unadjusted`.
  Its scope must match the release scope. `symbols` is exactly the five ETFs.
- `calendar`, `raw_bars` and `derived` are non-empty; calendar dates are unique
  and strictly increasing; every row date is in the calendar.
- `raw_bars` OHLC prices are finite and positive (or `null`), `volume` is
  finite and nonnegative (or `null`), `trading_status` is
  `trading|suspended|unknown`, and `high >= max(open, close)`,
  `low <= min(open, close)`.
- Schema v2 also accepts `partial_halt` with a halt window. Raw bars carry
  `open_available_at` and `close_available_at`. Missing opening-price
  evidence blocks an eligible version before freezing the day. A confirmed
  halt expires the corresponding opening order and a confirmed close can
  still be used for valuation.
- `derived.value` is finite and positive (or `null`).
- A corporate action is `cash_dividend` (`cash_per_share` + `pay_date`) or
  `share_adjustment` (`ratio`); numeric terms are finite and positive when
  present, `terms_evidence` may be `null` for genuinely unresolved terms, and
  `pay_date >= ex-date`. `action_id` is optional and derived as
  `cash_dividend:<symbol>:<ex-date>` or `share_adjustment:<symbol>:<ex-date>`;
  IDs are unique.
- Duplicate `(date, symbol)` rows, duplicate calendar dates, non-ISO dates,
  naive timestamps, empty evidence and out-of-calendar dates are invalid.
  A target `--date` outside the calendar is `NOT_A_SESSION` (exit 2); a
  non-session is never invented as a trading day.

Point-in-time cutoffs are enforced in Asia/Shanghai (`+08:00`): execution
prices must be available by the execution session's **09:30 open**; signals,
unadjusted closes and corporate-action terms must be available by the
session's **15:00 close** (terms by the ex-date open so they can be applied
before trading). A row published after its cutoff is treated as unavailable —
it is never substituted with a stale or later value.

#### 2. The ledger (`--ledger`)

The ledger is a SQLite database outside Git and outside the research project:

```text
<ledger>/shadow.sqlite
  input_snapshots(version_id, date, input_json, input_hash, frozen_at,
                  PRIMARY KEY(version_id, date))
  days(version_id, date, result_json, result_hash, committed_at, status,
       PRIMARY KEY(version_id, date))
  progress(version_id PRIMARY KEY, release_id, start_date,
           last_committed_date, state)
```

The minimum transaction boundary is **version × trading session**. Before
anything is computed, the complete per-version/per-session input snapshot
(the release's frozen execution settings, its state at that session's signal
point, the day's raw/derived/action rows, the applicable verified
dividend-correction set (see below), and the prior account state including
pending orders) is written and content-addressed. An input is frozen **only
when every required input is present**; a missing held close, unavailable or
suspended held input, unresolved held corporate-action terms, missing signal
input, or a correction whose `old_amount` does not match the booked amount
blocks that version before anything is written.

The day commit is one atomic `BEGIN IMMEDIATE` transaction: the order terminal
states, fills, cash, positions, fees, slippage, dividend entitlements/payments,
NAV, the new signal, the next session's orders, progress and anomalies are
written together with the result hash, or nothing is. `committed_at` records
the actual posting time; each order records its pre-registered `submitted_at`
signal point. `progress.last_committed_date` can only advance to the immediate
next feed session; a transaction that would skip or rewrite a session is
refused (`PROGRESS_CONFLICT`).

#### 3. What one `shadow update` does per version

Approval alone never starts tracking. Without `--release-id` the command
selects every release that is *tracked* at the target date:

| release state at the target date | processed automatically? |
| --- | --- |
| `enabled` | yes |
| `paused` / `retired`, with a ledger history | yes (settles/values, no new signals) |
| `paused` / `retired`, no ledger history | no (explicit `--release-id` reports `retired`) |
| `published` (approved but never enabled) | no (explicit `--release-id` reports `not_started`) |

An explicit `--release-id` (repeatable) processes exactly that release and
fetches its artifact by exact ID with the full hash verification; there is no
`latest`, no workspace copy and no fallback. A missing or tampered artifact
blocks that version (`RELEASE_NOT_FOUND` / `RELEASE_INTEGRITY_FAILED`) and never
substitutes another one.

The release's state is derived from its lifecycle event timeline **at each
session's after-close signal point**, not from today's state. The first run
backfills from the first feed session whose state is not `published` (the first
session at/after the pre-registered enable date); later runs resume at the
session after `last_committed_date`. Each skipped session is processed one at a
time, so a historical signal only ever uses data that was available at its own
signal time and a missed open is recorded instead of being filled
retroactively.

For every session the day transaction is:

1. **Corporate actions before the open.** Share adjustments are applied first
   to start-of-day holdings; then cash-dividend entitlements for start-of-day
   holdings on the ex-date become receivables (`quantity × cash_per_share`,
   payable on `pay_date`). A pay-date transfer moves the receivable into cash
   *after* the open settlement, so unpaid dividends are part of NAV but can
   never fund an opening trade, and the transfer itself never changes NAV. No
   entitlement is duplicated: it exists either as a receivable or as cash.
   The frozen applicable dividend-correction deltas are then applied, still
   before the open (see subsection 6).
2. **Settle the previous session's pending orders at the unadjusted open**, in
   the deterministic frozen order *sells before buys, then by symbol*. Every
   order receives exactly one terminal state: `filled`, `missing_open`,
   `missed_deadline`, `insufficient_cash` or `insufficient_shares`. A confirmed
   missing/unusable open (explicit `null`, `suspended`/`unknown` status, zero
   volume or a late row) expires only that order; the others still settle. Buy
   cost is checked against available cash at the fill price including fees, and
   sell quantity against the holdings at settlement. There is **no resize, no
   short, no leverage and no retroactive fill**: an unaffordable buy or an
   oversized sell is terminal and leaves the account untouched.
   Only orders whose recorded `submitted_at` (the pre-registered after-close
   signal point) precedes the execution session's 09:30 Asia/Shanghai deadline
   **and** whose day transaction was durably committed before that deadline in
   real wall-clock time (`persisted_before_deadline`, fixed when the order is
   committed) are fill-eligible. A backfill persisted after a past open is
   `missed_deadline`; a later signal creates a brand-new order with a new
   stable ID instead of retrying the expired one.
3. **Value at the unadjusted close** plus cash plus unpaid receivables. A
   missing/unavailable/suspended close for a held symbol, unresolved held
   action terms, or a missing after-close signal input blocks **that version
   only** — no day row, no progress advance, and a precise `reason`/
   `reason_code` in the output. A genuine evidenced non-fill is *not* a block.
4. **Generate the next session's signal only when the release is still
   `enabled` at that session's after-close signal point.** The frozen strategy
   and its frozen `signal_window_sessions` run over the trailing window of
   `derived` values available by the session's 15:00 close; target weights are
   sized at the unadjusted close (buy quantities include the frozen fees and
   slippage, and the buy side is budgeted at order-creation time from cash plus
   the same signal's net sell proceeds, so a full switch is affordable at a
   same-price open) and persisted as stable
   `order_id`s with `submitted_at` = the session's pre-registered signal point
   and `execution_date` = the next feed session. `paused`/`retired` versions
   never create new orders, but their existing orders and positions keep
   settling and valuing.

#### 4. Update output, statuses and exit codes

```json
{"schema_version": 1, "scope": "synthetic", "target_date": "2030-01-07",
 "completed_at": "2030-01-08T01:02:03+00:00",
 "versions": [{"release_id": "...", "status": "committed",
               "target_date": "2030-01-07",
               "committed_at": "2030-01-08T01:02:03.123456+00:00",
               "reason": null, "reason_code": null,
               "committed_date": "2030-01-07",
               "last_committed_date": "2030-01-07", "result_hash": "...",
               "anomalies": [], "unfilled_orders": []}]}
```

Every version entry carries the requested `target_date` and the **actual**
`committed_at` posting timestamp read from its committed day row; `committed_at`
is `null` for `blocked`, `not_started` and `retired` versions (a version that
did not commit has no posting time), and `committed_date` /
`last_committed_date` describe the last committed session. A blocked version
writes no day row and does not advance; the other versions still commit.

| status | meaning |
| --- | --- |
| `committed` | the target day is committed with every order terminal and nothing anomalous |
| `committed_with_anomalies` | the target day is committed, but an evidenced non-fill (missing open, missed deadline, insufficient cash/shares), a correction-caused cash deficit or another non-blocking anomaly was recorded |
| `already_committed` | the target day was already committed; the stored result hash is returned and nothing is regenerated or re-filled |
| `blocked` | that version wrote no day row and did not advance; `reason`/`reason_code` say why (e.g. `missing_close`, `missing_action_terms`, `missing_signal_input`, `frozen_input_changed`, `ledger_integrity_failed`, `release_integrity_failed`) |
| `not_started` | the release is approved but not enabled at/before the target (`NOT_ENABLED`), or is first tracked after it (`ENABLED_AFTER_TARGET`) |
| `retired` | the release is retired at the target with no ledger history to settle |
| `ended` | a #43 manual simulated-tracking version was retired by an evidence decision; its committed history stays unchanged and no later day, signal or order is generated (`reason_code: VERSION_ENDED`) |

The command exits `0` when no version is blocked, and `2` when at least one is.
A blocked version never rolls back another version's committed days. Global
operator errors (malformed feed, non-session target, malformed release ID) are
rejected with the project's standard `{"status":"rejected","error":...}`
document and exit 2. Argument misuse is a plain `ValueError` → exit 2.

#### 5. Safe re-runs, interruptions and integrity

The ledger is the only source of truth; `shadow update` never rewrites a
committed history.

- **Crash before the day commit**: the frozen input remains, no day row exists.
  The rerun reuses that frozen snapshot and produces an item-identical day
  (orders, fills, cash, positions, fees, dividends, NAV, signal and next orders
  are all deterministic given the frozen input). Two documented fault
  injection points exercise this in tests and have no effect when unset:
  `PRAJNATRADE_TEST_FAILPOINT=shadow_after_fills` (raises after the fills,
  before the commit) and `PRAJNATRADE_TEST_FAILPOINT=shadow_after_commit_before_report`
  (raises after the commit, before the report).
- **Crash after the day commit**: the rerun returns `already_committed` with
  the stored result and does not regenerate a signal, an order or a fill;
  report generation is read-only and can simply be repeated.
- **Frozen input changed**: if a frozen input exists but the current feed or
  prior account state would produce a different input hash, the version is
  blocked with `FROZEN_INPUT_CHANGED` and both hashes; frozen and current
  inputs are never mixed. Restoring the original feed lets the rerun proceed.
- **Tampered ledger**: every committed day and every frozen input is verified
  against its stored content address on reuse, on `already_committed` and in
  `shadow report`. A mismatch is `LEDGER_INTEGRITY_FAILED` (update blocks that
  version, report exits 2) instead of being silently accepted.

`shadow report` writes a trusted audit document
(`kind: shadow_live_ledger_report`, scope from the committed version) containing the
progress row, an integrity block, a summary (days, first/last committed date,
ending NAV, total return, fees, slippage, dividends, fills, unfilled orders,
anomalies, ending cash, every applied correction, the separate
`correction_impact_total`, and every correction-caused cash deficit) and every
committed day record with its input hash, result hash, `committed_at`,
`status` and full result. It only reads committed records and never re-runs a
strategy.

#### 6. Dividend corrections in the Shadow Live ledger (#33, synthetic only)

A verified official revision can change a dividend amount *after* the ex-date
was already booked. Re-running or rewriting the historical day would launder a
correction into the strategy's P&L, so Shadow Live instead appends **exactly
one net delta** `(new_amount - old_amount) × qualified_shares` on a later
session and reports it separately. `--corrections-store <snapshot-store>` opts a
run into corrections; without the flag no correction is ever applied, even when
the journal is present. The flag points at the trusted snapshot store that owns
the write-once revision journal; this is independent of `--store` (the release
store).

**Reading the journal only.** Revisions are taken exclusively from
`<snapshot-store>/.dividend-corrections/revision-<revision-id>.json` via
`dividend.load_verified_revision`, which requires `status: "verified"` and
`sha256(revision) == revision_id`. The loader is deduplicated by
`revision_id`; a proposal, an unverified observation, a tampered record or a
missing file is never evidence and is reported as an ignored entry with a stable
code (`REVISION_NOT_FOUND` / `REVISION_NOT_VERIFIED` / `REVISION_INVALID`) in
the update output's `corrections.ignored` list. Loading a journal never opens a
sealed `validation`/`final_holdout` partition payload and never debits
`queries.sqlite` — the command works after those payloads have been removed.
Only an unavailable corrections store is a request rejection
(`CORRECTIONS_STORE_UNAVAILABLE`, exit 2, nothing frozen or committed).

**Applicability.** For a version and session, a verified revision applies only
when all of these hold:

1. it is filed by the session's **15:00 Asia/Shanghai after-close cutoff**
   (`revision.filed_at <= session 15:00+08:00`). A correction filed after the
   cutoff waits for the next session whose input is not yet frozen; an already
   committed day is never reopened or adjusted retroactively;
2. its `ex_date` is at or before the session;
3. the ledger booked an entitlement for that symbol/ex-date, i.e. the version
   held shares through the ex-date (an unheld action gets no correction);
4. it has not already been applied for `(version_id, action_id, revision_id)` in
   any committed day — the dedupe key is persisted in the booked dividend and
   scanned across the ledger, so duplicate notices, a crash before the commit
   and a rerun after it never apply a second delta;
5. its `old_amount` equals the currently booked per-share amount — the original
   ex-date `cash_per_share` plus every prior applied delta for that
   entitlement. A mismatch is a per-version block
   (`CORRECTION_AMOUNT_MISMATCH`, no day row, no progress advance), never a
   guessed delta. Chained revisions are applied in filed order as long as each
   one's `old_amount` matches the then-booked amount.

The applicable set is fixed **inside the frozen per-day input snapshot before
computation**. A crash after the input was frozen reuses exactly that decision
(a revision filed after the freeze waits for the next uncommitted session),
and a rerun after the commit returns `already_committed` with the stored delta.

**Receivable versus cash.** Corrections are applied before the day's open and
payment processing:

- if the entitlement is still an **unpaid receivable**, the receivable is
  adjusted by the delta; a payment on or after that day transfers only the
  adjusted amount, so the amount is never deducted twice;
- if the entitlement was **already paid**, cash is adjusted by the delta. The
  historical payment and every earlier day's cash/NAV stay byte-identical.

**Deficits, not financing.** A paid-entitlement correction can push cash below
zero. That balance is recorded honestly in the day result as `cash_deficit`
(`{amount, caused_by: "dividend_correction", revision_ids, financed: false,
forced_sale: false}`) plus a `cash_deficit` anomaly; the version's day still
commits (`committed_with_anomalies`) and later buys fail normally with
`insufficient_cash` against the real balance. There is **no financing, no
forced sale, no cash invention, no leverage and no retroactive rewrite** of
signals, orders, fills, cash or NAV, and the account stays negative until
trading actually replenishes it.

**Reporting.** Each committed day carries
`corrections: [{revision_id, action_id, symbol, ex_date, pay_date, quantity,
booked_cash_per_share, old_amount, new_amount, delta, target:
"receivable"|"cash", status, amount, action_reference}]` and
`correction_impact` (the signed total delta for that day). `amount` is the signed
money delta, `delta` the per-share change, and `action_reference` points at the
original ex-date entitlement, whose stored `cash_per_share`/`amount` are never
rewritten. The update output summarises each version's corrections and impact,
and `shadow report` lists every correction, the separate
`correction_impact_total` and every cash deficit; the delta is **not** described
as strategy trading P&L (`total_return` is still NAV-based, with the separate
correction impact shown next to it). The correction day commits the event, the
adjusted balance and the resulting NAV in the same version × session
transaction.

**Synthetic scope.** This remains a synthetic mechanism test: synthetic
snapshot/journal, synthetic prices and a synthetic feed, with no real notice,
real market data, broker or network. It demonstrates the accounting and
idempotency rules of #16/#33; it is not evidence of real forward performance and
does not complete the real-chain acceptance of #35.

#### 7. Multi-version forward comparison (#34)

`prajnatrade shadow compare` is a **read-only** comparison of the forward
history in one ledger. It never runs a strategy, never fills an order, never
creates a ledger and never reads a backtest result: it selects the committed
rows of the requested versions and computes a small, deterministic report. It
is operator-only — `prajnatrade-agent` has no `shadow` command at all.

**Common interval.** Only dates committed for **every** requested version are
candidates. They are split into maximal runs of *consecutive* sessions in the
supplied synthetic feed calendar, and the longest run is chosen; ties go to the
latest run. Fewer than two sessions in that run makes the comparison
`comparable: false` with reason `INSUFFICIENT_COMMON_SESSIONS` and each
version's committed coverage (`committed_coverage.dates/sessions/
first_committed_date/last_committed_date`), but no metrics. All requested
versions must cover the chosen run: a third version with too little common
history makes the whole request incomparable rather than being silently
dropped or replaced by a shorter pair. There is **no interpolation**, no
re-dating and no mixing of backtest or synthetic-snapshot results; the only
inputs are the ledger's verified committed day rows and the feed calendar. The
document carries no generated timestamps, so identical inputs produce
byte-identical output.

**Normalisation.** Each version is rebased to the first session of the chosen
run: `starting_nav` is that day's closing NAV, `ending_nav` the last day's
closing NAV, `cumulative_return = ending_nav / starting_nav - 1`, and
`max_drawdown` is the worst peak-to-trough drawdown of the run's NAV series
(negative, as in `backtest.py`). The pre-run history is never used to
normalise and the versions share no cash or positions.

**Run cash flows.** `fees_paid` sums the run's `fees.day`, `dividends_paid` sums
the run's `dividends.payments[].amount` (actual pay-date transfers), and
`unfilled_orders` lists every non-`filled` terminal order in the run (with its
`status` and `reason`, e.g. `missing_open`, `missed_deadline`,
`insufficient_cash`, `insufficient_shares`) plus `unfilled_order_count`. A
genuine evidenced non-fill is reported, not treated as a block.

**Corrections are separate.** `correction_impact` is the run's signed sum of
the day-level correction deltas, `corrections` lists the applied revision
records, and `cash_deficits` / `cash_deficit_total` surface every
correction-caused negative cash balance. The strategy result excludes the
correction:

```text
strategy_return = (ending_nav - correction_impact) / starting_nav - 1
```

so a verified dividend revision is never laundered into strategy P&L; the
NAV-based `cumulative_return` still includes it, and the two differ by exactly
`-correction_impact / starting_nav`.

**Output schema.** The command prints (and, with `--output`, writes the same
bytes to) a deterministic document:

```json
{"schema_version": 1, "kind": "shadow_forward_comparison",
 "scope": "synthetic",
 "versions": ["<requested id>", "<requested id>"],
 "comparable": true, "reason": null,
 "comparison": {"comparable": true, "reason": null},
 "common_interval": {"start": "2030-01-04", "end": "2030-01-09", "sessions": 4},
 "per_version": {"<release id>": {
   "release_id": "<release id>", "included": true,
   "date_range": {"start": "2030-01-04", "end": "2030-01-09"},
   "sessions": 4, "committed_coverage": {"dates": ["..."], "sessions": 6,
   "first_committed_date": "2030-01-02",
   "last_committed_date": "2030-01-09"},
   "starting_nav": 100000.0, "ending_nav": 101234.5,
   "cumulative_return": 0.012345, "max_drawdown": -0.004,
   "fees_paid": 61.2, "dividends_paid": 399.7,
   "unfilled_orders": [{"date": "2030-01-08", "order_id": "...",
     "symbol": "518880", "side": "buy", "quantity": 10306.9,
     "status": "missing_open", "reason": "..."}],
   "unfilled_order_count": 1,
   "correction_impact": -39.97, "corrections": [{"..." : "..."}],
   "cash_deficits": [{"date": "2030-01-11", "amount": 12.5,
     "caused_by": "dividend_correction", "financed": false,
     "forced_sale": false, "revision_ids": ["..."]}],
   "cash_deficit_total": 12.5,
   "strategy_return": 0.012745}}}
```

The requested order is preserved in `versions`; `per_version` is a map keyed by
the exact release ID and every entry also repeats `release_id` so the document
survives `sort_keys` serialisation. When incomparable, `common_interval` is
`{start: null, end: null, sessions: 0}`, every entry has `included: false`, the
reason and its committed coverage, and the metric fields are `null`/empty.

**Read-only integrity.** Comparison reads only committed, content-addressed
ledger rows and their frozen inputs (the same verified reader as
`shadow.iter_committed_days`). A tampered day or input is a hard
`LEDGER_INTEGRITY_FAILED` rejection (exit 2, no output written); a missing
ledger is `LEDGER_NOT_FOUND`; a version with no committed history is
`VERSION_NOT_FOUND`; a committed date outside the comparison feed's calendar is
`FEED_CALENDAR_MISMATCH`; malformed version lists are `INVALID_VERSIONS` /
`INVALID_RELEASE_ID`; an unreadable feed is `INVALID_FEED`. Nothing is
recomputed, re-filled or recovered by falling back to another ledger, feed or
store.

**Synthetic scope.** The comparison is synthetic engineering evidence: synthetic
releases, synthetic feed and synthetic prices, with no real forward sessions,
no broker and no network. It demonstrates the #9/#34 comparison rules; it is
not evidence of real forward performance and does not satisfy the real-chain
gate of #35 (two real independently validated releases over at least two
common committed real forward sessions).

**Other limits.**

- The day result is an extensible structured object with the `corrections`
  records above, and `shadow.iter_committed_days(ledger, version_id=None)`
  yields the same verified day records used by the comparison. The update loop
  is version-independent: every version has fully independent cash, positions,
  orders, fees, dividends and NAV.
- Only the four declarative strategy families are executed. A release whose
  frozen strategy family is generated Python is blocked
  (`UNSUPPORTED_STRATEGY`): Shadow Live does not run generated Python locally.
- The release artifact's evaluation-time `execution_rules` describe the sealed
  runner: `missing_required_open` is `whole_rebalance_not_filled` under the
  legacy rules and `symbol_not_filled` under `actual-open-v1`. Shadow Live
  follows #9/#32 instead: a confirmed missing open expires only that order, and
  orders are concrete frozen quantities that are never resized at settlement.
- Buy quantities account for the frozen fees and slippage at the signal close;
  a price gap up can still make a frozen buy unaffordable, which is recorded as
  `insufficient_cash` rather than resized. A share adjustment between signal
  and open is applied to the holdings but never retroactively rescales the
  frozen order, so a stale oversized sell becomes `insufficient_shares` rather
  than a short.
- Self-consistency checks (non-negative cash and positions, one terminal state
  per order, no duplicate order IDs, no new signals while paused/retired) are
  enforced by the day computation and covered by the public-CLI tests. This
  remains synthetic mechanism evidence: no real broker, no real orders, no real
  market data and no end-to-end acceptance claim.

## End-to-end acceptance (#35, synthetic tooling only — not passed)

`prajnatrade acceptance report` is the operator-only acceptance checklist for
issue #17. It validates one strict evidence manifest and renders a deterministic
Markdown or JSON report from that manifest alone — no trusted store, ledger,
sealed partition or evaluation is read and nothing is re-run:

```sh
prajnatrade acceptance report \
  --input docs/acceptance/issue-17-manifest.json \
  --output docs/acceptance/issue-17-report.md   # or report.json
```

The manifest contains `delivery`, the six required scenarios and `real_chain`.
Version 2 records each release's policy, direction ID, Holdout range,
authorization ID, independence confirmation and an evidence reference. Its
forward rows carry the corresponding release ID, signal date, next session,
terminal order state and ledger hash. The real-chain gate checks the
consecutive common sessions for a qualifying pair of releases. Version 1
remains readable for historical evidence. Each scenario records status
(`verified`, `blocked`, `failed`, `not_run`), scope (synthetic/real, Mock/real
Agent, historical/forward), expected/observed, evidence references and
missing/unblock blockers. A `verified` scenario needs evidence and non-empty
findings, a `blocked` one needs blockers with missing/unblock, and a `failed`
one needs evidence plus a recorded deviation.

The verdict cannot over-claim. `end_to_end_passed` requires all six scenarios
verified **and** a real chain from at least one real-Agent candidate through
experiment → freeze → both protected stages → human approval → immutable release
→ real forward ledger, with at least two real independently validated,
human-approved releases and at least two consecutive, common, committed real
forward trading days. Synthetic-only evidence marks a scenario verified only for
its synthetic scope and never satisfies the real-chain gate; scenarios 2–6 must
carry real-scope evidence. Anything short of the full gate is reported as
`failed`, `blocked` or `not_run`, with explicit blockers and a plain statement
that the six-scenario rule was not met. The command is operator-only:
`prajnatrade-agent` has no `acceptance` command.

Issues #43 and #50 add synthetic Research Loop and Evidence Loop tracking only;
they add no acceptance scenario and do not change the #17 verdict.

**The overall #17 verdict remains blocked.** The tests and CLI scenario and
the historical real five-ETF baseline, bounded Mock campaign and controlled
model iteration are verified (4 of 6).
@ZombieIce accepted the two cost scenarios and both halt-related NAV gaps.
The real protected evaluation and real forward scenarios remain blocked. The
real candidate-to-release chain is empty.
See the [acceptance report](docs/acceptance/issue-17-report.md) and the
[signed Discovery review](docs/research/issue-24-researcher-handoff.md).

Each real forward day must also prove the after-close signal → next-open
reconciliation: `signal_date`, `orders_persisted_before_open`, the next session,
the order's terminal state (`filled` or `evidenced_not_filled`), and the trusted
`ledger_day_id`/`result_hash`. A committed, common, real day without that
reconciliation never satisfies the gate.

## Deferred structural refactors

The #17 work deliberately does not include two behaviour-preserving refactors:
splitting the largest functions in `shadow.py`, `release.py` and
`protected_eval.py`, and replacing the `cli.py` dispatch chain with a command
registry. They need separate review; the public cross-module helper seams and
the removed middlemen are recorded in
[`docs/adr/0001-synthetic-only-protected-research.md`](docs/adr/0001-synthetic-only-protected-research.md).
Domain terms live in [`GLOSSARY.md`](GLOSSARY.md).
