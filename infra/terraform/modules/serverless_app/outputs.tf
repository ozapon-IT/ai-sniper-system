output "lambda_function_names" {
  description = "作成した Lambda 関数名（キー: 論理名）。"
  value       = { for k, fn in aws_lambda_function.this : k => fn.function_name }
}

output "lambda_function_arns" {
  description = "作成した Lambda 関数 ARN。"
  value       = { for k, fn in aws_lambda_function.this : k => fn.arn }
}

output "dynamodb_table_name" {
  description = "アプリ用 DynamoDB テーブル名。"
  value       = aws_dynamodb_table.app.name
}

output "s3_log_bucket_name" {
  description = "アプリログ用 S3 バケット名。"
  value       = aws_s3_bucket.logs.bucket
}

output "scheduler_dlq_arn" {
  description = "Scheduler 失敗ペイロード採取用 DLQ の ARN（#104）。"
  value       = aws_sqs_queue.scheduler_dlq.arn
}

output "scheduler_dlq_url" {
  description = "Scheduler 失敗ペイロード採取用 DLQ の URL。"
  value       = aws_sqs_queue.scheduler_dlq.url
}

output "alerts_topic_arn" {
  description = "監視アラート集約 SNS トピックの ARN（#105）。"
  value       = aws_sns_topic.alerts.arn
}

output "alarm_names" {
  description = "作成した CloudWatch アラーム名の一覧（#105）。"
  value = concat(
    [
      aws_cloudwatch_metric_alarm.scheduler_invocation_dropped.alarm_name,
      aws_cloudwatch_metric_alarm.scheduler_target_error.alarm_name,
      aws_cloudwatch_metric_alarm.scheduler_dlq_messages.alarm_name,
    ],
    [for a in aws_cloudwatch_metric_alarm.lambda_errors : a.alarm_name],
    [for a in aws_cloudwatch_metric_alarm.app_errors : a.alarm_name],
  )
}

output "schedule_names" {
  description = "EventBridge Scheduler のスケジュール名と式。"
  value = {
    for k, s in aws_scheduler_schedule.this : k => {
      name       = s.name
      expression = s.schedule_expression
      timezone   = s.schedule_expression_timezone
    }
  }
}

# ORB デモモジュール（modules/orb_demo）へ渡すための参照。
# 🔴 渡すのは **Slack のみ**。GMO / Anthropic の ARN は出力しない（設計書 §3.2 封じ込め第2層）。
output "slack_webhook_secret_arn" {
  description = "Slack webhook シークレットの ARN（ORB デモに許可する唯一のシークレット）。"
  value       = aws_secretsmanager_secret.slack_webhook.arn
}

output "slack_webhook_secret_name" {
  description = "Slack webhook シークレット名。"
  value       = var.slack_webhook_secret_name
}

output "secrets_kms_key_arn" {
  description = "シークレット暗号化 CMK の ARN（Slack 復号に必要）。"
  value       = aws_kms_key.secrets.arn
}
