# Agent-OS → Multi-Tenant Enterprise SaaS — Master Product Blueprint

*Productizing the governed autonomous AI software factory into a multi-tenant enterprise SaaS where a customer acts as "CEO," gives high-level direction to an orchestrator, and the platform ships their apps/businesses/websites with minimal human intervention.*

**Status:** Design blueprint (2026-06). Grounded in (a) the existing agent-os architecture — Postgres+pgvector control plane, `factory.py` SPEC→BUILD→QA→REVIEW→LAUNCH, `tasks` + `FOR UPDATE SKIP LOCKED` work queue, `directory.py`/`orchestrate.py` brokered comms, `dashboard.py` mission control, `billing.py` SaaS layer, `frontdoor.py` self-serve, per-tenant vault BYO-keys — and (b) 2025–2026 research on real platforms (cited in companion files).

**Companion research files (full citations):**
- `identity-multitenancy-blueprint.md` — Area 1 deep dive (~80 sources)
- `agent-fleet-ux-research.md` — Area 2 deep dive (~90 sources)
- Area 3 (minimal-decision model) sources are inline in §3 below.

---

## 0. Architectural through-line

Three design commitments tie all three areas together and align with what agent-os already is:

1. **Tenant is the spine.** Every agent run, trace, conversation, task, cost record, and decision is stamped with `organization_id`. This is the same `tenant_id` the existing `billing.py`/`frontdoor.py` already thread through — we formalize and harden it (RLS, audit, isolation tests).
2. **The orchestrator is a product surface, not just a process.** The existing `orchestrate.py` (request_collaborator / hire_requests / controller-spawns) and `directory.py` (presence/roster/contact) become the *backend* of a live, CEO-facing org-map + chat + approval-inbox UX.
3. **Minimize human decisions structurally, not cosmetically.** The win is *not asking* (standing policies + a risk classifier), then making the residual asks frictionless decision cards. This maps directly onto the existing `appguard.py` (circuit breakers), `risk.py`/`osq.decisions()` (decision queue), and `responder.py` (auto-fix-before-paging) — they already embody "act when safe, escalate only judgment calls."

---

# Area 1 — Identity & Multi-Tenancy

## 1.1 Recommended stack (opinionated)

| Layer | Recommendation | Why |
|---|---|---|
| **Auth/SSO** | Managed provider exposing **SAML + OIDC + SCIM behind one integration**, **per-connection** pricing — **WorkOS AuthKit** (free to 1M MAU) or **Clerk** (SSO+SCIM in base paid tier). | Per-MAU pricing (Auth0/Supabase) spikes exactly when you land a big-seat enterprise. Building SSO in-house ≈ $110K–$1.1M over 3 yrs. |
| **Top-level tenant** | **`Organization`** (unit of contract + billing). | Beats "Account"/"Team"/"Workspace" naming; matches Vercel/Supabase/GitHub. The existing `tenants` table *is* this — rename conceptually to `organizations`. |
| **Identity model** | **`User` ↔ `Membership` ↔ `Organization`** join (one account → many orgs, Linear model). | Never a direct user→org FK. Lets one human "CEO" run multiple factories/orgs. |
| **Authorization** | **RBAC now** (owner/admin/member/billing/viewer) → add **ReBAC (OpenFGA or SpiceDB)** for resource-level sharing at scale → layer **ABAC** for context. | Real platforms keep 3–6 fixed roles + isolate billing as a seat-free role. |
| **Data isolation** | **Start pooled: one Postgres + RLS + `organization_id` on every table.** Documented graduation path to schema-per-tenant (Citus 12) and DB-per-tenant for regulated/large tenants. | Pool scales to 100k–millions of tenants, one-migration-all-tenants, trivial cross-tenant analytics. Matches the existing single-Postgres design and the "1:1 cloud-portability" goal. DB-per-tenant dies on `max_connections` long before storage. |
| **Audit/session** | Hash-chained append-only audit log → WORM store; short-lived JWT access + opaque rotating refresh tokens; passkeys/WebAuthn MFA; envelope encryption (KMS/Vault) for BYO-keys. | SOC2 + enterprise-deal table stakes. The existing `redact.py` + vault BYO-key + audit stream are the seeds. |

## 1.2 Auth & SSO — concrete design

**Signup/login.** Email-first (not usernames), minimal fields, progressive profiling. Offer email/password **and** social (Google = consumer default, GitHub = developer audience, Microsoft/Entra = business + bridge to corporate SSO) side-by-side — never social-only. Passwordless (magic link / passkey) reduces friction (~10% of users hit password reset monthly, ~75% abandon it). Always **Authorization Code + PKCE**; don't hand-roll OAuth.

