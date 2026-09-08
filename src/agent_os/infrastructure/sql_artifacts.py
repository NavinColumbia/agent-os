"""PostgreSQL/SQLite bootstrap artifact store with tenant RLS and idempotency."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import re
from typing import Any, Callable, Mapping

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    MetaData,
    String,
    Table,
    and_,
    create_engine,
    insert,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from agent_os.application.ports import ArtifactStore
from agent_os.infrastructure.dbos_lifecycle import sqlalchemy_url


artifact_metadata = MetaData()
_MEDIA_TYPE = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*"
    r"(?:;\s*[a-z0-9][a-z0-9_.-]*=[a-z0-9][a-z0-9_.-]*)*$"
)

artifacts = Table(
    "aos_v2_artifacts",
    artifact_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("artifact_id", String(96), primary_key=True),
    Column("digest", String(64), nullable=False),
    Column("byte_length", BigInteger, nullable=False),
    Column("media_type", String(256), nullable=False),
    Column("content", LargeBinary, nullable=False),
    Column("record", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

artifact_writes = Table(
    "aos_v2_artifact_writes",
    artifact_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("idempotency_key", String(256), primary_key=True),
    Column("artifact_id", String(96), nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "artifact_id"],
        ["aos_v2_artifacts.tenant_id", "aos_v2_artifacts.artifact_id"],
    ),
)

Index(
    "aos_v2_artifacts_tenant_created_idx",
    artifacts.c.tenant_id,
    artifacts.c.created_at.desc(),
    artifacts.c.artifact_id.desc(),
)


def _fingerprint(media_type: str, content: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(media_type.encode("utf-8"))
    digest.update(b"\0")
    digest.update(content)
    return digest.hexdigest()


class SQLArtifactStore(ArtifactStore):
    """Stores small control-plane artifacts until object storage is configured.

    The strict byte cap prevents PostgreSQL from silently becoming a general
    blob store. Large build outputs belong behind the same port in GCS/OCI.
    """

    def __init__(
        self,
        database_url: str,
        *,
        create_schema: bool = False,
        max_content_bytes: int = 2 * 1024 * 1024,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not database_url.strip() or max_content_bytes < 1:
            raise ValueError("database_url and a positive artifact byte limit are required")
        self._engine = create_engine(sqlalchemy_url(database_url), pool_pre_ping=True)
        self._max_content_bytes = max_content_bytes
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if create_schema:
            artifact_metadata.create_all(self._engine)

    @contextmanager
    def _tenant_connection(self, tenant_id: str):
        if not tenant_id.strip():
            raise ValueError("organization_id is required")
        with self._engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(text("SET LOCAL ROLE agentos_app"))
                connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": tenant_id},
                )
            yield connection

    @staticmethod
    def _insert_artifact(connection, values: Mapping[str, Any]) -> None:
        if connection.dialect.name == "postgresql":
            statement = postgresql_insert(artifacts).values(**values).on_conflict_do_nothing(
                index_elements=["tenant_id", "artifact_id"],
            )
        elif connection.dialect.name == "sqlite":
            statement = sqlite_insert(artifacts).values(**values).on_conflict_do_nothing(
                index_elements=["tenant_id", "artifact_id"],
            )
        else:
            statement = insert(artifacts).values(**values)
        connection.execute(statement)

    def put(
        self,
        *,
        organization_id: str,
        content: bytes,
        media_type: str,
        idempotency_key: str,
    ) -> str:
        if not isinstance(content, bytes):
            raise TypeError("artifact content must be bytes")
        media_type = media_type.strip().lower()
        idempotency_key = idempotency_key.strip()
        if not organization_id.strip() or not media_type or not idempotency_key:
            raise ValueError("artifact tenant, media type, and idempotency key are required")
        if not _MEDIA_TYPE.fullmatch(media_type):
            raise ValueError("artifact media type is invalid")
        if len(content) > self._max_content_bytes:
            raise ValueError(
                f"artifact exceeds the {self._max_content_bytes}-byte bootstrap store limit"
            )
        fingerprint = _fingerprint(media_type, content)
        artifact_id = f"artifact-{fingerprint}"
        digest = hashlib.sha256(content).hexdigest()
        created_at = self._clock()
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        record = {
            "artifact_id": artifact_id,
            "tenant_id": organization_id,
            "digest": digest,
            "byte_length": len(content),
            "media_type": media_type,
            "created_at": created_at.isoformat(),
        }
        request_key = and_(
            artifact_writes.c.tenant_id == organization_id,
            artifact_writes.c.idempotency_key == idempotency_key,
        )
        try:
            with self._tenant_connection(organization_id) as connection:
                prior = connection.execute(select(
                    artifact_writes.c.artifact_id,
                    artifact_writes.c.fingerprint,
                ).where(request_key)).mappings().one_or_none()
                if prior is not None:
                    if prior["fingerprint"] != fingerprint:
                        raise ValueError("artifact idempotency key was reused with different content")
                    return str(prior["artifact_id"])
                self._insert_artifact(connection, {
                    "tenant_id": organization_id,
                    "artifact_id": artifact_id,
                    "digest": digest,
                    "byte_length": len(content),
                    "media_type": media_type,
                    "content": content,
                    "record": record,
                    "created_at": created_at,
                })
                existing = connection.execute(select(
                    artifacts.c.digest,
                    artifacts.c.media_type,
                ).where(and_(
                    artifacts.c.tenant_id == organization_id,
                    artifacts.c.artifact_id == artifact_id,
                ))).mappings().one()
                if existing["digest"] != digest or existing["media_type"] != media_type:
                    raise ValueError("artifact content-address collision")
                connection.execute(insert(artifact_writes).values(
                    tenant_id=organization_id,
                    idempotency_key=idempotency_key,
                    artifact_id=artifact_id,
                    fingerprint=fingerprint,
                    created_at=created_at,
                ))
        except IntegrityError as exc:
            # A competing replica may commit the same request key first.
            with self._tenant_connection(organization_id) as connection:
                prior = connection.execute(select(
                    artifact_writes.c.artifact_id,
                    artifact_writes.c.fingerprint,
                ).where(request_key)).mappings().one_or_none()
            if prior is None:
                raise
            if prior["fingerprint"] != fingerprint:
                raise ValueError("artifact idempotency key was reused with different content") from exc
            return str(prior["artifact_id"])
        return artifact_id

    def get(self, organization_id: str, artifact_id: str) -> bytes | None:
        with self._tenant_connection(organization_id) as connection:
            value = connection.execute(select(artifacts.c.content).where(and_(
                artifacts.c.tenant_id == organization_id,
                artifacts.c.artifact_id == artifact_id,
            ))).scalar_one_or_none()
        return None if value is None else bytes(value)

    def describe(self, organization_id: str, artifact_id: str) -> Mapping[str, Any] | None:
        with self._tenant_connection(organization_id) as connection:
            value = connection.execute(select(artifacts.c.record).where(and_(
                artifacts.c.tenant_id == organization_id,
                artifacts.c.artifact_id == artifact_id,
            ))).scalar_one_or_none()
        return None if value is None else dict(value)

    def close(self) -> None:
        self._engine.dispose()
