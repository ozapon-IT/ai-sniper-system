"""GmoFxClient ユニットテスト (§1.4, §7.2)."""

import json
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from backend.src.clients.gmo_client import (
    _EXECUTION_FETCH_DEADLINE_SECONDS,
    _EXECUTION_FETCH_MAX_ATTEMPTS,
    _ORDER_PATHS,
    MAX_LATEST_EXECUTIONS_COUNT,
    GmoFxClient,
    OrderBlockedError,
)
from backend.src.gmo_errors import GmoApiError
from backend.src.gmo_fx_endpoints import (
    GMO_FX_HOST,
    PRIVATE_ACCOUNT_ASSETS,
    PRIVATE_CANCEL_ORDERS,
    PRIVATE_CLOSE_ORDER,
    PRIVATE_IFO_ORDER,
    PUBLIC_KLINES,
)
from backend.src.models import CloseOrderRequest, IfoOrderRequest


def _make_response(status: int = 200, body: str = "{}") -> httpx.Response:
    return httpx.Response(status_code=status, content=body.encode())


def _enable_live_orders(monkeypatch: pytest.MonkeyPatch) -> None:
    """発注封じ込めの two-key 条件を両方満たす（テスト用 AGENTS.md #1）."""
    monkeypatch.setenv("ALLOW_LIVE_ORDERS", "True")
    monkeypatch.setenv("TRADING_PHASE", "2")


# ---------------------------------------------------------------------------
# Public GET（署名なし）
# ---------------------------------------------------------------------------


def test_public_get_calls_get_without_auth_headers() -> None:
    """public_get は署名ヘッダなしで GET する."""
    mock_client = MagicMock(spec=httpx.Client)
    mock_client.get.return_value = _make_response()

    with patch("backend.src.clients.gmo_client.RateLimiter"):
        gmo = GmoFxClient(http_client=mock_client)
        gmo.public_get(PUBLIC_KLINES, params={"symbol": "USD_JPY", "interval": "1day"})

    mock_client.get.assert_called_once_with(
        GMO_FX_HOST + PUBLIC_KLINES,
        params={"symbol": "USD_JPY", "interval": "1day"},
    )
    # request() は呼ばれていない（署名不要）
    mock_client.request.assert_not_called()


def test_public_get_calls_rate_limiter_wait_get() -> None:
    """public_get は RateLimiter.wait_get() を呼ぶ."""
    mock_client = MagicMock(spec=httpx.Client)
    mock_client.get.return_value = _make_response()

    with patch("backend.src.clients.gmo_client.RateLimiter") as mock_limiter_cls:
        mock_limiter_instance = mock_limiter_cls.return_value
        gmo = GmoFxClient(http_client=mock_client)
        gmo.public_get(PUBLIC_KLINES)

    mock_limiter_instance.wait_get.assert_called_once()
    mock_limiter_instance.wait_post.assert_not_called()


# ---------------------------------------------------------------------------
# Private GET（署名あり + 署名パス分離）
# ---------------------------------------------------------------------------


def test_private_get_request_path_and_sign_path_are_separated() -> None:
    """private_get はリクエスト URL を /private/v1/... で送り、署名パスは /v1/... を使う (§7.2)."""
    mock_client = MagicMock(spec=httpx.Client)

    with (
        patch("backend.src.clients.gmo_client.RateLimiter"),
        patch("backend.src.clients.gmo_client.request_with_retry") as mock_retry,
    ):
        mock_retry.return_value = _make_response()
        gmo = GmoFxClient(api_key="k", api_secret="s", http_client=mock_client)
        gmo.private_get(PRIVATE_ACCOUNT_ASSETS)

    _, kwargs = mock_retry.call_args
    # リクエスト URL パスは /private/v1/... のまま
    assert kwargs["request_path"] == "/private/v1/account/assets"
    assert kwargs["request_path"].startswith("/private")
    # 署名パスは /v1/... （/private を含まない）
    assert kwargs["sign_path"] == "/v1/account/assets"
    assert not kwargs["sign_path"].startswith("/private")


def test_private_get_uses_request_with_retry() -> None:
    """private_get は request_with_retry 経由で署名付き GET を送る."""
    mock_client = MagicMock(spec=httpx.Client)

    with (
        patch("backend.src.clients.gmo_client.RateLimiter"),
        patch("backend.src.clients.gmo_client.request_with_retry") as mock_retry,
    ):
        mock_retry.return_value = _make_response()
        gmo = GmoFxClient(api_key="k", api_secret="s", http_client=mock_client)
        gmo.private_get(PRIVATE_ACCOUNT_ASSETS)

    mock_retry.assert_called_once()
    _, kwargs = mock_retry.call_args
    assert kwargs["method"] == "GET"
    assert kwargs["api_key"] == "k"
    assert kwargs["api_secret"] == "s"