**Enterprise SSO.** Support **SAML 2.0 and OIDC behind one integration** (SAML = broadest IdP coverage incl. ADFS/Shibboleth/on-prem Ping; OIDC = mobile/SPA-native). Implement **Home Realm Discovery by email domain** — register each enterprise's domain against its connection so login auto-routes to the right IdP. Support both SP-initiated and IdP-initiated. SAML baseline: require signed responses *and* assertions, SHA-2 certs, reject self-signed, enforce assertion-lifetime/replay checks.

**SCIM provisioning.** SSO authenticates at login; SCIM (RFC 7643/7644) handles create/update/**disable** after the account exists. Deprovisioning is the security-critical half — every account left active after offboarding is a backdoor.

**Enterprise gates (plan for them early).** SAML lands as "item one" on the security questionnaire at **~$30–50K ACV**; SCIM becomes mandatory around **~1,000-seat** customers. Build the org-first data model from day one. **Do not paywall SSO behind a top "Enterprise" tier alone** — include it mid-tier to avoid the "SSO tax" reputational/regulatory liability (CISA frames paywalled SSO as a security anti-pattern).

## 1.3 Tenancy hierarchy

```
Organization            (top tenant — billing, contract, SSO config, audit retention, plan/quota)
  └── Workspace          (optional grouping — e.g., a product line or business unit)
        └── Factory       (a customer's governed agent line — maps to a "line" in factory.py)
              └── Project  (one app/business/website — maps to products/<p>, tenant_products)
```

- **Billing attaches to the Organization** (1 plan per org, usage rolls up). This is what `billing.py` already does (signup→tenant+token+plan; free/pro/enterprise).
- Most platforms converge on a **3-tier billing-tenant → grouping → work-unit** shape. For agent-os: Organization → (optional) Workspace → Project, with **Factory** as the agent-fleet boundary. A free tenant may have one workspace/one factory; enterprise gets many.
- Scope SSO config, MFA policy, audit retention, session lifetime, and roles **to the Organization**, not globally.

## 1.4 RBAC — role matrix

| Role | Scope | Can do | Cannot |
|---|---|---|---|
| **Owner** | Org | Everything incl. delete org, transfer ownership, manage billing | — |
| **Admin** | Org/Workspace | Manage members, factories, policies, BYO-keys, approve decisions | Delete org, change billing plan |
| **Member** | Workspace/Project | Direct the orchestrator, start/steer builds, approve in-scope decisions | Manage members, billing, org policy |
| **Billing** | Org | View/manage plan, invoices, usage, payment — **seat-free** | Touch factories/agents/data |
| **Viewer** | Org/Workspace/Project | Read-only dashboards, traces, costs | Any mutation, any approval |

Keep the global set small (3–6 fixed roles), push customization to **scoped roles** (workspace/project owner). Isolate **billing as a narrow, seat-free role** (GitHub/Vercel/Slack pattern). Users can hold multiple roles (permissions combine, Stripe model).

**Authorization engine path.** Start with RBAC in-app. As resource-level sharing appears (e.g., "share this one project's dashboard with an external reviewer," "this agent may only touch these repos"), adopt **ReBAC** — **OpenFGA** (CNCF/Auth0 ecosystem, broad language support) or **SpiceDB** (full Zanzibar zookie/new-enemy consistency guarantee). Run **centralized policy authoring + embedded/sidecar evaluation** for latency. The existing **Cerbos** dependency (already in the selftest container check) is a stateless-PDP fit for centralized policy + low-latency local decisions — keep it for app-level RBAC/ABAC, add a ReBAC engine only when relationship-graph sharing demands it.

## 1.5 Data isolation — the graduation path

**Phase 1 (now → most tenants): Pooled.** Single Postgres, `organization_id` on every table, **Postgres RLS** as an implicit `WHERE` clause tied to roles. This matches the existing single-DB architecture and the "local→cloud is ~1:1 config" goal. Enforce isolation **in-band**; prove it with **automated isolation tests + audit logging** (necessary for SOC2 — RLS alone is not sufficient). Apply the five RLS performance disciplines:
1. Wrap auth functions in a subquery (`(select auth.uid())`) so the optimizer caches → up to ~1,100× on admin checks.
2. Index policy columns (`organization_id`, `user_id`) → avoids seq scans (~99.9%).
3. Add explicit client-side filters duplicating the predicate.
4. Always specify `TO authenticated` to short-circuit irrelevant roles.
5. Push membership joins into `SECURITY DEFINER` functions.

**Phase 2 (regulated / noisy / large tenants): Bridge.** Schema-per-tenant via **Citus 12 schema-based sharding** (`citus.enable_schema_based_sharding`) — viable at scale now that it fixed the PgBouncer pooling problem. Route by tenant tier in middleware.

**Phase 3 (HIPAA / data-residency / per-customer BAA): Silo.** Database-per-tenant behind a **provisioning control plane** (the existing `control-plane` repo is literally this). Justified only by blast-radius elimination, instant per-tenant restore, data residency.

> **Rule:** start pooled — the decision is *cheap to make, expensive to undo*. Migrating live tenant data = downtime customers notice. Don't start siloed.

**Per-tenant trace isolation** (already flagged as remaining work in the memory): tie every `trace`, `conversation`, `task`, `hire_request`, and `decision` row to `organization_id`; RLS-scope them; this is the concrete next step to make the existing debugger (`trace.py`) sellable per-tenant.

## 1.6 Audit & session security

- **Audit log:** append-only, **hash-chained**, shipped to a **separate WORM store** (S3 Object Lock compliance mode). Capture who/what/when/where (actor id/email/ip/session, action, resource type/id/changes, result, method, user_agent). 12-month retention, ~90 days hot. **Never log secrets** (the existing `redact.py` already scrubs sk-/aos_/AKIA/bearer/DB-url/keys at write time — extend it to the audit path). Expose as a **tenant-scoped query UI** for customer admins; durable-queue SIEM export. Cover SSO logins, SCIM events, MFA changes, role/admin changes. OWASP A09:2025 = "Security Logging *and Alerting* Failures" — alert on failed auth, not just log it.
- **Sessions:** short-lived JWT access (5–15 min) in `__Host-` HttpOnly/Secure/SameSite cookies + **opaque rotating refresh tokens** with token-family reuse detection + grace window; per-user token-version counter for "revoke all." Idle (15–30 min) + absolute (4–8 h) timeouts server-side, **org-configurable** for enterprise.
- **MFA:** **WebAuthn/passkeys** (phishing-resistant, NIST 800-63-4 AAL2/AAL3) with TOTP fallback; **step-up** on sensitive actions (spend approval, deploy, key rotation, data export).
- **BYO-API-keys (already in the product):** keep **envelope encryption** with a KMS/Vault-held KEK (the memory notes vault-encrypted per-tenant keys + a "cloud-KMS vault backend behind the existing interface" migration step — that interface is exactly right). Discard plaintext DEKs immediately; offer **CMEK/BYOK** to regulated customers.

---

# Area 2 — Agent-Interaction & Observability UX (the core)

**Design persona:** a **non-technical "CEO."** The load-bearing research insight (HatchWorks): *chat-first fails for non-technical users* because work is async/multi-step. Lead with **outcomes, taskboards, and receipts**, not transcripts. Progressive disclosure is the through-line everywhere: **summary → expandable detail → raw trace**, defaulting the CEO to layer 1.

## 2.1 Information architecture (the app shell)

```
┌──────────────────────────────────────────────────────────────────────┐
│  TOP BAR: Org switcher · Exec KPI strip · "Needs your input (N)" 🔔    │
├───────────────┬──────────────────────────────────┬───────────────────┤
│ LEFT RAIL     │  CENTER (swappable)              │ RIGHT (context)   │
│ • Orchestrator│  ▸ Org Map (live node-graph)     │ • Live activity   │
│ • Threads/    │  ▸ Plan view (Graph⇄Plan toggle) │   of selected     │
│   sessions    │  ▸ Chat with selected agent      │   agent           │
│   (unread •)  │  ▸ Fleet Dashboard (7 bands)     │ • Action receipts │
│ • Projects    │  ▸ Approval Inbox                 │ • "Why?" rationale│
│ • Pinned      │  ▸ Incidents                     │                   │
└───────────────┴──────────────────────────────────┴───────────────────┘
```

This is the **ClawPort "Org Map + swappable detail/feed pane"** layout (the closest 1:1 reference in the wild) upgraded with Conductor's animated hand-off edges and Magentic-UI's plan narration. The existing `dashboard.py` (live fleet, comms graph, message queue, health, two-way "message an agent") is the seed — it already has the fleet view, the hub-and-spoke comms SVG, and bidirectional messaging. We're leveling it up.

## 2.2 Chatting with the fleet (orchestrator + sub-agents)

**Every agent gets its own addressable thread.** The CEO can DM the orchestrator *and* drop into any sub-agent's thread, including background ones (Devin's "each managed Devin has its own session link, so you can message it directly" — the gold standard; Manus's invisible sub-agents are the counter-pattern to avoid).

Concrete UX:
- **Left-rail thread list** per session/agent with **unread dots** (clear on open), **pinning**, filters, rolled up under a Project container (Devin + Vercel v0).
- **Addressing grammar** (Cursor's Slack grammar is the best template): mention-to-start, mention-in-thread to steer, a keyword to **fork a new agent**, **"list my agents"** to inventory. In agent-os terms this maps onto `directory.py` (roster/find/contact) and `orchestrate.request_collaborator()` (reuse-vs-spawn).
- **Background HITL** (Claude Code 2026 model): when a background agent needs permission, the request **surfaces in the main session naming the asking agent**; approve to continue or deny that one call without killing the agent. **Visually separate background streams** from the foreground chat (a filed Claude Code bug warns against bleed).
- **Two-way messaging already exists** (`dashboard.py` POST /api/message bearer-auth → agent mailbox). Formalize: the orchestrator's mailbox is the CEO's default DM; each role-agent's `directory` presence + `inbox` is a per-agent thread.

Backend mapping: the existing **brokered durable mailboxes** (`conversations` + `inbox`, exactly-once, peer-can-be-offline) are exactly right for "DM an agent that's currently asleep" — the message waits in its inbox; the `dispatcher` wakes it.

## 2.3 Live activity view ("what is each agent doing right now")

Three archetypes from research; for a CEO, synthesize to: **checklist + status chip on top → stream of plain-language activity cards in the middle → collapsed thinking + raw diffs/traces one click deeper.**

- **Plain-language activity cards** ("Reading your sales spreadsheet," "Editing 3 files," "Running 71 tests"), streamed via SSE so motion signals "it's alive." Source: Claude Code TodoWrite + Magentic-UI plan steps.
- **Sticky % progress bar + one-line "current step."**
- **File-diff view** behind a tap: inline/side-by-side toggle, green/red/gray, per-file `[+12/−3]` summaries; separate *reviewing* from *editing* (keep the CEO in yes/no mode).
- **Thinking collapsed by default**, summarized when expanded (auto-open while streaming, auto-collapse when done). **Never stream raw chain-of-thought at a CEO.**
- **Scrubbable replay timeline** (Devin) for after-the-fact review.
- **Action receipts** — "what changed, where, when" — for every consequential action.

Backend mapping: the existing **`trace.py`** (full prompt+response+test output per stage, 20k cap, redacted at write) is the raw layer; the **`fleet.py` audit stream** is the activity feed; the dashboard already replays a run timeline via `/api/trace`. We add the SSE streaming + the plain-language card layer on top, and enforce per-tenant trace isolation.

## 2.4 Agent org-chart / hierarchy (live visual)

**A live React Flow node-graph:** orchestrator on top, role-agents and sub-agents below, edges = reporting/delegation. (ClawPort Org Map is the 1:1 reference; the existing dashboard's **hub-and-spoke comms SVG** built from the `conversations` fabric + audit is already a primitive version of this.)

- **Status by color *and* motion**, with a visible legend: idle=grey, running=blue/teal pulse, done=green check, error=red badge, **waiting-on-human=amber**.
- **Animate only the active hand-off edge** (Conductor) — clicking a hand-off card in the feed highlights the corresponding edge (bidirectional link between feed and map).
- **Click a node → friendly profile**: current task in plain English, reports-to, recent outputs, cost; prompts/tokens behind "advanced." (Maps to `directory.py` roster entry + the role manifest from `generate_org.py`.)
- **Graph ⇄ Plan toggle** (Magentic-UI): the **plan view** (ordered natural-language steps, each tagged with the assigned agent, completed steps auto-collapse) is often **more readable for a CEO** than a raw graph. The existing `factory.py` SPEC→BUILD→QA→REVIEW→LAUNCH stages *are* the plan steps.
- **n8n's negative lesson:** authoring canvases don't auto-highlight live execution — **live status highlighting is a feature you must build** (the dashboard's 3s auto-refresh is the start; move to SSE).

Backend: the **`directory.py`** roster (register/release presence, find, conflicts) + **`orchestrate.py`** hierarchy (controller → role agents; hire_requests as the spawn inbox) provide the live node + edge data. The org graph is *real* (memory notes "factory.py logs real controller↔role handoffs to conversations so the graph is real").

## 2.5 Inter-agent communication feed & hand-offs

Default to a **Slack-style labeled transcript** — `Researcher → Writer: "handing off the draft"` — with avatars/timestamps and a clear "now handing to ___" marker (AutoGen's `sender → recipient` transcript is the most human-readable).

- Render delegations as **first-class hand-off cards** (who → whom, task in one sentence, **one-line "why"**); clicking highlights the Org Map edge.
- **Group the feed by plan step, collapsible** (completed steps auto-collapse) — Magentic-UI ledger narration.
- Per-message **"Why?"** affordance revealing rationale + evidence (CoT hidden by default).
- For 20+ agents, prefer the **inbox/threaded model** with filters (by agent / task / "needs-human") over one shared chat.

Backend: this is a *direct render* of the existing `conversations` fabric (controller↔role handoffs, `contact()` direct messages, `request_collaborator` routing decisions). Conflicts (`directory.conflicts()` — two agents on overlapping resource globs) surface as a special feed item + map highlight, exactly as the dashboard already alerts.

## 2.6 Monitoring dashboards (the fleet dashboard, 7 bands)

Organized by **Golden Signals + agent-specific cost/quality + an exec ROI strip**. Universal drill-down: aggregate KPI → run table → trace waterfall → raw I/O.

- **Band 0 — Exec strip** (Stat tiles + Δ): Tasks completed today (& success %), Cost today + Δ vs budget, **Cost-per-task vs human baseline** (benchmark: agents should be **≥80% cheaper** than human labor), **Human-intervention rate** (red if **>40%** — the industry inadequate-ROI threshold), Agentic uptime (orchestration-graph completion %, not API uptime), Active agents now.
- **Band 1 — Traffic:** tasks started/min, sessions/day, **token burn rate** (in/out split), busiest agents/workflows.
- **Band 2 — Saturation/Queues** *(the band LLM tools under-serve — must add):* **queue-depth gauge** (threshold-colored), concurrency vs capacity %, 429 headroom / retry-storm, per-worker state-timeline.
- **Band 3 — Latency:** latency heatmap + P50/P95/P99, **TTFT**, per-step/tool slow-list.
- **Band 4 — Errors/Health:** run + tool failure-rate %, top error sources, **SLO widget + error budget**, incidents per 1,000 runs, guardrail blocks.
- **Band 5 — Cost/Tokens:** total cost + tokens over time (cached vs non-cached), most-expensive runs/prompts, cost attributed by agent/workflow/model/**customer**/sub-agent.
- **Band 6 — Quality/ROI:** eval/quality scores, **agent ROI per workflow** (labor $ saved − operating cost; red if negative), **blast-radius cap** ($ ceiling).

