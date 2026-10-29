# ---------------------------------------------------------------------------
# Scheduled jobs (§10.6, §5.6.1).
#
# 1. Nightly Firestore export (03:00 UTC) → gs://vurarad-backup
# 2. Weekly audit-chain verification (OIDC as vurarad-audit-verifier@)
# 3. Nightly incremental DICOM backup via Storage Transfer Service
#
# The verifier is a SEPARATE identity from the data writer — the thing that
# checks the audit trail is not the thing that writes it (§5.1).
# ---------------------------------------------------------------------------

# Project number is needed to address the Cloud Scheduler service agent.
data "google_project" "project" {
  project_id = var.project_id
}

locals {
  export_job_name = "firestore-export"
  verify_job_name = "audit-chain-verify"
  scheduler_sa    = "serviceAccount:service-${data.google_project.project.number}@gcp-sa-cloudscheduler.iam.gserviceaccount.com"
  export_run_url  = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${local.export_job_name}:run"
  verify_run_url  = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${local.verify_job_name}:run"
}

# Cloud Scheduler must be allowed to mint OIDC tokens as the target SAs.
resource "google_service_account_iam_member" "scheduler_token_creator_run" {
  service_account_id = google_service_account.run.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = local.scheduler_sa
}

resource "google_service_account_iam_member" "scheduler_token_creator_verifier" {
  service_account_id = google_service_account.audit_verifier.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = local.scheduler_sa
}

# ---------------------------------------------------------------------------
# 1. Nightly Firestore export job (§5.6.1).
# ---------------------------------------------------------------------------
resource "google_cloud_run_v2_job" "firestore_export" {
  name     = local.export_job_name
  location = var.region
  project  = var.project_id

  template {
    template {
      service_account = google_service_account.run.email
      timeout         = "1800s" # 30 min — ample at our data volume
      containers {
        image   = var.backup_job_image
        command = ["bash", "-c"]
        args = [
          <<-EOT
            set -e
            PREFIX="gs://${google_storage_bucket.backup.name}/firestore-exports/$(date -u +%Y%m%d-%H%M%S)"
            if gcloud firestore export "$${PREFIX}" --project="${var.project_id}"; then
              echo '{"event_type":"FIRESTORE_EXPORT_SUCCESS"}'
            else
              echo '{"event_type":"FIRESTORE_EXPORT_FAILED"}'
              exit 1
            fi
          EOT
        ]
      }
    }
  }

  depends_on = [google_project_service.enabled]
}

resource "google_cloud_scheduler_job" "nightly_firestore_export" {
  name        = "nightly-firestore-export"
  project     = var.project_id
  region      = var.region
  schedule    = "0 3 * * *" # 03:00 UTC daily
  time_zone   = "UTC"
  description = "Nightly Firestore export to gs://vurarad-backup (§5.6.1)"

  http_target {
    uri         = local.export_run_url
    http_method = "POST"
    oidc_token {
      service_account_email = google_service_account.run.email
      audience              = local.export_run_url
    }
  }

  depends_on = [google_cloud_run_v2_job.firestore_export]
}

# ---------------------------------------------------------------------------
# 2. Weekly audit-chain verification job (§5.1).
#    Runs as vurarad-audit-verifier@ (read-only on the audit bucket and the
#    Firestore mirror).  Invoked by Cloud Scheduler with an OIDC token.
# ---------------------------------------------------------------------------
resource "google_cloud_run_v2_job" "audit_chain_verify" {
  name     = local.verify_job_name
  location = var.region
  project  = var.project_id

  template {
    template {
      service_account = google_service_account.audit_verifier.email
      timeout         = "900s"
      containers {
        image   = var.verifier_image
        command = ["python", "-m", "app.tools.verify_audit_chain", "--days", "7"]
        env {
          name  = "GCP_PROJECT_ID"
          value = var.project_id
        }
        env {
          name  = "GCP_REGION"
          value = var.region
        }
        env {
          name  = "FIREBASE_PROJECT_ID"
          value = var.project_id
        }
        env {
          name  = "PIXEL_BUCKET_NAME"
          value = google_storage_bucket.dicom.name
        }
        env {
          name  = "AUDIT_BUCKET_NAME"
          value = google_storage_bucket.audit.name
        }
        env {
          name  = "FIRESTORE_DATABASE"
          value = google_firestore_database.firestore.name
        }
      }
    }
  }

  depends_on = [google_project_service.enabled]
}

resource "google_cloud_scheduler_job" "weekly_audit_verify" {
  name        = "weekly-audit-chain-verify"
  project     = var.project_id
  region      = var.region
  schedule    = "0 6 * * 1" # weekly, Monday 06:00 UTC
  time_zone   = "UTC"
  description = "Weekly cross-store audit-chain verification as vurarad-audit-verifier@ (§5.1)"

  http_target {
    uri         = local.verify_run_url
    http_method = "POST"
    oidc_token {
      service_account_email = google_service_account.audit_verifier.email
      audience              = local.verify_run_url
    }
  }

  depends_on = [google_cloud_run_v2_job.audit_chain_verify]
}

# ---------------------------------------------------------------------------
# 3. Nightly incremental DICOM backup via Storage Transfer Service (§5.6.1).
#    vurarad-dicom → vurarad-backup-dicom (us-east1).  Runs under the
#    Storage Transfer Google-managed service agent, not vurarad-run@.
# ---------------------------------------------------------------------------
resource "google_storage_transfer_job" "dicom_backup" {
  project     = var.project_id
  description = "Nightly incremental DICOM backup vurarad-dicom → vurarad-backup-dicom (§5.6.1)"

  transfer_spec {
    gcs_data_source {
      bucket_name = google_storage_bucket.dicom.name
    }
    gcs_data_sink {
      bucket_name = google_storage_bucket.backup_dicom.name
    }
    transfer_options {
      overwrite_when = "DIFFERENT"
    }
  }

  schedule {
    schedule_start_date {
      year  = 2026
      month = 1
      day   = 1
    }
    start_time_of_day {
      hours   = 3
      minutes = 30
      seconds = 0
      nanos   = 0
    }
    repeat_interval = "86400s" # daily
  }

  depends_on = [google_project_service.enabled]
}