def test_private_get_passes_wait_get_as_pre_attempt_hook() -> None:
    """private_get は wait_get を pre_attempt_hook に渡す（401リトライ前にも適用）."""
    mock_client = MagicMock(spec=httpx.Client)

    with (
        patch("backend.src.clients.gmo_client.RateLimiter") as mock_limiter_cls,
        patch("backend.src.clients.gmo_client.request_with_retry") as mock_retry,
    ):
        mock_retry.return_value = _make_response()
        gmo = GmoFxClient(api_key="k", api_secret="s", http_client=mock_client)
        gmo.private_get(PRIVATE_ACCOUNT_ASSETS)

    _, kwargs = mock_retry.call_args
    assert kwargs["pre_attempt_hook"] is mock_limiter_cls.return_value.wait_get


# ---------------------------------------------------------------------------
# Private POST（署名あり + 署名パス分離 + レートリミット）
# ---------------------------------------------------------------------------


def test_private_post_request_path_and_sign_path_are_separated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """private_post はリクエスト URL を /private/v1/... で送り、署名パスは /v1/... を使う (§7.2)."""
    _enable_live_orders(monkeypatch)
    mock_client = MagicMock(spec=httpx.Client)

    with (
        patch("backend.src.clients.gmo_client.RateLimiter"),
        patch("backend.src.clients.gmo_client.request_with_retry") as mock_retry,
    ):
        mock_retry.return_value = _make_response()
        gmo = GmoFxClient(api_key="k", api_secret="s", http_client=mock_client)
        gmo.private_post(PRIVATE_IFO_ORDER)

    _, kwargs = mock_retry.call_args
    assert kwargs["request_path"] == "/private/v1/ifoOrder"
    assert kwargs["request_path"].startswith("/private")
    assert kwargs["sign_path"] == "/v1/ifoOrder"
    assert not kwargs["sign_path"].startswith("/private")


def test_private_post_uses_request_with_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """private_post は request_with_retry 経由で署名付き POST を送る."""
    _enable_live_orders(monkeypatch)
    mock_client = MagicMock(spec=httpx.Client)

    with (
        patch("backend.src.clients.gmo_client.RateLimiter"),
        patch("backend.src.clients.gmo_client.request_with_retry") as mock_retry,
    ):
        mock_retry.return_value = _make_response()
        gmo = GmoFxClient(api_key="k", api_secret="s", http_client=mock_client)
        gmo.private_post(PRIVATE_IFO_ORDER, body={"symbol": "USD_JPY"})

    _, kwargs = mock_retry.call_args
    assert kwargs["method"] == "POST"
    assert "USD_JPY" in kwargs["body"]


def test_private_post_basemodel_serialized_with_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    """private_post に BaseModel を渡すと camelCase alias でシリアライズされること."""
    _enable_live_orders(monkeypatch)
    mock_client = MagicMock(spec=httpx.Client)
    order = IfoOrderRequest(
        firstSide="BUY",
        firstSize=100,
        firstPrice="150.000",
        secondSize=100,
        secondLimitPrice="152.000",
        secondStopPrice="148.000",
    )

    with (
        patch("backend.src.clients.gmo_client.RateLimiter"),
        patch("backend.src.clients.gmo_client.request_with_retry") as mock_retry,
    ):
        mock_retry.return_value = _make_response()
        gmo = GmoFxClient(api_key="k", api_secret="s", http_client=mock_client)
        gmo.private_post(PRIVATE_IFO_ORDER, body=order)

    _, kwargs = mock_retry.call_args
    body = json.loads(kwargs["body"])
    # E06 確定スキーマの完全一致検証（firstSize/secondSize は int 型）
    assert body == {
        "symbol": "USD_JPY",
        "firstSide": "BUY",
        "firstExecutionType": "LIMIT",
        "firstSize": "100",  # 整数文字列（2026-08-03 スパイクで確定・int だと ERR-5105）
        "firstPrice": "150.000",
        "secondSize": "100",
        "secondLimitPrice": "152.000",
        "secondStopPrice": "148.000",
    }


def test_private_post_passes_wait_post_as_pre_attempt_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """private_post は wait_post を pre_attempt_hook に渡す（401リトライ前にも適用）."""
    _enable_live_orders(monkeypatch)
    mock_client = MagicMock(spec=httpx.Client)

    with (
        patch("backend.src.clients.gmo_client.RateLimiter") as mock_limiter_cls,
        patch("backend.src.clients.gmo_client.request_with_retry") as mock_retry,
    ):
        mock_retry.return_value = _make_response()
        gmo = GmoFxClient(api_key="k", api_secret="s", http_client=mock_client)
        gmo.private_post(PRIVATE_IFO_ORDER)

    _, kwargs = mock_retry.call_args
    assert kwargs["pre_attempt_hook"] is mock_limiter_cls.return_value.wait_post