Backend: agent-os already has the data plumbing — **real build economics** (`factory.agent` captures real `total_cost_usd` + tokens per stage), **`portfolio.py`** (shipped/build-cost/revenue + MRR), **`osq.py`** (any agent queries complexity/cost/tokens/time/status as JSON), **`eval_factory.py`** (pass@1/latency benchmark). The existing `dashboard.py` already shows fleet, queue-with-latency, health/disk/backup, Portfolio KPIs. We're reorganizing into the 7-band exec layout and adding the saturation band + cost-per-task-vs-human tile.

## 2.7 Incidents & on-call (agents that page humans)

Treat the fleet like a **24/7 team that pages a human when stuck.** Lifecycle (copy PagerDuty/Datadog/Opsgenie verbatim): **Triggered** (active, unowned, escalation clock running) → **Acknowledged** (claimed; halts escalation; reverts on ack timeout) → **Resolved**.

- **Three states always visible:** Triggered (red + clock) / Acknowledged (amber) / Resolved (green).
- **"Who's on call now" panel for agents *and* humans** (on-call agent, standby human, time to next handoff).
- **Escalation ladder with timers**, repeating so nothing drops: **Agent → senior/role agent → on-call human → team.** Tie paging intensity to severity automatically (Datadog: WARN→low, ALERT→high).
- **Auto-built timeline + auto-assigned owner from second one.**
- **Automate by reversibility, not capability** (Rootly/Augment AI-SRE ladder: Read-only → Advised → Approved → Autonomous): reversible actions (retry, scale, restart) auto-run; irreversible ones (money, customer data, prod deploy) gate on a human.

