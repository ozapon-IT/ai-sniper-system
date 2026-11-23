"""Lambda 内から直接 Slack へ強アラートを送る（Issue #206, §2 / §7.3）。

CloudWatch Alarm 経由（slack_notifier）とは別系統の即時通知。
永続化失敗アラート（s3_logger）と手仕舞い失敗アラート（closeout_batch）が共用する。

webhook は Secrets Manager 経由で取得する（AGENTS.md #8、
slack_notifier と同じ `lambda_auth.get_slack_webhook_url`）。
アラート自体の失敗で呼び出し元を巻き込まないよう例外は伝播させない。
"""

from __future__ import annotations

import json
import logging
import urllib.request

from backend.src.lambda_auth import get_slack_webhook_url

logger = logging.getLogger(__name__)

_SLACK_TIMEOUT_SECONDS = 5


def send_slack_alert(message: str) -> bool:
    """message を Slack incoming webhook へ POST する。

    Returns:
        True: 送信成功。
        False: webhook 取得失敗または POST 失敗（例外は伝播させない）。
    """
    try:
        webhook_url = get_slack_webhook_url()
    except Exception as exc:
        # webhook が取れないとアラートが黙って消える。URL を含めず痕跡だけ残す。
        logger.error("slack_alert_webhook_unavailable", extra={"error_type": type(exc).__name__})
        return False
    if not webhook_url.startswith("https://"):
        logger.error("slack_alert_invalid_webhook_scheme")
        return False
    try:
        body = json.dumps({"text": message}).encode()
        req = urllib.request.Request(
            webhook_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_SLACK_TIMEOUT_SECONDS) as resp:  # noqa: S310
            resp.read()
        return True
    except Exception as exc:
        # str(exc) には webhook URL が混入し得る（urllib の URLError 等）。
        # 秘匿のため例外型名のみを記録する（AGENTS.md #8、slack_notifier と同一扱い）。
        logger.error("slack_alert_failed", extra={"error_type": type(exc).__name__})
        return False
