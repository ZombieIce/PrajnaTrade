# Issue #27 controlled real-model session — evidence record

**Status: template / not run.** No external model session, real campaign,
container execution or real receipt exists for issue #27 yet. Every value below
is marked `<pending>` until the trusted operator performs the session and fills
it in from the actual artifacts. Nothing in this file is real-model evidence,
and the `controlled_real_llm` scenario in
[`issue-17-manifest.json`](issue-17-manifest.json) must stay `blocked` until
this record is complete and accepted.

The relay and workspace mechanics are covered by synthetic public-script tests;
see [Mechanism verification](#mechanism-verification-synthetic-only).

## Session reference

| Field | Value |
| --- | --- |
| DSH GUI URL | `<pending: http(s) URL of the session>` |
| DSH session ID | `<pending>` |
| Model identity (provider/model as shown by the session) | `<pending>` |
| Session start (UTC) | `<pending>` |
| Session end (UTC) | `<pending>` |
| Trusted operator | `<pending>` |
| Redacted transcript location (outside Git) | `<pending>` |
| Redacted transcript SHA-256 | `<pending>` |
| Notes directory and SHA-256 of its sorted per-file digests | `<pending>` |
| Attribution note | `<pending: how the session reference maps to the two submissions>` |

Do not record API keys, cookies, account identifiers or environment values here
or in the transcript. The session reference is an attributable identifier, not
a credential.

## Campaign and public metadata

Populated from the trusted `campaign.json` installed into the project-only
workspace; the workspace has no trusted path.

| Field | Value |
| --- | --- |
| Public campaign ID | `<pending>` |
| Sealed snapshot ID | `<pending>` |
| `data_kind` | `<pending: must be real>` |
| Discovery start / end / sessions | `<pending>` |
| Discovery content SHA-256 | `<pending>` |
| Attempt budget | `<pending>` |
| Execution budget (seconds) | `<pending>` |
| Strategy executor kind | `<pending: must be container>` |
| Executor image / immutable digest | `<pending>` |
| Research workspace location (outside Git) | `<pending>` |
| Workspace commit / template hash | `<pending>` |
| Executor implementation record | [issue-27-python-executor.md](issue-27-python-executor.md) |

Budget sizing note: the accepted container executor starts one fresh
container per rebalance (about 0.68 s per invocation on the reference machine).
A full 656-session Discovery run is roughly 650 invocations, i.e. several
minutes of wall time, so the campaign must be sized for minutes rather than
the #26 Mock's 90 seconds; 3600 execution seconds covers two full Python
attempts with headroom. One Python submission is still one attempt.

## The two submissions

One controlled iteration means two linked submissions: a falsifiable first
hypothesis, then a follow-up whose stated reason cites the first real receipt.
At least one strategy must be Python source written or modified by the model
and executed by the trusted container executor.

| Field | Submission 1 | Submission 2 (follow-up) |
| --- | --- | --- |
| `submission_id` | `<pending>` | `<pending>` |
| `experiment_id` | `<pending>` | `<pending>` |
| `parent_experiment_id` | none | `<pending: first experiment_id>` |
| Hypothesis (verbatim model text) | `<pending>` | `<pending>` |
| Strategy family | `<pending>` | `<pending>` |
| Strategy source SHA-256 | `<pending>` | `<pending>` |
| Python source executed by the container executor | `<pending: yes/no; source digest>` | `<pending>` |
| Parameters | `<pending>` | `<pending>` |
| Receipt file and SHA-256 | `<pending>` | `<pending>` |
| Receipt status | `<pending>` | `<pending>` |
| Attempts remaining after receipt | `<pending>` | `<pending>` |
| Execution seconds remaining after receipt | `<pending>` | `<pending>` |

The submission files and raw receipts stay in the trusted evidence bundle
outside Git; only redacted IDs, hashes and summaries are recorded here.

## First receipt and the model's follow-up reason

- First receipt observed by the model at `<pending: timestamp>` from
  `outbox/<submission_id>.receipt.json`, SHA-256 `<pending>`.
- Metrics reported by the receipt (quote, do not invent): total return,
  benchmark, fees, slippage, return delta, fills/order attempts, NAV
  availability. `<pending>`
- Model's stated reason for the follow-up, quoted from
  `notes/<NNN>-<submission_id>.md`: `<pending>`
- Notes file SHA-256: `<pending>`
- How the follow-up uses the receipt (for example a different lookback or
  weight rule motivated by the observed metric): `<pending>`

A follow-up that is pre-recorded, manually written by the operator or not
linked to the first real receipt does not satisfy #27.

## Results, attempts and time

| Measurement | Submission 1 | Submission 2 | Campaign total |
| --- | ---: | ---: | ---: |
| Attempts consumed | `<pending>` | `<pending>` | `<pending>` |
| Execution seconds charged | `<pending>` | `<pending>` | `<pending>` |
| Final NAV (CNY) | `<pending>` | `<pending>` | n/a |
| Benchmark final NAV (CNY) | `<pending>` | `<pending>` | n/a |
| Total return / return delta | `<pending>` | `<pending>` | n/a |
| Fees / slippage (CNY) | `<pending>` | `<pending>` | `<pending>` |
| Bridge / interrupted attempts | `<pending>` | `<pending>` | `<pending>` |

Reconcile the receipt budget counters against SQLite with a read-only query;
record the query and result in the trusted bundle.

## Model usage

**No API invoice is available for this session and none is claimed.**

- The external model is a DeepSeek Harness session, not an
  OpenAI-compatible API call: there is no provider endpoint, model request ID,
  prompt/completion token count or API charge attributable to this project.
- DSH session usage/billing, if the operator can export it, is recorded as
  `<pending: available / unavailable>`; a DSH session reference is not an API
  invoice and an estimate is not a charge.
- What is attributable: the DSH session reference above, the two submission
  IDs and experiment IDs, the receipt hashes and the timestamps.
- No credentials or provider environment variables were supplied to the
  research workspace; `<pending: confirm none were present>`.

## Deployment isolation evidence

Record the trusted-side inspection receipts, not just the intent. All fields
are `<pending>` unless the real run produced them.

| Check | Expected | Observed |
| --- | --- | --- |
| Container runtime and version | Colima/Docker available on the trusted host | `<pending>` |
| Executor image digest at campaign creation | same as `campaign.json` `executor.digest` | `<pending>` |
| Model-generated source mount | read-only, only the submitted source/window | `<pending>` |
| Trusted store / project / repo mounts | none | `<pending>` |
| Network | disabled | `<pending>` |
| UID/GID | 65534 | `<pending>` |
| Root filesystem | read-only | `<pending>` |
| Capabilities | all dropped, no privilege escalation | `<pending>` |
| Expected-rejection probe (for example a sealed path) | `ENOENT` / denied | `<pending>` |
| Resource-abort probe (CPU/output/time) | bounded and recorded | `<pending>` |
| Execution exit status | zero for each executed strategy | `<pending>` |
| Research-dir probe from the research container | cannot open the trusted project/store/repo | `<pending>` |

## Research-boundary checks

- `campaign.json` contains only the public allowlist (`campaign_id`,
  `snapshot_id`, `data_kind`, Discovery range/content hash, budgets, executor
  image/digest/limits). `<pending: attach its SHA-256>`
- No file in the research workspace contains the snapshot-store path, the
  trusted-project path or a `.prajnatrade` path. `<pending: command and result>`
- Every relay-written receipt and sidecar is path-redacted; the sidecar
  `redacted` flag is recorded. `<pending>`
- The model never received the agent CLI, sealed payload, protected
  partitions, Validation/Final Holdout outcomes or credentials. `<pending>`
- `processed/` retains both requests; `outbox/` contains both receipts and
  sidecars; `notes/` contains the model's reasoning. `<pending: hashes>`

## Mechanism verification (synthetic only)

The following is available from the repository and is **not** real-model
evidence. It verifies the relay/workspace mechanism only.

| Check | Command | Result |
| --- | --- | --- |
| Relay and workspace tests | `PYTHONPATH=src python -m pytest -q tests/test_issue27_relay.py tests/test_issue27_workspace.py tests/test_issue27_setup_campaign.py` | `29 passed` |
| Lint | `ruff check .` | `All checks passed.` |
| Existing suite unaffected | `scripts/test.sh fast` | `202 passed, 3 skipped, 118 deselected` on the integrated branch (164 pre-existing fast tests + 29 relay/workspace/setup + 9 fake-executor tests; the 3 skips are Docker-gated and the 118 isolation-marked tests are unchanged) |

The deployment-isolation evidence required by #12/#27 must come from the real
run; synthetic probes and trusted-local execution do not count.

## Remaining blockers

#27:

- [ ] No authorized external-model session has run; the DSH session reference
      and redacted transcript are pending.
- [x] The container-executor submission path is integrated and advertised by
      `campaign create --help` (`--strategy-executor container`,
      `--strategy-image IMAGE`); the trusted-side boundary evidence is in
      [issue-27-python-executor.md](issue-27-python-executor.md). It still
      awaits a real campaign and real model session.
- [ ] No real Discovery campaign has been created for this session; the
      campaign ID, snapshot, budgets and executor digest are pending.
- [ ] The two linked submissions, the model-read first receipt and the
      self-justified follow-up have not been produced.
- [ ] Real container-isolation evidence for the executed Python strategy has
      not been collected.
- [ ] Model usage is only a session attribution; no API invoice exists.

#28 and later:

- [ ] Protected Validation/Final Holdout evaluation on the real campaign,
      with separate human authorization, debit-before-read and the controlled
      report, remains blocked (`#12`, `#28`).
- [ ] An approved release and reconciled forward Shadow Live days on real
      data remain blocked (`#9`, `#14`).
- [ ] End-to-end acceptance (`controlled_real_llm` and the real chain in
      [`issue-17-manifest.json`](issue-17-manifest.json)) remains `blocked`
      until the items above are real and accepted.

## Operator replay outline

Use placeholders, never a checked-in trusted path:

```sh
# 1. Build the project-only workspace outside Git (once).
python scripts/issue27_research_workspace.py \
  --output ~/PrajnaTrade-research/issue27

# 2. Create the real campaign on the trusted side and publish public metadata.
python scripts/issue27_setup_campaign.py \
  --store <trusted-snapshot-store> \
  --snapshot-id <approved-snapshot-id> \
  --trusted-project <trusted-project-outside-research> \
  --research-dir ~/PrajnaTrade-research/issue27 \
  --strategy-image prajnatrade-issue27-runner:local \
  --attempts <attempts> --max-seconds 3600

# 3. Start the new DSH session in the research workspace with AGENT_PROMPT.md,
#    then relay its requests (run one mode at a time):
python scripts/issue27_relay.py \
  --project <trusted-project-outside-research> \
  --research-dir ~/PrajnaTrade-research/issue27 \
  --campaign-id <campaign-id> --serve --poll-interval 5

# 4. Answer one interrupted/pending submission without an attempt:
python scripts/issue27_relay.py \
  --project <trusted-project-outside-research> \
  --research-dir ~/PrajnaTrade-research/issue27 \
  --campaign-id <campaign-id> --status <submission-id>
```

The setup script refuses a mismatched existing campaign; use `--force-new`
only when a fresh campaign is intended. The relay refuses a research directory
that is inside the project or contains `.prajnatrade`/`project.sqlite` data.

## Sign-off

- Operator: `<pending>`
- Date: `<pending>`
- Human acceptance decision: `<pending>`
