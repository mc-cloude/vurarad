# ---------------------------------------------------------------------------
# Logging & alerting (§5.1, §5.7, §8.9).
#
# 1. The vurarad-audit log sink → bucket-locked GCS bucket, written by a
#    Google-managed writer identity (NOT vurarad-run@).  This is the identity
#    boundary that makes the audit trail immutable (§5.1).
# 2. Data Access audit config: DATA_WRITE only for firestore + storage.
#    DATA_READ is deliberately NOT enabled (§5.7, acceptance criterion 7).
# 3. 11 alert policies (§8.9): 6 simple occurrence alerts + min-instances
#    drift + MFA failure rate + AUDIT_CHAIN_BROKEN (page) + nightly-export
#    26h no-success + 3 consecutive backup failures (page).
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Audit sink → vurarad-audit bucket (§5.1, acceptance criterion 4).
#   filter: logName="projects/<p>/logs/vurarad-audit"
#   The app emits audit events to a logger named "vurarad-audit"; the sink
#   captures them under a unique (Google-managed) writer identity.
# ---------------------------------------------------------------------------
resource "google_logging_project_sink" "audit" {
  name                   = "vurarad-audit-sink"
  project                = var.project_id
  destination            = "storage.googleapis.com/${google_storage_bucket.audit.name}"
  filter                 = "logName=\"projects/${var.project_id}/logs/vurarad-audit\""
  unique_writer_identity = true
  description            = "Routes the dedicated vurarad-audit log stream to the bucket-locked system of record (§5.1)."

  depends_on = [google_project_service.enabled]
}

# Grant the sink's Google-managed writer identity objectCreator (write only,
# NO delete, NO update) on the audit bucket.
resource "google_storage_bucket_iam_member" "audit_sink_writer" {
  bucket = google_storage_bucket.audit.name
  role   = "roles/storage.objectCreator"
  member = google_logging_project_sink.audit.writer_identity
}

# ---------------------------------------------------------------------------
# Data Access audit config (§5.7, acceptance criterion 7).
#   firestore + storage: DATA_WRITE only.  DATA_READ is NOT enabled — a
#   blanket DATA_READ on the DICOM bucket would emit one entry per instance
#   fetch.  The application-level audit log already records PHI reads at the
#   correct granularity (§5.1).  ADMIN_READ stays on (free, 400-day).
# ---------------------------------------------------------------------------
resource "google_project_iam_audit_config" "firestore_data_write" {
  project = var.project_id
  service = "firestore.googleapis.com"

  audit_log_config {
    log_type = "DATA_WRITE"
  }

  depends_on = [google_project_service.enabled]
}

resource "google_project_iam_audit_config" "storage_data_write" {
  project = var.project_id
  service = "storage.googleapis.com"

  audit_log_config {
    log_type = "DATA_WRITE"
  }

  depends_on = [google_project_service.enabled]
}

# ---------------------------------------------------------------------------
# Notification channels (§10.9).  Email for money/failed-job alerts; Pub/Sub
# for paging — used only by AUDIT_CHAIN_BROKEN and 3 consecutive backup
# failures (a 5-person clinic has no on-call rota; an alert nobody actions is
# worse than no alert).
# ---------------------------------------------------------------------------
resource "google_monitoring_notification_channel" "email" {
  display_name = "vuraRAD email alerts"
  type         = "email"
  labels = {
    email_address = var.notification_email
  }
}

resource "google_pubsub_topic" "paging" {
  name    = "vurarad-paging"
  project = var.project_id
}

resource "google_monitoring_notification_channel" "paging" {
  display_name = "vuraRAD paging (AUDIT_CHAIN_BROKEN, backup failures)"
  type         = "pubsub"
  labels = {
    topic = google_pubsub_topic.paging.id
  }
}

