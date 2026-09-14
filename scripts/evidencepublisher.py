#!/usr/bin/env python3
"""Durable, capacity-governed publisher for deferred QA video evidence.

Explorers close Chromium, persist the raw Playwright WebM, enqueue one row, and return. This daemon converts
that evidence to review-friendly MP4 later under the shared media/resource gate. A restart reclaims expired
claims; filesystem receipts backfill jobs if PostgreSQL was briefly unavailable when the explorer closed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import sys
import threading
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
QA = SCRIPTS / "qa"
for path in (SCRIPTS, QA):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import artifacts  # noqa: E402
import process_assurance  # noqa: E402
from dbpool import connection  # noqa: E402

CLAIM_LEASE_S = max(60, int(os.environ.get("AOS_EVIDENCE_PUBLISH_LEASE_S", "600")))
MAX_ATTEMPTS = max(1, int(os.environ.get("AOS_EVIDENCE_PUBLISH_ATTEMPTS", "3")))
RECEIPT_SCAN_LIMIT = max(1, min(1000, int(os.environ.get("AOS_EVIDENCE_RECEIPT_SCAN", "200"))))
RECEIPT_BACKFILL_INTERVAL_S = max(30.0, min(3600.0, float(os.environ.get(
    "AOS_EVIDENCE_RECEIPT_BACKFILL_INTERVAL_S", "300"))))
SPOOL_DIRNAME = ".encoding-queue"
PUBLISH_DURING_ACTIVE_QA = os.environ.get(
    "AOS_EVIDENCE_PUBLISH_DURING_QA", "0").strip().lower() in {"1", "true", "yes", "on"}
READY_HEARTBEAT_S = max(1.0, min(30.0, float(os.environ.get(
    "AOS_EVIDENCE_READY_HEARTBEAT_S", "15"))))
_last_receipt_backfill_at = 0.0


def _ensure():
    with connection() as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS qa_evidence_encoding_jobs (
            id BIGSERIAL PRIMARY KEY,
            source_path TEXT NOT NULL UNIQUE,
            output_path TEXT NOT NULL,
            receipt_path TEXT,
            status TEXT NOT NULL DEFAULT 'queued'
              CHECK (status IN ('queued','running','retry','done','failed')),
            attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            claim_token TEXT,
            claimed_at TIMESTAMPTZ,
            lease_until TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at TIMESTAMPTZ,
            error TEXT,
            result JSONB NOT NULL DEFAULT '{}'::jsonb
        )""")
        cur.execute("""CREATE INDEX IF NOT EXISTS qa_evidence_encoding_jobs_claim_idx
                         ON qa_evidence_encoding_jobs(status, lease_until, id)""")


def _roots():
    return [Path(root).resolve() for root in artifacts.search_roots() if Path(root).exists()]


def _scan_roots():
    """Roots eligible for automatic discovery.

    The Windows-visible legacy root is intentionally excluded by default: a recursive walk across WSL's
    Plan9 mount can block a daemon in uninterruptible I/O for minutes. New explorers use a native root-local
    spool. Operators may explicitly opt into one-time legacy recovery when no live QA is latency-sensitive.
    """
    native = Path(artifacts.root()).resolve()
    roots = [native] if native.exists() else []
    if os.environ.get("AOS_EVIDENCE_SCAN_LEGACY", "").strip().lower() in {"1", "true", "yes", "on"}:
        roots.extend(root for root in _roots() if root not in roots)
    return roots


def _inside_allowed_root(path):
    try:
        resolved = Path(path).resolve(strict=False)
    except OSError:
        return False
    return any(resolved == root or resolved.is_relative_to(root) for root in _roots())


def _safe_paths(source, output):
    source, output = Path(source).resolve(strict=False), Path(output).resolve(strict=False)
    if not (_inside_allowed_root(source) and _inside_allowed_root(output)):
        return None
    if source.suffix.lower() != ".webm" or output.suffix.lower() != ".mp4":
        return None
    if source.parent != output.parent or source.stem != output.stem:
        return None
    return source, output


def _write_receipt(path, payload):
    if not path:
        return
    target = Path(path)
    if not _inside_allowed_root(target):
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
        tmp.replace(target)
    except OSError:
        pass


def _spool_path(source):
    """Return a stable, root-local queue record without touching PostgreSQL.

    Explorer teardown calls :func:`defer` after Chromium has closed.  Keeping this path filesystem-only means
    a saturated or restarting database can never hold the scarce explorer slot while still leaving a durable,
    O(1)-discoverable handoff for the publisher.
    """
    source = Path(source).resolve(strict=False)
    for root in _roots():
        if source == root or source.is_relative_to(root):
            digest = hashlib.sha256(str(source).encode()).hexdigest()
            return root / SPOOL_DIRNAME / f"{digest}.json"
    return None


