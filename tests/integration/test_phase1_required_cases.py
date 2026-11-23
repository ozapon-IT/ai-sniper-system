"""フェーズ1必須テストケース（§7.1）。

§7.1 が定義する3つの必須ケースを専用テストとして実装する:
1. 月曜 PM 8:30 起動 — 前日足 = 金曜日足を使用すること
2. 休場日起動 — 早期 return (status=skipped)
3. 署名パス /v1/ifoOrder 一致

外部 HTTP を一切発生させない（external_api_calls == 0）。
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from backend.src.adapters.economic_calendar import CalendarResult
from backend.src.batch.decision_batch import _run as morning_run
from backend.src.batch.decision_batch import lambda_handler
from backend.src.gmo_fx_endpoints import PRIVATE_IFO_ORDER, to_sign_path
from backend.src.persistence.dynamodb_store import LockAcquireOutcome


@pytest.fixture(autouse=True)
def _mock_gmo_credentials() -> Any:
    """全テストで get_gmo_credentials を自動モックする（Secrets Manager 不要）。"""
    with patch(
        "backend.src.batch.decision_batch.get_gmo_credentials",
        return_value=("test_key", "test_secret"),
    ):
        yield

JST = ZoneInfo("Asia/Tokyo")

# ---------------------------------------------------------------------------
# テスト用定数
# ---------------------------------------------------------------------------

_FRIDAY = date(2026, 6, 26)   # 金曜
_MONDAY = date(2026, 6, 29)   # 月曜（前日足 = 金曜）
_HOLIDAY = date(2026, 1, 1)   # 元日（full_close 休場日）

_FRIDAY_CANDLE: dict[str, str] = {
    "openTime": "1782399600000",  # 2026-06-26 00:00 JST = 2026-06-25 15:00 UTC
    "open": "156.000",
    "high": "156.800",
    "low": "155.600",
    "close": "156.400",
    "volume": "8888",
}
_CANDLES_15 = [_FRIDAY_CANDLE] * 15

_TICKER = [{"bid": "156.799", "ask": "156.800"}]
# E04 確定形（#89/#92）: assets の data は単一 dict・equity/availableAmount（list や旧名 available は誤り）。
_ACCOUNT = {"equity": "600000", "availableAmount": "500000"}


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------


def _build_mock_client(
    candle: dict[str, str] | None = None,
    *,
    ticker: list[dict[str, str]] | None = None,
) -> MagicMock:
    from backend.src.gmo_fx_endpoints import (
        PRIVATE_ACCOUNT_ASSETS,
        PRIVATE_ACTIVE_ORDERS,
        PRIVATE_OPEN_POSITIONS,
        PUBLIC_KLINES,
        PUBLIC_TICKER,
    )

    c = candle or _FRIDAY_CANDLE
    quote = ticker if ticker is not None else _TICKER
    mock = MagicMock()

    def _public_get(path: str, params: dict[str, Any] | None = None) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        if path == PUBLIC_KLINES:
            # date=YYYY 形式で年全体を返す。status=0 が必須（YYYYMMDD は 404）
            resp.json.return_value = {"status": 0, "data": [c] * 15}
        elif path == PUBLIC_TICKER:
            resp.json.return_value = {"status": 0, "data": quote}
        else:
            # 想定外パスを黙って成功扱いにするとパス取り違えを隠す（実資金系）。即失敗させる。
            raise AssertionError(f"Unexpected public_get path in test mock: {path}")
        return resp

    def _private_get(path: str, params: dict[str, Any] | None = None) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        if path in (PRIVATE_ACTIVE_ORDERS, PRIVATE_OPEN_POSITIONS):
            resp.json.return_value = {"status": 0, "data": {"list": []}}
        elif path == PRIVATE_ACCOUNT_ASSETS:
            resp.json.return_value = {"status": 0, "data": _ACCOUNT}
        else:
            raise AssertionError(f"Unexpected private_get path in test mock: {path}")
        return resp

    mock.public_get.side_effect = _public_get
    mock.private_get.side_effect = _private_get
    mock.__enter__ = lambda s: s
    mock.__exit__ = MagicMock(return_value=False)
    return mock


def _make_llm_result(entry: float = 155.500) -> Any:
    from backend.src.llm.client import LlmResult
    from backend.src.llm.trade_decision import TradeDecision

    # SL/TP は Python が ATR_h/rr_target から決定論算出する（LLM は出さない・Ver 3.6）
    decision = TradeDecision(
        direction="LONG",
        execution_required=True,
        order_type="LIMIT",
        entry_price=entry,
        referenced_pivot_level="R1",
        sl_profile="standard",
        rr_target=1.0,
        rationale="テスト",
        sentiment_summary=None,
    )
    result = MagicMock(spec=LlmResult)
    result.decision = decision
    result.input_tokens = 10
    result.output_tokens = 5
    result.model = "mock"
    return result


# ---------------------------------------------------------------------------
# 必須ケース 1: 月曜 PM 8:30 — 前日足 = 金曜足
# ---------------------------------------------------------------------------


class TestMondayPivotUsesFridayCandle:
    """月曜 PM 8:30 起動時、前日足が金曜日のローソク足であることを検証する（§7.1）。"""

    def test_monday_decision_uses_friday_candle(self) -> None:
        """実パス（adapters.klines）が月曜 20:30 JST に金曜足を直近確定足として返すこと。

        §3.1 の直近確定取引日ロジックは klines.get_latest_confirmed_daily に一本化されている
        （旧 trading_day.get_latest_confirmed_candle は E05 の date=YYYY 確定により削除済み）。
        日足境界は 06:00 JST（E05 §5）。金曜足は openTime+24h=土 06:00 で月曜 20:30 時点は確定、
        月曜足（当日・進行中）は openTime+24h=火 06:00 で未確定 → 除外される。
        """
        from unittest.mock import patch as _patch

        from backend.src.adapters.klines import get_latest_confirmed_daily
        from backend.src.gmo_fx_endpoints import PUBLIC_KLINES

        def _daily(d: date) -> dict[str, str]:
            open_jst = datetime(d.year, d.month, d.day, 6, 0, 0, tzinfo=JST)
            return {
                "openTime": str(int(open_jst.timestamp() * 1000)),
                "open": "156.000",
                "high": "157.500",
                "low": "155.200",
                "close": "156.800",
            }

        _friday = date(2026, 6, 26)
        # 年足リスト: 金曜（確定）＋ 月曜（当日・進行中）。date=YYYY で年全体が返る想定。
        year_candles = [_daily(_friday), _daily(_MONDAY)]

        mock = MagicMock()
        seen_dates: list[str] = []

        def _public_get(path: str, params: dict[str, Any] | None = None) -> MagicMock:
            # E05 契約を固定: klines は PUBLIC_KLINES へ interval=1day・date=YYYY（4桁）で呼ぶ。
            # 実装が YYYYMMDD や別 endpoint に退行したらこのテストが落ちる。
            assert path == PUBLIC_KLINES, f"直近確定足は PUBLIC_KLINES を呼ぶこと (got {path})"
            assert params is not None
            assert params.get("interval") == "1day", params
            date_param = str(params.get("date", ""))
            assert date_param.isdigit() and len(date_param) == 4, f"date は YYYY 形式であること: {params}"
            seen_dates.append(date_param)
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"status": 0, "data": year_candles}
            return resp

        mock.public_get.side_effect = _public_get

        # 月曜 PM 8:30 JST → UTC aware
        now_utc = datetime(_MONDAY.year, _MONDAY.month, _MONDAY.day, 20, 30, 0, tzinfo=JST).astimezone(
            UTC
        )
        # 年足 15 本未満のため前年フォールバックが走る。その sleep(1) を実待ちさせない。
        with _patch("backend.src.adapters.klines.time.sleep"):
            candle = get_latest_confirmed_daily(mock, now_utc)

        assert str(_MONDAY.year) in seen_dates, f"当年（YYYY）で klines を取得すること (got {seen_dates})"

        assert candle is not None, "月曜 20:30 に確定日足が取得できること"
        open_ts = int(candle["openTime"]) // 1000
        candle_date = (
            datetime.fromtimestamp(open_ts, tz=UTC).astimezone(JST).date()
        )
        assert candle_date.weekday() == 4, f"前日足は金曜（weekday=4）であること (got {candle_date})"

    def test_monday_decision_batch_uses_friday_candle_in_pivot(self) -> None:
        """判断バッチが月曜 20:30 JST 起動でピボット計算に金曜足を使うこと（§7.1）。"""
        mock_client = _build_mock_client(candle=_FRIDAY_CANDLE)

        captured_pivot_args: list[dict[str, float]] = []

        def _spy_pivot(**kwargs: Any) -> Any:
            captured_pivot_args.append(kwargs)
            from backend.src.engine.pivot import PivotLevels

            return PivotLevels(
                p=156.167,
                r1=157.133,
                r2=158.067,
                s1=155.233,
                s2=154.267,
            )

        now_jst = datetime(_MONDAY.year, _MONDAY.month, _MONDAY.day, 20, 30, 0, tzinfo=JST)
        with (
            patch("backend.src.batch.decision_batch.get_confirmed_daily_candles", return_value=_CANDLES_15),
            patch("backend.src.batch.decision_batch.calculate_pivot", side_effect=_spy_pivot),
            patch("backend.src.batch.decision_batch.generate_decision", return_value=_make_llm_result()),
            patch(
                "backend.src.batch.decision_batch.dynamodb_store.acquire_lock_outcome",
                return_value=LockAcquireOutcome(acquired=True),
            ),
            patch("backend.src.batch.decision_batch.dynamodb_store.write_order_id", return_value=True),
            patch("backend.src.batch.decision_batch.s3_logger.save_decision_log"),
            patch("backend.src.batch.decision_batch.time.sleep"),
            patch("backend.src.batch.decision_batch._fetch_economic_events", return_value=([], CalendarResult())),
            patch("backend.src.batch.decision_batch._fetch_macro_context", return_value=None),
            patch("backend.src.batch.decision_batch.is_us_thin_market", return_value=False),
        ):
            morning_run(client=mock_client, today=_MONDAY, now_jst=now_jst)

        assert len(captured_pivot_args) == 1
        pivot_input = captured_pivot_args[0]
        # 金曜足の high/low/close で呼ばれていること
        assert pivot_input["high"] == pytest.approx(float(_FRIDAY_CANDLE["high"]))
        assert pivot_input["low"] == pytest.approx(float(_FRIDAY_CANDLE["low"]))
        assert pivot_input["close"] == pytest.approx(float(_FRIDAY_CANDLE["close"]))


# ---------------------------------------------------------------------------
# 必須ケース 2: 休場日起動 — 早期 return (status=skipped)
# ---------------------------------------------------------------------------


class TestHolidayEarlyReturn:
    """休場日起動時、外部 API 呼び出しなしで skipped を返すこと（§7.1, §3.5）。"""

    def test_holiday_returns_skipped(self) -> None:
        with patch(
            "backend.src.batch.decision_batch.is_market_holiday", return_value=True
        ), patch("backend.src.clients.gmo_client.GmoFxClient") as mock_cls:
            mock_cls.return_value.__enter__ = lambda s: s
            mock_cls.return_value.__exit__ = MagicMock(return_value=False)

            result = lambda_handler({}, None)

        assert result["status"] == "skipped"
        assert result.get("reason") == "market_holiday"

    def test_holiday_no_external_api_calls(self) -> None:
        """休場日は GET/POST とも 0 件であること（external_api_calls == 0）。"""
        mock_client = _build_mock_client()

        with (
            patch("backend.src.batch.decision_batch.is_market_holiday", return_value=True),
            patch(
                "backend.src.clients.gmo_client.GmoFxClient", return_value=mock_client
            ),
        ):
            lambda_handler({}, None)

        mock_client.public_get.assert_not_called()
        mock_client.private_get.assert_not_called()
        mock_client.private_post.assert_not_called()

    def test_holiday_llm_not_called(self) -> None:
        """休場日は LLM を呼ばないこと。"""
        with (
            patch("backend.src.batch.decision_batch.is_market_holiday", return_value=True),
            patch(
                "backend.src.batch.decision_batch.generate_decision"
            ) as mock_llm,
            patch("backend.src.clients.gmo_client.GmoFxClient"),
        ):
            lambda_handler({}, None)

        mock_llm.assert_not_called()


# ---------------------------------------------------------------------------
# 必須ケース: 指標 day-skip（夏時間の 21:30 は和集合の内側。#374）
# ---------------------------------------------------------------------------


class TestIndicatorDaySkipRequiredCase:
    """§7.1 / §3.3: 21:30 雇用統計日は LLM 呼び出し前に NO_ENTRY（RD-7 #172）。"""

    def test_employment_day_skips_before_llm(self) -> None:
        from backend.src.adapters.economic_calendar import EconomicEvent as AdapterEvent
        from backend.src.adapters.economic_calendar import ImpactLevel
        from backend.src.llm.prompt import EconomicEvent

        prompt_events = [
            EconomicEvent(
                time_jst="21:30",
                currency="USD",
                name="Non-Farm Payrolls",
                impact="High",
            )
        ]
        calendar_result = CalendarResult(
            events=[
                AdapterEvent(
                    name="Non-Farm Payrolls",
                    datetime_jst=datetime(2026, 6, 29, 21, 30, tzinfo=JST),
                    impact=ImpactLevel.HIGH,
                    currency="USD",
                )
            ],
            source_ok=True,
        )
        mock_client = _build_mock_client(candle=_FRIDAY_CANDLE)
        now_jst = datetime(_MONDAY.year, _MONDAY.month, _MONDAY.day, 20, 30, 0, tzinfo=JST)

        with (
            patch.dict(os.environ, {"ALLOW_LIVE_ORDERS": "", "TRADING_PHASE": "1"}, clear=False),
            patch("backend.src.batch.decision_batch.get_confirmed_daily_candles", return_value=_CANDLES_15),
            patch("backend.src.batch.decision_batch.generate_decision") as mock_llm,
            patch(
                "backend.src.batch.decision_batch.dynamodb_store.acquire_lock_outcome",
                return_value=LockAcquireOutcome(acquired=True),
            ) as mock_lock,
            patch("backend.src.batch.decision_batch.s3_logger.save_decision_log"),
            patch("backend.src.batch.decision_batch.time.sleep"),
            patch(
                "backend.src.batch.decision_batch._fetch_economic_events",
                return_value=(prompt_events, calendar_result),
            ),
            patch("backend.src.batch.decision_batch._fetch_macro_context", return_value=None),
            patch("backend.src.batch.decision_batch.is_us_thin_market", return_value=False),
        ):
            result = morning_run(client=mock_client, today=_MONDAY, now_jst=now_jst)

        assert result["status"] == "no_entry"
        assert result["reason"] == "indicator_day_skip"
        mock_llm.assert_not_called()
        mock_lock.assert_not_called()


