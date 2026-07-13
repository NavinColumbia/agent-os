#!/usr/bin/env python3
"""companymemory.py — the MEMORY SPINE (REBUILD-PLAN A3).

The architecture review's verdict: "not one agent remembers anything it learned yesterday." This fixes it.
Two durable stores, both injected into every agent's brief so a spawned agent is no longer a blank slate:

  1. COMPANY MEMORY (per tenant+org): the decisions, preferences, product history, and standing facts of
     THIS company — so every agent building for it knows what was already decided, what the CEO prefers,
     and what already shipped. Written by the controller/factory at key moments (a plan approved, a product
     delivered, a preference stated); read into the brief of every agent that works for that company.

  2. ROLE LESSONS (per role, cross-tenant): hard-won lessons distilled from failures — every BLOCKED build
     / fix-loop post-mortem produces a reusable lesson ("when X, do Y") for that ROLE, loaded into the
     brief of that role on future work so the fleet gets smarter over time instead of repeating mistakes.

Wired into factory.agent() so the brief carries: elite role charter (role_brief) + this company's memory +
this role's accumulated lessons. Every distill() is an AI call (cost is not a concern; NORTH-STAR).

  python companymemory.py selftest
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import psycopg  # noqa: E402
import trace as _trace  # noqa: E402  — shared .env.local DATABASE_URL

DB = _trace.DB
_CO_LIMIT = int(os.environ.get("AOS_MEMORY_CO_LIMIT", "10"))       # company-memory items injected per brief
_LESSON_LIMIT = int(os.environ.get("AOS_MEMORY_LESSON_LIMIT", "6"))  # role lessons injected per brief


def _ensure():
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS company_memory (
            id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, org_id TEXT,
            kind TEXT NOT NULL, text TEXT NOT NULL, weight INT DEFAULT 1,
            ts TIMESTAMPTZ DEFAULT now())""")
        cur.execute("CREATE INDEX IF NOT EXISTS company_memory_scope ON company_memory (tenant_id, org_id, ts DESC)")
        cur.execute("""CREATE TABLE IF NOT EXISTS role_lessons (
            id BIGSERIAL PRIMARY KEY, role TEXT NOT NULL, lesson TEXT NOT NULL UNIQUE,
            source TEXT, uses INT DEFAULT 0, ts TIMESTAMPTZ DEFAULT now())""")
        # MEMORY-LAYER (item 9) additive columns — safe defaults keep existing rows/callers identical:
        #   visibility 'shared' => today's single-pool behaviour; provenance columns default NULL.
        for col, ddl in (("visibility", "visibility TEXT NOT NULL DEFAULT 'shared'"),
                         ("role_scope", "role_scope TEXT"), ("author_actor", "author_actor TEXT"),
                         ("run_id", "run_id TEXT"), ("sources", "sources JSONB"), ("audit_id", "audit_id BIGINT")):
            cur.execute(f"ALTER TABLE company_memory ADD COLUMN IF NOT EXISTS {ddl}")
        for col, ddl in (("tenant_id", "tenant_id TEXT"), ("run_id", "run_id TEXT"),
                         ("author_actor", "author_actor TEXT"), ("audit_id", "audit_id BIGINT")):
            cur.execute(f"ALTER TABLE role_lessons ADD COLUMN IF NOT EXISTS {ddl}")
        # item 10: durable coordinator PLAN + per-phase SUMMARY, distinct from event history so a context
        # truncation can't lose the plan. Single-writer = the coordinator actor; history immutable.
        cur.execute("""CREATE TABLE IF NOT EXISTS memory_checkpoints (
            id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, run_id TEXT NOT NULL,
            actor_id TEXT, kind TEXT NOT NULL, phase TEXT, seq INT,
            content TEXT NOT NULL, superseded_by BIGINT, audit_id BIGINT,
            ts TIMESTAMPTZ DEFAULT now())""")
        cur.execute("CREATE INDEX IF NOT EXISTS memory_checkpoints_run ON memory_checkpoints (run_id, kind, seq)")
        c.commit()


