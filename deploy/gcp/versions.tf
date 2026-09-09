terraform {
  required_version = "= 1.12.6"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.22.0"
    }
  }

  backend "gcs" {}
}

provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google" {
  alias   = "sandbox"
  project = var.sandbox_project_id
  region  = var.region
}

provider "google" {
  alias   = "apps"
  project = var.app_project_id
  region  = var.region
}
