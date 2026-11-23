"""署名パスユニットテスト — CI 必須 (§7.1, §7.2).

§1.4 定義表の全 Private エンドポイントについて URL ↔ 署名パスの対応を検証する。
"""

import hashlib
import hmac

import pytest

from backend.src.gmo_fx_endpoints import (
    PRIVATE_ACCOUNT_ASSETS,
    PRIVATE_ACTIVE_ORDERS,
    PRIVATE_CANCEL_ORDERS,
    PRIVATE_CLOSE_ORDER,
    PRIVATE_EXECUTIONS,
    PRIVATE_IFO_ORDER,
    PRIVATE_LATEST_EXECUTIONS,
    PRIVATE_OPEN_POSITIONS,
    to_sign_path,
)
from backend.src.signer import build_headers


# §1.4 定義表の全 Private エンドポイント
@pytest.mark.parametrize(
    "request_path, expected_sign_path",
    [
        (PRIVATE_IFO_ORDER, "/v1/ifoOrder"),
        (PRIVATE_ACCOUNT_ASSETS, "/v1/account/assets"),
        (PRIVATE_EXECUTIONS, "/v1/executions"),
        (PRIVATE_LATEST_EXECUTIONS, "/v1/latestExecutions"),
        (PRIVATE_ACTIVE_ORDERS, "/v1/activeOrders"),
        (PRIVATE_CANCEL_ORDERS, "/v1/cancelOrders"),
        (PRIVATE_CLOSE_ORDER, "/v1/closeOrder"),
        (PRIVATE_OPEN_POSITIONS, "/v1/openPositions"),
    ],
)
def test_all_private_endpoints_sign_path(request_path: str, expected_sign_path: str) -> None:
    """全 Private エンドポイントで署名パスが /private を含まず /v1/... から始まること (§1.4, §7.2)."""
    sign_path = to_sign_path(request_path)
    assert sign_path == expected_sign_path
    assert not sign_path.startswith("/private")


def test_ifo_order_sign_path_is_v1_ifo_order() -> None:
    """ifoOrder の署名パスが /v1/ifoOrder であることを明示 assert (§7.1)."""
    assert to_sign_path(PRIVATE_IFO_ORDER) == "/v1/ifoOrder"
    assert to_sign_path(PRIVATE_IFO_ORDER) != "/private/v1/ifoOrder"


def test_sign_path_rejects_private_prefix() -> None:
    """to_sign_path に /private で始まらないパスを渡すと ValueError (フェイルファスト §7.2)."""
    with pytest.raises(ValueError, match="/private"):
        to_sign_path("/v1/ifoOrder")  # すでに変換済みのパスを再度渡す


def test_build_headers_rejects_non_v1_sign_path() -> None:
    """build_headers に /v1/ 以外で始まる sign_path を渡すと ValueError."""
    for bad_path in ["/private/v1/ifoOrder", "/public/v1/klines", "/ifoOrder"]:
        with pytest.raises(ValueError, match=r"/v1/"):
            build_headers(
                method="POST",
                sign_path=bad_path,
                api_key="key",
                api_secret="secret",
            )


def test_build_headers_contains_required_keys() -> None:
    """build_headers が API-KEY / API-TIMESTAMP / API-SIGN / Content-Type を含む."""
    headers = build_headers(
        method="GET",
        sign_path="/v1/account/assets",
        api_key="test-key",
        api_secret="test-secret",
    )
    assert "API-KEY" in headers
    assert "API-TIMESTAMP" in headers
    assert "API-SIGN" in headers
    assert headers["API-KEY"] == "test-key"
    assert headers["Content-Type"] == "application/json"


def test_build_headers_sign_is_hmac_sha256() -> None:
    """API-SIGN が正しい HMAC-SHA256 値であること."""
    api_key = "mykey"
    api_secret = "mysecret"
    sign_path = "/v1/ifoOrder"
    body = '{"symbol":"USD_JPY"}'

    headers = build_headers(
        method="POST",
        sign_path=sign_path,
        api_key=api_key,
        api_secret=api_secret,
        body=body,
    )

    timestamp = headers["API-TIMESTAMP"]
    text = timestamp + "POST" + sign_path + body
    expected_sign = hmac.new(
        api_secret.encode(),
        text.encode(),
        hashlib.sha256,
    ).hexdigest()
    assert headers["API-SIGN"] == expected_sign
