# ---------------------------------------------------------------------------
# Artifact Registry (§10.6).  One Docker repo + a cleanup policy:
#   - keep the 3 most recent tagged images of vurarad-api
#   - delete untagged images after 7 days
# Prevents unbounded image accumulation, which is a real cost line (§9.2).
# ---------------------------------------------------------------------------
resource "google_artifact_registry_repository" "repo" {
  location      = var.region
  repository_id = "vurarad"
  project       = var.project_id
  description   = "vuraRAD container images (§10.6)"
  format        = "DOCKER"

  # Keep the 3 most recent tagged vurarad-api images.
  cleanup_policies {
    id     = "keep-3-tagged"
    action = "KEEP"
    most_recent_versions {
      keep_count            = 3
      package_name_prefixes = ["vurarad-api"]
    }
  }

  # Delete untagged images older than 7 days.
  cleanup_policies {
    id     = "delete-untagged-7d"
    action = "DELETE"
    condition {
      tag_state  = "UNTAGGED"
      older_than = "604800s" # 7 days
    }
  }

  depends_on = [google_project_service.enabled]
}
