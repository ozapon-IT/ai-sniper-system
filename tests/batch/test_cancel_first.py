"""キャンセル・ファースト手仕舞いシーケンスのユニットテスト（§2, §6.2, AGENTS.md #3, #4）.

公式仕様（出典: https://api.coin.z.com/fxdocs/・2026-07-30 参照 / 付録B
docs/spikes/cancel_first_official_spec.md）に対する回帰ガードを含む:
  - cancelOrders は ``rootOrderIds``（親注文ID の number 配列・最大10件）。``orderId`` は存在しない
  - closeOrder は ``settlePosition``（最大10件）。トップレベル ``size`` / ``positionId`` は送らない
  - FX API に closeBulkOrder は存在しない（Issue #230）
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel

from backend.src.batch import cancel_first
from backend.src.batch.cancel_first import (
    _CLOSE_RETRY_MAX_ATTEMPTS,
    _POST_SLEEP_SECONDS,
    _RESIDUAL_POLL_ATTEMPTS,
    CancelFirstResult,
    CloseOrderTransportError,
    _position_key,
    run_cancel_first,
)
from backend.src.gmo_errors import GmoApiError
from backend.src.gmo_fx_endpoints import (
    PRIVATE_ACTIVE_ORDERS,
    PRIVATE_CANCEL_ORDERS,
    PRIVATE_CLOSE_ORDER,
    PRIVATE_OPEN_POSITIONS,
)
from backend.src.models import MAX_CANCEL_ROOT_ORDER_IDS, MAX_SETTLE_POSITIONS

# ---------------------------------------------------------------------------
# ボディ検査ヘルパー（実装は pydantic モデルを渡すため by_alias で dict 化する）
# ---------------------------------------------------------------------------


def _body_dict(body: Any) -> dict[str, Any]:
    """実送信される JSON 相当の dict を得る（BaseModel は by_alias でシリアライズ）."""
    if isinstance(body, BaseModel):
        parsed: dict[str, Any] = json.loads(body.model_dump_json(by_alias=True))
        return parsed
    return dict(body or {})


def _settle_position_ids(body: Any) -> list[str]:
    """closeOrder ボディの settlePosition から建玉ID（正規化キー）を取り出す."""
    items = _body_dict(body).get("settlePosition") or []
    return [
        _position_key(item.get("positionId"))
        for item in items
        if isinstance(item, dict)
    ]


# ---------------------------------------------------------------------------
# MockForexClient: Phase 1 でのスタブ実装
# ---------------------------------------------------------------------------


class MockForexClient:
    """Phase 1 モック: 呼び出し記録と状態遷移を保持する（AGENTS.md #1）。

    GmoFxClient.private_post は封じ込め中のため実送信しない。
    closeOrder を呼ぶと settlePosition に含まれる建玉が _positions から除かれ、
    決済後の再取得で残存建玉が正しく反映される（§6.2 Step3 の差分検証用）。
    """

    def __init__(
        self,
        *,
        positions: list[dict[str, Any]] | None = None,
        new_orders: list[dict[str, Any]] | None = None,
        settlement_orders: list[dict[str, Any]] | None = None,
        market_open: bool = True,
        market_status: str | None = None,
        event_log: list[str] | None = None,
    ) -> None:
        self._positions = list(positions or [])
        self._new_orders = list(new_orders or [])
        self._settlement_orders = list(settlement_orders or [])
        self._market_open = market_open
        # #207 (c): Step0 は公式ステータスも見る。既定は market_open に追随させ、
        # 既存テストの意図（「参照系 GET が落ちる＝閉場」）を保つ。
        self._market_status = (
            market_status if market_status is not None else ("OPEN" if market_open else "CLOSE")
        )
        self._event_log = event_log

        self.get_calls: list[tuple[str, dict[str, Any] | None]] = []
        self.post_calls: list[tuple[str, Any]] = []
        self.open_positions_get_count = 0

    def get_market_status(self) -> str:
        return self._market_status

    def private_get(
        self, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.get_calls.append((path, params))
        if path == PRIVATE_OPEN_POSITIONS:
            self.open_positions_get_count += 1
            if not self._market_open:
                raise ConnectionError("market closed or API unreachable")
            return {"status": 0, "data": list(self._positions)}
        # activeOrders: 新規(OPEN) + 決済(CLOSE) を合わせて返す
        orders = self._new_orders + self._settlement_orders
        return {"status": 0, "data": orders}

    def private_post(self, path: str, body: Any = None) -> dict[str, Any]:
        self.post_calls.append((path, body))
        if self._event_log is not None:
            self._event_log.append("post")
        if path == PRIVATE_CLOSE_ORDER:
            settled = set(_settle_position_ids(body))
            self._positions = [
                p
                for p in self._positions
                if _position_key(p.get("positionId")) not in settled
            ]
        return {"status": 0, "data": None}

    def post_call_paths(self) -> list[str]:
        """POST が呼ばれたパスのリスト（call order 確認用）."""
        return [p for p, _ in self.post_calls]

    def post_bodies(self, path: str) -> list[dict[str, Any]]:
        """指定パスへ送られたボディ（実送信 JSON 相当の dict）のリスト."""
        return [_body_dict(b) for p, b in self.post_calls if p == path]


# ---------------------------------------------------------------------------
# テストヘルパー
# ---------------------------------------------------------------------------

# 公式仕様に沿ったフィクスチャ: positionId / rootOrderId は number、size は string
_BUY_POSITION = {
    "positionId": 1000342,
    "symbol": "USD_JPY",
    "side": "BUY",
    "size": "100",
    "orderedSize": "0",
    "price": "150.000",
}
_SELL_POSITION = {
    "positionId": 1000343,
    "symbol": "USD_JPY",
    "side": "SELL",
    "size": "100",
    "orderedSize": "0",
    "price": "150.500",
}
_NEW_ORDER = {
    "rootOrderId": 123456789,
    "orderId": 987654321,
    "settleType": "OPEN",
    "status": "ORDERED",
}
_SETTLEMENT_ORDER = {
    "rootOrderId": 223456789,
    "orderId": 987654322,
    "settleType": "CLOSE",
    "status": "ORDERED",
}


def _make_positions(
    count: int, *, side: str = "BUY", start: int = 2000000
) -> list[dict[str, Any]]:
    """連番の建玉を生成する（チャンク分割検証用）."""
    return [
        {
            "positionId": start + i,
            "symbol": "USD_JPY",
            "side": side,
            "size": "100",
            "orderedSize": "0",
            "price": "150.000",
        }
        for i in range(count)
    ]


def _make_orders(
    count: int, *, settle_type: str = "OPEN", start: int = 300000
) -> list[dict[str, Any]]:
    """連番の有効注文を生成する（cancelOrders のチャンク分割検証用）."""
    return [
        {
            "rootOrderId": start + i,
            "orderId": start + 1000 + i,
            "settleType": settle_type,
            "status": "ORDERED",
        }
        for i in range(count)
    ]


def _patch_sleep() -> Any:
    """time.sleep をパッチして即時返却し、呼び出し回数を記録するモックを返す."""
    return patch("backend.src.batch.cancel_first.time.sleep")


@contextmanager
def _capture_log() -> Iterator[MagicMock]:
    """構造化ログをモックして event 名を検査できるようにする。

    structlog は cache_logger_on_first_use=True で設定されるため
    structlog.testing.capture_logs() はテスト順に依存して取りこぼす。
    モジュール変数 _log を差し替える方が決定論的。
    """
    with patch("backend.src.batch.cancel_first._log") as mock_log:
        yield mock_log


def _logged_events(mock_log: MagicMock, level: str) -> list[str]:
    """指定レベルで記録されたイベント名のリスト."""
    calls = getattr(mock_log, level).call_args_list
    return [c.args[0] for c in calls if c.args]


# ---------------------------------------------------------------------------
# Step0: 市場状態確認
# ---------------------------------------------------------------------------


class TestStep0MarketCheck:
    def test_market_closed_returns_retry_flag(self) -> None:
        """閉場時は market_closed=True かつ retry_next=True で即時 return すること（§7.3）."""
        client = MockForexClient(market_open=False)
        with _patch_sleep():
            result = run_cancel_first(client)

        assert result.market_closed is True
        assert result.retry_next is True
        assert client.post_calls == []

    def test_market_closed_does_not_proceed_to_steps(self) -> None:
        """閉場時は Step1〜3 を実行しないこと（AGENTS.md #4）."""
        client = MockForexClient(
            market_open=False,
            new_orders=[_NEW_ORDER],
            positions=[_BUY_POSITION],
        )
        with _patch_sleep():
            result = run_cancel_first(client)

        assert result.step1_cancelled_ids == []
        assert result.step3_closed_ids == []


