"""A missing or interrupted synthetic Holdout cannot trigger admission."""

from pathlib import Path

import pytest

from cli_helpers import cli, cli_env, ok, rejected
from test_issue37_holdout_cli import _authorize, _calendar, _selected_round
from test_protected_eval_cli import seal_snapshot, synthetic_snapshot


@pytest.mark.isolation
def test_missing_source_and_post_read_abort_preserve_the_same_holdout_debit(tmp_path: Path) -> None:
    import json

    sealed = seal_snapshot(tmp_path, synthetic_snapshot())
    store = tmp_path / "operator"
    selected = _selected_round(tmp_path / "selected", store, sealed, "Recovery fixture")
    approved = ok(_authorize(store, sealed, selected, _calendar(tmp_path)))
    plan_id = approved["plan_id"]
    args = (
        "holdout", "run", "--store", str(store), "--snapshot-store", str(sealed["store"]),
        "--project", str(selected["project"]), "--plan-id", plan_id,
    )

    payload = sealed["store"] / sealed["snapshot_id"] / "final_holdout.json"
    renamed = payload.with_suffix(".unavailable")
    payload.parent.chmod(0o755)
    payload.rename(renamed)
    missing = rejected(cli(*args), "SOURCE_MISSING")
    assert missing["query_debited"] is False
    assert missing["opened_protected_source"] is False
    renamed.rename(payload)

    first = cli(*args, env=cli_env(PRAJNATRADE_TEST_FAILPOINT="evaluation_after_read"))
    assert first.returncode == 2, first.stderr
    aborted = json.loads(first.stdout)
    assert aborted["status"] == "aborted"
    assert aborted["query"]["debited"] is True
    assert aborted["query"]["opened_protected_source"] is True
    assert aborted["query"]["reused"] is False

    retry = cli(*args)
    assert retry.returncode == 2, retry.stderr
    replayed = json.loads(retry.stdout)
    assert replayed["evaluation_id"] == aborted["evaluation_id"]
    assert replayed["query"]["reused"] is True
    assert replayed["record"]["record_hash"] == aborted["record"]["record_hash"]
    assert ok(cli("release", "list", "--store", str(store)))["releases"] == []
