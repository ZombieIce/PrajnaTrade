"""Reproduce #26 on a pre-authorized real Discovery snapshot in two environments.

Run setup on the trusted side with a Discovery-only view of the sealed store.
Run verify offline in the research environment with only the resulting projects.
Full experiment receipts belong outside Git.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from prajnatrade.providers import MockProvider

SEED = 1
ATTEMPTS = 3
SECONDS = 90
CAMPAIGNS = ("continuous", "resumed")
OPERATOR = (sys.executable, "-m", "prajnatrade")
AGENT = (
    sys.executable, "-c",
    "from prajnatrade.cli import agent_main; raise SystemExit(agent_main())",
)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def write(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")


def read(path):
    return json.loads(path.read_text())


def cli(command, *args, expected_exit=0):
    run = subprocess.run([*command, *map(str, args)], capture_output=True, text=True, timeout=120)
    require(run.returncode == expected_exit, f"{args[:2]} returned {run.returncode}: {run.stderr}")
    return json.loads(run.stdout)


def setup(output, store, snapshot_id):
    snapshot = store / snapshot_id
    require(
        {path.name for path in snapshot.iterdir()} == {"manifest.json", "discovery.json"},
        "The setup container must receive only the sealed manifest and Discovery partition.",
    )
    for name in CAMPAIGNS:
        project = output / name
        project.mkdir()
        cli(OPERATOR, "project", "init", "--project", project)
        campaign = cli(
            OPERATOR, "campaign", "create", "--project", project, "--store", store,
            "--snapshot-id", snapshot_id, "--attempts", ATTEMPTS, "--max-seconds", SECONDS,
            "--initial-cash", "100000", "--transaction-cost-bps", "5", "--slippage-bps", "0",
        )
        require(campaign["data_kind"] == "real", "Expected a real Discovery campaign.")
        write(project / "campaign.json", campaign)


def ledger(project, campaign_id):
    path = project / ".prajnatrade" / "project.sqlite"
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        elapsed = db.execute(
            "SELECT elapsed_seconds FROM campaigns WHERE campaign_id = ?", (campaign_id,),
        ).fetchone()[0]
        rows = db.execute(
            """SELECT submission_id, experiment_id, status FROM experiments
               WHERE campaign_id = ? ORDER BY rowid""", (campaign_id,),
        ).fetchall()
    return elapsed, rows


def interrupt(submit, project, campaign_id, submission_id):
    with subprocess.Popen([*AGENT, *map(str, submit)], stdout=subprocess.DEVNULL) as process:
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and process.poll() is None:
                _, rows = ledger(project, campaign_id)
                for key, experiment_id, status in rows:
                    if key == submission_id and status == "running":
                        process.kill()
                        require(process.wait(timeout=10) == -signal.SIGKILL, "Expected SIGKILL.")
                        return experiment_id
                time.sleep(0.005)
            raise RuntimeError("Missed the durable running state.")
        finally:
            if process.poll() is None:
                process.kill()


def run_campaign(project, *, interrupted):
    campaign = read(project / "campaign.json")
    campaign_id = campaign["campaign_id"]
    mock = MockProvider(SEED)
    context = {"experiments": []}
    parent = None
    trace = []
    recovered = None
    for iteration in range(1, ATTEMPTS + 1):
        proposal = mock.propose(iteration, context)
        submission_id = f"mock-{iteration:02}"
        request = {
            "schema_version": 1, "campaign_id": campaign_id, "submission_id": submission_id,
            "hypothesis": proposal.hypothesis, "strategy": {"family": proposal.strategy["family"]},
            "parameters": proposal.strategy["parameters"], "parent_experiment_id": parent,
        }
        path = project / f"{submission_id}.json"
        write(path, request)
        submit = ("experiment", "submit", "--project", project, "--input", path)
        lookup = ("--project", project, "--campaign-id", campaign_id, "--submission-id", submission_id)
        if interrupted and iteration == 2:
            live_id = interrupt(submit, project, campaign_id, submission_id)
            failed = cli(AGENT, "experiment", "status", *lookup, expected_exit=2)
            require(
                failed["experiment_id"] == live_id
                and failed["error"]["code"] == "EXECUTION_INTERRUPTED"
                and failed["error"]["next_action"] == "resume",
                f"Expected an interrupted and resumable experiment: {failed.get('error')}",
            )
            require(cli(AGENT, *submit, expected_exit=2) == failed, "Failed submission replay changed.")
            require(
                cli(AGENT, "experiment", "status", *lookup[:-1], "mock-01")["experiment_id"] == parent,
                "Committed parent disappeared after the kill.",
            )
            receipt = cli(AGENT, "experiment", "resume", *lookup)
            require(receipt["experiment_id"] == live_id, "Resume changed the experiment ID.")
            require(
                receipt["budget"]["attempts_remaining"] == failed["budget"]["attempts_remaining"]
                and receipt["budget"]["execution_seconds_remaining"]
                < failed["budget"]["execution_seconds_remaining"],
                "Resume must reuse the attempt and charge execution time.",
            )
            recovered = failed
        else:
            receipt = cli(AGENT, *submit)
        require(receipt.get("status") == "succeeded", f"Experiment failed: {receipt.get('error')}")
        require(receipt["data_kind"] == "real", "Experiment receipt must report real data.")
        require(receipt["fingerprints"]["partition"] == campaign["fingerprints"]["partition"],
                "Discovery fingerprint changed.")
        require(receipt["parent_experiment_id"] == parent, "Parent ID changed.")
        require(receipt["budget"]["attempts_remaining"] == ATTEMPTS - iteration, "Incorrect debit.")
        require(cli(AGENT, *submit) == receipt, "Submission replay changed the receipt or budget.")
        require(cli(AGENT, "experiment", "resume", *lookup) == receipt,
                "Repeating resume changed the receipt or budget.")
        discovery = receipt["discovery"]
        context["baseline"] = discovery["benchmark"]
        result = {"status": receipt["status"], "metrics": discovery["metrics"]}
        reflection = mock.reflect(proposal, result, context)
        trace.append({
            "proposal": asdict(proposal), "request": request, "receipt": receipt,
            "reflection": asdict(reflection),
        })
        context["experiments"].append({
            **result, "strategy": proposal.strategy, "decision": reflection.decision,
        })
        parent = receipt["experiment_id"]

    write(path, {**request, "submission_id": "over-budget"})
    refused = cli(AGENT, *submit, expected_exit=2)
    write(path, request)
    require(refused["error"]["code"] == "BUDGET_EXHAUSTED"
            and refused["budget"] == receipt["budget"], "Attempt cap was not enforced.")
    elapsed, rows = ledger(project, campaign_id)
    require(rows == [
        (item["receipt"]["submission_id"], item["receipt"]["experiment_id"], "succeeded")
        for item in trace
    ], "The ledger contains duplicate, missing or incomplete attempts.")
    require(0 < elapsed < SECONDS and SECONDS - elapsed == receipt["budget"]["execution_seconds_remaining"],
            "SQLite execution time does not reconcile to the receipt.")
    output = {
        "campaign": campaign, "trace": trace, "interruption": recovered,
        "refusal": refused, "elapsed_seconds": elapsed, "ledger": rows,
    }
    write(project / "trace.json", output)
    return output


def compact_trace(output):
    return [
        {
            "proposal": item["proposal"], "reflection": item["reflection"],
            "parent_submission": output["trace"][index - 1]["request"]["submission_id"] if index else None,
            "submission": item["request"]["submission_id"],
            **{field: item["receipt"][field] for field in ("fingerprints", "discovery", "diagnostics")},
        }
        for index, item in enumerate(output["trace"])
    ]


def verify(output, forbidden_paths):
    probes = {}
    for path in forbidden_paths:
        try:
            fd = os.open(path, os.O_RDONLY)
        except (FileNotFoundError, PermissionError) as error:
            probes[str(path)] = error.errno
        else:
            os.close(fd)
            raise RuntimeError(f"Research process can open protected path: {path}")
    require(os.getuid() != 0, "Run research with a non-root UID.")
    continuous = run_campaign(output / "continuous", interrupted=False)
    resumed = run_campaign(output / "resumed", interrupted=True)
    expected = compact_trace(continuous)
    require(expected == compact_trace(resumed), "Deterministic output or Mock decision diverged.")
    digest = hashlib.sha256(json.dumps(expected, sort_keys=True, allow_nan=False).encode()).hexdigest()
    summary = {
        "data_kind": "real", "provider": MockProvider.name, "model": MockProvider.model,
        "seed": SEED, "deterministic_trace_sha256": digest, "uid": os.getuid(),
        "inaccessible_paths": probes, "campaigns": [
            {
                "campaign": run["campaign"], "elapsed_seconds": run["elapsed_seconds"],
                "interruption": run["interruption"], "refusal": run["refusal"],
                "trace_receipts": [
                    {
                        "request": item["request"], "proposal": item["proposal"],
                        "reflection": item["reflection"], "experiment_id": item["receipt"]["experiment_id"],
                        "budget": item["receipt"]["budget"], "diagnostics": item["receipt"]["diagnostics"],
                        "metrics": item["receipt"]["discovery"]["metrics"],
                        "start_date": item["receipt"]["discovery"]["start_date"],
                        "end_date": item["receipt"]["discovery"]["end_date"],
                    }
                    for item in run["trace"]
                ],
            }
            for run in (continuous, resumed)
        ],
    }
    write(output / "summary.json", summary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=lambda value: Path(value).resolve())
    actions = parser.add_subparsers(dest="action", required=True)
    prepare = actions.add_parser("setup")
    prepare.add_argument("--store", type=Path, required=True)
    prepare.add_argument("--snapshot-id", required=True)
    research = actions.add_parser("verify")
    research.add_argument("--forbid", type=Path, action="append", required=True)
    args = parser.parse_args()
    if args.action == "setup":
        setup(args.output, args.store, args.snapshot_id)
    else:
        verify(args.output, args.forbid)


if __name__ == "__main__":
    main()