# ---------------------------------------------------------------------------
# Step1: 新規 IFD-OCO キャンセル
# ---------------------------------------------------------------------------


class TestStep1CancelNewOrders:
    def test_new_orders_are_cancelled(self) -> None:
        """新規注文が存在する場合に cancelOrders が呼ばれること（§6.2 Step1）."""
        client = MockForexClient(new_orders=[_NEW_ORDER])
        with _patch_sleep():
            result = run_cancel_first(client)

        assert result.step1_cancelled_ids == ["123456789"]
        assert PRIVATE_CANCEL_ORDERS in client.post_call_paths()

    def test_no_new_orders_skips_step1_post(self) -> None:
        """新規注文がない場合は cancelOrders を呼ばないこと."""
        client = MockForexClient(new_orders=[])
        with _patch_sleep():
            result = run_cancel_first(client)

        cancel_calls = [
            p for p in client.post_call_paths() if p == PRIVATE_CANCEL_ORDERS
        ]
        assert len(cancel_calls) == 0
        assert result.step1_cancelled_ids == []


# ---------------------------------------------------------------------------
# Step1 失敗時: Step2/3 を実行しない（AGENTS.md #4）
# ---------------------------------------------------------------------------


class TestStep1FailurePropagation:
    def test_step1_cancel_failure_halts_step2_and_step3(self) -> None:
        """Step1 の cancelOrders POST が例外を送出した場合、Step2/3 を実行しないこと（AGENTS.md #4）."""

        class FailingCancelClient(MockForexClient):
            def private_post(self, path: str, body: Any = None) -> dict[str, Any]:
                if path == PRIVATE_CANCEL_ORDERS:
                    raise RuntimeError("API error")
                return super().private_post(path, body)

        client = FailingCancelClient(
            new_orders=[_NEW_ORDER],
            settlement_orders=[_SETTLEMENT_ORDER],
            positions=[_BUY_POSITION],
        )
        with _patch_sleep():
            result = run_cancel_first(client)

        assert result.step1_failed is True
        assert result.step2_cancelled_ids == []
        assert result.step3_closed_ids == []

    def test_step1_fetch_failure_halts_step2_and_step3(self) -> None:
        """activeOrders GET が失敗した場合、step1_failed=True で Step2/3 を実行しないこと。

        取得失敗を「注文なし」と扱うと cancel-first の安全条件を壊すため、
        明示的に失敗として伝播させる必要がある（AGENTS.md #4）。
        """

        class FailingGetClient(MockForexClient):
            def private_get(
                self, path: str, params: dict[str, Any] | None = None
            ) -> dict[str, Any]:
                if path == PRIVATE_ACTIVE_ORDERS:
                    raise ConnectionError("activeOrders API unreachable")
                return super().private_get(path, params)

        client = FailingGetClient(
            new_orders=[_NEW_ORDER],
            settlement_orders=[_SETTLEMENT_ORDER],
            positions=[_BUY_POSITION],
        )
        with _patch_sleep():
            result = run_cancel_first(client)

        assert result.step1_failed is True
        assert result.step2_cancelled_ids == []
        assert result.step3_closed_ids == []

    def test_step1_failure_result_flags(self) -> None:
        """Step1 失敗時は step1_failed=True, market_closed=False で返ること."""

        class FailingClient(MockForexClient):
            def private_post(self, path: str, body: Any = None) -> dict[str, Any]:
                raise RuntimeError("cancel failed")

        client = FailingClient(new_orders=[_NEW_ORDER])
        with _patch_sleep():
            result = run_cancel_first(client)

        assert result.step1_failed is True
        assert result.market_closed is False


# ---------------------------------------------------------------------------
# Step2: 決済 OCO キャンセル
# ---------------------------------------------------------------------------


class TestStep2CancelSettlementOrders:
    def test_settlement_orders_are_cancelled(self) -> None:
        """決済 OCO 注文が存在する場合に cancelOrders が呼ばれること（§6.2 Step2）."""
        client = MockForexClient(settlement_orders=[_SETTLEMENT_ORDER])
        with _patch_sleep():
            result = run_cancel_first(client)

        assert result.step2_cancelled_ids == ["223456789"]

    def test_both_new_and_settlement_cancelled_in_order(self) -> None:
        """新規→決済の順で cancelOrders が呼ばれること（AGENTS.md #4 キャンセル順序）."""
        client = MockForexClient(
            new_orders=[_NEW_ORDER],
            settlement_orders=[_SETTLEMENT_ORDER],
        )
        with _patch_sleep():
            run_cancel_first(client)

        cancel_paths = [p for p, _ in client.post_calls if p == PRIVATE_CANCEL_ORDERS]
        assert len(cancel_paths) == 2

        first_body = _body_dict(client.post_calls[0][1])
        assert first_body["rootOrderIds"] == [123456789]

        second_body = _body_dict(client.post_calls[1][1])
        assert second_body["rootOrderIds"] == [223456789]


# ---------------------------------------------------------------------------
# Step1/Step2: cancelOrders ボディの公式スキーマ適合（Issue #230）
# ---------------------------------------------------------------------------


class TestCancelOrdersBody:
    """公式: `{"rootOrderIds": [<number>, ...]}`・最大10件。`orderId` は存在しない."""

    def test_body_uses_root_order_ids_array(self) -> None:
        """ボディが rootOrderIds の number 配列であること."""
        client = MockForexClient(new_orders=[_NEW_ORDER])
        with _patch_sleep():
            run_cancel_first(client)

        body = client.post_bodies(PRIVATE_CANCEL_ORDERS)[0]
        assert body == {"rootOrderIds": [123456789]}
        assert isinstance(body["rootOrderIds"][0], int)

    def test_legacy_order_id_param_is_not_sent(self) -> None:
        """公式仕様に存在しない `orderId` を送らないこと（Issue #230 の回帰ガード）."""
        client = MockForexClient(new_orders=[_NEW_ORDER])
        with _patch_sleep():
            run_cancel_first(client)

        body = client.post_bodies(PRIVATE_CANCEL_ORDERS)[0]
        assert "orderId" not in body
        assert "orderIds" not in body
        assert "clientOrderIds" not in body

    def test_root_order_id_is_sent_not_order_id(self) -> None:
        """activeOrders が両方返すとき rootOrderId（親注文ID）を送ること."""
        client = MockForexClient(new_orders=[_NEW_ORDER])
        with _patch_sleep():
            run_cancel_first(client)

        body = client.post_bodies(PRIVATE_CANCEL_ORDERS)[0]
        assert body["rootOrderIds"] == [_NEW_ORDER["rootOrderId"]]
        assert _NEW_ORDER["orderId"] not in body["rootOrderIds"]

    def test_oco_children_sharing_root_order_id_are_deduped(self) -> None:
        """OCO の 2 子注文が同一 rootOrderId を共有する場合 1 件に重複排除されること."""
        oco_children = [
            {
                "rootOrderId": 555000,
                "orderId": 555001,
                "settleType": "CLOSE",
                "status": "ORDERED",
            },
            {
                "rootOrderId": 555000,
                "orderId": 555002,
                "settleType": "CLOSE",
                "status": "ORDERED",
            },
        ]
        client = MockForexClient(settlement_orders=oco_children)
        with _patch_sleep():
            result = run_cancel_first(client)

        bodies = client.post_bodies(PRIVATE_CANCEL_ORDERS)
        assert len(bodies) == 1
        assert bodies[0]["rootOrderIds"] == [555000]
        assert result.step2_cancelled_ids == ["555000"]

    def test_chunks_above_official_limit(self) -> None:
        """11 件は 10 + 1 の 2 POST に分割されること（公式: 最大10件）."""
        client = MockForexClient(new_orders=_make_orders(11))
        with _patch_sleep() as mock_sleep:
            run_cancel_first(client)

        bodies = client.post_bodies(PRIVATE_CANCEL_ORDERS)
        assert [len(b["rootOrderIds"]) for b in bodies] == [
            MAX_CANCEL_ROOT_ORDER_IDS,
            1,
        ]
        # 分割された各 POST の後にもスリープが入ること（AGENTS.md #3）
        assert mock_sleep.call_count == 2

    def test_unacknowledged_cancel_is_warned(self) -> None:
        """data.success に含まれない親注文IDは警告されること（Step3 の ERR-423 予兆）."""

        class PartialSuccessClient(MockForexClient):
            def private_post(self, path: str, body: Any = None) -> dict[str, Any]:
                super().private_post(path, body)
                if path == PRIVATE_CANCEL_ORDERS:
                    # 1 件しか受け付けられなかったレスポンス
                    return {
                        "status": 0,
                        "data": {"success": [{"rootOrderId": 300000}]},
                    }
                return {"status": 0, "data": None}

        client = PartialSuccessClient(new_orders=_make_orders(3))
        with _patch_sleep(), _capture_log() as mock_log:
            run_cancel_first(client)

        assert "cancel_first_cancel_not_acknowledged" in _logged_events(
            mock_log, "warning"
        )


