from __future__ import annotations

import json
import tomllib
from pathlib import Path

from click.testing import CliRunner

from agent_os.entrypoints.cli import main


ROOT = Path(__file__).resolve().parents[1]


def test_production_lock_excludes_unhashable_local_project_and_covers_direct_dependencies():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    lock = tomllib.loads((ROOT / "pylock.toml").read_text())
    locked = {package["name"] for package in lock["packages"]}
    assert "agent-os" not in locked
    for requirement in pyproject["project"]["dependencies"]:
        normalized = requirement.split("[", 1)[0].split("=", 1)[0].lower().replace("_", "-")
        assert normalized in locked
    assert {
        "aiohttp", "aiohappyeyeballs", "aiosignal", "frozenlist",
        "http-ece", "multidict", "propcache", "py-vapid", "yarl",
    } <= locked


def test_runtime_image_installs_locked_dependencies_before_local_package():
    dockerfile = (ROOT / "deploy" / "Dockerfile.v2").read_text()
    assert "pip install --no-cache-dir -r pylock.toml" in dockerfile
    assert "pip install --no-cache-dir --no-deps ." in dockerfile
    assert "python:3.12-slim-trixie@sha256:" in dockerfile
    assert "COPY src ./src" in dockerfile
    assert "COPY scripts" not in dockerfile
    assert "COPY platform" not in dockerfile


def test_migration_image_orders_numeric_revisions_and_compatibility_suffixes():
    script = (ROOT / "deploy" / "migrate-v2.sh").read_text()
    assert "suffix_length" in script
    assert "-k1,1n -k2,2n" in script
    assert "sort -V" not in script
    assert "for migration in /migrations/*.sql" not in script
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "for f in postgres/initdb/*.sql" not in ci
    assert "suffix_length" in ci
    assert "-k1,1n -k2,2n" in ci
    assert "actions/checkout@v5" not in ci
    assert "actions/setup-python@v6" not in ci
    assert "actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09" in ci
    assert "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1" in ci
    assert "opentofu/setup-opentofu@a1320f892987e89d278cc92dc5adc984fb93aca4" in ci
    assert "permissions:\n  contents: read" in ci
    assert "Dockerfile.migrations-v2" in ci
    assert "Dockerfile.sandbox-v2" in ci
    assert "tofu -chdir=deploy/gcp validate" in ci


def test_packaged_cli_exposes_api_and_worker_processes():
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "serve" in result.output
    assert "worker" in result.output
    assert "activate-release" in result.output
    assert "connector-secret-name" in result.output
    assert "deployment-control" in result.output


def test_connector_secret_locator_cli_is_deterministic_and_secret_free():
    result = CliRunner().invoke(main, [
        "connector-secret-name", "--organization", "tenant-a",
        "--credential-ref", "jira-token", "--backend", "gcp",
    ])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["backend"] == "gcp"
    assert payload["locator"].startswith("agentos-connector-")
    assert "tenant-a" not in payload["locator"]
    assert "jira-token" not in payload["locator"]


def test_deployment_control_cli_routes_one_bounded_break_glass_action(monkeypatch):
    calls = []

    class Operator:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        def set_static_route(self, target, **kwargs):
            calls.append((target, kwargs))
            return {"kind": "static_site", "target": target, "status": "suspended"}

        def close(self):
            calls.append(("close", {}))

    monkeypatch.setattr("agent_os.entrypoints.cli.GCPDeploymentOperator", Operator)
    monkeypatch.setenv("AOS_V2_PUBLISHED_APP_BUCKET", "bucket")
    monkeypatch.setenv("AOS_V2_APP_PROJECT_ID", "app-project")
    monkeypatch.setenv("AOS_V2_APP_REGION", "us-central1")
    result = CliRunner().invoke(main, [
        "deployment-control", "--kind", "static", "--action", "suspend",
        "--target", "R" * 43, "--reason", "INC-9", "--actor", "on-call",
    ])

    assert result.exit_code == 0
    assert json.loads(result.output)["status"] == "suspended"
    assert calls == [
        ("init", {
            "published_bucket": "bucket", "app_project_id": "app-project",
            "region": "us-central1",
        }),
        ("R" * 43, {"suspended": True, "reason": "INC-9", "actor": "on-call"}),
        ("close", {}),
    ]


