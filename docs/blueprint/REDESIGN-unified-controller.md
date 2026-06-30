# REDESIGN — Unified Controller Console

Status: DECISION + BUILD PLAN (architect-approved). Conforms to `docs/STANDARDS-planning.md`
(SCOPE · IMPACT MAP · INVARIANTS · EDGE CASES · PARALLELIZATION · DONE CHECKLIST).

Synthesizes five research streams: controller-arch, ia-ux, agent-model, auth-gating, monetization-ux.
All line numbers reference the current `scripts/*.py` and were re-verified against the live tree.

---

## 1. THE ONE-LINE DECISION

Ship **one chat front-door** — a renamed, account-aware **"Assistant"** — backed by the existing
per-org `loopcontroller.py` state machine. The org becomes an **inline, searchable context selector**
on the Assistant, not a precondition. We **retire the duplicate front-doors** (the standalone
`orchestrator` "Quick build" nav item and the orphaned `build` form) and **gate every model call on
consent + a resolved tenant provider** before any spend. System agents stay **read-only**, custom
agents stay **editable**, agentic features stay a **curated catalog** — surfaced explicitly instead of
hidden.

We do **NOT** scrap `loopcontroller`. It is the crown jewel (durable, crash-recoverable,
consent/quota-gated `DISCOVER→…→DELIVER` with idempotent `advance()` / `resume_stalled()`). The clash
was never controller-vs-assistant; it was **three entry points** (per-org controller chat, tenant-level
orchestrator chat, static build form). We collapse the three surfaces into one and keep the engine.

---

## 2. TARGET MODEL

```
Account (auth.py → tenant_id)
└── Assistant            ← THE single chat surface (console JS key stays 'controller')
      org selector: "All orgs (home)" | <specific org>   ← inline searchable dropdown, switches in place
      • home thread  (org=0): router / portfolio Q&A / "create company" / quick throwaway build
      • org thread   (org=N): loopcontroller.thread_for_org(tid, N)  ← unchanged 9-phase engine
            └── governed fleets (research / design_fleet / qualityloop / verify / frontdoor)
```

- `org=0` means **home/router**; `org=N` means **the existing per-org controller thread** (today's
  behavior, unchanged). Same chat component, same persistence, same `ctlRender()`.
- A new thin **`assistant.py`** (~150 lines, no build logic of its own) owns the `org=0` home thread:
  classify intent → call `orgs.create()`, `crossorg`, or route into a `loopcontroller` org thread.
  For `org=N` the route delegates straight to `loopcontroller` as it does now.

### Reuse vs. replace (final calls)

| Module | Verdict |
|---|---|
| `loopcontroller.py` | **Reuse as-is** — the per-org engine. `thread_for_org(tid, org_id)` is the dispatch handle. Do not touch the state machine. |
| `orchestrator.py` | **Repurpose, don't delete.** It does two jobs: (a) chat persistence on `chat_messages` that `loopcontroller` depends on — keep as shared infra; (b) the quick-build clarify→propose→confirm assistant — demote to the home thread's "no-org throwaway build" branch behind `assistant.py`. |
| `orgs.py`, `crossorg.py`, `crossorgview.py`, `customagents.py`, `auth.py` | **Reuse.** Assistant calls `orgs.create()` instead of the `/orgs` form; crossorg backs portfolio Q&A. |
| `assistant.py` | **NEW (thin)** account-level router for the `org=0` home thread. |

---

## 3. THE FIVE DECISIVE CALLS

1. **Unified assistant, optional per-org context** (controller-arch + ia-ux + monetization Decision 1).
   One chat metaphor nested by scope: personal/cross-org assistant at top, per-org controller below.
   Kill the dual-chat split. `orchestrator` one-shot folds into the home thread; the `build` form
   leaves the nav.

2. **New IA + inline searchable org-switcher** (ia-ux). The topbar chip stops navigating away. Picking
   an org **switches context in place and re-renders the current view** — never forces a route change.
   New nav groups: WORKSPACE (Assistant=home, Cockpit, Projects, Design, Approvals, Activity) · BUILD
   (Agents, Agentic features, Templates) · PORTFOLIO (All orgs, Portfolio) · ACCOUNT (Billing,
   Providers, Integrations).

