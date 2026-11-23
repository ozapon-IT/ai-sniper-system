"""レンジ確定（15分足4本 / 1分足60本）とストップ価格。

設計書 §6.2 / ルール §3 / オラクル phase0_usdjpy_london_orb.py と同一。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, time

from backend.src.orb.constants import (
    EOD_TIME,
    ORDER_TIME,
    RANGE_BAR_COUNT_1M,
    RANGE_BAR_COUNT_15M,
    RANGE_START,
    STOP_BUFFER,
    WINDOW_END,
)
from backend.src.orb.models import Bar1m, Bar15, RangeLevels


def range_from_15m(day_bars: Sequence[Bar15]) -> RangeLevels | None:
    """15分足からレンジを確定する。

    条件: ``RANGE_START < local_close <= ORDER_TIME`` のちょうど 4 本。
    本数が異なれば None（``no_range``）。
    """
    range_bars = [b for b in day_bars if RANGE_START < b.local_close.time() <= ORDER_TIME]
    if len(range_bars) != RANGE_BAR_COUNT_15M:
        return None
    return _levels_from_hl(
        high=max(b.high for b in range_bars),
        low=min(b.low for b in range_bars),
    )


def range_from_1m(day_minutes: Sequence[Bar1m]) -> RangeLevels | None:
    """1分足からレンジを確定する（設計書 §6.2 本番経路）。

    条件: openTime（London）が ``8:00 <= t < 9:00`` のちょうど 60 本。
    15分足4本と数学的に等価であるべき（同一区間・同一 BID）。
    """
    range_bars = [b for b in day_minutes if RANGE_START <= b.local_open.time() < ORDER_TIME]
    if len(range_bars) != RANGE_BAR_COUNT_1M:
        return None
    return _levels_from_hl(
        high=max(b.high for b in range_bars),
        low=min(b.low for b in range_bars),
    )


def _levels_from_hl(*, high: float, low: float) -> RangeLevels:
    return RangeLevels(
        high=high,
        low=low,
        buy_stop=high + STOP_BUFFER,
        sell_stop=low - STOP_BUFFER,
    )


def eod_close_price(day_bars: Sequence[Bar15]) -> float | None:
    """London 16:00 に確定する 15分足の終値。無ければ None（呼び出し側で当日最終足へフォールバック）。"""
    bar_eod = next((b for b in day_bars if b.local_close.time() == EOD_TIME), None)
    return bar_eod.close if bar_eod is not None else None


def has_session_window(day_bars: Sequence[Bar15]) -> bool:
    """窓日判定: ``8:00 < local_close <= 11:00`` の足が1本でもあれば True。"""
    return any(RANGE_START < b.local_close.time() <= WINDOW_END for b in day_bars)


def collect_window_spreads(
    day_bars: Sequence[Bar15],
    ask_by_open: dict[datetime, Bar15],
    *,
    pip: float,
) -> list[float]:
    """窓内（8:00 < local_close <= 11:00）の ASK−BID 終値差を pips で返す。"""
    out: list[float] = []
    for b in day_bars:
        tc: time = b.local_close.time()
        if RANGE_START < tc <= WINDOW_END:
            abb = ask_by_open.get(b.open_time)
            if abb is not None:
                out.append((abb.close - b.close) / pip)
    return out
