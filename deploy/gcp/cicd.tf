locals {
  github_ci_enabled = var.github_repository_id != ""
  deploy_project_roles = toset([
    "roles/artifactregistry.reader",
    "roles/cloudbuild.builds.editor",
    "roles/run.admin",
    "roles/secretmanager.viewer",
    "roles/serviceusage.serviceUsageConsumer",
    "roles/viewer",
  ])
}

resource "google_service_account" "github_deploy" {
  count = local.github_ci_enabled ? 1 : 0

  account_id   = "${local.prefix}-github"
  display_name = "Agent OS ${var.environment} GitHub deployment"
}

resource "google_iam_workload_identity_pool" "github" {
  count = local.github_ci_enabled ? 1 : 0

  workload_identity_pool_id = "${local.prefix}-github"
  display_name              = "Agent OS GitHub"
  description               = "Repository-ID-bound GitHub Actions identities"

  depends_on = [google_project_service.required]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  count = local.github_ci_enabled ? 1 : 0

  workload_identity_pool_id          = google_iam_workload_identity_pool.github[0].workload_identity_pool_id
  workload_identity_pool_provider_id = "agent-os-repository"
  display_name                       = "Agent OS repository"
  attribute_condition                = "assertion.repository_id == '${var.github_repository_id}'"
  attribute_mapping = {
    "google.subject"          = "assertion.sub"
    "attribute.repository_id" = "assertion.repository_id"
  }

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account_iam_member" "github_federation" {
  count = local.github_ci_enabled ? 1 : 0

  service_account_id = google_service_account.github_deploy[0].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github[0].name}/attribute.repository_id/${var.github_repository_id}"
}

resource "google_project_iam_member" "github_deploy" {
  for_each = local.github_ci_enabled ? local.deploy_project_roles : toset([])

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.github_deploy[0].email}"
}

resource "google_service_account_iam_member" "github_runtime_act_as" {
  for_each = local.github_ci_enabled ? {
    api     = google_service_account.api.name
    worker  = google_service_account.worker.name
    migrate = google_service_account.migrate.name
    builder = google_service_account.builder.name
  } : {}

  service_account_id = each.value
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.github_deploy[0].email}"
}

data "google_storage_bucket" "state" {
  count = local.github_ci_enabled ? 1 : 0
  name  = var.state_bucket_name
}

resource "google_storage_bucket_iam_member" "github_state" {
  count = local.github_ci_enabled ? 1 : 0

  bucket = data.google_storage_bucket.state[0].name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.github_deploy[0].email}"
}
