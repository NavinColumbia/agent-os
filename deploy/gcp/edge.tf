resource "google_compute_global_address" "public_edge" {
  name         = "${local.prefix}-edge"
  address_type = "EXTERNAL"
  ip_version   = "IPV4"

  depends_on = [google_project_service.required]
}

resource "google_compute_region_network_endpoint_group" "api" {
  count = var.activate_services ? 1 : 0

  name                  = "${local.prefix}-api-neg"
  network_endpoint_type = "SERVERLESS"
  region                = var.region

  cloud_run {
    service = google_cloud_run_v2_service.api[0].name
  }
}

resource "google_compute_region_network_endpoint_group" "static_router" {
  count = var.activate_services ? 1 : 0

  name                  = "${local.prefix}-apps-neg"
  network_endpoint_type = "SERVERLESS"
  region                = var.region

  cloud_run {
    service = google_cloud_run_v2_service.static_router[0].name
  }
}

resource "google_compute_backend_service" "api" {
  count = var.activate_services ? 1 : 0

  name                  = "${local.prefix}-api"
  protocol              = "HTTP"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  timeout_sec           = 30

  backend {
    group = google_compute_region_network_endpoint_group.api[0].id
  }

  log_config {
    enable      = true
    sample_rate = 1
  }
}

resource "google_compute_backend_service" "static_router" {
  count = var.activate_services ? 1 : 0

  name                  = "${local.prefix}-apps"
  protocol              = "HTTP"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  timeout_sec           = 30

  backend {
    group = google_compute_region_network_endpoint_group.static_router[0].id
  }

  log_config {
    enable      = true
    sample_rate = 1
  }
}

resource "google_compute_url_map" "https" {
  count = var.activate_services ? 1 : 0

  name = "${local.prefix}-https"

  default_url_redirect {
    host_redirect          = local.public_host
    https_redirect         = true
    redirect_response_code = "PERMANENT_REDIRECT_DEFAULT"
    strip_query            = false
  }

  host_rule {
    hosts        = [local.public_host]
    path_matcher = "api"
  }

  host_rule {
    hosts        = [local.apps_host]
    path_matcher = "apps"
  }

  path_matcher {
    name            = "api"
    default_service = google_compute_backend_service.api[0].id
  }

  path_matcher {
    name            = "apps"
    default_service = google_compute_backend_service.static_router[0].id
  }
}

resource "google_compute_url_map" "http_redirect" {
  count = var.activate_services ? 1 : 0

  name = "${local.prefix}-http-redirect"

  default_url_redirect {
    https_redirect         = true
    redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
    strip_query            = false
  }
}

resource "google_compute_managed_ssl_certificate" "public_edge" {
  count = var.activate_services ? 1 : 0

  name = "${local.prefix}-edge-${substr(sha256("${local.public_host}:${local.apps_host}"), 0, 8)}"

  managed {
    domains = [local.public_host, local.apps_host]
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "google_compute_ssl_policy" "public_edge" {
  count = var.activate_services ? 1 : 0

  name            = "${local.prefix}-edge"
  profile         = "MODERN"
  min_tls_version = "TLS_1_2"
}

resource "google_compute_target_https_proxy" "public_edge" {
  count = var.activate_services ? 1 : 0

  name             = "${local.prefix}-https"
  url_map          = google_compute_url_map.https[0].id
  ssl_certificates = [google_compute_managed_ssl_certificate.public_edge[0].id]
  ssl_policy       = google_compute_ssl_policy.public_edge[0].id
}

resource "google_compute_target_http_proxy" "public_edge_redirect" {
  count = var.activate_services ? 1 : 0

  name    = "${local.prefix}-http"
  url_map = google_compute_url_map.http_redirect[0].id
}

resource "google_compute_global_forwarding_rule" "https" {
  count = var.activate_services ? 1 : 0

  name                  = "${local.prefix}-https"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  ip_address            = google_compute_global_address.public_edge.id
  port_range            = "443"
  target                = google_compute_target_https_proxy.public_edge[0].id
}

resource "google_compute_global_forwarding_rule" "http" {
  count = var.activate_services ? 1 : 0

  name                  = "${local.prefix}-http"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  ip_address            = google_compute_global_address.public_edge.id
  port_range            = "80"
  target                = google_compute_target_http_proxy.public_edge_redirect[0].id
}

data "google_dns_managed_zone" "public" {
  count = var.dns_managed_zone == "" ? 0 : 1

  name = var.dns_managed_zone
}

resource "google_dns_record_set" "api" {
  count = var.dns_managed_zone != "" ? 1 : 0

  managed_zone = data.google_dns_managed_zone.public[0].name
  name         = "${local.public_host}."
  type         = "A"
  ttl          = var.dns_ttl_seconds
  rrdatas      = [google_compute_global_address.public_edge.address]
}

resource "google_dns_record_set" "apps" {
  count = var.dns_managed_zone != "" ? 1 : 0

  managed_zone = data.google_dns_managed_zone.public[0].name
  name         = "${local.apps_host}."
  type         = "A"
  ttl          = var.dns_ttl_seconds
  rrdatas      = [google_compute_global_address.public_edge.address]
}
