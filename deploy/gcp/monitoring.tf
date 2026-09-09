locals {
  public_host = lower(trimprefix(var.public_base_url, "https://"))
  apps_host   = lower(trimprefix(var.apps_base_url, "https://"))
}

resource "google_monitoring_uptime_check_config" "static_apps" {
  count = var.activate_services ? 1 : 0

  display_name       = "${local.prefix} published apps"
  timeout            = "10s"
  period             = "60s"
  selected_regions   = ["USA", "EUROPE", "ASIA_PACIFIC"]
  checker_type       = "STATIC_IP_CHECKERS"
  log_check_failures = true
  user_labels        = local.labels

  http_check {
    path           = "/health"
    port           = 443
    request_method = "GET"
    use_ssl        = true
    validate_ssl   = true

    accepted_response_status_codes {
      status_class = "STATUS_CLASS_2XX"
    }
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = local.apps_host
    }
  }

  depends_on = [
    google_project_service.required,
    google_cloud_run_v2_service_iam_member.public_static_router,
  ]
}

resource "google_monitoring_alert_policy" "static_apps" {
  count = var.activate_services ? 1 : 0

  display_name          = "${local.prefix} published apps failure"
  combiner              = "OR"
  enabled               = true
  notification_channels = var.alert_notification_channels
  user_labels           = merge(local.labels, { severity = "critical" })

  documentation {
    mime_type = "text/markdown"
    content   = "The separate generated-app origin is failing from multiple regions. Check its Cloud Run revision, private bucket access, and DNS/TLS."
  }

  conditions {
    display_name = "Published-app /health failed from multiple regions"

    condition_threshold {
      comparison      = "COMPARISON_GT"
      duration        = "120s"
      threshold_value = 1
      filter          = "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND metric.label.check_id=\"${google_monitoring_uptime_check_config.static_apps[0].uptime_check_id}\" AND resource.type=\"uptime_url\""

      aggregations {
        alignment_period     = "120s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.*"]
      }

      trigger {
        count = 1
      }
    }
  }

  alert_strategy {
    auto_close = "1800s"
  }

  depends_on = [google_project_service.required]
}

resource "google_monitoring_uptime_check_config" "public_ready" {
  count = var.activate_services ? 1 : 0

  display_name       = "${local.prefix} public readiness"
  timeout            = "10s"
  period             = "60s"
  selected_regions   = ["USA", "EUROPE", "ASIA_PACIFIC"]
  checker_type       = "STATIC_IP_CHECKERS"
  log_check_failures = true
  user_labels        = local.labels

  http_check {
    path           = "/ready"
    port           = 443
    request_method = "GET"
    use_ssl        = true
    validate_ssl   = true

    accepted_response_status_codes {
      status_class = "STATUS_CLASS_2XX"
    }
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = local.public_host
    }
  }

  depends_on = [
    google_project_service.required,
    google_cloud_run_v2_service_iam_member.public_api,
  ]
}

resource "google_monitoring_alert_policy" "public_ready" {
  count = var.activate_services ? 1 : 0

  display_name          = "${local.prefix} public readiness failure"
  combiner              = "OR"
  enabled               = true
  notification_channels = var.alert_notification_channels
  user_labels           = merge(local.labels, { severity = "critical" })

  documentation {
    mime_type = "text/markdown"
    content   = "Agent OS public readiness has failed from at least two probe regions. Check the Cloud Run API revision, dependencies, and public DNS/TLS before rollback."
  }

  conditions {
    display_name = "Public /ready failed from multiple regions"

    condition_threshold {
      comparison      = "COMPARISON_GT"
      duration        = "120s"
      threshold_value = 1
      filter          = "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND metric.label.check_id=\"${google_monitoring_uptime_check_config.public_ready[0].uptime_check_id}\" AND resource.type=\"uptime_url\""

      aggregations {
        alignment_period     = "120s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.*"]
      }

      trigger {
        count = 1
      }
    }
  }

  alert_strategy {
    auto_close = "1800s"
  }

  depends_on = [google_project_service.required]
}
