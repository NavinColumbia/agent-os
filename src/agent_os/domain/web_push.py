"""Validated, secret-bearing Web Push subscription material.

The endpoint is a bearer capability and the ``auth`` value is encryption key
material.  Neither belongs in API responses, logs, experience events, or
plaintext database columns.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import re
from typing import Any, Mapping
from urllib.parse import urlparse


_EXACT_PUSH_HOSTS = frozenset({
    "fcm.googleapis.com",
    "updates.push.services.mozilla.com",
})


def _decode_base64url(value: str, *, label: str, minimum: int, maximum: int) -> bytes:
    if (
        not value
        or len(value) > maximum * 2
        or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None
    ):
        raise ValueError(f"Web Push {label} is missing or too long")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Web Push {label} is not valid base64url") from exc
    if not minimum <= len(decoded) <= maximum:
        raise ValueError(f"Web Push {label} has an invalid decoded length")
    return decoded


def push_endpoint_host(endpoint: str) -> str:
    """Return an allowlisted public push-service host.

    A subscription endpoint is supplied by the browser but later contacted by
    a privileged worker.  Restricting it to standards-based browser push
    services prevents that capability URL from becoming a generic SSRF target.
    """

    if not 1 <= len(endpoint) <= 4_096 or any(char in endpoint for char in "\r\n\0"):
        raise ValueError("Web Push endpoint is missing or too long")
    parsed = urlparse(endpoint)
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.port not in {None, 443}
    ):
        raise ValueError("Web Push endpoint must be a credential-free HTTPS URL")
    if host not in _EXACT_PUSH_HOSTS and not host.endswith(".push.apple.com"):
        raise ValueError("Web Push endpoint provider is not approved")
    return host


@dataclass(frozen=True)
class WebPushSubscriptionMaterial:
    endpoint: str
    p256dh: str
    auth: str
    expiration_time: int | None = None

    def __post_init__(self) -> None:
        push_endpoint_host(self.endpoint)
        # PushSubscription.getKey("p256dh") is an uncompressed P-256 point;
        # permissive lengths would admit data that fails only inside a worker.
        p256dh = _decode_base64url(
            self.p256dh, label="p256dh key", minimum=65, maximum=65,
        )
        if p256dh[0] != 0x04:
            raise ValueError("Web Push p256dh key must be an uncompressed P-256 point")
        _decode_base64url(self.auth, label="auth secret", minimum=16, maximum=16)
        if self.expiration_time is not None and not 0 < self.expiration_time <= 9_999_999_999_999:
            raise ValueError("Web Push expiration time is invalid")

    def to_secret_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "keys": {"p256dh": self.p256dh, "auth": self.auth},
            "expirationTime": self.expiration_time,
        }

    @classmethod
    def from_secret_dict(cls, raw: Mapping[str, Any]) -> "WebPushSubscriptionMaterial":
        keys = raw.get("keys")
        if not isinstance(keys, Mapping):
            raise ValueError("Web Push subscription keys are missing")
        expiration = raw.get("expirationTime")
        return cls(
            endpoint=str(raw.get("endpoint") or ""),
            p256dh=str(keys.get("p256dh") or ""),
            auth=str(keys.get("auth") or ""),
            expiration_time=None if expiration is None else int(expiration),
        )
