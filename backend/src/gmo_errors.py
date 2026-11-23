"""GMO FX API エラーエンベロープの構造化パース（§1.6, 付録B E04/E06）.

GMO Private API は業務エラーでも HTTP 200 を返し、ボディの ``status`` で成否を表す
（付録B: docs/spikes/E04_assets_response.md §1, E06_ifoOrder_mapping.md §1 で実確認済み）::

    {"status": 1,
     "messages": [{"message_code": "ERR-5012", "message_string": "..."}],
     "responsetime": "..."}

エラーコードは ``messages[].message_code``（``ERR-XXXX`` 形式）の構造化フィールドで
伝播させ、例外文字列への部分一致では判定しない（Issue #136: 4桁コードの素の部分一致は
注文 ID・サイズ等の数値断片と偶然一致し、決済失敗を偽成功として吸収する）。
"""

from __future__ import annotations

from typing import Any


class GmoApiError(RuntimeError):
    """GMO API がボディ ``status != 0`` を返した場合の業務エラー.

    Attributes:
        status: レスポンスボディの ``status`` 値。
        message_codes: ``messages[].message_code`` の一覧（例 ``("ERR-5012",)``）。
            呼び出し側はこの構造化フィールドへの**完全一致**で分類する。
            例外文字列への部分一致判定は禁止（Issue #136）。
    """

    def __init__(self, message: str, *, status: int | None, message_codes: tuple[str, ...]) -> None:
        super().__init__(message)
        self.status = status
        self.message_codes = message_codes


def extract_message_codes(body: Any) -> tuple[str, ...]:
    """レスポンスボディから ``messages[].message_code`` を抽出する.

    Args:
        body: パース済みレスポンスボディ（dict 想定。それ以外は空タプル）。

    Returns:
        message_code のタプル。形式不明な要素はスキップする。
    """
    if not isinstance(body, dict):
        return ()
    messages = body.get("messages")
    if not isinstance(messages, list):
        return ()
    codes: list[str] = []
    for msg in messages:
        if isinstance(msg, dict) and isinstance(msg.get("message_code"), str):
            codes.append(msg["message_code"])
    return tuple(codes)


def _parse_body(resp: Any) -> Any:
    """httpx.Response / dict の両方からボディ dict を取り出す（モック互換）.

    JSON デコード失敗（ValueError / json.JSONDecodeError）のみ None に倒す。
    それ以外の例外は実装バグの可能性があるため握りつぶさず伝播させる
    （広く捕捉すると検査自体がスキップされ、業務エラーの見逃しにつながる）。
    """
    if hasattr(resp, "json"):
        try:
            return resp.json()
        except ValueError:  # json.JSONDecodeError は ValueError のサブクラス
            return None
    return resp


def raise_for_gmo_status(resp: Any, *, context: str) -> None:
    """GMO レスポンスボディの ``status != 0`` を検査し、構造化例外を送出する.

    HTTP 200 でも業務エラーがあり得るため、HTTP 層の検査とは独立に呼ぶ
    （付録B E04 §1「HTTP 200 でも status を必ず見る」）。

    ``status`` キーが存在しない場合は検査しない（テスト用モックが ``data`` のみを
    返す場合との互換。実 GMO レスポンスには常に ``status`` が含まれる）。

    Args:
        resp: httpx.Response またはパース済み dict。
        context: エラーメッセージに含める呼び出し文脈（例 ``"closeOrder"``）。

    Raises:
        GmoApiError: ``status`` が存在し 0 以外の場合。
    """
    body = _parse_body(resp)
    if not isinstance(body, dict):
        return
    status = body.get("status")
    if status is None or status == 0:
        return
    codes = extract_message_codes(body)
    strings = [
        msg.get("message_string", "") for msg in body.get("messages", []) if isinstance(msg, dict)
    ]
    raise GmoApiError(
        f"GMO API error ({context}): status={status} codes={list(codes)} messages={strings}",
        status=status if isinstance(status, int) else None,
        message_codes=codes,
    )
