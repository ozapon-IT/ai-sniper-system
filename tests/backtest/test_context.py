"""backtest/context.py のユニットテスト（BT-1）。

受け入れ条件:
  - 文脈に含まれる全ローソク足が「openTime + interval <= 判断時刻」を満たす（カットオフ assert）
  - Pivot / ATR が本番関数の出力と一致（ゴールデンテスト）
  - キャッシュのみで動作（オフライン・ネットワークアクセスゼロ）
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from backend.src.adapters.klines import get_confirmed_candles
from backend.src.engine.atr import ATR_PERIOD, calculate_atr
from backend.src.engine.pivot import calculate_pivot
from backtest.context import build_day_context
from backtest.data import KlinesStore

_JST = ZoneInfo("Asia/Tokyo")

_DAY = date(2026, 6, 24)  # 水曜・非休場日
# フィクスチャは朝型（08:00 判断）で組んである。既定（夜型 20:30）とは別に明示する
_MORNING_DECISION = time(8, 0)
_CUTOFF_MS = int(datetime.combine(_DAY, time(8, 0), tzinfo=_JST).timestamp() * 1_000)

_MS_DAY = 24 * 3_600 * 1_000
_MS_4H = 4 * 3_600 * 1_000
_MS_5MIN = 5 * 60 * 1_000


def _jst_ms(d: date, hour: int, minute: int = 0) -> int:
    return int(datetime.combine(d, time(hour, minute), tzinfo=_JST).timestamp() * 1_000)


def _candle(open_time_ms: int, o: float, h: float, lo: float, c: float) -> dict[str, str]:
    return {
        "openTime": str(open_time_ms),
        "open": f"{o:.3f}",
        "high": f"{h:.3f}",
        "low": f"{lo:.3f}",
        "close": f"{c:.3f}",
    }


def _daily_candles(end_day: date, count: int, include_in_progress: bool) -> list[dict[str, str]]:
    """end_day までの平日 count 本の日足（openTime = JST 06:00）。

    include_in_progress=True なら翌営業日（判断当日）の進行中足も末尾に付ける。
    """
    days: list[date] = []
    d = end_day
    while len(days) < count:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    candles = [
        _candle(_jst_ms(day, 6), 155.0 + i * 0.1, 156.0 + i * 0.1, 154.0 + i * 0.1, 155.5 + i * 0.1)
        for i, day in enumerate(days)
    ]
    if include_in_progress:
        nxt = end_day + timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
        candles.append(_candle(_jst_ms(nxt, 6), 999.0, 999.9, 998.0, 999.5))
    return candles


def _4h_candles(day: date, count: int = 9) -> list[dict[str, str]]:
    """判断当日 05:00 JST 始まり（進行中）から 4h ずつ遡って count 本。"""
    start = _jst_ms(day, 5)
    times = [start - i * _MS_4H for i in range(count)]
    times.reverse()
    return [_candle(t, 156.0, 156.5, 155.5, 156.2 + i * 0.01) for i, t in enumerate(times)]


def _5min_candles(day: date, last_close_bid: float = 160.0) -> list[dict[str, str]]:
    """06:00〜08:30 JST の 5 分足。07:55 足（最後の確定足）の close を指定値にする。"""
    candles = []
    t = _jst_ms(day, 6)
    end = _jst_ms(day, 8, 30)
    while t <= end:
        close = last_close_bid if t == _jst_ms(day, 7, 55) else 159.0
        candles.append(_candle(t, 159.0, 159.5, 158.5, close))
        t += _MS_5MIN
    return candles


def _populate_standard_cache(store: KlinesStore, day: date = _DAY) -> list[dict[str, str]]:
    """当日の判断に十分なキャッシュ一式を作り、日足リスト（進行中足込み）を返す。"""
    prev_bd = day - timedelta(days=1)
    while prev_bd.weekday() >= 5:
        prev_bd -= timedelta(days=1)
    daily = _daily_candles(prev_bd, count=20, include_in_progress=True)
    store.save("BID", "1day", str(day.year), daily)
    store.save("BID", "4hour", str(day.year), _4h_candles(day))
    store.save("BID", "5min", day.strftime("%Y%m%d"), _5min_candles(day, last_close_bid=160.0))
    ask_5m = [{**c, "close": f"{float(c['close']) + 0.010:.3f}"} for c in _5min_candles(day, 160.0)]
    store.save("ASK", "5min", day.strftime("%Y%m%d"), ask_5m)
    return daily


class TestCutoff:
    def test_all_used_candles_confirmed_before_cutoff(self, tmp_path: Path) -> None:
        """受け入れ条件: 文脈に入った全足が openTime + interval <= 判断時刻を満たす。"""
        store = KlinesStore(cache_dir=tmp_path)
        _populate_standard_cache(store)

        result = build_day_context(_DAY, store, decision_time=_MORNING_DECISION)
        assert result is not None
        assert result.cutoff_ms == _CUTOFF_MS
        for open_time_ms, interval_ms in result.used_candles_ms:
            assert open_time_ms + interval_ms <= result.cutoff_ms

    def test_in_progress_daily_bar_excluded(self, tmp_path: Path) -> None:
        """判断当日の進行中日足（06:00 JST 始まり）は文脈に入らない。"""
        store = KlinesStore(cache_dir=tmp_path)
        _populate_standard_cache(store)

        result = build_day_context(_DAY, store, decision_time=_MORNING_DECISION)
        assert result is not None
        in_progress_open = _jst_ms(_DAY, 6)
        used_opens = {t for t, _ in result.used_candles_ms}
        assert in_progress_open not in used_opens
        # 進行中足のダミー価格 (999.x) が Pivot に混入していないことも確認
        assert result.prompt_context.pivot_p < 900

    def test_in_progress_4h_bar_excluded(self, tmp_path: Path) -> None:
        """05:00 JST 始まりの 4h 足（05:00+4h > 08:00）は直近 6 本に入らない。"""
        store = KlinesStore(cache_dir=tmp_path)
        _populate_standard_cache(store)

        result = build_day_context(_DAY, store, decision_time=_MORNING_DECISION)
        assert result is not None
        last_4h_open = max(t for t, ms in result.used_candles_ms if ms == 4 * 3_600 * 1_000)
        assert last_4h_open == _jst_ms(_DAY, 1)  # 01:00 JST 足が最後の確定足


def _5min_with_post_cutoff_spike(day: date) -> list[dict[str, str]]:
    """06:00〜08:30 JST の 5 分足。判断時刻(08:00)前に真の高安、判断後(08:15)に極端な spike。

    セッション高安が「判断時刻までの確定足のみ」から算出されること（未来情報排除）を
    検証するための系列。08:15 の 999/1 は cutoff 後なので session_high/low に混入してはならない。
    """
    candles = []
    t = _jst_ms(day, 6)
    end = _jst_ms(day, 8, 30)
    while t <= end:
        if t == _jst_ms(day, 6):
            o, h, lo, c = 155.0, 155.2, 154.8, 155.0  # セッション始値
        elif t == _jst_ms(day, 7):
            o, h, lo, c = 155.0, 160.0, 150.0, 155.0  # cutoff 前の真の高安
        elif t == _jst_ms(day, 8, 15):
            o, h, lo, c = 155.0, 999.0, 1.0, 155.0  # cutoff 後の spike（除外されるべき）
        else:
            o, h, lo, c = 155.0, 155.1, 154.9, 155.0
        candles.append(_candle(t, o, h, lo, c))
        t += _MS_5MIN
    return candles


class TestSessionStats:
    """当日セッション始値/高値/安値（RD-5 #170）のカットオフ・受入条件3。"""

    def test_session_open_high_low_populated(self, tmp_path: Path) -> None:
        """始値=06:00 足 open・高安は判断時刻までの確定足から算出される。"""
        store = KlinesStore(cache_dir=tmp_path)
        _populate_standard_cache(store)  # 標準 5min は一様な高安（159.5/158.5）

        result = build_day_context(_DAY, store, decision_time=_MORNING_DECISION)
        assert result is not None
        ctx = result.prompt_context
        assert ctx.session_open == 159.0
        assert ctx.session_high == 159.5
        assert ctx.session_low == 158.5

    def test_session_high_low_exclude_post_cutoff_candles(self, tmp_path: Path) -> None:
        """受入条件3: 判断時刻以降（08:15 spike）の値動きは session_high/low に入らない。"""
        store = KlinesStore(cache_dir=tmp_path)
        _populate_standard_cache(store)
        spike = _5min_with_post_cutoff_spike(_DAY)
        store.save("BID", "5min", _DAY.strftime("%Y%m%d"), spike)
        ask = [{**c, "close": f"{float(c['close']) + 0.010:.3f}"} for c in spike]
        store.save("ASK", "5min", _DAY.strftime("%Y%m%d"), ask)

        result = build_day_context(_DAY, store, decision_time=_MORNING_DECISION)
        assert result is not None
        ctx = result.prompt_context
        assert ctx.session_open == 155.0
        assert ctx.session_high == 160.0  # 07:00 足の高値。08:15 の 999 ではない
        assert ctx.session_low == 150.0  # 07:00 足の安値。08:15 の 1.0 ではない


