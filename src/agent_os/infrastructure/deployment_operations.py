"""Least-privilege emergency suspension for generated applications."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import time
from typing import Any, Callable, Mapping

import google.auth
from google.api_core.exceptions import GoogleAPICallError, NotFound, PreconditionFailed
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import AuthorizedSession
from google.cloud import storage
import requests


_BUCKET_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$")
_PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_REGION = re.compile(r"^[a-z]+-[a-z]+[0-9]$")
_ROUTE_ID = re.compile(r"^[A-Za-z0-9_-]{43}$")
_REVISION = re.compile(r"^[0-9a-f]{64}$")
_SERVICE_NAME = re.compile(r"^aos-[0-9a-f]{40}$")


class GCPDeploymentOperator:
    """Suspend or restore one opaque static route or generated Cloud Run service.

    The intended identity can update route pointers and Cloud Run services, but
    cannot read customer/model/payment secrets or delete forensic artifacts.
    Google Cloud Audit Logs provide the external break-glass audit trail.
    """

    def __init__(
        self,
        *,
        published_bucket: str,
        app_project_id: str,
        region: str,
        storage_client: Any | None = None,
        session: Any | None = None,
        request_timeout_seconds: float = 30,
        operation_timeout_seconds: float = 300,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not _BUCKET_NAME.fullmatch(published_bucket) or ".." in published_bucket:
            raise ValueError("published application bucket is invalid")
        if not _PROJECT_ID.fullmatch(app_project_id):
            raise ValueError("generated application project is invalid")
        if not _REGION.fullmatch(region):
            raise ValueError("generated application region is invalid")
        if not 0 < request_timeout_seconds <= 300:
            raise ValueError("request timeout must be between 0 and 300 seconds")
        if not 1 <= operation_timeout_seconds <= 3_600:
            raise ValueError("operation timeout must be between 1 and 3600 seconds")
        credentials = None
        if storage_client is None or session is None:
            try:
                credentials, _ = google.auth.default(
                    scopes=["https://www.googleapis.com/auth/cloud-platform"],
                )
            except GoogleAuthError as exc:
                raise ConnectionError(
                    "Google Application Default Credentials are unavailable"
                ) from exc
        self._owns_storage = storage_client is None
        self._owns_session = session is None
        self._storage = storage_client or storage.Client(credentials=credentials)
        self._session = session or AuthorizedSession(credentials)
        self._bucket = self._storage.bucket(published_bucket)
        self._project = app_project_id
        self._region = region
        self._request_timeout = request_timeout_seconds
        self._operation_timeout = operation_timeout_seconds
        self._sleep = sleep
        self._monotonic = monotonic
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _reason(reason: str, *, required: bool) -> str:
        value = reason.strip()
        if required and not value:
            raise ValueError("a suspension reason is required")
        if len(value) > 1_000 or any(character in value for character in "\r\n\0"):
            raise ValueError("suspension reason is invalid")
        return value

    @staticmethod
    def _actor(actor: str) -> str:
        value = actor.strip()
        if not value or len(value) > 256 or any(character in value for character in "\r\n\0"):
            raise ValueError("operator actor is invalid")
        return value

    @staticmethod
    def _pointer(content: bytes) -> Mapping[str, Any]:
        try:
            value = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("published route pointer is corrupt") from exc
        revision = value.get("revision") if isinstance(value, Mapping) else None
        if (
            not isinstance(value, Mapping)
            or value.get("format") != "agent-os.static-route.v1"
            or not isinstance(revision, str)
            or not _REVISION.fullmatch(revision)
            or value.get("status", "active") not in {"active", "suspended"}
        ):
            raise RuntimeError("published route pointer is corrupt")
        return value

    def set_static_route(
        self,
        route_id: str,
        *,
        suspended: bool,
        reason: str = "",
        actor: str,
    ) -> Mapping[str, Any]:
        if not _ROUTE_ID.fullmatch(route_id):
            raise ValueError("static route ID is invalid")
        reason = self._reason(reason, required=suspended)
        actor = self._actor(actor)
        desired = "suspended" if suspended else "active"
        blob = self._bucket.blob(f"routes/{route_id}.json")
        for _ in range(8):
            try:
                blob.reload(timeout=self._request_timeout)
                generation = blob.generation
                pointer = self._pointer(blob.download_as_bytes(
                    checksum="crc32c", timeout=self._request_timeout,
                ))
            except NotFound as exc:
                raise LookupError("static route does not exist") from exc
            except GoogleAPICallError as exc:
                raise ConnectionError("static route lookup failed") from exc
            if pointer.get("status", "active") == desired:
                return {
                    "kind": "static_site",
                    "target": route_id,
                    "status": desired,
                    "cached": True,
                    "generation": generation,
                    "actor": actor,
                    "reason": reason,
                }
            next_pointer = {
                "format": "agent-os.static-route.v1",
                "revision": pointer["revision"],
                "status": desired,
                "changed_at": self._clock().astimezone(timezone.utc).isoformat(),
                "changed_by": actor,
            }
            if suspended:
                next_pointer["reason"] = reason
            content = json.dumps(
                next_pointer, allow_nan=False, ensure_ascii=True,
                separators=(",", ":"), sort_keys=True,
            ).encode()
            try:
                blob.upload_from_string(
                    content,
                    content_type="application/json",
                    if_generation_match=generation,
                    checksum="crc32c",
                    timeout=self._request_timeout,
                )
                return {
                    "kind": "static_site",
                    "target": route_id,
                    "status": desired,
                    "cached": False,
                    "generation": blob.generation,
                    "actor": actor,
                    "reason": reason,
                }
            except PreconditionFailed:
                continue
            except GoogleAPICallError as exc:
                raise ConnectionError("static route update failed") from exc
        raise ConnectionError("static route update exceeded its concurrency retry bound")

    @property
    def _service_collection(self) -> str:
        return (
            f"https://run.googleapis.com/v2/projects/{self._project}/"
            f"locations/{self._region}/services"
        )

    @staticmethod
    def _response(response: Any, subject: str) -> Mapping[str, Any]:
        try:
            response.raise_for_status()
            value = response.json()
        except requests.HTTPError as exc:
            status = getattr(response, "status_code", None)
            if status == 404:
                raise LookupError("generated service does not exist") from exc
            if isinstance(status, int) and 400 <= status < 500:
                raise ValueError(f"generated service {subject} was rejected with HTTP {status}") from exc
            raise ConnectionError(f"generated service {subject} failed") from exc
        except (requests.RequestException, ValueError) as exc:
            raise ConnectionError(f"generated service {subject} returned invalid data") from exc
        if not isinstance(value, Mapping):
            raise ConnectionError(f"generated service {subject} returned invalid data")
        return value

    def _get_service(self, service_name: str) -> Mapping[str, Any]:
        try:
            response = self._session.get(
                f"{self._service_collection}/{service_name}",
                timeout=self._request_timeout,
            )
        except requests.RequestException as exc:
            raise ConnectionError("generated service lookup failed") from exc
        return self._response(response, "lookup")

    def set_service(
        self,
        service_name: str,
        *,
        suspended: bool,
        reason: str = "",
        actor: str,
    ) -> Mapping[str, Any]:
        if not _SERVICE_NAME.fullmatch(service_name):
            raise ValueError("generated service name is invalid")
        reason = self._reason(reason, required=suspended)
        actor = self._actor(actor)
        desired_public = not suspended
        current = self._get_service(service_name)
        if current.get("invokerIamDisabled") is desired_public and current.get("reconciling") is not True:
            return {
                "kind": "cloud_run_service",
                "target": service_name,
                "status": "suspended" if suspended else "active",
                "cached": True,
                "actor": actor,
                "reason": reason,
            }
        resource_name = (
            f"projects/{self._project}/locations/{self._region}/services/{service_name}"
        )
        try:
            response = self._session.patch(
                f"{self._service_collection}/{service_name}",
                params={"updateMask": "invoker_iam_disabled"},
                json={
                    "name": resource_name,
                    "invokerIamDisabled": desired_public,
                },
                timeout=self._request_timeout,
            )
        except requests.RequestException as exc:
            raise ConnectionError("generated service update failed") from exc
        self._response(response, "update")
        deadline = self._monotonic() + self._operation_timeout
        while self._monotonic() < deadline:
            current = self._get_service(service_name)
            if (
                current.get("reconciling") is not True
                and current.get("invokerIamDisabled") is desired_public
            ):
                return {
                    "kind": "cloud_run_service",
                    "target": service_name,
                    "status": "suspended" if suspended else "active",
                    "cached": False,
                    "actor": actor,
                    "reason": reason,
                }
            self._sleep(2)
        raise ConnectionError("generated service update exceeded its readiness deadline")

    def close(self) -> None:
        if self._owns_session:
            self._session.close()
        if self._owns_storage:
            self._storage.close()