Backend: this is **already built and proven** — `watchdog.py` (pages phone on new incidents with cooldown/dedup + recovered pings), `responder.py` (bounded auto-fix *before* paging — "killed dashboard → watchdog detected → responder restarted it → paged nobody"), `incident.py` (reasoning incident-commander writes an RCA + recommends one safe action + escalates *with analysis* for novel failures). The AI-SRE maturity ladder is **literally how responder/incident already split act-vs-escalate** (responder auto-fixes the reversible; judgement calls + approval-gated actions ESCALATE). The UX work is surfacing this as the incidents view + on-call panel.

## 2.8 Capacity, backpressure, idle agents

Agents = workers; tasks = a line of waiting customers.

- **One traffic-light pool view** (k9s colors): green busy / grey standby / yellow starting / red crashing — *"12 working, 5 standby, 2 stuck."*
- **Show idle positively** as "standby capacity/headroom," not "wasted." Show **busy ÷ total = utilization** as the saturation gauge.
- **One headline backlog number: "oldest waiting task is N minutes old"** (Sidekiq/SQS `ApproximateAgeOfOldestMessage` — far more intuitive than raw counts).
- **Distinguish waiting vs stuck** (RabbitMQ Ready vs Unacked) — different problems, different fixes.
- **Backpressure as a visible self-protection state** ("Backpressure: throttling intake"), not a failure.
- **Make autoscaling legible** (event feed: "2:14 — backlog hit 500, scaling 8→14") and **raise the saturation ceiling explicitly** ("Want 6 more agents, none available").

