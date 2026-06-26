"blueprint": "This is a synthesis task — I have all 8 reviews and need to produce one blueprint. No file exploration needed; the reviews are already grounded with specific refs. Let me write the blueprint directly.

# agent-os Implementation Blueprint: The Closed-Loop Controller

This synthesizes 8 dimension reviews into one buildable plan. The thesis across all reviews is consistent: **the primitives exist and are good (research fleet, build engine, verifier, durable mailbox, notifications, tenant providers), but they are orphaned silos.** No state machine stitches them into the vision's closed loop, and there is no org layer above the flat tenant→product model. This blueprint defines the spine that connects them.

---

## 1. The Closed-Loop Controller STATE MACHINE

One state machine per **org**, owned by `controller.py`, persisted on `controller_state`. The controller is the *only* thing that advances phases. `advance(thread_id)` is **idempotent and event-driven** — safe to call from a chat turn, a fleet-completion callback, or the crash-recovery sweeper.

The gate flag `awaiting ∈ {null, user_feedback, user_approval, credentials, fleet}` blocks transitions. When `awaiting != null`, `advance()` is a no-op until the gate clears.

| # | Phase | Entered when | Controller dispatches | Posted to chat (`awaiting`) | Gate → exit |
|---|---|---|---|---|---|
| 0 | `DISCOVER` | org/thread created | LLM clarify loop (ONE question/turn), accumulates `brief` JSONB | Clarifying Qs (`user_feedback`) | Controller LLM emits `[[RESEARCH]]` → `RESEARCH` |
| 1 | `RESEARCH` | brief complete | `controller_jobs(kind=research)` → `research.start()` → `research_fleet.research()` in worker; live subq progress | "Give me time to research…" + live "(4/8)" pill (`fleet`) | job `done` → `OPTIONS` |
| 2 | `OPTIONS` | research job done | `research._extract_options()` → strict-JSON option cards, one `recommended` | Option tiles, "Which direction fits?" (`user_approval`) | user `/choose` → `DEEP_DESIGN` |
| 3 | `DEEP_DESIGN` | option chosen | LLM emits `[[PLAN]]`: tech design + user stories + feature DAG → store `plan` | Design summary, iterate (`user_feedback`) | user "looks good" → `PLAN_APPROVAL` |
| 4 | `PLAN_APPROVAL` | design accepted | `tenantproviders.resolve(tid)`; if creds missing → `agent_request.ask(kind=credential)` | "Final plan + I need cloud creds" (`user_approval` / `credentials`) | approved & creds present → `PROTOTYPE` |
| 5 | `PROTOTYPE` | plan approved | `controller_jobs(kind=design)` → `design_fleet.prototype(plan)`; emits screens per `surface` (cockpit/team/external) | "Designing screens…" → gallery per round (`fleet`→`user_feedback`) | approved → `IMPLEMENT`; iterate keeps phase |
| 6 | `IMPLEMENT` | prototype approved | `controller_jobs(kind=build)` → `qualityloop.run(product, bar, api_key=tenantproviders.build_kwargs(tid))` | "Building…" (`fleet`) | bar met & SHIPPED → `TESTQA`; `ASK_USER` → `user_feedback` |
| 7 | `TESTQA` | build SHIPPED-candidate | `qualityloop` climb-to-bar: `verify.verify(product, rigor)`, escalate rigor until `_bar_met`; on stuck → `askuser.ask` | "Tested X, blocked on Y, need you to Z" + **urgent push** (`user_feedback`) | green & user-confirmed → `DELIVER` |
| 8 | `DELIVER` | QA green + accept | `appregistry.register`, `frontdoor._zip`, `orgs.record_artifact` | "Done — download in Projects" (`null`) | terminal; `org.phase='operate'` |

**Cross-org operations** (`merge`, `steal_feature`) re-enter as a **new thread** on a new/target org, driven by `crossorg.py` through its own `xorg_ops` state machine (`PROPOSED → SCOPING → PLANNED → AWAIT_APPROVAL → EXECUTING → INTEGRATING → DONE | BLOCKED | REJECTED`), gated by the existing `approvals` inbox.

**Durability:** every async dispatch is a `controller_jobs` row (not a bare daemon thread). `controller.resume_stalled()` (run from `scheduler.py`) finds jobs `status='running'` with a dead worker, or `awaiting='fleet'` whose job is `done` but phase didn't advance, and calls `advance()`. This replaces the fire-and-forget `_run_and_report` thread that dies with the process.