# ---------------------------------------------------------------------------
# 6 occurrence alerts — any matching log entry emails (§8.9).
# ---------------------------------------------------------------------------
locals {
  occurrence_alerts = {
    logging_policy_violation = {
      metric  = "logging_policy_violation"
      filter  = "logName=\"projects/${var.project_id}/logs/vurarad-audit\" (jsonPayload.event_type=\"LOGGING_POLICY_VIOLATION\" OR jsonPayload.error.code=\"LOGGING_POLICY_VIOLATION\")"
      display = "LOGGING_POLICY_VIOLATION"
      doc     = "A log policy violation was recorded (§8.9). Review the source of the violation."
    }
    ai_budget_warning = {
      metric  = "ai_budget_warning"
      filter  = "logName=\"projects/${var.project_id}/logs/vurarad-audit\" (jsonPayload.event_type=\"AI_BUDGET_WARNING\" OR jsonPayload.error.code=\"AI_BUDGET_WARNING\")"
      display = "AI_BUDGET_WARNING"
      doc     = "Gemini spend reached 80% of the AI budget (§8.9). The app circuit breaker is the hard stop; this is the early signal."
    }
    auth_lockout = {
      metric  = "auth_lockout"
      filter  = "logName=\"projects/${var.project_id}/logs/vurarad-audit\" jsonPayload.event_type=\"AUTH_LOCKOUT\""
      display = "AUTH_LOCKOUT"
      doc     = "An account lockout was recorded (§8.9). Could be benign or a brute-force attempt."
    }
    audit_exported = {
      metric  = "audit_exported"
      filter  = "logName=\"projects/${var.project_id}/logs/vurarad-audit\" jsonPayload.event_type=\"AUDIT_EXPORTED\""
      display = "AUDIT_EXPORTED"
      doc     = "An audit export was produced. An export is a disclosure of ePHI (§164.528); a human should know it happened (§8.9)."
    }
    patient_erased = {
      metric  = "patient_erased"
      filter  = "logName=\"projects/${var.project_id}/logs/vurarad-audit\" jsonPayload.event_type=\"PATIENT_ERASED\""
      display = "PATIENT_ERASED"
      doc     = "A patient erasure (GDPR Art. 17 / §3.12) was executed. Irreversible; verify the request was legitimate (§8.9)."
    }
    sink_delivery_errors = {
      metric  = "audit_sink_delivery_error"
      filter  = "logName=\"projects/${var.project_id}/logs/cloudaudit.googleapis.com%2Factivity\" protoPayload.serviceName=\"logging.googleapis.com\" severity=ERROR"
      display = "AUDIT_SINK_DELIVERY_ERROR"
      doc     = "The Cloud Logging service reported an error (§8.9). Investigate whether the vurarad-audit sink is still delivering to the bucket."
    }
  }
}

resource "google_logging_metric" "occurrence" {
  for_each = local.occurrence_alerts
  name     = each.value.metric
  filter   = each.value.filter
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_alert_policy" "occurrence" {
  for_each              = local.occurrence_alerts
  display_name          = each.value.display
  combiner              = "OR"
  notification_channels = [google_monitoring_notification_channel.email.id]

  conditions {
    display_name = "${each.value.display} in last 10m"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${each.value.metric}\""
      duration        = "0s"
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      aggregations {
        alignment_period     = "600s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
      }
      trigger {
        count = 1
      }
    }
  }

  documentation {
    content   = each.value.doc
    mime_type = "text/markdown"
  }
}

# ---------------------------------------------------------------------------
# Alert 7/11 — Cloud Run min-instances drift (§8.9, acceptance criterion 15).
# Fires on any Cloud Run service config change (Admin Activity log), which
# includes a min_instance_count change away from 0.  The test simulates
# `gcloud run services update ... --min-instances=1`, which emits this log
# and emails.
# ---------------------------------------------------------------------------
resource "google_logging_metric" "run_config_drift" {
  name   = "cloud_run_config_drift"
  filter = "logName=\"projects/${var.project_id}/logs/cloudaudit.googleapis.com%2Factivity\" protoPayload.serviceName=\"run.googleapis.com\" protoPayload.methodName=\"google.cloud.run.v2.Services.UpdateService\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_alert_policy" "run_min_instances_drift" {
  display_name          = "CLOUD_RUN_MIN_INSTANCES_DRIFT"
  combiner              = "OR"
  notification_channels = [google_monitoring_notification_channel.email.id]

  conditions {
    display_name = "Cloud Run service config changed"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/cloud_run_config_drift\""
      duration        = "0s"
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      aggregations {
        alignment_period     = "600s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
      }
      trigger {
        count = 1
      }
    }
  }

  documentation {
    content   = "A Cloud Run service was updated (§8.9). Verify min_instance_count is still 0 — a non-zero value breaks the scale-to-zero cost ceiling."
    mime_type = "text/markdown"
  }
}

# ---------------------------------------------------------------------------
# Alert 8/11 — MFA_CHALLENGE_FAILED rate > 5 per actor per hour (§8.9).
# Extracts the actor label so the alert can group per-actor.
# ---------------------------------------------------------------------------
resource "google_logging_metric" "mfa_challenge_failed" {
  name   = "mfa_challenge_failed"
  filter = "logName=\"projects/${var.project_id}/logs/vurarad-audit\" jsonPayload.event_type=\"MFA_CHALLENGE_FAILED\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
    labels {
      key        = "actor"
      value_type = "STRING"
    }
  }
  label_extractors = {
    actor = "EXTRACT(jsonPayload.actor)"
  }
}

resource "google_monitoring_alert_policy" "mfa_failure_rate" {
  display_name          = "MFA_CHALLENGE_FAILED_RATE"
  combiner              = "OR"
  notification_channels = [google_monitoring_notification_channel.email.id]

  conditions {
    display_name = "MFA failures > 5 per actor per hour"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/mfa_challenge_failed\""
      duration        = "3600s"
      comparison      = "COMPARISON_GT"
      threshold_value = 5
      aggregations {
        alignment_period     = "3600s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["metric.label.actor"]
      }
      trigger {
        count = 1
      }
    }
  }

  documentation {
    content   = "More than 5 MFA challenge failures by one actor in an hour (§8.9). Possible brute-force or a locked-out legitimate user."
    mime_type = "text/markdown"
  }
}

