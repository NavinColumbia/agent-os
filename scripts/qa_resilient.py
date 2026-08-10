#!/usr/bin/env python3
"""qa_resilient.py — run the full QA→dev→QA loop to a CLEAN verdict, resiliently.

The owner's bar: "complete it all, failure-resilient in case I log out, prompt me to log in again, keep
going regardless of time/rounds." A subscription plan rate-limits a long QA sweep into provider-failover
storms; a WSL reboot can drop Postgres; the isolated agent creds go stale when the host session logs out.
This wrapper makes ONE QA run of a connected product survive all three:

  * BEFORE each attempt: re-sync creds by content and PROVE an isolated-config `claude -p` call authenticates.
    If auth is DOWN, it does NOT burn a doomed run — it writes NEED_LOGIN to the status file and exits 3 so
    the supervising agent can prompt the owner to re-login, then relaunch.
  * DB outage: wait (bounded) for Postgres to come back (self-heal restarts it) before starting.
  * Each attempt runs the real coverage-driven qa_gate against the connected app. A pass (qa_ok) → DONE. A
    non-pass that is CONTAMINATED by provider failovers (many inconclusive/unknown, no consistent app bug) is
    retried; a non-pass with a REAL reproducible app bug → stop and report it (that's a genuine finding).
  * State is checkpointed to a JSON status file after every attempt so a crash/kill resumes cleanly.

Usage:  qa_resilient.py <product> [--tenant demo] [--max-attempts 30]
Exit codes: 0 clean pass · 2 real app bug found · 3 needs re-login (relaunch after /login) · 4 gave up.
Run with the agent-os venv python.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "qa"))

STATUS = Path(os.environ.get("AOS_QA_RESILIENT_STATUS", "/tmp/qa-resilient-status.json"))


def _status(**kw):
    """Checkpoint progress to the status file (the supervising agent reads this)."""
    cur = {}
    if STATUS.exists():
        try:
            cur = json.loads(STATUS.read_text())
        except Exception:
            cur = {}
    cur.update(kw)
    cur["updated_at"] = time.time()
    try:
        STATUS.write_text(json.dumps(cur, indent=2, default=str))
    except Exception:
        pass
    return cur


def _db_up(timeout_s=1.0):
    try:
        import aoscfg
        import psycopg
        psycopg.connect(aoscfg.DB, connect_timeout=timeout_s).close()
        return True
    except Exception:
        return False


def _wait_db(max_wait_s=600):
    """Wait (bounded) for Postgres to come back — self-heal (responder/watchdog) restarts it."""
    start = time.time()
    while time.time() - start < max_wait_s:
        if _db_up():
            return True
        time.sleep(10)
    return False


def _auth_ok():
    """Re-sync the isolated agent creds by content, then PROVE a `claude -p` call authenticates under that
    config. Returns True only on a real rc==0 reply — so a stale/logged-out token is caught BEFORE a run."""
    try:
        import factory
        cfg = factory._agent_config_dir()             # syncs host creds -> isolated by content
    except Exception:
        return False
    try:
        r = subprocess.run(["claude", "-p", "Reply with the single word OK"],
                           capture_output=True, text=True, timeout=90,
                           env={**os.environ, "CLAUDE_CONFIG_DIR": str(cfg)})
        out = ((r.stdout or "") + (r.stderr or "")).lower()
        if r.returncode == 0 and "ok" in out:
            return True
        # a login/auth failure signature => NEED_LOGIN (distinct from a transient overload)
        return not any(s in out for s in ("not logged in", "unauthor", "login", "invalid api key",
                                          "authentication", "expired"))
    except Exception:
        return False


def _verdict_is_contaminated(verdict_json_path):
    """A non-pass is CONTAMINATED (retry-worthy, not a real fail) when its blocks are provider-failover noise:
    stories that were inconclusive/unknown, and NO story that fails consistently with a real app bug. Reads the
    written verdict artifact. Returns (contaminated: bool, real_bug_titles: list)."""
    try:
        d = json.loads(Path(verdict_json_path).read_text())
    except Exception:
        return True, []                                # can't read it → treat as noise, retry
    per = (d.get("coverage") or {}).get("per_status", {})
    unknown = int(per.get("unknown", 0))
    bugs = (d.get("bugs") or {}).get("items", []) if isinstance(d.get("bugs"), dict) else (d.get("bugs") or [])
    # a REAL app bug: blocking, and NOT flagged as infra/inconclusive
    real = [b.get("title") for b in bugs
            if b.get("blocking") and not b.get("infra") and "crashed mid-story" not in (b.get("title") or "")
            and "could not launch" not in (b.get("title") or "")]
    contaminated = (unknown > 0 or not d.get("passed")) and not real
    return contaminated, real


def run(product, tenant="demo", max_attempts=30):
    _status(product=product, phase="starting", attempt=0, max_attempts=max_attempts)
    os.environ["AOS_COCKPIT_DEMO"] = "1"               # QA-mode: real-but-cheap directive dispatch

    for attempt in range(1, max_attempts + 1):
        # 1) DB must be up (wait for self-heal after a reboot)
        if not _db_up():
            _status(phase="waiting_db", attempt=attempt)
            if not _wait_db():
                _status(phase="db_down_gave_up", attempt=attempt)
                print("QA_RESILIENT db_down_gave_up", flush=True)
                return 4

        # 2) creds must authenticate (catches a logged-out host session BEFORE a doomed run)
        if not _auth_ok():
            _status(phase="need_login", attempt=attempt,
                    message="isolated agent creds not authenticating — owner must /login, then relaunch")
            print("QA_RESILIENT need_login", flush=True)
            return 3

        # 3) bring up the CONNECTED app + run the real coverage-driven QA gate
        _status(phase="qa_running", attempt=attempt)
        print(f"QA_RESILIENT attempt {attempt}/{max_attempts} — running qa_gate({product})", flush=True)
        try:
            import loopcontroller as lc
            v = lc.qa_gate(product, platform="web")
        except Exception as e:
            _status(phase="qa_crashed", attempt=attempt, error=str(e)[:200])
            # a crash mid-attempt (often a DB/creds drop) — loop retries after re-checking DB/auth
            time.sleep(5)
            continue

        qa_ok = bool(v.get("qa_ok"))
        vj = v.get("verdict_json")
        contaminated, real_bugs = _verdict_is_contaminated(vj) if vj else (True, [])
        _status(phase="attempt_done", attempt=attempt, qa_ok=qa_ok, verdict=v.get("verdict"),
                verdict_json=vj, real_bugs=real_bugs, contaminated=contaminated)

        if qa_ok:
            _status(phase="PASSED", attempt=attempt, verdict=v.get("verdict"))
            print(f"QA_RESILIENT PASSED attempt={attempt} verdict={v.get('verdict')}", flush=True)
            return 0
        if real_bugs:                                  # a genuine, reproducible app defect — stop & report
            _status(phase="REAL_BUG", attempt=attempt, real_bugs=real_bugs, verdict=v.get("verdict"))
            print(f"QA_RESILIENT REAL_BUG attempt={attempt} bugs={real_bugs}", flush=True)
            return 2
        # contaminated by failover noise → retry (fresh creds re-synced at the top of the loop)
        print(f"QA_RESILIENT retry attempt={attempt} (contaminated by provider failover, no real bug)", flush=True)
        time.sleep(3)

    _status(phase="gave_up", attempt=max_attempts)
    print("QA_RESILIENT gave_up", flush=True)
    return 4


def _selftest():
    # contamination classifier: infra-only non-pass is contaminated; a real blocking bug is not.
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "v.json"
        p.write_text(json.dumps({"passed": False, "coverage": {"per_status": {"unknown": 5, "passed": 35}},
                                 "bugs": {"items": [{"title": "explorer crashed mid-story: timeout",
                                                     "blocking": True, "infra": True}]}}))
        c, real = _verdict_is_contaminated(str(p))
        assert c and not real, (c, real)
        p.write_text(json.dumps({"passed": False, "coverage": {"per_status": {"blocked": 1, "passed": 39}},
                                 "bugs": {"items": [{"title": "Save button does nothing", "blocking": True}]}}))
        c, real = _verdict_is_contaminated(str(p))
        assert not c and real == ["Save button does nothing"], (c, real)
    print("qa_resilient selftest: PASS (contamination classifier: infra-noise retries, real bug stops)")
    return 0


if __name__ == "__main__":
    a = sys.argv[1:]
    if a and a[0] == "selftest":
        sys.exit(_selftest())
    if not a:
        print(__doc__)
        sys.exit(1)
    product = a[0]
    tenant = "demo"
    max_attempts = 30
    if "--tenant" in a:
        tenant = a[a.index("--tenant") + 1]
    if "--max-attempts" in a:
        max_attempts = int(a[a.index("--max-attempts") + 1])
    sys.exit(run(product, tenant=tenant, max_attempts=max_attempts))
