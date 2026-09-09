data "google_project" "current" {}

check "production_inputs" {
  assert {
    condition = (
      startswith(var.oidc_issuer, "https://") &&
      startswith(var.oidc_jwks_url, "https://") &&
      startswith(var.oidc_authorization_url, "https://") &&
      startswith(var.oidc_token_url, "https://")
    )
    error_message = "All OIDC endpoints must use HTTPS."
  }

  assert {
    condition = (
      var.free_model_budget_cents >= 1 &&
      var.starter_model_budget_cents > var.free_model_budget_cents &&
      var.growth_model_budget_cents > var.starter_model_budget_cents
    )
    error_message = "Model budgets must be positive and increase from free through growth."
  }

  assert {
    condition     = var.stripe_starter_price_id != var.stripe_growth_price_id
    error_message = "Stripe plan price IDs must be unique."
  }

  assert {
    condition     = var.github_repository_id == "" || var.state_bucket_name != ""
    error_message = "state_bucket_name is required when GitHub deployment identity is enabled."
  }
}

locals {
  prefix = "agentos-${var.environment}"
  labels = {
    application = "agent-os"
    environment = var.environment
    managed_by  = "opentofu"
  }

  enabled_apis = toset([
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "logging.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "storage.googleapis.com",
    "sts.googleapis.com",
  ])

  secret_ids = {
    migration_database_url   = "${local.prefix}-migration-database-url"
    system_database_url      = "${local.prefix}-system-database-url"
    application_database_url = "${local.prefix}-application-database-url"
    capability_secret        = "${local.prefix}-capability-secret"
    stripe_secret_key        = "${local.prefix}-stripe-secret-key"
    stripe_webhook_secret    = "${local.prefix}-stripe-webhook-secret"
    model_provider_key       = "${local.prefix}-model-provider-key"
  }

  common_environment = {
    AOS_ENVIRONMENT                              = var.environment
    AOS_V2_APPLICATION_VERSION                   = var.release_id
    AOS_V2_CREATE_SCHEMA                         = "0"
    AOS_V2_IDENTITY_MODE                         = "oidc"
    AOS_V2_OIDC_ISSUER                           = var.oidc_issuer
    AOS_V2_OIDC_AUDIENCE                         = var.oidc_audience
    AOS_V2_OIDC_JWKS_URL                         = var.oidc_jwks_url
    AOS_V2_OIDC_AUTHORIZATION_URL                = var.oidc_authorization_url
    AOS_V2_OIDC_TOKEN_URL                        = var.oidc_token_url
    AOS_V2_OIDC_CLIENT_ID                        = var.oidc_client_id
    AOS_V2_OIDC_SCOPE                            = var.oidc_scope
    AOS_V2_OIDC_AUTHORIZATION_AUDIENCE_PARAMETER = var.oidc_authorization_audience_parameter
    AOS_V2_OIDC_ORGANIZATION_CLAIM               = "org_id"
    AOS_V2_OIDC_ROLES_CLAIM                      = "roles"
    AOS_V2_OIDC_ALGORITHMS                       = "RS256,ES256"
    AOS_V2_PUBLIC_BASE_URL                       = var.public_base_url
    AOS_V2_TENANT_MONTHLY_MODEL_BUDGET_CENTS     = tostring(var.free_model_budget_cents)
    AOS_V2_ARTIFACT_BACKEND                      = "gcs"
    AOS_V2_ARTIFACT_MAX_CONTENT_BYTES            = tostring(var.artifact_max_content_bytes)
  }

  api_environment = merge(local.common_environment, {
    AOS_V2_BILLING_MODE                      = "stripe"
    AOS_V2_STRIPE_STARTER_PRICE_ID           = var.stripe_starter_price_id
    AOS_V2_STRIPE_GROWTH_PRICE_ID            = var.stripe_growth_price_id
    AOS_V2_STRIPE_STARTER_MODEL_BUDGET_CENTS = tostring(var.starter_model_budget_cents)
    AOS_V2_STRIPE_GROWTH_MODEL_BUDGET_CENTS  = tostring(var.growth_model_budget_cents)
    AOS_V2_STRIPE_API_VERSION                = "2025-06-30.basil"
  })

  worker_environment = merge(local.common_environment, {
    AOS_V2_MODEL                         = var.model
    AOS_V2_TENANT_DISCOVERY_LIMIT        = "128"
    AOS_V2_MAX_TURN_COST_CENTS           = "100"
    AOS_V2_MODEL_REQUEST_TIMEOUT_SECONDS = "120"
    AOS_V2_MANAGEMENT_CHECK_SECONDS      = "30"
    AOS_V2_SLOW_WORK_SECONDS             = "300"
    AOS_V2_MANAGEMENT_ESCALATION_CHECKS  = "3"
    AOS_V2_SANDBOX_BACKEND               = "disabled"
  })

  api_secret_environment = {
    AOS_V2_SYSTEM_DATABASE_URL      = "system_database_url"
    AOS_V2_APPLICATION_DATABASE_URL = "application_database_url"
    AOS_V2_CAPABILITY_SECRET        = "capability_secret"
    AOS_V2_STRIPE_SECRET_KEY        = "stripe_secret_key"
    AOS_V2_STRIPE_WEBHOOK_SECRET    = "stripe_webhook_secret"
  }

  worker_secret_environment = {
    AOS_V2_SYSTEM_DATABASE_URL              = "system_database_url"
    AOS_V2_APPLICATION_DATABASE_URL         = "application_database_url"
    AOS_V2_CAPABILITY_SECRET                = "capability_secret"
    (var.model_provider_secret_environment) = "model_provider_key"
  }
}