def test_evaluation_compose_runs_api_and_worker_from_the_same_image():
    compose = (ROOT / "deploy" / "docker-compose.v2.yml").read_text()
    assert "dockerfile: deploy/Dockerfile.v2" in compose
    assert "command: [\"agentos-v2\", \"worker\"]" in compose
    assert "service_completed_successfully" in compose
    assert "88-workflow-action-leases-v2.sql" in compose
    assert "89-notifications-v2.sql" in compose
    assert "90-artifacts-v2.sql" in compose
    assert "91-ready-tenant-discovery-v2.sql" in compose
    assert "92-preview-deployments-v2.sql" in compose
    assert "93-preview-capability-lifecycle-v2.sql" in compose
    assert "94-management-watch-v2.sql" in compose
    assert "95-company-directory-v2.sql" in compose
    assert "96-company-proposal-decisions-v2.sql" in compose
    assert "99-external-artifacts-v2.sql" in compose
    assert "99z-external-onboarding-v2.sql" in compose
    assert "99zz-connectors-v2.sql" in compose
    assert "99zzz-memberships-v2.sql" in compose
    assert "99zzzz-tenant-models-v2.sql" in compose
    assert "99zzzzz-notification-delivery-v2.sql" in compose
    assert "100-mission-assurance-kernel-v2.sql" in compose
    assert "101-mission-revision-history-v2.sql" in compose
    assert "102-personal-attention-state-v2.sql" in compose
    assert "103-structured-decision-responses-v2.sql" in compose
    assert "104-decision-response-redrive-v2.sql" in compose
    assert "105-experience-events-v2.sql" in compose
    assert "106-web-push-subscriptions-v2.sql" in compose
    assert "99zzzzzz-mission-participants-v2.sql" in compose
    assert "99zzzzzzz-mission-conversations-v2.sql" in compose
    assert "107-mission-work-accountability-v2.sql" in compose
    assert "108-execution-health-v2.sql" in compose
    assert "109-human-requests-v2.sql" in compose
    migration_image = (ROOT / "deploy" / "Dockerfile.migrations-v2").read_text()
    assert "99zzzzz-notification-delivery-v2.sql" in migration_image
    assert "100-mission-assurance-kernel-v2.sql" in migration_image
    assert "101-mission-revision-history-v2.sql" in migration_image
    assert "102-personal-attention-state-v2.sql" in migration_image
    assert "103-structured-decision-responses-v2.sql" in migration_image
    assert "104-decision-response-redrive-v2.sql" in migration_image
    assert "105-experience-events-v2.sql" in migration_image
    assert "106-web-push-subscriptions-v2.sql" in migration_image
    assert "99zzzzzz-mission-participants-v2.sql" in migration_image
    assert "99zzzzzzz-mission-conversations-v2.sql" in migration_image
    assert "107-mission-work-accountability-v2.sql" in migration_image
    assert "108-execution-health-v2.sql" in migration_image
    assert "109-human-requests-v2.sql" in migration_image
    required_v2_migrations = [
        path.name for path in (ROOT / "postgres" / "initdb").glob("*.sql")
        if int(path.name.split("-", 1)[0].rstrip("z")) >= 86
    ]
    assert required_v2_migrations
    for migration_name in required_v2_migrations:
        assert migration_name in compose, f"compose omits {migration_name}"
        assert migration_name in migration_image, f"migration image omits {migration_name}"
    assert '127.0.0.1:${AOS_V2_PUBLIC_PORT:-8080}:8080' in compose
    assert "GEMINI_API_KEY" in compose
    assert "AOS_V2_DATABASE_RUNTIME_PASSWORD" in compose
    assert "postgresql://agentos_runtime:" in compose
    assert "NOINHERIT" in compose
    assert 'CREATE EXTENSION IF NOT EXISTS "uuid-ossp"' in compose
    assert "CREATE SCHEMA IF NOT EXISTS dbos AUTHORIZATION agentos_runtime" in compose
    leases_migration = (ROOT / "postgres" / "initdb" / "88-workflow-action-leases-v2.sql").read_text()
    assert "CREATE INDEX IF NOT EXISTS aos_v2_workflow_actions_recoverable_idx" in leases_migration
    env_example = (ROOT / "deploy" / "v2.env.example").read_text()
    assert "AOS_V2_SANDBOX_BACKEND=disabled" in env_example
    assert "AOS_V2_PUBLIC_BASE_URL=http://localhost:8080" in env_example
    assert "AOS_V2_PREVIEW_TTL_SECONDS=604800" in env_example
    assert "AOS_V2_WEB_PUSH_PUBLIC_KEY" in env_example
    assert "AOS_V2_CONNECTOR_SECRET_HOST_DIR=./connector-secrets" in env_example
    assert "@sha256:" in env_example


def test_laptop_pilot_keeps_api_loopback_only_and_sandbox_worker_hardened():
    pilot = (ROOT / "deploy" / "docker-compose.pilot.yml").read_text()
    worker = (ROOT / "deploy" / "Dockerfile.worker-v2").read_text()
    operator = (ROOT / "deploy" / "local-pilot.sh").read_text()

    assert "/var/run/docker.sock:/var/run/docker.sock" in pilot
    assert "AOS_V2_SANDBOX_WORKSPACE_ROOT" in pilot
    assert "read_only: true" in pilot
    assert "no-new-privileges:true" in pilot
    assert "cap_drop:" in pilot
    assert "docker-cli=" in worker
    assert "@sha256:" in worker
    assert 'tailscale funnel --bg --yes --https="$FUNNEL_PORT"' in operator
    assert "issue-local-token" in operator
    assert "AOS_V2_BILLING_MODE=disabled" in operator
    assert "mktemp" in operator
    assert "Funnel was disabled" in operator
