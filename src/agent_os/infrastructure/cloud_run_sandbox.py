"""Hosted Cloud Run Job adapter for secretless, ephemeral sandbox execution."""

from __future__ import annotations

import base64
from datetime import timedelta
import hashlib
import json
import re
import time
from typing import Any, Callable, Mapping

import google.auth
from google.api_core.exceptions import PreconditionFailed
from google.auth.transport.requests import AuthorizedSession, Request as GoogleAuthRequest
from google.cloud import storage
import requests

from agent_os.application.command_worker import FatalCommandError, RetryableCommandError
from agent_os.application.ports import ArtifactStore, SandboxRunner
from agent_os.entrypoints.sandbox_job import RESULT_FORMAT, SOURCE_FORMAT
from agent_os.infrastructure.docker_sandbox import (
    SANDBOX_RESULT_MEDIA_TYPE,
    SOURCE_BUNDLE_MEDIA_TYPE,
    _bundle_path,
)


OPERATION_MEDIA_TYPE = "application/vnd.agent-os.sandbox-operation+json"
HOSTED_BACKEND = "cloud-run-job-v1"
_PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_RESOURCE_ID = re.compile(r"^[a-z][a-z0-9-]{0,61}[a-z0-9]$")


class CloudRunJobSandboxRunner(SandboxRunner):
    """Run one bounded action in a separately administered Cloud Run Job.

    Tenant bytes cross the boundary only through generation-guarded, short-lived
    GCS signed URLs. The job identity itself has no control-plane permissions.
    """

    def __init__(
        self,
        artifact_store: ArtifactStore,
        *,
        project_id: str,
        region: str,
        job_name: str,
        bucket_name: str,
        signing_service_account_email: str,
        sandbox_revision: str,
        timeout_seconds: int = 300,
        maximum_files: int = 2_000,
        maximum_output_bytes: int = 1024 * 1024,
        maximum_workspace_bytes: int = 4 * 1024 * 1024,
        maximum_log_bytes: int = 64 * 1024,
        storage_client: Any | None = None,
        session: Any | None = None,
        signed_url_factory: Callable[[Any, str, int], str] | None = None,
        poll_seconds: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not _PROJECT_ID.fullmatch(project_id):
            raise ValueError("hosted sandbox project ID is invalid")
        if not _RESOURCE_ID.fullmatch(job_name):
            raise ValueError("hosted sandbox job name is invalid")
        if not region or len(region) > 64 or not re.fullmatch(r"[a-z0-9-]+", region):
            raise ValueError("hosted sandbox region is invalid")
        if not bucket_name or len(bucket_name) > 222:
            raise ValueError("hosted sandbox bucket is invalid")
        if not signing_service_account_email.endswith(".iam.gserviceaccount.com"):
            raise ValueError("hosted sandbox signing service account is invalid")
        if not re.search(r"@sha256:[0-9a-f]{64}$", sandbox_revision):
            raise ValueError("hosted sandbox revision must be an immutable image digest")
        if (
            timeout_seconds < 1 or timeout_seconds > 86_400
            or maximum_files < 1 or maximum_files > 20_000
            or maximum_output_bytes < 1 or maximum_output_bytes > 64 * 1024 * 1024
            or maximum_workspace_bytes < maximum_output_bytes
            or maximum_workspace_bytes > 128 * 1024 * 1024
            or maximum_log_bytes < 1 or maximum_log_bytes > 1024 * 1024
            or poll_seconds <= 0
        ):
            raise ValueError("hosted sandbox limits are invalid")
        self._store = artifact_store
        self._project_id = project_id
        self._region = region
        self._job_name = job_name
        self._bucket_name = bucket_name
        self._signing_service_account_email = signing_service_account_email
        self._sandbox_revision = sandbox_revision
        self._timeout_seconds = timeout_seconds
        self._maximum_files = maximum_files
        self._maximum_output_bytes = maximum_output_bytes
        self._maximum_workspace_bytes = maximum_workspace_bytes
        self._maximum_log_bytes = maximum_log_bytes
        self._poll_seconds = poll_seconds
        self._sleep = sleep
        self._credentials = None
        if storage_client is None or session is None or signed_url_factory is None:
            credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            self._credentials = credentials
        self._storage = storage_client or storage.Client(credentials=self._credentials)
        self._session = session or AuthorizedSession(self._credentials)
        self._signed_url_factory = signed_url_factory or self._signed_url

    def _signed_url(self, blob: Any, method: str, lifetime_seconds: int) -> str:
        if self._credentials is None:
            raise RuntimeError("hosted sandbox signing credentials are unavailable")
        self._credentials.refresh(GoogleAuthRequest())
        headers = {"x-goog-if-generation-match": "0"} if method == "PUT" else None
        return blob.generate_signed_url(
            version="v4",
            expiration=timedelta(seconds=lifetime_seconds),
            method=method,
            content_type="application/json" if method == "PUT" else None,
            generation=blob.generation if method == "GET" else None,
            headers=headers,
            credentials=self._credentials,
            service_account_email=self._signing_service_account_email,
            access_token=self._credentials.token,
        )

    def _source_content(self, organization_id: str, artifact_id: str) -> bytes:
        record = self._store.describe(organization_id, artifact_id)
        content = self._store.get(organization_id, artifact_id)
        if record is None or content is None:
            raise FatalCommandError("sandbox source artifact does not exist in this tenant")
        if record.get("media_type") != SOURCE_BUNDLE_MEDIA_TYPE:
            raise FatalCommandError("sandbox source must use the Agent OS source-bundle media type")
        if len(content) > self._maximum_workspace_bytes + 1024 * 1024:
            raise FatalCommandError("sandbox source bundle exceeds the transfer limit")
        try:
            raw = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FatalCommandError("sandbox source bundle is not valid JSON") from exc
        if not isinstance(raw, Mapping) or raw.get("format") != SOURCE_FORMAT:
            raise FatalCommandError("sandbox source bundle format is unsupported")
        if not isinstance(raw.get("files"), Mapping) or len(raw["files"]) > self._maximum_files:
            raise FatalCommandError("sandbox source bundle has an invalid file map")
        return content

    def _fingerprint(
        self,
        organization_id: str,
        artifact_id: str,
        command: tuple[str, ...],
        idempotency_key: str,
    ) -> str:
        content = json.dumps(
            {
                "tenant": organization_id,
                "source": artifact_id,
                "command": command,
                "idempotency_key": idempotency_key,
                "sandbox_revision": self._sandbox_revision,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return hashlib.sha256(content).hexdigest()

    def _cached_result(
        self,
        organization_id: str,
        idempotency_key: str,
        fingerprint: str,
    ) -> Mapping[str, Any] | None:
        record = self._store.find_by_idempotency_key(
            organization_id, f"{idempotency_key}:sandbox-result",
        )
        if record is None:
            return None
        content = self._store.get(organization_id, str(record["artifact_id"]))
        try:
            result = json.loads(content) if content is not None else None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FatalCommandError("hosted sandbox result artifact is corrupt") from exc
        if not isinstance(result, Mapping) or result.get("fingerprint") != fingerprint:
            raise FatalCommandError("sandbox idempotency key was reused with different inputs")
        return {**dict(result), "result_artifact_id": record["artifact_id"], "cached": True}

    def _put_input(self, blob: Any, content: bytes) -> None:
        try:
            blob.upload_from_string(
                content, content_type="application/json", if_generation_match=0, checksum="crc32c",
            )
        except PreconditionFailed:
            if blob.download_as_bytes() != content:
                raise FatalCommandError("sandbox input staging collision")
        blob.reload()

    def _operation(self, organization_id: str, idempotency_key: str, fingerprint: str) -> str | None:
        record = self._store.find_by_idempotency_key(
            organization_id, f"{idempotency_key}:sandbox-operation",
        )
        if record is None:
            return None
        content = self._store.get(organization_id, str(record["artifact_id"]))
        try:
            operation = json.loads(content) if content is not None else None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FatalCommandError("sandbox operation receipt is corrupt") from exc
        if not isinstance(operation, Mapping) or operation.get("fingerprint") != fingerprint:
            raise FatalCommandError("sandbox operation receipt does not match this execution")
        name = operation.get("operation_name")
        if not isinstance(name, str) or not name.startswith("projects/"):
            raise FatalCommandError("sandbox operation receipt has an invalid operation name")
        return name

    @staticmethod
    def _response_json(response: Any, operation: str) -> Mapping[str, Any]:
        try:
            response.raise_for_status()
            body = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise RetryableCommandError(f"Cloud Run sandbox {operation} request failed") from exc
        if not isinstance(body, Mapping):
            raise RetryableCommandError(f"Cloud Run sandbox {operation} returned invalid JSON")
        return body

    def _launch(
        self,
        *,
        input_url: str,
        output_url: str,
        command: tuple[str, ...],
        fingerprint: str,
    ) -> str:
        command_json = json.dumps(command, ensure_ascii=True, separators=(",", ":")).encode()
        command_b64 = base64.b64encode(command_json).decode()
        if len(command_b64) > 32_000:
            raise FatalCommandError("hosted sandbox command exceeds the job override limit")
        environment = {
            "AOS_SANDBOX_INPUT_URL": input_url,
            "AOS_SANDBOX_OUTPUT_URL": output_url,
            "AOS_SANDBOX_COMMAND_B64": command_b64,
            "AOS_SANDBOX_FINGERPRINT": fingerprint,
            "AOS_SANDBOX_TIMEOUT_SECONDS": str(self._timeout_seconds),
            "AOS_SANDBOX_MAX_FILES": str(self._maximum_files),
            "AOS_SANDBOX_MAX_OUTPUT_BYTES": str(self._maximum_output_bytes),
            "AOS_SANDBOX_MAX_WORKSPACE_BYTES": str(self._maximum_workspace_bytes),
            "AOS_SANDBOX_MAX_LOG_BYTES": str(self._maximum_log_bytes),
        }
        endpoint = (
            f"https://run.googleapis.com/v2/projects/{self._project_id}/locations/"
            f"{self._region}/jobs/{self._job_name}:run"
        )
        try:
            response = self._session.post(
                endpoint,
                json={"overrides": {
                    "containerOverrides": [{
                        "name": "sandbox", "env": [
                            {"name": key, "value": value} for key, value in environment.items()
                        ],
                    }],
                    "taskCount": 1,
                    "timeout": f"{self._timeout_seconds + 60}s",
                }},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise RetryableCommandError("Cloud Run sandbox launch did not complete") from exc
        body = self._response_json(response, "launch")
        name = body.get("name")
        if not isinstance(name, str) or not name.startswith("projects/"):
            raise RetryableCommandError("Cloud Run sandbox launch returned no operation name")
        return name

    def _poll(self, operation_name: str) -> bool:
        endpoint = f"https://run.googleapis.com/v2/{operation_name}"
        deadline = time.monotonic() + self._timeout_seconds + 180
        while time.monotonic() < deadline:
            try:
                response = self._session.get(endpoint, timeout=30)
            except requests.RequestException as exc:
                raise RetryableCommandError("Cloud Run sandbox status request did not complete") from exc
            body = self._response_json(response, "status")
            if body.get("done") is True:
                return not bool(body.get("error"))
            self._sleep(self._poll_seconds)
        raise RetryableCommandError("Cloud Run sandbox operation exceeded its completion deadline")

    def _validated_output(self, content: bytes, fingerprint: str) -> tuple[bytes, Mapping[str, Any]]:
        maximum_envelope = self._maximum_output_bytes + 2 * self._maximum_log_bytes + 1024 * 1024
        if len(content) > maximum_envelope:
            raise FatalCommandError("hosted sandbox result exceeds the evidence limit")
        try:
            envelope = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FatalCommandError("hosted sandbox result is not valid JSON") from exc
        if (
            not isinstance(envelope, Mapping)
            or envelope.get("format") != RESULT_FORMAT
            or envelope.get("fingerprint") != fingerprint
            or not isinstance(envelope.get("output_bundle"), Mapping)
        ):
            raise FatalCommandError("hosted sandbox result envelope is invalid")
        bundle = envelope["output_bundle"]
        files = bundle.get("files")
        if bundle.get("format") != SOURCE_FORMAT or not isinstance(files, Mapping):
            raise FatalCommandError("hosted sandbox output bundle is invalid")
        if len(files) > self._maximum_files:
            raise FatalCommandError("hosted sandbox output exceeds the file-count limit")
        total = 0
        for raw_path, specification in files.items():
            _bundle_path(str(raw_path))
            if not isinstance(specification, Mapping) or not isinstance(specification.get("content"), str):
                raise FatalCommandError("hosted sandbox output file is invalid")
            if specification.get("encoding") != "base64" or not isinstance(
                specification.get("executable", False), bool,
            ):
                raise FatalCommandError("hosted sandbox output encoding is invalid")
            try:
                decoded = base64.b64decode(specification["content"], validate=True)
            except ValueError as exc:
                raise FatalCommandError("hosted sandbox output contains invalid base64") from exc
            total += len(decoded)
            if total > self._maximum_output_bytes:
                raise FatalCommandError("hosted sandbox output exceeds the artifact byte limit")
        for field in ("stdout", "stderr"):
            if not isinstance(envelope.get(field), str) or len(envelope[field].encode()) > self._maximum_log_bytes * 4:
                raise FatalCommandError(f"hosted sandbox {field} evidence is invalid")
        if not isinstance(envelope.get("exit_code"), int) or not isinstance(envelope.get("timed_out"), bool):
            raise FatalCommandError("hosted sandbox status evidence is invalid")
        output_error = envelope.get("output_error")
        if output_error is not None and (
            not isinstance(output_error, str) or len(output_error.encode()) > 4_000
        ):
            raise FatalCommandError("hosted sandbox output-error evidence is invalid")
        canonical = json.dumps(
            bundle, allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True,
        ).encode()
        return canonical, envelope

    def _persist_result(
        self,
        organization_id: str,
        artifact_id: str,
        command: tuple[str, ...],
        idempotency_key: str,
        fingerprint: str,
        output_content: bytes,
    ) -> Mapping[str, Any]:
        bundle, envelope = self._validated_output(output_content, fingerprint)
        output_artifact_id = self._store.put(
            organization_id=organization_id,
            content=bundle,
            media_type=SOURCE_BUNDLE_MEDIA_TYPE,
            idempotency_key=f"{idempotency_key}:sandbox-output",
        )
        result = {
            "sandbox_backend": HOSTED_BACKEND,
            "sandbox_revision": self._sandbox_revision,
            "fingerprint": fingerprint,
            "source_artifact_id": artifact_id,
            "command": list(command),
            "output_artifact_id": output_artifact_id,
            "exit_code": envelope["exit_code"],
            "timed_out": envelope["timed_out"],
            "output_error": envelope.get("output_error"),
            "stdout": envelope["stdout"],
            "stderr": envelope["stderr"],
        }
        result_artifact_id = self._store.put(
            organization_id=organization_id,
            content=json.dumps(
                result, allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True,
            ).encode(),
            media_type=SANDBOX_RESULT_MEDIA_TYPE,
            idempotency_key=f"{idempotency_key}:sandbox-result",
        )
        return {**result, "result_artifact_id": result_artifact_id, "cached": False}

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
        fingerprint = self._fingerprint(organization_id, artifact_id, command, idempotency_key)
        cached = self._cached_result(organization_id, idempotency_key, fingerprint)
        if cached is not None:
            return cached
        source_content = self._source_content(organization_id, artifact_id)
        tenant_hash = hashlib.sha256(organization_id.encode()).hexdigest()[:24]
        prefix = f"temporary/sandbox/{tenant_hash}/{fingerprint}"
        bucket = self._storage.bucket(self._bucket_name)
        input_blob = bucket.blob(f"{prefix}/input.json")
        output_blob = bucket.blob(f"{prefix}/output.json")
        if output_blob.exists():
            return self._persist_result(
                organization_id, artifact_id, command, idempotency_key, fingerprint,
                output_blob.download_as_bytes(),
            )
        self._put_input(input_blob, source_content)
        signed_lifetime = min(7 * 24 * 60 * 60 - 1, self._timeout_seconds + 15 * 60)
        input_url = self._signed_url_factory(input_blob, "GET", signed_lifetime)
        output_url = self._signed_url_factory(output_blob, "PUT", signed_lifetime)
        operation_name = self._operation(organization_id, idempotency_key, fingerprint)
        if operation_name is not None:
            succeeded = self._poll(operation_name)
            if succeeded and output_blob.exists():
                return self._persist_result(
                    organization_id, artifact_id, command, idempotency_key, fingerprint,
                    output_blob.download_as_bytes(),
                )
        operation_name = self._launch(
            input_url=input_url,
            output_url=output_url,
            command=command,
            fingerprint=fingerprint,
        )
        self._store.put(
            organization_id=organization_id,
            content=json.dumps(
                {"fingerprint": fingerprint, "operation_name": operation_name},
                ensure_ascii=True, separators=(",", ":"), sort_keys=True,
            ).encode(),
            media_type=OPERATION_MEDIA_TYPE,
            idempotency_key=f"{idempotency_key}:sandbox-operation",
        )
        if not self._poll(operation_name):
            raise RetryableCommandError("Cloud Run sandbox execution failed before producing evidence")
        if not output_blob.exists():
            raise RetryableCommandError("Cloud Run sandbox completed without durable output evidence")
        return self._persist_result(
            organization_id, artifact_id, command, idempotency_key, fingerprint,
            output_blob.download_as_bytes(),
        )
