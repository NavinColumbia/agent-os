"""Small identity boundary used until the hosted OIDC provider is configured."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
import time
from typing import Any, Mapping, Protocol


@dataclass(frozen=True)
class Principal:
    subject_id: str
    organization_id: str
    roles: frozenset[str]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "Principal":
        subject = str(raw.get("subject_id") or raw.get("sub") or "").strip()
        organization = str(raw.get("organization_id") or raw.get("org") or "").strip()
        roles_raw = raw.get("roles", ())
        if not isinstance(roles_raw, (list, tuple, set, frozenset)):
            raise ValueError("identity roles must be a list")
        roles = frozenset(str(role).strip() for role in roles_raw if str(role).strip())
        if not subject or not organization or not roles:
            raise ValueError("identity must contain subject, organization, and roles")
        return cls(subject, organization, roles)


class Authenticator(Protocol):
    def authenticate(self, authorization: str | None, session: str | None) -> Mapping[str, Any]: ...


def _b64_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64_decode(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


class HMACTokenIdentity:
    """Verifies opaque-looking local/BYOC bearer tokens without database state.

    Hosted production will inject an OIDC verifier through the same
    ``Authenticator`` boundary.  Tokens are signed, expire, and bind the tenant
    server-side; an untrusted ``X-Organization-Id`` header is never accepted.
    """

    def __init__(self, secret: str | bytes) -> None:
        self._secret = secret.encode() if isinstance(secret, str) else secret
        if len(self._secret) < 32:
            raise ValueError("identity signing secret must be at least 32 bytes")

    def issue(
        self,
        *,
        subject_id: str,
        organization_id: str,
        roles: tuple[str, ...] = ("owner",),
        ttl_seconds: int = 3600,
        now: int | None = None,
    ) -> str:
        issued = int(time.time() if now is None else now)
        payload = {
            "sub": subject_id,
            "org": organization_id,
            "roles": list(roles),
            "iat": issued,
            "exp": issued + ttl_seconds,
        }
        encoded = _b64_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        signature = _b64_encode(hmac.new(self._secret, encoded.encode(), hashlib.sha256).digest())
        return f"aosv2.{encoded}.{signature}"

    def authenticate(self, authorization: str | None, session: str | None) -> Mapping[str, Any]:
        candidate = session
        if authorization:
            scheme, _, value = authorization.partition(" ")
            if scheme.lower() != "bearer" or not value:
                raise ValueError("authorization must use Bearer")
            candidate = value
        if not candidate:
            raise ValueError("authentication required")
        try:
            prefix, encoded, supplied = candidate.split(".")
            if prefix != "aosv2":
                raise ValueError("unsupported token")
            expected = _b64_encode(hmac.new(self._secret, encoded.encode(), hashlib.sha256).digest())
            if not hmac.compare_digest(supplied, expected):
                raise ValueError("invalid token signature")
            payload = json.loads(_b64_decode(encoded))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid authentication token") from exc
        if not isinstance(payload, dict) or int(payload.get("exp", 0)) <= int(time.time()):
            raise ValueError("authentication token expired")
        return payload
