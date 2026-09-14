from __future__ import annotations

from datetime import datetime, timezone
import json

from google.api_core.exceptions import NotFound, PreconditionFailed
import pytest
import requests

from agent_os.infrastructure.deployment_operations import GCPDeploymentOperator


ROUTE_ID = "R" * 43
REVISION = "a" * 64
SERVICE = "aos-" + "b" * 40


class FakeBlob:
    def __init__(self, bucket: "FakeBucket", name: str) -> None:
        self.bucket = bucket
        self.name = name
        self.generation: int | None = None

    def reload(self, **kwargs) -> None:
        del kwargs
        if self.name not in self.bucket.objects:
            raise NotFound("missing")
        self.generation = self.bucket.objects[self.name]["generation"]

    def download_as_bytes(self, **kwargs) -> bytes:
        del kwargs
        if self.name not in self.bucket.objects:
            raise NotFound("missing")
        return self.bucket.objects[self.name]["content"]

    def upload_from_string(self, content: bytes, *, if_generation_match: int, **kwargs) -> None:
        del kwargs
        current = self.bucket.objects.get(self.name)
        generation = 0 if current is None else current["generation"]
        if self.bucket.conflict_once:
            self.bucket.conflict_once = False
            raise PreconditionFailed("raced")
        if generation != if_generation_match:
            raise PreconditionFailed("generation mismatch")
        self.bucket.generation += 1
        self.generation = self.bucket.generation
        self.bucket.objects[self.name] = {
            "generation": self.generation,
            "content": bytes(content),
        }


class FakeBucket:
    def __init__(self) -> None:
        self.generation = 1
        self.conflict_once = False
        self.objects = {
            f"routes/{ROUTE_ID}.json": {
                "generation": 1,
                "content": json.dumps({
                    "format": "agent-os.static-route.v1",
                    "revision": REVISION,
                }).encode(),
            },
        }

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self, name)


class FakeStorage:
    def __init__(self) -> None:
        self.value = FakeBucket()

    def bucket(self, name: str) -> FakeBucket:
        assert name == "valid-private-app-bucket"
        return self.value


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self.payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            response = requests.Response()
            response.status_code = self.status_code
            raise requests.HTTPError(response=response)

    def json(self) -> dict:
        return self.payload


class FakeSession:
    def __init__(self) -> None:
        self.public = True
        self.reconciling = False
        self.patches: list[dict] = []

    def get(self, url: str, **kwargs) -> FakeResponse:
        del kwargs
        assert url.endswith("/services/" + SERVICE)
        return FakeResponse({
            "name": SERVICE,
            "invokerIamDisabled": self.public,
            "reconciling": self.reconciling,
        })

    def patch(self, url: str, **kwargs) -> FakeResponse:
        assert url.endswith("/services/" + SERVICE)
        self.patches.append(kwargs)
        self.public = kwargs["json"]["invokerIamDisabled"]
        return FakeResponse({"name": "operations/change"})


def operator(*, storage=None, session=None) -> GCPDeploymentOperator:
    return GCPDeploymentOperator(
        published_bucket="valid-private-app-bucket",
        app_project_id="valid-app-project-123",
        region="us-central1",
        storage_client=storage or FakeStorage(),
        session=session or FakeSession(),
        sleep=lambda _: None,
        clock=lambda: datetime(2026, 9, 13, tzinfo=timezone.utc),
    )


def test_static_route_can_be_suspended_idempotently_and_restored_after_a_race():
    storage = FakeStorage()
    control = operator(storage=storage)

    suspended = control.set_static_route(
        ROUTE_ID, suspended=True, reason="INC-42 unsafe output", actor="on-call@example.test",
    )
    assert suspended == {
        "kind": "static_site", "target": ROUTE_ID, "status": "suspended",
        "cached": False, "generation": 2, "actor": "on-call@example.test",
        "reason": "INC-42 unsafe output",
    }
    pointer = json.loads(storage.value.objects[f"routes/{ROUTE_ID}.json"]["content"])
    assert pointer == {
        "changed_at": "2026-09-13T00:00:00+00:00",
        "changed_by": "on-call@example.test",
        "format": "agent-os.static-route.v1",
        "reason": "INC-42 unsafe output",
        "revision": REVISION,
        "status": "suspended",
    }
    assert control.set_static_route(
        ROUTE_ID, suspended=True, reason="repeat", actor="second-operator",
    )["cached"] is True

    storage.value.conflict_once = True
    restored = control.set_static_route(ROUTE_ID, suspended=False, actor="on-call@example.test")
    assert restored["status"] == "active"
    assert restored["generation"] == 3
    pointer = json.loads(storage.value.objects[f"routes/{ROUTE_ID}.json"]["content"])
    assert pointer["status"] == "active"
    assert "reason" not in pointer


def test_static_route_rejects_unsafe_identity_reason_and_missing_route():
    control = operator()
    with pytest.raises(ValueError, match="reason is required"):
        control.set_static_route(ROUTE_ID, suspended=True, actor="on-call")
    with pytest.raises(ValueError, match="actor is invalid"):
        control.set_static_route(ROUTE_ID, suspended=True, reason="incident", actor="bad\nactor")
    with pytest.raises(ValueError, match="route ID"):
        control.set_static_route("tenant-readable", suspended=False, actor="on-call")
    with pytest.raises(LookupError, match="does not exist"):
        control.set_static_route("Z" * 43, suspended=False, actor="on-call")


def test_cloud_run_service_can_be_suspended_idempotently_and_restored():
    session = FakeSession()
    control = operator(session=session)

    suspended = control.set_service(
        SERVICE, suspended=True, reason="INC-43 compromised output", actor="on-call",
    )
    assert suspended["status"] == "suspended"
    assert suspended["actor"] == "on-call"
    assert session.patches[0]["params"] == {"updateMask": "invoker_iam_disabled"}
    assert session.patches[0]["json"] == {
        "name": "projects/valid-app-project-123/locations/us-central1/services/" + SERVICE,
        "invokerIamDisabled": False,
    }
    assert control.set_service(
        SERVICE, suspended=True, reason="repeat", actor="on-call",
    )["cached"] is True
    restored = control.set_service(SERVICE, suspended=False, actor="on-call")
    assert restored["status"] == "active"
    assert session.public is True


def test_cloud_run_service_rejects_unbounded_targets_and_unexplained_suspension():
    control = operator()
    with pytest.raises(ValueError, match="service name"):
        control.set_service("some-customer-service", suspended=False, actor="on-call")
    with pytest.raises(ValueError, match="reason is required"):
        control.set_service(SERVICE, suspended=True, actor="on-call")