class TestGoldenParityWithProduction:
    def test_pivot_and_atr_match_production_functions(self, tmp_path: Path) -> None:
        """受け入れ条件: Pivot/ATR が本番関数を同一入力に適用した結果と一致する。"""
        store = KlinesStore(cache_dir=tmp_path)
        daily_all = _populate_standard_cache(store)

        result = build_day_context(_DAY, store, decision_time=_MORNING_DECISION)
        assert result is not None

        # 本番と同じ確定フィルタを独立に適用して期待値を作る
        confirmed = get_confirmed_candles(daily_all, _CUTOFF_MS, "1day")
        assert len(confirmed) >= ATR_PERIOD + 1
        expected_atr = calculate_atr(confirmed)
        latest = confirmed[-1]
        expected_pivot = calculate_pivot(
            high=float(latest["high"]),
            low=float(latest["low"]),
            close=float(latest["close"]),
        )

        ctx = result.prompt_context
        assert ctx.atr_14d == expected_atr
        assert (ctx.pivot_p, ctx.pivot_r1, ctx.pivot_s1, ctx.pivot_r2, ctx.pivot_s2) == (
            expected_pivot.p,
            expected_pivot.r1,
            expected_pivot.s1,
            expected_pivot.r2,
            expected_pivot.s2,
        )

        expected_change = (float(latest["close"]) - float(latest["open"])) / float(latest["open"])
        assert ctx.prev_day_change == expected_change

    def test_mid_price_from_last_confirmed_5min_bar(self, tmp_path: Path) -> None:
        """current_price_mid は 07:55 足（最後の確定 5 分足）の BID/ASK close の mid。"""
        store = KlinesStore(cache_dir=tmp_path)
        _populate_standard_cache(store)

        result = build_day_context(_DAY, store, decision_time=_MORNING_DECISION)
        assert result is not None
        assert result.prompt_context.current_price_mid == (160.0 + 160.010) / 2

    def test_negative_spread_normalized_for_edge_guard(self, tmp_path: Path) -> None:
        """ask < bid の異常足でも spread_pips は abs 正規化され G1 判定可能。"""
        store = KlinesStore(cache_dir=tmp_path / "cache")
        _populate_standard_cache(store)
        bid = store.load("BID", "5min", _DAY.strftime("%Y%m%d"))
        assert bid is not None
        # 07:55 足の ASK close を BID より低くして負スプレッドを作る
        ask = [{**c, "close": f"{float(c['close']) - 0.006:.3f}"} for c in bid]
        store.save("ASK", "5min", _DAY.strftime("%Y%m%d"), ask)

        result = build_day_context(_DAY, store, decision_time=_MORNING_DECISION)
        assert result is not None
        assert result.spread_pips == pytest.approx(0.6)


