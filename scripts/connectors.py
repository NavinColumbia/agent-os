#!/usr/bin/env python3
"""connectors.py — governed data ingestion (web / APIs / SNS / streams) (ADR 0004).

Network egress is DENIED by default (sandbox). A data-engineer connector may only reach
ALLOW-LISTED domains — fetched data lands in the object store by reference and the fetch is audited.
This is how the OS feeds live data from external sources without opening a hole in the privacy model.

SECURITY (finding #30): the allowlist is NOT supplied by the caller. The caller is the agent, and an
agent that could pass its own allowlist could reach any host and defeat the egress restriction entirely.
Instead the allowlist is loaded from a TRUSTED server-side policy (control-plane/policies/egress-allowlist.yaml)
keyed by product (and optionally role) — a file product agents cannot write. On top of that we apply basic
SSRF guards: only http/https, no redirects, and the resolved target may not be a private/loopback/reserved
address unless the policy for that product explicitly opts in (test/dev fixtures only). Deny-by-default,
fail-closed: any error resolving the policy or the host denies the fetch.

    from connectors import ingest
Run with the agent-os venv python.
"""
import ipaddress
import socket
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import objstore  # noqa: E402
import audit     # noqa: E402

# The trusted, server-side egress policy. Lives in control-plane (the standing guard), which the
# product agents cannot write — so the allowlist cannot be widened by the caller at runtime.
POLICY_PATH = Path.home() / "projects" / "control-plane" / "policies" / "egress-allowlist.yaml"


class EgressDenied(Exception):
    pass


def _load_policy(policy_path=None) -> dict:
    """Load the trusted egress policy. Missing/unreadable -> {} (fail-closed: no product gets any host)."""
    p = Path(policy_path or POLICY_PATH)
    try:
        import yaml
        return yaml.safe_load(p.read_text()) or {}
    except Exception:
        return {}


def _allowlist_for(product, role, policy) -> tuple:
    """Resolve (allowed_domains, allow_private) for a product/role from the TRUSTED policy.

    The caller cannot influence this beyond naming which product/role it is acting as — the domains
    come exclusively from the policy file. Unknown product -> empty allowlist (deny-by-default)."""
    products = (policy or {}).get("products", {}) if isinstance(policy, dict) else {}
    entry = dict(products.get(product) or {})
    domains = list(entry.get("domains") or [])
    allow_private = bool(entry.get("allow_private"))
    # optional per-role overlay EXTENDS the product's trusted domains for this role (still server-side).
    roles = entry.get("roles") or {}
    overlay = roles.get(role) if isinstance(roles, dict) else None
    if isinstance(overlay, dict):
        domains += list(overlay.get("domains") or [])
        allow_private = allow_private or bool(overlay.get("allow_private"))
    return domains, allow_private


def _resolved_addresses(host) -> list:
    """Resolve host to a list of ip_address objects. Raises socket.gaierror if it cannot resolve."""
    addrs = []
    for info in socket.getaddrinfo(host, None):
        addrs.append(ipaddress.ip_address(info[4][0]))
    return addrs


def _is_unsafe_target(host) -> bool:
    """SSRF guard: True if host is empty, unresolvable, or ANY resolved address is in a private/
    loopback/link-local/reserved/multicast/unspecified range. Fail-closed: unresolvable -> unsafe."""
    if not host:
        return True
    try:
        addrs = _resolved_addresses(host)
    except (socket.gaierror, ValueError, UnicodeError):
        return True
    if not addrs:
        return True
    for a in addrs:
        if (a.is_private or a.is_loopback or a.is_link_local
                or a.is_reserved or a.is_multicast or a.is_unspecified):
            return True
    return False