3. **Three explicit agent tiers, default-uneditable system agents** (agent-model). Surface
   System agents **read-only** (Controller + standard fleet roles, governed by
   `control-plane/roles/*.yaml`; `controller.yaml` is the only role with `can_spawn: true`). Custom
   agents stay editable (`ALLOWED_ROLES` allowlist + tenant-ownership checks). Agentic features stay a
   curated `CATALOG`. No change to who-can-touch-what — that's already structurally enforced; we only
   make it **visible**.

4. **Consent + provider gate before any model call** (auth-gating). Every tenant-facing AI action gates
   on **(a) consent and (b) a resolved provider** before the first `_llm`/`factory.agent` call, wires
   the tenant's provider into `factory._ctx` so spend lands on **their** account, and adds a
   `factory.agent` provider backstop symmetric to the existing consent backstop. Subscription login
   counts as resolved; nothing-connected does not.

5. **Monetization-driven UX** (monetization-ux). One front door is Decision 1 above. Real multi-seat +
   RBAC (replace the hardcoded `_team` stub at `console.py:123`) is **scoped here but sequenced as a
   follow-on epic** — it is the largest revenue unlock but is independent of the console collapse and
   must not block it. We land the unified surface + gating first, then RBAC on the same accounts/orgs
   spine reusing `governance.py` as the enforcement layer.

---

## 4. SCOPE

### In scope (this epic)
- Collapse three chat/build entry points to one Assistant surface (nav + routing).
- Inline searchable org-switcher that re-renders in place (`console.py` topbar + JS).
- `assistant.py` home-thread router (`org=0`); `org=N` delegates to `loopcontroller` unchanged.
- Consent + provider gate on `loopcontroller.say` and the home/quick-build path; `factory.agent`
  provider backstop; `tenantproviders.resolved()` + `apply_to_ctx()`.
- Make agent tiers explicit in the Agents/Build views (read-only system tier surfaced).

### Out of scope (named follow-on, not in this epic)
- Multi-seat members table + RBAC roles + invite flow + SSO/SCIM (monetization Decision 2). Tracked,
  sequenced next, depends only on the accounts/orgs spine — independent of this epic.
- Org-scoping `custom_agents.org_id` (column exists via `orgs._ensure()`, API ignores it).
- Manual-build affordance relocated inside Projects.

---

## 5. IMPACT MAP

Every file touched, with all callers of any changed signature/route enumerated.

### 5a. Frontend / IA — `scripts/console.py`
- **Line 351** topbar chip `onclick="go('orgs')"` → replace with searchable `orgsw` dropdown calling a
  new `pickOrg(id)`.
- **Line 376** `switchOrg(id)` currently does `...go('controller')` (forces route). New `pickOrg(id)`
  sets `ORG`, persists `aos_org`, reloads org list, and **re-renders CUR in place** (no `go()`).
  `switchOrg` stays as the "Open from All orgs" action (intentional navigation); callers at lines
  489, 633 unchanged.
- **Lines 371–374, 379** `ORG`/`ORGS`/`aos_org` plumbing and `resetSession()` — reused as-is.
- **Line 473 `VIEWS`** — rename `controller` label to **"Assistant"**, keep JS key `'controller'`
  (referenced by `CTLPOLL` 484, `ctlSend` 623, `ctlChoose` 627, `go('controller')` 376/627, view
  fetch 476). Remove `chat` (Quick build) and `build` from the nav list only; **do not** delete the
  endpoints.
- **Line 701** `if(!ORGS.length)go('orgs')` brand-new-CEO guide — keep; home becomes Assistant once an
  org exists.
- Nav regroup into WORKSPACE / BUILD / PORTFOLIO / ACCOUNT.
- Agents view (**line 540**) — render three tiers; system tier read-only (no create/run/toggle/delete
  controls), reading the standing role list from `orgview.ORG` hierarchy + manifests.

### 5b. Routing / endpoints — `scripts/console.py`
- **Keep** `/api/chat/*` (202–204) and `/api/build` (200) endpoints alive (orchestrator persistence +
  manual build still used by the home thread); only remove their nav entry points.
