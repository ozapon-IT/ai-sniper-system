# ロンドンORB デモ（モック）のインフラ。設計書 docs/ai-dev/london-orb-demo-design.md §3。
#
# 🔴 **夜型スナイパー（serverless_app）とは別モジュール**にする（Issue #299）。
# 同一モジュールに相乗りさせると `terraform plan` の差分に**実弾運用中の夜型リソース**が
# 混ざり、ORB 側の記述ミスが波及しうる。state は共有するが、リソース定義は分離する。
#
# 🔴 **本モジュールは GMO / Anthropic のシークレットへアクセス権を与えない**
# （設計書 §3.2 封じ込め第2層）。ORB デモは Public API のみで完結するため機能に支障はなく、
# 「実発注が構造的に不可能」という保証をインフラ側でも担保する。

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.name
  name       = "${var.project}-${var.env}-orb"

  common_tags = merge({
    Project   = var.project
    Env       = var.env
    Component = "orb-demo"
    ManagedBy = "terraform"
  }, var.tags)

  # 🔴 ORB デモの env には two-key（ALLOW_LIVE_ORDERS / TRADING_PHASE）も
  # GMO / Anthropic のシークレット名も**渡さない**（設計書 §3.2 封じ込め第3層）。
  # 渡さなければコード側が誤って参照しても値が無い。
  common_env = {
    S3_LOG_BUCKET             = var.s3_log_bucket_name
    ORB_DEMO_SESSIONS_TABLE   = aws_dynamodb_table.orb_sessions.name
    ORB_DEMO_ENABLED          = var.orb_demo_enabled ? "true" : "false"
    ORB_POLL_INTERVAL_SEC     = tostring(var.poll_interval_sec)
    SLACK_WEBHOOK_SECRET_NAME = var.slack_webhook_secret_name
    LOG_LEVEL                 = var.log_level
  }

  functions = {
    session_open = {
      name_suffix                    = "session-open"
      handler                        = "backend.src.batch.orb_session_open.lambda_handler"
      description                    = "ORB デモ: レンジ確定・仮想OCO設置（London 9:01）"
      timeout                        = 120
      reserved_concurrent_executions = null
    }
    poller = {
      name_suffix = "poller"
      handler     = "backend.src.batch.orb_poller.lambda_handler"
      description = "ORB デモ: ticker ポーリング・仮想約定（London 9:05〜16:00）"
      # 1 起動 = 5 分ぶんポーリングするため 300 秒 + 余裕。
      timeout = 330
      # 🔴 予約同時実行は**使えない**。本アカウントの同時実行上限は 10 で、AWS は
      # 未予約分を 10 未満にできない（`InvalidParameterValueException:
      # ... decreases account's UnreservedConcurrentExecution below its minimum value of [10]`）。
      # 1 を予約しようとして apply が失敗した（2026-08-14 実測）。
      # 15:55 回と 16:00 EOD 回の競合は **`session_store.save_session` の退行防止ガード**で
      # 担保する（インフラの同時実行制御ではなく、書き込み側で守る）。
      reserved_concurrent_executions = null
    }
    verify = {
      name_suffix                    = "verify"
      handler                        = "backend.src.batch.orb_verify.lambda_handler"
      description                    = "ORB デモ: 日次一致検証・ノート生成（London 16:30）"
      timeout                        = 180
      reserved_concurrent_executions = null
    }
    weekly = {
      name_suffix                    = "weekly"
      handler                        = "backend.src.batch.orb_weekly.lambda_handler"
      description                    = "ORB デモ: 週次サマリ（London 金 17:00）"
      timeout                        = 180
      reserved_concurrent_executions = null
    }
  }

  function_names = { for k, v in local.functions : k => "${local.name}-${v.name_suffix}" }
}

# Lambda パッケージ。夜型と同じビルド成果物（backend/ + config/ + 依存）を使う。
# 単一 zip の共有は既存の仕様であり、ORB のコードは夜型ハンドラから import されない
# （#299 で実測確認済み）ため無害。
data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = var.lambda_build_dir
  output_path = "${path.module}/.build/${local.name}-lambda.zip"
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
  timeout       = each.value.timeout
  memory_size   = var.lambda_memory_mb

  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256

  reserved_concurrent_executions = each.value.reserved_concurrent_executions

  environment {
    variables = local.common_env
  }

  depends_on = [aws_cloudwatch_log_group.lambda]

  tags = local.common_tags
}
