# Agentic Orchestration — the "very smart system" (NEXT BUILD after the agentic-QA loop)

**North star:** a human prompts a *vision*; an adaptive controller orchestrates hierarchical, self-organizing
agent fleets that research → plan (with the human) → build → exhaustively self-test → fix — and a **bug-free
product comes out, with no human ever filing a bug.** Every decision along the way is an AI call (see
`cost-off-max-ai` memory). This doc is the target architecture; sequenced AFTER `build-agentic-qa` lands.

## The hierarchy
```
Human vision
   │
CONTROLLER / COORDINATOR  ── makes ADAPTIVE agentic decisions (each an AI call)
   ├── Research fleet:  SUPERVISOR-researcher → parallel child researchers → aggregate → back to controller
   ├── (human plan sign-off)
   ├── Dev fleet:       PRINCIPAL/reviewer + parallel child devs, reviewer coordinates
   └── QA/coordinator:  runs the agentic testing loop (from build-agentic-qa), tracks tests, devises
                        parallel fix plans on failure
```

## What each layer does
- **Supervisor pattern (research & dev & QA all share it):** supervisor receives the task from the
  controller, decomposes + parallelizes to children, collects + aggregates their outputs, returns a
  synthesized result up. (Research today = `research_fleet.py`; dev builders = `project.py build`
  architect→builders→integrator; these are the seed.)
- **Adaptive controller decisions — the key upgrade.** The controller must REASON about results and
  remediate, not just advance a fixed state machine. Examples the owner gave:
  - a child researcher lacked web access → controller diagnoses it, enables the capability, and RESTARTS
    the research fleet, then re-collects.
  - once findings are in and coordinated with the human → finalize the plan → launch the dev fleet.
  Every such decision (diagnose / remediate / when-to-ask-human / spawn-which-fleet) is an AI call.

## What's genuinely NEW (the gap to build — most structure already exists)
Building blocks TODAY: `research_fleet.py` (supervisor+children), `design_fleet.py`, `loopcontroller.py`
(per-org state machine), `factory.py` (roles + spawn), `orchestrator.py` (agent↔agent `conversations`).
The NEW, missing layer is **dynamism + adaptivity**, all AI-driven:
1. **Dynamic spawning on demand** — a supervisor that judges "I need a new agent for X" and spawns it
   mid-flight (not a fixed fan-out). The controller/supervisor decides *how many* agents an AI call.
2. **Inter-agent clarification + context-sharing** — a supervisor-dev with a doubt about a child's
   external-API finding can open a clarification exchange with that child, and PROPAGATE the updated
   context to the other child devs (so a correction reaches everyone). Needs a real message-passing +
   shared-context fabric on top of `orchestrator.conversations`.
3. **Self-diagnosing / self-remediating supervisors & controller** — detect a capability gap or a stuck
   agent, decide the fix (enable a tool, re-brief, restart the sub-fleet), and re-run.
4. **Coordinator-as-QA-driver** — the coordinator runs the `build-agentic-qa` loop, tracks per-story test
   state, and on failure devises a *parallelized* fix plan (which devs, which files) and dispatches it,
   looping until zero bugs.

## Execution model: event-driven actors + hierarchical escalation (NOT barriers)
The single biggest shift from today's fleets (which fan out → wait for ALL children → aggregate). Every
agent is an **autonomous actor** with its own decide-loop, and the system is **asynchronous + interrupt-driven**:
- **Any agent can emit an event at any time** — not only at completion. Event kinds: `done`,
  `next?` ("what do I do next"), `blocked` (e.g. "no API creds for X"), `finding`, `question`,
  `need-agent`, `need-context`. Each emission is the agent making an AI decision about its own state.
- **Supervisors are interrupt-driven.** The instant a child's event arrives — *even while other children
  are still running* — the supervisor makes an AI decision: **resolve it locally** (unblock, re-brief,
  spawn a helper, hand the child its next task, broadcast a correction to siblings) **or ESCALATE** to the
  controller if it's beyond the supervisor's capability.
- **The controller is the top escalation tier** — resolves what supervisors can't (enable a capability,
  restart a sub-fleet, ask the human), then the work resumes without a global stop.
- **No idle waiting:** a fast child that finished doesn't block on a slow sibling; it asks "next?" and the
  supervisor keeps it busy. A blocked child doesn't stall the fleet; its event routes up immediately.
- Mimics a human org: IC hits a blocker → pings lead mid-sprint → lead unblocks or escalates to director;
  a correction one IC discovers is propagated to the others so nobody works off stale context.

Implementation implication: a real **async message bus + per-actor inbox** on top of
`orchestrator.conversations`, with each actor running a decide-loop that interleaves "do my work" with
"handle incoming messages," and supervisors subscribing to their children's event streams — replacing the
current `parallel()`-barrier fan-in for these fleets.

## Recursive, elastic org scaling (the tree grows itself)
The hierarchy is NOT fixed-depth — it is recursive and self-scaling, like a real company growing:
- **Any supervisor can spawn sub-supervisors**, which spawn their own children — arbitrary depth and
  width. Example: a `fintech` super-supervisor over a **Pay team** and a **Wallet team**, each with a
  head (Head of Pay, Head of Wallet) over its own devs; add a **Payments-Fraud team** later and the
  fintech supervisor just spawns another head. An IC can become a lead; a lead can spin up a team.
- **Org-structure decision at EVERY prompt/decision point** (an AI call): *"is the current team structure
  sufficient for this scope, or do we need to expand — a new supervisor, a new sub-team, more agents?"*
  **Bias toward expansion/scalability** when uncertain (cost is not a constraint).
- **Escalation + events work at every level** (§ execution model): IC → team-head → domain-supervisor →
  controller → human. A blocker or correction routes up only as far as needed and resolves there.
- **No fixed depth or width.** The system decomposes a large vision (e.g. "Google-scale fintech suite")
  into a deep tree of domains → teams → devs, elastically, and restructures as functionality grows.
- Goal restated: **one prompt (a vision) → a full, working, bug-free app** — a self-organizing org of
  agents that scales itself to whatever the vision demands.

## Design principles
- Every state decision = an AI call (maximally agentic; cost is not a constraint).
- Supervisors are recursive: a child can itself become a supervisor for a sub-decomposition.
- All agent↔agent comms + shared docs are logged (like the InvoiceFlow PLAN.json + handoff notes) so the
  whole run is auditable — the owner can inspect who did what and what was shared.
- Scale beyond this example: the pattern must hold for far more complex visions than the illustration.

## Sequencing
1. **In flight:** `build-agentic-qa` (browser-bridge + explorer + story-gen + dev-loop + report + qa_run).
2. **Next:** the adaptive controller + dynamic-spawning supervisors + inter-agent clarification/context
   fabric described above, with the coordinator driving the QA loop from step 1.