class TestYearBoundary:
    def test_prepends_previous_year_cache_when_insufficient(self, tmp_path: Path) -> None:
        """年初（当年確定足 < 15 本）は前年キャッシュを連結して ATR を成立させる。"""
        day = date(2026, 1, 7)  # 水曜・非休場日
        store = KlinesStore(cache_dir=tmp_path)
        # 2026 年分: 1/2(金), 1/5(月), 1/6(火) の 3 本のみ
        candles_2026 = [
            _candle(_jst_ms(d, 6), 155.0, 156.0, 154.0, 155.5)
            for d in (date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6))
        ]
        store.save("BID", "1day", "2026", candles_2026)
        store.save("BID", "1day", "2025", _daily_candles(date(2025, 12, 31), 18, False))
        store.save("BID", "4hour", "2026", _4h_candles(day))
        store.save("BID", "5min", day.strftime("%Y%m%d"), _5min_candles(day))
        store.save("ASK", "5min", day.strftime("%Y%m%d"), _5min_candles(day))

        result = build_day_context(day, store, decision_time=_MORNING_DECISION)
        assert result is not None
        daily_used = [t for t, ms in result.used_candles_ms if ms == _MS_DAY]
        assert len(daily_used) == ATR_PERIOD + 1  # 前年連結で 15 本成立


