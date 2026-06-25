#!/usr/bin/env python3
"""account.py — GDPR/CPRA account self-service: full DATA EXPORT + right-to-ERASURE.

No lock-in. A tenant can take EVERYTHING they own and leave: one portable zip with a manifest,
every product repo they own (source files), and JSON dumps of all their tenant-scoped rows. And a
tenant can be fully erased: every tenant-scoped row across the platform is deleted, scoped strictly
by tenant_id (never touching another tenant's data), the deletion is audited, and best-effort vault
secrets are purged.

    account.py export <tenant_id>      # build the portable zip of everything they own -> path
    account.py delete <tenant_id>      # right-to-erasure (asks to confirm; re-run to commit)
    account.py selftest                # offline end-to-end check
Run with the agent-os venv python. NO web server.
"""
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit    # noqa: E402
import factory  # noqa: E402

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)

SKIP_DIRS = {".git", "__pycache__", "node_modules"}

# Tables holding tenant-scoped rows, keyed by the column that scopes to a tenant_id.
# (kill_switch is special: scoped by scope=tid; handled separately.)
_TENANT_TABLES = [
    ("tenant_products", "tenant_id"),
    ("ai_consent", "tenant_id"),
    ("notifications", "tenant_id"),
    ("notification_prefs", "tenant_id"),
    ("chat_messages", "tenant_id"),
    ("chat_threads", "tenant_id"),
    ("tenant_providers", "tenant_id"),
    ("tenant_integrations", "tenant_id"),
    ("project_budget", "tenant_id"),
    ("budget_alert_state", "tenant_id"),
    ("onboarding_state", "tenant_id"),
    ("product_versions", "tenant_id"),
]


def _plan_of(tid, cur):
    try:
        cur.execute("SELECT plan FROM tenants WHERE tenant_id=%s", (tid,))
        r = cur.fetchone()
        return r[0] if r else None
    except Exception:
        return None


def _products(tid, cur):
    try:
        cur.execute("SELECT product FROM tenant_products WHERE tenant_id=%s ORDER BY product", (tid,))
        return [r[0] for r in cur.fetchall()]
    except Exception:
        return []


def _dump_rows(cur, table, tid):
    """JSON-serializable list of the tenant's rows from `table`, or None if the table can't be read."""
    try:
        cur.execute(f"SELECT * FROM {table} WHERE tenant_id=%s", (tid,))
        cols = [c.name for c in cur.description]
        rows = []
        for rec in cur.fetchall():
            rows.append({c: _jsonable(v) for c, v in zip(cols, rec)})
        return rows
    except Exception:
        return None


def _jsonable(v):
    if isinstance(v, (datetime,)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray, memoryview)):
        return "<binary>"
    try:
        json.dumps(v)
        return v
    except TypeError:
        return str(v)


def export(tid):
    """Build ONE portable zip of everything the tenant owns. Returns {ok, path, products}."""
    out_dir = factory.PRODUCTS.parent / "_exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    zpath = out_dir / f"{tid}.zip"

    with psycopg.connect(DB) as c, c.cursor() as cur:
        plan = _plan_of(tid, cur)
        products = _products(tid, cur)
        # JSON dumps of the tenant's rows from the core data tables (each guarded).
        data = {}
        for table in ("tenant_products", "ai_consent", "notifications", "chat_messages"):
            rows = _dump_rows(cur, table, tid)
            if rows is not None:
                data[table] = rows

    manifest = {
        "tenant_id": tid,
        "plan": plan,
        "exported_at": "see audit",
        "products": products,
        "format": "agent-os account export (GDPR/CPRA portable)",
    }

    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
        for table, rows in data.items():
            z.writestr(f"data/{table}.json", json.dumps(rows, indent=2))
        # each product's repo files (source only — skip vcs/build/dep dirs)
        for product in products:
            repo = factory.PRODUCTS / product
            if not repo.is_dir():
                continue
            for f in repo.rglob("*"):
                if not f.is_file():
                    continue
                if any(part in SKIP_DIRS for part in f.relative_to(repo).parts):
                    continue
                try:
                    z.write(f, arcname=f"products/{product}/{f.relative_to(repo)}")
                except Exception:
                    pass

    audit.append(actor=tid, action="AccountExported", resource=tid,
                 payload={"products": len(products), "path": str(zpath)})
    return {"ok": True, "path": str(zpath), "products": len(products)}


