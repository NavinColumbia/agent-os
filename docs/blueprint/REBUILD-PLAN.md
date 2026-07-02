# REBUILD PLAN — from the 2026-07 North-Star Architecture Review

Source: `docs/blueprint/ARCH-REVIEW-2026-07.json` (8 pessimistic domain critics; 24 REWRITE / 49
FALLS_SHORT / 78 MISSING / 51 churn moments). Constitution: `docs/NORTH-STAR.md`.

**The one-sentence diagnosis:** everything we promised ("every decision agentic", "a living org of
skilled employees", "zero bugs reach a human") exists somewhere in the repo — but NOT in the live path.
The shipping product is a regex waterfall (loopcontroller), stateless subprocess agents with no identity
or memory, a builder that grades its own homework at the LAUNCH gate, one chat thread as the entire CEO
experience, and a single-process http.server that cannot bill a dollar. The rebuild is therefore mostly
*promotion and wiring of the real thing into the live path*, plus three genuine new builds (memory spine,
capability/MCP layer, chief-of-staff).

## TRACK A — The Organism (the product's soul)
- **A1. ONE durable actor runtime.** Promote `scripts/orchestra/` to THE engine: agents + org trees as
  Postgres rows (identity, tenure, assignment, history — so "hiring" and the org chart are real), events
  on the persisted bus, parallel execution on the durable SKIP-LOCKED queue, crash-resumable, all factory
  gates enforced per actor. Delete the duplicate in-memory demos. loopcontroller dispatches into it.
- **A2. Agentic controller.** Keep loopcontroller's durability substrate (controller_jobs, SLA watchdog,
  gates); replace its decision layer wholesale: model-driven intent classification (kill every regex gate
  — "not good" must never read as approval), model-driven next-action/escalation/notification/org-expansion,
  N concurrent workstreams per company, life after DELIVER (v2s, ops, incidents — a company, not a wizard),
  heartbeat liveness (not the 30-min guillotine), mid-build steering (re-plan in flight, not cancel-only).
- **A3. Memory spine.** Per-tenant company memory (decisions, preferences, product history) injected into
  every agent brief; per-role lessons distilled from every BLOCKED/fix-loop post-mortem; the controller
  holds long-term company memory, not a 12-turn window.
- **A4. Capability layer.** Per-role `--allowedTools` derived from the role manifest (isolated
  CLAUDE_CONFIG_DIR — never the developer's personal settings.json), sandboxed Bash, MCP integration tier
  (GitHub, Slack, email/calendar, analytics, payments) bound to roles, skills as loadable playbook packages.

## TRACK B — The Experience (what the CEO sees)
- **B1. Chief-of-staff.** A first-class persistent persona with memory and a proactive turn loop: morning
  brief / "while you were away" (per-tenant, agentic, in the console — not operator ntfy), escalation
  etiquette ("your Head of Payments needs a decision by 3pm"), ONE ask-the-CEO mechanism (agent_request
  backed by durable suspend + reply_by + nudge). Delete askuser.py.
- **B2. The living org.** Dynamic org chart from real spawn/delegation data; click any agent → their work,
  message them directly (@-mention, "ask my CTO"); activity ticker; SSE/WebSocket live state everywhere
  (kill 5-15s polling). The org must visibly *work*.
- **B3. Real front-of-house.** SPA with URL routing/deep links/history, business KPIs (not token counts),
  search + command palette, multi-seat RBAC (kill the "roadmap" copy), pagination/aggregation for scale.
- **B4. Honest edges + demo magic.** Real OAuth for integrations (never "connected" unverified);
  an astonish-the-skeptic first 5 minutes before any BYO-key gate.

## TRACK C — The Business (sell it)
- **C1. QA is the ship gate** (FIRST — small, highest leverage). `qa_run` produces the LAUNCH artifact;
  `gate_check` binds to qa_report JSON (passed, blocking_open=0, stories>0); factory QA stage runs the
  agentic explorer against the running product; the builder NEVER grades its own homework; dogfood drives
  the live console through the explorer daily. Fix dev_loop's always-restart-and-re-explore.
- **C2. Scale tier.** ASGI (FastAPI/uvicorn) + stateless web instances, connection pooling, durable job
  queue with a separate worker fleet (web tier only enqueues), per-tenant audit chains (kill the global
  advisory lock), HA Postgres.
- **C3. Real billing.** Stripe: card capture, metering, webhook-driven plan state, dunning. A plan change
  is a payment event, never a column flip.
- **C4. Trust wiring.** The money circuit-breaker and human-approval gate must actually FIRE in production
  paths; per-action explainability ("why the AI did X" from traces); blast-radius statement per action.

## Sequencing
1. **C1** (immediately — closes "no human ever files a bug" and it's mostly wiring)
2. **A1 → A2** (the soul; A1 unblocks the real org chart, hiring, escalation)
3. **B1 + A3** (chief-of-staff + memory — the "feels human" moment)
4. **A4 + B2 + B3** (capable agents + living org + real SPA)
5. **C2 + C3 + C4 + B4** (deployable, billable, trustworthy, honest)

Every phase lands behind the full guard suite + ground-truth verification (docs/STANDARDS-verification.md);
nothing is "done" until verified against reality, not a relayed green.
