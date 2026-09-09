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
  value = var.activate_services ? var.public_base_url : null
}

output "static_apps_url" {
  value = var.activate_services ? var.apps_base_url : null
}

output "cloud_run_api_uri" {
  value = try(google_cloud_run_v2_service.api[0].uri, null)
}

output "cloud_run_static_router_uri" {
  value = try(google_cloud_run_v2_service.static_router[0].uri, null)
}

output "public_edge_ipv4" {
  value = google_compute_global_address.public_edge.address
}

output "public_edge_certificate" {
  value = try(google_compute_managed_ssl_certificate.public_edge[0].name, null)
}

output "required_external_dns_records" {
  value = var.dns_managed_zone == "" ? {
    (local.public_host) = google_compute_global_address.public_edge.address
    (local.apps_host)   = google_compute_global_address.public_edge.address
  } : {}
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

output "app_builder_image" {
  value = var.app_builder_image
}

output "generated_app_source_bucket" {
  value = google_storage_bucket.app_sources.name
}

output "generated_app_repository" {
  value = "${var.region}-docker.pkg.dev/${var.app_project_id}/${google_artifact_registry_repository.generated_apps.repository_id}"
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
