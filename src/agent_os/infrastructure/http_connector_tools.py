"""Governed, tenant-owned HTTP connector execution for graph tool nodes."""

from __future__ import annotations

from collections.abc import Callable
import base64
import hashlib
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import socket
import ssl
import stat
from typing import Any, Mapping, Protocol
from urllib.parse import urlencode, unquote, urlparse

import google.auth
from google.auth.transport.requests import AuthorizedSession

from agent_os.application.command_worker import FatalCommandError, RetryableCommandError
from agent_os.application.ports import ArtifactStore, ConnectorRegistry
from agent_os.domain.workflow import NodeKind, WorkflowDefinition, WorkflowNode
from agent_os.domain.workflow_runtime import TokenStatus, WorkflowAction, WorkflowRunState
from agent_os.infrastructure.graph_output_refs import resolve_prior_output
from agent_os.infrastructure.sql_connectors import HTTPConnectorDefinition


_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_MEDIA_TYPE_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz0123456789!#$&^_.+-/;= "
)


class ConnectorSecretResolver(Protocol):
    def resolve(self, tenant_id: str, credential_ref: str) -> str: ...


class FileConnectorSecretResolver:
    """Read an orchestrator-mounted secret without accepting caller-controlled paths."""

    def __init__(self, root: str) -> None:
        path = Path(root)
        if not path.is_absolute():
            raise ValueError("connector secret directory must be absolute")
        self._root = path

    @staticmethod
    def tenant_directory(tenant_id: str) -> str:
        return hashlib.sha256(tenant_id.encode()).hexdigest()

    def resolve(self, tenant_id: str, credential_ref: str) -> str:
        if not tenant_id.strip() or not credential_ref.strip():
            raise FatalCommandError("connector credential identity is missing")
        path = self._root / self.tenant_directory(tenant_id) / credential_ref
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise FatalCommandError(
                f"connector credential {credential_ref!r} is not provisioned"
            ) from exc
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode) or not 1 <= details.st_size <= 65_536:
                raise FatalCommandError("connector credential file is empty, non-regular, or too large")
            value = os.read(descriptor, 65_537)
        finally:
            os.close(descriptor)
        if len(value) > 65_536:
            raise FatalCommandError("connector credential file is too large")
        try:
            decoded = value.decode().strip()
        except UnicodeDecodeError as exc:
            raise FatalCommandError("connector credential must be UTF-8 text") from exc
        if not decoded:
            raise FatalCommandError("connector credential is empty")
        return decoded


class GCPConnectorSecretResolver:
    """Resolve a deterministic tenant/ref Secret Manager name with workload identity."""

    def __init__(self, project_id: str, *, session: Any | None = None) -> None:
        project_id = project_id.strip()
        if not project_id or "/" in project_id:
            raise ValueError("connector Secret Manager project_id is required")
        self._project_id = project_id
        if session is None:
            credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            session = AuthorizedSession(credentials)
        self._session = session

    @staticmethod
    def secret_name(tenant_id: str, credential_ref: str) -> str:
        tenant = hashlib.sha256(tenant_id.encode()).hexdigest()[:24]
        reference = hashlib.sha256(credential_ref.encode()).hexdigest()[:24]
        return f"agentos-connector-{tenant}-{reference}"

    def resolve(self, tenant_id: str, credential_ref: str) -> str:
        name = self.secret_name(tenant_id, credential_ref)
        url = (
            "https://secretmanager.googleapis.com/v1/projects/"
            f"{self._project_id}/secrets/{name}/versions/latest:access"
        )
        try:
            response = self._session.get(url, timeout=15)
        except Exception as exc:
            raise RetryableCommandError("connector secret store is unavailable") from exc
        if response.status_code in {403, 404}:
            raise FatalCommandError(
                f"connector credential {credential_ref!r} is not provisioned or authorized"
            )
        if response.status_code == 429 or 500 <= response.status_code <= 599:
            raise RetryableCommandError("connector secret store returned a transient error")
        if response.status_code != 200:
            raise FatalCommandError("connector secret store rejected credential access")
        try:
            encoded = response.json()["payload"]["data"]
            value = base64.b64decode(encoded, validate=True)
        except (KeyError, TypeError, ValueError) as exc:
            raise FatalCommandError("connector secret store returned malformed data") from exc
        if not 1 <= len(value) <= 65_536:
            raise FatalCommandError("connector credential is empty or too large")
        try:
            decoded = value.decode().strip()
        except UnicodeDecodeError as exc:
            raise FatalCommandError("connector credential must be UTF-8 text") from exc
        if not decoded:
            raise FatalCommandError("connector credential is empty")
        return decoded


