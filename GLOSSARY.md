# Glossary

PrajnaTrade is a single-context repository. These terms are used in issue titles,
tests, CLI output and the ADRs; prefer them over synonyms.

| Term | Meaning |
| --- | --- |
| **Sealed snapshot** | A content-addressed set of inputs sealed outside Git. The snapshot ID hashes the canonical manifest, including the three partition fingerprints; a payload is verified against its descriptor before use. |
| **Partition** | One frozen, fingerprinted range of a sealed snapshot: `discovery`, `validation`, or `final_holdout`. The split is `floor(3N/5)`, `floor(N/5)`, remainder. |
| **Discovery / Validation / Final Holdout** | Discovery informs initial strategy development. Validation supplies reusable evidence for revision and selection under a budget. Final Holdout provides a controlled independent test of a frozen selection. |
| **Research direction** | A strategy design with a stable identity across names, revisions and Campaigns. The research lead declares whether prior Holdout feedback influenced the design. |
| **Research round** | A Campaign within a direction with a confirmed objective, constraints, comparison protocol and Validation budget. It ends in selection or a retrospective. |
| **Campaign** | A bounded, frozen research run over the Discovery partition with a fixed provider, config, seed and budget. It owns attempts, experiment receipts and budget accounting. |
| **Experiment** | One submitted attempt inside a campaign, with a hypothesis, strategy/parameters and a receipt-driven result. Parent/child experiment IDs record iteration; resubmission is idempotent. |
| **Candidate** | A frozen experiment selected for Final Holdout. Its selection links the strategy version, the precise Validation evidence, the reasons for selection and the remaining risks. |
| **Protected evaluation** | A trusted Validation or Final Holdout run under a fixed protocol that records authorization, the exact range, isolation evidence, an allowance debit and the scored result. |
| **Query debit** | The allowance charged before a sealed read. Each new version uses one Validation allowance in its round. A direction uses one Holdout opportunity for an interval. The legacy debit represented a whole partition. |
| **Holdout authorization** | The research lead's approval to evaluate the exact frozen selection and enable that release on a trusted pass. It records the range, the comparison evidence, and the lead's selection independence declaration. |
| **Controlled report** | The operator-only report rendered from stored immutable evaluation records (`evaluation report`). It shows the exact plan, linked authorizations and debits, coverage, thresholds, costs, neighbourhood, regimes, curves and evidence verdict. |
| **Immutable release** | The unique version of a frozen Candidate backed by successful evidence and human approval. A trusted Holdout pass can fulfil conditional approval and schedule its enablement. |
| **Shadow Live** | The trusted forward-accounting ledger for released versions (`shadow update`). Each version has an independent cash/position/order account; one trading day commits atomically in SQLite, reruns return the stored day, and no order is filled retroactively. |
| **Correction disposition** | The trusted re-review of a verified dividend-correction revision against a release (`release disposition`). It records exposure by snapshot lineage plus evaluated ex-date range, classifies `sustained` / `pending_review` / `no_longer_supported`, may pause new signals at the first uncommitted after-close point, and never opens a sealed payload or debits a query. |
| **Scenario status** | In the six-scenario acceptance manifest: `verified`, `blocked`, `failed` or `not_run`, always scoped by data/agent/period. Synthetic evidence verifies only its synthetic scope and never satisfies the real-chain gate. |
