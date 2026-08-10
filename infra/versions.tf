# ---------------------------------------------------------------------------
# Terraform + provider version pins for the GCP root module (§10.6).
#
# The GCP module owns the entire vuraRAD control plane: Firestore, GCS, the
# immutable audit sink, IAM, Cloud Run, budgets, and the scheduled backup /
# chain-verification jobs.  A single `google` provider is used throughout so
# `terraform plan` in CI can detect drift on every HIPAA- and budget-relevant
# setting in one pass (§10.6).
# ---------------------------------------------------------------------------
terraform {
  required_version = ">= 1.7.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}
