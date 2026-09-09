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
    condition     = can(regex("^https://[A-Za-z0-9.-]+(?::[0-9]+)?$", var.public_base_url))
    error_message = "public_base_url must be an HTTPS origin without a path."
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
    condition     = contains(["OPENAI_API_KEY", "ANTHROPIC_API_KEY"], var.model_provider_secret_environment)
    error_message = "Only the currently packaged OpenAI or Anthropic provider secret names are supported."
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

variable "worker_instances" {
  type    = number
  default = 1

  validation {
    condition     = var.worker_instances >= 0 && var.worker_instances <= 10
    error_message = "worker_instances must be between 0 and 10 for the bootstrap cell."
  }
}

variable "deletion_protection" {
  type    = bool
  default = true
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
