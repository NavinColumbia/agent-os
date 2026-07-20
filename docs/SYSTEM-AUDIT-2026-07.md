# agent-os — Full System Audit (2026-07-18)

A ground-up read of **all** code (~130 Python files, ~44k LOC, 52 SQL migrations) and all 23 docs,
measured against `NORTH-STAR.md` ("every user is a CEO running AI-agent companies; astonish a skeptic;
zero bugs reach a human; nothing fails invisibly; sellable for hundreds of millions"). Findings are
file:line-grounded and severity-ranked. Fixed items are marked ✅.

## Headline
The **engine is real and genuinely sophisticated** — durable, single-owner, crash-resumable, governed,
observable, with an agentic-org spine — and it is **well self-tested offline**. The gap to the North Star is
in three bands: (1) a cluster of **real correctness / security / billing bugs** a skeptic's due-diligence
would find; (2) the marquee **orchestra runtime is not actually the live path** and much is "offline-green,
never run live"; (3) **product UX + proactive comms** are thinner than the "astonish a skeptic" bar. The docs
(esp. HANDOFF) trail reality.

---

## Fixed during this audit
- ✅ **`loopcontroller.py` missing module-level `import time`** — `_llm`'s retry/backoff (line ~1811) threw
  `NameError` exactly when a model call timed out (stranding the chat turn). One-line fix.
- ✅ **Console `/download/<product>` unauth + IDOR + traversal** (console.py:1556) — returned any tenant's
  built repo to anyone, before auth. Ported frontdoor's auth + slug + `is_relative_to` + `tenancy.owns` guard
  (all failures → 404).
- ✅ **Pulse orphan reaper** (`pulse.reap_orphans()`) + wired into the watchdog — dead-process pulse ghosts
  (5–6 day old QA runs) no longer linger as `stalled` forever / page on nonexistent work.
- ✅ **Watchdog self-page after boot** — `tick()` now beats `watchdog` FIRST (its own liveness proof) so a
  slow/first tick can't flag its own heartbeat; selftest cleans up its `selftest-probe` row.

## Verified FALSE alarm
- `controller.py:41 import gate_check` resolves fine — `gate_check.py` lives in the **control-plane repo**
  (`~/projects/control-plane/scripts/`), by design. The LAUNCH gate exists. `controller.py` (DBOS path) has
  no live importer — dormant, not a live risk.

---

## A. Correctness / durability — the "nothing fails invisibly / loses nothing" bar
1. ✅ **FIXED — orchestra `_persist` is now atomic** (`store.persist_step`, runtime.py:199) — actor-update +
   all emits + event-completion land in ONE transaction. A crash before commit persists nothing (events
   reappear after their lease, step re-runs cleanly); no more strand-a-parent-forever. Runtime crash-resume
   selftest still lossless; new `test_orchestra_persist_step_is_atomic_and_validating`.
2. ✅ **FIXED — `say()` now advances under the drive lock** (`_advance_owned`, loopcontroller.py) — the 4
   interactive advance sites take `thread_drive_lock` exactly like jobd/resume_stalled, closing the
   interactive double-drive race.
3. ✅ **FIXED — dead parked worker reaped by pid immediately** (`_reap_dead_jobs` fast path) — a running job
   whose `worker_pid` is provably gone is reaped now (crash-resume kicks in) instead of pinning 'fleet' up to
   the 20-min floor. New `test_reap_dead_parked_worker_by_pid_before_floor`.
4b. ✅ **FIXED (found by the LIVE company-org run) — run_org self-heals abandoned claims** — a pool thread that claims an event then abandons it on stall left the org hung ~15 min waiting out the 900s claim lease. `store.release_stale_claims` (called at run_org startup; safe because run_org joins its pool before returning) frees orphaned claims in seconds. New `test_release_stale_claims_frees_abandoned_events`.
4. **At-least-once re-run duplicates side effects** — *largely subsumed by #1* (no more emitted-but-not-
   completed window on the decide loop). Residual: cross-PROCESS duplicate tool dispatch (jobrunner `_JOBS`
   is process-local) — lower priority, single-process is the current assumption. No idempotency key on emits.
5. 🟡 **ADDRESSED (offline, flagged) — research now has a crash-resumable `run_org` path** — new
   `research_org.run_research_via_org` + `research_subq` tool + `research-coordinator` branch, behind
   `AOS_RESEARCH_RUNORG` (default OFF). Offline-proven (contract preserved, crash-resume lossless, billing
   correct: `_selftest_via_org` + 2 unit tests). Remaining: the owner-gated live run, then flip default +
   delete the synchronous path. Plan: `docs/RESEARCH-RUNORG-REWIRE.md`.

