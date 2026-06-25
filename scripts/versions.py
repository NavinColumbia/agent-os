#!/usr/bin/env python3
"""versions.py — durable VERSIONING + ROLLBACK of a built product ("undo that, go back to yesterday").

Every snapshot tars the product's repo dir into a versions store and records a product_versions row with
the next version number. rollback() is itself reversible: before it overwrites the live repo it auto-saves
the current state as a new version, then extracts the chosen version's tar over the (cleared) repo dir.
Ownership is checked against tenant_products so a tenant can only snapshot/roll back its own products.

    versions.py selftest
    versions.py json <tenant_id> <product>      # list versions for the tenant's product
Run with the agent-os venv python.
"""
import json
import shutil
import sys
import tarfile
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit    # noqa: E402
import factory  # noqa: E402  (factory.PRODUCTS = the products dir)

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)

VSTORE = factory.PRODUCTS.parent / "_versions"   # where snapshot tar.gz files live
SKIP = {".git", "__pycache__", "node_modules"}   # never snapshot these


def _ensure():
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS product_versions (
            id BIGSERIAL PRIMARY KEY, tenant_id TEXT, product TEXT, version INT, label TEXT,
            snapshot_path TEXT, created_at TIMESTAMPTZ DEFAULT now())""")
        c.commit()


def _owns(tid, product):
    """True iff `product` belongs to tenant `tid` per tenant_products."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT 1 FROM tenant_products WHERE product=%s AND tenant_id=%s", (product, tid))
        return cur.fetchone() is not None


def _next_version(product):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM product_versions WHERE product=%s", (product,))
        return cur.fetchone()[0]


def _tar_filter(ti):
    # drop any member whose path crosses a skipped dir
    parts = set(Path(ti.name).parts)
    return None if parts & SKIP else ti


def snapshot(tid, product, label=""):
    """Tar the product's repo dir and record a product_versions row at the next version number."""
    _ensure()
    if not _owns(tid, product):
        return {"error": "not owner"}
    repo = factory.PRODUCTS / product
    if not repo.is_dir():
        return {"error": "no repo"}
    VSTORE.mkdir(parents=True, exist_ok=True)
    version = _next_version(product)
    snap = VSTORE / f"{product}.v{version}.tar.gz"
    with tarfile.open(snap, "w:gz") as tar:
        tar.add(repo, arcname=".", filter=_tar_filter)
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO product_versions (tenant_id, product, version, label, snapshot_path)
                       VALUES (%s,%s,%s,%s,%s)""", (tid, product, version, label, str(snap)))
        c.commit()
    return {"version": version, "label": label, "snapshot_path": str(snap)}


def versions(tid, product):
    """List a tenant's product versions (newest first) with on-disk snapshot sizes."""
    _ensure()
    if not _owns(tid, product):
        return []
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT version, label, created_at, snapshot_path FROM product_versions
                       WHERE tenant_id=%s AND product=%s ORDER BY version DESC""", (tid, product))
        rows = cur.fetchall()
    out = []
    for version, label, created_at, path in rows:
        p = Path(path)
        size = p.stat().st_size if p.exists() else 0
        out.append({"version": version, "label": label, "created_at": str(created_at), "size_bytes": size})
    return out


def rollback(tid, product, version):
    """Restore `product` to `version`. Auto-saves the current state first so rollback is reversible."""
    _ensure()
    if not _owns(tid, product):
        return {"error": "not owner"}
    repo = factory.PRODUCTS / product
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT snapshot_path FROM product_versions
                       WHERE tenant_id=%s AND product=%s AND version=%s""", (tid, product, version))
        row = cur.fetchone()
    if not row:
        return {"error": "unknown version"}
    snap = Path(row[0])
    if not snap.exists():
        return {"error": "snapshot missing"}
    # snapshot current state so the rollback itself can be undone
    if repo.is_dir():
        snapshot(tid, product, label="pre-rollback auto-save")
    # clear then extract the chosen version over the repo dir
    if repo.exists():
        shutil.rmtree(repo)
    repo.mkdir(parents=True, exist_ok=True)
    with tarfile.open(snap, "r:gz") as tar:
        tar.extractall(repo)
    audit.append(actor="versions", action="Rollback", resource=product, decision="executed",
                 payload={"tenant_id": tid, "restored_version": version})
    return {"ok": True, "restored_version": version}


def _selftest():
    import billing
    import os
    tid = billing.signup("versions-selftest", "free")["tenant_id"]
    product = "vtest-" + os.urandom(3).hex()
    foreign = "vtest-foreign-" + os.urandom(3).hex()
    repo = factory.PRODUCTS / product
    f = repo / "app.txt"
    try:
        _ensure()
        factory.PRODUCTS.mkdir(parents=True, exist_ok=True)
        repo.mkdir(parents=True, exist_ok=True)
        f.write_text("v1")
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("INSERT INTO tenant_products (product, tenant_id) VALUES (%s,%s)", (product, tid))
            c.commit()

        s1 = snapshot(tid, product)                         # v1
        f.write_text("v2")
        s2 = snapshot(tid, product)                         # v2
        before = versions(tid, product)
        rb = rollback(tid, product, 1)                      # restore v1
        restored = f.read_text()
        after = versions(tid, product)

        # ownership guard: a product the tenant does not own -> error (no snapshot taken)
        guard = snapshot(tid, foreign)

        ok = (s1["version"] == 1 and s2["version"] == 2
              and rb.get("ok") and restored == "v1"
              and len(after) > len(before)                  # pre-rollback auto-save added a version
              and any(v["label"] == "pre-rollback auto-save" for v in after)
              and "error" in guard)
        print(f"v1={s1['version']} v2={s2['version']} restored={restored!r} "
              f"versions {len(before)}->{len(after)} guard={guard}")
        print("PASS: snapshot/rollback restores prior version, auto-saves first, owner-checked ✅"
              if ok else "FAIL")
        sys.exit(0 if ok else 1)
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM product_versions WHERE tenant_id=%s OR product IN (%s,%s)",
                        (tid, product, foreign))
            cur.execute("DELETE FROM tenant_products WHERE product IN (%s,%s)", (product, foreign))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
            c.commit()
        for pat in (f"{product}.v*.tar.gz", f"{foreign}.v*.tar.gz"):
            for t in VSTORE.glob(pat):
                t.unlink(missing_ok=True)
        if repo.exists():
            shutil.rmtree(repo, ignore_errors=True)


def _main(a):
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "json" and len(a) > 2:
        print(json.dumps(versions(a[1], a[2]), indent=2))
    else:
        sys.exit("usage: versions.py selftest | json <tenant_id> <product>")


if __name__ == "__main__":
    _main(sys.argv[1:])