def defer(source, output=None, receipt=None):
    """Durably hand off an encode request using only bounded local filesystem operations."""
    source = Path(source)
    output = Path(output) if output else source.with_suffix(".mp4")
    safe = _safe_paths(source, output)
    if safe is None:
        return {"deferred": False, "reason": "evidence paths are outside the bounded QA root"}
    source, output = safe
    default_dir = source.parent.parent if source.parent.name == "videos" else source.parent
    receipt = Path(receipt).resolve(strict=False) if receipt else default_dir / "encoding-status.json"
    if not _inside_allowed_root(receipt):
        return {"deferred": False, "reason": "receipt path is outside the bounded QA root"}
    spool = _spool_path(source)
    if spool is None:
        return {"deferred": False, "reason": "no bounded queue root"}
    payload = {"status": "deferred", "source": str(source), "output": str(output),
               "receipt": str(receipt), "recorded_at": time.time()}
    try:
        spool.parent.mkdir(parents=True, exist_ok=True)
        tmp = spool.with_name(f".{spool.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        tmp.replace(spool)
    except OSError as exc:
        return {"deferred": False, "reason": str(exc)[:300]}
    _write_receipt(receipt, {**payload, "spool": str(spool),
                             "reason": "explorer released before review-convenience encoding"})
    return {"deferred": True, "source": str(source), "output": str(output),
            "receipt": str(receipt), "spool": str(spool)}


def enqueue(source, output=None, receipt=None):
    source = Path(source)
    output = Path(output) if output else source.with_suffix(".mp4")
    safe = _safe_paths(source, output)
    if safe is None:
        return {"queued": False, "reason": "evidence paths are outside the bounded QA root"}
    source, output = safe
    default_dir = source.parent.parent if source.parent.name == "videos" else source.parent
    receipt = Path(receipt).resolve(strict=False) if receipt else default_dir / "encoding-status.json"
    if not _inside_allowed_root(receipt):
        return {"queued": False, "reason": "receipt path is outside the bounded QA root"}
    _ensure()
    with connection() as c, c.cursor() as cur:
        cur.execute("""INSERT INTO qa_evidence_encoding_jobs(source_path,output_path,receipt_path)
                       VALUES (%s,%s,%s)
                       ON CONFLICT (source_path) DO UPDATE
                         SET output_path=EXCLUDED.output_path,receipt_path=EXCLUDED.receipt_path,
                             updated_at=now()
                       RETURNING id,status""", (str(source), str(output), str(receipt)))
        job_id, status = cur.fetchone()
    _write_receipt(receipt, {"status": "encoded" if status == "done" else status,
                             "job_id": job_id, "source": str(source), "output": str(output),
                             "queued_at": time.time()})
    return {"queued": True, "job_id": job_id, "status": status,
            "source": str(source), "output": str(output)}


def drain_spool(limit=RECEIPT_SCAN_LIMIT):
    """Move root-local handoffs into the transactional queue; duplicates are idempotent."""
    found = queued = invalid = 0
    for root in _scan_roots():
        spool_dir = root / SPOOL_DIRNAME
        try:
            records = sorted(spool_dir.glob("*.json"), key=lambda p: (p.stat().st_mtime_ns, p.name))
        except OSError:
            continue
        for record in records:
            if found >= max(1, int(limit)):
                return {"found": found, "queued": queued, "invalid": invalid, "bounded": True}
            found += 1
            try:
                data = json.loads(record.read_text())
                result = enqueue(data["source"], data.get("output"), data.get("receipt"))
                accepted = bool(result.get("queued"))
            except (KeyError, OSError, TypeError, ValueError):
                accepted = False
                result = {"reason": "invalid queue record"}
            if accepted:
                queued += 1
                try:
                    record.unlink()
                except OSError:
                    pass
            else:
                invalid += 1
                # Unsafe/corrupt records cannot become valid through retries. Quarantine them so one poison
                # item cannot monopolize the bounded scan, but retain the bytes for operator inspection.
                if "outside the bounded" in str(result.get("reason")) or result.get("reason") == "invalid queue record":
                    try:
                        record.replace(record.with_suffix(".invalid"))
                    except OSError:
                        pass
    return {"found": found, "queued": queued, "invalid": invalid, "bounded": False}


def _claim():
    token = f"publisher:{os.getpid()}:{time.time_ns()}"
    _ensure()
    with connection() as c, c.cursor() as cur:
        cur.execute("""UPDATE qa_evidence_encoding_jobs
                          SET status='retry',claim_token=NULL,lease_until=NULL,updated_at=now(),
                              error=COALESCE(error,'publisher lease expired')
                        WHERE status='running' AND lease_until<=now()""")
        # A lease may expire on the final allowed attempt between the explicit reclaim pass and this claim.
        # Close that state here as well so it cannot become an unclaimable permanent `retry` row.
        cur.execute("""UPDATE qa_evidence_encoding_jobs
                          SET status='failed',claim_token=NULL,lease_until=NULL,updated_at=now(),
                              finished_at=now(),
                              error=COALESCE(error,'evidence encoding attempts exhausted')
                        WHERE status IN ('queued','retry') AND attempts >= %s""",
                    (MAX_ATTEMPTS,))
        cur.execute("""WITH candidate AS (
                         SELECT id FROM qa_evidence_encoding_jobs
                          WHERE status IN ('queued','retry') AND attempts < %s
                          ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1
                       )
                       UPDATE qa_evidence_encoding_jobs j
                          SET status='running',attempts=attempts+1,claim_token=%s,
                              claimed_at=now(),lease_until=now()+(%s||' seconds')::interval,updated_at=now()
                         FROM candidate c WHERE j.id=c.id
                       RETURNING j.id,j.source_path,j.output_path,j.receipt_path,j.attempts""",
                    (MAX_ATTEMPTS, token, str(CLAIM_LEASE_S)))
        row = cur.fetchone()
    return (token, row) if row else (None, None)


def terminalize_exhausted():
    """Turn unclaimable final-attempt retries into durable terminal failures.

    Older publishers left these rows in ``retry`` forever: the claim predicate excludes them, while receipt
    backfill kept touching them every five seconds.  Terminalizing both the database row and its filesystem
    receipt makes the failed encode visible without discarding the already-durable raw WebM.
    """
    _ensure()
    with connection() as c, c.cursor() as cur:
        cur.execute("""UPDATE qa_evidence_encoding_jobs
                          SET status='failed',claim_token=NULL,lease_until=NULL,updated_at=now(),
                              finished_at=now(),
                              error=COALESCE(error,'evidence encoding attempts exhausted')
                        WHERE status IN ('queued','retry') AND attempts >= %s
                      RETURNING id,receipt_path,source_path,output_path,attempts,error""",
                    (MAX_ATTEMPTS,))
        rows = cur.fetchall()
    for job_id, receipt, source, output, attempts, error in rows:
        _write_receipt(receipt, {"status": "failed", "job_id": int(job_id), "source": source,
                                 "output": output, "attempts": int(attempts), "error": error,
                                 "reason": "raw WebM retained; convenience MP4 encoding exhausted",
                                 "updated_at": time.time()})
    return len(rows)


def reclaim_abandoned_claims():
    """Release dead/expired publisher generations even while live QA defers new encoding.

    Capacity priority must stop new ffmpeg work, not leave the health surface claiming that dead publishers
    are still running for the full ten-minute crash lease. Tokens contain the local publisher PID; absence of
    that exact process is sufficient to release its claim, while an unparseable or live PID fails closed and
    retains the normal lease backstop.
    """
    _ensure()
    reclaimed = 0
    with connection() as c, c.cursor() as cur:
        cur.execute("""SELECT id,claim_token,lease_until<=now()
                         FROM qa_evidence_encoding_jobs WHERE status='running'""")
        rows = cur.fetchall()
        for job_id, token, expired in rows:
            owner_dead = False
            match = re.fullmatch(r"publisher:(\d+):\d+", str(token or ""))
            if match:
                owner_dead = process_assurance.read_snapshot(int(match.group(1))) is None
            if not (expired or owner_dead):
                continue
            cur.execute("""UPDATE qa_evidence_encoding_jobs
                              SET status='retry',claim_token=NULL,lease_until=NULL,updated_at=now(),
                                  error=CASE WHEN %s THEN 'publisher process exited' ELSE
                                        COALESCE(error,'publisher lease expired') END
                            WHERE id=%s AND status='running' AND claim_token IS NOT DISTINCT FROM %s""",
                        (owner_dead, int(job_id), token))
            reclaimed += cur.rowcount
    return reclaimed


def _active_qa_explorers():
    """Return global latency-sensitive QA work, including lease-transition gaps.

    A rolling controller handoff briefly has no explorer lease. Looking only at tool rows let the publisher
    seize media capacity in that millisecond and keep ffmpeg running after the successor QA worker started.
    The durable controller state remains TESTQA/awaiting=fleet across generations, so it closes that race.
    """
    try:
        with connection() as c, c.cursor() as cur:
            cur.execute("""SELECT
                              (SELECT count(*) FROM orchestra_tool_leases
                                WHERE tool='qa_explore' AND lease_until>now()),
                              (SELECT count(*) FROM controller_state
                                WHERE phase='TESTQA' AND awaiting='fleet'),
                              (SELECT count(*)
                                 FROM orchestra_runs r
                                 JOIN orchestra_actors a
                                   ON a.run_id=r.run_id AND a.tenant_id=r.tenant_id
                                WHERE r.status='running' AND a.role='qa-coordinator'
                                  AND a.status IN ('working','blocked')
                                  AND a.last_active > now()-interval '2 minutes')""")
            leases, campaigns, direct_campaigns = cur.fetchone()
            return (max(0, int(leases or 0)) + max(0, int(campaigns or 0))
                    + max(0, int(direct_campaigns or 0)))
    except Exception:
        return None


def _finish(job_id, token, *, output=None, error=None, attempts=1):
    succeeded = output is not None
    status = "done" if succeeded else ("retry" if int(attempts) < MAX_ATTEMPTS else "failed")
    result = artifacts.probe_media(output) if succeeded else None
    with connection() as c, c.cursor() as cur:
        cur.execute("""UPDATE qa_evidence_encoding_jobs
                          SET status=%s,claim_token=NULL,lease_until=NULL,updated_at=now(),
                              finished_at=CASE WHEN %s IN ('done','failed') THEN now() ELSE NULL END,
                              error=%s,result=%s::jsonb
                        WHERE id=%s AND status='running' AND claim_token=%s
                        RETURNING receipt_path,source_path,output_path""",
                    (status, status, str(error or "")[:1000] or None,
                     json.dumps(result or {}, default=str), int(job_id), token))
        row = cur.fetchone()
    if row:
        receipt, source, target = row
        _write_receipt(receipt, {"status": status, "job_id": int(job_id), "source": source,
                                 "output": target, "attempts": int(attempts),
                                 "error": str(error or "")[:1000] or None,
                                 "result": result, "updated_at": time.time()})
    return {"job_id": int(job_id), "status": status, "result": result, "error": error}


def _defer_capacity(job_id, token, *, source, output, receipt, attempts):
    """Release a claim without consuming an attempt when live QA owns all media/browser capacity."""
    with connection() as c, c.cursor() as cur:
        cur.execute("""UPDATE qa_evidence_encoding_jobs
                          SET status='retry',attempts=GREATEST(0,attempts-1),claim_token=NULL,
                              lease_until=NULL,updated_at=now(),error='waiting for media capacity'
                        WHERE id=%s AND status='running' AND claim_token=%s RETURNING id""",
                    (int(job_id), token))
        owned = cur.fetchone() is not None
    if owned:
        _write_receipt(receipt, {"status": "deferred", "job_id": int(job_id),
                                 "source": str(source), "output": str(output),
                                 "attempts": max(0, int(attempts) - 1),
                                 "reason": "waiting for media capacity", "updated_at": time.time()})
    return {"job_id": int(job_id), "status": "deferred", "capacity_wait": True,
            "claim_released": owned}


def backfill_receipts(limit=RECEIPT_SCAN_LIMIT):
    scanned = found = queued = 0
    for root in _scan_roots():
        try:
            receipts = root.rglob("encoding-status.json")
            for receipt in receipts:
                scanned += 1
                try:
                    data = json.loads(receipt.read_text())
                except (OSError, ValueError, TypeError):
                    continue
                if data.get("status") not in {"deferred", "queued", "retry"}:
                    continue
                if found >= max(1, int(limit)):
                    return {"scanned": scanned, "found": found, "queued": queued, "bounded": True}
                found += 1
                source = data.get("source")
                if not source:
                    continue
                result = enqueue(source, Path(source).with_suffix(".mp4"), receipt)
                queued += int(bool(result.get("queued")))
        except OSError:
            continue
    return {"scanned": scanned, "found": found, "queued": queued, "bounded": False}


def backfill_receipts_if_due(limit=RECEIPT_SCAN_LIMIT, *, now=None):
    """Run the legacy receipt recovery scan at most once per configured interval.

    New explorers always create O(1)-discoverable spool records, so recursively walking the evidence tree on
    every idle five-second tick only burns CPU.  Receipt scanning remains as a crash/old-version recovery path.
    """
    global _last_receipt_backfill_at
    current = time.monotonic() if now is None else float(now)
    if _last_receipt_backfill_at and current - _last_receipt_backfill_at < RECEIPT_BACKFILL_INTERVAL_S:
        return {"skipped": True, "reason": "backfill interval has not elapsed"}
    result = backfill_receipts(limit)
    _last_receipt_backfill_at = current
    return result


def run_once():
    spool = drain_spool()
    reclaim_abandoned_claims()
    exhausted = terminalize_exhausted()
    active_qa = _active_qa_explorers()
    # Encoding is review convenience; the raw WebM and queue receipt are already durable. Do not indirectly
    # occupy one of the shared browser/media slots while a live explorer is trying to make product progress.
    # Fail closed on an unknown lease state. Dedicated encoder nodes can opt in explicitly.
    if not PUBLISH_DURING_ACTIVE_QA and (active_qa is None or active_qa > 0):
        return {"state": "deferred-active-qa", "active_qa_explorers": active_qa, "spool": spool,
                "terminalized": exhausted}
    token, row = _claim()
    if not row:
        backfill = backfill_receipts_if_due()
        token, row = _claim()
        if not row:
            return {"state": "idle", "spool": spool, "backfill": backfill,
                    "terminalized": exhausted}
    job_id, source, output, receipt, attempts = row
    safe = _safe_paths(source, output)
    if safe is None:
        return _finish(job_id, token, error="unsafe evidence paths", attempts=attempts)
    source, output = safe
    if not source.is_file() or source.stat().st_size <= 0:
        return _finish(job_id, token, error="raw WebM is missing or empty", attempts=attempts)
    encoded = artifacts.webm_to_mp4_result(source, output)
    if encoded.get("status") == "deferred":
        return _defer_capacity(job_id, token, source=source, output=output, receipt=receipt,
                               attempts=attempts)
    if encoded.get("status") != "done":
        return _finish(job_id, token, error=encoded.get("reason") or "MP4 encoding failed",
                       attempts=attempts)
    return _finish(job_id, token, output=encoded["path"], attempts=attempts)


def status():
    _ensure()
    with connection() as c, c.cursor() as cur:
        cur.execute("SELECT status,count(*) FROM qa_evidence_encoding_jobs GROUP BY status ORDER BY status")
        counts = {status: count for status, count in cur.fetchall()}
        cur.execute("""SELECT id,status,attempts,source_path,output_path,error,updated_at
                         FROM qa_evidence_encoding_jobs ORDER BY id DESC LIMIT 20""")
        recent = [{"id": row[0], "status": row[1], "attempts": row[2], "source": row[3],
                   "output": row[4], "error": row[5], "updated_at": row[6]}
                  for row in cur.fetchall()]
    return {"counts": counts, "recent": recent}


def _start_readiness_heartbeat(stopping, current_detail):
    """Keep generation-bound readiness fresh while a legitimate encode blocks the main loop."""
    import singleton_exec

    def beat():
        while not stopping.is_set():
            try:
                singleton_exec.mark_ready("evidence-publisher", current_detail())
            except Exception:
                pass
            stopping.wait(READY_HEARTBEAT_S)

    thread = threading.Thread(
        target=beat, name="evidence-publisher-readiness", daemon=True)
    thread.start()
    return thread


def serve(interval_s=5):
    interval_s = min(300.0, max(1.0, float(interval_s)))
    stopping = threading.Event()
    latest = {"value": {"state": "starting"}}
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_args: stopping.set())
    heartbeat = _start_readiness_heartbeat(stopping, lambda: latest["value"])
    try:
        while not stopping.is_set():
            try:
                latest["value"] = {"state": "working", "started_at": time.time()}
                latest["value"] = run_once()
            except Exception as exc:
                latest["value"] = {"state": "error", "error": str(exc)[:500]}
                print(json.dumps({"evidence_publisher_error": str(exc)[:500]}), flush=True)
            stopping.wait(interval_s)
    finally:
        stopping.set()
        heartbeat.join(timeout=2)
        try:
            import singleton_exec
            singleton_exec.clear_ready("evidence-publisher")
        except Exception:
            pass
    return 0


def main(argv=None):
    argv = list(argv or sys.argv[1:])
    if not argv or argv[0] == "once":
        print(json.dumps(run_once(), indent=2, default=str))
        return 0
    if argv[0] == "serve":
        return serve(argv[1] if len(argv) > 1 else 5)
    if argv[0] == "status":
        print(json.dumps(status(), indent=2, default=str))
        return 0
    raise SystemExit("usage: evidencepublisher.py once|serve [interval_s]|status")


if __name__ == "__main__":
    raise SystemExit(main())
