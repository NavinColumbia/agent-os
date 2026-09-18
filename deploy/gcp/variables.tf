variable "project_id" {
  description = "Existing billed GCP project for the trusted Agent OS control plane."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be a valid GCP project ID."
  }
}

variable "region" {
  description = "Single beta-cell region; keep the database and runtime close."
  type        = string
  default     = "us-central1"
}

variable "sandbox_project_id" {
  description = "Existing billed GCP project dedicated to untrusted sandbox execution."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.sandbox_project_id))
    error_message = "sandbox_project_id must be a valid GCP project ID."
  }
}

variable "app_project_id" {
  description = "Existing billed GCP project dedicated to generated customer application builds and serving."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.app_project_id))
    error_message = "app_project_id must be a valid GCP project ID."
  }
}

variable "environment" {
  type    = string
  default = "production"

  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "environment must be staging or production."
  }
}

variable "activate_services" {
  description = "Create API and worker only after secret versions exist and the migration job succeeded."
  type        = bool
  default     = false
}

variable "application_image" {
  description = "Agent OS OCI image pinned by sha256 digest."
  type        = string
  default     = ""

  validation {
    condition     = !var.activate_services || can(regex("@sha256:[0-9a-f]{64}$", var.application_image))
    error_message = "application_image must be an immutable @sha256 digest when services are active."
  }
}

variable "migration_image" {
  description = "V2 migration OCI image pinned by sha256 digest; empty during infrastructure bootstrap."
  type        = string
  default     = ""

  validation {
    condition     = var.migration_image == "" || can(regex("@sha256:[0-9a-f]{64}$", var.migration_image))
    error_message = "migration_image must be empty or an immutable @sha256 digest."
  }
}

variable "sandbox_image" {
  description = "Secretless hosted-sandbox OCI image pinned by sha256 digest."
  type        = string
  default     = ""

  validation {
    condition     = !var.activate_services || can(regex("@sha256:[0-9a-f]{64}$", var.sandbox_image))
    error_message = "sandbox_image must be an immutable @sha256 digest when services are active."
  }
}

variable "app_builder_image" {
  description = "Trusted Cloud Build Docker builder image pinned by sha256 digest."
  type        = string
  default     = "gcr.io/cloud-builders/docker@sha256:3d00b6c1a9b862621c30fc74d4f2abfc62bcbdee631ed3febd31e7edbdf6252c"

  validation {
    condition     = !var.activate_services || can(regex("@sha256:[0-9a-f]{64}$", var.app_builder_image))
    error_message = "app_builder_image must be an immutable @sha256 digest when services are active."
  }
}

variable "app_max_instances" {
  description = "Per-generated-service maximum instance count for the first-customer cell."
  type        = number
  default     = 10

  validation {
    condition     = var.app_max_instances >= 1 && var.app_max_instances <= 100
    error_message = "app_max_instances must be between 1 and 100."
  }
}

variable "sandbox_timeout_seconds" {
  description = "Maximum untrusted command duration; the job gets a bounded evidence-upload grace period."
  type        = number
  default     = 300

  validation {
    condition     = var.sandbox_timeout_seconds >= 1 && var.sandbox_timeout_seconds <= 3600
    error_message = "sandbox_timeout_seconds must be between 1 and 3600."
  }
}

variable "release_id" {
  description = "Auditable Git commit/release identifier exposed to the runtime."
  type        = string
  default     = "bootstrap"

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]{1,128}$", var.release_id))
    error_message = "release_id must be a bounded identifier."
  }
}

variable "public_base_url" {
  description = "Final HTTPS origin configured in OIDC and Stripe."
  type        = string

  validation {
    condition     = can(regex("^https://[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$", var.public_base_url))
    error_message = "public_base_url must be a public HTTPS DNS origin without a port or path."
  }
}

variable "apps_base_url" {
  description = "Separate HTTPS origin used only for generated static applications."
  type        = string

  validation {
    condition     = can(regex("^https://[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$", var.apps_base_url))
    error_message = "apps_base_url must be a public HTTPS DNS origin without a port or path."
  }
}

variable "dns_managed_zone" {
  description = "Optional existing Cloud DNS managed-zone name; empty emits the A record for external DNS."
  type        = string
  default     = ""

  validation {
    condition     = var.dns_managed_zone == "" || can(regex("^[a-z][a-z0-9-]{0,61}[a-z0-9]$", var.dns_managed_zone))
    error_message = "dns_managed_zone must be empty or a valid Cloud DNS managed-zone name."
  }
}

variable "dns_ttl_seconds" {
  description = "TTL for automatically managed public A records."
  type        = number
  default     = 300

  validation {
    condition     = var.dns_ttl_seconds >= 30 && var.dns_ttl_seconds <= 86400
    error_message = "dns_ttl_seconds must be between 30 and 86400."
  }
}

variable "oidc_issuer" {
  type = string
}

variable "oidc_audience" {
  type = string
}

variable "oidc_jwks_url" {
  type = string
}

variable "oidc_authorization_url" {
  type = string
}

variable "oidc_token_url" {
  type = string
}

variable "oidc_client_id" {
  type = string
}

