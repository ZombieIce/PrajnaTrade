# Testing

The suite needs no network and runs from the repository root.

## Commands

```bash
# Fast mode: skips tests marked "isolation" (safe in nested/agent sessions).
scripts/test.sh fast

# Full mode: the entire suite, including real-sandbox tests.
scripts/test.sh full

# Anything else is passed to pytest.
scripts/test.sh tests/test_acceptance_cli.py -q
```

`scripts/test.sh` defaults `PYTHON` to `python`, sets `PYTHONPATH=src` and
`PYTHONDONTWRITEBYTECODE=1`, and prints the mode and command. Point it at a
specific interpreter with `PYTHON=/path/to/python scripts/test.sh fast`.

A single test file, without the wrapper:

```bash
PYTHONPATH=src python -m pytest -q tests/test_acceptance_cli.py
```

The full suite is the slow path (about 11 minutes on `macos-latest` in the #37
CI run); at the time of writing this branch collects **483 tests**. CI
(`.github/workflows/tests.yml`) runs the full suite on `macos-latest` with
Python 3.12; `lint` runs `ruff check .`.

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

## Actual daily open tests (#56)

`tests/test_actual_open_cli.py` covers the schema-3 real contract and the
`actual-open-v1` execution rule. It owns the reusable synthetic schema-3 fixture
builders (`schema_v3_fixture`, `seal_v3`, `day_at` and the halt/suspension/
conflict mutators); #58 and #59 import them instead of copying them. It seals a
schema-3 manifest and asserts the `execution_evidence` policy block, one row per
non-trading sealed session/symbol, and the `actual_open_after_halt` /
`confirmed_no_trade` / `missing_required_open` / `source_conflict` /
`unknown_status` counts, plus that the historical `gaps` shape is unchanged and
an evidenced actual open is no longer a raw-bar gap. Negative seal cases reject a
missing or unsealed price source, a resumption-status notice reused as the price
source, a missing `status_evidence_id`, an undeclared `raw_price_basis`, every
malformed `open_conflict` (an executable open, fewer than two sources, duplicate
sources, unsealed evidence, non-positive or identical opens) and a schema-2 row
carrying any of the three schema-3 keys. The public-CLI price test runs the same
target on a normal-open sample, a resumed-halt sample with the same daily open
and a variant whose execution-day close differs: both the candidate and the
benchmark must produce the same fill date, base price, quantity, fee, ending
cash and sealed `open_evidence`, the close variant may change only that day's
valuation, and a `next-open-v1` control cannot fill the halted day. A v1
`round validate` receipt on a schema-3 `actual-open-v1` Campaign asserts the
additive provenance identity. The file is fast and not marked `isolation`.

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
