import http.client
import json
import sys
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import assurance_public


@contextmanager
def _server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), assurance_public.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _request(port, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    data = response.read()
    result = response.status, dict(response.getheaders()), data
    connection.close()
    return result


def test_public_server_exposes_only_sales_allowlist_and_security_headers():
    with _server() as port:
        status, headers, body = _request(port, "GET", "/")
        assert status == 200 and b"Request a founding audit" in body
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        assert headers["X-Content-Type-Options"] == "nosniff"

        status, headers, body = _request(
            port, "HEAD", "/assurance/sample/assets/public-trust-boundary.png")
        assert status == 200 and headers["Content-Type"] == "image/png" and body == b""

        status, _headers, body = _request(port, "GET", "/api/projects")
        assert status == 404 and json.loads(body) == {"error": "not found"}


def test_public_intake_is_same_origin_json_and_rate_limited(monkeypatch):
    limited = []
    submitted = []
    monkeypatch.setattr(assurance_public.auth, "public_rate_limit",
                        lambda policy, account, client: limited.append((policy, account, client)) or
                        {"allowed": True, "retry_after_s": 0})
    monkeypatch.setattr(assurance_public.assurance, "submit",
                        lambda body: submitted.append(body) or {"ok": True, "request_id": "arp_test"})

    with _server() as port:
        host = f"127.0.0.1:{port}"
        status, _headers, _body = _request(
            port, "POST", "/api/assurance/intake", "{}",
            {"Content-Type": "application/json", "Origin": "https://attacker.example"})
        assert status == 403 and submitted == []

        payload = json.dumps({"email": "founder@example.com"})
        status, _headers, body = _request(
            port, "POST", "/api/assurance/intake", payload,
            {"Content-Type": "application/json", "Origin": f"http://{host}", "Host": host,
             "X-Forwarded-For": "203.0.113.9"})
        assert status == 200 and json.loads(body)["request_id"] == "arp_test"
        assert limited == [("assurance-intake", "founder@example.com", "203.0.113.9")]
        assert submitted == [{"email": "founder@example.com"}]