# ---------------------------------------------------------------------------
# 必須ケース: エッジガード day-skip（RD-8 #194）
# ---------------------------------------------------------------------------


class TestEdgeGuardDaySkipRequiredCase:
    """§7.1 / §3.3: G1 ワイドスプレッド日は LLM 呼び出し前に NO_ENTRY（RD-8 #194）。"""

    def test_wide_spread_skips_before_llm(self) -> None:
        mock_client = _build_mock_client(
            ticker=[{"bid": "156.750", "ask": "156.800"}],
            candle=_FRIDAY_CANDLE,
        )
        now_jst = datetime(_MONDAY.year, _MONDAY.month, _MONDAY.day, 20, 30, 0, tzinfo=JST)

        with (
            patch.dict(os.environ, {"ALLOW_LIVE_ORDERS": "", "TRADING_PHASE": "1"}, clear=False),
            patch("backend.src.batch.decision_batch.get_confirmed_daily_candles", return_value=_CANDLES_15),
            patch("backend.src.batch.decision_batch.generate_decision") as mock_llm,
            patch(
                "backend.src.batch.decision_batch.dynamodb_store.acquire_lock_outcome",
                return_value=LockAcquireOutcome(acquired=True),
            ) as mock_lock,
            patch("backend.src.batch.decision_batch.s3_logger.save_decision_log"),
            patch("backend.src.batch.decision_batch.time.sleep"),
            patch(
                "backend.src.batch.decision_batch._fetch_economic_events",
                return_value=([], CalendarResult(events=[], source_ok=True)),
            ),
            patch("backend.src.batch.decision_batch._fetch_macro_context", return_value=None),
            patch("backend.src.batch.decision_batch.is_us_thin_market", return_value=False),
        ):
            result = morning_run(client=mock_client, today=_MONDAY, now_jst=now_jst)

        assert result["status"] == "no_entry"
        assert result["reason"] == "wide_spread_day_skip"
        mock_llm.assert_not_called()
        mock_lock.assert_not_called()


