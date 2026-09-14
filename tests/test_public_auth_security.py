import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import console  # noqa: E402


def test_browser_auth_uses_opaque_cookie_and_never_requires_api_token_storage():
    page = console.PAGE
    assert "r.api_token||'cookie'" in page
    assert "if(r.api_token)localStorage.setItem('aos_tenant',r.api_token)" in page  # legacy CLI-token bridge only
    assert "if(TOK&&TOK!=='cookie')" in page
    source = Path(console.__file__).read_text()
    assert 'public.pop("api_token", None)' in source
    assert "HttpOnly; SameSite=Lax" in source
    assert 'p == "/api/logout"' in source


def test_public_auth_routes_are_durably_rate_limited():
    source = Path(console.__file__).read_text()
    auth_source = (ROOT / "scripts" / "auth.py").read_text()
    assert "auth.public_rate_limit(" in source
    assert "auth_rate_limits" in auth_source
    assert "ON CONFLICT(action,key_hash,bucket) DO UPDATE" in auth_source
    assert 'return self._json(429' in source
    assert '"free")  # plan is server-owned' in source
    assert "MAX_REQUEST_BODY = 1_048_576" in source
    assert 'return self._json(413' in source


def test_public_proxy_adds_csp_and_transport_headers():
    caddy = (ROOT / "deploy" / "Caddyfile").read_text()
    assert "Strict-Transport-Security" in caddy
    assert "Content-Security-Policy" in caddy
    assert "frame-ancestors 'none'" in caddy
    assert "form-action 'self'" in caddy
