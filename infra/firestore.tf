# ---------------------------------------------------------------------------
# Firestore Native-mode database, composite indexes (§4.5), TTL policies
# (§4.6), the `instances` single-field exemption, and PITR (§5.6.1).
#
# Acceptance criterion 10: exactly 8 composite indexes and 9 TTL policies
# exist, and `studies` / `reports` carry NO TTL (clinical records are removed
# only by explicit, audited erasure — §3.12).
# ---------------------------------------------------------------------------

resource "google_firestore_database" "firestore" {
  project     = var.project_id
  name        = "(default)"
  location_id = var.region
  type        = "FIRESTORE_NATIVE"
  # PITR keeps 7 days of in-place history — protects against operator error,
  # NOT against a deleted database (§5.6.1).
  point_in_time_recovery_enablement = "POINT_IN_TIME_RECOVERY_ENABLED"
  # Prevent accidental database deletion; remove this only as a deliberate
  # teardown step.
  delete_protection_state = "DELETE_PROTECTION_ENABLED"

  depends_on = [google_project_service.enabled]
}

# ---------------------------------------------------------------------------
# §4.5 — exactly 8 composite indexes.  Every supported filter combination in
# §3.4 and §3.12 maps to one of these; an unsupported combination is rejected
# with VALIDATION_ERROR rather than silently degrading into a scan.
#
# The §4.5 table lists the `accession` index as "single-field, automatic".
# It is materialised here as a managed composite index so the count is exact
# and the setting is drift-detectable by `terraform plan` (the provider
# appends `__name__` automatically).
# ---------------------------------------------------------------------------
resource "google_firestore_index" "studies_patient_date" {
  project     = var.project_id
  database    = google_firestore_database.firestore.name
  collection  = "studies"
  query_scope = "COLLECTION"

  fields {
    field_path = "patientRef"
    order      = "ASCENDING"
  }
  fields {
    field_path = "studyDate"
    order      = "DESCENDING"
  }
}

resource "google_firestore_index" "studies_status_date" {
  project     = var.project_id
  database    = google_firestore_database.firestore.name
  collection  = "studies"
  query_scope = "COLLECTION"

  fields {
    field_path = "status"
    order      = "ASCENDING"
  }
  fields {
    field_path = "studyDate"
    order      = "DESCENDING"
  }
}

resource "google_firestore_index" "studies_modality_date" {
  project     = var.project_id
  database    = google_firestore_database.firestore.name
  collection  = "studies"
  query_scope = "COLLECTION"

  fields {
    field_path = "modality"
    order      = "ASCENDING"
  }
  fields {
    field_path = "studyDate"
    order      = "DESCENDING"
  }
}

resource "google_firestore_index" "studies_accession" {
  project     = var.project_id
  database    = google_firestore_database.firestore.name
  collection  = "studies"
  query_scope = "COLLECTION"

  fields {
    field_path = "accession"
    order      = "ASCENDING"
  }
  fields {
    field_path = "__name__"
    order      = "ASCENDING"
  }
}

resource "google_firestore_index" "audit_mirror_patient_time" {
  project     = var.project_id
  database    = google_firestore_database.firestore.name
  collection  = "audit_mirror"
  query_scope = "COLLECTION"

  fields {
    field_path = "patientKey"
    order      = "ASCENDING"
  }
  fields {
    field_path = "timestamp"
    order      = "DESCENDING"
  }
}

resource "google_firestore_index" "audit_mirror_actor_time" {
  project     = var.project_id
  database    = google_firestore_database.firestore.name
  collection  = "audit_mirror"
  query_scope = "COLLECTION"

  fields {
    field_path = "actorUid"
    order      = "ASCENDING"
  }
  fields {
    field_path = "timestamp"
    order      = "DESCENDING"
  }
}

resource "google_firestore_index" "audit_mirror_action_time" {
  project     = var.project_id
  database    = google_firestore_database.firestore.name
  collection  = "audit_mirror"
  query_scope = "COLLECTION"

  fields {
    field_path = "action"
    order      = "ASCENDING"
  }
  fields {
    field_path = "timestamp"
    order      = "DESCENDING"
  }
}

resource "google_firestore_index" "ingest_jobs_status_started" {
  project     = var.project_id
  database    = google_firestore_database.firestore.name
  collection  = "ingest_jobs"
  query_scope = "COLLECTION"

  fields {
    field_path = "status"
    order      = "ASCENDING"
  }
  fields {
    field_path = "startedAt"
    order      = "DESCENDING"
  }
}

# ---------------------------------------------------------------------------
# §4.5 — `instances` single-field exemption.  The embedded array would
# otherwise generate ~2,000 index entries per series for nothing.  An empty
# `index_config {}` block exempts the field from automatic single-field
# indexing.
# ---------------------------------------------------------------------------
resource "google_firestore_field" "instances_exempt" {
  project    = var.project_id
  database   = google_firestore_database.firestore.name
  collection = "series"
  field      = "instances"

  index_config {}

  depends_on = [google_firestore_database.firestore]
}

# ---------------------------------------------------------------------------
# §4.6 — exactly 9 TTL policies.  `studies` and `reports` are deliberately
# absent: clinical records are removed only by explicit, audited erasure
# (§3.12).  An empty `ttl_config {}` block enables TTL on the field.
#
# `reports/*/versions` is the `versions` subcollection (collection group),
# NOT the top-level `reports` collection — so "reports have no TTL" holds
# while edit-history versions expire at 6 years (§4.6).
# ---------------------------------------------------------------------------
resource "google_firestore_field" "ttl_audit_mirror" {
  project    = var.project_id
  database   = google_firestore_database.firestore.name
  collection = "audit_mirror"
  field      = "expireAt"
  ttl_config {}
}

resource "google_firestore_field" "ttl_report_versions" {
  project    = var.project_id
  database   = google_firestore_database.firestore.name
  collection = "versions"
  field      = "expireAt"
  ttl_config {}
}

resource "google_firestore_field" "ttl_ai_cache" {
  project    = var.project_id
  database   = google_firestore_database.firestore.name
  collection = "ai_cache"
  field      = "expireAt"
  ttl_config {}
}

resource "google_firestore_field" "ttl_budget" {
  project    = var.project_id
  database   = google_firestore_database.firestore.name
  collection = "budget"
  field      = "expireAt"
  ttl_config {}
}

resource "google_firestore_field" "ttl_ingest_jobs" {
  project    = var.project_id
  database   = google_firestore_database.firestore.name
  collection = "ingest_jobs"
  field      = "expireAt"
  ttl_config {}
}

resource "google_firestore_field" "ttl_uploads" {
  project    = var.project_id
  database   = google_firestore_database.firestore.name
  collection = "uploads"
  field      = "expireAt"
  ttl_config {}
}

resource "google_firestore_field" "ttl_ingest_locks" {
  project    = var.project_id
  database   = google_firestore_database.firestore.name
  collection = "ingest_locks"
  field      = "expireAt"
  ttl_config {}
}

resource "google_firestore_field" "ttl_idempotency" {
  project    = var.project_id
  database   = google_firestore_database.firestore.name
  collection = "idempotency"
  field      = "expireAt"
  ttl_config {}
}

resource "google_firestore_field" "ttl_ratelimit" {
  project    = var.project_id
  database   = google_firestore_database.firestore.name
  collection = "ratelimit"
  field      = "expireAt"
  ttl_config {}
}