**Controller functions:** `start(tid, org_id)` · `say(tid, thread_id, msg)` (phase-specific system prompt + sentinel block, one generic `_parse_block(text, tag)`) · `choose(tid, thread_id, option_id)` · `advance(thread_id)` (the transition fn) · `_dispatch(thread_id, kind)` (insert job + spawn worker + on-done `advance`+`_report`) · `_report(thread_id, text, meta, urgent)` (= `orchestrator.post` + `notifications.send`) · `resume_stalled()`.

---

## 2. The Multi-Org Data Model

The atom becomes the **org**. `tenant = the human owner`; an org is a container of products + research + design + dev/test + one controller thread + budget. One personal assistant spans all orgs; one controller per org.

### New tables

```sql
-- 40-orgs.sql
CREATE TABLE orgs (
  org_id      TEXT PRIMARY KEY,                         -- 'o-'+token_hex(5)
  tenant_id   TEXT NOT NULL REFERENCES tenants(tenant_id),
  name        TEXT NOT NULL,
  slug        TEXT NOT NULL,
  vision      TEXT,                                     -- "build a YouTube competitor"
  phase       TEXT NOT NULL DEFAULT 'intake',           -- intake|research|planning|design|build|operate
  status      TEXT NOT NULL DEFAULT 'active',           -- active|archived|merging
  controller_thread_id BIGINT,                          -- FK chat_threads.id
  cockpit_layout JSONB DEFAULT '{}',                    -- CEO-customizable widgets
  budget_usd  NUMERIC DEFAULT 0,
  created_at  TIMESTAMPTZ DEFAULT now(),
  archived_at TIMESTAMPTZ,
  UNIQUE (tenant_id, slug)                              -- slug unique WITHIN a CEO
);
CREATE INDEX orgs_tenant_idx ON orgs (tenant_id) WHERE status='active';

CREATE TABLE org_phase_events (
  id BIGSERIAL PRIMARY KEY, org_id TEXT NOT NULL REFERENCES orgs,
  phase TEXT, entered_at TIMESTAMPTZ DEFAULT now(), actor TEXT
);

-- the ONE place an org's full context is enumerable → enables cross-org ops
CREATE TABLE org_artifacts (
  id BIGSERIAL PRIMARY KEY, org_id TEXT NOT NULL REFERENCES orgs,
  kind TEXT NOT NULL,        -- research_report|plan|design|product_repo|spec|qa_report
  product TEXT, path TEXT, ref TEXT, summary TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX org_artifacts_org_idx ON org_artifacts (org_id, kind);

CREATE TABLE org_members (                               -- human-team surface (retires _team() stub)
  org_id TEXT NOT NULL REFERENCES orgs, tenant_id TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'member',                  -- ceo|admin|member|viewer
  status TEXT NOT NULL DEFAULT 'active',
  PRIMARY KEY (org_id, tenant_id)
);

CREATE TABLE design_artifacts (                          -- prototype gallery + 3-audience split
  id BIGSERIAL PRIMARY KEY, org_id TEXT NOT NULL REFERENCES orgs,
  kind TEXT, surface TEXT,                              -- surface: cockpit|team|external
  title TEXT, image_path TEXT, status TEXT DEFAULT 'draft',  -- draft|review|approved
  created_by TEXT, created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE recommendations (                           -- cross-org data-driven recs
  id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, org_id TEXT,
  kind TEXT, title TEXT, body TEXT, score NUMERIC,
  evidence JSONB DEFAULT '{}', dismissed_at TIMESTAMPTZ, created_at TIMESTAMPTZ DEFAULT now()
);
```

### Controller state + jobs (orchestration layer)

```sql
CREATE TABLE controller_state (
  thread_id BIGINT PRIMARY KEY, tenant_id TEXT, org_id TEXT,
  phase TEXT NOT NULL DEFAULT 'DISCOVER',
  brief JSONB, options JSONB, chosen_option JSONB, plan JSONB,
  research_report TEXT, prototype JSONB, product TEXT,
  awaiting TEXT,                                         -- null|user_feedback|user_approval|credentials|fleet
  updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE controller_jobs (
  id BIGSERIAL PRIMARY KEY, thread_id BIGINT, tenant_id TEXT,
  phase TEXT, kind TEXT,                                 -- research|design|build|qa
  status TEXT DEFAULT 'running', result JSONB,
  started_at TIMESTAMPTZ DEFAULT now(), finished_at TIMESTAMPTZ
);
```

### How existing tables get `org_id` (additive ALTERs + backfill)

