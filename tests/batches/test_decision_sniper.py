"""朝バッチ統合テスト（§2, §3.5, §7.1）。

受け入れ条件:
- 休場日 → skipped / LLM・発注呼び出しなし
- 既存 activeOrder → 二重発注防止（skipped）
- 既存 openPosition → 二重発注防止（skipped）
- DynamoDB 原子ロック獲得失敗 → skipped
- activeOrders/openPositions 取得失敗 → fail-closed（skipped）
- MOCK_MODE 時 POST 外部 HTTP 0 件（OrderBlockedError = スタブ）
- 正常フロー（stubbed）で status=stubbed を返す
- entry/TP が LLM 出力 tick ズレでも validation を通過する（M-1）
- 価格序列違反・R:R 不足 → validation_failed（発注なし）
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import httpx
import pytest

from backend.src.adapters.economic_calendar import CalendarResult
from backend.src.batch.decision_batch import _run, lambda_handler
from backend.src.clients.gmo_client import GmoFxClient, OrderBlockedError
from backend.src.gmo_fx_endpoints import PRIVATE_IFO_ORDER
from backend.src.persistence.dynamodb_store import LockAcquireOutcome

JST = ZoneInfo("Asia/Tokyo")
TODAY = date(2026, 6, 30)  # 月曜
NOW_JST = datetime(2026, 6, 30, 7, 30, 0, tzinfo=JST)


@pytest.fixture(autouse=True)
def _no_real_sleep() -> Any:
    """データ取得間の `time.sleep(1)`（レート抑制）を実待ちさせない。

    本番フローの sleep は安全要件（§7.2）として保持しつつ、テストの実時間だけ短縮する。
    レート制御そのものの検証は rate_limiter のユニットテストが担う。
    """
    with patch("backend.src.batch.decision_batch.time.sleep"):
        yield


@pytest.fixture(autouse=True)
def _mock_economic_calendar() -> Any:
    """ForexFactory 外部 HTTP を避け、day-skip 判定を空カレンダーで固定する。"""
    with patch(
        "backend.src.batch.decision_batch._fetch_economic_events",
        return_value=([], CalendarResult()),
    ):
        yield

# ---------------------------------------------------------------------------
# ヘルパー: モック daily candle / LLM decision
# ---------------------------------------------------------------------------

_CANDLE = {
    "openTime": "1751212800000",  # 2025-06-29 JST (UTC+9)
    "open": "157.000",
    "high": "158.500",
    "low": "156.500",
    "close": "157.800",
    "volume": "12345",
}

_CANDLES_15 = [_CANDLE] * 15

_TICKER = [{"bid": "157.505", "ask": "157.510"}]

_LLM_DECISION_LONG = {
    "direction": "LONG",
    "execution_required": True,
    "order_type": "LIMIT",
    # entry=157.2001 は意図的 tick ズレ（M-1 丸めテスト用）→ 丸め後 157.200
    # SL = 157.200 - ATR(≈2.0)*1.5 = 154.200、risk=3.000
    # TP は risk >= 3.0 確保: 160.5001 → 160.500、reward=3.300 → R:R=1.1 ≥ 1.0
    "entry_price": 157.2001,
    "take_profit": 160.5001,
    "referenced_pivot_level": "R1",
    "sl_profile": "wide",
    "rationale": "テスト用 LONG 判断",
    "sentiment_summary": None,
}


def _make_mock_client(
    active_orders: list[Any] | None = None,
    open_positions: list[Any] | None = None,
    active_orders_status: int = 200,
    open_positions_status: int = 200,
    klines_daily: list[dict[str, str]] | None = None,
    klines_4h: list[dict[str, str]] | None = None,
    ticker: list[Any] | None = None,
    equity: float = 500_000.0,
) -> MagicMock:
    """テスト用 GmoFxClient モックを組み立てる。"""
    client = MagicMock()

    def _public_get(path: str, params: dict[str, Any] | None = None) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        if "klines" in path:
            interval = (params or {}).get("interval", "1day")
            if interval == "4hour":
                # 正しいエンベロープ: {"status": 0, "data": [...]}（YYYYMMDD は 404, date=YYYY を使用）
                resp.json.return_value = {"status": 0, "data": klines_4h or []}
            else:
                # date=YYYY で年全体を返す。status=0 が必須
                resp.json.return_value = {"status": 0, "data": klines_daily or _CANDLES_15}
        elif "ticker" in path:
            resp.json.return_value = {
                "status": 0,
                "data": ticker if ticker is not None else _TICKER,
            }
        else:
            resp.json.return_value = {"data": []}
        return resp

    def _private_get(path: str, params: dict[str, Any] | None = None) -> MagicMock:
        resp = MagicMock()
        if "activeOrders" in path:
            resp.status_code = active_orders_status
            resp.json.return_value = {"status": 0, "data": {"list": active_orders or []}}
        elif "openPositions" in path:
            resp.status_code = open_positions_status
            resp.json.return_value = {"status": 0, "data": {"list": open_positions or []}}
        elif "assets" in path:
            resp.status_code = 200
            resp.json.return_value = {
                "status": 0,
                "data": {"equity": str(equity), "availableAmount": str(equity)},
            }
        else:
            resp.status_code = 200
            resp.json.return_value = {"data": {}}
        return resp

    client.public_get.side_effect = _public_get
    client.private_get.side_effect = _private_get
    client.private_post.side_effect = OrderBlockedError("phase1 stub")

    return client


def _make_llm_result(decision_dict: dict[str, Any] | None = None) -> Any:
    from backend.src.llm.client import LlmResult
    from backend.src.llm.trade_decision import TradeDecision

    d = decision_dict or _LLM_DECISION_LONG
    decision = TradeDecision(**d)
    return LlmResult(
        decision=decision,
        input_tokens=100,
        output_tokens=50,
        model="claude-sonnet-4-6",
    )


def _common_patches(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """正常フロー用の共通 patch コンテキストを返すジェネレータ。"""
    patches = {
        "backend.src.persistence.dynamodb_store.acquire_lock_outcome": LockAcquireOutcome(acquired=True),
        "backend.src.persistence.s3_logger.save_decision_log": None,
        "backend.src.batch.decision_batch.generate_decision": _make_llm_result(),
        "backend.src.batch.decision_batch.get_confirmed_daily_candles": _CANDLES_15,
        # §3.3: 外部カレンダー API を叩かない（autouse fixture と同型）
        "backend.src.batch.decision_batch._fetch_economic_events": ([], CalendarResult()),
        # §3.3 補助入力: ネットワーク呼び出しを避けるため degrade（None）でスタブ
        "backend.src.batch.decision_batch._fetch_macro_context": None,
        **(extra or {}),
    }
    return patches


# ---------------------------------------------------------------------------
# 休場日テスト（lambda_handler 経由）
# ---------------------------------------------------------------------------


class TestHolidayEarlyReturn:
    def test_holiday_skipped(self) -> None:
        with (
            patch("backend.src.batch.decision_batch._get_today_jst", return_value=date(2026, 1, 1)),
            patch("backend.src.batch.decision_batch._get_now_jst", return_value=NOW_JST),
        ):
            result = lambda_handler({}, None)

        assert result["status"] == "skipped"
        assert result["reason"] == "market_holiday"

    def test_holiday_llm_not_called(self) -> None:
        with (
            patch("backend.src.batch.decision_batch._get_today_jst", return_value=date(2026, 1, 1)),
            patch("backend.src.batch.decision_batch._get_now_jst", return_value=NOW_JST),
            patch("backend.src.batch.decision_batch.generate_decision") as mock_llm,
        ):
            lambda_handler({}, None)

        mock_llm.assert_not_called()

    def test_holiday_no_external_http(self) -> None:
        """休場日起動で GmoFxClient のリクエストが発生しないこと。"""
        with (
            patch("backend.src.batch.decision_batch._get_today_jst", return_value=date(2026, 1, 1)),
            patch("backend.src.batch.decision_batch._get_now_jst", return_value=NOW_JST),
            patch("backend.src.batch.decision_batch.GmoFxClient") as mock_gmo_client,
        ):
            lambda_handler({}, None)

        instance = mock_gmo_client.return_value.__enter__.return_value
        instance.public_get.assert_not_called()
        instance.private_get.assert_not_called()
        instance.private_post.assert_not_called()


# ---------------------------------------------------------------------------
# 冪等性チェック（二重発注防止）
# ---------------------------------------------------------------------------


class TestIdempotency:
    def _run_with_patches(self, client: MagicMock) -> dict[str, Any]:
        with (
            patch(
                "backend.src.persistence.dynamodb_store.acquire_lock_outcome",
                return_value=LockAcquireOutcome(acquired=True),
            ),
            patch("backend.src.persistence.s3_logger.save_decision_log"),
            patch(
                "backend.src.batch.decision_batch.generate_decision",
                return_value=_make_llm_result(),
            ),
            patch(
                "backend.src.batch.decision_batch.get_confirmed_daily_candles",
                return_value=_CANDLES_15,
            ),
        ):
            return _run(client=client, today=TODAY, now_jst=NOW_JST)

    def test_active_orders_skip(self) -> None:
        client = _make_mock_client(active_orders=[{"orderId": "999"}])
        result = self._run_with_patches(client)
        assert result["status"] == "skipped"
        assert result["reason"] == "active_orders_exist"

    def test_open_positions_skip(self) -> None:
        client = _make_mock_client(open_positions=[{"positionId": "888"}])
        result = self._run_with_patches(client)
        assert result["status"] == "skipped"
        assert result["reason"] == "open_positions_exist"

    def test_dynamodb_lock_skip(self) -> None:
        """DynamoDB 原子ロック獲得失敗（既存ロック）→ skipped。"""
        client = _make_mock_client()
        with (
            patch(
                "backend.src.persistence.dynamodb_store.acquire_lock_outcome",
                return_value=LockAcquireOutcome(acquired=False, lock_exists=True),
            ),
            patch("backend.src.persistence.s3_logger.save_decision_log"),
            patch(
                "backend.src.batch.decision_batch.generate_decision",
                return_value=_make_llm_result(),
            ),
            patch(
                "backend.src.batch.decision_batch.get_confirmed_daily_candles",
                return_value=_CANDLES_15,
            ),
        ):
            result = _run(client=client, today=TODAY, now_jst=NOW_JST)
        assert result["status"] == "skipped"
        assert result["reason"] == "dynamodb_lock_exists"

    def test_dynamodb_lock_unavailable(self) -> None:
        """#210 L-3: DDB 障害は dynamodb_lock_unavailable + Slack。"""
        client = _make_mock_client()
        with (
            patch(
                "backend.src.persistence.dynamodb_store.acquire_lock_outcome",
                return_value=LockAcquireOutcome(acquired=False, lock_exists=False),
            ),
            patch("backend.src.persistence.s3_logger.save_decision_log"),
            patch(
                "backend.src.batch.decision_batch.generate_decision",
                return_value=_make_llm_result(),
            ),
            patch(
                "backend.src.batch.decision_batch.get_confirmed_daily_candles",
                return_value=_CANDLES_15,
            ),
            patch("backend.src.batch.lock_acquire.send_slack_alert") as slack,
        ):
            result = _run(client=client, today=TODAY, now_jst=NOW_JST)
        assert result["status"] == "skipped"
        assert result["reason"] == "dynamodb_lock_unavailable"
        slack.assert_called_once()

    def test_no_double_order_when_active_order(self) -> None:
        """既存 activeOrder がある場合に LLM と private_post が呼ばれないこと。"""
        client = _make_mock_client(active_orders=[{"orderId": "999"}])
        with patch("backend.src.batch.decision_batch.generate_decision") as mock_llm:
            _run(client=client, today=TODAY, now_jst=NOW_JST)

        mock_llm.assert_not_called()
        client.private_post.assert_not_called()

    def test_fail_closed_on_active_orders_error(self) -> None:
        """activeOrders 取得失敗（例外）→ fail-closed で skipped。"""
        client = _make_mock_client()
        client.private_get.side_effect = Exception("network error")
        result = _run(client=client, today=TODAY, now_jst=NOW_JST)
        assert result["status"] == "skipped"
        assert "fetch_failed" in result["reason"]

    def test_fail_closed_on_active_orders_non200(self) -> None:
        """activeOrders が非 200 → fail-closed で skipped。"""
        client = _make_mock_client(active_orders_status=500)
        result = _run(client=client, today=TODAY, now_jst=NOW_JST)
        assert result["status"] == "skipped"
        assert result["reason"] == "active_orders_fetch_failed"

    def test_fail_closed_on_open_positions_non200(self) -> None:
        """openPositions が非 200 → fail-closed で skipped。"""
        client = _make_mock_client(open_positions_status=500)
        result = _run(client=client, today=TODAY, now_jst=NOW_JST)
        assert result["status"] == "skipped"
        assert result["reason"] == "open_positions_fetch_failed"


