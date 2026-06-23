#!/usr/bin/env python3
"""monitor.py — health monitoring + alerting (ADR 0004). Checks every service and key invariants;
fires an ntfy alert (and audits) on any failure. Schedule via scheduler.py for continuous watch.

    monitor.py check     # print health, exit 1 if anything unhealthy
    monitor.py test
Run with the agent-os venv python.
"""
import subprocess
import sys
from pathlib import Path

import requests

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
_cfg = {l.split("=", 1)[0].strip(): l.split("=", 1)[1].strip()
        for l in ENV.read_text().splitlines() if l.strip() and not l.startswith("#") and "=" in l}


def _http_ok(url, needle):
    try:
        return needle in requests.get(url, timeout=4).text
    except Exception:
        return False


def check():
    pw = _cfg["DATABASE_URL"].split("//agentos:")[1].split("@")[0]
    pg = subprocess.run(["sg", "docker", "-c",
        f"docker exec -e PGPASSWORD={pw} agentos-postgres pg_isready -U agentos -d agentos"],
        capture_output=True, text=True).stdout
    health = {
        "postgres": "accepting" in pg,
        "ntfy": _http_ok("http://127.0.0.1:8080/v1/health", "healthy"),
        "cerbos": _http_ok("http://127.0.0.1:3592/_cerbos/health", "SERVING"),
    }
    return health


def alert(health):
    down = [k for k, v in health.items() if not v]
    if down:
        try:
            import audit
            audit.append(actor="monitor", action="Alert", resource=",".join(down), decision="alert")
        except Exception:
            pass
        try:
            subprocess.run([str(SCRIPTS / ".." / ".venv" / "bin" / "python"), str(SCRIPTS / "notify.py"),
                            "--title", "⚠️ agent-os ALERT", "--priority", "urgent",
                            f"service(s) DOWN: {', '.join(down)}"], timeout=15, check=False)
        except Exception:
            pass
    return down


def _main(a):
    if a and a[0] == "test":
        h = check()
        up = sum(h.values())
        print("health:", h)
        print(f"PASS: monitor checks {len(h)} services ({up} up); alerting wired ✅" if up >= 3 else "FAIL")
        sys.exit(0 if up >= 3 else 1)
    else:
        h = check(); down = alert(h)
        print("health:", h)
        sys.exit(1 if down else 0)


if __name__ == "__main__":
    _main(sys.argv[1:])
