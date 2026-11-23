variable "region" {
  description = "AWS リージョン（ap-northeast-1 固定）。"
  type        = string
  default     = "ap-northeast-1"

  validation {
    condition     = var.region == "ap-northeast-1"
    error_message = "staging の region は ap-northeast-1 のみ許可（backend.tf の固定値と乖離させない）。"
  }
}

variable "lambda_build_dir" {
  description = "scripts/build_lambda.sh が生成する Lambda バンドルディレクトリ。"
  type        = string
  default     = "../../build/pkg"
}

# トレード安全フラグ（既定は安全側・AGENTS.md #1）。staging では Phase 1 のまま。
variable "trading_phase" {
  type    = string
  default = "1"
}

variable "allow_live_orders" {
  type    = bool
  default = false
}

# 秘匿値（Anthropic / Slack 等）は Terraform 入力で受け取らない。
# Secrets Manager に格納し、Lambda 実行時に名前で取得する（AGENTS.md #8）。

# 判断 cron。変数の既定は安全側 DISABLED。
# 運用中の値は phase2.auto.tfvars が宣言する（省略すると止まって見える）。
variable "schedule_state" {
  type    = string
  default = "DISABLED"
}

# 手仕舞い cron。判断とは別。既定 DISABLED（新しい呼び出し元は安全側）。
# 建玉が残ったときはこの変数と watchdog だけ ENABLED に戻す（判断は戻さない）。
variable "closeout_schedule_state" {
  description = "手仕舞いバッチの Scheduler 状態。既定 DISABLED。判断の schedule_state とは別（#371）。"
  type        = string
  default     = "DISABLED"
}

# 手仕舞い欠測ウォッチドッグ。既定 ENABLED（#295）。
# schedule_state とは独立。計画停止だけ phase2.auto.tfvars で DISABLED にする。
# この既定を DISABLED にすると、tfvars の行を消した素の apply で監視が沈黙する。
variable "watchdog_schedule_state" {
  description = "手仕舞い欠測ウォッチドッグの Scheduler 状態。既定 ENABLED（#295）。計画停止のみ DISABLED。"
  type        = string
  default     = "ENABLED"
}

variable "orb_schedule_state" {
  description = "ORB デモの EventBridge Scheduler 状態。既定 DISABLED（稼働開始は明示的な判断）。"
  type        = string
  default     = "DISABLED"
}

variable "orb_demo_enabled" {
  description = "ORB デモのアプリ側マスタースイッチ。既定 false。"
  type        = bool
  default     = false
}
