# agent-os — Capability Index (v2)

Every capability below is implemented and **proven by `bash scripts/selftest.sh` → 30/30**.
Proprietary (LICENSE); authorship attested in PROVENANCE.json.

## Orchestration & governance
- Standing **Controller** runs products through SPEC→BUILD→QA→REVIEW→LAUNCH, crash-resumable.
- **21 governed roles** (PM, tech-lead, builders, QA, security, data-scientist, ML-engineer, data-engineer, analyst, finance, incident-commander, …).
- **Capability manifests** enforced by a PreToolUse reference monitor + **OS sandbox** (bubblewrap/seccomp/Landlock, deny-egress) + **Cerbos PDP**.
- **Tamper-evident audit** (HMAC hash-chain) of every decision; **gate-checks** (no stage without artifacts).
- **Upward-feedback Change-Requests** (re-flow), **durable human-approval** (phone), **bounded meetings**.

## Agents & communication
- **Bring-Your-Own-Agent**: Claude (full agent) + any OpenAI-compatible (DeepSeek/OpenAI/Together/Groq/Ollama) — uniformly governed.
- **Durable token-free ask-await** (suspend-until-reply, crash-surviving), **deadlock detection**, typed 12-intent messages.
- **Object store**: images/files/objects shared by sha256 reference, dedup, **TTL+GC**, **encrypted at rest**.
- **Distributed dispatch** over NATS (decoupled workers → horizontal scale).

## Build, test, ship
- **Durable execution** (DBOS on Postgres): crash-resume from exact step, exactly-once.
- **Real governed agent workers** (proven: allowed edits done, forbidden `.env` write denied + audited).
- **QA harness**: live multi-viewport screenshots + axe a11y + behavioral E2E + vision critique.
- **Design-as-code** (DTCG tokens → CSS vars). **Feature flags** + gradual rollout.

## Data & ML
- **Governed data connectors** (live web/API/SNS ingestion via egress allow-list → object store).
- **Experiment tracking** (log/compare/best). **Graph + vector memory** + reflection. **Eval harness** (Inspect AI). **OTel-GenAI tracing**.

## Secrets, scale & ops
- **Scoped secrets vault** (encrypted; test/QA gets test secrets, prod never leaks).
- **Budget governor** (per-product token caps). **Scheduler** (recurring jobs). **Health monitor + alerting**.
- **Multi-tenancy** (each customer isolated). **Self-metrics** (throughput/rework/cost). **Retention sweeps**.
- **HTTP API + dashboard** (token-auth service surface). **One-command product scaffolding**. **Full backup/restore**.
- **Cloud-migratable** (Postgres→RDS, objstore→S3, NATS→managed, vault→KMS — config swaps, no rewrite).

## IP & provenance
- Proprietary LICENSE, **Ed25519-signed provenance** over all design+code, watermark/canary, **whitepaper + patent guide**.

## Requires you (not code) to go further
Native mobile E2E → a **Mac runner**. Production scale (GPU/CDN/many users) → a **cloud account** (OS orchestrates). Going live → **file the PPA**, publish, onboard users (see CEO-TODO.md).
