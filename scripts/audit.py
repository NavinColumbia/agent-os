#!/usr/bin/env python3
"""audit.py — append-only, hash-chained, tamper-evident audit log (ADR 0004 K4).

Every agent decision/action is recorded with an HMAC-SHA256 chain:
  entry_hash = HMAC(key, canonical(business_fields) || prev_hash)
so ANY edit/delete/reorder of history is detectable by verify() — even by an actor
with write access to Postgres. The HMAC key lives in .env.local (gitignored), never in git.

    from audit import append, verify
    append(actor="builder", action="Bash", resource="git push origin main", decision="deny",
           payload={"reason": "protected branch"})
    ok, err = verify()    # (True, None) or (False, "chain broken at id=N")

CLI:  audit.py append <actor> <action> <resource> <decision> ['<json payload>']
      audit.py verify
      audit.py tail [N]
Run with the agent-os venv python.
"""
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path

import psycopg

ENV_LOCAL = Path.home() / "projects" / "agent-os" / ".env.local"


def _cfg():
    cfg = {}
    if ENV_LOCAL.exists():
        for line in ENV_LOCAL.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    db = os.environ.get("DATABASE_URL") or cfg.get("DATABASE_URL")
    key = os.environ.get("AUDIT_HMAC_KEY") or cfg.get("AUDIT_HMAC_KEY")
    if not db or not key:
        sys.exit("ERROR: DATABASE_URL and AUDIT_HMAC_KEY required (env or .env.local)")
    return db, key.encode()


def _canonical(actor, action, resource, decision, payload, prev_hash):
    # Deterministic serialization so the hash is reproducible across processes.
    body = {
        "actor": actor, "action": action, "resource": resource or "",
        "decision": decision, "payload": payload or {},
    }
    blob = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return blob + "||" + (prev_hash or "")


def _chain_hash(key, canonical):
    return hmac.new(key, canonical.encode(), hashlib.sha256).hexdigest()


