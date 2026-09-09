"""Build verified source and promote digest-pinned generated Cloud Run services."""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import re
import shlex
import tarfile
import time
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

import google.auth
from google.api_core.exceptions import GoogleAPICallError, PreconditionFailed
from google.auth.transport.requests import AuthorizedSession
from google.cloud import storage
import requests

from agent_os.application.command_worker import FatalCommandError, RetryableCommandError
from agent_os.application.ports import ApplicationDeployer, ArtifactStore
from agent_os.infrastructure.docker_sandbox import (
    SOURCE_BUNDLE_MEDIA_TYPE,
    _bundle_path,
)
from agent_os.infrastructure.gcs_static_sites import contains_credential_like_material


SERVICE_RELEASE_RECEIPT_MEDIA_TYPE = "application/vnd.agent-os.service-release+json"
SERVICE_BUILD_OPERATION_MEDIA_TYPE = "application/vnd.agent-os.service-build-operation+json"
SERVICE_FAILURE_RECEIPT_MEDIA_TYPE = "application/vnd.agent-os.service-release-failure+json"
SERVICE_DEPLOYMENT_BACKEND = "cloud-run-service-v1"
_PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_RESOURCE_ID = re.compile(r"^[a-z][a-z0-9-]{0,61}[a-z0-9]$")
_APP_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_SOURCE_PATH = re.compile(r"^[A-Za-z0-9._@+/-]{1,512}$")
_PINNED_IMAGE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_SERVICE_ACCOUNT_EMAIL = re.compile(
    r"^[a-z][a-z0-9-]{4,28}[a-z0-9]@"
    r"(?P<project>[a-z][a-z0-9-]{4,28}[a-z0-9])\.iam\.gserviceaccount\.com$"
)
_HEALTH_PATH = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]{0,255}$")
_BUILD_ACTIVE = frozenset({"STATUS_UNKNOWN", "QUEUED", "WORKING", "PENDING"})
_MAX_FILES = 2_000
_MAX_BYTES = 16 * 1024 * 1024


def _credential_path(path: str) -> bool:
    lowered = f"/{path.lower()}"
    basename = lowered.rsplit("/", 1)[-1]
    return (
        basename == ".env"
        or basename.startswith(".env.")
        or basename.endswith((".pem", ".key"))
        or "credentials" in basename
        or basename.startswith("id_rsa")
        or "/keys/" in lowered
        or "/secrets/" in lowered
        or lowered.startswith(("/.aws/", "/.ssh/"))
    )


def _validate_dockerfile(content: bytes) -> None:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FatalCommandError("service Dockerfile must be UTF-8") from exc
    if len(content) > 128 * 1024:
        raise FatalCommandError("service Dockerfile exceeds 128 KiB")
    logical_lines: list[str] = []
    pending = ""
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        pending = f"{pending}{stripped}"
        if pending.endswith("\\"):
            pending = pending[:-1] + " "
            continue
        logical_lines.append(pending)
        pending = ""
    if pending:
        logical_lines.append(pending)
    images = []
    final_user = None
    for line in logical_lines:
        try:
            words = shlex.split(line, comments=True, posix=True)
        except ValueError as exc:
            raise FatalCommandError("service Dockerfile contains invalid syntax") from exc
        if not words:
            continue
        instruction = words[0].upper()
        if instruction == "FROM":
            arguments = [word for word in words[1:] if not word.startswith("--")]
            if not arguments:
                raise FatalCommandError("service Dockerfile FROM instruction is invalid")
            images.append(arguments[0])
            final_user = None
        elif instruction == "USER":
            final_user = words[1] if len(words) == 2 else None
    if not images:
        raise FatalCommandError("service source requires a Dockerfile FROM instruction")
    if any(image.lower() != "scratch" and not _PINNED_IMAGE.fullmatch(image) for image in images):
        raise FatalCommandError("every service Dockerfile base image must be pinned by sha256 digest")
    if final_user is None or not re.fullmatch(r"[1-9][0-9]{0,9}(?::[1-9][0-9]{0,9})?", final_user):
        raise FatalCommandError("service Dockerfile final stage must declare a numeric non-root USER")


