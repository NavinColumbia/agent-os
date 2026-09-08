# North Star — what agent-os IS

**Every user is a CEO running several companies — except every employee is an AI agent.**

**Ultimate North Star:** Agent OS can found, expand, and operate AI organizations capable of creating other
world-class companies and systems. Capability must scale with the mission: from a solo product to programs
with thousands of concurrent specialist agents, human executives, large budgets, proprietary data, physical
operations, and regulated responsibilities. Given the necessary capital, time, compute, data, permissions,
and human specialists, the platform should be able to organize an effort comparable in scope to a leading
quantitative-trading firm, AAA game studio, global software company, or an important category nobody has
invented yet.

This is an organizational capability target, not a promise that money or one prompt guarantees a valuable
outcome. Agent OS must make uncertainty, prerequisites, legal boundaries, risk, evidence, and human authority
explicit while continuously improving the odds of success. It must never fake completion or quietly reduce a
large mission to a toy implementation.

The experience must be *indistinguishable* from being the real CEO of several multi-billion-dollar
companies with tens of thousands of employees:

- **Human-pattern interactions.** Alerting, hiring, escalation, status reporting, clarification,
  disagreement, hand-offs — every communication mimics how a real elite org communicates with its CEO.
  The CEO is briefed, consulted on the calls that matter, and never bothered with noise.
- **An adaptive Chief of Staff, not a form or script.** It continuously discovers the CEO's actual team,
  money, services, credentials, data, authority, and constraints; asks only questions that change the plan;
  fills safe temporary roles itself; proposes recruiting or vendors when capability is genuinely missing;
  and, after a person joins, proactively offers the sensible reassignment, delegation, and scoped copilot.
- **One trustworthy company memory.** Agent OS owns a durable tenant-scoped graph of missions, people,
  agents, services, work, conversations, decisions, approvals, costs, evidence, and incidents. Jira, Linear,
  Slack, email, and future tools are useful synchronized interfaces—not competing sources of truth. Every
  human can opt into a 1:1 AI assistant with explicit, revocable, least-privilege scopes.
- **FAANG-grade internal processes, all of them.** Product creation, research, design, engineering,
  QA, security, support, ops, incident response, post-mortems, planning, review cycles — every internal
  process a top company runs, the org runs autonomously, scalably, and fast.
- **Extremely skilled agents, every decision agentic.** Every actor holds elite skills and every
  decision — next action, org expansion, escalation, notification, hiring — is an intelligent AI
  decision, not hardcoded flow. All possible skills and extensions available.
- **Capable of the most complex things.** Managing several companies at once, arbitrarily complex
  visions, elastic recursive orgs that grow themselves to whatever scale the work demands. A mission may
  create portfolios, divisions, programs, teams, agents, sub-workflows, tools, simulations, and external
  human/vendor relationships; the CEO sees one coherent accountable organization rather than a linear chain.
- **Quality bar: astonish a skeptic.** Assume users are fickle, hard to win, and trust AI *less* than
  humans. One frustration and they're gone. The system must *surprise* them with how good it is —
  frustration-free, high-quality, zero bugs reaching a human.
- **Resilient to LOUD and SILENT failures — anything and everything.** OS shutdown, a container dying,
  provider rate-limits/529 storms, a hung agent, a stalled workflow, a network blip — the system detects
  it (observer agents constantly watching, heartbeats on everything including *agentic work in progress*),
  heals itself where it can, and **proactively communicates** otherwise: the controller pings the CEO
  before the CEO ever wonders "did something silently die?". Long-running work reports progress on a
  cadence; silence is itself treated as a failure signal. Nothing fails invisibly.
- **Long work is managed, not murdered by a stopwatch.** Deadlines and per-call limits bound individual
  operations, but a healthy mission is not abandoned because an arbitrary global timer fired at 70% progress.
  Durable checkpoints, owned leases, idempotent effects, provider failover/backoff, progress evidence, stall
  diagnosis, and escalation allow work to continue through restarts, rate limits, regional outages, and long
  human/external waits.
- **Models propose; governed truth commits.** Model output is schema-validated and treated as an untrusted
  proposal. Identity, tenancy, permissions, budgets, recipients, evidence, decision authority, and side-effect
  policy are verified deterministically. Hallucinated people, tools, success claims, or evidence cannot become
  company facts merely because an agent said them.

**Commercial bar:** sellable for hundreds of millions; hundreds of thousands of users, each launching
millions of dollars of business through it.

Every architecture decision, every review, every standard in this repo answers to this document.
Anything — any mechanism, any subsystem, any prior decision — may be completely rewritten if it falls
short of this bar. Be nitpicky and pessimistic on the product's behalf; the user experience is the
only judge that matters.
