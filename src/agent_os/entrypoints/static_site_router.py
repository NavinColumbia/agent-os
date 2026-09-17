"""Read-only public runtime for immutable generated static applications."""

from __future__ import annotations

from dataclasses import dataclass
import json
import mimetypes
import os
import re
from typing import Any, Mapping

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import RedirectResponse
from google.api_core.exceptions import NotFound
from google.cloud import storage


_BUCKET_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$")
_ROUTE_ID = re.compile(r"^[A-Za-z0-9_-]{43}$")
_REVISION = re.compile(r"^[0-9a-f]{64}$")
_ASSET_PATH = re.compile(r"^[^\\\x00-\x1f\x7f]{1,512}$")
_HTML_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "font-src 'self' data:; media-src 'self'; connect-src 'none'; "
    "object-src 'none'; base-uri 'self'; form-action 'none'; frame-ancestors 'none'"
)


@dataclass(frozen=True)
class StaticSiteRouterSettings:
    bucket_name: str
    host: str = "0.0.0.0"  # secscan:allow Cloud Run requires container-wide ingress
    port: int = 8080
    storage_timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> "StaticSiteRouterSettings":
        bucket_name = os.getenv("AOS_V2_PUBLISHED_APP_BUCKET", "").strip()
        if not _BUCKET_NAME.fullmatch(bucket_name) or ".." in bucket_name:
            raise ValueError("AOS_V2_PUBLISHED_APP_BUCKET must be a valid private GCS bucket")
        port = int(os.getenv("PORT", os.getenv("AOS_V2_PORT", "8080")))
        timeout = float(os.getenv("AOS_V2_STATIC_STORAGE_TIMEOUT_SECONDS", "30"))
        if not 1 <= port <= 65535:
            raise ValueError("static-site router port must be between 1 and 65535")
        if not 0 < timeout <= 300:
            raise ValueError("static-site storage timeout must be between 0 and 300 seconds")
        return cls(
            bucket_name=bucket_name,
            host=os.getenv("AOS_V2_HOST", "0.0.0.0").strip() or "0.0.0.0",  # secscan:allow Cloud Run ingress
            port=port,
            storage_timeout_seconds=timeout,
        )


def _safe_asset_path(raw: str) -> str:
    if (
        not _ASSET_PATH.fullmatch(raw)
        or raw.startswith("/")
        or "\\" in raw
        or any(part in {"", ".", ".."} for part in raw.split("/"))
    ):
        raise HTTPException(status_code=404, detail="application asset not found")
    return raw


def _security_headers(*, html: bool) -> dict[str, str]:
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
        "Cross-Origin-Resource-Policy": "same-origin",
    }
    if html:
        headers["Content-Security-Policy"] = _HTML_CSP
    return headers


def create_static_site_router(
    *,
    bucket_name: str,
    storage_client: Any | None = None,
    storage_timeout_seconds: float = 30.0,
) -> FastAPI:
    """Create a secretless reader for the private published-app bucket."""

    if not _BUCKET_NAME.fullmatch(bucket_name) or ".." in bucket_name:
        raise ValueError("a valid private published-app GCS bucket is required")
    if not 0 < storage_timeout_seconds <= 300:
        raise ValueError("static-site storage timeout must be between 0 and 300 seconds")
    client = storage_client or storage.Client()
    bucket = client.bucket(bucket_name)
    app = FastAPI(
        title="Agent OS Published Apps",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    def download(name: str) -> bytes:
        blob = bucket.blob(name)
        try:
            return blob.download_as_bytes(
                checksum="crc32c", timeout=storage_timeout_seconds,
            )
        except NotFound as exc:
            raise HTTPException(status_code=404, detail="published application not found") from exc
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail="published application storage is temporarily unavailable",
            ) from exc

    def active_revision(route_id: str) -> str:
        content = download(f"routes/{route_id}.json")
        try:
            pointer = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=503, detail="published route is corrupt") from exc
        revision = pointer.get("revision") if isinstance(pointer, Mapping) else None
        if (
            not isinstance(pointer, Mapping)
            or pointer.get("format") != "agent-os.static-route.v1"
            or not isinstance(revision, str)
            or not _REVISION.fullmatch(revision)
        ):
            raise HTTPException(status_code=503, detail="published route is corrupt")
        route_status = pointer.get("status", "active")
        if route_status == "suspended":
            raise HTTPException(
                status_code=410,
                detail="published application is suspended",
                headers={"Cache-Control": "no-store", **_security_headers(html=False)},
            )
        if route_status != "active":
            raise HTTPException(status_code=503, detail="published route is corrupt")
        return revision

    @app.get("/health", include_in_schema=False)
    def health() -> Mapping[str, str]:
        return {"status": "ok"}

    @app.get("/p/{route_id}/", include_in_schema=False)
    def stable_route(route_id: str) -> Response:
        if not _ROUTE_ID.fullmatch(route_id):
            raise HTTPException(status_code=404, detail="published application not found")
        revision = active_revision(route_id)
        response = RedirectResponse(
            url=f"/p/{route_id}/{revision}/index.html",
            status_code=307,
            headers={"Cache-Control": "no-store", **_security_headers(html=False)},
        )
        return response

    @app.get("/p/{route_id}/{revision}/{asset_path:path}", include_in_schema=False)
    def immutable_asset(route_id: str, revision: str, asset_path: str) -> Response:
        if not _ROUTE_ID.fullmatch(route_id) or not _REVISION.fullmatch(revision):
            raise HTTPException(status_code=404, detail="application asset not found")
        # Immutable URLs stay cacheable, but every fresh request still checks
        # the stable route kill switch so incident response can fence all known
        # revisions without deleting forensic release objects.
        active_revision(route_id)
        path = _safe_asset_path(asset_path)
        content = download(f"releases/{route_id}/{revision}/{path}")
        media_type = mimetypes.guess_type(path)[0]
        if not isinstance(media_type, str) or not media_type:
            media_type = "application/octet-stream"
        html = media_type in {"text/html", "application/xhtml+xml"}
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",
                **_security_headers(html=html),
            },
        )

    return app


def build_static_site_router(
    settings: StaticSiteRouterSettings | None = None,
) -> FastAPI:
    settings = settings or StaticSiteRouterSettings.from_env()
    return create_static_site_router(
        bucket_name=settings.bucket_name,
        storage_timeout_seconds=settings.storage_timeout_seconds,
    )