class TestSkipConditions:
    def test_holiday_returns_none(self, tmp_path: Path) -> None:
        store = KlinesStore(cache_dir=tmp_path)
        assert build_day_context(date(2026, 1, 1), store) is None  # 元日（full_close）

    def test_missing_daily_cache_returns_none(self, tmp_path: Path) -> None:
        store = KlinesStore(cache_dir=tmp_path)
        assert build_day_context(_DAY, store, decision_time=_MORNING_DECISION) is None

    def test_insufficient_daily_returns_none(self, tmp_path: Path) -> None:
        store = KlinesStore(cache_dir=tmp_path)
        prev_bd = date(2026, 6, 23)
        store.save("BID", "1day", "2026", _daily_candles(prev_bd, 5, False))  # 15 本未満
        store.save("BID", "4hour", "2026", _4h_candles(_DAY))
        store.save("BID", "5min", _DAY.strftime("%Y%m%d"), _5min_candles(_DAY))
        store.save("ASK", "5min", _DAY.strftime("%Y%m%d"), _5min_candles(_DAY))
        assert build_day_context(_DAY, store, decision_time=_MORNING_DECISION) is None

    def test_missing_5min_returns_none(self, tmp_path: Path) -> None:
        store = KlinesStore(cache_dir=tmp_path)
        prev_bd = date(2026, 6, 23)
        store.save("BID", "1day", "2026", _daily_candles(prev_bd, 20, True))
        store.save("BID", "4hour", "2026", _4h_candles(_DAY))
        assert build_day_context(_DAY, store, decision_time=_MORNING_DECISION) is None

    def test_us_thin_market_flag(self, tmp_path: Path) -> None:
        """us_thin 日はスキップせずフラグだけ立つ（本番と同じ扱い）。"""
        day = date(2026, 7, 3)  # 独立記念日振替（us_thin・金曜）
        store = KlinesStore(cache_dir=tmp_path)
        prev_bd = date(2026, 7, 2)
        store.save("BID", "1day", "2026", _daily_candles(prev_bd, 20, False))
        store.save("BID", "4hour", "2026", _4h_candles(day))
        store.save("BID", "5min", day.strftime("%Y%m%d"), _5min_candles(day))
        store.save("ASK", "5min", day.strftime("%Y%m%d"), _5min_candles(day))

        result = build_day_context(day, store, decision_time=_MORNING_DECISION)
        assert result is not None
        assert result.prompt_context.us_thin_market is True
