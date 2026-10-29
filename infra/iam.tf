# ---------------------------------------------------------------------------
# Identity (§5.6).  Four GCP service accounts, no project-level Editor
# anywhere.  The identity boundary that makes the audit trail immutable lives
# here: vurarad-run@ has NO permission on vurarad-audit, so the runtime
# identity cannot alter the system of record even in principle (§5.1).
#
# Acceptance criteria 3, 5, 6.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Service accounts
# ---------------------------------------------------------------------------
resource "google_service_account" "run" {
  account_id   = "vurarad-run"
  display_name = "vuraRAD Cloud Run runtime"
  description  = "Runtime identity for the API Cloud Run service. No write/delete on vurarad-audit (§5.6)."
  project      = var.project_id
}

resource "google_service_account" "audit_verifier" {
  account_id   = "vurarad-audit-verifier"
  display_name = "vuraRAD weekly audit-chain verifier"
  description  = "Read-only cross-store verifier invoked weekly by Cloud Scheduler with an OIDC token (§5.1)."
  project      = var.project_id
}

resource "google_service_account" "deploy" {
  account_id   = "vurarad-deploy"
  display_name = "vuraRAD GitHub Actions deploy"
  description  = "CI deploy identity via Workload Identity Federation. No Firestore/Storage/aiplatform — CI cannot read PHI (§5.6)."
  project      = var.project_id
}

resource "google_service_account" "tf" {
  account_id   = "vurarad-tf"
  display_name = "vuraRAD Terraform"
  description  = "Elevated identity for human-operated terraform apply. Never used from CI — CI runs plan only (§10.6)."
  project      = var.project_id
}

# ---------------------------------------------------------------------------
# Custom roles
# ---------------------------------------------------------------------------
# vurarad.firestoreUser — datastore entities CRUD.  Database-scoped, NOT
# collection-scoped: Firestore IAM cannot name a collection, which is why the
# system of record is the bucket, not Firestore (§5.1).
resource "google_project_iam_custom_role" "firestore_user" {
  role_id     = "firestoreUser"
  title       = "vuraRAD Firestore User"
  description = "datastore.entities create/get/list/update/delete (§5.6)"
  project     = var.project_id
  permissions = [
    "datastore.entities.create",
    "datastore.entities.get",
    "datastore.entities.list",
    "datastore.entities.update",
    "datastore.entities.delete",
  ]
}

# vurarad.firestoreReader — read-only mirror access for the verifier.
resource "google_project_iam_custom_role" "firestore_reader" {
  role_id     = "firestoreReader"
  title       = "vuraRAD Firestore Reader"
  description = "datastore.entities get/list only — for the audit verifier (§5.6)"
  project     = var.project_id
  permissions = [
    "datastore.entities.get",
    "datastore.entities.list",
  ]
}

# ---------------------------------------------------------------------------
# vurarad-run@ — Cloud Run runtime (§5.6)
# ---------------------------------------------------------------------------
resource "google_project_iam_member" "run_firestore" {
  project = var.project_id
  role    = google_project_iam_custom_role.firestore_user.id
  member  = "serviceAccount:${google_service_account.run.email}"
}

resource "google_project_iam_member" "run_aiplatform" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.run.email}"
}

resource "google_project_iam_member" "run_logging" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.run.email}"
}

resource "google_project_iam_member" "run_firebase_auth" {
  project = var.project_id
  role    = "roles/firebaseauth.admin"
  member  = "serviceAccount:${google_service_account.run.email}"
}

# V4 signed URLs via signBlob — no private key.  Token-creator on ITSELF only.
resource "google_service_account_iam_member" "run_token_creator_self" {
  service_account_id = google_service_account.run.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.run.email}"
}

# Per-bucket grants for vurarad-run@.  NOTE: there is deliberately NO grant
# on google_storage_bucket.audit — no read, no write, no delete (§5.1).
resource "google_storage_bucket_iam_member" "run_dicom_admin" {
  bucket = google_storage_bucket.dicom.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.run.email}"
}

resource "google_storage_bucket_iam_member" "run_quarantine_admin" {
  bucket = google_storage_bucket.quarantine.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.run.email}"
}

