# ---------------------------------------------------------------------------
# Input variables for the AWS root module (§10.7).
# ---------------------------------------------------------------------------

variable "aws_region" {
  description = "AWS region for the SPA S3 bucket + CloudFront distribution."
  type        = string
  default     = "us-east-1"
}

variable "bucket_prefix" {
  description = "Prefix for the S3 bucket name.  Defaults to the canonical vurarad-spa referenced by the acceptance criteria."
  type        = string
  default     = "vurarad-"
}

variable "github_repo" {
  description = "GitHub repository allowed to assume the CDN deploy role, in org/repo form.  Trust is pinned to its main branch."
  type        = string
  default     = "mc-cloude/vurarad"
}

# GitHub's OIDC provider thumbprint(s).  GitHub rotates its intermediate CAs,
# so list both known thumbprints; AWS also fetches the issuer CA chain.
# See: https://docs.github.com/en/actions/deployment/security-hardening-your-deployments/about-security-hardening-with-openid-connect
variable "github_oidc_thumbprints" {
  description = "Thumbprint(s) of the GitHub Actions OIDC issuer CA."
  type        = list(string)
  default     = ["6938fd4ed98ab3f473f3c6a7503b6d82b9b39e23"]
}

variable "alert_email" {
  description = "Email address that receives CloudFront free-tier usage alarms (§8.9)."
  type        = string
  default     = "ops@example.com"
}
