# ---------------------------------------------------------------------------
# GitHub OIDC for the CDN deploy role (§10.7, §5.6).
#
#   vurarad-cdn-deploy: s3:PutObject / s3:DeleteObject on vurarad-spa/* and
#   cloudfront:CreateInvalidation on the one distribution ARN.  NO s3:GetObject,
#   no IAM, no other bucket — CI cannot read the bundle back (acceptance
#   criterion 6).  Trust policy pinned to repo:mc-cloude/vurarad:ref:refs/heads/main.
#
# No AWS access keys exist — GitHub Actions assumes the role via OIDC (§5.6).
# ---------------------------------------------------------------------------

resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = var.github_oidc_thumbprints

  tags = {
    Application = "vurarad"
    Purpose     = "github-actions-oidc"
  }
}

# Trust policy — pinned to the repo's main branch.
data "aws_iam_policy_document" "cdn_deploy_assume_role" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repo}:ref:refs/heads/main"]
    }
  }
}

resource "aws_iam_role" "cdn_deploy" {
  name               = "vurarad-cdn-deploy"
  assume_role_policy = data.aws_iam_policy_document.cdn_deploy_assume_role.json
  description        = "GitHub Actions SPA publish role — no s3:GetObject (§5.6, §10.7)"
}

# Permissions: publish + invalidate only.  NO s3:GetObject (acceptance criterion 6).
data "aws_iam_policy_document" "cdn_deploy_permissions" {
  statement {
    sid    = "PutDeleteSpaObjects"
    effect = "Allow"
    actions = [
      "s3:PutObject",
      "s3:DeleteObject",
    ]
    resources = ["${aws_s3_bucket.spa.arn}/*"]
  }

  statement {
    sid       = "CreateInvalidation"
    effect    = "Allow"
    actions   = ["cloudfront:CreateInvalidation"]
    resources = [aws_cloudfront_distribution.spa.arn]
  }
}

resource "aws_iam_role_policy" "cdn_deploy" {
  name   = "vurarad-cdn-deploy-permissions"
  role   = aws_iam_role.cdn_deploy.id
  policy = data.aws_iam_policy_document.cdn_deploy_permissions.json
}
