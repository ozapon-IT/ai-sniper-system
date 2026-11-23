# EventBridge Scheduler。**timezone は Europe/London**（AGENTS.md 規約6 の明示的な例外）。
#
# 🔴 ORB の判定基準そのものが Europe/London であり、JST 固定 cron で近似することは禁止
# （英 DST と米 DST は切替週が異なる）。`ScheduleExpressionTimezone` に IANA 名を指定でき、
# DST は AWS 側が自動調整する（公式裏取り済み・設計書 §5.2）。
# 出典: https://docs.aws.amazon.com/scheduler/latest/UserGuide/schedule-types.html（2026-08-08 参照）
#
# 🔴 スケジュールグループは夜型の `default` と**分けないといけない**。
# 夜型の CloudWatch アラーム（InvocationDroppedCount / TargetErrorCount /
# InvocationsFailedToBeSentToDeadLetterCount）は `ScheduleGroup=default` 次元なので、
# ORB を default に入れるとデモの失敗が実弾アラートを鳴らす。
# 出典: https://docs.aws.amazon.com/scheduler/latest/UserGuide/managing-schedule-group.html（2026-08-14 参照）

resource "aws_scheduler_schedule_group" "orb" {
  name = local.name
  tags = local.common_tags
}

resource "aws_sqs_queue" "scheduler_dlq" {
  name                      = "${local.name}-scheduler-dlq"
  message_retention_seconds = 1209600 # 14日
  sqs_managed_sse_enabled   = true    # SSE-SQS(AES-256)

  tags = local.common_tags
}

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
    # ⚠️ aws:SourceArn 条件は付けない（EventBridge Scheduler は DLQ 送信時に提示しないため、
    # 付けると常に permanent 失敗になる・夜型 #127 と同じアンチパターン）。
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
  # すべて Europe/London。設計書 §3 の構成図と一致させる。
  # session_open は 9:01（9:00 だと 8:59 足の未確定値を掴む・ORB-1b / §6.2）。
  # poller は 9:02 / 9:00〜15:55 / 16:00 を分ける。EOD は「16:00 以降最初の観測」（§6.4）。
  # poller_eod の input は run_seconds=30。15:55 回（300秒）との重なりは
  # `session_store` の終端 sticky + entry 先着確定で守る（予約同時実行は使えない・#299 / #304）。
  schedules = {
    session_open = {
      target_fn      = "session_open"
      expression     = "cron(1 9 ? * MON-FRI *)"
      description    = "ORB: レンジ確定・仮想OCO設置（London 9:01）"
      retry_attempts = 1
      input          = null
    }
    # 🔴 **9:02 の 1 本**（#307）。エントリー窓は 9:00 に開くが session_open が 9:01・
    # intraday の初回が 9:05 なので、**`ARMED` なのに誰も見ていない 9:01→9:05** が空く。
    # ORB はレンジブレイク直後にトリガーが走りやすく、ここが盲点だと §6.3 の推定滑りが
    # 系統的に悪化側へ偏る（初日 2026-08-14 は verify の `fill_time_in_replay_bar=false` で顕在化）。
    # 9:00:00〜9:01 は閉じない: 実弾でも `ifdOrder` はレンジ確定後に乗るため条件が一致する。
    # 9:02 が session_open に先行したら `no_session` で即 return し 9:05 の回が拾う。
    poller_open = {
      target_fn      = "poller"
      expression     = "cron(2 9 ? * MON-FRI *)"
      description    = "ORB: ticker ポーリング（London 9:02・9:05 までの穴埋め・§3）"
      retry_attempts = 0
      input          = null
    }
    # 🔴 **`*/5` であって `5/5` ではない**（#306）。AWS の `/` は「開始値からの増分」なので
    # `5/5` は 5,10,…,55 で **:00 に折り返さない**。9:00 の回を避ける意図で `5/5` と書いた結果、
    # **毎時 :00 まで落ちて 1 日 6 回 × 約 5 分の観測盲点**になっていた（2026-08-14 実測 78 回）。
    # <https://docs.aws.amazon.com/scheduler/latest/UserGuide/schedule-types.html>（2026-08-15 参照）
    # 9:00 の回は session_open 前なので `no_session` で即 return する無害な空振り。
    poller_intraday = {
      target_fn      = "poller"
      expression     = "cron(*/5 9-15 ? * MON-FRI *)"
      description    = "ORB: ticker ポーリング（London 9:00〜15:55・5分毎。9:00 は空振り）"
      retry_attempts = 0
      input          = null
    }
    poller_eod = {
      target_fn      = "poller"
      expression     = "cron(0 16 ? * MON-FRI *)"
      description    = "ORB: EOD 観測（London 16:00・§6.4）"
      retry_attempts = 2
      input          = jsonencode({ run_seconds = 30 })
    }
    verify = {
      target_fn      = "verify"
      expression     = "cron(30 16 ? * MON-FRI *)"
      description    = "ORB: 日次一致検証・ノート生成（London 16:30）"
      retry_attempts = 2
      input          = null
    }
    weekly = {
      target_fn      = "weekly"
      expression     = "cron(0 17 ? * FRI *)"
      description    = "ORB: 週次サマリ（London 金 17:00）"
      retry_attempts = 1
      input          = null
    }
  }
}

resource "aws_scheduler_schedule" "orb" {
  for_each = local.schedules

  name        = "${local.name}-${each.key}"
  description = each.value.description
  group_name  = aws_scheduler_schedule_group.orb.name
  # 既定は DISABLED。デモの稼働開始は明示的な判断（`ORB_DEMO_ENABLED` と整合）。
  state = var.schedule_state

  schedule_expression = each.value.expression
  # 🔴 ここが AGENTS.md 規約6 の例外。JST に読み替えないこと。
  schedule_expression_timezone = "Europe/London"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.this[each.value.target_fn].arn
    role_arn = aws_iam_role.scheduler.arn
    input    = each.value.input

    retry_policy {
      maximum_retry_attempts       = each.value.retry_attempts
      maximum_event_age_in_seconds = 600
    }

    dead_letter_config {
      arn = aws_sqs_queue.scheduler_dlq.arn
    }
  }
}

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
    # 🔴 `aws:SourceArn` は **schedule-group ARN** を指定する。
    # スケジュール ARN や名前プレフィックス形式にすると assume 不可となり、
    # **全 cron が permanent 失敗**する（夜型 #129/#130 で実際に起きた）。
    # 公式が Important で明記:
    # https://docs.aws.amazon.com/scheduler/latest/UserGuide/cross-service-confused-deputy-prevention.html
    # ORB 専用グループに閉じ、夜型 default グループからの assume を許さない。
    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = [aws_scheduler_schedule_group.orb.arn]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${local.name}-scheduler-role"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
  tags               = local.common_tags
}

data "aws_iam_policy_document" "scheduler" {
  statement {
    sid       = "InvokeOrbLambdas"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [for fn in aws_lambda_function.this : fn.arn]
  }

  # Scheduler が DLQ へ失敗ペイロードを書くための権限（夜型 #104）。
  # これが無いと invoke 失敗が DLQ に届かず、証跡が消える。
  statement {
    sid       = "SendToDLQ"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.scheduler_dlq.arn]
  }
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "${local.name}-scheduler-policy"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}