resource "google_project_service" "required" {
  for_each = local.enabled_apis

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "runtime" {
  location      = var.region
  repository_id = "${local.prefix}-runtime"
  description   = "Digest-pinned Agent OS runtime and migration images"
  format        = "DOCKER"
  labels        = local.labels

  depends_on = [google_project_service.required]
}

resource "google_storage_bucket" "artifacts" {
  name                        = "${var.project_id}-${local.prefix}-artifacts"
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
      age            = 30
      matches_prefix = ["temporary/"]
    }
    action {
      type = "Delete"
    }
  }
}

resource "google_storage_bucket_iam_member" "artifact_readers" {
  for_each = {
    api    = google_service_account.api.email
    worker = google_service_account.worker.email
  }

  bucket = google_storage_bucket.artifacts.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${each.value}"
}

resource "google_storage_bucket_iam_member" "artifact_writer" {
  bucket = google_storage_bucket.artifacts.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_service_account" "api" {
  account_id   = "${local.prefix}-api"
  display_name = "Agent OS ${var.environment} API"
}

resource "google_service_account" "worker" {
  account_id   = "${local.prefix}-worker"
  display_name = "Agent OS ${var.environment} worker"
}

resource "google_service_account" "migrate" {
  account_id   = "${local.prefix}-migrate"
  display_name = "Agent OS ${var.environment} migration job"
}

resource "google_service_account" "builder" {
  account_id   = "${local.prefix}-builder"
  display_name = "Agent OS ${var.environment} Cloud Build"
}

resource "google_secret_manager_secret" "runtime" {
  for_each = local.secret_ids

  secret_id = each.value
  labels    = local.labels

  replication {
    auto {}
  }

  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_iam_member" "api" {
  for_each = toset([
    "system_database_url", "application_database_url", "capability_secret",
    "stripe_secret_key", "stripe_webhook_secret",
  ])

  secret_id = google_secret_manager_secret.runtime[each.value].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.api.email}"
}

resource "google_secret_manager_secret_iam_member" "worker" {
  for_each = toset([
    "system_database_url", "application_database_url", "capability_secret", "model_provider_key",
  ])

  secret_id = google_secret_manager_secret.runtime[each.value].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_secret_manager_secret_iam_member" "migrate" {
  secret_id = google_secret_manager_secret.runtime["migration_database_url"].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.migrate.email}"
}

resource "google_artifact_registry_repository_iam_member" "runtime_readers" {
  for_each = {
    api     = google_service_account.api.email
    worker  = google_service_account.worker.email
    migrate = google_service_account.migrate.email
  }

  location   = google_artifact_registry_repository.runtime.location
  repository = google_artifact_registry_repository.runtime.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${each.value}"
}

