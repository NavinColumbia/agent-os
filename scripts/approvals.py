#!/usr/bin/env python3
"""approvals.py — the tenant-facing Approvals / Decisions inbox (Area 8 governance core).

Everything that is waiting on a HUMAN decision, scoped to one tenant, aggregated into a single
list so nothing sits unseen: products that were paused (a PAUSED.html landed, or the kill_switch
halted them), open hire_requests an agent raised, a pending AI-consent gate that blocks builds,
and dead-lettered tasks that exhausted retries and need a person to retry or drop them. decide()
applies the SAFE side of each decision and records it in the tamper-evident audit chain.

    approvals.py json <tenant_id>     # the inbox as JSON
    approvals.py selftest
Run with the agent-os venv python.  Data/logic module only — binds no server.
"""
import sys
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit    # noqa: E402
import consent  # noqa: E402

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)
PRODUCTS = Path.home() / "projects" / "products"

# Optional collaborators — present in this factory, but degrade gracefully if absent.
try:
    import killswitch  # noqa: E402
except Exception:       # pragma: no cover
    killswitch = None


def _tenant_products(tid):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT product FROM tenant_products WHERE tenant_id=%s", (tid,))
        return [r[0] for r in cur.fetchall()]


def _paused_apps(tid):
    """A tenant product is 'paused' if it has a PAUSED.html OR a kill_switch row (scoped or global)."""
    prods = _tenant_products(tid)
    if not prods:
        return []
    halted = {}
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT scope, reason FROM kill_switch WHERE scope IN ('global', %s) OR scope = ANY(%s)",
                    (tid, prods))
        for scope, reason in cur.fetchall():
            halted[scope] = reason or "halted via kill_switch"
    out = []
    global_reason = halted.get("global")
    for p in prods:
        reason = None
        if (PRODUCTS / p / "PAUSED.html").exists():
            reason = "PAUSED.html present (auto-paused)"
        elif p in halted:
            reason = halted[p]
        elif global_reason is not None:
            reason = f"global halt: {global_reason}"
        if reason:
            out.append({"product": p, "reason": reason})
    return out


