# per-Lambda 実行ロール＋最小権限インラインポリシー（AGENTS.md #8 / aws-bootstrap.md §7）。
# 付与は perms フラグに応じた必要分のみ（S3 / DynamoDB / Secrets）。

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

  # DynamoDB（テーブル単位）。`dynamodb = true` の判断・手仕舞いにだけ付く。
  # DeleteItem は release_lock_outcome（#138 / #208: 未約定確実な発注失敗・閉場でのロック解放）に要る（#406）。
  # 削除できるのは条件式（`attribute_exists AND order_id = pending`）を満たすレコードだけ
  # （コード側 dynamodb_store.release_lock_outcome。IAM では制限できない）。
  # Scan / Query / BatchWriteItem / DeleteTable などは付けない（tests/infra で固定）。
  dynamic "statement" {
    for_each = each.value.dynamodb ? [1] : []
    content {
      sid       = "DynamoDB"
      effect    = "Allow"
      actions   = ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"]
      resources = [aws_dynamodb_table.app.arn]
    }
  }

  # S3 書き込み（許可プレフィックス配下のオブジェクトのみ）。
  dynamic "statement" {
    for_each = length(each.value.s3_write_prefixes) > 0 ? [1] : []
    content {
      sid       = "S3Write"
      effect    = "Allow"
      actions   = ["s3:PutObject"]
      resources = [for p in each.value.s3_write_prefixes : "${aws_s3_bucket.logs.arn}/${p}*"]
    }
  }

  # S3 読み取り（許可プレフィックス配下のオブジェクトのみ）。
  dynamic "statement" {
    for_each = length(each.value.s3_read_prefixes) > 0 ? [1] : []
    content {
      sid       = "S3ReadObject"
      effect    = "Allow"
      actions   = ["s3:GetObject"]
      resources = [for p in each.value.s3_read_prefixes : "${aws_s3_bucket.logs.arn}/${p}*"]
    }
  }

  # ListBucket は s3:prefix 条件で許可プレフィックスに限定。
  dynamic "statement" {
    for_each = length(each.value.s3_read_prefixes) > 0 ? [1] : []
    content {
      sid       = "S3List"
      effect    = "Allow"
      actions   = ["s3:ListBucket"]
      resources = [aws_s3_bucket.logs.arn]
      condition {
        test     = "StringLike"
        variable = "s3:prefix"
        values   = [for p in each.value.s3_read_prefixes : "${p}*"]
      }
    }
  }

  # Secrets Manager（この関数が触るシークレットの「完全 ARN」のみ。prefix wildcard を使わない）。
  dynamic "statement" {
    for_each = length(each.value.secret_names) > 0 ? [1] : []
    content {
      sid       = "Secrets"
      effect    = "Allow"
      actions   = ["secretsmanager:GetSecretValue"]
      resources = [for n in each.value.secret_names : local.secret_arns[n]]
    }
  }

  # 専用 CMK で暗号化したシークレットを読む関数のみ kms:Decrypt を許可。
  # Secrets Manager 経由の復号に限定（kms:ViaService 条件）。
  dynamic "statement" {
    for_each = length(setintersection(toset(each.value.secret_names), local.cmk_secret_names)) > 0 ? [1] : []
    content {
      sid       = "DecryptSecretsCMK"
      effect    = "Allow"
      actions   = ["kms:Decrypt", "kms:DescribeKey"]
      resources = [aws_kms_key.secrets.arn]
      condition {
        test     = "StringEquals"
        variable = "kms:ViaService"
        values   = ["secretsmanager.${local.region}.amazonaws.com"]
      }
    }
  }
}

resource "aws_iam_role_policy" "lambda" {
  for_each = local.functions

  name   = "${local.function_names[each.key]}-policy"
  role   = aws_iam_role.lambda[each.key].id
  policy = data.aws_iam_policy_document.lambda[each.key].json
}
