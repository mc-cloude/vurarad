# ---------------------------------------------------------------------------
# CloudFront distribution for the SPA (§10.7).
#
#   - Origin is the S3 REST endpoint (not the website endpoint — that cannot
#     be OAC-protected and would require a public bucket).
#   - Origin Access Control with SigV4 — the only principal that can read the
#     bucket.
#   - Default root object index.html.
#   - A viewer-request CloudFront Function rewrites extensionless non-/assets/
#     paths to /index.html for SPA routing; 403/404 error responses backstop it.
#   - A response-headers policy carries the security header set.
#   - Access logging is deliberately OFF — CloudFront logs would record
#     request lines containing study identifiers (§10.7).
# ---------------------------------------------------------------------------

# Origin Access Control — SigV4, always sign.
resource "aws_cloudfront_origin_access_control" "spa" {
  name                              = "vurarad-spa-oac"
  description                       = "OAC for the vurarad SPA S3 origin (§10.7)"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# Viewer-request CloudFront Function: SPA routing for extensionless paths.
resource "aws_cloudfront_function" "spa_routing" {
  name    = "vurarad-spa-routing"
  runtime = "cloudfront-js-1.0"
  comment = "Rewrite extensionless non-/assets/ paths to /index.html (§10.7)"
  publish = true
  code    = <<-EOT
    function handler(event) {
      var request = event.request;
      var uri = request.uri;
      // SPA routing: extensionless paths that are not under /assets/ serve
      // the app shell, which then renders the route client-side.
      if (uri.indexOf('/assets/') === -1 && uri.indexOf('.') === -1) {
        request.uri = '/index.html';
      }
      return request;
    }
  EOT
}

# Response-headers policy: the security header set.
resource "aws_cloudfront_response_headers_policy" "security" {
  name    = "vurarad-security-headers"
  comment = "Security headers for the vuraRAD SPA (§10.7)"

  security_headers_config {
    frame_options {
      frame_option = "DENY"
      override     = true
    }
    content_type_options {
      override = true
    }
    referrer_policy {
      referrer_policy = "strict-origin-when-cross-origin"
      override        = true
    }
    strict_transport_security {
      access_control_max_age_sec = 63072000
      include_subdomains         = true
      preload                    = true
      override                   = true
    }
    content_security_policy {
      content_security_policy = "default-src 'self'; img-src 'self' data: blob: https:; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self' https:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
      override                = true
    }
  }
}

# Cache policy for static assets.
resource "aws_cloudfront_cache_policy" "spa" {
  name        = "vurarad-spa-cache"
  default_ttl = 86400
  max_ttl     = 31536000
  min_ttl     = 0

  parameters_in_cache_key_and_forwarded_to_origin {
    cookies_config {
      cookie_behavior = "none"
    }
    headers_config {
      header_behavior = "none"
    }
    query_strings_config {
      query_string_behavior = "none"
    }
    enable_accept_encoding_brotli = true
    enable_accept_encoding_gzip   = true
  }
}

resource "aws_cloudfront_distribution" "spa" {
  enabled             = true
  is_ipv6_enabled     = true
  default_root_object = "index.html"
  comment             = "vuraRAD SPA distribution (§10.7)"
  price_class         = "PriceClass_100"

  origin {
    domain_name              = aws_s3_bucket.spa.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.spa.id
    origin_id                = "vurarad-spa-origin"
  }

  default_cache_behavior {
    target_origin_id       = "vurarad-spa-origin"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true

    cache_policy_id            = aws_cloudfront_cache_policy.spa.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security.id

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.spa_routing.arn
    }
  }

  # SPA routing backstop: 403/404 serve the app shell.
  custom_error_response {
    error_code            = 403
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 0
  }
  custom_error_response {
    error_code            = 404
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 0
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }

  # Access logging intentionally omitted (OFF) — see file header (§10.7).

  tags = {
    Application = "vurarad"
    Purpose     = "static-frontend-hosting"
  }
}
