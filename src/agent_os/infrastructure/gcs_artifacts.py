"""Immutable GCS payloads with tenant-RLS identity and idempotency metadata."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from google.api_core.exceptions import NotFound, PreconditionFailed
from google.cloud import storage

from agent_os.infrastructure.sql_artifacts import SQLArtifactStore


_BUCKET_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$")


def _tenant_prefix(organization_id: str) -> str:
    tenant_digest = hashlib.sha256(organization_id.encode("utf-8")).hexdigest()
    return f"tenants/{tenant_digest}/artifacts/"


class GCSArtifactStore(SQLArtifactStore):
    """Stores immutable bytes in GCS and authoritative metadata in SQL.

    Object names contain only a one-way tenant digest and a content-addressed
    artifact ID. SQL remains the authorization boundary: callers cannot derive
    or read an object until the tenant-RLS metadata lookup succeeds.
    """

    def __init__(
        self,
        database_url: str,
        *,
        bucket_name: str,
        create_schema: bool = False,
        max_content_bytes: int = 64 * 1024 * 1024,
        retention_days: int = 365,
        timeout_seconds: float = 60.0,
        clock: Callable[..., Any] | None = None,
        client: Any | None = None,
    ) -> None:
        bucket_name = bucket_name.strip()
        if not _BUCKET_NAME.fullmatch(bucket_name) or ".." in bucket_name:
            raise ValueError("a valid private GCS bucket name is required")
        if timeout_seconds <= 0:
            raise ValueError("GCS timeout must be positive")
        if not 30 <= retention_days <= 3650:
            raise ValueError("GCS retention days must be between 30 and 3650")
        super().__init__(
            database_url,
            create_schema=create_schema,
            max_content_bytes=max_content_bytes,
            clock=clock,
        )
        self._owns_client = client is None
        self._client = client or storage.Client()
        self._bucket = self._client.bucket(bucket_name)
        self._retention_days = retention_days
        self._timeout_seconds = timeout_seconds

    @staticmethod
    def _object_name(organization_id: str, artifact_id: str) -> str:
        return f"{_tenant_prefix(organization_id)}{artifact_id}"

    @staticmethod
    def _assert_blob_identity(blob: Any, prepared: Mapping[str, Any]) -> int:
        metadata = blob.metadata or {}
        if (
            int(blob.size) != int(prepared["byte_length"])
            or metadata.get("agentos-artifact-id") != prepared["artifact_id"]
            or metadata.get("sha256") != prepared["digest"]
        ):
            raise RuntimeError("immutable artifact object conflicts with SQL identity")
        if blob.generation is None:
            raise RuntimeError("immutable artifact object has no generation")
        return int(blob.generation)

    def put(
        self,
        *,
        organization_id: str,
        content: bytes,
        media_type: str,
        idempotency_key: str,
    ) -> str:
        prepared = self._prepare_put(
            organization_id=organization_id,
            content=content,
            media_type=media_type,
            idempotency_key=idempotency_key,
        )
        prior = self._find_write(organization_id, str(prepared["idempotency_key"]))
        if prior is not None:
            if prior["fingerprint"] != prepared["fingerprint"]:
                raise ValueError("artifact idempotency key was reused with different content")
            return str(prior["artifact_id"])

        object_name = self._object_name(organization_id, str(prepared["artifact_id"]))
        blob = self._bucket.blob(object_name)
        blob.metadata = {
            "agentos-artifact-id": str(prepared["artifact_id"]),
            "sha256": str(prepared["digest"]),
        }
        try:
            blob.upload_from_string(
                content,
                content_type=str(prepared["media_type"]),
                if_generation_match=0,
                checksum="crc32c",
                timeout=self._timeout_seconds,
            )
        except PreconditionFailed:
            blob.reload(timeout=self._timeout_seconds)
        generation = self._assert_blob_identity(blob, prepared)
        prepared_record = dict(prepared["record"])
        prepared_record.update({
            "storage_backend": "gcs",
            "generation": generation,
            "retention_until": (
                prepared["created_at"] + timedelta(days=self._retention_days)
            ).isoformat(),
        })
        prepared["record"] = prepared_record
        return self._persist_prepared(
            prepared,
            content=None,
            storage_backend="gcs",
            object_name=object_name,
        )

    def get(self, organization_id: str, artifact_id: str) -> bytes | None:
        stored = self._load_artifact(organization_id, artifact_id)
        if stored is None:
            return None
        if stored["storage_backend"] == "inline":
            if stored["content"] is None:
                raise RuntimeError("inline artifact payload is missing")
            return bytes(stored["content"])
        if stored["storage_backend"] != "gcs":
            raise RuntimeError("artifact has an unsupported storage backend")

        expected_name = self._object_name(organization_id, artifact_id)
        if stored["object_name"] != expected_name:
            raise RuntimeError("artifact object escaped its tenant prefix")
        record = dict(stored["record"])
        try:
            generation = int(record["generation"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("artifact metadata has no valid object generation") from exc
        blob = self._bucket.blob(expected_name, generation=generation)
        try:
            content = blob.download_as_bytes(
                if_generation_match=generation,
                checksum="crc32c",
                timeout=self._timeout_seconds,
            )
        except NotFound as exc:
            retention_value = record.get("retention_until")
            if isinstance(retention_value, str):
                try:
                    retention_until = datetime.fromisoformat(retention_value)
                except ValueError:
                    retention_until = None
                if retention_until is not None:
                    if retention_until.tzinfo is None:
                        retention_until = retention_until.replace(tzinfo=timezone.utc)
                    now = self._clock()
                    if now.tzinfo is None:
                        now = now.replace(tzinfo=timezone.utc)
                    if now >= retention_until:
                        return None
            raise RuntimeError("artifact object is missing from private storage") from exc
        if (
            len(content) != int(record["byte_length"])
            or hashlib.sha256(content).hexdigest() != record["digest"]
        ):
            raise RuntimeError("artifact object failed end-to-end integrity verification")
        return content

    def close(self) -> None:
        try:
            if self._owns_client:
                self._client.close()
        finally:
            super().close()


def build_artifact_store(
    database_url: str,
    *,
    backend: str,
    bucket_name: str,
    create_schema: bool,
    max_content_bytes: int,
    retention_days: int = 365,
) -> SQLArtifactStore:
    """Construct the configured adapter without leaking cloud concerns upward."""
    if backend == "sql":
        return SQLArtifactStore(
            database_url,
            create_schema=create_schema,
            max_content_bytes=max_content_bytes,
        )
    if backend == "gcs":
        return GCSArtifactStore(
            database_url,
            bucket_name=bucket_name,
            create_schema=create_schema,
            max_content_bytes=max_content_bytes,
            retention_days=retention_days,
        )
    raise ValueError("artifact backend must be sql or gcs")
