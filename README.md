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
gaps for reproducibility. This interface currently accepts synthetic sealed
snapshots only; it is not evidence of real-data correctness or OS isolation.

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

### External Discovery experiment protocol (synthetic only)

The operator seals a synthetic snapshot, initializes a project, and creates a
Campaign against its Discovery partition. Campaign creation reads Discovery
only, copies that partition into the project, and locks its snapshot ID,
evaluation rules, runtime fingerprints, and attempt/time budgets in the
project's `.prajnatrade` directory. Move that directory with the project to
preserve its identity; `--project` is just its location.

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
must be available before the next open; execution uses unadjusted opens with a
single all-or-none rebalance attempt, while NAV uses unadjusted closes, cash,
and dividend receivables. Missing required inputs produce signal gaps,
unfilled orders, or unavailable NAV rather than substituted prices. Each
experiment includes fills, order attempts, the NAV curve, fingerprints, and an
equal-weight buy-and-hold comparison over the same Discovery interval and
evaluation settings.

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

### Protected Validation (MVP-10, synthetic only)

`prajnatrade evaluation ...` performs one preregistered, human-authorized,
OS-isolated Validation evaluation of a frozen candidate against a sealed
synthetic Validation partition. **This is synthetic engineering evidence only.**
It does not consume real market data, does not validate a real strategy, does
not prove independent out-of-sample evidence for real markets, and does not
grant admission, approve a release, or replace human review. Final Holdout
authorization and the controlled report belong to later tickets; this workflow
fixes the two-stage plan but only executes Validation.

The trusted store is one directory outside Git (the same store is reused by
later release/Shadow Live workflows). The evaluation ledger in
`queries.sqlite` is the budget boundary: use one trusted store per project and
operator, because a copy of the store is a separate deployment with its own
ledger.

```text
<store>/
  plans/<plan_id>.json                                   # frozen plan (0400)
  authorizations/<plan_id>/validation-<auth_id>.json     # one authorization
  authorizations/<plan_id>/validation.authorized.json    # which one is used
  audit/<audit_id>.json                                  # rejected requests
  queries.sqlite                                         # debit ledger
  records/<evaluation_id>/record.json                    # trusted record
```

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
`["validation", "final_holdout"]`. The parameter neighbourhood is the existing
declarative robustness plan (or an explicit `no_tunable_parameters`), frozen
with its variants, exclusions, and thresholds.

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
`evaluation status` shows it. Final Holdout authorization is deliberately not
implemented here.

#### 3. Run the one debited Validation evaluation

```sh
prajnatrade evaluation run --store /trusted/evaluations \
  --snapshot-store /trusted/snapshots --plan-id <plan-id> \
  --stage validation --project ./strategy-A
```

Checks run **before** the sealed read: the plan's content address, the frozen
candidate (re-read and hash-checked from the project), the sealed manifest and
both protected descriptors, the project binding, the matching authorization,
and the real macOS isolation probe. On any failure the request is rejected with
a stable `error.code` (`FINGERPRINT_MISMATCH`, `AUTHORIZATION_REQUIRED`,
`ISOLATION_UNAVAILABLE`, ...), a matching `audit/<audit_id>.json` with
`opened_protected_source: false`, and no debit. There is no fallback to local
execution and no bypass flag.

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

#### 4. What the research side may see

The research-side command is read-only and cannot plan, authorize, or run:

```sh
prajnatrade-agent evaluation status --project ./strategy-A --candidate-id <id>
```

Its `validation` value is either `{"status": "pending"}` or exactly this
coarse receipt:

```json
{"schema_version": 1, "stage": "validation",
 "status": "passed|rejected|insufficient_evidence",
 "reason": "THRESHOLD_NOT_MET|COVERAGE_INSUFFICIENT|EVIDENCE_INCOMPLETE",
 "message": "..."}
```

It contains no numbers, dates, metrics, fills, curves, variants, logs,
durations, or admission state (a passing receipt has `reason: null`). A
rejected **request**
(permissions, budget, fingerprint, isolation) is not a strategy failure and
publishes no strategy receipt. The coarse receipt reflects the latest completed
evaluation of that frozen plan; deterministic replay in the same trusted store
returns the same payload.

The operator view is `prajnatrade evaluation status --store <store>
--plan-id <plan-id>`: the frozen plan, authorizations, query ledger state, and
each stage's evaluation summary.
