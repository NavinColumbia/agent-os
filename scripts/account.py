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
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from psycopg import sql

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit    # noqa: E402
import factory  # noqa: E402
from dbpool import connection, tenant_connection  # noqa: E402

SKIP_DIRS = {".git", "__pycache__", "node_modules"}

# Core tables included in portable exports. Erasure does not use this list: the
# live catalog is authoritative so newly added tenant-owned tables cannot escape
# deletion merely because this module was not hand-updated.
_EXPORT_TABLES = [
    ("tenant_products", "tenant_id"),
    ("ai_consent", "tenant_id"),
    ("notifications", "tenant_id"),
    ("notification_deliveries", "tenant_id"),
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

# The tamper-evident audit chain is an explicit compliance record. Deleting or
# rewriting a historical link would invalidate every later hash, so account
# erasure retains it under the platform's legal/audit retention policy and makes
# that exception visible in the result instead of falsely claiming zero rows.
_RETAINED_COMPLIANCE_TABLES = {"audit_log"}


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

    with tenant_connection(tid) as c, c.cursor() as cur:
        plan = _plan_of(tid, cur)
        products = _products(tid, cur)
        # JSON dumps of the tenant's rows from the core data tables (each guarded).
        data = {}
        for table, _column in _EXPORT_TABLES:
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
                 payload={"products": len(products), "path": str(zpath)}, tenant_id=tid)
    return {"ok": True, "path": str(zpath), "products": len(products)}


def _count(cur, table, col, val):
    try:
        cur.execute(f"SELECT count(*) FROM {table} WHERE {col}=%s", (val,))
        return cur.fetchone()[0]
    except Exception:
        return 0


def _tenant_tables(cur):
    """Discover every current tenant-owned base table and its scope column.

    This is deliberately catalog-driven. The platform adds tenant-owned modules
    frequently; a hand-maintained erasure list silently left data behind after
    every schema expansion.
    """
    cur.execute("""SELECT table_name
                     FROM information_schema.columns
                    WHERE table_schema='public' AND column_name='tenant_id'
                      AND table_name IN (
                          SELECT table_name FROM information_schema.tables
                           WHERE table_schema='public' AND table_type='BASE TABLE')
                    ORDER BY table_name""")
    scoped = {name: "tenant_id" for (name,) in cur.fetchall()
              if name not in _RETAINED_COMPLIANCE_TABLES | {"tenants", "secrets"}}
    cur.execute("SELECT to_regclass('public.task_board') IS NOT NULL")
    if cur.fetchone()[0]:
        scoped["task_board"] = "tenant"
    return scoped


def _tenant_delete_order(cur, scoped):
    """Return child-before-parent order for non-deferrable foreign keys."""
    names = set(scoped)
    cur.execute("""SELECT conrelid::regclass::text,confrelid::regclass::text
                     FROM pg_constraint
                    WHERE contype='f' AND connamespace='public'::regnamespace
                      AND NOT condeferrable""")
    children = {name: set() for name in names}
    for child, parent in cur.fetchall():
        child, parent = child.split(".")[-1], parent.split(".")[-1]
        if child in names and parent in names and child != parent:
            children[parent].add(child)

    ordered, visiting, visited = [], set(), set()

    def visit(name):
        if name in visited:
            return
        if name in visiting:
            raise RuntimeError(f"non-deferrable tenant-table FK cycle at {name}")
        visiting.add(name)
        for child in sorted(children[name]):
            visit(child)
        visiting.remove(name)
        visited.add(name)
        ordered.append(name)

    for table in sorted(names):
        visit(table)
    return ordered


