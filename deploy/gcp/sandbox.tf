locals {
  sandbox_enabled_apis = toset([
    "compute.googleapis.com",
    "dns.googleapis.com",
    "iam.googleapis.com",
    "run.googleapis.com",
    "serviceusage.googleapis.com",
  ])
}

data "google_project" "sandbox" {
  provider   = google.sandbox
  project_id = var.sandbox_project_id
}

resource "google_project_service" "sandbox_required" {
  provider = google.sandbox
  for_each = local.sandbox_enabled_apis

  project            = var.sandbox_project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_service_account" "sandbox" {
  provider = google.sandbox

  account_id   = "${local.prefix}-sandbox"
  display_name = "Agent OS secretless ${var.environment} sandbox"

  depends_on = [google_project_service.sandbox_required]
}

resource "google_compute_network" "sandbox" {
  provider = google.sandbox

  name                    = "${local.prefix}-sandbox"
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"

  depends_on = [google_project_service.sandbox_required]
}

resource "google_compute_subnetwork" "sandbox" {
  provider = google.sandbox

  name                     = "${local.prefix}-sandbox"
  ip_cidr_range            = "10.31.0.0/24"
  region                   = var.region
  network                  = google_compute_network.sandbox.id
  private_ip_google_access = true
}

resource "google_dns_managed_zone" "sandbox_googleapis" {
  provider = google.sandbox

  name        = "${local.prefix}-restricted-googleapis"
  dns_name    = "googleapis.com."
  description = "Resolve sandbox Google API traffic only through restricted.googleapis.com"
  visibility  = "private"

  private_visibility_config {
    networks {
      network_url = google_compute_network.sandbox.id
    }
  }

  depends_on = [google_project_service.sandbox_required]
}

resource "google_dns_record_set" "sandbox_restricted_googleapis" {
  provider = google.sandbox

  managed_zone = google_dns_managed_zone.sandbox_googleapis.name
  name         = "restricted.googleapis.com."
  type         = "A"
  ttl          = 300
  rrdatas      = ["199.36.153.4", "199.36.153.5", "199.36.153.6", "199.36.153.7"]
}

resource "google_dns_record_set" "sandbox_googleapis_wildcard" {
  provider = google.sandbox

  managed_zone = google_dns_managed_zone.sandbox_googleapis.name
  name         = "*.googleapis.com."
  type         = "CNAME"
  ttl          = 300
  rrdatas      = ["restricted.googleapis.com."]
}

resource "google_compute_firewall" "sandbox_allow_restricted_googleapis" {
  provider = google.sandbox

  name               = "${local.prefix}-sandbox-allow-googleapis"
  network            = google_compute_network.sandbox.name
  direction          = "EGRESS"
  priority           = 900
  destination_ranges = ["199.36.153.4/30", "34.126.0.0/18"]
  target_tags        = ["agentos-sandbox"]

  allow {
    protocol = "tcp"
    ports    = ["443"]
  }
}

resource "google_compute_firewall" "sandbox_deny_other_egress" {
  provider = google.sandbox

  name               = "${local.prefix}-sandbox-deny-egress"
  network            = google_compute_network.sandbox.name
  direction          = "EGRESS"
  priority           = 1000
  destination_ranges = ["0.0.0.0/0"]
  target_tags        = ["agentos-sandbox"]

  deny {
    protocol = "all"
  }
}

# Image pulling happens under Google's Cloud Run service agent. The runtime
# identity below receives no project role and therefore no ambient data access.
resource "google_artifact_registry_repository_iam_member" "sandbox_image_reader" {
  location   = google_artifact_registry_repository.runtime.location
  repository = google_artifact_registry_repository.runtime.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:service-${data.google_project.sandbox.number}@serverless-robot-prod.iam.gserviceaccount.com"

  depends_on = [google_project_service.sandbox_required]
}

resource "google_cloud_run_v2_job" "sandbox" {
  provider = google.sandbox
  count    = var.activate_services ? 1 : 0

  name                = "${local.prefix}-sandbox"
  location            = var.region
  deletion_protection = var.deletion_protection
  labels              = local.labels
  launch_stage        = "GA"

  template {
    task_count  = 1
    parallelism = 1

    template {
      service_account       = google_service_account.sandbox.email
      max_retries           = 0
      timeout               = "${var.sandbox_timeout_seconds + 60}s"
      execution_environment = "EXECUTION_ENVIRONMENT_GEN2"

      containers {
        name  = "sandbox"
        image = var.sandbox_image

        resources {
          limits = {
            cpu    = "1"
            memory = "512Mi"
          }
        }

        env {
          name  = "AOS_SANDBOX_WORK_ROOT"
          value = "/sandbox-work"
        }

        volume_mounts {
          name       = "workspace"
          mount_path = "/sandbox-work"
        }
      }

      volumes {
        name = "workspace"
        empty_dir {
          medium     = "MEMORY"
          size_limit = "16Mi"
        }
      }

      vpc_access {
        egress = "ALL_TRAFFIC"
        network_interfaces {
          network    = google_compute_network.sandbox.name
          subnetwork = google_compute_subnetwork.sandbox.name
          tags       = ["agentos-sandbox"]
        }
      }
    }
  }

  depends_on = [
    google_project_service.sandbox_required,
    google_artifact_registry_repository_iam_member.sandbox_image_reader,
    google_compute_firewall.sandbox_allow_restricted_googleapis,
    google_compute_firewall.sandbox_deny_other_egress,
    google_dns_record_set.sandbox_restricted_googleapis,
    google_dns_record_set.sandbox_googleapis_wildcard,
  ]
}

resource "google_cloud_run_v2_job_iam_member" "worker_sandbox_runner" {
  provider = google.sandbox
  count    = var.activate_services ? 1 : 0

  project  = var.sandbox_project_id
  location = var.region
  name     = google_cloud_run_v2_job.sandbox[0].name
  role     = "roles/run.developer"
  member   = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_service_account_iam_member" "worker_self_signer" {
  service_account_id = google_service_account.worker.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.worker.email}"
}

locals {
  sandbox_github_project_roles = toset([
    "roles/compute.networkAdmin",
    "roles/dns.admin",
    "roles/iam.serviceAccountAdmin",
    "roles/resourcemanager.projectIamAdmin",
    "roles/run.admin",
    "roles/serviceusage.serviceUsageAdmin",
  ])
}

resource "google_project_iam_member" "sandbox_github_deploy" {
  provider = google.sandbox
  for_each = local.github_ci_enabled ? local.sandbox_github_project_roles : toset([])

  project = var.sandbox_project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.github_deploy[0].email}"
}

resource "google_service_account_iam_member" "github_sandbox_act_as" {
  provider = google.sandbox
  count    = local.github_ci_enabled ? 1 : 0

  service_account_id = google_service_account.sandbox.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.github_deploy[0].email}"
}
