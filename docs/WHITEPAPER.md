# A Single-Box, Privacy-First, Governed Operating System for Heterogeneous AI Agents

**Author:** Navin (github.com/NavinColumbia). **Status:** working paper, v1 (2026). **Proprietary — see LICENSE.**
Provenance: this document is covered by the Ed25519-signed manifest in `PROVENANCE.json`.

## Abstract
We present **agent-os**, an operating system for autonomous AI agents that runs entirely on a single
machine, preserves privacy by construction (no cloud dependency; localhost/Tailscale only), and applies
**uniform, enforceable governance to heterogeneous third-party agents** — Anthropic Claude, DeepSeek,
OpenAI, or local models — without modifying those agents. The system composes (1) a durable-execution
backbone, (2) capability-manifest enforcement combined with OS-level sandboxing and an externalized policy
decision point, (3) a tamper-evident, hash-chained decision audit, (4) a durable, token-free
"ask–await" inter-agent communication fabric with deadlock detection, and (5) a crash-resumable lifecycle
orchestrator with artifact-gated stage transitions and self-measurement. Every component is implemented and
empirically validated by an automated regression suite (15/15 passing). We argue the integrated design is
novel for the *single-box, privacy-first, provider-agnostic* deployment class that existing cloud/cluster
multi-agent frameworks do not target, and we identify the specific defensible contributions.

## 1. Problem
Production multi-agent systems today assume the cloud: distributed orchestrators, managed durable-execution
clusters, vendor-locked agent protocols, mutable observability stores, and trust in the provider's data
handling. This excludes a large and growing class of users — privacy-sensitive developers, regulated teams,
and individuals — who want an autonomous agent organization that (a) runs on hardware they control, (b) never
sends their code or data off-box, (c) lets them use *whatever* agent/model they prefer (or can afford), and
(d) provably constrains and audits what those agents do. No integrated system targets this class.

## 2. Architecture
agent-os runs on one host (Windows+WSL2/Ubuntu reference) over three local services — PostgreSQL+pgvector,
NATS/JetStream, and a self-hosted notification bus — plus a localhost policy engine. A standing **Controller**
orchestrates role agents through a product lifecycle. The contributions:

**C1 — Durable-execution backbone (single Postgres).** Agent work runs as durable workflows whose state and
side-effects are checkpointed in the *same* Postgres that stores application state, yielding transactional,
exactly-once step execution and automatic resume-from-last-step after any crash — with no separate workflow
cluster. (Validated: a workflow hard-killed mid-run resumes at the exact step; completed steps are not re-run.)

**C2 — Uniform governance of heterogeneous agents (the BYO-agent core).** A capability *manifest* per agent
role declares allowed tools, paths, and forbidden command patterns. Enforcement is layered: a pre-execution
reference monitor (deny-beats-allow) → an externalized Policy Decision Point (policy-as-code, git-versioned)
→ OS-level sandbox (namespace + syscall filtering + deny-by-default network egress). The **same** governance
applies to any provider — a full Claude agent or any OpenAI-compatible endpoint (DeepSeek/OpenAI/local) —
without changing the agent. (Validated: a live third-party agent performed allowed edits and was *denied*
writing a protected file, with the denial recorded immutably.)

**C3 — Tamper-evident decision provenance.** Every allow/deny/executed decision is appended to an
HMAC-SHA256 **hash-chained** log; any edit, deletion, or reordering — even by an actor with database write
access — is detectable. (Validated: a `deny→allow` tamper was caught.)

**C4 — Durable, token-free ask–await communication.** An agent that needs an answer suspends as a durable
workflow — holding *no* process and consuming *no* tokens while parked — and resumes when the reply event
arrives, **surviving the asker's crash**. Every wait carries a deadline (no unbounded waits), and a Controller
maintains a wait-for graph with strongly-connected-component **deadlock detection** and deterministic victim
selection. A small typed performative vocabulary enables deterministic dispatch. (Validated: requester
suspended → process killed → reply sent by another process → requester resumed with the answer; A↔B cycle
detected.)

**C5 — Schema-checked output verification.** Consumers validate an agent's output against the producer's
advertised output schema before acceptance — a concrete attack on the inter-agent *semantic verification* gap
that current agent-communication protocols leave open.

**C6 — Artifact-gated, self-measuring lifecycle.** The Controller cannot enter a stage whose required
artifacts are absent (e.g., no BUILD without an approved spec+ADR; no LAUNCH without a QA report), making the
governance model self-enforcing rather than advisory. The org records an append-only metrics ledger
(throughput, **rework rate**, feedback-loop latency, cost) so it measurably improves itself over time.

**C7 — Cryptographic authorship provenance of the design itself.** The full design+code is hashed into a
signed manifest with a unique build fingerprint, providing portable proof of authorship and a theft canary.

## 3. Implementation & validation
~25 components, ~2.5k LOC, on the stack above. A single command (`selftest.sh`) re-proves the entire system:
enforcement (8/8 deny/allow), sandbox (egress+filesystem blocked), audit integrity + tamper detection, PDP
(6/6), signed identity, deadlock detection, durable execution, ask-await, graph memory + reflection, eval
harness, tracing, stage-gates, metrics, and the full Controller lifecycle — **15/15 passing**.

## 4. Relation to prior work
Cloud multi-agent frameworks (orchestrator-worker research systems, graph orchestrators, role-based "software
company" agent teams) target distributed, cloud, often single-provider deployments. Durable-execution engines
and policy engines exist as *separate* products. Agent-communication standards (MCP for tools; A2A/ANP for
cross-org agent interop) target federation across trust domains. **agent-os's contribution is the integration**:
binding durable execution, layered capability governance, tamper-evident provenance, durable token-free
communication, and provider-agnostic agent governance into one privacy-first, single-host system — the
deployment class none of the above serves. The single-box constraint is exploited as an advantage (Postgres
co-location → transactional exactly-once; one trust domain → no federation/PKI overhead).

## 5. Implications
The design enables a product where users **bring their own agent and API tokens** and run an autonomous,
auditable agent organization on their own hardware. It is directly applicable to privacy-regulated software
development, and its governance/provenance layers address the accountability requirements emerging in
agentic-AI security guidance.

## 6. Reproducibility & IP
All claims are reproducible via `selftest.sh`. The Work is proprietary (LICENSE); authorship is attested by
`PROVENANCE.json`. Patentable subject matter is enumerated in `docs/PATENT-GUIDE.md`.
