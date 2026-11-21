# Test suite conventions

The suite is offline and uses synthetic fixtures only.  Run it from the
checkout root with the project's Python on `PYTHONPATH`:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src <venv>/python -m pytest -q -p no:cacheprovider
```

## Shared public-CLI helpers

`tests/cli_helpers.py` owns the helpers every public-CLI test needs:

- `repo_root()` / `src_path()` resolve the checkout from `Path(__file__)`;
  never hard-code a machine-specific absolute path.
- `cli_env(extra=None, **env_extra)` copies the parent environment, prepends
  this checkout's `src` to `PYTHONPATH`, and merges `extra`/`env_extra`
  (failpoints, provider variables).
- `cli(*args, agent=False, timeout=60, env=None, cwd=None)` runs
  `python -m prajnatrade`; `agent=True` (or `agent_cli`) runs the agent entry
  point.  The documented default timeout is **60 seconds**; a test that
  intentionally waits on a longer budget passes `timeout=` explicitly.
- `ok()`, `rejected()` and `write_json()` are the shared JSON assertions and
  fixture writer.
- `AGENT_COMMAND` is the agent entry-point command tuple, for tests that need
  to drive it with `subprocess.Popen` instead of `cli`.

Scenario fixtures stay with the module that owns them:
`synthetic_snapshot`/`build_scenario` in `test_protected_eval_cli.py` and
`sealed_snapshot_input` in `test_experiment_cli.py`.  The existing
`from test_protected_eval_cli import ...` cross-imports still work; import
generic helpers from `cli_helpers` directly.

## Isolation marker

Tests that require the real macOS sandbox (`sandbox_init`, isolation probes, or
a protected-evaluation/release/shadow flow that runs the isolation gate) are
marked `isolation`:

- module level with `pytestmark = pytest.mark.isolation` in
  `test_isolation.py`, `test_protected_eval_cli.py`,
  `test_holdout_report_cli.py`, `test_release_cli.py`, `test_impact_cli.py`,
  `test_shadow_cli.py`, `test_compare_cli.py`, `test_correction_delta_cli.py`;
- individually for the generated-Python/probe tests in `test_research_cli.py`
  and the generated-Python parameter of
  `test_openai_provider.py::test_campaign_records_provider_usage_without_persisting_api_credentials`.

The fast suite deselects them:

```sh
... -m "not isolation"
```

`pyproject.toml` (owned by a separate change) registers the marker.  Until that
lands, pytest emits `PytestUnknownMarkWarning` for `isolation`; selection with
`-m` still works.

## Order independence

Every test builds its own state under `tmp_path` or `tmp_path_factory`; no test
may depend on module-scoped state or on another test running first.  Tests that
share a module-scoped fixture must still work when selected alone.

The regression that motivated this rule was the #29 receipt check: it used to
depend on state created by earlier tests in the module.  The guard is to select
it by itself, from the checkout root, and confirm it still passes:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src <venv>/python -m pytest -q -p no:cacheprovider \
  tests/test_holdout_report_cli.py::test_holdout_receipt_is_byte_identical_across_every_state
```

Add a similar standalone check when fixing an order-dependent test; the module
docstring of `test_holdout_report_cli.py` records that the receipt test builds
every pending/unauthorized/completed/rejected/replayed state itself.
