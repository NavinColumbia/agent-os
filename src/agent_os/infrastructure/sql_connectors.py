"""Tenant-fenced registry for governed external HTTP capabilities."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Mapping
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    JSON,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    create_engine,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError

from agent_os.application.ports import ConnectorRegistry
from agent_os.infrastructure.dbos_lifecycle import sqlalchemy_url
from agent_os.infrastructure.sql_experience_events import (
    SQLExperienceEventLog,
    experience_source_key,
)


connector_metadata = MetaData()
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,62}[a-z0-9]$")
_CREDENTIAL_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,128}$")
_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"})
_SAFE_RESPONSE_BYTES = 8 * 1024 * 1024
_BLOCKED_AUTH_HEADERS = frozenset({
    "host", "content-length", "transfer-encoding", "connection", "upgrade",
    "proxy-authorization", "proxy-authenticate", "forwarded", "x-forwarded-for",
})


connectors = Table(
    "aos_v2_connectors",
    connector_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("connector_id", String(64), primary_key=True),
    Column("definition", JSON, nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("active", Boolean, nullable=False),
    Column("idempotency_key", String(200), nullable=False),
    Column("created_by", String(256), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("disabled_by", String(256), nullable=True),
    Column("disabled_at", DateTime(timezone=True), nullable=True),
    Column("disabled_reason", Text, nullable=True),
    Column("disable_idempotency_key", String(200), nullable=True),
    UniqueConstraint("tenant_id", "idempotency_key"),
)


class HTTPConnectorDefinition(BaseModel):
    """An owner-approved capability envelope, never a container for secret bytes."""

    model_config = ConfigDict(extra="forbid")

    connector_id: str = Field(min_length=2, max_length=64)
    display_name: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1, max_length=2_000)
    allowed_path_prefixes: list[str] = Field(min_length=1, max_length=32)
    allowed_methods: list[str] = Field(default_factory=lambda: ["GET"], max_length=6)
    auth_kind: str = Field(default="none", pattern=r"^(none|bearer|header)$")
    credential_ref: str | None = Field(default=None, max_length=128)
    auth_header: str | None = Field(default=None, max_length=128)
    idempotency_header: str | None = Field(default=None, max_length=128)
    timeout_seconds: int = Field(default=30, ge=1, le=120)
    max_response_bytes: int = Field(default=2 * 1024 * 1024, ge=1, le=_SAFE_RESPONSE_BYTES)

    @model_validator(mode="after")
    def secure_capability(self) -> "HTTPConnectorDefinition":
        if not _IDENTIFIER.fullmatch(self.connector_id):
            raise ValueError("connector_id must be a lowercase slug")
        parsed = urlparse(self.base_url)
        if (
            parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"} or parsed.port not in {None, 443}
        ):
            raise ValueError("connector base_url must be a credential-free HTTPS origin")
        normalized_paths: list[str] = []
        for raw in self.allowed_path_prefixes:
            path = raw.strip()
            if (
                not path.startswith("/") or ".." in path.split("/")
                or "?" in path or "#" in path or not path
            ):
                raise ValueError("connector path prefixes must be absolute URL paths")
            normalized_paths.append(path)
        if len(set(normalized_paths)) != len(normalized_paths):
            raise ValueError("connector path prefixes must be unique")
        methods = [item.strip().upper() for item in self.allowed_methods]
        if not methods or len(set(methods)) != len(methods) or set(methods) - _METHODS:
            raise ValueError("connector methods are missing, duplicated, or unsupported")
        credential_ref = None if self.credential_ref is None else self.credential_ref.strip()
        if self.auth_kind == "none":
            if credential_ref or self.auth_header:
                raise ValueError("unauthenticated connectors cannot name credential settings")
        elif credential_ref is None or not _CREDENTIAL_REF.fullmatch(credential_ref):
            raise ValueError("authenticated connectors require a bounded credential_ref")
        auth_header = None if self.auth_header is None else self.auth_header.strip()
        if self.auth_kind == "header" and (
            not auth_header or not _HEADER_NAME.fullmatch(auth_header)
            or auth_header.lower() in _BLOCKED_AUTH_HEADERS
        ):
            raise ValueError("header authentication requires a safe header name")
        if self.auth_kind != "header" and auth_header:
            raise ValueError("auth_header is valid only for header authentication")
        idempotency_header = (
            None if self.idempotency_header is None else self.idempotency_header.strip()
        )
        if set(methods) - {"GET", "HEAD"} and (
            not idempotency_header
            or not _HEADER_NAME.fullmatch(idempotency_header)
            or idempotency_header.lower() in _BLOCKED_AUTH_HEADERS
            or idempotency_header.lower() == (auth_header or "").lower()
        ):
            raise ValueError("write-capable connectors require a safe idempotency header")
        self.allowed_path_prefixes = normalized_paths
        self.allowed_methods = methods
        self.credential_ref = credential_ref
        self.auth_header = auth_header
        self.idempotency_header = idempotency_header
        self.base_url = self.base_url.rstrip("/")
        return self


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fingerprint(raw: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        raw, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


class SQLConnectorRegistry(ConnectorRegistry):
    def __init__(self, database_url: str, *, create_schema: bool = False) -> None:
        if not database_url.strip():
            raise ValueError("database_url is required")
        self._engine = create_engine(sqlalchemy_url(database_url), pool_pre_ping=True)
        self._experience_events = SQLExperienceEventLog(self._tenant_connection)
        if create_schema:
            connector_metadata.create_all(self._engine)
            self._experience_events.create_schema(self._engine)

    @contextmanager
    def _tenant_connection(self, tenant_id: str):
        if not tenant_id.strip():
            raise ValueError("tenant_id is required")
        with self._engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(text("SET LOCAL ROLE agentos_app"))
                connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": tenant_id},
                )
            yield connection

    def register_connector(
        self,
        *,
        tenant_id: str,
        definition: Mapping[str, Any],
        actor_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        if not actor_id.strip() or not 8 <= len(idempotency_key.strip()) <= 200:
            raise ValueError("connector actor and bounded idempotency key are required")
        model = HTTPConnectorDefinition.model_validate(definition)
        record = model.model_dump(mode="json")
        fingerprint = _fingerprint(record)
        now = _now()
        values = {
            "tenant_id": tenant_id,
            "connector_id": model.connector_id,
            "definition": record,
            "fingerprint": fingerprint,
            "active": True,
            "idempotency_key": idempotency_key.strip(),
            "created_by": actor_id,
            "created_at": now,
        }
        try:
            with self._tenant_connection(tenant_id) as connection:
                prior = connection.execute(select(connectors).where(and_(
                    connectors.c.tenant_id == tenant_id,
                    connectors.c.idempotency_key == idempotency_key.strip(),
                ))).mappings().one_or_none()
                if prior is not None:
                    if prior["fingerprint"] != fingerprint:
                        raise ValueError(
                            "connector idempotency key was reused with a different definition"
                        )
                    return self._record(prior, duplicate=True)
                by_id = connection.execute(select(connectors.c.connector_id).where(and_(
                    connectors.c.tenant_id == tenant_id,
                    connectors.c.connector_id == model.connector_id,
                ))).scalar_one_or_none()
                if by_id is not None:
                    raise ValueError("connector_id already exists and definitions are immutable")
                existing_count = len(connection.execute(select(
                    connectors.c.connector_id,
                ).where(connectors.c.tenant_id == tenant_id).limit(256)).all())
                if existing_count >= 256:
                    raise ValueError("tenant connector limit reached")
                connection.execute(insert(connectors).values(**values))
                self._experience_events.append(
                    connection,
                    tenant_id=tenant_id,
                    source_key=experience_source_key(
                        "integration.connector.registered", model.connector_id,
                    ),
                    resource_type="connector",
                    resource_id=model.connector_id,
                    projection_revision=1,
                    kind="integration.connector.registered",
                    audience_ids=("tenant:members",),
                    safe_summary="External connector registered.",
                    occurred_at=now,
                )
        except IntegrityError as exc:
            with self._tenant_connection(tenant_id) as connection:
                prior = connection.execute(select(connectors).where(and_(
                    connectors.c.tenant_id == tenant_id,
                    connectors.c.idempotency_key == idempotency_key.strip(),
                ))).mappings().one_or_none()
            if prior is None or prior["fingerprint"] != fingerprint:
                raise ValueError("connector registration conflicted with another writer") from exc
            return self._record(prior, duplicate=True)
        return {
            **record,
            "active": True,
            "created_by": actor_id,
            "created_at": now.isoformat(),
            "duplicate": False,
        }

    @staticmethod
    def _record(row: Mapping[str, Any], *, duplicate: bool = False) -> Mapping[str, Any]:
        record = dict(row["definition"])
        record.update({
            "active": bool(row["active"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"].isoformat(),
            "disabled_by": row["disabled_by"],
            "disabled_at": (
                None if row["disabled_at"] is None else row["disabled_at"].isoformat()
            ),
            "disabled_reason": row["disabled_reason"],
            "duplicate": duplicate,
        })
        return record

    def get_connector(
        self, tenant_id: str, connector_id: str,
    ) -> Mapping[str, Any] | None:
        with self._tenant_connection(tenant_id) as connection:
            row = connection.execute(select(connectors).where(and_(
                connectors.c.tenant_id == tenant_id,
                connectors.c.connector_id == connector_id,
            ))).mappings().one_or_none()
        return None if row is None else self._record(row)

    def list_connectors(self, tenant_id: str) -> tuple[Mapping[str, Any], ...]:
        with self._tenant_connection(tenant_id) as connection:
            rows = connection.execute(select(connectors).where(
                connectors.c.tenant_id == tenant_id,
            ).order_by(connectors.c.connector_id)).mappings().all()
        return tuple(self._record(row) for row in rows)

    def disable_connector(
        self,
        *,
        tenant_id: str,
        connector_id: str,
        actor_id: str,
        reason: str,
        idempotency_key: str,
    ) -> Mapping[str, Any] | None:
        reason = reason.strip()
        idempotency_key = idempotency_key.strip()
        if not actor_id.strip() or not reason or not 8 <= len(idempotency_key) <= 200:
            raise ValueError("connector disable actor, reason, and idempotency key are required")
        key = and_(
            connectors.c.tenant_id == tenant_id,
            connectors.c.connector_id == connector_id,
        )
        with self._tenant_connection(tenant_id) as connection:
            row = connection.execute(select(connectors).where(key).with_for_update()).mappings().one_or_none()
            if row is None:
                return None
            if not row["active"]:
                if row["disable_idempotency_key"] != idempotency_key:
                    raise ValueError("connector is already disabled by another decision")
                return self._record(row, duplicate=True)
            now = _now()
            connection.execute(update(connectors).where(key).values(
                active=False,
                disabled_by=actor_id,
                disabled_at=now,
                disabled_reason=reason,
                disable_idempotency_key=idempotency_key,
            ))
            self._experience_events.append(
                connection,
                tenant_id=tenant_id,
                source_key=experience_source_key(
                    "integration.connector.disabled", connector_id, idempotency_key,
                ),
                resource_type="connector",
                resource_id=connector_id,
                projection_revision=2,
                kind="integration.connector.disabled",
                audience_ids=("tenant:members",),
                safe_summary="External connector disabled.",
                occurred_at=now,
            )
            updated = dict(row)
            updated.update({
                "active": False,
                "disabled_by": actor_id,
                "disabled_at": now,
                "disabled_reason": reason,
                "disable_idempotency_key": idempotency_key,
            })
        return self._record(updated)

    def close(self) -> None:
        self._engine.dispose()
