# agent-os Platform — Master Product Blueprint

> **The vision:** turn agent-os (a governed autonomous AI software factory) into a **multi-tenant, enterprise-grade SaaS** where a customer acts as **CEO** — gives high-level direction to an orchestrator — and a fleet of governed AI agents builds, ships, deploys and operates their apps/businesses with **minimal human intervention**. Each customer runs their own factory. Bring-your-own keys. Free → Enterprise. Web + mobile + app store. *"If the big clouds vanished, you could still run your software on this."*

This is the **specification**, produced from 4 parallel deep-research streams (hundreds of cited sources). It is **not** the built product — building it is a multi-quarter, team-scale effort. The good news, found independently by every stream: **most of the hard backend already exists in agent-os; the work is a productization layer + surfacing existing primitives as CEO-facing UX.**

---

## The four pillars (read in order)
1. **[01 — Architecture, Identity, Multi-tenancy & Agent UX](blueprint/01-architecture-identity-agentux.md)** — auth/SSO, org model, tenant isolation, the agent-interaction & observability UX, the minimal-decision model. *(deep dives: [identity](blueprint/research-identity-multitenancy.md), [agent fleet UX](blueprint/research-agent-fleet-ux.md))*
2. **[02 — Integrations, Onboarding, Deploy & Security](blueprint/02-integrations-onboarding-deploy-security.md)** — the integrations catalog, the "minimal-info / AI-assisted / confirm-and-go" onboarding & provisioning flows (Stripe, BYO-key, AWS deploy, GPU rental), deploy/infra automation, and the per-tenant security model. *(deep dive: [secrets & security](blueprint/research-secrets-security.md))*
3. **[03 — Monetization, Finance, Legal & Personas](blueprint/03-monetization-finance-legal-personas.md)** — pricing/packaging, unit economics, legal/compliance/risk, and the persona × industry needs matrices. *(deep dive: [legal & compliance](blueprint/research-legal-compliance.md))*
4. **[04 — Exhaustive Screen Inventory](blueprint/04-screens-inventory.md)** — ~240 screens across web + mobile + admin, 16 areas. The "list every view/page" deliverable.
5. **[05 — Product Lifecycle (marketing/sales/pitch/feedback/IP/whitepaper + task board + external integrations)](blueprint/05-lifecycle.md)** — the part *after* "build", for our product and users'.

---

## What already exists vs. what's net-new
The factory engine is built. The platform shell around it is the work.

| Already in agent-os (the engine) | Net-new (the productization layer) |
|---|---|
| Governed SPEC→BUILD→QA→REVIEW→LAUNCH factory, crash-resume, Codex failover | Multi-tenant **auth + org/RBAC** (managed provider: WorkOS/Clerk) |
| Tamper-evident **audit**, sandbox, prompt-injection defense, redaction | **Hardened per-tenant isolation** (RLS on every row → schema → DB-per-tenant graduation) |
| **BYO-key** routing, per-tenant vault (Fernet) | **Billing**: Stripe metered + spend caps + plan entitlements/feature-flags |
| `appguard` spend caps, `responder` self-heal, `watchdog`, incident-commander | **CEO-facing UX**: cockpit, orchestrator chat, pipeline view, approvals inbox, trace explorer |
| `directory` (agent presence/conflicts), comms, traces, metrics | **Integrations catalog** + AI-assisted onboarding/provisioning flows |
| Postgres-as-control-plane, `tasks`+SKIP-LOCKED queue, snapshots, Terraform skeleton | **Deploy automation** (build→live URL; deploy-to-customer-cloud), **mobile apps**, **marketing site + admin back-office** |

---

