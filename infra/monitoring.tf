# ---------------------------------------------------------------------------
# Monitoring: budgets, uptime check, SLO definitions (§8.9, §10.9).
#
#   $20 project budget (50/90/100% alerts) + $15 Vertex-scoped + $10
#   Storage-scoped.  Cloud Billing budgets ALERT, they do not cap — the
#   app-level Gemini circuit breaker is the hard stop (§8.9).
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------
# $20/mo project-wide budget with 50% / 90% / 100% email alerts (§8.9).
resource "google_billing_budget" "project" {
  billing_account = var.billing_account_id
  display_name    = "vuraRAD project $20/mo"

  budget_filter {
    projects               = ["projects/${var.project_id}"]
    credit_types_treatment = "EXCLUDE_ALL_CREDITS"
  }

  amount {
    specified_amount {
      currency_code = "USD"
      units         = "20"
    }
  }

  threshold_rules {
    threshold_percent = 50.0
  }
  threshold_rules {
    threshold_percent = 90.0
  }
  threshold_rules {
    threshold_percent = 100.0
  }

  all_updates_rule {
    monitoring_notification_channels = [google_monitoring_notification_channel.email.id]
  }
}

# $15/mo Vertex AI (Gemini) service-scoped budget (§8.9).
resource "google_billing_budget" "vertex" {
  billing_account = var.billing_account_id
  display_name    = "vuraRAD Vertex AI $15/mo"

  budget_filter {
    projects               = ["projects/${var.project_id}"]
    credit_types_treatment = "EXCLUDE_ALL_CREDITS"
    services               = var.vertex_ai_service_id != "" ? [var.vertex_ai_service_id] : []
  }

  amount {
    specified_amount {
      currency_code = "USD"
      units         = "15"
    }
  }

  threshold_rules {
    threshold_percent = 90.0
  }
  threshold_rules {
    threshold_percent = 100.0
  }

  all_updates_rule {
    monitoring_notification_channels = [google_monitoring_notification_channel.email.id]
  }
}

# $10/mo Cloud Storage (egress) service-scoped budget (§8.9, §9.2).
resource "google_billing_budget" "storage" {
  billing_account = var.billing_account_id
  display_name    = "vuraRAD Cloud Storage $10/mo"

  budget_filter {
    projects               = ["projects/${var.project_id}"]
    credit_types_treatment = "EXCLUDE_ALL_CREDITS"
    services               = var.storage_service_id != "" ? [var.storage_service_id] : []
  }

  amount {
    specified_amount {
      currency_code = "USD"
      units         = "10"
    }
  }

  threshold_rules {
    threshold_percent = 90.0
  }
  threshold_rules {
    threshold_percent = 100.0
  }

  all_updates_rule {
    monitoring_notification_channels = [google_monitoring_notification_channel.email.id]
  }
}

# ---------------------------------------------------------------------------
# Uptime check on /healthz (§10.6).  The host is the Cloud Run stable URL.
# ---------------------------------------------------------------------------
locals {
  # Extract the hostname from the Cloud Run service URI (https://<host>).
  api_host = regex("://([^/]+)", google_cloud_run_v2_service.api.uri)[0]
}

resource "google_monitoring_uptime_check_config" "healthz" {
  display_name = "vuraRAD /healthz uptime"
  project      = var.project_id
  timeout      = "10s"
  period       = "60s"

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = local.api_host
    }
  }

  http_check {
    path    = "/healthz"
    port    = 443
    use_ssl = true
    headers = {}
  }

  depends_on = [google_cloud_run_v2_service.api]
}

# ---------------------------------------------------------------------------
# SLO definitions (§10.9).  The API service is a custom Monitoring service;
# the SLOs use Cloud Run request metrics.  Per-route latency SLOs (/studies
# p95<300ms, /access-urls p95<600ms) and the first-image availability SLO
# require custom metrics emitted by those routes and are documented here as
# the next step — the two below use the standard Cloud Run metrics.
# ---------------------------------------------------------------------------
resource "google_monitoring_service" "api" {
  service_id   = "vurarad-api"
  display_name = "vuraRAD API"
  project      = var.project_id

  user_labels = {
    environment = var.environment
  }
}

# SLO 1 — API availability: 99.5% non-5xx over a rolling 30 days (§10.9).
resource "google_monitoring_slo" "api_availability" {
  service         = google_monitoring_service.api.service_id
  slo_id          = "api-availability"
  display_name    = "API availability (non-5xx) 99.5% / 30d"
  goal            = 0.995
  calendar_period = "MONTH"

  request_based_sli {
    good_total_ratio {
      good_service_filter  = "metric.type=\"run.googleapis.com/request_count\" resource.type=\"cloud_run_revision\" metric.label.response_code_class!=\"500\""
      total_service_filter = "metric.type=\"run.googleapis.com/request_count\" resource.type=\"cloud_run_revision\""
    }
  }
}

# SLO 2 — API request latency: p95 < 300ms over a rolling 7 days (§10.9).
# run.googleapis.com/request_latencies is a distribution in milliseconds.
resource "google_monitoring_slo" "api_latency" {
  service             = google_monitoring_service.api.service_id
  slo_id              = "api-latency-p95"
  display_name        = "API request latency p95 < 300ms / 7d"
  goal                = 0.95
  rolling_period_days = 7 # 7 days

  request_based_sli {
    distribution_cut {
      distribution_filter = "metric.type=\"run.googleapis.com/request_latencies\" resource.type=\"cloud_run_revision\""
      range {
        max = 300 # ms
      }
    }
  }
}
