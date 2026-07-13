#!/usr/bin/env python3
"""scheduler.py — recurring autonomous jobs (ADR 0004): periodic data ingestion, retention sweeps,
monitors. register() a job + an interval; tick() runs whatever is due, audits each run, advances
next_run. Drive tick() from cron/systemd-timer/a loop. Combined with connectors.py this gives
"live data feeding from any source on a cadence".

    scheduler.py register <name> <interval_s> '<command>'
    scheduler.py bootstrap        # (idempotently) register the default recovery/sweep jobs
    scheduler.py tick             # run all due jobs (also self-heals missing defaults)
    scheduler.py enable <name> | disable <name> | deregister <name>
    scheduler.py list
    scheduler.py selftest
Run with the agent-os venv python.
"""
import os
import shlex
import subprocess
import sys
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent                      # agent-os root; relative job commands resolve against it
VENV_PY = str(ROOT / ".venv" / "bin" / "python")
sys.path.insert(0, str(SCRIPTS))
import audit  # noqa: E402

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)

# Per-job wall-clock bound. A job that exceeds this is killed and its turn skipped — it must NOT
# wedge the whole tick (see tick() isolation). Exposed as a module constant so the selftest can
# shrink it without sleeping for two minutes.
JOB_TIMEOUT = 120

# #8/#14: the recovery + sweep jobs that MUST exist for the platform to self-heal. ticker.sh only
# calls `scheduler.py tick`, so nothing else registers these; tick() bootstraps them idempotently
# (ON CONFLICT DO NOTHING — operator overrides of command/interval are preserved). Without this the
# schedules table can come up empty and stalled loops, interrupted builds, expired task leases and
# orphan processes are never recovered.
DEFAULT_SCHEDULES = [
    ("controller-resume", f"{VENV_PY} {SCRIPTS / 'loopcontroller.py'} resume", 600),
    # User-facing SLA: surface "taking longer than usual" on jobs that overran their ETA promptly (tight
    # cadence), well before the 30-min reaper. `controller-resume` also calls it as a 10-min backstop.
    ("controller-sla",    f"{VENV_PY} {SCRIPTS / 'loopcontroller.py'} watchdog", 120),
    ("resume-sweep",      f"{VENV_PY} {SCRIPTS / 'factory.py'} resume-sweep", 600),
    ("tasksweep",         f"{VENV_PY} {SCRIPTS / 'tasksweep.py'} run", 600),
    ("reap-orphans",      f"{VENV_PY} {SCRIPTS / 'reap.py'} run", 600),
    # F12: kill hung/orphaned `claude` agent calls (a dead parent orphans its claude child, which then holds
    # subscription capacity forever → new calls throttle+hang). Tight cadence; jobd also reaps every tick, this
    # is the always-on backstop for when jobd itself is down.
    ("claude-reap",       f"{VENV_PY} {SCRIPTS / 'clauded.py'} reap", 120),
    # Standing acceptance/dogfood pass (DAILY): a rotating demanding-user persona DRIVES the live
    # console through the qa explorer on the real journeys and FILES findings (blockers alert at once).
    # `cron` detaches the real run so JOB_TIMEOUT can't guillotine a long browser pass.
    ("acceptance-dogfood", f"{VENV_PY} {SCRIPTS / 'dogfood.py'} cron", 86400),
    # CEO chief-of-staff brief (REBUILD-PLAN B1): DAILY per-tenant briefing composed from each company's
    # real state, delivered into their console notifications — the "your executive team briefs you" moment.
    ("chiefofstaff-daily", f"{VENV_PY} {SCRIPTS / 'chiefofstaff.py'} push-daily", 86400),
]


def register(name, command, interval_s):
    # first run is ONE INTERVAL out, not immediately — so registering e.g. a weekly eval doesn't fire
    # a fleet of builds the instant it's set up.
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO schedules (name, command, interval_s, next_run)
                       VALUES (%s,%s,%s, now() + (%s||' seconds')::interval)
                       ON CONFLICT (name) DO UPDATE SET command=EXCLUDED.command, interval_s=EXCLUDED.interval_s""",
                    (name, command, interval_s, interval_s))
        c.commit()


def deregister(name):
    """#12: drop a schedule so it stops firing (e.g. when its custom agent is deleted). Returns True
    if a row was removed. Idempotent — removing a non-existent schedule is a no-op."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("DELETE FROM schedules WHERE name=%s", (name,))
        removed = cur.rowcount > 0
        c.commit()
    audit.append(actor="scheduler", action="Deregister", resource=name,
                 decision="removed" if removed else "absent", payload={})
    return removed


# aliases so callers probing for any reasonable de-register verb find one (customagents.delete does)
unregister = deregister
remove = deregister


