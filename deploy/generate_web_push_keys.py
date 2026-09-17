#!/usr/bin/env python3
"""Generate one matching URL-safe VAPID key pair for Agent OS Web Push."""

from __future__ import annotations

import base64

from py_vapid import Vapid02


def _base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def main() -> None:
    vapid = Vapid02()
    vapid.generate_keys()
    private_number = vapid.private_key.private_numbers().private_value
    numbers = vapid.public_key.public_numbers()
    private_key = _base64url(private_number.to_bytes(32, "big"))
    public_key = _base64url(
        b"\x04" + numbers.x.to_bytes(32, "big") + numbers.y.to_bytes(32, "big")
    )
    print(f"AOS_V2_WEB_PUSH_PUBLIC_KEY={public_key}")
    print(f"AOS_V2_WEB_PUSH_PRIVATE_KEY={private_key}")
    print("AOS_V2_WEB_PUSH_SUBJECT=mailto:push-operations@example.com")


if __name__ == "__main__":
    main()
