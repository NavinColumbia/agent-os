#!/usr/bin/env python3
"""api.py — the agent-os HTTP API (the service surface). Lets external users/agents drive the OS:
check status, read metrics, scaffold products, run lifecycles. Bearer-token auth; binds 127.0.0.1
(expose over Tailscale via `tailscale serve`, never 0.0.0.0). Stdlib only — zero deps.

    api.py serve [port]
Endpoints:
    GET  /health                      (public)
    GET  /status                      (auth) — service health
    GET  /metrics?product=NAME        (auth) — product KPIs
    POST /products/NAME               (auth) — scaffold a governed product
    POST /run/NAME                    (auth) — run the lifecycle (returns result)
"""
import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
ENV = Path.home() / "projects" / "agent-os" / ".env.local"
_cfg = {l.split("=", 1)[0].strip(): l.split("=", 1)[1].strip()
        for l in ENV.read_text().splitlines() if l.strip() and not l.startswith("#") and "=" in l}
TOKEN = _cfg.get("AOS_API_TOKEN", "")
PY = str(SCRIPTS / ".." / ".venv" / "bin" / "python")


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, code, obj):
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def _authed(self):
        return self.headers.get("Authorization", "") == f"Bearer {TOKEN}"

    def do_GET(self):
        u = urlparse(self.path); path = u.path
        if path == "/health":
            return self._send(200, {"ok": True, "service": "agent-os"})
        if not self._authed():
            return self._send(401, {"error": "unauthorized — Bearer token required"})
        if path == "/status":
            import monitor
            return self._send(200, monitor.check())
        if path == "/metrics":
            import metrics
            prod = parse_qs(u.query).get("product", [None])[0]
            return self._send(200, metrics.kpis(prod))
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        parts = self.path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "products":
            r = subprocess.run(["bash", str(SCRIPTS / "new-product.sh"), parts[1]], capture_output=True, text=True)
            return self._send(200 if r.returncode == 0 else 400, {"product": parts[1], "ok": r.returncode == 0, "log": r.stdout[-400:] or r.stderr[-400:]})
        if len(parts) == 2 and parts[0] == "run":
            r = subprocess.run([PY, str(SCRIPTS / "controller.py"), "run", parts[1]], capture_output=True, text=True, timeout=300)
            ok = "LAUNCHED" in r.stdout
            return self._send(200 if ok else 400, {"product": parts[1], "launched": ok, "log": r.stdout[-400:]})
        return self._send(404, {"error": "not found"})


def main(port=8090):
    srv = HTTPServer(("127.0.0.1", port), H)   # localhost only; never 0.0.0.0
    print(f"agent-os API on http://127.0.0.1:{port} (Bearer auth)")
    srv.serve_forever()


if __name__ == "__main__":
    main(int(sys.argv[2]) if len(sys.argv) > 2 else 8090)
