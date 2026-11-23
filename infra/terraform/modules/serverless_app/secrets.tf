# Secrets Manager の「箱」。値（version）は TF で作らない＝実値を tfstate に残さない（AGENTS.md #8）。
# 実値は口座開設後/ダッシュボード実装時に out-of-band（CLI/コンソール）で投入する。
#
# 既存（out-of-band 作成済み）は data で参照し、未作成のものは TF で箱を作成する。
# いずれも IAM では「完全 ARN」を参照し、prefix ワイルドカードを使わない。

# 実資金系の認証情報は専用 CMK で暗号化し、鍵レベルで他 secret と分離する
# （アカウント共通の AWS 管理キーを使わない）。
resource "aws_kms_key" "secrets" {
  description             = "${local.name} Secrets Manager 用 CMK（GMO 資格情報 / Slack webhook）"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  tags                    = local.common_tags
}

resource "aws_kms_alias" "secrets" {
  name          = "alias/${local.name}-secrets"
  target_key_id = aws_kms_key.secrets.key_id
}

# 値（version）の自動 rotation は意図的に設定しない（aws_secretsmanager_secret_rotation 不使用）。
# 理由:
#   - gmo-credentials … GMO コインの FX API キー/シークレットは外部ブローカー発行で、
#     AWS rotation Lambda から無人で再発行できる API がない。誤った自動回転は発注経路の
#     認証断＝実資金事故に直結するため、運用者が管理画面で再発行→Secrets Manager へ
#     out-of-band 投入する「手動 rotation」を採用する（定期見直しは運用手順で担保）。
# KMS キー側は enable_key_rotation=true で年次自動回転する（鍵と値で回転責務が異なる）。

# Anthropic API キー: aws-bootstrap.md §4 で作成済み → data で ARN 解決。
data "aws_secretsmanager_secret" "anthropic" {
  name = var.anthropic_secret_name
}

# GMO 資格情報: 口座開設後に値を投入。箱のみ TF 管理。専用 CMK で暗号化。
resource "aws_secretsmanager_secret" "gmo_credentials" {
  name        = var.gmo_credentials_secret_name
  description = "GMO API 資格情報（api_key/api_secret）。値は out-of-band 投入。TF は箱のみ。"
  kms_key_id  = aws_kms_key.secrets.arn
  tags        = local.common_tags
}

# Slack incoming webhook URL（監視アラート通知用・#105）: 値は out-of-band 投入。
# 箱のみ TF 管理。webhook URL は秘匿情報のため env / tfstate に平文を残さない（AGENTS.md #8）。専用 CMK で暗号化。
resource "aws_secretsmanager_secret" "slack_webhook" {
  name        = var.slack_webhook_secret_name
  description = "Slack incoming webhook URL（監視アラート通知）。値は out-of-band 投入。TF は箱のみ。"
  kms_key_id  = aws_kms_key.secrets.arn
  tags        = local.common_tags
}

locals {
  # シークレット名 → 完全 ARN。IAM の per-function 許可で参照する。
  secret_arns = {
    (var.anthropic_secret_name)       = data.aws_secretsmanager_secret.anthropic.arn
    (var.gmo_credentials_secret_name) = aws_secretsmanager_secret.gmo_credentials.arn
    (var.slack_webhook_secret_name)   = aws_secretsmanager_secret.slack_webhook.arn
  }

  # 専用 CMK で暗号化したシークレット名（これらを読む関数は kms:Decrypt が必要）。
  cmk_secret_names = toset([
    var.gmo_credentials_secret_name,
    var.slack_webhook_secret_name,
  ])
}
