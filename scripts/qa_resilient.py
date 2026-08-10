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


# Sustained-concurrency ceiling for QA on a SUBSCRIPTION plan. The real cause of the failover storm (39
# failovers in ~30 stories) is too many heavy model calls sustained for the ~hour a round takes — the
# subscription rate-limits, calls fail over to Codex, and coverage degrades. The cure is to keep sustained
# concurrency LOW so the plan is never hammered. Slow but CLEAN (owner: "don't care how long"). We start
# conservative and DE-ESCALATE further if an attempt still shows a failover storm.
# The REAL cure for the failover storm is factory's 529-patience (retry Anthropic overloads on the trusted
# engine before failover), not crippling concurrency. So start at a sane concurrency and only step down if a
# storm somehow still recurs. 529s now cost a brief wait, not a Codex degrade.
_CONC_LADDER = [8, 6, 4, 3]


def _apply_throttle(level_idx):
    """Pin model + browser concurrency for this attempt (env — read by claude_gate/qa_run at import time in
    the child call path). Lower index = more concurrency; we step down the ladder as needed."""
    n = _CONC_LADDER[min(level_idx, len(_CONC_LADDER) - 1)]
    os.environ["AOS_CLAUDE_GLOBAL_MAX"] = str(n)        # total concurrent claude calls box-wide
    os.environ["AOS_QA_PARALLEL"] = str(n)              # QA browser workers (match — no queueing past the gate)
    return n


def _reset_claude_slots(n):
    """Resize the cross-process claude concurrency pool to n by clearing it so claude_gate._ensure reseeds at
    the new size. Best-effort — the gate fails open if the table is unavailable."""
    try:
        import aoscfg
        import psycopg
        with psycopg.connect(aoscfg.DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM claude_slots")
            cur.execute("INSERT INTO claude_slots (slot_id) SELECT g FROM generate_series(1,%s) g "
                        "ON CONFLICT (slot_id) DO NOTHING", (n,))
            c.commit()
    except Exception:
        pass


def _run_qa_subprocess(product, timeout_s=None):
    """Run one qa_gate in a fresh subprocess (throttle env already set). Returns the verdict dict or None on a
    crash. The child prints one JSON line prefixed VERDICT_JSON: which we parse; it also writes the durable
    docs/QA-VERDICT.json regardless."""
    code = (
        "import sys,os,json;sys.path.insert(0,%r);sys.path.insert(0,%r);"
        "import loopcontroller as lc;"
        "v=lc.qa_gate(%r, platform='web');"
        "print('VERDICT_JSON:'+json.dumps(v, default=str))"
        % (str(SCRIPTS), str(SCRIPTS / "qa"), product)
    )
    try:
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           timeout=timeout_s, env=os.environ.copy())
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None
    for line in (p.stdout or "").splitlines():
        if line.startswith("VERDICT_JSON:"):
            try:
                return json.loads(line[len("VERDICT_JSON:"):])
            except Exception:
                return None
    return None


def _attempt_stormed(verdict_json_path):
    """Did this attempt still get rate-limited into a failover storm? True when the verdict has many
    inconclusive/unknown stories — the signal to throttle harder next attempt."""
    try:
        d = json.loads(Path(verdict_json_path).read_text())
        return int((d.get("coverage") or {}).get("per_status", {}).get("unknown", 0)) >= 5
    except Exception:
        return False


def run(product, tenant="demo", max_attempts=30):
    _status(product=product, phase="starting", attempt=0, max_attempts=max_attempts)
    os.environ["AOS_COCKPIT_DEMO"] = "1"               # QA-mode: real-but-cheap directive dispatch
    storm_level = 0                                     # index into _CONC_LADDER; rises when a storm recurs

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

        # 3) bring up the CONNECTED app + run the real coverage-driven QA gate — in a SUBPROCESS with a LOW
        #    sustained-concurrency throttle so the subscription isn't rate-limited into a failover storm. Fresh
        #    import picks up the throttle; the slot pool is reset so the new gate size actually applies. A
        #    subprocess also crash-isolates the attempt (a segfault can't kill this durable runner).
        conc = _apply_throttle(storm_level)
        _reset_claude_slots(conc)
        _status(phase="qa_running", attempt=attempt, concurrency=conc)
        print(f"QA_RESILIENT attempt {attempt}/{max_attempts} — qa_gate({product}) @ concurrency={conc}", flush=True)
        v = _run_qa_subprocess(product)
        if v is None:
            _status(phase="qa_crashed", attempt=attempt)
            time.sleep(5)
            continue

        qa_ok = bool(v.get("qa_ok"))
        vj = v.get("verdict_json")
        # if this attempt STILL stormed (many failovers), step the throttle down for the next try
        if _attempt_stormed(vj):
            storm_level += 1
            print(f"QA_RESILIENT storm persisted → de-escalating concurrency (level {storm_level})", flush=True)
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
