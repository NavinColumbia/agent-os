import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import account  # noqa: E402
import billing  # noqa: E402
import vault  # noqa: E402
from dbpool import connection  # noqa: E402


def _signup(label):
    return billing.signup(f"{label}-{uuid.uuid4().hex[:10]}", "free")["tenant_id"]


def test_erasure_inventory_is_catalog_driven_and_covers_new_modules():
    with connection() as conn, conn.cursor() as cur:
        scoped = account._tenant_tables(cur)
    assert {"agent_requests", "management_cases", "orchestra_runs", "proactive_sent",
            "resource_commitments", "task_board"}.issubset(scoped)
    assert "audit_log" not in scoped


def test_account_erasure_removes_dynamic_rows_and_files_without_cross_tenant_loss():
    tenant_a, tenant_b = _signup("erase-a"), _signup("erase-b")
    product = f"erase-{uuid.uuid4().hex[:12]}"
    product_dir = account.factory.PRODUCTS / product
    try:
        with connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO tenant_products(product,tenant_id) VALUES (%s,%s)",
                        (product, tenant_a))
            for tenant in (tenant_a, tenant_b):
                cur.execute("""INSERT INTO notifications
                                 (tenant_id,channel,category,level,title,body)
                               VALUES (%s,'in_app','system','info','erasure fixture','body')""",
                            (tenant,))
                cur.execute("""INSERT INTO proactive_sent(tenant_id,sig,last_sent)
                               VALUES (%s,'erasure-fixture',now())""", (tenant,))
                cur.execute("""INSERT INTO kill_switch(scope,reason,set_by)
                               VALUES (%s,'erasure fixture','pytest')
                               ON CONFLICT(scope) DO UPDATE SET reason=EXCLUDED.reason,set_by=EXCLUDED.set_by""",
                            (tenant,))
        product_dir.mkdir(parents=True)
        (product_dir / "owned.txt").write_text("tenant-owned artifact")
        vault.put_secret("fixture", f"tenant:{tenant_a}", "prod", ["builder"], "secret-a")
        vault.put_secret("fixture", f"tenant:{tenant_b}", "prod", ["builder"], "secret-b")

        result = account.delete(tenant_a, confirm=True)

        assert result["ok"] is True
        assert not product_dir.exists()
        with connection() as conn, conn.cursor() as cur:
            for table in ("tenants", "tenant_products", "notifications", "proactive_sent"):
                cur.execute(f"SELECT count(*) FROM {table} WHERE tenant_id=%s", (tenant_a,))
                assert cur.fetchone()[0] == 0, table
            cur.execute("SELECT count(*) FROM kill_switch WHERE scope=%s", (tenant_a,))
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT count(*) FROM tenants WHERE tenant_id=%s", (tenant_b,))
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT count(*) FROM notifications WHERE tenant_id=%s", (tenant_b,))
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT count(*) FROM proactive_sent WHERE tenant_id=%s", (tenant_b,))
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT count(*) FROM kill_switch WHERE scope=%s", (tenant_b,))
            assert cur.fetchone()[0] == 1
        try:
            vault.get_secret("fixture", f"tenant:{tenant_a}", "prod", "builder", tenant_id=tenant_a)
            raise AssertionError("erased tenant secret remained readable")
        except vault.AccessDenied:
            pass
        assert vault.get_secret("fixture", f"tenant:{tenant_b}", "prod", "builder",
                                tenant_id=tenant_b) == "secret-b"
    finally:
        account.delete(tenant_a, confirm=True)
        account.delete(tenant_b, confirm=True)


def test_database_erasure_failure_rolls_back_before_files_are_removed(monkeypatch):
    tenant = _signup("erase-rollback")
    product = f"erase-rollback-{uuid.uuid4().hex[:8]}"
    product_dir = account.factory.PRODUCTS / product
    with connection() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO tenant_products(product,tenant_id) VALUES (%s,%s)", (product, tenant))
    product_dir.mkdir(parents=True)
    (product_dir / "must-survive.txt").write_text("rollback proof")
    real_order = account._tenant_delete_order
    monkeypatch.setattr(account, "_tenant_delete_order",
                        lambda cur, scoped: real_order(cur, scoped) + ["not_a_real_table"])
    try:
        result = account.delete(tenant, confirm=True)
        assert result["ok"] is False and "database" in result["failures"]
        assert product_dir.exists()
        with connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM tenants WHERE tenant_id=%s", (tenant,))
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT count(*) FROM tenant_products WHERE tenant_id=%s", (tenant,))
            assert cur.fetchone()[0] == 1
    finally:
        monkeypatch.setattr(account, "_tenant_delete_order", real_order)
        account.delete(tenant, confirm=True)
