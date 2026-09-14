"""Tenant-fenced model selection with immutable, secret-free audit history."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Mapping

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    JSON,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    and_,
    create_engine,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError

from agent_os.application.ports import TenantModelStore
from agent_os.infrastructure.dbos_lifecycle import sqlalchemy_url


model_setting_metadata = MetaData()
_PROVIDERS = frozenset({"openai", "anthropic", "google"})
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}$")
_CREDENTIAL_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


model_settings = Table(
    "aos_v2_model_settings",
    model_setting_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("version", Integer, nullable=False),
    Column("configuration", JSON, nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("updated_by", String(256), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

model_setting_events = Table(
    "aos_v2_model_setting_events",
    model_setting_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("idempotency_key", String(200), nullable=False),
    Column("configuration", JSON, nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("actor_id", String(256), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("tenant_id", "idempotency_key"),
)


def _fingerprint(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


class SQLTenantModelStore(TenantModelStore):
    def __init__(self, database_url: str, *, create_schema: bool = False) -> None:
        if not database_url.strip():
            raise ValueError("database_url is required")
        self._engine = create_engine(sqlalchemy_url(database_url), pool_pre_ping=True)
        if create_schema:
            model_setting_metadata.create_all(self._engine)

    @contextmanager
    def _connection(self, tenant_id: str):
        if not tenant_id.strip() or len(tenant_id) > 128:
            raise ValueError("tenant model setting requires a bounded tenant identity")
        with self._engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(text("SET LOCAL ROLE agentos_app"))
                connection.execute(
                    text("SELECT set_config('app.tenant_id', :value, true)"),
                    {"value": tenant_id},
                )
            yield connection

    @staticmethod
    def _configuration(
        provider: str, model_name: str, credential_ref: str | None,
    ) -> dict[str, Any]:
        provider = provider.strip().lower()
        model_name = model_name.strip()
        credential_ref = None if credential_ref is None else credential_ref.strip()
        if provider not in _PROVIDERS:
            raise ValueError("model provider must be openai, anthropic, or google")
        if not _MODEL_NAME.fullmatch(model_name):
            raise ValueError("model name is invalid or too long")
        if credential_ref is not None and not _CREDENTIAL_REF.fullmatch(credential_ref):
            raise ValueError("model credential_ref is invalid")
        return {
            "provider": provider,
            "model_name": model_name,
            "credential_ref": credential_ref,
            "credential_source": "tenant" if credential_ref else "platform",
        }

    @staticmethod
    def _record(
        configuration: Mapping[str, Any], *, version: int, actor_id: str,
        updated_at: datetime, duplicate: bool = False,
    ) -> Mapping[str, Any]:
        return {
            **dict(configuration),
            "version": version,
            "updated_by": actor_id,
            "updated_at": updated_at.isoformat(),
            "duplicate": duplicate,
        }

    def get_model_setting(self, tenant_id: str) -> Mapping[str, Any] | None:
        with self._connection(tenant_id) as connection:
            row = connection.execute(select(model_settings).where(
                model_settings.c.tenant_id == tenant_id,
            )).mappings().one_or_none()
        if row is None:
            return None
        return self._record(
            row["configuration"], version=row["version"], actor_id=row["updated_by"],
            updated_at=row["updated_at"],
        )

    def set_model_setting(
        self,
        *,
        tenant_id: str,
        provider: str,
        model_name: str,
        credential_ref: str | None,
        actor_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        configuration = self._configuration(provider, model_name, credential_ref)
        fingerprint = _fingerprint(configuration)
        actor_id = actor_id.strip()
        idempotency_key = idempotency_key.strip()
        if not actor_id or len(actor_id) > 255 or not 8 <= len(idempotency_key) <= 200:
            raise ValueError("model setting actor and bounded idempotency key are required")
        now = datetime.now(timezone.utc)
        try:
            with self._connection(tenant_id) as connection:
                replay = connection.execute(select(model_setting_events).where(and_(
                    model_setting_events.c.tenant_id == tenant_id,
                    model_setting_events.c.idempotency_key == idempotency_key,
                ))).mappings().one_or_none()
                if replay is not None:
                    if replay["fingerprint"] != fingerprint:
                        raise ValueError("model setting idempotency key was reused with different terms")
                    return self._record(
                        replay["configuration"], version=replay["version"],
                        actor_id=replay["actor_id"], updated_at=replay["created_at"], duplicate=True,
                    )
                current = connection.execute(select(model_settings).where(
                    model_settings.c.tenant_id == tenant_id,
                ).with_for_update()).mappings().one_or_none()
                version = 1 if current is None else int(current["version"]) + 1
                values = {
                    "tenant_id": tenant_id,
                    "version": version,
                    "configuration": configuration,
                    "fingerprint": fingerprint,
                    "updated_by": actor_id,
                    "updated_at": now,
                }
                if current is None:
                    connection.execute(insert(model_settings).values(**values))
                else:
                    connection.execute(update(model_settings).where(and_(
                        model_settings.c.tenant_id == tenant_id,
                        model_settings.c.version == current["version"],
                    )).values(**values))
                connection.execute(insert(model_setting_events).values(
                    tenant_id=tenant_id,
                    version=version,
                    idempotency_key=idempotency_key,
                    configuration=configuration,
                    fingerprint=fingerprint,
                    actor_id=actor_id,
                    created_at=now,
                ))
        except IntegrityError as exc:
            with self._connection(tenant_id) as connection:
                replay = connection.execute(select(model_setting_events).where(and_(
                    model_setting_events.c.tenant_id == tenant_id,
                    model_setting_events.c.idempotency_key == idempotency_key,
                ))).mappings().one_or_none()
            if replay is None or replay["fingerprint"] != fingerprint:
                raise ValueError("model setting conflicted with another writer") from exc
            return self._record(
                replay["configuration"], version=replay["version"], actor_id=replay["actor_id"],
                updated_at=replay["created_at"], duplicate=True,
            )
        return self._record(
            configuration, version=version, actor_id=actor_id, updated_at=now,
        )

    def close(self) -> None:
        self._engine.dispose()
