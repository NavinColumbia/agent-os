from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import sqlite3
from pathlib import Path

import pytest
from google.api_core.exceptions import NotFound, PreconditionFailed

from agent_os.infrastructure.gcs_artifacts import GCSArtifactStore
from agent_os.infrastructure.sql_artifacts import SQLArtifactStore


class FakeBlob:
    def __init__(self, objects, name: str, generation: int | None = None):
        self._objects = objects
        self.name = name
        self._requested_generation = generation
        self.metadata = None
        self.size = None
        self.generation = generation
        self.upload_attempts = 0

    def _load(self):
        value = self._objects.get(self.name)
        if value is None or (
            self._requested_generation is not None
            and value["generation"] != self._requested_generation
        ):
            raise NotFound("missing")
        self.metadata = dict(value["metadata"])
        self.size = len(value["content"])
        self.generation = value["generation"]
        return value

    def upload_from_string(self, content, **kwargs):
        self.upload_attempts += 1
        assert kwargs["if_generation_match"] == 0
        assert kwargs["checksum"] == "crc32c"
        if self.name in self._objects:
            raise PreconditionFailed("exists")
        self._objects[self.name] = {
            "content": bytes(content),
            "metadata": dict(self.metadata),
            "generation": 1,
        }
        self._load()

    def reload(self, **kwargs):
        self._load()

    def download_as_bytes(self, **kwargs):
        value = self._load()
        assert kwargs["if_generation_match"] == value["generation"]
        assert kwargs["checksum"] == "crc32c"
        return bytes(value["content"])


class FakeBucket:
    def __init__(self):
        self.objects = {}
        self.blobs = []

    def blob(self, name: str, generation: int | None = None):
        blob = FakeBlob(self.objects, name, generation)
        self.blobs.append(blob)
        return blob


class FakeClient:
    def __init__(self):
        self.value = FakeBucket()

    def bucket(self, name: str):
        assert name == "agent-os-artifacts"
        return self.value


@pytest.fixture
def store(tmp_path: Path):
    database = tmp_path / "artifacts.sqlite3"
    client = FakeClient()
    value = GCSArtifactStore(
        f"sqlite:///{database}",
        bucket_name="agent-os-artifacts",
        create_schema=True,
        max_content_bytes=64,
        clock=lambda: datetime(2026, 9, 8, 20, tzinfo=timezone.utc),
        client=client,
    )
    try:
        yield value, client, database
    finally:
        value.close()


def test_gcs_artifacts_are_immutable_integrity_checked_and_tenant_isolated(store):
    artifacts, client, database = store
    artifact_id = artifacts.put(
        organization_id="tenant-secret-name",
        content=b"deployable source",
        media_type="application/octet-stream",
        idempotency_key="build-1",
    )
    replay = artifacts.put(
        organization_id="tenant-secret-name",
        content=b"deployable source",
        media_type="application/octet-stream",
        idempotency_key="build-1",
    )
    deduplicated = artifacts.put(
        organization_id="tenant-secret-name",
        content=b"deployable source",
        media_type="application/octet-stream",
        idempotency_key="build-2",
    )

    assert replay == deduplicated == artifact_id
    assert len(client.value.blobs) == 2
    assert sum(blob.upload_attempts for blob in client.value.blobs) == 2
    assert artifacts.get("tenant-secret-name", artifact_id) == b"deployable source"
    assert artifacts.get("another-tenant", artifact_id) is None
    assert artifacts.describe("another-tenant", artifact_id) is None
    object_name = next(iter(client.value.objects))
    assert "tenant-secret-name" not in object_name
    assert object_name == (
        f"tenants/{hashlib.sha256(b'tenant-secret-name').hexdigest()}/artifacts/{artifact_id}"
    )
    assert client.value.objects[object_name]["content"] == b"deployable source"

    with sqlite3.connect(database) as connection:
        backend, content, locator = connection.execute(
            "SELECT storage_backend, content, object_name FROM aos_v2_artifacts"
        ).fetchone()
    assert (backend, content, locator) == ("gcs", None, object_name)

    client.value.objects[object_name]["content"] = b"corrupted"
    with pytest.raises(RuntimeError, match="integrity verification"):
        artifacts.get("tenant-secret-name", artifact_id)


def test_gcs_artifact_conflicts_and_limits_fail_closed(store):
    artifacts, client, _ = store
    artifacts.put(
        organization_id="tenant-a",
        content=b"one",
        media_type="text/plain",
        idempotency_key="fixed",
    )
    object_count = len(client.value.objects)
    with pytest.raises(ValueError, match="reused with different content"):
        artifacts.put(
            organization_id="tenant-a",
            content=b"two",
            media_type="text/plain",
            idempotency_key="fixed",
        )
    assert len(client.value.objects) == object_count

    with pytest.raises(ValueError, match="64-byte"):
        artifacts.put(
            organization_id="tenant-a",
            content=b"x" * 65,
            media_type="text/plain",
            idempotency_key="too-large",
        )


def test_gcs_adapter_reads_inline_artifacts_during_migration(tmp_path: Path):
    database_url = f"sqlite:///{tmp_path / 'mixed.sqlite3'}"
    inline = SQLArtifactStore(database_url, create_schema=True)
    artifact_id = inline.put(
        organization_id="tenant-a",
        content=b"old inline evidence",
        media_type="text/plain",
        idempotency_key="legacy",
    )
    inline.close()

    external = GCSArtifactStore(
        database_url,
        bucket_name="agent-os-artifacts",
        create_schema=True,
        client=FakeClient(),
    )
    try:
        assert external.get("tenant-a", artifact_id) == b"old inline evidence"
    finally:
        external.close()
