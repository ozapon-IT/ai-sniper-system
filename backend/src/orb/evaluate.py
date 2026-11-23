"""1営業日の判定（pure）。"""

from __future__ import annotations

from collections.abc import Sequence

from backend.src.orb.models import Bar1m, Bar15, DayResult, RangeLevels, Trade
from backend.src.orb.range import has_session_window, range_from_15m
from backend.src.orb.resolve import resolve_exit, resolve_fill


def evaluate_day(
    day: str,
    day_bars_15: Sequence[Bar15],
    day_minutes: Sequence[Bar1m],
    *,
    range_levels: RangeLevels | None = None,
) -> DayResult | None:
    """1窓日を評価する。窓外なら None（窓日カウント対象外）。

    Args:
        day: ``YYYY-MM-DD``（Europe/London 日付）
        day_bars_15: 当該日の 15分足（ウォームアップ済み系列から抽出）
        day_minutes: 当該日の 1分足（7:45〜16:30 読み込み済み想定）
        range_levels: 事前計算したレンジ。None なら 15分足4本から算出（オラクル経路）
    """
    if not has_session_window(day_bars_15):
        return None

    levels = range_levels if range_levels is not None else range_from_15m(day_bars_15)
    if levels is None:
        return DayResult(day=day, skip_reason="no_range", trade=None, range_levels=None)

    fill, skip = resolve_fill(day_minutes, levels)
    if skip == "ambiguous_both":
        return DayResult(day=day, skip_reason="ambiguous_both", trade=None, range_levels=levels)
    if skip == "no_fill" or fill is None:
        return DayResult(day=day, skip_reason="no_fill", trade=None, range_levels=levels)

    exit_ev = resolve_exit(day_minutes, fill, day_bars_15)
    trade = Trade(
        day=day,
        direction=fill.direction,
        outcome=exit_ev.outcome,
        r=exit_ev.r,
        risk_pips=fill.risk_pips,
        entry=fill.price,
        exit=exit_ev.price,
        sl=fill.sl,
        buy_stop=levels.buy_stop,
        sell_stop=levels.sell_stop,
        fill_et=fill.fill_et.isoformat(),
        range_high=levels.high,
        range_low=levels.low,
    )
    return DayResult(day=day, skip_reason=None, trade=trade, range_levels=levels)
