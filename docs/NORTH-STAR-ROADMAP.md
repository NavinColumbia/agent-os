# North Star Roadmap — from here to the full CEO-directed org

Answers: *"what's pending to have a full org (researchers, accounting, finance, legal, product, …) working
under CEO direction, with cross-org communication, visibility into everyone's actions, and multi-level
coordinators who own their subtree?"* Anchored to [`NORTH-STAR.md`](NORTH-STAR.md).

## The headline: the ARCHITECTURE for that org already exists. What's pending is DEPTH + ACTIVATION.

The hard part — a durable, recursive, multi-level agent org with coordinators, communication, and visibility
— is built. The QA/dev agentic org shipped this session is the **proven, end-to-end template** for one
function; the rest is largely *replicating that template* across functions + turning the engine on.

### What EXISTS today (grounded in the code)
| North-Star capability | Where it lives | State |
|---|---|---|
| **Recursive multi-level org** controller → domains → teams → workers, elastic (grows itself) | `orchestra/runtime.py`, `store.py`, `org_decider.plan_org` | ✅ built + selftested |
| **Multi-level COORDINATORS** who own their subtree (decompose, delegate, aggregate, escalate, resolve, broadcast) | `_supervisor_step` / `_controller_step` | ✅ |
| **Communication across the org** (task/done/blocked/finding/question/need_agent/escalate/resolve/context_update) | the durable event bus (`orchestra_events`) | ✅ |
| **Visibility into everyone's actions** — live "what is every agent doing, stuck?" + comms graph + directory + activity + tamper-evident audit | `pulse.py`, `dashboard.py`, `audit.py` | ✅ (pulse added this session) |
| **92 role charters** — researcher, accountant-bookkeeper, finance-cost-controller, legal-compliance-*, product-manager, group-product-manager, data-scientist/engineer/analyst, marketing, design, sales, … | `~/projects/control-plane/roles/*.yaml` (governance: can_spawn, capabilities) | ✅ the "job descriptions" exist |
| **Plan an org from a vision** (any business → domains → teams → roles, open vocab) | `org_decider.plan_org` | ✅ |
| **CEO → org directive routing** (production callsite; the orchestra IS the engine) | `loopcontroller.py` behind the `orchestra_on()` flag | ✅ wired, flag-gated |
| **CEO reporting** — founder's digest to your phone | `digest.py`, ntfy | ✅ |
| **A full function working END-TO-END** — QA/dev: coordinator → workers → real TOOLS → hand-off → fix → re-test → honest verdict → auditor → artifacts | `qa/qa_agentic.py` + `orchestra/{tools,jobrunner}.py` | ✅ this session (the template) |
| **Human-in-the-loop escalation** (a coordinator consults the CEO when it's truly a person's call) | `run_org(human_hook=...)`, `_CONTROLLER_PROMPT` consult_human | ✅ |

So: coordinators, comms, visibility, 92 functions' charters, org-planning, CEO routing, CEO reporting — **all
present.** The org can already be *planned* for any company and *run* on a durable substrate.

## What's PENDING (depth + activation, in priority order)

### 1. Give each function REAL work (generalize the tool-worker pattern) — the biggest chunk
Today most of the 92 roles run as **text-only AI workers** (a `factory.agent` decision). That's genuinely
enough for knowledge work (a PM writing a spec, a strategy memo, a legal *review* of a provided doc, a
finance analysis of provided numbers). But functions that need an **external action** need a TOOL — exactly
the **tool-worker pattern proven this session** (`orchestra/tools.py` + `jobrunner.py` dispatch-and-park):
- **researcher** → a web-research tool (search + fetch + synthesize; `deep-research` skill / `connectors.py`)
- **accountant / finance-cost-controller** → query real billing/metrics/usage → produce a report
- **finance-reporter** → assemble the CEO finance digest from real numbers (extend `digest.py`)
- **legal-compliance** → check a real doc/config against policy (like the security/governance scanners)
- **data-*** → run real queries; **QA/dev** → already done (browser + git-diff tools)
Each is: a role manifest (exists) + one `run_tool` entry + (optionally) a coordinator branch. ~a day each,
same shape as `qa_explore`/`dev_fix`.

### 2. Turn the orchestra engine ON by default, at parity
The org engine is flag-gated (`loopcontroller.orchestra_on()`), like `qa_run(agentic=True)` is opt-in. Prove
a real multi-team run at parity with the legacy "fleet", then flip the flag. (Same discipline as HANDOFF
phase 6-iii for QA.)

### 3. Exercise + validate a FULL company run end-to-end (the integration proof)
Drive one real CEO directive through a *multi-function* org: e.g. *"launch product X"* → product-manager
coordinator specs it → research team gathers market intel → finance projects cost/price → legal reviews →
build team builds → QA/dev org (done) verifies → launch — with each coordinator **reporting up** and the CEO
**briefed via digest / consulted on the calls that matter**. The substrate supports every arrow; this is the
first end-to-end *drive* + hardening pass (expect to find the same kind of "under-stubbed / timing" issues we
found and fixed in the QA drive).

### 4. Deepen whole-org visibility + human-pattern CEO comms in the CONSOLE
`pulse`/dashboard already show every agent; surface the **whole standing org tree** (all teams, every action,
each coordinator's subtree, live) in the CEO console (`console.py`, :8099) — not just the ops dashboard. Round
out the human-pattern comms: briefing, status-on-a-cadence, clarification, **disagreement**, hand-offs (some
in `loopcontroller`/`digest`/ask-await; make them cohesive and CEO-facing).

### 5. Standing org that persists across directives
Confirm the org is a **standing company** (persists, takes directives over time, grows/shrinks) vs ephemeral
per-run. `store` rows are durable and `loopcontroller` holds threads; verify/keep a long-lived org the CEO
directs continuously, with idle teams parked (zero token cost) until tasked.

## The one-paragraph truth
You don't need to build an org from scratch — **it's built.** Recursive coordinators, the message bus, live
visibility, 92 function charters, org-planning, CEO routing, and CEO reporting all exist, and this session
proved a full function (QA/dev) working end-to-end on it. To reach the North Star's *full* org, the work is:
(1) give the other functions real tools where they need external action — the exact pattern we just proved;
(2) flip the orchestra engine on at parity; (3) drive + harden one real multi-team company run end-to-end;
(4) deepen whole-org visibility + CEO comms in the console. It's breadth and hardening on a finished spine,
not new architecture.