# ---------------------------------------------------------------------------
# 必須ケース 3: 署名パス /v1/ifoOrder 一致
# ---------------------------------------------------------------------------


class TestSignaturePath:
    """署名パスが /v1/ifoOrder であり /private を含まないこと（§7.1, §7.2）。"""

    def test_ifo_order_sign_path_equals_v1_ifo_order(self) -> None:
        """PRIVATE_IFO_ORDER の to_sign_path が /v1/ifoOrder であること（§7.1 必須 assert）。"""
        sign_path = to_sign_path(PRIVATE_IFO_ORDER)
        assert sign_path == "/v1/ifoOrder", (
            f"署名パスは /v1/ifoOrder でなければなりません (実際: {sign_path})"
        )

    def test_ifo_order_sign_path_excludes_private(self) -> None:
        """/private が署名パスに含まれないこと（401 の主因を防ぐ）。"""
        sign_path = to_sign_path(PRIVATE_IFO_ORDER)
        assert "/private" not in sign_path

    def test_ifo_order_request_url_contains_private(self) -> None:
        """リクエスト URL は /private/v1/ifoOrder から始まること（署名パスとの非対称性）。"""
        assert PRIVATE_IFO_ORDER.startswith("/private/v1/"), (
            f"リクエスト URL は /private/v1/ から始まるべきです (実際: {PRIVATE_IFO_ORDER})"
        )

    def test_request_url_and_sign_path_are_asymmetric(self) -> None:
        """リクエスト URL と署名パスが異なること（§7.2 非対称性の明示検証）。"""
        sign_path = to_sign_path(PRIVATE_IFO_ORDER)
        assert sign_path != PRIVATE_IFO_ORDER


