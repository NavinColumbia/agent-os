#!/usr/bin/env python3
"""mac_runner.py — remote macOS runner for iOS/macOS tasks (the Mac joins as a worker over Tailscale).

The control plane stays on this Linux/WSL box; the Mac is a specialized executor reached over SSH on
the tailnet. This connector runs commands on the Mac (Xcode, simulator, Appium) and audits each one —
enabling native iOS build + simulator E2E that can't run on Linux.

Config in ~/projects/agent-os/.env.local:
    MAC_HOST=<mac-tailnet-name-or-ip>     MAC_USER=<your-mac-username>

    mac_runner.py check                 # verify SSH + Xcode on the Mac
    mac_runner.py sims                  # list iOS simulators
    mac_runner.py run '<shell cmd>'     # run an arbitrary command on the Mac
    mac_runner.py ios-e2e <app.app> <bundle_id>   # boot sim, install, launch (E2E scaffold)
Run with the agent-os venv python.
"""
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit  # noqa: E402

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
_cfg = {l.split("=", 1)[0].strip(): l.split("=", 1)[1].strip()
        for l in ENV.read_text().splitlines() if l.strip() and not l.startswith("#") and "=" in l}
HOST = _cfg.get("MAC_HOST"); USER = _cfg.get("MAC_USER")


def run(cmd, timeout=300):
    """Run a shell command on the Mac over SSH; audited. Returns {rc, out, err}."""
    if not (HOST and USER):
        raise RuntimeError("MAC_HOST/MAC_USER not set in .env.local — add the Mac (see MAC-SETUP.md)")
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", f"{USER}@{HOST}", cmd]
    p = subprocess.run(ssh, capture_output=True, text=True, timeout=timeout)
    audit.append(actor="mac-runner", action="RemoteExec", resource=(cmd[:120]),
                 decision="executed", payload={"rc": p.returncode, "host": HOST})
    return {"rc": p.returncode, "out": p.stdout.strip(), "err": p.stderr.strip()[:500]}


def check():
    ssh_ok = run("echo ok")["out"] == "ok"
    xcode = run("xcodebuild -version 2>/dev/null | head -1")["out"] if ssh_ok else ""
    simctl = run("xcrun simctl help >/dev/null 2>&1 && echo yes")["out"] if ssh_ok else ""
    return {"ssh": ssh_ok, "xcode": xcode, "simctl_available": simctl == "yes"}


def list_sims():
    return run("xcrun simctl list devices available")["out"]


def ios_e2e(app_path, bundle_id, device="iPhone 15"):
    """Minimal native E2E: boot a simulator, install the app, launch it. Extend with Appium/XCUITest
    for click-through assertions + screenshots (which flow back to the QA harness / object store)."""
    steps = [
        f"xcrun simctl boot '{device}' || true",
        f"xcrun simctl install booted '{app_path}'",
        f"xcrun simctl launch booted '{bundle_id}'",
        f"xcrun simctl io booted screenshot /tmp/ios-e2e.png",
    ]
    out = []
    for s in steps:
        r = run(s); out.append({"cmd": s, "rc": r["rc"], "out": r["out"][:200]})
        if r["rc"] != 0 and "boot" not in s:
            break
    return out


def _main(a):
    if not a:
        sys.exit("usage: mac_runner.py check|sims|run|ios-e2e ...")
    if a[0] == "check":
        print(check())
    elif a[0] == "sims":
        print(list_sims())
    elif a[0] == "run":
        print(run(a[1]))
    elif a[0] == "ios-e2e":
        for step in ios_e2e(a[1], a[2]):
            print(step)


if __name__ == "__main__":
    _main(sys.argv[1:])
