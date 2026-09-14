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
    assert 'ALTER ROLE :"runtime_role" NOINHERIT' in script
    assert 'GRANT agentos_app, agentos_worker TO :"runtime_role"' in script
    assert 'CREATE EXTENSION IF NOT EXISTS "uuid-ossp"' in script
    assert "CREATE SCHEMA IF NOT EXISTS dbos AUTHORIZATION" in script
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
    assert "put_secret_version tenant_derivation_secret AOS_V2_TENANT_DERIVATION_SECRET 1 0" in deploy

    worker_secrets = main.split("worker_secret_environment = {", 1)[1].split("}", 1)[0]
    api_secrets = main.split("api_secret_environment = {", 1)[1].split("}", 1)[0]
    assert "stripe" not in worker_secrets
    assert "model_provider_key" in worker_secrets
    assert "stripe_secret_key" in api_secrets
    assert "model_provider_key" not in api_secrets
    assert "migration_database_url" not in worker_secrets
    assert "migration_database_url" not in api_secrets
    assert "tenant_derivation_secret" in api_secrets
    assert "tenant_derivation_secret" not in worker_secrets
    assert 'AOS_V2_CONNECTOR_SECRET_BACKEND' in main and '= "gcp"' in main
    assert "AOS_V2_CONNECTOR_SECRET_PROJECT_ID" in main and "= var.project_id" in main
    assert 'AOS_V2_ARTIFACT_BACKEND' in main and '= "gcs"' in main
    assert 'role   = "roles/storage.objectCreator"' in main
    assert 'role   = "roles/storage.objectViewer"' in main
    writers = main.split('resource "google_storage_bucket_iam_member" "artifact_writers"', 1)[1]
    assert "api    = google_service_account.api.email" in writers
    assert "worker = google_service_account.worker.email" in writers
    assert 'matches_prefix = ["tenants/"]' in main
    assert "age            = var.artifact_retention_days" in main
    assert "AOS_V2_ARTIFACT_RETENTION_DAYS" in main
    assert "days_since_noncurrent_time = 7" in main
    assert 'TF_VAR_artifact_retention_days="${AOS_V2_ARTIFACT_RETENTION_DAYS:-365}"' in deploy
    variables = text("deploy/gcp/variables.tf")
    assert "GEMINI_API_KEY" in variables and "GOOGLE_API_KEY" in variables
    assert 'startswith(var.model, "google:")' in main
    subprocess.run(["bash", "-n", str(ROOT / "deploy/gcp/deploy.sh")], check=True)


def test_break_glass_identity_can_only_fence_generated_apps_and_has_no_secret_or_delete_access():
    main = text("deploy/gcp/main.tf")
    apps = text("deploy/gcp/apps.tf")
    outputs = text("deploy/gcp/outputs.tf")
    docs = text("deploy/gcp/README.md")

    assert 'resource "google_service_account" "incident_operator"' in main
    route_binding = main.split(
        'resource "google_storage_bucket_iam_member" "incident_operator_route_writer"', 1,
    )[1].split("\n}\n", 1)[0]
    assert "published_app_route_writer" in route_binding
    assert "objects/routes/" in route_binding
    assert "secretmanager" not in route_binding.lower()

    role = apps.split(
        'resource "google_project_iam_custom_role" "incident_service_controller"', 1,
    )[1].split("\n}\n", 1)[0]
    assert '"run.services.get"' in role
    assert '"run.services.update"' in role
    for forbidden in ("run.services.create", "run.services.delete", "secretmanager", "storage.objects"):
        assert forbidden not in role
    binding = apps.split(
        'resource "google_project_iam_member" "incident_operator_service_controller"', 1,
    )[1].split("\n}\n", 1)[0]
    assert "incident_service_controller" in binding
    assert "incident_operator.email" in binding
    assert 'output "incident_operator_service_account"' in outputs
    assert "roles/iam.serviceAccountTokenCreator" in docs
    assert "deployment-control" in docs


