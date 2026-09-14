#!/usr/bin/env python3
"""Minimal public boundary for the Release Assurance sales funnel.

This deliberately does not import or route the Agent OS console. It exposes four allowlisted GET surfaces
and one rate-limited JSON intake so a temporary public tunnel cannot become a path to tenant operations.
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import assurance
import auth

MAX_BODY = 32 * 1024
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    ),
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "AgentOSAssurance/1"

    def log_message(self, _format, *_args):
        # Intake URLs and addresses do not belong in an unbounded web-server log.
        return

    def _send(self, code, body, content_type, *, cache="no-store"):
        body = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache)
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, value, *, retry_after=None):
        if retry_after is not None:
            # Base helper cannot add an arbitrary header, so write this small response explicitly.
            body = json.dumps(value).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Retry-After", str(retry_after))
            for name, header_value in SECURITY_HEADERS.items():
                self.send_header(name, header_value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._send(code, json.dumps(value), "application/json")

    def _client_identity(self):
        # This server binds to loopback and is reached through the local reverse proxy, so the proxy-provided
        # first address is the useful abuse key. A direct public socket is never opened by this process.
        forwarded = (self.headers.get("X-Forwarded-For") or "").split(",", 1)[0].strip()
        return forwarded or str((self.client_address or ("unknown",))[0])

    def _origin_ok(self):
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return True
        parsed = urlparse(origin)
        return parsed.scheme in {"http", "https"} and parsed.netloc == self.headers.get("Host", "")

    def _get(self):
        path = urlparse(self.path).path
        if path in {"/", "/assurance", "/assurance/"}:
            return self._send(200, assurance.page(), "text/html; charset=utf-8")
        if path in {"/assurance/sample", "/assurance/sample/"}:
            return self._send(200, assurance.sample_page(), "text/html; charset=utf-8")
        if path.startswith("/assurance/sample/assets/"):
            body = assurance.sample_asset(path.rsplit("/", 1)[-1])
            if body is not None:
                return self._send(200, body, "image/png", cache="public, max-age=86400")
        if path == "/health":
            return self._json(200, {"service": "release-assurance", "ok": True})
        return self._json(404, {"error": "not found"})

    def do_GET(self):
        self._get()

    def do_HEAD(self):
        self._get()

    def do_POST(self):
        if urlparse(self.path).path != "/api/assurance/intake":
            return self._json(404, {"error": "not found"})
        if not self._origin_ok():
            return self._json(403, {"error": "origin not allowed"})
        if (self.headers.get("Content-Type") or "").split(";", 1)[0].strip() != "application/json":
            return self._json(415, {"error": "application/json required"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._json(400, {"error": "invalid content length"})
        if length < 0 or length > MAX_BODY:
            return self._json(413, {"error": "request body too large"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._json(400, {"error": "invalid JSON"})
        try:
            limited = auth.public_rate_limit(
                "assurance-intake", body.get("email", "") if isinstance(body, dict) else "",
                self._client_identity(),
            )
        except Exception:
            return self._json(503, {"error": "intake temporarily unavailable — try again"})
        if not limited["allowed"]:
            return self._json(429, {"error": "too many requests — try again later",
                                    "retry_after_s": limited["retry_after_s"]},
                              retry_after=limited["retry_after_s"])
        try:
            result = assurance.submit(body)
        except Exception:
            return self._json(503, {"error": "intake temporarily unavailable — try again"})
        return self._json(200 if result.get("ok") else 400, result)


def serve(port=8100):
    server = ThreadingHTTPServer(("127.0.0.1", int(port)), Handler)
    server.serve_forever()


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "serve":
        serve(int(sys.argv[2]) if len(sys.argv) >= 3 else 8100)
    else:
        raise SystemExit("usage: assurance_public.py serve [port]")
