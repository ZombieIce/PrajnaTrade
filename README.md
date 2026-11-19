# PrajnaTrade

PrajnaTrade is a local-first research CLI for a fixed portfolio universe:
ETFs `511260`, `518880`, `515080`, `513300`, `159612`, plus cash.

## Data format

CSV and Parquet inputs use one row per symbol and date with these columns:
`date,symbol,open,high,low,close,volume,price_basis`. Prices must consistently
use the documented `total_return` adjustment basis, and every row must declare
that basis so the loader can reject mixed or mismatched data. Missing opens or
closes are retained and reported rather than silently filled.

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
Campaigns created before this change may have an incompatible fingerprint and
must be recreated rather than resumed or validated.

## Research, validation, and reports

Mock campaigns are deterministic orchestration checks, not AI research. They
split the ordered sessions into discovery, validation, and a final holdout
(60% / 20% / remainder). Campaigns require at least five distinct sessions and
retain every attempt in SQLite:

```sh
prajnatrade research run \
  --db research.sqlite --data bars.csv --price-basis total_return \
  --data-kind real --provider mock --seed 7 --experiments 20 --steps 5

prajnatrade research status --db research.sqlite --campaign-id <campaign-id>
prajnatrade research resume --db research.sqlite --campaign-id <campaign-id>
```

Paused campaigns and campaigns left in `running` state by an interrupted
process can be resumed from their persisted experiment history. Resume a
`running` campaign only after its original process has stopped; concurrent
workers on the same campaign are not supported. The campaign time budget is
checked between attempts; it does not interrupt an attempt already in progress.

Freeze a successful experiment before evaluating it on the independent
validation interval. The final holdout can be consumed only once, after
validation and an explicit confirmation. Both evaluations require the original
campaign dataset, price basis, and data-kind label; the frozen dataset
fingerprint is checked before evaluation:

```sh
prajnatrade research freeze \
  --db research.sqlite --campaign-id <campaign-id> \
  --experiment-id <experiment-id> --name candidate-1 \
  --conclusion "Candidate and conclusions are frozen before validation."

prajnatrade research validate \
  --db research.sqlite --candidate-id <candidate-id> \
  --data bars.csv --price-basis total_return --data-kind real

prajnatrade research holdout \
  --db research.sqlite --candidate-id <candidate-id> \
  --data bars.csv --price-basis total_return --data-kind real \
  --confirmation "I CONFIRM THE CANDIDATE AND CONCLUSIONS ARE FROZEN"

prajnatrade research report \
  --db research.sqlite --campaign-id <campaign-id> \
  --output report.html
```

For an OpenAI-compatible provider, configure `PRAJNATRADE_OPENAI_ENDPOINT`,
`PRAJNATRADE_OPENAI_MODEL`, and the environment variable named by
`--api-key-env` (default `OPENAI_API_KEY`). Supply
`--input-cost-per-1k-tokens` and `--output-cost-per-1k-tokens` so the configured
token and estimated-cost budgets can be enforced. Credentials are read from the
environment and are not persisted. Request budgets use token and cost estimates;
actual provider billing can differ, especially when usage is unavailable or
reported inaccurately. Failed requests without usable usage data reserve the
estimated request budget and are marked `usage_estimated` in experiment usage.

By default, strategies are declarative specifications (`equal_weight`,
`momentum`, `trend_filter`, and `inverse_volatility`); generated Python is not
executed. To allow generated Python, explicitly pass
`--trusted-local-generated-python` to each `research run`, `resume`, `validate`,
or `holdout` command that executes it. The generated source and parameters are
retained with the experiment. Source defines `strategy(history, state)` and may
use the globals `ETF_SYMBOLS`, `CASH`, and `parameters`.

**This is not a sandbox.** Generated Python runs with the full privileges of
the local process: it can access files, credentials, the network, and holdout
data, and can interfere with the research engine. Only enable it for code you
trust; do not use it for unattended execution of untrusted model output. The
holdout gate is an application control, not an isolation boundary against
generated code. Parameter robustness is reported as not applicable for Python
strategies because they have no declared parameter schema.

The Mock provider can emit deterministic Python when opted in, but a Mock
campaign is never evidence of model novelty or investable performance. Results
are research evidence, not investment advice or a guarantee of future returns.
