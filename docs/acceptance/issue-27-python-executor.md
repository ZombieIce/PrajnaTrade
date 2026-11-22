# Issue #27 trusted container executor evidence

This record covers only the trusted-side implementation: the accepted Python
submission schema, `campaign create --strategy-executor container`, and the
verified Docker/Colima boundary that executes generated Python. It is **not**
#27 acceptance: the external model session, the project-only relay/workspace
and the real-Discovery iteration have not run. The #27 verdict therefore
remains blocked.

## Runner image

- Base: `python:3.12-bookworm@sha256:dbbe4ceb97851e2e5fa83798b239811f871cb743b259ba3563737349f6bcfaa0`
- Installed by `scripts/issue27-runner/Dockerfile`: `numpy==2.2.6`,
  `pandas==2.2.3` (the #26 pinned versions), `pip install --no-cache-dir`.
- Local tag: `prajnatrade-issue27-runner:local`.
- Observed local image ID on this machine:
  `sha256:4371b913b52ebb266fdd8d9854dd4ac5d69e6de1677c41d9c5547c7b52240778`.

The local image ID is the digest the executor resolves and freezes into a
Campaign; it is content-addressed but includes build metadata, so a rebuild can
produce a different ID. The base image digest above is stable. The build needs
network access for the pinned wheels; running the image is offline.

Colima on this machine reported Docker server 29.5.2 (`aarch64`, linux) and the
client 29.8.1. Reproduce with:

```sh
DOCKER_HOST=unix://$HOME/.colima/default/docker.sock \
  scripts/issue27-runner/build.sh
```

## Boundary exercised

`ContainerStrategyExecutor` runs one fresh container per strategy call with no
mounts, `--network none`, `--read-only`, `--user 65534:65534`, `--cap-drop
ALL`, `--security-opt no-new-privileges`, `--pids-limit 64`, `--memory 512m`,
`--cpus 1`, `--tmpfs /tmp:rw,size=16m`, `-e PYTHONDONTWRITEBYTECODE=1`, and
`python -I -c <worker>`. The trusted side reads the container ID, calls
`docker inspect` while the worker is blocked waiting for inspection, verifies
the actual mounts/network/read-only/user/capabilities/limits, and only then
acknowledges the worker so the strategy may start. Weights are validated again
in the trusted process. Docker/Colima unavailability, digest changes, timeouts,
output overruns, non-JSON output and invalid weights fail the attempt; there is
no trusted-local fallback.

## Verification run

```sh
# focused unit tests with a fake executor
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 $PYTHON -m pytest -q -p no:cacheprovider \
  tests/test_experiment_python.py
# 9 passed

# real Colima/Docker boundary (skipped when unavailable)
DOCKER_HOST=unix://$HOME/.colima/default/docker.sock \
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 $PYTHON -m pytest -q -p no:cacheprovider \
  -m docker tests/test_container_executor.py
# 3 passed

# full suite and lint
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 $PYTHON -m pytest -q -p no:cacheprovider
# 294 passed (282 existing + 9 fake-executor + 3 Docker-gated)
ruff check .
# All checks passed!
```

The Docker-gated test asserts fills/metrics for a generated equal-weight
strategy, that the container has no mounts, no network, a read-only root, UID
65534 and the frozen limits, that a host sentinel and a network socket are
unusable inside the container, and that the trusted side rejects non-unit
weights while charging the attempt.

## What remains for #27

The trusted execution boundary is available, but #27 acceptance still needs:

- the project-only relay/workspace that gives the external DeepSeek Harness
  session only real Discovery receipts and accepts its generated Python;
- an authorized real model session that reads the first real receipt, explains
  its decision, and submits a parent-linked follow-up;
- a real-Discovery campaign run with this executor and retained,
  non-secret evidence.

Declarative strategies, trusted-local execution and pre-recorded proposals do
not satisfy #27. Protected evaluation of frozen Python candidates is also not
part of this change.
