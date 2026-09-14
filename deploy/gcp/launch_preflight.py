#!/usr/bin/env python3
"""Offline, secret-redacting configuration gate for first GCP activation."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlparse


DEFAULT_BUILDER_IMAGE = (
    "gcr.io/cloud-builders/docker@sha256:"
    "3d00b6c1a9b862621c30fc74d4f2abfc62bcbdee631ed3febd31e7edbdf6252c"
)
PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
REGION = re.compile(r"^[a-z]+-[a-z]+[0-9]$")
DNS_ZONE = re.compile(r"^[a-z][a-z0-9-]{0,61}[a-z0-9]$")
DATABASE_ROLE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,62}$")
PINNED_IMAGE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
PRICE_ID = re.compile(r"^price_[A-Za-z0-9]+$")
RELEASE_ID = re.compile(r"^[0-9a-f]{40}$")
CHANNEL = re.compile(r"^projects/[^/]+/notificationChannels/[0-9]+$")

NONSECRET_REQUIRED = (
    "GCP_PROJECT_ID",
    "GCP_SANDBOX_PROJECT_ID",
    "GCP_APP_PROJECT_ID",
    "AOS_V2_PUBLIC_BASE_URL",
    "AOS_V2_APPS_BASE_URL",
    "AOS_V2_OIDC_ISSUER",
    "AOS_V2_OIDC_AUDIENCE",
    "AOS_V2_OIDC_JWKS_URL",
    "AOS_V2_OIDC_AUTHORIZATION_URL",
    "AOS_V2_OIDC_TOKEN_URL",
    "AOS_V2_OIDC_CLIENT_ID",
    "AOS_V2_STRIPE_STARTER_PRICE_ID",
    "AOS_V2_STRIPE_GROWTH_PRICE_ID",
    "AOS_V2_MODEL",
    "AOS_V2_DATABASE_RUNTIME_ROLE",
    "AOS_ALERT_NOTIFICATION_CHANNELS",
)
BOOTSTRAP_SECRETS = (
    "AOS_V2_MIGRATION_DATABASE_URL",
    "AOS_V2_SYSTEM_DATABASE_URL",
    "AOS_V2_APPLICATION_DATABASE_URL",
    "AOS_V2_STRIPE_SECRET_KEY",
    "AOS_V2_STRIPE_WEBHOOK_SECRET",
    "AOS_V2_MODEL_PROVIDER_KEY",
)
ALIASES = {
    "GCP_PROJECT_ID": "TF_VAR_project_id",
    "GCP_SANDBOX_PROJECT_ID": "TF_VAR_sandbox_project_id",
    "GCP_APP_PROJECT_ID": "TF_VAR_app_project_id",
    "GCP_REGION": "TF_VAR_region",
    "GCP_DNS_MANAGED_ZONE": "TF_VAR_dns_managed_zone",
    "AOS_V2_PUBLIC_BASE_URL": "TF_VAR_public_base_url",
    "AOS_V2_APPS_BASE_URL": "TF_VAR_apps_base_url",
    "AOS_V2_OIDC_ISSUER": "TF_VAR_oidc_issuer",
    "AOS_V2_OIDC_AUDIENCE": "TF_VAR_oidc_audience",
    "AOS_V2_OIDC_JWKS_URL": "TF_VAR_oidc_jwks_url",
    "AOS_V2_OIDC_AUTHORIZATION_URL": "TF_VAR_oidc_authorization_url",
    "AOS_V2_OIDC_TOKEN_URL": "TF_VAR_oidc_token_url",
    "AOS_V2_OIDC_CLIENT_ID": "TF_VAR_oidc_client_id",
    "AOS_V2_OIDC_AUTHORIZATION_AUDIENCE_PARAMETER": (
        "TF_VAR_oidc_authorization_audience_parameter"
    ),
    "AOS_V2_STRIPE_STARTER_PRICE_ID": "TF_VAR_stripe_starter_price_id",
    "AOS_V2_STRIPE_GROWTH_PRICE_ID": "TF_VAR_stripe_growth_price_id",
    "AOS_V2_MODEL": "TF_VAR_model",
    "AOS_V2_MODEL_PROVIDER_SECRET_ENVIRONMENT": (
        "TF_VAR_model_provider_secret_environment"
    ),
    "AOS_V2_DATABASE_RUNTIME_ROLE": "TF_VAR_database_runtime_role",
    "AOS_V2_APP_BUILDER_IMAGE": "TF_VAR_app_builder_image",
    "AOS_ALERT_NOTIFICATION_CHANNELS": "TF_VAR_alert_notification_channels",
}


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _configured(values: Mapping[str, str], key: str) -> bool:
    value = str(values.get(key, "")).strip()
    return bool(value) and "CHANGE_ME" not in value


def _hostname(value: str) -> bool:
    return (
        value.isascii()
        and not value.endswith(".")
        and "." in value
        and len(value) <= 253
        and all(
            1 <= len(label) <= 63
            and label[0].isalnum()
            and label[-1].isalnum()
            and all(character.isalnum() or character == "-" for character in label)
            for label in value.split(".")
        )
    )


def _https(value: str, *, origin: bool) -> tuple[bool, str | None]:
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return False, None
    valid = (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and _hostname(parsed.hostname)
        and not parsed.username
        and not parsed.password
        and not parsed.fragment
        and port is None
        and (not origin or (parsed.path in {"", "/"} and not parsed.query))
    )
    return valid, parsed.hostname


def _database(value: str) -> tuple[bool, str | None]:
    try:
        parsed = urlparse(value)
        parsed.port
    except ValueError:
        return False, None
    modes = parse_qs(parsed.query).get("sslmode", [])
    username = unquote(parsed.username or "")
    valid = (
        parsed.scheme in {"postgres", "postgresql"}
        and bool(parsed.hostname)
        and bool(username)
        and parsed.password is not None
        and parsed.path not in {"", "/"}
        and not parsed.fragment
        and len(modes) == 1
        and modes[0] in {"require", "verify-ca", "verify-full"}
    )
    return valid, username or None


def evaluate(values: Mapping[str, str], *, require_bootstrap_secrets: bool) -> dict[str, Any]:
    values = {
        key: str(values.get(key) or values.get(alias) or "")
        for key, alias in ALIASES.items()
    } | {
        key: str(value) for key, value in values.items() if key not in ALIASES
    }
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    missing = [key for key in NONSECRET_REQUIRED if not _configured(values, key)]
    add(
        "required non-secret configuration",
        not missing,
        "complete" if not missing else f"missing: {', '.join(missing)}",
    )

    projects = [str(values.get(key, "")) for key in (
        "GCP_PROJECT_ID", "GCP_SANDBOX_PROJECT_ID", "GCP_APP_PROJECT_ID",
    )]
    projects_ok = (
        all(PROJECT_ID.fullmatch(item) for item in projects)
        and len(set(projects)) == 3
    )
    add(
        "three isolated GCP projects",
        projects_ok,
        "three valid distinct IDs" if projects_ok else "IDs must be valid and distinct",
    )
    region = str(values.get("GCP_REGION") or "us-central1")
    add("GCP region", bool(REGION.fullmatch(region)), "valid region" if REGION.fullmatch(region) else "invalid")

    public_url = str(values.get("AOS_V2_PUBLIC_BASE_URL", ""))
    apps_url = str(values.get("AOS_V2_APPS_BASE_URL", ""))
    public_ok, public_host = _https(public_url, origin=True)
    apps_ok, apps_host = _https(apps_url, origin=True)
    add(
        "separate public HTTPS origins",
        public_ok and apps_ok and public_host != apps_host,
        "valid and separate" if public_ok and apps_ok and public_host != apps_host else "invalid or overlapping",
    )

    oidc_urls = [str(values.get(key, "")) for key in (
        "AOS_V2_OIDC_ISSUER", "AOS_V2_OIDC_JWKS_URL",
        "AOS_V2_OIDC_AUTHORIZATION_URL", "AOS_V2_OIDC_TOKEN_URL",
    )]
    oidc_endpoints_ok = all(_https(item, origin=False)[0] for item in oidc_urls)
    add(
        "OIDC HTTPS endpoints",
        oidc_endpoints_ok,
        "valid public HTTPS endpoints" if oidc_endpoints_ok else "one or more endpoints are missing or invalid",
    )
    audience = str(values.get("AOS_V2_OIDC_AUDIENCE", ""))
    client_id = str(values.get("AOS_V2_OIDC_CLIENT_ID", ""))
    audience_parameter = str(values.get("AOS_V2_OIDC_AUTHORIZATION_AUDIENCE_PARAMETER", ""))
    oidc_client_ok = (
        bool(audience.strip()) and 1 <= len(client_id) <= 512
        and (not audience_parameter or bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", audience_parameter)))
    )
    add(
        "OIDC public client contract",
        oidc_client_ok,
        "audience, PKCE client, and optional audience parameter are bounded"
        if oidc_client_ok else "audience, client ID, or audience parameter is missing or invalid",
    )

    prices = [str(values.get(key, "")) for key in (
        "AOS_V2_STRIPE_STARTER_PRICE_ID", "AOS_V2_STRIPE_GROWTH_PRICE_ID",
    )]
    prices_ok = all(PRICE_ID.fullmatch(item) for item in prices) and len(set(prices)) == 2
    add(
        "Stripe price IDs",
        prices_ok,
        "two distinct price IDs" if prices_ok else "price IDs are missing, malformed, or duplicated",
    )

    model = str(values.get("AOS_V2_MODEL", ""))
    provider_environment = str(
        values.get("AOS_V2_MODEL_PROVIDER_SECRET_ENVIRONMENT") or "OPENAI_API_KEY"
    )
    expected_environment = (
        {"OPENAI_API_KEY"} if model.startswith("openai:")
        else {"ANTHROPIC_API_KEY"} if model.startswith("anthropic:")
        else {"GEMINI_API_KEY", "GOOGLE_API_KEY"} if model.startswith("google:")
        else set()
    )
    model_ok = provider_environment in expected_environment
    add(
        "model provider binding",
        model_ok,
        "supported provider and matching isolated secret environment"
        if model_ok else "model or provider secret environment is unsupported/mismatched",
    )
    role = str(values.get("AOS_V2_DATABASE_RUNTIME_ROLE", ""))
    role_ok = bool(DATABASE_ROLE.fullmatch(role))
    add(
        "database runtime role", role_ok,
        "valid bounded PostgreSQL role" if role_ok else "role is missing or invalid",
    )

    builder = str(values.get("AOS_V2_APP_BUILDER_IMAGE") or DEFAULT_BUILDER_IMAGE)
    builder_ok = bool(PINNED_IMAGE.fullmatch(builder))
    add(
        "build image provenance", builder_ok,
        "digest-pinned builder" if builder_ok else "builder is not digest-pinned",
    )
    release = str(values.get("AOS_RELEASE_ID", ""))
    add(
        "release identity",
        not release or bool(RELEASE_ID.fullmatch(release)),
        "Git HEAD will be used" if not release else "exact commit configured",
    )
    zone = str(values.get("GCP_DNS_MANAGED_ZONE", ""))
    add(
        "DNS ownership mode",
        not zone or bool(DNS_ZONE.fullmatch(zone)),
        "external DNS" if not zone else "valid Cloud DNS zone name",
    )
    raw_channels = str(values.get("AOS_ALERT_NOTIFICATION_CHANNELS", ""))
    try:
        channels = json.loads(raw_channels)
    except json.JSONDecodeError:
        channels = None
    add(
        "production alert delivery",
        isinstance(channels, list) and bool(channels)
        and all(isinstance(item, str) and CHANNEL.fullmatch(item) for item in channels),
        "at least one valid Monitoring notification channel",
    )

    missing_secrets = [key for key in BOOTSTRAP_SECRETS if not _configured(values, key)]
    if require_bootstrap_secrets:
        add(
            "initial Secret Manager payloads",
            not missing_secrets,
            "present (values redacted)" if not missing_secrets
            else f"missing: {', '.join(missing_secrets)}",
        )
        database_results = {
            key: _database(str(values.get(key, "")))
            for key in BOOTSTRAP_SECRETS[:3]
        }
        databases_ok = all(result[0] for result in database_results.values())
        add(
            "TLS PostgreSQL DSNs",
            databases_ok,
            "three valid password-authenticated TLS DSNs (values redacted)"
            if databases_ok else "one or more DSNs are missing, malformed, or do not require TLS",
        )
        migration_user = database_results["AOS_V2_MIGRATION_DATABASE_URL"][1]
        runtime_users = {
            database_results["AOS_V2_SYSTEM_DATABASE_URL"][1],
            database_results["AOS_V2_APPLICATION_DATABASE_URL"][1],
        }
        separation_ok = (
            len(runtime_users) == 1 and role in runtime_users and migration_user not in runtime_users
        )
        add(
            "database privilege separation",
            separation_ok,
            "migration owner is separate; both runtimes use the constrained login"
            if separation_ok else "migration/runtime database principals are not safely separated",
        )
        stripe_key = str(values.get("AOS_V2_STRIPE_SECRET_KEY", ""))
        webhook_key = str(values.get("AOS_V2_STRIPE_WEBHOOK_SECRET", ""))
        stripe_secrets_ok = (
            stripe_key.startswith("sk_live_") and len(stripe_key) >= 20
            and webhook_key.startswith("whsec_") and len(webhook_key) >= 20
        )
        add(
            "Stripe live secrets",
            stripe_secrets_ok,
            "live key and webhook signing secret present (values redacted)"
            if stripe_secrets_ok else "live key or webhook secret is missing/invalid",
        )
        model_key = str(values.get("AOS_V2_MODEL_PROVIDER_KEY", ""))
        model_secret_ok = len(model_key) >= 20
        add(
            "model provider secret",
            model_secret_ok,
            "present and bounded (value redacted)" if model_secret_ok else "missing or too short",
        )

    return {
        "ok": all(check["ok"] for check in checks),
        "mode": "bootstrap" if require_bootstrap_secrets else "nonsecret",
        "checks": checks,
        "note": "No credential value, network request, cloud mutation, or payment was emitted.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--require-bootstrap-secrets", action="store_true")
    args = parser.parse_args(argv)
    values = dict(os.environ)
    file_check = None
    if args.env_file is not None:
        try:
            mode = stat.S_IMODE(args.env_file.stat().st_mode)
            file_check = {
                "check": "credential file permissions",
                "ok": mode & 0o077 == 0,
                "detail": f"mode {mode:04o}" if mode & 0o077 == 0 else "must be chmod 600",
            }
            values.update(read_env_file(args.env_file))
        except OSError:
            file_check = {
                "check": "credential file permissions", "ok": False,
                "detail": "file is missing or unreadable",
            }
    report = evaluate(values, require_bootstrap_secrets=args.require_bootstrap_secrets)
    if file_check is not None:
        report["checks"].append(file_check)
        report["ok"] = report["ok"] and file_check["ok"]
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
