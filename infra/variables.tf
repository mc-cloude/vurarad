# ---------------------------------------------------------------------------
# Input variables for the GCP root module.  Project IDs, region, and
# environment are parameterised so the same module applies to staging and
# production.  Security-relevant values have no dangerous defaults.
# ---------------------------------------------------------------------------

variable "project_id" {
  description = "GCP project ID hosting the vuraRAD platform."
  type        = string
}

variable "region" {
  description = "Primary GCP region.  D16 locks africa-south1 as the primary deployment region."
  type        = string
  default     = "africa-south1"
}

variable "backup_region" {
  description = "Region for the backup buckets.  Deliberately separate from the primary region so a single-region outage cannot take both the live data and the backup (§5.6.1).  Acceptance criterion 13 requires us-east1."
  type        = string
  default     = "us-east1"
}

variable "environment" {
  description = "Deployment environment — drives naming and is recorded in resource labels."
  type        = string
  default     = "production"

  validation {
    condition     = contains(["development", "staging", "production"], var.environment)
    error_message = "environment must be one of development, staging, production."
  }
}

variable "bucket_prefix" {
  description = "Prefix for GCS bucket names.  Defaults to the canonical vurarad- names referenced by the acceptance criteria (vurarad-audit, vurarad-spa, …)."
  type        = string
  default     = "vurarad-"
}

variable "spa_domain" {
  description = "Public CloudFront domain serving the SPA, used as the GCS CORS origin for signed-URL GETs (§10.6).  Overridden per environment."
  type        = string
  default     = "app.vurarad.com"
}

variable "github_repo" {
  description = "GitHub repository allowed to federate via Workload Identity, in org/repo form (e.g. mc-cloude/vurarad)."
  type        = string
  default     = "mc-cloude/vurarad"
}

variable "billing_account_id" {
  description = "Cloud Billing account ID — required for the budget alert resources (§8.9, §10.6)."
  type        = string
}

# Cloud Billing budgets scope by the billing-catalog service ID, not the
# human-readable name (the format is services/{id}).  The IDs are global and
# can be looked up with: `gcloud services list --enabled --format='value(name)'`
# or the Cloud Billing Catalog API.  When left empty the budget falls back to
# project-wide; set them in tfvars for true service scoping.
variable "vertex_ai_service_id" {
  description = "Cloud Billing catalog service ID for Vertex AI (e.g. services/XXXX-XXXX-XXXX).  Empty => project-wide fallback for the $15 budget."
  type        = string
  default     = ""
}

variable "storage_service_id" {
  description = "Cloud Billing catalog service ID for Cloud Storage (e.g. services/XXXX-XXXX-XXXX).  Empty => project-wide fallback for the $10 budget."
  type        = string
  default     = ""
}

variable "api_image_tag" {
  description = "Container image tag for the Cloud Run API revision.  Pinned at deploy time; :latest keeps the module stable across re-applies (§10.6)."
  type        = string
  default     = "latest"
}

variable "firebase_admin_config_version" {
  description = "Pinned Secret Manager version for firebase-admin-config.  `latest` is never referenced (§5.7); update on rotation."
  type        = string
  default     = "1"
}

variable "backup_job_image" {
  description = "Container image (full reference) for the nightly Firestore export Cloud Run Job.  Built out-of-band; the infra only wires the trigger (§5.6.1)."
  type        = string
  default     = "us-docker.pkg.dev/google.com/cloudsdktool/cloud-sdk:slim"
}

variable "verifier_image" {
  description = "Container image for the weekly audit-chain verifier job.  Defaults to the API image, which packages app/tools/verify_audit_chain.py."
  type        = string
  default     = "us-central1-docker.pkg.dev/REPLACE/vurarad/vurarad-api:latest"
}

variable "notification_email" {
  description = "Email address that receives non-paging alert notifications (budget thresholds, AUTH_LOCKOUT, AUDIT_EXPORTED, …).  AUDIT_CHAIN_BROKEN pages instead (§10.9)."
  type        = string
  default     = "ops@example.com"
}

variable "pager_endpoint" {
  description = "Endpoint for the paging notification channel (Pub/Sub or webhook) used only by AUDIT_CHAIN_BROKEN."
  type        = string
  default     = ""
}
