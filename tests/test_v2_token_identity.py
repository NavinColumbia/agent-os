import pytest

from agent_os.api.auth import HMACTokenIdentity, Principal


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