```sql
ALTER TABLE tenant_products ADD COLUMN org_id TEXT REFERENCES orgs(org_id);
ALTER TABLE chat_threads    ADD COLUMN org_id TEXT REFERENCES orgs(org_id),
                            ADD COLUMN kind TEXT NOT NULL DEFAULT 'controller'; -- assistant|controller
ALTER TABLE chat_messages   ADD COLUMN org_id TEXT;
ALTER TABLE custom_agents   ADD COLUMN org_id TEXT;
ALTER TABLE findings        ADD COLUMN org_id TEXT;
ALTER TABLE directory       ADD COLUMN org_id TEXT;     -- fast per-org roster
-- comms/scale fabric (Review 5): org-scoped routing
ALTER TABLE tasks           ADD COLUMN org_id TEXT;
ALTER TABLE conversations   ADD COLUMN org_id TEXT;
```

**Backfill (one-shot):** create one default org per existing tenant; stamp every `tenant_products` row (then `SET NOT NULL`), and every `directory`/`findings`/`custom_agents`/`chat_threads`/`chat_messages` row via the `product → org` join. Attach the existing global chat thread to the default org.

**Product naming fix:** mint as `f"{org.slug[:8]}-{user_slug}"` so collisions are org-scoped, not a global 6-char gamble (Reviews 3, 4).

### Personal-assistant vs per-org-controller

