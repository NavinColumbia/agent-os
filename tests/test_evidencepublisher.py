import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "qa"))

import evidencepublisher as publisher


def test_readiness_heartbeat_remains_fresh_during_long_encode(monkeypatch):
    calls = []
    stopped = threading.Event()
    monkeypatch.setattr(publisher, "READY_HEARTBEAT_S", 0.01)
    import singleton_exec
    monkeypatch.setattr(singleton_exec, "mark_ready",
                        lambda name, detail: calls.append((name, detail)) or True)

    thread = publisher._start_readiness_heartbeat(
        stopped, lambda: {"state": "working", "job_id": 7})
    deadline = time.monotonic() + 1
    while len(calls) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    stopped.set()
    thread.join(1)

    assert not thread.is_alive()
    assert len(calls) >= 3
    assert all(name == "evidence-publisher" for name, _detail in calls)
    assert calls[-1][1] == {"state": "working", "job_id": 7}


def test_defer_is_filesystem_only_and_spool_is_discoverable(monkeypatch, tmp_path):
    source = tmp_path / "run" / "videos" / "story.webm"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"raw")
    receipt = source.parent.parent / "encoding-status.json"
    monkeypatch.setattr(publisher, "_roots", lambda: [tmp_path.resolve()])
    monkeypatch.setattr(publisher, "_ensure",
                        lambda: (_ for _ in ()).throw(AssertionError("defer must not touch PostgreSQL")))

    result = publisher.defer(source, receipt=receipt)

    assert result["deferred"] is True
    queued = json.loads(Path(result["spool"]).read_text())
    assert queued["source"] == str(source.resolve())
    assert json.loads(receipt.read_text())["status"] == "deferred"


def test_defer_rejects_paths_outside_evidence_root(monkeypatch, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    source = tmp_path / "outside.webm"
    source.write_bytes(b"raw")
    monkeypatch.setattr(publisher, "_roots", lambda: [allowed.resolve()])

    result = publisher.defer(source)

    assert result["deferred"] is False
    assert not (allowed / publisher.SPOOL_DIRNAME).exists()


def test_spool_drain_is_idempotent_and_removes_only_accepted_records(monkeypatch, tmp_path):
    source = tmp_path / "run" / "videos" / "story.webm"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"raw")
    monkeypatch.setattr(publisher, "_roots", lambda: [tmp_path.resolve()])
    monkeypatch.setattr(publisher, "_scan_roots", lambda: [tmp_path.resolve()])
    deferred = publisher.defer(source)
    calls = []
    monkeypatch.setattr(publisher, "enqueue",
                        lambda *args: calls.append(args) or {"queued": True, "status": "queued"})

    first = publisher.drain_spool()
    second = publisher.drain_spool()

    assert first["queued"] == 1 and second["queued"] == 0
    assert len(calls) == 1 and not Path(deferred["spool"]).exists()


def test_receipt_backfill_limit_counts_candidates_not_completed_receipts(monkeypatch, tmp_path):
    monkeypatch.setattr(publisher, "_roots", lambda: [tmp_path.resolve()])
    monkeypatch.setattr(publisher, "_scan_roots", lambda: [tmp_path.resolve()])
    for index in range(3):
        run = tmp_path / f"done-{index}"
        run.mkdir()
        (run / "encoding-status.json").write_text(json.dumps({"status": "done"}))
    pending = tmp_path / "pending"
    pending.mkdir()
    source = pending / "story.webm"
    source.write_bytes(b"raw")
    (pending / "encoding-status.json").write_text(json.dumps({
        "status": "deferred", "source": str(source)}))
    calls = []
    monkeypatch.setattr(publisher, "enqueue",
                        lambda *args: calls.append(args) or {"queued": True})

    result = publisher.backfill_receipts(limit=1)

    assert result["found"] == 1 and result["queued"] == 1
    assert len(calls) == 1