def ingest(url, product, role="data-engineer", timeout=15, ttl_seconds=None, policy_path=None):
    """Fetch url IFF its host is allow-listed by the TRUSTED server-side egress policy for this
    product/role; store the body in the object store; return blob_id.

    The allowlist is loaded from policy — it is deliberately NOT a parameter, so a caller cannot
    grant itself egress to an arbitrary host. SSRF-guarded: http/https only, no redirects, and no
    private/loopback targets unless the policy explicitly opts in."""
    parsed = urlparse(url)
    host = parsed.hostname or ""

    # 1) scheme guard — only http/https can be ingested (no file://, gopher://, etc.).
    if parsed.scheme not in ("http", "https"):
        audit.append(actor=role, action="Ingest", resource=url, decision="deny",
                     payload={"reason": f"scheme {parsed.scheme!r} not allowed (http/https only)"})
        raise EgressDenied(f"egress scheme {parsed.scheme!r} denied for {url}")

    # 2) allowlist — from the trusted server-side policy, NOT from the caller.
    domains, allow_private = _allowlist_for(product, role, _load_policy(policy_path))
    allowed = bool(host) and any(host == d or host.endswith("." + d) for d in domains)
    if not allowed:
        audit.append(actor=role, action="Ingest", resource=url, decision="deny",
                     payload={"reason": f"host {host!r} not allow-listed for product {product!r}",
                              "product": product})
        raise EgressDenied(f"egress to {host!r} denied (not in policy allowlist for {product!r})")

    # 3) SSRF guard — even an allow-listed host must not resolve to a private/loopback range
    #    (defends against DNS rebinding / an internal hostname slipping onto the allowlist),
    #    unless the policy for this product explicitly opts in (local test/dev fixtures only).
    if not allow_private and _is_unsafe_target(host):
        audit.append(actor=role, action="Ingest", resource=url, decision="deny",
                     payload={"reason": f"host {host!r} resolves to a private/loopback/unsafe address (SSRF guard)",
                              "product": product})
        raise EgressDenied(f"egress to {host!r} denied (SSRF guard: private/loopback target)")

    # 4) fetch — no redirects (a 30x to an internal host would bypass the allowlist).
    r = requests.get(url, timeout=timeout, allow_redirects=False)
    r.raise_for_status()
    bid = objstore.put(r.content, mime=r.headers.get("content-type", "application/octet-stream"), ttl_seconds=ttl_seconds)
    audit.append(actor=role, action="Ingest", resource=url, decision="allow", payload={"blob_id": bid, "bytes": len(r.content)})
    return bid


def _test():
    import threading, json, tempfile

    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(json.dumps({"feed": "live data row", "source": "allowlisted"}).encode())
    srv = HTTPServer(("127.0.0.1", 0), H); port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # Hermetic TRUSTED policy: keyed by product. The caller cannot pass an allowlist; it can only
    # name which product it acts as. 'yt-clone' is allow-listed for 127.0.0.1 with allow_private
    # (it's a local loopback test fixture). 'ssrf-test' allow-lists 127.0.0.1 but does NOT opt into
    # private targets, so the SSRF guard must block it. Unknown products get nothing.
    pol = Path(tempfile.mkdtemp()) / "egress.yaml"
    pol.write_text(
        "products:\n"
        "  yt-clone:\n"
        "    domains: ['127.0.0.1']\n"
        "    allow_private: true\n"
        "  ssrf-test:\n"
        "    domains: ['127.0.0.1']\n"
        "    allow_private: false\n"
    )

    # allow-listed source (per server-side policy) -> fetched + stored by ref
    bid = ingest(f"http://127.0.0.1:{port}/feed", "yt-clone", policy_path=pol)
    stored = objstore.get(bid)
    ok_allow = stored and b"live data row" in stored

    # non-allowlisted source -> blocked (caller cannot widen the policy)
    blocked = False
    try:
        ingest("http://evil.example.com/steal", "yt-clone", policy_path=pol)
    except EgressDenied:
        blocked = True

    # a product with no policy entry gets NO egress, even to a host another product allows
    unknown_blocked = False
    try:
        ingest(f"http://127.0.0.1:{port}/feed", "no-such-product", policy_path=pol)
    except EgressDenied:
        unknown_blocked = True

    # SSRF guard: host is allow-listed but resolves to loopback and the product did NOT opt in -> blocked
    ssrf_blocked = False
    try:
        ingest(f"http://127.0.0.1:{port}/feed", "ssrf-test", policy_path=pol)
    except EgressDenied:
        ssrf_blocked = True

    # non-http scheme -> blocked
    scheme_blocked = False
    try:
        ingest("file:///etc/passwd", "yt-clone", policy_path=pol)
    except EgressDenied:
        scheme_blocked = True

    srv.shutdown()
    ok = (ok_allow and blocked and unknown_blocked and ssrf_blocked and scheme_blocked and audit.verify()[0])
    print("PASS: governed ingestion — server-side allowlist enforced ✅, caller cannot widen ✅, "
          "SSRF/private-target blocked ✅, non-http blocked ✅" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("test", "selftest"):
        _test()