Backend: the work queue **is** Postgres `tasks` + `FOR UPDATE SKIP LOCKED`; horizontal scale = N worker processes on N machines, same `DATABASE_URL`. Queue depth, oldest-task-age, and per-worker state are all derivable from `tasks` + `heartbeats` (the dashboard already shows queue latency + ♥ heartbeat age). The per-agent **priority task queue** (1..9, drained highest-first) in `orchestrate.py` gives the "waiting vs stuck" and priority data directly.

---

# Area 3 — The "Minimal CEO Decisions" Interaction Model

**Goal restated:** the orchestrator should interrupt the CEO **only when it genuinely needs a decision**, ask for the **minimum**, and make the residual asks **frictionless**. The single highest-leverage finding (Anthropic Claude Code auto mode): **93% of permission prompts get rubber-stamped** → the win is *not asking* via standing policies + a risk classifier, then making the rest one-tap decision cards.

## 3.1 The six load-bearing ideas (each grounded in a shipping product)

1. **Durable interrupt/resume substrate.** The agent must pause indefinitely, persist full state, and resume from exactly where it stopped — the CEO may take 5 seconds or 5 days. (LangGraph `interrupt()` / OpenAI Agents SDK approvals / Step Functions `.waitForTaskToken` / Temporal signals.) **agent-os already has the substrate**: factory crash-resume via trace checkpoints (skip stages already done) + brokered durable mailboxes (suspend-until-reply in `orchestrate.py`). "Ask the human" = a durable tool call, not a blocking RPC.
2. **Two-stage risk classifier gates what reaches the human at all.** Auto-approve safe + reversible; escalate only risky/irreversible/low-confidence. Anthropic's model: Stage 1 fast single-token yes/no tuned to err toward blocking; Stage 2 chain-of-thought only on flagged actions. ~20 default block categories (irreversible deletions, security degradation, credential access). **Block ≠ stop** — a blocked action triggers retry-with-a-safer-path.
3. **Standing policies replace per-event asks.** The CEO sets guardrails once; the agent interrupts only when it would cross a line. (Ramp/Brex: 90%+ of in-policy spend never asks; out-of-policy declines at swipe.)
4. **Decision cards, not conversations.** Each interrupt is a scannable card with a **recommended option pre-selected** and a **one-tap approve** (Ramp "approve with one click, no login"; GitHub deployment review).
5. **Confidence-gated, tiered, batched notifications.** Reserve push/SMS for genuinely urgent-and-blocking; batch the rest into a digest. Route by *consequence × reversibility × confidence*.
6. **Smart defaults + minimal structured elicitation.** When you must ask: flat structured form, recommended pre-selection, enum choices over free text (MCP elicitation; the default effect — <5% of users change a good default).