# ---------------------------------------------------------------------------
# Alert 9/11 — AUDIT_CHAIN_BROKEN — PAGES, not email (§8.9, §10.9).
# The only compliance-event alert: the record of who touched which patient may
# no longer be trustworthy.  Raised by verify_audit_chain.py on divergence
# between the bucket and the mirror (§5.1, acceptance criterion 15).
#
# The verifier runs as read-only vurarad-audit-verifier@ and writes the finding
# to its Cloud Run job stdout as a JSON line; Cloud Run captures that as
# jsonPayload (logName run.googleapis.com/jobs/...), so the metric matches the
# jsonPayload fields project-wide rather than the vurarad-audit log.  Matching
# the vurarad-audit log instead would require granting the verifier
# logging.logWriter — which would let it inject fake audit events into the
# bucket via the sink, breaking the "verifier is not the writer" boundary
# (§5.1).  AUDIT_CHAIN_BROKEN is a unique event type emitted only by the
# verifier, so the unconstrained jsonPayload filter has no false positives.
# ---------------------------------------------------------------------------
resource "google_logging_metric" "audit_chain_broken" {
  name   = "audit_chain_broken"
  filter = "jsonPayload.event_type=\"AUDIT_CHAIN_BROKEN\" OR jsonPayload.error.code=\"AUDIT_CHAIN_BROKEN\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_alert_policy" "audit_chain_broken" {
  display_name          = "AUDIT_CHAIN_BROKEN"
  combiner              = "OR"
  notification_channels = [google_monitoring_notification_channel.paging.id]
  alert_strategy {
    notification_rate_limit {
      period = "3600s"
    }
  }

  conditions {
    display_name = "audit chain divergence in last 10m"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/audit_chain_broken\""
      duration        = "0s"
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      aggregations {
        alignment_period     = "600s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
      }
      trigger {
        count = 1
      }
    }
  }

  documentation {
    content   = "The audit hash chain diverged between the bucket-locked system of record and the Firestore mirror (§5.1). This is a compliance event with a clock on it — page immediately."
    mime_type = "text/markdown"
  }
}

# ---------------------------------------------------------------------------
# Alert 10/11 — nightly Firestore export did not succeed within 26 h (§8.9).
# A counter of export-success log entries; condition_absent fires when no
# success has been seen for 26 h.
# ---------------------------------------------------------------------------
resource "google_logging_metric" "firestore_export_success" {
  name   = "firestore_export_success"
  filter = "logName=\"projects/${var.project_id}/logs/run.googleapis.com%2Fstdout\" jsonPayload.event_type=\"FIRESTORE_EXPORT_SUCCESS\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_alert_policy" "nightly_export_missing" {
  display_name          = "NIGHTLY_FIRESTORE_EXPORT_MISSING"
  combiner              = "OR"
  notification_channels = [google_monitoring_notification_channel.email.id]

  conditions {
    display_name = "no successful Firestore export in 26h"
    condition_absent {
      filter   = "metric.type=\"logging.googleapis.com/user/firestore_export_success\""
      duration = "93600s" # 26 h
      aggregations {
        alignment_period = "3600s"
      }
    }
  }

  documentation {
    content   = "No successful nightly Firestore export in 26 h (§8.9). Backups are the tested-recovery prerequisite (§5.6.1); a missing backup is a DR risk."
    mime_type = "text/markdown"
  }
}

# ---------------------------------------------------------------------------
# Alert 11/11 — three consecutive nightly backup failures — PAGES (§10.9).
# Counter of export-failure log entries; alerts when >= 3 failures occur in a
# 72 h window (approximating three consecutive nightly runs).
# ---------------------------------------------------------------------------
resource "google_logging_metric" "firestore_export_failed" {
  name   = "firestore_export_failed"
  filter = "logName=\"projects/${var.project_id}/logs/run.googleapis.com%2Fstdout\" jsonPayload.event_type=\"FIRESTORE_EXPORT_FAILED\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_alert_policy" "three_backup_failures" {
  display_name          = "THREE_CONSECUTIVE_BACKUP_FAILURES"
  combiner              = "OR"
  notification_channels = [google_monitoring_notification_channel.paging.id]
  alert_strategy {
    notification_rate_limit {
      period = "3600s"
    }
  }

  conditions {
    display_name = ">= 3 export failures in 72h"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/firestore_export_failed\""
      duration        = "259200s" # 72 h
      comparison      = "COMPARISON_GT"
      threshold_value = 2
      aggregations {
        alignment_period     = "259200s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
      }
      trigger {
        count = 1
      }
    }
  }

  documentation {
    content   = "Three consecutive nightly backup failures (§10.9). The tested-recovery commitment is at risk — page."
    mime_type = "text/markdown"
  }
}
