"""Fail-closed local Docker sandbox for untrusted source bundles."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tempfile
from threading import Thread
from typing import Any, Mapping
import uuid

from agent_os.application.command_worker import FatalCommandError, RetryableCommandError
from agent_os.application.ports import ArtifactStore, SandboxRunner


SOURCE_BUNDLE_MEDIA_TYPE = "application/vnd.agent-os.source-bundle+json"
SANDBOX_RESULT_MEDIA_TYPE = "application/vnd.agent-os.sandbox-result+json"
DEFAULT_PYTHON_IMAGE = (
    "python:3.12-slim-trixie@"
    "sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea"
)
OUTPUT_REJECTED_EXIT_CODE = 65
_IGNORED_OUTPUT_PARTS = {".git", ".pytest_cache", "__pycache__"}
_MATERIALIZE_SCRIPT = """
import base64, json, os, pathlib, sys
bundle = json.load(sys.stdin)
root = pathlib.Path('/workspace')
for relative, spec in bundle['files'].items():
    target = root.joinpath(*relative.split('/'))
    target.parent.mkdir(parents=True, exist_ok=True)
    if spec['encoding'] == 'utf-8':
        content = spec['content'].encode('utf-8')
    else:
        content = base64.b64decode(spec['content'], validate=True)
    target.write_bytes(content)
    os.chmod(target, 0o755 if spec.get('executable') is True else 0o644)
""".strip()
_PACKAGE_SCRIPT = """
import base64, json, pathlib, sys
root = pathlib.Path('/workspace')
ignored = {'.git', '.pytest_cache', '__pycache__'}
maximum_files = int(sys.argv[1])
maximum_bytes = int(sys.argv[2])
try:
    files = {}
    total = 0
    for candidate in sorted(root.rglob('*')):
        relative = candidate.relative_to(root)
        if any(part in ignored for part in relative.parts):
            continue
        if candidate.is_symlink():
            raise ValueError('sandbox output may not contain symbolic links')
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise ValueError('sandbox output may contain only regular files')
        if len(files) >= maximum_files:
            raise ValueError('sandbox output exceeds the file-count limit')
        content = candidate.read_bytes()
        total += len(content)
        if total > maximum_bytes:
            raise ValueError('sandbox output exceeds the artifact byte limit')
        files[relative.as_posix()] = {
            'encoding': 'base64',
            'content': base64.b64encode(content).decode('ascii'),
            'executable': bool(candidate.stat().st_mode & 0o111),
        }
    print(json.dumps({'format': 'agent-os.source-bundle.v1', 'files': files},
                     ensure_ascii=True, separators=(',', ':'), sort_keys=True))
except Exception as exc:
    print(json.dumps({'error': f'{type(exc).__name__}: {str(exc)[:1000]}'},
                     ensure_ascii=True, separators=(',', ':'), sort_keys=True))
