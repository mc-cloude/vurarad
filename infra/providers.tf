# ---------------------------------------------------------------------------
# Provider configuration.  The project and region are bound once so every
# resource inherits them unless explicitly overridden (backup buckets use
# var.backup_region).
# ---------------------------------------------------------------------------
provider "google" {
  project = var.project_id
  region  = var.region
}
