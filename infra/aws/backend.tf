# ---------------------------------------------------------------------------
# Remote state.  Shares the same vurarad-tfstate GCS bucket as the GCP module
# under a distinct prefix so one `terraform plan` pass in CI covers both
# clouds (§10.7).  The bucket name is a literal (backend blocks cannot use
# variables); override at `terraform init` with `-backend-config=` if needed.
# ---------------------------------------------------------------------------
terraform {
  backend "gcs" {
    bucket = "vurarad-tfstate"
    prefix = "infra/aws"
  }
}