""".strip()


def _bundle_path(raw: str) -> PurePosixPath:
    if not raw or "\0" in raw or len(raw) > 512:
        raise FatalCommandError("source bundle contains an invalid path")
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise FatalCommandError("source bundle path is not valid UTF-8") from exc
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise FatalCommandError("source bundle paths must be normalized and relative")
    return path


def encode_source_bundle(files: Mapping[str, str | bytes]) -> bytes:
    """Create the canonical, archive-free source bundle representation."""

    encoded: dict[str, Mapping[str, Any]] = {}
    for raw_path, value in sorted(files.items()):
        path = _bundle_path(raw_path).as_posix()
        if isinstance(value, str):
            encoded[path] = {"encoding": "utf-8", "content": value, "executable": False}
        elif isinstance(value, bytes):
            encoded[path] = {
                "encoding": "base64",
                "content": base64.b64encode(value).decode("ascii"),
                "executable": False,
            }
        else:
            raise TypeError("source bundle values must be strings or bytes")
    return json.dumps(
        {"format": "agent-os.source-bundle.v1", "files": encoded},
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class DockerSandboxRunner(SandboxRunner):
    """Execute direct argv in a pinned, networkless, resource-capped image.

    This adapter is for a dedicated development/CI runner host. It never falls
    back to the host when Docker or isolation setup is unavailable.
    """

    def __init__(
        self,
        artifact_store: ArtifactStore,
        *,
        image: str = DEFAULT_PYTHON_IMAGE,
        docker_binary: str = "docker",
        timeout_seconds: int = 300,
        memory_limit: str = "512m",
        cpu_limit: str = "1.0",
        pids_limit: int = 128,
        max_files: int = 2_000,
        max_output_bundle_bytes: int = 1024 * 1024,
        workspace_limit_bytes: int = 4 * 1024 * 1024,
        max_log_bytes: int = 64 * 1024,
    ) -> None:
        resolved = shutil.which(docker_binary)
        image_digest = image.rpartition("@sha256:")[2]
        if resolved is None:
            raise ValueError("Docker sandbox backend is configured but docker is unavailable")
        if len(image_digest) != 64 or any(char not in "0123456789abcdef" for char in image_digest):
            raise ValueError("sandbox image must be pinned by a lowercase sha256 digest")
        if (
            timeout_seconds < 1 or pids_limit < 1 or max_files < 1
            or max_output_bundle_bytes < 1 or workspace_limit_bytes < max_output_bundle_bytes
            or max_log_bytes < 1
        ):
            raise ValueError("sandbox resource limits must be positive")
        self._store = artifact_store
        self._image = image
        self._docker = resolved
        self._timeout_seconds = timeout_seconds
        self._memory_limit = memory_limit
        self._cpu_limit = cpu_limit
        self._pids_limit = pids_limit
        self._max_files = max_files
        self._max_output_bundle_bytes = max_output_bundle_bytes
        self._workspace_limit_bytes = workspace_limit_bytes
        self._max_log_bytes = max_log_bytes
        self._run_uid = os.getuid() if os.getuid() != 0 else 65532
        self._run_gid = os.getgid() if os.getuid() != 0 else 65532

    def _load_source(self, organization_id: str, artifact_id: str) -> Mapping[str, Any]:
        record = self._store.describe(organization_id, artifact_id)
        content = self._store.get(organization_id, artifact_id)
        if record is None or content is None:
            raise FatalCommandError("sandbox source artifact does not exist in this tenant")
        if record.get("media_type") != SOURCE_BUNDLE_MEDIA_TYPE:
            raise FatalCommandError("sandbox source must use the Agent OS source-bundle media type")
        try:
            raw = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FatalCommandError("sandbox source bundle is not valid JSON") from exc
        if not isinstance(raw, Mapping) or raw.get("format") != "agent-os.source-bundle.v1":
            raise FatalCommandError("sandbox source bundle format is unsupported")
        files = raw.get("files")
        if not isinstance(files, Mapping) or len(files) > self._max_files:
            raise FatalCommandError("sandbox source bundle has an invalid file map")
        return files

    def _materialize(self, workspace: Path, files: Mapping[str, Any]) -> None:
        if len(files) > self._max_files:
            raise FatalCommandError("source bundle exceeds the file-count limit")
        total = 0
        for raw_path, specification in sorted(files.items()):
            path = _bundle_path(str(raw_path))
            if not isinstance(specification, Mapping):
                raise FatalCommandError("source bundle file specification must be an object")
            encoding = specification.get("encoding")
            raw_content = specification.get("content")
            if not isinstance(raw_content, str):
                raise FatalCommandError("source bundle file content must be a string")
            try:
                if encoding == "utf-8":
                    content = raw_content.encode("utf-8")
                elif encoding == "base64":
                    content = base64.b64decode(raw_content, validate=True)
                else:
                    raise FatalCommandError("source bundle file encoding is unsupported")
            except ValueError as exc:
                raise FatalCommandError("source bundle contains invalid base64") from exc
            total += len(content)
            if total > self._max_output_bundle_bytes:
                raise FatalCommandError("source bundle exceeds the sandbox materialization limit")
            destination = workspace.joinpath(*path.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            destination.chmod(0o777 if specification.get("executable") is True else 0o666)
        workspace.chmod(0o777)
        for candidate in workspace.rglob("*"):
            if candidate.is_dir():
                candidate.chmod(0o777)

    def _capture(self, pipe, target: bytearray) -> None:
        try:
            while True:
                chunk = pipe.read(8192)
                if not chunk:
                    return
                target.extend(chunk)
                overflow = len(target) - self._max_log_bytes
                if overflow > 0:
                    del target[:overflow]
        finally:
            pipe.close()

    def _docker_control(
        self,
        arguments: list[str],
        operation: str,
        *,
        input_bytes: bytes | None = None,
    ) -> subprocess.CompletedProcess:
        try:
            options: dict[str, Any] = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "timeout": 30,
                "check": False,
            }
            if input_bytes is None:
                options["stdin"] = subprocess.DEVNULL
            else:
                options["input"] = input_bytes
            result = subprocess.run([self._docker, *arguments], **options)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RetryableCommandError(f"Docker sandbox {operation} did not complete") from exc
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise RetryableCommandError(
                f"Docker sandbox {operation} failed: {detail[:1500]}"
            )
        return result

    def _remove_container(self, container_name: str) -> None:
        self._docker_control(
            ["rm", "--force", container_name],
            "container cleanup",
        )

    def _package(self, workspace: Path) -> bytes:
        files: dict[str, Mapping[str, Any]] = {}
        total = 0
        for candidate in sorted(workspace.rglob("*")):
            relative = candidate.relative_to(workspace)
            if any(part in _IGNORED_OUTPUT_PARTS for part in relative.parts):
                continue
            if candidate.is_symlink():
                raise FatalCommandError("sandbox output may not contain symbolic links")
            if candidate.is_dir():
                continue
            if not candidate.is_file():
                raise FatalCommandError("sandbox output may contain only regular files")
            if len(files) >= self._max_files:
                raise FatalCommandError("sandbox output exceeds the file-count limit")
            content = candidate.read_bytes()
            total += len(content)
            if total > self._max_output_bundle_bytes:
                raise FatalCommandError("sandbox output exceeds the artifact byte limit")
            files[relative.as_posix()] = {
                "encoding": "base64",
                "content": base64.b64encode(content).decode("ascii"),
                "executable": bool(candidate.stat().st_mode & 0o111),
            }
        return json.dumps(
            {"format": "agent-os.source-bundle.v1", "files": files},
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def _cached_result(
        self,
        organization_id: str,
        idempotency_key: str,
        artifact_id: str,
        command: tuple[str, ...],
    ) -> Mapping[str, Any] | None:
        record = self._store.find_by_idempotency_key(
            organization_id, f"{idempotency_key}:sandbox-result",
        )
        if record is None:
            return None
        content = self._store.get(organization_id, str(record["artifact_id"]))
        if content is None:
            raise FatalCommandError("sandbox result idempotency record lost its artifact")
        try:
            result = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FatalCommandError("sandbox result artifact is corrupt") from exc
        if not isinstance(result, Mapping):
            raise FatalCommandError("sandbox result artifact is not an object")
        if (
            result.get("source_artifact_id") != artifact_id
            or result.get("command") != list(command)
            or result.get("image") != self._image
        ):
            raise FatalCommandError(
                "sandbox idempotency key was reused with different source, command, or image"
            )
        return {**dict(result), "result_artifact_id": record["artifact_id"], "cached": True}

    def run(
        self,
        *,
        organization_id: str,
        artifact_id: str,
        command: tuple[str, ...],
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        if (
            not command or len(command) > 64
            or any(not isinstance(item, str) or not item or "\0" in item or len(item) > 4096 for item in command)
        ):
            raise FatalCommandError("sandbox command must be bounded direct argv")
        cached = self._cached_result(organization_id, idempotency_key, artifact_id, command)
        if cached is not None:
            return cached
        files = self._load_source(organization_id, artifact_id)
        invocation = hashlib.sha256(
            f"{organization_id}:{idempotency_key}:{uuid.uuid4().hex}".encode()
        ).hexdigest()[:24]
        container_name = f"aos-sbx-{invocation}"
        stdout = bytearray()
        stderr = bytearray()
        timed_out = False
        output_error: str | None = None
        with tempfile.TemporaryDirectory(prefix="agent-os-sandbox-") as temporary:
            source_workspace = Path(temporary) / "source"
            output_workspace = Path(temporary) / "output"
            source_workspace.mkdir(mode=0o755)
            self._materialize(source_workspace, files)
            bundle_input = json.dumps(
                {"format": "agent-os.source-bundle.v1", "files": files},
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            container_started = False
            try:
                self._docker_control([
                    "run",
                    "--detach",
                    "--pull",
                    "never",
                    "--name",
                    container_name,
                    "--network",
                    "none",
                    "--read-only",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges:true",
                    "--pids-limit",
                    str(self._pids_limit),
                    "--memory",
                    self._memory_limit,
                    "--cpus",
                    self._cpu_limit,
                    "--ulimit",
                    "nofile=1024:1024",
                    "--user",
                    f"{self._run_uid}:{self._run_gid}",
                    "--tmpfs",
                    "/tmp:rw,noexec,nosuid,nodev,size=64m",
                    "--tmpfs",
                    f"/workspace:rw,nosuid,nodev,size={self._workspace_limit_bytes},mode=1777",
                    "--workdir",
                    "/workspace",
                    "--env",
                    "HOME=/tmp",
                    "--env",
                    "PYTHONDONTWRITEBYTECODE=1",
                    "--label",
                    "agent-os.sandbox=true",
                    self._image,
                    "sleep",
                    "86400",
                ], "container start")
                container_started = True
                self._docker_control([
                    "exec",
                    "--interactive",
                    "--user",
                    f"{self._run_uid}:{self._run_gid}",
                    "--workdir",
                    "/workspace",
                    container_name,
                    "python",
                    "-c",
                    _MATERIALIZE_SCRIPT,
                ], "source materialization", input_bytes=bundle_input)
                docker_command = [
                    self._docker,
                    "exec",
                    "--user",
                    f"{self._run_uid}:{self._run_gid}",
                    "--workdir",
                    "/workspace",
                    container_name,
                    *command,
                ]
                try:
                    process = subprocess.Popen(
                        docker_command,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                except OSError as exc:
                    raise RetryableCommandError("Docker sandbox execution could not start") from exc
                if process.stdout is None or process.stderr is None:
                    raise RetryableCommandError("Docker sandbox execution pipes were not created")
                readers = (
                    Thread(target=self._capture, args=(process.stdout, stdout), daemon=True),
                    Thread(target=self._capture, args=(process.stderr, stderr), daemon=True),
                )
                for reader in readers:
                    reader.start()
                try:
                    exit_code = process.wait(timeout=self._timeout_seconds)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    self._remove_container(container_name)
                    container_started = False
                    try:
                        exit_code = process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        exit_code = process.wait(timeout=5)
                for reader in readers:
                    reader.join(timeout=5)
                if timed_out:
                    exit_code = 124
                    output_workspace.mkdir(mode=0o755)
                    output_error = "sandbox timed out before output collection"
                else:
                    output_workspace.mkdir(mode=0o755)
                    packaged = self._docker_control([
                        "exec",
                        "--user",
                        f"{self._run_uid}:{self._run_gid}",
                        "--workdir",
                        "/workspace",
                        container_name,
                        "python",
                        "-I",
                        "-c",
                        _PACKAGE_SCRIPT,
                        str(self._max_files),
                        str(self._max_output_bundle_bytes),
                    ], "output packaging")
                    try:
                        packaged_bundle = json.loads(packaged.stdout)
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise FatalCommandError(
                            "sandbox output packager returned invalid evidence"
                        ) from exc
                    if not isinstance(packaged_bundle, Mapping):
                        raise FatalCommandError("sandbox output packager returned no evidence object")
                    if packaged_bundle.get("error"):
                        output_error = str(packaged_bundle["error"])
                        if exit_code == 0:
                            exit_code = OUTPUT_REJECTED_EXIT_CODE
                    else:
                        packaged_files = packaged_bundle.get("files")
                        if (
                            packaged_bundle.get("format") != "agent-os.source-bundle.v1"
                            or not isinstance(packaged_files, Mapping)
                        ):
                            raise FatalCommandError("sandbox output bundle format is invalid")
                        self._materialize(output_workspace, packaged_files)
            finally:
                if container_started:
                    self._remove_container(container_name)
            output_content = self._package(output_workspace)

        output_artifact_id = self._store.put(
            organization_id=organization_id,
            content=output_content,
            media_type=SOURCE_BUNDLE_MEDIA_TYPE,
            idempotency_key=f"{idempotency_key}:sandbox-output",
        )
        result = {
            "sandbox_backend": "docker-local-v1",
            "image": self._image,
            "source_artifact_id": artifact_id,
            "command": list(command),
            "output_artifact_id": output_artifact_id,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "output_error": output_error,
            "stdout": bytes(stdout).decode("utf-8", errors="replace"),
            "stderr": bytes(stderr).decode("utf-8", errors="replace"),
        }
        result_content = json.dumps(
            result, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        ).encode("utf-8")
        result_artifact_id = self._store.put(
            organization_id=organization_id,
            content=result_content,
            media_type=SANDBOX_RESULT_MEDIA_TYPE,
            idempotency_key=f"{idempotency_key}:sandbox-result",
        )
        return {**result, "result_artifact_id": result_artifact_id, "cached": False}