variable "oidc_scope" {
  type    = string
  default = "openid profile email"
}

variable "oidc_authorization_audience_parameter" {
  type    = string
  default = ""
}

variable "stripe_starter_price_id" {
  type = string

  validation {
    condition     = startswith(var.stripe_starter_price_id, "price_")
    error_message = "stripe_starter_price_id must be a Stripe price_ identifier."
  }
}

variable "stripe_growth_price_id" {
  type = string

  validation {
    condition     = startswith(var.stripe_growth_price_id, "price_")
    error_message = "stripe_growth_price_id must be a Stripe price_ identifier."
  }
}

variable "free_model_budget_cents" {
  type    = number
  default = 10000
}

variable "starter_model_budget_cents" {
  type    = number
  default = 50000
}

variable "growth_model_budget_cents" {
  type    = number
  default = 250000
}

variable "artifact_max_content_bytes" {
  description = "Per-artifact in-memory admission ceiling for this bootstrap adapter."
  type        = number
  default     = 67108864

  validation {
    condition     = var.artifact_max_content_bytes >= 1 && var.artifact_max_content_bytes <= 1073741824
    error_message = "artifact_max_content_bytes must be between 1 byte and 1 GiB."
  }
}

variable "artifact_retention_days" {
  description = "Customer artifact retention window; metadata remains as an audit tombstone after payload expiry."
  type        = number
  default     = 365

  validation {
    condition     = var.artifact_retention_days >= 30 && var.artifact_retention_days <= 3650
    error_message = "artifact_retention_days must be between 30 and 3650."
  }
}

variable "model" {
  description = "Explicit PydanticAI provider:model selected for the worker."
  type        = string
}

variable "database_runtime_role" {
  description = "Non-owner PostgreSQL LOGIN used by the application database URL."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z][A-Za-z0-9_-]{0,62}$", var.database_runtime_role))
    error_message = "database_runtime_role must be a simple PostgreSQL role name."
  }
}

variable "model_provider_secret_environment" {
  description = "Provider SDK variable receiving the isolated provider secret."
  type        = string
  default     = "OPENAI_API_KEY"

  validation {
    condition = contains([
      "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
    ], var.model_provider_secret_environment)
    error_message = "Only packaged OpenAI, Anthropic, or Google provider secret names are supported."
  }
}

variable "api_max_instances" {
  type    = number
  default = 10

  validation {
    condition     = var.api_max_instances >= 1 && var.api_max_instances <= 100
    error_message = "api_max_instances must be between 1 and 100 for the bootstrap cell."
  }
}

variable "static_router_max_instances" {
  type    = number
  default = 20

  validation {
    condition     = var.static_router_max_instances >= 1 && var.static_router_max_instances <= 1000
    error_message = "static_router_max_instances must be between 1 and 1000."
  }
}

variable "worker_instances" {
  type    = number
  default = 1

  validation {
    condition     = var.worker_instances >= 1 && var.worker_instances <= 10
    error_message = "worker_instances must be between 1 and 10 for the bootstrap cell."
  }
}

variable "rollout_worker_enabled" {
  description = "Temporarily run a second release-fenced worker during a zero-gap API rollout."
  type        = bool
  default     = false
}

variable "deletion_protection" {
  type    = bool
  default = true
}

variable "alert_notification_channels" {
  description = "Existing Cloud Monitoring notification-channel resource names used by production alerts."
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for channel in var.alert_notification_channels :
      can(regex("^projects/[^/]+/notificationChannels/[0-9]+$", channel))
    ])
    error_message = "Each alert channel must be a full projects/PROJECT/notificationChannels/ID resource name."
  }
}

variable "otlp_traces_endpoint" {
  description = "Optional OTLP/HTTP traces endpoint, normally an HTTPS collector or a localhost sidecar."
  type        = string
  default     = ""

  validation {
    condition = (
      var.otlp_traces_endpoint == "" ||
      can(regex("^https://[^[:space:]]+$", var.otlp_traces_endpoint)) ||
      can(regex("^http://(127\\.0\\.0\\.1|localhost|\\[::1\\])(:[0-9]+)?/", var.otlp_traces_endpoint))
    )
    error_message = "otlp_traces_endpoint must be empty, HTTPS, or an HTTP localhost sidecar URL."
  }
}

variable "otlp_trace_sample_ratio" {
  description = "Parent-based probability for exported API traces."
  type        = number
  default     = 0.1

  validation {
    condition     = var.otlp_trace_sample_ratio > 0 && var.otlp_trace_sample_ratio <= 1
    error_message = "otlp_trace_sample_ratio must be greater than zero and at most one."
  }
}

variable "state_bucket_name" {
  description = "Existing versioned GCS bucket used by the gcs backend and CI deploy identity."
  type        = string
  default     = ""
}

variable "github_repository_id" {
  description = "Immutable numeric GitHub repository ID; empty disables CI workload identity resources."
  type        = string
  default     = ""

  validation {
    condition     = var.github_repository_id == "" || can(regex("^[0-9]+$", var.github_repository_id))
    error_message = "github_repository_id must be empty or numeric."
  }
}