def _audit(action, resource, payload, tenant_id=None):
    """Best-effort tamper-evident provenance for a memory write; returns audit_id or None. Never raises —
    a memory write must not break because the audit chain hiccuped (fail-open observability)."""
    try:
        import audit
        actor = f"actor:{payload.get('author_actor')}" if payload.get("author_actor") else "memory"
        return audit.append(actor=actor, action=action, resource=resource, payload=payload, tenant_id=tenant_id)[0]
    except Exception:
        return None


# ── company memory ──────────────────────────────────────────────────────────────────────────────────
def remember(tenant_id, org_id, kind, text, *, visibility="shared", role_scope=None,
             author_actor=None, run_id=None, sources=None):
    """Record a durable fact about this company. kind ∈ decision|preference|product|context|constraint.
    Provenance kwargs (author_actor/run_id/sources) are recorded + written into the tamper-evident audit chain;
    visibility='private' + role_scope narrow who can later recall it (two-tier model). All optional → existing
    callers (positional tenant_id/org_id/kind/text) behave exactly as before (shared, no provenance)."""
    if not (tenant_id and text):
        return
    _ensure()
    import json as _json
    aid = _audit("MemoryWrite", f"company_memory:{kind}",
                 {"tenant": str(tenant_id), "org": org_id, "kind": kind, "visibility": visibility,
                  "author_actor": author_actor, "run_id": run_id}, tenant_id=str(tenant_id))
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO company_memory
                       (tenant_id, org_id, kind, text, visibility, role_scope, author_actor, run_id, sources, audit_id)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (str(tenant_id), str(org_id) if org_id else None, kind, text[:1000], visibility,
                     role_scope, str(author_actor) if author_actor else None, str(run_id) if run_id else None,
                     _json.dumps(sources) if sources else None, aid))
        c.commit()


def recall(tenant_id, org_id=None, limit=_CO_LIMIT, role=None):
    """The company's SHARED memory items, newest first, scoped to (tenant, org) plus tenant-wide items. When a
    `role` is given, role-scoped fragments are filtered to that role (private notes are never returned here)."""
    if not tenant_id:
        return []
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT kind, text FROM company_memory
                       WHERE tenant_id=%s AND (org_id=%s OR org_id IS NULL)
                         AND visibility='shared'
                         AND (role_scope IS NULL OR %s::text IS NULL OR role_scope=%s)
                       ORDER BY weight DESC, ts DESC LIMIT %s""",
                    (str(tenant_id), str(org_id) if org_id else None, role, role, limit))
        return [{"kind": k, "text": t} for k, t in cur.fetchall()]


# ── coordinator plan / phase-summary checkpoints (item 10) ────────────────────────────────────────────
def checkpoint(tenant_id, run_id, kind, content, *, actor_id=None, phase=None, seq=None):
    """Durably record a coordinator's PLAN or a per-phase SUMMARY (kind ∈ 'plan'|'phase_summary'), distinct from
    the event history so a context truncation can't lose it. Returns the checkpoint id. History is immutable —
    a revised plan is a NEW row (the reader takes the latest)."""
    if not (tenant_id and run_id and content):
        return None
    _ensure()
    aid = _audit("MemoryCheckpoint", f"memory_checkpoints:{kind}",
                 {"tenant": str(tenant_id), "run_id": str(run_id), "kind": kind, "phase": phase,
                  "author_actor": actor_id}, tenant_id=str(tenant_id))
    with psycopg.connect(DB) as c, c.cursor() as cur:
        if seq is None:
            cur.execute("SELECT COALESCE(MAX(seq),0)+1 FROM memory_checkpoints WHERE run_id=%s AND kind=%s",
                        (str(run_id), kind))
            seq = cur.fetchone()[0]
        cur.execute("""INSERT INTO memory_checkpoints (tenant_id, run_id, actor_id, kind, phase, seq, content, audit_id)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (str(tenant_id), str(run_id), str(actor_id) if actor_id else None, kind, phase, seq,
                     str(content)[:8000], aid))
        cid = cur.fetchone()[0]
        c.commit()
        return cid


def plan(run_id):
    """The latest PLAN checkpoint for a run (None if the coordinator hasn't recorded one)."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT content FROM memory_checkpoints WHERE run_id=%s AND kind='plan'
                       ORDER BY seq DESC, ts DESC LIMIT 1""", (str(run_id),))
        row = cur.fetchone()
        return row[0] if row else None


