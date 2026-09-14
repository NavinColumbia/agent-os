from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_os.infrastructure.sql_artifacts import SQLArtifactStore


@pytest.fixture
def store(tmp_path: Path):
    value = SQLArtifactStore(
        f"sqlite:///{tmp_path / 'artifacts.sqlite3'}",
        create_schema=True,
        max_content_bytes=32,
        clock=lambda: datetime(2026, 9, 8, 16, tzinfo=timezone.utc),
    )
    try:
        yield value
    finally:
        value.close()


def test_artifacts_are_content_addressed_idempotent_and_tenant_isolated(store):
    first = store.put(
        organization_id="tenant-a",
        content=b"hello",
        media_type="text/plain",
        idempotency_key="request-one",
    )
    replay = store.put(
        organization_id="tenant-a",
        content=b"hello",
        media_type="text/plain",
        idempotency_key="request-one",
    )
    deduplicated = store.put(
        organization_id="tenant-a",
        content=b"hello",
        media_type="text/plain",
        idempotency_key="request-two",
    )

    assert first == replay == deduplicated
    assert store.get("tenant-a", first) == b"hello"
    assert store.get("tenant-b", first) is None
    assert store.describe("tenant-b", first) is None
    assert store.describe("tenant-a", first) == {
        "artifact_id": first,
        "tenant_id": "tenant-a",
        "digest": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
        "byte_length": 5,
        "media_type": "text/plain",
        "created_at": "2026-09-08T16:00:00+00:00",
    }
    assert store.find_by_idempotency_key("tenant-a", "request-one")["artifact_id"] == first
    assert store.find_by_idempotency_key("tenant-b", "request-one") is None


def test_artifact_request_key_conflicts_and_size_limit_fail_closed(store):
    store.put(
        organization_id="tenant-a",
        content=b"first",
        media_type="text/plain",
        idempotency_key="same-request",
    )

    with pytest.raises(ValueError, match="reused with different content"):
        store.put(
            organization_id="tenant-a",
            content=b"second",
            media_type="text/plain",
            idempotency_key="same-request",
        )
    with pytest.raises(ValueError, match="32-byte"):
        store.put(
            organization_id="tenant-a",
            content=b"x" * 33,
            media_type="text/plain",
            idempotency_key="too-large",
        )


def test_media_type_is_part_of_artifact_identity(store):
    plain = store.put(
        organization_id="tenant-a", content=b"{}", media_type="text/plain",
        idempotency_key="plain-request",
    )
    structured = store.put(
        organization_id="tenant-a", content=b"{}", media_type="application/json",
        idempotency_key="json-request",
    )

    assert plain != structured

    with pytest.raises(ValueError, match="media type is invalid"):
        store.put(
            organization_id="tenant-a", content=b"x", media_type="text/html\r\nX-Evil: true",
            idempotency_key="bad-media-type",
        )


def test_artifact_inventory_is_bounded_filtered_and_tenant_scoped(store):
    plain = store.put(
        organization_id="tenant-a", content=b"plain", media_type="text/plain",
        idempotency_key="inventory-plain",
    )
    structured = store.put(
        organization_id="tenant-a", content=b"{}", media_type="application/json",
        idempotency_key="inventory-json",
    )
    store.put(
        organization_id="tenant-b", content=b"hidden", media_type="text/plain",
        idempotency_key="inventory-hidden",
    )

    assert [item["artifact_id"] for item in store.list_artifacts("tenant-a")] == [
        structured, plain,
    ]
    assert store.list_artifacts(
        "tenant-a", media_types=("application/json",), limit=1,
    )[0]["artifact_id"] == structured
    assert store.list_artifacts("tenant-b")[0]["tenant_id"] == "tenant-b"
    with pytest.raises(ValueError, match="limit"):
        store.list_artifacts("tenant-a", limit=0)