# ---------------------------------------------------------------------------
# Step3: 建玉成行クローズ（closeOrder + settlePosition バッチ）
# ---------------------------------------------------------------------------


class TestStep3ClosePositions:
    def test_positions_are_closed_with_close_order(self) -> None:
        """建玉がある場合に closeOrder が呼ばれ、クローズ済みとして記録されること（§6.2 Step3）."""
        client = MockForexClient(positions=[_BUY_POSITION])
        with _patch_sleep():
            result = run_cancel_first(client)

        assert PRIVATE_CLOSE_ORDER in client.post_call_paths()
        assert "1000342" in result.step3_closed_ids
        assert result.step3_residual_ids == []
        assert result.step3_verification_failed is False

    def test_no_positions_skips_step3(self) -> None:
        """建玉がない場合は closeOrder を呼ばないこと."""
        client = MockForexClient(positions=[])
        with _patch_sleep():
            result = run_cancel_first(client)

        assert PRIVATE_CLOSE_ORDER not in client.post_call_paths()
        assert result.step3_closed_ids == []

    def test_residual_position_is_not_reported_as_closed(self) -> None:
        """closeOrder 受付後も残存する建玉は closed ではなく residual として記録すること。

        closeOrder の成功は status:WAITING の「受付」であり約定確認ではないため、
        再取得で消えていない建玉をクローズ済みと記録してはならない。
        """

        class NonSettlingCloseClient(MockForexClient):
            """closeOrder を受け付けるが建玉を閉じない（受付のみ）シミュレーション."""

            def private_post(self, path: str, body: Any = None) -> dict[str, Any]:
                self.post_calls.append((path, body))
                return {"status": 0, "data": None}

        client = NonSettlingCloseClient(positions=[_BUY_POSITION])
        with _patch_sleep(), _capture_log() as mock_log:
            result = run_cancel_first(client)

        assert PRIVATE_CLOSE_ORDER in client.post_call_paths()
        assert result.step3_closed_ids == []
        assert result.step3_residual_ids == ["1000342"]
        assert "cancel_first_step3_residual_positions" in _logged_events(
            mock_log, "warning"
        )

    def test_each_position_is_closed_exactly_once(self) -> None:
        """建玉 1 件に対する closeOrder POST はちょうど 1 回であること。

        旧実装の closeBulkOrder + closeOrder フォールバック二段構えは
        二重決済の余地があった（Issue #230 の回帰ガード）。
        """
        client = MockForexClient(positions=[_BUY_POSITION])
        with _patch_sleep():
            run_cancel_first(client)

        close_posts = [p for p in client.post_call_paths() if p == PRIVATE_CLOSE_ORDER]
        assert len(close_posts) == 1

    def test_verification_get_happens_once_after_all_posts(self) -> None:
        """建玉が多くても決済確認の openPositions GET は step0/step3/検証の 3 回のみ."""
        client = MockForexClient(positions=_make_positions(25))
        with _patch_sleep():
            run_cancel_first(client)

        assert client.open_positions_get_count == 3


# ---------------------------------------------------------------------------
# Step3: closeOrder ボディの公式スキーマ適合（Issue #230）
# ---------------------------------------------------------------------------


class TestCloseOrderBody:
    """公式: settlePosition 配列（最大10件）。size / positionId のトップレベル指定は不可."""

    def test_body_matches_official_schema(self) -> None:
        """ボディが公式リクエスト例と同一構造であること."""
        client = MockForexClient(positions=[_BUY_POSITION])
        with _patch_sleep():
            run_cancel_first(client)

        body = client.post_bodies(PRIVATE_CLOSE_ORDER)[0]
        assert body == {
            "symbol": "USD_JPY",
            "side": "SELL",
            "executionType": "MARKET",
            "settlePosition": [{"positionId": 1000342, "size": "100"}],
        }

    def test_top_level_size_is_not_sent(self) -> None:
        """トップレベル size を送らないこと（ERR-5214 の回帰ガード）.

        公式: 「size settlePosition いずれか1つが必須です。2つ同時には設定できません。」
        ERR-5214: 決済注文において、建玉IDを指定している場合に注文数量を指定すると返る。
        """
        client = MockForexClient(positions=[_BUY_POSITION])
        with _patch_sleep():
            run_cancel_first(client)

        body = client.post_bodies(PRIVATE_CLOSE_ORDER)[0]
        assert "size" not in body

    def test_top_level_position_id_is_not_sent(self) -> None:
        """トップレベル positionId を送らないこと（公式仕様に存在しない・Issue #230）."""
        client = MockForexClient(positions=[_BUY_POSITION])
        with _patch_sleep():
            run_cancel_first(client)

        body = client.post_bodies(PRIVATE_CLOSE_ORDER)[0]
        assert "positionId" not in body

    def test_settle_position_key_spelling(self) -> None:
        """公式パラメータ表の誤植 `settlePostion` に追随していないこと."""
        client = MockForexClient(positions=[_BUY_POSITION])
        with _patch_sleep():
            run_cancel_first(client)

        body = client.post_bodies(PRIVATE_CLOSE_ORDER)[0]
        assert "settlePosition" in body
        assert "settlePostion" not in body

    def test_position_id_is_number_and_size_is_string(self) -> None:
        """settlePosition.positionId は number、size は string（公式仕様の型）."""
        client = MockForexClient(positions=[_BUY_POSITION])
        with _patch_sleep():
            run_cancel_first(client)

        item = client.post_bodies(PRIVATE_CLOSE_ORDER)[0]["settlePosition"][0]
        assert isinstance(item["positionId"], int)
        assert isinstance(item["size"], str)

    def test_size_is_passed_through_verbatim(self) -> None:
        """size は openPositions が返した文字列を無加工で送ること（推測禁止）."""
        position = {**_BUY_POSITION, "size": "12300"}
        client = MockForexClient(positions=[position])
        with _patch_sleep():
            run_cancel_first(client)

        item = client.post_bodies(PRIVATE_CLOSE_ORDER)[0]["settlePosition"][0]
        assert item["size"] == "12300"

    def test_no_null_fields_in_body(self) -> None:
        """未使用フィールドの null を実送信しないこと（ERR-5106 の温床）."""
        client = MockForexClient(positions=[_BUY_POSITION])
        with _patch_sleep():
            run_cancel_first(client)

        raw = [b for p, b in client.post_calls if p == PRIVATE_CLOSE_ORDER][0]
        assert isinstance(raw, BaseModel)
        assert "null" not in raw.model_dump_json(by_alias=True)


