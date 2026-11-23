"""tests/alerting/test_slack_alert.py — Lambda 内直接 Slack 通知のテスト（Issue #206）。

`s3_logger._slack_alert` から移設した実体（send_slack_alert）のテスト。
Secrets Manager 経由の webhook 取得・URL 秘匿（Issue #140, AGENTS.md #8/#9）を含む。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from unittest.mock import MagicMock

import pytest

from backend.src.alerting import slack_alert

# 秘匿 webhook URL のダミー。ログへの漏洩検査に使う（AGENTS.md #8）。
WEBHOOK = "https://hooks.slack.com/services/T000/B000/SECRETTOKEN"


def test_send_slack_alert_uses_secrets_manager_webhook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """webhook は平文 env ではなく get_slack_webhook_url（Secrets Manager）から取得する。"""
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(slack_alert, "get_slack_webhook_url", lambda: WEBHOOK)

    posted: list[urllib.request.Request] = []

    def _fake_urlopen(req: urllib.request.Request, timeout: float | None = None) -> MagicMock:
        posted.append(req)
        return MagicMock()

    monkeypatch.setattr(slack_alert.urllib.request, "urlopen", _fake_urlopen)

    assert slack_alert.send_slack_alert("test message") is True

    assert len(posted) == 1
    assert posted[0].full_url == WEBHOOK
    assert json.loads(posted[0].data.decode()) == {"text": "test message"}


def test_send_slack_alert_webhook_unavailable_returns_false(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """webhook 取得失敗時は URL を含まないローカルログを残し、例外を伝播せず False を返す。"""

    def _fetch_fail() -> str:
        raise RuntimeError(f"secret fetch failed: {WEBHOOK}")

    monkeypatch.setattr(slack_alert, "get_slack_webhook_url", _fetch_fail)

    with caplog.at_level(logging.ERROR):
        assert slack_alert.send_slack_alert("msg") is False

    records = [r for r in caplog.records if r.getMessage() == "slack_alert_webhook_unavailable"]
    assert len(records) == 1
    assert records[0].error_type == "RuntimeError"
    assert WEBHOOK not in repr(records[0].__dict__)


def test_send_slack_alert_invalid_scheme_returns_false(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """非 https スキームの webhook は POST 前に拒否する（防御的検証）。"""
    monkeypatch.setattr(slack_alert, "get_slack_webhook_url", lambda: "http://hooks.slack.com/bad")

    with caplog.at_level(logging.ERROR):
        assert slack_alert.send_slack_alert("msg") is False

    records = [r for r in caplog.records if r.getMessage() == "slack_alert_invalid_webhook_scheme"]
    assert len(records) == 1


def test_send_slack_alert_post_failure_logs_type_name_only(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """送信失敗ログには webhook URL を出さず例外型名のみ記録する（AGENTS.md #8）。"""
    monkeypatch.setattr(slack_alert, "get_slack_webhook_url", lambda: WEBHOOK)

    def _raise(req: urllib.request.Request, timeout: float | None = None) -> None:
        # urllib の実挙動を模し、例外メッセージに webhook URL を混入させる
        raise urllib.error.URLError(f"POST failed: {WEBHOOK}")

    monkeypatch.setattr(slack_alert.urllib.request, "urlopen", _raise)

    with caplog.at_level(logging.ERROR):
        assert slack_alert.send_slack_alert("msg") is False  # 例外を伝播しない

    records = [r for r in caplog.records if r.getMessage() == "slack_alert_failed"]
    assert len(records) == 1
    assert records[0].error_type == "URLError"
    assert not hasattr(records[0], "error")  # str(exc) を記録しない
    assert WEBHOOK not in repr(records[0].__dict__)
