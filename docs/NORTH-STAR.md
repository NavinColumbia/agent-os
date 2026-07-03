# North Star — what agent-os IS

**Every user is a CEO running several companies — except every employee is an AI agent.**

The experience must be *indistinguishable* from being the real CEO of several multi-billion-dollar
companies with tens of thousands of employees:

- **Human-pattern interactions.** Alerting, hiring, escalation, status reporting, clarification,
  disagreement, hand-offs — every communication mimics how a real elite org communicates with its CEO.
  The CEO is briefed, consulted on the calls that matter, and never bothered with noise.
- **FAANG-grade internal processes, all of them.** Product creation, research, design, engineering,
  QA, security, support, ops, incident response, post-mortems, planning, review cycles — every internal
  process a top company runs, the org runs autonomously, scalably, and fast.
- **Extremely skilled agents, every decision agentic.** Every actor holds elite skills and every
  decision — next action, org expansion, escalation, notification, hiring — is an intelligent AI
  decision, not hardcoded flow. All possible skills and extensions available.
- **Capable of the most complex things.** Managing several companies at once, arbitrarily complex
  visions, elastic recursive orgs that grow themselves to whatever scale the work demands.
- **Quality bar: astonish a skeptic.** Assume users are fickle, hard to win, and trust AI *less* than
  humans. One frustration and they're gone. The system must *surprise* them with how good it is —
  frustration-free, high-quality, zero bugs reaching a human.
- **Resilient to LOUD and SILENT failures — anything and everything.** OS shutdown, a container dying,
  provider rate-limits/529 storms, a hung agent, a stalled workflow, a network blip — the system detects
  it (observer agents constantly watching, heartbeats on everything including *agentic work in progress*),
  heals itself where it can, and **proactively communicates** otherwise: the controller pings the CEO
  before the CEO ever wonders "did something silently die?". Long-running work reports progress on a
  cadence; silence is itself treated as a failure signal. Nothing fails invisibly.

**Commercial bar:** sellable for hundreds of millions; hundreds of thousands of users, each launching
millions of dollars of business through it.

Every architecture decision, every review, every standard in this repo answers to this document.
Anything — any mechanism, any subsystem, any prior decision — may be completely rewritten if it falls
short of this bar. Be nitpicky and pessimistic on the product's behalf; the user experience is the
only judge that matters.