def test_release_order_is_migrate_then_activate_and_images_require_digests():
    deploy = text("deploy/gcp/deploy.sh")
    variables = text("deploy/gcp/variables.tf")
    migration_position = deploy.index('gcloud run jobs execute "agentos-${deployment_environment}-migrate"')
    activation_position = deploy.index('-var="activate_services=true"', migration_position + 1)
    assert migration_position < activation_position
    assert "state list | rg -q '^google_cloud_run_v2_service\\.api\\[0\\]$'" in deploy
    assert "-target='google_cloud_run_v2_job.migrate[0]'" in deploy
    assert '--api-url "$api_url"' in deploy
    assert '--apps-url "$apps_url"' in deploy
    assert variables.count('@sha256:[0-9a-f]{64}$') == 4
    assert 'sandbox_tag="${runtime_repository}/sandbox:${release_id}"' in deploy
    assert '-var="sandbox_image=${sandbox_image}"' in deploy

    workflow_text = text(".github/workflows/deploy-gcp.yml")
    workflow = yaml.safe_load(workflow_text)
    assert isinstance(workflow, dict)
    assert "workflow_dispatch:" in workflow_text
    assert "\n  push:" not in workflow_text
    assert "environment: production" in workflow_text
    migration_step = workflow_text.index("-target='google_cloud_run_v2_job.migrate[0]'")
    serving_step = workflow_text.index("apply API and worker revisions")
    assert migration_step < serving_step
    assert "google-github-actions/auth@7c6bc770dae815cd3e89ee6cdf493a5fab2cc093 # v3" in workflow_text
    for floating_action in ("actions/checkout@v5", "google-github-actions/auth@v3", "setup-gcloud@v3"):
        assert floating_action not in workflow_text


def test_public_readiness_monitoring_has_multi_region_alert_and_channel_wiring():
    main = text("deploy/gcp/main.tf")
    monitoring = text("deploy/gcp/monitoring.tf")
    variables = text("deploy/gcp/variables.tf")
    cicd = text("deploy/gcp/cicd.tf")

    assert '"monitoring.googleapis.com"' in main
    assert 'resource "google_monitoring_uptime_check_config" "public_ready"' in monitoring
    assert 'path           = "/ready"' in monitoring
    assert 'selected_regions   = ["USA", "EUROPE", "ASIA_PACIFIC"]' in monitoring
    assert 'validate_ssl   = true' in monitoring
    assert 'log_check_failures = true' in monitoring
    assert 'resource "google_monitoring_alert_policy" "public_ready"' in monitoring
    assert 'cross_series_reducer = "REDUCE_COUNT_FALSE"' in monitoring
    assert 'duration        = "120s"' in monitoring
    assert "notification_channels = var.alert_notification_channels" in monitoring
    assert 'variable "alert_notification_channels"' in variables
    assert "AOS_ALERT_NOTIFICATION_CHANNELS || '[]'" in text(".github/workflows/deploy-gcp.yml")
    assert '"roles/monitoring.uptimeCheckConfigEditor"' in cicd
    assert '"roles/monitoring.alertPolicyEditor"' in cicd


def test_rollback_is_digest_pinned_serving_only_and_health_checked():
    rollback = text("deploy/gcp/rollback.sh")

    assert 'current_migration_image=$(tofu -chdir="$tofu_root" output -raw migration_image)' in rollback
    assert rollback.count('@sha256:[0-9a-f]{64}$') == 4
    assert "-target='google_cloud_run_v2_service.api[0]'" in rollback
    assert "-target='google_cloud_run_v2_worker_pool.worker[0]'" in rollback
    assert "google_cloud_run_v2_job.migrate" not in rollback
    assert "gcloud run jobs execute" not in rollback
    assert '--api-url "$api_url"' in rollback
    assert '--apps-url "$apps_url"' in rollback
    subprocess.run(["bash", "-n", str(ROOT / "deploy/gcp/rollback.sh")], check=True)


