#!/usr/bin/env python3
"""objstore.py — content-addressed object store for the comm fabric (ADR 0005 FilePart).

Agents share images, PDFs, or any binary/object BY REFERENCE: put() returns a sha256 id; that id
travels inside a message (as a FilePart); the receiver calls get(id). Content-addressing dedups
automatically (same bytes → same id → stored once). Per-object TTL + gc() give record expiry.

Small/medium objects live in Postgres BYTEA (one store, transactional). For very large media, store
on disk keyed by the same sha256 and keep only metadata here (documented; not needed yet).

    objstore.py put <file> [ttl_seconds]
    objstore.py get <id> <out_file>
    objstore.py gc
    from objstore import put, get, meta, as_part, gc
Run with the agent-os venv python.
"""
import hashlib
import sys
from pathlib import Path

import psycopg

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
_cfg = {l.split("=", 1)[0].strip(): l.split("=", 1)[1].strip()
        for l in ENV.read_text().splitlines() if l.strip() and not l.startswith("#") and "=" in l}
DB = _cfg["DATABASE_URL"]


def _fernet():
    from cryptography.fernet import Fernet
    return Fernet(_cfg["VAULT_KEY"].encode())


def put(data, mime=None, ttl_seconds=None, encrypt=False):
    """Store bytes (or a file path); returns the content id (sha256 of plaintext). Dedup.
    encrypt=True stores the bytes Fernet-encrypted at rest (id still addresses the plaintext)."""
    if isinstance(data, (str, Path)) and Path(data).exists():
        if mime is None:
            ext = Path(data).suffix.lower()
            mime = {".png": "image/png", ".jpg": "image/jpeg", ".pdf": "application/pdf",
                    ".json": "application/json", ".csv": "text/csv"}.get(ext, "application/octet-stream")
        data = Path(data).read_bytes()
    bid = hashlib.sha256(data).hexdigest()
    stored = _fernet().encrypt(data) if encrypt else data
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute(
            """INSERT INTO blobs (id, mime, size_bytes, data, encrypted, expires_at)
               VALUES (%s,%s,%s,%s,%s, CASE WHEN %s::int IS NULL THEN NULL ELSE now() + (%s::int || ' seconds')::interval END)
               ON CONFLICT (id) DO UPDATE SET expires_at = EXCLUDED.expires_at""",
            (bid, mime, len(data), stored, encrypt, ttl_seconds, ttl_seconds),
        )
        c.commit()
    return bid


def get(blob_id):
    """Return bytes, or None if missing or expired."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT data, encrypted FROM blobs WHERE id=%s AND (expires_at IS NULL OR expires_at > now())", (blob_id,))
        r = cur.fetchone()
        if not r:
            return None
        return _fernet().decrypt(bytes(r[0])) if r[1] else bytes(r[0])


def meta(blob_id):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT id, mime, size_bytes, created_at, expires_at FROM blobs WHERE id=%s", (blob_id,))
        r = cur.fetchone()
        if not r:
            return None
        return {"id": r[0], "mime": r[1], "size_bytes": r[2], "created_at": str(r[3]), "expires_at": str(r[4]) if r[4] else None}


def as_part(blob_id):
    """A FilePart to embed in a comm-fabric Message (reference, not inline bytes)."""
    m = meta(blob_id)
    return {"kind": "file", "blob_id": blob_id, "mime": m["mime"] if m else None, "size": m["size_bytes"] if m else None}


def gc():
    """Delete expired objects; returns count removed."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("DELETE FROM blobs WHERE expires_at IS NOT NULL AND expires_at <= now()")
        n = cur.rowcount; c.commit(); return n


def _main(argv):
    if not argv:
        sys.exit("usage: objstore.py put <file> [ttl] | get <id> <out> | gc")
    if argv[0] == "put":
        ttl = int(argv[2]) if len(argv) > 2 else None
        bid = put(argv[1], ttl_seconds=ttl)
        print(f"stored {bid} {meta(bid)}")
    elif argv[0] == "get":
        d = get(argv[1])
        if d is None:
            print("MISSING or EXPIRED"); sys.exit(1)
        Path(argv[2]).write_bytes(d); print(f"wrote {len(d)} bytes -> {argv[2]}")
    elif argv[0] == "gc":
        print(f"gc removed {gc()} expired object(s)")
    elif argv[0] == "test":
        blob = b"agent-os object" * 50
        b = put(blob, mime="application/octet-stream", ttl_seconds=3600)
        assert get(b) == blob and put(blob) == b           # round-trip + dedup
        e = put(b"ephemeral", ttl_seconds=-1)
        assert gc() >= 1 and get(e) is None                # TTL + GC
        assert "blob_id" in as_part(b)                     # FilePart ref
        print("PASS: objstore round-trip + dedup + TTL/GC + FilePart ✅")


if __name__ == "__main__":
    _main(sys.argv[1:])
