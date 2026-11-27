"""Public-CLI tests for local plugin registration, fingerprinting, and verification (#75 / #74-A)."""

from __future__ import annotations

import json
from pathlib import Path

from cli_helpers import assert_hex, cli
from test_formal_validation_cli import formal_scenario
from test_protected_eval_cli import plan_input, seal_snapshot, synthetic_snapshot, write_json
from test_research_loop_cli import ok, round_status, submit_version, validate_version, version_input
from test_shadow_cli import report_days, shadow_feed, shadow_update


def _write_thin_plugin(plugin_dir: Path, *, family: str = "synth_mom", weight: float = 0.5) -> tuple[Path, Path, Path]:
    """Create a synthetic thin-wrapper plugin + separate implementation module + config."""
    plugin_dir.mkdir(parents=True, exist_ok=True)
    impl_path = plugin_dir / "synth_impl.py"
    impl_path.write_text(
        "\n".join(
            [
                "from prajnatrade.data import CASH, ETF_SYMBOLS",
                "",
                "def compute_weights(history, state):",
                f"    w = {weight!r}",
                "    first = ETF_SYMBOLS[0]",
                "    return {**{s: (w if s == first else 0.0) for s in ETF_SYMBOLS}, CASH: 1.0 - w}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    wrapper_path = plugin_dir / "synth_wrapper.py"
    wrapper_path.write_text(
        "\n".join(
            [
                "import synth_impl",
                "",
                "def make_plugin(parameters):",
                "    def strategy(history, state):",
                "        return synth_impl.compute_weights(history, state)",
                "    return strategy",
                "",
            ]
        ),
        encoding="utf-8",
    )
    config_path = plugin_dir / "plugins.json"
    config_path.write_text(
        json.dumps(
            {
                "plugins": [
                    {
                        "family": family,
                        "entrypoint": "synth_wrapper.py:make_plugin",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return config_path, wrapper_path, impl_path


def test_plugin_register_thin_wrapper_closure_and_agent_boundary(tmp_path: Path) -> None:
    project = tmp_path / "project"
    ok(cli("project", "init", "--project", str(project)))

    plugin_dir = tmp_path / "plugins"
    config_path, wrapper_path, impl_path = _write_thin_plugin(plugin_dir)

    reg = ok(
        cli(
            "plugin",
            "register",
            "--project",
            str(project),
            "--plugin-config",
            str(config_path),
        )
    )
    assert reg["status"] == "registered"
    assert len(reg["plugins"]) == 1
    entry = reg["plugins"][0]
    assert entry["family"] == "synth_mom"
    assert_hex(entry["plugin_id"])
    assert_hex(entry["entrypoint_sha256"])
    assert_hex(entry["dependencies_hash"])
    dep_keys = {d["relpath_or_key"] for d in entry["dependencies"]}
    assert "synth_wrapper.py" in dep_keys
    assert "synth_impl.py" in dep_keys

    # Agent can list registered plugins, but cannot register arbitrary paths or pass --plugin-config.
    listed = ok(cli("plugin", "list", "--project", str(project), agent=True))
    assert [p["family"] for p in listed["plugins"]] == ["synth_mom"]

    forbidden_flag = cli(
        "plugin", "register", "--project", str(project), "--plugin-config", str(config_path), agent=True
    )
    assert forbidden_flag.returncode != 0

    forbidden_submit_flag = cli(
        "experiment", "submit", "--project", str(project), "--plugin-config", str(config_path),
        "--input", str(tmp_path / "missing.json"), agent=True,
    )
    assert forbidden_submit_flag.returncode != 0


def test_plugin_registration_diagnostics_and_builtin_invariance(tmp_path: Path) -> None:
    from prajnatrade.plugins import PLUGIN_CONFIG_ENV, PluginError, load_plugin_registry
    from prajnatrade.strategies import strategy_fingerprint

    baseline_fp = strategy_fingerprint({"family": "momentum", "parameters": {"lookback": 20, "top_k": 1, "cash_if_negative": True}})

    plugin_dir = tmp_path / "plugins"
    config_path, _, _ = _write_thin_plugin(plugin_dir, family="synth_mom")
    reg = load_plugin_registry(env={PLUGIN_CONFIG_ENV: str(config_path)})
    assert "synth_mom" in reg
    # Loading an unrelated plugin never alters built-in strategy fingerprints.
    assert strategy_fingerprint({"family": "momentum", "parameters": {"lookback": 20, "top_k": 1, "cash_if_negative": True}}) == baseline_fp

    # Duplicate family / built-in collision -> PLUGIN_FAMILY_CONFLICT
    conflict_cfg = tmp_path / "conflict.json"
    conflict_cfg.write_text(
        json.dumps({"plugins": [{"family": "momentum", "entrypoint": str(plugin_dir / "synth_wrapper.py:make_plugin")}]}),
        encoding="utf-8",
    )
    try:
        load_plugin_registry(plugin_config=conflict_cfg)
        raise AssertionError("Expected PLUGIN_FAMILY_CONFLICT")
    except PluginError as err:
        assert err.code == "PLUGIN_FAMILY_CONFLICT"

    # Unresolvable entrypoint -> PLUGIN_ENTRYPOINT_UNRESOLVABLE
    bad_ep_cfg = tmp_path / "bad_ep.json"
    bad_ep_cfg.write_text(
        json.dumps({"plugins": [{"family": "bad_ep", "entrypoint": str(plugin_dir / "missing.py:make")}]}),
        encoding="utf-8",
    )
    try:
        load_plugin_registry(plugin_config=bad_ep_cfg)
        raise AssertionError("Expected PLUGIN_ENTRYPOINT_UNRESOLVABLE")
    except PluginError as err:
        assert err.code == "PLUGIN_ENTRYPOINT_UNRESOLVABLE"

    # Unbindable dependency -> PLUGIN_DEPENDENCY_UNBINDABLE
    bad_dep_wrapper = plugin_dir / "bad_dep_wrapper.py"
    bad_dep_wrapper.write_text("import nonexistent_local_helper_xyz\ndef make(p): return lambda h, s: None\n", encoding="utf-8")
    bad_dep_cfg = tmp_path / "bad_dep.json"
    bad_dep_cfg.write_text(
        json.dumps({"plugins": [{"family": "bad_dep", "entrypoint": str(bad_dep_wrapper) + ":make"}]}),
        encoding="utf-8",
    )
    try:
        load_plugin_registry(plugin_config=bad_dep_cfg)
        raise AssertionError("Expected PLUGIN_DEPENDENCY_UNBINDABLE")
    except PluginError as err:
        assert err.code == "PLUGIN_DEPENDENCY_UNBINDABLE"


def test_discovery_freezes_plugin_identity_blocks_impl_tamper_and_protected_eval_refuses(
    tmp_path: Path,
) -> None:
    snap = seal_snapshot(tmp_path, synthetic_snapshot(day_count=20))
    project = tmp_path / "project"
    store = tmp_path / "trusted_store"
    snapshot_store = snap["store"]
    snapshot_id = snap["snapshot_id"]
    thresholds = write_json(tmp_path / "thresholds.json", plan_input())
    ok(cli("project", "init", "--project", str(project)))

    plugin_dir = tmp_path / "plugins"
    config_path, _, impl_path = _write_thin_plugin(plugin_dir, family="synth_mom", weight=0.6)
    reg = ok(
        cli("plugin", "register", "--project", str(project), "--plugin-config", str(config_path))
    )
    registered_entry = reg["plugins"][0]

    campaign = ok(
        cli(
            "campaign",
            "create",
            "--project",
            str(project),
            "--store",
            str(snapshot_store),
            "--snapshot-id",
            snapshot_id,
            "--attempts",
            "5",
            "--max-seconds",
            "30",
        )
    )
    campaign_id = campaign["campaign_id"]

    # Agent submits unregistered family -> PLUGIN_NOT_REGISTERED
    unreg_req = tmp_path / "unreg.json"
    unreg_req.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "campaign_id": campaign_id,
                "submission_id": "sub-unreg",
                "hypothesis": "unregistered plugin",
                "strategy": {"family": "unknown_plugin"},
                "parameters": {},
            }
        ),
        encoding="utf-8",
    )
    unreg_res = cli("experiment", "submit", "--project", str(project), "--input", str(unreg_req), agent=True)
    assert unreg_res.returncode != 0
    assert "PLUGIN_NOT_REGISTERED" in unreg_res.stdout + unreg_res.stderr

    # Agent tries to pass entrypoint in submission JSON -> PLUGIN_REGISTRATION_FORBIDDEN
    forbid_req = tmp_path / "forbid.json"
    forbid_req.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "campaign_id": campaign_id,
                "submission_id": "sub-forbid",
                "hypothesis": "agent tries to register path",
                "strategy": {"family": "synth_mom", "entrypoint": "evil.py:make"},
                "parameters": {},
            }
        ),
        encoding="utf-8",
    )
    forbid_res = cli("experiment", "submit", "--project", str(project), "--input", str(forbid_req), agent=True)
    assert forbid_res.returncode != 0
    assert "PLUGIN_REGISTRATION_FORBIDDEN" in forbid_res.stdout + forbid_res.stderr

    # Agent submits registered plugin -> succeeds and freezes plugin identity
    sub_req = tmp_path / "sub.json"
    sub_req.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "campaign_id": campaign_id,
                "submission_id": "sub-1",
                "hypothesis": "registered thin-wrapper plugin",
                "strategy": {"family": "synth_mom"},
                "parameters": {"alpha": 1},
            }
        ),
        encoding="utf-8",
    )
    exp = ok(cli("experiment", "submit", "--project", str(project), "--input", str(sub_req), agent=True))
    assert exp["status"] == "succeeded"
    exp_status = ok(
        cli(
            "experiment",
            "status",
            "--project",
            str(project),
            "--campaign-id",
            campaign_id,
            "--submission-id",
            "sub-1",
            agent=True,
        )
    )
    frozen_plugin = exp_status["strategy"]["plugin"]
    assert frozen_plugin["plugin_id"] == registered_entry["plugin_id"]
    assert frozen_plugin["dependencies_hash"] == registered_entry["dependencies_hash"]

    # Freeze candidate and verify Protected Evaluation refuses local plugins
    cand = ok(
        cli(
            "candidate",
            "freeze",
            "--project",
            str(project),
            "--campaign-id",
            campaign_id,
            "--submission-id",
            "sub-1",
            "--name",
            "synth-cand",
            "--conclusion",
            "ready",
        )
    )
    plan_res = cli(
        "evaluation",
        "plan",
        "--project",
        str(project),
        "--store",
        str(store),
        "--snapshot-store",
        str(snapshot_store),
        "--candidate-id",
        cand["candidate_id"],
        "--input",
        str(thresholds),
    )
    assert plan_res.returncode != 0
    assert "PLUGIN_NOT_ALLOWED_IN_PROTECTED_EVAL" in plan_res.stdout + plan_res.stderr

    # Modify ONLY the underlying implementation file behind the thin wrapper -> PLUGIN_CONTENT_CHANGED
    impl_path.write_text(impl_path.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
    tamper_req = tmp_path / "sub2.json"
    tamper_req.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "campaign_id": campaign_id,
                "submission_id": "sub-2",
                "hypothesis": "should fail after impl edit",
                "strategy": {"family": "synth_mom"},
                "parameters": {"alpha": 1},
            }
        ),
        encoding="utf-8",
    )
    tamper_res = cli("experiment", "submit", "--project", str(project), "--input", str(tamper_req), agent=True)
    assert tamper_res.returncode != 0
    assert "PLUGIN_CONTENT_CHANGED" in tamper_res.stdout + tamper_res.stderr


