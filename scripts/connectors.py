#!/usr/bin/env python3
"""connectors.py — governed data ingestion (web / APIs / SNS / streams) (ADR 0004).

Network egress is DENIED by default (sandbox). A data-engineer connector may only reach
ALLOW-LISTED domains — fetched data lands in the object store by reference and the fetch is audited.
This is how the OS feeds live data from external sources without opening a hole in the privacy model.

    from connectors import ingest
Run with the agent-os venv python.
"""
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import objstore  # noqa: E402
import audit     # noqa: E402


class EgressDenied(Exception):
    pass


def ingest(url, product, allowlist, role="data-engineer", timeout=15, ttl_seconds=None):
    """Fetch url IFF its host is allow-listed; store the body in the object store; return blob_id."""
    host = urlparse(url).hostname or ""
    allowed = any(host == d or host.endswith("." + d) for d in allowlist)
    if not allowed:
        audit.append(actor=role, action="Ingest", resource=url, decision="deny",
                     payload={"reason": f"host {host} not in allowlist {allowlist}"})
        raise EgressDenied(f"egress to {host} denied (allowlist: {allowlist})")
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    bid = objstore.put(r.content, mime=r.headers.get("content-type", "application/octet-stream"), ttl_seconds=ttl_seconds)
    audit.append(actor=role, action="Ingest", resource=url, decision="allow", payload={"blob_id": bid, "bytes": len(r.content)})
    return bid


def _test():
    import threading, json
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(json.dumps({"feed": "live data row", "source": "allowlisted"}).encode())
    srv = HTTPServer(("127.0.0.1", 0), H); port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # allow-listed source -> fetched + stored by ref
    bid = ingest(f"http://127.0.0.1:{port}/feed", "yt-clone", allowlist=["127.0.0.1"])
    stored = objstore.get(bid)
    ok_allow = stored and b"live data row" in stored
    # non-allowlisted source -> blocked
    blocked = False
    try:
        ingest("http://evil.example.com/steal", "yt-clone", allowlist=["127.0.0.1"])
    except EgressDenied:
        blocked = True
    srv.shutdown()
    print("PASS: governed ingestion — allow-listed source stored by ref ✅, non-allowlisted blocked ✅"
          if (ok_allow and blocked and audit.verify()[0]) else "FAIL")
    sys.exit(0 if (ok_allow and blocked) else 1)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        _test()