- **`/api/controller/say|choose`** (218–219) unchanged signatures; `loopcontroller.say` body changes
  internally (gating) but the route contract is identical.
- New route `/api/assistant/say` (or extend `/api/controller/say` with `org=0` semantics via
  `assistant.py`) — **decision: route `org=0` through `assistant.py`, `org=N` through `loopcontroller`
  inside the existing `/api/controller/say` lambda** to avoid a new contract. The lambda at 218 becomes
  `org=0 ? assistant.say(...) : loopcontroller.say(...)`.
- `ORG_SCOPED_POST` (line 95) and `ORG_SCOPED_GET` (92) — `_owns()` IDOR gating preserved; `org=0`
  (home) is the tenant's own account, already owned.

### 5c. Auth / provider gate
- **`tenantproviders.py`** — add `resolved(tid)` and `apply_to_ctx(tid)` after `build_kwargs`
  (line 153). `resolve` (126) and `build_kwargs` (148) unchanged; callers unaffected.
- **`loopcontroller.py`** — `say` (170): after the existing consent gate (188 `require_consent`),
  before any `_llm` call (582), call `tenantproviders.resolved(tid)`; if false return a
  `provider_required` prompt; if true `apply_to_ctx(tid)` so `_ctx` carries the tenant key. The early
  `factory._ctx.api_key = api_key` (177, always `None`) is superseded by `apply_to_ctx`. The late
  `PLAN_APPROVAL` resolve (402) stays as defense-in-depth.
- **`orchestrator.py`** — `say` (113): add `consent.require_consent` + provider gate + set
  `factory._ctx.tenant = tid` (currently never set → consent backstop bypassed) before
  `factory.agent` (124). `confirm` (201) already gates consent (208) — add the provider gate there too.
- **`factory.py`** — add a **provider backstop** in `agent()` symmetric to the existing consent
  backstop (keyed on `_ctx.tenant`): refuse tenant work on platform default when no provider resolved.
  Enumerate `factory.agent` callers: `orchestrator.say` (124), `loopcontroller` `_llm`/research
  fan-out, `customagents.run_now`, `agentfeatures`, `crossorg`. Backstop must accept
  `auth_mode='subscription'` as resolved so subscription logins are not blocked.

### 5d. New file — `scripts/assistant.py`
- Owns `org=0` home thread on `chat_messages` (reuse `orchestrator` persistence). Intents:
  create company (`orgs.create`), quick throwaway build (orchestrator one-shot, now gated), open/route
  to org (`loopcontroller.thread_for_org`), portfolio Q&A (`crossorg`). No build logic of its own.

### 5e. Monetization follow-on (named, not in this epic)
- `console.py:123 _team` stub → real members table; gate routes via `governance.py`. Independent spine.

---

## 6. INVARIANTS TO PRESERVE

1. **HTTP auth gate** — every route except `/api/signup`, `/api/login` resolves `X-Tenant-Token` or
   401s (`console.py:759-763, 789-793`). Unchanged. Read endpoints, signup/login, orgs, providers,
   consent, billing, onboarding stay **ungated** so onboarding is never bricked.
2. **Consent gate** — `loopcontroller.say` consent check (186-199) and the `factory.agent` consent
   backstop (436-447, keyed on `_ctx.tenant`) must still fire. We **add** a provider gate, never weaken
   consent.
3. **Spawn unforgeability** — `controller.yaml` remains the only role with `can_spawn: true`;
   `governance.enforce("controller","spawn")` on the build path unchanged. No user agent becomes a
   spawner.
4. **IDOR / org ownership** — `_owns()` gating on `ORG_SCOPED_*` preserved; `org=0` is the tenant's own
   home, already owned.
5. **Custom-agent boundary** — `ALLOWED_ROLES` allowlist + `tenant_id != tid` checks on
   `run_now/toggle/delete` unchanged. No row or route can mutate a Tier-A (system) agent.
6. **loopcontroller state machine** — durable `controller_jobs`, idempotent `advance()`,
   `resume_stalled()`, phase order untouched.
7. **selftest 111/0** must stay green; 15 quality lenses, audit/killswitch/spend-caps unchanged.

---

## 7. EDGE / EMPTY / ERROR / LOADING CASES