## B. Observability — the "silence is a signal" bar
6. ✅ **FIXED — watchdog survives a DB outage** — `tick()` now probes Postgres first (DB-free) and, if it's
   down, pages out-of-band via ntfy (file-deduped, since the dedup table is in the dead DB) and returns
   instead of throwing. Announces recovery when the DB returns. New `test_watchdog_pages_out_of_band_when_db_down`.
7. **The pager itself can die silently** — `notify.send` is fail-open; a down ntfy drops every page with no
   out-of-band fallback, and nothing detects a dead ntfy except a module that pages through ntfy.
8. **In-stage build hangs invisible ~30min** — the controller beats once per stage at cadence 600; factory
   direct builds beat no pulse at all.

## C. Billing / cost — the "commercial" bar
9. ✅ **FIXED — Codex spend is now counted** — `_codex_cost` converts Codex's token counts to estimated USD
   (`CODEX_PRICE`, env-overridable) so `_run_once_codex` reports real spend instead of `0.0`; a platform
   failover now counts against the budget cap. New `test_codex_spend_is_counted_not_zero`.
10. **Default budget is unlimited** — `BUDGET_USD=0=unlimited` when the env is unset (the default). No global
    dollar stop on a default fleet run.
11. ✅ **FIXED — the `byo-key-no-platform-failover` invariant is now tested** — the load-bearing rule that a
    tenant outage can't bill the platform has no asserting selftest branch.

## D. QA integrity — the "zero bugs reach a human" bar
12. ✅ **FIXED (fail-open half) — auditor gate now fails CLOSED** (`_audit_unavailable`) — an exception in
    `review.review` downgrades a would-be pass to not-passed (an unverifiable run can't ship). New
    `test_qa_auditor_gate_fails_closed_when_audit_unavailable`. (The deliberate `AOS_QA_AUDIT_GATE=0`
    operator/offline-test override remains.)
13. ✅ **FIXED — finding re-verification faces the auditor** — qa_run gained an `audit_gate` param decoupled
    from finding-filing; `findings.verify` passes `audit_gate=True`, so a fixer's re-run clears the skeptical
    jury the original had to.
14. ✅ **FIXED — ship path now saturates the story SET** — `qa_run` calls `story_gen.saturate_stories`
    (generate → INDEPENDENT coverage judge in a different role → expand on named gaps, bounded, corpus-
    persisted with regression pins) instead of one un-judged `generate_stories`. `AOS_QA_SATURATE=0` +
    safe fallback preserved. This is the direct fix for the "50 actions, I'm out / needed 5,000" gap.

## E. Security / due-diligence — the "sellable for hundreds of millions" bar
15. **No Postgres RLS** — tenant isolation rests on every hand-written query remembering its predicate;
    `budget`/`appguard` already key on bare `product`. One forgotten WHERE = cross-tenant leak.
16. **Audit HMAC key co-located with the DB + captured in every snapshot** (snapshot.py:131) — an insider (or
    anyone who restores a `.aosnap`) can forge/rewrite the "tamper-evident" chain undetectably. Needs KMS/HSM
    or external notarization.
17. 🟡 **PARTLY FIXED — verify/reset codes now cap attempts** — `MAX_CODE_ATTEMPTS` (default 6) burns a code
    after too many wrong guesses (forcing a resend), bounding brute-force of the 6-digit code; a resend
    restores a fresh budget. New `test_auth_verify_code_locks_after_max_attempts`. *Still open:* login/signup
    response enumeration + PBKDF2→Argon2id.
18. ✅ **FIXED — consent is provider-aware** — `consent.for_tenant` resolves the tenant's ACTUAL provider
    (engine → OpenAI/Anthropic) and the gate/disclosure auto-use it; an OpenAI consent no longer satisfies an
    Anthropic gate. Every existing caller (passes no provider) became correct automatically. New
    `test_consent_names_the_tenants_actual_provider`. Closes the EU AI Act Art.50 / Apple 5.1.2(i) blocker.
19. **Ungoverned spawn path** — `providers.py`→`agent_worker.run_agent` runs `claude -p --permission-mode
    acceptEdits` with NO consent/budget/killswitch gate and NO cost capture. Any caller escapes every gate.
20. ✅ **FIXED — publish can't leak secrets** — `_GITIGNORE` now covers `.env*`/`keys/`/`secrets/`/`*.pem`/
    `*.key`/credentials, and publish unstages any secret-shaped file (`_is_secret_path`) before commit
    (fail-closed, even against a force-add/pre-tracked file). New `test_appregistry_never_publishes_secret_paths`.

## F. Product UX — the "astonish a skeptic" bar
21. ✅ **FIXED — cockpit first paint is now concurrent** — the 8 sequential `await get()` calls are one
    `Promise.all` (fault-tolerant, brief still async); no more multi-second phone spinner. Client JS
    node-syntax-checked; console selftest green.
22. **No proactive push** — the whole "briefed on the calls that matter" promise is poll-only (5–15s while a
    tab is open); a CEO who closes the tab hears nothing.
23. **Human-pattern comms are shallow** — options are neutral cards; no disagreement surfacing, no escalation
    tone, no hand-off narrative. Reads like a build tool with good copy, not a chief of staff.
24. **Dead-ends** — the help assistant can route to a nonexistent view (error card); Team/versions/budgets/
    cross-org-ops exist server-side but no CEO screen renders them; OAuth integrations report "connected" from
    a `prompt()`.
25. **The richest "live company" view is operator-only** (`dashboard.py`) — pulse/comms-graph/org-tree that
    would actually astonish are behind `AOS_API_TOKEN`; the CEO sees flat tables.

## G. Architecture hygiene
26. **~1700 lines of superseded orchestra prototypes** (actor.py, supervisor.py, orchestra.py, bus.py's
    MessageBus) read as live but have no non-test callers — dilute "this is THE engine."
27. 🟡 **PROGRESSED — the living company org is now billing-correct + reliably completes offline** — every
    knowledge-work tool rebuilds the tenant's provider (`_agent_tool`→`_apply_tenant_ctx`, tenant injected by
    jobrunner) so a company run bills the tenant, not the platform; `run_company_org` now **finalizes the run**
    (the root CEO-coordinator has no controller above it, so a completed run used to linger `running` forever);
    and a Band-2 regression (the `research-coordinator` branch hijacking the generic role) is fixed. Company
    selftest now passes 3/3. Remaining: a live console trigger + the owner-gated live run. `qa_agentic` still
    opt-in.
