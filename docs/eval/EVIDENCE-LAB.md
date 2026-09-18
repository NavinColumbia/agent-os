# Evidence Lab and release-evidence runbook

This is the operational boundary between “the code works locally” and “customers should trust or pay for it.”
It deliberately refuses to turn model confidence, screenshots, or a clean local test run into product value.

## What is complete without vendor credentials

- Migration `110-product-evidence-v2.sql` creates append-only tenant-fenced studies, observations, decisions,
  and value receipts. The application database role receives `SELECT, INSERT` only—no mutation or deletion.
- The runtime composes `SQLProductEvidenceStore`; local SQLite development and PostgreSQL deployment use the
  same port and product semantics.
- `/v2/product-studies` and `/v2/value-receipts` enforce tenant identity, role capabilities, idempotency, and
  durable artifact existence. Agents cannot call evaluation or label synthetic output as human/production.
- The CEO workspace presents hypotheses, evidence counts, current dispositions, reasons, and baseline-relative
  value in plain language. Its per-study calibration panel reports synthetic/human preference agreement,
  Cohen's kappa, coverage, delta error, protocol defects, and task/segment/metric disagreements.
- `GET /v2/product-studies/{study_id}/revisions/{revision}/calibration` computes that calibration directly
  from immutable observations. Missing human pairs remain visibly uncalibrated; synthetic votes never fill them.
- `agentos-v2 benchmark-report` validates an exact baseline/candidate task matrix and emits digested system
  outcomes, failure counts, and a conservative value receipt.
- `agentos-v2 resilience-report` evaluates the required six fault classes. A local result always reports
  `production_release_evidence: false`, even if every local scenario passes.
- The API and worker can export vendor-neutral OTLP/HTTP traces through
  `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`; API responses return `X-Trace-ID`, worker spans retain outcome and
  retry class, and neither path exports tenant IDs, prompts, tool inputs, model outputs, query strings, or
  authorization data. Remote staging/production endpoints must use HTTPS; an HTTP localhost collector sidecar
  is permitted. `AOS_V2_OTLP_TRACE_SAMPLE_RATIO` controls parent-based sampling.
- Portable account export includes every Evidence Lab table; right-to-erasure remains catalog-driven.

## Benchmark input contract

Freeze the manifest before trials begin. It names the exact baseline and candidate, task/segment pairs,
repetitions, rubric version, and admitted latency regression. Each trial must contain:

- the manifest ID/revision and one registered task/system/repetition cell;
- success, normalized quality, reliability, p95 input latency, model cost, human minutes, and interventions;
- a first-failure class (`requirements`, `model`, `tool`, `orchestration`, `infrastructure`, `policy`,
  `human_dependency`, or `verification`) when unsuccessful;
- distinct maker and evaluator identities; and
- one or more raw artifact IDs.

Run the deterministic evaluator:

```bash
agentos-v2 benchmark-report \
  --manifest benchmark-manifest.json \
  --trials benchmark-trials.json \
  --customer-price-cents 2000 \
  --human-hourly-value-cents 6000
```

The output is suitable for artifact upload followed by `POST /v2/value-receipts`. Do not publish only positive
receipts; failed and negative comparisons are part of the product record.

## Fault-campaign input contract

Every campaign must pre-register all of:

1. provider throttle;
2. partial tool failure;
3. process death;
4. stale lease;
5. duplicate event; and
6. artifact corruption.

Each scenario sets a recovery deadline and maximum data loss/duplicate effects. Its observation must prove the
injection happened, stayed contained, restored the accepted outcome, engaged the registered fallback, preserved
tenant isolation, and left a complete audit trail with raw evidence IDs.

```bash
agentos-v2 resilience-report \
  --campaign fault-campaign.json \
  --observations fault-observations.json
```

## Remaining evidence—not software claims

The repository cannot truthfully fabricate these gates:

- target-user consent, recruitment, and observed sessions to populate the implemented synthetic-versus-human
  disagreement calibration report;
- representative direct-model and Agent OS executions using real provider accounts and paid-customer tasks;
- a deployed OTLP collector/backend plus production-shaped fault injection and alert-to-diagnosis-to-recovery
  evidence;
- external OIDC, secret manager, object storage, notification, billing, sandbox, deployment, backup/restore,
  domain/TLS, accessibility, and security-review evidence; and
- at least one attributable customer outcome showing value above price.

Those runs require the corresponding accounts/credentials, real participants, and a deployed environment. The
system must continue to label them pending until their artifacts exist; configuration alone is not evidence.