def set_enabled(name, enabled):
    """#12: pause/resume a schedule WITHOUT deleting it (e.g. when a custom agent is toggled off).
    A disabled schedule is skipped by tick() but keeps its row + next_run. Returns True if updated."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("UPDATE schedules SET enabled=%s WHERE name=%s", (bool(enabled), name))
        updated = cur.rowcount > 0
        c.commit()
    audit.append(actor="scheduler", action="SetEnabled", resource=name,
                 decision="enabled" if enabled else "disabled",
                 payload={"found": updated})
    return updated


def bootstrap():
    """Idempotently register the default recovery/sweep schedules (#8, #14). Uses ON CONFLICT DO
    NOTHING so it never clobbers an operator's customised command/interval and never resets a
    running job's next_run. Returns the number of schedules newly created."""
    created = 0
    with psycopg.connect(DB) as c, c.cursor() as cur:
        for name, command, interval_s in DEFAULT_SCHEDULES:
            cur.execute("""INSERT INTO schedules (name, command, interval_s, next_run)
                           VALUES (%s,%s,%s, now() + (%s||' seconds')::interval)
                           ON CONFLICT (name) DO NOTHING""",
                        (name, command, interval_s, interval_s))
            created += cur.rowcount
        c.commit()
    if created:
        audit.append(actor="scheduler", action="Bootstrap", resource="defaults",
                     decision="registered", payload={"created": created})
    return created


def _safe_argv(command):
    """Parse a stored command into an argv list and validate it — we run jobs WITHOUT a shell, so we
    must (a) tokenise ourselves and (b) refuse anything that isn't a legitimate agent-os job.

    FAIL-CLOSED: returns None for anything we can't prove is safe (unparseable, empty, or an
    executable that is neither the venv python interpreter nor a file living under the agent-os root).
    A None here means tick() will NOT execute the command — a corrupted/injected schedules row can no
    longer get a shell. We still advance next_run for it so one bad row can't wedge the loop."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if not argv:
        return None
    prog = argv[0]
    base = os.path.basename(prog)
    if base == "python" or base == "python3" or base.startswith("python3."):
        return argv
    # otherwise the program must resolve to a file inside the agent-os tree
    p = Path(prog) if os.path.isabs(prog) else (ROOT / prog)
    try:
        p.resolve().relative_to(ROOT.resolve())
        return argv
    except ValueError:
        return None


def tick():
    """Run all due jobs; return how many actually executed.

    #15: each job is isolated. A job that raises, times out, or fails validation must NOT abort the
    tick or starve the other due jobs, and must NOT pin next_run (a 'poison' job that never advances
    would block forever). So every job is wrapped, TimeoutExpired is caught, and next_run is advanced
    for EVERY due job no matter the outcome. We also self-heal the default schedules first so a fresh
    install starts recovering immediately even though ticker.sh only ever calls `tick`."""
    bootstrap()
    ran = 0
    # CLAIM phase — a SHORT transaction: grab all due jobs (FOR UPDATE SKIP LOCKED so concurrent tickers
    # don't double-claim) and advance next_run+last_run immediately, then commit. We do NOT run any job
    # inside this transaction: running subprocesses inside the open txn pinned it idle-in-transaction AND
    # held the row locks for the whole tick (a real hang risk per hang-resilience). Advancing next_run at
    # claim time also keeps the 'slow/poison job never pins the loop' guarantee.
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT name, command FROM schedules WHERE enabled AND next_run <= now() "
                    "ORDER BY next_run FOR UPDATE SKIP LOCKED")
        due = cur.fetchall()
        for name, _command in due:
            cur.execute("UPDATE schedules SET last_run=now(), "
                        "next_run=now() + (interval_s || ' seconds')::interval WHERE name=%s", (name,))
        c.commit()
    # RUN phase — no open transaction, no row locks held while subprocesses run (which can take up to
    # JOB_TIMEOUT each). Each job is isolated: a raise/timeout/rejection never aborts the others.
    for name, command in due:
        decision, rc = "executed", None
        argv = _safe_argv(command)
        if argv is None:
            decision = "rejected"                            # fail-closed: unvalidated command never runs
        else:
            try:
                rc = subprocess.run(argv, cwd=str(ROOT), capture_output=True,
                                    timeout=JOB_TIMEOUT).returncode
                if rc != 0:
                    decision = "nonzero"
                else:
                    ran += 1
            except subprocess.TimeoutExpired:
                decision = "timeout"
            except Exception as e:                           # never let one job abort the whole tick
                decision = "error"
                rc = None
                audit.append(actor="scheduler", action="RunJob", resource=name,
                             decision="error", payload={"error": str(e)[:300]})
        audit.append(actor="scheduler", action="RunJob", resource=name, decision=decision,
                     payload={"rc": rc})
    return ran


def _selftest():
    names = ["selftest-job", "selftest-good", "selftest-poison", "selftest-denied", "selftest-dis"]
    marker = Path("/tmp/scheduler-selftest-marker")
    marker.unlink(missing_ok=True)
    global JOB_TIMEOUT
    saved_timeout = JOB_TIMEOUT
    try:
        # ---- (1) basic: a due job runs once, advances next_run, is not re-run immediately ----
        register("selftest-job", "true" and f"{VENV_PY} -c pass", interval_s=3600)
        with psycopg.connect(DB) as c, c.cursor() as cur:   # force due (register defers first run)
            cur.execute("UPDATE schedules SET next_run=now() WHERE name='selftest-job'"); c.commit()
        n1 = tick()                                          # runs it now (plus any other due jobs)
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT last_run IS NOT NULL, next_run > now() FROM schedules WHERE name='selftest-job'")
            ran_recorded, scheduled_future = cur.fetchone(); c.commit()
        basic_ok = n1 >= 1 and ran_recorded and scheduled_future

        # ---- (15) isolation: a poison (timeout) job next to a good one — good still runs, BOTH advance ----
        JOB_TIMEOUT = 1
        register("selftest-good", f"{VENV_PY} -c pass", interval_s=3600)
        register("selftest-poison", f"{VENV_PY} -c \"import time; time.sleep(30)\"", interval_s=3600)
        # ---- (15) fail-closed: a non-agent-os command is REJECTED, never executed ----
        register("selftest-denied", f"/usr/bin/touch {marker}", interval_s=3600)
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("UPDATE schedules SET next_run=now() WHERE name IN "
                        "('selftest-good','selftest-poison','selftest-denied')"); c.commit()
        tick()                                               # must NOT raise despite the poison job
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT name, next_run > now() FROM schedules WHERE name IN "
                        "('selftest-good','selftest-poison','selftest-denied')")
            adv = dict(cur.fetchall()); c.commit()
        isolation_ok = adv.get("selftest-good") and adv.get("selftest-poison") and adv.get("selftest-denied")
        denied_ok = not marker.exists()                      # rejected command never touched the FS
        JOB_TIMEOUT = saved_timeout

        # ---- (12) set_enabled disables (kept, not deleted); deregister removes ----
        register("selftest-dis", f"{VENV_PY} -c pass", interval_s=3600)
        set_enabled("selftest-dis", False)
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("UPDATE schedules SET next_run=now() WHERE name='selftest-dis'"); c.commit()
        tick()
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT enabled, last_run IS NULL FROM schedules WHERE name='selftest-dis'")
            still_disabled, never_ran = cur.fetchone(); c.commit()
        disable_ok = (not still_disabled) and never_ran      # disabled row kept AND skipped by tick

        removed = deregister("selftest-dis")
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT count(*) FROM schedules WHERE name='selftest-dis'")
            gone = cur.fetchone()[0] == 0; c.commit()
        deregister_ok = removed and gone

        # ---- (8/14) bootstrap registered the default recovery/sweep jobs ----
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT count(*) FROM schedules WHERE name = ANY(%s)",
                        ([n for n, _, _ in DEFAULT_SCHEDULES],))
            defaults_present = cur.fetchone()[0] == len(DEFAULT_SCHEDULES); c.commit()

        ok = (basic_ok and isolation_ok and denied_ok and disable_ok and deregister_ok
              and defaults_present)
        print(f"basic(run+advance)={basic_ok} isolation(poison-doesnt-block)={isolation_ok} "
              f"fail-closed(rejected-not-run)={denied_ok} set_enabled+skip={disable_ok} "
              f"deregister={deregister_ok} defaults-bootstrapped={defaults_present}")
        print("PASS: scheduler runs due jobs once, isolates poison jobs, fail-closed on bad "
              "commands, supports enable/deregister, bootstraps recovery jobs, audited ✅"
              if ok else "FAIL")
    finally:
        JOB_TIMEOUT = saved_timeout
        marker.unlink(missing_ok=True)
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM schedules WHERE name = ANY(%s)", (names,)); c.commit()
    sys.exit(0 if ok else 1)


def _main(a):
    if a and a[0] == "register":
        register(a[1], a[3], int(a[2])); print(f"registered '{a[1]}' every {a[2]}s")
    elif a and a[0] == "bootstrap":
        print(f"bootstrapped {bootstrap()} default schedule(s)")
    elif a and a[0] == "tick":
        print(f"ran {tick()} due job(s)")
    elif a and a[0] in ("enable", "disable") and len(a) > 1:
        found = set_enabled(a[1], a[0] == "enable")
        print(f"{'enabled' if a[0] == 'enable' else 'disabled'} '{a[1]}'" if found else f"no such schedule '{a[1]}'")
    elif a and a[0] == "deregister" and len(a) > 1:
        print(f"deregistered '{a[1]}'" if deregister(a[1]) else f"no such schedule '{a[1]}'")
    elif a and a[0] == "list":
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT name, interval_s, enabled, last_run, next_run FROM schedules ORDER BY name")
            for r in cur.fetchall():
                print(r)
    elif a and a[0] in ("selftest", "test"):
        _selftest()
    else:
        sys.exit("usage: scheduler.py register|bootstrap|tick|enable|disable|deregister|list|selftest ...")


if __name__ == "__main__":
    _main(sys.argv[1:])