class TestCloseOrderChunking:
    def test_exactly_ten_positions_use_one_post(self) -> None:
        """10 件はちょうど 1 POST（公式上限と一致）."""
        client = MockForexClient(positions=_make_positions(MAX_SETTLE_POSITIONS))
        with _patch_sleep():
            run_cancel_first(client)

        bodies = client.post_bodies(PRIVATE_CLOSE_ORDER)
        assert len(bodies) == 1
        assert len(bodies[0]["settlePosition"]) == MAX_SETTLE_POSITIONS

    @pytest.mark.parametrize(
        ("count", "expected_chunk_sizes"),
        [(11, [10, 1]), (25, [10, 10, 5])],
    )
    def test_chunks_split_above_limit(
        self, count: int, expected_chunk_sizes: list[int]
    ) -> None:
        """10 件を超えたら分割され、全建玉が重複なく 1 度だけ含まれること."""
        positions = _make_positions(count)
        client = MockForexClient(positions=positions)
        with _patch_sleep():
            result = run_cancel_first(client)

        bodies = client.post_bodies(PRIVATE_CLOSE_ORDER)
        assert [len(b["settlePosition"]) for b in bodies] == expected_chunk_sizes
        assert all(
            len(b["settlePosition"]) <= MAX_SETTLE_POSITIONS for b in bodies
        )

        sent_ids = [
            item["positionId"] for b in bodies for item in b["settlePosition"]
        ]
        assert len(sent_ids) == len(set(sent_ids)), "同じ建玉を 2 度送ってはならない"
        assert set(sent_ids) == {p["positionId"] for p in positions}
        assert len(result.step3_closed_ids) == count

    def test_hedged_positions_produce_one_post_per_side(self) -> None:
        """両建て時は決済方向ごとに POST が分かれ、混在しないこと."""
        client = MockForexClient(positions=[_BUY_POSITION, _SELL_POSITION])
        with _patch_sleep():
            run_cancel_first(client)

        bodies = client.post_bodies(PRIVATE_CLOSE_ORDER)
        assert len(bodies) == 2
        # sorted() により決済方向 BUY（= SELL 建玉）が先
        assert [b["side"] for b in bodies] == ["BUY", "SELL"]
        assert bodies[0]["settlePosition"] == [{"positionId": 1000343, "size": "100"}]
        assert bodies[1]["settlePosition"] == [{"positionId": 1000342, "size": "100"}]

    def test_side_order_is_deterministic(self) -> None:
        """建玉リストの順序を反転しても POST の side 順が変わらないこと（set 非決定性の排除）."""
        sides: list[list[str]] = []
        for positions in ([_BUY_POSITION, _SELL_POSITION], [_SELL_POSITION, _BUY_POSITION]):
            client = MockForexClient(positions=positions)
            with _patch_sleep():
                run_cancel_first(client)
            sides.append([b["side"] for b in client.post_bodies(PRIVATE_CLOSE_ORDER)])

        assert sides[0] == sides[1] == ["BUY", "SELL"]

    def test_duplicate_position_id_sent_once(self) -> None:
        """同一 positionId が重複して返っても settlePosition は 1 要素（ERR-512 回避）."""
        client = MockForexClient(positions=[_BUY_POSITION, dict(_BUY_POSITION)])
        with _patch_sleep(), _capture_log() as mock_log:
            run_cancel_first(client)

        bodies = client.post_bodies(PRIVATE_CLOSE_ORDER)
        assert len(bodies) == 1
        assert bodies[0]["settlePosition"] == [{"positionId": 1000342, "size": "100"}]
        assert "cancel_first_duplicate_position_id" in _logged_events(
            mock_log, "warning"
        )


# ---------------------------------------------------------------------------
# Step3: POST 前バリデーション（fail-closed）
# ---------------------------------------------------------------------------


class TestStep3PreflightValidation:
    """不正な建玉は POST を 1 件も送らずに落とす（旧実装の暗黙既定値を排除）."""

    @pytest.mark.parametrize(
        "broken",
        [
            pytest.param({"symbol": "USD_JPY", "side": "BUY", "size": "100"}, id="no_position_id"),
            pytest.param({"positionId": 1000342, "side": "BUY"}, id="no_size"),
            pytest.param({"positionId": 1000342, "side": "BUY", "size": "0"}, id="zero_size"),
            pytest.param({"positionId": 1000342, "size": "100"}, id="no_side"),
            pytest.param(
                {"positionId": 1000342, "side": "LONG", "size": "100"}, id="invalid_side"
            ),
            pytest.param(
                {"positionId": "pos-1", "side": "BUY", "size": "100"},
                id="non_numeric_position_id",
            ),
        ],
    )
    def test_invalid_position_fails_closed_without_post(
        self, broken: dict[str, Any]
    ) -> None:
        client = MockForexClient(positions=[broken])
        with _patch_sleep(), pytest.raises(ValueError):
            run_cancel_first(client)

        assert PRIVATE_CLOSE_ORDER not in client.post_call_paths()

    def test_ordered_size_nonzero_logs_warning_and_sends_full_size(self) -> None:
        """orderedSize が残っていても size は満額送り、ERR-423 の予兆を警告すること."""
        position = {**_BUY_POSITION, "orderedSize": "100"}
        client = MockForexClient(positions=[position])
        with _patch_sleep(), _capture_log() as mock_log:
            run_cancel_first(client)

        item = client.post_bodies(PRIVATE_CLOSE_ORDER)[0]["settlePosition"][0]
        assert item["size"] == "100"
        assert "cancel_first_close_ordered_size_nonzero" in _logged_events(
            mock_log, "warning"
        )

    def test_page_limit_reached_logs_warning(self) -> None:
        """取得件数が公式 count 上限（100）に達したら警告すること（prevId 未対応の tripwire）."""
        client = MockForexClient(positions=_make_positions(100))
        with _patch_sleep(), _capture_log() as mock_log:
            run_cancel_first(client)

        assert "cancel_first_page_limit_reached" in _logged_events(mock_log, "warning")


# ---------------------------------------------------------------------------
# call order: cancelOrders → cancelOrders → closeOrder（§2, AGENTS.md #4）
# ---------------------------------------------------------------------------


class TestCallOrder:
    def test_full_sequence_call_order(self) -> None:
        """cancelOrders(新規) → cancelOrders(決済) → closeOrder の順で呼ばれること（§2, AGENTS.md #4）."""
        client = MockForexClient(
            new_orders=[_NEW_ORDER],
            settlement_orders=[_SETTLEMENT_ORDER],
            positions=[_BUY_POSITION],
        )
        with _patch_sleep():
            run_cancel_first(client)

        post_paths = client.post_call_paths()
        assert post_paths[0] == PRIVATE_CANCEL_ORDERS, "Step1: cancelOrders（新規）が先"
        assert post_paths[1] == PRIVATE_CANCEL_ORDERS, "Step2: cancelOrders（決済）が次"
        assert post_paths[2] == PRIVATE_CLOSE_ORDER, "Step3: closeOrder が最後"


# ---------------------------------------------------------------------------
# POST 間 sleep ≥1 秒の検証（AGENTS.md #3, §6.2）
# ---------------------------------------------------------------------------


class TestPostSleep:
    def test_sleep_called_between_posts(self) -> None:
        """複数 POST がある場合に sleep が呼ばれること（AGENTS.md #3）."""
        client = MockForexClient(
            new_orders=[_NEW_ORDER],
            settlement_orders=[_SETTLEMENT_ORDER],
            positions=[_BUY_POSITION],
        )
        with _patch_sleep() as mock_sleep:
            run_cancel_first(client)

        assert mock_sleep.called, "time.sleep が呼ばれていること"
        for call_args in mock_sleep.call_args_list:
            seconds = call_args[0][0]
            assert seconds >= _POST_SLEEP_SECONDS, (
                f"sleep が {_POST_SLEEP_SECONDS}秒未満: {seconds}秒"
            )

    def test_sleep_not_called_when_no_posts(self) -> None:
        """POST がない場合（新規注文・建玉なし）は sleep しないこと."""
        client = MockForexClient(new_orders=[], settlement_orders=[], positions=[])
        with _patch_sleep() as mock_sleep:
            run_cancel_first(client)

        mock_sleep.assert_not_called()

    def test_post_and_sleep_alternate(self) -> None:
        """すべての POST の直後に sleep が入ること（連続 POST が起きない・AGENTS.md #3）."""
        events: list[str] = []
        client = MockForexClient(
            new_orders=_make_orders(11),
            settlement_orders=[_SETTLEMENT_ORDER],
            positions=_make_positions(11),
            event_log=events,
        )
        with patch("backend.src.batch.cancel_first.time.sleep") as mock_sleep:
            mock_sleep.side_effect = lambda _seconds: events.append("sleep")
            run_cancel_first(client)

        # cancelOrders 2+1 回、closeOrder 2 回 → 計 5 POST、各直後に sleep
        assert events == ["post", "sleep"] * 5


# ---------------------------------------------------------------------------
# 正常完了: CancelFirstResult の型確認
# ---------------------------------------------------------------------------


class TestResultType:
    def test_result_is_cancel_first_result(self) -> None:
        """run_cancel_first は CancelFirstResult を返すこと."""
        client = MockForexClient()
        with _patch_sleep():
            result = run_cancel_first(client)

        assert isinstance(result, CancelFirstResult)

    def test_successful_run_returns_ok_flags(self) -> None:
        """市場開場・Step1 成功の場合、market_closed=False, step1_failed=False."""
        client = MockForexClient()
        with _patch_sleep():
            result = run_cancel_first(client)

        assert result.market_closed is False
        assert result.step1_failed is False
        assert result.retry_next is False
        assert result.step3_residual_ids == []
        assert result.step3_verification_failed is False


