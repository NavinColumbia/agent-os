"""Tenant-fenced static preview deployment with opaque public capabilities."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import re
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

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
    update,
)
from sqlalchemy.exc import IntegrityError

from agent_os.application.command_worker import FatalCommandError
from agent_os.application.ports import ArtifactStore, PreviewDeploymentStore
from agent_os.infrastructure.dbos_lifecycle import sqlalchemy_url


preview_metadata = MetaData()
_PUBLIC_ID = re.compile(r"^[A-Za-z0-9_-]{43}$")
_HTML_MEDIA_TYPES = {"text/html", "text/html; charset=utf-8"}
_SOURCE_BUNDLE_MEDIA_TYPE = "application/vnd.agent-os.source-bundle+json"
_MAX_PREVIEW_BYTES = 256 * 1024


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        return None

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
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("revoked_at", DateTime(timezone=True)),
    Column("revocation_key", String(256)),
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


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class SQLStaticPreviewDeployer(PreviewDeploymentStore):
    """Publish a bounded HTML artifact at an unguessable, revocable URL."""

    def __init__(
        self,
        database_url: str,
        artifact_store: ArtifactStore,
        *,
        public_base_url: str,
        capability_secret: str,
        ttl_seconds: int = 7 * 24 * 60 * 60,
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
        if not 60 <= ttl_seconds <= 30 * 24 * 60 * 60:
            raise ValueError("preview deployment TTL must be between 60 seconds and 30 days")
        self._engine = create_engine(sqlalchemy_url(database_url), pool_pre_ping=True)
        self._artifacts = artifact_store
        self._public_base_url = public_base_url.rstrip("/")
        self._public_base = urlparse(self._public_base_url)
        self._secret = capability_secret.encode()
        self._ttl = timedelta(seconds=ttl_seconds)
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

    def _html_artifact(
        self, organization_id: str, artifact_id: str, idempotency_key: str,
    ) -> tuple[str, bytes, str | None]:
        """Resolve exact preview HTML without asking a model to copy bytes."""

        artifact = self._artifacts.describe(organization_id, artifact_id)
        content = self._artifacts.get(organization_id, artifact_id)
        if artifact is None or content is None:
            raise FatalCommandError("preview artifact does not exist in this tenant")
        media_type = str(artifact.get("media_type") or "").lower()
        if media_type in _HTML_MEDIA_TYPES:
            return artifact_id, content, None
        if media_type != _SOURCE_BUNDLE_MEDIA_TYPE:
            raise FatalCommandError(
                "static preview deployment requires text/html or a source bundle with index.html"
            )
        try:
            bundle = json.loads(content)
            files = bundle.get("files") if isinstance(bundle, Mapping) else None
            specification = files.get("index.html") if isinstance(files, Mapping) else None
            if (
                not isinstance(bundle, Mapping)
                or bundle.get("format") != "agent-os.source-bundle.v1"
                or not isinstance(specification, Mapping)
            ):
                raise ValueError("missing canonical index.html")
            encoding = specification.get("encoding")
            raw_content = specification.get("content")
            if encoding == "utf-8" and isinstance(raw_content, str):
                html = raw_content.encode("utf-8")
            elif encoding == "base64" and isinstance(raw_content, str):
                html = base64.b64decode(raw_content, validate=True)
            else:
                raise ValueError("unsupported index.html encoding")
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise FatalCommandError(
                "preview source bundle has no valid canonical index.html"
            ) from exc
        try:
            derived_artifact_id = self._artifacts.put(
                organization_id=organization_id,
                content=html,
                media_type="text/html; charset=utf-8",
                idempotency_key=(
                    f"preview-index:v1:{idempotency_key}:{artifact_id}"
                ),
            )
        except ValueError as exc:
            raise FatalCommandError("preview index artifact could not be persisted") from exc
        return derived_artifact_id, html, artifact_id

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
        artifact_id, content, source_artifact_id = self._html_artifact(
            organization_id, artifact_id, idempotency_key,
        )
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
        created_at = _utc(created_at)
        expires_at = created_at + self._ttl
        receipt = {
            "format": "agent-os.static-preview-deployment.v1",
            "tenant_id": organization_id,
            "deployment_id": deployment_id,
            "artifact_id": artifact_id,
            **({"source_artifact_id": source_artifact_id} if source_artifact_id else {}),
            "public_url": public_url,
            "expires_at": expires_at.isoformat(),
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
            "revoked_at": None,
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
                    expires_at=expires_at,
                    revoked_at=None,
                    revocation_key=None,
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
                preview_deployments.c.expires_at > _utc(self._clock()),
            ))).scalar_one_or_none()
        return None if raw is None else dict(raw)

    def verify_fetch(
        self,
        *,
        organization_id: str,
        public_url: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        """Fetch one issued preview URL without granting generic workflow networking."""

        organization_id = organization_id.strip()
        public_url = public_url.strip()
        idempotency_key = idempotency_key.strip()
        if (
            not organization_id
            or not public_url
            or not idempotency_key
            or len(idempotency_key) > 200
        ):
            raise FatalCommandError("preview fetch identity is missing or unbounded")
        parsed = urlparse(public_url)
        base = self._public_base
        if (
            parsed.scheme != base.scheme
            or parsed.netloc != base.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise FatalCommandError("preview fetch URL is outside the configured preview origin")
        expected_prefix = f"{base.path.rstrip('/')}/v2/public/previews/"
        if not parsed.path.startswith(expected_prefix):
            raise FatalCommandError("preview fetch URL is outside the preview capability path")
        suffix = parsed.path[len(expected_prefix):].split("/")
        if len(suffix) != 2 or any(not item for item in suffix):
            raise FatalCommandError("preview fetch URL has an invalid capability path")
        tenant_slug, public_id = suffix
        record = self.resolve_public(tenant_slug, public_id)
        if record is None or record.get("tenant_id") != organization_id:
            raise FatalCommandError("preview fetch URL is not active for this tenant")
        artifact_id = str(record.get("artifact_id") or "")
        expected = self._artifacts.get(organization_id, artifact_id)
        if expected is None:
            raise FatalCommandError("preview fetch source artifact is unavailable")

        status_code: int | None = None
        content_type: str | None = None
        body = b""
        error: str | None = None
        try:
            opener = build_opener(_RejectRedirects())
            request = Request(
                public_url,
                headers={
                    "Accept": "text/html",
                    "User-Agent": "agent-os-preview-verifier/1",
                },
            )
            with opener.open(request, timeout=10) as response:
                status_code = int(response.status)
                content_type = response.headers.get_content_type()
                if response.geturl() != public_url:
                    raise OSError("preview fetch changed URL")
                body = response.read(_MAX_PREVIEW_BYTES + 1)
        except HTTPError as exc:
            status_code = int(exc.code)
            error = f"HTTP {exc.code}"
        except (URLError, TimeoutError, OSError) as exc:
            error = f"{type(exc).__name__}: {str(exc)[:300]}"

        actual_digest = hashlib.sha256(body).hexdigest() if body else None
        expected_digest = hashlib.sha256(expected).hexdigest()
        verified = bool(
            error is None
            and status_code == 200
            and content_type == "text/html"
            and len(body) <= _MAX_PREVIEW_BYTES
            and body == expected
        )
        verification = {
            "format": "agent-os.static-preview-fetch.v1",
            "tenant_id": organization_id,
            "deployment_id": record.get("deployment_id"),
            "artifact_id": artifact_id,
            "public_url": public_url,
            "status_code": status_code,
            "content_type": content_type,
            "content_bytes": len(body),
            "content_sha256": actual_digest,
            "expected_sha256": expected_digest,
            "digest_matches": body == expected,
            "redirected": False,
            "verified": verified,
            "error": error,
        }
        verification_artifact_id = self._artifacts.put(
            organization_id=organization_id,
            content=json.dumps(
                verification, allow_nan=False, separators=(",", ":"), sort_keys=True,
            ).encode(),
            media_type="application/json",
            idempotency_key=f"preview-fetch-verification:{idempotency_key}",
        )
        return {
            **verification,
            "verification_artifact_id": verification_artifact_id,
        }

    def list_previews(
        self, organization_id: str, *, limit: int = 100,
    ) -> tuple[Mapping[str, Any], ...]:
        if not 1 <= limit <= 500:
            raise ValueError("preview list limit must be between 1 and 500")
        now = _utc(self._clock())
        with self._tenant_connection(organization_id) as connection:
            rows = connection.execute(select(
                preview_deployments.c.record,
                preview_deployments.c.active,
                preview_deployments.c.expires_at,
                preview_deployments.c.revoked_at,
            ).where(
                preview_deployments.c.tenant_id == organization_id,
            ).order_by(
                preview_deployments.c.created_at.desc(),
                preview_deployments.c.deployment_id.desc(),
            ).limit(limit)).mappings().all()
        results = []
        for row in rows:
            record = dict(row["record"])
            active = bool(row["active"])
            expires_at = _utc(row["expires_at"])
            revoked_at = row["revoked_at"]
            record.update({
                "active": active,
                "expires_at": expires_at.isoformat(),
                "revoked_at": None if revoked_at is None else _utc(revoked_at).isoformat(),
                "status": (
                    "revoked" if not active else "expired" if expires_at <= now else "active"
                ),
            })
            results.append(record)
        return tuple(results)

    def revoke(
        self,
        *,
        organization_id: str,
        deployment_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any] | None:
        organization_id = organization_id.strip()
        deployment_id = deployment_id.strip()
        idempotency_key = idempotency_key.strip()
        if (
            not organization_id
            or not deployment_id
            or not idempotency_key
            or len(deployment_id) > 96
            or len(idempotency_key) > 200
        ):
            raise ValueError("preview revocation identity is missing or unbounded")
        key = and_(
            preview_deployments.c.tenant_id == organization_id,
            preview_deployments.c.deployment_id == deployment_id,
        )
        with self._tenant_connection(organization_id) as connection:
            row = connection.execute(select(
                preview_deployments.c.record,
                preview_deployments.c.active,
                preview_deployments.c.revoked_at,
            ).where(key).with_for_update()).mappings().one_or_none()
            if row is None:
                return None
            if not row["active"]:
                record = dict(row["record"])
                record.update({
                    "active": False,
                    "revoked_at": (
                        None if row["revoked_at"] is None
                        else _utc(row["revoked_at"]).isoformat()
                    ),
                    "status": "revoked",
                })
                return record
            revoked_at = _utc(self._clock())
            record = dict(row["record"])
            record.update({
                "active": False,
                "revoked_at": revoked_at.isoformat(),
                "status": "revoked",
            })
            connection.execute(update(preview_deployments).where(key).values(
                active=False,
                revoked_at=revoked_at,
                revocation_key=idempotency_key,
                record=record,
            ))
            return record

    def close(self) -> None:
        self._engine.dispose()