# ---------------------------------------------------------------------------
# MOCK_MODE 時 POST 外部 HTTP 0 件
# ---------------------------------------------------------------------------


class TestNoExternalPostInPhase1:
    def test_no_external_post_calls(self) -> None:
        """Phase 1 で two-key interlock により private_post が呼ばれず HTTP 送信も 0 件。

        _run 内の two-key チェック（ALLOW_LIVE_ORDERS + TRADING_PHASE）により
        _place_ifo_order 自体が呼ばれないため、private_post は一切呼ばれない。
        """
        client = _make_mock_client()

        with (
            patch.dict("os.environ", {"ALLOW_LIVE_ORDERS": "", "TRADING_PHASE": "1"}),
            patch(
                "backend.src.persistence.dynamodb_store.acquire_lock_outcome",
                return_value=LockAcquireOutcome(acquired=True),
            ),
            patch(
                "backend.src.persistence.dynamodb_store.write_order_id"
            ) as mock_write_order_id,
            patch("backend.src.persistence.s3_logger.save_decision_log"),
            patch(
                "backend.src.batch.decision_batch.generate_decision",
                return_value=_make_llm_result(),
            ),
            patch(
                "backend.src.batch.decision_batch.get_confirmed_daily_candles",
                return_value=_CANDLES_15,
            ),
        ):
            result = _run(client=client, today=TODAY, now_jst=NOW_JST)

        # two-key interlock でブロックされ status=stubbed
        assert result["status"] == "stubbed"
        assert result["reason"] == "live_orders_disabled"
        # private_post は一切呼ばれない（HTTP 送信 0 件）
        client.private_post.assert_not_called()
        mock_write_order_id.assert_not_called()

    def test_order_blocked_if_only_one_key_set(self) -> None:
        """ALLOW_LIVE_ORDERS のみ True でも（TRADING_PHASE 未設定）スタブ応答を返すこと。"""
        client = _make_mock_client()

        with (
            patch.dict("os.environ", {"ALLOW_LIVE_ORDERS": "True", "TRADING_PHASE": "1"}),
            patch(
                "backend.src.persistence.dynamodb_store.acquire_lock_outcome",
                return_value=LockAcquireOutcome(acquired=True),
            ),
            patch("backend.src.persistence.s3_logger.save_decision_log"),
            patch(
                "backend.src.batch.decision_batch.generate_decision",
                return_value=_make_llm_result(),
            ),
            patch(
                "backend.src.batch.decision_batch.get_confirmed_daily_candles",
                return_value=_CANDLES_15,
            ),
        ):
            result = _run(client=client, today=TODAY, now_jst=NOW_JST)

        assert result["stubbed"] is True
        assert result["orderId"] is None
        client.private_post.assert_not_called()


