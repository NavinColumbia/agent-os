from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai.models.test import TestModel
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIResponsesModel

from agent_os.application.ports import TenantModelStore
from agent_os.infrastructure.sql_tenant_models import SQLTenantModelStore
from agent_os.infrastructure.tenant_model_resolver import TenantModelResolver


def store(tmp_path: Path) -> SQLTenantModelStore:
    return SQLTenantModelStore(
        f"sqlite:///{tmp_path / 'tenant-models.sqlite3'}", create_schema=True,
    )


def test_tenant_model_policy_is_versioned_idempotent_and_isolated(tmp_path: Path):
    settings = store(tmp_path)
    assert isinstance(settings, TenantModelStore)
    try:
        first = settings.set_model_setting(
            tenant_id="org-a", provider="openai", model_name="gpt-5.1",
            credential_ref=None, actor_id="owner-a", idempotency_key="model-setting-one",
        )
        replay = settings.set_model_setting(
            tenant_id="org-a", provider="openai", model_name="gpt-5.1",
            credential_ref=None, actor_id="owner-a", idempotency_key="model-setting-one",
        )
        second = settings.set_model_setting(
            tenant_id="org-a", provider="anthropic", model_name="claude-opus-4-1",
            credential_ref="company-anthropic", actor_id="owner-a",
            idempotency_key="model-setting-two",
        )
        assert first["version"] == 1 and first["credential_source"] == "platform"
        assert replay["version"] == 1 and replay["duplicate"] is True
        assert second["version"] == 2 and second["credential_source"] == "tenant"
        assert settings.get_model_setting("org-a")["model_name"] == "claude-opus-4-1"
        assert settings.get_model_setting("org-b") is None

        with pytest.raises(ValueError, match="different terms"):
            settings.set_model_setting(
                tenant_id="org-a", provider="google", model_name="gemini-2.5-pro",
                credential_ref=None, actor_id="owner-a", idempotency_key="model-setting-two",
            )
        with pytest.raises(ValueError, match="provider"):
            settings.set_model_setting(
                tenant_id="org-a", provider="unknown", model_name="model",
                credential_ref=None, actor_id="owner-a", idempotency_key="model-setting-bad",
            )
    finally:
        settings.close()


class SecretResolver:
    def __init__(self) -> None:
        self.calls = []

    def resolve(self, tenant_id: str, credential_ref: str) -> str:
        self.calls.append((tenant_id, credential_ref))
        return "test-provider-api-key"


@pytest.mark.parametrize(
    ("provider", "model_name", "model_type"),
    [
        ("openai", "gpt-5", OpenAIResponsesModel),
        ("anthropic", "claude-opus-4-1", AnthropicModel),
        ("google", "gemini-2.5-pro", GoogleModel),
    ],
)
def test_tenant_model_resolver_injects_byok_only_inside_provider_client(
    tmp_path: Path, provider: str, model_name: str, model_type,
):
    settings = store(tmp_path)
    secrets = SecretResolver()
    try:
        settings.set_model_setting(
            tenant_id="org-a", provider=provider, model_name=model_name,
            credential_ref="private-model-key", actor_id="owner-a",
            idempotency_key=f"setting-{provider}",
        )
        selected = TenantModelResolver(
            settings, secrets, fallback_model="openai:gpt-5-mini",
        )("org-a")
        assert isinstance(selected.model, model_type)
        assert selected.name == f"{provider}:{model_name}"
        assert secrets.calls == [("org-a", "private-model-key")]
        assert "test-provider-api-key" not in repr(settings.get_model_setting("org-a"))
    finally:
        settings.close()


def test_tenant_model_resolver_uses_platform_fallback_without_secret_access(tmp_path: Path):
    settings = store(tmp_path)
    secrets = SecretResolver()
    try:
        selected = TenantModelResolver(
            settings, secrets, fallback_model="google:gemini-2.5-flash",
        )("org-a")
        assert selected.model == "google:gemini-2.5-flash"
        assert selected.name == "google:gemini-2.5-flash"
        assert secrets.calls == []
    finally:
        settings.close()


def test_tenant_model_resolver_preserves_a_local_model_adapter_and_auditable_name(tmp_path: Path):
    settings = store(tmp_path)
    secrets = SecretResolver()
    fallback = TestModel()
    try:
        selected = TenantModelResolver(
            settings,
            secrets,
            fallback_model=fallback,
            fallback_name="codex-cli:default",
        )("org-a")
        assert selected.model is fallback
        assert selected.name == "codex-cli:default"
        assert secrets.calls == []
    finally:
        settings.close()


def test_tenant_model_migration_is_secret_free_and_forces_rls():
    migration = (
        Path(__file__).resolve().parents[1]
        / "postgres/initdb/99zzzz-tenant-models-v2.sql"
    ).read_text()
    assert migration.count("ENABLE ROW LEVEL SECURITY") == 2
    assert migration.count("FORCE ROW LEVEL SECURITY") == 2
    assert "current_setting('app.tenant_id', true)" in migration
    assert "credential_ref" in migration
    assert "api_key" not in migration.lower()
    assert "GRANT DELETE" not in migration