def _delete_database_rows(tid, products):
    """Atomically delete every operational tenant row under exact owner scope."""
    scopes = [p for p in products if p] + [f"tenant:{tid}"]
    with connection() as c, c.cursor() as cur:
        cur.execute("SET LOCAL lock_timeout='2s'")
        cur.execute("SET LOCAL statement_timeout='30s'")
        cur.execute("SET CONSTRAINTS ALL DEFERRED")
        scoped = _tenant_tables(cur)
        order = _tenant_delete_order(cur, scoped)
        deleted = {}

        # Some legacy product secrets predate direct tenant ownership. Delete
        # both the tenant column and every captured product namespace in the
        # same DB transaction as the rest of the account.
        cur.execute("DELETE FROM secrets WHERE tenant_id=%s OR product=ANY(%s)", (tid, scopes))
        deleted["secrets"] = cur.rowcount

        for table in order:
            cur.execute(
                sql.SQL("DELETE FROM {} WHERE {}=%s").format(
                    sql.Identifier(table), sql.Identifier(scoped[table])),
                (tid,),
            )
            deleted[table] = cur.rowcount

        cur.execute("DELETE FROM kill_switch WHERE scope=%s", (tid,))
        deleted["kill_switch"] = cur.rowcount
        cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
        deleted["tenants"] = cur.rowcount

        remaining = {}
        for table, column in scoped.items():
            cur.execute(
                sql.SQL("SELECT count(*) FROM {} WHERE {}=%s").format(
                    sql.Identifier(table), sql.Identifier(column)),
                (tid,),
            )
            count = int(cur.fetchone()[0])
            if count:
                remaining[table] = count
        cur.execute("SELECT count(*) FROM tenants WHERE tenant_id=%s", (tid,))
        if cur.fetchone()[0]:
            remaining["tenants"] = 1
        if remaining:
            raise RuntimeError(f"tenant rows remained after erasure: {remaining}")

        retained = {}
        for table in sorted(_RETAINED_COMPLIANCE_TABLES):
            cur.execute(sql.SQL("SELECT count(*) FROM {} WHERE tenant_id=%s").format(sql.Identifier(table)),
                        (tid,))
            retained[table] = int(cur.fetchone()[0])
    return deleted, retained


def _delete_files(tid, products):
    """Remove only exact, flat product roots and this tenant's export archive."""
    failures = {}
    root = factory.PRODUCTS.resolve()
    for product in products:
        try:
            path = (root / str(product)).resolve()
            if path.parent != root:
                raise ValueError("product path escaped the products root")
            if path.exists():
                shutil.rmtree(path)
        except Exception as exc:
            failures[f"product:{product}"] = str(exc)[:200]
    try:
        export_path = (factory.PRODUCTS.parent / "_exports" / f"{tid}.zip").resolve()
        expected_parent = (factory.PRODUCTS.parent / "_exports").resolve()
        if export_path.parent != expected_parent:
            raise ValueError("export path escaped the export root")
        export_path.unlink(missing_ok=True)
    except Exception as exc:
        failures["export_archive"] = str(exc)[:200]
    return failures


def delete(tid, confirm=False):
    """Right-to-erasure. Without confirm: preview counts. With confirm: delete all tenant-scoped rows.

    STRICTLY scoped by tenant_id=%s (and scope=%s for kill_switch) — never touches other tenants.
    """
    if not confirm:
        with connection() as c, c.cursor() as cur:
            scoped = _tenant_tables(cur)
            counts = {t: _count(cur, t, col, tid) for t, col in scoped.items()}
            counts["kill_switch"] = _count(cur, "kill_switch", "scope", tid)
            counts["tenants"] = _count(cur, "tenants", "tenant_id", tid)
            retained = {t: _count(cur, t, "tenant_id", tid)
                        for t in _RETAINED_COMPLIANCE_TABLES}
        return {"requires_confirm": True, "will_delete": counts,
                "retained_compliance_records": retained}

    # Capture the tenant's products FIRST, in their own (read-only) transaction, so that
    # (a) a failed read can't poison the deletion transaction, and (b) we still know which
    # products the tenant owned AFTER tenant_products is deleted — vault secrets are scoped
    # by `product`, so we need this list to purge them below.
    with tenant_connection(tid) as c, c.cursor() as cur:
        products = _products(tid, cur)

    failures = {}
    try:
        deleted, retained = _delete_database_rows(tid, products)
    except Exception as exc:
        deleted, retained = {}, {}
        failures["database"] = str(exc)[:500]
    else:
        failures.update(_delete_files(tid, products))

    ok = not failures
    result = {"ok": ok, "deleted": ok, "rows_deleted": deleted,
              "vault": f"purged {deleted.get('secrets', 0)} secret(s)",
              "retained_compliance_records": retained}
    if failures:
        result["partial"] = True
        result["failures"] = failures

    audit.append(actor=tid, action="AccountDeleted", resource=tid,
                 payload={"vault": result["vault"], "ok": ok, "failures": list(failures),
                          "retained_compliance_records": retained}, tenant_id=tid)
    return result


