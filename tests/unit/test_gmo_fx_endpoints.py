"""署名パス変換ユニットテスト — CI 必須 (§7.1, §7.2).

HMAC-SHA256 署名パスは /private を除いた /v1/... から始まること。
"""

import pytest

from backend.src.gmo_fx_endpoints import (
    GMO_FX_HOST,
    PRIVATE_IFO_ORDER,
    to_sign_path,
)


@pytest.mark.parametrize(
    "request_path, expected_sign_path",
    [
        ("/private/v1/ifoOrder", "/v1/ifoOrder"),
        ("/private/v1/cancelOrders", "/v1/cancelOrders"),
        ("/private/v1/closeOrder", "/v1/closeOrder"),
        ("/private/v1/account/assets", "/v1/account/assets"),
        ("/private/v1/activeOrders", "/v1/activeOrders"),
        ("/private/v1/openPositions", "/v1/openPositions"),
    ],
)
def test_sign_path_strips_private(request_path: str, expected_sign_path: str) -> None:
    """署名パスが /private を含まず /v1/... から始まること (§7.2)."""
    assert to_sign_path(request_path) == expected_sign_path


def test_sign_path_does_not_start_with_private() -> None:
    """/private を含む文字列が署名パスとして渡されないことを確認."""
    sign_path = to_sign_path(PRIVATE_IFO_ORDER)
    assert not sign_path.startswith("/private"), (
        f"署名パスに /private が含まれている: {sign_path}"
    )


def test_ifo_order_endpoint_name() -> None:
    """IFD-OCO は ifoOrder であり ifdocoOrder / ifdOrder ではない (§7.1)."""
    assert PRIVATE_IFO_ORDER == "/private/v1/ifoOrder"
    assert "ifdoco" not in PRIVATE_IFO_ORDER
    assert PRIVATE_IFO_ORDER != "/private/v1/ifdOrder"


def test_gmo_host() -> None:
    assert GMO_FX_HOST == "https://forex-api.coin.z.com"


def test_close_bulk_order_constant_is_absent() -> None:
    """closeBulkOrder の定数を持たないこと（FX API に該当エンドポイントは存在しない）.

    公式ドキュメント全文（https://api.coin.z.com/fxdocs/・2026-07-30 参照）で
    closeBulkOrder の出現は 0 件。これは暗号資産API側の名称であり、混同すると
    live 化時に手仕舞い・非常停止の Step3 が失敗する（Issue #230）。
    建玉の一括決済は closeOrder の settlePosition（最大10件）で行う。
    """
    import backend.src.gmo_fx_endpoints as endpoints

    assert not hasattr(endpoints, "PRIVATE_CLOSE_BULK_ORDER")
    bulk_paths = [
        name
        for name, value in vars(endpoints).items()
        if isinstance(value, str) and "closeBulk" in value
    ]
    assert bulk_paths == []
