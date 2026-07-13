# Agent-memory layer — design (research item 9)

The design for an explicit agent-memory layer, grounded in what agent-os already has (`companymemory.py`, the
orchestra actor `memory` field, `audit.py`, `governance.py`, `store.py` conventions) and the SOTA memory research
(signals A4/A5 + Appendix theme A in [`AGENTIC-AI-RESEARCH.md`](AGENTIC-AI-RESEARCH.md)). Tracks
[`IMPROVEMENTS-PLAN.md`](IMPROVEMENTS-PLAN.md) items 9/10/11. **Extend, don't greenfield** — the substrate exists.

## 1. What exists today (grounded)
- **`scripts/companymemory.py`** — the "memory spine." Two Postgres tables created inline by `_ensure()`:
  - `company_memory` (per **tenant+org** facts: `kind ∈ decision|preference|product|context|constraint`, `text`,
    `weight`, `ts`) — read by `recall()`, written by `remember()`.
  - `role_lessons` (per **role, cross-tenant** distilled lessons; `UNIQUE(lesson)`, `uses` counter) — `add_lesson`,
    `lessons_for` (increments `uses` on read), `distill_lesson` (an AI call that extracts a rule from a failure).
  - `brief_context(tenant, org, role)` assembles the block injected into an agent's brief.
  - **Callers:** `factory.py:719` injects `brief_context` on the heavy agent path (the READ path); `loopcontroller.py:1066`
    `remember(...,"product",...)` on DELIVER (essentially the ONLY prod WRITE). **`distill_lesson`/`add_lesson` have
    NO prod callers** — the fleet-learning loop is built but never fired.
  - **Convention gap:** tables live only in `_ensure()`, with **no `postgres/initdb/NN-*.sql`** (unlike every other subsystem).
- **`orchestra_actors.memory` JSONB** — confirmed **per-actor working memory** (private, run-scoped, shallow-merged,
  resume-safe). It is the "working memory" leaf and is already durable. **Keep as-is; do not overload.**

### The gap vs target
| Target primitive | Today |
|---|---|
| Factual (org shared knowledge) | `company_memory` ✅ but flat, no role scope, no provenance |
| Experiential (episodic cross-run lessons) | `role_lessons` ✅ shape, but cross-tenant only + **no writer wired** |
| Working (per-task scratch) | `orchestra_actors.memory` ✅ durable/private/resume-safe |
| Two-tier private→shared | ❌ single shared pool, no promotion path |
| Provenance (who/what/when) | ❌ only `ts`+`source`, not tamper-evident |
| Transactive "who knows what" | ❌ absent |
| Per-role access control | ❌ `recall` returns everything for the tenant |
| Coordinator PLAN + phase SUMMARY (item 10) | ❌ plans live only in event history |
| Compaction (item 11) | ❌ absent |
| initdb migration | ❌ inline DDL only |

## 2. Proposed model (`postgres/initdb/52-memory.sql` — next free number after 51-pulse)
Keep the two existing tables + API (callers depend on them); add missing dimensions **additively** + a real migration
applied via a `store.ensure()`-style loader in a new `scripts/memory.py` (which re-exports `companymemory`).

- **Factual — extend `company_memory`** with `visibility('private'|'shared', default 'shared')`, `role_scope`
  (NULL=all roles), `author_actor`, `run_id`, `sources JSONB`, `audit_id`. Existing `remember()` writes
  `shared/NULL` → **identical behaviour, zero migration risk.**