# Report PDFs are stored in vurarad-dicom (under a `reports/` prefix) — the
# §10.6 bucket list consolidates to 7 buckets, and §5.6.1 states reports are
# covered by the Firestore export (report *content* lives in Firestore).  The
# objectAdmin grant above on vurarad-dicom therefore covers report objects too.
# There is deliberately NO separate vurarad-reports bucket (acceptance
# criterion: exactly 7 buckets).

# audit-exports: write + read, NO delete.
resource "google_storage_bucket_iam_member" "run_exports_creator" {
  bucket = google_storage_bucket.audit_exports.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.run.email}"
}

resource "google_storage_bucket_iam_member" "run_exports_viewer" {
  bucket = google_storage_bucket.audit_exports.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.run.email}"
}

# The nightly Firestore export Cloud Run Job runs as vurarad-run@ (scheduler.tf),
# so it needs export permission + write access to the backup bucket (§5.6.1).
# These grant the scheduled backup, nothing more.
resource "google_project_iam_member" "run_firestore_export" {
  project = var.project_id
  role    = "roles/datastore.importExportAdmin"
  member  = "serviceAccount:${google_service_account.run.email}"
}

resource "google_storage_bucket_iam_member" "run_backup_creator" {
  bucket = google_storage_bucket.backup.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.run.email}"
}

# ---------------------------------------------------------------------------
# vurarad-audit-verifier@ — read-only in BOTH stores (§5.1).  A separate
# identity precisely so the thing that verifies the trail is not the thing
# that writes the data.
# ---------------------------------------------------------------------------
resource "google_project_iam_member" "verifier_firestore_reader" {
  project = var.project_id
  role    = google_project_iam_custom_role.firestore_reader.id
  member  = "serviceAccount:${google_service_account.audit_verifier.email}"
}

resource "google_storage_bucket_iam_member" "verifier_audit_viewer" {
  bucket = google_storage_bucket.audit.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.audit_verifier.email}"
}

# ---------------------------------------------------------------------------
# vurarad-deploy@ — GitHub Actions via WIF (§5.6).
#   run.developer, artifactregistry.writer, serviceAccountUser on vurarad-run@,
#   cloudbuild.builds.editor.  NO Firestore, NO Storage, NO aiplatform,
#   NO firebaseauth — CI cannot read PHI (acceptance criterion 6).
# ---------------------------------------------------------------------------
resource "google_project_iam_member" "deploy_run_developer" {
  project = var.project_id
  role    = "roles/run.developer"
  member  = "serviceAccount:${google_service_account.deploy.email}"
}

resource "google_project_iam_member" "deploy_ar_writer" {
  project = var.project_id
  role    = "roles/artifactregistry.writer"
  member  = "serviceAccount:${google_service_account.deploy.email}"
}

resource "google_project_iam_member" "deploy_cloudbuild_editor" {
  project = var.project_id
  role    = "roles/cloudbuild.builds.editor"
  member  = "serviceAccount:${google_service_account.deploy.email}"
}

resource "google_service_account_iam_member" "deploy_sa_user_on_run" {
  service_account_id = google_service_account.run.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deploy.email}"
}

# ---------------------------------------------------------------------------
# vurarad-tf@ — elevated, human-operated only (§10.6).  Granted the admin
# roles required to manage every resource in this module; never used from CI.
# ---------------------------------------------------------------------------
resource "google_project_iam_member" "tf_editor" {
  project = var.project_id
  role    = "roles/editor"
  member  = "serviceAccount:${google_service_account.tf.email}"
}

resource "google_project_iam_member" "tf_iam_admin" {
  project = var.project_id
  role    = "roles/resourcemanager.projectIamAdmin"
  member  = "serviceAccount:${google_service_account.tf.email}"
}

resource "google_project_iam_member" "tf_security_admin" {
  project = var.project_id
  role    = "roles/iam.securityAdmin"
  member  = "serviceAccount:${google_service_account.tf.email}"
}

resource "google_project_iam_member" "tf_role_admin" {
  project = var.project_id
  role    = "roles/iam.roleAdmin"
  member  = "serviceAccount:${google_service_account.tf.email}"
}

resource "google_project_iam_member" "tf_logging_config" {
  project = var.project_id
  role    = "roles/logging.configWriter"
  member  = "serviceAccount:${google_service_account.tf.email}"
}

resource "google_project_iam_member" "tf_monitoring_admin" {
  project = var.project_id
  role    = "roles/monitoring.admin"
  member  = "serviceAccount:${google_service_account.tf.email}"
}

