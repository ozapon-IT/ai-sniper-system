# 監視アラート（Issue #105 / §7.1）。
# CloudWatch Alarm → SNS（単一トピックに集約）→ slack_notifier Lambda → Slack webhook。
# 2026-06-30 の初回 morning 発火ドロップが「アラーム未整備」で無痕跡だった穴を塞ぐ（#104）。
#
# 検知対象（受け入れ条件）:
#   - AWS/Scheduler InvocationDroppedCount >= 1（ScheduleGroup=default）
#   - AWS/Scheduler TargetErrorCount       >= 1
#   - 各バッチ Lambda の AWS/Lambda Errors  >= 1（decision-batch / closeout-batch）
#   - Scheduler DLQ ApproximateNumberOfMessagesVisible >= 1（#104 の DLQ）

# --- SNS 暗号化用 CMK -------------------------------------------------------
# 秘匿情報用 CMK（aws_kms_key.secrets）とは責務を分離し、アラート経路専用の鍵を持つ。
# CloudWatch が暗号化済み SNS へ publish するには鍵ポリシーで cloudwatch サービスに
# GenerateDataKey/Decrypt を許可する必要がある（AWS 管理キー alias/aws/sns では不可）。
data "aws_iam_policy_document" "alerts_kms" {
  statement {
    sid       = "EnableRootAccountAdmin"
    effect    = "Allow"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account_id}:root"]
    }
  }

  statement {
    sid       = "AllowCloudWatchAlarmsEncrypt"
    effect    = "Allow"
    actions   = ["kms:GenerateDataKey*", "kms:Decrypt"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }
  }
}

resource "aws_kms_key" "alerts" {
  description             = "${local.name} 監視アラート SNS 用 CMK（#105）"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  policy                  = data.aws_iam_policy_document.alerts_kms.json
  tags                    = local.common_tags
}

resource "aws_kms_alias" "alerts" {
  name          = "alias/${local.name}-alerts"
  target_key_id = aws_kms_key.alerts.key_id
}

# --- SNS トピック（全アラームを集約）---------------------------------------
resource "aws_sns_topic" "alerts" {
  name              = "${local.name}-alerts"
  kms_master_key_id = aws_kms_key.alerts.id
  tags              = local.common_tags
}

# CloudWatch アラームからの publish のみ許可（このアカウント・本 prefix のアラームに限定）。
data "aws_iam_policy_document" "alerts_topic" {
  statement {
    sid       = "AllowCloudWatchAlarmsPublish"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]
    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:cloudwatch:${local.region}:${local.account_id}:alarm:${local.name}-*"]
    }
  }
}

resource "aws_sns_topic_policy" "alerts" {
  arn    = aws_sns_topic.alerts.arn
  policy = data.aws_iam_policy_document.alerts_topic.json
}

# --- SNS → slack_notifier Lambda 配信 --------------------------------------
resource "aws_lambda_permission" "alerts_sns_invoke" {
  statement_id  = "AllowSNSInvokeSlackNotifier"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.this["slack_notifier"].function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.alerts.arn
}

resource "aws_sns_topic_subscription" "alerts_to_slack" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.this["slack_notifier"].arn

  depends_on = [aws_lambda_permission.alerts_sns_invoke]
}

# --- CloudWatch アラーム ----------------------------------------------------

# ① Scheduler 発火ドロップ（EventAge 超過・retry 枯渇等で invoke されなかった件数）。
resource "aws_cloudwatch_metric_alarm" "scheduler_invocation_dropped" {
  alarm_name          = "${local.name}-scheduler-invocation-dropped"
  alarm_description   = "EventBridge Scheduler の発火がドロップされた（ScheduleGroup=default）。invoke 未到達=バッチ未実行。"
  namespace           = "AWS/Scheduler"
  metric_name         = "InvocationDroppedCount"
  dimensions          = { ScheduleGroup = "default" }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = local.common_tags
}

# ② Scheduler ターゲットエラー（Lambda invoke が失敗した件数）。
resource "aws_cloudwatch_metric_alarm" "scheduler_target_error" {
  alarm_name          = "${local.name}-scheduler-target-error"
  alarm_description   = "EventBridge Scheduler のターゲット invoke が失敗した（ScheduleGroup=default）。"
  namespace           = "AWS/Scheduler"
  metric_name         = "TargetErrorCount"
  dimensions          = { ScheduleGroup = "default" }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = local.common_tags
}

# ③ 各バッチ Lambda の実行エラー（ハンドラ内例外）。発注経路に直結する 2 関数のみ監視。
locals {
  # AppErrors ログメトリクスフィルタ（$.level = "error"）の対象。
  # 🔴 closeout_watchdog は**含めない**。欠測検知時に自分で logger.error を出すため、
  # 含めると「正常にアラートした」だけで AppErrors アラームも鳴り二重通知になる。
  # watchdog の異常は下の Errors アラーム（未捕捉例外）で拾う。
  monitored_lambdas = {
    decision_batch = local.function_names["decision_batch"]
    closeout_batch = local.function_names["closeout_batch"]
  }

  # Lambda Errors（未捕捉例外）アラームの対象。
  # 🔴 closeout_watchdog を含めるのは、**監視自身が落ちたときに沈黙しない**ため（#295）。
  # Slack 送信の手前で例外になると watchdog は何も通知できない。
  error_alarm_lambdas = merge(local.monitored_lambdas, {
    closeout_watchdog = local.function_names["closeout_watchdog"]
  })
}

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  for_each = local.error_alarm_lambdas

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
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = local.common_tags
}

