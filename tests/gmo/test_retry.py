"""401 再署名リトライ ユニットテスト (§7.2).

401→200 と 401→401 の両パターンをモック HTTP で検証する。
"""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from backend.src.retry import ForexAuthError, request_with_retry


def _make_response(status_code: int) -> httpx.Response:
    return httpx.Response(status_code)


class TestRequestWithRetry:
    """request_with_retry のモック HTTP テスト."""

    def test_200_on_first_attempt_no_retry(self) -> None:
        """初回 200 ならリトライなしでレスポンスを返す."""
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.request.return_value = _make_response(200)

        resp = request_with_retry(
            client=mock_client,
            method="GET",
            url="https://forex-api.coin.z.com/private/v1/account/assets",
            request_path="/private/v1/account/assets",
            api_key="key",
            api_secret="secret",
        )

        assert resp.status_code == 200
        assert mock_client.request.call_count == 1

    def test_401_then_200_retries_once_and_succeeds(self) -> None:
        """401 → 200 パターン: 1 回リトライして 200 を返す."""
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.request.side_effect = [
            _make_response(401),
            _make_response(200),
        ]

        resp = request_with_retry(
            client=mock_client,
            method="GET",
            url="https://forex-api.coin.z.com/private/v1/account/assets",
            request_path="/private/v1/account/assets",
            api_key="key",
            api_secret="secret",
        )

        assert resp.status_code == 200
        assert mock_client.request.call_count == 2

    def test_401_then_401_raises_forex_auth_error(self) -> None:
        """401 → 401 パターン: ForexAuthError を送出する."""
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.request.side_effect = [
            _make_response(401),
            _make_response(401),
        ]

        with pytest.raises(ForexAuthError):
            request_with_retry(
                client=mock_client,
                method="GET",
                url="https://forex-api.coin.z.com/private/v1/account/assets",
                request_path="/private/v1/account/assets",
                api_key="key",
                api_secret="secret",
            )

    def test_retry_count_is_exactly_one(self) -> None:
        """401 時のリトライが exactly 1 回のみ（合計 2 回のリクエスト）."""
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.request.side_effect = [
            _make_response(401),
            _make_response(401),
        ]

        with pytest.raises(ForexAuthError):
            request_with_retry(
                client=mock_client,
                method="POST",
                url="https://forex-api.coin.z.com/private/v1/ifoOrder",
                request_path="/private/v1/ifoOrder",
                api_key="key",
                api_secret="secret",
                body='{"symbol":"USD_JPY"}',
            )

        assert mock_client.request.call_count == 2, (
            f"リトライが 1 回を超えている: call_count={mock_client.request.call_count}"
        )

    def test_each_attempt_uses_fresh_timestamp(self) -> None:
        """リトライ時に新しいタイムスタンプで再署名すること（API-TIMESTAMP が異なる）."""
        timestamps: list[str] = []

        def capture_headers(method: str, url: str, **kwargs: object) -> httpx.Response:
            headers = kwargs.get("headers", {})
            if isinstance(headers, dict):
                timestamps.append(headers.get("API-TIMESTAMP", ""))
            return _make_response(401)

        mock_client = MagicMock(spec=httpx.Client)
        mock_client.request.side_effect = capture_headers

        with patch("backend.src.signer._timestamp_ms") as mock_ts:
            mock_ts.side_effect = ["1000000000000", "1000000000999"]
            with pytest.raises(ForexAuthError):
                request_with_retry(
                    client=mock_client,
                    method="GET",
                    url="https://forex-api.coin.z.com/private/v1/account/assets",
                    request_path="/private/v1/account/assets",
                    api_key="key",
                    api_secret="secret",
                )

        # タイムスタンプが 2 回発行され、かつ両リクエストで異なる値が使われること
        assert mock_ts.call_count == 2
        assert len(timestamps) == 2
        assert timestamps[0] == "1000000000000"
        assert timestamps[1] == "1000000000999"
        assert timestamps[0] != timestamps[1]


class TestPostAttemptHook:
    """post_attempt_hook の呼び出しタイミング検証."""

    def test_post_attempt_hook_called_once_on_success(self) -> None:
        """正常 POST では post_attempt_hook が 1 回だけ呼ばれること."""
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.request.return_value = _make_response(200)
        hook = MagicMock()

        request_with_retry(
            client=mock_client,
            method="POST",
            url="https://forex-api.coin.z.com/private/v1/ifoOrder",
            request_path="/private/v1/ifoOrder",
            api_key="key",
            api_secret="secret",
            body='{"symbol":"USD_JPY"}',
            post_attempt_hook=hook,
        )

        hook.assert_called_once()

    def test_post_attempt_hook_called_twice_on_401_retry(self) -> None:
        """401 リトライ時は post_attempt_hook が各試行後に呼ばれる（合計 2 回）."""
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.request.side_effect = [_make_response(401), _make_response(200)]
        hook = MagicMock()

        with patch("backend.src.retry.time.sleep"):
            request_with_retry(
                client=mock_client,
                method="POST",
                url="https://forex-api.coin.z.com/private/v1/ifoOrder",
                request_path="/private/v1/ifoOrder",
                api_key="key",
                api_secret="secret",
                body='{"symbol":"USD_JPY"}',
                post_attempt_hook=hook,
            )

        assert hook.call_count == 2

    def test_post_attempt_hook_called_on_exception(self) -> None:
        """接続例外でも post_attempt_hook が呼ばれること（即時再送を防ぐ）."""
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.request.side_effect = httpx.ConnectError("connection refused")
        hook = MagicMock()

        with pytest.raises(httpx.ConnectError):
            request_with_retry(
                client=mock_client,
                method="POST",
                url="https://forex-api.coin.z.com/private/v1/ifoOrder",
                request_path="/private/v1/ifoOrder",
                api_key="key",
                api_secret="secret",
                body='{"symbol":"USD_JPY"}',
                post_attempt_hook=hook,
            )

        hook.assert_called_once()

    def test_post_attempt_hook_not_called_for_get(self) -> None:
        """GET では post_attempt_hook が呼ばれないこと（POST 専用）."""
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.request.return_value = _make_response(200)
        hook = MagicMock()

        request_with_retry(
            client=mock_client,
            method="GET",
            url="https://forex-api.coin.z.com/private/v1/account/assets",
            request_path="/private/v1/account/assets",
            api_key="key",
            api_secret="secret",
            post_attempt_hook=hook,
        )

        hook.assert_not_called()