def summaries(run_id, limit=20):
    """The per-phase SUMMARY checkpoints for a run, in order — the compaction unit item 11 reuses."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT phase, content FROM memory_checkpoints WHERE run_id=%s AND kind='phase_summary'
                       ORDER BY seq ASC, ts ASC LIMIT %s""", (str(run_id), limit))
        return [{"phase": p, "content": t} for p, t in cur.fetchall()]


# ── role lessons ────────────────────────────────────────────────────────────────────────────────────
def add_lesson(role, lesson, source=None, *, tenant_id=None, run_id=None, author_actor=None):
    """Store a reusable lesson for a role (idempotent on the lesson text). Provenance kwargs are recorded +
    audited. tenant_id NULL = a cross-tenant fleet lesson (today's behaviour); a value scopes it to that tenant."""
    if not (role and lesson):
        return
    _ensure()
    aid = _audit("MemoryLesson", f"role_lessons:{role}",
                 {"role": role, "tenant": tenant_id, "run_id": run_id, "author_actor": author_actor,
                  "source": source}, tenant_id=str(tenant_id) if tenant_id else None)
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO role_lessons (role, lesson, source, tenant_id, run_id, author_actor, audit_id)
                       VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (lesson) DO NOTHING""",
                    (role, lesson[:500], source, str(tenant_id) if tenant_id else None,
                     str(run_id) if run_id else None, str(author_actor) if author_actor else None, aid))
        c.commit()


def lessons_for(role, limit=_LESSON_LIMIT):
    """The most-used, newest lessons for a role."""
    if not role:
        return []
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT lesson FROM role_lessons WHERE role=%s
                       ORDER BY uses DESC, ts DESC LIMIT %s""", (role, limit))
        rows = [r[0] for r in cur.fetchall()]
        if rows:                                      # count the read as a use (popular lessons float up)
            cur.execute("UPDATE role_lessons SET uses=uses+1 WHERE role=%s AND lesson = ANY(%s)", (role, rows))
            c.commit()
    return rows


def distill_lesson(role, incident, api_key=None):
    """AI-distill ONE reusable, generalizable lesson from a failure/fix incident, and store it for the role.
    Every distill is a model call — the fleet learning from its own mistakes. Best-effort: never raises."""
    try:
        import factory
        if api_key is not None:
            try:
                factory._ctx.api_key = api_key
            except Exception:
                pass
        prompt = ("From this build failure/fix, extract ONE reusable, generalizable lesson for a "
                  f"{role} — a short imperative rule that would prevent the class of problem next time "
                  "(not the specific detail). Reply with ONLY the lesson sentence, <=200 chars, or 'NONE' "
                  f"if there is no generalizable lesson.\nINCIDENT:\n{str(incident)[:2000]}")
        r = factory.agent("classifier", ".", prompt, light=True, model=factory.CHEAP_MODEL)
        text = ((r or {}).get("out_full") or (r or {}).get("out") or "").strip().strip('"').strip()
        text = text.splitlines()[0].strip() if text else ""
        if text and text.upper() != "NONE" and len(text) > 8:
            add_lesson(role, text, source="fix-loop")
            return text
    except Exception:
        pass
    return None


# ── the brief injection ─────────────────────────────────────────────────────────────────────────────
def brief_context(tenant_id, org_id, role):
    """The memory block appended to an agent's brief: this company's memory + this role's lessons.
    Returns '' when there's nothing to add (a brand-new tenant with no history + a role with no lessons)."""
    parts = []
    mem = recall(tenant_id, org_id, role=role) if tenant_id else []
    if mem:
        parts.append("WHAT YOU ALREADY KNOW ABOUT THIS COMPANY (honor these — do not re-decide settled "
                     "things or contradict stated preferences):\n"
                     + "\n".join(f"  - [{m['kind']}] {m['text']}" for m in mem))
    les = lessons_for(role) if role else []
    if les:
        parts.append("LESSONS YOUR ROLE HAS LEARNED THE HARD WAY (apply them):\n"
                     + "\n".join(f"  - {l}" for l in les))
    return ("\n\n" + "\n\n".join(parts)) if parts else ""


