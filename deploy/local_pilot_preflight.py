#!/usr/bin/env python3
"""Secret-redacting checks for the invite-only, single-laptop pilot."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV = ROOT / ".runtime" / "local-pilot" / "pilot.env"


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip().strip("'\"")
    return values


def configured(values: dict[str, str], name: str) -> bool:
    value = values.get(name, "").strip()
    return bool(value) and "CHANGE_ME" not in value and "tailnet.ts.net" not in value


def validate(values: dict[str, str], *, env_file: Path) -> list[dict[str, object]]:
    checks: list[dict[str, object]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    try:
        info = env_file.stat()
        mode = stat.S_IMODE(info.st_mode)
        private = stat.S_ISREG(info.st_mode) and not env_file.is_symlink() and mode == 0o600
        owner = info.st_uid == os.getuid()
    except OSError:
        mode, private, owner = 0, False, False
    add("owner-only environment file", private and owner,
        f"regular owner file mode {mode:04o}" if private and owner else
        "pilot.env must be an owner-owned regular non-symlink file with mode 0600")

    required = (
        "AOS_V2_POSTGRES_PASSWORD", "AOS_V2_DATABASE_RUNTIME_PASSWORD",
        "AOS_V2_AUTH_SECRET", "AOS_V2_CAPABILITY_SECRET",
        "AOS_V2_PUBLIC_BASE_URL", "AOS_V2_MODEL", "AOS_V2_SANDBOX_IMAGE",
        "AOS_V2_SANDBOX_WORKSPACE_ROOT", "AOS_PILOT_UID", "AOS_PILOT_GID",
        "AOS_PILOT_DOCKER_GID",
    )
    for name in required:
        add(name, configured(values, name), "configured" if configured(values, name) else "missing")

    auth = values.get("AOS_V2_AUTH_SECRET", "")
    capability = values.get("AOS_V2_CAPABILITY_SECRET", "")
    database_owner = values.get("AOS_V2_POSTGRES_PASSWORD", "")
    database_runtime = values.get("AOS_V2_DATABASE_RUNTIME_PASSWORD", "")
    strong_database = (
        len(database_owner.encode()) >= 32 and len(database_runtime.encode()) >= 32
        and database_owner != database_runtime
    )
    add("separate strong database credentials", strong_database,
        "owner and runtime credentials are separate" if strong_database else
        "database owner/runtime passwords must be distinct and at least 32 bytes")
    strong = (
        len(auth.encode()) >= 32 and len(capability.encode()) >= 32 and auth != capability
        and auth not in {database_owner, database_runtime}
        and capability not in {database_owner, database_runtime}
    )
    add("independent strong signing secrets", strong,
        "separate secrets of at least 32 bytes" if strong else "missing, weak, or reused")

    add("invite-only identity", values.get("AOS_V2_IDENTITY_MODE") == "hmac",
        "signed expiring invitations" if values.get("AOS_V2_IDENTITY_MODE") == "hmac" else
        "pilot requires HMAC invitations")
    add("billing disabled", values.get("AOS_V2_BILLING_MODE") == "disabled",
        "no customer charge path" if values.get("AOS_V2_BILLING_MODE") == "disabled" else
        "pilot must not claim paid entitlements")

    raw_url = values.get("AOS_V2_PUBLIC_BASE_URL", "")
    try:
        public = urlparse(raw_url)
        public_ok = (
            public.scheme == "https" and bool(public.hostname)
            and public.hostname.endswith(".ts.net") and public.port in {443, 8443, 10000}
            and not public.username and not public.password and public.path in {"", "/"}
            and not public.query and not public.fragment
        )
    except ValueError:
        public_ok = False
    add("Tailscale Funnel HTTPS origin", public_ok,
        raw_url if public_ok else "use the stable https://<machine>.<tailnet>.ts.net[:port] origin")

    model = values.get("AOS_V2_MODEL", "")
    provider = model.partition(":")[0]
    provider_keys = {
        "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
        "openai": ("OPENAI_API_KEY",),
        "anthropic": ("ANTHROPIC_API_KEY",),
    }
    candidates = provider_keys.get(provider, ())
    provider_ready = bool(candidates) and any(configured(values, name) for name in candidates)
    add("model/provider binding", provider_ready,
        f"{provider} credential configured" if provider_ready else
        f"configure one supported key for {provider or 'the selected provider'}")

    image = values.get("AOS_V2_SANDBOX_IMAGE", "")
    digest = image.rpartition("@sha256:")[2]
    pinned = len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
    add("immutable sandbox image", pinned, "digest pinned" if pinned else
        "sandbox image must use @sha256:<64 hex>")

    root_raw = values.get("AOS_V2_SANDBOX_WORKSPACE_ROOT", "")
    workspace = Path(root_raw) if root_raw else None
    workspace_ok = bool(
        workspace and workspace.is_absolute() and workspace.is_dir()
        and os.access(workspace, os.W_OK)
    )
    add("sandbox workspace", workspace_ok,
        str(workspace) if workspace_ok else "absolute writable directory required")

    docker_socket = Path("/var/run/docker.sock")
    try:
        socket_info = docker_socket.stat()
        socket_ok = stat.S_ISSOCK(socket_info.st_mode) and not bool(socket_info.st_mode & stat.S_IWOTH)
        expected_gid = int(values.get("AOS_PILOT_DOCKER_GID", "-1"))
        group_ok = expected_gid == socket_info.st_gid
    except (OSError, ValueError):
        socket_ok, group_ok = False, False
    add("Docker control socket", socket_ok, "socket present and not world-writable" if socket_ok else "unsafe or missing")
    add("Docker group identity", group_ok, "matches local socket" if group_ok else "regenerate pilot.env")

    try:
        uid_ok = int(values.get("AOS_PILOT_UID", "-1")) == os.getuid()
        gid_ok = int(values.get("AOS_PILOT_GID", "-1")) == os.getgid()
    except ValueError:
        uid_ok, gid_ok = False, False
    add("host user identity", uid_ok and gid_ok,
        "matches the current WSL user" if uid_ok and gid_ok else "regenerate pilot.env")

    try:
        port = int(values.get("AOS_V2_PUBLIC_PORT", ""))
        port_ok = 1024 <= port <= 65535
    except ValueError:
        port, port_ok = 0, False
    add("loopback application port", port_ok,
        f"127.0.0.1:{port}" if port_ok else
        "AOS_V2_PUBLIC_PORT must be between 1024 and 65535")

    try:
        timeout = int(values.get("AOS_V2_SANDBOX_TIMEOUT_SECONDS", ""))
        timeout_ok = 1 <= timeout <= 3600
    except ValueError:
        timeout, timeout_ok = 0, False
    add("bounded sandbox timeout", timeout_ok,
        f"{timeout} seconds" if timeout_ok else "must be between 1 and 3600 seconds")
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=str(DEFAULT_ENV))
    args = parser.parse_args(argv)
    env_file = Path(args.env_file).resolve()
    checks = validate(read_env(env_file), env_file=env_file)
    report = {
        "ok": all(bool(item["ok"]) for item in checks),
        "env_file": str(env_file),
        "checks": checks,
        "note": "No secret value was printed and no network or deployment mutation was performed.",
    }
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
