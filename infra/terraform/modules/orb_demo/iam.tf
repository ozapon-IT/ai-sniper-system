# ORB デモの実行ロール。**最小権限**（設計書 §3.2 封じ込め第2層）。
#
# 🔴 **GMO / Anthropic のシークレットへの `secretsmanager:GetSecretValue` を与えない。**
# ORB デモは Public API（`/public/v1/*`・認証不要）だけで完結するため機能に支障がなく、
# 「実発注が構造的に不可能」をコード層（#331 の import 境界）だけでなく IAM でも担保する。
# 許可するのは **Slack webhook のシークレットのみ**（アラート送信に必要）。
#
# tests/infra/test_orb_terraform.py が「GMO/Anthropic が現れないこと」を固定している。

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  for_each = local.functions

  name               = "${local.function_names[each.key]}-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = local.common_tags
}

data "aws_iam_policy_document" "lambda" {
  for_each = local.functions

  # CloudWatch Logs（自分のロググループのみ）。
  statement {
    sid       = "Logs"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.lambda[each.key].arn}:*"]
  }

  # セッション状態テーブル（ORB 専用。夜型の job-lock テーブルには触らせない）。
  statement {
    sid       = "OrbSessionsTable"
    effect    = "Allow"
    actions   = ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.orb_sessions.arn]
  }

  # S3 は既存ログバケットを流用しつつ **orb-demo/ プレフィックスに限定**する。
  # 夜型のトレードノート・実行ログ（税務証跡）を ORB から書き換えられないようにする。
  statement {
    sid       = "OrbS3Write"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["arn:aws:s3:::${var.s3_log_bucket_name}/orb-demo/*"]
  }

  statement {
    sid       = "OrbS3Read"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["arn:aws:s3:::${var.s3_log_bucket_name}/orb-demo/*"]
  }

  # 週次ジョブが verify レポートを列挙するため。プレフィックス条件で範囲を絞る。
  statement {
    sid       = "OrbS3List"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = ["arn:aws:s3:::${var.s3_log_bucket_name}"]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["orb-demo/*"]
    }
  }

  # 🔴 Slack webhook **のみ**。GMO / Anthropic は意図的に含めない。
  statement {
    sid       = "SlackWebhookSecretOnly"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [var.slack_webhook_secret_arn]
  }

  # Slack シークレットは専用 CMK で暗号化されているため復号権も要る。
  # `kms:ViaService` で Secrets Manager 経由に限定し、鍵の汎用利用は許さない。
  statement {
    sid       = "DecryptSlackSecretViaSecretsManager"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:DescribeKey"]
    resources = [var.secrets_kms_key_arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["secretsmanager.${local.region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "lambda" {
  for_each = local.functions

  name   = "${local.function_names[each.key]}-policy"
  role   = aws_iam_role.lambda[each.key].id
  policy = data.aws_iam_policy_document.lambda[each.key].json
}
