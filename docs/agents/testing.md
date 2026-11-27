# Testing

The suite uses synthetic fixtures and runs from the repository root. Provider integration tests use a local HTTP stub.

## Commands

```bash
# Fast mode skips tests marked "isolation".
scripts/test.sh fast

# Full mode: the entire suite, including real-sandbox tests.
scripts/test.sh full

# Anything else is passed to pytest.
scripts/test.sh tests/test_acceptance_cli.py -q
```

`scripts/test.sh` defaults `PYTHON` to `python`, sets `PYTHONPATH=src` and
`PYTHONDONTWRITEBYTECODE=1`, enables `pytest-xdist` (`-n auto --dist worksteal`)
when installed (set `PYTEST_WORKERS=0` for serial runs), and prints the mode
and command. Point it at a specific interpreter with
`PYTHON=/path/to/python scripts/test.sh fast`.

A single test file, without the wrapper:

```bash
PYTHONPATH=src python -m pytest -q tests/test_acceptance_cli.py
```

[CI](../../.github/workflows/tests.yml) splits the suite across parallel jobs:
`pytest (fast, ubuntu)` runs `-m "not isolation"` with `pytest-xdist` on Linux,
`pytest (isolation, macos)` runs `-m isolation` with `pytest-xdist` on macOS,
and `ruff` runs on Linux. Run the relevant CLI tests first for local iteration.
If a provider test raises `PermissionError` while binding a loopback HTTP
server, run that test on the host or use the full CI result for the same commit.

## Isolation tests

Some tests exercise the real macOS `sandbox_init` boundary (isolation,
protected evaluation, release, and shadow paths). That requires the process to
be able to install a seatbelt profile, which fails when the suite itself is
already running inside the DSH sandbox — a nested sandbox cannot create its own
boundary. Those tests are marked with the `isolation` pytest marker (registered
in `pyproject.toml`):

```bash
python -m pytest -q -m isolation     # only the real-sandbox tests, on the host
python -m pytest -q -m "not isolation"
```

In a nested/agent session use `scripts/test.sh fast`; run `full` on the host or
let CI do it. A failure that names `sandbox_init`, `sandbox_install_failed`, or
an unavailable sandbox runtime is usually this boundary, not a product bug —
confirm on the host before changing code.

## Snapshot schema and execution-rule changes

For changes to the snapshot schema or matching rules, verify both the
acquisition conversion and direct `snapshot seal` with synthetic inputs.
Use the same cases to check the public manifest and the Formal Validation
preflight. A known blocking gap names its session and symbol and leaves the
allowance at zero before the trailing read. Use the matching tests to verify
fills and cash, including the effect of realized sell proceeds.

Check Protected Evaluation's supported versions at its public plan entry
point and the frozen runtime fingerprints for changes to shared execution
dependencies. Include these checks when the affected consumers are outside
the diff. The module and test entry points are in
[Architecture](architecture.md).

## Research Loop and Evidence Loop tests (#43)

`tests/test_research_loop_cli.py` covers the round contract, locked versions and
Lightweight Validation; `tests/test_version_review_cli.py` covers approve/return/
end, manual simulated-tracking admission and platform-fix revalidation;
`tests/test_corrected_evidence_cli.py` covers corrected-successor evidence
(lineage, frozen interval/data-kind/prior history, recomputed Discovery runs
and the affected diff); `tests/test_evidence_loop_cli.py` covers the observation
recommendation, due/filled/unfilled opportunities, scoped pauses, review
triggers and dual-version tracking. All four are **fast**: they drive the
public CLI on synthetic sealed snapshots, Lightweight Validation runs
declarative strategies in the trusted controller, and the review/evidence tests
build `manual_tracking` artifacts and ledger fixtures directly instead of
running a protected evaluation. They are not marked `isolation`; see
[`tests/README.md`](../../tests/README.md) for the rule if that changes.

## Discovery split tests (#50-T1)

