"""DynamoDB 冪等ロック獲得失敗の共通分岐（#210 / #213）。

decision_batch と closeout_batch で「既存ロックは冪等 skip / DDB 障害時のみ Slack 通知」を
一元化し、片方だけ挙動がずれる事故を防ぐ。
"""

from __future__ import annotations

import logging
from datetime import date
from enum import StrEnum
from typing import Any

from backend.src.alerting.slack_alert import send_slack_alert
from backend.src.persistence.dynamodb_store import LockAcquireOutcome

logger = logging.getLogger(__name__)


class LockFailureKind(StrEnum):
    LOCK_EXISTS = "lock_exists"
    UNAVAILABLE = "unavailable"


def classify_lock_failure(outcome: LockAcquireOutcome) -> LockFailureKind | None:
    """acquired=True なら None。失敗時は lock_exists と unavailable を区別する。"""
    if outcome.acquired:
        return None
    if outcome.lock_exists:
        return LockFailureKind.LOCK_EXISTS
    return LockFailureKind.UNAVAILABLE


def notify_lock_unavailable(
    *,
    log_event: str,
    log_extra: dict[str, Any],
    slack_message: str,
) -> None:
    """DDB バックエンド障害時の監査ログと Slack 強アラート（両バッチ共通）。"""
    logger.error(log_event, extra=log_extra)
    send_slack_alert(slack_message)


def notify_lock_release_failed(*, job: str, day: date, error_event: str) -> None:
    """ロック解放の**障害**（条件不成立ではない）の監査ログと Slack 通知（#406）。

    解放に失敗するとロックが残り、同日（手仕舞いは同朝）の再試行ができない。二重発注・二重決済には
    ならない側だが、運用者が「その日はやり直せない」ことに気づけるように通知する。
    本文に秘密・ログ本文は入れない。
    """
    logger.error(
        error_event,
        extra={"job": job, "date": day.isoformat()},
    )
    send_slack_alert(
        f"[AI Sniper][ALERT] ロックの解放に失敗しました（job={job} date={day.isoformat()}）\n"
        f"DynamoDB の権限（dynamodb:DeleteItem）または障害の可能性があります。ロックが残るため、"
        f"この日の再試行はできません（二重発注・二重決済にはなりません）。"
        f"CloudWatch Logs の dynamodb_lock_release_failed を確認してください（Runbook §5.2）。"
    )
