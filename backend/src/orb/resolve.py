"""約定解決（1分足）とエグジット（SL / EOD）。

オラクル phase0_usdjpy_london_orb.py の fill〜exit ループと同一。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from backend.src.orb.constants import CANCEL_TIME, EOD_TIME, ORDER_TIME, PIP
from backend.src.orb.models import Bar1m, Bar15, Direction, Outcome, RangeLevels
from backend.src.orb.range import eod_close_price


@dataclass(frozen=True)
class FillEvent:
    direction: Direction
    price: float
    fill_et: datetime
    sl: float
    risk: float  # 価格単位（オラクルの risk と同一）
    risk_pips: float


@dataclass(frozen=True)
class ExitEvent:
    outcome: Outcome
    price: float
    r: float


def resolve_fill(
    day_minutes: Sequence[Bar1m],
    levels: RangeLevels,
    *,
    allow_long: bool = True,
    allow_short: bool = True,
) -> tuple[FillEvent | None, str | None]:
    """9:00〜11:00 の1分足で最初のトリガーを解決する。

    Returns:
        (fill, skip_reason) — skip_reason は ``ambiguous_both`` / ``no_fill`` / None
    """
    if not day_minutes:
        return None, "no_fill"

    buy_stop = levels.buy_stop
    sell_stop = levels.sell_stop
    fill_dir: Direction | None = None
    fill_px: float | None = None
    fill_et: datetime | None = None

    for bar in day_minutes:
        tt = bar.local_open.time()
        if tt < ORDER_TIME:
            continue
        if tt > CANCEL_TIME:
            break
        hit_buy = allow_long and bar.high >= buy_stop
        hit_sell = allow_short and bar.low <= sell_stop
        if hit_buy and hit_sell:
            return None, "ambiguous_both"
        if hit_buy:
            # ギャップ不利側: max(buy_stop, open)
            fill_dir, fill_px, fill_et = 1, max(buy_stop, bar.open), bar.local_open
            break
        if hit_sell:
            fill_dir, fill_px, fill_et = -1, min(sell_stop, bar.open), bar.local_open
            break

    if fill_dir is None or fill_px is None or fill_et is None:
        return None, "no_fill"

    sl = sell_stop if fill_dir > 0 else buy_stop
    risk = (fill_px - sl) if fill_dir > 0 else (sl - fill_px)
    if risk <= 0:
        return None, "no_fill"

    return (
        FillEvent(
            direction=fill_dir,
            price=fill_px,
            fill_et=fill_et,
            sl=sl,
            risk=risk,
            risk_pips=risk / PIP,
        ),
        None,
    )


def resolve_exit(
    day_minutes: Sequence[Bar1m],
    fill: FillEvent,
    day_bars_15: Sequence[Bar15],
) -> ExitEvent:
    """fill バー以降の SL / EOD を解決する。TP なし（基本形）。

    同一バーでは SL を先に判定。``et.time() >= 16:00`` で break し EOD。
    EOD 価格は local_close == 16:00 の 15分足終値。無ければ当日最終足の終値。
    """
    outcome: Outcome | None = None
    exit_px: float | None = None
    sl = fill.sl
    fill_dir = fill.direction

    for bar in day_minutes:
        if bar.local_open < fill.fill_et:
            continue
        if bar.local_open.time() >= EOD_TIME:
            break
        hit_sl = (bar.low <= sl) if fill_dir > 0 else (bar.high >= sl)
        if hit_sl:
            outcome, exit_px = "sl", sl
            break

    if outcome is None or exit_px is None:
        outcome = "eod"
        eod = eod_close_price(day_bars_15)
        exit_px = eod if eod is not None else day_bars_15[-1].close

    r = (exit_px - fill.price) / fill.risk * fill_dir
    return ExitEvent(outcome=outcome, price=exit_px, r=r)
