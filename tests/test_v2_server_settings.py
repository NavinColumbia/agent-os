from __future__ import annotations

import pytest

from agent_os.entrypoints.server import ServerSettings


def test_development_defaults_to_scale_zero_local_storage(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in (
        "AOS_ENVIRONMENT", "AOS_V2_SYSTEM_DATABASE_URL", "AOS_V2_APPLICATION_DATABASE_URL",
        "DATABASE_URL", "AOS_V2_AUTH_SECRET", "AOS_V2_CREATE_SCHEMA",
        "AOS_V2_PUBLIC_BASE_URL", "AOS_V2_PREVIEW_TTL_SECONDS",
        "AOS_V2_IDENTITY_MODE", "AOS_V2_CAPABILITY_SECRET", "AOS_V2_OIDC_ISSUER",
        "AOS_V2_OIDC_AUDIENCE", "AOS_V2_OIDC_JWKS_URL",
        "AOS_V2_OIDC_AUTHORIZATION_URL", "AOS_V2_OIDC_TOKEN_URL", "AOS_V2_OIDC_CLIENT_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = ServerSettings.from_env()
    assert settings.environment == "development"
    assert settings.system_database_url.startswith("sqlite:///")
    assert settings.create_schema is True
    assert len(settings.auth_secret) >= 32
    assert settings.identity_mode == "hmac"
    assert settings.capability_secret == settings.auth_secret
    assert settings.public_base_url == "http://127.0.0.1:8080"
    assert settings.preview_ttl_seconds == 604800


def test_production_fails_closed_without_postgres_migrations_and_strong_secret(monkeypatch):
    monkeypatch.setenv("AOS_ENVIRONMENT", "production")
    monkeypatch.delenv("AOS_V2_SYSTEM_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AOS_V2_AUTH_SECRET", raising=False)
    monkeypatch.delenv("AOS_V2_IDENTITY_MODE", raising=False)
    monkeypatch.delenv("AOS_V2_CAPABILITY_SECRET", raising=False)
    monkeypatch.delenv("AOS_V2_OIDC_ISSUER", raising=False)
    monkeypatch.delenv("AOS_V2_OIDC_AUDIENCE", raising=False)
    monkeypatch.delenv("AOS_V2_OIDC_JWKS_URL", raising=False)
    monkeypatch.delenv("AOS_V2_OIDC_AUTHORIZATION_URL", raising=False)
    monkeypatch.delenv("AOS_V2_OIDC_TOKEN_URL", raising=False)
    monkeypatch.delenv("AOS_V2_OIDC_CLIENT_ID", raising=False)
    with pytest.raises(ValueError, match="system database must be PostgreSQL"):
        ServerSettings.from_env()

    monkeypatch.setenv("AOS_V2_SYSTEM_DATABASE_URL", "postgresql://db/system")
    monkeypatch.setenv("DATABASE_URL", "postgresql://db/application")
    monkeypatch.setenv("AOS_V2_CAPABILITY_SECRET", "capability-secret" * 4)
    monkeypatch.setenv("AOS_V2_CREATE_SCHEMA", "1")
    with pytest.raises(ValueError, match="explicit migrations"):
        ServerSettings.from_env()

    monkeypatch.setenv("AOS_V2_CREATE_SCHEMA", "0")
    with pytest.raises(ValueError, match="OIDC issuer"):
        ServerSettings.from_env()

    monkeypatch.setenv("AOS_V2_OIDC_ISSUER", "https://identity.example.test")
    monkeypatch.setenv("AOS_V2_OIDC_AUDIENCE", "agent-os-api")
    monkeypatch.setenv("AOS_V2_OIDC_JWKS_URL", "https://identity.example.test/jwks.json")
    monkeypatch.setenv("AOS_V2_OIDC_AUTHORIZATION_URL", "https://identity.example.test/authorize")
    monkeypatch.setenv("AOS_V2_OIDC_TOKEN_URL", "https://identity.example.test/oauth/token")
    monkeypatch.setenv("AOS_V2_OIDC_CLIENT_ID", "agent-os-browser")
    monkeypatch.setenv("AOS_V2_PUBLIC_BASE_URL", "http://agent-os.example.test")
    with pytest.raises(ValueError, match="must use HTTPS"):
        ServerSettings.from_env()

    monkeypatch.setenv("AOS_V2_PUBLIC_BASE_URL", "https://agent-os.example.test/")
    settings = ServerSettings.from_env()
    assert settings.public_base_url == "https://agent-os.example.test"
    assert settings.identity_mode == "oidc"
    assert settings.auth_secret == ""

    monkeypatch.setenv("AOS_V2_IDENTITY_MODE", "hmac")
    with pytest.raises(ValueError, match="production requires"):
        ServerSettings.from_env()
    monkeypatch.setenv("AOS_V2_IDENTITY_MODE", "oidc")

    monkeypatch.setenv("AOS_V2_PREVIEW_TTL_SECONDS", "59")
    with pytest.raises(ValueError, match="between 60 and 2592000"):
        ServerSettings.from_env()
