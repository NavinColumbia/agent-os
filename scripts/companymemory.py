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
        c.commit()


# ── company memory ──────────────────────────────────────────────────────────────────────────────────
def remember(tenant_id, org_id, kind, text):
    """Record a durable fact about this company. kind ∈ decision|preference|product|context|constraint."""
    if not (tenant_id and text):
        return
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO company_memory (tenant_id, org_id, kind, text)
                       VALUES (%s,%s,%s,%s)""", (str(tenant_id), str(org_id) if org_id else None, kind, text[:1000]))
        c.commit()


def recall(tenant_id, org_id=None, limit=_CO_LIMIT):
    """The company's memory items, newest first, scoped to (tenant, org) plus tenant-wide items."""
    if not tenant_id:
        return []
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT kind, text FROM company_memory
                       WHERE tenant_id=%s AND (org_id=%s OR org_id IS NULL)
                       ORDER BY weight DESC, ts DESC LIMIT %s""",
                    (str(tenant_id), str(org_id) if org_id else None, limit))
        return [{"kind": k, "text": t} for k, t in cur.fetchall()]


# ── role lessons ────────────────────────────────────────────────────────────────────────────────────
def add_lesson(role, lesson, source=None):
    """Store a reusable lesson for a role (idempotent on the lesson text)."""
    if not (role and lesson):
        return
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO role_lessons (role, lesson, source) VALUES (%s,%s,%s)
                       ON CONFLICT (lesson) DO NOTHING""", (role, lesson[:500], source))
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
    mem = recall(tenant_id, org_id) if tenant_id else []
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
        print("PASS: companymemory — company memory (scoped) + role lessons (dedup, self-reinforcing) "
              "injected into agent briefs ✅" if ok else "FAIL")
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM company_memory WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM role_lessons WHERE role LIKE 'role-%%' OR role LIKE 'norole-%%'")
            c.commit()
    return ok


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "selftest"
    if cmd == "selftest":
        sys.exit(0 if _selftest() else 1)
    elif cmd == "recall":
        for m in recall(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None):
            print(m)