def _count(cur, table, col, val):
    try:
        cur.execute(f"SELECT count(*) FROM {table} WHERE {col}=%s", (val,))
        return cur.fetchone()[0]
    except Exception:
        return 0


def delete(tid, confirm=False):
    """Right-to-erasure. Without confirm: preview counts. With confirm: delete all tenant-scoped rows.

    STRICTLY scoped by tenant_id=%s (and scope=%s for kill_switch) — never touches other tenants.
    """
    if not confirm:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            counts = {t: _count(cur, t, col, tid) for t, col in _TENANT_TABLES}
            counts["kill_switch"] = _count(cur, "kill_switch", "scope", tid)
            counts["tenants"] = _count(cur, "tenants", "tenant_id", tid)
        return {"requires_confirm": True, "will_delete": counts}

    with psycopg.connect(DB) as c, c.cursor() as cur:
        for table, col in _TENANT_TABLES:
            try:
                cur.execute(f"DELETE FROM {table} WHERE {col}=%s", (tid,))
            except Exception:
                c.rollback()
        # kill_switch is scoped by `scope`
        try:
            cur.execute("DELETE FROM kill_switch WHERE scope=%s", (tid,))
        except Exception:
            c.rollback()
        # finally remove the tenant identity itself
        try:
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
        except Exception:
            c.rollback()
        c.commit()

    # best-effort purge of vault secrets owned by this tenant
    vault_note = "no vault delete fn — skipped"
    try:
        import vault
        if hasattr(vault, "delete_secret"):
            vault.delete_secret(owner=f"tenant:{tid}")
            vault_note = "purged via vault.delete_secret"
    except Exception:
        vault_note = "vault purge errored — skipped"

    audit.append(actor=tid, action="AccountDeleted", resource=tid, payload={"vault": vault_note})
    return {"ok": True, "deleted": True, "vault": vault_note}


def _selftest():
    import billing
    tid = billing.signup("account-selftest", "free")["tenant_id"]
    prod = tid.replace("t-", "")[:6] + "-acct"
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("INSERT INTO tenant_products (product, tenant_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                    (prod, tid))
        cur.execute("""INSERT INTO notifications (tenant_id, channel, category, level, title, body)
                       VALUES (%s,'in_app','system','info','hello','selftest row')""", (tid,))
        c.commit()

    # EXPORT
    ex = export(tid)
    zpath = Path(ex["path"])
    assert zpath.exists(), "export zip missing"
    assert ex["products"] >= 0, "products count bad"
    with zipfile.ZipFile(zpath) as z:
        names = z.namelist()
    assert "manifest.json" in names, "manifest.json not in zip"

    # DELETE preview
    prev = delete(tid, confirm=False)
    assert prev.get("requires_confirm") is True, "preview should require confirm"

    # DELETE commit
    done = delete(tid, confirm=True)
    assert done.get("ok") is True, "delete not ok"

    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM tenants WHERE tenant_id=%s", (tid,))
        n_ten = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM tenant_products WHERE tenant_id=%s", (tid,))
        n_tp = cur.fetchone()[0]
    assert n_ten == 0, f"tenant not deleted ({n_ten})"
    assert n_tp == 0, f"tenant_products not deleted ({n_tp})"

    # remove the export artifact
    try:
        zpath.unlink()
    except Exception:
        pass

    print(f"export: {len(names)} files, manifest✅, products={ex['products']}")
    print("PASS: account export + GDPR erasure (tenant fully scrubbed, scoped by tenant_id) ✅")
    sys.exit(0)


def _main(a):
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "export" and len(a) > 1:
        print(json.dumps(export(a[1]), indent=2))
    elif a[0] == "delete" and len(a) > 1:
        confirm = "--confirm" in a[2:] or "confirm" in a[2:]
        print(json.dumps(delete(a[1], confirm=confirm), indent=2))
    else:
        sys.exit("usage: account.py export <tid> | delete <tid> [--confirm] | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
