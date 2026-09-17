"""At-rest protection and delivery boundary for standards-based Web Push."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any, Mapping

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

from agent_os.domain.web_push import WebPushSubscriptionMaterial


class WebPushSubscriptionProtector:
    """Encrypt browser capability URLs and key material with bound AAD.

    The composition root supplies the already-separated capability secret.
    HKDF derives an independent AES-256-GCM key for this purpose so the stored
    bytes cannot be replayed across tenants, people, devices, or purposes.
    A cloud KMS adapter can later replace this class without changing records
    or API semantics.
    """

    VERSION = 1

    def __init__(self, root_secret: str | bytes) -> None:
        secret = root_secret.encode() if isinstance(root_secret, str) else root_secret
        if len(secret) < 32:
            raise ValueError("Web Push protection requires at least 32 bytes of key material")
        self._key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=b"agent-os:web-push-subscription:v1",
        ).derive(secret)
        self._identifier_key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=b"agent-os:web-push-identifiers:v1",
        ).derive(secret)

    @staticmethod
    def _aad(tenant_id: str, subject_id: str, subscription_id: str) -> bytes:
        return json.dumps(
            ["agent-os:web-push-subscription:v1", tenant_id, subject_id, subscription_id],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()

    def subscription_id(self, tenant_id: str, subject_id: str, device_id: str) -> str:
        material = json.dumps(
            [tenant_id, subject_id, device_id], ensure_ascii=False, separators=(",", ":"),
        ).encode()
        return "push-" + hmac.new(
            self._identifier_key, material, hashlib.sha256,
        ).hexdigest()

    def endpoint_hash(self, endpoint: str) -> str:
        return hmac.new(
            self._identifier_key, endpoint.encode(), hashlib.sha256,
        ).hexdigest()

    def seal(
        self,
        material: WebPushSubscriptionMaterial,
        *,
        tenant_id: str,
        subject_id: str,
        subscription_id: str,
    ) -> bytes:
        plaintext = json.dumps(
            material.to_secret_dict(),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        nonce = os.urandom(12)
        ciphertext = AESGCM(self._key).encrypt(
            nonce, plaintext, self._aad(tenant_id, subject_id, subscription_id),
        )
        return bytes((self.VERSION,)) + nonce + ciphertext

    def open(
        self,
        sealed: bytes,
        *,
        tenant_id: str,
        subject_id: str,
        subscription_id: str,
    ) -> WebPushSubscriptionMaterial:
        if len(sealed) < 30 or sealed[0] != self.VERSION:
            raise ValueError("Web Push subscription ciphertext version is unsupported")
        plaintext = AESGCM(self._key).decrypt(
            sealed[1:13],
            sealed[13:],
            self._aad(tenant_id, subject_id, subscription_id),
        )
        try:
            raw: Mapping[str, Any] = json.loads(plaintext)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise ValueError("Web Push subscription ciphertext is invalid") from exc
        return WebPushSubscriptionMaterial.from_secret_dict(raw)


def public_subscription_record(row: Mapping[str, Any], *, duplicate: bool = False) -> dict[str, Any]:
    """Project safe device metadata without the endpoint or browser keys."""

    return {
        "subscription_id": str(row["subscription_id"]),
        "device_id": str(row["device_id"]),
        "device_name": str(row["device_name"]),
        "provider": str(row["provider"]),
        "active": bool(row["active"]),
        "expiration_time": (
            None if row.get("expiration_time") is None
            else int(row["expiration_time"])
        ),
        "version": int(row["version"]),
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
        "revoked_at": (
            None if row.get("revoked_at") is None else row["revoked_at"].isoformat()
        ),
        "duplicate": duplicate,
    }


def registration_fingerprint(
    *,
    tenant_id: str,
    subject_id: str,
    device_id: str,
    device_name: str,
    audience_ids: tuple[str, ...],
    material: WebPushSubscriptionMaterial,
) -> str:
    encoded = json.dumps({
        "tenant_id": tenant_id,
        "subject_id": subject_id,
        "device_id": device_id,
        "device_name": device_name,
        "audience_ids": list(audience_ids),
        "subscription": material.to_secret_dict(),
    }, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode()).hexdigest()