# ④ Scheduler DLQ にメッセージが滞留（invoke 失敗ペイロードが入った=要調査・#104）。
resource "aws_cloudwatch_metric_alarm" "scheduler_dlq_messages" {
  alarm_name          = "${local.name}-scheduler-dlq-messages"
  alarm_description   = "Scheduler DLQ に失敗ペイロードが滞留している（invoke 失敗の採取・#104）。"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.scheduler_dlq.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = local.common_tags
}

# ⑤ Scheduler → DLQ への配信自体が失敗（#130）。
# DLQ が空でもこれが立つ＝エラー情報そのものが失われるサイレント最悪経路。
# 「DLQ が空」≠「エラー無し」を必ず併読する運用にするための検知。
resource "aws_cloudwatch_metric_alarm" "scheduler_dlq_delivery_failed" {
  alarm_name          = "${local.name}-scheduler-dlq-delivery-failed"
  alarm_description   = "EventBridge Scheduler の DLQ への配信自体が失敗した（ScheduleGroup=default）。DLQ が空でもエラーが無いとは限らない（#130）。errorCode は InvocationsFailedToBeSentToDeadLetterCount_<error_code> 派生メトリクスで確認（docs/runbook/README.md §5.4）。"
  namespace           = "AWS/Scheduler"
  metric_name         = "InvocationsFailedToBeSentToDeadLetterCount"
  dimensions          = { ScheduleGroup = "default" }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = local.common_tags
}

# ⑥ Scheduler → DLQ への配信成功件数の可観測化（#130）。
# 「DLQ に到達＝失敗の証跡が採れた状態」を能動的に確認できるようにする。
# 注: ⑤（配信自体の失敗）と同じ SNS トピック・同じ深刻度で アラート用チャネル に通知される。
# 専用の低優先度チャネルは持たないため、Slack 上は⑤と同様に「要確認」として扱うこと。
resource "aws_cloudwatch_metric_alarm" "scheduler_dlq_delivery_succeeded" {
  alarm_name          = "${local.name}-scheduler-dlq-delivery-succeeded"
  alarm_description   = "EventBridge Scheduler の DLQ への配信が発生した（invoke 失敗の証跡採取・#130）。DLQ 本体（scheduler_dlq_messages）と合わせて要確認。"
  namespace           = "AWS/Scheduler"
  metric_name         = "InvocationsSentToDeadLetterCount"
  dimensions          = { ScheduleGroup = "default" }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = local.common_tags
}

# --- アプリ内エラーのログメトリクスフィルタ（#143）-----------------------------
# GMO status!=0 等、Lambda は正常終了するがアプリ内でエラーを記録したケースを検知する。
# structlog は全ロガー（stdlib 経由含む）を JSON 1行・トップレベル "level" キーで出力する
# （backend/src/logging_config.py の add_log_level + foreign_pre_chain）。
# レベル基準の JSON パターンにすることで、degrade 系 warning（例: macro_context_fetch_failed
# = yfinance 未インストール時の設計どおりの縮退）を誤検知しない（#142/#143）。
# 旧: テキスト OR パターン ?"fetch_account_api_error" ?"ERR-" ?"exception" ?"failed" は
#     "failed" が広すぎて warning 縮退ログにもマッチし誤発報していた。
# 注: JSON パターンでも dimensions は使わない（#111 の教訓。metric_transformation 側で回避）。
locals {
  app_error_pattern = "{ $.level = \"error\" }"
}

resource "aws_cloudwatch_log_metric_filter" "app_errors" {
  for_each = local.monitored_lambdas

  name           = "${each.value}-app-errors"
  log_group_name = aws_cloudwatch_log_group.lambda[each.key].name
  pattern        = local.app_error_pattern

  # 関数ごとに別リソース（for_each）なので dimension での区別は不要。メトリクス名を
  # 関数別にユニーク化する。テキスト(OR)パターンは dimensions を付けられない（CloudWatch
  # Logs は JSON/スペース区切りで名前付きフィールドを抽出する場合のみ可）ため dimension は
  # 使わない。dimension を外したので default_value を併用でき、無マッチ期間も 0 を emit して
  # アラームを安定させる。
  metric_transformation {
    name          = "${each.value}-AppErrorCount"
    namespace     = "${local.name}/AppErrors"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "app_errors" {
  for_each = local.monitored_lambdas

  alarm_name          = "${each.value}-app-errors"
  alarm_description   = "${each.value} のログにアプリ内エラー（GMO status!=0 / ERR- / exception 等）を検知した。"
  namespace           = "${local.name}/AppErrors"
  metric_name         = "${each.value}-AppErrorCount"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = local.common_tags
}
