"""決済・キャンセル系リクエストモデルのユニットテスト（Issue #230）.

公式仕様（出典: https://api.coin.z.com/fxdocs/・2026-07-30 参照 /
付録B docs/spikes/cancel_first_official_spec.md）に対する型レベルの回帰ガード。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from backend.src.models import (
    MAX_CANCEL_ROOT_ORDER_IDS,
    MAX_SETTLE_POSITIONS,
    CancelOrdersRequest,
    CloseOrderRequest,
    CloseOrderResponse,
    SettlePositionItem,
)


def _wire(model: CloseOrderRequest | CancelOrdersRequest) -> dict[str, Any]:
    """実送信される JSON 相当の dict."""
    parsed: dict[str, Any] = json.loads(model.model_dump_json(by_alias=True))
    return parsed


# ---------------------------------------------------------------------------
# CloseOrderRequest
# ---------------------------------------------------------------------------


class TestCloseOrderRequest:
    def test_matches_official_request_example(self) -> None:
        """公式リクエスト例と同じキー・型でシリアライズされること."""
        body = CloseOrderRequest(
            side="SELL", settlePosition=[{"positionId": 1000342, "size": "10000"}]
        )
        assert _wire(body) == {
            "symbol": "USD_JPY",
            "side": "SELL",
            "executionType": "MARKET",
            "settlePosition": [{"positionId": 1000342, "size": "10000"}],
        }

    def test_has_no_top_level_size_field(self) -> None:
        """トップレベル size フィールドを持たないこと（size と settlePosition は排他）.

        公式: 「size settlePosition いずれか1つが必須です。2つ同時には設定できません。」
        フィールド自体を定義しないことで ERR-5214 を構造的に不可能にする。
        """
        assert "size" not in CloseOrderRequest.model_fields

    def test_has_no_top_level_position_id_field(self) -> None:
        """トップレベル positionId フィールドを持たないこと（公式仕様に存在しない）."""
        assert "position_id" not in CloseOrderRequest.model_fields
        assert "positionId" not in CloseOrderRequest.model_fields

    def test_extra_top_level_size_is_not_serialized(self) -> None:
        """size を渡してもボディに現れないこと（ERR-5214 の回帰ガード）."""
        body = CloseOrderRequest(
            side="SELL",
            size="100",  # type: ignore[call-arg]
            settlePosition=[{"positionId": 1000342, "size": "100"}],
        )
        assert "size" not in _wire(body)

    def test_no_null_fields_serialized(self) -> None:
        """未使用 Optional を定義していないため null を実送信しないこと（ERR-5106 対策）."""
        body = CloseOrderRequest(
            side="BUY", settlePosition=[{"positionId": 1, "size": "100"}]
        )
        assert "null" not in body.model_dump_json(by_alias=True)

    def test_rejects_empty_settle_position(self) -> None:
        with pytest.raises(ValidationError):
            CloseOrderRequest(side="SELL", settlePosition=[])

    def test_rejects_more_than_official_limit(self) -> None:
        """settlePosition は最大10件（公式）を超えたら拒否すること."""
        items = [
            {"positionId": 1000000 + i, "size": "100"}
            for i in range(MAX_SETTLE_POSITIONS + 1)
        ]
        with pytest.raises(ValidationError):
            CloseOrderRequest(side="SELL", settlePosition=items)

    def test_accepts_exactly_official_limit(self) -> None:
        items = [
            {"positionId": 1000000 + i, "size": "100"}
            for i in range(MAX_SETTLE_POSITIONS)
        ]
        body = CloseOrderRequest(side="SELL", settlePosition=items)
        assert len(body.settle_position) == MAX_SETTLE_POSITIONS

    def test_rejects_duplicate_position_ids(self) -> None:
        """同一建玉IDの重複を拒否すること（ERR-512）."""
        with pytest.raises(ValidationError):
            CloseOrderRequest(
                side="SELL",
                settlePosition=[
                    {"positionId": 1000342, "size": "100"},
                    {"positionId": 1000342, "size": "100"},
                ],
            )

    def test_rejects_non_market_execution_type(self) -> None:
        """本システムは成行決済のみ（LIMIT/OCO は limitPrice 必須で別ルールになる）."""
        with pytest.raises(ValidationError):
            CloseOrderRequest(
                side="SELL",
                executionType="LIMIT",
                settlePosition=[{"positionId": 1, "size": "100"}],
            )


class TestSettlePositionItem:
    def test_position_id_is_normalized_to_int(self) -> None:
        """公式が number なので数値文字列も int に正規化されること."""
        item = SettlePositionItem(positionId="1000342", size="100")
        assert item.position_id == 1000342
        assert isinstance(item.position_id, int)

    def test_non_numeric_position_id_is_rejected(self) -> None:
        """非数値の建玉IDは POST 前に落ちること（ERR-5106 の往復を待たない）."""
        with pytest.raises(ValidationError):
            SettlePositionItem(positionId="pos-1", size="100")

    def test_size_must_be_string(self) -> None:
        """公式は string。数値を渡したら型を推測せず拒否すること."""
        with pytest.raises(ValidationError):
            SettlePositionItem(positionId=1000342, size=100)

    def test_serializes_with_official_aliases(self) -> None:
        item = SettlePositionItem(positionId=1000342, size="100")
        assert json.loads(item.model_dump_json(by_alias=True)) == {
            "positionId": 1000342,
            "size": "100",
        }


# ---------------------------------------------------------------------------
# CancelOrdersRequest
# ---------------------------------------------------------------------------


class TestCancelOrdersRequest:
    def test_matches_official_request_example(self) -> None:
        """公式リクエスト例 `{"rootOrderIds": [123456789, 223456789]}` と一致すること."""
        body = CancelOrdersRequest(rootOrderIds=[123456789, 223456789])
        assert _wire(body) == {"rootOrderIds": [123456789, 223456789]}

    def test_has_no_order_id_field(self) -> None:
        """`orderId` フィールドを持たないこと（公式仕様に存在しない・Issue #230）."""
        assert "order_id" not in CancelOrdersRequest.model_fields
        assert "orderId" not in CancelOrdersRequest.model_fields

    def test_root_order_ids_normalized_to_int(self) -> None:
        body = CancelOrdersRequest(rootOrderIds=["123456789"])
        assert body.root_order_ids == [123456789]

    def test_rejects_non_numeric_id(self) -> None:
        with pytest.raises(ValidationError):
            CancelOrdersRequest(rootOrderIds=["ord-1"])

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            CancelOrdersRequest(rootOrderIds=[])

    def test_rejects_more_than_official_limit(self) -> None:
        """cancelOrders は最大10件（公式）."""
        with pytest.raises(ValidationError):
            CancelOrdersRequest(
                rootOrderIds=list(range(1, MAX_CANCEL_ROOT_ORDER_IDS + 2))
            )

    def test_rejects_duplicates(self) -> None:
        with pytest.raises(ValidationError):
            CancelOrdersRequest(rootOrderIds=[123456789, 123456789])


# ---------------------------------------------------------------------------
# CloseOrderResponse（data は配列・status:"WAITING" は約定ではない）
# ---------------------------------------------------------------------------


class TestCloseOrderResponse:
    def test_parses_official_response_example(self) -> None:
        """公式レスポンス例（data は配列）をパースできること."""
        payload = {
            "status": 0,
            "data": [
                {
                    "rootOrderId": 123456789,
                    "clientOrderId": "abc123",
                    "orderId": 123456789,
                    "symbol": "USD_JPY",
                    "side": "BUY",
                    "orderType": "NORMAL",
                    "executionType": "LIMIT",
                    "settleType": "CLOSE",
                    "size": "10000",
                    "price": "135.5",
                    "status": "WAITING",
                    "expiry": "20230418",
                    "timestamp": "2019-03-19T01:07:24.467Z",
                }
            ],
            "responsetime": "2019-03-19T01:07:24.557Z",
        }
        parsed = CloseOrderResponse.model_validate(payload)
        assert parsed.data is not None
        item = parsed.data[0]
        # POST 成功は「受付」であり約定確認ではない（§付録B）
        assert item.status == "WAITING"
        assert item.settle_type == "CLOSE"
        assert item.order_id == 123456789
