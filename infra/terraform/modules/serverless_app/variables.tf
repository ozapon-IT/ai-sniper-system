variable "project" {
  description = "リソース命名 prefix（AGENTS.md / aws-bootstrap.md §6）。"
  type        = string
  default     = "ai-sniper"
}

variable "env" {
  description = "環境識別子（staging / prod）。タグとリソース名サフィックスに使用。"
  type        = string
}

variable "region" {
  description = "AWS リージョン。ap-northeast-1 固定（aws-bootstrap.md §6）。"
  type        = string
  default     = "ap-northeast-1"
}

variable "lambda_build_dir" {
  description = <<-EOT
    ビルド済み Lambda バンドルの中身を含むディレクトリ。scripts/build_lambda.sh が生成する。
    backend/ + config/ + 依存をルート直下に含む（インポートは repo ルート絶対のため）。
    archive_file がこれを zip 化する。
  EOT
  type        = string
}

variable "lambda_runtime" {
  description = "Lambda Python ランタイム。pyproject の requires-python>=3.11 に合わせる。"
  type        = string
  default     = "python3.11"
}

variable "lambda_timeout_seconds" {
  description = "Lambda タイムアウト秒。LLM 呼び出し＋GMO POST（>=1s sleep 連続）を見込む。"
  type        = number
  default     = 120
}

variable "lambda_memory_mb" {
  description = "Lambda メモリ(MB)。"
  type        = number
  default     = 512
}

variable "schedule_state" {
  description = <<-EOT
    判断バッチ（平日 20:30 JST）の EventBridge Scheduler 状態（ENABLED / DISABLED）。
    手仕舞いは var.closeout_schedule_state、ウォッチドッグは var.watchdog_schedule_state。
    既定は安全側（DISABLED）。有効化は caller が明示的に ENABLED を渡したときのみ。
  EOT
  type        = string
  default     = "DISABLED"

  validation {
    condition     = contains(["ENABLED", "DISABLED"], var.schedule_state)
    error_message = "schedule_state は ENABLED / DISABLED のいずれか。"
  }
}

variable "closeout_schedule_state" {
  description = <<-EOT
    手仕舞いバッチ（火〜土 04:30 JST）の Scheduler 状態。判断の schedule_state とは別。
    建玉が残っているときは、判断を止めたまま手仕舞いだけ ENABLED に戻せる（#371）。
    既定は安全側（DISABLED）。判断が ENABLED のときは validation により ENABLED 必須。
  EOT
  type        = string
  default     = "DISABLED"

  validation {
    condition     = contains(["ENABLED", "DISABLED"], var.closeout_schedule_state)
    error_message = "closeout_schedule_state は ENABLED / DISABLED のいずれか。"
  }

  # 他変数の参照は Terraform 1.9 以降（このモジュールの required_version は ~> 1.15.7）。
  # https://developer.hashicorp.com/terraform/language/values/variables#custom-validation-rules
  # （参照日 2026-11-01）
  validation {
    condition = (
      var.schedule_state != "ENABLED" || var.closeout_schedule_state == "ENABLED"
    )
    error_message = "schedule_state（判断 20:30）が ENABLED のとき closeout_schedule_state も ENABLED であること。判断だけ有効だと手仕舞いが無い。"
  }
}

variable "watchdog_schedule_state" {
  description = <<-EOT
    手仕舞い欠測ウォッチドッグ（火〜土 05:00 JST）の Scheduler 状態。
    既定は ENABLED。var.schedule_state とは別変数で、trading cron を止めても
    監視は沈黙しない（#295）。DISABLED は建玉・注文が 0 の計画停止だけ
    （docs/runbook/README.md）。既定を ENABLED 以外にしないこと。
  EOT
  type        = string
  default     = "ENABLED"

  validation {
    condition     = contains(["ENABLED", "DISABLED"], var.watchdog_schedule_state)
    error_message = "watchdog_schedule_state は ENABLED / DISABLED のいずれか。"
  }

  # closeout → watchdog の向きだけ参照する（逆参照は変数の循環になる）。
  # 判断が動くときは手仕舞いも監視も必須。手仕舞いだけ戻すときも欠測監視は残す（#295）。
  validation {
    condition = (
      (var.schedule_state != "ENABLED" || var.watchdog_schedule_state == "ENABLED")
      && (var.closeout_schedule_state != "ENABLED" || var.watchdog_schedule_state == "ENABLED")
    )
    error_message = "判断または手仕舞いが ENABLED のとき watchdog_schedule_state も ENABLED であること。手仕舞いが動いて監視が止まると欠測を検知できない（#295）。"
  }
}

