# ---------------------------------------------------------------------------
# Cloud Run v2 — the API service (§10.6, acceptance criterion 11).
#
#   min_instance_count = 0  (explicit, drift-detected by the alert in
#                            logging.tf and by `terraform plan` in CI)
#   max_instance_count = 10 (a deliberate spend ceiling: bounds worst-case
#                            concurrency-driven cost under a spike or loop bug)
#   cpu 1 / memory 512Mi / concurrency 40 / timeout 300s
#   secret env vars pinned to versions (secrets.tf, §5.7)
#   startup_probe → /readyz
#
# A non-zero min_instance_count breaks the scale-to-zero cost ceiling and is
# the single most important drift setting (§8.9).
# ---------------------------------------------------------------------------
resource "google_cloud_run_v2_service" "api" {
  name     = "vurarad-api"
  project  = var.project_id
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.run.email
    scaling {
      min_instance_count = 0
      max_instance_count = 10
    }
    max_instance_request_concurrency = 40
    timeout                          = "300s"

    containers {
      # Pinned image tag — :latest keeps the module stable across re-applies;
      # the deploy workflow pins a digest at release time (§10.5).
      image = "${google_artifact_registry_repository.repo.location}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.repo.name}/vurarad-api:${var.api_image_tag}"
      name  = "vurarad-api"
      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }

      # Non-secret env vars point the app at the infra it runs on (§1.1).
      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "GCP_REGION"
        value = var.region
      }
      env {
        name  = "ENVIRONMENT"
        value = var.environment
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
        name  = "QUARANTINE_BUCKET_NAME"
        value = google_storage_bucket.quarantine.name
      }
      env {
        name  = "FIREBASE_PROJECT_ID"
        value = var.project_id
      }

      # Secret env var — pinned to a numeric version (§5.7). Rotation = new
      # version + new revision; `latest` is never referenced so a rotation
      # cannot silently change behaviour mid-revision.
      env {
        name = "FIREBASE_ADMIN_CONFIG"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.firebase_admin_config.secret_id
            version = var.firebase_admin_config_version
          }
        }
      }

      startup_probe {
        http_get {
          path = "/readyz"
          port = 8080
        }
        period_seconds  = 10
        timeout_seconds = 5
      }
    }
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  depends_on = [
    google_project_service.enabled,
    google_storage_bucket_iam_member.run_dicom_admin,
    google_secret_manager_secret_iam_member.run_firebase_config,
  ]
}
