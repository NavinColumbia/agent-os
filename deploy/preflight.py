#!/usr/bin/env python3
"""Fail-closed public-launch preflight. It never calls Stripe or sends email."""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _read_env(path: Path) -> dict[str, str]:
    values = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return values
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _configured(values: dict[str, str], key: str) -> bool:
    value = str(os.environ.get(key) or values.get(key) or "").strip()
    return bool(value) and "CHANGE-ME" not in value and "example.com" not in value


def _mounted_offsite(path: Path) -> bool:
    """Require an explicit mount boundary; an ordinary same-host directory is not an off-host backup."""
    try:
        return path.is_dir() and os.access(path, os.W_OK) and path.is_mount()
    except OSError:
        return False


def _host_caddy_service_available() -> bool:
    """The installer uses the distro Caddy systemd unit; a stray binary alone is not a runnable edge."""
    if shutil.which("caddy") is None:
        return False
    return any(path.is_file() for path in (
        Path("/etc/systemd/system/caddy.service"),
        Path("/lib/systemd/system/caddy.service"),
        Path("/usr/lib/systemd/system/caddy.service"),
    ))


def validate_config(values: dict[str, str], *, control_plane: Path | None = None) -> list[dict]:
    checks = []

    def add(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    for key in ("DATABASE_URL", "AUDIT_HMAC_KEY", "VAULT_KEY", "AOS_API_TOKEN"):
        add(key, _configured(values, key), "configured" if _configured(values, key) else "missing or placeholder")

    audit_key = str(os.environ.get("AUDIT_HMAC_KEY") or values.get("AUDIT_HMAC_KEY") or "")
    api_token = str(os.environ.get("AOS_API_TOKEN") or values.get("AOS_API_TOKEN") or "")
    add("AUDIT_HMAC_KEY strength", len(audit_key) >= 32 and "CHANGE-ME" not in audit_key,
        "at least 32 characters" if len(audit_key) >= 32 and "CHANGE-ME" not in audit_key else
        "must be at least 32 non-placeholder characters")
    add("AOS_API_TOKEN strength", len(api_token) >= 32 and "CHANGE-ME" not in api_token,
        "at least 32 characters" if len(api_token) >= 32 and "CHANGE-ME" not in api_token else
        "must be at least 32 non-placeholder characters")

    vault_key = os.environ.get("VAULT_KEY") or values.get("VAULT_KEY") or ""
    try:
        vault_ok = len(base64.urlsafe_b64decode(vault_key.encode())) == 32
    except Exception:
        vault_ok = False
    add("VAULT_KEY format", vault_ok, "valid Fernet key" if vault_ok else "must encode exactly 32 bytes")

    public_host = (os.environ.get("AOS_PUBLIC_HOST") or values.get("AOS_PUBLIC_HOST") or "").strip()
    host_labels = public_host.rstrip(".").split(".")
    host_ok = bool(len(host_labels) >= 2 and len(public_host) <= 253 and all(
        re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
        for label in host_labels) and not any(
        marker in public_host.lower() for marker in ("example.com", "localhost", "127.0.0.1")))
    add("public hostname", host_ok, public_host or "missing AOS_PUBLIC_HOST")

    public_url = (os.environ.get("AOS_PUBLIC_URL") or values.get("AOS_PUBLIC_URL") or "").strip()
    parsed = urlparse(public_url)
    url_ok = parsed.scheme == "https" and parsed.hostname == public_host
    add("public HTTPS URL", url_ok, public_url or "missing AOS_PUBLIC_URL")

    smtp_raw = str(os.environ.get("AOS_SMTP_URL") or values.get("AOS_SMTP_URL") or "").strip()
    smtp = urlparse(smtp_raw)
    smtp_ok = (_configured(values, "AOS_SMTP_URL") and smtp.scheme in {"smtp", "smtps"}
               and bool(smtp.hostname))
    sendgrid = str(os.environ.get("SENDGRID_API_KEY") or values.get("SENDGRID_API_KEY") or "").strip()
    sendgrid_ok = _configured(values, "SENDGRID_API_KEY") and sendgrid.startswith("SG.")
    email_ok = smtp_ok or sendgrid_ok
    add("transactional email", email_ok, "SMTP or SendGrid configured" if email_ok else
        "configure a valid smtp(s) URL or SG.* SendGrid key before public signup")
    sender = str(os.environ.get("AOS_SMTP_FROM") or values.get("AOS_SMTP_FROM") or "").strip()
    sender_ok = bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", sender)) and _configured(
        values, "AOS_SMTP_FROM")
    add("sender address", sender_ok, "configured" if sender_ok else "missing or invalid AOS_SMTP_FROM")

    ntfy_topic = str(os.environ.get("NTFY_TOPIC") or values.get("NTFY_TOPIC") or "").strip()
    ntfy_ok = len(ntfy_topic) >= 24 and "CHANGE-ME" not in ntfy_topic
    add("operator notification topic", ntfy_ok,
        "configured" if ntfy_ok else "NTFY_TOPIC must be a long unguessable value")

    snapshot_pass = str(os.environ.get("AOSNAP_PASS") or values.get("AOSNAP_PASS") or "")
    snapshot_pass_ok = len(snapshot_pass) >= 20 and "CHANGE-ME" not in snapshot_pass
    add("snapshot passphrase", snapshot_pass_ok,
        "configured" if snapshot_pass_ok else "AOSNAP_PASS must be at least 20 non-placeholder characters")
    offsite_raw = (os.environ.get("AOSNAP_OFFSITE_DIR") or values.get("AOSNAP_OFFSITE_DIR") or "").strip()
    offsite_path = Path(offsite_raw).expanduser() if offsite_raw and "CHANGE-ME" not in offsite_raw else None
    offsite_ok = bool(offsite_path and _mounted_offsite(offsite_path))
    add("off-host snapshot destination", offsite_ok,
        str(offsite_path) if offsite_ok else
        "configure a writable mounted AOSNAP_OFFSITE_DIR (an ordinary host directory is rejected)")

    for key in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET",
                "AOS_STRIPE_PRICE_PRO", "AOS_STRIPE_PRICE_ENTERPRISE"):
        add(key, _configured(values, key), "configured" if _configured(values, key) else "missing")
    for plan in ("PRO", "ENTERPRISE"):
        key = f"AOS_STRIPE_PRICE_{plan}"
        price = str(os.environ.get(key) or values.get(key) or "")
        add(f"Stripe {plan.lower()} price format", price.startswith("price_") and _configured(values, key),
            "Stripe price ID" if price.startswith("price_") and _configured(values, key) else
            f"{key} must be a live price_* ID")
    stripe_secret = str(os.environ.get("STRIPE_SECRET_KEY") or values.get("STRIPE_SECRET_KEY") or "")
    webhook_secret = str(os.environ.get("STRIPE_WEBHOOK_SECRET") or
                         values.get("STRIPE_WEBHOOK_SECRET") or "")
    add("Stripe live mode", stripe_secret.startswith("sk_live_") and
        webhook_secret.startswith("whsec_"),
        "live secret and signed webhook configured" if stripe_secret.startswith("sk_live_") and
        webhook_secret.startswith("whsec_") else "public launch requires sk_live_ and whsec_ credentials")
    success = os.environ.get("AOS_STRIPE_SUCCESS_URL") or values.get("AOS_STRIPE_SUCCESS_URL") or ""
    cancel = os.environ.get("AOS_STRIPE_CANCEL_URL") or values.get("AOS_STRIPE_CANCEL_URL") or ""
    add("Stripe return URLs", all(urlparse(item).scheme == "https" and
        urlparse(item).hostname == public_host for item in (success, cancel)),
        "same-origin HTTPS" if success and cancel else "success/cancel URLs missing")

    cp = control_plane or Path(os.environ.get("AOS_CONTROL_PLANE_ROOT") or
                               (ROOT.parent / "control-plane"))
    cp_ok = (cp / "roles").is_dir() and (cp / "hooks" / "manifest_policy.py").is_file()
    add("control plane", cp_ok, str(cp))
    host_caddy = _host_caddy_service_available()
    docker_edge = shutil.which("docker") is not None
    add("Caddy runtime", host_caddy or docker_edge,
        "host Caddy installed" if host_caddy else
        ("Docker Caddy fallback available" if docker_edge else
         "install Caddy or Docker before public launch"))
    return checks


