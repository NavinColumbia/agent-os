from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "gcp_launch_preflight", ROOT / "deploy" / "gcp" / "launch_preflight.py",
)
assert SPEC is not None and SPEC.loader is not None
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


def complete_values() -> dict[str, str]:
    return {
        "GCP_PROJECT_ID": "agentos-control",
        "GCP_SANDBOX_PROJECT_ID": "agentos-sandbox",
        "GCP_APP_PROJECT_ID": "agentos-apps",
        "GCP_REGION": "us-central1",
        "AOS_V2_PUBLIC_BASE_URL": "https://agent.example.test",
        "AOS_V2_APPS_BASE_URL": "https://apps.example.test",
        "AOS_V2_OIDC_ISSUER": "https://identity.example.test/",
        "AOS_V2_OIDC_AUDIENCE": "agent-os-api",
        "AOS_V2_OIDC_JWKS_URL": "https://identity.example.test/.well-known/jwks.json",
        "AOS_V2_OIDC_AUTHORIZATION_URL": "https://identity.example.test/authorize",
        "AOS_V2_OIDC_TOKEN_URL": "https://identity.example.test/oauth/token",
        "AOS_V2_OIDC_CLIENT_ID": "public-pkce-client",
        "AOS_V2_OIDC_AUTHORIZATION_AUDIENCE_PARAMETER": "audience",
        "AOS_V2_STRIPE_STARTER_PRICE_ID": "price_starter123",
        "AOS_V2_STRIPE_GROWTH_PRICE_ID": "price_growth123",
        "AOS_V2_MODEL": "openai:gpt-5-mini",
        "AOS_V2_MODEL_PROVIDER_SECRET_ENVIRONMENT": "OPENAI_API_KEY",
        "AOS_V2_DATABASE_RUNTIME_ROLE": "agentos_runtime",
        "AOS_ALERT_NOTIFICATION_CHANNELS": (
            '["projects/agentos-control/notificationChannels/123"]'
        ),
        "AOS_V2_MIGRATION_DATABASE_URL": (
            "postgresql://migration:secret@db.example.test/agentos?sslmode=require"
        ),
        "AOS_V2_SYSTEM_DATABASE_URL": (
            "postgresql://agentos_runtime:secret@db.example.test/agentos?sslmode=require"
        ),
        "AOS_V2_APPLICATION_DATABASE_URL": (
            "postgresql://agentos_runtime:secret@db.example.test/agentos?sslmode=require"
        ),
        "AOS_V2_STRIPE_SECRET_KEY": "sk_live_abcdefghijklmnopqrstuvwxyz",
        "AOS_V2_STRIPE_WEBHOOK_SECRET": "whsec_abcdefghijklmnopqrstuvwxyz",
        "AOS_V2_MODEL_PROVIDER_KEY": "sk-proj-abcdefghijklmnopqrstuvwxyz",
    }


def test_launch_preflight_accepts_complete_isolated_live_configuration():
    report = preflight.evaluate(complete_values(), require_bootstrap_secrets=True)
    assert report["ok"] is True
    assert all(check["ok"] for check in report["checks"])


def test_launch_preflight_names_missing_inputs_without_exposing_values():
    values = complete_values()
    values.pop("GCP_APP_PROJECT_ID")
    values["AOS_V2_STRIPE_SECRET_KEY"] = "sk_live_do-not-print-this-value"
    values["AOS_V2_MODEL_PROVIDER_KEY"] = "tiny"

    report = preflight.evaluate(values, require_bootstrap_secrets=True)
    rendered = str(report)

    assert report["ok"] is False
    assert "GCP_APP_PROJECT_ID" in rendered
    assert "sk_live_do-not-print-this-value" not in rendered
    assert "tiny" not in rendered


def test_launch_preflight_rejects_overlapping_projects_origins_and_database_roles():
    values = complete_values()
    values["GCP_APP_PROJECT_ID"] = values["GCP_PROJECT_ID"]
    values["AOS_V2_APPS_BASE_URL"] = values["AOS_V2_PUBLIC_BASE_URL"]
    values["AOS_V2_MIGRATION_DATABASE_URL"] = values["AOS_V2_SYSTEM_DATABASE_URL"]
    report = preflight.evaluate(values, require_bootstrap_secrets=True)

    failed = {item["check"] for item in report["checks"] if not item["ok"]}
    assert "three isolated GCP projects" in failed
    assert "separate public HTTPS origins" in failed
    assert "database privilege separation" in failed


def test_launch_preflight_env_file_parser_and_permissions_are_secret_safe(tmp_path: Path):
    path = tmp_path / "launch.env"
    path.write_text("export VALUE='secret words'\nPLAIN=value\n", encoding="utf-8")
    path.chmod(0o600)
    assert preflight.read_env_file(path) == {"VALUE": "secret words", "PLAIN": "value"}


def test_checked_in_launch_template_fails_closed_until_every_placeholder_is_replaced():
    values = preflight.read_env_file(ROOT / "deploy" / "gcp" / "launch.env.example")
    report = preflight.evaluate(values, require_bootstrap_secrets=True)
    assert report["ok"] is False
    required = report["checks"][0]
    assert "GCP_PROJECT_ID" in required["detail"]
    assert "AOS_V2_MODEL_PROVIDER_KEY" in str(report)


def test_launch_preflight_accepts_github_workflow_terraform_aliases():
    canonical = complete_values()
    aliases = {
        preflight.ALIASES[key]: value
        for key, value in canonical.items()
        if key in preflight.ALIASES
    }
    report = preflight.evaluate(aliases, require_bootstrap_secrets=False)
    assert report["ok"] is True
