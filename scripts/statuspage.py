#!/usr/bin/env python3
"""statuspage.py — a public, tenant-facing status page (operational / degraded / down).

The launch research calls for a status page "from day one": when builds are slow or the AI provider is
degraded, users should see honest system state instead of guessing. This derives a rollup from real
signals — service health (dashboard._health), active watchdog alerts, and dead-letter queue depth — and
renders a tiny HTML page + /status.json. Read-only, no secrets, binds 127.0.0.1 (front it with Tailscale
serve / a reverse proxy to expose publicly, same as the other surfaces).

    statuspage.py serve [port]    # default 8096
    statuspage.py json            # print the current status JSON
    statuspage.py selftest
Run with the agent-os venv python.
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)


def _active_alerts():
    """Watchdog alerts seen recently (the incident list). Best-effort — a DB blip = unknown, not crash."""
    try:
        with psycopg.connect(DB, connect_timeout=3) as c, c.cursor() as cur:
            cur.execute("""SELECT signature, level FROM watchdog_alerts
                           WHERE first_seen > now() - interval '2 hours' ORDER BY first_seen DESC LIMIT 20""")
            return [{"signature": s, "level": lv} for s, lv in cur.fetchall()]
    except Exception:
        return None


def _dlq_depth():
    try:
        with psycopg.connect(DB, connect_timeout=3) as c, c.cursor() as cur:
            cur.execute("SELECT count(*) FROM tasks WHERE status='dead'")
            return cur.fetchone()[0]
    except Exception:
        return None


def status():
    """Roll real signals into one public verdict: operational | degraded | major_outage | unknown."""
    components = {}
    try:
        import dashboard
        h = dashboard._health()                      # {component: ok/bad-ish} per service
        components = h if isinstance(h, dict) else {}
    except Exception:
        components = {}
    alerts = _active_alerts()
    dlq = _dlq_depth()

    if alerts is None and not components:
        verdict = "unknown"                          # can't reach control plane
    else:
        crit = [a for a in (alerts or []) if a.get("level") in ("crit", "critical", "error")]
        warn = [a for a in (alerts or []) if a.get("level") in ("warn", "warning")]
        down = [k for k, v in components.items()
                if isinstance(v, str) and v.lower() not in ("ok", "up", "healthy", "green")]
        if crit or down:
            verdict = "major_outage"
        elif warn or (dlq or 0) > 0:
            verdict = "degraded"
        else:
            verdict = "operational"
    return {"verdict": verdict, "components": components, "incidents": alerts or [],
            "dead_letter_depth": dlq}


_LABEL = {"operational": ("All systems operational", "#3fb950"),
          "degraded": ("Degraded performance", "#d29922"),
          "major_outage": ("Major outage", "#f85149"),
          "unknown": ("Status unavailable", "#7d8795")}


def _html():
    s = status()
    label, color = _LABEL.get(s["verdict"], _LABEL["unknown"])
    rows = "".join(
        f"<div class=row><span>{k}</span><span class=ok>{v}</span></div>" for k, v in s["components"].items()
    ) or "<div class=row><span class=mut>no component data</span></div>"
    inc = "".join(f"<li><b>{i['level']}</b> · {i['signature']}</li>" for i in s["incidents"]) \
        or "<li class=mut>No incidents in the last 2 hours.</li>"
    dlq = s["dead_letter_depth"]
    dlq_line = f"<div class=row><span>Build queue (dead-lettered)</span><span class={'bad' if (dlq or 0) else 'ok'}>{dlq if dlq is not None else '—'}</span></div>"
    return f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>agent-os · status</title><style>
body{{margin:0;background:#0a0d13;color:#d7dee8;font:15px/1.5 system-ui,sans-serif}}
.wrap{{max-width:620px;margin:0 auto;padding:36px 18px}}
.badge{{display:inline-block;padding:8px 16px;border-radius:999px;font-weight:600;color:#fff;background:{color}}}
.card{{background:#111722;border:1px solid #1e2733;border-radius:12px;padding:16px;margin:18px 0}}
.row{{display:flex;justify-content:space-between;border-top:1px solid #1e2733;padding:8px 0}}
.row:first-child{{border-top:none}}.ok{{color:#3fb950}}.bad{{color:#f85149}}.mut{{color:#7d8795}}
h1{{font-size:22px}}h2{{font-size:13px;text-transform:uppercase;letter-spacing:.7px;color:#7d8795}}
ul{{padding-left:18px}}</style>
<div class=wrap><h1>⬡ agent-os status</h1>
<p><span class=badge>{label}</span></p>
<div class=card><h2>Services</h2>{rows}{dlq_line}</div>
<div class=card><h2>Recent incidents</h2><ul>{inc}</ul></div>
<p class=mut>Auto-derived from live health, watchdog alerts, and queue depth.</p></div>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        p = urlparse(self.path).path
        if p in ("/", "/status"):
            b = _html().encode()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        elif p in ("/status.json", "/health"):
            b = json.dumps(status()).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        else:
            self.send_response(404); self.end_headers()


def _selftest():
    s = status()
    html = _html()
    ok = (s["verdict"] in _LABEL and "agent-os status" in html and "Services" in html
          and isinstance(s["incidents"], list))
    print(f"verdict={s['verdict']} components={len(s['components'])} incidents={len(s['incidents'])} dlq={s['dead_letter_depth']}")
    print("PASS: status page renders a real verdict from live signals ✅" if ok else "FAIL")
    sys.exit(0 if ok else 1)


def _main(a):
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "json":
        print(json.dumps(status(), indent=2))
    elif a[0] == "serve":
        port = int(a[1]) if len(a) > 1 else 8096
        print(f"status page on http://127.0.0.1:{port}")
        ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
    else:
        sys.exit("usage: statuspage.py serve [port] | json | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
