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
import hmac
import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from aoscfg import ENV


def _load_cfg(path):
    try:
        text = path.read_text()
    except OSError:
        return {}
    return {l.split("=", 1)[0].strip(): l.split("=", 1)[1].strip()
            for l in text.splitlines() if l.strip() and not l.startswith("#") and "=" in l}


_cfg = _load_cfg(ENV)
TOKEN = _cfg.get("AOS_API_TOKEN", "")
PY = str(SCRIPTS / ".." / ".venv" / "bin" / "python")


def _credential_ok(auth_header, token):
    """FAIL-CLOSED bearer check (findings #1 / #53).

    With an empty/unset token there is NO valid credential, so deny everything: otherwise the
    naive `auth == f"Bearer {token}"` makes `Bearer ` a valid header and any attacker is authed
    (fail-open auth bypass). Denying here cannot break liveness — /health stays public and a real
    token simply has to be configured. Constant-time compare avoids a token timing oracle.
    """
    if not token:
        return False
    return hmac.compare_digest(auth_header, f"Bearer {token}")


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, code, obj):
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def _authed(self):
        return _credential_ok(self.headers.get("Authorization", ""), TOKEN)

    def do_GET(self):
        u = urlparse(self.path); path = u.path
        if path == "/":
            import monitor
            h = monitor.check()
            rows = "".join(f"<tr><td>{k}</td><td>{'🟢 up' if v else '🔴 down'}</td></tr>" for k, v in h.items())
            html = (f"<!doctype html><meta charset=utf-8><title>agent-os</title>"
                    f"<style>body{{font-family:system-ui;background:#0f1115;color:#e6e8ec;max-width:680px;margin:3rem auto}}"
                    f"h1{{color:#4cc2a3}}table{{border-collapse:collapse;width:100%}}td{{border:1px solid #283041;padding:.5rem}}</style>"
                    f"<h1>agent-os</h1><p>Private, governed operating system for AI agents.</p>"
                    f"<h3>Services</h3><table>{rows}</table>"
                    f"<p style='color:#9aa3b2'>API: token-auth at /status /metrics, POST /products/&lt;name&gt;, /run/&lt;name&gt;</p>")
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
            return self.wfile.write(html.encode())
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
    if not TOKEN:
        # FAIL-CLOSED at startup: an empty token means every authed route would fail-open
        # (findings #1 / #53). Refuse to serve rather than stand up an open API.
        sys.exit("refusing to start: AOS_API_TOKEN is empty/unset in .env.local — this would "
                 "fail-open auth (any 'Bearer ' would authenticate). Set AOS_API_TOKEN and retry.")
    srv = HTTPServer(("127.0.0.1", port), H)   # localhost only; never 0.0.0.0
    print(f"agent-os API on http://127.0.0.1:{port} (Bearer auth)")
    srv.serve_forever()


def _selftest() -> int:
    """Prove the bearer check is FAIL-CLOSED on an empty/unset token (findings #1 / #53)."""
    problems = []
    cases = [
        # (token, auth_header, expected_ok, why)
        ("",        "",                 False, "empty token + empty header must deny"),
        ("",        "Bearer ",          False, "empty token + 'Bearer ' must deny (the bypass)"),
        ("",        "Bearer anything",  False, "empty token must deny any bearer"),
        ("s3cret",  "Bearer s3cret",    True,  "correct token must authenticate"),
        ("s3cret",  "Bearer wrong",     False, "wrong token must deny"),
        ("s3cret",  "Bearer s3cret ",   False, "trailing space must deny (exact match)"),
        ("s3cret",  "",                 False, "missing header must deny"),
        ("s3cret",  "s3cret",           False, "missing 'Bearer ' prefix must deny"),
    ]
    for token, header, expected, why in cases:
        got = _credential_ok(header, token)
        ok = "OK " if got == expected else "BAD"
        print(f"  [{ok}] token={token!r:10} header={header!r:18} -> {got}  ({why})")
        if got != expected:
            problems.append(why)

    # main() must refuse to serve when the configured token is empty.
    import unittest.mock as _mock
    refused = False
    with _mock.patch.object(sys.modules[__name__], "TOKEN", ""):
        try:
            main(0)
        except SystemExit:
            refused = True
    if not refused:
        problems.append("main() must refuse to start with an empty AOS_API_TOKEN")
    print(f"  [{'OK ' if refused else 'BAD'}] main() refuses to serve when AOS_API_TOKEN is empty")

    print("api.selftest")
    if problems:
        print("\nFAIL:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\nPASS: bearer auth is fail-closed; empty token denies all and blocks startup.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        sys.exit(_selftest())
    if len(sys.argv) > 1 and sys.argv[1] not in ("serve",):
        sys.exit("usage: api.py serve [port] | api.py selftest")
    main(int(sys.argv[2]) if len(sys.argv) > 2 else 8090)
