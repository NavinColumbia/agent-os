import sys
import threading
import types
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import chiefofstaff
import notifications
import scheduler
from dbpool import tenant_connection


def test_scheduled_daily_brief_is_activity_scoped_paged_and_model_free(monkeypatch):
    monkeypatch.setattr(chiefofstaff, "_daily_tenants", lambda _key, _limit: ["active-a", "active-b"])
    monkeypatch.setattr(chiefofstaff, "_facts", lambda tid, _org: {
        "awaiting": [{"title": f"Decision for {tid}"}], "health": {"ok": True},
        "verdict": {"verdict": "healthy"}, "portfolio": {"products_touched_30d": 1},
        "spend": {"cost_usd_30d": 0}, "workstreams": []})
    monkeypatch.setattr(chiefofstaff, "brief",
                        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("batch must not call model brief")))
    sent = []
    monkeypatch.setitem(sys.modules, "notifications", types.SimpleNamespace(
        send=lambda *a, **k: sent.append((a, k)) or {"id": len(sent)}))

    result = chiefofstaff.push_daily(limit=25)

    assert result["briefs_sent"] == 2 and len(sent) == 2
    assert all(call[1]["context_key"].startswith("chief-of-staff-daily:") for call in sent)
    assert next(interval for name, _command, interval in scheduler.DEFAULT_SCHEDULES
                if name == "chiefofstaff-daily") == 300


def test_context_key_makes_notification_retry_idempotent():
    tid = f"brief-idempotence-{uuid.uuid4().hex[:10]}"
    key = f"chief-of-staff-daily:{uuid.uuid4().hex}"
    try:
        first = notifications.send(tid, "digest", "Daily", "one", level="silent", context_key=key)
        second = notifications.send(tid, "digest", "Daily", "two", level="silent", context_key=key)
        assert first["id"] == second["id"] and first["duplicate"] is False
        assert second["duplicate"] is True and second["channels"] == []
        with tenant_connection(tid) as c, c.cursor() as cur:
            cur.execute("SELECT count(*) FROM notifications WHERE tenant_id=%s AND context_key=%s", (tid, key))
            assert cur.fetchone()[0] == 1
    finally:
        with tenant_connection(tid) as c, c.cursor() as cur:
            cur.execute("DELETE FROM notification_deliveries WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM notifications WHERE tenant_id=%s", (tid,))


def test_active_tenant_query_has_a_durable_daily_tail_cursor():
    source = (ROOT / "scripts" / "chiefofstaff.py").read_text()
    assert "NOT EXISTS (" in source and "n.context_key=%s" in source
    assert "controller_state" in source and "agent_requests" in source and "tenant_products" in source


def test_daily_discovery_failure_is_degraded_not_false_green(monkeypatch):
    class BrokenConnection:
        def __enter__(self):
            raise RuntimeError("database offline")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(chiefofstaff, "connection", lambda: BrokenConnection())
    try:
        chiefofstaff._daily_tenants("daily:test", 25)
    except RuntimeError as exc:
        assert "discovery unavailable" in str(exc)
    else:
        raise AssertionError("database blindness must fail the schedule")


def test_concurrent_daily_push_counts_one_semantic_delivery(monkeypatch):
    tid = f"brief-concurrent-{uuid.uuid4().hex[:10]}"
    monkeypatch.setattr(chiefofstaff, "_daily_tenants", lambda _key, _limit: [tid])
    monkeypatch.setattr(chiefofstaff, "_facts", lambda _tid, _org: {
        "awaiting": [{"title": "Approve"}], "health": None, "verdict": {"verdict": "healthy"},
        "portfolio": None, "spend": None, "workstreams": []})
    barrier = threading.Barrier(2)
    real_send = notifications.send

    def racing_send(*args, **kwargs):
        barrier.wait(timeout=5)
        return real_send(*args, **kwargs)

    monkeypatch.setitem(sys.modules, "notifications", types.SimpleNamespace(send=racing_send))
    results = []
    threads = [threading.Thread(target=lambda: results.append(chiefofstaff.push_daily(limit=1))) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert all(not thread.is_alive() for thread in threads)
        assert sum(r["briefs_sent"] for r in results) == 1
        assert sum(r["duplicates"] for r in results) == 1
    finally:
        with tenant_connection(tid) as c, c.cursor() as cur:
            cur.execute("DELETE FROM notification_deliveries WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM notifications WHERE tenant_id=%s", (tid,))


def test_notification_dedupe_lock_is_bounded():
    source = (ROOT / "scripts" / "notifications.py").read_text()
    timeout = source.index("SET LOCAL lock_timeout='500ms'")
    lock = source.index("SELECT pg_advisory_xact_lock", timeout)
    assert timeout < lock
