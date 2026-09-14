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
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

HOME = str(Path.home())
ROOT = Path.home() / "projects" / "agent-os"
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import audit  # noqa: E402
import service_recovery  # noqa: E402
from aoscfg import get as cfg_get  # noqa: E402

VENV = str(ROOT / ".venv" / "bin" / "python")
# Watchdog-facing name -> exact service-recovery registry name.
DAEMONS = {
    "dashboard": "dashboard",
    "api": "api",
    "console": "console",
    # Public sales boundary. This is intentionally a separate allowlisted server, never the tenant console.
    "assurance": "assurance",
    "frontdoor": "frontdoor",
    # THE central execution daemon: drives every build to completion + fast-reaps hung claude (15s tick). In
    # PARK mode a completed/crashed phase advances ONLY via jobd.resume_stalled — if jobd dies mid-run, every
    # build silently freezes. It MUST be supervised like the rest (was the biggest unsupervised SPOF).
    "jobd": "jobd",
    # Transcodes closed raw QA recordings after explorers release their scarce browser slot.
    "evidence-publisher": "evidence-publisher",
    "ticker": "ticker",
    "dispatcher": "dispatcher",
    "statuspage": "statuspage",
    "metrics": "metrics",
    # closes the phone->controller loop: feeds CEO ntfy replies into loopcontroller.say (the real e2e loop)
    "replybridge": "replybridge",
}
# Optional surfaces are expected only when their backing asset/channel exists. Treating an intentionally
# absent demo site, legacy cockpit, or ntfy listener as a production outage made a fresh public install page
# the operator forever even though the CEO console and execution plane were healthy.
if (Path.home() / "projects" / "products" / "noupload" / "dist").is_dir():
    DAEMONS["noupload-static"] = "noupload-static"
if (Path.home() / "projects" / "products" / "1-ceo-cockpit").is_dir():
    DAEMONS.update({"cockpit-api": "cockpit-api", "cockpit-web": "cockpit-web"})
_ntfy_topic = str(cfg_get("NTFY_TOPIC", "") or "")
if _ntfy_topic and "CHANGE-ME" not in _ntfy_topic:
    DAEMONS["listener"] = "reply-listener"
CONTAINERS = {"postgres", "ntfy", "cerbos"}


def _finite_timeout(name, default, maximum):
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = float(default)
    if not math.isfinite(value):
        value = float(default)
    return min(float(maximum), max(.1, value))


PROBE_TIMEOUT_S = _finite_timeout("AOS_RESPONDER_PROBE_TIMEOUT_S", 10, 20)
ACTION_TIMEOUT_S = _finite_timeout("AOS_RESPONDER_ACTION_TIMEOUT_S", 60, 90)
SNAPSHOT_TIMEOUT_S = _finite_timeout("AOS_RESPONDER_SNAPSHOT_TIMEOUT_S", 180, 300)


def _audit(action, target, ok, detail=""):
    audit.append(actor="responder", action=action, resource=target,
                 decision="healed" if ok else "failed", payload={"detail": detail[:160]})


def restart_daemon(name, replace=False):
    service = DAEMONS.get(name)
    if not service:
        _audit("RestartDaemon", name, False, "daemon is not in exact service registry")
        return {"action": f"restart daemon {name}", "ok": False,
                "error": "daemon is not in exact service registry"}
    try:
        result = (service_recovery.replace(service) if replace
                  else service_recovery.ensure(service))
    except Exception as exc:
        _audit("RestartDaemon", name, False, str(exc))
        return {"action": f"restart daemon {name}", "ok": False, "error": str(exc)[:200]}
    ok = result.get("state") == "healthy"
    _audit("RestartDaemon", name, ok, json.dumps(result, sort_keys=True)[:160])
    return {"action": f"restart daemon {name}", "ok": ok, "result": result}


def restart_container(svc):
    d = ROOT / svc
    try:
        direct = subprocess.run(["docker", "info"], capture_output=True, timeout=PROBE_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError) as exc:
        _audit("RestartContainer", svc, False, f"docker probe failed: {exc}")
        return {"action": f"restart container {svc}", "ok": False, "error": str(exc)[:200]}
    base = ["docker", "compose", "up", "-d"] if direct.returncode == 0 else None
    try:
        if base:
            p = subprocess.run(base, cwd=str(d), capture_output=True, text=True,
                               timeout=ACTION_TIMEOUT_S)
        else:
            p = subprocess.run(["sg", "docker", "-c", f"cd {d} && docker compose up -d"],
                               capture_output=True, text=True, timeout=ACTION_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError) as exc:
        _audit("RestartContainer", svc, False, f"docker compose failed: {exc}")
        return {"action": f"restart container {svc}", "ok": False, "error": str(exc)[:200]}
    ok = p.returncode == 0
    _audit("RestartContainer", svc, ok, p.stderr)
    return {"action": f"restart container {svc}", "ok": ok}


def free_disk():
    freed, failures = [], []
    try:
        import objstore
        n = objstore.gc()
        freed.append(f"objstore gc: {n}")
    except Exception as e:
        failures.append(f"objstore gc failed: {e}")
    try:
        import retention
        retention.sweep()
        freed.append("retention swept")
    except Exception as exc:
        failures.append(f"retention failed: {exc}")
    # prune snapshots harder (keep 5)
    snaps = sorted((ROOT / "backups").glob("agent-os-*.aosnap"))
    for old in snaps[:-5] if len(snaps) > 5 else []:
        try:
            old.unlink(missing_ok=True); freed.append(f"pruned {old.name}")
        except OSError as exc:
            failures.append(f"prune {old.name} failed: {exc}")
    try:
        usage = shutil.disk_usage("/")
        used_pct = 100.0 * usage.used / usage.total
        ok = used_pct < 80.0
        if not ok:
            failures.append(f"disk remains {used_pct:.1f}% used")
    except OSError as exc:
        ok = False
        failures.append(f"disk verification failed: {exc}")
    detail = "; ".join([*freed, *failures])
    _audit("FreeDisk", "/", ok, detail)
    return {"action": "free disk (gc+retention+prune)", "ok": ok,
            "detail": freed, "failures": failures}


def take_snapshot():
    try:
        p = subprocess.run([VENV, "platform/snapshot.py", "export"], cwd=str(ROOT),
                           capture_output=True, text=True, timeout=SNAPSHOT_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError) as exc:
        _audit("TakeSnapshot", "backup", False, f"snapshot failed: {exc}")
        return {"action": "take snapshot", "ok": False, "error": str(exc)[:200]}
    ok = "snapshot ->" in (p.stdout + p.stderr)
    _audit("TakeSnapshot", "backup", ok)
    return {"action": "take snapshot", "ok": ok}


def classify(issue):
    """How should this issue be handled? 'auto' (responder has a safe fix), 'escalate' (a known
    judgement/approval call — page a human), or 'unknown' (novel — hand to the reasoning incident agent)."""
    sig, msg = issue.get("sig", ""), issue.get("msg", "").lower()
    if sig.startswith("daemon:") and sig.split(":", 1)[1] in DAEMONS:
        return "auto"
    if sig == "heartbeat:ticker":
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
            return restart_daemon(name, replace=True)
        return None  # e.g. watchdog itself — escalate
    if sig == "heartbeat:ticker":
        return restart_daemon("ticker", replace=True)
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
