# Lambda ×5（判断 / 手仕舞い / Read API / Kill Switch / Slack）。整合性メモ参照。
# handler は repo ルート絶対インポート（backend.src.*）。バンドルルートに backend/ と config/。
# 各関数の IAM は「実際に触る範囲」だけを per-function で許可（最小権限・AGENTS.md #8）:
#   - secret_names      … 取得を許す Secrets Manager のシークレット名（ARN は -* で suffix 吸収）
#   - s3_write_prefixes … 書き込みを許す S3 キープレフィックス（s3_logger のキー設計に対応）
#   - s3_read_prefixes  … 読み取り＋ListBucket を許す S3 キープレフィックス
# これにより read_api は GMO/LLM 系 secret や無関係オブジェクトへ横断アクセスできない。
#
# Slack webhook（#350）: send_slack_alert に到達する関数だけに付与する。
#   decision / closeout / kill_switch は S3 失敗・lock 障害・Step3 失敗で呼ぶ。
#   read_api は読み取りのみなので付けない（最小権限）。
locals {
  functions = {
    # decision_batch の s3_read_prefixes（#402）: 同じ日の 2 回目の起動が 1 回目の判断ログを
    # 上書きしないよう、保存前に既存ログを読む。GetObject と、s3:prefix 条件付きの ListBucket
    # （存在確認の ListObjectsV2 に使う）が付く。反映には terraform apply（IAM）と Lambda の再ビルドが要る。
    decision_batch = {
      name_suffix       = "decision-batch"
      handler           = "backend.src.batch.decision_batch.lambda_handler"
      description       = "判断・IFD-OCO 注文予約バッチ（平日 PM 8:30 JST）。Phase 1 スタブ封じ込め。"
      extra_env         = { DECISION_MODEL = var.decision_model }
      dynamodb          = true
      secret_names      = [var.gmo_credentials_secret_name, var.anthropic_secret_name, var.slack_webhook_secret_name]
      s3_write_prefixes = ["logs/decision/"]
      s3_read_prefixes  = ["logs/decision/"]
    }
    closeout_batch = {
      name_suffix       = "closeout-batch"
      handler           = "backend.src.batch.closeout_batch.lambda_handler"
      description       = "手仕舞いバッチ（翌 AM 4:30 JST・火〜土単一 cron）。キャンセルファースト＋ノート生成。"
      extra_env         = { NIGHT_MODEL = var.night_model }
      dynamodb          = true
      secret_names      = [var.gmo_credentials_secret_name, var.anthropic_secret_name, var.slack_webhook_secret_name]
      s3_write_prefixes = ["logs/closeout/", "notes/", "summary/"]
      s3_read_prefixes  = ["logs/", "summary/"]
    }
    read_api = {
      name_suffix       = "read-api"
      handler           = "backend.src.read_api.handler.lambda_handler"
      description       = "ダッシュボード Read API（invoke 専用・HTTP 公開しない・§1.6.1）。"
      extra_env         = {}
      dynamodb          = false
      secret_names      = []
      s3_write_prefixes = []
      s3_read_prefixes  = ["logs/", "notes/", "summary/"]
    }
    kill_switch = {
      name_suffix       = "kill-switch"
      handler           = "backend.src.kill_switch.handler.lambda_handler"
      description       = "Kill Switch（invoke 専用・冪等・キャンセルファースト手仕舞い）。"
      extra_env         = {}
      dynamodb          = false
      secret_names      = [var.gmo_credentials_secret_name, var.slack_webhook_secret_name]
      s3_write_prefixes = ["logs/kill_switch/"]
      s3_read_prefixes  = []
    }
    slack_notifier = {
      name_suffix       = "slack-notifier"
      handler           = "backend.src.alerting.slack_notifier.lambda_handler"
      description       = "監視アラート通知（CloudWatch Alarm → SNS → Slack webhook・#105）。"
      extra_env         = {}
      dynamodb          = false
      secret_names      = [var.slack_webhook_secret_name]
      s3_write_prefixes = []
      s3_read_prefixes  = []
    }
    closeout_watchdog = {
      name_suffix = "closeout-watchdog"
      handler     = "backend.src.monitor.closeout_watchdog.lambda_handler"
      description = "手仕舞い欠測ウォッチドッグ（火〜土 5:00 JST・#295）。S3 の closeout ログ不在を検知。"
      extra_env   = {}
      dynamodb    = false
      # Slack webhook のみ。GMO 資格情報は渡さない（発注経路に触らせない）。
      secret_names      = [var.slack_webhook_secret_name]
      s3_write_prefixes = []
      s3_read_prefixes  = ["logs/"]
    }
  }

  function_names = { for k, v in local.functions : k => "${local.name}-${v.name_suffix}" }
}

resource "aws_cloudwatch_log_group" "lambda" {
  for_each = local.functions

  name              = "/aws/lambda/${local.function_names[each.key]}"
  retention_in_days = var.log_retention_days
  tags              = local.common_tags
}

resource "aws_lambda_function" "this" {
  for_each = local.functions

  function_name = local.function_names[each.key]
  description   = each.value.description
  role          = aws_iam_role.lambda[each.key].arn
  handler       = each.value.handler
  runtime       = var.lambda_runtime
  timeout       = var.lambda_timeout_seconds
  memory_size   = var.lambda_memory_mb

  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256

  environment {
    variables = merge(local.common_env, each.value.extra_env)
  }

  depends_on = [aws_cloudwatch_log_group.lambda]

  tags = local.common_tags
}