`tests/test_discovery_split_cli.py` covers the opt-in 70/30 split of a
Campaign's sealed Discovery interval. It drives `campaign create
--discovery-split 70/30` and the v2 `round open/submit/status` protocol through
the public CLI on synthetic snapshots, and recomputes the canonical slice
hashes independently (the 15-session fixture seals Discovery=9 and gives
development 6 / trailing 3, a session count not divisible by 10). It checks the
development-only Campaign copy and its stored bytes, runs the same strategy on
two snapshots whose development rows are identical but whose trailing rows
differ and asserts identical signals, fills, equity and metrics, asserts the
unchanged v1 kickoff/locked/receipt shape, and includes one direct unit test of
`snapshot.verify_discovery_split` for tampered descriptors and payloads. The
file is fast and not marked `isolation`.

## Dual-loop synthetic acceptance (#35)

`tests/test_issue35_synthetic_acceptance_cli.py` follows two locked versions
from one 70/30 Discovery split through formal Validation, manual
simulated-tracking approval and daily Shadow Live updates. It verifies the
orders against their next-session terminal states and recomputes the common
interval and both full histories from the verified ledger reports. The test
runs in fast mode. The [evidence index](../acceptance/issue-35-synthetic.md)
lists the replay command and retained artifacts.

## Authorized full-history tests (#61)

`tests/test_full_history_cli.py` drives the public `snapshot seal --full-history`,
`campaign create --discovery-split 70/30` and `round open` commands using
synthetic-only fixtures, including schema-2/3 inputs marked as real to exercise
real provenance without using market data. It asserts the 1,094-session 765/329 split, content
fingerprints, warm-up versus actual scoring days, authorization binding, and
disclosure of prior feedback for a successor snapshot with overlapping dates.
The test also confirms that this mode does not turn its research Validation
segment into protected-partition access.

## Actual daily open tests (#56)

`tests/test_actual_open_cli.py` covers the schema-3 real contract and the
`actual-open-v1` execution rule. It owns the reusable synthetic schema-3 fixture
builders (`schema_v3_fixture`, `seal_v3`, `day_at` and the halt/suspension/
conflict mutators); #58 and #59 import them instead of copying them. It seals a
schema-3 manifest and asserts the `execution_evidence` policy block, one row per
non-trading sealed session/symbol, and the `actual_open_after_halt` /
`confirmed_no_trade` / `missing_required_open` / `source_conflict` /
`unknown_status` counts, plus that the historical `gaps` shape is unchanged and
an evidenced actual open is no longer a raw-bar gap. The `execution_evidence`
block carries exactly `policy`, `rows` and `counts` (the manifest's own
`schema_version` gates it; there is no unread inner copy) and it carries
references only: a Final Holdout halted-at-open row (actual open `12.34`) and a
source conflict (`12.34`/`12.39`) must not put either quote into
`snapshot manifest`, no evidence row has an `open` or `open_conflict` key, a
conflict exposes the two sources as `conflict_sources`, and the sealed
Discovery payload still returns its prices. Negative seal cases reject a
missing or unsealed price source, a resumption-status notice reused as the
price source and a price archive reused as the status notice (the evidence role
kinds), a missing `status_evidence_id`, an undeclared `raw_price_basis`, an
actual open whose halt window covers the whole session or whose `volume` is
zero (a hand-built row also checks `is_executable_open` and `unfilled_reason`
directly), a null `open_conflict` that would otherwise hide a trading row's
missing open, and every malformed `open_conflict` (an executable open, fewer
than two sources, duplicate sources, unsealed evidence, non-positive or
identical opens, including on a `trading` row that keeps its other OHLCV
fields, and on a `suspended` row where a no-trade day has no open to dispute)
and a schema-2 row carrying any of the three schema-3 keys. A valid trading row
carrying `open_conflict: null` seals with no execution-evidence row (an explicit
null is absent, not a conflict). The public-CLI
price test runs the same target on a normal-open sample, a resumed-halt sample
with the same daily open and a variant whose execution-day close differs: both
the candidate and the benchmark must produce the same fill date, base price,
quantity, fee, ending cash and sealed `open_evidence`, the close variant may
change only that day's valuation, and a `next-open-v1` control cannot fill the
halted day. A non-zero `--slippage-bps` case repeats the normal-open/resumed-halt
pair and recomputes every fill price as the sealed actual open moved by that
rate, asserts the recorded slippage cost is identical and reconciles the
post-fill cash against the same fee/slippage formulas. A v1 `round validate`
receipt on a schema-3 `actual-open-v1` Campaign asserts the additive provenance
identity. A public-seam check fixes the retrospective rule: a `trading` row
whose actual open is published at 15:05 is executable under `actual-open-v1`
(positive volume still required) while `next-open-v1`/`retry-initial-v1` and
the synthetic `available_at` seam keep their exact 09:30 gates. The file is
fast and not marked `isolation`.

`tests/test_execution_rules_cli.py` owns the matching behaviour of the frozen
execution rules and imports those schema-3 builders. For #58 it drives
schema-3 Campaigns through the public CLI and asserts:

- **Five-ETF basket.** On the entry day one symbol is a confirmed whole-day
  suspension, one resumed with a trusted actual open and three trade normally.
  Four symbols fill at their own actual opens, the suspended one is recorded
  unfilled with `confirmed_no_trade`, and cash, fees, holdings and the reliable
  close valuation are recomputed from the fill rows. The attempt is
  `partially_filled` with `basket_complete: false`, `metrics.orders_partially_filled`
  is one, and `position_evidence` reports the partial build. The equal-weight
  candidate and the frozen benchmark agree on attempts, fills, equity, metrics
  and position evidence. Changing only the suspended symbol's tradability makes
  it fill and the account delta is recomputed from the new fill and the
  proportional scale.
- **Retrospective matching.** The reviewer's repro: five ETFs are ordered
  before the open and one symbol's actual open is 10 in both samples while its
  quote is published only at 15:05. Marked normal and marked resumed-halt, both
  samples fill 5/5 with the same fill date, base price, quantity, fee and cash
  for the candidate and the benchmark; a `next-open-v1` control keeps the 09:30
  gate and records the late symbol as a missing open.
- **Sell then buy.** A declarative `momentum` strategy (`lookback: 2`,
  `top_k: 1`, `cash_if_negative: false`) over engineered per-symbol closes
  starts fully invested in A, then targets B with no original cash. The fills
  are sell A then buy B, B's debit equals A's realized sell net, and quantities,
  fees, cash and the attempt terminal state are hand-recomputed. When A is a
  confirmed whole-day suspension at the switch, the held symbol has no
  trustworthy open: B does not fill, A's shares are retained at the next
  confirmed mark, fees and cash are unchanged, the attempt is `not_filled`, A's
  unfilled reason is `confirmed_no_trade` and the priced buyer is listed with
  `equity_basis_unknown`.
- **Explicit exit vs unknown equity.** With two holdings whose target weights
  are both zero, a confirmed suspension of A leaves the day's equity basis
  unknown but B's exit is sized independently: B sells in full at its trusted
  open, cash equals the realized sell net, A keeps every share and is recorded
  `confirmed_no_trade`, and the attempt is `partially_filled` with
  `filled_symbols: [B]`. The reviewer's suspended-seller sample and the
  weight-positive freeze sample (`equity_basis_unknown` for every
  equity-dependent order) are both covered.
- **Held no-trade equity basis.** With two holdings and a target that asks for
  a positive weight on one of them, a confirmed whole-day halt on the other
  holding leaves equity unknown: no buy or retargeting fill, the priced
  equity-dependent order is `equity_basis_unknown`, while the priced holding
  whose target is zero is an explicit exit and sells in full for its realized
  net. The halted holding keeps its parcel and confirmed-close mark, NAV drops
  only by the exit fee, and the next priced decision sizes and rebalances
  normally. This is the case that used to retarget the priced holdings against
  a reduced open equity and sell them.
- **Build state.** The last positive-target attempt drives
  `position_evidence.basket_build_state`: the five-ETF entry basket is
  `partial`, a completed target is `completed`, and a blocked attempt that
  produced no fill at all is `unfilled` (the blocked switch and the submit-once
  benchmark cover it); no positive target is `none`.
- **Unfilled accounting and no retry.** A day whose five required symbols are a
  whole-day halt, a missing required open, an unknown status and two confirmed
  suspensions records every sealed-row reason, keeps the 100000 cash, and is
  never retried automatically under `actual-open-v1`: the next attempt comes
  from a new strategy decision. The submit-once benchmark records only the
  blocked attempt and never moves cash. The same payload under
  `retry-initial-v1` reuses the blocked signal on the next open and keeps its
  exact legacy attempt shape and legacy metrics/position-evidence key sets.

`tests/test_acquisition_conversion_cli.py` covers the #57 acquisition
conversion without any network or real artifact. It imports the checked-in
`scripts/acquire_real_etf_snapshot.py` by path (its `main` is guarded), feeds
synthetic Tencent-shaped vendor bars and synthetic `open_halt` /
`full_halt_until` notices with synthetic `price_archive`/`halt` evidence items,
and calls the pure `build_snapshot_input` (which delegates to
`convert_raw_bars`). It asserts schema 3, a normal row's vendor open and price
source, the halted row's actual open `10.5` with the vendor `open_source` and a
distinct halt-notice `status_evidence_id`, and the whole-day row's null prices
with its halt notice. A `full_halt_until` notice with a same-day resume keeps
its actual open (the real 2026-11-20 case), while one whose resume is on a later
session covers the day and becomes `suspended`. The same converted document is sealed through the public
CLI and scored under `actual-open-v1`: the `execution_evidence` classifications
are `actual_open_after_halt` / `confirmed_no_trade`, the equal-weight Discovery
fill on the halt day uses base price `10.5` and echoes the sealed
`open_evidence`, and the confirmed no-trade symbol never fills (4/5). The real
45-row verification belongs to actual use. This file runs in fast mode.

## Formal trailing Validation tests (#50-T2)

`tests/test_formal_validation_cli.py` covers protocol-v2 `round validate` and
`round revalidate`. It drives the public CLI on synthetic sealed snapshots with
a 70/30 split Campaign (the 15-session fixture gives development 6 / trailing 3)
and asserts: the receipt's candidate and benchmark are accounted on the trailing
real dates only from the trailing first session with the frozen initial cash
(the trusted receipt carries per-session curves; the published projection does
not), a whole-interval v1 replay is distinguishable, a declared trailing gap
blocks before the read with zero uses, the first read counts exactly once across
a failpoint interruption, resume, terminal-receipt recovery, replay and
failed/insufficient verdicts, an exhausted allowance refuses before the read,
two subprocesses racing the last automatic slot yield exactly one read, a
compliant prehistory scores every trailing session while a short one reports
`SCORING_START_INSUFFICIENT` and keeps its use, later trailing data does not
change the first decision, `round status` audits the allowance counters,
lifecycle and per-version protocol/scoring/warmup/usage, and a trailing-only
corrected successor produces a formal revalidation epoch with the prior receipt
and affected diff while a changed development slice is refused with
`EVIDENCE_DEVELOPMENT_MISMATCH`. The file is fast and not marked `isolation`.

## Formal Validation pre-read execution gate and 329-day receipt tests (#59)

`tests/test_formal_validation_cli.py` also owns the end-to-end #56/#59 acceptance
surface. It imports the schema-3 builders from `test_actual_open_cli.py`
(`schema_v3_fixture`, `day_at`, `raw_row`, `make_actual_open_halt`,
`make_confirmed_suspension`, `make_missing_required_open`, `make_open_conflict`,
`OPEN_SOURCE`) and extends the shared builders without changing existing callers:
`split_campaign` and `formal_scenario` take an optional
`execution_rules: str | None = None` that appends `--execution-rules` only when
given (the default keeps every existing record at `next-open-v1`).

- **Pre-read gate.** A schema-3 `actual-open-v1` scenario with a
  `confirmed_no_trade` suspension inside the trailing segment is not a
  `VALIDATION_DATA_GAP`: the read proceeds, records exactly one automatic use,
  and still ends `insufficient_evidence`/`COVERAGE_INSUFFICIENT` because the
  held suspended close is a valuation gap. Evidence explains only the gap kinds
  it proves: keeping a trailing `actual_open_after_halt` row but removing that
  ETF's derived `available_at` on the same day rejects `VALIDATION_DATA_GAP`
  with `counted: false`, 0/1 uses, the `availability` gap naming the
  date/symbol and no receipt/use (`actual_open_after_halt` explains `raw_bars`
  only). A trailing `availability` gap that no `execution_evidence` row
  explains is still unexplained and rejects with `counted: false`,
  `execution_evidence: []` and the per-kind `gaps`. An explicit
  `open_conflict: null` on a valid trailing trading row is not a conflict: the
  manifest has no row for it and the read passes and counts one use.
- **329-day positive.** The fixture is `schema_v3_fixture(1824)` with
  `corporate_actions` cleared and a deterministic `1 + 0.005 * index` drift on
  raw OHLC and derived values, so the momentum candidate buys the alphabetically
  first symbol on the first trailing session. Before sealing, 45
  `make_actual_open_halt` rows are placed on 44 distinct trailing sessions (the
  first trailing session carries two halted symbols, including the candidate's
  symbol) and each row keeps its own drifted actual open. 1824 sessions give a
  1094-session sealed Discovery interval, development 765 and trailing 329.
  `round validate` must return `passed` with `scoring` and both runs at 329
  sessions/329 valued sessions ending on the trailing end, 329-point trusted
  curves per side, `usage.kind: automatic`/`sequence: 1`, exactly one
  `research_loop_validation_uses` row, a candidate and a benchmark fill on a
  halted session whose `open_evidence` equals the sealed `open_source`, and the
  receipt `provenance` carrying `snapshot_schema_version: 3`,
  `execution_rule_version: "actual-open-v1"` and
  `price_basis: "unadjusted_open"`. Replaying the same command returns the same
  `validation_id` with `reused: true` and still one use row (and
  `round status` shows one use).
- **Zero-use negatives.** The same 1824-session recipe with one trailing halt
  row replaced by `make_missing_required_open` (seal stays valid) or by
  `make_open_conflict` rejects `VALIDATION_DATA_GAP` at the gate with
  `counted: false`, `allowance.automatic_used == 0` of 1, the blocking
  `execution_evidence` row naming the date/symbol/classification and reason in
  the message, `gaps: {}`, no terminal receipt, no
  `research-loop-validations` directory, zero use/attempt rows, and
  `round status` still `research` with version validation `not_run`.
- **Receipt identity and legacy readability.** The v2 `provenance` gains the
  three flat keys; a legacy schema-1 `next-open-v1` v1 receipt has no
  `provenance` and its `checks.data_availability.snapshot` keeps the exact
  pre-#56 keys (no `schema_version` leak). A schema-1 formal trailing receipt
  publishes the same pre-#56 availability `snapshot` block, while its
  `provenance` carries `snapshot_schema_version: 1` and the legacy rule; the
  formal identity is read from the sealed read receipt, never from the
  availability block.
- **Discovery/formal identity.** One split schema-3 `actual-open-v1` Campaign
  carries an evidenced halt in the development segment and one in the trailing
  segment. The Campaign evaluation, round kickoff/locked version and formal
  receipt provenance name `actual-open-v1`, snapshot schema 3 and
  `unadjusted_open`, and the Discovery candidate's development fill and the
  formal candidate's trailing fill carry the same sealed `open_evidence` shape
  and the same fee/slippage formula (`price == raw_open`, fee on that price).
- **Formal position prerequisite.** One split schema-3 `actual-open-v1`
  Campaign carries a confirmed suspended symbol on the benchmark's entry day
  (the first trailing session); the pre-read gate accepts it as benign. The run
  proceeds and consumes exactly one counted use, but the benchmark filled only
  4/5 required symbols, so the receipt is
  `insufficient_evidence`/`POSITION_EVIDENCE_INSUFFICIENT` with
  `checks.position.ok` false, both sides' `basket_build_state` published
  (`partial` for the benchmark, `completed` for the candidate), the benchmark's
  four fills and `fill_count` recorded, and the #56 `provenance` identity
  intact.

The 329-day tests are subprocess-heavy (about 8-11 CLI calls each, a ~4.4 MB
payload, ~10 s per test) but not marked `isolation`; the file is still fast
compared with the full suite.

## Manual Validation admission tests (#50-T3)

`tests/test_validation_admission_cli.py` covers the protocol-v2 pause after the
automatic Validation limit and the researcher's manual admission. It drives the
public CLI on the same synthetic 70/30 split Campaigns as #50-T2 (the 15-session
fixture gives development 6 / trailing 3) and asserts: the exhausted limit
refuses before the read while `lifecycle` is `awaiting_validation_admission` and
Discovery experiments/`round submit` keep working, `round admit` records one
write-once content-addressed admission (reviewer, reason, comparison,
selection impact) with no counter change, replays identical content, converges
a trusted-only crash leftover, and refuses conflicting/closed/ended/approved/
unknown/already-counted versions plus v1 rounds, the admitted version's next
validate inserts exactly one `manual` use reflected in the receipt `usage`,
another version cannot consume it, two subprocesses consuming the same
admission or racing the last slot yield one counted read, an interrupted manual
read keeps its use and resume converges, close-at-limit records
`selected_validation`/`validation_allowance`, early no-candidate close with
Campaign budget remaining still works, and a protocol-v1 round keeps its legacy
status/closure/receipt shapes. The file is fast and not marked `isolation`.

## Validation feedback, detail and cross-round reuse tests (#50-T4)

`tests/test_validation_feedback_cli.py` covers the T4 traceability surface. It
drives the agent and operator CLI on the same synthetic 70/30 split Campaigns as
#50-T2/T3 and imports the formal-validation, split, review, admission and
research-loop builders. It asserts: the v2 receipt `summary` (return, drawdown,
costs, fills, verdict reason) reaches `round status` and the agent view while
`equity_curve`/`curves`, ledger/paths, `query_debited`/
`opened_protected_source` internals and review reviewer/reason stay hidden, and
a legacy summary keeps its v1 shape; `round detail` records the question and
normalized records, returns only the requested published blocks, replays without
a new row or Validation use, and rejects unknown/unavailable records and a
version without a receipt; `round feedback` links a detail access and the next
Campaign experiment, replays by content and never consumes a use, and
`revision_of`/`feedback_ids` are validated provenance outside `version_id`, with
V2 re-scored on the same protocol, trailing segment, benchmark cash, costs and
thresholds; a returned V1 is re-approved on its stored formal receipt while a
`failed` or `insufficient_evidence` formal receipt cannot be approved; a v2
`round open` discloses the prior round's used interval, overlap and Validation
use history plus its feedback on the same snapshot (or on a corrected successor
with the same frozen development content hash) and reuses the stored disclosure
across the trusted-write crash window while the old receipt keeps reading under
`discovery-replay`; and a
synthetic #35 chain links snapshot, round, version, experiment, real development
and scoring dates, warmup source, allowance usage (including a manual admission
read), feedback, review state and tracking. The file is fast and not marked
`isolation`.

## Docker tests

`tests/test_container_executor.py` exercises the real Docker/Colima boundary for
generated Python and is marked `docker`. It skips cleanly when the Docker CLI,
the daemon or the runner image is unavailable, so the default suite does not
require Docker. To run it, first build the runner image and point Docker at
Colima:

```bash
DOCKER_HOST=unix://$HOME/.colima/default/docker.sock \
  scripts/issue27-runner/build.sh
