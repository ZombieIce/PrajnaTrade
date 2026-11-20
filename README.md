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

### Protected Validation and Final Holdout (MVP-10/MVP-11, synthetic only)

`prajnatrade evaluation ...` performs one preregistered, human-authorized,
OS-isolated evaluation of a frozen candidate against sealed synthetic
Validation **and** Final Holdout partitions, each debited once. Validation and
Final Holdout have separate human authorizations; Final Holdout is only
authorized after the same frozen plan's Validation passed, and the controlled
report is rendered from the immutable records. **This is synthetic engineering
evidence only.** It does not consume real market data, does not validate a real
strategy, does not prove independent out-of-sample evidence for real markets,
and does not grant admission, approve a release, or replace human review.

The trusted store is one directory outside Git (the same store is reused by
later release/Shadow Live workflows). The evaluation ledger in
`queries.sqlite` is the budget boundary: use one trusted store per project and
operator, because a copy of the store is a separate deployment with its own
ledger.

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
and the real macOS isolation probe. On any failure the request is rejected with
a stable `error.code` (`FINGERPRINT_MISMATCH`, `AUTHORIZATION_REQUIRED`,
`ISOLATION_UNAVAILABLE`, ...), a matching `audit/<audit_id>.json` with
`opened_protected_source: false`, and no debit. There is no fallback to local
execution and no bypass flag.

Protected Validation checks the **macOS sandbox boundary only**: it evaluates
declarative strategies in the trusted controller and never executes untrusted
or generated Python. On a host where the generated-Python worker's 2 GiB
address-space limit cannot be enforced, the probe retries the boundary without
RLIMIT_AS and records `memory_limit_enforced: false` together with
`failure_reason: "memory_limit_unavailable"`; `verified` still requires
`sandbox_init`, protected read/write denial, network denial and process-creation
denial to pass. A passed Validation therefore does **not** imply that
generated-Python memory, time or process isolation is verified: that claim
remains blocked by #12/#18/#27, and the generated-Python gate
(`--isolated-generated-python`) still refuses to execute when the memory limit
is unavailable.

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
total-return values from earlier partitions. The Holdout prior is Discovery
(research-visible) plus a derived export written into the Validation record
(`prior_export_for_holdout`: the tail of Validation sessions needed by the
frozen signal window and regime lookback, bound to the Validation evaluation id,
plan, candidate, snapshot and partition hash). The Holdout run loads that export
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

### Immutable release and lifecycle (MVP-12, synthetic only)

`prajnatrade release ...` turns an explicitly human-approved frozen candidate
into a uniquely identified, immutable release version and lets an operator
enable, pause, resume and retire tracking of that version at pre-registered
after-close signal dates. **This is synthetic mechanism evidence only.** It does
not approve a real strategy, does not prove real-market performance, does not
read real market data. Shadow Live accounting is built on top of these
artifacts (see the next section). The research side (`prajnatrade-agent`) has
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
- The release artifact is the admission record; the evaluation report remains a
  non-admission presentation. Admission of a *real* strategy and real forward
  tracking remain blocked (#12/#18/#27); the synthetic Shadow Live mechanism
  below is engineering evidence only.
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

### Shadow Live one-day ledger (MVP-13, synthetic only)

`prajnatrade shadow update` accounts **one manually triggered synthetic trading
day** for every tracked immutable release version, and `prajnatrade shadow
report` dumps the committed ledger for trusted audit. Shadow Live is **virtual
accounting only**: it never connects to a broker, never sends or simulates a
real order, never reads real market data and never uses the network. Every
number below is synthetic engineering evidence, not a strategy result.

A release ID is the account boundary. Each version keeps its own cash,
positions, orders, fees, slippage, dividends and NAV; prices may be shared,
money never is. `prajnatrade-agent` has no `shadow` command at all, so no
Shadow Live state can flow back to a research Agent.

```text
prajnatrade shadow update --ledger /trusted/shadow-ledger \
  --store /trusted/evaluations --data synthetic-feed.json --date 2030-01-07 \
  [--release-id <exact-release-id> ...]
prajnatrade shadow report --ledger /trusted/shadow-ledger \
  --version <exact-release-id> --output ledger-report.json
```

#### 1. The synthetic feed (`--data`)

The feed is versioned synthetic JSON. Every row carries a timezone-aware
`available_at` and a non-empty `availability_evidence`; a missing price is an
explicit `null`, never a fabricated value.

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

- `schema_version` is 1, `data_kind` is `synthetic`, and `symbols` is exactly
  the five ETFs.
- `calendar`, `raw_bars` and `derived` are non-empty; calendar dates are unique
  and strictly increasing; every row date is in the calendar.
- `raw_bars` OHLC prices are finite and positive (or `null`), `volume` is
  finite and nonnegative (or `null`), `trading_status` is
  `trading|suspended|unknown`, and `high >= max(open, close)`,
  `low <= min(open, close)`.
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
point, the day's raw/derived/action rows, and the prior account state including
pending orders) is written and content-addressed. An input is frozen **only
when every required input is present**; a missing held close, unavailable or
suspended held input, unresolved held corporate-action terms or missing signal
input blocks that version before anything is written.

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
 "versions": [{"release_id": "...", "status": "committed", "reason": null,
               "reason_code": null, "committed_date": "2030-01-07",
               "last_committed_date": "2030-01-07", "result_hash": "...",
               "anomalies": [], "unfilled_orders": []}]}
```

| status | meaning |
| --- | --- |
| `committed` | the target day is committed with every order terminal and nothing anomalous |
| `committed_with_anomalies` | the target day is committed, but an evidenced non-fill (missing open, missed deadline, insufficient cash/shares) or a non-blocking anomaly was recorded |
| `already_committed` | the target day was already committed; the stored result hash is returned and nothing is regenerated or re-filled |
| `blocked` | that version wrote no day row and did not advance; `reason`/`reason_code` say why (e.g. `missing_close`, `missing_action_terms`, `missing_signal_input`, `frozen_input_changed`, `ledger_integrity_failed`, `release_integrity_failed`) |
| `not_started` | the release is approved but not enabled at/before the target (`NOT_ENABLED`), or is first tracked after it (`ENABLED_AFTER_TARGET`) |
| `retired` | the release is retired at the target with no ledger history to settle |

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
(`kind: shadow_live_ledger_report`, `scope: synthetic`) containing the
progress row, an integrity block, a summary (days, first/last committed date,
ending NAV, total return, fees, slippage, dividends, fills, unfilled orders,
anomalies) and every committed day record with its input hash, result hash,
`committed_at`, `status` and full result. It only reads committed records and
never re-runs a strategy.

#### 6. Hooks for #33 and #34, and limits

- The day result is an extensible structured object; `corrections: []` is
  reserved for the #33 dividend-correction deltas, and the module exposes
  `shadow.iter_committed_days(ledger, version_id=None)` yielding verified day
  records for the #33/#34 read side. The update loop is version-independent:
  every version has fully independent cash, positions, orders, fees, dividends
  and NAV.
- No multi-version comparison command is implemented (#34); only the
  per-version daily update and audit dump exist here.
- Only the four declarative strategy families are executed. A release whose
  frozen strategy family is generated Python is blocked
  (`UNSUPPORTED_STRATEGY`): Shadow Live does not run generated Python locally.
- The release artifact's evaluation-time `execution_rules` describe the sealed
  runner (e.g. `missing_required_open: whole_rebalance_not_filled`). Shadow Live
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
