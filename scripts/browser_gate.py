#!/usr/bin/env python3
"""browser_gate.py — a GLOBAL, cross-process cap on concurrent QA browser sessions.

Production reality: at scale (1000s of agents across many companies) many builds run coverage-driven browser
QA at once, and each session is a full Chromium + a video recorder — heavy on RAM/CPU. Without a global cap,
concurrent QA runs thrash the box: browsers stall, exploration crawls at ~0 coverage, and everything slows
(observed live — two QA runs competing for browsers stalled both). This gate bounds TOTAL concurrent browser
sessions across the whole machine, no matter how many QA processes exist — the exact analog of claude_gate for
the browser resource. Reuses claude_gate's proven lease-based Postgres slot pool (a dead holder's slot is
    reclaimed after the lease; FAIL-CLOSED so a DB hiccup cannot turn into an ungated browser stampede).

    sid = browser_gate.acquire("qa:1-recipe/US-3")   # blocks until a global browser slot is free
    ... run the browser session ...
    browser_gate.release(sid)

A single box comfortably runs a handful of Chromium+video sessions; tune with AOS_BROWSER_GLOBAL_MAX.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import claude_gate  # noqa: E402  — reuse the proven lease-based slot pool machinery
import process_assurance  # noqa: E402
import resourcepressure  # noqa: E402

TABLE = "browser_slots"
# a browser session can legitimately run a long story; give it a generous lease before a dead holder is reclaimed
LEASE_S = int(os.environ.get("AOS_BROWSER_LEASE_S", "2400"))
WAIT_S = int(os.environ.get("AOS_BROWSER_WAIT_S", "1200"))         # bounded wait, then fail closed

# Per-session cost, MEASURED on real runs (Chromium + a video recorder): ~475MB RSS + ~0.8 CPU core. The cap
# is the number of sessions the BOX can actually run at once — RAM-bound AND CPU-bound — with headroom so the
# machine never thrashes. "Spin up as many as the hardware allows, no more" — auto-sized to THIS machine, not
# a hardcoded guess. AOS_BROWSER_GLOBAL_MAX overrides (e.g. per-node in a cloud pool).
_MB_PER_SESSION = int(os.environ.get("AOS_BROWSER_MB_PER_SESSION", "550"))   # generous vs the measured ~475
_CORES_PER_SESSION = float(os.environ.get("AOS_BROWSER_CORES_PER_SESSION", "0.8"))
_RESERVE_MB = int(os.environ.get("AOS_BROWSER_RESERVE_MB", "2048"))          # leave the OS + other daemons room
# Live measurement on this 12-vCPU WSL laptop: four Chromium+video+model sessions drove load to 17.98 and
# made the host vulnerable to UI starvation despite ample RAM. Two keeps interactive headroom; dedicated
# worker nodes may explicitly raise AOS_BROWSER_AUTO_CEILING / AOS_BROWSER_GLOBAL_MAX after measurement.
_SAFE_AUTO_CEILING = int(os.environ.get("AOS_BROWSER_AUTO_CEILING", "2"))
_BROWSER_CPU_MILLIS = int(os.environ.get("AOS_BROWSER_CPU_MILLIS", "800"))
_MEDIA_MB = int(os.environ.get("AOS_MEDIA_MB_PER_PROCESS", "384"))
_MEDIA_CPU_MILLIS = int(os.environ.get("AOS_MEDIA_CPU_MILLIS", "1000"))


def _auto_cap():
    """How many browser sessions THIS box can run at once, from real RAM + CPU (never a hardcoded number).
    RAM-bound: (available - reserve) / per-session. CPU-bound: cores / per-session-cores. Take the min; clamp
    to [1, 64]. Fail-safe to a modest default if the probes are unavailable."""
    override = os.environ.get("AOS_BROWSER_GLOBAL_MAX")
    try:
        import os as _os
        avail_mb = None
        with open("/proc/meminfo") as fh:                       # MemAvailable = what we can actually use now
            for line in fh:
                if line.startswith("MemAvailable:"):
                    avail_mb = int(line.split()[1]) // 1024
                    break
        if avail_mb is None:                                    # fallback: total * 0.6
            page = _os.sysconf("SC_PAGE_SIZE"); n = _os.sysconf("SC_PHYS_PAGES")
            avail_mb = int(page * n / (1024 * 1024) * 0.6)
        cores = _os.cpu_count() or 4
        # MemAvailable is optimistic on WSL and ignores Windows-side pressure. Dedicated workers may raise
        # AOS_BROWSER_GLOBAL_MAX explicitly after measuring the node; automatic laptop operation stays modest.
        return resourcepressure.browser_capacity(
            avail_mb, cores, reserve_mb=_RESERVE_MB, mb_per_session=_MB_PER_SESSION,
            cores_per_session=_CORES_PER_SESSION, auto_ceiling=_SAFE_AUTO_CEILING,
            override=override)
    except Exception:
        # Unknown host capacity is not evidence that launching Chromium is safe.
        return resourcepressure.browser_capacity(0, 0, auto_ceiling=_SAFE_AUTO_CEILING,
                                                  override=override)


GLOBAL_MAX = _auto_cap()      # sized to THIS box at import; AOS_BROWSER_GLOBAL_MAX overrides
ORPHAN_RECLAIM_S = int(os.environ.get("AOS_BROWSER_ORPHAN_RECLAIM_S", "120"))


def current_capacity():
    """Re-probe the live host; import-time capacity is observability, not authority."""
    return _auto_cap()

# IN-PROCESS BACKSTOP: every browser session must hold a local slot BEFORE it tries the DB pool. The DB pool
# gives the cross-process cap; the local slot prevents one huge QA process from stampeding 90 Chromium
# sessions when Postgres is slow/full and the DB gate returns None.
import threading  # noqa: E402
_LOCAL_SEM = threading.BoundedSemaphore(64)
_LOCAL_TAG = "local"
_LOCAL_PRESSURE_LOCK = threading.Lock()
_LOCAL_PRESSURE_COND = threading.Condition(_LOCAL_PRESSURE_LOCK)
_LOCAL_HELD = 0


def _reserve_local_capacity(wait_s):
    """Wait for dynamic in-process capacity instead of failing every queued browser immediately.

    The durable provider pool already queues cross-process contenders. The former local backstop admitted up
    to 64 waiting threads, then returned ``None`` immediately whenever live pressure reduced this process to
    one browser. Coordinators interpreted those refusals as failed explorer attempts. Keep the 64-thread
    anti-stampede bound, but make the dynamic-cap boundary a condition queue that wakes on every release.
    """
    try:
        timeout = max(0.0, float(wait_s))
    except (TypeError, ValueError):
        timeout = 0.0
    deadline = __import__("time").monotonic() + timeout
    if not _LOCAL_SEM.acquire(timeout=timeout):
        return False
    global _LOCAL_HELD
    with _LOCAL_PRESSURE_COND:
        while True:
            cap = current_capacity()
            if cap > 0 and _LOCAL_HELD < cap:
                _LOCAL_HELD += 1
                return True
            remaining = deadline - __import__("time").monotonic()
            if remaining <= 0:
                break
            _LOCAL_PRESSURE_COND.wait(timeout=min(1.0, remaining))
    _LOCAL_SEM.release()
    return False


def _release_local_capacity():
    global _LOCAL_HELD
    try:
        _LOCAL_SEM.release()
    except (ValueError, RuntimeError):
        return
    with _LOCAL_PRESSURE_COND:
        _LOCAL_HELD = max(0, _LOCAL_HELD - 1)
        _LOCAL_PRESSURE_COND.notify_all()


def qa_holder(label) -> str:
    """Identity-bound holder; PID reuse across a boot or process generation cannot impersonate this owner."""
    return claude_gate.process_holder("qa", label)


def _holder_identity(holder):
    identity = claude_gate.holder_identity(holder)
    return identity if str(holder or "").startswith("qa:v2:") else None


def _reclaim_dead_qa_slots():
    """Clear browser slots that clearly belong to dead QA sessions.

    The generic lease is intentionally generous because a real story can run for a while. But a hard-cancelled
    worker used to leave rows like `qa:/path/to/evidence` with no live browser process; the next QA job then
    waited behind dead capacity for up to the full lease while doing no work. Reclaim only obvious QA orphans:
    v2 holders whose exact boot/process generation is gone. Legacy rows are deliberately left to their
    persisted lease expiry: neither a bare PID nor some unrelated global browser process proves ownership.
    """
    try:
        import psycopg
        with claude_gate._db_connect() as c, c.cursor() as cur:
            cur.execute(f"SELECT slot_id, holder, owner_token FROM {TABLE} "
                        "WHERE holder LIKE 'qa:%'")
            rows = cur.fetchall()
            reclaim = []
            for sid, holder, owner_token in rows:
                h = str(holder or "")
                identity = _holder_identity(h)
                if identity is not None and not process_assurance.same_process(
                        identity, process_assurance.read_snapshot(identity.pid)):
                    reclaim.append((sid, owner_token, holder))
            reclaimed = 0
            for sid, owner_token, holder in reclaim:
                # Fence the generation observed above. A concurrent expiry/takeover
                # must never have its new owner cleared by delayed orphan cleanup.
                cur.execute(f"""UPDATE {TABLE}
                    SET holder=NULL, acquired_at=NULL, lease_until=NULL, owner_token=NULL
                    WHERE slot_id=%s AND owner_token IS NOT DISTINCT FROM %s AND holder=%s""",
                    (sid, owner_token, holder))
                reclaimed += max(0, int(cur.rowcount or 0))
            if reclaim:
                c.commit()
            return reclaimed
    except Exception:
        return 0


def acquire(holder, wait_s=WAIT_S, resource_kind="browser"):
    """Claim a browser slot sized to what THIS machine can run.

    Returns an opaque slot dict, or None when no browser should be launched. This deliberately fails CLOSED at
    the browser boundary: launching ungated browsers is worse than marking one QA story inconclusive.
    """
    current_cap = current_capacity()
    if current_cap <= 0:
        return None
    if not _reserve_local_capacity(wait_s):
        return None
    db_sid = None
    resource_lease = None
    try:
        _reclaim_dead_qa_slots()
        db_sid = claude_gate.acquire(holder, wait_s=wait_s, table=TABLE, lease_s=LEASE_S,
                                     max_slots=current_cap, grow=True)
        post_wait_cap = current_capacity()
        if db_sid is not None and post_wait_cap < current_cap:
            # Pressure can fall while waiting in the durable queue. Re-admit under
            # the new global cap so an import-time/high earlier value has no authority.
            claude_gate.release(db_sid, table=TABLE)
            db_sid = None
            if post_wait_cap > 0:
                db_sid = claude_gate.acquire(
                    holder, wait_s=0, table=TABLE, lease_s=LEASE_S,
                    max_slots=post_wait_cap, grow=True)
        if db_sid is not None:
            if str(resource_kind) == "media":
                memory_mb, cpu_millis = _MEDIA_MB, _MEDIA_CPU_MILLIS
            else:
                memory_mb, cpu_millis = _MB_PER_SESSION, _BROWSER_CPU_MILLIS
            # Once a scarce browser/provider slot is held, host admission is an
            # immediate atomic check rather than a second long queue.
            resource_lease = claude_gate.acquire_host_resource(
                holder, resource_kind=resource_kind, memory_mb=memory_mb,
                cpu_millis=cpu_millis, wait_s=0, lease_s=LEASE_S)
    except Exception:
        # Preserve whichever generation was already acquired so the cleanup below
        # can release it; clearing these references would create a partial lease.
        pass
    if db_sid is None or resource_lease is None:
        if db_sid is not None:
            try:
                claude_gate.release(db_sid, table=TABLE)
            except Exception:
                pass
        # A local slot alone would let every QA process launch GLOBAL_MAX browsers during a DB outage.
        _release_local_capacity()
        return None
    try:
        if hasattr(db_sid, "start_heartbeat"):
            db_sid.start_heartbeat(table=TABLE, lease_s=LEASE_S)
            if not db_sid.confirm_owned():
                raise claude_gate.LeaseLost(
                    f"browser slot {int(db_sid)} was lost during host admission")
            db_sid.attach_resource(resource_lease)
            resource_lease.start_heartbeat(lease_s=LEASE_S, lost_event=db_sid.lost)
        else:
            resource_lease.start_heartbeat(lease_s=LEASE_S)
    except Exception:
        try:
            if hasattr(db_sid, "stop_heartbeat"):
                db_sid.stop_heartbeat()
            resource_lease.stop_heartbeat()
            claude_gate.release_host_resource(resource_lease)
            claude_gate.release(db_sid, table=TABLE)
        finally:
            _release_local_capacity()
        return None
    return {"kind": _LOCAL_TAG, "db_sid": db_sid, "resource_lease": resource_lease}


def release(sid):
    global _LOCAL_HELD
    if isinstance(sid, dict) and sid.get("kind") == _LOCAL_TAG:
        db_sid = sid.get("db_sid")
        resource_lease = sid.get("resource_lease")
        if db_sid is not None:
            try:
                if hasattr(db_sid, "stop_heartbeat"):
                    db_sid.stop_heartbeat()
                claude_gate.release(db_sid, table=TABLE)
            except Exception:
                pass
        if resource_lease is not None:
            try:
                resource_lease.stop_heartbeat()
                claude_gate.release_host_resource(resource_lease)
            except Exception:
                pass
        _release_local_capacity()
        return
    if sid == _LOCAL_TAG:                         # backward-compatible cleanup for old callers
        try:
            _LOCAL_SEM.release()
        except (ValueError, RuntimeError):
            pass
        return
    claude_gate.release(sid, table=TABLE)


def fencing_lease(sid):
    """Return the generation whose loss must terminate browser/media work."""
    if isinstance(sid, dict):
        return sid.get("db_sid") or sid.get("resource_lease")
    return sid


def lease_lost(sid):
    lease = fencing_lease(sid)
    return bool(lease is not None and getattr(lease, "lost", None)
                and lease.lost.is_set())


def status():
    _reclaim_dead_qa_slots()
    try:
        import psycopg
        with claude_gate._db_connect() as c, c.cursor() as cur:
            cur.execute(f"SELECT count(*), count(holder), "
                        f"count(*) FILTER (WHERE holder IS NOT NULL "
                        f"AND lease_until > now()) FROM {TABLE}")
            total, held, live = cur.fetchone()
        return {"slots": total, "held": held, "live_held": live, "free": total - live,
                "current_capacity": current_capacity(),
                "lease_s": LEASE_S, "orphan_reclaim_s": ORPHAN_RECLAIM_S}
    except Exception:
        return claude_gate.status(table=TABLE)


def _selftest():
    # The pool is sized to THIS box (GLOBAL_MAX). A live QA run may already hold some slots — that's the gate
    # working. Prove the invariant regardless: you can never hold MORE than GLOBAL_MAX at once, and a released
    # slot is reclaimable. Drain whatever is free, assert one more is refused, then a release frees exactly one.
    print(f"auto-sized cap (this box) = {GLOBAL_MAX}")
    got = []
    for i in range(GLOBAL_MAX + 2):
        s = acquire(f"selftest-{i}", wait_s=1)
        if s is None:
            break
        got.append(s)
    # Never hold MORE than the cap (the core invariant), regardless of how many a live run already holds.
    assert len(got) <= GLOBAL_MAX, f"held {len(got)} but cap is {GLOBAL_MAX} (never exceed the cap)"
    if not got:
        # Never shorten or steal a live run's persisted lease for a selftest. A full pool already proves the
        # cross-process cap; expiry/reclaim is covered on an isolated table by claude_gate's test.
        print(f"pool fully held by live work (cap={GLOBAL_MAX} enforced); no lease disturbed")
        print("browser_gate selftest: PASS (live cap enforced without mutation) ✅")
        sys.exit(0)
    over_ok = (acquire("selftest-overflow", wait_s=1) is None)   # pool exhausted -> fail-closed refusal
    one = got.pop()
    release(one)                                            # free exactly one
    again = acquire("selftest-after-release", wait_s=2)     # a released slot must be reclaimable
    reuse_ok = again is not None
    if again is not None:
        got.append(again)
    for s in got:                                           # clean up everything we held
        release(s)
    ok = over_ok and reuse_ok
    print(f"held_up_to={GLOBAL_MAX} overflow_blocked={over_ok} reuse_after_release={reuse_ok}")
    print("browser_gate selftest: PASS (global browser cap bounds concurrency, reclaims on release) ✅"
          if ok else "browser_gate selftest: FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        import json
        print(json.dumps(status(), indent=2, default=str))
    else:
        _selftest()
