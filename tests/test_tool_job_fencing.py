import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ORCHESTRA = ROOT / "scripts" / "orchestra"
SCRIPTS = ROOT / "scripts"
for path in (str(ORCHESTRA), str(SCRIPTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

import jobrunner
import store


def test_durable_tool_lease_fences_expired_owner_across_connections():
    """A new DB connection can renew; takeover advances the token and permanently fences the old owner."""
    key = f"test-tool-fence:{uuid.uuid4().hex}"
    tenant = f"test-fence-{uuid.uuid4().hex}"
    try:
        first = store.claim_tool_job_lease(key, 1, tenant, 11, "qa_explore", "owner-a", lease_s=60)
        assert first and first["fence_token"] == 1
        assert store.claim_tool_job_lease(key, 1, tenant, 11, "qa_explore", "owner-b", lease_s=60) is None
        assert store.renew_tool_job_lease(key, "owner-a", 1, tenant, lease_s=60)["valid"]

        # Simulate a holder that missed its heartbeat/session and exceeded its durable lease.
        with store._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute("UPDATE orchestra_tool_leases SET lease_until=now()-interval '1 second' WHERE lease_key=%s",
                        (key,))
            conn.commit()
        second = store.claim_tool_job_lease(key, 1, tenant, 11, "qa_explore", "owner-b", lease_s=60)
        assert second and second["fence_token"] == 2
        assert not store.tool_job_lease_valid(key, "owner-a", 1, tenant)
        assert not store.renew_tool_job_lease(key, "owner-a", 1, tenant, lease_s=60)["valid"]
        assert not store.release_tool_job_lease(key, "owner-a", 1, tenant)
        assert store.tool_job_lease_valid(key, "owner-b", 2, tenant)
    finally:
        with store._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM orchestra_tool_leases WHERE lease_key=%s", (key,))
            conn.commit()


def test_jobrunner_passes_fencing_aware_cancel_surface_to_tool():
    seen = {}

    class FakeStore:
        @contextmanager
        def tool_job_lock(self, *args, **kwargs):
            yield {"lease_key": "k", "owner_id": "o", "fence_token": 7, "lease_s": 90}

        def renew_tool_job_lease(self, *args):
            return {"valid": True}

        def tool_job_lease_valid(self, *args):
            return True

        def emit_once(self, *args):
            return {"id": 1}

    def tool(_name, args):
        seen["cancel"] = args["_cancel_event"]
        assert not args["_cancel_event"].is_set()
        return {"status": "done", "findings": [], "result": {"ok": True}}

    job = {"run_id": 900001, "tenant": "t", "actor_id": 2, "tool": "research",
           "args": {}, "attempt": 0}
    jid = jobrunner.dispatch(job, FakeStore(), run_tool=tool, sync=True)
    assert isinstance(seen["cancel"], jobrunner._FencedCancel)
    assert jobrunner._JOBS[jid]["state"] == "done"
    with jobrunner._LOCK:
        jobrunner._JOBS.pop(jid, None)


def test_fencing_cancel_fails_closed_when_generation_is_lost():
    class LostStore:
        def tool_job_lease_valid(self, *args):
            return False

    guard = jobrunner._FencedCancel(
        jobrunner.threading.Event(), LostStore(),
        {"lease_key": "k", "owner_id": "old", "fence_token": 3}, "t")
    assert guard.is_set()
    assert guard.lost.is_set()


def test_heartbeat_reconnects_after_one_broken_db_session():
    class ReconnectingStore:
        def __init__(self):
            self.renews = 0

        def renew_tool_job_lease(self, *args):
            self.renews += 1
            if self.renews == 1:
                raise ConnectionError("simulated dead pooled session")
            return {"valid": True}

        def tool_job_lease_valid(self, *args):
            return True

    fake = ReconnectingStore()
    base = jobrunner.threading.Event()
    lease = {"lease_key": "reconnect", "owner_id": "owner", "fence_token": 9, "lease_s": 3}
    guard = jobrunner._FencedCancel(base, fake, lease, "t")
    with jobrunner._LeaseHeartbeat(fake, lease, "t", guard):
        deadline = time.time() + 2.8
        while fake.renews < 2 and time.time() < deadline:
            time.sleep(0.05)
    assert fake.renews >= 2
    assert not guard.lost.is_set()
