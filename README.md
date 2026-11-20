# PrajnaTrade

PrajnaTrade is a local-first research CLI for a fixed portfolio universe:
ETFs `511260`, `518880`, `515080`, `513300`, `159612`, plus cash.

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

Validate a dataset before running a baseline:

```sh
prajnatrade data validate \
  --data bars.csv --price-basis total_return --data-kind real

prajnatrade baseline \
  --data bars.csv --price-basis total_return --data-kind real \
  --initial-cash 100000 --transaction-cost-bps 5 --slippage-bps 0 \
  --output results/baseline.json
```

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

`snapshot seal` accepts **synthetic only** version-1 JSON and writes a shared,
content-addressed snapshot outside any Git repository. This does not acquire or
verify real market data. A trusted operator prepares the input and store:

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

The legacy `research`, `baseline`, and validation commands described below do
not consume this sealed snapshot interface and do not inherit its checks.
Campaigns created before this change may have an incompatible fingerprint and
must be recreated rather than resumed or validated.

## Research, validation, and reports

Mock campaigns are deterministic orchestration checks, not AI research. They
use recorded decisions to refine a retained or investigated direction or
switch after a rejection or branch. The unparameterized equal-weight control
can only be retested, not tuned. Mock proposals remain limited to the supported
declarative strategy families unless trusted-local Python is enabled. Campaigns
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
executed. To allow generated Python, explicitly pass
`--trusted-local-generated-python` to each `research run`, `resume`, `validate`,
or `holdout` command that executes it. The generated source and parameters are
retained with the experiment. Source defines `strategy(history, state)` and may
use the globals `ETF_SYMBOLS`, `CASH`, and `parameters`.

**This is not a sandbox.** Generated Python runs with the full privileges of
the local process: it can access files, credentials and the network, and can
interfere with the research engine. Keep holdout data outside the process's
access until confirmation; the CLI cannot enforce filesystem or network
isolation. Only enable generated Python for code you trust; do not use it for
unattended execution of untrusted model output.

Generated proposals must declare each numeric parameter in `parameter_schema`:
`{"parameters":{"window":{"type":"integer","default":10,"min":2,"max":20,"step":1}},"constraints":[]}`.
Types are `integer` or `number`; constraints compare a named parameter to a
number or another parameter using `<`, `<=`, `==`, `>=`, or `>`, for example
`{"left":"fast","op":"<","right":"slow"}`. Use
`{"parameters":{},"constraints":[],"no_tunable_parameters":true}` only for a
strategy with no parameters. A strategy with only non-numeric parameters is
inconclusive. This declaration cannot prove that
trusted source has no hidden tuning knobs; review the source. Legacy Python
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