def _selftest():
    import billing
    import vault
    tid = billing.signup("account-selftest", "free")["tenant_id"]
    prod = tid.replace("t-", "")[:6] + "-acct"
    with connection() as c, c.cursor() as cur:
        cur.execute("INSERT INTO tenant_products (product, tenant_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                    (prod, tid))
        cur.execute("""INSERT INTO notifications (tenant_id, channel, category, level, title, body)
                       VALUES (%s,'in_app','system','info','hello','selftest row')""", (tid,))
        cur.execute("""INSERT INTO proactive_sent(tenant_id,sig,last_sent)
                       VALUES (%s,'account-erasure-selftest',now())
                       ON CONFLICT (tenant_id,sig) DO UPDATE SET last_sent=now()""", (tid,))

    product_dir = factory.PRODUCTS / prod
    product_dir.mkdir(parents=True, exist_ok=True)
    (product_dir / "tenant-owned.txt").write_text("delete me")

    # Plant the tenant's OWN credential in the vault under the synthetic product='tenant:<tid>'
    # namespace (exactly how BYO LLM keys + integration keys are stored). This guards the
    # GDPR-erasure gap: delete(confirm=True) must purge it, not just tenant_products-scoped secrets.
    vault.put_secret("byo_llm_key", f"tenant:{tid}", "prod", ["builder", "factory"], "sk-acct-selftest")
    assert vault.get_secret("byo_llm_key", f"tenant:{tid}", "prod", "builder", tenant_id=tid) == "sk-acct-selftest", \
        "BYO vault secret not readable before delete"

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

    with connection() as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM tenants WHERE tenant_id=%s", (tid,))
        n_ten = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM tenant_products WHERE tenant_id=%s", (tid,))
        n_tp = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM proactive_sent WHERE tenant_id=%s", (tid,))
        n_proactive = cur.fetchone()[0]
    assert n_ten == 0, f"tenant not deleted ({n_ten})"
    assert n_tp == 0, f"tenant_products not deleted ({n_tp})"
    assert n_proactive == 0, f"catalog-discovered tenant table survived ({n_proactive})"
    assert not product_dir.exists(), "tenant-owned product repository survived erasure"

    # The tenant's BYO credential must be GONE: erasure purges product='tenant:<tid>' secrets,
    # so a now-orphaned read must fail-closed (not found / access denied), never decrypt.
    assert done.get("vault", "").startswith("purged"), f"vault not purged: {done.get('vault')!r}"
    try:
        vault.get_secret("byo_llm_key", f"tenant:{tid}", "prod", "builder", tenant_id=tid)
        raise AssertionError("BYO vault secret SURVIVED account deletion (GDPR erasure gap)")
    except vault.AccessDenied:
        pass

    assert not zpath.exists(), "portable export archive survived erasure"

    print(f"export: {len(names)} files, manifest✅, products={ex['products']}")
    print("PASS: account export + GDPR erasure (operational tenant data/files scrubbed; "
          "BYO vault secret purged; tamper-evident compliance trail explicitly retained) ✅")
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
