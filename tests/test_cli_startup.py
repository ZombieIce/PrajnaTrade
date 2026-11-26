"""Subprocess smoke tests and lazy-import guards for the public CLI (#66)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from cli_helpers import AGENT_COMMAND, agent_cli, cli, cli_env, ok, src_path

HEAVY_MODULES = ("numpy", "pandas", "pyarrow")


def _run_probe(code: str) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=cli_env(),
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    return json.loads(result.stdout)


def test_cli_import_and_help_do_not_load_heavy_dependencies() -> None:
    payload = _run_probe(
        """
import contextlib, io, json, sys
from prajnatrade.cli import build_parser, main

build_parser(agent_only=False)
build_parser(agent_only=True)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    try:
        main(["--help"])
    except SystemExit as exc:
        code = exc.code
loaded = sorted({"numpy", "pandas", "pyarrow"} & set(sys.modules))
print(json.dumps({"code": code, "loaded": loaded, "has_usage": "usage:" in buf.getvalue()}))
"""
    )
    assert payload == {"code": 0, "loaded": [], "has_usage": True}


def test_lightweight_project_and_snapshot_commands_stay_free_of_heavy_imports(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    payload = _run_probe(
        f"""
import contextlib, io, json, sys
from prajnatrade.cli import agent_main, main

out = io.StringIO()
with contextlib.redirect_stdout(out):
    init_code = main(["project", "init", "--project", {str(project)!r}])
    status_code = agent_main(["project", "status", "--project", {str(project)!r}])
loaded = sorted({{"numpy", "pandas", "pyarrow"}} & set(sys.modules))
print(json.dumps({{"init_code": init_code, "status_code": status_code, "loaded": loaded}}))
"""
    )
    assert payload == {"init_code": 0, "status_code": 0, "loaded": []}


def test_real_subprocess_cli_and_agent_entry_points(tmp_path: Path) -> None:
    help_result = cli("--help", use_subprocess=True)
    assert help_result.returncode == 0, help_result.stderr
    assert "usage: prajnatrade" in help_result.stdout

    agent_help = subprocess.run(
        [*AGENT_COMMAND, "--help"],
        capture_output=True,
        text=True,
        env=cli_env(),
        timeout=30,
        check=False,
    )
    assert agent_help.returncode == 0, agent_help.stderr
    assert "usage: prajnatrade-agent" in agent_help.stdout

    project = tmp_path / "subproc-project"
    initialized = ok(cli("project", "init", "--project", str(project), use_subprocess=True))
    inspected = ok(agent_cli("project", "status", "--project", str(project), use_subprocess=True))
    assert inspected["project_id"] == initialized["project_id"]


def test_cli_helper_subprocess_env_toggle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "env-toggle-project"
    monkeypatch.setenv("PRAJNATRADE_TEST_CLI_SUBPROCESS", "1")
    initialized = ok(cli("project", "init", "--project", str(project)))
    inspected = ok(agent_cli("project", "status", "--project", str(project)))
    assert inspected["project_id"] == initialized["project_id"]


def test_in_process_cli_captures_exit_codes_stderr_env_and_cwd(
    tmp_path: Path,
) -> None:
    bad_args = cli("project", "nonexistent")
    assert bad_args.returncode == 2
    assert bad_args.stdout == ""
    assert "invalid choice" in bad_args.stderr

    rel_project = tmp_path / "nested" / "rel-proj"
    rel_project.parent.mkdir(parents=True)
    before_cwd = Path.cwd()
    initialized = ok(cli("project", "init", "--project", "rel-proj", cwd=rel_project.parent))
    assert Path.cwd() == before_cwd
    assert (rel_project / ".prajnatrade" / "project.sqlite").is_file()
    inspected = ok(agent_cli("project", "status", "--project", str(rel_project)))
    assert inspected["project_id"] == initialized["project_id"]

    # Passing env must not leak into the parent process environment.
    env = cli_env(PRAJNATRADE_TEST_SENTINEL="temporary-value")
    _ = ok(agent_cli("project", "status", "--project", str(rel_project), env=env))
    assert "PRAJNATRADE_TEST_SENTINEL" not in __import__("os").environ
    assert str(src_path()) in sys.path


def test_cli_helper_auto_falls_back_for_threads_and_custom_pythonpath(
    tmp_path: Path,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    project = tmp_path / "thread-project"
    initialized = ok(cli("project", "init", "--project", str(project)))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: ok(agent_cli("project", "status", "--project", str(project))),
                range(2),
            )
        )
    assert all(item["project_id"] == initialized["project_id"] for item in results)

    custom_env = cli_env()
    custom_env["PYTHONPATH"] = f"{tmp_path}:{custom_env['PYTHONPATH']}"
    inspected = ok(agent_cli("project", "status", "--project", str(project), env=custom_env))
    assert inspected["project_id"] == initialized["project_id"]

