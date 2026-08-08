# ---------------------------------------------------------------------------
# Terraform + provider version pins for the AWS root module (§10.7).
#
# A second, small root module for static frontend hosting: S3 + CloudFront +
# OAC + GitHub OIDC.  Two clouds, one state backend (shared vurarad-tfstate
# bucket under a distinct prefix), so `terraform plan` in CI covers both and
# neither can drift unnoticed.
# ---------------------------------------------------------------------------
terraform {
  required_version = ">= 1.7.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}
