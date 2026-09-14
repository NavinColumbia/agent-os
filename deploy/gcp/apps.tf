locals {
  app_enabled_apis = toset([
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "iam.googleapis.com",
    "logging.googleapis.com",
    "run.googleapis.com",
    "serviceusage.googleapis.com",
    "storage.googleapis.com",
  ])
}

data "google_project" "apps" {
  provider   = google.apps
  project_id = var.app_project_id
}

resource "google_project_service" "apps_required" {
  provider = google.apps
  for_each = local.app_enabled_apis

  project            = var.app_project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_storage_bucket" "app_sources" {
  provider = google.apps

  name                        = "${var.app_project_id}-${local.prefix}-sources"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  labels                      = local.labels

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      age            = 7
      matches_prefix = ["temporary/app-build-sources/"]
    }
    action {
      type = "Delete"
    }
  }

  lifecycle_rule {
    condition {
      days_since_noncurrent_time = 1
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.apps_required]
}

resource "google_artifact_registry_repository" "generated_apps" {
  provider = google.apps

  location      = var.region
  repository_id = "${local.prefix}-apps"
  description   = "Digest-pinned generated customer application images"
  format        = "DOCKER"
  labels        = local.labels

  depends_on = [google_project_service.apps_required]
}

# The build identity can read only staged customer source, write images, and
# emit build logs. It cannot deploy services or read Agent OS control data.
resource "google_service_account" "app_builder" {
  provider = google.apps

  account_id   = "${local.prefix}-build"
  display_name = "Agent OS ${var.environment} generated-app builder"

  depends_on = [google_project_service.apps_required]
}

# Generated code receives an identity with no project-level roles. Product
# credentials are added later through a separate per-app capability workflow.
resource "google_service_account" "app_runtime" {
  provider = google.apps

  account_id   = "${local.prefix}-runtime"
  display_name = "Agent OS ${var.environment} generated-app runtime"

  depends_on = [google_project_service.apps_required]
}

resource "google_project_iam_custom_role" "app_source_access" {
  provider = google.apps

  role_id     = replace("${local.prefix}_app_source", "-", "_")
  title       = "Agent OS ${var.environment} generated-app source access"
  description = "Create and collision-check bounded generated-app build sources"
  permissions = [
    "storage.objects.create",
    "storage.objects.get",
  ]
}

resource "google_storage_bucket_iam_member" "worker_app_source" {
  provider = google.apps

  bucket = google_storage_bucket.app_sources.name
  role   = google_project_iam_custom_role.app_source_access.name
  member = "serviceAccount:${google_service_account.worker.email}"

  condition {
    title      = "temporary_app_build_sources_only"
    expression = "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.app_sources.name}/objects/temporary/app-build-sources/')"
  }
}

resource "google_storage_bucket_iam_member" "builder_app_source_reader" {
  provider = google.apps

  bucket = google_storage_bucket.app_sources.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.app_builder.email}"

  condition {
    title      = "temporary_app_build_sources_only"
    expression = "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.app_sources.name}/objects/temporary/app-build-sources/')"
  }
}

resource "google_artifact_registry_repository_iam_member" "app_builder_writer" {
  provider = google.apps

  location   = google_artifact_registry_repository.generated_apps.location
  repository = google_artifact_registry_repository.generated_apps.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.app_builder.email}"
}

resource "google_project_iam_member" "app_builder_logging" {
  provider = google.apps

  project = var.app_project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.app_builder.email}"
}

resource "google_artifact_registry_repository_iam_member" "app_service_image_reader" {
  provider = google.apps

  location   = google_artifact_registry_repository.generated_apps.location
  repository = google_artifact_registry_repository.generated_apps.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:service-${data.google_project.apps.number}@serverless-robot-prod.iam.gserviceaccount.com"
}

resource "google_project_iam_custom_role" "app_build_controller" {
  provider = google.apps

  role_id     = replace("${local.prefix}_app_build_controller", "-", "_")
  title       = "Agent OS ${var.environment} generated-app build controller"
  description = "Start and reconcile only Agent OS-controlled Cloud Builds"
  permissions = [
    "cloudbuild.builds.create",
    "cloudbuild.builds.get",
    "cloudbuild.builds.list",
    "serviceusage.services.use",
  ]
}

resource "google_project_iam_member" "worker_app_build_controller" {
  provider = google.apps

  project = var.app_project_id
  role    = google_project_iam_custom_role.app_build_controller.name
  member  = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_project_iam_custom_role" "app_service_controller" {
  provider = google.apps

  role_id     = replace("${local.prefix}_app_service_controller", "-", "_")
  title       = "Agent OS ${var.environment} generated-app service controller"
  description = "Create, read, and update generated Cloud Run services without delete authority"
  permissions = [
    "run.services.create",
    "run.services.get",
    "run.services.update",
  ]
}

resource "google_project_iam_member" "worker_app_service_controller" {
  provider = google.apps

  project = var.app_project_id
  role    = google_project_iam_custom_role.app_service_controller.name
  member  = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_service_account_iam_member" "worker_app_builder_act_as" {
  provider = google.apps

  service_account_id = google_service_account.app_builder.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_service_account_iam_member" "worker_app_runtime_act_as" {
  provider = google.apps

  service_account_id = google_service_account.app_runtime.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.worker.email}"
}

locals {
  app_github_project_roles = toset([
    "roles/artifactregistry.admin",
    "roles/iam.roleAdmin",
    "roles/iam.serviceAccountAdmin",
    "roles/resourcemanager.projectIamAdmin",
    "roles/serviceusage.serviceUsageAdmin",
    "roles/storage.admin",
  ])
}

resource "google_project_iam_member" "github_apps_deploy" {
  provider = google.apps
  for_each = local.github_ci_enabled ? local.app_github_project_roles : toset([])

  project = var.app_project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.github_deploy[0].email}"
}