# ---------------------------------------------------------------------------
# Step2 失敗時: Step3 を実行しない（AGENTS.md #4）回帰テスト
# ---------------------------------------------------------------------------


class TestStep2FailurePropagation:
    def test_step2_fetch_failure_halts_step3(self) -> None:
        """Step1 成功後に activeOrders GET（Step2 用）が失敗した場合、
        step2_failed=True かつ closeOrder が呼ばれないこと。

        子 OCO を取り消せたか不明なまま成行クローズへ進むと建玉がロックされる恐れがある。
        取得失敗を「注文なし」と空扱いして Step3 に進めてはならない（AGENTS.md #4）。
        """

        class Step2FailingClient(MockForexClient):
            """Step1 の activeOrders GET は成功し、Step2 の GET だけ失敗するクライアント."""

            def __init__(self, **kwargs: Any) -> None:
                super().__init__(**kwargs)
                self._active_orders_call_count = 0

            def private_get(
                self, path: str, params: dict[str, Any] | None = None
            ) -> dict[str, Any]:
                if path == PRIVATE_ACTIVE_ORDERS:
                    self._active_orders_call_count += 1
                    if self._active_orders_call_count == 2:
                        # Step2 の取得（2回目の activeOrders GET）だけ失敗させる
                        raise ConnectionError("Step2 activeOrders API unreachable")
                return super().private_get(path, params)

        client = Step2FailingClient(
            new_orders=[_NEW_ORDER],
            settlement_orders=[_SETTLEMENT_ORDER],
            positions=[_BUY_POSITION],
        )
        with _patch_sleep():
            result = run_cancel_first(client)

        assert result.step2_failed is True
        assert PRIVATE_CLOSE_ORDER not in client.post_call_paths(), (
            "Step2 GET 失敗時は Step3（closeOrder）を呼んではならない"
        )
        assert result.step3_closed_ids == []

    def test_step2_cancel_failure_halts_step3(self) -> None:
        """Step2 の cancelOrders POST が失敗した場合も step2_failed=True で Step3 を実行しないこと."""

        class Step2CancelFailingClient(MockForexClient):
            _step2_cancel_attempted: bool = False

            def private_post(self, path: str, body: Any = None) -> dict[str, Any]:
                # Step1 cancel は成功、Step2 cancel（2回目の CANCEL_ORDERS POST）だけ失敗
                if path == PRIVATE_CANCEL_ORDERS and self._step2_cancel_attempted:
                    raise RuntimeError("Step2 cancelOrders API error")
                if path == PRIVATE_CANCEL_ORDERS:
                    self._step2_cancel_attempted = True
                return super().private_post(path, body)

        client = Step2CancelFailingClient(
            new_orders=[_NEW_ORDER],
            settlement_orders=[_SETTLEMENT_ORDER],
            positions=[_BUY_POSITION],
        )
        with _patch_sleep():
            result = run_cancel_first(client)

        assert result.step2_failed is True
        assert PRIVATE_CLOSE_ORDER not in client.post_call_paths()
        assert result.step3_closed_ids == []


# ---------------------------------------------------------------------------
# GMO ボディエンベロープ検査（HTTP 200 / status != 0、Issue #136・付録B E04/E06）
# ---------------------------------------------------------------------------

_GMO_ERROR_BODY: dict[str, Any] = {
    "status": 1,
    "messages": [
        {"message_code": "ERR-5003", "message_string": "Requests are too many."}
    ],
    "responsetime": "2026-06-29T09:00:00.000Z",
}

_ERR_423_BODY: dict[str, Any] = {
    "status": 1,
    "messages": [
        {
            "message_code": "ERR-423",
            "message_string": "The order quantity exceeds the total position quantity.",
        }
    ],
    "responsetime": "2026-07-30T09:00:00.000Z",
}


class TestGmoEnvelopeFailClosed:
    """ボディ status != 0 の業務エラーを成功と誤認しないこと（fail-closed）."""

    def test_step1_active_orders_envelope_error_marks_step1_failed(self) -> None:
        """activeOrders が status:1 を返したら「注文なし」と混同せず step1_failed."""

        class EnvelopeErrorOnGetClient(MockForexClient):
            def private_get(
                self, path: str, params: dict[str, Any] | None = None
            ) -> dict[str, Any]:
                if path == PRIVATE_ACTIVE_ORDERS:
                    self.get_calls.append((path, params))
                    return dict(_GMO_ERROR_BODY)
                return super().private_get(path, params)

        client = EnvelopeErrorOnGetClient(positions=[_BUY_POSITION])
        with _patch_sleep():
            result = run_cancel_first(client)

        assert result.step1_failed is True
        assert client.post_calls == []

    def test_step1_cancel_envelope_error_marks_step1_failed(self) -> None:
        """cancelOrders が status:1 を返したらキャンセル成功と誤認せず step1_failed."""

        class EnvelopeErrorOnCancelClient(MockForexClient):
            def private_post(self, path: str, body: Any = None) -> dict[str, Any]:
                self.post_calls.append((path, body))
                if path == PRIVATE_CANCEL_ORDERS:
                    return dict(_GMO_ERROR_BODY)
                return {"status": 0, "data": None}

        client = EnvelopeErrorOnCancelClient(
            new_orders=[_NEW_ORDER], positions=[_BUY_POSITION]
        )
        with _patch_sleep():
            result = run_cancel_first(client)

        assert result.step1_failed is True
        assert PRIVATE_CLOSE_ORDER not in client.post_call_paths()

    def test_close_order_envelope_error_raises_structured_gmo_api_error(self) -> None:
        """closeOrder の status:1 は message_code を構造化保持した GmoApiError で伝播する.

        kill switch はこの message_codes への完全一致で no-op か fail-closed かを判定する
        （Issue #136: 例外文字列の部分一致判定を排除）。
        """

        class EnvelopeErrorOnCloseClient(MockForexClient):
            def private_post(self, path: str, body: Any = None) -> dict[str, Any]:
                self.post_calls.append((path, body))
                if path == PRIVATE_CLOSE_ORDER:
                    return dict(_GMO_ERROR_BODY)
                return {"status": 0, "data": None}

        client = EnvelopeErrorOnCloseClient(positions=[_BUY_POSITION])
        with _patch_sleep(), pytest.raises(GmoApiError) as exc_info:
            run_cancel_first(client)

        assert exc_info.value.message_codes == ("ERR-5003",)
        assert exc_info.value.status == 1

    def test_err423_propagates_fail_closed(self) -> None:
        """ERR-423（総有効注文数量との合計超過）も fail-closed で伝播すること."""

        class Err423Client(MockForexClient):
            def private_post(self, path: str, body: Any = None) -> dict[str, Any]:
                self.post_calls.append((path, body))
                if path == PRIVATE_CLOSE_ORDER:
                    return dict(_ERR_423_BODY)
                return {"status": 0, "data": None}

        client = Err423Client(positions=[_BUY_POSITION])
        with _patch_sleep(), pytest.raises(GmoApiError) as exc_info:
            run_cancel_first(client)

        assert exc_info.value.message_codes == ("ERR-423",)

    def test_failing_chunk_stops_subsequent_chunks(self) -> None:
        """1 本目のチャンクが status:1 なら後続チャンクを送らないこと（fail-fast）.

        #207 (b) L1 で確定拒否は再送するようになったが、**再送は同じチャンクに対してのみ**で、
        後続チャンクへは進まない（fail-fast は維持）。
        """

        class Err423Client(MockForexClient):
            def private_post(self, path: str, body: Any = None) -> dict[str, Any]:
                self.post_calls.append((path, body))
                if path == PRIVATE_CLOSE_ORDER:
                    return dict(_ERR_423_BODY)
                return {"status": 0, "data": None}

        client = Err423Client(positions=_make_positions(25))  # 3 チャンク分
        with _patch_sleep(), pytest.raises(GmoApiError):
            run_cancel_first(client)

        # 送られた closeOrder はすべて **同一チャンク**（＝1 本目）に対するもの
        sent = [
            tuple(item["positionId"] for item in body["settlePosition"])
            for body in client.post_bodies(PRIVATE_CLOSE_ORDER)
        ]
        assert len(set(sent)) == 1, "後続チャンクが送信された（fail-fast が壊れている）"
        assert len(sent) == _CLOSE_RETRY_MAX_ATTEMPTS

    def test_step0_market_check_envelope_error_treated_as_closed(self) -> None:
        """step0 の openPositions が status:1 を返したら市場オープンと誤認せず閉場扱い."""

        class EnvelopeErrorOnStep0Client(MockForexClient):
            def private_get(
                self, path: str, params: dict[str, Any] | None = None
            ) -> dict[str, Any]:
                if path == PRIVATE_OPEN_POSITIONS:
                    self.get_calls.append((path, params))
                    self.open_positions_get_count += 1
                    return dict(_GMO_ERROR_BODY)
                return super().private_get(path, params)

        client = EnvelopeErrorOnStep0Client(positions=[_BUY_POSITION])
        with _patch_sleep():
            result = run_cancel_first(client)

        assert result.market_closed is True
        assert result.retry_next is True
        assert client.post_calls == []

    def test_step3_open_positions_envelope_error_propagates_fail_closed(self) -> None:
        """step3 の openPositions が status:1 を返したら「建玉なし」と混同せず伝播する.

        建玉取得エラーを空建玉（成功）に変換すると決済失敗が偽成功になるため、
        GmoApiError を再送出して kill switch 側の fail-closed（500）に倒す（Issue #136）。
        """

        class EnvelopeErrorOnPositionsClient(MockForexClient):
            def private_get(
                self, path: str, params: dict[str, Any] | None = None
            ) -> dict[str, Any]:
                if path == PRIVATE_OPEN_POSITIONS:
                    self.get_calls.append((path, params))
                    self.open_positions_get_count += 1
                    if self.open_positions_get_count > 1:
                        # step0 の市場確認は素通しし、step3 の建玉取得のみエラーにする
                        return dict(_GMO_ERROR_BODY)
                    return {"status": 0, "data": list(self._positions)}
                return super().private_get(path, params)

        client = EnvelopeErrorOnPositionsClient(positions=[_BUY_POSITION])
        with _patch_sleep(), pytest.raises(GmoApiError) as exc_info:
            run_cancel_first(client)

        assert exc_info.value.message_codes == ("ERR-5003",)
        assert PRIVATE_CLOSE_ORDER not in client.post_call_paths()

    def test_verification_refetch_failure_is_fail_closed(self) -> None:
        """決済後の検証 GET が失敗したら closed を報告せず verification_failed に倒すこと.

        クローズ確認が取れないまま「決済済み」と記録すると偽成功になる。
        例外は投げず、観測可能なフラグで呼び出し元に伝える。
        """

        class VerificationFailingClient(MockForexClient):
            def private_get(
                self, path: str, params: dict[str, Any] | None = None
            ) -> dict[str, Any]:
                if path == PRIVATE_OPEN_POSITIONS:
                    self.get_calls.append((path, params))
                    self.open_positions_get_count += 1
                    if self.open_positions_get_count > 2:
                        # step0・step3 は成功し、決済後の検証 GET だけ失敗させる
                        raise ConnectionError("openPositions unreachable")
                    return {"status": 0, "data": list(self._positions)}
                return super().private_get(path, params)

        client = VerificationFailingClient(positions=[_BUY_POSITION])
        with _patch_sleep():
            result = run_cancel_first(client)

        assert result.step3_closed_ids == []
        assert result.step3_residual_ids == []
        assert result.step3_verification_failed is True
        assert PRIVATE_CLOSE_ORDER in client.post_call_paths()

    def test_status_key_absent_is_not_treated_as_error(self) -> None:
        """status キーを持たないレスポンス（モック互換）は GET/POST とも検査せず従来どおり動作する."""

        class NoStatusKeyClient(MockForexClient):
            def private_get(
                self, path: str, params: dict[str, Any] | None = None
            ) -> dict[str, Any]:
                resp = super().private_get(path, params)
                return {"data": resp["data"]}

            def private_post(self, path: str, body: Any = None) -> dict[str, Any]:
                resp = super().private_post(path, body)
                return {"data": resp["data"]}

        # cancelOrders / closeOrder の POST 経路も通す
        client = NoStatusKeyClient(new_orders=[_NEW_ORDER], positions=[_BUY_POSITION])
        with _patch_sleep():
            result = run_cancel_first(client)

        assert result.step1_cancelled_ids == ["123456789"]
        assert result.step1_failed is False
        assert result.step3_closed_ids == ["1000342"]
        assert PRIVATE_CANCEL_ORDERS in client.post_call_paths()
        assert PRIVATE_CLOSE_ORDER in client.post_call_paths()


