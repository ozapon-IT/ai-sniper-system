"""klines.py のユニットテスト（§3.1, E05）。

付録 B 確定仕様に基づく 4 ケース:
  ① 進行中足の除外（get_confirmed_candles）
  ② 土日ギャップ下で月曜起動 → 金曜足が返る（get_latest_confirmed_daily）
  ③ OHLC を Decimal で扱い末尾ゼロ省略 ("157") を誤らない
  ④ fetch_klines_for_year が date=YYYY を組む（YYYYMMDD ではない）
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from backend.src.adapters.klines import (
    PRICE_TYPE_FOR_INDICATORS,
    SymbolInfo,
    fetch_klines_for_year,
    fetch_symbol_info,
    get_confirmed_candles,
    get_confirmed_daily_candles,
    get_latest_confirmed_daily,
    get_recent_4h_context,
)

# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------


def _candle(open_time_ms: int, close: str = "157.123") -> dict[str, str]:
    return {
        "openTime": str(open_time_ms),
        "open": "157.000",
        "high": "157.500",
        "low": "156.800",
        "close": close,
    }


def _dt(*args: int) -> datetime:
    """UTC datetime を簡易生成する。引数は (year, month, day, hour=0, min=0)。"""
    return datetime(*args, tzinfo=UTC)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1_000)


# ---------------------------------------------------------------------------
# ① 進行中足の除外
# ---------------------------------------------------------------------------


class TestGetConfirmedCandles:
    def test_in_progress_last_candle_excluded(self) -> None:
        """配列末尾の進行中足（openTime + interval > now）は除外される。"""
        # 2026-06-27 06:00 JST = 2026-06-26 21:00 UTC が日足の openTime
        fri_open_ms = _ms(_dt(2026, 6, 26, 21, 0))  # JST 06/27 06:00
        # 2026-06-29 06:00 JST = 2026-06-28 21:00 UTC が月曜日足（進行中）
        mon_open_ms = _ms(_dt(2026, 6, 28, 21, 0))  # JST 06/29 06:00

        candles = [_candle(fri_open_ms), _candle(mon_open_ms)]
        # now = 2026-06-29 07:30 JST = 2026-06-28 22:30 UTC
        now_ms = _ms(_dt(2026, 6, 28, 22, 30))

        # fri: fri_open + 24h = 06/27 21:00 UTC = 06/27 06:00 UTC + 24h
        # 実際: fri_open_ms + 24h = 2026-06-27 21:00 UTC。now = 06-28 22:30 UTC → 確定 ✓
        # mon: mon_open_ms + 24h = 2026-06-29 21:00 UTC > now → 進行中 ✗
        confirmed = get_confirmed_candles(candles, now_ms, "1day")

        assert len(confirmed) == 1
        assert confirmed[0]["openTime"] == str(fri_open_ms)

    def test_all_confirmed(self) -> None:
        """十分前の足は全件確定と判定される。"""
        old_ms = _ms(_dt(2026, 1, 2, 21, 0))
        older_ms = _ms(_dt(2026, 1, 3, 21, 0))
        now_ms = _ms(_dt(2026, 6, 29, 22, 0))  # 大幅に将来

        candles = [_candle(old_ms), _candle(older_ms)]
        confirmed = get_confirmed_candles(candles, now_ms, "1day")
        assert len(confirmed) == 2

    def test_unsupported_interval_raises(self) -> None:
        """サポート外の interval は ValueError。"""
        with pytest.raises(ValueError, match="unsupported interval"):
            get_confirmed_candles([], 0, "30min")

    def test_5min_in_progress_excluded(self) -> None:
        """5 分足でも進行中足が除外される（backtest BT-1 が共有する確定判定）。"""
        t0 = _ms(_dt(2026, 6, 24, 9, 0))
        t1 = _ms(_dt(2026, 6, 24, 9, 5))
        now_ms = _ms(_dt(2026, 6, 24, 9, 7))  # t1 足は 09:10 まで進行中
        confirmed = get_confirmed_candles([_candle(t0), _candle(t1)], now_ms, "5min")
        assert [c["openTime"] for c in confirmed] == [str(t0)]

    def test_4hour_in_progress_excluded(self) -> None:
        """4 時間足でも進行中足が除外される。"""
        # 07:30 UTC = 4h 足のまだ進行中（UTC 08:00 まで継続）
        current_4h_open = _ms(_dt(2026, 6, 29, 4, 0))  # 08:00 JST 始まり
        prev_4h_open = _ms(_dt(2026, 6, 29, 0, 0))  # 04:00 JST 始まり（確定済み）
        now_ms = _ms(_dt(2026, 6, 29, 7, 30))  # 07:30 UTC

        candles = [_candle(prev_4h_open), _candle(current_4h_open)]
        confirmed = get_confirmed_candles(candles, now_ms, "4hour")
        assert len(confirmed) == 1
        assert confirmed[0]["openTime"] == str(prev_4h_open)


# ---------------------------------------------------------------------------
# ② 土日ギャップ下で月曜起動 → 金曜足が返る
# ---------------------------------------------------------------------------


class TestGetLatestConfirmedDaily:
    def _make_client(self, candles_2026: list[dict[str, str]]) -> MagicMock:
        """当年の klines を返すモッククライアント。"""
        client = MagicMock()
        ok_resp = MagicMock()
        ok_resp.json.return_value = {"status": 0, "data": candles_2026}
        client.public_get.return_value = ok_resp
        return client

    def test_monday_returns_friday_candle(self) -> None:
        """月曜 07:30 UTC 起動時に直近確定足 = 金曜足が返る（§3.1）。"""
        # 金曜日足 openTime: 2026-06-26 21:00 UTC（JST 06/27 06:00）
        fri_open_ms = _ms(_dt(2026, 6, 26, 21, 0))
        # 月曜日足 openTime: 2026-06-28 21:00 UTC（JST 06/29 06:00）← 進行中
        mon_open_ms = _ms(_dt(2026, 6, 28, 21, 0))

        client = self._make_client([_candle(fri_open_ms, "157"), _candle(mon_open_ms, "158")])
        # now = 月曜 07:30 JST = 2026-06-28 22:30 UTC（バッチ起動タイミング）
        now_utc = _dt(2026, 6, 28, 22, 30)

        result = get_latest_confirmed_daily(client, now_utc)

        assert result is not None
        assert result["openTime"] == str(fri_open_ms)
        assert result["close"] == "157"  # 末尾ゼロ省略文字列のまま返る

    def test_no_confirmed_returns_none(self) -> None:
        """確定済み足が1本もない場合は None。"""
        # 進行中の足のみ（openTime + 24h > now）
        in_progress_ms = _ms(_dt(2026, 6, 28, 21, 0))
        client = self._make_client([_candle(in_progress_ms)])
        now_utc = _dt(2026, 6, 28, 22, 0)  # in_progress + 24h = 06-29 22:00 > now

        result = get_latest_confirmed_daily(client, now_utc)
        assert result is None

    def test_api_error_returns_none(self) -> None:
        """API がエラーを返す場合（status != 0）は None。"""
        client = MagicMock()
        err_resp = MagicMock()
        err_resp.json.return_value = {"status_code": 404, "message": "Not found"}
        client.public_get.return_value = err_resp

        result = get_latest_confirmed_daily(client, _dt(2026, 6, 29, 0, 0))
        assert result is None


# ---------------------------------------------------------------------------
# ③ OHLC を Decimal で扱い末尾ゼロ省略 ("157") を誤らない
# ---------------------------------------------------------------------------


class TestOhlcDecimal:
    def test_trailing_zero_omitted_string_parses_correctly(self) -> None:
        """API が "157" を返しても Decimal("157") で正しく扱える。"""
        candle = _candle(_ms(_dt(2026, 6, 26, 21, 0)), close="157")
        close = Decimal(candle["close"])
        assert close == Decimal("157.000")
        assert close == Decimal("157")
        # tick 丸めに使用する場合（§3.4）
        tick = Decimal("0.001")
        assert close.quantize(tick) == Decimal("157.000")

    def test_full_precision_string_parses_correctly(self) -> None:
        """通常精度 "157.123" も正確に扱える。"""
        candle = _candle(_ms(_dt(2026, 6, 26, 21, 0)), close="157.123")
        close = Decimal(candle["close"])
        assert close == Decimal("157.123")

    def test_ohlc_fields_all_parseable(self) -> None:
        """open/high/low/close すべてが Decimal で扱える。"""
        open_ms = _ms(_dt(2026, 6, 26, 21, 0))
        candle: dict[str, str] = {
            "openTime": str(open_ms),
            "open": "157",
            "high": "157.5",
            "low": "156.8",
            "close": "157.1",
        }
        for field in ("open", "high", "low", "close"):
            assert isinstance(Decimal(candle[field]), Decimal)


# ---------------------------------------------------------------------------
# ④ fetch_klines_for_year が date=YYYY を組む（YYYYMMDD ではない）
# ---------------------------------------------------------------------------


class TestFetchKlinesForYear:
    def test_sends_year_only_date_param(self) -> None:
        """API リクエストの date パラメータが YYYY 形式（YYYYMMDD は 404 になるため）。"""
        client = MagicMock()
        ok_resp = MagicMock()
        ok_resp.json.return_value = {"status": 0, "data": []}
        client.public_get.return_value = ok_resp

        fetch_klines_for_year(client, "USD_JPY", "1day", 2026)

        client.public_get.assert_called_once()
        _, kwargs = client.public_get.call_args
        params = kwargs.get("params") or client.public_get.call_args[0][1]
        assert params["date"] == "2026"
        assert params["date"] != "20260101"

    def test_sends_bid_price_type(self) -> None:
        """priceType は PRICE_TYPE_FOR_INDICATORS = 'BID' で送る。"""
        client = MagicMock()
        ok_resp = MagicMock()
        ok_resp.json.return_value = {"status": 0, "data": []}
        client.public_get.return_value = ok_resp

        fetch_klines_for_year(client, "USD_JPY", "1day", 2026)

        _, kwargs = client.public_get.call_args
        params = kwargs.get("params") or client.public_get.call_args[0][1]
        assert params["priceType"] == PRICE_TYPE_FOR_INDICATORS
        assert params["priceType"] == "BID"

    def test_returns_sorted_by_open_time(self) -> None:
        """返却リストは openTime 昇順になっている。"""
        client = MagicMock()
        ok_resp = MagicMock()
        # 意図的に降順で渡してソート確認
        ok_resp.json.return_value = {
            "status": 0,
            "data": [
                {"openTime": "2000", "open": "1", "high": "1", "low": "1", "close": "1"},
                {"openTime": "1000", "open": "1", "high": "1", "low": "1", "close": "1"},
            ],
        }
        client.public_get.return_value = ok_resp

        result = fetch_klines_for_year(client, "USD_JPY", "1day", 2026)

        assert result[0]["openTime"] == "1000"
        assert result[1]["openTime"] == "2000"

    def test_api_error_returns_none(self) -> None:
        """API エラー（status_code:404）時は None を返す（[] との区別が呼び出し元で重要）。"""
        client = MagicMock()
        err_resp = MagicMock()
        err_resp.json.return_value = {"status_code": 404, "message": "Not found"}
        client.public_get.return_value = err_resp

        result = fetch_klines_for_year(client, "USD_JPY", "1day", 2026)
        assert result is None


# ---------------------------------------------------------------------------
# 4時間足直近 6 本
# ---------------------------------------------------------------------------


class TestGetRecent4hContext:
    def _make_many_4h_candles(self, count: int, base_ms: int) -> list[dict[str, str]]:
        interval = 4 * 3_600 * 1_000
        return [_candle(base_ms + i * interval) for i in range(count)]

    def test_returns_last_6_confirmed(self) -> None:
        """確定済み足から末尾 6 本だけ返す。"""
        # 10 本の確定済み + 1 本の進行中
        base_ms = _ms(_dt(2026, 6, 20, 0, 0))
        interval_ms = 4 * 3_600 * 1_000
        candles = self._make_many_4h_candles(11, base_ms)
        in_progress_ms = base_ms + 10 * interval_ms
        # now = 進行中足の開始から 1 時間後
        now_ms = in_progress_ms + 3_600 * 1_000

        client = MagicMock()
        ok_resp = MagicMock()
        ok_resp.json.return_value = {"status": 0, "data": candles}
        client.public_get.return_value = ok_resp

        result = get_recent_4h_context(client, datetime.fromtimestamp(now_ms / 1_000, tz=UTC))

        assert len(result) == 6
        # 末尾 6 本（10 本のうち最後の 6 本）
        confirmed_open_times = [base_ms + i * interval_ms for i in range(10)]
        expected = [str(t) for t in confirmed_open_times[-6:]]
        assert [c["openTime"] for c in result] == expected


# ---------------------------------------------------------------------------
# SymbolInfo
# ---------------------------------------------------------------------------


class TestSymbolInfo:
    def test_frozen_dataclass(self) -> None:
        info = SymbolInfo(
            tick_size=Decimal("0.001"),
            min_lot=100,
            lot_step=1,
            max_lot=500000,
        )
        assert info.tick_size == Decimal("0.001")
        assert info.min_lot == 100
        with pytest.raises(AttributeError):
            info.tick_size = Decimal("1")  # type: ignore[misc]


# ---------------------------------------------------------------------------
# fetch_symbol_info
# ---------------------------------------------------------------------------


class TestFetchSymbolInfo:
    def _make_client(self, data: list[dict]) -> MagicMock:
        client = MagicMock()
        resp = MagicMock()
        resp.json.return_value = {"status": 0, "data": data}
        client.public_get.return_value = resp
        return client

    def test_happy_path_usd_jpy(self) -> None:
        """USD/JPY の実確認値（付録 B）が SymbolInfo に正しく変換される。"""
        client = self._make_client(
            [
                {
                    "symbol": "USD_JPY",
                    "tickSize": "0.001",
                    "minOpenOrderSize": "100",
                    "sizeStep": "1",
                    "maxOrderSize": "500000",
                }
            ]
        )
        info = fetch_symbol_info(client, "USD_JPY")

        assert info.tick_size == Decimal("0.001")
        assert info.min_lot == 100
        assert info.lot_step == 1
        assert info.max_lot == 500000

    def test_tick_size_is_decimal_not_float(self) -> None:
        """文字列→ Decimal 変換で精度が保たれる（float 変換では精度損失リスクあり）。"""
        client = self._make_client(
            [
                {
                    "symbol": "USD_JPY",
                    "tickSize": "0.001",
                    "minOpenOrderSize": "100",
                    "sizeStep": "1",
                    "maxOrderSize": "500000",
                }
            ]
        )
        info = fetch_symbol_info(client, "USD_JPY")
        assert isinstance(info.tick_size, Decimal)
        assert info.tick_size == Decimal("1") / Decimal("1000")

    def test_api_error_raises(self) -> None:
        """API がエラーを返した場合は RuntimeError。"""
        client = MagicMock()
        resp = MagicMock()
        resp.json.return_value = {"status_code": 404, "message": "Not found"}
        client.public_get.return_value = resp

        with pytest.raises(RuntimeError, match="symbols fetch failed"):
            fetch_symbol_info(client, "USD_JPY")

    def test_symbol_not_found_raises(self) -> None:
        """レスポンスに対象 symbol がない場合は RuntimeError。"""
        client = self._make_client(
            [
                {
                    "symbol": "EUR_JPY",
                    "tickSize": "0.001",
                    "minOpenOrderSize": "100",
                    "sizeStep": "1",
                    "maxOrderSize": "500000",
                }
            ]
        )
        with pytest.raises(RuntimeError, match="not found in symbols response"):
            fetch_symbol_info(client, "USD_JPY")


# ---------------------------------------------------------------------------
# 年跨ぎ補完（get_confirmed_daily_candles / get_recent_4h_context）
# ---------------------------------------------------------------------------


class TestYearBoundaryFallback:
    """年初に当年の確定足が 15 本未満の場合に前年分を連結して補完する。"""

    def _make_two_year_client(
        self,
        current_year_candles: list[dict],
        prev_year_candles: list[dict],
    ) -> MagicMock:
        """1回目呼び出しで当年、2回目呼び出しで前年データを返すモック。"""
        client = MagicMock()
        ok_curr = MagicMock()
        ok_curr.json.return_value = {"status": 0, "data": current_year_candles}
        ok_prev = MagicMock()
        ok_prev.json.return_value = {"status": 0, "data": prev_year_candles}
        client.public_get.side_effect = [ok_curr, ok_prev]
        return client

    def test_year_start_concatenates_prev_year(self) -> None:
        """当年の確定足が 14 本しかなければ前年分を連結して 15 本以上になる。"""
        now_ms = _ms(_dt(2026, 1, 20, 22, 30))  # 年初20日目

        interval_ms = 24 * 3_600 * 1_000
        # 当年: 14 本の確定済み日足
        base_2026 = _ms(_dt(2025, 12, 31, 21, 0))  # JST 2026-01-01 06:00
        curr_candles = [_candle(base_2026 + i * interval_ms) for i in range(14)]
        # 前年: 250 本以上の確定済み日足（簡易的に base を 2025-01-01 相当）
        base_2025 = _ms(_dt(2024, 12, 31, 21, 0))
        prev_candles = [_candle(base_2025 + i * interval_ms) for i in range(260)]

        client = self._make_two_year_client(curr_candles, prev_candles)
        result = get_confirmed_daily_candles(client, datetime.fromtimestamp(now_ms / 1_000, tz=UTC))

        assert len(result) >= 15, f"前年連結で 15 本以上になること (got {len(result)})"
        assert client.public_get.call_count == 2, "前年取得の2回目 API 呼び出しが行われること"

    def test_sufficient_current_year_no_fallback(self) -> None:
        """当年の確定足が 15 本以上あれば前年取得は行わない。"""
        now_ms = _ms(_dt(2026, 6, 29, 22, 30))
        interval_ms = 24 * 3_600 * 1_000
        base = _ms(_dt(2025, 12, 31, 21, 0))
        curr_candles = [_candle(base + i * interval_ms) for i in range(130)]

        client = MagicMock()
        ok_resp = MagicMock()
        ok_resp.json.return_value = {"status": 0, "data": curr_candles}
        client.public_get.return_value = ok_resp

        result = get_confirmed_daily_candles(client, datetime.fromtimestamp(now_ms / 1_000, tz=UTC))

        assert len(result) >= 15
        assert client.public_get.call_count == 1, "当年分だけで足りる場合は1回のみ"

    def test_4h_year_boundary_concatenates(self) -> None:
        """4 時間足でも年跨ぎ補完が機能する。"""
        now_ms = _ms(_dt(2026, 1, 1, 6, 0))  # 元日早朝（当年 4h 足ゼロ）
        interval_4h = 4 * 3_600 * 1_000
        base_prev = _ms(_dt(2025, 12, 30, 0, 0))
        prev_4h = [_candle(base_prev + i * interval_4h) for i in range(10)]

        ok_curr = MagicMock()
        ok_curr.json.return_value = {"status": 0, "data": []}
        ok_prev = MagicMock()
        ok_prev.json.return_value = {"status": 0, "data": prev_4h}

        client = MagicMock()
        client.public_get.side_effect = [ok_curr, ok_prev]

        result = get_recent_4h_context(client, datetime.fromtimestamp(now_ms / 1_000, tz=UTC))

        # 前年 10 本確定済みのうち末尾 6 本が返る
        assert len(result) == 6

    def test_current_year_fetch_error_does_not_use_prev_year(self) -> None:
        """当年取得が API エラー（None）の場合、前年の stale データを使わない（fail-closed）。"""
        now = _dt(2026, 6, 29, 0, 0)
        interval_ms = 24 * 3_600 * 1_000
        base_prev = _ms(_dt(2025, 1, 1, 0, 0))
        prev_candles = [_candle(base_prev + i * interval_ms) for i in range(260)]

        err_resp = MagicMock()
        err_resp.json.return_value = {"status_code": 404, "message": "Not found"}
        ok_prev = MagicMock()
        ok_prev.json.return_value = {"status": 0, "data": prev_candles}

        client = MagicMock()
        # 当年取得 → エラー。前年取得 → 成功（ただし呼ばれてはいけない）
        client.public_get.side_effect = [err_resp, ok_prev]

        # get_confirmed_daily_candles は fail-closed で空を返す
        result = get_confirmed_daily_candles(client, now)
        assert result == [], "当年取得失敗時は空リストを返し、前年足を最新足として採用しない"
        # 前年取得 API が呼ばれていないこと（call_count == 1）
        assert client.public_get.call_count == 1, "エラー時は前年 API を呼ばない"

    def test_4h_current_year_fetch_error_does_not_use_prev_year(self) -> None:
        """4h 当年取得エラー時も fail-closed で空リストを返し、前年 API を呼ばない。"""
        now = _dt(2026, 6, 29, 0, 0)
        interval_ms = 4 * 3_600 * 1_000
        base_prev = _ms(_dt(2025, 1, 1, 0, 0))
        prev_candles = [_candle(base_prev + i * interval_ms) for i in range(50)]

        err_resp = MagicMock()
        err_resp.json.return_value = {"status_code": 404, "message": "Not found"}
        ok_prev = MagicMock()
        ok_prev.json.return_value = {"status": 0, "data": prev_candles}

        client = MagicMock()
        client.public_get.side_effect = [err_resp, ok_prev]

        result = get_recent_4h_context(client, now)
        assert result == [], (
            "4h 当年取得失敗時は空リストを返し、stale な前年 4h 足を LLM 入力に使わない"
        )
        assert client.public_get.call_count == 1, "エラー時は前年 API を呼ばない"


# ---------------------------------------------------------------------------
# get_session_ohlc（RD-5 #170: 当日セッション始値/高値/安値）
# ---------------------------------------------------------------------------


def _c5(open_time_ms: int, o: str, h: str, low: str) -> dict[str, str]:
    """5min ローソク足（open/high/low を明示）。close は本テストでは未使用。"""
    return {"openTime": str(open_time_ms), "open": o, "high": h, "low": low, "close": o}


class TestGetSessionOhlc:
    def _client(self, candles: list[dict[str, str]], status: int = 0) -> MagicMock:
        client = MagicMock()
        resp = MagicMock()
        body = {"status": status, "data": candles} if status == 0 else {"status_code": 404}
        resp.json.return_value = body
        client.public_get.return_value = resp
        return client

    def test_open_high_low_from_confirmed_candles(self) -> None:
        """始値=最古 open・高値=確定足 high 最大・安値=確定足 low 最小。進行中足は含めない。"""
        from backend.src.adapters import klines

        # 判断時刻 now = 2026-07-03 03:00 UTC（= 12:00 JST）。セッション始点 06:00 JST = 前日 21:00 UTC。
        now = _dt(2026, 7, 3, 3, 0)
        base = _ms(_dt(2026, 7, 2, 21, 0))
        step = 5 * 60 * 1_000
        candles = [
            _c5(base + 0 * step, o="155.0", h="155.3", low="154.9"),
            _c5(base + 1 * step, o="155.1", h="155.6", low="155.0"),  # session high
            _c5(base + 2 * step, o="155.2", h="155.4", low="154.7"),  # session low
            # 進行中足（openTime + 5min > now）。極端な高安を持つが除外されるべき。
            _c5(_ms(_dt(2026, 7, 3, 2, 58)), o="155.0", h="999.0", low="0.0"),
        ]
        result = klines.get_session_ohlc(self._client(candles), now)
        assert result == (155.0, 155.6, 154.7)

    def test_uses_jst_yyyymmdd_and_bid(self) -> None:
        """date は JST 当日 YYYYMMDD・priceType は BID・interval は 5min。"""
        from backend.src.adapters import klines

        now = _dt(2026, 7, 3, 3, 0)  # 12:00 JST → 20260703
        base = _ms(_dt(2026, 7, 2, 21, 0))
        client = self._client([_c5(base, o="155.0", h="155.1", low="154.9")])
        klines.get_session_ohlc(client, now)
        _, kwargs = client.public_get.call_args
        params = kwargs["params"]
        assert params["date"] == "20260703"
        assert params["interval"] == "5min"
        assert params["priceType"] == PRICE_TYPE_FOR_INDICATORS == "BID"

    def test_api_error_returns_none(self) -> None:
        """status != 0（取得失敗）は None（degrade）。"""
        from backend.src.adapters import klines

        now = _dt(2026, 7, 3, 3, 0)
        result = klines.get_session_ohlc(self._client([], status=404), now)
        assert result is None

    def test_no_confirmed_candle_returns_none(self) -> None:
        """当日確定足が無い（早朝起動等）ときは None（degrade）。"""
        from backend.src.adapters import klines

        now = _dt(2026, 7, 3, 3, 0)
        # 進行中足のみ（openTime + 5min > now）
        only_in_progress = [_c5(_ms(_dt(2026, 7, 3, 2, 58)), o="155.0", h="155.1", low="154.9")]
        result = klines.get_session_ohlc(self._client(only_in_progress), now)
        assert result is None


# ---------------------------------------------------------------------------
# get_split_session_ohlc / split_session_ohlc_from_confirmed（RD-9 #195）
# ---------------------------------------------------------------------------


class TestSplitSessionOhlc:
    def test_boundary_1455_tokyo_only_1500_europe(self) -> None:
        """14:55 確定足は東京のみ、15:00 以降は欧州（§3.3 RD-9）。"""
        from backend.src.adapters import klines
        from backend.src.adapters.klines import (
            split_session_ohlc_from_confirmed,
            tokyo_europe_boundary_ms,
        )

        day = datetime(2026, 7, 3, 20, 30, tzinfo=klines._JST).date()
        boundary = tokyo_europe_boundary_ms(day)
        base_tokyo = boundary - 5 * 60 * 1_000  # 14:55 JST
        base_europe = boundary  # 15:00 JST
        confirmed = [
            _c5(base_tokyo - 5 * 60 * 1_000, o="154.0", h="154.5", low="153.8"),  # 14:50
            _c5(base_tokyo, o="154.1", h="155.0", low="154.0"),  # 14:55 → Tokyo high
            _c5(base_europe, o="155.1", h="156.0", low="154.5"),  # 15:00 → Europe
            _c5(base_europe + 5 * 60 * 1_000, o="155.5", h="155.8", low="154.2"),
        ]
        split = split_session_ohlc_from_confirmed(confirmed, boundary)
        assert split is not None
        assert split.session_open == 154.0
        assert split.session_high == 156.0
        assert split.session_low == 153.8
        assert split.tokyo_open == 154.0
        assert split.tokyo_high == 155.0
        assert split.tokyo_low == 153.8
        assert split.europe_open == 155.1
        assert split.europe_high == 156.0
        assert split.europe_low == 154.2

    def test_get_split_session_ohlc_delegates_same_session_block(self) -> None:
        """get_session_ohlc は get_split_session_ohlc の session_* と一致（後方互換）。"""
        from backend.src.adapters import klines

        now = _dt(2026, 7, 3, 11, 30)  # 20:30 JST
        # 2026-07-03 06:00 JST = 2026-07-02 21:00 UTC
        base = _ms(_dt(2026, 7, 2, 21, 0))
        step = 5 * 60 * 1_000
        candles = [
            _c5(base + 0 * step, o="155.0", h="155.3", low="154.9"),
            _c5(base + 1 * step, o="155.1", h="155.6", low="155.0"),
        ]
        client = MagicMock()
        resp = MagicMock()
        resp.json.return_value = {"status": 0, "data": candles}
        client.public_get.return_value = resp
        legacy = klines.get_session_ohlc(client, now)
        split = klines.get_split_session_ohlc(client, now)
        assert legacy == (split.session_open, split.session_high, split.session_low)

    def test_get_split_session_ohlc_sorts_descending_api_response(self) -> None:
        """#210 L-1: API が降順で返しても session_open は最古足の open になる。"""
        from backend.src.adapters import klines

        now = _dt(2026, 7, 3, 11, 30)  # 20:30 JST
        base = _ms(_dt(2026, 7, 2, 21, 0))
        step = 5 * 60 * 1_000
        # 意図的に降順（新しい足が先頭）
        candles = [
            _c5(base + 1 * step, o="155.9", h="156.0", low="155.5"),
            _c5(base + 0 * step, o="154.0", h="154.5", low="153.8"),
        ]
        client = MagicMock()
        resp = MagicMock()
        resp.json.return_value = {"status": 0, "data": candles}
        client.public_get.return_value = resp
        split = klines.get_split_session_ohlc(client, now)
        assert split is not None
        assert split.session_open == 154.0

    def test_europe_only_when_all_confirmed_after_boundary(self) -> None:
        """確定足がすべて 15:00 以降のとき東京は None・欧州のみ（graceful degradation）。"""
        from backend.src.adapters import klines
        from backend.src.adapters.klines import (
            split_session_ohlc_from_confirmed,
            tokyo_europe_boundary_ms,
        )

        day = datetime(2026, 7, 3, 20, 30, tzinfo=klines._JST).date()
        boundary = tokyo_europe_boundary_ms(day)
        confirmed = [
            _c5(boundary, o="155.1", h="156.0", low="154.5"),
            _c5(boundary + 5 * 60 * 1_000, o="155.5", h="155.8", low="154.2"),
        ]
        split = split_session_ohlc_from_confirmed(confirmed, boundary)
        assert split is not None
        assert split.session_open == 155.1
        assert split.session_high == 156.0
        assert split.session_low == 154.2
        assert split.tokyo_open is None
        assert split.tokyo_high is None
        assert split.tokyo_low is None
        assert split.europe_open == 155.1
        assert split.europe_high == 156.0
        assert split.europe_low == 154.2