def _deterministic_archive(files: Mapping[str, tuple[bytes, bool]]) -> bytes:
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for path, (content, executable) in sorted(files.items()):
                info = tarfile.TarInfo(path)
                info.size = len(content)
                info.mode = 0o755 if executable else 0o644
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                info.mtime = 0
                archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


class CloudRunServiceDeployer(ApplicationDeployer):
    """Build untrusted source with a narrow identity and promote an immutable image."""

    def __init__(
        self,
        artifact_store: ArtifactStore,
        *,
        project_id: str,
        region: str,
        source_bucket: str,
        repository: str,
        build_service_account_email: str,
        runtime_service_account_email: str,
        builder_image: str,
        maximum_files: int = _MAX_FILES,
        maximum_bytes: int = _MAX_BYTES,
        maximum_instances: int = 10,
        build_timeout_seconds: int = 1_200,
        deploy_timeout_seconds: int = 600,
        request_timeout_seconds: float = 30,
        poll_seconds: float = 2,
        storage_client: Any | None = None,
        session: Any | None = None,
        public_session: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not _PROJECT_ID.fullmatch(project_id):
            raise ValueError("generated-app project ID is invalid")
        if not region or len(region) > 64 or not re.fullmatch(r"[a-z0-9-]+", region):
            raise ValueError("generated-app region is invalid")
        if not _RESOURCE_ID.fullmatch(repository):
            raise ValueError("generated-app repository is invalid")
        if not source_bucket or len(source_bucket) > 222:
            raise ValueError("generated-app source bucket is invalid")
        for email in (build_service_account_email, runtime_service_account_email):
            account = _SERVICE_ACCOUNT_EMAIL.fullmatch(email)
            if account is None or account.group("project") != project_id:
                raise ValueError(
                    "generated-app service accounts must belong to the generated-app project"
                )
        if not _PINNED_IMAGE.fullmatch(builder_image):
            raise ValueError("generated-app builder image must be pinned by sha256 digest")
        if (
            not 1 <= maximum_files <= 20_000
            or not 1 <= maximum_bytes <= 64 * 1024 * 1024
            or not 1 <= maximum_instances <= 100
            or not 60 <= build_timeout_seconds <= 3_600
            or not 60 <= deploy_timeout_seconds <= 3_600
            or not 0 < request_timeout_seconds <= 120
            or poll_seconds <= 0
        ):
            raise ValueError("generated-app deployment limits are invalid")
        self._store = artifact_store
        self._project_id = project_id
        self._region = region
        self._source_bucket = source_bucket
        self._repository = repository
        self._build_service_account = build_service_account_email
        self._runtime_service_account = runtime_service_account_email
        self._builder_image = builder_image
        self._maximum_files = maximum_files
        self._maximum_bytes = maximum_bytes
        self._maximum_instances = maximum_instances
        self._build_timeout_seconds = build_timeout_seconds
        self._deploy_timeout_seconds = deploy_timeout_seconds
        self._request_timeout_seconds = request_timeout_seconds
        self._poll_seconds = poll_seconds
        self._sleep = sleep
        self._monotonic = monotonic
        credentials = None
        if storage_client is None or session is None:
            credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
        self._storage = storage_client or storage.Client(credentials=credentials)
        self._session = session or AuthorizedSession(credentials)
        self._public_session = public_session or requests.Session()

    def _source(self, organization_id: str, artifact_id: str) -> tuple[bytes, bytes]:
        record = self._store.describe(organization_id, artifact_id)
        content = self._store.get(organization_id, artifact_id)
        if record is None or content is None:
            raise FatalCommandError("service source artifact does not exist in this tenant")
        if record.get("media_type") != SOURCE_BUNDLE_MEDIA_TYPE:
            raise FatalCommandError("service source must be an Agent OS source bundle")
        if len(content) > self._maximum_bytes * 2 + 1024 * 1024:
            raise FatalCommandError("service source bundle exceeds the transfer limit")
        try:
            bundle = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FatalCommandError("service source bundle is not valid JSON") from exc
        raw_files = bundle.get("files") if isinstance(bundle, Mapping) else None
        if (
            not isinstance(bundle, Mapping)
            or bundle.get("format") != "agent-os.source-bundle.v1"
            or not isinstance(raw_files, Mapping)
            or not 1 <= len(raw_files) <= self._maximum_files
        ):
            raise FatalCommandError("service source bundle format is unsupported")
        files: dict[str, tuple[bytes, bool]] = {}
        total = 0
        for raw_path, specification in sorted(raw_files.items()):
            path = _bundle_path(str(raw_path)).as_posix()
            if not _SOURCE_PATH.fullmatch(path):
                raise FatalCommandError("service source paths contain unsupported characters")
            if _credential_path(path):
                raise FatalCommandError("service source contains a credential-bearing path")
            if not isinstance(specification, Mapping):
                raise FatalCommandError("service source file specification is invalid")
            raw_value = specification.get("content")
            executable = specification.get("executable", False)
            if not isinstance(raw_value, str) or not isinstance(executable, bool):
                raise FatalCommandError("service source file content or mode is invalid")
            try:
                if specification.get("encoding") == "utf-8":
                    value = raw_value.encode()
                elif specification.get("encoding") == "base64":
                    value = base64.b64decode(raw_value, validate=True)
                else:
                    raise FatalCommandError("service source file encoding is unsupported")
            except ValueError as exc:
                raise FatalCommandError("service source contains invalid base64") from exc
            total += len(value)
            if total > self._maximum_bytes:
                raise FatalCommandError("service source exceeds the byte limit")
            if contains_credential_like_material(value):
                raise FatalCommandError("service source contains credential-like material")
            files[path] = (value, executable)
        dockerfile = files.get("Dockerfile")
        if dockerfile is None:
            raise FatalCommandError("service source requires a root Dockerfile")
        _validate_dockerfile(dockerfile[0])
        return content, _deterministic_archive(files)

    @staticmethod
    def _json_response(response: Any, subject: str) -> Mapping[str, Any]:
        try:
            response.raise_for_status()
            value = response.json()
        except requests.HTTPError as exc:
            status = getattr(response, "status_code", None)
            if isinstance(status, int) and 400 <= status < 500 and status not in {408, 409, 429}:
                raise FatalCommandError(
                    f"generated-app {subject} was rejected with HTTP {status}"
                ) from exc
            raise RetryableCommandError(f"generated-app {subject} request failed") from exc
        except (requests.RequestException, ValueError) as exc:
            raise RetryableCommandError(f"generated-app {subject} request failed") from exc
        if not isinstance(value, Mapping):
            raise RetryableCommandError(f"generated-app {subject} returned invalid JSON")
        return value

    def _fingerprint(
        self, organization_id: str, app_slug: str, canonical_source: bytes,
    ) -> str:
        contract = json.dumps({
            "format": SERVICE_DEPLOYMENT_BACKEND,
            "organization_id": organization_id,
            "app_slug": app_slug,
            "source_digest": hashlib.sha256(canonical_source).hexdigest(),
            "project_id": self._project_id,
            "region": self._region,
            "repository": self._repository,
            "builder_image": self._builder_image,
            "runtime_service_account": self._runtime_service_account,
            "maximum_instances": self._maximum_instances,
        }, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
        return hashlib.sha256(contract).hexdigest()

    def _cached(
        self, organization_id: str, idempotency_key: str, *, artifact_id: str,
        app_slug: str, health_path: str, fingerprint: str,
    ) -> Mapping[str, Any] | None:
        record = self._store.find_by_idempotency_key(
            organization_id, f"{idempotency_key}:service-release-receipt",
        )
        if record is None:
            return None
        content = self._store.get(organization_id, str(record["artifact_id"]))
        try:
            receipt = json.loads(content) if content is not None else None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FatalCommandError("service deployment receipt is corrupt") from exc
        if (
            not isinstance(receipt, Mapping)
            or receipt.get("source_artifact_id") != artifact_id
            or receipt.get("app_slug") != app_slug
            or receipt.get("health_path") != health_path
            or receipt.get("fingerprint") != fingerprint
        ):
            raise FatalCommandError("service deployment idempotency key was reused with different input")
        return {**dict(receipt), "receipt_artifact_id": record["artifact_id"], "cached": True}

    def _stage_source(self, organization_id: str, fingerprint: str, archive: bytes) -> tuple[str, int]:
        tenant_hash = hashlib.sha256(organization_id.encode()).hexdigest()[:24]
        object_name = f"temporary/app-build-sources/{tenant_hash}/{fingerprint}.tar.gz"
        blob = self._storage.bucket(self._source_bucket).blob(object_name)
        try:
            blob.upload_from_string(
                archive,
                content_type="application/gzip",
                if_generation_match=0,
                checksum="crc32c",
                timeout=self._request_timeout_seconds,
            )
        except PreconditionFailed:
            try:
                if blob.download_as_bytes(
                    checksum="crc32c", timeout=self._request_timeout_seconds,
                ) != archive:
                    raise FatalCommandError("service build source staging collision")
            except FatalCommandError:
                raise
            except Exception as exc:
                raise RetryableCommandError("service build source collision could not be verified") from exc
        except GoogleAPICallError as exc:
            raise RetryableCommandError("service build source upload failed") from exc
        try:
            blob.reload(timeout=self._request_timeout_seconds)
        except GoogleAPICallError as exc:
            raise RetryableCommandError("service build source generation lookup failed") from exc
        generation = blob.generation
        if not isinstance(generation, int) or generation < 1:
            raise RetryableCommandError("service build source has no immutable generation")
        return object_name, generation

    @property
    def _build_collection(self) -> str:
        return (
            f"https://cloudbuild.googleapis.com/v1/projects/{self._project_id}/"
            f"locations/{self._region}/builds"
        )

    def _find_build(self, build_tag: str) -> Mapping[str, Any] | None:
        try:
            response = self._session.get(
                self._build_collection,
                params={"filter": f'tags="{build_tag}"', "pageSize": 10},
                timeout=self._request_timeout_seconds,
            )
        except requests.RequestException as exc:
            raise RetryableCommandError("generated-app build reconciliation did not complete") from exc
        body = self._json_response(response, "build reconciliation")
        builds = body.get("builds", [])
        if not isinstance(builds, list):
            raise RetryableCommandError("generated-app build reconciliation returned invalid builds")
        matches = [item for item in builds if isinstance(item, Mapping) and build_tag in item.get("tags", [])]
        successful = [item for item in matches if item.get("status") == "SUCCESS"]
        active = [item for item in matches if item.get("status") in _BUILD_ACTIVE]
        # A failed build is durable evidence, not a reusable deployment input. A
        # later workflow action may retry the same source with a fresh build.
        candidates = successful or active
        return None if not candidates else max(candidates, key=lambda item: str(item.get("createTime", "")))

    def _launch_build(
        self, *, object_name: str, generation: int, build_tag: str, image_tag: str,
    ) -> Mapping[str, Any]:
        body = {
            "source": {"storageSource": {
                "bucket": self._source_bucket,
                "object": object_name,
                "generation": str(generation),
            }},
            "steps": [{
                "name": self._builder_image,
                "args": ["build", "--pull", "--tag", image_tag, "."],
            }],
            "images": [image_tag],
            "tags": [build_tag],
            "timeout": f"{self._build_timeout_seconds}s",
            "queueTtl": "300s",
            "options": {"logging": "CLOUD_LOGGING_ONLY"},
            "serviceAccount": (
                f"projects/{self._project_id}/serviceAccounts/{self._build_service_account}"
            ),
        }
        try:
            response = self._session.post(
                self._build_collection, json=body, timeout=self._request_timeout_seconds,
            )
        except requests.RequestException as exc:
            raise RetryableCommandError("generated-app build launch did not complete") from exc
        operation = self._json_response(response, "build launch")
        build = operation.get("metadata", {}).get("build", {})
        if not isinstance(build, Mapping) or not isinstance(build.get("id"), str):
            raise RetryableCommandError("generated-app build launch returned no build identity")
        return build

    def _record_build(
        self, organization_id: str, idempotency_key: str, fingerprint: str,
        build: Mapping[str, Any],
    ) -> tuple[str, str]:
        build_id = build.get("id")
        if not isinstance(build_id, str) or not build_id:
            raise RetryableCommandError("generated-app build has no identity")
        operation = {"fingerprint": fingerprint, "build_id": build_id}
        try:
            artifact_id = self._store.put(
                organization_id=organization_id,
                content=json.dumps(operation, separators=(",", ":"), sort_keys=True).encode(),
                media_type=SERVICE_BUILD_OPERATION_MEDIA_TYPE,
                idempotency_key=f"{idempotency_key}:service-build-operation",
            )
        except ValueError:
            # Another worker can win the idempotency-key insert after both
            # replicas reconciled the build tag. Adopt that durable winner.
            winner = self._recorded_build_id(
                organization_id, idempotency_key, fingerprint,
            )
            record = self._store.find_by_idempotency_key(
                organization_id, f"{idempotency_key}:service-build-operation",
            )
            if winner is None or record is None:
                raise
            return str(record["artifact_id"]), winner
        return artifact_id, build_id

    def _recorded_build_id(
        self, organization_id: str, idempotency_key: str, fingerprint: str,
    ) -> str | None:
        record = self._store.find_by_idempotency_key(
            organization_id, f"{idempotency_key}:service-build-operation",
        )
        if record is None:
            return None
        content = self._store.get(organization_id, str(record["artifact_id"]))
        try:
            operation = json.loads(content) if content is not None else None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FatalCommandError("generated-app build operation evidence is corrupt") from exc
        if (
            not isinstance(operation, Mapping)
            or operation.get("fingerprint") != fingerprint
            or not isinstance(operation.get("build_id"), str)
            or not operation["build_id"]
        ):
            raise FatalCommandError("generated-app build operation does not match this release")
        return operation["build_id"]

    def _get_build(self, build_id: str) -> Mapping[str, Any]:
        try:
            response = self._session.get(
                f"{self._build_collection}/{build_id}",
                timeout=self._request_timeout_seconds,
            )
        except requests.RequestException as exc:
            raise RetryableCommandError("generated-app build lookup failed") from exc
        build = self._json_response(response, "build lookup")
        if build.get("id") != build_id:
            raise RetryableCommandError("generated-app build lookup returned the wrong identity")
        return build

    def _poll_build(self, build: Mapping[str, Any]) -> Mapping[str, Any]:
        build_id = build.get("id")
        if not isinstance(build_id, str) or not build_id:
            raise RetryableCommandError("generated-app build has no identity")
        deadline = self._monotonic() + self._build_timeout_seconds + 180
        current = build
        while self._monotonic() < deadline:
            status = current.get("status")
            if status == "SUCCESS":
                return current
            if status not in _BUILD_ACTIVE and status is not None:
                detail = str(current.get("statusDetail") or status)[:500]
                raise FatalCommandError(f"generated-app image build failed: {detail}")
            try:
                response = self._session.get(
                    f"{self._build_collection}/{build_id}",
                    timeout=self._request_timeout_seconds,
                )
            except requests.RequestException as exc:
                raise RetryableCommandError("generated-app build status request failed") from exc
            current = self._json_response(response, "build status")
            if current.get("status") in _BUILD_ACTIVE:
                self._sleep(self._poll_seconds)
        raise RetryableCommandError("generated-app build exceeded its completion deadline")

    @staticmethod
    def _image_digest(build: Mapping[str, Any], image_tag: str) -> str:
        results = build.get("results")
        images = results.get("images") if isinstance(results, Mapping) else None
        if not isinstance(images, list):
            raise RetryableCommandError("generated-app build returned no image results")
        for image in images:
            if not isinstance(image, Mapping):
                continue
            if image.get("name") == image_tag and isinstance(image.get("digest"), str):
                digest = image["digest"]
                if re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                    return digest
        raise RetryableCommandError("generated-app build returned no matching image digest")

    @property
    def _service_collection(self) -> str:
        return (
            f"https://run.googleapis.com/v2/projects/{self._project_id}/"
            f"locations/{self._region}/services"
        )

    def _service_name(self, organization_id: str, app_slug: str) -> str:
        digest = hashlib.sha256(f"service:v1:{organization_id}:{app_slug}".encode()).hexdigest()
        return f"aos-{digest[:40]}"

    def _promote(self, service_name: str, revision: str, image: str) -> None:
        resource_name = (
            f"projects/{self._project_id}/locations/{self._region}/services/{service_name}"
        )
        body = {
            "name": resource_name,
            "description": "Generated and governed by Agent OS",
            "labels": {"agent-os-managed": "true"},
            "ingress": "INGRESS_TRAFFIC_ALL",
            "invokerIamDisabled": True,
            "template": {
                "revision": revision,
                "serviceAccount": self._runtime_service_account,
                "timeout": "300s",
                "maxInstanceRequestConcurrency": 80,
                "scaling": {"minInstanceCount": 0, "maxInstanceCount": self._maximum_instances},
                "containers": [{
                    "name": "application",
                    "image": image,
                    "ports": [{"name": "http1", "containerPort": 8080}],
                    "resources": {
                        "limits": {"cpu": "1", "memory": "512Mi"},
                        "cpuIdle": True,
                        "startupCpuBoost": True,
                    },
                }],
            },
            "traffic": [{
                "type": "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST", "percent": 100,
            }],
        }
        try:
            response = self._session.patch(
                f"{self._service_collection}/{service_name}",
                params={
                    "allowMissing": "true",
                    "updateMask": "description,labels,ingress,invoker_iam_disabled,template,traffic",
                },
                json=body,
                timeout=self._request_timeout_seconds,
            )
        except requests.RequestException as exc:
            raise RetryableCommandError("generated-app service promotion did not complete") from exc
        self._json_response(response, "service promotion")

    def _existing_service(self, service_name: str) -> Mapping[str, Any] | None:
        try:
            response = self._session.get(
                f"{self._service_collection}/{service_name}",
                timeout=self._request_timeout_seconds,
            )
        except requests.RequestException as exc:
            raise RetryableCommandError("generated-app prior service lookup failed") from exc
        if getattr(response, "status_code", None) == 404:
            return None
        return self._json_response(response, "prior service lookup")

    def _rollback(self, service_name: str, previous_revision: str) -> None:
        resource_name = (
            f"projects/{self._project_id}/locations/{self._region}/services/{service_name}"
        )
        short_revision = previous_revision.rsplit("/", 1)[-1]
        try:
            response = self._session.patch(
                f"{self._service_collection}/{service_name}",
                params={"updateMask": "traffic"},
                json={
                    "name": resource_name,
                    "traffic": [{
                        "type": "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION",
                        "revision": short_revision,
                        "percent": 100,
                    }],
                },
                timeout=self._request_timeout_seconds,
            )
        except requests.RequestException as exc:
            raise RetryableCommandError("generated-app rollback request failed") from exc
        self._json_response(response, "rollback")
        deadline = self._monotonic() + self._deploy_timeout_seconds
        while self._monotonic() < deadline:
            service = self._existing_service(service_name)
            if service is None:
                raise RetryableCommandError("generated-app service disappeared during rollback")
            statuses = service.get("trafficStatuses", [])
            if service.get("reconciling") is not True and isinstance(statuses, list) and any(
                isinstance(status, Mapping)
                and str(status.get("revision", "")).rsplit("/", 1)[-1] == short_revision
                and status.get("percent") == 100
                for status in statuses
            ):
                return
            self._sleep(self._poll_seconds)
        raise RetryableCommandError("generated-app rollback exceeded its readiness deadline")

    def _record_failed_release(
        self,
        organization_id: str,
        idempotency_key: str,
        *,
        artifact_id: str,
        app_slug: str,
        fingerprint: str,
        revision: str,
        previous_revision: str | None,
        reason: str,
        rolled_back: bool,
    ) -> str:
        failure = {
            "kind": "cloud_run_service_failure",
            "deployment_backend": SERVICE_DEPLOYMENT_BACKEND,
            "source_artifact_id": artifact_id,
            "app_slug": app_slug,
            "fingerprint": fingerprint,
            "revision": revision,
            "previous_revision": previous_revision,
            "reason": reason[:2_000],
            "rolled_back": rolled_back,
        }
        return self._store.put(
            organization_id=organization_id,
            content=json.dumps(failure, separators=(",", ":"), sort_keys=True).encode(),
            media_type=SERVICE_FAILURE_RECEIPT_MEDIA_TYPE,
            idempotency_key=f"{idempotency_key}:service-failed-release",
        )

    def _poll_service(self, service_name: str, revision: str) -> Mapping[str, Any]:
        deadline = self._monotonic() + self._deploy_timeout_seconds
        endpoint = f"{self._service_collection}/{service_name}"
        while self._monotonic() < deadline:
            try:
                response = self._session.get(endpoint, timeout=self._request_timeout_seconds)
            except requests.RequestException as exc:
                raise RetryableCommandError("generated-app service status request failed") from exc
            service = self._json_response(response, "service status")
            if service.get("reconciling") is True:
                self._sleep(self._poll_seconds)
                continue
            terminal = service.get("terminalCondition")
            ready = service.get("latestReadyRevision")
            if (
                isinstance(terminal, Mapping)
                and terminal.get("state") == "CONDITION_SUCCEEDED"
                and isinstance(ready, str)
                and ready.rsplit("/", 1)[-1] == revision
            ):
                return service
            message = (
                str(terminal.get("message") or terminal.get("reason") or "not ready")
                if isinstance(terminal, Mapping) else "not ready"
            )
            raise FatalCommandError(f"generated-app service failed to become ready: {message[:500]}")
        raise RetryableCommandError("generated-app service exceeded its readiness deadline")

    def _health(self, public_url: str, health_path: str) -> None:
        parsed = urlparse(public_url)
        try:
            explicit_port = parsed.port
        except ValueError as exc:
            raise RetryableCommandError(
                "generated-app service returned an invalid public URL"
            ) from exc
        if (
            parsed.scheme != "https"
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
            or explicit_port is not None
            or parsed.hostname is None
            or not parsed.hostname.endswith(".run.app")
        ):
            raise RetryableCommandError("generated-app service returned an invalid public URL")
        endpoint = f"{public_url.rstrip('/')}{health_path}"
        deadline = self._monotonic() + min(120, self._deploy_timeout_seconds)
        last_status = None
        while self._monotonic() < deadline:
            try:
                response = self._public_session.get(
                    endpoint,
                    timeout=self._request_timeout_seconds,
                    allow_redirects=False,
                )
                last_status = response.status_code
                if 200 <= response.status_code < 300:
                    return
            except requests.RequestException:
                pass
            self._sleep(self._poll_seconds)
        raise FatalCommandError(
            f"generated-app service health check failed at {health_path} (last status {last_status})"
        )

    def deploy_service(
        self,
        *,
        organization_id: str,
        artifact_id: str,
        app_slug: str,
        health_path: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        if not _APP_SLUG.fullmatch(app_slug):
            raise FatalCommandError("service app_slug must be a lowercase DNS label")
        if not _HEALTH_PATH.fullmatch(health_path) or "//" in health_path or ".." in health_path:
            raise FatalCommandError("service health_path must be a bounded absolute URL path")
        canonical_source, archive = self._source(organization_id, artifact_id)
        fingerprint = self._fingerprint(organization_id, app_slug, canonical_source)
        cached = self._cached(
            organization_id, idempotency_key, artifact_id=artifact_id, app_slug=app_slug,
            health_path=health_path, fingerprint=fingerprint,
        )
        if cached is not None:
            return cached
        object_name, generation = self._stage_source(organization_id, fingerprint, archive)
        service_name = self._service_name(organization_id, app_slug)
        image_tag = (
            f"{self._region}-docker.pkg.dev/{self._project_id}/{self._repository}/"
            f"{service_name}:{fingerprint[:32]}"
        )
        build_tag = f"agentos-{fingerprint}"
        recorded_build_id = self._recorded_build_id(
            organization_id, idempotency_key, fingerprint,
        )
        if recorded_build_id is not None:
            build = self._get_build(recorded_build_id)
            operation_record = self._store.find_by_idempotency_key(
                organization_id, f"{idempotency_key}:service-build-operation",
            )
            if operation_record is None:  # pragma: no cover - impossible without store corruption
                raise FatalCommandError("generated-app build operation evidence disappeared")
            build_operation_artifact_id = str(operation_record["artifact_id"])
        else:
            build = self._find_build(build_tag)
            if build is None:
                build = self._launch_build(
                    object_name=object_name, generation=generation,
                    build_tag=build_tag, image_tag=image_tag,
                )
            build_operation_artifact_id, authoritative_build_id = self._record_build(
                organization_id, idempotency_key, fingerprint, build,
            )
            if authoritative_build_id != build.get("id"):
                build = self._get_build(authoritative_build_id)
        build = self._poll_build(build)
        digest = self._image_digest(build, image_tag)
        immutable_image = f"{image_tag.rsplit(':', 1)[0]}@{digest}"
        revision = f"{service_name}-{fingerprint[:12]}"
        prior_service = self._existing_service(service_name)
        prior_ready = (
            prior_service.get("latestReadyRevision")
            if isinstance(prior_service, Mapping)
            else None
        )
        previous_revision = prior_ready if isinstance(prior_ready, str) else None
        try:
            self._promote(service_name, revision, immutable_image)
            service = self._poll_service(service_name, revision)
            public_url = service.get("uri")
            if not isinstance(public_url, str):
                raise RetryableCommandError("generated-app service returned no public URL")
            self._health(public_url, health_path)
        except FatalCommandError as exc:
            rolled_back = False
            if previous_revision is not None and previous_revision.rsplit("/", 1)[-1] != revision:
                self._rollback(service_name, previous_revision)
                rolled_back = True
            failure_artifact_id = self._record_failed_release(
                organization_id,
                idempotency_key,
                artifact_id=artifact_id,
                app_slug=app_slug,
                fingerprint=fingerprint,
                revision=revision,
                previous_revision=previous_revision,
                reason=str(exc),
                rolled_back=rolled_back,
            )
            status = "traffic rolled back" if rolled_back else "no prior revision was available"
            raise FatalCommandError(
                f"{exc}; {status}; failure evidence {failure_artifact_id}"
            ) from exc
        deployment_id = "service-" + hashlib.sha256(
            f"{organization_id}:{app_slug}:{fingerprint}".encode(),
        ).hexdigest()[:32]
        receipt = {
            "kind": "cloud_run_service",
            "deployment_backend": SERVICE_DEPLOYMENT_BACKEND,
            "deployment_id": deployment_id,
            "fingerprint": fingerprint,
            "app_slug": app_slug,
            "service_name": service_name,
            "revision": revision,
            "image": immutable_image,
            "build_id": build["id"],
            "build_operation_artifact_id": build_operation_artifact_id,
            "source_artifact_id": artifact_id,
            "source_object": object_name,
            "source_generation": generation,
            "health_path": health_path,
            "public_url": public_url,
        }
        receipt_artifact_id = self._store.put(
            organization_id=organization_id,
            content=json.dumps(receipt, separators=(",", ":"), sort_keys=True).encode(),
            media_type=SERVICE_RELEASE_RECEIPT_MEDIA_TYPE,
            idempotency_key=f"{idempotency_key}:service-release-receipt",
        )
        return {**receipt, "receipt_artifact_id": receipt_artifact_id, "cached": False}
