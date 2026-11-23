"""ロンドンORB 判定コアの型定義。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from backend.src.orb.constants import PIP

SkipReason = Literal["no_range", "no_fill", "ambiguous_both"]
Outcome = Literal["sl", "eod"]
Direction = Literal[1, -1]  # 1=long, -1=short


@dataclass(frozen=True)
class Bar15:
    """15分足。open_time は UTC（klines openTime）。

    判定は local_close（open_time + 15min を Europe/London に変換）で行う。
    1分足の openTime 基準とは非対称 — 揃えると全日ズレる。
    """

    open_time: datetime  # UTC aware
    open: float
    high: float
    low: float
    close: float
    local_close: datetime  # Europe/London


@dataclass(frozen=True)
class Bar1m:
    """1分足。local_open は Europe/London に変換した openTime。"""

    local_open: datetime  # Europe/London
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class RangeLevels:
    high: float
    low: float
    buy_stop: float
    sell_stop: float

    @property
    def width_pips(self) -> float:
        return (self.high - self.low) / PIP


@dataclass(frozen=True)
class Trade:
    """約定1件。r_net はスプレッド中央値確定後に付与する。"""

    day: str
    direction: Direction
    outcome: Outcome
    r: float
    risk_pips: float
    entry: float
    exit: float
    sl: float
    buy_stop: float
    sell_stop: float
    fill_et: str
    range_high: float
    range_low: float
    r_net: float | None = None


@dataclass(frozen=True)
class DayResult:
    """1営業日の判定結果。window 外の日は呼び出し側で除外する。"""

    day: str
    skip_reason: SkipReason | None
    trade: Trade | None
    range_levels: RangeLevels | None


@dataclass(frozen=True)
class FunnelStats:
    no_range: int = 0
    no_fill: int = 0
    ambiguous_both: int = 0
    fills: int = 0


@dataclass(frozen=True)
class ReplaySummary:
    days: int
    fills: int
    funnel: FunnelStats
    sl_pct: float | None
    eod_r_mean: float | None
    risk_pips_med: float | None
    spread_med: float
    ev_gross: float
    ev_net: float
    sd: float
    se: float
    t: float