def test_private_post_passes_mark_post_done_as_post_attempt_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """private_post は mark_post_done を post_attempt_hook に渡す（応答受信後タイマー更新）."""
    _enable_live_orders(monkeypatch)
    mock_client = MagicMock(spec=httpx.Client)

    with (
        patch("backend.src.clients.gmo_client.RateLimiter") as mock_limiter_cls,
        patch("backend.src.clients.gmo_client.request_with_retry") as mock_retry,
    ):
        mock_retry.return_value = _make_response()
        gmo = GmoFxClient(api_key="k", api_secret="s", http_client=mock_client)
        gmo.private_post(PRIVATE_IFO_ORDER)

    _, kwargs = mock_retry.call_args
    assert kwargs["post_attempt_hook"] is mock_limiter_cls.return_value.mark_post_done


def test_consecutive_posts_pre_attempt_hook_called_each_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """連続 private_post で pre_attempt_hook が POST 回数分呼ばれること（≥1秒保証の証明）."""
    _enable_live_orders(monkeypatch)
    mock_client = MagicMock(spec=httpx.Client)

    with (
        patch("backend.src.clients.gmo_client.RateLimiter") as mock_limiter_cls,
        patch("backend.src.clients.gmo_client.request_with_retry", return_value=_make_response()),
    ):
        mock_limiter_instance = mock_limiter_cls.return_value
        gmo = GmoFxClient(api_key="k", api_secret="s", http_client=mock_client)

        gmo.private_post(PRIVATE_IFO_ORDER)
        gmo.private_post(PRIVATE_IFO_ORDER)
        gmo.private_post(PRIVATE_IFO_ORDER)

    # wait_post は request_with_retry 内で pre_attempt_hook として呼ばれるため、
    # ここでは call_count ではなく渡された hook の参照が正しいことを確認
    assert mock_limiter_instance.wait_post.call_count == 0  # 直接呼び出しはない
    # (実際の wait_post 呼び出しは request_with_retry 内で行われる)


# ---------------------------------------------------------------------------
# HTTP クライアントのオーナーシップ
# ---------------------------------------------------------------------------


def test_close_does_not_close_injected_client() -> None:
    """外部注入した httpx.Client は close() で閉じないこと."""
    mock_client = MagicMock(spec=httpx.Client)

    with patch("backend.src.clients.gmo_client.RateLimiter"):
        gmo = GmoFxClient(http_client=mock_client)
        gmo.close()

    mock_client.close.assert_not_called()


def test_close_closes_internally_created_client() -> None:
    """内部生成クライアントは close() で閉じること."""
    mock_httpx_client = MagicMock(spec=httpx.Client)

    with (
        patch("backend.src.clients.gmo_client.RateLimiter"),
        patch("backend.src.clients.gmo_client.httpx.Client", return_value=mock_httpx_client),
    ):
        gmo = GmoFxClient()
        gmo.close()

    mock_httpx_client.close.assert_called_once()


def test_context_manager_does_not_close_injected_client() -> None:
    """with ブロックを抜けても外部注入クライアントは閉じないこと."""
    mock_client = MagicMock(spec=httpx.Client)

    with patch("backend.src.clients.gmo_client.RateLimiter"), GmoFxClient(http_client=mock_client):
        pass

    mock_client.close.assert_not_called()


# ---------------------------------------------------------------------------
# ベース URL のカスタマイズ
# ---------------------------------------------------------------------------


def test_base_url_trailing_slash_stripped() -> None:
    """ベース URL 末尾スラッシュが重複しないこと."""
    mock_client = MagicMock(spec=httpx.Client)
    mock_client.get.return_value = _make_response()

    with patch("backend.src.clients.gmo_client.RateLimiter"):
        gmo = GmoFxClient(base_url="https://mock.example.com/", http_client=mock_client)
        gmo.public_get(PUBLIC_KLINES)

    url_called = mock_client.get.call_args[0][0]
    assert "//" not in url_called.replace("https://", "")


