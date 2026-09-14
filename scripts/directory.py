#!/usr/bin/env python3
"""directory.py — the live agent directory: presence, work registry, discovery, direct contact, and
conflict detection. This is how agents find and reach each other WITHOUT sockets or a central C2.

Model (see also ADR 0005): the role manifests are the static org chart; this table is the dynamic
layer — who is active, what they're working on, which resources they hold. Any agent looks up the
directory and addresses a message DIRECTLY to a peer via the durable broker (conversations + inbox,
exactly-once) — mailbox semantics, so the peer can be offline and still receive it. Hierarchy is used
only for escalation. Overlapping resource claims surface as conflicts to coordinate on.

    directory.py roster                         # who's active + what they're working on
    directory.py find [--role R] [--product P]  # discover agents to contact
    directory.py conflicts                      # overlapping resource claims (who steps on whom)
    directory.py contact <from> <to> <intent> "<msg>"   # direct brokered message (no socket)
    directory.py register <agent_id> <role> <product> <task> <res1,res2>
    directory.py selftest
Run with the agent-os venv python.
"""
import json
import sys
import threading
import time
from pathlib import Path

from dbpool import connection, tenant_connection  # noqa: E402

ACTIVE_WINDOW = "15 minutes"
_ensured = False
_ensure_lock = threading.Lock()


def _column_exists(cur, table, column):
    cur.execute("""SELECT 1 FROM information_schema.columns
                   WHERE table_schema = current_schema()
                     AND table_name = %s AND column_name = %s""", (table, column))
    return cur.fetchone() is not None


def _ensure():
    global _ensured
    if _ensured:
        return
    with _ensure_lock:
        if _ensured:
            return
        with connection() as c, c.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout = '2s'")
            cur.execute("""CREATE TABLE IF NOT EXISTS directory (agent_id TEXT PRIMARY KEY, role TEXT NOT NULL,
                           status TEXT NOT NULL DEFAULT 'active', product TEXT, task TEXT,
                           resources TEXT[] NOT NULL DEFAULT '{}', updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
            if not _column_exists(cur, "directory", "tenant_id"):
                cur.execute("ALTER TABLE directory ADD COLUMN tenant_id TEXT")
            cur.execute("CREATE INDEX IF NOT EXISTS directory_tenant_id_rls_idx ON directory (tenant_id)")
            if not _column_exists(cur, "conversations", "tenant_id"):
                cur.execute("ALTER TABLE conversations ADD COLUMN tenant_id TEXT")
            cur.execute("CREATE INDEX IF NOT EXISTS conversations_tenant_id_rls_idx ON conversations (tenant_id)")
            if not _column_exists(cur, "inbox", "tenant_id"):
                cur.execute("ALTER TABLE inbox ADD COLUMN tenant_id TEXT")
            cur.execute("CREATE INDEX IF NOT EXISTS inbox_tenant_id_rls_idx ON inbox (tenant_id)")
        _ensured = True


def _tenant_for_product(product):
    if not product:
        return None
    try:
        with connection() as c, c.cursor() as cur:
            cur.execute("SELECT tenant_id FROM tenant_products WHERE product=%s LIMIT 1", (product,))
            row = cur.fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _tenant_for_agent(agent_id):
    if not agent_id:
        return None
    try:
        with connection() as c, c.cursor() as cur:
            cur.execute("SELECT tenant_id FROM directory WHERE agent_id=%s", (agent_id,))
            row = cur.fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _conn(tenant_id=None):
    return tenant_connection(tenant_id) if tenant_id else connection()


def register(agent_id, role, product=None, task=None, resources=None, tenant_id=None):
    _ensure()
    tenant_id = tenant_id or _tenant_for_product(product)
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO directory (agent_id, role, status, product, task, resources, tenant_id, updated_at)
                       VALUES (%s,%s,'active',%s,%s,%s,%s, now())
                       ON CONFLICT (agent_id) DO UPDATE SET role=EXCLUDED.role, status='active',
                         product=EXCLUDED.product, task=EXCLUDED.task, resources=EXCLUDED.resources,
                         tenant_id=EXCLUDED.tenant_id, updated_at=now()""",
                    (agent_id, role, product, task, resources or [], tenant_id))


def release(agent_id, tenant_id=None):
    tenant_id = tenant_id or _tenant_for_agent(agent_id)
    with _conn(tenant_id) as c, c.cursor() as cur:
        if tenant_id:
            cur.execute("""UPDATE directory SET status='idle', resources='{}', updated_at=now()
                           WHERE agent_id=%s AND tenant_id=%s""", (agent_id, tenant_id))
        else:
            cur.execute("UPDATE directory SET status='idle', resources='{}', updated_at=now() WHERE agent_id=%s", (agent_id,))


def roster(active_only=True, tenant_id=None):
    _ensure()
    where, args = [], []
    if tenant_id:
        where.append("tenant_id=%s")
        args.append(tenant_id)
    if active_only:
        where.append("status='active'")
        where.append(f"updated_at > now() - interval '{ACTIVE_WINDOW}'")
    clause = "WHERE " + " AND ".join(where) if where else ""
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute(f"""SELECT agent_id, role, status, product, task, resources,
                          round(EXTRACT(EPOCH FROM now()-updated_at)) FROM directory
                        {clause}
                        ORDER BY updated_at DESC""", tuple(args))
        return [{"agent_id": a, "role": r, "status": s, "product": p, "task": t, "resources": res, "age_s": int(age)}
                for a, r, s, p, t, res, age in cur.fetchall()]


def find(role=None, product=None, tenant_id=None):
    rows = roster(active_only=True, tenant_id=tenant_id)
    return [x for x in rows if (role is None or x["role"] == role) and (product is None or x["product"] == product)]


