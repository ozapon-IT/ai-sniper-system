"""slack_notifier Lambda のユニットテスト（Issue #105 / §7.1）。

CloudWatch Alarm → SNS → Slack webhook の整形・配信ロジックを検証する。
webhook URL は lambda_auth.get_slack_webhook_url 経由で取得する。
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import backend.src.alerting.slack_notifier as notifier
import backend.src.lambda_auth as _auth


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch: pytest.MonkeyPatch) -> Any:
    """各テスト前後にキャッシュをリセットし、env を明示除去して環境依存を防ぐ。"""
    monkeypatch.delenv("ALLOW_LOCAL_SLACK_WEBHOOK", raising=False)
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("SLACK_WEBHOOK_SECRET_NAME", raising=False)
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    # レート制御の実 sleep をスタブしてテストを高速化する。
    monkeypatch.setattr(notifier.time, "sleep", lambda *_a, **_k: None)
    _auth.reset_token_cache()
    yield
    _auth.reset_token_cache()


def _alarm_message(
    name: str = "ai-sniper-staging-scheduler-invocation-dropped",
    state: str = "ALARM",
) -> str:
    """CloudWatch Alarm が SNS へ送る Message（JSON 文字列）を模す。"""
    return json.dumps(
        {
            "AlarmName": name,
            "AlarmDescription": "EventBridge Scheduler の発火がドロップされた。",
            "NewStateValue": state,
            "NewStateReason": "Threshold Crossed: 1 datapoint [1.0] >= 1.0",
            "Region": "Asia Pacific (Tokyo)",
            "Trigger": {"Namespace": "AWS/Scheduler", "MetricName": "InvocationDroppedCount"},
        }
    )


def _sns_event(*messages: str) -> dict[str, Any]:
    return {"Records": [{"Sns": {"Message": m, "Subject": "ALARM: scheduler"}} for m in messages]}


class TestFormatAlarmMessage:
    def test_formats_cloudwatch_alarm_fields(self) -> None:
        text = notifier._format_alarm_message(_alarm_message(), "ALARM: scheduler")
        assert "ai-sniper-staging-scheduler-invocation-dropped" in text
        assert "ALARM" in text
        assert "AWS/Scheduler/InvocationDroppedCount" in text
        assert "[AI Sniper][ALERT]" in text

    def test_falls_back_to_raw_for_non_json(self) -> None:
        text = notifier._format_alarm_message("plain text alert", "subj")
        assert "plain text alert" in text
        assert "[AI Sniper][ALERT]" in text

    def test_handles_non_dict_json(self) -> None:
        # JSON だが dict でない（配列）場合も生メッセージ扱いで落ちない。
        text = notifier._format_alarm_message("[1, 2, 3]", None)
        assert "[1, 2, 3]" in text


class TestLambdaHandler:
    def test_posts_each_record_to_slack(self) -> None:
        msg_a = _alarm_message(name="ai-sniper-staging-scheduler-target-error")
        msg_b = _alarm_message(name="ai-sniper-staging-scheduler-dlq-messages")
        with (
            patch.object(
                notifier, "get_slack_webhook_url", return_value="https://hooks.slack.test/x"
            ),
            patch.object(notifier, "_post_to_slack") as mock_post,
        ):
            result = notifier.lambda_handler(_sns_event(msg_a, msg_b))

        assert result == {"delivered": 2, "failed": 0}
        # 各レコードが順序どおり、固有の整形済み本文・同一 webhook URL で送られること。
        assert mock_post.call_count == 2
        (url_a, text_a) = mock_post.call_args_list[0].args
        (url_b, text_b) = mock_post.call_args_list[1].args
        assert url_a == url_b == "https://hooks.slack.test/x"
        assert "ai-sniper-staging-scheduler-target-error" in text_a
        assert "ai-sniper-staging-scheduler-dlq-messages" in text_b
        assert "[AI Sniper][ALERT]" in text_a and "[AI Sniper][ALERT]" in text_b

    def test_enforces_rate_limit_between_posts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 2 レコード送信時、連続 POST の間に >=1s の待機を挟む契約を固定する。
        # monotonic を決め打ち（毎回 100.0）し、2 件目の elapsed=0 → sleep(1.0) を検証。
        monkeypatch.setattr(notifier.time, "monotonic", lambda: 100.0)
        mock_sleep = MagicMock()
        monkeypatch.setattr(notifier.time, "sleep", mock_sleep)
        with (
            patch.object(
                notifier, "get_slack_webhook_url", return_value="https://hooks.slack.test/x"
            ),
            patch.object(notifier, "_post_to_slack"),
        ):
            notifier.lambda_handler(_sns_event(_alarm_message(), _alarm_message()))
        # 1 件目では待機せず、2 件目の直前に 1 回だけ >=1s の sleep が入る。
        assert mock_sleep.call_count == 1
        assert mock_sleep.call_args.args[0] >= 1.0

    def test_no_records_is_noop(self) -> None:
        with patch.object(notifier, "_post_to_slack") as mock_post:
            result = notifier.lambda_handler({"Records": []})
        assert result == {"delivered": 0, "failed": 0}
        mock_post.assert_not_called()

    def test_webhook_fetch_failure_raises_for_redelivery(self) -> None:
        # webhook が取れない → 失敗終了して SNS/Lambda の再配信に委ねる。
        with (
            patch.object(notifier, "get_slack_webhook_url", side_effect=RuntimeError("no secret")),
            patch.object(notifier, "_post_to_slack") as mock_post,
            pytest.raises(RuntimeError, match="webhook"),
        ):
            notifier.lambda_handler(_sns_event(_alarm_message()))
        mock_post.assert_not_called()

    def test_one_bad_record_still_sends_good_then_raises(self) -> None:
        # 不正レコードがあっても正常レコードは送信し、最後に失敗終了する。
        event = {
            "Records": [
                {"NotSns": {}},  # 不正レコード
                {"Sns": {"Message": _alarm_message(), "Subject": "s"}},
            ]
        }
        with (
            patch.object(
                notifier, "get_slack_webhook_url", return_value="https://hooks.slack.test/x"
            ),
            patch.object(notifier, "_post_to_slack") as mock_post,
            pytest.raises(RuntimeError, match="failed=1"),
        ):
            notifier.lambda_handler(event)
        assert mock_post.call_count == 1

    def test_post_failure_raises_for_redelivery(self) -> None:
        with (
            patch.object(
                notifier, "get_slack_webhook_url", return_value="https://hooks.slack.test/x"
            ),
            patch.object(notifier, "_post_to_slack", side_effect=OSError("network")),
            pytest.raises(RuntimeError, match="failed=2"),
        ):
            notifier.lambda_handler(_sns_event(_alarm_message(), _alarm_message()))


class TestGetSlackWebhookUrl:
    def test_returns_url_from_secrets_manager(self) -> None:
        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {"SecretString": "https://hooks.slack.test/sm"}
        with patch("backend.src.lambda_auth.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_sm
            result = _auth.get_slack_webhook_url()
        assert result == "https://hooks.slack.test/sm"
        mock_sm.get_secret_value.assert_called_once_with(SecretId=_auth.SLACK_WEBHOOK_SECRET_NAME)

    def test_caches_within_invocation(self) -> None:
        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {"SecretString": "https://hooks.slack.test/c"}
        with patch("backend.src.lambda_auth.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_sm
            _auth.get_slack_webhook_url()
            _auth.get_slack_webhook_url()
        assert mock_sm.get_secret_value.call_count == 1

    def test_local_fallback_when_flag_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ALLOW_LOCAL_SLACK_WEBHOOK", "true")
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/local")
        with patch("backend.src.lambda_auth.boto3") as mock_boto3:
            result = _auth.get_slack_webhook_url()
        assert result == "https://hooks.slack.test/local"
        mock_boto3.client.assert_not_called()

    def test_no_local_fallback_without_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/ignored")
        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {"SecretString": "https://hooks.slack.test/sm"}
        with patch("backend.src.lambda_auth.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_sm
            result = _auth.get_slack_webhook_url()
        assert result == "https://hooks.slack.test/sm"

    def test_no_local_fallback_in_lambda_runtime(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 本番バイパス防止: Lambda 実行環境ではフラグ＋env が揃っても Secrets Manager を使う。
        monkeypatch.setenv("ALLOW_LOCAL_SLACK_WEBHOOK", "true")
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/should-not-leak")
        monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "ai-sniper-staging-slack-notifier")
        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {"SecretString": "https://hooks.slack.test/sm"}
        with patch("backend.src.lambda_auth.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_sm
            result = _auth.get_slack_webhook_url()
        assert result == "https://hooks.slack.test/sm"

    def test_uses_overridden_secret_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Terraform/Lambda env で secret 名を差し替える契約を固定する。
        monkeypatch.setattr(_auth, "SLACK_WEBHOOK_SECRET_NAME", "custom/slack-webhook")
        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {
            "SecretString": "https://hooks.slack.test/override"
        }
        with patch("backend.src.lambda_auth.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_sm
            result = _auth.get_slack_webhook_url()
        assert result == "https://hooks.slack.test/override"
        mock_sm.get_secret_value.assert_called_once_with(SecretId="custom/slack-webhook")

    def test_raises_if_secret_empty(self) -> None:
        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {"SecretString": "   "}
        with (
            patch("backend.src.lambda_auth.boto3") as mock_boto3,
            pytest.raises(RuntimeError, match=_auth.SLACK_WEBHOOK_SECRET_NAME),
        ):
            mock_boto3.client.return_value = mock_sm
            _auth.get_slack_webhook_url()

    def test_reset_clears_cache(self) -> None:
        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {"SecretString": "https://hooks.slack.test/rotated"}
        with patch("backend.src.lambda_auth.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_sm
            _auth.get_slack_webhook_url()
            _auth.reset_token_cache()
            _auth.get_slack_webhook_url()
        assert mock_sm.get_secret_value.call_count == 2
