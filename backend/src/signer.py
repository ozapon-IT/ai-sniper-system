"""GMO FX API HMAC-SHA256 署名ユーティリティ (§7.2).

署名対象テキストのパスは /private を含めず /v1/... から始める（Critical）。
request_path と sign_path は必ず別引数で受け取る設計。
"""

import hashlib
import hmac
import time
from typing import Literal


def _timestamp_ms() -> str:
    """現在時刻をミリ秒 UNIX タイムスタンプ（文字列）で返す."""
    return str(int(time.time() * 1000))


def build_headers(
    *,
    method: Literal["GET", "POST"],
    sign_path: str,
    api_key: str,
    api_secret: str,
    body: str = "",
) -> dict[str, str]:
    """API-KEY / API-TIMESTAMP / API-SIGN ヘッダを組み立てる.

    Args:
        method: HTTP メソッド（"GET" または "POST"）。
        sign_path: HMAC-SHA256 署名対象パス（/v1/... から始まる）。
                   /private を含めてはならない（§7.2 Critical）。
        api_key: GMO FX API Key。
        api_secret: GMO FX API Secret。
        body: POST の場合はリクエストボディ JSON 文字列。GET の場合は空文字。

    Raises:
        ValueError: sign_path が /private で始まる場合（署名パス取り違えを早期検出）。
    """
    if not sign_path.startswith("/v1/"):
        raise ValueError(
            f"sign_path は /v1/... から始まる必要があります: {sign_path!r}。"
            "to_sign_path() で変換してから渡すこと (§7.2)。"
        )

    # タイムスタンプは HTTP 送信直前に取得する（コールドスタート対策 §7.2）。
    timestamp = _timestamp_ms()
    text = timestamp + method + sign_path + body
    sign = hmac.new(
        api_secret.encode(),
        text.encode(),
        hashlib.sha256,
    ).hexdigest()

    return {
        "API-KEY": api_key,
        "API-TIMESTAMP": timestamp,
        "API-SIGN": sign,
        "Content-Type": "application/json",
    }