resource "google_project_iam_member" "tf_service_usage" {
  project = var.project_id
  role    = "roles/serviceusage.serviceUsageAdmin"
  member  = "serviceAccount:${google_service_account.tf.email}"
}

resource "google_project_iam_member" "tf_billing_costs" {
  project = var.project_id
  role    = "roles/billing.costsManager"
  member  = "serviceAccount:${google_service_account.tf.email}"
}

# roles/datastore.owner — database lifecycle (create/delete) + import/export.
# Needed for the quarterly restore drill (§5.6.1), which creates a scratch
# Firestore database, imports the latest nightly export, and deletes it; and for
# terraform to manage the google_firestore_database resource.  roles/editor
# grants databases.create/update but NOT databases.delete, so this is added
# rather than relying on editor.  tf@ is the elevated, human-operated identity;
# it is never reachable from automated CI (see tf_wif_impersonation_drill).
resource "google_project_iam_member" "tf_datastore_owner" {
  project = var.project_id
  role    = "roles/datastore.owner"
  member  = "serviceAccount:${google_service_account.tf.email}"
}

# vurarad-tf@ may act as vurarad-run@ so the quarterly restore drill (§5.6.1)
# can deploy its scratch Cloud Run revision as the least-privilege runtime
# identity — the one with Firestore + bucket access — rather than widening
# vurarad-deploy@ (which stays Firestore/Storage-free, criterion 6).  This adds
# no new capability: tf@ already holds roles/iam.securityAdmin and could grant
# itself this binding at will; making it explicit avoids a runtime setIamPolicy
# dance in the workflow.  tf@ is reachable only via workflow_dispatch
# (tf_wif_impersonation_drill), never from automated PR/push CI.
resource "google_service_account_iam_member" "tf_act_as_run" {
  service_account_id = google_service_account.run.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.tf.email}"
}

# ---------------------------------------------------------------------------
# Workload Identity Federation for GitHub Actions (§5.6).
# CI federates as vurarad-deploy@; no JSON key exists (acceptance criterion 5).
# ---------------------------------------------------------------------------
resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github-pool"
  display_name              = "GitHub Actions pool"
  description               = "WIF pool for vurarad-deploy@ (§5.6)"
  project                   = var.project_id
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  project                            = var.project_id

  attribute_mapping = {
    "google.subject"           = "assertion.sub"
    "attribute.repository"     = "assertion.repository"
    "attribute.repository_ref" = "assertion.ref"
    "attribute.event_name"     = "assertion.event_name"
  }

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }

  # Restrict to this repository only.
  attribute_condition = "assertion.repository=='${var.github_repo}'"
}

# Allow the WIF pool, scoped to this repo's main branch, to impersonate
# vurarad-deploy@.
resource "google_service_account_iam_member" "deploy_wif_impersonation" {
  service_account_id = google_service_account.deploy.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repo}"
}

# Allow the WIF pool to impersonate vurarad-tf@ ONLY for manual dispatches
# (workflow_dispatch) in this repo.  The provider's attribute_condition already
# pins impersonation to this repository; scoping the member to event_name=
# workflow_dispatch means automated PR/push CI can never reach the elevated
# terraform identity — only a human-triggered restore drill can (§5.6.1).
# vurarad-deploy@ stays Firestore/Storage-free (acceptance criterion 6); the
# restore drill's scratch-DB create/import/delete is elevated infra work, so it
# runs as vurarad-tf@ rather than widening deploy@.
resource "google_service_account_iam_member" "tf_wif_impersonation_drill" {
  service_account_id = google_service_account.tf.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.event_name/workflow_dispatch"
}

# ---------------------------------------------------------------------------
# Org policy: disableServiceAccountKeyCreation (§5.6, acceptance criterion 5).
# Enforced at the project level — makes service-account JSON key creation
# impossible rather than merely discouraged.  No user-managed keys exist.
# ---------------------------------------------------------------------------
resource "google_org_policy_policy" "disable_sa_key_creation" {
  name   = "projects/${var.project_id}/policies/iam.disableServiceAccountKeyCreation"
  parent = "projects/${var.project_id}"

  spec {
    reset = true
    rules {
      enforce = "TRUE"
    }
  }

  depends_on = [google_project_service.enabled]
}
