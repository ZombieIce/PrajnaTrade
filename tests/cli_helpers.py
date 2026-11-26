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

import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import threading
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = REPO_ROOT / "src"
DEFAULT_TIMEOUT_SECONDS = 60.0
HEX = re.compile(r"[0-9a-f]{64}")
AGENT_COMMAND = (
    sys.executable,
    "-c",
    "from prajnatrade.cli import agent_main; raise SystemExit(agent_main())",
)


def assert_hex(value: object) -> None:
    """Assert that ``value`` is a 64-character lowercase hexadecimal ID."""
    assert isinstance(value, str) and HEX.fullmatch(value), value


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


def _run_in_process(
    *args: str,
    agent: bool,
    env: dict[str, str] | None,
    cwd: Path | None,
) -> subprocess.CompletedProcess[str]:
    if str(SRC_PATH) not in sys.path:
        sys.path.insert(0, str(SRC_PATH))
    from prajnatrade.cli import agent_main, main

    argv = [os.fspath(arg) for arg in args]
    command = AGENT_COMMAND if agent else (sys.executable, "-m", "prajnatrade")
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    saved_env = os.environ.copy() if env is not None else None
    saved_cwd = os.getcwd() if cwd is not None else None
    try:
        if env is not None:
            os.environ.clear()
            os.environ.update(env)
        if cwd is not None:
            os.chdir(cwd)
        entry = agent_main if agent else main
        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            try:
                raw_code = entry(argv)
                returncode = 0 if raw_code is None else int(raw_code)
            except SystemExit as exc:
                if exc.code is None:
                    returncode = 0
                elif isinstance(exc.code, int):
                    returncode = exc.code
                else:
                    print(exc.code, file=stderr_buf)
                    returncode = 1
            except Exception:
                traceback.print_exc(file=stderr_buf)
                returncode = 1
    finally:
        if saved_cwd is not None:
            os.chdir(saved_cwd)
        if saved_env is not None:
            os.environ.clear()
            os.environ.update(saved_env)
    return subprocess.CompletedProcess(
        args=[*command, *argv],
        returncode=returncode,
        stdout=stdout_buf.getvalue(),
        stderr=stderr_buf.getvalue(),
    )


def _requires_subprocess(
    *, use_subprocess: bool, env: dict[str, str] | None
) -> bool:
    if use_subprocess or os.environ.get("PRAJNATRADE_TEST_CLI_SUBPROCESS") == "1":
        return True
    if threading.current_thread() is not threading.main_thread():
        return True
    if env is not None and env.get("PYTHONPATH") != cli_env()["PYTHONPATH"]:
        return True
    return False


def cli(
    *args: str,
    agent: bool = False,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    use_subprocess: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run the public CLI and capture text output.

    ``agent=True`` runs the research/agent entry point
    (``prajnatrade-agent`` equivalent) instead of ``python -m prajnatrade``.

    By default this invokes ``prajnatrade.cli.main`` / ``agent_main`` in
    process to avoid thousands of cold interpreter startups across the test
    suite, while returning an equivalent ``subprocess.CompletedProcess``.
    Calls automatically fall back to a real child Python process when
    ``use_subprocess=True``, when ``PRAJNATRADE_TEST_CLI_SUBPROCESS=1`` is set,
    when called from a non-main thread (e.g. concurrent ``ThreadPoolExecutor``
    tests), or when ``env`` overrides ``PYTHONPATH`` (e.g. runtime-upgrade
    fingerprint tests).  ``timeout`` applies in subprocess mode (default 60s).
    """
    if _requires_subprocess(use_subprocess=use_subprocess, env=env):
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
    return _run_in_process(*args, agent=agent, env=env, cwd=cwd)


def agent_cli(
    *args: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    use_subprocess: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run the agent entry point; shorthand for ``cli(..., agent=True)``."""
    return cli(
        *args,
        agent=True,
        timeout=timeout,
        env=env,
        cwd=cwd,
        use_subprocess=use_subprocess,
    )


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


def canonical(value: object) -> bytes:
    """Canonical JSON bytes, an independent oracle for the repo's content hashes."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def digest(value: object) -> str:
    """SHA-256 hex of ``canonical(value)``; the test-side content address."""
    return hashlib.sha256(canonical(value)).hexdigest()


def round_validate_argv(
    project: Path, store: Path, snapshots: Path, round_id: str,
) -> list[str]:
    """The argv prefix every ``round validate`` race participant shares.

    Callers append the version ID under test, so two subprocesses can race the
    same round from one command definition instead of hand-building it in each
    test module.  The explicit interpreter and ``-m`` keep the run hermetic.
    """
    return [
        sys.executable, "-m", "prajnatrade", "round", "validate",
        "--project", str(project), "--store", str(store),
        "--snapshot-store", str(snapshots), "--round-id", round_id,
        "--version-id",
    ]