DOCKER_HOST=unix://$HOME/.colima/default/docker.sock \
  PYTHONPATH=src python -m pytest -q -m docker tests/test_container_executor.py
```

`PRAJNA_ISSUE27_IMAGE` overrides the image tag (default
`prajnatrade-issue27-runner:local`); `PRAJNA_DOCKER_HOST` overrides
`DOCKER_HOST`. The image build needs network for the pinned wheels; the tests
run it with `--network none`.

## venv and pip inside the sandbox

Inside the DSH sandbox, `python -m venv` and `pip install` can fail because
`platform.mac_ver()` comes back empty. Do not try to bootstrap a venv there.
Use an already-created venv outside the sandbox, for example the known working
one on this machine:

```bash
PYTHON=/Users/yibinzhang/go/src/copilot-worktrees/PrajnaTrade/zombieice-fluffy-giggle/.venv/bin/python \
  scripts/test.sh fast
```

That path is machine-local; it exists so a sandboxed session does not have to
create an environment. Do not copy it into committed code or generated
artifacts.

## Checked-in artifacts

Reports and other generated artifacts that are committed (acceptance evidence,
experiment receipts, fixtures) must not contain machine-specific paths. Use
repository-relative paths or placeholders such as `$PYTHON`, never absolute
home-directory or venv paths. A leaked absolute path makes the artifact
unreproducible on another machine and has had to be fixed before.

## Discovery quarterly summaries (#62)

`tests/test_discovery_summary.py` checks synthetic continuous-ledger quarter
arithmetic, including a next-quarter first-day loss, drawdown from the previous
quarter close, missing end/prior NAV boundaries, and retained actual dates.
`tests/test_experiment_cli.py` verifies that a completed experiment's receipt
and later `experiment status` expose the same identity-bound summary, and that
daily-return coverage counts only truly adjacent valued trading sessions.

## Discovery evidence precheck (#63)

`tests/test_formal_validation_cli.py` drives the public `round validate` seam
with synthetic receipts to verify identity binding and pre-read hard-condition
and common-scoring checks. A failed hard condition, unavailable required metric,
unmet scoring prerequisite or mismatched summary leaves the trailing payload
unread and the Validation allowance unchanged; a missing disclosure-only metric
and one-quarter performance disclosure may proceed. Counted Validation replay
and interruption recovery keep #50's existing semantics.

## Frozen decision-pause audit (#82)

`tests/test_formal_validation_cli.py` drives public `round validate` with
synthetic schema-3 snapshots under a frozen 15:30 cutoff. It contrasts timely
confirmed suspensions, post-cutoff evidence, and unexplained/independent
valuation gaps; verifies per-account pause counts, consecutive decision days,
monthly deferral outcomes, no new target, prior-order settlement and daily NAV
reconciliation; and checks default disclosure versus a frozen per-account pause
cap. Invalid evidence and exceeded pre-read caps consume zero Validation uses,
while a legal first read and replay consume one in total. The formal receipt
also publishes the trailing segment's separate pause audit.

## Suspended holding carry-forward valuation (#68: #69, #70, #71)

Three fast, non-`isolation` public-CLI suites cover Spec #68 (ADR 0003) on
synthetic fixtures (including schema-3 `data_kind: "real"` fixtures used
strictly for provenance and evidence semantics, plus schema-1/schema-2
controls):

- `tests/test_suspended_valuation_cli.py` (#69 / #68-A): drives `snapshot seal`,
  `campaign create --valuation-policy carry-forward-suspended-v1`,
  `experiment submit`, `candidate freeze` and `evaluation plan`. Verifies frozen
  valuation policy identity and Protected Evaluation rejection, the zero-cost
  100@10 -> 1,000 suspended -> 900 resumed -> -10% drawdown arithmetic with
  source-close/evidence/carried-session audit trails and `strict-close-v1`
  legacy control, candidate/benchmark parity and differing holdings, staggered
  multi-ETF halts and unheld-symbol gap isolation, continuous multi-day
  suspensions across weekends, calendar quarters and `--full-history`
  authorized/scoring end boundaries (plus negative tail-unknown rejection and
  schema-2 real suspended-tail boundary alignment) with quarterly summary
  reconciliation and end-of-period carry-forward disclosure, every failure and
  recovery path (missing authorized prior close, unknown gap, late notice after
  cutoff, conflicting status/trade evidence including whole-session
  `partial_halt` with zero/null volume and a contradictory close, mid-chain
  break, resumed open without close), 61-observation warmup and order-sizing
  independence from carried prices, no lookahead and account isolation, and
  fail-closed unexplained corporate actions during suspension.
- `tests/test_suspended_corporate_actions_cli.py` (#70 / #68-B): verifies
  `snapshot seal` validation and canonical sorting of corporate-action
  `sequence` numbers, evidenced ex-dividend adjustment (`valuation_price` minus
  `cash_per_share`, dividend receivable on ex-date, cash transfer on pay-date
  while still suspended) and share-split adjustment (`shares * ratio`,
  `valuation_price / ratio`) with preserved `source_close_*` and `adjustments`
  trails, same-day sequenced ordering (`div->split` vs `split->div`) and
  resumption without double-adjustment, fail-closed atomic rejection on
  ambiguous order, missing/late terms, `pay_date < ex_date`, and non-positive
  adjusted carried prices, persistence of unresolved corporate-action gaps even
  after exiting the position, per-account isolation, replay idempotency, and
  `strict-close-v1` legacy controls.
- `tests/test_formal_suspended_valuation_cli.py` (#71 / #68-C): drives the full
  70/30 split Campaign -> Discovery -> `round open`/`submit`/`validate`/
  `status`/`detail` flow under `carry-forward-suspended-v1`. Verifies frozen
  policy identity across Campaign, Round, Locked Version, Discovery summary, and
  formal/v1 receipt `provenance.valuation_policy_version`, additive
  `runs.<side>.valuation_summary` (including `ending_carried_forward`) and
  quarterly/trailing ledger reconciliation, distinct drawdown hard-condition
  outcomes (pass within cap, fail violating cap, insufficient evidence),
  zero-use Discovery precheck and sealed-manifest pre-read rejections vs
  one-use post-read holdings-dependent trailing valuation gaps, replay without
  double-debit, legacy `strict-close-v1` receipt readability, and research-side
  agent visibility boundaries.

## Local plugins, after-close decisions, whole-lot settlement & stateful E2E (#74: #75–#79)

Six fast, non-`isolation` public-CLI suites cover Spec #74 on offline synthetic fixtures while preserving all legacy semantics and #35 regressions:

- `tests/test_local_plugins_cli.py` (#75 / #74-A): explicit `--plugin-config` / `--plugin` / `PRAJNATRADE_PLUGIN_CONFIG` registration (`plugin register`, operator-only), AST transitive local dependency closure and SHA-256 fingerprinting (including thin-wrapper imported helpers), rejection of unbindable dependencies / family conflicts / unregistered families / Protected Evaluation plugin attempts, cross-stage verification across Discovery / Formal Validation / Shadow (`PLUGIN_CONTENT_CHANGED` preserving committed Shadow days), and platform-fix revalidation eligibility (`REVALIDATION_INELIGIBLE` when plugin/rules change vs zero-allowance revalidation when identical).
- `tests/test_shadow_feed_v3_valuation_cli.py` (#76 / #74-B): schema-3 Shadow feed validation (evidence roles, conflicts, corporate-action `sequence` sorting), no-lookahead `shadow export-feed`, and Shadow `carry-forward-suspended-v1` valuation (ex-dividend receivable/pay-date transfers, share splits, resumption without double-adjustment, and fail-closed gap/conflict/late-notice blockers).
- `tests/test_decision_timing_observations_cli.py` (#77 / #74-C): frozen `after-close-common-v1` cutoff/timezone and `history_requirements` propagation, common-valid-close selection with whole-day suspension backfill, single-day signal pause on missing/late/conflicting data while prior orders settle independently, and pre-enablement warmup isolation.
- `tests/test_open_lot_settlement_cli.py` (#78 / #74-D, #81-B / #83): frozen `actual-open-lot-v1` and `--lot-size`/`--lot-sizes`, pre-open `open_weight`/`fixed_amount` order freezing, open-time whole-lot matching with sells-first realized proceeds, proportional scaling, `lot_rounding_residual` vs `cash_constrained`, `equity_basis_unknown` with explicit exits & fixed-amount orders still executing, and `daily_simulation_assumptions` disclosure. Public-CLI synthetic cases reconcile pre-allocation target lots, allocated amounts, actual fills, shortfall and completion metrics for $2,050/$3,000 targets against $2,000 cash, non-zero costs across multiple symbols, zero-trade targets and a missing-open reason; Discovery, Formal Validation and Shadow expose matching order evidence, and anchors advance only for complete or zero-trade-satisfied targets.
- `tests/test_stateful_rebalance_cli.py` (#79 / #74-E): all 8 strategy state fields and `prior_close_weights` contract across Discovery/Validation/Shadow, zero-trade satisfied target anchor updates (200 held vs 205 target @ lot=100), non-advancement on `cash_constrained`/suspended/partial fills, cross-month signal attribution, non-monthly `risk_control` isolation from `last_monthly_filled_target`, intra-month deferral recovery / whole-month `not_executed` / no auto-resend of submitted partial orders, own initial account startup (`initial_cash`/`initial_positions`) & warmup, restart/replay idempotency, and `shadow migrate-state` for both uniquely reconstructible and `STATE_NOT_UNIQUELY_RECONSTRUCTIBLE` ledgers.
- `tests/test_issue74_e2e_synthetic_acceptance_cli.py` (#79 / #74): full public-CLI E2E synthetic acceptance reusing #35 helpers across seal → plugin register → campaign → Discovery → Formal Validation → platform-fix revalidate → manual approval → `shadow export-feed`/`update`/`report`/`compare` + replay, with exact day-by-day expected/actual reconciliation and single-mutation negative counterexamples. The [evidence index](../acceptance/issue-74-synthetic.md) documents the replay commands, daily ledger table, counterexamples and artifact keys.
