"""Secretless controller for one ephemeral hosted sandbox execution."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path, PurePosixPath
import resource
import signal
import subprocess
import tempfile
from threading import Thread
from typing import Any, Mapping
from urllib.parse import urlparse
from urllib.request import Request, urlopen


RESULT_FORMAT = "agent-os.sandbox-job-result.v1"
SOURCE_FORMAT = "agent-os.source-bundle.v1"
OUTPUT_REJECTED_EXIT_CODE = 65
CONTROLLER_ERROR_EXIT_CODE = 70
_IGNORED_OUTPUT_PARTS = {".git", ".pytest_cache", "__pycache__"}


def _positive_int(name: str, *, maximum: int) -> int:
    raw = os.environ.get(name, "")
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not 1 <= value <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return value


def _path(raw: object) -> PurePosixPath:
    if not isinstance(raw, str) or not raw or "\0" in raw or len(raw) > 512:
        raise ValueError("source bundle contains an invalid path")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("source bundle paths must be normalized and relative")
    return path


def _decode_file(specification: object) -> tuple[bytes, bool]:
    if not isinstance(specification, Mapping):
        raise ValueError("source bundle file specification must be an object")
    content = specification.get("content")
    executable = specification.get("executable", False)
    if not isinstance(content, str) or not isinstance(executable, bool):
        raise ValueError("source bundle file content or mode is invalid")
    if specification.get("encoding") == "utf-8":
        return content.encode("utf-8"), executable
    if specification.get("encoding") == "base64":
        try:
            return base64.b64decode(content, validate=True), executable
        except ValueError as exc:
            raise ValueError("source bundle contains invalid base64") from exc
    raise ValueError("source bundle file encoding is unsupported")


def _materialize(
    root: Path,
    bundle: object,
    *,
    maximum_files: int,
    maximum_bytes: int,
    owner_uid: int,
    owner_gid: int,
) -> None:
    if not isinstance(bundle, Mapping) or bundle.get("format") != SOURCE_FORMAT:
        raise ValueError("source bundle format is unsupported")
    files = bundle.get("files")
    if not isinstance(files, Mapping) or len(files) > maximum_files:
        raise ValueError("source bundle has an invalid file map")
    total = 0
    for raw_path, specification in sorted(files.items()):
        relative = _path(raw_path)
        content, executable = _decode_file(specification)
        total += len(content)
        if total > maximum_bytes:
            raise ValueError("source bundle exceeds the workspace byte limit")
        target = root.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        target.chmod(0o700 if executable else 0o600)
    root.chmod(0o700)
    if os.geteuid() == 0:
        for candidate in (root, *root.rglob("*")):
            os.chown(candidate, owner_uid, owner_gid, follow_symlinks=False)


def _package(root: Path, *, maximum_files: int, maximum_bytes: int) -> Mapping[str, Any]:
    files: dict[str, Mapping[str, Any]] = {}
    total = 0
    for candidate in sorted(root.rglob("*")):
        relative = candidate.relative_to(root)
        if any(part in _IGNORED_OUTPUT_PARTS for part in relative.parts):
            continue
        if candidate.is_symlink():
            raise ValueError("sandbox output may not contain symbolic links")
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise ValueError("sandbox output may contain only regular files")
        if len(files) >= maximum_files:
            raise ValueError("sandbox output exceeds the file-count limit")
        content = candidate.read_bytes()
        total += len(content)
        if total > maximum_bytes:
            raise ValueError("sandbox output exceeds the artifact byte limit")
        files[relative.as_posix()] = {
            "encoding": "base64",
            "content": base64.b64encode(content).decode("ascii"),
            "executable": bool(candidate.stat().st_mode & 0o111),
        }
    return {"format": SOURCE_FORMAT, "files": files}


def _capture(pipe, target: bytearray, maximum_bytes: int) -> None:
    try:
        while True:
            chunk = pipe.read(8192)
            if not chunk:
                return
            target.extend(chunk)
            overflow = len(target) - maximum_bytes
            if overflow > 0:
                del target[:overflow]
    finally:
        pipe.close()


def _validated_command(raw: object) -> tuple[str, ...]:
    if (
        not isinstance(raw, list) or not raw or len(raw) > 64
        or any(not isinstance(item, str) or not item or "\0" in item or len(item) > 4096 for item in raw)
    ):
        raise ValueError("sandbox command must be bounded direct argv")
    return tuple(raw)


def execute_bundle(
    bundle: object,
    command: tuple[str, ...],
    *,
    fingerprint: str,
    timeout_seconds: int,
    maximum_files: int,
    maximum_output_bytes: int,
    maximum_workspace_bytes: int,
    maximum_log_bytes: int,
    run_uid: int = 65532,
    run_gid: int = 65532,
    work_root: str | None = None,
) -> Mapping[str, Any]:
    """Run untrusted argv as an unprivileged child and return bounded evidence."""

    stdout = bytearray()
    stderr = bytearray()
    timed_out = False
    output_error: str | None = None
    with tempfile.TemporaryDirectory(
        prefix="agent-os-hosted-sandbox-", dir=work_root,
    ) as temporary:
        workspace = Path(temporary) / "workspace"
        workspace.mkdir(mode=0o700)
        _materialize(
            workspace,
            bundle,
            maximum_files=maximum_files,
            maximum_bytes=maximum_workspace_bytes,
            owner_uid=run_uid,
            owner_gid=run_gid,
        )
        options: dict[str, Any] = {
            "cwd": workspace,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": {
                "HOME": str(workspace),
                "TMPDIR": str(workspace),
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            "start_new_session": True,
        }
        if os.geteuid() == 0:
            def child_limits() -> None:
                resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
                resource.setrlimit(resource.RLIMIT_NOFILE, (1024, 1024))
                resource.setrlimit(resource.RLIMIT_NPROC, (128, 128))
                resource.setrlimit(
                    resource.RLIMIT_FSIZE,
                    (maximum_workspace_bytes, maximum_workspace_bytes),
                )

            options.update(
                user=run_uid,
                group=run_gid,
                extra_groups=(),
                umask=0o077,
                preexec_fn=child_limits,
            )
        process = subprocess.Popen(command, **options)
        if process.stdout is None or process.stderr is None:
            raise RuntimeError("sandbox execution pipes were not created")
        readers = (
            Thread(target=_capture, args=(process.stdout, stdout, maximum_log_bytes), daemon=True),
            Thread(target=_capture, args=(process.stderr, stderr, maximum_log_bytes), daemon=True),
        )
        for reader in readers:
            reader.start()
        try:
            exit_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGKILL)
            exit_code = process.wait(timeout=10)
            exit_code = 124
        for reader in readers:
            reader.join(timeout=5)
        try:
            output_bundle = _package(
                workspace, maximum_files=maximum_files, maximum_bytes=maximum_output_bytes,
            )
        except ValueError as exc:
            output_bundle = {"format": SOURCE_FORMAT, "files": {}}
            output_error = str(exc)[:1000]
            if exit_code == 0:
                exit_code = OUTPUT_REJECTED_EXIT_CODE
    return {
        "format": RESULT_FORMAT,
        "fingerprint": fingerprint,
        "output_bundle": output_bundle,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "output_error": output_error,
        "stdout": bytes(stdout).decode("utf-8", errors="replace"),
        "stderr": bytes(stderr).decode("utf-8", errors="replace"),
    }


def _validated_storage_url(raw: str) -> str:
    parsed = urlparse(raw)
    if parsed.scheme != "https" or parsed.hostname != "storage.googleapis.com":
        raise ValueError("sandbox transfer URL must be an HTTPS Cloud Storage URL")
    return raw


def _download_json(url: str, maximum_bytes: int) -> object:
    with urlopen(Request(_validated_storage_url(url), method="GET"), timeout=30) as response:
        content = response.read(maximum_bytes + 1)
    if len(content) > maximum_bytes:
        raise ValueError("sandbox input exceeds the transfer byte limit")
    return json.loads(content)


def _upload_json(url: str, value: Mapping[str, Any], maximum_bytes: int) -> None:
    content = json.dumps(
        value, allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    if len(content) > maximum_bytes:
        raise ValueError("sandbox result exceeds the transfer byte limit")
    request = Request(
        _validated_storage_url(url), data=content, method="PUT",
        headers={"Content-Type": "application/json", "x-goog-if-generation-match": "0"},
    )
    with urlopen(request, timeout=30) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError("sandbox result upload failed")


def main() -> int:
    """Fetch one input, erase transfer credentials, execute, and upload evidence."""

    try:
        input_url = os.environ["AOS_SANDBOX_INPUT_URL"]
        output_url = os.environ["AOS_SANDBOX_OUTPUT_URL"]
        fingerprint = os.environ["AOS_SANDBOX_FINGERPRINT"]
        if len(fingerprint) != 64 or any(char not in "0123456789abcdef" for char in fingerprint):
            raise ValueError("sandbox fingerprint is invalid")
        timeout_seconds = _positive_int("AOS_SANDBOX_TIMEOUT_SECONDS", maximum=86_400)
        maximum_files = _positive_int("AOS_SANDBOX_MAX_FILES", maximum=20_000)
        maximum_output_bytes = _positive_int("AOS_SANDBOX_MAX_OUTPUT_BYTES", maximum=64 * 1024 * 1024)
        maximum_workspace_bytes = _positive_int("AOS_SANDBOX_MAX_WORKSPACE_BYTES", maximum=128 * 1024 * 1024)
        maximum_log_bytes = _positive_int("AOS_SANDBOX_MAX_LOG_BYTES", maximum=1024 * 1024)
        work_root = os.environ.get("AOS_SANDBOX_WORK_ROOT", "")
        if work_root != "/sandbox-work":
            raise ValueError("sandbox work root must be the bounded mounted volume")
        maximum_transfer_bytes = maximum_workspace_bytes + 1024 * 1024
        bundle = _download_json(input_url, maximum_transfer_bytes)
        command_content = base64.b64decode(
            os.environ["AOS_SANDBOX_COMMAND_B64"], validate=True,
        )
        if len(command_content) > 24 * 1024:
            raise ValueError("sandbox command encoding is too large")
        command = _validated_command(json.loads(command_content))
        # The untrusted child runs as another UID. Remove signed URLs and all
        # ambient configuration before it starts; the job identity has no IAM roles.
        os.environ.clear()
        result = execute_bundle(
            bundle,
            command,
            fingerprint=fingerprint,
            timeout_seconds=timeout_seconds,
            maximum_files=maximum_files,
            maximum_output_bytes=maximum_output_bytes,
            maximum_workspace_bytes=maximum_workspace_bytes,
            maximum_log_bytes=maximum_log_bytes,
            work_root=work_root,
        )
        _upload_json(output_url, result, maximum_output_bytes + 2 * maximum_log_bytes + 1024 * 1024)
        return 0
    except Exception as exc:
        print(f"sandbox controller error: {type(exc).__name__}: {str(exc)[:1000]}", flush=True)
        return CONTROLLER_ERROR_EXIT_CODE


if __name__ == "__main__":
    raise SystemExit(main())