def _hire_requests():
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT id, requester, need_role, reason FROM hire_requests
                       WHERE status='open' ORDER BY id""")
        return [{"id": r[0], "requester": r[1], "need_role": r[2], "reason": r[3] or ""}
                for r in cur.fetchall()]


def _dead_letters(tid):
    """Dead-lettered tasks. Scope to the tenant's products where a task title/role references them;
    a dead task with no obvious tenant link still surfaces (a human must clear it somewhere)."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT id, title, role, last_error, attempts FROM tasks
                       WHERE status='dead' ORDER BY id""")
        return [{"id": r[0], "title": r[1] or "", "role": r[2] or "",
                 "last_error": r[3] or "", "attempts": r[4] or 0} for r in cur.fetchall()]


def inbox(tid):
    """Aggregate everything awaiting a human decision for this tenant into ONE list."""
    items = []

    for p in _paused_apps(tid):
        items.append({
            "id": f"paused_app:{p['product']}",
            "kind": "paused_app",
            "ref": p["product"],
            "title": f"Resume or retire '{p['product']}'",
            "detail": p["reason"],
            "severity": "high",
            "action_label": "Approve resume",
        })

    if not consent.require_consent(tid):
        st = consent.state(tid)
        items.append({
            "id": f"consent:{tid}",
            "kind": "consent",
            "ref": tid,
            "title": "AI consent required before builds can run",
            "detail": f"{st['provider']} · disclosure {st['version']} not yet accepted",
            "severity": "high",
            "action_label": "Approve consent",
        })

    for h in _hire_requests():
        items.append({
            "id": f"hire_request:{h['id']}",
            "kind": "hire_request",
            "ref": h["id"],
            "title": f"Approve hiring a '{h['need_role']}'",
            "detail": f"requested by {h['requester']}" + (f": {h['reason']}" if h["reason"] else ""),
            "severity": "med",
            "action_label": "Approve / Deny",
        })

    for d in _dead_letters(tid):
        items.append({
            "id": f"dead_letter:{d['id']}",
            "kind": "dead_letter",
            "ref": d["id"],
            "title": f"Dead-lettered task: {d['title'][:80] or d['role'] or ('#' + str(d['id']))}",
            "detail": f"failed after {d['attempts']} attempts — {d['last_error'][:160]}",
            "severity": "med",
            "action_label": "Retry / Drop",
        })

    return {"items": items, "count": len(items)}


def decide(tid, kind, ref, verdict):
    """Apply the SAFE side of a human decision and record it in the audit chain."""
    verdict = (verdict or "").lower()

    if kind == "consent":
        if verdict == "approve":
            consent.record(tid)
        else:
            consent.revoke(tid)

    elif kind == "paused_app":
        if verdict == "approve":
            if killswitch is not None:
                killswitch.resume(ref)            # clear scoped kill_switch row
            (PRODUCTS / ref / "PAUSED.html").unlink(missing_ok=True)
        # 'deny'/'retire' is a no-op here beyond the audit note (retirement is a separate flow).

    elif kind == "hire_request":
        new = "fulfilled" if verdict == "approve" else "denied"
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("UPDATE hire_requests SET status=%s WHERE id=%s", (new, int(ref)))
            c.commit()

    elif kind == "dead_letter":
        if verdict == "retry":
            with psycopg.connect(DB) as c, c.cursor() as cur:
                cur.execute("""UPDATE tasks SET status='pending', not_before=now(), last_error=NULL
                               WHERE id=%s""", (int(ref),))
                c.commit()
        else:  # 'drop'
            with psycopg.connect(DB) as c, c.cursor() as cur:
                cur.execute("UPDATE tasks SET status='done' WHERE id=%s", (int(ref),))
                c.commit()

    else:
        raise ValueError(f"unknown decision kind: {kind}")

    audit.append(actor="approvals", action="DecisionApplied", resource=f"{kind}:{ref}",
                 decision=verdict, payload={"tenant": tid, "kind": kind, "ref": str(ref)})
    return {"ok": True, "kind": kind, "ref": ref, "verdict": verdict}


def _selftest():
    import billing
    reg = billing.signup("approvals-selftest", "free")
    tid = reg["tenant_id"]
    hire_id = None
    dead_id = None
    try:
        # An item we fully control: an OPEN hire_request (global, surfaces in every tenant's inbox).
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""INSERT INTO hire_requests (requester, need_role, reason, status)
                           VALUES (%s,%s,%s,'open') RETURNING id""",
                        ("approvals-selftest", "qa-bot", "selftest hire"))
            hire_id = cur.fetchone()[0]
            # A dead-lettered task that a human must retry/drop.
            cur.execute("""INSERT INTO tasks (assignee, requester, role, title, status, attempts, max_retry)
                           VALUES ('','approvals-selftest','qa-bot','selftest dead task','dead',3,3)
                           RETURNING id""")
            dead_id = cur.fetchone()[0]
            c.commit()

        box = inbox(tid)
        kinds = {i["kind"] for i in box["items"]}
        hire_present = any(i["kind"] == "hire_request" and i["ref"] == hire_id for i in box["items"])
        dead_present = any(i["kind"] == "dead_letter" and i["ref"] == dead_id for i in box["items"])
        consent_present = "consent" in kinds       # fresh tenant -> consent must be required

        # Resolve the hire_request and verify it leaves the inbox.
        decide(tid, "hire_request", hire_id, "approve")
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT status FROM hire_requests WHERE id=%s", (hire_id,))
            hire_status = cur.fetchone()[0]
        hire_resolved = hire_status == "fulfilled" and \
            not any(i["kind"] == "hire_request" and i["ref"] == hire_id for i in inbox(tid)["items"])

        # Retry the dead task and verify it's no longer dead.
        decide(tid, "dead_letter", dead_id, "retry")
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT status FROM tasks WHERE id=%s", (dead_id,))
            task_status = cur.fetchone()[0]
        dead_resolved = task_status == "pending" and \
            not any(i["kind"] == "dead_letter" and i["ref"] == dead_id for i in inbox(tid)["items"])

        # Approve consent and verify the gate item clears.
        decide(tid, "consent", tid, "approve")
        consent_resolved = consent.require_consent(tid) and \
            not any(i["kind"] == "consent" for i in inbox(tid)["items"])

        ok = (hire_present and dead_present and consent_present and
              hire_resolved and dead_resolved and consent_resolved)
        print(f"surfaced: hire={hire_present} dead={dead_present} consent={consent_present} | "
              f"resolved: hire={hire_resolved} dead={dead_resolved} consent={consent_resolved}")
        print("PASS: approvals inbox aggregates + decide() resolves each kind ✅" if ok else "FAIL")
        rc = 0 if ok else 1
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            if dead_id is not None:
                cur.execute("DELETE FROM tasks WHERE id=%s", (dead_id,))
            if hire_id is not None:
                cur.execute("DELETE FROM hire_requests WHERE id=%s", (hire_id,))
            cur.execute("DELETE FROM ai_consent WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenant_products WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
            c.commit()
    sys.exit(rc)


def _main(argv):
    import json
    if not argv or argv[0] == "selftest":
        _selftest()
    elif argv[0] == "json" and len(argv) > 1:
        print(json.dumps(inbox(argv[1]), indent=2, default=str))
    else:
        sys.exit("usage: approvals.py json <tenant_id> | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