def append(actor, action, resource="", decision="executed", payload=None, tenant_id=None):
    """Append one tamper-evident entry; returns (id, entry_hash).

    ADDITIVE per-tenant sub-chain (C2): the GLOBAL chain (prev_hash/entry_hash) is computed EXACTLY as before
    (unchanged — global verify() and every existing trail stay valid). In parallel, when a tenant is derivable
    (arg or payload.tenant/tenant_id), we ALSO link this row into that tenant's OWN hash sub-chain
    (t_prev_hash/t_entry_hash), so each tenant has an independently-verifiable audit trail (verify_tenant).
    Untenanted rows (platform events) simply leave the tenant columns NULL — the global chain covers them."""
    db, key = _cfg()
    payload = payload or {}
    # derive the tenant for the sub-chain: explicit arg > payload.tenant/tenant_id > actor when the actor IS a
    # tenant (ids are 't-…'; no role/system actor uses that prefix), so tenant-actor events (account export/
    # delete, settings, etc.) land in the tenant's own trail too — without threading tid through every caller.
    tid = (tenant_id or ((payload.get("tenant") or payload.get("tenant_id")) if isinstance(payload, dict) else None)
           or (actor if isinstance(actor, str) and actor.startswith("t-") else None))
    with psycopg.connect(db) as conn, conn.cursor() as cur:
        # serialize appends so the chain has no races
        cur.execute("SELECT pg_advisory_xact_lock(742042)")
        cur.execute("SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        prev_hash = row[0] if row else ""
        canonical = _canonical(actor, action, resource, decision, payload, prev_hash)
        entry_hash = _chain_hash(key, canonical)          # GLOBAL chain — UNCHANGED
        # per-tenant sub-chain: link to THIS tenant's previous entry (independent of the global order).
        t_prev = t_entry = None
        if tid:
            cur.execute("SELECT t_entry_hash FROM audit_log WHERE tenant_id=%s ORDER BY id DESC LIMIT 1", (str(tid),))
            r2 = cur.fetchone()
            t_prev = (r2[0] if r2 and r2[0] else "")
            t_entry = _chain_hash(key, _canonical(actor, action, resource, decision, payload, t_prev)
                                  + "||TENANT:" + str(tid))
        cur.execute(
            """INSERT INTO audit_log (actor, action, resource, decision, payload, prev_hash, entry_hash,
                                      tenant_id, t_prev_hash, t_entry_hash)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (actor, action, resource, decision, json.dumps(payload), prev_hash, entry_hash,
             (str(tid) if tid else None), t_prev, t_entry),
        )
        new_id = cur.fetchone()[0]
        conn.commit()
        return new_id, entry_hash


def reseal(operator=None, break_glass=False, reason=""):
    """Repair a chain broken ONLY by legitimate row DELETIONS (gaps) — under break-glass.

    SECURITY (finding #51): the previous reseal() blindly recomputed entry_hash over whatever
    business content happened to sit in each row, so anyone holding the HMAC key could LAUNDER a
    tamper simply by re-running it — the chain's entire value is that entry_hash binds the
    *business fields*, and re-signing changed fields silently destroys that tamper-evidence.

    This version is deliberately narrow and fail-closed:
      * It is BREAK-GLASS only: requires an explicit operator identity AND break_glass=True, so it
        can never run by accident or from automation.
      * Pass 1 proves every surviving row is individually authentic: entry_hash must equal
        HMAC(canonical(its own business fields) || its own stored prev_hash). A failure here is
        real content tampering (or forgery), NOT a deletion gap — we REFUSE and abort without
        touching a single row. reseal must never re-sign changed business fields.
      * Pass 2 only relinks prev_hash across the deletion gaps of those proven-authentic rows.
        Business fields are NEVER altered; a row's entry_hash changes solely because the prev_hash
        it binds legitimately moved to the new surviving predecessor.
      * It appends a tamper-evident 'AuditResealed' record (who / why / which ids) so the repair
        is itself attributable and chained.

    Idempotent: a no-op (and no AuditResealed record) on an already-intact chain.
    """
    if not break_glass or not operator:
        raise PermissionError(
            "reseal() is break-glass only: pass operator=<name> and break_glass=True. It exists "
            "solely to relink prev_hash across legitimately-deleted rows; it will NOT re-sign "
            "tampered content. If verify() reports an entry_hash mismatch, investigate — do not reseal.")
    db, key = _cfg()
    relinked = []
    with psycopg.connect(db) as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(742042)")
        cur.execute("SELECT id, actor, action, resource, decision, payload, prev_hash, entry_hash FROM audit_log ORDER BY id")
        rows = cur.fetchall()
        # Pass 1 (fail-closed): every surviving row must be self-consistent BEFORE we touch anything.
        # Self-consistency = entry_hash binds this row's own business fields and its own stored
        # prev_hash. If that fails, the business content was tampered (not merely a deleted neighbour);
        # re-signing it would launder the tamper, so we abort the whole reseal.
        for rid, actor, action, resource, decision, payload, prev_hash, entry_hash in rows:
            self_hash = _chain_hash(key, _canonical(actor, action, resource, decision, payload, prev_hash))
            if self_hash != entry_hash:
                raise PermissionError(
                    f"reseal REFUSED: row id={rid} fails self-consistency — its business fields were "
                    f"altered after signing (content tamper, not a deletion gap). reseal will not "
                    f"re-sign changed content; investigate the breach instead.")
        # Pass 2: relink prev_hash across deletion gaps for these proven-authentic rows only.
        prev = ""
        for rid, actor, action, resource, decision, payload, prev_hash, entry_hash in rows:
            if prev_hash != prev:
                new_hash = _chain_hash(key, _canonical(actor, action, resource, decision, payload, prev))
                cur.execute("UPDATE audit_log SET prev_hash=%s, entry_hash=%s WHERE id=%s", (prev, new_hash, rid))
                relinked.append(rid)
                prev = new_hash
            else:
                prev = entry_hash
        conn.commit()
    if relinked:
        # Tamper-evident, chained record of the break-glass repair (who/why/what).
        append(actor=f"operator:{operator}", action="AuditResealed", resource="audit_log",
               decision="break_glass",
               payload={"operator": operator, "reason": reason,
                        "relinked_ids": relinked, "count": len(relinked)})
    return len(relinked)


def verify():
    """Walk the chain; return (True, None) if intact else (False, reason)."""
    db, key = _cfg()
    with psycopg.connect(db) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, actor, action, resource, decision, payload, prev_hash, entry_hash FROM audit_log ORDER BY id"
        )
        prev = ""
        for r in cur.fetchall():
            rid, actor, action, resource, decision, payload, prev_hash, entry_hash = r
            if prev_hash != prev:
                return False, f"prev_hash mismatch at id={rid} (chain reordered/deleted)"
            expect = _chain_hash(key, _canonical(actor, action, resource, decision, payload, prev_hash))
            if expect != entry_hash:
                return False, f"entry_hash mismatch at id={rid} (row tampered)"
            prev = entry_hash
        return True, None


def verify_tenant(tid):
    """Walk THIS tenant's audit SUB-chain; return (True, None) if intact else (False, reason). Independently
    verifiable: a tenant (or their auditor) can cryptographically prove their own complete, unbroken trail
    without trusting the platform or seeing any other tenant's rows (C2 per-tenant audit chains)."""
    db, key = _cfg()
    with psycopg.connect(db) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, actor, action, resource, decision, payload, t_prev_hash, t_entry_hash "
            "FROM audit_log WHERE tenant_id=%s ORDER BY id", (str(tid),))
        prev = ""
        for r in cur.fetchall():
            rid, actor, action, resource, decision, payload, t_prev_hash, t_entry_hash = r
            if t_prev_hash != prev:
                return False, f"t_prev_hash mismatch at id={rid} (tenant chain reordered/deleted)"
            expect = _chain_hash(key, _canonical(actor, action, resource, decision, payload, prev)
                                 + "||TENANT:" + str(tid))
            if expect != t_entry_hash:
                return False, f"t_entry_hash mismatch at id={rid} (tenant row tampered)"
            prev = t_entry_hash
        return True, None


def _main(argv):
    if not argv:
        sys.exit("usage: audit.py append <actor> <action> <resource> <decision> ['<json>'] | verify | tail [N]")
    cmd = argv[0]
    if cmd == "append":
        actor, action, resource, decision = (argv[1:5] + ["", "", "", "executed"])[:4]
        payload = json.loads(argv[5]) if len(argv) > 5 else {}
        rid, h = append(actor, action, resource, decision, payload)
        print(f"appended id={rid} hash={h[:16]}…")
    elif cmd == "verify":
        ok, err = verify()
        print("AUDIT CHAIN INTACT ✅" if ok else f"AUDIT CHAIN BROKEN ❌ — {err}")
        sys.exit(0 if ok else 1)
    elif cmd == "reseal":
        # Break-glass only:  audit.py reseal <operator> '<reason>' --break-glass
        if "--break-glass" not in argv:
            sys.exit("reseal is break-glass only: audit.py reseal <operator> '<reason>' --break-glass "
                     "(relinks prev_hash across legitimately-deleted rows; never re-signs tampered content)")
        rest = [a for a in argv[1:] if a != "--break-glass"]
        operator = rest[0] if rest else ""
        if not operator:
            sys.exit("reseal requires an operator: audit.py reseal <operator> '<reason>' --break-glass")
        reason = rest[1] if len(rest) > 1 else ""
        n = reseal(operator=operator, break_glass=True, reason=reason)
        print(f"resealed {n} row(s) — break-glass by {operator}"
              + ("" if n else " (chain already intact; no-op)"))
    elif cmd == "tail":
        db, _ = _cfg()
        n = int(argv[1]) if len(argv) > 1 else 10
        with psycopg.connect(db) as conn, conn.cursor() as cur:
            cur.execute("SELECT id, ts, actor, action, decision FROM audit_log ORDER BY id DESC LIMIT %s", (n,))
            for row in cur.fetchall():
                print(row)
    else:
        sys.exit(f"unknown command: {cmd}")


if __name__ == "__main__":
    _main(sys.argv[1:])
