# Architecture Research Synthesis: Production-Grade Governed Autonomous Multi-Agent Platform

> **Scope.** This report synthesises findings from eight primary research documents (findings/00–07) to answer three production questions for a governed autonomous multi-agent software factory built on durable brokered mailboxes, a presence directory, a watchdog + self-healing responder, and Postgres as the control plane: (a) how to harden inter-agent communication and coordination at scale, (b) how to make watchdog/observability/self-healing production-ready, and (c) how to package and publish the platform to the Apple App Store, Google Play, and web. Sources are cited inline; weakly-sourced or contested claims are flagged.

---

## Key Findings (Executive Summary)

1. **Exactly-once delivery is theoretically impossible** across two separate systems. The practical production standard is at-least-once delivery combined with idempotent consumers (outbox + inbox deduplication). All architecture decisions for inter-agent messaging should start here.

2. **The control plane must be deterministic, not probabilistic.** Agents *suggest*; Postgres *enforces*. Hard limits, idempotency keys, and audit records must live in the relational layer, not in LLM reasoning chains.

3. **Hybrid coordination wins.** Orchestration within a domain (explicit workflow sequencing with saga rollback) and choreography across domains (event-driven, broker-mediated) gives the best balance of visibility and autonomy. Neither pure pattern alone scales.

4. **Backpressure is not optional at fleet scale.** The MAST study (March 2025, 1,600+ execution traces) found failure rates of 41–87% in unstructured multi-agent networks; bounded queues, semaphores, AIMD adaptive concurrency, and circuit breakers are the minimum viable stack. [Finding 01]

5. **AI-specific failure modes break classical watchdog design.** Silent semantic failure (process alive, model producing garbage) and stuck generation (streaming response that never completes) require quality-validation watchdogs beyond liveness pings. [Findings 02, 03]

6. **Circuit breaker state must be externalized** (Redis-backed counters across all scheduler replicas). A circuit breaker that lives inside the agent process can be bypassed by prompt injection or model misbehavior. [Finding 02]

7. **Standard containers are insufficient for untrusted agent code.** Hardware-enforced microVM isolation (Firecracker, Kata Containers) is the industry consensus for 2026. [Finding 04]

8. **OpenTelemetry is the only viable foundation** for agent fleet observability, but the GenAI Semantic Conventions are still in Development status (v1.41 as of mid-2026) — attribute names can still change before GA. [Finding 05]

9. **Apple Guideline 2.5.2 and Google's AccessibilityService prohibition are the two hardest platform blockers** for autonomous developer tools. Both enforced with high-profile rejections in early 2026. [Finding 06]

10. **EU AI Act full enforcement begins August 2, 2026.** US companies serving EU users are in scope. Key obligations: Article 50 AI disclosure, open-loop architecture, human oversight mechanisms, and control to stop/override agents at runtime. [Finding 07]

---

## Part A: Hardening Inter-Agent Communication and Coordination at Scale

### A.1 Orchestration vs. Choreography: Choosing the Right Pattern

| Pattern | Strengths | Weaknesses | Best used for |
|---|---|---|---|
| **Orchestration** | High observability; explicit state machine; saga rollback in one place | Single point of failure; orchestrator is bottleneck | Intra-domain workflows with chained task graphs |
| **Choreography** | Loose coupling; natural resilience to broker crash; independent scaling | Distributed state; end-to-end tracing is harder; ordering complexity | Cross-domain events; independent agent triggers |
| **Hybrid (recommended)** | Visibility at micro-level + autonomy at macro-level | Higher design complexity | Production multi-agent systems |

The recommended pattern: use orchestration *within* a domain (e.g., retrieve → rank → generate) and choreography *across* domains (e.g., completed pipeline result emits an event that triggers a downstream audit agent). This matches both the Microsoft multi-agent reference architecture guidance and production patterns observed in agentic deployments. [Finding 00]