def test_receipt_backfill_is_rate_limited_between_idle_ticks(monkeypatch):
    calls = []
    monkeypatch.setattr(publisher, "_last_receipt_backfill_at", 0.0)
    monkeypatch.setattr(publisher, "RECEIPT_BACKFILL_INTERVAL_S", 300.0)
    monkeypatch.setattr(publisher, "backfill_receipts",
                        lambda limit: calls.append(limit) or {"queued": 0})

    assert publisher.backfill_receipts_if_due(now=1000.0) == {"queued": 0}
    skipped = publisher.backfill_receipts_if_due(now=1001.0)
    assert skipped["skipped"] is True
    assert publisher.backfill_receipts_if_due(now=1300.0) == {"queued": 0}
    assert calls == [publisher.RECEIPT_SCAN_LIMIT, publisher.RECEIPT_SCAN_LIMIT]


def test_exhausted_retries_become_failed_and_publish_terminal_receipts(monkeypatch, tmp_path):
    receipt = tmp_path / "encoding-status.json"
    source = tmp_path / "story.webm"
    output = tmp_path / "story.mp4"
    source.write_bytes(b"raw")
    executed = []

    class Cursor:
        def execute(self, sql, params=None):
            executed.append((sql, params))

        def fetchall(self):
            return [(19, str(receipt), str(source), str(output), 3, "ffmpeg failed")]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Connection:
        def cursor(self):
            return Cursor()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(publisher, "_ensure", lambda: None)
    monkeypatch.setattr(publisher, "_roots", lambda: [tmp_path.resolve()])
    monkeypatch.setattr(publisher, "connection", lambda: Connection())

    assert publisher.terminalize_exhausted() == 1
    sql, params = executed[0]
    assert "status='failed'" in sql and "attempts >= %s" in sql
    assert params == (publisher.MAX_ATTEMPTS,)
    persisted = json.loads(receipt.read_text())
    assert persisted["status"] == "failed" and persisted["attempts"] == 3
    assert persisted["source"] == str(source)


def test_legacy_windows_root_is_not_scanned_without_explicit_opt_in(monkeypatch, tmp_path):
    native = tmp_path / "native"
    legacy = tmp_path / "legacy"
    native.mkdir()
    legacy.mkdir()
    monkeypatch.setattr(publisher.artifacts, "root", lambda: native)
    monkeypatch.setattr(publisher, "_roots", lambda: [native.resolve(), legacy.resolve()])
    monkeypatch.delenv("AOS_EVIDENCE_SCAN_LEGACY", raising=False)

    assert publisher._scan_roots() == [native.resolve()]

    monkeypatch.setenv("AOS_EVIDENCE_SCAN_LEGACY", "1")
    assert publisher._scan_roots() == [native.resolve(), legacy.resolve()]


def test_duplicate_enqueue_sql_does_not_clear_an_active_claim(monkeypatch, tmp_path):
    source = tmp_path / "story.webm"
    source.write_bytes(b"raw")
    monkeypatch.setattr(publisher, "_roots", lambda: [tmp_path.resolve()])
    seen = {}

    class Cursor:
        def execute(self, sql, params=None):
            if "INSERT INTO qa_evidence_encoding_jobs" in sql:
                seen["sql"] = sql

        def fetchone(self):
            return 7, "running"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Connection:
        def cursor(self):
            return Cursor()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(publisher, "_ensure", lambda: None)
    monkeypatch.setattr(publisher, "connection", lambda: Connection())

    result = publisher.enqueue(source)

    assert result["status"] == "running"
    assert "lease_until=NULL" not in seen["sql"]
    assert "claim_token=NULL" not in seen["sql"]


def test_capacity_deferral_releases_claim_without_consuming_attempt(monkeypatch, tmp_path):
    source = tmp_path / "story.webm"
    source.write_bytes(b"raw")
    output = tmp_path / "story.mp4"
    receipt = tmp_path / "encoding-status.json"
    monkeypatch.setattr(publisher, "_roots", lambda: [tmp_path.resolve()])
    monkeypatch.setattr(publisher, "drain_spool", lambda: {})
    monkeypatch.setattr(publisher, "reclaim_abandoned_claims", lambda: 0)
    monkeypatch.setattr(publisher, "_active_qa_explorers", lambda: 0)
    monkeypatch.setattr(publisher, "_claim",
                        lambda: ("lease", (9, str(source), str(output), str(receipt), 2)))
    monkeypatch.setattr(publisher.artifacts, "webm_to_mp4_result",
                        lambda *_args: {"status": "deferred", "reason": "media capacity unavailable"})
    seen = {}

    def defer(job_id, token, **kwargs):
        seen.update({"job_id": job_id, "token": token, **kwargs})
        return {"status": "deferred", "capacity_wait": True}

    monkeypatch.setattr(publisher, "_defer_capacity", defer)

    result = publisher.run_once()

    assert result["capacity_wait"] is True
    assert seen["attempts"] == 2 and seen["receipt"] == str(receipt)


