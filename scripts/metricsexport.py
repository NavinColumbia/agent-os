#!/usr/bin/env python3
"""metricsexport.py — Prometheus-exposition metrics for the agent fleet (real observability).

api.py /metrics returns JSON business KPIs — not scrapeable. This emits OpenMetrics/Prometheus text on
127.0.0.1:9101/metrics so Grafana Agent / Prometheus can scrape build health, queue reliability, agent
load, and spend. All values come from the live control plane (traces, tasks, audit_log). Binds localhost
(front with a scrape sidecar). Read-only.

    metricsexport.py serve [port]   # default 9101
    metricsexport.py print          # dump the exposition once
    metricsexport.py selftest
Run with the agent-os venv python.
"""
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


def _metrics():
    """Collect (name, type, help, value) tuples from the live control plane. DB-blip tolerant."""
    m = []
    try:
        with psycopg.connect(DB, connect_timeout=3) as c, c.cursor() as cur:
            # --- queue reliability (the new durable-queue signals) ---
            cur.execute("SELECT status, count(*) FROM tasks GROUP BY status")
            by_status = {s: n for s, n in cur.fetchall()}
            for st in ("pending", "active", "done", "dead"):
                m.append((f"agentos_tasks{{status=\"{st}\"}}", "gauge",
                          "Task queue depth by status", by_status.get(st, 0)))
            m.append(("agentos_task_dead_letter_depth", "gauge",
                      "Dead-lettered tasks awaiting human action", by_status.get("dead", 0)))
            cur.execute("SELECT COALESCE(sum(attempts),0) FROM tasks WHERE attempts IS NOT NULL")
            m.append(("agentos_task_retries_total", "counter", "Total task retry attempts", cur.fetchone()[0]))

            # --- build outcomes (last 24h) ---
            cur.execute("""SELECT decision, count(*) FROM audit_log
                           WHERE action='ProductComplete' AND ts > now() - interval '24 hours'
                           GROUP BY decision""")
            outcomes = {d: n for d, n in cur.fetchall()}
            launched = outcomes.get("LAUNCHED", 0)
            total = sum(outcomes.values()) or 0
            m.append(("agentos_builds_total_24h", "gauge", "Builds completed in last 24h", total))
            m.append(("agentos_builds_launched_24h", "gauge", "Builds LAUNCHED in last 24h", launched))
            m.append(("agentos_build_success_ratio_24h", "gauge", "LAUNCHED / total builds (24h)",
                      round(launched / total, 4) if total else 1.0))

            # --- agent spend + provider errors (from traces) ---
            cur.execute("""SELECT COALESCE(sum(cost_usd),0), COALESCE(sum(tokens_in),0)+COALESCE(sum(tokens_out),0)
                           FROM traces WHERE ts > now() - interval '24 hours'""")
            cost, toks = cur.fetchone()
            m.append(("agentos_agent_cost_usd_24h", "gauge", "Agent spend USD last 24h", round(float(cost), 4)))
            m.append(("agentos_agent_tokens_24h", "gauge", "Agent tokens last 24h", int(toks)))
            cur.execute("SELECT count(*) FROM traces WHERE rc <> 0 AND ts > now() - interval '24 hours'")
            m.append(("agentos_agent_step_errors_24h", "counter", "Failed agent steps last 24h",
                      cur.fetchone()[0]))

            # --- liveness ---
            cur.execute("SELECT count(*) FROM heartbeats WHERE ts > now() - interval '30 minutes'")
            m.append(("agentos_live_heartbeats", "gauge", "Components with a fresh heartbeat", cur.fetchone()[0]))
            cur.execute("""SELECT count(*) FROM watchdog_alerts WHERE first_seen > now() - interval '2 hours'""")
            m.append(("agentos_active_alerts", "gauge", "Watchdog alerts in last 2h", cur.fetchone()[0]))
        m.append(("agentos_scrape_ok", "gauge", "1 if the last scrape reached the control plane", 1))
    except Exception:
        m.append(("agentos_scrape_ok", "gauge", "1 if the last scrape reached the control plane", 0))
    return m


def exposition():
    """Render the metrics in Prometheus text exposition format."""
    out, seen = [], set()
    for name, typ, helptext, val in _metrics():
        base = name.split("{")[0]
        if base not in seen:                       # HELP/TYPE once per metric family
            out.append(f"# HELP {base} {helptext}")
            out.append(f"# TYPE {base} {typ}")
            seen.add(base)
        out.append(f"{name} {val}")
    return "\n".join(out) + "\n"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if urlparse(self.path).path == "/metrics":
            b = exposition().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        else:
            self.send_response(404); self.end_headers()


def _selftest():
    text = exposition()
    ok = ("# TYPE agentos_tasks gauge" in text
          and "agentos_task_dead_letter_depth" in text
          and "agentos_build_success_ratio_24h" in text
          and "agentos_scrape_ok 1" in text)        # control plane reachable in this env
    print(text.splitlines()[0] if text else "(empty)")
    print(f"families: tasks={'agentos_tasks' in text} dlq={'dead_letter' in text} "
          f"success={'success_ratio' in text} scrape_ok={'agentos_scrape_ok 1' in text}")
    print("PASS: prometheus exposition with real fleet metrics ✅" if ok else "FAIL")
    sys.exit(0 if ok else 1)


def _main(a):
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "print":
        print(exposition())
    elif a[0] == "serve":
        port = int(a[1]) if len(a) > 1 else 9101
        print(f"metrics on http://127.0.0.1:{port}/metrics")
        ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
    else:
        sys.exit("usage: metricsexport.py serve [port] | print | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
