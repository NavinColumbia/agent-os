from __future__ import annotations

import pytest

from agent_os.entrypoints.server import ServerSettings


def test_development_defaults_to_scale_zero_local_storage(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in (
        "AOS_ENVIRONMENT", "AOS_V2_SYSTEM_DATABASE_URL", "AOS_V2_APPLICATION_DATABASE_URL",
        "DATABASE_URL", "AOS_V2_AUTH_SECRET", "AOS_V2_CREATE_SCHEMA",
        "AOS_V2_PUBLIC_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = ServerSettings.from_env()
    assert settings.environment == "development"
    assert settings.system_database_url.startswith("sqlite:///")
    assert settings.create_schema is True
    assert len(settings.auth_secret) >= 32
    assert settings.public_base_url == "http://127.0.0.1:8080"


def test_production_fails_closed_without_postgres_migrations_and_strong_secret(monkeypatch):
    monkeypatch.setenv("AOS_ENVIRONMENT", "production")
    monkeypatch.delenv("AOS_V2_SYSTEM_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AOS_V2_AUTH_SECRET", raising=False)
    with pytest.raises(ValueError, match="system database must be PostgreSQL"):
        ServerSettings.from_env()

    monkeypatch.setenv("AOS_V2_SYSTEM_DATABASE_URL", "postgresql://db/system")
    monkeypatch.setenv("DATABASE_URL", "postgresql://db/application")
    monkeypatch.setenv("AOS_V2_AUTH_SECRET", "strong-secret" * 4)
    monkeypatch.setenv("AOS_V2_CREATE_SCHEMA", "1")
    with pytest.raises(ValueError, match="explicit migrations"):
        ServerSettings.from_env()

    monkeypatch.setenv("AOS_V2_CREATE_SCHEMA", "0")
    monkeypatch.setenv("AOS_V2_PUBLIC_BASE_URL", "http://agent-os.example.test")
    with pytest.raises(ValueError, match="must use HTTPS"):
        ServerSettings.from_env()

    monkeypatch.setenv("AOS_V2_PUBLIC_BASE_URL", "https://agent-os.example.test/")
    settings = ServerSettings.from_env()
    assert settings.public_base_url == "https://agent-os.example.test"
