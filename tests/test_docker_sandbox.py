from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import subprocess

import pytest

from agent_os.application.command_worker import FatalCommandError, RetryableCommandError
from agent_os.infrastructure.docker_sandbox import (
    DEFAULT_PYTHON_IMAGE,
    DockerSandboxRunner,
    SOURCE_BUNDLE_MEDIA_TYPE,
    encode_source_bundle,
)
from agent_os.infrastructure.sql_artifacts import SQLArtifactStore


def test_source_bundle_rejects_archive_traversal_without_extracting_an_archive():
    with pytest.raises(FatalCommandError, match="normalized and relative"):
        encode_source_bundle({"../escape.py": "bad"})
    with pytest.raises(FatalCommandError, match="normalized and relative"):
        encode_source_bundle({"/absolute.py": "bad"})


def test_docker_runner_enforces_isolation_argv_and_replays_durable_result(monkeypatch, tmp_path: Path):
    store = SQLArtifactStore(f"sqlite:///{tmp_path / 'sandbox.sqlite3'}", create_schema=True)
    source_id = store.put(
        organization_id="tenant-a",
        content=encode_source_bundle({"main.py": "print('hello')\n"}),
        media_type=SOURCE_BUNDLE_MEDIA_TYPE,
        idempotency_key="source-request",
    )
    invocations = []
    control_invocations = []

    class FakeProcess:
        def __init__(self, command, **kwargs):
            del kwargs
            invocations.append(command)
            self.stdout = BytesIO(b"hello\n")
            self.stderr = BytesIO()

        def wait(self, timeout):
            assert timeout == 30
            return 0

    monkeypatch.setattr("agent_os.infrastructure.docker_sandbox.subprocess.Popen", FakeProcess)

    def fake_run(command, **kwargs):
        del kwargs
        control_invocations.append(command)
        stdout = b""
        if any("maximum_files = int" in item for item in command):
            stdout = encode_source_bundle({"main.py": "print('hello')\n"})
        return subprocess.CompletedProcess(command, 0, stdout, b"")

    monkeypatch.setattr("agent_os.infrastructure.docker_sandbox.subprocess.run", fake_run)
    runner = DockerSandboxRunner(
        store,
        docker_binary="/bin/true",
        timeout_seconds=30,
    )

    first = runner.run(
        organization_id="tenant-a",
        artifact_id=source_id,
        command=("python", "main.py"),
        idempotency_key="sandbox-action",
    )
    replay = runner.run(
        organization_id="tenant-a",
        artifact_id=source_id,
        command=("python", "main.py"),
        idempotency_key="sandbox-action",
    )

    assert len(invocations) == 1
    create_command = next(command for command in control_invocations if command[1] == "run")
    assert create_command[create_command.index("--network") + 1] == "none"
    assert "--read-only" in create_command
    assert create_command[create_command.index("--cap-drop") + 1] == "ALL"
    assert create_command[create_command.index("--security-opt") + 1] == "no-new-privileges:true"
    assert create_command[create_command.index("--user") + 1] != "0:0"
    assert create_command[create_command.index("--pull") + 1] == "never"
    assert any("/workspace:rw,nosuid,nodev,size=" in item for item in create_command)
    assert DEFAULT_PYTHON_IMAGE in create_command
    execute_command = invocations[0]
    assert execute_command[-2:] == ["python", "main.py"]
    assert first["exit_code"] == 0
    assert first["cached"] is False
    assert first["output_error"] is None
    assert replay == {**first, "cached": True}
    manifest = json.loads(store.get("tenant-a", first["result_artifact_id"]))
    assert manifest["source_artifact_id"] == source_id
    assert manifest["command"] == ["python", "main.py"]
    with pytest.raises(FatalCommandError, match="reused with different"):
        runner.run(
            organization_id="tenant-a", artifact_id=source_id,
            command=("python", "different.py"), idempotency_key="sandbox-action",
        )
    store.close()