# ---------------------------------------------------------------------------
# M-1: entry/TP tick 丸め → validation 通過
# ---------------------------------------------------------------------------


class TestEntryTpTickRounding:
    def test_tick_rounded_entry_passes_validation(self) -> None:
        """LLM が微小ズレの entry_price/take_profit を出力しても tick 丸めでバリデーションを通ること。"""
        decision_with_tick_error = {
            **_LLM_DECISION_LONG,
            "entry_price": 157.2001,
            "take_profit": 160.5001,
        }
        client = _make_mock_client()

        with (
            patch(
                "backend.src.persistence.dynamodb_store.acquire_lock_outcome",
                return_value=LockAcquireOutcome(acquired=True),
            ),
            patch("backend.src.persistence.s3_logger.save_decision_log"),
            patch(
                "backend.src.batch.decision_batch.generate_decision",
                return_value=_make_llm_result(decision_with_tick_error),
            ),
            patch(
                "backend.src.batch.decision_batch.get_confirmed_daily_candles",
                return_value=_CANDLES_15,
            ),
        ):
            result = _run(client=client, today=TODAY, now_jst=NOW_JST)

        assert result.get("reason") != "validation_failed", f"validation failed: {result}"
        entry = result.get("entry_price")
        if entry is not None:
            from decimal import Decimal

            d = Decimal(str(entry)).quantize(Decimal("0.001"))
            assert Decimal(str(entry)) == d, f"entry_price {entry} is not tick-aligned"


