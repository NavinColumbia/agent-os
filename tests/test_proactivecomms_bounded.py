import sys
import types
import uuid
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import proactivecomms as mod  # noqa: E402


def test_sweep_all_reads_global_pulse_once(monkeypatch):
    tenants = [f"t-{n}" for n in range(20)]
    monkeypatch.setattr(mod, "_priority_tenant_batch", lambda _limit, execution_scope=None: [])
    monkeypatch.setattr(mod, "_active_tenant_batch",
                        lambda _limit, execution_scope=None: tenants)
    calls = []
    fake_pulse = types.ModuleType("pulse")
    fake_pulse.live = lambda: calls.append("live") or [
        {"tenant_id": tid, "status": "active", "age_s": 9999, "work_id": tid}
        for tid in tenants
    ]
    monkeypatch.setitem(sys.modules, "pulse", fake_pulse)
    observed = []

    def fake_sweep(tid, **kwargs):
        rows = kwargs["pulse_rows"]
        observed.append((tid, len(rows)))
        return []

    monkeypatch.setattr(mod, "sweep", fake_sweep)
    result = mod.sweep_all(limit=len(tenants))
    assert calls == ["live"]
    assert result["checked"] == len(tenants)
    assert observed == [(tid, len(tenants)) for tid in tenants]


def test_scheduled_batch_excludes_inactive_and_test_tenants():
    from aoscfg import DB
    if not DB:
        pytest.skip("no database")
    import psycopg

    suffix = uuid.uuid4().hex[:10]
    prod, test, inactive = (f"pc-prod-{suffix}", f"pc-test-{suffix}", f"pc-idle-{suffix}")
    base = 980_000_000 + int(suffix[:7], 16)
    old_cursor = None
    try:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT cursor_tenant_id FROM proactive_sweep_state WHERE name='sweep-all'")
            row = cur.fetchone(); old_cursor = row[0] if row else None
            for tid in (prod, test, inactive):
                cur.execute("""INSERT INTO tenants (tenant_id,name,api_token,plan,period_start)
                               VALUES (%s,%s,%s,'free',now())""", (tid, tid, f"tok-{tid}"))
            cur.execute("""INSERT INTO controller_state
                           (thread_id,tenant_id,phase,awaiting,updated_at,execution_scope)
                           VALUES (%s,%s,'DELIVER',NULL,now(),'production'),
                                  (%s,%s,'DELIVER',NULL,now(),'test')""",
                        (base, prod, base + 1, test))
            c.commit()

        selected = mod._active_tenant_batch(limit=5000, execution_scope="production")
        assert prod in selected
        assert test not in selected
        assert inactive not in selected

        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""UPDATE controller_state
                              SET phase='OPTIONS',awaiting='user_approval',
                                  updated_at=now()-interval '3 days'
                            WHERE thread_id=%s""", (base,))
            c.commit()
        assert prod in mod._priority_tenant_batch(limit=5000)
        assert prod not in mod._priority_tenant_batch(
            limit=5000, execution_scope="production")
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM controller_state WHERE thread_id IN (%s,%s)", (base, base + 1))
            cur.execute("DELETE FROM tenants WHERE tenant_id=ANY(%s)", ([prod, test, inactive],))
            if old_cursor is None:
                cur.execute("DELETE FROM proactive_sweep_state WHERE name='sweep-all'")
            else:
                cur.execute("""INSERT INTO proactive_sweep_state(name,cursor_tenant_id)
                               VALUES ('sweep-all',%s)
                               ON CONFLICT(name) DO UPDATE SET cursor_tenant_id=EXCLUDED.cursor_tenant_id""",
                            (old_cursor,))
            c.commit()


def test_test_default_cannot_publish_to_operator_topic(monkeypatch):
    import notify
    monkeypatch.setenv("AOS_DISABLE_EXTERNAL_NOTIFICATIONS", "1")
    monkeypatch.setattr(notify.requests, "post", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("external transport must not be called")))
    assert notify.send("synthetic test page", topic="would-be-live") is False


def test_scheduled_priority_and_inbox_exclude_legacy_questions():
    """Unscoped historical asks stay manually inspectable but can never enter production reminders."""
    from aoscfg import DB
    if not DB:
        pytest.skip("no database")
    import psycopg
    import agent_request
    import approvals

    suffix = uuid.uuid4().hex[:10]
    tid = f"pc-request-scope-{suffix}"
    thread_id = 1_040_000_000 + int(suffix[:7], 16)
    legacy_id = production_id = None
    try:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""INSERT INTO tenants (tenant_id,name,api_token,plan,period_start)
                           VALUES (%s,%s,%s,'free',now())""", (tid, tid, f"tok-{tid}"))
            cur.execute("""INSERT INTO controller_state
                           (thread_id,tenant_id,phase,awaiting,updated_at,execution_scope)
                           VALUES (%s,%s,'TESTQA','user_feedback',now(),'production')""",
                        (thread_id, tid))
            cur.execute("""INSERT INTO agent_requests
                           (tenant_id,thread_id,kind,question,status,execution_scope)
                           VALUES (%s,NULL,'decision','old synthetic question','open','legacy'),
                                  (%s,%s,'decision','current production question','open','production')
                           RETURNING id""", (tid, tid, thread_id))
            legacy_id, production_id = [row[0] for row in cur.fetchall()]

        assert tid in mod._priority_tenant_batch(limit=5000, execution_scope="production")
        scheduled = approvals.inbox(tid, execution_scope="production")["items"]
        scheduled_question_ids = {item["id"] for item in scheduled if item["kind"] == "question"}
        assert scheduled_question_ids == {f"question:{production_id}"}
        manual = agent_request.open_requests(tid)
        assert {row["id"] for row in manual} == {legacy_id, production_id}
        assert {row["id"] for row in agent_request.open_requests(
            tid, execution_scope="production")} == {production_id}
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM agent_requests WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM controller_state WHERE thread_id=%s", (thread_id,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))


def test_progress_update_uses_supplied_snapshot_without_import(monkeypatch):
    tid = "t-a"
    rows = [{"tenant_id": tid, "status": "active", "age_s": mod.PROGRESS_MIN_AGE_S + 1,
             "work_id": "qa:1", "label": "QA"}]
    poison = types.ModuleType("pulse")
    poison.live = lambda: (_ for _ in ()).throw(AssertionError("must not query pulse"))
    monkeypatch.setitem(sys.modules, "pulse", poison)
    result = mod._progress_update(tid, pulse_rows=rows)
    assert len(result) == 1
    assert result[0]["id"] == "progress:active-work"
