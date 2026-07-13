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
| **The generic pattern for ANY function** + a FULL multi-function company org (CEO-coordinator → function coordinators → tool-worker teams → reports up) | `orchestra/company.py`, `tools.py` (research/finance), `_coordinator_specs` company+generic branches | ✅ this session (offline-proven) |
| **Human-in-the-loop escalation** (a coordinator consults the CEO when it's truly a person's call) | `run_org(human_hook=...)`, `_CONTROLLER_PROMPT` consult_human | ✅ |

So: coordinators, comms, visibility, 92 functions' charters, org-planning, CEO routing, CEO reporting — **all
present.** The org can already be *planned* for any company and *run* on a durable substrate.

## What's PENDING (depth + activation, in priority order)

### 1. Give each function REAL work (generalize the tool-worker pattern) — IN PROGRESS
✅ DONE (`820d9a2`): the pattern is now GENERIC — any coordinator whose `memory.context` declares a `tool` +
a list of `items` spawns one tool-worker per item (`runtime._coordinator_specs` generic branch). Tools in
`orchestra/tools.py`: `research` (web-grounded, cited), `finance_report` (CEO financial report), and
**`knowledge_work`** — a catch-all that runs ANY of the 92 role charters (product-manager spec, legal review,
strategy, analysis) as a real deliverable. So EVERY function can be staffed today: structured ones
(qa/dev/research/finance) with specialized tools, everything else via `knowledge_work` +
`worker_role=<role>`. **Remaining: specialized tools where a function needs a real EXTERNAL action** (legal
doc-scan against policy, live data queries, marketing/design asset generation, connectors) — each is one
`run_tool` entry, same `_agent_tool`/tool-worker shape. Original note:
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

### 2. Turn the orchestra engine ON by default, at parity — DONE (default) + live-start verified
✅ The orchestra engine is **already ON by default** (`AOS_ORCHESTRA` defaults to "1"; `research.py orchestra_on()`)
— it's the production research engine via `loopcontroller`. ✅ A LIVE start of the full company org was verified
(real factory + real tools): `CEO-coordinator (working) → research-coordinator (working) → researcher
tool-worker (blocked = dispatch-and-parked, running the real tool)`. Remaining: a full live company run to
completion at parity (Codex — the long-running validation), and wiring `company.run_company_org` as a
`loopcontroller` production callsite for arbitrary CEO directives (today `loopcontroller` routes the RESEARCH
phase to orchestra; extend to the multi-function company org).

### 3. Exercise + validate a FULL company run end-to-end (the integration proof)
✅ DEMONSTRATED OFFLINE (`1849dc0`): `scripts/orchestra/company.py` `run_company_org(vision, functions)` drives
a CEO-coordinator → function coordinators (research + finance) → tool-worker teams → reports aggregating up to
the CEO, `run=done`, reliably (stubs the tool + factory seams). The multi-level, multi-function, report-up
shape works on the real runtime. **Remaining = the LIVE run** (Codex): real research/finance tools + real AI,
a richer directive (e.g. *"launch product X"* → product → research → finance → legal → build → QA/dev(done) →
launch), each coordinator reporting up, the CEO briefed via `digest` / consulted on the calls that matter.
Expect the same "under-stubbed/timing" fixes we made in the QA drive.

### 4. Deepen whole-org visibility + human-pattern CEO comms — MOSTLY DONE
✅ `pulse`/dashboard show every agent live; ✅ an **Org chart** panel on the dashboard (`124fc24`); ✅ the CEO
CONSOLE already has the living org chart (`orgview.py` area B2 → `store.org_tree`, `/api/org/live`); ✅
specialized external-action tools: `data_query` (`c2b1412`), `legal_scan` + `connector_ingest` (`e7111e6`) —
the org's tool set now spans qa/dev/research/finance/knowledge_work/data/legal/connectors; ✅ human-pattern
CEO reporting: the founder digest now includes a **"YOUR ORG (LIVE)"** section (`194af72`); ✅ a first-class
**DISAGREEMENT** posture (`15fa34b`) — an agent that judges its assignment wrong emits `disagree`, parks, and
the objection routes UP to the CEO tier to rule on (proceed/revise) via `human_hook`; ✅ **artifact-output
tools** (`b214251`): `produce_artifact` (writes real deliverable files — marketing copy, docs, reports) +
`design_asset` (SVG/HTML mockups). **Item 4 is essentially done.**

### 6. Arbitrary CEO directives → the org (loopcontroller callsite) — DONE
✅ `company.run_directive(directive)` (`cd6f2a7`) AI-plans the org functions a free-text directive needs, then
drives the company org. ✅ `loopcontroller.run_ceo_directive(directive)` (`338117b`) wires it into the CEO
controller as an ISOLATED callsite (records a controller_jobs row; does NOT touch the product-build phase
machine). Remaining: production should dispatch it async (it can be long-running) + surface its result in the
console; and the LIVE full run to completion (Codex).

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
