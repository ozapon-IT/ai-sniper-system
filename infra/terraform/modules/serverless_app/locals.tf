data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.name
  name       = "${var.project}-${var.env}"

  common_tags = merge({
    Project   = var.project
    Env       = var.env
    ManagedBy = "terraform"
  }, var.tags)

  # Lambda 共通 env。トレード安全フラグは既定で安全側（AGENTS.md #1）。
  # シークレットは「名前のみ」を渡し、実値は実行時に Secrets Manager から取得する
  # （平文値を env / tfstate に残さない・AGENTS.md #8）。
  common_env = {
    DYNAMODB_TABLE              = var.dynamodb_table_name
    S3_LOG_BUCKET               = var.s3_log_bucket_name
    GMO_CREDENTIALS_SECRET_NAME = var.gmo_credentials_secret_name
    ANTHROPIC_SECRET_NAME       = var.anthropic_secret_name
    TRADING_PHASE               = var.trading_phase
    ALLOW_LIVE_ORDERS           = var.allow_live_orders ? "true" : "false"
    MVP_MODE                    = var.mvp_mode ? "true" : "false"
    TRADE_SYMBOL                = var.trade_symbol
    RISK_PERCENT                = var.risk_percent
    MAX_LEVERAGE                = var.max_leverage
    MIN_RR_RATIO                = var.min_rr_ratio
    ECONOMIC_CALENDAR_SOURCE    = var.economic_calendar_source
  }
}
