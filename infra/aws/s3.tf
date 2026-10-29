# ---------------------------------------------------------------------------
# S3 bucket for the SPA (§10.7, acceptance criterion 8).
#
#   vurarad-spa: Block Public Access FULLY enabled, versioning on, SSE-S3.
#   The bucket policy grants s3:GetObject ONLY to the CloudFront distribution
#   via Origin Access Control, conditioned on AWS:SourceArn.  The bucket has
#   no policy granting `*` — the OAC principal is the only reader.
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "spa" {
  bucket = "${var.bucket_prefix}spa"

  # Tags are intentionally minimal — no PHI ever lives here (§5.6).
  tags = {
    Name        = "${var.bucket_prefix}spa"
    Application = "vurarad"
    Purpose     = "static-frontend-hosting"
  }
}

# Block Public Access — fully on (acceptance criterion 8).
resource "aws_s3_bucket_public_access_block" "spa" {
  bucket                  = aws_s3_bucket.spa.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Versioning ON — so a bad publish can be rolled back.
resource "aws_s3_bucket_versioning" "spa" {
  bucket = aws_s3_bucket.spa.id

  versioning_configuration {
    status = "Enabled"
  }
}

# SSE-S3 encryption (BAA-covered, no KMS key-custody cost).
resource "aws_s3_bucket_server_side_encryption_configuration" "spa" {
  bucket = aws_s3_bucket.spa.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Bucket policy: s3:GetObject ONLY to CloudFront via OAC, conditioned on the
# distribution ARN (§10.7).  No `*` principal, no public read.
resource "aws_s3_bucket_policy" "spa" {
  bucket = aws_s3_bucket.spa.id

  # Depends on the public access block so the policy cannot momentarily
  # grant public access before the block is applied.
  depends_on = [aws_s3_bucket_public_access_block.spa]

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowCloudFrontServicePrincipalReadOnly"
        Effect    = "Allow"
        Principal = { Service = "cloudfront.amazonaws.com" }
        Action    = "s3:GetObject"
        Resource  = "${aws_s3_bucket.spa.arn}/*"
        Condition = {
          StringEquals = {
            "AWS:SourceArn" = aws_cloudfront_distribution.spa.arn
          }
        }
      }
    ]
  })
}