def test_formal_validation_platform_fix_revalidation_and_ineligible_refusals(
    tmp_path: Path,
) -> None:
    setup = formal_scenario(tmp_path)
    project = setup["project"]
    store = setup["store"]
    snapshot_store = setup["snapshots"]
    campaign_id = setup["campaign"]["campaign_id"]
    round_id = setup["opened"]["round_id"]

    plugin_dir = tmp_path / "plugins"
    config_path, _, impl_path = _write_thin_plugin(plugin_dir, family="synth_mom", weight=0.5)
    ok(cli("plugin", "register", "--project", str(project), "--plugin-config", str(config_path)))

    req = write_json(
        tmp_path / "exp-plugin.json",
        {
            "schema_version": 1,
            "campaign_id": campaign_id,
            "submission_id": "sub-plugin",
            "hypothesis": "thin-wrapper plugin formal validation",
            "strategy": {"family": "synth_mom"},
            "parameters": {"alpha": 1},
        },
    )
    exp = ok(cli("experiment", "submit", "--project", str(project), "--input", str(req), agent=True))
    assert exp["status"] == "succeeded"

    version = ok(
        submit_version(
            project,
            round_id,
            "ver-1",
            write_json(tmp_path / "ver-1.json", version_input(exp["experiment_id"])),
        )
    )
    version_id = version["version_id"]
    assert "plugin" in version["locked"]["strategy"]

    # Validate with explicit --plugin-config -> passes and uses 1 automatic slot
    val = ok(
        cli(
            "round",
            "validate",
            "--project",
            str(project),
            "--store",
            str(store),
            "--snapshot-store",
            str(snapshot_store),
            "--round-id",
            round_id,
            "--version-id",
            version_id,
            "--plugin-config",
            str(config_path),
        )
    )
    assert val["outcome"] == "passed"
    assert val["allowance"]["automatic_used"] == 1

    # Platform-fix revalidation with identical plugin, strategy, and rules -> succeeds with zero allowance increment
    reval = ok(
        cli(
            "round",
            "revalidate",
            "--project",
            str(project),
            "--store",
            str(store),
            "--snapshot-store",
            str(snapshot_store),
            "--round-id",
            round_id,
            "--version-id",
            version_id,
            "--reason",
            "platform fix under identical plugin and rules",
            "--plugin-config",
            str(config_path),
        )
    )
    assert reval["status"] == "revalidated"
    assert reval["previous_validation_id"] == val["validation_id"]
    assert reval["validation_id"] != val["validation_id"]
    assert_hex(reval["revalidation_id"])
    # Old receipt remains readable in trusted store
    old_receipt = store / "research-loop-validations" / f"{val['validation_id']}.json"
    assert old_receipt.is_file()
    status_after = ok(round_status(project, round_id))
    assert status_after["validation_allowance"]["automatic_used"] == 1

    # Modify ONLY underlying implementation file -> REVALIDATION_INELIGIBLE
    impl_path.write_text(impl_path.read_text(encoding="utf-8") + "\n# modified\n", encoding="utf-8")
    ineligible = cli(
        "round",
        "revalidate",
        "--project",
        str(project),
        "--store",
        str(store),
        "--snapshot-store",
        str(snapshot_store),
        "--round-id",
        round_id,
        "--version-id",
        version_id,
        "--reason",
        "attempt after impl edit",
        "--plugin-config",
        str(config_path),
    )
    assert ineligible.returncode != 0
    assert "REVALIDATION_INELIGIBLE" in ineligible.stdout + ineligible.stderr
    assert "PLUGIN_CONTENT_CHANGED" in ineligible.stdout + ineligible.stderr