## 3.2 The policy / guardrail layer (where you win "minimal decisions")

Give the CEO a **one-time guardrails setup**, in their language, that the agent consults instead of asking:

- **Spend autonomy:** "Auto-approve infra/tooling spend under $X/month; ask above." (Ramp envelope model — issue scoped budgets, not blank checks.) **agent-os mapping:** `appguard.py` already has `spend_cap` + `loss_limit` per app and auto-PAUSES bleeding apps — extend to per-org spend policy.
- **Ship autonomy:** "Auto-ship reversible changes that pass tests; ask before anything irreversible or user-facing-breaking." (GitHub risk-threshold deploy gates.) **Mapping:** `factory.py` already gates LAUNCH on green QA (else BLOCKED_AT_QA) — that's the reversibility gate.
- **Risk tiers:** map the block taxonomy (deletions, security, credentials, external comms, public posting) to *always-ask*. **Mapping:** the memory already notes "publish stays human-gated (public_post)" and approval-gated actions (spend/deploy/secrets/data) ESCALATE in `responder.py`.
- **Pre-authorized actions:** a standing allowlist ("never ask about: running tests, creating preview deploys, opening draft PRs").
- **Standing answers:** capture repeated decisions as policy (approved "use Stripe" once → don't re-ask).
- **Hard guardrails (never overridable):** segregation-of-duties invariants in code (no prod deploy on red tests; no spend above absolute cap; deny rules win even in autonomous mode — Claude Code's "deny beats bypass").

**The compounding effect:** every policy permanently removes a *class* of future interruptions. The orchestrator should **propose new policies** after repeated identical approvals — *"You've approved 5 preview deploys this week. Auto-approve these going forward? Yes / No"* — actively driving the decision count toward zero.

## 3.3 Anatomy of a decision card (the 5-second decision)

```
┌─────────────────────────────────────────────────────┐
│ 🟡 Decision needed · from Orchestrator · 2m ago      │
│                                                       │
│ Ship the checkout redesign to production?            │  ← 1. plain-language headline
│                                                       │
│ ✓ Recommended: Ship now                              │  ← 2. recommendation PRE-SELECTED
│   Reversible — I can roll back in <2 min.            │  ← 3. why now / stakes (consequence+reversibility)
│   ~1,200 users affected.                             │
│                                                       │
│ [ $0 infra ]  [ Risk: Low ]                          │  ← 4. cost / risk badges
│                                                       │
│ ┌──────────┐ ┌─────────┐ ┌────────┐                 │  ← 5. 2–3 buttons (primary = recommended)
│ │ Approve  │ │ Change… │ │ Reject │                 │
│ └──────────┘ └─────────┘ └────────┘                 │
│                                                       │
│ ▸ See the work (diff · plan · logs)                  │  ← 6. collapsed detail (progressive disclosure)
│ 💬 Add a note…                                       │  ← 7. audit affordance (GitHub model)
└─────────────────────────────────────────────────────┘
```

Rules: one decision per card; strip jargon; buttons reflect the actual branches; **update the card to its resolved state immediately after action** (Slack best practice); approve from notification in one tap, no login (Ramp). **agent-os mapping:** `osq.decisions()` + `risk.py` already compute "what awaits the human" (paused apps, dedup'd hire approvals) and fold it into `digest.daily()` ("AWAITING YOUR DECISION" section). The Approval Inbox is the UI for this existing decision queue.

## 3.4 When to interrupt vs proceed — the decision rule

```
high confidence + low consequence + reversible   → act silently (log only)
moderate confidence                               → ask a clarifying question (low-friction)
low confidence OR high consequence OR irreversible → escalate (decision card + notification)
confidence override                               → escalate whenever certainty is low, even inside an autonomous category
```

Starting thresholds (tune on your own block/approve telemetry, as Anthropic did): high → act; medium (~60–90%) → act with caveats + offer to escalate; low (<60%) → escalate. **agent-os mapping:** `responder.classify()` already does exactly this split — known/reversible → auto-fix; `unknown`/judgement → `incident.py` reasoning + escalate-with-analysis. The factory's **callee-driven timeout handshake** (the agent estimates its own runtime/confidence pre-flight) is a confidence signal you can reuse for the gate.

## 3.5 Notification tiers (avoid fatigue, never miss critical)

| Tier | Example | Channel | Batching |
|---|---|---|---|
| **P0 — blocking + irreversible/costly** | "Approve prod deploy / $5k spend?" | Push + SMS | Never batch; immediate |
| **P1 — blocking, reversible** | "Pick between two UX approaches" | Push / in-app | 15-min coalesce |
| **P2 — FYI, action optional** | "Shipped feature X" | In-app | Hourly/daily digest |
| **P3 — progress/telemetry** | "Tests passing, deploying staging" | Activity feed only | Daily digest |

Pair every P0/P1 with a **timeout + safe default** (Step Functions' hard lesson) and a **reminder** (Ramp's 2-day nudge) so an unanswered decision degrades gracefully instead of stalling the build. **agent-os mapping:** the **ntfy phone bridge** is the P0/P1 push channel (already proactive on LAUNCHED/BLOCKED_AT_QA/incidents); the **weekly founder `digest.py`** is the P2/P3 batch. Native Claude Code remote control (phone app) = the CEO↔orchestrator DM channel.

## 3.6 Minimal-input elicitation

- **Always pre-select the recommended option** ("We recommend X — Approve / Change"); Approve is one tap, Change opens alternatives (progressive disclosure).
- **Structured choices over free text** (titled enums, multi-select); **flat schemas only** (MCP constraint — renders as a simple form).
- **Three response actions:** Accept / Reject / Cancel (a dismissal ≠ a rejection — handle distinctly).
- **Ask just-in-time at the moment it blocks**, not a 20-question upfront intake. (Tradeoff: occasionally the agent lacks something — mitigate with good standing policies so common cases are pre-answered.)
- **Never elicit credentials/PII** through these prompts.

---

# Build order (productization roadmap)

**Phase A — Tenancy hardening (make the existing single-tenant-ish stack truly multi-tenant):**
1. Formalize `organizations` + `User`↔`Membership`↔`Organization`; carry `organization_id` on every row (`tasks`, `traces`, `conversations`, `hire_requests`, `decisions`, `tenant_products`, cost rows).
2. Postgres RLS + automated isolation tests; per-tenant trace isolation (the flagged remaining work).
3. Managed auth (WorkOS/Clerk): social login now, SAML/OIDC/SCIM behind one integration; org-scoped SSO/MFA/session policy.
4. RBAC (owner/admin/member/billing/viewer) via the existing Cerbos; hash-chained audit log → WORM (extend `redact.py` to the audit path).

**Phase B — CEO observability UX (level up `dashboard.py` → the command center):**
5. App shell: Org switcher + Exec KPI strip + "Needs your input" badge; left-rail addressable threads per agent (orchestrator + sub-agents) on the brokered mailboxes.
6. Live **Org Map** (React Flow, status by color+motion, animated active edge) from `directory.py`+`conversations`; **Graph⇄Plan toggle** (factory stages = plan).
7. SSE live-activity cards (plain language) + file-diff + collapsed thinking, from `trace.py`/`fleet.py`; scrubbable replay.
8. 7-band fleet dashboard (reorg existing panels + add Saturation/queue band + cost-per-task-vs-human tile) from `portfolio.py`/`osq.py`/build economics.
9. Inter-agent feed (labeled transcript + hand-off cards + "Why?") from `conversations`; conflict items from `directory.conflicts()`.
10. Incidents view + "who's on call" (agents+humans) + escalation ladder, surfacing `watchdog.py`/`responder.py`/`incident.py`; capacity traffic-light + oldest-waiting-task from `tasks`/`heartbeats`.

**Phase C — Minimal-decision model:**
11. Standing **policy/guardrail layer** (per-org spend caps via `appguard.py`, ship autonomy via QA gate, pre-authorized allowlist, risk-tier always-ask).
12. **Two-stage risk classifier** as the ask-vs-act gate (extend `responder.classify`).
13. **Approval Inbox** + decision cards (pre-selected recommendation, one-tap, progressive disclosure) on `osq.decisions()`/`risk.py`.
14. Tiered/batched notifications (ntfy P0/P1, `digest.py` P2/P3) with timeout+default+reminder.
15. **Policy-proposal loop** ("auto-approve these going forward?") that drives the human-decision count toward zero.

---

# One-paragraph product narrative (for the blueprint top sheet)

A customer signs up, creates an **Organization** (their factory), invites their team with **RBAC roles** and enterprise **SSO/SCIM**, and pastes a **BYO LLM key** (envelope-encrypted per-tenant). They set **standing guardrails once** — spend caps, ship-autonomy, pre-authorized actions. They then **DM the orchestrator in plain English** ("build me a booking site for my clinic"). The orchestrator decomposes the work, **hires role-agents** from a pre-built governed org, and runs the **SPEC→BUILD→QA→REVIEW→LAUNCH** factory autonomously. The CEO watches a **live Org Map** of agents (who's working, idle, stuck), reads **plain-language activity cards** and **hand-off feed**, and monitors a **7-band fleet dashboard** (cost-per-task vs human, intervention rate, queue depth). A **two-stage risk classifier** + **standing policies** mean the platform proceeds silently on everything safe and reversible; only genuine judgment calls surface as **one-tap decision cards** in an **Approval Inbox**, pushed by tier (SMS for blocking-and-costly, digest for FYI). When an agent gets stuck, it **pages up an escalation ladder** (agent → senior agent → on-call human), with the **responder auto-fixing reversible incidents before paging anyone**. Every action is **traced, redacted, audited, and tenant-isolated**. The result: a customer ships software end-to-end as a CEO who makes a handful of decisions a week, not a thousand.
