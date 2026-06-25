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


def append(actor, action, resource="", decision="executed", payload=None):
    """Append one tamper-evident entry; returns (id, entry_hash)."""
    db, key = _cfg()
    payload = payload or {}
    with psycopg.connect(db) as conn, conn.cursor() as cur:
        # serialize appends so the chain has no races
        cur.execute("SELECT pg_advisory_xact_lock(742042)")
        cur.execute("SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        prev_hash = row[0] if row else ""
        canonical = _canonical(actor, action, resource, decision, payload, prev_hash)
        entry_hash = _chain_hash(key, canonical)
        cur.execute(
            """INSERT INTO audit_log (actor, action, resource, decision, payload, prev_hash, entry_hash)
               VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (actor, action, resource, decision, json.dumps(payload), prev_hash, entry_hash),
        )
        new_id = cur.fetchone()[0]
        conn.commit()
        return new_id, entry_hash


def reseal():
    """Recompute the prev_hash/entry_hash chain in id order. Idempotent (a no-op on an intact chain).
    Use ONLY to repair a chain broken by legitimate row-deletion test pollution — NOT to hide tampering."""
    db, key = _cfg()
    fixed = 0
    with psycopg.connect(db) as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(742042)")
        cur.execute("SELECT id, actor, action, resource, decision, payload, prev_hash, entry_hash FROM audit_log ORDER BY id")
        prev = ""
        for rid, actor, action, resource, decision, payload, prev_hash, entry_hash in cur.fetchall():
            want = _chain_hash(key, _canonical(actor, action, resource, decision, payload, prev))
            if prev_hash != prev or entry_hash != want:
                cur.execute("UPDATE audit_log SET prev_hash=%s, entry_hash=%s WHERE id=%s", (prev, want, rid))
                fixed += 1
            prev = want
        conn.commit()
    return fixed


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
        n = reseal()
        print(f"resealed {n} row(s); chain recomputed in id order")
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
