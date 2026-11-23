module "app" {
  source = "../../modules/serverless_app"

  env    = "staging"
  region = var.region

  lambda_build_dir = var.lambda_build_dir

  # リソース名は caller で明示（module に default 無し）。
  # staging はコード既定（dynamodb_store.py / s3_logger.py）と一致させる。
  # prod を追加する際は必ず別名（衝突回避・環境分離）。
  dynamodb_table_name = "your-job-lock-table"
  s3_log_bucket_name  = "your-log-bucket"

  # 封じ込め two-key（既定は安全側）。
  trading_phase     = var.trading_phase
  allow_live_orders = var.allow_live_orders


  # 判断 / 手仕舞い / ウォッチドッグは別変数（#295 / #371）。for_each のキーは共有しない。
  schedule_state          = var.schedule_state
  closeout_schedule_state = var.closeout_schedule_state
  watchdog_schedule_state = var.watchdog_schedule_state
}

# ロンドンORB デモ（モック・実発注なし）。Issue #299 / 設計書 §3。
# 🔴 **夜型（module.app）とは別モジュール**。plan の差分が混ざらないようにし、
# ORB 側の記述ミスが実弾運用中のリソースへ波及するのを防ぐ。
module "orb_demo" {
  source = "../../modules/orb_demo"

  env    = "staging"
  region = var.region

  lambda_build_dir = var.lambda_build_dir

  # S3 は夜型と同じバケットを流用し、IAM で orb-demo/ プレフィックスに限定する。
  s3_log_bucket_name = module.app.s3_log_bucket_name

  # 🔴 渡すシークレットは Slack のみ。GMO / Anthropic は渡さない（封じ込め第2層）。
  slack_webhook_secret_name = module.app.slack_webhook_secret_name
  slack_webhook_secret_arn  = module.app.slack_webhook_secret_arn
  secrets_kms_key_arn       = module.app.secrets_kms_key_arn
  alerts_topic_arn          = module.app.alerts_topic_arn

  # テーブル名は caller で明示（prod 追加時は別名にして衝突回避）。
  orb_sessions_table_name = "orb_demo_sessions"

  # 既定は DISABLED / false。デモの稼働開始は phase2.auto.tfvars を編集して PR。
  schedule_state   = var.orb_schedule_state
  orb_demo_enabled = var.orb_demo_enabled
}