# ---------------------------------------------------------------------------
# バリデーション失敗テスト
# ---------------------------------------------------------------------------


class TestValidationFailure:
    def _run_with(self, decision_dict: dict[str, Any]) -> dict[str, Any]:
        client = _make_mock_client()
        with (
            patch(
                "backend.src.persistence.dynamodb_store.acquire_lock_outcome",
                return_value=LockAcquireOutcome(acquired=True),
            ),
            patch("backend.src.persistence.s3_logger.save_decision_log"),
            patch(
                "backend.src.batch.decision_batch.generate_decision",
                return_value=_make_llm_result(decision_dict),
            ),
            patch(
                "backend.src.batch.decision_batch.get_confirmed_daily_candles",
                return_value=_CANDLES_15,
            ),
        ):
            return _run(client=client, today=TODAY, now_jst=NOW_JST)

    def test_entry_distance_violation_no_order(self) -> None:
        """現値乖離上限違反（entry が現値から遠すぎ）→ validation_failed で発注しないこと（§3.4・Ver 3.6）。

        Ver 3.6 では SL/TP を Python が決定論算出するため、LLM 由来の価格序列違反・R:R 違反は
        構造的に起きない（→ 発生し得る新しい発注前拒否は現値乖離上限）。現値 mid≈157.505 から
        遠い entry=154.000 を出させ、MAX_ENTRY_DISTANCE_VIOLATION で発注が止まることを確認する。
        """
        bad_decision = {**_LLM_DECISION_LONG, "entry_price": 154.000}
        client = _make_mock_client()
        with (
            patch(
                "backend.src.persistence.dynamodb_store.acquire_lock_outcome",
                return_value=LockAcquireOutcome(acquired=True),
            ),
            patch("backend.src.persistence.s3_logger.save_decision_log"),
            patch(
                "backend.src.batch.decision_batch.generate_decision",
                return_value=_make_llm_result(bad_decision),
            ),
            patch(
                "backend.src.batch.decision_batch.get_confirmed_daily_candles",
                return_value=_CANDLES_15,
            ),
        ):
            result = _run(client=client, today=TODAY, now_jst=NOW_JST)

        assert result["status"] == "no_entry"
        assert result["reason"] == "validation_failed"
        assert "MAX_ENTRY_DISTANCE" in result["detail"]
        client.private_post.assert_not_called()