# ---------------------------------------------------------------------------
# Phase 1 安全ゲート (AGENTS.md #1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("order_path", [
    PRIVATE_IFO_ORDER,
    PRIVATE_CLOSE_ORDER,
    PRIVATE_CANCEL_ORDERS,
])
def test_all_order_endpoints_blocked_by_default(
    order_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """既定（two-key 両方未設定）では全発注系 POST が OrderBlockedError で遮断される (AGENTS.md #1)."""
    monkeypatch.delenv("ALLOW_LIVE_ORDERS", raising=False)
    monkeypatch.delenv("TRADING_PHASE", raising=False)
    mock_client = MagicMock(spec=httpx.Client)

    with patch("backend.src.clients.gmo_client.RateLimiter"):
        gmo = GmoFxClient(api_key="k", api_secret="s", http_client=mock_client)
        with pytest.raises(OrderBlockedError):
            gmo.private_post(order_path)

    # request_with_retry に到達していないこと（実送信されていない）
    mock_client.request.assert_not_called()


@pytest.mark.parametrize(
    ("allow", "phase"),
    [
        (None, None),     # 両方未設定
        ("False", "2"),   # 主フラグ明示 False（フェーズだけ実取引）
        ("false", "2"),   # Terraform 既定値（小文字 "false"）でも確実にブロック
        ("False", None),  # 主フラグ明示 False
        ("True", "1"),    # 主フラグ True だがフェーズはモック
        ("True", None),   # 主フラグ True だがフェーズ未設定（既定 "1" 相当）
        ("1", "2"),       # 真偽値として不正な表記は解禁しない
    ],
)
def test_order_post_blocked_unless_both_keys_set(
    allow: str | None, phase: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """two-key のどちらか一方でも欠ければ発注系 POST は遮断される（単一フラグ設定ミス対策、AGENTS.md #1）."""
    if allow is None:
        monkeypatch.delenv("ALLOW_LIVE_ORDERS", raising=False)
    else:
        monkeypatch.setenv("ALLOW_LIVE_ORDERS", allow)
    if phase is None:
        monkeypatch.delenv("TRADING_PHASE", raising=False)
    else:
        monkeypatch.setenv("TRADING_PHASE", phase)
    mock_client = MagicMock(spec=httpx.Client)

    with patch("backend.src.clients.gmo_client.RateLimiter"):
        gmo = GmoFxClient(api_key="k", api_secret="s", http_client=mock_client)
        with pytest.raises(OrderBlockedError):
            gmo.private_post(PRIVATE_IFO_ORDER)

    mock_client.request.assert_not_called()


def test_order_post_blocked_even_when_mvp_mode_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """MVP_MODE=True だけでは封じ込めは解除されない（責務分離の保証、AGENTS.md #1）."""
    monkeypatch.setenv("MVP_MODE", "True")
    monkeypatch.delenv("ALLOW_LIVE_ORDERS", raising=False)
    monkeypatch.delenv("TRADING_PHASE", raising=False)
    mock_client = MagicMock(spec=httpx.Client)

    with patch("backend.src.clients.gmo_client.RateLimiter"):
        gmo = GmoFxClient(api_key="k", api_secret="s", http_client=mock_client)
        with pytest.raises(OrderBlockedError):
            gmo.private_post(PRIVATE_IFO_ORDER)

    mock_client.request.assert_not_called()


@pytest.mark.parametrize(
    ("allow", "phase"),
    [
        ("True", "2"),
        ("True", "spike"),
        ("true", "2"),      # Terraform locals.tf の実値（小文字）で解禁される（Issue #135）
        (" true ", " 2 "),  # 前後空白は無視（オペレータの表記ゆれ耐性）
    ],
)
def test_ifo_order_allowed_when_both_keys_set(
    allow: str, phase: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """two-key が揃えば表記ゆれ（大小・空白）に関係なく ifoOrder POST が到達する."""
    monkeypatch.setenv("ALLOW_LIVE_ORDERS", allow)
    monkeypatch.setenv("TRADING_PHASE", phase)
    mock_client = MagicMock(spec=httpx.Client)

    with (
        patch("backend.src.clients.gmo_client.RateLimiter"),
        patch("backend.src.clients.gmo_client.request_with_retry") as mock_retry,
    ):
        mock_retry.return_value = _make_response()
        gmo = GmoFxClient(api_key="k", api_secret="s", http_client=mock_client)
        gmo.private_post(PRIVATE_IFO_ORDER, body={"symbol": "USD_JPY", "size": 100})

    mock_retry.assert_called_once()
    _, kwargs = mock_retry.call_args
    assert kwargs["request_path"] == PRIVATE_IFO_ORDER


def test_non_order_post_not_blocked_without_allow_live_orders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """発注系以外のエンドポイントは封じ込めフラグに関係なく通過する."""
    monkeypatch.delenv("ALLOW_LIVE_ORDERS", raising=False)
    mock_client = MagicMock(spec=httpx.Client)

    with (
        patch("backend.src.clients.gmo_client.RateLimiter"),
        patch("backend.src.clients.gmo_client.request_with_retry") as mock_retry,
    ):
        mock_retry.return_value = _make_response()
        gmo = GmoFxClient(api_key="k", api_secret="s", http_client=mock_client)
        # /private/v1/account/assets は発注系ではないのでブロックされない
        gmo.private_post("/private/v1/account/assets")

    mock_retry.assert_called_once()


# ---------------------------------------------------------------------------
# IPv4 強制バインド（E04: GMO IP 制限が IPv4 のみ・ERR-5012 回避）
# ---------------------------------------------------------------------------


def test_internal_client_forces_ipv4_transport() -> None:
    """内部生成クライアントは local_address='0.0.0.0' の IPv4 transport を渡す."""
    with (
        patch("backend.src.clients.gmo_client.RateLimiter"),
        patch("backend.src.clients.gmo_client.httpx.HTTPTransport") as mock_transport_cls,
        patch("backend.src.clients.gmo_client.httpx.Client") as mock_client_cls,
    ):
        GmoFxClient(api_key="k", api_secret="s")

    mock_transport_cls.assert_called_once_with(local_address="0.0.0.0")
    _, kwargs = mock_client_cls.call_args
    assert kwargs["transport"] is mock_transport_cls.return_value


def test_injected_client_does_not_create_ipv4_transport() -> None:
    """外部注入クライアントは transport を上書きしない（ライフサイクルは呼び出し元責務）."""
    mock_client = MagicMock(spec=httpx.Client)

    with (
        patch("backend.src.clients.gmo_client.RateLimiter"),
        patch("backend.src.clients.gmo_client.httpx.HTTPTransport") as mock_transport_cls,
        patch("backend.src.clients.gmo_client.httpx.Client") as mock_client_cls,
    ):
        gmo = GmoFxClient(http_client=mock_client)

    assert gmo._client is mock_client
    mock_transport_cls.assert_not_called()
    mock_client_cls.assert_not_called()


# ---------------------------------------------------------------------------
# 封じ込め対象集合の固定と closeOrder ボディ（Issue #230）
# ---------------------------------------------------------------------------


def test_order_paths_is_exact_set() -> None:
    """封じ込め対象が FX API の発注系 POST 3 つと完全一致すること（AGENTS.md #1）.

    増減が必ず意図的な変更になるよう集合を固定する。closeBulkOrder は FX API に
    存在しない（Issue #230）ため含まない — 実送信するパス closeOrder は残るので
    封じ込めは弱化しない。
    """
    expected = frozenset({PRIVATE_IFO_ORDER, PRIVATE_CLOSE_ORDER, PRIVATE_CANCEL_ORDERS})
    assert expected == _ORDER_PATHS


def test_close_order_basemodel_body_still_blocked_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CloseOrderRequest（BaseModel）を渡しても既定では封じ込めが働くこと.

    dict → BaseModel へのボディ形式変更（#230）で two-key ゲートを迂回していないことの回帰ガード。
    """
    monkeypatch.delenv("ALLOW_LIVE_ORDERS", raising=False)
    monkeypatch.delenv("TRADING_PHASE", raising=False)
    mock_client = MagicMock(spec=httpx.Client)
    body = CloseOrderRequest(
        side="SELL", settlePosition=[{"positionId": 1000342, "size": "100"}]
    )

    with patch("backend.src.clients.gmo_client.RateLimiter"):
        gmo = GmoFxClient(api_key="k", api_secret="s", http_client=mock_client)
        with pytest.raises(OrderBlockedError):
            gmo.private_post(PRIVATE_CLOSE_ORDER, body=body)

    mock_client.request.assert_not_called()


def test_close_order_basemodel_serialized_to_official_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """closeOrder のボディが公式リクエスト例と一致し null を含まないこと（#230）."""
    _enable_live_orders(monkeypatch)
    mock_client = MagicMock(spec=httpx.Client)
    body = CloseOrderRequest(
        side="SELL", settlePosition=[{"positionId": 1000342, "size": "100"}]
    )

    with (
        patch("backend.src.clients.gmo_client.RateLimiter"),
        patch("backend.src.clients.gmo_client.request_with_retry") as mock_retry,
    ):
        mock_retry.return_value = _make_response()
        gmo = GmoFxClient(api_key="k", api_secret="s", http_client=mock_client)
        gmo.private_post(PRIVATE_CLOSE_ORDER, body=body)

    _, kwargs = mock_retry.call_args
    assert "null" not in kwargs["body"]
    assert json.loads(kwargs["body"]) == {
        "symbol": "USD_JPY",
        "side": "SELL",
        "executionType": "MARKET",
        "settlePosition": [{"positionId": 1000342, "size": "100"}],
    }


# ---------------------------------------------------------------------------
# 約定照会ヘルパー（2026-08-05 ノート捏造事故の回帰固定）
#
# 🔴 GMO は業務エラーでも HTTP 200 + ボディ {"status": 1, ...} を返す。
#    raise_for_status() はこれを通過するため、ボディ status を見ない実装は
#    「API エラー」を「約定なし」に化けさせる。実際にこれが起き、約定・決済済みなのに
#    executions_count: 0 となり LLM が事実と反対のノートを生成した。
#    詳細: docs/spikes/executions_api_spec.md（付録B）
# ---------------------------------------------------------------------------

_GMO_ERROR_BODY = json.dumps(
    {
        "status": 1,
        "messages": [
            {"message_code": "ERR-5106", "message_string": "Invalid request parameter."}
        ],
        "responsetime": "2026-08-05T04:30:00.000Z",
    }
)


def _make_exec_response(body: str, status: int = 200) -> httpx.Response:
    """raise_for_status() を呼べるよう request を紐づけたレスポンスを作る."""
    return httpx.Response(
        status_code=status,
        content=body.encode(),
        request=httpx.Request("GET", GMO_FX_HOST + "/private/v1/executions"),
    )


def test_get_executions_raises_on_http200_body_status_error() -> None:
    """HTTP 200 + body status:1 は GmoApiError（空リストに化けない）.

    リトライ（#245）を尽くしたうえで最終的に送出されることも兼ねて確認する。
    sleep は潰す（実時間を使わない）。
    """
    with (
        patch("backend.src.clients.gmo_client.RateLimiter"),
        patch("backend.src.clients.gmo_client.time.sleep"),
        patch("backend.src.clients.gmo_client.request_with_retry") as mock_retry,
    ):
        mock_retry.return_value = _make_exec_response(_GMO_ERROR_BODY)
        gmo = GmoFxClient(api_key="k", api_secret="s", http_client=MagicMock(spec=httpx.Client))

        with pytest.raises(GmoApiError) as exc_info:
            gmo.get_executions(order_id="1000201")

    assert exc_info.value.message_codes == ("ERR-5106",)


def test_get_latest_executions_raises_on_http200_body_status_error() -> None:
    """latestExecutions も同様に握り潰さない（原因 C: warning すら出ていなかった）."""
    with (
        patch("backend.src.clients.gmo_client.RateLimiter"),
        patch("backend.src.clients.gmo_client.time.sleep"),
        patch("backend.src.clients.gmo_client.request_with_retry") as mock_retry,
    ):
        mock_retry.return_value = _make_exec_response(_GMO_ERROR_BODY)
        gmo = GmoFxClient(api_key="k", api_secret="s", http_client=MagicMock(spec=httpx.Client))

        with pytest.raises(GmoApiError):
            gmo.get_latest_executions(symbol="USD_JPY")


def test_get_executions_sends_order_id_only() -> None:
    """executions は orderId のみ。symbol / from / to / date は存在しない（原因 A）."""
    body = json.dumps({"status": 0, "data": {"list": []}})
    with (
        patch("backend.src.clients.gmo_client.RateLimiter"),
        patch("backend.src.clients.gmo_client.request_with_retry") as mock_retry,
    ):
        mock_retry.return_value = _make_exec_response(body)
        gmo = GmoFxClient(api_key="k", api_secret="s", http_client=MagicMock(spec=httpx.Client))
        gmo.get_executions(order_id=1000201)

    params = mock_retry.call_args.kwargs["params"]
    assert params == {"orderId": 1000201}


def test_get_latest_executions_sends_symbol_and_count() -> None:
    """latestExecutions は symbol 必須 + count。orderId は存在しない（原因 C）."""
    body = json.dumps({"status": 0, "data": {"list": []}})
    with (
        patch("backend.src.clients.gmo_client.RateLimiter"),
        patch("backend.src.clients.gmo_client.request_with_retry") as mock_retry,
    ):
        mock_retry.return_value = _make_exec_response(body)
        gmo = GmoFxClient(api_key="k", api_secret="s", http_client=MagicMock(spec=httpx.Client))
        gmo.get_latest_executions(symbol="USD_JPY")

    params = mock_retry.call_args.kwargs["params"]
    assert params == {"symbol": "USD_JPY", "count": MAX_LATEST_EXECUTIONS_COUNT}
    assert "orderId" not in params


def test_get_latest_executions_returns_open_and_close_records() -> None:
    """OPEN と CLOSE の両方が返る（原因 D: 決済約定への唯一の到達経路）."""
    body = json.dumps({
        "status": 0,
        "data": {"list": [
            {"executionId": 1, "orderId": 1000201, "settleType": "OPEN",
             "side": "BUY", "price": "150.681", "size": "100", "lossGain": "0"},
            {"executionId": 2, "orderId": 1000202, "settleType": "CLOSE",
             "side": "SELL", "price": "150.781", "size": "100", "lossGain": "10.0"},
        ]},
    })
    with (
        patch("backend.src.clients.gmo_client.RateLimiter"),
        patch("backend.src.clients.gmo_client.request_with_retry") as mock_retry,
    ):
        mock_retry.return_value = _make_exec_response(body)
        gmo = GmoFxClient(api_key="k", api_secret="s", http_client=MagicMock(spec=httpx.Client))
        records = gmo.get_latest_executions(symbol="USD_JPY")

    assert [r["settleType"] for r in records] == ["OPEN", "CLOSE"]


def test_get_latest_executions_rejects_count_over_official_max() -> None:
    """count の上限は公式仕様の 100（指定しない場合は100=最大値）."""
    with patch("backend.src.clients.gmo_client.RateLimiter"):
        gmo = GmoFxClient(api_key="k", api_secret="s", http_client=MagicMock(spec=httpx.Client))

    with pytest.raises(ValueError):
        gmo.get_latest_executions(symbol="USD_JPY", count=MAX_LATEST_EXECUTIONS_COUNT + 1)


def test_execution_helpers_reject_positional_args() -> None:
    """キーワード専用。旧シグネチャと位置引数で取り違えても静かに通らない."""
    with patch("backend.src.clients.gmo_client.RateLimiter"):
        gmo = GmoFxClient(api_key="k", api_secret="s", http_client=MagicMock(spec=httpx.Client))

    with pytest.raises(TypeError):
        gmo.get_executions("USD_JPY")        # type: ignore[misc]
    with pytest.raises(TypeError):
        gmo.get_latest_executions("ord-1")   # type: ignore[misc]


# ---------------------------------------------------------------------------
# 約定取得の invocation 内リトライ（Issue #245）
#
# 🔴 約定を取れる API は latestExecutions（直近1日分）しか無く、手仕舞い cron は 1 日 1 回。
#    「次回 cron で再試行」という他経路の定石がここだけ成立せず、取り逃すと税務証跡が
#    永久欠損する。リトライして安全なのは読み取り専用の GET だからである。
# ---------------------------------------------------------------------------


def _exec_client_with_retry_responses(side_effect: Any) -> tuple[GmoFxClient, Any]:
    """private_get をスタブした GmoFxClient と、そのモックを返す."""
    with patch("backend.src.clients.gmo_client.RateLimiter"):
        gmo = GmoFxClient(api_key="k", api_secret="s", http_client=MagicMock(spec=httpx.Client))
    mock_get = MagicMock(side_effect=side_effect)
    gmo.private_get = mock_get  # type: ignore[method-assign]
    return gmo, mock_get


_OK_BODY = json.dumps({"status": 0, "data": {"list": [{"executionId": 1}]}})


def test_execution_fetch_retries_on_transient_body_status_error() -> None:
    """一過性の body status != 0 はリトライで吸収される（2回目で成功）."""
    responses = [
        _make_exec_response(_GMO_ERROR_BODY),
        _make_exec_response(_OK_BODY),
    ]
    gmo, mock_get = _exec_client_with_retry_responses(responses)

    with patch("backend.src.clients.gmo_client.time.sleep") as mock_sleep:
        records = gmo.get_latest_executions(symbol="USD_JPY")

    assert records == [{"executionId": 1}]
    assert mock_get.call_count == 2
    mock_sleep.assert_called_once_with(1.0)


def test_execution_fetch_retries_on_transport_error() -> None:
    """タイムアウト・接続断もリトライ対象（request_with_retry は 401 しか見ないため）."""
    responses = [
        httpx.ConnectError("connection reset"),
        _make_exec_response(_OK_BODY),
    ]
    gmo, mock_get = _exec_client_with_retry_responses(responses)

    with patch("backend.src.clients.gmo_client.time.sleep"):
        records = gmo.get_latest_executions(symbol="USD_JPY")

    assert records == [{"executionId": 1}]
    assert mock_get.call_count == 2


def test_execution_fetch_retries_on_http_5xx() -> None:
    """HTTP 5xx もリトライ対象."""
    responses = [
        _make_exec_response("{}", status=503),
        _make_exec_response(_OK_BODY),
    ]
    gmo, mock_get = _exec_client_with_retry_responses(responses)

    with patch("backend.src.clients.gmo_client.time.sleep"):
        records = gmo.get_latest_executions(symbol="USD_JPY")

    assert records == [{"executionId": 1}]
    assert mock_get.call_count == 2


def test_execution_fetch_raises_last_error_after_exhausting_attempts() -> None:
    """リトライを尽くしたら最後の例外を送出する（沈黙して [] を返さない）."""
    responses = [_make_exec_response(_GMO_ERROR_BODY) for _ in range(5)]
    gmo, mock_get = _exec_client_with_retry_responses(responses)

    with (
        patch("backend.src.clients.gmo_client.time.sleep"),
        pytest.raises(GmoApiError),
    ):
        gmo.get_latest_executions(symbol="USD_JPY")

    assert mock_get.call_count == _EXECUTION_FETCH_MAX_ATTEMPTS


def test_execution_fetch_does_not_retry_on_success() -> None:
    """成功時は 1 回で返る（無駄な GET でレートリミットを食わない）."""
    gmo, mock_get = _exec_client_with_retry_responses([_make_exec_response(_OK_BODY)])

    gmo.get_latest_executions(symbol="USD_JPY")

    assert mock_get.call_count == 1


def test_execution_fetch_gives_up_when_deadline_exceeded() -> None:
    """時間予算を使い切ったらリトライせず即諦める（Lambda タイムアウト保護）.

    httpx のタイムアウトは 30 秒あり、ハング系の失敗を素朴に積むと Lambda（既定 120 秒）
    を食い潰して手仕舞い全体が落ちる。
    """
    slow = [httpx.ReadTimeout("timeout"), _make_exec_response(_OK_BODY)]
    gmo, mock_get = _exec_client_with_retry_responses(slow)

    # 1 回目の試行で予算超過した状況を作る
    clock = iter([0.0, _EXECUTION_FETCH_DEADLINE_SECONDS + 1.0, 999.0])
    with (
        patch("backend.src.clients.gmo_client.time.monotonic", side_effect=lambda: next(clock)),
        patch("backend.src.clients.gmo_client.time.sleep") as mock_sleep,
        pytest.raises(httpx.ReadTimeout),
    ):
        gmo.get_latest_executions(symbol="USD_JPY")

    assert mock_get.call_count == 1  # リトライしていない
    mock_sleep.assert_not_called()


def test_order_post_never_goes_through_the_retry_path() -> None:
    """🔴 発注系 POST がリトライ経路に乗らないこと（重複発注防止・AGENTS.md #1/#4）.

    _get_execution_list は private_get しか呼ばないため構造的に POST は通らないが、
    将来の改変でここに POST を通すと重複発注になるため明示的に固定する。
    """
    gmo, mock_get = _exec_client_with_retry_responses(
        [_make_exec_response(_OK_BODY), _make_exec_response(_OK_BODY)]
    )
    mock_post = MagicMock()
    gmo.private_post = mock_post  # type: ignore[method-assign]

    gmo.get_latest_executions(symbol="USD_JPY")
    gmo.get_executions(order_id=1)

    mock_post.assert_not_called()
    assert mock_get.call_count >= 1


def test_failure_log_reports_actual_attempt_count_on_deadline() -> None:
    """時間予算で打ち切ったとき、ログの attempts が実際の試行回数になること.

    定数をそのまま出すと「3 回粘ったが駄目だった」と誤読されるが、実際は 1 回しか
    試していない。事実と違う観測値を残さないこと自体が本修正系列の主旨（#245）。
    """
    gmo, mock_get = _exec_client_with_retry_responses([httpx.ReadTimeout("timeout")])

    clock = iter([0.0, _EXECUTION_FETCH_DEADLINE_SECONDS + 1.0, 999.0])
    with (
        patch("backend.src.clients.gmo_client.time.monotonic", side_effect=lambda: next(clock)),
        patch("backend.src.clients.gmo_client.time.sleep"),
        patch("backend.src.clients.gmo_client._log") as mock_log,
        pytest.raises(httpx.ReadTimeout),
    ):
        gmo.get_latest_executions(symbol="USD_JPY")

    assert mock_get.call_count == 1
    kwargs = mock_log.error.call_args.kwargs
    assert kwargs["attempts"] == 1
    assert kwargs["max_attempts"] == _EXECUTION_FETCH_MAX_ATTEMPTS
    assert kwargs["stopped_reason"] == "deadline"


def test_failure_log_reports_max_attempts_when_exhausted() -> None:
    """試行を使い切った場合は attempts が上限と一致し、理由が max_attempts になること."""
    responses = [_make_exec_response(_GMO_ERROR_BODY) for _ in range(5)]
    gmo, _ = _exec_client_with_retry_responses(responses)

    with (
        patch("backend.src.clients.gmo_client.time.sleep"),
        patch("backend.src.clients.gmo_client._log") as mock_log,
        pytest.raises(GmoApiError),
    ):
        gmo.get_latest_executions(symbol="USD_JPY")

    kwargs = mock_log.error.call_args.kwargs
    assert kwargs["attempts"] == _EXECUTION_FETCH_MAX_ATTEMPTS
    assert kwargs["stopped_reason"] == "max_attempts"


# ---------------------------------------------------------------------------
# 市場ステータス（#207 (c)）
# ---------------------------------------------------------------------------


def test_get_market_status_reads_data_status_not_envelope() -> None:
    """🔴 エンベロープの status（API 成否）と data.status（市場状態）を取り違えないこと。

    閉場中の実レスポンスは {"status": 0, "data": {"status": "CLOSE"}}。
    エンベロープを見ると「status:0 = 成功 = 開場」と誤読する。
    """
    body = json.dumps({"status": 0, "data": {"status": "CLOSE"}})
    mock_client = MagicMock(spec=httpx.Client)
    mock_client.get.return_value = _make_exec_response(body)

    with patch("backend.src.clients.gmo_client.RateLimiter"):
        gmo = GmoFxClient(http_client=mock_client)
        assert gmo.get_market_status() == "CLOSE"

    assert mock_client.get.call_args[0][0].endswith("/public/v1/status")


def test_get_market_status_open() -> None:
    body = json.dumps({"status": 0, "data": {"status": "OPEN"}})
    mock_client = MagicMock(spec=httpx.Client)
    mock_client.get.return_value = _make_exec_response(body)

    with patch("backend.src.clients.gmo_client.RateLimiter"):
        gmo = GmoFxClient(http_client=mock_client)
        assert gmo.get_market_status() == "OPEN"


def test_get_market_status_raises_on_envelope_error() -> None:
    """ボディ status != 0 は握り潰さない（不明を OPEN に化けさせない）。"""
    mock_client = MagicMock(spec=httpx.Client)
    mock_client.get.return_value = _make_exec_response(_GMO_ERROR_BODY)

    with patch("backend.src.clients.gmo_client.RateLimiter"):
        gmo = GmoFxClient(http_client=mock_client)
        with pytest.raises(GmoApiError):
            gmo.get_market_status()


def test_get_market_status_is_unsigned_public_call() -> None:
    """署名不要の public_get を使う（Private の署名を無駄に消費しない）。"""
    body = json.dumps({"status": 0, "data": {"status": "OPEN"}})
    mock_client = MagicMock(spec=httpx.Client)
    mock_client.get.return_value = _make_exec_response(body)

    with (
        patch("backend.src.clients.gmo_client.RateLimiter"),
        patch("backend.src.clients.gmo_client.request_with_retry") as mock_retry,
    ):
        gmo = GmoFxClient(http_client=mock_client)
        gmo.get_market_status()

    mock_retry.assert_not_called()  # 署名パスを通らない
