#!/usr/bin/env python3
"""test_tenant_isolation.py — guard: a tenant NEVER sees another tenant's data.

Born from two real cross-tenant leaks (approvals._dead_letters + _hire_requests both queried global tables
with NO tenant filter, so every CEO's inbox showed every tenant's items — 78a0db3, 3561b8d). This creates
two tenants with their OWN product + dead-letter + hire-request + traces, and asserts each tenant's
tenant-facing reads (approvals inbox, trace observability) surface ONLY their own — never the other's. FAILS
loudly if any read regresses to a global/unscoped query.

    python test_tenant_isolation.py     # prints PASS / FAIL
Run with the agent-os venv python.
"""
import sys
import uuid
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import psycopg          # noqa: E402
import trace as _trace  # noqa: E402
import billing          # noqa: E402
import orchestrate      # noqa: E402
import approvals        # noqa: E402
import agent_request    # noqa: E402
import traceview        # noqa: E402

DB = _trace.DB
ok = True


def chk(cond, label):
    global ok
    print(("PASS" if cond else "FAIL") + f": {label}")
    ok = ok and bool(cond)


def _seed(tag):
    tid = billing.signup(f"iso-{tag}-{uuid.uuid4().hex[:5]}", "free")["tenant_id"]
    prod = f"iso-{tag}-{uuid.uuid4().hex[:6]}"
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("INSERT INTO tenant_products (product, tenant_id) VALUES (%s,%s)", (prod, tid))
        cur.execute("""INSERT INTO tasks (assignee, requester, role, title, status, attempts, max_retry)
                       VALUES (%s,'iso','builder','dead one','dead',3,3) RETURNING id""", (f"builder@{prod}",))
        dead_id = cur.fetchone()[0]
        cur.execute("""INSERT INTO traces (product, stage, role, kind, rc, output, prompt, run_id, cost_usd, ts)
                       VALUES (%s,'BUILD','builder','agent',0,'built','p',900900,0.1,now())""", (prod,))
        c.commit()
    hire_id = orchestrate.file_hire("iso-agent", "qa-bot", "need one", tenant_id=tid)
    q_id = agent_request.ask(tid, "seed question?", kind="decision")["request_id"]
    return {"tid": tid, "prod": prod, "dead_id": dead_id, "hire_id": hire_id, "q_id": q_id}


def _cleanup(*seeds):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        for s in seeds:
            cur.execute("DELETE FROM tasks WHERE id=%s", (s["dead_id"],))
            cur.execute("DELETE FROM hire_requests WHERE id=%s", (s["hire_id"],))
            cur.execute("DELETE FROM agent_requests WHERE id=%s", (s["q_id"],))
            cur.execute("DELETE FROM traces WHERE product=%s", (s["prod"],))
            cur.execute("DELETE FROM tenant_products WHERE product=%s", (s["prod"],))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (s["tid"],))
        c.commit()


A = B = None
try:
    A, B = _seed("a"), _seed("b")

    a_inbox = approvals.inbox(A["tid"])["items"]
    b_inbox = approvals.inbox(B["tid"])["items"]

    chk(any(i["kind"] == "dead_letter" and i["ref"] == A["dead_id"] for i in a_inbox),
        "tenant A sees its OWN dead-letter")
    chk(not any(i["kind"] == "dead_letter" and i["ref"] == B["dead_id"] for i in a_inbox),
        "tenant A does NOT see tenant B's dead-letter")
    chk(any(i["kind"] == "hire_request" and i["ref"] == A["hire_id"] for i in a_inbox),
        "tenant A sees its OWN hire-request")
    chk(not any(i["kind"] == "hire_request" and i["ref"] == B["hire_id"] for i in a_inbox),
        "tenant A does NOT see tenant B's hire-request")
    chk(not any(i["kind"] == "dead_letter" and i["ref"] == A["dead_id"] for i in b_inbox),
        "tenant B does NOT see tenant A's dead-letter (symmetric)")

    # WRITE isolation: tenant B must NOT be able to DECIDE (mutate) tenant A's items — even by guessing
    # the id. A cross-tenant write is worse than a read; decide() must verify ownership per kind.
    def _rejected(fn):
        try:
            fn()
            return False
        except Exception:
            return True

    chk(_rejected(lambda: approvals.decide(B["tid"], "dead_letter", A["dead_id"], "drop")),
        "tenant B CANNOT decide tenant A's dead-letter (cross-tenant write blocked)")
    chk(_rejected(lambda: approvals.decide(B["tid"], "hire_request", A["hire_id"], "approve")),
        "tenant B CANNOT decide tenant A's hire-request (cross-tenant write blocked)")
    chk(not _rejected(lambda: approvals.decide(A["tid"], "dead_letter", A["dead_id"], "retry")),
        "tenant A CAN decide its OWN dead-letter (ownership allows)")
    chk(_rejected(lambda: agent_request.answer(A["q_id"], "hack", tenant_id=B["tid"])),
        "tenant B CANNOT answer tenant A's AI question (cross-tenant write blocked)")
    chk(agent_request.get(A["q_id"])["status"] == "open",
        "tenant A's question stays OPEN after B's blocked answer attempt")

    # observability: each tenant's trace aggregate counts ONLY its own product's runs.
    ao = traceview.overview(A["tid"])
    a_prods = {r.get("product") for r in (ao.get("recent") or [])}
    chk(B["prod"] not in a_prods, "tenant A's trace observability excludes tenant B's product")
except Exception as e:
    chk(False, f"tenant-isolation test raised: {str(e)[:100]}")
finally:
    _cleanup(*[s for s in (A, B) if s])

print("PASS: tenant isolation holds — a tenant sees only its own inbox items + traces"
      if ok else "FAIL: cross-tenant leak detected")
sys.exit(0 if ok else 1)
