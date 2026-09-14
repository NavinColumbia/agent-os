# Rewire: research → the crash-resumable `run_org` engine

**Goal (owner directive, "rewire first, then live-run"):** the RESEARCH phase — orchestra's one live
production caller — must run on the durable, crash-resumable `runtime.run_org` decide-loop (lease-reclaim on
crash), not the current synchronous `ThreadPoolExecutor` in `research_org.run_research` that loses in-flight
work if the process dies (the run sits `running` until `abandon_stale_runs` sweeps it ~2h later).

**Invariant that must NOT break:** research is on the critical path of *every* real build and spends real
tenant/subscription money. The rewire shipped **additively, behind `AOS_RESEARCH_RUNORG`**. It is now
**default ON** after the offline crash-resume proof; set `AOS_RESEARCH_RUNORG=0` to roll back to the proven
synchronous path during debugging. A live parity run is still required before deleting the old path.

## The 3 confirmed integration challenges (with the fix for each)

### 1. Output contract (findings/ + REPORT.md)
`research_fleet.synthesize(repo, question)` reads the `findings/NN-*.md` files that
`research_fleet.research_one(repo, idx, subq, api_key)` writes. The generic `tools.research` does NOT write
these. **Fix:** add a thin tool `research_subq` to `orchestra/tools.py` that wraps `research_fleet.research_one`
so each parked tool-worker writes a contract finding file. `synthesize` runs once after the org completes (a
single cheap call, kept OUT of the org — a mid-synthesize crash just re-runs it).

### 2. factory._ctx / provider (BILLING CORRECTNESS — the riskiest)
`jobrunner._run` executes the tool in a background thread that does NOT inherit `factory._ctx` (thread-local:
tenant, api_key, engine, provider). A tenant BYO-key run would silently fall back to the platform default.
**Fix:** persist only the tenant/org/provider IDENTIFIERS in the worker's `tool_args` (NEVER the api_key — no
secret at rest), and have `research_subq` rebuild `factory._ctx` from them at the top, reusing the exact
provider-resolution `loopcontroller._rebuild_ctx` already uses for parked workers
(`_resolved_provider` + `_apply_provider_ctx`). Platform runs (tenant=`platform`) need no key (subscription).
Add an assertion in the offline proof that a tenant run resolves the tenant's provider, not the platform's.

### 3. Drive loop (tool-workers park async)
A single `run_org` returns while tool jobs are still parked/running. **Fix:** reuse the proven
`company.run_company_org` drive loop (re-enter `run_org` until the run is terminal or no jobs are active and
all inboxes are empty), with `reconcile_parked` at startup for crash-resume.

## Shape of the org (reuses the existing generic tool-team branch, runtime.py:472)
```
controller ──task──▶ research-coordinator (supervisor)
                        │  decompose(question) → N subqs        (one AI call, in the coordinator's first step)
                        ├─task─▶ researcher.w0  (tool=research_subq, args={idx,subq,repo,tenant,org})  ─park─▶ jobrunner
                        ├─task─▶ researcher.w1  …                                                        (crash-reclaimable)
                        └─ aggregate when all children terminal → run_research calls synthesize() → REPORT.md
```
The decompose is the coordinator's deterministic spec step: add a `research-coordinator` branch to
`_coordinator_specs` that calls `research_fleet.decompose` and returns one `research_subq` tool-worker spec per
subq (mirrors the qa-coordinator branch). No new AI-decompose prompt; reuse research_fleet's.

## Impact map (every caller / seam touched)
- `orchestra/tools.py` — NEW `research_subq` tool + registry entry. (additive)
- `orchestra/runtime.py` `_coordinator_specs` — NEW `research-coordinator` branch. (additive; unmatched roles
  unchanged)
- `orchestra/research_org.py` — `run_research_via_org(...)` alongside the existing legacy path; the public
  `run_research` dispatches to the durable org path unless `AOS_RESEARCH_RUNORG=0`. (additive rollback path intact)
- `research.py` — unchanged (it already calls `research_org.run_research`; the flag switches the engine
  beneath it). Confirm `orchestra_on()` still gates as today.
- No schema change (actors/events already carry everything; tenant/org already columns).

## Edge/empty/error cases to cover in the proof
- decompose returns 0 subqs → coordinator finishes with an honest empty report (never a fabricated one).
- one subq's tool fails → that worker reports `failed`; `answered < subquestions`; run still completes and
  synthesizes from the successful findings (parity with today's "one blocked child" selftest).
- process killed after K children done, before the rest → re-entry `reconcile_parked` re-dispatches ONLY the
  unfinished workers; the K done are not re-run (idempotent via `job_id_for`); report is lossless.
- kill-switch halted mid-run → no new work; run stays resumable (store gates emits; persist_step already
  proven).
- tenant BYO-key run → `research_subq` rebuilds the TENANT's provider; spend attributed to the tenant.

## Done-checklist (each mapped to a named proof)
- [x] `research_subq` tool (tools.py) writes the contract finding via `research_fleet.research_one`; registered.
- [x] `_coordinator_specs` research-coordinator branch decomposes → one `research_subq` spec/subq (runtime.py).
- [x] full org run: coordinator → N researchers → synthesize → REPORT.md where research.py expects it →
      `research_org._selftest_via_org()` part A (subquestions=3, answered=2, run done).
- [x] CRASH-RESUME: a worker parked mid-job (finding deleted, in-proc handle gone) is re-dispatched by
      `reconcile_parked` and re-writes its finding losslessly → `_selftest_via_org()` part B.
- [x] BILLING: `tools._apply_tenant_ctx` resolves the tenant provider (platform→host, claude→their key,
      codex→engine codex) → `test_research_subq_ctx_rebuild_bills_the_tenant`.
- [x] wired behind `AOS_RESEARCH_RUNORG` (default ON, rollback `0`); legacy path + runtime + tools selftests still green.
- [ ] **NEXT (owner-gated, spends):** live run on one real question; compare report +
      billing to legacy.
- [x] flip default to on.
- [ ] delete the synchronous path + `ThreadPoolExecutor` after the live parity run.

## Rollout
1. Land the additive code + offline proofs. 2. Flip default ON with `AOS_RESEARCH_RUNORG=0` rollback.
3. One live research, compare report + billing to legacy. 4. Remove the old synchronous path.