# ---------------------------------------------------------------------------
# 正常フロー（スタブ）
# ---------------------------------------------------------------------------


class TestNormalFlowStubbed:
    def test_stubbed_status_returned(self) -> None:
        """正常フローでスタブ実行 → status=stubbed を返すこと。"""
        client = _make_mock_client()

        with (
            patch(
                "backend.src.persistence.dynamodb_store.acquire_lock_outcome",
                return_value=LockAcquireOutcome(acquired=True),
            ),
            patch(
                "backend.src.persistence.dynamodb_store.write_order_id"
            ) as mock_write_order_id,
            patch("backend.src.persistence.s3_logger.save_decision_log"),
            patch(
                "backend.src.batch.decision_batch.generate_decision",
                return_value=_make_llm_result(),
            ),
            patch(
                "backend.src.batch.decision_batch.get_confirmed_daily_candles",
                return_value=_CANDLES_15,
            ),
        ):
            result = _run(client=client, today=TODAY, now_jst=NOW_JST)

        assert result["status"] == "stubbed"
        assert result["direction"] == "LONG"
        mock_write_order_id.assert_not_called()

    def test_dynamodb_lock_acquired_before_order(self) -> None:
        """ロック取得 → private_post の順序を検証する（安全ガード回帰防止）。

        ALLOW_LIVE_ORDERS=True かつ TRADING_PHASE=2 でライブ発注フローを起動し、
        events リストで acquire_lock が private_post より前に実行されることを確認する。
        """
        client = _make_mock_client()
        events: list[str] = []

        def _lock_side_effect(*args: Any, **kwargs: Any) -> LockAcquireOutcome:
            events.append("lock")
            return LockAcquireOutcome(acquired=True)

        post_resp = MagicMock()
        post_resp.status_code = 200
        post_resp.json.return_value = {"data": "ORD-001"}

        def _post_side_effect(*args: Any, **kwargs: Any) -> MagicMock:
            events.append("post")
            return post_resp

        client.private_post.side_effect = _post_side_effect

        with (
            patch.dict("os.environ", {"ALLOW_LIVE_ORDERS": "True", "TRADING_PHASE": "2"}),
            patch(
                "backend.src.persistence.dynamodb_store.acquire_lock_outcome",
                side_effect=_lock_side_effect,
            ) as mock_lock,
            patch(
                "backend.src.persistence.dynamodb_store.write_order_id", return_value=True
            ),
            patch("backend.src.persistence.s3_logger.save_decision_log"),
            patch(
                "backend.src.batch.decision_batch.generate_decision",
                return_value=_make_llm_result(),
            ),
            patch(
                "backend.src.batch.decision_batch.get_confirmed_daily_candles",
                return_value=_CANDLES_15,
            ),
        ):
            _run(client=client, today=TODAY, now_jst=NOW_JST)

        mock_lock.assert_called_once_with(today=TODAY)
        assert "lock" in events
        assert "post" in events
        assert events.index("lock") < events.index("post"), "ロック取得が発注より後になっている"

    def test_no_entry_when_direction_no_entry(self) -> None:
        """LLM が NO_ENTRY を返した場合に発注しないこと。"""
        no_entry_decision = {
            "direction": "NO_ENTRY",
            "execution_required": False,
            "rationale": "不明確な地合い",
            "sl_profile": "wide",
        }
        client = _make_mock_client()

        with (
            patch(
                "backend.src.persistence.dynamodb_store.acquire_lock_outcome",
                return_value=LockAcquireOutcome(acquired=True),
            ),
            patch(
                "backend.src.persistence.dynamodb_store.write_order_id"
            ) as mock_write_order_id,
            patch("backend.src.persistence.s3_logger.save_decision_log"),
            patch(
                "backend.src.batch.decision_batch.generate_decision",
                return_value=_make_llm_result(no_entry_decision),
            ),
            patch(
                "backend.src.batch.decision_batch.get_confirmed_daily_candles",
                return_value=_CANDLES_15,
            ),
        ):
            result = _run(client=client, today=TODAY, now_jst=NOW_JST)

        assert result["status"] == "no_entry"
        client.private_post.assert_not_called()
        mock_write_order_id.assert_not_called()


