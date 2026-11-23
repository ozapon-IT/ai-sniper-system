"""ifoOrder のリクエスト/レスポンス実仕様テスト（2026-08-03 実発注スパイク・#91）。"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from backend.src.models import IfoOrderRequest, IfoOrderResponse

# 2026-08-03 の実発注スパイクで確認したレスポンスと同じ形のデータ。
# 公開版では注文 ID・時刻を架空の値に置き換えている。
_REAL_SUCCESS_RESPONSE = {
    "status": 0,
    "data": [
        {
            "executionType": "LIMIT",
            "expiry": "20260902",
            "orderId": 1000101,
            "orderType": "IFDOCO",
            "price": "156.428",
            "rootOrderId": 1000101,
            "settleType": "OPEN",
            "side": "BUY",
            "size": "100",
            "status": "WAITING",
            "symbol": "USD_JPY",
            "timestamp": "2026-08-03T06:00:00.000Z",
        },
        {
            "executionType": "LIMIT",
            "expiry": "20260902",
            "orderId": 1000102,
            "orderType": "IFDOCO",
            "price": "156.928",
            "rootOrderId": 1000101,
            "settleType": "CLOSE",
            "side": "SELL",
            "size": "100",
            "status": "WAITING",
            "symbol": "USD_JPY",
            "timestamp": "2026-08-03T06:00:00.000Z",
        },
        {
            "executionType": "STOP",
            "expiry": "20260902",
            "orderId": 1000103,
            "orderType": "IFDOCO",
            "price": "155.928",
            "rootOrderId": 1000101,
            "settleType": "CLOSE",
            "side": "SELL",
            "size": "100",
            "status": "WAITING",
            "symbol": "USD_JPY",
            "timestamp": "2026-08-03T06:00:00.000Z",
        },
    ],
    "responsetime": "2026-08-03T06:00:00.010Z",
}


def _request(**overrides: object) -> IfoOrderRequest:
    body: dict[str, object] = {
        "symbol": "USD_JPY",
        "firstSide": "BUY",
        "firstExecutionType": "LIMIT",
        "firstPrice": "156.400",
        "firstSize": 100,
        "secondSize": 100,
        "secondLimitPrice": "156.800",
        "secondStopPrice": "156.000",
    }
    body.update(overrides)
    return IfoOrderRequest(**body)  # type: ignore[arg-type]


class TestSizeSerializedAsIntegerString:
    """size は整数文字列で送る。JSON number だと ERR-5105 で発注が1件も通らない。"""

    def test_size_is_string_on_the_wire(self) -> None:
        wire = json.loads(_request().model_dump_json(by_alias=True))
        assert wire["firstSize"] == "100"
        assert wire["secondSize"] == "100"
        assert isinstance(wire["firstSize"], str)
        assert isinstance(wire["secondSize"], str)

    def test_prices_stay_strings(self) -> None:
        wire = json.loads(_request().model_dump_json(by_alias=True))
        for key in ("firstPrice", "secondLimitPrice", "secondStopPrice"):
            assert isinstance(wire[key], str)

    def test_python_side_stays_int_so_lot_validation_still_applies(self) -> None:
        """str 化はワイヤーのみ。ロット下限100・100倍数の検証は int のまま効く。"""
        assert _request().first_size == 100
        with pytest.raises(ValidationError, match="最小 100 通貨"):
            _request(firstSize=50)
        with pytest.raises(ValidationError, match="100 の倍数"):
            _request(firstSize=150)


class TestSuccessResponseIsOrderList:
    """data は注文オブジェクトの配列。従来の str 推測は誤りだった。"""

    def test_parses_real_spike_response(self) -> None:
        parsed = IfoOrderResponse.model_validate(_REAL_SUCCESS_RESPONSE)
        assert parsed.status == 0
        assert parsed.data is not None
        assert len(parsed.data) == 3

    def test_root_order_id_is_shared_by_all_three(self) -> None:
        parsed = IfoOrderResponse.model_validate(_REAL_SUCCESS_RESPONSE)
        assert parsed.data is not None
        assert {o.root_order_id for o in parsed.data} == {1000101}
        assert parsed.root_order_id() == 1000101

    def test_entry_order_id_equals_root_order_id(self) -> None:
        parsed = IfoOrderResponse.model_validate(_REAL_SUCCESS_RESPONSE)
        assert parsed.data is not None
        entry = next(o for o in parsed.data if o.settle_type == "OPEN")
        assert entry.order_id == entry.root_order_id == 1000101

    def test_settle_types_are_one_open_and_two_close(self) -> None:
        parsed = IfoOrderResponse.model_validate(_REAL_SUCCESS_RESPONSE)
        assert parsed.data is not None
        assert sorted(o.settle_type for o in parsed.data) == ["CLOSE", "CLOSE", "OPEN"]

    def test_root_order_id_is_none_when_data_absent(self) -> None:
        parsed = IfoOrderResponse.model_validate({"status": 0, "responsetime": "x"})
        assert parsed.root_order_id() is None

    def test_legacy_string_data_is_rejected(self) -> None:
        """旧推測形（data が注文ID直値の str）は受け付けない — 実仕様と違うため。"""
        with pytest.raises(ValidationError):
            IfoOrderResponse.model_validate(
                {"status": 0, "responsetime": "x", "data": "1000101"}
            )


class TestRootOrderIdInvariant:
    """3 件は同一 rootOrderId を共有する — ドキュメント上の不変条件をコードで強制する。"""

    def test_mismatched_root_order_ids_are_rejected(self) -> None:
        broken = [dict(o) for o in _REAL_SUCCESS_RESPONSE["data"]]  # type: ignore[arg-type]
        broken[1]["rootOrderId"] = 99999999
        with pytest.raises(ValidationError, match="rootOrderId が注文間で一致しません"):
            IfoOrderResponse.model_validate(
                {"status": 0, "responsetime": "x", "data": broken}
            )

    def test_single_order_is_accepted(self) -> None:
        """1 件だけでも不変条件は満たす（closeOrder 等の単発レスポンス形に備える）。"""
        one = [dict(_REAL_SUCCESS_RESPONSE["data"][0])]  # type: ignore[index]
        parsed = IfoOrderResponse.model_validate({"status": 0, "responsetime": "x", "data": one})
        assert parsed.root_order_id() == 1000101
