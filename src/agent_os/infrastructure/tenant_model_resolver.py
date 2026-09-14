"""Resolve tenant model policy and inject credentials only inside the worker."""

from __future__ import annotations

from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider

from agent_os.application.ports import TenantModelStore
from agent_os.infrastructure.http_connector_tools import ConnectorSecretResolver
from agent_os.infrastructure.pydantic_agents import ModelSelection


class TenantModelResolver:
    """Select a platform model or construct a tenant-keyed provider client."""

    def __init__(
        self,
        settings: TenantModelStore,
        secrets: ConnectorSecretResolver,
        *,
        fallback_model: str,
    ) -> None:
        if not fallback_model.strip():
            raise ValueError("tenant model resolver requires an explicit fallback model")
        self._settings = settings
        self._secrets = secrets
        self._fallback = fallback_model.strip()

    def __call__(self, tenant_id: str) -> ModelSelection:
        setting = self._settings.get_model_setting(tenant_id)
        if setting is None:
            return ModelSelection(self._fallback, self._fallback)
        provider = str(setting["provider"])
        model_name = str(setting["model_name"])
        canonical = f"{provider}:{model_name}"
        credential_ref = setting.get("credential_ref")
        if not credential_ref:
            return ModelSelection(canonical, canonical)
        api_key = self._secrets.resolve(tenant_id, str(credential_ref))
        if provider == "openai":
            model = OpenAIResponsesModel(model_name, provider=OpenAIProvider(api_key=api_key))
        elif provider == "anthropic":
            model = AnthropicModel(model_name, provider=AnthropicProvider(api_key=api_key))
        elif provider == "google":
            model = GoogleModel(model_name, provider=GoogleProvider(api_key=api_key))
        else:  # The store validates this, but fail closed if another adapter violates its port.
            raise ValueError("tenant model provider is unsupported")
        return ModelSelection(model, canonical)
