"""ロンドンORB 判定コアのユニットテスト（境界ケース）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from backend.src.orb.constants import LON, PIP, STOP_BUFFER
from backend.src.orb.evaluate import evaluate_day
from backend.src.orb.models import Bar1m, Bar15, RangeLevels
from backend.src.orb.range import has_session_window, range_from_1m, range_from_15m
from backend.src.orb.resolve import resolve_exit, resolve_fill


def _utc(y: int, m: int, d: int, hh: int, mm: int) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


def _bar15(
    open_utc: datetime,
    *,
    o: float,
    h: float,
    low: float,
    c: float,
) -> Bar15:
    local_close = (open_utc + timedelta(minutes=15)).astimezone(LON)
    return Bar15(
        open_time=open_utc,
        open=o,
        high=h,
        low=low,
        close=c,
        local_close=local_close,
    )


def _bar1m(
    local_open: datetime,
    *,
    o: float,
    h: float,
    low: float,
    c: float,
) -> Bar1m:
    return Bar1m(local_open=local_open, open=o, high=h, low=low, close=c)


def _london(y: int, m: int, d: int, hh: int, mm: int) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=LON)


# --- レンジ: 15分足4本 ---
# London 夏時間(BST=UTC+1) で open UTC を逆算しやすい日を使う
# 2024-06-03 は月曜・BST。London 8:00 = UTC 7:00


class TestRangeFrom15m:
    def test_exactly_four_bars(self) -> None:
        # open 7:00/7:15/7:30/7:45 UTC → local_close 8:15/8:30/8:45/9:00 London
        opens = [
            _utc(2024, 6, 3, 7, 0),
            _utc(2024, 6, 3, 7, 15),
            _utc(2024, 6, 3, 7, 30),
            _utc(2024, 6, 3, 7, 45),
        ]
        bars = [
            _bar15(opens[0], o=150.0, h=150.10, low=149.90, c=150.00),
            _bar15(opens[1], o=150.0, h=150.20, low=149.95, c=150.05),
            _bar15(opens[2], o=150.0, h=150.15, low=149.80, c=150.00),
            _bar15(opens[3], o=150.0, h=150.05, low=149.85, c=150.00),
        ]
        levels = range_from_15m(bars)
        assert levels is not None
        assert levels.high == pytest.approx(150.20)
        assert levels.low == pytest.approx(149.80)
        assert levels.buy_stop == pytest.approx(150.20 + STOP_BUFFER)
        assert levels.sell_stop == pytest.approx(149.80 - STOP_BUFFER)

    def test_fewer_than_four_is_no_range(self) -> None:
        opens = [_utc(2024, 6, 3, 7, 0), _utc(2024, 6, 3, 7, 15), _utc(2024, 6, 3, 7, 30)]
        bars = [_bar15(o, o=150.0, h=150.1, low=149.9, c=150.0) for o in opens]
        assert range_from_15m(bars) is None


class TestRangeFrom1m:
    def test_exactly_sixty_bars(self) -> None:
        bars = [
            _bar1m(
                _london(2024, 6, 3, 8, m),
                o=150.0,
                h=150.0 + (0.01 if m == 30 else 0.0),
                low=150.0 - (0.02 if m == 10 else 0.0),
                c=150.0,
            )
            for m in range(60)
        ]
        levels = range_from_1m(bars)
        assert levels is not None
        assert levels.high == pytest.approx(150.01)
        assert levels.low == pytest.approx(149.98)

    def test_fifty_nine_is_no_range(self) -> None:
        bars = [
            _bar1m(_london(2024, 6, 3, 8, m), o=150.0, h=150.1, low=149.9, c=150.0)
            for m in range(59)
        ]
        assert range_from_1m(bars) is None


class TestResolveFill:
    def _levels(self) -> RangeLevels:
        return RangeLevels(
            high=150.20,
            low=149.80,
            buy_stop=150.20 + STOP_BUFFER,
            sell_stop=149.80 - STOP_BUFFER,
        )

    def test_ambiguous_both_same_bar(self) -> None:
        levels = self._levels()
        mins = [
            _bar1m(
                _london(2024, 6, 3, 9, 5),
                o=150.0,
                h=levels.buy_stop + 0.01,
                low=levels.sell_stop - 0.01,
                c=150.0,
            )
        ]
        fill, skip = resolve_fill(mins, levels)
        assert fill is None
        assert skip == "ambiguous_both"

    def test_gap_adverse_long(self) -> None:
        levels = self._levels()
        # open が buy_stop を超えてギャップ → fill = open
        gap_open = levels.buy_stop + 0.05
        mins = [
            _bar1m(
                _london(2024, 6, 3, 9, 1),
                o=gap_open,
                h=gap_open + 0.01,
                low=gap_open - 0.01,
                c=gap_open,
            )
        ]
        fill, skip = resolve_fill(mins, levels)
        assert skip is None
        assert fill is not None
        assert fill.direction == 1
        assert fill.price == pytest.approx(gap_open)

    def test_gap_adverse_short(self) -> None:
        levels = self._levels()
        gap_open = levels.sell_stop - 0.05
        mins = [
            _bar1m(
                _london(2024, 6, 3, 9, 1),
                o=gap_open,
                h=gap_open + 0.01,
                low=gap_open - 0.01,
                c=gap_open,
            )
        ]
        fill, skip = resolve_fill(mins, levels)
        assert skip is None
        assert fill is not None
        assert fill.direction == -1
        assert fill.price == pytest.approx(gap_open)

    def test_no_fill_after_cancel(self) -> None:
        levels = self._levels()
        # 11:01 以降は見ない。9:00〜11:00 は触れない
        mins = [
            _bar1m(
                _london(2024, 6, 3, 11, 1),
                o=150.0,
                h=levels.buy_stop + 1,
                low=150.0,
                c=150.0,
            )
        ]
        fill, skip = resolve_fill(mins, levels)
        assert fill is None
        assert skip == "no_fill"

    def test_empty_minutes_is_no_fill(self) -> None:
        fill, skip = resolve_fill([], self._levels())
        assert fill is None
        assert skip == "no_fill"


class TestResolveExit:
    def test_sl_before_eod(self) -> None:
        levels = RangeLevels(
            high=150.20,
            low=149.80,
            buy_stop=150.21,
            sell_stop=149.79,
        )
        fill, _ = resolve_fill(
            [
                _bar1m(
                    _london(2024, 6, 3, 9, 10),
                    o=150.21,
                    h=150.22,
                    low=150.20,
                    c=150.21,
                )
            ],
            levels,
        )
        assert fill is not None
        # SL タッチ
        day_mins = [
            _bar1m(_london(2024, 6, 3, 9, 10), o=150.21, h=150.22, low=150.20, c=150.21),
            _bar1m(
                _london(2024, 6, 3, 10, 0),
                o=150.0,
                h=150.0,
                low=levels.sell_stop - 0.01,
                c=149.78,
            ),
        ]
        # 15分足: EOD 用にダミー（使われないはず）
        day_15 = [
            _bar15(_utc(2024, 6, 3, 7, m), o=150.0, h=150.2, low=149.8, c=150.0)
            for m in (0, 15, 30, 45)
        ]
        # EOD 足
        day_15.append(_bar15(_utc(2024, 6, 3, 14, 45), o=150.0, h=150.1, low=149.9, c=150.50))
        exit_ev = resolve_exit(day_mins, fill, day_15)
        assert exit_ev.outcome == "sl"
        assert exit_ev.price == pytest.approx(fill.sl)

    def test_eod_fallback_when_1600_bar_missing(self) -> None:
        levels = RangeLevels(
            high=150.20,
            low=149.80,
            buy_stop=150.21,
            sell_stop=149.79,
        )
        fill_bar = _bar1m(_london(2024, 6, 3, 9, 10), o=150.21, h=150.25, low=150.20, c=150.22)
        fill, _ = resolve_fill([fill_bar], levels)
        assert fill is not None
        # EOD まで SL なし。16:00 足は無い → 最終足終値
        day_mins = [
            fill_bar,
            _bar1m(_london(2024, 6, 3, 15, 0), o=150.30, h=150.35, low=150.28, c=150.32),
        ]
        day_15 = [
            _bar15(_utc(2024, 6, 3, 7, m), o=150.0, h=150.2, low=149.8, c=150.0)
            for m in (0, 15, 30, 45)
        ]
        # 最終足（local_close != 16:00）
        last = _bar15(_utc(2024, 6, 3, 14, 30), o=150.3, h=150.4, low=150.2, c=150.33)
        day_15.append(last)
        assert last.local_close.time().hour != 16 or last.local_close.time().minute != 0
        exit_ev = resolve_exit(day_mins, fill, day_15)
        assert exit_ev.outcome == "eod"
        assert exit_ev.price == pytest.approx(last.close)


class TestEvaluateDay:
    def test_no_window_returns_none(self) -> None:
        # local_close が窓外のみ
        bars = [_bar15(_utc(2024, 6, 3, 2, 0), o=150.0, h=150.1, low=149.9, c=150.0)]
        assert has_session_window(bars) is False
        assert evaluate_day("2024-06-03", bars, []) is None

    def test_no_range_classification(self) -> None:
        # 窓はあるがレンジ4本未満
        # local_close 8:15, 8:30, 10:00 → 窓内だがレンジは2本
        bars = [
            _bar15(_utc(2024, 6, 3, 7, 0), o=150.0, h=150.1, low=149.9, c=150.0),
            _bar15(_utc(2024, 6, 3, 7, 15), o=150.0, h=150.1, low=149.9, c=150.0),
            _bar15(_utc(2024, 6, 3, 8, 45), o=150.0, h=150.1, low=149.9, c=150.0),
        ]
        result = evaluate_day("2024-06-03", bars, [])
        assert result is not None
        assert result.skip_reason == "no_range"
        assert result.trade is None


class TestStopBufferPips:
    def test_buffer_is_one_pip(self) -> None:
        assert pytest.approx(1.0 * PIP) == STOP_BUFFER
        assert pytest.approx(0.01) == STOP_BUFFER
