output "artifact_registry_repository" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.runtime.repository_id}"
}

output "artifact_bucket" {
  value = google_storage_bucket.artifacts.name
}

output "published_app_bucket" {
  value = google_storage_bucket.published_apps.name
}

output "runtime_secret_ids" {
  value = { for key, secret in google_secret_manager_secret.runtime : key => secret.secret_id }
}

output "migration_job" {
  value = try(google_cloud_run_v2_job.migrate[0].name, null)
}

output "api_url" {
  value = try(google_cloud_run_v2_service.api[0].uri, null)
}

output "static_apps_url" {
  value = try(google_cloud_run_v2_service.static_router[0].uri, null)
}

output "application_image" {
  value = var.application_image
}

output "migration_image" {
  value = var.migration_image
}

output "sandbox_image" {
  value = var.sandbox_image
}

output "sandbox_job" {
  value = try(google_cloud_run_v2_job.sandbox[0].name, null)
}

output "readiness_uptime_check" {
  value = try(google_monitoring_uptime_check_config.public_ready[0].name, null)
}

output "readiness_alert_policy" {
  value = try(google_monitoring_alert_policy.public_ready[0].name, null)
}

output "worker_pool" {
  value = try(google_cloud_run_v2_worker_pool.worker[0].name, null)
}

output "build_service_account" {
  value = google_service_account.builder.name
}

output "github_workload_identity_provider" {
  value = try(google_iam_workload_identity_pool_provider.github[0].name, null)
}

output "github_deploy_service_account" {
  value = try(google_service_account.github_deploy[0].email, null)
}
