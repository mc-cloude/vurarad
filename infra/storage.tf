# ---------------------------------------------------------------------------
# The 7 GCS buckets (§10.6).  All use uniform bucket-level access and have
# public-access-prevention ENFORCED (acceptance criterion 8).  The
# differences are what matter — retention, lifecycle, soft-delete, and
# versioning per the table in §10.6.
# ---------------------------------------------------------------------------

locals {
  # 2,190 days in seconds — the HIPAA §164.316(b)(2)(i) 6-year retention.
  audit_retention_seconds = 2190 * 24 * 60 * 60
}

# ---------------------------------------------------------------------------
# vurarad-dicom — imaging objects.
#   Standard → Nearline@90d → Coldline@365d.  Versioning OFF (cost).
#   CORS allows GET/HEAD from the CloudFront origin with Range so the DICOM
#   loader can stream byte ranges via signed URLs (§3.6, §10.6).
# ---------------------------------------------------------------------------
resource "google_storage_bucket" "dicom" {
  name                        = "${var.bucket_prefix}dicom"
  project                     = var.project_id
  location                    = var.region
  storage_class               = "STANDARD"
  force_destroy               = false
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  lifecycle_rule {
    condition {
      age = 90
    }
    action {
      type          = "SetStorageClass"
      storage_class = "NEARLINE"
    }
  }
  lifecycle_rule {
    condition {
      age = 365
    }
    action {
      type          = "SetStorageClass"
      storage_class = "COLDLINE"
    }
  }

  cors {
    origin          = ["https://${var.spa_domain}"]
    method          = ["GET", "HEAD"]
    response_header = ["Content-Range", "Content-Length", "Content-Type", "Cache-Control", "Range"]
    max_age_seconds = 3600
  }
}

# ---------------------------------------------------------------------------
# vurarad-quarantine — ingest staging.
#   Standard, lifecycle delete@7d.  soft_delete_policy.retention_duration_seconds
#   = 0 — the default 7-day soft delete would bill ~100 GB/mo of deleted
#   objects and double the §9.2 quarantine line (acceptance criterion 9).
# ---------------------------------------------------------------------------
resource "google_storage_bucket" "quarantine" {
  name                        = "${var.bucket_prefix}quarantine"
  project                     = var.project_id
  location                    = var.region
  storage_class               = "STANDARD"
  force_destroy               = false
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  soft_delete_policy {
    retention_duration_seconds = 0
  }

  lifecycle_rule {
    condition {
      age = 7
    }
    action {
      type = "Delete"
    }
  }
}

# ---------------------------------------------------------------------------
# vurarad-audit — the immutable system of record (§5.1).
#   Standard → Coldline@90d.  retention_policy { 2190d, is_locked = true } +
#   lifecycle { prevent_destroy = true }.  Once locked the retention period
#   cannot be shortened or removed by ANYONE, including a project owner, and
#   objects cannot be deleted before expiry (acceptance criterion 2).  This
#   is a one-way door — the apply that first sets is_locked is a deliberate,
#   separately-reviewed step (§10.6).
#
#   Written ONLY by the Cloud Logging sink's Google-managed service agent
#   (logging.tf).  vurarad-run@ has no permission here at all (iam.tf).
# ---------------------------------------------------------------------------
resource "google_storage_bucket" "audit" {
  name                        = "${var.bucket_prefix}audit"
  project                     = var.project_id
  location                    = var.region
  storage_class               = "STANDARD"
  force_destroy               = false
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  retention_policy {
    retention_period = local.audit_retention_seconds
    is_locked        = true
  }

  lifecycle_rule {
    condition {
      age = 90
    }
    action {
      type          = "SetStorageClass"
      storage_class = "COLDLINE"
    }
  }

  # prevent_destroy: `terraform destroy` will refuse to delete this bucket.
  # Combined with the locked retention this is the WORM boundary (§5.1).
  lifecycle {
    prevent_destroy = true
  }
}

# ---------------------------------------------------------------------------
# vurarad-audit-exports — signed export objects.
#   Standard, lifecycle delete@30d.  Objects carry `private, no-store` and
#   are served via 10-minute signed URLs only.  vurarad-run@ has
#   objectCreator + objectViewer here but NO delete (iam.tf).
# ---------------------------------------------------------------------------
resource "google_storage_bucket" "audit_exports" {
  name                        = "${var.bucket_prefix}audit-exports"
  project                     = var.project_id
  location                    = var.region
  storage_class               = "STANDARD"
  force_destroy               = false
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  lifecycle_rule {
    condition {
      age = 30
    }
    action {
      type = "Delete"
    }
  }
}

# ---------------------------------------------------------------------------
# vurarad-backup — Firestore exports.
#   us-east1 (NOT the primary region — acceptance criterion 13), Nearline,
#   versioning ON, lifecycle delete@35d (§5.6.1).  Different region on
#   purpose: a single-region outage or an accidental region-wide lifecycle
#   rule should not take both the primary and the backup.
# ---------------------------------------------------------------------------
resource "google_storage_bucket" "backup" {
  name                        = "${var.bucket_prefix}backup"
  project                     = var.project_id
  location                    = var.backup_region
  storage_class               = "NEARLINE"
  force_destroy               = false
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      age = 35
    }
    action {
      type = "Delete"
    }
  }
}

# ---------------------------------------------------------------------------
# vurarad-backup-dicom — Storage Transfer destination for DICOM.
#   us-east1, Coldline, soft delete disabled (acceptance criterion 9).
# ---------------------------------------------------------------------------
resource "google_storage_bucket" "backup_dicom" {
  name                        = "${var.bucket_prefix}backup-dicom"
  project                     = var.project_id
  location                    = var.backup_region
  storage_class               = "COLDLINE"
  force_destroy               = false
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  soft_delete_policy {
    retention_duration_seconds = 0
  }
}

# ---------------------------------------------------------------------------
# vurarad-tfstate — Terraform remote state.
#   us-central1-equivalent primary region, Standard, versioning ON, keep 90
#   versions (§10.6, [#15]).  Shared with the AWS module under a distinct
#   prefix (backend.tf / infra/aws/backend.tf).
#
#   Bootstrap note: this bucket must exist before `terraform init` can load
#   remote state.  The first apply that creates it is run with local state
#   (`terraform init -backend=false`), then migrated with `terraform init
#   -migrate-state` once the bucket exists.
# ---------------------------------------------------------------------------
resource "google_storage_bucket" "tfstate" {
  name                        = "${var.bucket_prefix}tfstate"
  project                     = var.project_id
  location                    = var.region
  storage_class               = "STANDARD"
  force_destroy               = false
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = true
  }

  # Keep the 90 most recent versions of every state object; prune older.
  lifecycle_rule {
    condition {
      num_newer_versions = 90
      with_state         = "ANY"
    }
    action {
      type = "Delete"
    }
  }
}
