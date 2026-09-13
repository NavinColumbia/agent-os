from __future__ import annotations

import importlib.util
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "local_pilot_preflight", ROOT / "deploy" / "local_pilot_preflight.py",
)
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


def valid_values(workspace: Path, docker_gid: int) -> dict[str, str]:
    return {
        "AOS_V2_POSTGRES_PASSWORD": "p" * 48,
        "AOS_V2_DATABASE_RUNTIME_PASSWORD": "d" * 48,
        "AOS_V2_AUTH_SECRET": "a" * 64,
        "AOS_V2_CAPABILITY_SECRET": "c" * 64,
        "AOS_V2_PUBLIC_PORT": "18088",
        "AOS_V2_PUBLIC_BASE_URL": "https://pilot.example-tail.ts.net:10000",
        "AOS_V2_APPLICATION_VERSION": "local-pilot",
        "AOS_V2_IDENTITY_MODE": "hmac",
        "AOS_V2_BILLING_MODE": "disabled",
        "AOS_V2_MODEL": "google:gemini-3.8-flash",
        "GEMINI_API_KEY": "private-google-key",
        "AOS_V2_SANDBOX_IMAGE": "python@example@sha256:" + "a" * 64,
        "AOS_V2_SANDBOX_WORKSPACE_ROOT": str(workspace),
        "AOS_V2_SANDBOX_TIMEOUT_SECONDS": "300",
        "AOS_PILOT_UID": str(os.getuid()),
        "AOS_PILOT_GID": str(os.getgid()),
        "AOS_PILOT_DOCKER_GID": str(docker_gid),
    }


def test_preflight_accepts_private_invite_only_free_pilot_without_leaking_key(tmp_path):
    environment = tmp_path / "pilot.env"
    environment.write_text("placeholder\n")
    environment.chmod(0o600)
    workspace = tmp_path / "sandbox"
    workspace.mkdir()
    socket_info = os.stat("/var/run/docker.sock")
    values = valid_values(workspace, socket_info.st_gid)

    checks = preflight.validate(values, env_file=environment)

    assert all(item["ok"] for item in checks), checks
    assert "private-google-key" not in str(checks)


def test_preflight_rejects_open_billing_placeholder_key_and_reused_secrets(tmp_path):
    environment = tmp_path / "pilot.env"
    environment.write_text("placeholder\n")
    environment.chmod(0o644)
    workspace = tmp_path / "sandbox"
    workspace.mkdir()
    socket_info = os.stat("/var/run/docker.sock")
    values = valid_values(workspace, socket_info.st_gid)
    values.update({
        "AOS_V2_CAPABILITY_SECRET": values["AOS_V2_AUTH_SECRET"],
        "AOS_V2_DATABASE_RUNTIME_PASSWORD": values["AOS_V2_POSTGRES_PASSWORD"],
        "AOS_V2_BILLING_MODE": "stripe",
        "GEMINI_API_KEY": "CHANGE_ME_FREE_AUTH_KEY",
    })

    failed = {
        item["check"] for item in preflight.validate(values, env_file=environment)
        if not item["ok"]
    }

    assert {
        "owner-only environment file", "independent strong signing secrets",
        "separate strong database credentials", "billing disabled", "model/provider binding",
    } <= failed
