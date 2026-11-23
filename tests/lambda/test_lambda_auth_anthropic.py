"""lambda_auth.get_anthropic_api_key のユニットテスト（Issue #76）。"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import backend.src.lambda_auth as _auth


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch: pytest.MonkeyPatch) -> Any:
    """各テスト前後にキャッシュをリセットし、env を明示的に除去して環境依存を防ぐ。"""
    monkeypatch.delenv("ALLOW_LOCAL_ANTHROPIC_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_SECRET_NAME", raising=False)
    _auth.reset_token_cache()
    yield
    _auth.reset_token_cache()


class TestGetAnthropicApiKey:
    def test_returns_key_from_secrets_manager(self) -> None:
        """Secrets Manager から API キーを取得して返すこと。"""
        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {"SecretString": "sk-ant-test-key"}

        with patch("backend.src.lambda_auth.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_sm
            result = _auth.get_anthropic_api_key()

        assert result == "sk-ant-test-key"
        mock_sm.get_secret_value.assert_called_once_with(SecretId=_auth.ANTHROPIC_SECRET_NAME)

    def test_caches_within_invocation(self) -> None:
        """invocation 内で 2 回呼んでも Secrets Manager を 1 回しか呼ばないこと。"""
        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {"SecretString": "sk-cached"}

        with patch("backend.src.lambda_auth.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_sm
            first = _auth.get_anthropic_api_key()
            second = _auth.get_anthropic_api_key()

        assert first == second == "sk-cached"
        assert mock_sm.get_secret_value.call_count == 1

    def test_reset_token_cache_clears_anthropic_key(self) -> None:
        """reset_token_cache 後は再取得すること。"""
        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {"SecretString": "sk-ant-rotated"}

        with patch("backend.src.lambda_auth.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_sm
            _auth.get_anthropic_api_key()
            _auth.reset_token_cache()
            _auth.get_anthropic_api_key()

        assert mock_sm.get_secret_value.call_count == 2

    def test_local_fallback_when_flag_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ALLOW_LOCAL_ANTHROPIC_KEY=true かつ env に値があれば env を返すこと。"""
        monkeypatch.setenv("ALLOW_LOCAL_ANTHROPIC_KEY", "true")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-local")

        with patch("backend.src.lambda_auth.boto3") as mock_boto3:
            result = _auth.get_anthropic_api_key()

        assert result == "sk-local"
        mock_boto3.client.assert_not_called()

    def test_no_local_fallback_without_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ALLOW_LOCAL_ANTHROPIC_KEY が未設定なら env を無視して Secrets Manager を使うこと。"""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-ignored")
        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {"SecretString": "sk-from-sm"}

        with patch("backend.src.lambda_auth.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_sm
            result = _auth.get_anthropic_api_key()

        assert result == "sk-from-sm"

    def test_raises_if_secret_is_empty(self) -> None:
        """Secrets Manager が空文字を返した場合は RuntimeError を送出すること。"""
        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {"SecretString": "   "}

        with (
            patch("backend.src.lambda_auth.boto3") as mock_boto3,
            pytest.raises(RuntimeError, match=_auth.ANTHROPIC_SECRET_NAME),
        ):
            mock_boto3.client.return_value = mock_sm
            _auth.get_anthropic_api_key()

    def test_uses_overridden_secret_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ANTHROPIC_SECRET_NAME を上書きした場合、その名前で Secrets Manager を呼ぶこと。"""
        monkeypatch.setattr(_auth, "ANTHROPIC_SECRET_NAME", "custom/anthropic")
        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {"SecretString": "sk-override"}

        with patch("backend.src.lambda_auth.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_sm
            result = _auth.get_anthropic_api_key()

        assert result == "sk-override"
        mock_sm.get_secret_value.assert_called_once_with(SecretId="custom/anthropic")

    def test_falls_back_to_secrets_manager_when_local_key_is_blank(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ALLOW_LOCAL_ANTHROPIC_KEY=true でも ANTHROPIC_API_KEY が空白のみなら SM を使うこと。"""
        monkeypatch.setenv("ALLOW_LOCAL_ANTHROPIC_KEY", "true")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")  # 空白のみ → 無効
        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {"SecretString": "sk-sm-fallback"}

        with patch("backend.src.lambda_auth.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_sm
            result = _auth.get_anthropic_api_key()

        assert result == "sk-sm-fallback"
        mock_sm.get_secret_value.assert_called_once()
