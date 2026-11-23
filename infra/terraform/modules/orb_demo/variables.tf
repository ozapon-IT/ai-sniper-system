variable "project" {
  description = "リソース名のプレフィックス。"
  type        = string
  default     = "ai-sniper"
}

variable "env" {
  description = "環境名（staging / prod）。"
  type        = string
}

variable "region" {
  description = "AWS リージョン（環境側から渡す。module 内では data source 経由でも解決する）。"
  type        = string
  default     = ""
}

variable "lambda_build_dir" {
  description = <<-EOT
    Lambda デプロイパッケージのビルド済みディレクトリ。
    夜型（serverless_app）と同じ成果物を指す。単一 zip の共有は既存仕様であり、
    ORB のコードは夜型ハンドラから import されない（#299 で実測確認済み）。
  EOT
  type        = string
}

variable "s3_log_bucket_name" {
  description = "既存のログバケット名。ORB は orb-demo/ プレフィックスのみ使う。"
  type        = string
}

variable "orb_sessions_table_name" {
  description = "ORB セッション状態テーブル名（夜型 job-lock とは別）。"
  type        = string
  default     = "orb_demo_sessions"
}

variable "slack_webhook_secret_name" {
  description = "Slack webhook シークレットの名前（値ではない）。"
  type        = string
}

variable "slack_webhook_secret_arn" {
  description = <<-EOT
    Slack webhook シークレットの ARN。**ORB に許可する唯一のシークレット**。
    GMO / Anthropic のシークレットは意図的に渡さない（設計書 §3.2 封じ込め第2層）。
  EOT
  type        = string
}

variable "secrets_kms_key_arn" {
  description = <<-EOT
    Slack シークレットを暗号化している CMK の ARN。復号（`kms:Decrypt`）に必要。
    `kms:ViaService` で Secrets Manager 経由に限定して付与する。
  EOT
  type        = string
}

variable "alerts_topic_arn" {
  description = <<-EOT
    夜型モジュールの監視 SNS トピック ARN。ORB の CloudWatch アラームをここに載せる。
    夜型と同じチャネルだが、アラーム名は `ai-sniper-staging-orb-*` なので実弾と区別できる。
    スケジュールグループは夜型と分けてあるため、メトリクス次元は混ざらない。
  EOT
  type        = string
}

variable "schedule_state" {
  description = <<-EOT
    EventBridge Scheduler の状態（ENABLED / DISABLED）。
    **既定は DISABLED**。デモの稼働開始（= n≥20 約定に向けたクロックの開始）は
    明示的な判断とし、apply しただけでは走り出さないようにする。
  EOT
  type        = string
  default     = "DISABLED"

  validation {
    condition     = contains(["ENABLED", "DISABLED"], var.schedule_state)
    error_message = "schedule_state は ENABLED / DISABLED のいずれか。"
  }
}

variable "orb_demo_enabled" {
  description = "アプリ側マスタースイッチ（`ORB_DEMO_ENABLED`）。既定 false。"
  type        = bool
  default     = false
}

variable "poll_interval_sec" {
  description = "ticker ポーリング間隔（秒）。下限 1・既定 5（設計書 §3.3）。"
  type        = number
  default     = 5

  validation {
    condition     = var.poll_interval_sec >= 1
    error_message = "poll_interval_sec は 1 以上（Public REST の上限が非公開のため保守側）。"
  }
}

variable "lambda_runtime" {
  description = "Lambda ランタイム。"
  type        = string
  default     = "python3.11"
}

variable "lambda_memory_mb" {
  description = "Lambda メモリ（MB）。"
  type        = number
  default     = 512
}

variable "log_retention_days" {
  description = "CloudWatch Logs の保持日数。"
  type        = number
  default     = 30
}

variable "log_level" {
  description = "アプリのログレベル。"
  type        = string
  default     = "INFO"
}

variable "tags" {
  description = "追加タグ。"
  type        = map(string)
  default     = {}
}
