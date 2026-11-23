"""監視アラート通知 Lambda（Issue #105 / §7.1）。

CloudWatch Alarm → SNS → 本ハンドラ → Slack incoming webhook。

SNS の各レコードに含まれる CloudWatch Alarm の JSON ペイロードを人間可読な
テキストへ整形し、`s3_logger._slack_alert` と同じ urllib POST パターンで
Slack webhook へ送信する。webhook URL は秘匿情報のため env / tfstate に
平文を残さず Secrets Manager から取得する（AGENTS.md #8、`lambda_auth.get_slack_webhook_url`）。

通知到達自体が監視の生命線のため、1 レコードの整形・送信失敗が他レコードを
巻き込まないよう個別に握りつぶしてログ記録する。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Any

from backend.src.lambda_auth import get_slack_webhook_url, reset_token_cache
from backend.src.logging_config import configure_logging

configure_logging()
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_SLACK_TIMEOUT_SECONDS = 5


def _format_alarm_message(sns_message: str, subject: str | None) -> str:
    """SNS メッセージ（CloudWatch Alarm JSON）を Slack 用テキストへ整形する。

    JSON として解釈できない場合は生メッセージをそのまま添える（汎用 SNS にも耐える）。
    """
    try:
        payload = json.loads(sns_message)
    except (json.JSONDecodeError, ValueError):
        payload = None

    if not isinstance(payload, dict):
        head = subject or "AWS 通知"
        return f"[AI Sniper][ALERT] {head}\n{sns_message}"

    alarm_name = payload.get("AlarmName", "(不明なアラーム)")
    state = payload.get("NewStateValue", "?")
    reason = payload.get("NewStateReason", "")
    description = payload.get("AlarmDescription") or ""
    region = payload.get("Region", "")
    raw_trigger = payload.get("Trigger")
    trigger: dict[str, Any] = raw_trigger if isinstance(raw_trigger, dict) else {}
    namespace = trigger.get("Namespace", "")
    metric = trigger.get("MetricName", "")

    lines = [f"[AI Sniper][ALERT] {alarm_name} → {state}"]
    if description:
        lines.append(description)
    if namespace or metric:
        lines.append(f"metric: {namespace}/{metric}".rstrip("/"))
    if region:
        lines.append(f"region: {region}")
    if reason:
        lines.append(f"reason: {reason}")
    return "\n".join(lines)


def _post_to_slack(webhook_url: str, text: str) -> None:
    """Slack incoming webhook へ JSON を POST する（s3_logger._slack_alert と同一パターン）。"""
    body = json.dumps({"text": text}).encode()
    req = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=_SLACK_TIMEOUT_SECONDS)  # noqa: S310 (固定 https webhook)


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:  # noqa: ARG001
    """SNS イベントを受け取り、各 CloudWatch Alarm を Slack へ通知する。

    配信に 1 件でも失敗した場合は最後に例外を送出して **失敗終了** する。これにより
    SNS → Lambda の非同期再配信（リトライ）が走り、通知の永久欠落を防ぐ。
    SNS → Lambda は all-or-nothing 再送のため、再送時に成功済みレコードが重複送信され
    得るが、監視通知は「欠落より重複の方が安全」のためこのトレードオフを許容する。

    Returns:
        全件成功時のみ {"delivered": 件数, "failed": 0} を返す（運用ログ・テスト用）。

    Raises:
        RuntimeError: webhook 取得失敗、または 1 件以上の配信失敗があったとき。
    """
    reset_token_cache()  # webhook ローテーションに追従（ウォームコンテナ対策・§7.2）

    records = event.get("Records") if isinstance(event, dict) else None
    if not isinstance(records, list) or not records:
        logger.warning("slack_notifier_no_records")
        return {"delivered": 0, "failed": 0}

    try:
        webhook_url = get_slack_webhook_url()
    except Exception as exc:
        # webhook が取れないと全通知が落ちる。痕跡を残しつつ失敗終了で再配信させる。
        logger.error("slack_webhook_fetch_failed", extra={"error": str(exc)})
        raise RuntimeError("slack webhook の取得に失敗しました") from exc

    delivered = 0
    failed = 0
    # Slack webhook のレート上限（約 1 req/s）対策: 連続 POST 間に >=1s を確保する。
    last_post_started_at: float | None = None
    for record in records:
        sns = record.get("Sns") if isinstance(record, dict) else None
        if not isinstance(sns, dict):
            failed += 1
            logger.warning("slack_notifier_bad_record")
            continue
        message = sns.get("Message", "")
        subject = sns.get("Subject")
        text = _format_alarm_message(message if isinstance(message, str) else str(message), subject)
        try:
            if last_post_started_at is not None:
                elapsed = time.monotonic() - last_post_started_at
                if elapsed < 1.0:
                    time.sleep(1.0 - elapsed)
            last_post_started_at = time.monotonic()
            _post_to_slack(webhook_url, text)
            delivered += 1
            logger.info("slack_alert_delivered", extra={"subject": subject})
        except Exception as exc:
            failed += 1
            # str(exc) には webhook URL が混入し得る（urllib の URLError 等）。
            # 秘匿のため例外型名のみを記録する（AGENTS.md #8）。
            logger.error("slack_alert_post_failed", extra={"error_type": type(exc).__name__})

    if failed:
        # 部分失敗でも失敗終了させ、SNS → Lambda の再配信に委ねる。
        raise RuntimeError(f"slack 通知に失敗しました（delivered={delivered} failed={failed}）")

    return {"delivered": delivered, "failed": failed}
