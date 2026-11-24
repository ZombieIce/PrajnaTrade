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
`synthetic_snapshot`/`build_scenario` in `test_protected_eval_cli.py`,
`sealed_snapshot_input` in `test_experiment_cli.py`, the #43 round builders
(`round_input`, `open_round`, `submit_version`, `validate_version`) in
`test_research_loop_cli.py`, the 70/30 split builders (`split_campaign`,
`stored_campaign`, `v2_round_input`) in `test_discovery_split_cli.py`, and the
corrected-successor builders in `test_corrected_evidence_cli.py`; the review,
evidence, formal-validation, admission and validation-feedback tests
cross-import those builders rather than copying them. Each scenario builder
(`formal_scenario`, `v2_round_input`, `split_campaign`, `admit`,
`approve_version`, ...) stays owned by the module that first needed it.  The existing `from
test_protected_eval_cli import ...` cross-imports still work; import generic
helpers from `cli_helpers` directly.

## Isolation marker

Tests that require the real macOS sandbox (`sandbox_init`, isolation probes, or
a protected-evaluation/release/shadow flow that runs the isolation gate) are
marked `isolation`:

- module level with `pytestmark = pytest.mark.isolation` in
  `test_isolation.py`, `test_protected_eval_cli.py`,
  `test_holdout_report_cli.py`, `test_release_cli.py`, `test_impact_cli.py`,
  `test_shadow_cli.py`, `test_compare_cli.py`, `test_correction_delta_cli.py`,
  `test_shadow_schedule.py`, `test_validation_batch_cli.py` and
  `test_validation_scoring_cli.py`;
- individually for the generated-Python/probe tests in `test_research_cli.py`,
  `test_research_direction_cli.py`, `test_reusable_validation_cli.py`,
  `test_issue37_holdout_cli.py`, `test_holdout_recovery_cli.py`,
  `test_direction_schema_v2_cli.py`, `test_execution_rules_cli.py`,
  `test_validation_selection_cli.py`, and the generated-Python parameter of
  `test_openai_provider.py::test_campaign_records_provider_usage_without_persisting_api_credentials`.

The fast suite deselects them:

```sh
... -m "not isolation"
```

`pyproject.toml` registers the `isolation` marker. The #43 Research Loop,
version-review, corrected-evidence, #50 discovery-split/formal-validation,
admission and validation-feedback and Evidence Loop tests are fast:
Lightweight Validation runs declarative strategies in the trusted controller,
and the review/evidence tests build `manual_tracking` artifacts and ledger
fixtures directly instead of running a protected flow. If a #43 test starts
creating releases through the protected flow, mark it and record why in the
module docstring.

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