def runtime_checks(values: dict[str, str]) -> list[dict]:
    checks = []
    try:
        import psycopg
        dsn = os.environ.get("DATABASE_URL") or values.get("DATABASE_URL")
        with psycopg.connect(dsn, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            ok = cur.fetchone() == (1,)
        checks.append({"check": "database connectivity", "ok": ok, "detail": "SELECT 1"})
    except Exception as exc:
        checks.append({"check": "database connectivity", "ok": False,
                       "detail": f"{type(exc).__name__}: {str(exc)[:180]}"})
    try:
        import rls_readiness
        result = rls_readiness.evaluate(rls_readiness._catalog())
        gaps = sum(len(result.get(key) or []) for key in (
            "missing_rls", "missing_force_rls", "missing_policy", "missing_tenant_index",
            "indirect_scope_needs_tenant_id", "unknown_unscoped_tables",
            "global_operational_app_role_exposure", "missing_tenant_sequence_grant"))
        gaps += len(result.get("special_policy_needed") or {})
        checks.append({"check": "database tenant isolation", "ok": bool(result.get("ok")),
                       "detail": "RLS readiness green" if result.get("ok") else
                       f"RLS readiness has {gaps} unresolved gap(s)"})
    except Exception as exc:
        checks.append({"check": "database tenant isolation", "ok": False,
                       "detail": f"{type(exc).__name__}: {str(exc)[:180]}"})
    return checks


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=str(ROOT / ".env.local"))
    parser.add_argument("--config-only", action="store_true")
    args = parser.parse_args(argv)
    env_file = Path(args.env_file).resolve()
    values = _read_env(env_file)
    checks = validate_config(values)
    try:
        env_mode = stat.S_IMODE(env_file.stat().st_mode)
        private_env = env_mode & 0o077 == 0
    except OSError:
        env_mode, private_env = 0, False
    checks.append({"check": "environment file permissions", "ok": private_env,
                   "detail": (f"mode {env_mode:04o}" if private_env else
                              "the credential file must exist and be chmod 600")})
    if not args.config_only:
        checks.extend(runtime_checks(values))
    report = {"ok": all(item["ok"] for item in checks), "env_file": str(env_file), "checks": checks,
              "note": "No Stripe request, payment, email, deployment, or DNS mutation was performed."}
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