def test_docker_runner_requires_pinned_image_and_source_bundle_media_type(tmp_path: Path):
    store = SQLArtifactStore(f"sqlite:///{tmp_path / 'sandbox.sqlite3'}", create_schema=True)
    with pytest.raises(ValueError, match="pinned"):
        DockerSandboxRunner(store, image="python:latest", docker_binary="/bin/true")
    wrong_id = store.put(
        organization_id="tenant-a", content=b"not a bundle", media_type="text/plain",
        idempotency_key="wrong-source",
    )
    runner = DockerSandboxRunner(store, docker_binary="/bin/true")
    with pytest.raises(FatalCommandError, match="source-bundle"):
        runner.run(
            organization_id="tenant-a", artifact_id=wrong_id,
            command=("python", "main.py"), idempotency_key="wrong-run",
        )
    store.close()


def test_docker_runner_uses_an_explicit_shared_workspace_root(tmp_path: Path):
    store = SQLArtifactStore(f"sqlite:///{tmp_path / 'sandbox.sqlite3'}", create_schema=True)
    workspace = tmp_path / "shared"
    workspace.mkdir()
    runner = DockerSandboxRunner(
        store, docker_binary="/bin/true", workspace_root=workspace,
    )
    assert runner._workspace_root == workspace.resolve()

    with pytest.raises(ValueError, match="must be absolute"):
        DockerSandboxRunner(
            store, docker_binary="/bin/true", workspace_root="relative/path",
        )
    with pytest.raises(ValueError, match="must exist"):
        DockerSandboxRunner(
            store, docker_binary="/bin/true", workspace_root=tmp_path / "missing",
        )
    store.close()


def test_docker_infrastructure_failure_is_retryable_and_not_cached(monkeypatch, tmp_path: Path):
    store = SQLArtifactStore(f"sqlite:///{tmp_path / 'infra.sqlite3'}", create_schema=True)
    source_id = store.put(
        organization_id="tenant-a",
        content=encode_source_bundle({"main.py": "print('hello')"}),
        media_type=SOURCE_BUNDLE_MEDIA_TYPE,
        idempotency_key="source-request",
    )

    monkeypatch.setattr(
        "agent_os.infrastructure.docker_sandbox.subprocess.run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 125, b"", b"pinned image is not pre-pulled",
        ),
    )
    runner = DockerSandboxRunner(store, docker_binary="/bin/true")

    with pytest.raises(RetryableCommandError, match="container start failed"):
        runner.run(
            organization_id="tenant-a", artifact_id=source_id,
            command=("python", "main.py"), idempotency_key="infra-action",
        )

    assert store.find_by_idempotency_key("tenant-a", "infra-action:sandbox-result") is None
    store.close()


def test_docker_runner_persists_output_rejection_as_failure_evidence(monkeypatch, tmp_path: Path):
    store = SQLArtifactStore(f"sqlite:///{tmp_path / 'output-rejection.sqlite3'}", create_schema=True)
    source_id = store.put(
        organization_id="tenant-a",
        content=encode_source_bundle({"main.py": "print('hello')"}),
        media_type=SOURCE_BUNDLE_MEDIA_TYPE,
        idempotency_key="rejection-source",
    )

    class FakeProcess:
        stdout = BytesIO()
        stderr = BytesIO()

        def __init__(self, command, **kwargs):
            del command, kwargs

        def wait(self, timeout):
            del timeout
            return 0

    monkeypatch.setattr("agent_os.infrastructure.docker_sandbox.subprocess.Popen", FakeProcess)

    def fake_run(command, **kwargs):
        del kwargs
        stdout = b""
        if any("maximum_files = int" in item for item in command):
            stdout = b'{"error":"sandbox output exceeds the artifact byte limit"}'
        return subprocess.CompletedProcess(command, 0, stdout, b"")

    monkeypatch.setattr("agent_os.infrastructure.docker_sandbox.subprocess.run", fake_run)
    runner = DockerSandboxRunner(store, docker_binary="/bin/true")
    result = runner.run(
        organization_id="tenant-a",
        artifact_id=source_id,
        command=("python", "main.py"),
        idempotency_key="rejection-run",
    )

    assert result["exit_code"] == 65
    assert result["output_error"] == "sandbox output exceeds the artifact byte limit"
    assert json.loads(store.get("tenant-a", result["output_artifact_id"]))["files"] == {}
    assert store.describe("tenant-a", result["result_artifact_id"]) is not None
    store.close()