28. **Two copies of the factory gate chain** (agent() inline vs `_chat_gates`) — a new gate can silently miss
    one path. Hardcoded paths bypass the `aoscfg` chokepoint in several modules.

## H. Docs
29. **HANDOFF.md omits the entire durability overhaul** (Steps 1–3, dispatch-and-park default) — the most
    recent and most load-bearing work; still frames the agentic-QA-org as "the biggest remaining item."
    "MUST stay 137 passed" is a stale hard-coded gate (selftest.sh is ~152 shell checks; the pytest suite is
    one of them = 40 tests). "Working tree clean / commits … de7d9f7" is stale (HEAD is past it).
30. **ARCHITECTURE-OVERHAUL.md:120 says park is "default OFF"** — contradicted by code (default ON,
    loopcontroller.py:438) and by PARK-VALIDATION.md. Same stale string at loopcontroller.py:2525.
31. **RESUME-agent-os.md metrics off 2–5×** (66 vs 147 modules, ~7k vs ~33k LOC, 108 vs 406 commits) — visa/
    portfolio evidence risk.

---

## The true remaining critical path (per the docs, confirmed by code)
Almost every big "✅" is **offline/stubbed-green, not live-proven**. The real gate is a **live end-to-end run**:
- dispatch-and-park multi-hour crash test on a real build (runbook: PARK-VALIDATION.md),
- agentic-QA path at parity with procedural, then flip the default,
- the full company-org (`company.py`) actually wired and run,
- the auditor validated against a real human-labeled fixture set (kappa/position-bias).

## Recommended order
1. **Correctness/security sprint** (A + C + E confirmed bugs) — "zero bugs reach a human" is the North Star's
   own first priority. Most are cheap and confirmed. (2 done this pass.)
2. **Observability holes** (B) — make the watchdog survive a DB outage + add an out-of-band pager check.
3. **Refresh HANDOFF + kill the stale gate numbers** (H) — so the SSOT stops lying to the next engineer.
4. **Live validation** — actually run the thing end-to-end and close the offline→live gap.
5. **Product UX + proactive comms** (F) — the "astonish a skeptic" surface.
6. **Architecture hygiene** (G) — quarantine the dead prototypes, unify the gate chain, wire the real engine.