def test_hosted_sandbox_is_cross_project_secretless_and_network_denied():
    main = text("deploy/gcp/main.tf")
    sandbox = text("deploy/gcp/sandbox.tf")
    variables = text("deploy/gcp/variables.tf")
    build = text("deploy/gcp/cloudbuild.yaml")

    assert 'var.sandbox_project_id != var.project_id' in main
    assert 'AOS_V2_SANDBOX_BACKEND                 = "cloud-run-job"' in main
    worker = main.split('resource "google_cloud_run_v2_worker_pool" "worker"', 1)[1]
    assert "google_cloud_run_v2_job_iam_member.worker_sandbox_runner" in worker
    assert "google_service_account_iam_member.worker_self_signer" in worker
    assert 'variable "sandbox_project_id"' in variables
    assert 'provider = google.sandbox' in sandbox
    assert 'service_account       = google_service_account.sandbox.email' in sandbox
    assert 'role               = "roles/iam.serviceAccountTokenCreator"' in sandbox
    assert 'role     = "roles/run.developer"' in sandbox
    assert 'direction          = "EGRESS"' in sandbox
    assert 'destination_ranges = ["0.0.0.0/0"]' in sandbox
    assert 'protocol = "all"' in sandbox
    assert 'destination_ranges = ["199.36.153.4/30", "34.126.0.0/18"]' in sandbox
    assert 'rrdatas      = ["restricted.googleapis.com."]' in sandbox
    assert 'size_limit = "16Mi"' in sandbox
    assert "secret_key_ref" not in sandbox
    assert "AOS_V2_MODEL" not in sandbox
    assert "Dockerfile.sandbox-v2" in build
    assert "${_REPOSITORY}/sandbox:${_RELEASE_ID}" in build


def test_release_build_uses_exact_commit_source_and_only_digest_pinned_steps():
    build = text("deploy/gcp/cloudbuild.yaml")
    release = text("deploy/gcp/build-release.sh")

    assert build.count("name: ${_DOCKER_BUILDER_IMAGE}") == 6
    assert "name: gcr.io/cloud-builders/docker\n" not in build
    assert "_DOCKER_BUILDER_IMAGE: required" in build
    assert "git archive --format=tar.gz" in release
    assert 'git show "${release_id}:deploy/gcp/cloudbuild.yaml"' in release
    assert 'gcloud builds submit "$source_archive"' in release
    assert "gcloud builds submit ." not in release
    assert "@sha256:[0-9a-f]{64}" in release
    subprocess.run(["bash", "-n", str(ROOT / "deploy/gcp/build-release.sh")], check=True)


def test_generated_apps_use_a_separate_secretless_origin_and_prefix_scoped_storage_roles():
    main = text("deploy/gcp/main.tf")
    variables = text("deploy/gcp/variables.tf")
    monitoring = text("deploy/gcp/monitoring.tf")
    deploy = text("deploy/gcp/deploy.sh")

    assert 'variable "apps_base_url"' in variables
    assert 'lower(local.apps_host) != lower(local.public_host)' in main
    assert 'resource "google_storage_bucket" "published_apps"' in main
    assert 'public_access_prevention    = "enforced"' in main
    assert 'resource "google_cloud_run_v2_service" "static_router"' in main
    router = main.split('resource "google_cloud_run_v2_service" "static_router"', 1)[1]
    router = router.split('resource "google_cloud_run_v2_service_iam_member"', 1)[0]
    assert 'args    = ["static-router"]' in router
    assert "secret_key_ref" not in router
    assert "AOS_V2_SYSTEM_DATABASE_URL" not in router
    assert "google_service_account.static_router.email" in router
    assert 'permissions = ["storage.objects.get"]' in main
    assert "objects/releases/" in main
    assert "objects/routes/" in main
    assert '"storage.objects.delete"' not in main
    worker = main.split('resource "google_cloud_run_v2_worker_pool" "worker"', 1)[1]
    assert "google_storage_bucket_iam_member.published_app_release_writer" in worker
    assert "google_storage_bucket_iam_member.published_app_route_writer" in worker
    assert 'resource "google_monitoring_uptime_check_config" "static_apps"' in monitoring
    assert 'path           = "/health"' in monitoring
    assert "AOS_V2_APPS_BASE_URL" in deploy
    assert '--apps-url "$apps_url"' in deploy


def test_rollback_keeps_public_app_router_on_the_same_known_good_revision():
    rollback = text("deploy/gcp/rollback.sh")

    assert "-target='google_cloud_run_v2_service.static_router[0]'" in rollback
    assert '--apps-url "$apps_url"' in rollback