# ---------------------------------------------------------------------------
# フェーズ1シミュレーション統合テスト（1日分 CI 用）
# ---------------------------------------------------------------------------


class TestPhase1Simulation1Day:
    """CI 用の1日分シミュレーション — external_api_calls == 0 かつ failed_days == 0。"""

    def test_1day_simulation_no_external_calls(self) -> None:
        """1日分シミュレーションで外部 HTTP が 0 件であること。"""
        import sys
        from pathlib import Path

        # scripts/ を path に追加
        scripts_dir = Path(__file__).resolve().parent.parent.parent / "scripts"
        if str(scripts_dir.parent) not in sys.path:
            sys.path.insert(0, str(scripts_dir.parent))

        from scripts.run_local_simulation import _simulate_day

        os.environ["MOCK_MODE"] = "True"
        os.environ["ALLOW_LIVE_ORDERS"] = "False"
        os.environ["TRADING_PHASE"] = "1"

        sim_day = date(2026, 6, 29)  # 月曜

        external_counter: list[int] = [0]
        result = _simulate_day(sim_day, external_counter)

        assert external_counter[0] == 0, "外部 API 呼び出し数は 0 であること"
        # 非休場の営業日は発注パイプラインを最後まで実行し stubbed になること。
        # skipped（冪等性/口座取得の早期 return）はモックドリフトの兆候なので許容しない（§7.1）。
        assert result["decision"].get("status") == "stubbed", (
            f"判断バッチが発注スタブまで到達していない（モックドリフトの疑い）: "
            f"{result['decision'].get('status')} / reason={result['decision'].get('reason')}"
        )

    def test_1day_simulation_failed_days_zero(self) -> None:
        """1日分シミュレーションで failed_days == 0 であること。"""
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).resolve().parent.parent.parent / "scripts"
        if str(scripts_dir.parent) not in sys.path:
            sys.path.insert(0, str(scripts_dir.parent))

        from scripts.run_local_simulation import _simulate_day

        os.environ["MOCK_MODE"] = "True"
        os.environ["ALLOW_LIVE_ORDERS"] = "False"
        os.environ["TRADING_PHASE"] = "1"

        sim_day = date(2026, 6, 30)  # 火曜

        external_counter: list[int] = [0]
        result = _simulate_day(sim_day, external_counter)

        decision_status = result["decision"].get("status")
        assert decision_status == "stubbed", (
            f"判断バッチが発注スタブまで到達していない（モックドリフトの疑い）: "
            f"{decision_status} / reason={result['decision'].get('reason')}"
        )
        # 火曜は夜バッチが走り、note_generated == True であること
        assert result["closeout"] is not None, "火曜は夜バッチが走ること"
        assert result["closeout"].get("status") == "ok", (
            f"夜バッチ status が不正: {result['closeout'].get('status')}"
        )
        assert result["closeout"].get("note_generated") is True, (
            "夜バッチで trade note が生成されること（note_generated==True）"
        )