def _public_addresses(host: str, port: int) -> tuple[str, ...]:
    try:
        values = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise RetryableCommandError("connector hostname could not be resolved") from exc
    addresses: list[str] = []
    for value in values:
        raw = value[4][0]
        try:
            address = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise FatalCommandError("connector DNS returned an invalid address") from exc
        if not address.is_global:
            raise FatalCommandError("connector target resolved to a non-public address")
        if raw not in addresses:
            addresses.append(raw)
    if not addresses:
        raise RetryableCommandError("connector hostname returned no addresses")
    return tuple(addresses)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, *, timeout: float) -> None:
        super().__init__(host, port=443, timeout=timeout, context=ssl.create_default_context())
        self._address = address

    def connect(self) -> None:
        raw = socket.create_connection((self._address, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


ConnectorTransport = Callable[
    [str, str, Mapping[str, str], bytes | None, int, int],
    tuple[int, Mapping[str, str], bytes],
]


def pinned_https_request(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout_seconds: int,
    max_response_bytes: int,
) -> tuple[int, Mapping[str, str], bytes]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.port not in {None, 443}:
        raise FatalCommandError("connector execution requires a standard HTTPS target")
    addresses = _public_addresses(parsed.hostname, 443)
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    last_error: OSError | ssl.SSLError | None = None
    for address in addresses:
        connection = _PinnedHTTPSConnection(
            parsed.hostname, address, timeout=float(timeout_seconds),
        )
        try:
            connection.request(method, target, body=body, headers=dict(headers))
            response = connection.getresponse()
            if 300 <= response.status <= 399:
                raise FatalCommandError("connector redirects are denied")
            content = response.read(max_response_bytes + 1)
            if len(content) > max_response_bytes:
                raise FatalCommandError("connector response exceeded its admitted byte limit")
            return response.status, dict(response.headers.items()), content
        except (TimeoutError, OSError, ssl.SSLError) as exc:
            last_error = exc
        finally:
            connection.close()
    raise RetryableCommandError("connector request could not reach its pinned public target") from last_error


def _safe_media_type(raw: str | None) -> str:
    value = (raw or "application/octet-stream").split(",", 1)[0].strip().lower()
    if (
        not value or len(value) > 256 or "/" not in value
        or any(character not in _MEDIA_TYPE_CHARS for character in value)
    ):
        return "application/octet-stream"
    return value


def _retry_after(headers: Mapping[str, str]) -> int | None:
    raw = next((value for key, value in headers.items() if key.lower() == "retry-after"), None)
    try:
        value = int(raw) if raw is not None else None
    except ValueError:
        return None
    return None if value is None else min(max(value, 0), 300)


class HTTPConnectorToolNodeHandlers:
    def __init__(
        self,
        registry: ConnectorRegistry,
        artifacts: ArtifactStore,
        secrets: ConnectorSecretResolver,
        *,
        transport: ConnectorTransport = pinned_https_request,
    ) -> None:
        self._registry = registry
        self._artifacts = artifacts
        self._secrets = secrets
        self._transport = transport

    def named_handlers(self):
        return {"connector.invoke": self.execute}

    @staticmethod
    def _approved(
        definition: WorkflowDefinition,
        state: WorkflowRunState,
        approval_node_id: str,
    ) -> bool:
        node = next((item for item in definition.nodes if item.node_id == approval_node_id), None)
        tokens = [item for item in state.tokens if item.node_id == approval_node_id]
        response = None if len(tokens) != 1 else tokens[0].output.get("human_response")
        return bool(
            node is not None and node.kind is NodeKind.HUMAN and len(tokens) == 1
            and tokens[0].status is TokenStatus.SUCCEEDED
            and isinstance(response, Mapping) and response.get("approved") is True
        )

    def _replay(
        self, tenant_id: str, action: WorkflowAction, condition: str,
    ) -> Mapping[str, Any] | None:
        record = self._artifacts.find_by_idempotency_key(
            tenant_id, f"{action.action_id}:connector-receipt",
        )
        if record is None:
            return None
        content = self._artifacts.get(tenant_id, str(record["artifact_id"]))
        try:
            receipt = json.loads(content or b"")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FatalCommandError("connector replay receipt is unreadable") from exc
        if not isinstance(receipt, Mapping):
            raise FatalCommandError("connector replay receipt is malformed")
        response_artifact_id = str(receipt.get("response_artifact_id") or "")
        if not response_artifact_id:
            raise FatalCommandError("connector replay receipt has no response artifact")
        return {
            "disposition": "complete",
            "satisfied_conditions": [condition],
            "evidence_ids": [response_artifact_id, str(record["artifact_id"])],
            "output": {**dict(receipt), "receipt_artifact_id": str(record["artifact_id"])},
        }

    def execute(
        self,
        tenant_id: str,
        run_id: str,
        definition: WorkflowDefinition,
        state: WorkflowRunState,
        action: WorkflowAction,
        node: WorkflowNode,
    ) -> Mapping[str, Any]:
        del run_id
        if node.kind is not NodeKind.TOOL or node.configuration.get("tool") != "connector.invoke":
            raise FatalCommandError("connector handler received the wrong tool node")
        connector_id = str(node.configuration.get("connector_id") or "")
        stored = self._registry.get_connector(tenant_id, connector_id)
        if stored is None or not stored.get("active"):
            raise FatalCommandError("connector is missing or disabled for this tenant")
        try:
            connector = HTTPConnectorDefinition.model_validate({
                key: stored[key]
                for key in HTTPConnectorDefinition.model_fields
            })
        except (KeyError, ValueError) as exc:
            raise FatalCommandError("stored connector definition is invalid") from exc
        method = str(node.configuration.get("method") or "GET").upper()
        if method not in connector.allowed_methods:
            raise FatalCommandError("connector method is outside its owner-approved capability")
        path = str(node.configuration.get("path") or "")
        decoded_segments = unquote(path).split("/")
        if (
            not path.startswith("/") or path.startswith("//") or "\\" in path
            or any(character in path for character in "\r\n?#")
            or any(segment in {".", ".."} for segment in decoded_segments)
            or not any(
                path == prefix
                or path.startswith(prefix if prefix.endswith("/") else prefix + "/")
                for prefix in connector.allowed_path_prefixes
            )
        ):
            raise FatalCommandError("connector path is outside its owner-approved capability")
        condition = str(node.configuration.get("success_condition") or "")
        available = {
            edge.condition for edge in definition.outgoing(node.node_id)
            if edge.condition != "always"
        }
        if not condition or condition not in available:
            raise FatalCommandError("connector success condition is not a declared path")
        replay = self._replay(tenant_id, action, condition)
        if replay is not None:
            return replay
        if method in _WRITE_METHODS:
            approval_node_id = str(node.configuration.get("approval_node_id") or "")
            if not self._approved(definition, state, approval_node_id):
                raise FatalCommandError("connector write requires a completed explicit human approval")
        raw_query = node.configuration.get("query", {})
        if not isinstance(raw_query, Mapping) or len(raw_query) > 64:
            raise FatalCommandError("connector query must be a bounded object")
        query: list[tuple[str, str]] = []
        for key, value in raw_query.items():
            if not isinstance(key, str) or not 1 <= len(key) <= 128:
                raise FatalCommandError("connector query keys are invalid")
            values = value if isinstance(value, list) else [value]
            if len(values) > 32:
                raise FatalCommandError("connector query value list is too large")
            for item in values:
                if (
                    not isinstance(item, (str, int, float, bool))
                    or isinstance(item, float) and not math.isfinite(item)
                    or len(str(item)) > 2_000
                ):
                    raise FatalCommandError("connector query values must be bounded primitives")
                query.append((key, str(item).lower() if isinstance(item, bool) else str(item)))
        url = connector.base_url + path
        if query:
            url += "?" + urlencode(query)
        body: bytes | None = None
        content_type = "application/json"
        source = node.configuration.get("source")
        if source is not None:
            if method not in _WRITE_METHODS:
                raise FatalCommandError("connector request bodies require a write method")
            artifact_id = resolve_prior_output(state, source, subject="connector request")
            if not isinstance(artifact_id, str) or not artifact_id:
                raise FatalCommandError("connector request source is not an artifact ID")
            record = self._artifacts.describe(tenant_id, artifact_id)
            body = self._artifacts.get(tenant_id, artifact_id)
            if record is None or body is None:
                raise FatalCommandError("connector request artifact is missing")
            content_type = str(record.get("media_type") or "application/octet-stream")
            if len(body) > 2 * 1024 * 1024:
                raise FatalCommandError("connector request body exceeds 2 MiB")
        elif method in {"POST", "PUT", "PATCH"}:
            body = b"{}"
        headers = {
            "Accept": "application/json, text/plain, application/octet-stream;q=0.8",
            "Content-Type": content_type,
            "Host": urlparse(connector.base_url).hostname or "",
            "User-Agent": "AgentOS-Connector/1.0",
        }
        if connector.auth_kind != "none":
            secret = self._secrets.resolve(tenant_id, str(connector.credential_ref))
            if connector.auth_kind == "bearer":
                headers["Authorization"] = f"Bearer {secret}"
            else:
                headers[str(connector.auth_header)] = secret
        if method in _WRITE_METHODS:
            headers[str(connector.idempotency_header)] = action.action_id
        status, response_headers, content = self._transport(
            method, url, headers, body, connector.timeout_seconds,
            connector.max_response_bytes,
        )
        if status in {408, 425, 429} or 500 <= status <= 599:
            raise RetryableCommandError(
                f"connector returned retryable HTTP status {status}",
                retry_after_seconds=_retry_after(response_headers),
            )
        if not 200 <= status <= 299:
            raise FatalCommandError(f"connector returned terminal HTTP status {status}")
        response_artifact_id = self._artifacts.put(
            organization_id=tenant_id,
            content=content,
            media_type=_safe_media_type(next((
                value for key, value in response_headers.items()
                if key.lower() == "content-type"
            ), None)),
            idempotency_key=f"{action.action_id}:connector-response",
        )
        receipt = {
            "connector_id": connector.connector_id,
            "method": method,
            "path": path,
            "status_code": status,
            "response_artifact_id": response_artifact_id,
            "request_fingerprint": hashlib.sha256(
                json.dumps({
                    "connector_id": connector.connector_id,
                    "method": method,
                    "path": path,
                    "query": query,
                    "body_sha256": None if body is None else hashlib.sha256(body).hexdigest(),
                }, separators=(",", ":"), sort_keys=True).encode()
            ).hexdigest(),
        }
        receipt_artifact_id = self._artifacts.put(
            organization_id=tenant_id,
            content=json.dumps(receipt, separators=(",", ":"), sort_keys=True).encode(),
            media_type="application/json",
            idempotency_key=f"{action.action_id}:connector-receipt",
        )
        return {
            "disposition": "complete",
            "satisfied_conditions": [condition],
            "evidence_ids": [response_artifact_id, receipt_artifact_id],
            "output": {**receipt, "receipt_artifact_id": receipt_artifact_id},
        }
