# Glossary

PrajnaTrade is a single-context repository. These terms are used in issue titles,
tests, CLI output and the ADRs; prefer them over synonyms.

| Term | Meaning |
| --- | --- |
| **Sealed snapshot** | A content-addressed set of synthetic inputs written outside Git by `prajnatrade snapshot seal`. The snapshot ID hashes the canonical manifest, including the three partition fingerprints; a payload is verified against its descriptor before use. |
| **Partition** | One frozen, fingerprinted range of a sealed snapshot: `discovery`, `validation`, or `final_holdout`. The split is `floor(3N/5)`, `floor(N/5)`, remainder. |
| **Discovery / Validation / Final Holdout** | Discovery is the research-visible partition (campaigns, candidates, baselines). Validation and Final Holdout are sealed protected partitions; each is read once through a separately authorized protected evaluation and never re-opened during a replay. |
| **Campaign** | A bounded, frozen research run over the Discovery partition with a fixed provider, config, seed and budget. It owns attempts, experiment receipts and budget accounting. |
| **Experiment** | One submitted attempt inside a campaign, with a hypothesis, strategy/parameters and a receipt-driven result. Parent/child experiment IDs record iteration; resubmission is idempotent. |
| **Candidate** | A frozen, content-addressed experiment result promoted for protected evaluation (`candidate freeze`). Its candidate JSON/hash binds the snapshot, strategy and parameters; a changed input is a new candidate. |
| **Protected evaluation** | The trusted-side Validation or Final Holdout run over a sealed partition: frozen plan, single human authorization, debit-before-read, OS-isolation probe, immutable evaluation record and a coarse research-facing receipt. The evaluator never falls back to a different range. |
| **Query debit** | The one-read-per-partition allowance recorded in `queries.sqlite`, keyed by **(partition name, partition content hash)** before any sealed read. Repackaging the same content under a new snapshot ID cannot buy another read; a retry by the same plan/candidate/stage reuses the stored record. |
| **Controlled report** | The operator-only report rendered from stored immutable evaluation records (`evaluation report`). It shows fingerprints, authorizations, coverage, thresholds, costs, neighbourhood, regimes and curves, and states that it does **not** grant admission. Its verdict names `evidence_complete` and `all_stages_passed`, never admission. |
| **Immutable release** | The unique content-addressed `release_id` created by an explicit human `release approve` over two passed protected stages. The artifact, lifecycle events and explicit rejection decisions are write-once; approval never enables tracking. |
| **Shadow Live** | The trusted forward-accounting ledger for released versions (`shadow update`). Each version has an independent cash/position/order account; one trading day commits atomically in SQLite, reruns return the stored day, and no order is filled retroactively. |
| **Correction disposition** | The trusted re-review of a verified dividend-correction revision against a release (`release disposition`). It records exposure by snapshot lineage plus evaluated ex-date range, classifies `sustained` / `pending_review` / `no_longer_supported`, may pause new signals at the first uncommitted after-close point, and never opens a sealed payload or debits a query. |
| **Scenario status** | In the six-scenario acceptance manifest: `verified`, `blocked`, `failed` or `not_run`, always scoped by data/agent/period. Synthetic evidence verifies only its synthetic scope and never satisfies the real-chain gate. |