class TestOrderedSizeReflectionPoll:
    """Step2 の取消反映ラグを待ってから Step3 に進む（Issue #241）。

    cancelOrders は「受付」で反映は非同期。反映前に closeOrder を投げると
    ERR-423 / ERR-189 になり、決済 OCO を消した直後の建玉が SL/TP なしで
    翌日まで残る。2026-08-03 の実発注スパイクで実測した挙動の回帰。
    """

    # ポーリングが起きない場合の openPositions GET 回数:
    #   Step0 市場確認 1 + Step3 建玉取得 1 + Step3 決済検証の再取得 1
    _OPEN_POSITIONS_GETS_WITHOUT_POLL = 3

    class _LaggyClient(MockForexClient):
        """openPositions の orderedSize が N 回目の取得で "0" に落ちるモック。"""

        def __init__(self, *, reflect_on_call: int, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._reflect_on_call = reflect_on_call

        def private_get(
            self, path: str, params: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            result = super().private_get(path, params)
            if path == PRIVATE_OPEN_POSITIONS:
                reflected = self.open_positions_get_count >= self._reflect_on_call
                result["data"] = [
                    {**p, "orderedSize": "0" if reflected else "100"}
                    for p in result["data"]
                ]
            return result

    @staticmethod
    def _settlement_order() -> dict[str, Any]:
        return {
            "rootOrderId": 1000101,
            "orderId": 1000102,
            "symbol": "USD_JPY",
            "side": "SELL",
            "settleType": "CLOSE",
            "size": "100",
        }

    def test_waits_until_ordered_size_becomes_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """2 回目の取得で反映されたら、そこから Step3 へ進む。"""
        slept: list[float] = []
        monkeypatch.setattr(cancel_first.time, "sleep", lambda s: slept.append(s))

        # GET 内訳: 1=Step0 市場確認 / 2=Step3 初回取得（未反映）/ 3=ポーリング再取得（反映）
        client = self._LaggyClient(
            reflect_on_call=3,
            positions=[dict(_BUY_POSITION)],
            settlement_orders=[self._settlement_order()],
        )
        result = cancel_first.run_cancel_first(client)

        # 反映待ちで 1 回余分に取得している
        assert client.open_positions_get_count == self._OPEN_POSITIONS_GETS_WITHOUT_POLL + 1
        # 決済まで到達している
        assert result.step3_closed_ids == ["1000342"]
        # ポーリングの sleep が入った（POST 間 sleep とは別に 1.0 秒）
        assert 1.0 in slept

    def test_no_wait_when_already_reflected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """初回から orderedSize=0 なら待たずに進む（通常時に遅延を足さない）。"""
        monkeypatch.setattr(cancel_first.time, "sleep", lambda s: None)

        client = self._LaggyClient(
            reflect_on_call=1,
            positions=[dict(_BUY_POSITION)],
            settlement_orders=[self._settlement_order()],
        )
        result = cancel_first.run_cancel_first(client)

        assert result.step3_closed_ids == ["1000342"]
        # ポーリング無しのベースライン: Step0 市場確認 + Step3 取得 + 決済検証の再取得
        assert client.open_positions_get_count == self._OPEN_POSITIONS_GETS_WITHOUT_POLL

    def test_skips_poll_when_step2_cancelled_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Step2 が 1 件も取り消していないなら待たない（待つ対象が無い）。"""
        monkeypatch.setattr(cancel_first.time, "sleep", lambda s: None)

        # 決済注文なし = Step2 は no-op。orderedSize は非0 のままだが待たずに進む
        client = self._LaggyClient(
            reflect_on_call=99,
            positions=[dict(_BUY_POSITION)],
            settlement_orders=[],
        )
        result = cancel_first.run_cancel_first(client)

        assert result.step2_cancelled_ids == []
        # orderedSize は非0 のままだが、待つ対象が無いので追加取得しない
        assert client.open_positions_get_count == self._OPEN_POSITIONS_GETS_WITHOUT_POLL
        assert result.step3_closed_ids == ["1000342"]

    def test_timeout_still_attempts_close_fail_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """反映されないままタイムアウトしても例外にせず満額 close を試みる（従来挙動）。"""
        monkeypatch.setattr(cancel_first.time, "sleep", lambda s: None)

        client = self._LaggyClient(
            reflect_on_call=99,  # 最後まで反映されない
            positions=[dict(_BUY_POSITION)],
            settlement_orders=[self._settlement_order()],
        )
        result = cancel_first.run_cancel_first(client)

        # 上限まで再取得している
        assert client.open_positions_get_count >= cancel_first._ORDERED_SIZE_POLL_MAX_ATTEMPTS
        # 例外にせず close まで進む（失敗は次回起動に委ねる fail-closed を維持）
        assert result.step3_closed_ids == ["1000342"]


class TestStep3ExceptionContract:
    """🔴 run_cancel_first は Step3 失敗を **例外のまま送出し続ける**こと（#207 / #136）。

    #207 で「Step3 失敗が Lambda ごと落とす」問題を直したが、修正は
    **closeout_batch 側での捕捉**として実装した。run_cancel_first を非送出化（結果フラグ化）
    してはならない。同じ関数を kill switch（backend/src/kill_switch/handler.py）も呼んでおり、
    そちらは GmoApiError.message_codes による分類で fail-closed（HTTP 500）を判定する。
    非送出化すると **緊急停止が「決済成功」を誤報する**。
    """

    def test_close_failure_still_raises_for_kill_switch(self) -> None:
        class Err423Client(MockForexClient):
            def private_post(self, path: str, body: Any = None) -> dict[str, Any]:
                self.post_calls.append((path, body))
                if path == PRIVATE_CLOSE_ORDER:
                    return dict(_ERR_423_BODY)
                return {"status": 0, "data": None}

        client = Err423Client(positions=[_BUY_POSITION])
        with _patch_sleep(), pytest.raises(GmoApiError) as exc_info:
            run_cancel_first(client)

        # kill switch はこの message_codes で no-op か fail-closed かを判定する
        assert exc_info.value.message_codes == ("ERR-423",)


# ---------------------------------------------------------------------------
# #207 (b) L1: closeOrder の invocation 内リトライ（状態機械）
#
# 🔴 再送してよいのは「GMO が応答を返して明示的に拒否した」場合だけ。
#    タイムアウト・接続断・HTTP エラーは「受付済みかもしれない」ので再送しない。
# ---------------------------------------------------------------------------


class _CloseFailNTimesClient(MockForexClient):
    """closeOrder を最初の N 回だけ失敗させるクライアント."""

    def __init__(self, *args: Any, fail_times: int = 1, body: Any = None, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self._fail_times = fail_times
        self._fail_body = body if body is not None else _ERR_423_BODY
        self.close_attempts = 0

    def private_post(self, path: str, body: Any = None) -> dict[str, Any]:
        self.post_calls.append((path, body))
        if self._event_log is not None:
            self._event_log.append("post")
        if path == PRIVATE_CLOSE_ORDER:
            self.close_attempts += 1
            if self.close_attempts <= self._fail_times:
                return dict(self._fail_body)
            self._positions = []  # 受理されたので建玉は消える
            return {"status": 0, "data": None}
        return {"status": 0, "data": None}


class TestCloseRetryStateMachine:
    def test_definite_rejection_is_retried_and_succeeds(self) -> None:
        """確定拒否（ERR-423）は再送され、2 回目で成功すること。

        Step2 の取消反映ラグで ERR-423 になるケースが典型。待って再送すれば通る。
        """
        client = _CloseFailNTimesClient(positions=[_BUY_POSITION], fail_times=1)
        with _patch_sleep():
            result = run_cancel_first(client)

        assert client.close_attempts == 2
        assert result.step3_closed_ids == ["1000342"]
        assert result.step3_residual_ids == []

    def test_uncertain_failure_is_not_retried(self) -> None:
        """🔴 タイムアウト・接続断は再送しない（受付済みかもしれないため）。"""

        class TimeoutClient(MockForexClient):
            def __init__(self, *a: Any, **kw: Any) -> None:
                super().__init__(*a, **kw)
                self.close_attempts = 0

            def private_post(self, path: str, body: Any = None) -> dict[str, Any]:
                self.post_calls.append((path, body))
                if path == PRIVATE_CLOSE_ORDER:
                    self.close_attempts += 1
                    raise TimeoutError("read timeout")
                return {"status": 0, "data": None}

        client = TimeoutClient(positions=[_BUY_POSITION])
        with _patch_sleep(), pytest.raises(TimeoutError):
            run_cancel_first(client)

        assert client.close_attempts == 1, "不確実な失敗を再送してはいけない"

    def test_http_error_is_not_retried_and_not_silent(self) -> None:
        """🔴 HTTP 5xx は再送せず、かつ「受付成功」に化けないこと。

        従来は raise_for_gmo_status しか通しておらず、JSON でないボディの 5xx を
        素通りさせて「送信済み」と記録していた。
        """

        class Http502Client(MockForexClient):
            def __init__(self, *a: Any, **kw: Any) -> None:
                super().__init__(*a, **kw)
                self.close_attempts = 0

            def private_post(self, path: str, body: Any = None) -> Any:
                self.post_calls.append((path, body))
                if path == PRIVATE_CLOSE_ORDER:
                    self.close_attempts += 1
                    return SimpleNamespace(status_code=502, json=lambda: None)
                return {"status": 0, "data": None}

        client = Http502Client(positions=[_BUY_POSITION])
        with _patch_sleep(), pytest.raises(CloseOrderTransportError):
            run_cancel_first(client)

        assert client.close_attempts == 1

    def test_no_retry_while_ordered_size_is_pending(self) -> None:
        """🔴 orderedSize != 0（有効注文が動いている）なら再送しない（二重受付の回避）。"""
        locked = {**_BUY_POSITION, "orderedSize": "100"}

        class Err423Client(MockForexClient):
            def __init__(self, *a: Any, **kw: Any) -> None:
                super().__init__(*a, **kw)
                self.close_attempts = 0

            def private_post(self, path: str, body: Any = None) -> dict[str, Any]:
                self.post_calls.append((path, body))
                if path == PRIVATE_CLOSE_ORDER:
                    self.close_attempts += 1
                    return dict(_ERR_423_BODY)
                return {"status": 0, "data": None}

        client = Err423Client(positions=[locked])
        with _patch_sleep(), pytest.raises(GmoApiError):
            run_cancel_first(client)

        assert client.close_attempts == 1, "有効注文が動いている間に再送してはいけない"

    def test_no_retry_when_gate_fetch_fails(self) -> None:
        """🔴 ゲート用 openPositions が引けないなら再送しない（見えないのに撃たない）。"""

        class GateBlindClient(MockForexClient):
            seen_position_gets = 0

            def __init__(self, *a: Any, **kw: Any) -> None:
                super().__init__(*a, **kw)
                self.close_attempts = 0

            def private_post(self, path: str, body: Any = None) -> dict[str, Any]:
                self.post_calls.append((path, body))
                if path == PRIVATE_CLOSE_ORDER:
                    self.close_attempts += 1
                    return dict(_ERR_423_BODY)
                return {"status": 0, "data": None}

            def private_get(
                self, path: str, params: dict[str, Any] | None = None
            ) -> dict[str, Any]:
                # step0 と step3 の建玉取得は通し、**ゲート判定の GET だけ**落とす。
                # （super() 側もカウントするため、ここでは別カウンタで数える）
                if path == PRIVATE_OPEN_POSITIONS:
                    self.seen_position_gets += 1
                    if self.seen_position_gets > 2:
                        raise RuntimeError("network down")
                return super().private_get(path, params)

        client = GateBlindClient(positions=[_BUY_POSITION])
        client.seen_position_gets = 0
        with _patch_sleep(), pytest.raises(GmoApiError):
            run_cancel_first(client)

        assert client.close_attempts == 1

    def test_already_closed_stops_retry(self) -> None:
        """拒否応答でも建玉が消えていれば成功扱いで再送しないこと。"""

        class VanishingClient(MockForexClient):
            def __init__(self, *a: Any, **kw: Any) -> None:
                super().__init__(*a, **kw)
                self.close_attempts = 0

            def private_post(self, path: str, body: Any = None) -> dict[str, Any]:
                self.post_calls.append((path, body))
                if path == PRIVATE_CLOSE_ORDER:
                    self.close_attempts += 1
                    self._positions = []  # 実は受理されていた
                    return dict(_ERR_423_BODY)
                return {"status": 0, "data": None}

        client = VanishingClient(positions=[_BUY_POSITION])
        with _patch_sleep():
            result = run_cancel_first(client)

        assert client.close_attempts == 1
        assert result.step3_residual_ids == []


class _NeverFillsClient(MockForexClient):
    """closeOrder は受理する（status:0）が建玉が**いつまでも約定しない**クライアント。

    既定の MockForexClient は closeOrder で建玉を即座に消すため、
    「受付されたが閉じられない」状態を再現できない。
    """

    def private_post(self, path: str, body: Any = None) -> dict[str, Any]:
        self.post_calls.append((path, body))
        return {"status": 0, "data": None}  # 受付のみ。_positions は減らさない


class TestResidualDiscrimination:
    """#207 (b) L1: 一時的な残存と「本当に閉じられていない」を短ポーリングで判別する。"""

    def test_transient_residual_is_not_reported(self) -> None:
        """ポーリング中に消えた残存は residual に載せない（正常な約定待ち）。"""

        class SlowFillClient(MockForexClient):
            def private_get(
                self, path: str, params: dict[str, Any] | None = None
            ) -> dict[str, Any]:
                if path == PRIVATE_OPEN_POSITIONS:
                    self.open_positions_get_count += 1
                    # step0 / step3 取得 / 検証1回目までは建玉あり、2回目で消える
                    if self.open_positions_get_count >= 4:
                        return {"status": 0, "data": []}
                return super().private_get(path, params)

        client = SlowFillClient(positions=[_BUY_POSITION])
        with _patch_sleep():
            result = run_cancel_first(client)

        assert result.step3_residual_ids == []
        assert result.step3_closed_ids == ["1000342"]

    def test_persistent_residual_is_reported(self) -> None:
        """ポーリングしても消えない残存は residual として報告する（強アラート対象）。"""
        client = _NeverFillsClient(positions=[_BUY_POSITION])
        with _patch_sleep():
            result = run_cancel_first(client)

        assert result.step3_residual_ids == ["1000342"]
        assert result.step3_closed_ids == []

    def test_residual_poll_count_is_bounded(self) -> None:
        """残存ポーリングは上限回数で打ち切ること（Lambda タイムアウト保護）。"""
        client = _NeverFillsClient(positions=[_BUY_POSITION])
        with _patch_sleep():
            run_cancel_first(client)

        # step0(1) + step3 取得(1) + 検証ポーリング(_RESIDUAL_POLL_ATTEMPTS)
        assert client.open_positions_get_count == 2 + _RESIDUAL_POLL_ATTEMPTS


class TestClosePostIntervalOnAllPaths:
    """🔴 closeOrder の POST 間隔は**どの経路でも**守ること（AGENTS.md #3・#248 指摘 1）。

    ALREADY_CLOSED（拒否応答だが建玉は消えていた）で return する経路は、直前に POST 済みなのに
    スリープを挟まないと、多チャンク時に次チャンクの POST が 1 秒未満になる。
    """

    def test_sleep_between_chunks_even_when_already_closed(self) -> None:
        events: list[str] = []

        class RejectButVanishedClient(MockForexClient):
            """1 本目のチャンクだけ「拒否応答だが建玉は消えている」を返す."""

            def __init__(self, *a: Any, **kw: Any) -> None:
                super().__init__(*a, **kw)
                self.close_attempts = 0

            def private_post(self, path: str, body: Any = None) -> dict[str, Any]:
                self.post_calls.append((path, body))
                if self._event_log is not None:
                    self._event_log.append("post")
                if path != PRIVATE_CLOSE_ORDER:
                    return {"status": 0, "data": None}
                self.close_attempts += 1
                settled = set(_settle_position_ids(body))
                # 建玉は消える（＝受理されていた）が、応答は拒否
                self._positions = [
                    p
                    for p in self._positions
                    if _position_key(p.get("positionId")) not in settled
                ]
                if self.close_attempts == 1:
                    return dict(_ERR_423_BODY)
                return {"status": 0, "data": None}

        # 11 件 = closeOrder 2 チャンク
        client = RejectButVanishedClient(positions=_make_positions(11), event_log=events)
        with patch("backend.src.batch.cancel_first.time.sleep") as mock_sleep:
            mock_sleep.side_effect = lambda _s: events.append("sleep")
            run_cancel_first(client)

        # 1 本目は ALREADY_CLOSED で return するが、その後も必ず sleep が入る
        assert events == ["post", "sleep", "post", "sleep"], events

    def test_every_close_post_is_followed_by_sleep_on_retry_path(self) -> None:
        """再送経路でも POST の直後に必ず sleep が入ること."""
        events: list[str] = []
        client = _CloseFailNTimesClient(
            positions=[_BUY_POSITION], fail_times=1, event_log=events
        )
        with patch("backend.src.batch.cancel_first.time.sleep") as mock_sleep:
            mock_sleep.side_effect = lambda _s: events.append("sleep")
            run_cancel_first(client)

        # 拒否 → sleep → 再送 → sleep（チャンク境界）
        assert events == ["post", "sleep", "post", "sleep"], events


class TestStep0MarketStatus:
    """#207 (c): Step0 は公式ステータス（GET /public/v1/status）で開閉を判定する。

    🔴 2026-08-08(土) の実測で、**閉場中でも参照系 GET は HTTP 200 + status:0** を返すと
    確認された（付録B docs/spikes/market_status_closed.md）。
    従来の代理判定（openPositions が通れば取引時間内）では閉場を検知できない。
    """

    @pytest.mark.parametrize("closed_status", ["CLOSE", "MAINTENANCE"])
    def test_closed_status_stops_before_any_post(self, closed_status: str) -> None:
        """🔴 スパイクで実測した状況の回帰: 参照系 GET は正常でも閉場なら止まる。"""
        # market_open=True → 参照系 GET は 200 / status:0 を返す（＝閉場中の実挙動）
        client = MockForexClient(
            positions=[_BUY_POSITION],
            new_orders=[_NEW_ORDER],
            settlement_orders=[_SETTLEMENT_ORDER],
            market_open=True,
            market_status=closed_status,
        )
        with _patch_sleep():
            result = run_cancel_first(client)

        assert result.market_closed is True
        assert result.retry_next is True
        assert client.post_calls == [], "閉場中に POST を送ってはいけない"

    def test_open_status_proceeds(self) -> None:
        client = MockForexClient(positions=[_BUY_POSITION], market_status="OPEN")
        with _patch_sleep():
            result = run_cancel_first(client)

        assert result.market_closed is False
        assert result.step3_closed_ids == ["1000342"]

    def test_unknown_status_is_treated_as_closed(self) -> None:
        """未知の値・空は閉場扱い（fail-safe）。"""
        client = MockForexClient(positions=[_BUY_POSITION], market_status="")
        with _patch_sleep():
            result = run_cancel_first(client)

        assert result.market_closed is True
        assert client.post_calls == []

    def test_status_fetch_failure_is_treated_as_closed(self) -> None:
        """ステータス取得自体が失敗したら閉場扱い（撃たない）。"""

        class StatusBlindClient(MockForexClient):
            def get_market_status(self) -> str:
                raise RuntimeError("network down")

        client = StatusBlindClient(positions=[_BUY_POSITION])
        with _patch_sleep():
            result = run_cancel_first(client)

        assert result.market_closed is True
        assert client.post_calls == []

    def test_open_but_broken_api_is_still_closed(self) -> None:
        """市場は OPEN でも参照系 GET が壊れていれば止まる（API 障害検知は維持）。"""

        class BrokenApiClient(MockForexClient):
            def private_get(
                self, path: str, params: dict[str, Any] | None = None
            ) -> dict[str, Any]:
                if path == PRIVATE_OPEN_POSITIONS:
                    self.open_positions_get_count += 1
                    return dict(_GMO_ERROR_BODY)
                return super().private_get(path, params)

        client = BrokenApiClient(positions=[_BUY_POSITION], market_status="OPEN")
        with _patch_sleep():
            result = run_cancel_first(client)

        assert result.market_closed is True
        assert client.post_calls == []

    def test_status_is_checked_before_private_calls(self) -> None:
        """閉場なら Private GET すら投げない（無駄な署名リクエストを出さない）。"""
        client = MockForexClient(positions=[_BUY_POSITION], market_status="CLOSE")
        with _patch_sleep():
            run_cancel_first(client)

        assert client.get_calls == []