def test_generated_backend_apps_have_a_third_least_privilege_scale_to_zero_plane():
    apps = text("deploy/gcp/apps.tf")
    main = text("deploy/gcp/main.tf")
    versions = text("deploy/gcp/versions.tf")
    variables = text("deploy/gcp/variables.tf")
    deploy = text("deploy/gcp/deploy.sh")
    workflow = text(".github/workflows/deploy-gcp.yml")

    assert 'variable "app_project_id"' in variables
    assert 'alias   = "apps"' in versions
    assert 'var.app_project_id != var.project_id' in main
    assert 'var.app_project_id != var.sandbox_project_id' in main
    assert 'provider = google.apps' in apps
    assert 'resource "google_storage_bucket" "app_sources"' in apps
    assert 'matches_prefix = ["temporary/app-build-sources/"]' in apps
    assert 'resource "google_artifact_registry_repository" "generated_apps"' in apps
    assert 'resource "google_service_account" "app_builder"' in apps
    assert 'resource "google_service_account" "app_runtime"' in apps
    assert 'role    = "roles/logging.logWriter"' in apps
    assert 'role       = "roles/artifactregistry.writer"' in apps
    assert 'permissions = [' in apps
    assert '"cloudbuild.builds.create"' in apps
    assert '"cloudbuild.builds.get"' in apps
    assert '"cloudbuild.builds.list"' in apps
    assert '"serviceusage.services.use"' in apps
    assert '"run.services.create"' in apps
    assert '"run.services.get"' in apps
    assert '"run.services.update"' in apps
    assert '"run.services.delete"' not in apps
    assert 'role               = "roles/iam.serviceAccountUser"' in apps
    assert 'AOS_V2_APP_PROJECT_ID' in main
    assert 'AOS_V2_APP_BUILDER_IMAGE' in main
    assert 'GCP_APP_PROJECT_ID' in deploy
    assert 'AOS_V2_APP_BUILDER_IMAGE' in deploy
    assert 'TF_VAR_app_project_id: ${{ vars.GCP_APP_PROJECT_ID }}' in workflow
    assert "TF_VAR_app_builder_image:" in workflow
    assert "gcr.io/cloud-builders/docker@sha256:" in workflow


def test_public_domains_terminate_at_a_managed_tls_edge_without_run_app_bypass():
    edge = text("deploy/gcp/edge.tf")
    main = text("deploy/gcp/main.tf")
    outputs = text("deploy/gcp/outputs.tf")
    deploy = text("deploy/gcp/deploy.sh")

    assert 'resource "google_compute_global_address" "public_edge"' in edge
    assert edge.count('network_endpoint_type = "SERVERLESS"') == 2
    assert edge.count('load_balancing_scheme = "EXTERNAL_MANAGED"') == 4
    assert 'resource "google_compute_managed_ssl_certificate" "public_edge"' in edge
    assert 'min_tls_version = "TLS_1_2"' in edge
    assert 'https_redirect         = true' in edge
    assert 'resource "google_dns_record_set" "api"' in edge
    assert 'resource "google_dns_record_set" "apps"' in edge
    assert main.count('ingress              = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"') == 2
    assert main.count("default_uri_disabled = true") == 2
    assert 'output "required_external_dns_records"' in outputs
    assert "edge_check.py" in deploy
    assert "--dns-only --timeout-seconds 0" in deploy


def test_owner_only_launch_file_wrapper_runs_redacting_preflight_before_deploy():
    wrapper = text("deploy/gcp/deploy-from-env.sh")
    example = text("deploy/gcp/launch.env.example")

    assert 'file_mode=$(stat -c \'%a\' "$environment_file")' in wrapper
    assert 'file_owner=$(stat -c \'%u\' "$environment_file")' in wrapper
    assert '[[ ! -f "$environment_file" || -L "$environment_file" ]]' in wrapper
    assert "--require-bootstrap-secrets" in wrapper
    assert wrapper.index('"${preflight[@]}"') < wrapper.index('source "$environment_file"')
    assert "exec deploy/gcp/deploy.sh" in wrapper
    assert "AOS_V2_STRIPE_WEBHOOK_SECRET=whsec_CHANGE_ME" in example
    assert "AOS_V2_MODEL_PROVIDER_KEY=sk-proj-CHANGE_ME" in example
    subprocess.run(["bash", "-n", str(ROOT / "deploy/gcp/deploy-from-env.sh")], check=True)