- **Personal assistant** = one tenant-level thread (`chat_threads kind='assistant', org_id NULL`). System prompt fed by `orgs_of(tenant_id)` (portfolio level). Job: create/switch/archive orgs, "how are all my orgs doing", launch cross-org ops. Action block `[[NEW_ORG]] name / vision [[/NEW_ORG]]` → `orgs.create_org`.
- **Per-org controller** = `chat_threads kind='controller', org_id=…`. Drives the §1 state machine. Its state brief is `orgs.context_brief(org_id)` (this org's products, latest research summary, plan, design status, open findings, spend) — the per-org analogue of `_state_brief`.

---

## 3. New Modules to Build

| Module | Purpose | Key functions | DB tables | Console routes / views |
|---|---|---|---|---|
| `controller.py` | The closed-loop state machine (§1). Owns phase transitions; replaces the 2-state orchestrator stub | `start` `say` `choose` `advance` `_dispatch` `_report` `resume_stalled` | `controller_state`, `controller_jobs`, +`chat_threads.phase` | `/api/chat/say` `/api/chat/choose` `/api/chat/state` |
| `orgs.py` | Org-layer API (mirrors `tenancy.py`) | `create_org` `orgs_of` `org_for_product` `products_of_org` `owns_org` `set_phase` `register_product` `record_artifact` `context_brief` `set_cockpit_layout` | `orgs`, `org_phase_events`, `org_artifacts` | `/api/orgs` `/api/orgs/new` `/api/org/summary` `/api/org/phase` `/api/org/cockpit_layout` |
| `research.py` | State+options layer over the existing engine; async kickoff, tenant/thread scoping, prose→selectable options | `start` `_run` `_extract_options` `select` `run_state` | `research_runs`, `research_subq`, `research_options` | `/api/research/start` `/api/research/state` `/api/research/select` |
| `design_fleet.py` | Prototype fleet (clone of `research_fleet.py`); decompose plan→screens, parallel `frontend-engineer`/`ux-designer`, emit by `surface` | `prototype(plan, out_dir, api_key)` | writes `design_artifacts` via `orgs.record_artifact` | (via controller PROTOTYPE phase) |
| `qualityloop.py` | Unified climb-to-bar dev→test→qa→verify loop composing `factory`+`verify`+`improve`; the "iterate until perfect" engine | `run` `resume` `_measure` `_bar_met` (progress detector) | `quality_runs`, `quality_measurements`, `build_outcomes` | (via controller IMPLEMENT/TESTQA) |
| `askuser.py` | "Help me when stuck" — pauses loop, posts question to controller chat, resumes on answer | `ask` `answer` | `ask_user_requests` | (lands in chat via `orchestrator.post`) |
| `agent_request.py` | Tenant/phase-scoped durable, push-notifying, reply-resumable agent→human request (DBOS workflow, `workflow_id==request_id`) | `ask` `wait`(workflow) `answer` `open_requests` `get` | `agent_requests` | `/requests/ask` `/requests` `/requests/<id>` `/requests/<id>/answer` |
| `push.py` | Per-tenant push transport (fills the dead `push` pref); ntfy topic at signup | `send` `register` | `push_targets` | `/push/register` |
| `recommend.py` | Data-driven strategy recommender (generalizes `estimate.py` cost→strategy/quality) | `recommend_strategy` `recommend_next` `feedback` | reads `build_outcomes`; writes `recommendations` | feeds `next_steps` + `/api/x/recommendations` |
| `crossorg.py` | Cross-org merge/steal-feature engine; read-only scope fleet over source, governed execute | `authorize` `scope_port` `scope_merge` `propose` `execute` | `xorg_ops`, `org_lineage`, `crossorg_grants` | `/api/xorg/propose` `/api/xorg/<id>`; approval via existing inbox |
| `crossorgview.py` | Cross-org user surface (one user's many orgs) | `portfolio` `analytics` `failures` `recommendations` | reads above + `org_metrics` | `/api/x/portfolio` `/api/x/analytics` `/api/x/failures` |
| `designview.py` | Prototype gallery + 3-audience split (cockpit/team/external) | `gallery` `submit` `decide` | `design_artifacts` | `/api/design` `/api/design/decide` |
| `teamview.py` | Human-team surface (retires `_team()` stub) | `members` `invite` `set_role` | `org_members` | `/api/org/team` `/api/org/team/invite` |
| `alerts.py` | Monitoring-agent→agent alert routing (deduped, owned, escalatable) | `raise` | `alerts` (`UNIQUE(signature) WHERE open`) | wired from `monitor.py`/`watchdog.py` |
| `pool.py` + `worker.py` | Off-box distributed workers on the existing SKIP-LOCKED queue; makes `scale.cloud-pool` real | `worker.loop` `pool.autoscale` `pool.reap_dead` | `workers` | (cron + cloud provider) |

**Surgical edits (not new modules):** `orchestrator.py` becomes the chat I/O layer (`post`/`history`) + assistant `say`; `project.py` gains pure `union_plans`/`detect_collisions`/`slice_feature`; `factory.build_product` calls `verify.verify` after REVIEW when `rigor>=2`; `orchestrate.pick_assignee` scores `quality≫load≫cost` from a new `agent_scores` table; `cockpit`/`billing.usage`/`orgview`/`projectsview`/`livestatus` gain an optional `org_id` param (per-org vs cross-org switch — one substitution); `console.py` NAV becomes an org-switcher + All-orgs/per-org scope toggle.

---

## 4. Prioritized Build Order

Ordered by **quality-enabling + foundational first**. All items 1–8 are **buildable now** (no user accounts needed — single-tenant dev tenant suffices). Items 9–11 are partially **gated on real user auth/credentials** (subscription login to Anthropic/ChatGPT, cloud creds) for full value but can be built and tested with BYO keys.

1. **Wire `verify` into the default build path** — S. *Unlocks:* the strongest quality lever, on by default (today off unless `AOS_RIGOR>1`). Make `factory.build_product` call `verify.verify(product, rigor)` after REVIEW when `rigor>=2`, gate LAUNCH on it. Cheapest big quality win, pure existing code. **Buildable now.**

2. **Org layer: `40-orgs.sql` + `orgs.py` + backfill** — M. *Unlocks:* everything multi-org; the container the whole vision hangs off. `create_org/orgs_of/owns_org/products_of_org/register_product/record_artifact/context_brief` + selftest. Backfill one default org per tenant. **Buildable now.**

3. **`controller.py` state machine (DISCOVER→DELIVER skeleton) + `controller_state`/`controller_jobs`** — L. *Unlocks:* the closed loop itself; durable async (replaces dying daemon thread). Port `_run_and_report`→`_report`; phase→prompt+sentinel table; `advance` transition fn; `resume_stalled` into `scheduler.py`. **Buildable now** (stub fleets first).

4. **`research.py` + refactor `research_fleet.research(progress, repo)` + options model** — M. *Unlocks:* RESEARCH→OPTIONS phases; "give me time to research" → selectable cards. Connects the orphaned fleet. **Buildable now.**

5. **`qualityloop.py` + `build_outcomes`/`quality_runs`/`quality_measurements`** — L. *Unlocks:* "iterate until perfect"; climb-to-bar composing factory+verify+improve; the learning store. Wire into IMPLEMENT/TESTQA. **Buildable now.**

6. **`agent_request.py` + `push.py` + wire `reply_listener` → `DBOS.send`** — M. *Unlocks:* proactive push + credential asks + reply-resume; the "we need X to continue" loop. Wires the orphaned `approval_gate` pattern, tenant/phase-scoped. **Buildable now** (push transport via per-tenant ntfy topic).

7. **`askuser.py`** — S. *Unlocks:* "ask the user to do hard things" mid-loop; pauses `qualityloop`, posts to controller chat, resumes on answer. **Buildable now.**

8. **`design_fleet.py` + `designview.py` (3-surface split)** — M. *Unlocks:* PROTOTYPE phase; prototype gallery for cockpit/team/external audiences. Clone of `research_fleet`. **Buildable now.**

9. **`recommend.py` (generalize `estimate.py`)** — M. *Unlocks:* data-driven strategy ("rigor 3 ships 92% clean"); called from controller before IMPLEMENT. Needs accumulated `build_outcomes` to be useful → low confidence until data exists. **Buildable now, value grows with usage.**

10. **Console IA: org switcher + scope toggle + `crossorgview.py` + `teamview.py`** — L. *Unlocks:* per-org vs all-orgs pages, phase tracker, human team. Re-scope existing views by `org_id`. **Buildable now; team RBAC gated on multi-seat user accounts.**

11. **Scale + cross-org ops: `alerts.py`, `agent_scores`/`pick_assignee`, `pool.py`+`worker.py`, `crossorg.py`+`crossorgview` failures** — L. *Unlocks:* monitoring→agent alerts, quality-weighted routing, off-box "millions of consultants", merge/steal-feature. Build last; depends on org layer + manifest. **Routing/alerts buildable now; distributed pool gated on cloud creds/accounts.**

---

## 5. The First Implementable Slice (the spine)

**Goal: one thin end-to-end pass through the real closed loop on a dev tenant**, proving every gate and the durable-async pattern, with stub-but-real fleets where the heavy ones aren't wired yet.

Build, in order:

1. **`40-orgs.sql` + `orgs.py`** (item 2) with backfill + selftest. Gives a real org to attach a controller thread to.
2. **`controller.py`** (item 3) with `controller_state`/`controller_jobs`, the full phase table, `advance`, `_dispatch`, `_report`, `resume_stalled`. Wire `/api/chat/say` → `controller.say`, add `/api/chat/choose` and `/api/chat/state`.
3. **`research.py`** (item 4) wrapping the existing engine — this is the *one* real fleet in the slice, proving async kickoff → progress → options → user pick.
4. **Stub the other phases** with minimal real implementations: DEEP_DESIGN emits a `[[PLAN]]` JSON from the LLM (no fleet); PROTOTYPE posts a one-screen placeholder; IMPLEMENT calls existing `project.build_complex` (or `qualityloop` if item 5 is done) on a tiny product; TESTQA runs `verify.verify`.
5. **`agent_request.ask` at PLAN_APPROVAL** (item 6, minimal) — fire a real proactive push ("I need cloud creds") and resume on answer, proving the credential gate + push + reply-resume end-to-end.

**Acceptance (a `controller._selftest` mirroring `orchestrator._selftest`):** mock `factory.agent` + the fleets to drive **DISCOVER → RESEARCH → OPTIONS → DEEP_DESIGN → PLAN_APPROVAL → PROTOTYPE → IMPLEMENT → TESTQA → DELIVER**, asserting (a) `phase` advances *only* on the correct trigger, (b) each `awaiting` gate blocks `advance()` until cleared, (c) a killed worker mid-RESEARCH is recovered by `resume_stalled()` and the phase still advances, (d) the PLAN_APPROVAL credential ask posts a notification and resumes on `answer`.

This spine makes the closed loop **real and durable end-to-end** with exactly one heavy fleet (research) fully wired; design_fleet, qualityloop's full climb, recommend, cross-org, and distributed scale then slot into the already-proven phase/gate/job machinery without re-architecting.

Key files to create: `/home/swami/projects/agent-os/postgres/initdb/40-orgs.sql`, `/home/swami/projects/agent-os/scripts/orgs.py`, `/home/swami/projects/agent-os/scripts/controller.py` (replace stub), `/home/swami/projects/agent-os/scripts/research.py`, `/home/swami/projects/agent-os/scripts/agent_request.py`. Key files to edit: `/home/swami/projects/agent-os/scripts/orchestrator.py` (→ chat I/O + assistant), `/home/swami/projects/agent-os/scripts/research_fleet.py` (add `progress`/`repo` params), `/home/swami/projects/agent-os/scripts/console.py` (chat routes + org switcher), `/home/swami/projects/agent-os/scripts/scheduler.py` (add `resume_stalled`), `/home/swami/projects/agent-os/scripts/factory.py` (wire `verify` into default path)."
  }
}