- **Zero orgs:** home Assistant (`org=0`) is the landing; "create company" intent works; existing
  `if(!ORGS.length)go('orgs')` guide preserved as fallback.
- **Org switcher empty / single org:** dropdown shows "All orgs (home)" always; with one org it
  defaults to it (line 374 logic reused).
- **Stale `aos_org` (deleted org):** line 373 already resets to 0 — keep.
- **No provider connected:** gate returns `provider_required` **before** any spend; UI deep-links to
  Providers (onboarding step 2). Subscription login (`auth_mode='subscription'`, no key) = resolved →
  allowed.
- **api_key connection with missing vault secret:** `resolved()` returns false → prompt reconnect,
  do not silently fall back to platform credentials.
- **Pre-consent text in quick-build:** now blocked (`orchestrator.say` gains consent gate + sets
  `_ctx.tenant`).
- **In-place org switch mid-poll:** `CTLPOLL` (484) re-targets `?org=`+ORG on next tick; switching
  must not orphan the interval (guard on `CUR`).
- **org=0 routed to loopcontroller by mistake:** route guard sends `org=0` to `assistant.py`, never to
  `thread_for_org(tid, 0)`.
- **Unauthorized / 401 mid-session:** existing `e.kind==='auth'` → `signOut()` path (462,467) reused.

---

## 8. PARALLELIZATION PLAN

**INDEPENDENT (fan out concurrently):**
- A. **Auth/provider gate backend** — `tenantproviders.resolved/apply_to_ctx`, `loopcontroller.say`
  gate, `orchestrator` gate, `factory.agent` backstop. Pure backend; no IA dependency.
- B. **IA / org-switcher frontend** — `console.py` topbar dropdown + `pickOrg()` + nav regroup +
  label rename. Pure frontend; routes unchanged.
- C. **Agent-tier surfacing** — read-only system tier in Agents view. Frontend + a read helper; no
  state mutation.

**ORDERED (sequential, with reason):**
- D. **`assistant.py` home router** depends on A (gating helpers exist) and on the route change in
  `console.py:218` (org=0 dispatch) — land A first, then D, then wire the route. B and C can land in
  parallel with A/D since they don't touch the gate or the router.
- E. **RBAC / multi-seat** — strictly after this epic; independent spine, separate epic.

---

## 9. DONE CHECKLIST (each item → proving guard)

- [ ] One chat surface in nav; `chat` + `build` removed from nav, endpoints still 200 →
      manual route check + nav render assertion.
- [ ] Org switcher re-renders in place (no route change) on pick → `pickOrg` does not call `go()`;
      UI smoke check.
- [ ] `org=0` routes to `assistant.py`, `org=N` to `loopcontroller` → unit assert on the `/api/
      controller/say` dispatch branch.
- [ ] `loopcontroller.say` refuses before `_llm` when `resolved(tid)` is false → new gate test;
      subscription login passes.
- [ ] `orchestrator.say/confirm` gate consent + provider and set `_ctx.tenant` → test that
      pre-consent / no-provider text never reaches `factory.agent`.
- [ ] `factory.agent` provider backstop refuses tenant work on platform default → governance/factory
      test; `auth_mode='subscription'` allowed.
- [ ] Tenant spend lands on tenant account (`apply_to_ctx` wires `_ctx`) → assert `_ctx` key set
      before model call.
- [ ] System agents render read-only (no mutate controls); custom agents still editable; agentic
      features curated → Agents-view render assertion + existing `ALLOWED_ROLES`/ownership selftest.
- [ ] Invariants hold: `controller.yaml` sole `can_spawn`, `_owns` IDOR gate, consent backstop, HTTP
      401 gate → existing guards + `selftest 111/0` green.
- [ ] No signature/route contract broken: `/api/controller/*`, `/api/chat/*`, `/api/build` callers
      enumerated in §5 all still valid.

---

## 10. SEQUENCING SUMMARY

1. Land **A** (gating backend) + **B** (IA/switcher) + **C** (agent tiers) in parallel.
2. Land **D** (`assistant.py` + `org=0` route dispatch) on top of A.
3. Verify against §9 + `selftest`.
4. Open **E** (RBAC/multi-seat) as the next epic on the same accounts/orgs/governance spine.
