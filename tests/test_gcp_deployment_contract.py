from __future__ import annotations

from pathlib import Path
import subprocess

import yaml


ROOT = Path(__file__).resolve().parents[1]


def text(path: str) -> str:
    return (ROOT / path).read_text()


def test_migration_image_is_digest_pinned_non_root_complete_and_admin_isolated():
    dockerfile = text("deploy/Dockerfile.migrations-v2")
    assert "FROM postgres:16@sha256:" in dockerfile
    assert "USER 999:999" in dockerfile
    for revision in range(86, 100):
        assert dockerfile.count(f"postgres/initdb/{revision}-") == 1

    script = text("deploy/migrate-v2.sh")
    assert "AOS_V2_MIGRATION_DATABASE_URL" in script
    assert "AOS_V2_SYSTEM_DATABASE_URL" not in script
    assert "NOT rolsuper" in script
    assert "NOT rolbypassrls" in script
    assert 'GRANT agentos_app, agentos_worker TO :"runtime_role"' in script
    subprocess.run(["sh", "-n", str(ROOT / "deploy/migrate-v2.sh")], check=True)


def test_gcp_cell_keeps_secrets_out_of_state_and_out_of_wrong_processes():
    main = text("deploy/gcp/main.tf")
    deploy = text("deploy/gcp/deploy.sh")
    versions = text("deploy/gcp/versions.tf")

    assert 'required_version = "= 1.12.6"' in versions
    assert 'version = "~> 7.22.0"' in versions
    assert "secret_data" not in main
    assert "TF_VAR_stripe_secret" not in deploy
    assert "TF_VAR_model_provider_key" not in deploy
    assert "put_secret_version stripe_secret_key" in deploy
    assert "put_secret_version model_provider_key" in deploy
    assert "put_secret_version migration_database_url" in deploy

    worker_secrets = main.split("worker_secret_environment = {", 1)[1].split("}", 1)[0]
    api_secrets = main.split("api_secret_environment = {", 1)[1].split("}", 1)[0]
    assert "stripe" not in worker_secrets
    assert "model_provider_key" in worker_secrets
    assert "stripe_secret_key" in api_secrets
    assert "model_provider_key" not in api_secrets
    assert "migration_database_url" not in worker_secrets
    assert "migration_database_url" not in api_secrets
    assert 'AOS_V2_ARTIFACT_BACKEND                      = "gcs"' in main
    assert 'role   = "roles/storage.objectCreator"' in main
    assert 'role   = "roles/storage.objectViewer"' in main
    subprocess.run(["bash", "-n", str(ROOT / "deploy/gcp/deploy.sh")], check=True)


def test_release_order_is_migrate_then_activate_and_images_require_digests():
    deploy = text("deploy/gcp/deploy.sh")
    variables = text("deploy/gcp/variables.tf")
    migration_position = deploy.index('gcloud run jobs execute "agentos-${deployment_environment}-migrate"')
    activation_position = deploy.index('-var="activate_services=true"', migration_position + 1)
    assert migration_position < activation_position
    assert "state list | rg -q '^google_cloud_run_v2_service\\.api\\[0\\]$'" in deploy
    assert "-target='google_cloud_run_v2_job.migrate[0]'" in deploy
    assert '${api_url}/ready' in deploy
    assert variables.count('@sha256:[0-9a-f]{64}$') == 2

    workflow_text = text("deploy/gcp/github-actions-deploy.yml")
    workflow = yaml.safe_load(workflow_text)
    assert isinstance(workflow, dict)
    migration_step = workflow_text.index("-target='google_cloud_run_v2_job.migrate[0]'")
    serving_step = workflow_text.index("apply API and worker revisions")
    assert migration_step < serving_step
    assert "google-github-actions/auth@7c6bc770dae815cd3e89ee6cdf493a5fab2cc093 # v3" in workflow_text
    for floating_action in ("actions/checkout@v5", "google-github-actions/auth@v3", "setup-gcloud@v3"):
        assert floating_action not in workflow_text
