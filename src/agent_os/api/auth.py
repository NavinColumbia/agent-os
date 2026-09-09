"""Identity adapters for local evaluation and hosted OIDC access tokens."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
import time
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlparse

import jwt
from jwt import PyJWKClient
from jwt.exceptions import PyJWTError


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
        if len(subject) > 255 or len(organization) > 255:
            raise ValueError("identity subject and organization must not exceed 255 characters")
        if len(roles) > 32 or any(len(role) > 64 for role in roles):
            raise ValueError("identity roles exceed the supported bounds")
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


class SigningKeyProvider(Protocol):
    def get_signing_key_from_jwt(self, token: str) -> Any: ...


class OIDCTokenIdentity:
    """Validate provider-neutral OIDC access tokens against rotating JWKS keys.

    Identity-provider claims are normalized at this boundary. Tenant identity
    is taken from a verified organization claim or, when explicitly enabled,
    a server-keyed personal tenant derived from the verified issuer/subject;
    it is never taken from a request header.
    Hosted browser sessions belong in an upstream BFF; accepting a bearer token
    from this service's cookie would introduce a CSRF-prone second auth path.
    """

    _SUPPORTED_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"})

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str,
        organization_claim: str = "org_id",
        roles_claim: str = "roles",
        algorithms: Sequence[str] = ("RS256", "ES256"),
        leeway_seconds: int = 60,
        maximum_token_lifetime_seconds: int = 86_400,
        jwks_cache_seconds: int = 300,
        jwks_timeout_seconds: float = 5.0,
        signing_keys: SigningKeyProvider | None = None,
        personal_tenant_secret: str | bytes | None = None,
    ) -> None:
        self._issuer = issuer.rstrip("/")
        self._audience = audience.strip()
        self._organization_claim = organization_claim.strip()
        self._roles_claim = roles_claim.strip()
        self._algorithms = tuple(dict.fromkeys(item.strip().upper() for item in algorithms if item.strip()))
        self._leeway_seconds = leeway_seconds
        self._maximum_token_lifetime_seconds = maximum_token_lifetime_seconds
        if isinstance(personal_tenant_secret, str):
            personal_tenant_secret = personal_tenant_secret.encode()
        if personal_tenant_secret is not None and len(personal_tenant_secret) < 32:
            raise ValueError("OIDC personal-tenant secret must be at least 32 bytes")
        self._personal_tenant_secret = personal_tenant_secret

        for label, value in (("issuer", self._issuer), ("JWKS URL", jwks_url)):
            parsed = urlparse(value)
            if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.fragment:
                raise ValueError(f"OIDC {label} must be an HTTPS URL without credentials or a fragment")
        if not self._audience or len(self._audience) > 255:
            raise ValueError("OIDC audience is required and must not exceed 255 characters")
        if (
            not self._organization_claim
            or not self._roles_claim
            or len(self._organization_claim) > 255
            or len(self._roles_claim) > 255
        ):
            raise ValueError("OIDC organization and roles claim names are required")
        if not self._algorithms or any(item not in self._SUPPORTED_ALGORITHMS for item in self._algorithms):
            raise ValueError("OIDC algorithms must be an explicit asymmetric allowlist")
        if not 0 <= leeway_seconds <= 300:
            raise ValueError("OIDC clock leeway must be between 0 and 300 seconds")
        if not 60 <= maximum_token_lifetime_seconds <= 86_400:
            raise ValueError("OIDC maximum token lifetime must be between 60 and 86400 seconds")
        if not 60 <= jwks_cache_seconds <= 3600:
            raise ValueError("OIDC JWKS cache must be between 60 and 3600 seconds")
        if not 1 <= jwks_timeout_seconds <= 30:
            raise ValueError("OIDC JWKS timeout must be between 1 and 30 seconds")

        self._signing_keys = signing_keys or PyJWKClient(
            jwks_url,
            cache_keys=True,
            max_cached_keys=16,
            cache_jwk_set=True,
            lifespan=jwks_cache_seconds,
            timeout=jwks_timeout_seconds,
        )

    def authenticate(self, authorization: str | None, session: str | None) -> Mapping[str, Any]:
        if not authorization:
            if session:
                raise ValueError("hosted authentication requires a Bearer access token")
            raise ValueError("authentication required")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise ValueError("authorization must use Bearer")
        if len(token) > 16_384:
            raise ValueError("authentication token exceeds the supported size")

        try:
            header = jwt.get_unverified_header(token)
        except (PyJWTError, TypeError, KeyError) as exc:
            raise ValueError("invalid authentication token") from exc
        algorithm = str(header.get("alg") or "").upper()
        key_id = str(header.get("kid") or "").strip()
        if algorithm not in self._algorithms or not key_id:
            raise ValueError("authentication token uses an unsupported signing key")
        try:
            signing_key = self._signing_keys.get_signing_key_from_jwt(token)
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=self._algorithms,
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway_seconds,
                options={"require": ["sub", "iat", "exp"]},
            )
        except (PyJWTError, TypeError, KeyError, ValueError) as exc:
            raise ValueError("invalid authentication token") from exc

        issued_at = payload.get("iat")
        expires_at = payload.get("exp")
        if not isinstance(issued_at, (int, float)) or isinstance(issued_at, bool):
            raise ValueError("authentication token has an invalid issued-at claim")
        if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
            raise ValueError("authentication token has an invalid expiration claim")
        if expires_at - issued_at > self._maximum_token_lifetime_seconds:
            raise ValueError("authentication token lifetime exceeds policy")
        audiences = payload.get("aud")
        if isinstance(audiences, list) and len(audiences) > 1 and payload.get("azp") != self._audience:
            raise ValueError("authentication token authorized party is invalid")

        subject = payload.get("sub")
        if not isinstance(subject, str) or not subject.strip():
            raise ValueError("authentication token has an invalid subject claim")
        organization = payload.get(self._organization_claim)
        roles = payload.get(self._roles_claim)
        if not isinstance(organization, str) or not organization.strip():
            if self._personal_tenant_secret is None:
                raise ValueError("authentication token is missing the organization claim")
            material = f"agent-os:personal-tenant:v1:{self._issuer}:{subject.strip()}".encode()
            organization = "tenant-" + _b64_encode(hmac.new(
                self._personal_tenant_secret, material, hashlib.sha256,
            ).digest())
            # Provider claims cannot grant authority inside an automatically
            # isolated personal company. Its sole verified subject is owner.
            roles = ["owner"]
        elif (
            not isinstance(roles, (list, tuple, set, frozenset))
            or not all(isinstance(role, str) for role in roles)
        ):
            raise ValueError("authentication token is missing the roles claim")
        # Validate all normalized bounds here so direct Authenticator consumers
        # receive the same guarantees as FastAPI's Principal dependency.
        principal = Principal.from_mapping({"sub": subject, "org": organization, "roles": roles})
        return {
            "sub": principal.subject_id,
            "org": principal.organization_id,
            "roles": sorted(principal.roles),
        }
