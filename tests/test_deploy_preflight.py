import base64
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("deploy_preflight", ROOT / "deploy" / "preflight.py")
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


def test_public_preflight_reports_credentials_without_exposing_values(monkeypatch, tmp_path):
    cp = tmp_path / "control-plane"
    (cp / "roles").mkdir(parents=True)
    (cp / "hooks").mkdir()
    (cp / "hooks" / "manifest_policy.py").write_text("# policy\n")
    offsite = tmp_path / "offsite"; offsite.mkdir()
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/usr/bin/caddy" if name == "caddy" else None)
    monkeypatch.setattr(preflight, "_host_caddy_service_available", lambda: True)
    monkeypatch.setattr(preflight, "_mounted_offsite", lambda path: path == offsite)
    monkeypatch.delenv("AOS_PUBLIC_HOST", raising=False)
    key = base64.urlsafe_b64encode(b"x" * 32).decode()
    values = {
        "DATABASE_URL": "postgresql://user:private-password@db/agentos",
        "AUDIT_HMAC_KEY": "a" * 64,
        "VAULT_KEY": key,
        "AOS_API_TOKEN": "p" * 64,
        "AOS_PUBLIC_HOST": "agents.acme.test",
        "AOS_PUBLIC_URL": "https://agents.acme.test",
        "AOS_SMTP_URL": "smtps://user:private-password@mail.acme.test:465",
        "AOS_SMTP_FROM": "no-reply@acme.test",
        "NTFY_TOPIC": "aos-" + "n" * 48,
        "AOSNAP_PASS": "private-snapshot-passphrase",
        "AOSNAP_OFFSITE_DIR": str(offsite),
        "STRIPE_SECRET_KEY": "sk_live_private",
        "STRIPE_WEBHOOK_SECRET": "whsec_private",
        "AOS_STRIPE_PRICE_PRO": "price_pro",
        "AOS_STRIPE_PRICE_ENTERPRISE": "price_enterprise",
        "AOS_STRIPE_SUCCESS_URL": "https://agents.acme.test/#billing",
        "AOS_STRIPE_CANCEL_URL": "https://agents.acme.test/#billing",
    }

    checks = preflight.validate_config(values, control_plane=cp)

    assert all(item["ok"] for item in checks), checks
    rendered = str(checks)
    assert "private-password" not in rendered
    assert "sk_live_private" not in rendered
    assert "private-snapshot-passphrase" not in rendered


def test_public_preflight_accepts_docker_caddy_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(
        preflight.shutil, "which",
        lambda name: "/usr/bin/docker" if name == "docker" else None)

    checks = preflight.validate_config({}, control_plane=tmp_path / "missing")

    edge = next(item for item in checks if item["check"] == "Caddy runtime")
    assert edge == {"check": "Caddy runtime", "ok": True,
                    "detail": "Docker Caddy fallback available"}


def test_public_installer_selects_validated_host_or_container_edge():
    source = (ROOT / "deploy" / "install-public.sh").read_text()
    compose = (ROOT / "deploy" / "docker-compose.public.yml").read_text()

    assert "EDGE_MODE=host" in source and "EDGE_MODE=container" in source
    assert 'caddy validate --config /etc/caddy/Caddyfile' in source
    assert 'env "AOS_PUBLIC_HOST=$PUBLIC_HOST" caddy validate' in source
    assert 'systemctl is-active --quiet caddy.service' in source
    assert 'ps --status running --services' in source
    assert 'systemctl restart agentos-supervisor.service' in source
    assert 'docker compose --env-file "$ROOT/.env.local"' in source
    assert "network_mode: host" in compose
    assert "caddy:2.10.2-alpine" in compose
    assert "no-new-privileges:true" in compose
    assert "env_file:" not in compose
    assert "AOS_PUBLIC_HOST: ${AOS_PUBLIC_HOST:?" in compose


def test_public_preflight_fails_closed_on_placeholders(tmp_path, monkeypatch):
    # CI exports valid bootstrap credentials.  This case specifically exercises
    # an all-placeholder configuration, so isolate it from the parent process.
    for key in ("DATABASE_URL", "AUDIT_HMAC_KEY", "VAULT_KEY", "AOS_API_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    checks = preflight.validate_config({
        "DATABASE_URL": "postgresql://agentos:CHANGE-ME@127.0.0.1/agentos",
        "VAULT_KEY": "CHANGE-ME",
        "AOS_PUBLIC_HOST": "agent.example.com",
    }, control_plane=tmp_path / "missing")

    failed = {item["check"] for item in checks if not item["ok"]}
    assert {"DATABASE_URL", "AUDIT_HMAC_KEY", "VAULT_KEY", "VAULT_KEY format",
            "public hostname", "transactional email", "operator notification topic", "snapshot passphrase",
            "off-host snapshot destination", "STRIPE_SECRET_KEY", "control plane"} <= failed


def test_public_preflight_rejects_test_mode_stripe_credentials(tmp_path):
    checks = preflight.validate_config({
        "STRIPE_SECRET_KEY": "sk_test_private",
        "STRIPE_WEBHOOK_SECRET": "whsec_private",
    }, control_plane=tmp_path / "missing")

    assert next(item for item in checks if item["check"] == "Stripe live mode")["ok"] is False


def test_rebuild_replaces_secret_placeholders_instead_of_treating_them_as_real_values():
    source = (ROOT / "platform" / "rebuild.sh").read_text()
    assert '[[ "$current" == *CHANGE-ME* ]]' in source
    for key in ("AOS_API_TOKEN", "AUDIT_HMAC_KEY", "VAULT_KEY", "AOSNAP_PASS"):
        assert f"ensure_secret {key}" in source
    assert '[[ "$database_url" == *CHANGE-ME* ]]' in source
    assert "DATABASE_URL bound to generated local Postgres credential" in source


def test_migration_uses_the_installed_root_instead_of_a_hard_coded_home_checkout():
    source = (ROOT / "platform" / "migrate.sh").read_text()
    assert 'ROOT="${AOS_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"' in source
    assert '$HOME/projects/agent-os' not in source
    assert 'rls_readiness.py" quiescence' in source
    assert 'rls_readiness.py" report' in source


def test_public_install_proves_rls_after_ordered_migrations():
    rebuild = (ROOT / "platform" / "rebuild.sh").read_text()
    preflight_source = (ROOT / "deploy" / "preflight.py").read_text()
    assert 'rls_readiness.py" rollout-gate' in rebuild
    assert '"database tenant isolation"' in preflight_source


def test_env_example_documents_every_public_launch_input():
    values = preflight._read_env(ROOT / "deploy" / "public.env.example")
    required = {
        "DATABASE_URL", "AUDIT_HMAC_KEY", "VAULT_KEY", "AOS_API_TOKEN",
        "AOS_PUBLIC_HOST", "AOS_PUBLIC_URL", "AOS_SMTP_URL", "AOS_SMTP_FROM",
        "NTFY_TOPIC", "AOSNAP_PASS", "AOSNAP_OFFSITE_DIR",
        "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET",
        "AOS_STRIPE_PRICE_PRO", "AOS_STRIPE_PRICE_ENTERPRISE",
        "AOS_STRIPE_SUCCESS_URL", "AOS_STRIPE_CANCEL_URL",
    }
    assert required <= set(values)
