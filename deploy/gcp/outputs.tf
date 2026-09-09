output "artifact_registry_repository" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.runtime.repository_id}"
}

output "artifact_bucket" {
  value = google_storage_bucket.artifacts.name
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
