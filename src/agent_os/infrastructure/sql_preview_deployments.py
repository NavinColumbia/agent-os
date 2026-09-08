"""Tenant-fenced static preview deployment with opaque public capabilities."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Index,
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
)
from sqlalchemy.exc import IntegrityError

from agent_os.application.command_worker import FatalCommandError
from agent_os.application.ports import ArtifactStore, PreviewDeploymentStore
from agent_os.infrastructure.dbos_lifecycle import sqlalchemy_url


preview_metadata = MetaData()
_PUBLIC_ID = re.compile(r"^[A-Za-z0-9_-]{43}$")
_HTML_MEDIA_TYPES = {"text/html", "text/html; charset=utf-8"}
_MAX_PREVIEW_BYTES = 256 * 1024

preview_deployments = Table(
    "aos_v2_preview_deployments",
    preview_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("deployment_id", String(96), primary_key=True),
    Column("public_id", String(64), nullable=False),
    Column("artifact_id", String(96), nullable=False),
    Column("receipt_artifact_id", String(96), nullable=False),
    Column("idempotency_key", String(256), nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("public_url", Text, nullable=False),
    Column("active", Boolean, nullable=False, default=True),
    Column("record", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("tenant_id", "public_id", name="aos_v2_preview_public_key"),
    UniqueConstraint(
        "tenant_id", "idempotency_key", name="aos_v2_preview_idempotency_key",
    ),
)

Index(
    "aos_v2_preview_deployments_tenant_created_idx",
    preview_deployments.c.tenant_id,
    preview_deployments.c.created_at.desc(),
    preview_deployments.c.deployment_id.desc(),
)


def _tenant_slug(tenant_id: str) -> str:
    return base64.urlsafe_b64encode(tenant_id.encode()).decode().rstrip("=")


def _tenant_from_slug(value: str) -> str | None:
    if not value or len(value) > 256 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return None
    try:
        padding = "=" * (-len(value) % 4)
        tenant_id = base64.b64decode(value + padding, altchars=b"-_", validate=True).decode()
    except (UnicodeDecodeError, ValueError):
        return None
    if not tenant_id or _tenant_slug(tenant_id) != value:
        return None
    return tenant_id


class SQLStaticPreviewDeployer(PreviewDeploymentStore):
    """Publish a bounded HTML artifact at an unguessable, revocable URL."""

    def __init__(
        self,
        database_url: str,
        artifact_store: ArtifactStore,
        *,
        public_base_url: str,
        capability_secret: str,
        create_schema: bool = False,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        parsed = urlparse(public_base_url)
        if (
            not database_url.strip()
            or parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("preview deployment requires a valid HTTP(S) public base URL")
        if len(capability_secret.encode()) < 32:
            raise ValueError("preview deployment capability secret must be at least 32 bytes")
        self._engine = create_engine(sqlalchemy_url(database_url), pool_pre_ping=True)
        self._artifacts = artifact_store
        self._public_base_url = public_base_url.rstrip("/")
        self._secret = capability_secret.encode()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if create_schema:
            preview_metadata.create_all(self._engine)

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

    def _public_id(self, tenant_id: str, idempotency_key: str) -> str:
        material = f"agent-os:static-preview:v1:{tenant_id}:{idempotency_key}".encode()
        digest = hmac.new(self._secret, material, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")

    def deploy(
        self,
        *,
        organization_id: str,
        artifact_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        organization_id = organization_id.strip()
        artifact_id = artifact_id.strip()
        idempotency_key = idempotency_key.strip()
        if (
            not organization_id
            or not artifact_id
            or not idempotency_key
            or len(idempotency_key) > 200
        ):
            raise FatalCommandError("preview deployment identity is missing or unbounded")
        artifact = self._artifacts.describe(organization_id, artifact_id)
        content = self._artifacts.get(organization_id, artifact_id)
        if artifact is None or content is None:
            raise FatalCommandError("preview artifact does not exist in this tenant")
        if str(artifact.get("media_type") or "").lower() not in _HTML_MEDIA_TYPES:
            raise FatalCommandError("static preview deployment requires a text/html artifact")
        if len(content) > _MAX_PREVIEW_BYTES:
            raise FatalCommandError("static preview artifact exceeds 256 KiB")
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FatalCommandError("static preview artifact must be valid UTF-8") from exc

        fingerprint = hashlib.sha256(f"static-preview:v1:{artifact_id}".encode()).hexdigest()
        public_id = self._public_id(organization_id, idempotency_key)
        deployment_id = "deployment-" + hashlib.sha256(
            f"{organization_id}:{idempotency_key}:{fingerprint}".encode()
        ).hexdigest()
        public_url = (
            f"{self._public_base_url}/v2/public/previews/"
            f"{_tenant_slug(organization_id)}/{public_id}"
        )
        request_key = and_(
            preview_deployments.c.tenant_id == organization_id,
            preview_deployments.c.idempotency_key == idempotency_key,
        )
        with self._tenant_connection(organization_id) as connection:
            prior = connection.execute(select(
                preview_deployments.c.fingerprint,
                preview_deployments.c.record,
            ).where(request_key)).mappings().one_or_none()
        if prior is not None:
            if prior["fingerprint"] != fingerprint:
                raise FatalCommandError(
                    "preview idempotency key was reused with a different artifact"
                )
            return dict(prior["record"])

        created_at = self._clock()
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        receipt = {
            "format": "agent-os.static-preview-deployment.v1",
            "tenant_id": organization_id,
            "deployment_id": deployment_id,
            "artifact_id": artifact_id,
            "public_url": public_url,
        }
        try:
            receipt_artifact_id = self._artifacts.put(
                organization_id=organization_id,
                content=json.dumps(
                    receipt, allow_nan=False, separators=(",", ":"), sort_keys=True,
                ).encode(),
                media_type="application/json",
                idempotency_key=f"preview-receipt:{idempotency_key}",
            )
        except ValueError as exc:
            # Concurrent replicas may race with different input under the same
            # action ID. Convert the artifact-store conflict into a durable
            # graph-node rejection instead of leaving its token RUNNING.
            if "idempotency key was reused" in str(exc):
                raise FatalCommandError(
                    "preview idempotency key was reused with a different artifact"
                ) from exc
            raise
        record = {
            **receipt,
            "public_id": public_id,
            "receipt_artifact_id": receipt_artifact_id,
            "active": True,
            "created_at": created_at.isoformat(),
        }
        try:
            with self._tenant_connection(organization_id) as connection:
                connection.execute(insert(preview_deployments).values(
                    tenant_id=organization_id,
                    deployment_id=deployment_id,
                    public_id=public_id,
                    artifact_id=artifact_id,
                    receipt_artifact_id=receipt_artifact_id,
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                    public_url=public_url,
                    active=True,
                    record=record,
                    created_at=created_at,
                ))
        except IntegrityError as exc:
            with self._tenant_connection(organization_id) as connection:
                prior = connection.execute(select(
                    preview_deployments.c.fingerprint,
                    preview_deployments.c.record,
                ).where(request_key)).mappings().one_or_none()
            if prior is None:
                raise
            if prior["fingerprint"] != fingerprint:
                raise FatalCommandError(
                    "preview idempotency key was reused with a different artifact"
                ) from exc
            return dict(prior["record"])
        return record

    def resolve_public(
        self, tenant_slug: str, public_id: str,
    ) -> Mapping[str, Any] | None:
        tenant_id = _tenant_from_slug(tenant_slug)
        if tenant_id is None or not _PUBLIC_ID.fullmatch(public_id):
            return None
        with self._tenant_connection(tenant_id) as connection:
            raw = connection.execute(select(
                preview_deployments.c.record,
            ).where(and_(
                preview_deployments.c.tenant_id == tenant_id,
                preview_deployments.c.public_id == public_id,
                preview_deployments.c.active.is_(True),
            ))).scalar_one_or_none()
        return None if raw is None else dict(raw)

    def close(self) -> None:
        self._engine.dispose()
