# ---------------------------------------------------------------------------
# Enable every API the module depends on (§10.6).  `disable_on_destroy` is
# false so a teardown does not strand other consumers of the project.
# ---------------------------------------------------------------------------
locals {
  enabled_apis = [
    "run.googleapis.com",
    "firestore.googleapis.com",
    "storage.googleapis.com",
    "aiplatform.googleapis.com",
    "secretmanager.googleapis.com",
    "identitytoolkit.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "cloudscheduler.googleapis.com",
    "storagetransfer.googleapis.com",
    "iamcredentials.googleapis.com",
    "pubsub.googleapis.com",
  ]
}

resource "google_project_service" "enabled" {
  for_each           = toset(local.enabled_apis)
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}
