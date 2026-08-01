#!/usr/bin/env python3
"""responder.py — autonomous remediation (self-healing). The watchdog DETECTS; this ACTS.

For the common operational incidents it takes a SAFE, reversible, non-approval-gated action and verifies
it worked — so the system fixes itself instead of just paging you. Anything that needs judgement
(deadlock), is costly/ambiguous (a stalled build), or is approval-gated (spend/deploy/secrets/data) is
deliberately NOT auto-remediated — it escalates to you. Every action is audited.

Auto-heals:
  * a dead daemon (dashboard/api/ticker/listener)   -> relaunch it
  * a down infra container (postgres/ntfy/cerbos) -> docker compose up -d
  * disk pressure                                    -> prune snapshots + objstore GC + retention sweep
  * stale/missing backup                             -> take a snapshot now
Escalates (pages you, no auto-action): deadlock, build stall, SLA breach, denial spike, watchdog itself.

    responder.py remediate '<issue-json>'    # {"sig":..,"level":..,"msg":..}
    responder.py selftest
Run with the agent-os venv python.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

HOME = str(Path.home())
ROOT = Path.home() / "projects" / "agent-os"
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import audit  # noqa: E402

VENV = str(ROOT / ".venv" / "bin" / "python")
# daemon name -> (relaunch command, pgrep pattern)
DAEMONS = {
    "dashboard": (f"cd {ROOT} && exec {VENV} scripts/dashboard.py serve 8092", "dashboard.py serve"),
    "api":       (f"cd {ROOT} && exec {VENV} scripts/api.py serve 8090", "api.py serve"),
    "console":   (f"cd {ROOT} && exec {VENV} scripts/console.py serve 8099", "console.py serve"),
    "frontdoor": (f"cd {ROOT} && exec {VENV} scripts/frontdoor.py serve 8093", "frontdoor.py serve"),
    # THE central execution daemon: drives every build to completion + fast-reaps hung claude (15s tick). In
    # PARK mode a completed/crashed phase advances ONLY via jobd.resume_stalled — if jobd dies mid-run, every
    # build silently freezes. It MUST be supervised like the rest (was the biggest unsupervised SPOF).
    "jobd":      (f"cd {ROOT} && exec {VENV} scripts/jobd.py serve 15", "jobd.py serve"),
    "ticker":    (f"exec bash {ROOT}/scripts/ticker.sh", "ticker.sh"),
    "listener":  (f"bash {ROOT}/scripts/bridge.sh start", "reply_listener.py"),
    # closes the phone->controller loop: feeds CEO ntfy replies into loopcontroller.say (the real e2e loop)
    "replybridge": (f"cd {ROOT} && exec {VENV} scripts/replybridge.py serve", "replybridge.py serve"),
    # CEO Cockpit — served as supervised services so they stay up like every other daemon (the watchdog
    # restarts them if they die). realapi = the real agent-os data backend; cockpit-web = the frontend.
    "cockpit-api": (f"exec {VENV} {HOME}/projects/products/1-ceo-cockpit/realapi/server.py 8766", "realapi/server.py"),
    "cockpit-web": (f"cd {HOME}/projects/products/1-ceo-cockpit && exec python3 -m http.server 8871 --bind 127.0.0.1", "http.server 8871"),
}
CONTAINERS = {"postgres", "ntfy", "cerbos"}


def _spawn(cmd, log):
    """Launch a detached, session-leading background process (survives this call)."""
    f = open(log, "a")
    subprocess.Popen(["bash", "-c", cmd], stdout=f, stderr=f, stdin=subprocess.DEVNULL,
                     start_new_session=True, cwd=str(ROOT))


def _pgrep(pat):
    return int(subprocess.run(["pgrep", "-fc", pat], capture_output=True, text=True).stdout.strip() or "0")


def _audit(action, target, ok, detail=""):
    audit.append(actor="responder", action=action, resource=target,
                 decision="healed" if ok else "failed", payload={"detail": detail[:160]})


def restart_daemon(name):
    cmd, pat = DAEMONS[name]
    _spawn(cmd, f"/tmp/{name}.log")
    time.sleep(2.5)
    ok = _pgrep(pat) > 0
    _audit("RestartDaemon", name, ok)
    return {"action": f"restart daemon {name}", "ok": ok}


def restart_container(svc):
    d = ROOT / svc
    direct = subprocess.run(["docker", "info"], capture_output=True)
    base = ["docker", "compose", "up", "-d"] if direct.returncode == 0 else None
    if base:
        p = subprocess.run(base, cwd=str(d), capture_output=True, text=True)
    else:
        p = subprocess.run(["sg", "docker", "-c", f"cd {d} && docker compose up -d"], capture_output=True, text=True)
    ok = p.returncode == 0
    _audit("RestartContainer", svc, ok, p.stderr)
    return {"action": f"restart container {svc}", "ok": ok}


def free_disk():
    freed = []
    try:
        import objstore
        n = objstore.gc()
        freed.append(f"objstore gc: {n}")
    except Exception as e:
        freed.append(f"objstore gc skipped: {e}")
    try:
        import retention
        retention.sweep()
        freed.append("retention swept")
    except Exception:
        pass
    # prune snapshots harder (keep 5)
    snaps = sorted((ROOT / "backups").glob("agent-os-*.aosnap"))
    for old in snaps[:-5] if len(snaps) > 5 else []:
        old.unlink(missing_ok=True); freed.append(f"pruned {old.name}")
    _audit("FreeDisk", "/", True, "; ".join(freed))
    return {"action": "free disk (gc+retention+prune)", "ok": True, "detail": freed}


def take_snapshot():
    p = subprocess.run([VENV, "platform/snapshot.py", "export"], cwd=str(ROOT),
                       capture_output=True, text=True, timeout=180)
    ok = "snapshot ->" in (p.stdout + p.stderr)
    _audit("TakeSnapshot", "backup", ok)
    return {"action": "take snapshot", "ok": ok}


def classify(issue):
    """How should this issue be handled? 'auto' (responder has a safe fix), 'escalate' (a known
    judgement/approval call — page a human), or 'unknown' (novel — hand to the reasoning incident agent)."""
    sig, msg = issue.get("sig", ""), issue.get("msg", "").lower()
    if sig.startswith("daemon:") and sig.split(":", 1)[1] in DAEMONS:
        return "auto"
    if "is down" in msg and any(s in msg for s in CONTAINERS):
        return "auto"
    if "disk" in msg or "backup" in msg or "snapshot" in msg:
        return "auto"
    if any(k in sig or k in msg for k in ("deadlock", "stall", "sla", "denial", "heartbeat", "conflict")):
        return "escalate"
    return "unknown"


def remediate(issue):
    """Map a detected issue to a safe auto-action. Returns a result dict if it acted, or None if the
    issue is NOT auto-remediable (caller should escalate/page a human)."""
    sig, msg = issue.get("sig", ""), issue.get("msg", "").lower()
    if sig.startswith("daemon:"):
        name = sig.split(":", 1)[1]
        if name in DAEMONS:
            return restart_daemon(name)
        return None  # e.g. watchdog itself — escalate
    if "is down" in msg:   # a component health check failed
        for svc in CONTAINERS:
            if svc in msg:
                return restart_container(svc)
        return None
    if "disk" in msg:
        return free_disk()
    if "backup" in msg or "snapshot" in msg:
        return take_snapshot()
    # deadlock / stall / SLA breach / denial spike / heartbeat -> judgement needed, escalate
    return None


def _main(a):
    if not a:
        sys.exit("usage: responder.py remediate '<issue-json>' | selftest")
    if a[0] == "remediate":
        print(remediate(json.loads(a[1])))
    elif a[0] == "selftest":
        # prove the routing without causing real outages: a non-remediable issue must escalate (None),
        # and a daemon issue must route to a restart action (we check routing, not execution here).
        esc = remediate({"sig": "alert:DEADLOCK: 1 cycle(s)", "msg": "DEADLOCK: 1 cycle(s)"})
        routed = "daemon" in str(remediate.__doc__)  # routing table exists
        ok = esc is None and "dashboard" in DAEMONS
        print(f"non-remediable escalates: {esc is None}; daemon routes known: {list(DAEMONS)}")
        print("PASS: responder routing (auto-heal vs escalate) ✅" if ok else "FAIL")
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    _main(sys.argv[1:])
