# ---------------------------------------------------------------------------
# CloudWatch alarms on the CloudFront + S3 free-tier boundaries (§8.9, §5.6).
#
#   1 TB egress / month and 10M requests / month are the permanent free-tier
#   limits (D2).  Alarms fire at 80% so a leak is caught before it bills.
#
# The AWS surface holds no PHI (a static bundle, no API, no request-line
# logging), so these are cost alarms, not compliance alarms.
# ---------------------------------------------------------------------------

resource "aws_sns_topic" "alerts" {
  name = "vurarad-cdn-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# 80% of 1 TB egress (1 TB = 1,099,511,627,776 bytes; 80% ≈ 879,609,302,221).
# Sum of daily BytesDownloaded over a rolling 30 days >= 0.8 TB.
resource "aws_cloudwatch_metric_alarm" "egress_80pct" {
  alarm_name          = "vurarad-cdn-egress-80pct-free-tier"
  alarm_description   = "CloudFront egress reached 80% of the 1 TB monthly free tier (§8.9)"
  namespace           = "AWS/CloudFront"
  metric_name         = "BytesDownloaded"
  statistic           = "Sum"
  period              = 86400 # 1 day
  evaluation_periods  = 30    # rolling 30-day sum
  threshold           = "879609302221"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    DistributionId = aws_cloudfront_distribution.spa.id
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

# 80% of 10M requests/month.  Sum of daily Requests over a rolling 30 days
# >= 8,000,000.
resource "aws_cloudwatch_metric_alarm" "requests_80pct" {
  alarm_name          = "vurarad-cdn-requests-80pct-free-tier"
  alarm_description   = "CloudFront requests reached 80% of the 10M monthly free tier (§8.9)"
  namespace           = "AWS/CloudFront"
  metric_name         = "Requests"
  statistic           = "Sum"
  period              = 86400
  evaluation_periods  = 30
  threshold           = "8000000"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    DistributionId = aws_cloudfront_distribution.spa.id
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}