def _prefix(glob):
    return glob.split("*", 1)[0].rstrip("/")


def _overlap(a_res, b_res):
    """Two resource sets overlap if any pair shares a path prefix (one contains the other)."""
    hits = []
    for ra in a_res:
        pa = _prefix(ra)
        for rb in b_res:
            pb = _prefix(rb)
            if pa and pb and (pa == pb or pa.startswith(pb + "/") or pb.startswith(pa + "/") or pa.startswith(pb) or pb.startswith(pa)):
                hits.append(ra if len(ra) >= len(rb) else rb)
    return hits


def conflicts(tenant_id=None):
    """Active agents on the SAME product whose resource claims overlap -> they may step on each other."""
    rows = roster(active_only=True, tenant_id=tenant_id)
    out, seen = [], set()
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            a, b = rows[i], rows[j]
            if a["product"] and a["product"] == b["product"] and a["agent_id"] != b["agent_id"]:
                ov = _overlap(a["resources"], b["resources"])
                if ov:
                    key = tuple(sorted([a["agent_id"], b["agent_id"]]))
                    if key not in seen:
                        seen.add(key)
                        out.append({"product": a["product"], "resource": sorted(set(ov))[0],
                                    "agents": list(key)})
    return out


def contact(frm, to, intent, content, tenant_id=None):
    """Direct, brokered, durable message (no socket). Goes into the conversation log + the recipient's
    inbox (exactly-once). The recipient need not be online — it processes it when it next runs."""
    _ensure()
    mid = f"dm-{int(time.time()*1000)}"
    tenant_id = tenant_id or _tenant_for_agent(to) or _tenant_for_agent(frm)
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO conversations
                         (conversation_id, message_id, intent, sender, recipient, content, tenant_id)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                    (f"dm-{frm}-{to}", mid, intent, frm, to, json.dumps({"text": content}), tenant_id))
        cur.execute("""INSERT INTO inbox (subscriber, message_id, tenant_id)
                       VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""", (to, mid, tenant_id))
    return {"message_id": mid, "from": frm, "to": to, "intent": intent}


def _main(a):
    if not a or a[0] == "roster":
        for x in roster():
            print(f"  {x['agent_id']:26} {x['role']:18} {x['status']:6} on {x['product'] or '-'} :: {x['task'] or '-'}  {x['resources']}")
    elif a[0] == "find":
        role = a[a.index("--role") + 1] if "--role" in a else None
        product = a[a.index("--product") + 1] if "--product" in a else None
        print(json.dumps(find(role, product), indent=2))
    elif a[0] == "conflicts":
        cs = conflicts()
        print(json.dumps(cs, indent=2) if cs else "no conflicts")
    elif a[0] == "contact":
        print(json.dumps(contact(a[1], a[2], a[3], a[4]), indent=2))
    elif a[0] == "register":
        res = a[5].split(",") if len(a) > 5 else []
        register(a[1], a[2], a[3] if len(a) > 3 else None, a[4] if len(a) > 4 else None, res)
        print("registered")
    elif a[0] == "selftest":
        _ensure()
        import os
        import billing
        t = billing.signup("directory-selftest")
        tid = t["tenant_id"]
        p = f"dt-{os.urandom(3).hex()}"
        msg = None
        try:
            with tenant_connection(tid) as c, c.cursor() as cur:
                cur.execute("INSERT INTO tenant_products (product, tenant_id) VALUES (%s,%s)", (p, tid))
            register(f"builder@{p}", "builder", p, "BUILD", ["src/**", "tests/**"])
            register(f"refactorer@{p}", "staff-engineer", p, "REFACTOR", ["src/**"])
            register(f"docs@{p}", "technical-writer", p, "DOCS", ["docs/**"])
            cs = conflicts(tenant_id=tid)
            conflict_found = any(c["product"] == p for c in cs)
            msg = contact(f"builder@{p}", f"refactorer@{p}", "conflict",
                          "we both edit src/** — split files?")
            scoped = roster(tenant_id=tid)
            tenant_scoped = scoped and all(x["product"] == p for x in scoped if x["agent_id"].endswith(f"@{p}"))
            with tenant_connection(tid) as c, c.cursor() as cur:
                cur.execute("SELECT tenant_id FROM conversations WHERE message_id=%s", (msg["message_id"],))
                conv_tid = cur.fetchone()
                cur.execute("SELECT tenant_id FROM inbox WHERE message_id=%s", (msg["message_id"],))
                inbox_tid = cur.fetchone()
            contact_tagged = conv_tid and conv_tid[0] == tid and inbox_tid and inbox_tid[0] == tid
            # no false conflict between builder and docs (disjoint paths)
            no_false = not any(set(c["agents"]) == {f"builder@{p}", f"docs@{p}"} for c in cs)
            ok = conflict_found and no_false and msg["message_id"].startswith("dm-") and tenant_scoped and contact_tagged
            print(f"conflict detected on src/**: {conflict_found}; no false (docs disjoint): {no_false}; "
                  f"tenant_scoped={tenant_scoped}; contact_tagged={contact_tagged}; direct contact: {msg['message_id']}")
        finally:
            with tenant_connection(tid) as c, c.cursor() as cur:
                if msg:
                    cur.execute("DELETE FROM inbox WHERE message_id=%s", (msg["message_id"],))
                    cur.execute("DELETE FROM conversations WHERE message_id=%s", (msg["message_id"],))
                cur.execute("DELETE FROM directory WHERE product=%s", (p,))
                cur.execute("DELETE FROM tenant_products WHERE product=%s", (p,))
            with connection() as c, c.cursor() as cur:
                cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
        print("PASS: directory presence + conflict detection + direct contact ✅" if ok else "FAIL")
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    _main(sys.argv[1:])