# ---------------------------------------------------------------------------
# §3.3 補助入力: マクロ取得失敗時の degrade テスト（Issue #107）
# ---------------------------------------------------------------------------


class TestMacroDegradeDoesNotCauseNoEntry:
    """マクロ取得失敗時にバッチが NO_ENTRY に倒れず degrade して続行することを確認する。"""

    def test_macro_fetch_failure_does_not_stop_batch(self) -> None:
        """_fetch_macro_context が None を返してもバッチが正常完了（no_entry or stubbed）する。"""
        client = _make_mock_client()

        with (
            patch(
                "backend.src.persistence.dynamodb_store.acquire_lock_outcome",
                return_value=LockAcquireOutcome(acquired=True),
            ),
            patch("backend.src.persistence.s3_logger.save_decision_log"),
            patch(
                "backend.src.batch.decision_batch.generate_decision",
                return_value=_make_llm_result(),
            ),
            patch(
                "backend.src.batch.decision_batch.get_confirmed_daily_candles",
                return_value=_CANDLES_15,
            ),
            patch(
                "backend.src.batch.decision_batch._fetch_macro_context",
                return_value=None,
            ),
        ):
            result = _run(client=client, today=TODAY, now_jst=NOW_JST)

        # macro fetch 失敗で NO_ENTRY 固定にはなっていないこと
        assert result["status"] in {"stubbed", "no_entry", "validation_failed"}
        # no_entry の reason が macro 由来ではないこと（macro degrade は reason を出さない）
        if result["status"] == "no_entry":
            assert result.get("reason") != "macro_fetch_failed"


