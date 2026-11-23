"""GMO コイン FX API エンドポイント定数 (§7.2, §1.4).

リクエスト URL は /private/v1/... または /public/v1/...
HMAC-SHA256 署名パスは /v1/... から始める（/private を含めない）。
"""

GMO_FX_HOST = "https://forex-api.coin.z.com"

# --- Public endpoints ---
PUBLIC_SYMBOLS = "/public/v1/symbols"
# 外国為替FXステータス（署名不要）。``data.status`` が MAINTENANCE / CLOSE / OPEN。
# 🔴 エンベロープの ``status``（API 成否）と ``data.status``（市場状態）は**同名で別物**。
# 出典: https://api.coin.z.com/fxdocs/（2026-08-08 参照）
# 閉場中の実レスポンスは付録B docs/spikes/market_status_closed.md に記録。
PUBLIC_STATUS = "/public/v1/status"
PUBLIC_KLINES = "/public/v1/klines"
PUBLIC_TICKER = "/public/v1/ticker"
PUBLIC_ORDERBOOKS = "/public/v1/orderbooks"

# --- Private endpoints (リクエスト URL パス) ---
PRIVATE_ACCOUNT_ASSETS = "/private/v1/account/assets"
PRIVATE_ACTIVE_ORDERS = "/private/v1/activeOrders"
PRIVATE_OPEN_POSITIONS = "/private/v1/openPositions"
PRIVATE_EXECUTIONS = "/private/v1/executions"
PRIVATE_LATEST_EXECUTIONS = "/private/v1/latestExecutions"

# POST endpoints
PRIVATE_IFO_ORDER = "/private/v1/ifoOrder"       # IFD-OCO 注文（ifdocoOrder / ifdOrder ではない）
PRIVATE_CANCEL_ORDERS = "/private/v1/cancelOrders"
# 🔴 FX API に建玉一括決済エンドポイント（closeBulkOrder）は存在しない（Issue #230）。
#    公式ドキュメント全文で closeBulkOrder の出現は 0 件で、これは暗号資産API
#    （https://api.coin.z.com/docs/）側の名称。似た名前の cancelBulkOrder は
#    「注文の一括キャンセル」であって建玉決済ではない。
#    建玉の一括決済は closeOrder の settlePosition（最大10件/回）で行う。
#    PRIVATE_CLOSE_BULK_ORDER を再追加しないこと。
#    出典: https://api.coin.z.com/fxdocs/（2026-07-30 参照）
#    詳細: docs/spikes/cancel_first_official_spec.md（付録B）
PRIVATE_CLOSE_ORDER = "/private/v1/closeOrder"


def to_sign_path(request_path: str) -> str:
    """リクエスト URL パスから HMAC-SHA256 署名パスへ変換する (§7.2).

    /private/v1/ifoOrder -> /v1/ifoOrder
    /public/v1/klines    -> /v1/klines

    Raises:
        ValueError: /private または /public 以外のパスが渡された場合。
                    不正パスをそのまま署名すると全リクエスト 401 になるため fail-fast とする。
    """
    if request_path.startswith("/private"):
        return request_path[len("/private"):]
    if request_path.startswith("/public"):
        return request_path[len("/public"):]
    raise ValueError(
        f"to_sign_path: expected path starting with /private or /public, got {request_path!r}"
    )