def _selftest():
    import uuid
    _ensure()
    tid = f"cmem-selftest-{uuid.uuid4().hex[:8]}"
    ok = True

    def chk(cond, label):
        nonlocal ok
        print(("PASS" if cond else "FAIL") + f": {label}")
        ok = ok and bool(cond)

    try:
        # company memory round-trips + scoping
        remember(tid, "1", "preference", "CEO prefers a dark-mode UI by default")
        remember(tid, "1", "decision", "Chose Postgres over SQLite for the data layer")
        remember(tid, None, "constraint", "Never email customers without opt-in")   # tenant-wide
        remember(tid, "2", "product", "Shipped an unrelated thing in another org")   # different org
        mem = recall(tid, "1")
        texts = " | ".join(m["text"] for m in mem)
        chk("dark-mode" in texts and "Postgres" in texts and "opt-in" in texts and "another org" not in texts,
            f"company memory recalls org-1 + tenant-wide, EXCLUDES org-2 ({len(mem)} items)")

        # role lessons: dedupe + injection
        lrole = f"role-{uuid.uuid4().hex[:6]}"
        add_lesson(lrole, "Always propagate a changed function signature to all callers")
        add_lesson(lrole, "Always propagate a changed function signature to all callers")   # dup -> ignored
        add_lesson(lrole, "Park the mouse before capturing UI state to avoid hover false positives")
        les = lessons_for(lrole)
        chk(len(les) == 2, f"role lessons stored + deduped (got {len(les)})")

        # the assembled brief block contains both
        bc = brief_context(tid, "1", lrole)
        chk("WHAT YOU ALREADY KNOW" in bc and "dark-mode" in bc and "LESSONS YOUR ROLE" in bc
            and "propagate" in bc, "brief_context injects company memory + role lessons")

        # empty for a brand-new tenant/role
        chk(brief_context(f"empty-{uuid.uuid4().hex[:6]}", None, f"norole-{uuid.uuid4().hex[:6]}") == "",
            "brief_context is empty for a tenant/role with no history (no noise)")

        # MEMORY-LAYER (item 9): two-tier visibility — a PRIVATE fragment is NOT returned by shared recall
        remember(tid, "1", "context", "a private scratch note", visibility="private", author_actor="42", run_id="r1")
        chk("private scratch" not in " | ".join(m["text"] for m in recall(tid, "1")),
            "private fragments are excluded from shared recall (two-tier)")
        # role-scoped fragment: visible to its role, invisible to another
        remember(tid, "1", "constraint", "backend must use connection pooling", role_scope="backend-engineer")
        seen_be = "connection pooling" in " | ".join(m["text"] for m in recall(tid, "1", role="backend-engineer"))
        seen_other = "connection pooling" in " | ".join(m["text"] for m in recall(tid, "1", role="designer"))
        chk(seen_be and not seen_other, "role-scoped memory reaches its role, not others")
        # provenance actually persisted
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT count(*) FROM company_memory WHERE tenant_id=%s AND author_actor='42'", (tid,))
            chk(cur.fetchone()[0] == 1, "provenance (author_actor) persisted on the write")

        # item 10: coordinator PLAN + phase-summary checkpoints round-trip; a revised plan supersedes
        checkpoint(tid, "run-xyz", "plan", "step 1: research; step 2: build", actor_id="ceo")
        checkpoint(tid, "run-xyz", "plan", "REVISED: step 1: build; step 2: ship", actor_id="ceo")
        checkpoint(tid, "run-xyz", "phase_summary", "research done: market is X", phase="research")
        chk(plan("run-xyz") and "REVISED" in plan("run-xyz"), "plan() returns the LATEST plan checkpoint")
        chk(len(summaries("run-xyz")) == 1 and summaries("run-xyz")[0]["phase"] == "research",
            "phase-summary checkpoints round-trip (the compaction unit for item 11)")

        print("PASS: companymemory — company memory (scoped) + role lessons (dedup, self-reinforcing) "
              "injected into agent briefs; two-tier visibility + role-scope + provenance + plan/summary "
              "checkpoints ✅" if ok else "FAIL")
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM company_memory WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM role_lessons WHERE role LIKE 'role-%%' OR role LIKE 'norole-%%'")
            cur.execute("DELETE FROM memory_checkpoints WHERE tenant_id=%s", (tid,))
            c.commit()
    return ok


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "selftest"
    if cmd == "selftest":
        sys.exit(0 if _selftest() else 1)
    elif cmd == "recall":
        for m in recall(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None):
            print(m)
