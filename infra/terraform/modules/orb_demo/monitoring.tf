# ORB デモの監視。夜型の SNS（#105）に載せるが、メトリクス次元は ORB 専用グループ。
# 夜型のアラームは `ScheduleGroup=default` なので、グループを分けたあと ORB 側に
# 同等の検知が無いとデモ障害が沈黙する。

resource "aws_cloudwatch_metric_alarm" "scheduler_invocation_dropped" {
  alarm_name          = "${local.name}-scheduler-invocation-dropped"
  alarm_description   = "ORB Scheduler の発火がドロップされた（ScheduleGroup=${aws_scheduler_schedule_group.orb.name}）。"
  namespace           = "AWS/Scheduler"
  metric_name         = "InvocationDroppedCount"
  dimensions          = { ScheduleGroup = aws_scheduler_schedule_group.orb.name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.alerts_topic_arn]
  tags                = local.common_tags
}

resource "aws_cloudwatch_metric_alarm" "scheduler_target_error" {
  alarm_name          = "${local.name}-scheduler-target-error"
  alarm_description   = "ORB Scheduler のターゲット invoke が失敗した（ScheduleGroup=${aws_scheduler_schedule_group.orb.name}）。"
  namespace           = "AWS/Scheduler"
  metric_name         = "TargetErrorCount"
  dimensions          = { ScheduleGroup = aws_scheduler_schedule_group.orb.name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.alerts_topic_arn]
  tags                = local.common_tags
}

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  for_each = local.function_names

  alarm_name          = "${each.value}-errors"
  alarm_description   = "${each.value} がエラー終了した（AWS/Lambda Errors）。"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = each.value }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.alerts_topic_arn]
  tags                = local.common_tags
}

resource "aws_cloudwatch_metric_alarm" "scheduler_dlq_messages" {
  alarm_name          = "${local.name}-scheduler-dlq-messages"
  alarm_description   = "ORB Scheduler DLQ に失敗ペイロードが滞留している。"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.scheduler_dlq.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.alerts_topic_arn]
  tags                = local.common_tags
}

resource "aws_cloudwatch_metric_alarm" "scheduler_dlq_delivery_failed" {
  alarm_name          = "${local.name}-scheduler-dlq-delivery-failed"
  alarm_description   = "ORB Scheduler の DLQ への配信自体が失敗した（ScheduleGroup=${aws_scheduler_schedule_group.orb.name}）。DLQ が空でもエラーが無いとは限らない（#130）。"
  namespace           = "AWS/Scheduler"
  metric_name         = "InvocationsFailedToBeSentToDeadLetterCount"
  dimensions          = { ScheduleGroup = aws_scheduler_schedule_group.orb.name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.alerts_topic_arn]
  tags                = local.common_tags
}