variable "log_retention_days" {
  description = "CloudWatch Logs 保持日数。税務証跡（§5.2）の主は S3 のためログは短め既定。"
  type        = number
  default     = 90
}

# --- リソース名（整合性メモ: コード既定 env と一致させる） ---

# 環境ドリフト/グローバル名衝突を避けるため default を持たせず caller に必須化する。
# 値は env 変数（DYNAMODB_TABLE / S3_LOG_BUCKET）経由で Lambda に渡るため、
# コード既定と一致させる責務は呼び出し側にある（dynamodb_store.py / s3_logger.py）。
variable "dynamodb_table_name" {
  description = "アプリ用 DynamoDB テーブル名（env 含み環境ごとに一意にする）。"
  type        = string
}

variable "s3_log_bucket_name" {
  description = "アプリログ/ダッシュボード用 S3 バケット名（グローバル一意・環境ごとに分ける）。"
  type        = string
}

# --- Secrets（値は TF/ tfstate に置かない。out-of-band 投入。名前のみ参照） ---

variable "gmo_credentials_secret_name" {
  description = "GMO API 資格情報シークレット名（lambda_auth.py 既定）。値は口座開設後に out-of-band 投入。"
  type        = string
  default     = "your-app/gmo-credentials"
}

variable "anthropic_secret_name" {
  description = "Anthropic API キーのシークレット名（aws-bootstrap.md §4。投入済み）。"
  type        = string
  default     = "your-app/anthropic-api-key"
}

variable "slack_webhook_secret_name" {
  description = "Slack incoming webhook URL のシークレット名（監視アラート通知・#105）。値は out-of-band 投入。"
  type        = string
  default     = "your-app/slack-webhook-url"
}

# --- アプリ挙動・トレード安全（AGENTS.md 連動。既定は安全側） ---

variable "trading_phase" {
  description = <<-EOT
    封じ込め two-key の片方（AGENTS.md #1）。既定 "1"（Phase 1 モック）で安全側。
    実発注は ALLOW_LIVE_ORDERS=true かつ TRADING_PHASE in {2, spike} の両立が必要。
  EOT
  type        = string
  default     = "1"

  validation {
    condition     = contains(["1", "2", "spike"], var.trading_phase)
    error_message = "trading_phase は \"1\" / \"2\" / \"spike\" のいずれか。"
  }
}

variable "allow_live_orders" {
  description = "封じ込め two-key の片方（AGENTS.md #1）。既定 false で安全側。"
  type        = bool
  default     = false
}

variable "mvp_mode" {
  description = "MVP_MODE（100通貨固定・§4.2）。封じ込めとは別責務。"
  type        = bool
  default     = true
}

variable "trade_symbol" {
  description = "取引通貨ペア（TRADE_SYMBOL）。"
  type        = string
  default     = "USD_JPY"
}

variable "risk_percent" {
  description = "RISK_PERCENT。"
  type        = string
  default     = "0.01"
}

variable "max_leverage" {
  description = "MAX_LEVERAGE（§4.2 V_leverage_cap 上限。仕様既定 10）。"
  type        = string
  default     = "10"
}

variable "min_rr_ratio" {
  description = "MIN_RR_RATIO（§3.4 R:R 下限。仕様既定 1.0）。"
  type        = string
  default     = "1.0"
}

variable "economic_calendar_source" {
  description = "ECONOMIC_CALENDAR_SOURCE（§3.3 / #210）。未知値はアプリ側で forexfactory にフォールバック。"
  type        = string
  default     = "forexfactory"
}

variable "decision_model" {
  description = "判断バッチの LLM モデル ID（DECISION_MODEL）。"
  type        = string
  default     = "claude-sonnet-4-6"
}

variable "night_model" {
  description = "夜ノート生成の LLM モデル ID（NIGHT_MODEL）。"
  type        = string
  default     = "claude-haiku-4-5"
}

# 注意: シークレットの「値」は Terraform 変数で受け取らない（sensitive でも tfstate に残るため・
# AGENTS.md #8）。Anthropic API キー / Slack Webhook 等は Secrets Manager に格納し、
# Lambda 実行時に名前（ANTHROPIC_SECRET_NAME 等）で取得する（runtime fetch・フォローアップ）。

variable "tags" {
  description = "全リソース共通タグ。"
  type        = map(string)
  default     = {}
}
