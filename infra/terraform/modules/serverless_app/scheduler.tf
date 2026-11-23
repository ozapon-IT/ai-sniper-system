# EventBridge Scheduler（cron×2）。timezone=Asia/Tokyo を Critical 指定（§1.1）。
# 旧 aws_cloudwatch_event_rule は UTC 専用のため使用禁止（aws-bootstrap.md §6）。

# Scheduler DLQ — invoke 失敗ペイロードの採取先（#104）。
# 暗号化は SSE-SQS（AES-256）。DLQ は scheduler のエラーメタのみ保持し
# ペイロードは Input=null で機密なし → CMK 不要・AGENTS.md #8 に抵触しない（#124）。
# CMK 暗号化が DLQ 書き込み失敗の一因と疑い、変数から外して切り分ける。
resource "aws_sqs_queue" "scheduler_dlq" {
  name                      = "${local.name}-scheduler-dlq"
  message_retention_seconds = 1209600 # 14日
  sqs_managed_sse_enabled   = true    # SSE-SQS(AES-256)

  tags = local.common_tags
}

# SQS アクセスポリシー: Scheduler サービスからの SendMessage のみ許可。
data "aws_iam_policy_document" "scheduler_dlq" {
  statement {
    sid    = "AllowSchedulerDLQ"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.scheduler_dlq.arn]
    # ⚠️ aws:SourceArn 条件は付けないこと。EventBridge Scheduler の assume 検証は
    # aws:SourceArn を提示しないため、この条件があると DLQ への SendMessage が
    # 常に permanent 失敗になる（#127・trust 側と同一のアンチパターン）。
    # confused-deputy 対策は aws:SourceAccount で十分（AWS 文書の最小構成）。
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_sqs_queue_policy" "scheduler_dlq" {
  queue_url = aws_sqs_queue.scheduler_dlq.url
  policy    = data.aws_iam_policy_document.scheduler_dlq.json
}

locals {
  schedules = {
    decision_weekday = {
      target_fn   = "decision_batch"
      expression  = "cron(30 20 ? * MON-FRI *)" # 平日 PM 8:30 JST
      description = "判断バッチ 平日 20:30 JST"
      # 判断バッチは DynamoDB 当日ロックで冪等（§3.4）。一過性 5xx をリカバリするため retry=2。
      retry_attempts = 2
    }
    closeout_daily = {
      target_fn   = "closeout_batch"
      expression  = "cron(30 4 ? * TUE-SAT *)" # 火〜土 AM 4:30 JST（単一 cron）
      description = "手仕舞いバッチ 火〜土 4:30 JST（週次総括は土曜のみ Lambda 内判定）"
      # closeout_batch は DynamoDB 当日ロック（JOB_CLOSEOUT・対象取引日キー・#208）で冪等。
      # 重複配信はロックで skip。ロック取得後の途中失敗はロック保持（#138 準拠）のため
      # retry しても no-op になる → retry=0 を維持。
      # #213: ロックバックエンド障害（DDB 例外）は冪等 skip に潰さず lock_unavailable で
      # 失敗させ Lambda 内から強アラート（建玉残存の可能性を運用へ通知）。S3 は非書き込みで
      # 処理済み取引日の確定証跡を上書きしない（M-2 完全保護）。
      retry_attempts = 0
      # §7.3 DST: 4:30 JST は NY クローズ前で通年安全側（冬 EST ≈7:00 JST → 2.5h 前 / 夏 EDT ≈6:00 JST → 1.5h 前）。
      # 翌 5:00 は GMO 実測スプレッド 12.8 pips ワイド帯のため 4:30 が手仕舞い上限。
      # mode（weekday/friday）は event 未指定時 Lambda が土曜=friday・それ以外=weekday と判定。
    }
  }

  # 🔴 for_each のキーは変えない（リソースアドレスを維持する・#371）。
  # state だけキーごとに分ける。closeout を schedule_state と共有すると、
  # 建玉残存時に手仕舞いだけ戻せば判断 20:30 の実発注も再開する。
  schedule_state_by_key = {
    decision_weekday = var.schedule_state
    closeout_daily   = var.closeout_schedule_state
  }

  # 🔴 監視スケジュールは trading とは**別リソース**かつ**別変数**で定義する（#295 / #371）。
  # local.schedules と同じ for_each に入れると `state = var.schedule_state` を共有し、
  # 「両 cron が DISABLED になる」事故（#294 型）で**監視ごと沈黙する**。
  # 監視は trading cron と運命を共有してはならない。計画停止で止めるときは
  # var.watchdog_schedule_state を明示的に DISABLED にする（建玉・注文 0 のときだけ）。
  watchdog_schedules = {
    closeout_watchdog = {
      target_fn   = "closeout_watchdog"
      expression  = "cron(0 5 ? * TUE-SAT *)" # 火〜土 AM 5:00 JST（closeout の 30 分後）
      description = "手仕舞い欠測ウォッチドッグ 火〜土 5:00 JST（#295）"
    }
  }
}