**Sources:** [Microsoft multi-agent reference architecture](https://microsoft.github.io/multi-agent-reference-architecture/docs/agents-communication/Message-Driven.html); [n8n orchestration vs. choreography blog](https://blog.n8n.io/orchestration-vs-choreography/)

### A.2 Postgres-Native Queue Mechanisms: What to Use and When

The project already uses Postgres as its control plane. The recommended queue primitive stack, from lowest to highest abstraction:

#### A.2.1 `SELECT … FOR UPDATE SKIP LOCKED` (foundational primitive)
```sql
SELECT * FROM task_queue
WHERE status = 'pending'
ORDER BY created_at
LIMIT 1
FOR UPDATE SKIP LOCKED;
```
- `SKIP LOCKED` enables parallel consumption without contention.
- FIFO ordering requires a stable `ORDER BY` key (auto-increment `id` or `created_at`); without it, order is non-deterministic under concurrent consumers.
- This is the basis for PGMQ and pg-boss; use directly only if you need tight control over schema.

**Source:** [PostgreSQL as Message Broker — Epilis Blog](https://www.epilis.gr/en/blog/2023/12/24/postgresql-message-broker/); [PostgreSQL + SKIP LOCKED — Medium / The Atomic Architect](https://medium.com/@the_atomic_architect/postgresql-replaced-my-message-queue-and-taught-me-skip-locked-along-the-way-87d59e5b9525)

#### A.2.2 PGMQ (extension or pure SQL)
- Visibility-timeout model: `pgmq.read(queue, vt, n)` makes messages invisible for `vt` seconds. Messages reappear if the consumer does not delete/archive within the window.
- FIFO queues (message group keys) support per-key ordering; standard queues do not guarantee order under concurrent consumers.
- **Gap:** No native dead-letter queue in current PGMQ documentation — DLQ must be implemented at the application layer. Verify against current PGMQ changelog before committing to this library.
- Used in production at Supabase, Tembo, and pgflow.

**Source:** [PGMQ GitHub](https://github.com/pgmq/pgmq)

#### A.2.3 pg-boss (recommended for most teams)
- Node.js library; wraps `SKIP LOCKED` with automatic retries + exponential backoff, **native dead-letter queues**, priority queues, cron scheduling, and job dependency graphs.
- Jobs created inside an existing Postgres transaction — critical for the transactional outbox pattern.
- Works in Kubernetes ReplicaSet / multi-master topologies.
- Provides ORM adapters (Prisma, Knex, Drizzle).

**Source:** [pg-boss GitHub](https://github.com/timgit/pg-boss)

#### A.2.4 `LISTEN`/`NOTIFY` (wake-up signal only)
- At-most-once. Events are in-memory only; a disconnected subscriber **permanently loses the notification**.
- Use exclusively as a low-latency "new work available" signal paired with a durable polling loop. Never use as the primary delivery channel.

### A.3 Transactional Outbox Pattern (Canonical Atomicity Guarantee)

The outbox pattern ensures agent state changes and outgoing messages are atomic:

1. **Same transaction:** the agent writes its business-logic state change *and* inserts a row into an `outbox` table.
2. **Relay process** (a separate deployment, not co-located with agents): polls `outbox WHERE sent_at IS NULL ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT N`, publishes to the downstream queue, marks `sent_at`.
3. **Guarantee:** at-least-once — if the relay crashes between publish and `UPDATE`, the row redelivers. Downstream consumers must be idempotent.

Extended outbox schema fields worth tracking: `entity_id`, `entity_name`, `retries`, `status`, `error_message`, `correlation_id`.

**Operational requirements:**
- Run the relay as an independent deployment unit (not colocated with API replicas) to avoid lock contention.
- Automate outbox table cleanup; unprocessed rows accumulate silently.
- Instrument: unsent event age, incoming rate, outgoing dispatch rate, relay error rate.

**Sources:** [Transactional Outbox Pattern: Theory to Production — npiontko.pro](https://www.npiontko.pro/2025/05/19/outbox-pattern); [Transactional Outbox Pattern — gmhafiz.com](https://www.gmhafiz.com/blog/transactional-outbox-pattern/)

### A.4 Delivery Semantics: Honest Assessment

| Semantic | Achievability |
|---|---|
| At-most-once | Trivial (LISTEN/NOTIFY, unacked INSERT) |
| At-least-once | Practical production standard — achievable |
| Exactly-once delivery | **Theoretically impossible** in a distributed system |
| Exactly-once processing | Achievable via idempotent consumers + deduplication |

**Source:** [No such thing as exactly-once delivery — Sequin Blog](https://blog.sequinstream.com/at-most-once-at-least-once-and-exactly-once-delivery/); [Exactly-once message delivery — exactly-once.github.io](https://exactly-once.github.io/posts/exactly-once-delivery/)

### A.5 Idempotency: Implementation Patterns

Each message must carry a stable `idempotency_key` (UUID or content hash). The consumer stores processed keys in a deduplication store. Processing the message and recording the key must happen in **the same atomic transaction**.

**Outbox + Inbox (dual-write solution):**
```sql
-- Producer: single transaction, business change + outbox record
BEGIN;
  UPDATE agent_tasks SET status = 'working' WHERE id = $1;
  INSERT INTO outbox_messages (id, occurred_on, type, data)
    VALUES (gen_random_uuid(), now(), 'task.assigned', $payload);
COMMIT;

-- Consumer: deduplication + state update in one transaction
BEGIN;
  INSERT INTO inbox_messages (id, received_at)
    VALUES ($message_id, now())
    ON CONFLICT (id) DO NOTHING;
  -- only proceed if insert succeeded (check rows affected)
  UPDATE agent_tasks SET status = 'completed' WHERE ...;
COMMIT;
```

Three idempotency key strategies with trade-offs:
- **UUIDs/ULIDs:** Simple; storage grows with volume. Use ULIDv7 for time-based expiry. Inbox retention window must exceed the maximum redelivery window.
- **Monotonic sequences:** Storage-efficient; producer must serialize assignments (bottleneck under multi-producer workloads).
- **CDC/WAL offsets (recommended for scale):** Uses Postgres WAL LSN via Debezium or a similar CDC tool. Eliminates producer sequencing bottleneck. Adds CDC infrastructure.

**Sources:** [Outbox, Inbox patterns — Event-Driven.io](https://event-driven.io/en/outbox_inbox_patterns_and_delivery_guarantees_explained/); [On Idempotency Keys — Gunnar Morling](https://www.morling.dev/blog/on-idempotency-keys/)

### A.6 Message Ordering at Scale

Pure FIFO is easy to state, hard to preserve under concurrent consumers:
- **Single consumer:** ordering guaranteed by `ORDER BY id`.
- **Multiple concurrent consumers with SKIP LOCKED:** different workers acquire rows out-of-order; global FIFO is lost.
- **Per-key partitioning** (route messages with the same `agent_id`/`entity_id` to the same consumer shard): preserves ordering per entity. PGMQ message group keys implement this natively.
- **Sequence numbers / version tags:** embed a monotonic version in the message payload; consumers reject or queue messages whose version is not `current_version + 1`.
- **Optimistic locking:** consumer reads `(data, version)`, processes, then `UPDATE … WHERE version = $read_version`. Conflict → retry.

### A.7 Backpressure: Minimum Viable Stack for Fleet Scale

Without backpressure, queues grow without bound. The MAST study (1,600+ execution traces, March 2025) found failure rates of 41–87% in unstructured pipelines.

**Layered backpressure stack:**

1. **Presence directory capacity signals:** an overloaded agent updates its Agent Card with `capacity: low` or `status: busy`. Upstream callers check the directory and route to an alternative agent before even issuing work to the mailbox. [Finding 01]

2. **Bounded inbound mailbox:** hard capacity limit on the task queue. When full, return `503 / queue-full` immediately — do not silently enqueue. Capacity formula: `queue_length = service_rate × tolerable_delay`. [Finding 01]

3. **Semaphore concurrency cap:** limit simultaneous in-flight requests to a downstream agent. Discover the right value empirically — the largest concurrency at which the downstream error rate does not climb. Example finding: ~3 concurrent GitHub Push operations in one production deployment. [Finding 01]

4. **AIMD adaptive concurrency (TCP-analogue):** start at concurrency 2. On success within latency targets, increment by 1. On error or SLO breach, halve. Avoids guessing a fixed limit upfront. [Finding 01]

5. **Token bucket rate limiting:** address per-second rate limits (orthogonal to concurrency caps). Refill slightly below the downstream's documented limit; absorb momentary spikes via burst capacity. [Finding 01]

6. **Priority queuing + load shedding:** classify tasks P0 (user-facing), P1 (enrichment), P2 (optional elaboration). Under load, drop P2 immediately, then P1. Track what was shed for audit. [Finding 01]

7. **Three-level token budget:** hard ceiling at job level, proportional fraction per sub-agent, strict per-call limit. Each level enforces independently. [Finding 01]

8. **Circuit breaker on downstream error rate:** rolling window (e.g., 50% failures over last 10 calls). When threshold crossed, open circuit — return cached fallback or immediate failure. Re-attempt after cooldown. [Findings 01, 02, 03]

**Sources:** [Backpressure in Agent Pipelines — TianPan.co](https://tianpan.co/blog/2026-04-12-backpressure-in-agent-pipelines-when-ai-generates-work-faster-than-it-can-execute); [Flow Control for Autonomous Agents — Antigravity Lab](https://antigravitylab.net/en/articles/agents/antigravity-agent-flow-control-backpressure-queue-design)

### A.8 Dead-Letter Queues

Messages that fail after all retries must not loop forever or be silently dropped.

**Core mechanism (visibility timeout + receive count):**
1. Consumer fetches message → it becomes invisible for a configurable timeout.
2. On success, consumer deletes the message.
3. On failure/crash, timeout expires → message becomes visible again.
4. After `maxReceiveCount` attempts, the broker routes to the DLQ.

**DLQ metadata to capture:** original queue/topic, task ID, failure reason, exception stack, attempt count, first/last-attempt timestamps, full unmodified payload, delivery count.

**Automated failure triage:**

| Category | Detection | Action |
|---|---|---|
| Retriable | Transient (timeout, 5xx) | Auto-replay with backoff after delay |
| Fixable | Schema mismatch, validation | Route to manual-review dashboard |
| Poison | Permanent errors, logic bugs | Quarantine + alert; archive to cold storage |

**Recommended retry counts by error type:**
- Transient errors (timeouts, 503): 3–5
- Rate limits (429): 5–10
- Schema/deserialization errors: 0–1 (retry is pointless without a code fix)

**DLQ alerting:** alert on absolute depth ≥ 10 (warning) / ≥ 100 (critical) AND on rate-of-change — sudden spikes demand urgency even at low absolute counts.

In a durable-mailbox architecture, the DLQ is a second task store with `status: dead_letter`. Dead-lettered tasks retain full history and can be patched and re-enqueued without touching the main mailbox.

**Sources:** [Dead Letter Queue Patterns — Codelit.io](https://codelit.io/blog/dead-letter-queue-patterns); [Dead Letter Channel — Enterprise Integration Patterns](https://www.enterpriseintegrationpatterns.com/patterns/messaging/DeadLetterChannel.html); [Integration Patterns IV: Retries and DLQs — LittleHorse](https://littlehorse.io/blog/retries-and-dlq)

### A.9 Postgres as Deterministic Control Plane

Key principle: **separate the probabilistic agent layer from the deterministic policy layer**.

```
[LLM Agent]  →  [Deterministic Policy Engine (Postgres)]  →  [Effectors / Other Agents]
                  - enforces hard limits (tier caps, cooldowns)
                  - one-action-per-cycle guarantee (advisory locks)
                  - structured audit log
                  - idempotency key uniqueness constraint
```

- Agents *suggest* actions; the policy engine *enforces* invariants before committing.
- Direct agent access to destructive operations (schema changes, `DROP TABLE`, cluster failover) must be gated through the policy engine, not granted directly.
- Structured Postgres logs are auditable; raw LLM traces are not suitable for compliance.
- Model version changes and prompt drift can silently alter agent behavior — Postgres is the source of truth for what *actually happened*.

**Source:** [The Agentic Confusion: Deterministic Postgres Control Plane — EnterpriseDB](https://www.enterprisedb.com/blog/agentic-confusion-why-i-keep-my-postgres-control-plane-deterministic)

### A.10 Known Gaps and Open Questions (Inter-Agent Messaging)

- **Temporal / durable workflow engines:** Tools like Temporal (also Postgres-backed) offer first-class exactly-once workflow semantics via event-sourced history. Not deeply covered in the research. May be a better fit than raw Postgres queuing for long-running multi-step agent workflows. *Weakly sourced — flagged.* [Finding 00]
- **Cross-agent message schema / protocol:** No strong consensus standard for the message envelope in agentic systems. MCP, A2A, and custom JSON schemas all appear in production. The A2A protocol formalizes task lifecycle states (`SUBMITTED → WORKING → COMPLETED/FAILED`) and does not define a standard DLQ concept — failed tasks reach `FAILED` state and stay in the task store. DLQ semantics must be built on top by the agent runtime. [Findings 00, 01]
- **Presence directory cache TTL:** No concrete recommendation for TTL values in agent-mesh contexts was found in any source. Callers cache Agent Cards; the lag between an overloaded agent updating its card and callers seeing the update is a real gap. [Finding 01]

---

## Part B: Making Watchdog, Observability, and Self-Healing Production-Ready

### B.1 Failure Mode Taxonomy (Type First, Then Instrument)

A single generic "agent is unhealthy" alert is not actionable. Production systems must instrument typed failure events. Galileo's 2025 production survey identifies seven categories:

| Failure Mode | Primary Signal |
|---|---|
| Specification gap | Increasing error rate on novel inputs |
| Reasoning loop / hallucination cascade | Identical tool calls with unchanged state |
| Context / memory corruption | Semantic drift between sessions |
| Multi-agent communication breakdown | Unparseable or missing fields at handoffs |
| Tool misuse / scope violation | Tool calls outside declared permission set |
| Prompt injection | Anomalous instruction origins in trace |
| Verification / termination failure | Step count exceeds budget; no terminal state reached |

**Source:** [7 AI Agent Failure Modes — Galileo](https://galileo.ai/blog/agent-failure-modes-guide)

### B.2 Watchdog Design: Four Required Components

#### B.2.1 Circuit Breaker (primary pattern)
The circuit breaker is the dominant production watchdog for LLM-based agents. Unlike classical microservice circuit breakers, AI agent breakers must handle *quality degradation* and *non-deterministic* outputs.

**Three-state machine:**
```
CLOSED ──(threshold breached)──► OPEN ──(backoff expires)──► HALF-OPEN
  ▲                                                               │
  └──────────────────(probe succeeds)────────────────────────────┘
                           │
                    (probe fails)──► OPEN (reset backoff)
```

**AI-specific thresholds (beyond HTTP error rates):**
- Error rate > 50% over a 100-request window
- Response latency > 30 seconds = failure tier
- Token consumption > 80% of quota within the window
- Semantic validation failures (output doesn't match contract schema)
- Hallucination-detection signals from evaluator models

**Critical constraint:** Circuit breaker enforcement must live *outside* the agent's own code — in the governance/orchestration plane. If it runs inside the agent, prompt injection can bypass it.

**Distributed state:** Circuit breaker failure counters must be synchronized across all scheduler replicas (e.g., Redis-backed: `cordum:cb:safety:failures`). Per-replica in-memory fallback activates if Redis is unavailable. [Finding 03]

**Production defaults (Cordum production implementation):**
- Trip: 3 consecutive failures → OPEN
- Open window: 30 seconds
- Half-open probes: 3 attempts
- Close condition: 2 successful probes
- Safety timeout: 2s gRPC + 3s defense wrapper

**Fail-mode governance:** choose *fail-closed* (reject with exponential backoff; appropriate for irreversible actions) vs. *fail-open* (allow through with `safety_bypassed=true` audit tag; appropriate for staging). This is a policy decision that must be made per action tier, not per system.

**Sources:** [Circuit Breaker Patterns for AI Agent Reliability — Brandon Lincoln Hendricks](https://brandonlincolnhendricks.com/research/circuit-breaker-patterns-ai-agent-reliability); [AI Agent Circuit Breaker Pattern — Cordum](https://cordum.io/blog/ai-agent-circuit-breaker-pattern)

#### B.2.2 Runaway-Loop Watchdog
Targets infinite-loop pathologies separately from the circuit breaker:
- Terminate on N consecutive *identical* tool calls with no state change (N = 2–3).
- Hard cap on total steps per task (e.g., 50 steps).
- Track *cost velocity* independently of cumulative spend: e.g., $50/hour triggers a stop even if the total session budget is not exhausted. Catches fast loops before cumulative damage accumulates.

#### B.2.3 Scope Violation Watchdog
A real-time permission enforcer, not an after-the-fact audit:
- Each agent action checked against a declared permission set before execution.
- Any call outside scope triggers immediate stop + durable audit record capturing: trigger reason, action at termination, total steps, cumulative cost, full execution trace.

#### B.2.4 Multi-Agent Communication Monitor
- Enforce shared JSON schemas with strict validation at message boundaries.
- Detect "semantic contract violations" — structurally valid messages that violate behavioral contracts.
- A centralized control plane (not peer-to-peer trust) is recommended to avoid cascading misinterpretations.

**Source:** [Partnership on AI — Real-Time Failure Detection in AI Agents (PDF, Sept 2025)](https://partnershiponai.org/wp-content/uploads/2025/09/agents-real-time-failure-detection.pdf)

### B.3 Heartbeat and Liveness Protocols

**Classical heartbeat pattern:**
- Each agent periodically sends a "I'm alive" signal (push model).
- Monitor timeout = k × heartbeat_interval (k = 2–3) to tolerate transient network latency.
- If no heartbeat within the timeout, the monitor declares failure and triggers recovery.

**Push vs. passive:** Push is preferred for latency-sensitive production systems. Passive inference (agent is actively completing tasks → implicitly alive) is a useful supplement.

**Two complementary probe types (Kubernetes-style):**
- **Liveness probe:** Is the process alive? Failure → restart.
- **Readiness probe:** Is the agent ready to accept new tasks? Failure → remove from task queue without restarting. For LLM agents, readiness must include a model-connectivity check.

**Session-level heartbeat for long-running agents:**
- Agent emits a progress heartbeat at configurable intervals (every 60–300 seconds): current step, estimated completion, resource consumption.
- Orchestrator resets a per-agent watchdog timer on each heartbeat.
- Missing heartbeat → declare stalled → trigger recovery.
- **Key parameter:** Heartbeat interval must be shorter than the task's expected longest single-step duration to avoid false positives during legitimate long tool calls.

**Sources:** [HeartBeat Pattern — Martin Fowler / Unmesh Joshi](https://martinfowler.com/articles/patterns-of-distributed-systems/heartbeat.html); [Heartbeats in Distributed Systems — Arpit Bhayani](https://arpitbhayani.me/blogs/heartbeats-in-distributed-systems/)

### B.4 Human Escalation: Decision Logic

#### B.4.1 Risk Tiers (Not Confidence Scores Alone)
Model confidence is systematically miscalibrated. A claimed 90% confidence often corresponds to ~75% actual accuracy; across a three-step chain: `0.75³ ≈ 42%` actual reliability (not the claimed `0.90³ = 73%`). Escalation policy must be built on *action type and consequence*, not model-reported confidence scores. [Finding 02]

**Four-tier action-risk classification:**

| Tier | Action Type | Examples | Oversight Mode |
|---|---|---|---|
| 1 | Read-only / internal | Queries, analysis, draft generation | Fully autonomous |
| 2 | Reversible / internal | CRM updates, ticket routing, KB edits | Autonomous + logging |
| 3 | External / third-party facing | Sending messages, publishing content | Async approval queue |
| 4 | Irreversible / high-consequence | Deploys, payments, deletions, security changes | Mandatory sync human approval |

**Critical:** Tier classification must be enforced at the workflow execution layer, not decided by the AI at runtime.

#### B.4.2 Escalation Trigger Matrix

| Trigger | Handoff Mode | Context to Include |
|---|---|---|
| Confidence below threshold (e.g., < 0.72) | Async review | Query + alternatives considered |
| Tier 3 action detected | Async queue | Proposed change diff |
| Tier 4 action detected | Mandatory sync block | Plain-language description + impact |
| Circuit breaker tripped | Async + incident flag | Last N steps + error trace |
| Injection suspected | Sync block + security escalation | Raw input + session ID |
| SLA breach imminent | Sync with priority flag | SLA clock + blocker reason |
| Retry budget exhausted | Async review | Full attempt log |
| Scope violation attempted | Immediate stop + async alert | Attempted action + permission set |

#### B.4.3 Async-First Approval Infrastructure
Synchronous approval blocks fail in production (AWS API Gateway default timeout 29 s; OAuth token expiry; pagination cursors going stale). Field data: 66% of production agents tolerate > 1-minute latency on approvals. Required elements:
- **Idempotency keys** — ensure exactly-once execution when the agent resumes
- **Action hashing** — detect if the environment changed since the approval request was issued (stale approvals can authorize the wrong action)
- **TTL on approval requests** — 7-day standard; shorter for time-sensitive operations

**Human context package required fields:** plain-language action description, agent's reasoning, financial/resource impact, reversibility flag, alternatives the agent considered, before/after diff, session ID, approval deadline.

**Warning:** Confirmation fatigue is a security vulnerability. Over-gating trains reviewers to approve reflexively. Reserve synchronous interruption for Tier 4 only.

**Sources:** [Human-in-the-Loop Escalation Design 2026 — Digital Applied](https://www.digitalapplied.com/blog/human-in-the-loop-escalation-design-ai-agents-2026); [How to Build HITL Oversight — Galileo](https://galileo.ai/blog/human-in-the-loop-agent-oversight)

### B.5 Automatic Recovery: Ordered Sequence Before Human Escalation

When the watchdog fires, attempt recovery in this order before escalating:

1. **Retry with backoff** — transient network / rate-limit errors. Cap at 3 attempts with exponential backoff.
2. **Model downgrade** — if primary model is degraded, fall back to a smaller/faster tier (e.g., Opus → Sonnet → Haiku). Communicate reduced capability explicitly in the response.
3. **Cached response** — serve the last valid cached result for idempotent read operations.
4. **Rule-based fallback** — deterministic logic for narrow, well-defined subtasks.
5. **Task re-queue on a healthy agent** — isolate the failed agent instance; re-assign the task from the last known good checkpoint.
6. **Escalate to human** — only after the above options are exhausted, or immediately for Tier 4 actions.

**Checkpoint design:** persist checkpoints at semantically meaningful boundaries (end of each subtask, not every N steps) to minimize re-work on recovery.

### B.6 Self-Healing Architecture: Five Pattern Families

#### B.6.1 Supervisor Trees (Erlang/OTP Applied to Agents)

Three restart strategies:

| Strategy | When to use |
|---|---|
| **One-for-One** | Children are independent (stateless executor pools) |
| **One-for-All** | Children must stay in sync (vector store + session store + cache) |
| **Rest-for-One** | Children have ordered dependencies (auth/token manager before request queue) |

**Flood control:** Supervisors implement a `{MaxRestarts, PeriodSeconds}` limit. If a child exceeds the threshold, the supervisor terminates and propagates failure upward, preventing infinite restart loops.

**AI-specific complications:**
- **Silent semantic failure:** process alive and LLM responding, but output is nonsensical. Requires output quality validation, not just liveness pings.
- **Stuck generation:** process awaiting a streaming response that will never complete. Requires wall-clock timeouts on stream progress.
- **Idempotency requirement:** if an agent crashes after making an external write, naive restart repeats the action. Tool calls must be idempotent, or the agent must maintain an acknowledged-call log.

**Source:** [Supervisor Trees for AI Agent Systems — Zylos Research](https://zylos.ai/research/2026-03-16-supervisor-trees-fault-tolerance-ai-agent-systems)

#### B.6.2 Checkpointing and Durable Workflows

**Three checkpointing strategies:**
- **Safe-point checkpointing:** persist full agent state after each atomic step (each tool call completion, each LLM response). LangGraph v1.2 does this by serializing the entire graph state to PostgreSQL or Redis after every graph step.
- **Event sourcing:** store state transitions (the conversation log is naturally an event log). *Approximate reconstruction only* because LLM outputs are non-deterministic — acceptable for most incident recovery.
- **Snapshot + tail replay:** full checkpoint every N events, then replay only events after the last checkpoint. For agents with long task histories, every 50 events bounds recovery time.

**Durable workflows (exactly-once execution):** Convex, Temporal, Inngest, and others provide execution that decouples agent work from the originating HTTP request or process lifetime. Each step executes exactly once; transient failure does not leave status suspended. This may be a better fit than raw Postgres queuing for complex multi-step agent workflows — *flagged as weakly sourced relative to the core Postgres-native approach.*

**Source:** [Durable Workflows and Strong Guarantees — Convex](https://stack.convex.dev/durable-workflows-and-strong-guarantees)

#### B.6.3 Saga Pattern for Compensation Transactions

LLM agents interact with external systems via tool calls. When a multi-step workflow fails at step 4, steps 1–3 side-effects are already live. A simple ROLLBACK is not possible.

**Saga structure:** every forward action node must have a corresponding compensating node. On failure, execute compensations in reverse order.

```
T₁ → T₂ → T₃ → T₄ [FAILS]
                ↓
         C₃ → C₂ → C₁   (compensations run in reverse)
```

**SagaLLM** (arXiv 2503.11951) formalizes this as middleware with:
- Task Execution Agents (domain-specific, clean I/O interfaces)
- GlobalValidationAgent (central validator: syntax, semantic coherence, factual accuracy, constraint adherence, inter-agent dependency satisfaction)
- SagaCoordinatorAgent (orchestrates sequencing and compensation execution)
- Two-level recovery: operation-level and workflow-level

**Atomix** (arXiv 2602.14849) provides progress-aware transactional semantics at the individual tool-call layer: epoch tagging, deferred commit until frontier predicates confirm no conflicting work is pending, effect classification (bufferable / externalized reversible / irreversible), idempotency keys + durable dedup state. Overhead is microsecond-scale relative to tool latency.

**Key caveats:**
- Compensation can itself fail. Compensation progress must be recorded for resumption.
- Idempotency must be designed at tool-authoring time, not added later.
- Externalized effects (email sent, payment charged) are often not idempotent by default.

**Sources:** [SagaLLM — arXiv 2503.11951](https://arxiv.org/html/2503.11951v3); [Atomix — arXiv 2602.14849](https://arxiv.org/abs/2602.14849); [Compensating Transaction Pattern — Azure Architecture Center](https://learn.microsoft.com/en-us/azure/architecture/patterns/compensating-transaction)

#### B.6.4 Bulkhead Pattern (Resource Isolation)

- Separate resource pools per agent class (distinct API quotas, compute instances, rate limit buckets). A malfunctioning agent cannot consume shared LLM token quota.
- Each external API gets its own circuit breaker instance with isolated failure tracking.
- Container-level hard resource caps (CPU, memory) prevent a single agent exhausting host resources.
- Caching with adaptive TTLs creates read-path redundancy: downstream API failure degrades to cached data rather than propagating as an error.

#### B.6.5 Three-Store Memory Architecture

| Memory Type | Content | Storage | Use in Recovery |
|---|---|---|---|
| Episodic | Past incident timelines: symptoms, diagnosis, actions, outcomes | Vector DB (Pinecone, Weaviate, Qdrant) | Semantic similarity search for analogous incidents |
| Semantic | Environment facts: topology, config, dependency maps | Graph DB (Neo4j) + Vector DB | Contextual grounding for diagnosis |
| Procedural | Ranked remediation strategies with success rates | Relational DB (Postgres) | "Living playbooks" updated after every incident |

**Intelligent decay:** composite relevance score per entry (recency × retrieval frequency × utility). Prefer *consolidation* (merging similar episodic records into generalized patterns) over deletion.

**The six-node self-healing pipeline:**
```
Observe → Diagnose → Plan → Act → Validate → Save-to-Memory
```
Every incident makes the next one faster — but only if the post-incident write step is disciplined.

**Source:** [Building Memory for Self-Healing AI Agents — Medium](https://medium.com/@daryadi.foo/building-memory-for-self-healing-agents-7cabba799f77)

### B.7 Observability: OTel-Native Agent Fleet Instrumentation

#### B.7.1 Why Agent Fleets Differ

- **Reasoning loops:** a single user request may fan out to dozens of LLM calls and recursive sub-agent invocations.
- **Emergent failures:** reachability checks miss agents that are alive but producing wrong outputs.
- **Cost as a performance dimension:** token economics must be tracked beside latency and error rate.

A complex agent can generate 50 child spans per root trace. Billing primitives (per span vs. per trace vs. per GB) produce very different costs at fleet scale.

#### B.7.2 Trace Context Propagation Across Async Mailbox Hops

The W3C Trace Context standard (`traceparent`/`tracestate`) is the foundation. Key rules:

1. **Never rely on thread/coroutine context alone.** For async queues (Kafka, Redis Streams, in-process mailboxes), OTel context is NOT automatically forwarded — it must be serialized into message metadata at send time and deserialized at receive time.
2. **Preserve the trace ID, generate a fresh span ID** for each hop (parent → child link across the async gap).
3. **Use `SpanLink` for fan-out** when one message spawns N parallel agents — this avoids forcing a strict tree topology.
4. **Propagate `W3C Baggage`** for session-scoped identifiers (user ID, tenant ID) via `BaggageSpanProcessor`.

```
Producer (Agent A sends):
  ctx = otel.propagate.inject({}, carrier=message_metadata)
  queue.send(payload, headers=message_metadata)

Consumer (Agent B receives):
  ctx = otel.propagate.extract(carrier=message_metadata)
  with tracer.start_as_current_span("agent_b_handle", context=ctx):
      ...
```

**Source:** [AI Agent Observability — OpenTelemetry Blog](https://opentelemetry.io/blog/2025/ai-agent-observability/)

#### B.7.3 OTel GenAI Semantic Conventions (v1.41 — Development Status)

**Caveat:** These conventions are in **Development** status as of mid-2026. Attribute names prefixed with `gen_ai.` can change before GA without a major version bump. Build dashboards and alerts on the stable metric names; accept churn on newer agent-specific attributes. Enable `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental` for dual emission during the transition.

Four span operation types:
- `create_agent` (CLIENT) — creating an agent on a remote service
- `invoke_agent` (CLIENT/INTERNAL) — invoking an agent to perform a task
- `invoke_workflow` (INTERNAL) — executing a predefined workflow
- `execute_tool` (INTERNAL) — a single tool execution

**Mandatory baseline metrics:**
- `gen_ai.client.operation.duration` (Histogram, seconds) — end-to-end latency per GenAI operation; dimension by model and provider
- `gen_ai.client.token.usage` (Histogram, {token}) — token consumption per operation; split input vs. output

**Secondary signals:** tool execution duration, tool error rate, agent invocation count, queue depth/mailbox backlog, reasoning depth/loop count, backtracks/replans, cost per conversation (`(input_tokens × input_price) + (output_tokens × output_price)`).

**Content capture is opt-in and off by default.** For PII-sensitive deployments, store content externally and reference via URL on the span.

**Source:** [OTel GenAI Semantic Conventions — Greptime](https://greptime.com/blogs/2026-05-09-opentelemetry-genai-semantic-conventions)

#### B.7.4 Governance-Aware Telemetry

A 2026 preprint (arXiv 2604.05119) proposes emitting governance-specific spans alongside performance spans: policy decisions, permission checks, and override events. These feed a closed-loop enforcement layer that can halt or redirect agents in real time based on telemetry — directly relevant to a governed agent OS design.

**Source:** [Governance-Aware Agent Telemetry — arXiv 2604.05119](https://arxiv.org/pdf/2604.05119)

#### B.7.5 Observability Platform Recommendations

| Priority | Recommendation | Notes |
|---|---|---|
| Data residency / sovereignty required | Self-host **Langfuse** (MIT license; Postgres + ClickHouse) | Acquired by ClickHouse Jan 2026; free to self-host |
| High-volume fleet, cost-sensitive | Self-hosted **Grafana LGTM** or **VictoriaMetrics stack** | Zero license cost; needs custom GenAI dashboards |
| Speed to production | Managed **LangSmith** or **Braintrust** | Trace-based billing becomes expensive at scale |
| Already on Datadog APM | Extend with **Datadog LLM Observability** | Closed ecosystem; premium pricing |
| Cost visibility without re-instrumentation | **Helicone** proxy | Single point of failure for whole fleet |

Auto-instrumentation via **OpenLLMetry** (Traceloop), **OpenLIT**, or **OpenInference** (Arize) covers 40+ frameworks with minimal code change and emits OTel-compliant spans to any OTLP-compatible backend.

**Important caveat:** Only ~15% of GenAI deployments instrument observability as of early 2026. Most vendor claims about production scale are based on small samples.

**Source:** [Best AI Observability Tools 2026 — Arize](https://arize.com/blog/best-ai-observability-tools-for-autonomous-agents-in-2026/)

### B.8 Regulatory Dimension for Self-Healing Design

EU AI Act Article 14 (effective August 2, 2026) mandates "human-machine interface tools" for high-risk AI systems — essentially codifying escalation design, override mechanisms, and open-loop architecture as legal requirements for agents in regulated domains. This is not only a design best practice but an enforceable compliance obligation.

---

## Part C: Extending and Publishing the Platform

### C.1 Multi-Tenant Isolation and Security for Untrusted Agent Code

#### C.1.1 The Non-Negotiable Ten

Before running untrusted or third-party agent code in production, the following controls are universally required:

1. **MicroVM or equivalent hardware isolation** (Firecracker, Kata Containers) for any code executing untrusted inputs. Standard containers share the host OS kernel; a single container escape exposes every tenant on the host.
2. **Default-deny egress NetworkPolicies** with FQDN-based allowlists (Calico or Cilium for FQDN egress rules). Agents with unrestricted egress can exfiltrate data, reach command-and-control infrastructure, or abuse third-party APIs.
3. **Per-tenant KEK envelope encryption** with crypto-shredding capability. Destroying a tenant's Key Encryption Key renders all their data cryptographically inaccessible without storage deletion.
4. **SPIFFE/SPIRE workload identity** for per-service secret scoping. Each service gets a SPIFFE x509 certificate; the secret store grants access by SPIFFE identity, not hostname. Service B on the same host as Service A cannot access Service A's secrets.
5. **Tenant context at the storage layer** (PostgreSQL Row-Level Security + storage key embedding). Application-layer tenant filtering alone is insufficient; a single missed WHERE clause creates cross-tenant data exposure.
6. **Authorization envelopes on every action** (action type + resource identifier + tenant context), resolved by backend code. The LLM must never be delegated the authorization decision.
7. **Short-lived credentials** with explicit expiration on all agent identities (IETF drafts for agent credentials require this).
8. **Segregated audit pipelines** with PII redaction before log storage. Agent logs are the highest PII leakage surface in the system.
9. **eBPF behavioral monitoring** (e.g., ARMO/Kubescape) after a 7–14 day baselining period. Catches policy violations that static NetworkPolicies and RBAC cannot — e.g., an agent that begins calling unexpected external APIs mid-task.
10. **mTLS on all internal service-to-service communication.** Described across multiple sources as "the most commonly skipped and most impactful security control."

**Source:** [AI Agent Sandboxing Enterprise Guide 2026 — BeyondScale](https://beyondscale.tech/blog/ai-agent-sandboxing-enterprise-security-guide); [Multi-tenant isolation for AI agents — Blaxel](https://blaxel.ai/blog/multi-tenant-isolation-ai-agents); [Secret management in multi-tenant environments — Pinterest Engineering](https://medium.com/pinterest-engineering/secret-management-in-multi-tenant-environments-debc9236a744)

#### C.1.2 Sandboxing Technology Comparison

| Technology | Isolation Level | Cold Start | Best For |
|---|---|---|---|
| **Firecracker microVM** | Hardware (separate kernel per VM) | ~125 ms | Untrusted/third-party code, regulated workloads |
| **gVisor (runsc)** | Syscall interception (user-space kernel) | Fast | Compute-heavy agents, moderate-risk code |
| **Standard containers** | Shared host kernel | Very fast | Trusted, reviewed code only |
| **V8 Isolates** | JS VM isolation | <1 ms | JS-only, latency-critical |

Firecracker boots a VM in ~125 ms and is resumable from standby in <25 ms. AWS Lambda uses it precisely because hardware-enforced memory isolation requires escaping both guest kernel and hypervisor to break out.

**Sources:** [How to sandbox AI agents 2026 — Northflank](https://northflank.com/blog/how-to-sandbox-ai-agents); [Firecracker vs gVisor — Northflank](https://northflank.com/blog/firecracker-vs-gvisor)

#### C.1.3 Five-Identity Model for Privilege Separation

Multi-tenant agent systems require five distinct identities that traditional applications collapse into one:

1. **Trigger identity** — the human who initiates the action
2. **Execution identity** — the OAuth credential making downstream API calls
3. **Authorization identity** — the principal whose delegated grant authorizes the action
4. **Tenant identity** — the organizational boundary
5. **Attribution identity** — the human recorded in downstream audit logs for compliance

Conflating these identities is one of the leading causes of privilege escalation in multi-agent systems.

OWASP LLM06:2025: *"Critical controls like privilege separation and authorization bounds checks must not be delegated to the LLM."*

**Source:** [Access Control for Multi-Tenant AI Agents — Scalekit](https://www.scalekit.com/blog/access-control-multi-tenant-ai-agents)

#### C.1.4 Three-Tier Storage Isolation Model

| Tier | Model | When |
|---|---|---|
| Pool | Shared tables + Row-Level Security | Low-risk SaaS, internal tools |
| Bridge | Schema-per-tenant on shared instances | B2B data, moderate compliance |
| Silo | Instance-per-tenant | HIPAA, FedRAMP, high-value tenants |

**Warning from production teams:** retrofitting tenant boundaries after deployment is expensive and often forces full re-architecture. Enforce boundaries from day one. Salesforce's platform (7,000+ concurrent sessions) enforces 24-hour TTL on session data; thread-safe access patterns prevent race conditions that could surface one tenant's context in another's session.

**Source:** [Building a Multi-Tenant AI Agent Platform — Salesforce Engineering](https://engineering.salesforce.com/building-a-multi-tenant-ai-agent-platform-handling-7k-sessions-without-cross-team-interference/)

### C.2 App Store Publication: Apple App Store

#### C.2.1 The Critical Hard Walls

**Guideline 2.5.2 (Code Execution)** is the most consequential restriction:
> "Apps should be self-contained in their bundles … may not download, install, or execute code which introduces or changes features or functionality of the app."

In practice:
- LLM inference weights must be bundled at submission; cannot be downloaded post-install.
- Model updates require a new binary submission through App Review.
- Generated code running inside an embedded web view violates 2.5.2.
- **Generated apps/previews must open in an external browser (Safari)**, not an in-app web view.
- Fine-tuning cannot produce executable artifacts loaded back into the runtime.

**2026 enforcement (high-profile cases):**
- March 2026: Apple blocked updates for **Replit** (required routing previews to Safari) and **Vibecode** (required removing ability to build/run apps targeting Apple platforms).
- **"Anything"** was pulled from the store entirely.
- A lawsuit challenging Guideline 2.5.2 is pending as of March 2026.

**Sources:** [Apple pushing back on vibe coding apps — 9to5Mac (March 2026)](https://9to5mac.com/2026/03/18/apple-pushing-back-on-vibe-coding-iphone-apps-developers-say/); [Apple pulls 'Anything' from App Store — MacRumors (March 2026)](https://www.macrumors.com/2026/03/30/apple-pulls-vibe-coding-app/)

#### C.2.2 On-Device vs. Cloud AI

| Approach | Entitlement | Data Disclosure |
|---|---|---|
| CoreML / Create ML | None for basic use | Exempt — on-device |
| Apple Foundation Models (iOS 26+, WWDC 2025) | "Foundation Models" entitlement required | Exempt — on-device |
| Foundation Models + LoRA adapters | Separate adapter entitlement; Account Holder must request from Apple | Exempt |
| Third-party cloud LLM (OpenAI, Anthropic, etc.) | None specific | Required — named-provider consent before first transmission |

**Source:** [Apple Foundation Models documentation](https://developer.apple.com/documentation/FoundationModels); [Foundation Models Adapter Entitlement](https://developer.apple.com/apple-intelligence/foundation-models-adapter/)

#### C.2.3 Privacy Requirements (Guideline 5.1.2(i), effective November 2025)

- **Named provider disclosure:** must name the specific provider (e.g., "Anthropic Claude") — generic "AI service providers" is rejected.
- **Purpose statement:** users must be told what the AI does with their data.
- **Data type enumeration:** prompts, logs, uploaded files, device metadata — all listed.
- **Explicit opt-in** before first transmission; cannot bundle into ToS acceptance.
- **Privacy Manifest** (`PrivacyInfo.xcprivacy`) mandatory for all submissions. Apple rejected **12% of App Store submissions in Q1 2025** for Privacy Manifest violations.

**Source:** [Apple Updated App Review Guidelines — November 2025](https://developer.apple.com/news/?id=d75yllv4); [Guideline 5.1.2(i) explained — DEV Community](https://dev.to/arshtechpro/apples-guideline-512i-the-ai-data-sharing-rule-that-will-impact-every-ios-developer-1b0p/)

#### C.2.4 iOS Sandbox Constraints for Developer Tools

- File system access outside the app container requires iCloud Drive or system file picker.
- **Shell command execution is not possible on iOS** (no POSIX fork/exec). A developer tool that relies on running shell commands cannot ship on iOS as-is.
- IPC between apps is limited to URL schemes, App Extensions, or Share Sheets.

#### C.2.5 Apple Review Pitfall Summary

| Pitfall | Guideline | Consequence |
|---|---|---|
| Generated code previewed in in-app web view | 2.5.2 | Update blocked |
| LLM weights downloaded post-install | 2.5.2 | Immediate rejection |
| Generic naming of "AI service providers" | 5.1.2(i) | Rejection; re-review required |
| Privacy Manifest missing SDK declarations | Privacy Manifest | 12% Q1 2025 rejection rate |
| Foundation Models adapters without entitlement | Entitlements policy | Runtime crash; App Review failure |
| Attempting shell execution on iOS | Sandbox / 2.5.2 | App inoperable |

### C.3 App Store Publication: Google Play

#### C.3.1 The Critical Hard Wall

**AccessibilityService API prohibition (enforced January 28, 2026):**
> "Any use of the Accessibility API that enables an app to autonomously initiate, plan, and execute actions or decisions is strictly prohibited."

This prohibits computer-use-style agents that control Android device UI without explicit user action per step. Deterministic, rule-based automation (human-defined scripts) remains allowed.

**February 2026 tightening:** Google also restricted AccessibilityService in Android's Advanced Protection Mode — reducing device compatibility for apps depending on it.

**Source:** [Google Play: Use of AccessibilityService API](https://support.google.com/googleplay/android-developer/answer/10964491?hl=en); [Google Play Policy October 30, 2025](https://support.google.com/googleplay/android-developer/answer/16550159?hl=en)

#### C.3.2 AI-Generated Content Policy

Prohibited generated outputs include: malicious code (exploits, malware, ransomware scripts), non-consensual sexual deepfakes, voice/video impersonations for fraud, and deceptive election content. Developer liability explicitly extends to "any output produced by the model, including content created by users" — injected prompts that trigger prohibited output are the developer's responsibility to prevent.

**Required:** proactive (not reactive) moderation. Cannot rely solely on user reports.

#### C.3.3 Privacy and Data Safety

- Complete the Data Safety form for 14 data categories (what is collected, shared, why, security, user deletion rights).
- Third-party SDK data collection must be reflected in the form.
- GDPR: if data practices vary by country, document in the "About This App" section.

#### C.3.4 Google Play Pitfall Summary

| Pitfall | Consequence |
|---|---|
| AccessibilityService for autonomous AI actions | Rejection; account termination risk |
| AI-generated content not labeled in listing | Rejection |
| Third-party SDK data not in Data Safety form | Rejection / post-publish enforcement |
| Proactive moderation absent | Rejection or removal |
| No privacy policy URL | Cannot complete Data Safety form; blocked |

**Enforcement scale:** Google blocked 1.75 million apps in 2025 using AI-assisted review. Developer account termination is notoriously difficult to appeal.

**Source:** [Google AI blocked 1.75M harmful apps — TechRepublic](https://www.techrepublic.com/article/news-google-ai-blocked-1-75-million-apps-2025/)

### C.4 Policy Gaps in Current Store Policies

Both stores have significant undefined areas specifically relevant to governed multi-agent systems:

1. **Agent-to-agent communication:** Neither store defines rules for apps orchestrating multiple AI agents calling each other.
2. **Long-running background agents:** iOS background execution is severely limited (BGTask, push-triggered); Android has battery optimization restrictions. Neither store has specific policy for agentic tasks running autonomously for minutes to hours.
3. **Tool use / function calling:** No explicit policy governs LLM-driven agents calling device APIs (calendar, camera, files). Currently falls under existing API permission policies.
4. **Model provenance:** Neither store requires disclosure of which model version or training data was used.
5. **Agent output attribution:** No requirement yet for labeling which outputs were AI-generated vs. human-authored within a developer tool.

### C.5 Web Platform: Compliance and Infrastructure Hardening

#### C.5.1 EU AI Act Obligations

The EU AI Act does not create a separate category for "agentic AI" — it classifies systems by the tasks they perform. Agents influencing credit decisions, employment screening, fraud detection, or critical infrastructure control are **high-risk** under Annex III.

**Already in force (since February 2025):**
- **Article 4 (AI Literacy):** documented AI policy + internal training required.
- **Article 5 (Prohibited Practices):** social scoring, covert manipulation, workplace emotion recognition fully prohibited.

**GPAI obligations (since August 2025):** if the platform exposes or builds upon a general-purpose AI model:
- Technical documentation (training data, test process, energy consumption, intended uses); retain 10 years.
- Public summary of training data using mandatory EU templates.
- Respond to downstream provider information requests within 14 days.
- Designated copyright personnel; respect robots.txt; rightsholder complaint mechanism.

**Full enforcement: August 2, 2026.** From that date:
- **Article 50:** conversational AI must disclose to users they are interacting with an AI.
- High-risk obligations enforceable: technical documentation, open-loop architecture, human oversight (structured intervention points), and control to stop/correct/override agents at runtime.

**Extraterritorial scope:** Article 2(1)(c) — US SaaS companies serving EU customers are in scope.

**Penalties:** up to €35M or 7% of global revenue for prohibited practices; €15M or 3% for high-risk or GPAI violations.

**Sources:** [EU AI Act Compliance 2026 — Covasant](https://www.covasant.com/blogs/eu-ai-act-compliance-autonomous-agents-enterprise-2026); [EU AI Act GPAI obligations — Latham & Watkins](https://www.lw.com/en/insights/eu-ai-act-gpai-model-obligations-in-force-and-final-gpai-code-of-practice-in-place); [EU AI Act 2026 for US Companies — Tredence](https://www.tredence.com/blog/eu-ai-act-compliance-guide-us-companies)

**Caveat:** No search returned a definitive official EU AI Office checklist specifically for autonomous agent platforms (as distinct from AI systems generally). The compliance guidance sources are industry commentary, not official EU guidance.

#### C.5.2 GDPR and Data Processing Agreements

Under GDPR Article 28, a DPA is mandatory before any personal data is processed. The platform typically acts as **processor**; each enterprise customer is the **controller**. Failure to have a required DPA: fines up to €10M or 2% of global annual turnover.

**Mandatory DPA content:** subject matter / nature / purpose / duration; categories of data; processing only on documented instructions; confidentiality; Article 32 security measures; sub-processor governance; data subject rights assistance; audit rights; deletion/return at contract end.

**AI-specific GDPR concerns:**
- **Article 22:** automated individual decision-making requires transparency, opt-out rights, and human review options when agents make decisions with legal or significant effects.
- **Article 35:** DPIA required before processing likely to result in high risk (large-scale profiling, systematic monitoring).
- **CNIL guidance (2024):** document legal basis for fine-tuning on personal data; do not repurpose user data for model training without explicit consent.

**International data transfers:** all EU personal data transfers to the US require 2021 Standard Contractual Clauses (Module 2 for controller-to-processor). The transition deadline expired February 2, 2026 — any transfers using old SCCs are non-compliant. Do not rely on the EU-US Data Privacy Framework alone; a "Schrems III" CJEU challenge has been signalled.

**Sources:** [GDPR Compliance for SaaS 2026 — Feroot Security](https://www.feroot.com/blog/gdpr-saas-compliance-2025/); [SaaS DPA Guide — Secure Privacy](https://secureprivacy.ai/blog/data-processing-agreements-dpas-for-saas)

#### C.5.3 SOC 2 Type II

Enterprise buyers routinely require SOC 2 Type II before signing. Type I is a point-in-time snapshot; Type II requires 3–12 months of observation.

**AI-specific controls auditors examine:**
- Code execution isolation (microVM architecture required; containers alone are insufficient)
- Training data integrity (sanitization; data-poisoning defense)
- Model drift detection (continuous monitoring; documented re-evaluation cadence)
- Autonomous decision auditing (persistent audit logs; explainable-AI mechanisms)
- Access control (context-aware runtime policies, not just static RBAC)
- Output filtering (detecting data leakage in agent outputs)

**Timeline and cost estimates (2025–2026):**
- Compliance platform + certified auditor: 4–12 months; $25,000–$80,000
- Custom build: 6–18 months; $35,000–$250,000+

Type II cannot be shortcut for enterprise sales — the observation period is mandatory.

**Sources:** [SOC 2 for AI Companies — Comp AI](https://www.trycomp.ai/hub/soc-2-for-ai-companies); [SOC 2 Compliance for AI Agents 2026 — Blaxel](https://blaxel.ai/blog/soc-2-compliance-ai-guide)

#### C.5.4 Acceptable-Use Policy Technical Enforcement

AUP enforcement must be at the **infrastructure layer**, not just application layer.

**Required in Terms of Service:**
- Prohibition on AI agents that do not identify themselves in HTTP User-Agent headers when accessing third-party platforms (see *Amazon v. Perplexity AI*)
- Prohibition on generating malicious code, circumventing safety mitigations, inputting personal data of third parties without consent, using outputs in automated decisions without human review

**Technical enforcement architecture:**
- Prompt-level analysis identifying what data is exposed in every agent interaction
- Real-time inspection of prompts, retrieved context, model outputs, and tool calls before actions are taken
- Block or flag at the tool-execution layer
- Progressive deployment: monitor-only → soft enforcement → full enforcement with automated remediation
- Performance targets: simple policy evaluation < 10 ms; complex multi-condition policies < 50 ms added latency

**Source:** [Technical Deep Dive: Policy-Based AI Agent Governance — Airia](https://airia.com/agent-constraints-a-technical-deep-dive-into-policy-based-ai-agent-governance/)

#### C.5.5 SaaS Infrastructure Hardening Pre-Launch Checklist

| Domain | Minimum before public launch |
|---|---|
| Identity and access | MFA (TOTP + WebAuthn) for all users; least-privilege IAM; regular access reviews; context-aware runtime policies for agents |
| Network and compute | VPC segmentation; production/staging/development isolation; database not reachable from public internet; WAF (OWASP rule set); DDoS mitigation; microVM sandboxes for code execution |
| Secrets | All secrets in secrets manager (Vault, AWS Secrets Manager, GCP Secret Manager); zero secrets in version control; pre-commit hooks + CI scanning; IaC scanning before every deploy |
| Data protection | TLS 1.3 in transit; AES-256 at rest; column-level encryption for PII; automated data retention deletion pipelines; per-tenant storage isolation where warranted |
| Application security | SCA + SAST in CI/CD; container image scanning; SBOM maintained; external pentest before public launch; threat modelling for all new agent capabilities |
| Logging and incident response | Centralized tamper-evident audit logs; SIEM with anomaly alerts; IR plan tested with tabletop exercises; 72-hour GDPR breach notification; log retention 90 days accessible + 1 year cold storage |
| AI-specific | Output filtering (data leakage detection); prompt injection defenses at application and infrastructure layers; model drift monitoring; human-in-the-loop escalation paths for Tier 4 actions |

**Caveat:** 67% of SaaS security incidents in 2025 were traced to misconfigurations. IaC scanning before every deploy is not optional.

**Sources:** [SaaS Security Checklist 2026 — Technology.org](https://www.technology.org/2026/03/18/saas-security-checklist-before-launch-2026-guide/); [SaaS Security Checklist — Instinctools](https://www.instinctools.com/blog/saas-security-checklist/)

### C.6 Pre-Launch Compliance Gate: Combined Timeline

| Domain | Minimum before public launch | Estimated timeline |
|---|---|---|
| EU AI Act | AI literacy training, risk classification documented, prohibited-practice audit, Article 50 disclosure implemented | 1–2 months |
| GDPR DPA | Template DPA drafted, sub-processor list published, DPIA for high-risk processing | 1–2 months |
| International data transfers | 2021 SCCs signed with all US-based processors | 2–4 weeks |
| SOC 2 | Type I achievable; Type II requires 4–12 months observation | 4–12 months |
| AUP enforcement | Prompt-level enforcement layer live; rate limiting; kill-switch; agent identity headers | 2–3 months |
| Infrastructure hardening | MFA, secrets management, VPC isolation, WAF, encryption, external pentest | 2–3 months |
| GPAI compliance (if applicable) | GPAI Code of Practice signed; training data documentation; copyright policy | 1–3 months |

---

## Aggregate Gap Analysis

The following gaps are either unresolved in the research findings, weakly sourced, or genuinely open questions in the industry:

| Gap | Status | Implication |
|---|---|---|
| PGMQ native DLQ | Not found in documentation; may require application-layer implementation | Verify against current PGMQ changelog before committing |
| Temporal / durable workflow engines as Postgres alternatives | Mentioned once; not deeply evaluated | Worth a separate evaluation spike if workflow complexity grows |
| Cross-agent message schema / envelope standard | No consensus; MCP, A2A, and custom JSON all appear in production | A2A task lifecycle states are the closest published standard |
| Presence directory cache TTL recommendations | No concrete values found in any source | Requires empirical tuning; start with a short TTL (30–60 s) and adjust |
| EU AI official checklist for agent platforms | Not found; guidance is from industry commentators, not EU AI Office | Monitor EU AI Office publications; consult legal counsel for high-stakes classification |
| EU-US DPF long-term legal status | "Schrems III" challenge signalled; not yet filed as of early 2026 | SCCs must be in place regardless |
| App store policy for long-running background agents | Not defined by either Apple or Google | Design for graceful degradation; use push notifications and BGTask on iOS |
| App store policy for agent-to-agent orchestration | Not defined | Document orchestration architecture proactively for App Review |
| Contradictory: "durable workflow vs. raw Postgres" | Convex/Temporal claim exactly-once execution; Postgres experts note inherent at-least-once reality | Use the Postgres approach for the control plane; evaluate Temporal/Convex for complex multi-step saga orchestration specifically |

---

## Recommended Stack Summary

| Concern | Pattern | Mechanism |
|---|---|---|
| Durable task dispatch | Transactional outbox | `FOR UPDATE SKIP LOCKED` + outbox table |
| Lightweight in-process queuing | Visibility-timeout queue | PGMQ or pg-boss |
| Intra-domain orchestration | Central orchestrator row per workflow | pg-boss job dependency graph |
| Cross-domain choreography | Event rows + relay | Outbox → downstream queue |
| Exactly-once processing | Idempotent consumer + inbox dedup | `processed_messages` table with unique constraint |
| Message ordering (per entity) | Per-key partitioning | PGMQ FIFO group keys or shard-by-agent-id |
| Race condition prevention | Optimistic locking | Version column + `UPDATE … WHERE version = $v` |
| Control plane enforcement | Deterministic policy engine | Postgres stored logic + advisory locks |
| Wake-up signal | LISTEN/NOTIFY | Paired with durable polling only |
| Backpressure | Bounded queues + AIMD + circuit breaker | pg-boss concurrency + Redis-backed circuit breaker state |
| DLQ | Application-layer DLQ on pg-boss | `status: dead_letter` task store + enriched metadata |
| Failure detection | Circuit breaker + heartbeat + scope watchdog | Governance plane (outside agent code) |
| Human escalation | Tier-based async-first approval | Idempotency keys + action hashing + TTL |
| Self-healing | Supervisor trees + saga + checkpoint | LangGraph v1.2 or Temporal for complex sagas |
| Sandboxing | MicroVM isolation | Firecracker or Kata Containers |
| Secret management | Per-service cryptographic identity | SPIFFE/SPIRE |
| Network egress | Default-deny + FQDN allowlist | Calico/Cilium NetworkPolicies |
| Observability | OTel-native with GenAI semconv | OpenLLMetry + Langfuse (self-hosted) |
| Compliance | DPA + SOC 2 Type II + EU AI Act | 4–12 month SOC 2 observation window |
| App Store (Apple) | No in-app code execution; cloud AI with named-provider consent | External browser for all generated output |
| App Store (Google) | No AccessibilityService for autonomous actions; proactive moderation | Data Safety form + content output filtering |

---

*Synthesised from findings/00–07. Research date: 2026-06-25. All source URLs are from the original findings documents; no URLs have been invented or inferred. Claims without a finding citation are flagged explicitly.*