- **Experiential — extend `role_lessons`** with `tenant_id` (NULL = today's global fleet lesson), `run_id`,
  `author_actor`, `audit_id`; change `UNIQUE(lesson)` → `UNIQUE(tenant_id, role, lesson)`.
- **Working — unchanged** (`orchestra_actors.memory`).
- **NEW `memory_checkpoints`** (item 10): `kind ∈ plan|phase_summary`, `phase`, `seq`, `content`, `superseded_by`
  (immutable history), `audit_id`. Single-writer = the coordinator actor.
- **NEW `memory_directory`** (transactive index, later): `(tenant, run, topic, actor, role, fragment_ref)` — "who
  knows what" over the fragment tables; populated on write. Defer.

### Scoping & two-tier
Per-tenant (every table), per-org (`org_id` union as today), per-role (`role_scope` filter in `recall`). **Two-tier
(Collaborative Memory 2505.18279):** a worker writes `visibility='private'` (author-only); a **supervisor/controller
`share(fragment_id)`** promotes it to `shared`. The existing supervisor tree IS the promotion authority.

### Provenance & audit
Every write records `author_actor/run_id/sources/ts` AND calls `audit.append(action="MemoryWrite", ...)` (HMAC
hash-chained, tamper-evident), storing the `audit_id` on the fragment → memory mutations are **provable and
non-repudiable**. Route `content` through `scripts/sanitize.py` on write (strip secrets/injection before a fact
enters a future brief).

## 3. API (`scripts/memory.py`; `scope = {tenant_id, org_id, run_id, actor_id, role}`)
```
remember(scope, kind, content, *, visibility='shared', role_scope=None, sources=None)
recall(scope, query=None, *, kinds=None, limit=10)      # filtered view; v1 recency/weight, v2 semantic
note(scope, text)                                       # PRIVATE working note
share(scope, fragment_id)                               # governed: author OR supervisor/controller only
lesson(scope, role, incident) / lessons_for(scope, role)   # experiential (now tenant-aware)
checkpoint(scope, kind, content, phase=None)            # item 10: plan | phase_summary
plan(scope) / summaries(scope)
compact(scope, actor_id)                                # item 11: phase detail -> one phase_summary, prune
brief_context(scope, role)                              # unchanged assembly, now role/visibility aware
who_knows(scope, topic)                                 # transactive (later)
```
**Wiring:** controller's first decide step → `checkpoint('plan', ...)`; `loopcontroller` phase transitions →
`checkpoint('phase_summary', ...)`; fix-loop/BLOCKED post-mortem → `lesson(...)` (activates the dead writer);
`finding` emit → mirror to `note(...)`; `factory.py:719` read path gains role/visibility filtering for free;
`share()`/scope-narrowing writes gated by `governance.may(role, cap)`.

## 4. The 5 LLM-MAS challenges → one decision each
1. **Synchronization** — single-writer per fragment class; each write is one `psycopg` transaction+commit (the
   `update_actor` rule). No two agents mutate a row concurrently.
2. **Access control** — two-tier `visibility` + `role_scope` filtered views; scope-narrowing/promotion gated by
   governance manifests, audited on denial.
3. **Scalability** — `weight`/`uses` ordering + caps (exist) + item-11 `compact()`; indexes keep reads O(log n).
4. **Alignment** — workers write only `private`; entering `shared` needs a supervised `share()` → one agent can't
   silently make its belief org truth.
5. **Safety** — provenance + tamper-evident `audit_id` + `sanitize` on write; memory writes gated by
   `killswitch.is_halted` (a halted org accepts no new shared knowledge).

## 5. Build order
**FIRST (smallest useful, extends companymemory):**
1. `52-memory.sql` capturing current tables verbatim + additive columns w/ safe defaults; point `_ensure()` at it.
2. Provenance + `audit.append` on every write (thread `scope` through `remember`/`add_lesson`).
3. **Activate the experiential writer** — call `distill_lesson`/`add_lesson` at the fix-loop/BLOCKED post-mortem
   (highest ROI: code exists, never called); add `tenant_id`.
4. **Item-10 checkpoints** (`memory_checkpoints` + `checkpoint/plan/summaries`) wired into controller + phase
   transitions — makes the system resume-safe against context truncation.

**Item 10 reuses:** the new table + API + single-writer/audit patterns. **Item 11 (`compact`) reuses** item-10's
`phase_summary` rows as its compaction unit (no new storage).

**LATER:** `note()`/`share()` two-tier + `role_scope` views; `memory_directory`+`who_knows()`; semantic `recall` (pgvector).

## 6. Open questions (decide before committing)
1. **Global vs tenant-private role lessons** — cross-tenant fleet learning vs privacy. Lean: keep global default
   (`tenant_id=NULL`), add tenant-private opt-in.
2. **Does the coordinator actually hit context truncation today?** `factory.py:705` delegates context mgmt to the
   CLI. Item 10 is unconditionally worth it; **item 11 is conditional** — measure real coordinator context sizes first.
3. **MemAct learnable working-memory curation** — out of scope for v1 (large RL commitment); item-17 owes a 3-vote
   verify before any code. Don't hand-roll a worse heuristic meanwhile.
4. **Promotion authority precision** — supervisor-only vs author-self-publish-with-audit; a `can_share_memory`
   governance capability is the lever.
5. **Retention / GDPR** — `company_memory` accretes tenant decisions indefinitely; add a TTL + tenant-scoped purge
   (`store.py` `DELETE ... WHERE tenant_id=%s` template) before it becomes the org's long-term brain.

**Key files:** `scripts/companymemory.py` (extend), `scripts/orchestra/store.py:70-82,196-258` (migration+single-writer
pattern), `runtime.py:158-183,265-333` (wiring points), `factory.py:713-729` (read/injection), `loopcontroller.py:1051-1075`
(phase transitions), `audit.py:60-81` (provenance), `governance.py:13-19` (access gate), `postgres/initdb/50-orchestra.sql`
(migration style; next file `52-memory.sql`).