# ---------------------------------------------------------------------------
# two-key interlock — 実 GmoFxClient を通した HTTP 送信 0 件と署名パスの検証
# ---------------------------------------------------------------------------


class TestTwoKeyInterlockViaRealClient:
    """実 GmoFxClient.private_post() を呼んで two-key interlock を検証する。

    Phase 1 では HTTP transport まで到達せず OrderBlockedError が送出されることを確認する。
    また、署名パスが /v1/... 形式（/private を含まない）であることを to_sign_path で確認する（§7.2）。
    """

    def test_no_http_post_in_phase1(self) -> None:
        """Phase 1 で private_post が HTTP 送信まで到達しないこと。

        実 GmoFxClient を使用し、httpx.Client.request のみをモック。
        ALLOW_LIVE_ORDERS / TRADING_PHASE が未設定（Phase 1）の状態で
        OrderBlockedError が送出され、httpx の request は 0 回であることを確認する。
        """
        mock_http = MagicMock(spec=httpx.Client)

        with (
            patch.dict("os.environ", {"ALLOW_LIVE_ORDERS": "", "TRADING_PHASE": "1"}),
            GmoFxClient(
                api_key="test-key",
                api_secret="test-secret",
                http_client=mock_http,
            ) as gmo_client,
            pytest.raises(OrderBlockedError),
        ):
            gmo_client.private_post(
                PRIVATE_IFO_ORDER,
                body={"symbol": "USD_JPY", "side": "BUY", "size": "100"},
            )

        # HTTP transport へのリクエストが 0 件であること（全メソッド含む）
        assert mock_http.method_calls == []

    def test_private_post_keeps_request_and_sign_paths_separate(self) -> None:
        """private_post が request_path=/private/v1/... と sign_path=/v1/... を分離して渡すこと。

        §7.2 / AGENTS.md #2: HMAC 署名パスは /v1/...（/private を含まない）。
        リクエスト URL パスは /private/v1/...。両者を別引数で渡す契約を固定する。
        """
        mock_http = MagicMock(spec=httpx.Client)

        with (
            patch.dict("os.environ", {"ALLOW_LIVE_ORDERS": "True", "TRADING_PHASE": "2"}),
            patch("backend.src.clients.gmo_client.request_with_retry") as mock_rwr,
            GmoFxClient(api_key="k", api_secret="s", http_client=mock_http) as gmo_client,
        ):
            mock_rwr.return_value = MagicMock(status_code=200)
            gmo_client.private_post(PRIVATE_IFO_ORDER, body={})

        kwargs = mock_rwr.call_args.kwargs
        assert kwargs["request_path"] == "/private/v1/ifoOrder"
        assert kwargs["sign_path"] == "/v1/ifoOrder"
        # HTTP transport は request_with_retry に委ねるため直接呼ばれない
        assert mock_http.method_calls == []

    def test_two_key_both_required(self) -> None:
        """ALLOW_LIVE_ORDERS のみ True でも（TRADING_PHASE が未設定）OrderBlockedError。"""
        mock_http = MagicMock(spec=httpx.Client)

        with (
            patch.dict("os.environ", {"ALLOW_LIVE_ORDERS": "True", "TRADING_PHASE": "1"}),
            GmoFxClient(api_key="k", api_secret="s", http_client=mock_http) as gmo_client,
            pytest.raises(OrderBlockedError),
        ):
            gmo_client.private_post(PRIVATE_IFO_ORDER, body={})

        assert mock_http.method_calls == []