## The doctrines that run through everything (the load-bearing insights)
- **"Don't ask — govern."** Anthropic found **~93% of approval prompts get rubber-stamped.** The win is *not asking*: standing policies + a two-stage risk classifier, then making the residual asks one-tap decision cards with a pre-selected recommendation. agent-os already has the pieces (`appguard` + `responder` + `risk`/`osq.decisions`) → fuse into one **universal policy gate** + **Approvals Inbox**.
- **The onboarding doctrine:** collect *minimum* input → hand off to the provider-hosted flow for anything regulated (Stripe KYC, OAuth, cloud console) so we never touch hard data → **verify out-of-band** (never trust the redirect) → gate consequential actions behind **one confirmation stating what / cost / reversibility / blast-radius** → **tier autonomy by cost and reversibility** (serverless GPU = autonomous; prod deploy = confirm).
- **Isolation:** containers are **not** a security boundary for hostile code → **microVM-per-tenant** for untrusted execution. Watch the **"lethal trifacta"** (untrusted input + private data + exfil channel) — apply the Rule of Two.
- **Pricing:** **hybrid (seat + metered build-credits) with a hard spend cap ON by default.** Uncapped credit billing caused industry backlash in 2025 (Cursor refunds) — *predictable + capped* is a differentiation lever, not a limitation.
- **Economics:** inference is the dominant COGS; AI-native margins are **52–65%, not 80%**. **BYO-key is the central margin lever** (→ 88–92% on those accounts; blended target 72–78% via BYO mix + prompt caching + model routing + batch).
- **Legal:** disclaim AI output "AS IS" + customer-indemnifies; AI-generated code is **uncopyrightable** (protect via trade-secret/contract); **don't be merchant-of-record in v1** — tenants connect their *own* Stripe (Connect doesn't offload chargeback/tax liability); SOC 2 Type II early; hard caps as a **denial-of-wallet** control; name crypto-mining in the AUP.
- **The real moat is trust/verification, not speed.** The governance layer (audit + approvals + sandbox + the green QA gate) is what makes it safe for a stranger to run an autonomous factory — that *is* the sellable value. **White-label for agencies** is open whitespace.

---

## Packaging (recommended starting shape)
| Tier | Price (anchor) | Who | Key unlocks |
|---|---|---|---|
| **Free** | $0 + BYO-key | solo, trying it | single workspace, limited builds, BYO-key only, community support |
| **Pro** | ~$25/mo + metered credits | indie / non-technical founder | more builds, observability/debugger, marketing-kit, custom domains |
| **Team** | ~$40/seat/mo | small teams / agencies | seats, RBAC, shared blueprints, **white-label** (agency whitespace) |
| **Enterprise** | custom (~$2.5–10k+/mo floor) | SMB→enterprise internal tools | SSO/SCIM, audit/SIEM, deploy-to-own-cloud, data residency, SLA, eval scorecard |

**Beachheads (per persona research):** **internal B2B tools** + **e-commerce ops** — two opposite GTM motions (flat-price + guardrails for non-technical/SMB; code-export + no-lock-in for technical/startup/agency).

---

## Phased roadmap
- **Phase A — Tenancy & identity foundation:** managed auth, Organization/RBAC, RLS-on-every-row tenant isolation, Stripe billing + plan entitlements, BYO-key center. *(Unlocks: a stranger can sign up, pay, and run an isolated factory.)*
- **Phase B — CEO cockpit & observability UX:** orchestrator chat, pipeline view, agent comms graph + per-agent chat, trace explorer, monitoring — i.e. **surface existing primitives** (`directory`/`traces`/`watchdog`/`conversations`) as UX.
- **Phase C — The minimal-decision engine:** universal policy gate + Approvals Inbox + standing policies + risk classifier + proactive orchestrator pings.
- **Phase D — Integrations & autonomous provisioning:** the catalog + Stripe-Connect/AWS-deploy/GPU-rental confirm-and-go flows + deploy-to-live-URL automation.
- **Phase E — Hardening & scale:** microVM isolation, SOC 2 Type II, abuse/T&S + content moderation, admin back-office, mobile apps, marketing site.

---

## Honest reality check
- This is a **funded-team, multi-quarter product**, not a weekend build. The blueprint exists so it can be built **incrementally and deliberately** — and agent-os can **dogfood itself** to build large parts of its own platform.
- The **highest-leverage first slice (a real MVP)**: Phase A + the *thinnest* vertical of Phase B/C — **auth + org + BYO-key + orchestrator chat + one governed build + the approvals inbox.** That's the smallest thing that lets one external user direct a build and safely approve gated actions — i.e. proves the entire product thesis with the least code.
- Caveats from the research (flagged for re-verification at build time): vendor pricing/ARR figures are directional; API shapes drift (Stripe controller props, Google one-time secret reveal, GPU prices, MCP auth); the legal sections are research, **not legal advice** — confirm with counsel before launch.

*Generated from parallel deep research, grounded in the existing agent-os architecture. The screen inventory and pillar docs are the working spec; this page is the map.*
