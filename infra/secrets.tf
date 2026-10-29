# ---------------------------------------------------------------------------
# Secret Manager (§5.7).  Exactly 2 secrets, inside the 6-free-versions tier.
# Values are supplied OUT-OF-BAND — never in .tf or in Terraform state.  Each
# is mounted as a secret ENVIRONMENT variable (not a volume) so a missing
# secret fails the Cloud Run revision at startup, composing with the app's
# fail-fast boot (§1.1).  `latest` is never referenced — pinned versions only,
# so a rotation cannot silently change behaviour mid-revision.
# ---------------------------------------------------------------------------

# Identity Platform / Firebase Admin config — mounted into the Cloud Run
# revision (run.tf) so the app can verify tokens and set custom claims (§6.5).
resource "google_secret_manager_secret" "firebase_admin_config" {
  secret_id = "firebase-admin-config"
  project   = var.project_id
  replication {
    auto {}
  }

  depends_on = [google_project_service.enabled]
}

# The AWS CDN deploy role ARN — not a credential, but kept out of the repo
# (§5.7).  Consumed by the GitHub Actions deploy workflow (not by Cloud Run).
resource "google_secret_manager_secret" "aws_cdn_deploy_role_arn" {
  secret_id = "aws-cdn-deploy-role-arn"
  project   = var.project_id
  replication {
    auto {}
  }

  depends_on = [google_project_service.enabled]
}

# vurarad-run@ gets secretAccessor on the Firebase Admin config only —
# per-secret, not project-wide (§5.6).
resource "google_secret_manager_secret_iam_member" "run_firebase_config" {
  secret_id = google_secret_manager_secret.firebase_admin_config.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.run.email}"
}
