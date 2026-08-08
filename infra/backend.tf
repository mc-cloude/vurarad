# ---------------------------------------------------------------------------
# Remote state.  State lives in the same vurarad-tfstate bucket created by
# storage.tf under a distinct prefix; the AWS module (infra/aws) shares the
# bucket under a different prefix so `terraform plan` in CI covers both
# clouds and neither can drift unnoticed (§10.7).
#
# Backend blocks cannot reference variables, so the bucket name is a literal
# matching the storage.tf resource (`${var.bucket_prefix}tfstate` => the
# default `vurarad-tfstate`).  Override at `terraform init` time with
# `-backend-config=` if a different state bucket is used.
# ---------------------------------------------------------------------------
terraform {
  backend "gcs" {
    bucket = "vurarad-tfstate"
    prefix = "infra"
  }
}
