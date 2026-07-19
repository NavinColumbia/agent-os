# System Requirements (living — maintained by visionkeeper)

> Auto-refined by the CEO's requirements-provider agent. The fixed constitution is `NORTH-STAR.md`; this is the
> evolving spec the controller and roadmap answer to. Do not hand-edit the sections below — run
> `visionkeeper.py refine` (or it refines on demand).

## CEO vision (standing)
I am a CEO who wants to run MULTIPLE companies and products at once, with AI agents — not humans — doing ALL the work. The agents should continuously REFINE the requirements themselves, OWN their work end to end, coordinate through rich, human-like communication, and hold a quality bar that ASTONISHES a skeptic (zero bugs reach a human; nothing fails invisibly). Humans (me) should be involved ONLY where absolutely necessary, and the system must tell me UP FRONT everything it needs from me — credentials, accounts, credit/budget, legal/compliance, and any approvals or emails — rather than interrupting me mid-build.

## Refined vision
Agent-os is the operational nervous system for a CEO managing multiple businesses simultaneously. AI agents act as an elite executive team—autonomous, accountable, communicating like humans, and holding quality standards that eliminate firefighting and surprise failures. The CEO directs strategy; agents execute end-to-end, refine their own roadmap, heal failures before they surface, and report only what matters.

## Goals
- Multi-company orchestration with hard tenant isolation (cryptographic; zero cross-tenant leakage), unified CEO oversight, and auditable trail per company
- Production-grade orchestra runtime: crash-resumable, fully instrumented (SLA: 5s query latency, zero silent failures, 2s progress cadence for long-running work)
- Proactive comms engine: CEO receives unsolicited alerts for blockers, decisions, anomalies, and handoff requests before asking; no important signal lost to noise
- Agentic requirement-refinement loop: agents and CEO co-author roadmap; requirements self-sharpen via agent feedback as work unfolds, preventing misalignment
- Scale from 1 to 10+ simultaneous companies and elastic org depth without degrading quality, SLAs, or requiring CEO operational work
- Nothing fails invisibly: 100% of system and agent failures detected by watchdog before CEO awareness (detection SLA <2s; notification <5s)

## Non-goals
- Replacing CEO's strategic judgment or accountability (agents inform and execute; humans decide)
- Cutting costs through blind automation without auditability or quality verification
- Removing human escalation for novel, high-stakes, or ambiguous decisions
- Generic, reusable agent frameworks (only domain-specific agents for business operations)
- Tolerating silent failures as a speed tradeoff

## Quality bar
- Zero known correctness, security, or billing bugs reaching production (weekly end-to-end audit + fix-before-release gate)
- CEO-facing query latency SLA: 5 seconds (status, decision request, escalation) or automatic escalation
- Long-running agent work reports progress every 2 seconds; silence triggers failure alarm within 2s
- Watchdog detection SLA: 100% of system/agent failures detected within 2 seconds; CEO notified within 5 seconds
- Multi-company isolation is cryptographically hard (zero tenant-crossing possible, even under adversarial behavior or credential compromise)
- Zero stalled work: any agent or workflow hung >60 seconds is detected, reported, and handed off or escalated by watchdog
- Billing per-company accuracy: ±1% monthly audit variance; CEO sees real-time spend forecast with circuit-breaker triggers

## What the system needs from the CEO (up front)
- **API budget & billing account authorization (Anthropic models, external LLMs, compute/storage, monitoring)** (budget, before_start) — Agents make unbounded API calls; CEO must set spending ceiling and own billing account for cost forecasting and circuit-breakers
- **Initial company/product configuration (business names, success metrics, growth targets, revenue models)** (approval, before_start) — Roadmap and agent objectives derive from company visions; agent-os can't infer what you're building or success metrics
- **External system credentials & access (GitHub, Slack, payment processors, databases, analytics, CRM)** (credential, as_needed) — Agents need read/write access to execute end-to-end: commit code, post updates, process payments, move data
- **Legal/compliance guardrails (data retention, access controls, audit logging, regulatory scope, incident response SLA)** (legal, before_start) — Agents' autonomy must respect regulatory & contractual bounds; CEO defines what agent-os can't do
- **CEO escalation preferences (which decisions need your input, which are agent-autonomous, escalation contact)** (approval, before_start) — Must know upfront to avoid under-escalation (bugs reaching prod) or over-escalation (CEO bottleneck)
- **Incident response & downtime tolerance (max acceptable outage, SLA degradation policy, who gets paged)** (approval, before_start) — Determines failure detection thresholds and notification urgency

## Next capabilities (sharpest first)
- Production-grade orchestra runtime: live-path end-to-end audit (trace any company decision → agent → API call → outcome) + crash-resumable task graph + SLA dashboard
- Proactive comms engine v1: detect and notify CEO of blockers (missing credentials, API quota hit), decisions (hire agent, reallocate resources), anomalies (unusual spend, retry storm)—no ask required
- Multi-company hard isolation + unified command center: cryptographic tenant boundaries, per-company audit trail, CEO sees all companies' status at once
- Self-healing failure recovery: watchdog detects stuck work, attempts automated recovery (retry, fallback, escalate to sibling), reports CEO only if unrecoverable
- Agentic requirement-refinement loop: agents propose roadmap refinements based on work-in-progress learnings; CEO approves on cadence; roadmap stays sharp without interruption
- Billing audit & forecasting: per-company spend tracking, monthly ±1% accuracy audit, spend forecast to completion, circuit-breaker rules (auto-pause if over budget)

## Open questions (CEO-level only)
- How many concurrent companies in year 1, and expected team size per company? (determines scale tier, isolation strategy, compute provisioning)
- Which external systems are non-negotiable for day 1? (GitHub, Slack, Stripe, PostgreSQL, analytics—prioritize by business criticality)
- Regulatory posture? (SOC2, HIPAA, GDPR, PCI, industry-specific; audit cadence; data residency; access logging)
- Who is the escalation contact when agent-os needs a human decision, and what's your decision latency SLA? (5 min, 1 hour, next business day?)
