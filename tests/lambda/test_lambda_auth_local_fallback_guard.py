"""_local_fallback_allowed の本番バイパス防止ガード横展開テスト（Issue #139）。

get_gmo_credentials / get_anthropic_api_key / get_slack_webhook_url の3関数で
「AWS_LAMBDA_FUNCTION_NAME セット時は env フォールバック無効化」ガードが
効いていることを確認する。

NOTE: get_dashboard_token は Ver 3.7（#177）で廃止したため対象外。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import backend.src.lambda_auth as _auth

_ENV_KEYS = [
    "ALLOW_LOCAL_GMO_CREDENTIALS",
    "GMO_API_KEY",
    "GMO_API_SECRET",
    "ALLOW_LOCAL_ANTHROPIC_KEY",
    "ANTHROPIC_API_KEY",
    "ALLOW_LOCAL_SLACK_WEBHOOK",
    "SLACK_WEBHOOK_URL",
    "AWS_LAMBDA_FUNCTION_NAME",
]


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    _auth.reset_token_cache()
    yield
    _auth.reset_token_cache()


class TestLocalFallbackAllowedHelper:
    def test_true_when_flag_set_and_not_lambda(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ALLOW_LOCAL_GMO_CREDENTIALS", "true")
        assert _auth._local_fallback_allowed("ALLOW_LOCAL_GMO_CREDENTIALS") is True

    def test_false_when_lambda_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ALLOW_LOCAL_GMO_CREDENTIALS", "true")
        monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "ai-sniper-staging-read-api")
        assert _auth._local_fallback_allowed("ALLOW_LOCAL_GMO_CREDENTIALS") is False

    def test_false_when_flag_unset(self) -> None:
        assert _auth._local_fallback_allowed("ALLOW_LOCAL_GMO_CREDENTIALS") is False


class TestGmoCredentialsLambdaGuard:
    def test_local_fallback_when_not_lambda(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ALLOW_LOCAL_GMO_CREDENTIALS", "true")
        monkeypatch.setenv("GMO_API_KEY", "local-key")
        monkeypatch.setenv("GMO_API_SECRET", "local-secret")

        with patch("backend.src.lambda_auth.boto3") as mock_boto3:
            result = _auth.get_gmo_credentials()

        assert result == ("local-key", "local-secret")
        mock_boto3.client.assert_not_called()

    def test_ignores_env_when_lambda_function_name_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """本番バイパス防止: 実取引の GMO 認証情報が平文 env から読まれないこと。"""
        monkeypatch.setenv("ALLOW_LOCAL_GMO_CREDENTIALS", "true")
        monkeypatch.setenv("GMO_API_KEY", "should-be-ignored")
        monkeypatch.setenv("GMO_API_SECRET", "should-be-ignored")
        monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "ai-sniper-staging-decision-batch")
        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {
            "SecretString": '{"api_key": "sm-key", "api_secret": "sm-secret"}'
        }

        with patch("backend.src.lambda_auth.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_sm
            result = _auth.get_gmo_credentials()

        assert result == ("sm-key", "sm-secret")
        mock_sm.get_secret_value.assert_called_once()


class TestAnthropicApiKeyLambdaGuard:
    def test_local_fallback_when_not_lambda(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ALLOW_LOCAL_ANTHROPIC_KEY", "true")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-local")

        with patch("backend.src.lambda_auth.boto3") as mock_boto3:
            result = _auth.get_anthropic_api_key()

        assert result == "sk-local"
        mock_boto3.client.assert_not_called()

    def test_ignores_env_when_lambda_function_name_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ALLOW_LOCAL_ANTHROPIC_KEY", "true")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "should-be-ignored")
        monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "ai-sniper-staging-decision-batch")
        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {"SecretString": "sk-sm"}

        with patch("backend.src.lambda_auth.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_sm
            result = _auth.get_anthropic_api_key()

        assert result == "sk-sm"
        mock_sm.get_secret_value.assert_called_once()


class TestSlackWebhookLambdaGuard:
    def test_local_fallback_when_not_lambda(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ALLOW_LOCAL_SLACK_WEBHOOK", "true")
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/local")

        with patch("backend.src.lambda_auth.boto3") as mock_boto3:
            result = _auth.get_slack_webhook_url()

        assert result == "https://hooks.slack.test/local"
        mock_boto3.client.assert_not_called()

    def test_ignores_env_when_lambda_function_name_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ALLOW_LOCAL_SLACK_WEBHOOK", "true")
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "should-be-ignored")
        monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "ai-sniper-staging-notifier")
        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {"SecretString": "https://hooks.slack.test/sm"}

        with patch("backend.src.lambda_auth.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_sm
            result = _auth.get_slack_webhook_url()

        assert result == "https://hooks.slack.test/sm"
        mock_sm.get_secret_value.assert_called_once()
