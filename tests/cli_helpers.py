"""Shared helpers for the public-CLI tests.

Every test module in ``tests/`` imports these helpers directly, for example
``from cli_helpers import cli, cli_env``.  Pytest inserts the ``tests``
directory on ``sys.path`` (the suite uses the default prepend import mode), so
this works the same way as the existing ``from test_protected_eval_cli import
...`` cross-imports and does not depend on the working directory.

Only helpers that are genuinely generic live here.  Scenario fixtures stay in
the module that owns them (``synthetic_snapshot`` and ``build_scenario`` in
``test_protected_eval_cli``, ``sealed_snapshot_input`` in
``test_experiment_cli``, and so on).

Test-writing rules this module exists to support:

* Resolve the checkout from ``Path(__file__)``; never hard-code a machine path.
* Build mutable fixtures under ``tmp_path`` (or ``tmp_path_factory``) and make
  every test self-contained.  No test may rely on module-scoped state or on
  another test having run first; see ``tests/README.md`` for the check used to
  guard the #29 receipt regression.
* Tests that require the real macOS sandbox are marked ``isolation`` so the
  fast suite can deselect them with ``-m "not isolation"``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = REPO_ROOT / "src"
DEFAULT_TIMEOUT_SECONDS = 60.0
AGENT_COMMAND = (
    sys.executable,
    "-c",
    "from prajnatrade.cli import agent_main; raise SystemExit(agent_main())",
)


def repo_root() -> Path:
    """Return the checkout root that contains ``src`` and ``tests``."""
    return REPO_ROOT


def src_path() -> Path:
    """Return the checkout's ``src`` directory, the value put on PYTHONPATH."""
    return SRC_PATH


def cli_env(
    extra: dict[str, str] | None = None, **env_extra: str
) -> dict[str, str]:
    """Copy the parent environment and prepend this checkout's ``src``.

    ``extra`` (a mapping) and ``env_extra`` (keyword arguments) are merged last,
    so tests can set failpoints or provider variables without losing the
    interpreter's environment.  The keyword form preserves the historical
    ``cli_env(PRAJNATRADE_TEST_FAILPOINT="...")`` call style.
    """
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        (str(SRC_PATH), env.get("PYTHONPATH", ""))
    )
    if extra:
        env.update(extra)
    if env_extra:
        env.update(env_extra)
    return env


def cli(
    *args: str,
    agent: bool = False,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the public CLI in a subprocess and capture text output.

    ``agent=True`` runs the research/agent entry point
    (``prajnatrade-agent`` equivalent) instead of ``python -m prajnatrade``.

    The default timeout is 60 seconds, long enough for every current CLI test
    and short enough to fail a hung subprocess quickly.  A test that
    intentionally waits on a longer budget must pass ``timeout=`` explicitly.
    """
    command = AGENT_COMMAND if agent else (sys.executable, "-m", "prajnatrade")
    return subprocess.run(
        [*command, *args],
        capture_output=True,
        text=True,
        env=env or cli_env(),
        cwd=cwd,
        timeout=timeout,
        check=False,
    )


def agent_cli(
    *args: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the agent entry point; shorthand for ``cli(..., agent=True)``."""
    return cli(*args, agent=True, timeout=timeout, env=env, cwd=cwd)


def ok(result: subprocess.CompletedProcess[str]) -> dict:
    """Assert a successful CLI result and return its JSON payload."""
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    return json.loads(result.stdout)


def validation_service(project: Path, store: Path, snapshots: Path, round_id: str) -> dict:
    """Process pending proposals and details through the trusted Validation CLI."""
    return ok(cli(
        "validation", "service", "--project", str(project), "--store", str(store),
        "--snapshot-store", str(snapshots), "--round-id", round_id,
    ))


def rejected(
    result: subprocess.CompletedProcess[str],
    code: str,
    *,
    phase: str | None = None,
) -> dict:
    """Assert a protocol rejection with ``code`` (and ``phase`` when given)."""
    assert result.returncode == 2, (result.returncode, result.stdout, result.stderr)
    payload = json.loads(result.stdout)
    assert payload["status"] == "rejected", payload
    assert payload["error"]["code"] == code, payload
    if phase is not None:
        assert payload["error"]["phase"] == phase, payload
    return payload


def write_json(path: Path, payload: object) -> Path:
    """Write ``payload`` as JSON (creating parents) and return ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path