def test_manual_tracking_approval_and_shadow_blocks_tamper_preserving_committed_days(
    tmp_path: Path,
) -> None:
    setup = formal_scenario(tmp_path)
    project = setup["project"]
    store = setup["store"]
    snapshot_store = setup["snapshots"]
    campaign_id = setup["campaign"]["campaign_id"]
    round_id = setup["opened"]["round_id"]

    plugin_dir = tmp_path / "plugins"
    config_path, _, impl_path = _write_thin_plugin(plugin_dir, family="synth_mom", weight=0.5)
    ok(cli("plugin", "register", "--project", str(project), "--plugin-config", str(config_path)))

    req = write_json(
        tmp_path / "exp-shadow.json",
        {
            "schema_version": 1,
            "campaign_id": campaign_id,
            "submission_id": "sub-shadow",
            "hypothesis": "shadow tracking for local plugin",
            "strategy": {"family": "synth_mom"},
            "parameters": {"alpha": 1},
        },
    )
    exp = ok(cli("experiment", "submit", "--project", str(project), "--input", str(req), agent=True))
    version = ok(
        submit_version(
            project,
            round_id,
            "ver-shadow",
            write_json(tmp_path / "ver-shadow.json", version_input(exp["experiment_id"])),
        )
    )
    version_id = version["version_id"]
    ok(validate_version(project, store, snapshot_store, round_id, version_id))

    sessions = ["2027-01-04", "2027-01-05", "2027-01-06", "2027-01-07"]
    calendar_path = write_json(
        tmp_path / "calendar.json",
        {
            "schema_version": 1,
            "data_kind": "synthetic",
            "source": "synthetic_calendar",
            "sessions": sessions,
        },
    )
    review_input = write_json(
        tmp_path / "approve.json",
        {
            "observation_sessions": 2,
            "observation_reason": "Synthetic operator confirms 2 sessions.",
            "accepted_risks": ["Synthetic prices do not establish real-market evidence."],
        },
    )
    reviewed = ok(
        cli(
            "round",
            "review",
            "--project",
            str(project),
            "--store",
            str(store),
            "--round-id",
            round_id,
            "--version-id",
            version_id,
            "--decision",
            "approve",
            "--reviewer",
            "synth-operator",
            "--reason",
            "synthetic approval",
            "--input",
            str(review_input),
            "--calendar",
            str(calendar_path),
        )
    )
    release_id = reviewed["tracking"]["release_id"]
    artifact = json.loads(
        (store / "releases" / release_id / "artifact.json").read_text(encoding="utf-8")
    )
    assert artifact["candidate"]["strategy"]["plugin"]["plugin_id"] == version["locked"]["strategy"]["plugin"]["plugin_id"]

    feed = shadow_feed(sessions)
    ledger = tmp_path / "ledger"
    for session in sessions[:2]:
        daily = write_json(
            tmp_path / f"feed-{session}.json",
            {
                **feed,
                **{
                    key: [row for row in feed[key] if row["date"] <= session]
                    for key in ("raw_bars", "derived")
                },
            },
        )
        upd = ok(
            cli(
                "shadow",
                "update",
                "--ledger",
                str(ledger),
                "--store",
                str(store),
                "--data",
                str(daily),
                "--date",
                session,
                "--release-id",
                release_id,
                "--corrections-store",
                str(snapshot_store),
                "--plugin-config",
                str(config_path),
            )
        )
        assert upd["versions"][0]["status"] == "committed"

    days_before = report_days(ledger, release_id, tmp_path / "report-before.json")
    assert [d["date"] for d in days_before] == sessions[:2]

    # Modify ONLY underlying implementation file -> shadow update blocks with PLUGIN_CONTENT_CHANGED
    # and NEVER alters already-committed Shadow days
    impl_path.write_text(impl_path.read_text(encoding="utf-8") + "\n# tampered in shadow\n", encoding="utf-8")
    daily_3 = write_json(
        tmp_path / f"feed-{sessions[2]}.json",
        {
            **feed,
            **{
                key: [row for row in feed[key] if row["date"] <= sessions[2]]
                for key in ("raw_bars", "derived")
            },
        },
    )
    blocked = cli(
        "shadow",
        "update",
        "--ledger",
        str(ledger),
        "--store",
        str(store),
        "--data",
        str(daily_3),
        "--date",
        sessions[2],
        "--release-id",
        release_id,
        "--corrections-store",
        str(snapshot_store),
        "--plugin-config",
        str(config_path),
    )
    assert blocked.returncode != 0
    assert "PLUGIN_CONTENT_CHANGED" in blocked.stdout + blocked.stderr

    days_after = report_days(ledger, release_id, tmp_path / "report-after.json")
    assert days_after == days_before


