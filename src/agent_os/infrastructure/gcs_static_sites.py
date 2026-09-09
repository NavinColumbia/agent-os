"""Production static-site releases on private GCS objects and a public router."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import re
from typing import Any, Mapping
from urllib.parse import urlparse

from google.api_core.exceptions import GoogleAPICallError, NotFound, PreconditionFailed
from google.cloud import storage

from agent_os.application.command_worker import FatalCommandError, RetryableCommandError
from agent_os.application.ports import ArtifactStore, StaticSiteDeployer
from agent_os.infrastructure.docker_sandbox import SOURCE_BUNDLE_MEDIA_TYPE, _bundle_path


STATIC_SITE_RECEIPT_MEDIA_TYPE = "application/vnd.agent-os.static-site-release+json"
_APP_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_BUCKET_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$")
_PUBLIC_PATH = re.compile(r"^[A-Za-z0-9._~@+/-]{1,512}$")
_SECRET_PATTERNS = (
    re.compile(r"sk-(?:proj-|ant-)?[A-Za-z0-9_-]{16,}"),
    re.compile(r"sk_(?:live|test)_[A-Za-z0-9]{16,}"),
    re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"),
    re.compile(r"(?:ghp_|gho_|github_pat_)[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{6,}"),
    re.compile(
        r"\b(?:api[_-]?key|apikey|secret|passwd|password|bearer|access[_-]?key)\b"
        r"\s*[:=]\s*['\"]?[A-Za-z0-9+/_=-]{16,}",
        re.IGNORECASE,
    ),
)
_MAX_FILES = 1_000
_MAX_BYTES = 10 * 1024 * 1024


def contains_credential_like_material(content: bytes) -> bool:
    """Conservative release-boundary scan shared by generated-app deployers."""

    searchable = content.decode("utf-8", errors="ignore")
    return any(pattern.search(searchable) for pattern in _SECRET_PATTERNS)


class GCSStaticSiteDeployer(StaticSiteDeployer):
    """Publish immutable assets and atomically advance a stable route pointer."""

    def __init__(
        self,
        artifact_store: ArtifactStore,
        *,
        bucket_name: str,
        public_base_url: str,
        capability_secret: str,
        storage_client: Any | None = None,
        maximum_files: int = _MAX_FILES,
        maximum_bytes: int = _MAX_BYTES,
        timeout_seconds: float = 60.0,
    ) -> None:
        parsed = urlparse(public_base_url)
        if (
            parsed.scheme != "https" or not parsed.netloc or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment or parsed.username or parsed.password
        ):
            raise ValueError("static-site public base URL must be an HTTPS origin")
        if not _BUCKET_NAME.fullmatch(bucket_name) or ".." in bucket_name:
            raise ValueError("static-site bucket name is invalid")
        if len(capability_secret.encode()) < 32:
            raise ValueError("static-site capability secret must be at least 32 bytes")
        if not 1 <= maximum_files <= 10_000 or not 1 <= maximum_bytes <= 64 * 1024 * 1024:
            raise ValueError("static-site limits are invalid")
        if not 0 < timeout_seconds <= 300:
            raise ValueError("static-site storage timeout must be between 0 and 300 seconds")
        self._artifacts = artifact_store
        self._bucket = (storage_client or storage.Client()).bucket(bucket_name)
        self._public_base_url = public_base_url.rstrip("/")
        self._secret = capability_secret.encode()
        self._maximum_files = maximum_files
        self._maximum_bytes = maximum_bytes
        self._timeout_seconds = timeout_seconds

    def _route_id(self, organization_id: str, app_slug: str) -> str:
        digest = hmac.new(
            self._secret, f"static-site:v1:{organization_id}:{app_slug}".encode(), hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")

    def _source(
        self, organization_id: str, artifact_id: str,
    ) -> tuple[bytes, Mapping[str, tuple[bytes, bool]]]:
        record = self._artifacts.describe(organization_id, artifact_id)
        content = self._artifacts.get(organization_id, artifact_id)
        if record is None or content is None:
            raise FatalCommandError("static-site source artifact does not exist in this tenant")
        if record.get("media_type") != SOURCE_BUNDLE_MEDIA_TYPE:
            raise FatalCommandError("static-site source must be an Agent OS source bundle")
        if len(content) > self._maximum_bytes * 2 + 1024 * 1024:
            raise FatalCommandError("static-site source bundle exceeds the transfer limit")
        try:
            bundle = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FatalCommandError("static-site source bundle is not valid JSON") from exc
        if not isinstance(bundle, Mapping) or bundle.get("format") != "agent-os.source-bundle.v1":
            raise FatalCommandError("static-site source bundle format is unsupported")
        raw_files = bundle.get("files")
        if not isinstance(raw_files, Mapping) or not 1 <= len(raw_files) <= self._maximum_files:
            raise FatalCommandError("static-site source has an invalid file map")
        files: dict[str, tuple[bytes, bool]] = {}
        total = 0
        for raw_path, specification in sorted(raw_files.items()):
            path = _bundle_path(str(raw_path)).as_posix()
            if not _PUBLIC_PATH.fullmatch(path):
                raise FatalCommandError(
                    "static-site paths may contain only URL-safe path characters"
                )
            lowered = f"/{path.lower()}"
            basename = lowered.rsplit("/", 1)[-1]
            if (
                basename == ".env"
                or basename.startswith(".env.")
                or basename.endswith((".pem", ".key"))
                or "credentials" in basename
                or basename.startswith("id_rsa")
                or "/keys/" in lowered
                or "/secrets/" in lowered
                or lowered.startswith(("/.aws/", "/.ssh/"))
            ):
                raise FatalCommandError("static-site bundle contains a credential-bearing path")
            if not isinstance(specification, Mapping):
                raise FatalCommandError("static-site file specification must be an object")
            raw_value = specification.get("content")
            executable = specification.get("executable", False)
            if not isinstance(raw_value, str) or not isinstance(executable, bool):
                raise FatalCommandError("static-site file content or mode is invalid")
            try:
                if specification.get("encoding") == "utf-8":
                    value = raw_value.encode()
                elif specification.get("encoding") == "base64":
                    value = base64.b64decode(raw_value, validate=True)
                else:
                    raise FatalCommandError("static-site file encoding is unsupported")
            except ValueError as exc:
                raise FatalCommandError("static-site file contains invalid base64") from exc
            total += len(value)
            if total > self._maximum_bytes:
                raise FatalCommandError("static-site bundle exceeds the byte limit")
            if contains_credential_like_material(value):
                raise FatalCommandError("static-site bundle contains credential-like material")
            files[path] = (value, executable)
        if "index.html" not in files:
            raise FatalCommandError("static-site bundle requires index.html")
        return content, files

    def _upload_immutable(self, blob: Any, content: bytes, content_type: str) -> None:
        try:
            blob.upload_from_string(
                content,
                content_type=content_type,
                if_generation_match=0,
                checksum="crc32c",
                timeout=self._timeout_seconds,
            )
        except PreconditionFailed:
            try:
                existing = blob.download_as_bytes(
                    checksum="crc32c", timeout=self._timeout_seconds,
                )
            except Exception as exc:
                raise RetryableCommandError("static-site release collision could not be verified") from exc
            if existing != content:
                raise FatalCommandError("static-site immutable object collision")
        except GoogleAPICallError as exc:
            raise RetryableCommandError("static-site release upload failed") from exc

    def _advance_route(self, route_id: str, pointer: bytes) -> None:
        blob = self._bucket.blob(f"routes/{route_id}.json")
        for _ in range(8):
            try:
                blob.reload(timeout=self._timeout_seconds)
                generation = blob.generation
                if blob.download_as_bytes(
                    checksum="crc32c", timeout=self._timeout_seconds,
                ) == pointer:
                    return
            except NotFound:
                generation = 0
            except GoogleAPICallError as exc:
                raise RetryableCommandError("static-site route lookup failed") from exc
            try:
                blob.upload_from_string(
                    pointer,
                    content_type="application/json",
                    if_generation_match=generation,
                    checksum="crc32c",
                    timeout=self._timeout_seconds,
                )
                return
            except PreconditionFailed:
                continue
            except Exception as exc:
                raise RetryableCommandError("static-site route update failed") from exc
        raise RetryableCommandError("static-site route update exceeded its concurrency retry bound")

    def _cached(
        self,
        organization_id: str,
        idempotency_key: str,
        *,
        artifact_id: str,
        app_slug: str,
    ) -> Mapping[str, Any] | None:
        record = self._artifacts.find_by_idempotency_key(
            organization_id, f"{idempotency_key}:static-site-receipt",
        )
        if record is None:
            return None
        content = self._artifacts.get(organization_id, str(record["artifact_id"]))
        try:
            receipt = json.loads(content) if content is not None else None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FatalCommandError("static-site deployment receipt is corrupt") from exc
        if (
            not isinstance(receipt, Mapping)
            or receipt.get("source_artifact_id") != artifact_id
            or receipt.get("app_slug") != app_slug
        ):
            raise FatalCommandError("static-site idempotency key was reused with different input")
        return {**dict(receipt), "receipt_artifact_id": record["artifact_id"], "cached": True}

    def deploy_static(
        self,
        *,
        organization_id: str,
        artifact_id: str,
        app_slug: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        if not _APP_SLUG.fullmatch(app_slug):
            raise FatalCommandError("static-site app_slug must be a lowercase DNS label")
        cached = self._cached(
            organization_id, idempotency_key, artifact_id=artifact_id, app_slug=app_slug,
        )
        if cached is not None:
            return cached
        canonical_source, files = self._source(organization_id, artifact_id)
        revision = hashlib.sha256(canonical_source).hexdigest()
        route_id = self._route_id(organization_id, app_slug)
        for path, (content, _) in files.items():
            content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
            self._upload_immutable(
                self._bucket.blob(f"releases/{route_id}/{revision}/{path}"),
                content,
                content_type,
            )
        pointer = json.dumps(
            {"format": "agent-os.static-route.v1", "revision": revision},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        self._advance_route(route_id, pointer)
        deployment_id = "static-" + hashlib.sha256(
            f"{organization_id}:{app_slug}:{revision}".encode()
        ).hexdigest()[:32]
        receipt = {
            "kind": "static_site",
            "deployment_id": deployment_id,
            "app_slug": app_slug,
            "route_id": route_id,
            "revision": revision,
            "source_artifact_id": artifact_id,
            "public_url": f"{self._public_base_url}/p/{route_id}/",
        }
        receipt_artifact_id = self._artifacts.put(
            organization_id=organization_id,
            content=json.dumps(
                receipt, ensure_ascii=True, separators=(",", ":"), sort_keys=True,
            ).encode(),
            media_type=STATIC_SITE_RECEIPT_MEDIA_TYPE,
            idempotency_key=f"{idempotency_key}:static-site-receipt",
        )
        return {**receipt, "receipt_artifact_id": receipt_artifact_id, "cached": False}
