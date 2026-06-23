#!/usr/bin/env python3
"""chaos.py — fault injection. Proves the self-healing actually works under adversarial conditions,
not just on a clean run: it KILLS live daemons and asserts the watchdog+responder bring them back.

Only targets safe, auto-healable daemons (never Postgres). Each scenario: verify up -> SIGKILL ->
verify down -> watchdog.tick() (detect + self-heal) -> verify back up. Reports a resilience scorecard.

    chaos.py            # run all scenarios + verdict
Run with the agent-os venv python.
"""
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path.home() / "projects" / "agent-os"
sys.path.insert(0, str(ROOT / "scripts"))
import watchdog  # noqa: E402

SCENARIOS = [
    ("dashboard", "dashboard.py serve", "http://127.0.0.1:8092/health"),
    ("api", "api.py serve", "http://127.0.0.1:8090/health"),
]


def _pgrep(pat):
    return [int(x) for x in subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True).stdout.split()]


def _http_ok(url):
    try:
        urllib.request.urlopen(url, timeout=3)
        return True
    except Exception:
        return False


def main():
    results = []
    for name, pat, url in SCENARIOS:
        up_before = _http_ok(url) or bool(_pgrep(pat))
        for p in _pgrep(pat):
            try:
                os.kill(p, signal.SIGKILL)
            except Exception:
                pass
        time.sleep(1.5)
        killed = not _pgrep(pat)
        watchdog.tick()                     # autonomous detect + self-heal
        time.sleep(2.5)
        healed = bool(_pgrep(pat))
        ok = up_before and killed and healed
        results.append({"scenario": name, "killed": killed, "healed": healed, "ok": ok})
        print(f"  {name:10} up_before={up_before} killed={killed} healed={healed} -> {'PASS' if ok else 'FAIL'}")
    passed = sum(r["ok"] for r in results)
    print(f"\nchaos resilience: {passed}/{len(results)} scenarios recovered autonomously")
    print("PASS: chaos resilience (kill -> watchdog -> auto-heal) ✅" if passed == len(results) else "FAIL")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
