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
CI run); at the time of writing this branch collects **417 tests**. CI
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
