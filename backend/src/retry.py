"""GMO FX API 401 再署名リトライ (§7.2).

401 検知時にタイムスタンプ再取得＋再署名後、exactly 1 回リトライする。
2 回目 401 は ForexAuthError として即時送出する。
"""

import time
from collections.abc import Callable

import httpx

from backend.src.gmo_fx_endpoints import to_sign_path
from backend.src.signer import build_headers


class ForexAuthError(Exception):
    """401 リトライが尽きた場合に送出する認証エラー."""


def request_with_retry(
    *,
    client: httpx.Client,
    method: str,
    url: str,
    request_path: str,
    api_key: str,
    api_secret: str,
    body: str = "",
    sign_path: str | None = None,
    pre_attempt_hook: Callable[[], None] | None = None,
    post_attempt_hook: Callable[[], None] | None = None,
    **kwargs: object,
) -> httpx.Response:
    """署名ヘッダを付与して HTTP リクエストを送信し、401 時に 1 回だけ再署名リトライする.

    Args:
        client: httpx.Client インスタンス。
        method: HTTP メソッド（"GET" / "POST"）。
        url: フル URL（ホスト含む）。
        request_path: リクエスト URL パス（/private/v1/...）。
        api_key: GMO FX API Key。
        api_secret: GMO FX API Secret。
        body: POST ボディ JSON 文字列。GET の場合は空文字。
        sign_path: HMAC 署名パス（/v1/...）。省略時は to_sign_path(request_path) で導出。
                   呼び出し元が to_sign_path を明示的に適用済みの場合はここに渡す（§7.2）。
        pre_attempt_hook: 各試行（初回・リトライ）の直前に呼ばれるコールバック。
                          レートリミット wait を試行ごとに挟むために使う。
        post_attempt_hook: 各試行の応答受信後に呼ばれるコールバック。
                           POST の場合は mark_post_done() を渡し、応答受信後にタイマーを更新する。
        **kwargs: httpx.Client.request に転送する追加引数。

    Returns:
        httpx.Response

    Raises:
        ForexAuthError: 1 回リトライ後も 401 の場合。
    """
    resolved_sign_path = sign_path if sign_path is not None else to_sign_path(request_path)
    m = method.upper()

    def _do_request() -> httpx.Response:
        if pre_attempt_hook is not None:
            pre_attempt_hook()
        headers = build_headers(
            method=m,  # type: ignore[arg-type]
            sign_path=resolved_sign_path,
            api_key=api_key,
            api_secret=api_secret,
            body=body,
        )
        try:
            if body:
                resp = client.request(m, url, headers=headers, content=body, **kwargs)  # type: ignore[arg-type]
            else:
                resp = client.request(m, url, headers=headers, **kwargs)  # type: ignore[arg-type]
            return resp
        finally:
            # 例外（タイムアウト・接続断）でも POST タイマーを進め、即時再送を防ぐ (AGENTS.md #3)
            if m == "POST" and post_attempt_hook is not None:
                post_attempt_hook()

    response = _do_request()
    if response.status_code != 401:
        return response

    # 401: POST はレートリミット遵守のため 1 秒待機してから再署名リトライ (§7.2, AGENTS.md #3)
    if m == "POST":
        time.sleep(1)
    response = _do_request()
    if response.status_code == 401:
        raise ForexAuthError(
            f"GMO FX API 認証失敗（リトライ後も 401）: {method} {url}"
        )
    return response