resource "google_artifact_registry_repository_iam_member" "builder_writer" {
  location   = google_artifact_registry_repository.runtime.location
  repository = google_artifact_registry_repository.runtime.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.builder.email}"
}

resource "google_project_iam_member" "builder_logging" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.builder.email}"
}

resource "google_cloud_run_v2_job" "migrate" {
  count = var.migration_image == "" ? 0 : 1

  name                = "${local.prefix}-migrate"
  location            = var.region
  deletion_protection = var.deletion_protection
  labels              = local.labels

  template {
    task_count = 1
    template {
      service_account = google_service_account.migrate.email
      max_retries     = 1
      timeout         = "1800s"

      containers {
        image = var.migration_image

        resources {
          limits = {
            cpu    = "1"
            memory = "512Mi"
          }
        }

        env {
          name = "AOS_V2_MIGRATION_DATABASE_URL"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.runtime["migration_database_url"].secret_id
              version = "latest"
            }
          }
        }

        env {
          name  = "AOS_V2_DATABASE_RUNTIME_ROLE"
          value = var.database_runtime_role
        }
      }
    }
  }

  depends_on = [
    google_project_service.required,
    google_secret_manager_secret_iam_member.migrate,
    google_artifact_registry_repository_iam_member.runtime_readers,
  ]
}

resource "google_cloud_run_v2_service" "api" {
  count = var.activate_services ? 1 : 0

  name                = "${local.prefix}-api"
  location            = var.region
  deletion_protection = var.deletion_protection
  ingress             = "INGRESS_TRAFFIC_ALL"
  labels              = local.labels

  template {
    service_account                  = google_service_account.api.email
    timeout                          = "300s"
    max_instance_request_concurrency = 40

    scaling {
      min_instance_count = 0
      max_instance_count = var.api_max_instances
    }

    containers {
      image = var.application_image

      ports {
        name           = "http1"
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      startup_probe {
        initial_delay_seconds = 1
        timeout_seconds       = 3
        period_seconds        = 3
        failure_threshold     = 20
        http_get {
          path = "/health"
          port = 8080
        }
      }

      liveness_probe {
        initial_delay_seconds = 10
        timeout_seconds       = 3
        period_seconds        = 30
        failure_threshold     = 3
        http_get {
          path = "/health"
          port = 8080
        }
      }

      dynamic "env" {
        for_each = local.api_environment
        content {
          name  = env.key
          value = env.value
        }
      }

      env {
        name  = "AOS_V2_ARTIFACT_BUCKET"
        value = google_storage_bucket.artifacts.name
      }

      dynamic "env" {
        for_each = local.api_secret_environment
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.runtime[env.value].secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  depends_on = [
    google_project_service.required,
    google_secret_manager_secret_iam_member.api,
    google_artifact_registry_repository_iam_member.runtime_readers,
    google_storage_bucket_iam_member.artifact_readers,
  ]
}

resource "google_cloud_run_v2_service_iam_member" "public_api" {
  count = var.activate_services ? 1 : 0

  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.api[0].name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_run_v2_worker_pool" "worker" {
  count = var.activate_services ? 1 : 0

  name                = "${local.prefix}-worker"
  location            = var.region
  deletion_protection = var.deletion_protection
  labels              = local.labels

  scaling {
    scaling_mode          = "MANUAL"
    manual_instance_count = var.worker_instances
  }

  template {
    service_account = google_service_account.worker.email

    containers {
      name    = "worker"
      image   = var.application_image
      command = ["agentos-v2"]
      args    = ["worker"]

      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
      }

      dynamic "env" {
        for_each = local.worker_environment
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = local.worker_secret_environment
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.runtime[env.value].secret_id
              version = "latest"
            }
          }
        }
      }

      env {
        name  = "AOS_V2_ARTIFACT_BUCKET"
        value = google_storage_bucket.artifacts.name
      }
    }
  }

  depends_on = [
    google_project_service.required,
    google_secret_manager_secret_iam_member.worker,
    google_artifact_registry_repository_iam_member.runtime_readers,
    google_storage_bucket_iam_member.artifact_readers,
    google_storage_bucket_iam_member.artifact_writer,
  ]
}