def test_publisher_prioritizes_live_qa_without_claiming_or_encoding(monkeypatch):
    monkeypatch.setattr(publisher, "PUBLISH_DURING_ACTIVE_QA", False)
    monkeypatch.setattr(publisher, "drain_spool", lambda: {"queued": 2})
    monkeypatch.setattr(publisher, "reclaim_abandoned_claims", lambda: 0)
    monkeypatch.setattr(publisher, "terminalize_exhausted", lambda: 0)
    monkeypatch.setattr(publisher, "_active_qa_explorers", lambda: 2)
    monkeypatch.setattr(publisher, "_claim",
                        lambda: (_ for _ in ()).throw(AssertionError("must not claim during live QA")))

    result = publisher.run_once()

    assert result == {"state": "deferred-active-qa", "active_qa_explorers": 2,
                      "spool": {"queued": 2}, "terminalized": 0}


def test_unknown_live_qa_state_defers_review_convenience_work(monkeypatch):
    monkeypatch.setattr(publisher, "PUBLISH_DURING_ACTIVE_QA", False)
    monkeypatch.setattr(publisher, "drain_spool", lambda: {})
    monkeypatch.setattr(publisher, "reclaim_abandoned_claims", lambda: 0)
    monkeypatch.setattr(publisher, "terminalize_exhausted", lambda: 0)
    monkeypatch.setattr(publisher, "_active_qa_explorers", lambda: None)
    monkeypatch.setattr(publisher, "_claim",
                        lambda: (_ for _ in ()).throw(AssertionError("unknown state fails closed")))

    assert publisher.run_once()["state"] == "deferred-active-qa"


def test_dead_publisher_claims_are_released_before_capacity_deferral(monkeypatch):
    updates = []

    class Cursor:
        rowcount = 0

        def execute(self, sql, params=None):
            if sql.lstrip().startswith("SELECT"):
                self.rows = [
                    (51, "publisher:111:123", False),
                    (52, "publisher:222:456", False),
                    (53, "legacy-token", True),
                ]
                self.rowcount = 0
            else:
                updates.append(params)
                self.rowcount = 1

        def fetchall(self):
            return self.rows

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Connection:
        def cursor(self):
            return Cursor()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(publisher, "_ensure", lambda: None)
    monkeypatch.setattr(publisher, "connection", lambda: Connection())
    monkeypatch.setattr(publisher.process_assurance, "read_snapshot",
                        lambda pid: object() if pid == 222 else None)

    assert publisher.reclaim_abandoned_claims() == 2
    assert [(params[1], params[2]) for params in updates] == [
        (51, "publisher:111:123"), (53, "legacy-token")]


def test_testqa_campaign_closes_encoder_claim_gap_between_explorer_leases(monkeypatch):
    class Cursor:
        def execute(self, sql, _params=None):
            assert "controller_state" in sql and "orchestra_tool_leases" in sql

        def fetchone(self):
            return 0, 1, 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Connection:
        def cursor(self):
            return Cursor()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(publisher, "connection", lambda: Connection())

    assert publisher._active_qa_explorers() == 1


def test_direct_checkpoint_campaign_closes_encoder_claim_gap_without_controller_state(monkeypatch):
    seen = {}

    class Cursor:
        def execute(self, sql, _params=None):
            seen["sql"] = sql

        def fetchone(self):
            return 0, 0, 1

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Connection:
        def cursor(self):
            return Cursor()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(publisher, "connection", lambda: Connection())

    assert publisher._active_qa_explorers() == 1
    assert "qa-coordinator" in seen["sql"]
    assert "last_active > now()-interval '2 minutes'" in seen["sql"]