# 🔴 **`state` は `var.watchdog_schedule_state`（既定 ENABLED）。`var.schedule_state` を参照しないこと。**
# これが本ウォッチドッグの設計上いちばん重要な性質（Issue #295）。
# schedule_state を参照すると、trading cron が止まる事故で監視も同時に止まり、穴が復活する。
# 計画停止（#371）は別変数を DISABLED にする。tests/infra/test_watchdog_independence.py が固定している。
resource "aws_scheduler_schedule" "watchdog" {
  for_each = local.watchdog_schedules

  name        = "${local.name}-${each.key}"
  description = each.value.description
  group_name  = "default"
  state       = var.watchdog_schedule_state

  schedule_expression          = each.value.expression
  schedule_expression_timezone = "Asia/Tokyo"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.this[each.value.target_fn].arn
    role_arn = aws_iam_role.scheduler.arn

    # 読み取りと通知のみで冪等。一過性の失敗は次回起動で回復するため retry は最小。
    retry_policy {
      maximum_retry_attempts       = 1
      maximum_event_age_in_seconds = 600
    }

    dead_letter_config {
      arn = aws_sqs_queue.scheduler_dlq.arn
    }
  }
}

# Scheduler が Lambda を invoke するためのロール。
data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
    # aws:SourceArn は「スケジュール/名前プレフィックス」形式（#127 で試した値）では
    # 常に不一致になり assume 不可（2026-07-01 実証）。EventBridge Scheduler が提示するのは
    # スケジュールグループ ARN のため、そのグループ ARN を明示する必要がある（AWS公式が
    # Important で明記: https://docs.aws.amazon.com/scheduler/latest/UserGuide/cross-service-confused-deputy-prevention.html）。
    # #129: グループ ARN 形式で復活。aws:SourceAccount 単独より狭いスコープになる（confused-deputy 対策強化）。
    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = ["arn:aws:scheduler:${local.region}:${local.account_id}:schedule-group/default"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${local.name}-scheduler-role"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
  tags               = local.common_tags
}

data "aws_iam_policy_document" "scheduler_invoke" {
  statement {
    sid     = "InvokeLambda"
    effect  = "Allow"
    actions = ["lambda:InvokeFunction"]
    # 🔴 watchdog_schedules も含めること（#295）。含め忘れると Scheduler が
    # ウォッチドッグを invoke できず、**監視が沈黙する**（#130 と同型の事故）。
    resources = [
      for k in distinct(concat(
        [for s in local.schedules : s.target_fn],
        [for s in local.watchdog_schedules : s.target_fn],
      )) :
      aws_lambda_function.this[k].arn
    ]
  }

  # Scheduler が DLQ へ失敗ペイロードを書くための権限（#104）。
  # DLQ は SSE-SQS(AES-256) のため KMS 権限は不要（#124 で EncryptDLQ を削除）。
  statement {
    sid       = "SendToDLQ"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.scheduler_dlq.arn]
  }
}

resource "aws_iam_role_policy" "scheduler_invoke" {
  name   = "${local.name}-scheduler-invoke"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler_invoke.json
}

resource "aws_scheduler_schedule" "this" {
  for_each = local.schedules

  name        = "${local.name}-${each.key}"
  description = each.value.description
  group_name  = "default"
  state       = local.schedule_state_by_key[each.key]

  schedule_expression          = each.value.expression
  schedule_expression_timezone = "Asia/Tokyo" # §1.1 Critical

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.this[each.value.target_fn].arn
    role_arn = aws_iam_role.scheduler.arn

    # closeout_daily は input なし（mode は Lambda 内で曜日判定）。decision_weekday も null。
    input = try(each.value.input, null)

    # retry_attempts はスケジュール種別ごとに設定（locals.schedules 参照）。
    # decision_weekday=2（DynamoDB 当日ロックで冪等・§3.4）、
    # closeout_daily=0（JOB_CLOSEOUT ロックで冪等・#208。ロック取得後の失敗は保持のため retry は no-op）。
    # EventAge=300s はスケジュール時刻から 5分超の古い発火を破棄し悪い価格帯での約定を防ぐ。
    retry_policy {
      maximum_retry_attempts       = each.value.retry_attempts
      maximum_event_age_in_seconds = 300
    }

    dead_letter_config {
      arn = aws_sqs_queue.scheduler_dlq.arn
    }
  }
}
