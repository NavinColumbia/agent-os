from __future__ import annotations

import base64
import json
from pathlib import Path
from urllib.parse import urlparse

import pytest

from agent_os.application.command_worker import FatalCommandError
from agent_os.entrypoints.sandbox_job import RESULT_FORMAT
from agent_os.infrastructure.cloud_run_sandbox import CloudRunJobSandboxRunner
from agent_os.infrastructure.docker_sandbox import SOURCE_BUNDLE_MEDIA_TYPE, encode_source_bundle
from agent_os.infrastructure.sql_artifacts import SQLArtifactStore


class FakeBlob:
    def __init__(self, values, name):
        self.values = values
        self.name = name
        self.generation = None
        self.size = None

    def upload_from_string(self, content, **kwargs):
        assert kwargs["if_generation_match"] == 0
        if self.name in self.values:
            raise AssertionError("unexpected duplicate input upload")
        self.values[self.name] = bytes(content)
        self.reload()

    def reload(self):
        self.generation = 1
        self.size = len(self.values[self.name])

    def exists(self):
        return self.name in self.values

    def download_as_bytes(self):
        return self.values[self.name]


class FakeBucket:
    def __init__(self, values):
        self.values = values

    def blob(self, name):
        return FakeBlob(self.values, name)


class FakeStorage:
    def __init__(self):
        self.values = {}

    def bucket(self, name):
        assert name == "control-artifacts"
        return FakeBucket(self.values)


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class FakeSession:
    def __init__(self, storage):
        self.storage = storage
        self.launches = []

    def post(self, url, *, json, timeout):
        assert timeout == 30
        self.launches.append((url, json))
        env = {
            item["name"]: item["value"]
            for item in json["overrides"]["containerOverrides"][0]["env"]
        }
        output_name = urlparse(env["AOS_SANDBOX_OUTPUT_URL"]).path.split("/", 2)[2]
        self.storage.values[output_name] = __import__("json").dumps({
            "format": RESULT_FORMAT,
            "fingerprint": env["AOS_SANDBOX_FINGERPRINT"],
            "output_bundle": {
                "format": "agent-os.source-bundle.v1",
                "files": {"result.txt": {
                    "encoding": "base64",
                    "content": base64.b64encode(b"passed").decode(),
                    "executable": False,
                }},
            },
            "exit_code": 0,
            "timed_out": False,
            "output_error": None,
            "stdout": "ok\n",
            "stderr": "",
        }).encode()
        return FakeResponse({"name": "projects/sandbox-proj/locations/us-central1/operations/op-1"})

    def get(self, url, *, timeout):
        assert url.endswith("/operations/op-1") and timeout == 30
        return FakeResponse({"done": True, "response": {}})


def make_runner(tmp_path: Path):
    store = SQLArtifactStore(f"sqlite:///{tmp_path / 'artifacts.sqlite3'}", create_schema=True)
    storage = FakeStorage()
    session = FakeSession(storage)
    runner = CloudRunJobSandboxRunner(
        store,
        project_id="sandbox-proj",
        region="us-central1",
        job_name="agentos-production-sandbox",
        bucket_name="control-artifacts",
        signing_service_account_email="worker@control-proj.iam.gserviceaccount.com",
        sandbox_revision="registry/sandbox@sha256:" + "a" * 64,
        storage_client=storage,
        session=session,
        signed_url_factory=lambda blob, method, lifetime: (
            f"https://storage.googleapis.com/control-artifacts/{blob.name}?method={method}&ttl={lifetime}"
        ),
        sleep=lambda _: None,
    )
    return store, storage, session, runner


def test_cloud_run_runner_uses_secretless_overrides_persists_evidence_and_replays(tmp_path):
    store, storage, session, runner = make_runner(tmp_path)
    source_id = store.put(
        organization_id="tenant-a",
        content=encode_source_bundle({"main.py": "print('ok')\n"}),
        media_type=SOURCE_BUNDLE_MEDIA_TYPE,
        idempotency_key="source",
    )

    first = runner.run(
        organization_id="tenant-a", artifact_id=source_id,
        command=("python", "main.py"), idempotency_key="action-1",
    )
    replay = runner.run(
        organization_id="tenant-a", artifact_id=source_id,
        command=("python", "main.py"), idempotency_key="action-1",
    )

    assert first["sandbox_backend"] == "cloud-run-job-v1"
    assert first["exit_code"] == 0
    assert json.loads(store.get("tenant-a", first["output_artifact_id"]))["files"]["result.txt"]
    assert replay == {**first, "cached": True}
    assert len(session.launches) == 1
    url, payload = session.launches[0]
    assert url.endswith("/jobs/agentos-production-sandbox:run")
    assert payload["overrides"]["taskCount"] == 1
    names = {
        item["name"] for item in payload["overrides"]["containerOverrides"][0]["env"]
    }
    assert "AOS_SANDBOX_INPUT_URL" in names
    assert not any("DATABASE" in name or "MODEL" in name or "SECRET" in name for name in names)
    assert any(name.startswith("temporary/sandbox/") for name in storage.values)
    store.close()


def test_cloud_run_runner_rejects_cross_input_idempotency_reuse(tmp_path):
    store, _, _, runner = make_runner(tmp_path)
    first_id = store.put(
        organization_id="tenant-a", content=encode_source_bundle({"a.py": "pass"}),
        media_type=SOURCE_BUNDLE_MEDIA_TYPE, idempotency_key="source-a",
    )
    second_id = store.put(
        organization_id="tenant-a", content=encode_source_bundle({"b.py": "pass"}),
        media_type=SOURCE_BUNDLE_MEDIA_TYPE, idempotency_key="source-b",
    )
    runner.run(
        organization_id="tenant-a", artifact_id=first_id,
        command=("python", "a.py"), idempotency_key="same-action",
    )
    with pytest.raises(FatalCommandError, match="reused with different"):
        runner.run(
            organization_id="tenant-a", artifact_id=second_id,
            command=("python", "b.py"), idempotency_key="same-action",
        )
    store.close()
