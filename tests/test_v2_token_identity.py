from dataclasses import dataclass
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from agent_os.api.auth import HMACTokenIdentity, OIDCTokenIdentity, Principal


def test_signed_token_binds_subject_tenant_roles_and_rejects_tampering(monkeypatch):
    identity = HMACTokenIdentity("s" * 32)
    token = identity.issue(
        subject_id="human-1", organization_id="tenant-1", roles=("owner",), now=100, ttl_seconds=60
    )
    monkeypatch.setattr("agent_os.api.auth.time.time", lambda: 120)
    principal = Principal.from_mapping(identity.authenticate(f"Bearer {token}", None))
    assert principal.organization_id == "tenant-1"
    assert principal.roles == {"owner"}

    with pytest.raises(ValueError, match="invalid"):
        identity.authenticate(f"Bearer {token[:-1]}x", None)


def test_signed_token_expiration_and_minimum_secret(monkeypatch):
    with pytest.raises(ValueError, match="32 bytes"):
        HMACTokenIdentity("short")
    identity = HMACTokenIdentity("s" * 32)
    token = identity.issue(subject_id="human-1", organization_id="tenant-1", now=100, ttl_seconds=1)
    monkeypatch.setattr("agent_os.api.auth.time.time", lambda: 102)
    with pytest.raises(ValueError, match="expired"):
        identity.authenticate(None, token)


@dataclass
class _SigningKey:
    key: object


class _SigningKeys:
    def __init__(self, key: object) -> None:
        self.key = key
        self.calls = 0

    def get_signing_key_from_jwt(self, token: str) -> _SigningKey:
        self.calls += 1
        return _SigningKey(self.key)


def _oidc_fixture() -> tuple[OIDCTokenIdentity, object, _SigningKeys]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    keys = _SigningKeys(private_key.public_key())
    identity = OIDCTokenIdentity(
        issuer="https://identity.example.test",
        audience="agent-os-api",
        jwks_url="https://identity.example.test/.well-known/jwks.json",
        algorithms=("RS256",),
        leeway_seconds=0,
        signing_keys=keys,
    )
    return identity, private_key, keys


def _oidc_token(private_key: object, **overrides: object) -> str:
    now = int(time.time())
    payload = {
        "sub": "human-1",
        "org_id": "tenant-1",
        "roles": ["owner", "operator"],
        "iss": "https://identity.example.test",
        "aud": "agent-os-api",
        "iat": now - 1,
        "exp": now + 300,
        **overrides,
    }
    return jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": "rotating-key-1"})


def test_oidc_token_verifies_asymmetric_signature_and_normalizes_tenant_identity():
    identity, private_key, keys = _oidc_fixture()
    principal = Principal.from_mapping(
        identity.authenticate(f"Bearer {_oidc_token(private_key)}", None)
    )
    assert principal == Principal("human-1", "tenant-1", frozenset({"owner", "operator"}))
    assert keys.calls == 1


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"iss": "https://attacker.example.test"}, "invalid authentication token"),
        ({"aud": "some-other-api"}, "invalid authentication token"),
        ({"org_id": ""}, "organization claim"),
        ({"roles": "owner"}, "roles claim"),
        (
            {"iat": int(time.time()) - 1, "exp": int(time.time()) + 86_401},
            "lifetime exceeds policy",
        ),
        ({"aud": ["agent-os-api", "other-api"]}, "authorized party"),
    ],
)
def test_oidc_token_fails_closed_for_wrong_or_ambiguous_claims(override, message):
    identity, private_key, _ = _oidc_fixture()
    with pytest.raises(ValueError, match=message):
        identity.authenticate(f"Bearer {_oidc_token(private_key, **override)}", None)


def test_oidc_token_rejects_wrong_signature_cookie_and_symmetric_algorithm():
    identity, private_key, keys = _oidc_fixture()
    wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(ValueError, match="invalid authentication token"):
        identity.authenticate(f"Bearer {_oidc_token(wrong_key)}", None)
    with pytest.raises(ValueError, match="requires a Bearer"):
        identity.authenticate(None, _oidc_token(private_key))
    symmetric = jwt.encode(
        {"sub": "human-1"}, "not-a-production-secret-that-is-long-enough", algorithm="HS256",
        headers={"kid": "bad"},
    )
    with pytest.raises(ValueError, match="unsupported signing key"):
        identity.authenticate(f"Bearer {symmetric}", None)
    assert keys.calls == 1


def test_oidc_configuration_rejects_insecure_urls_and_symmetric_algorithms():
    with pytest.raises(ValueError, match="HTTPS URL"):
        OIDCTokenIdentity(
            issuer="http://identity.example.test",
            audience="agent-os-api",
            jwks_url="https://identity.example.test/jwks",
        )
    with pytest.raises(ValueError, match="asymmetric allowlist"):
        OIDCTokenIdentity(
            issuer="https://identity.example.test",
            audience="agent-os-api",
            jwks_url="https://identity.example.test/jwks",
            algorithms=("HS256",),
        )
