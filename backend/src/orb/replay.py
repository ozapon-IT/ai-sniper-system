"""全営業日リプレイ（オラクル run() 相当）。"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from backend.src.orb.cache_loader import load_bars_15m, load_minutes_by_day
from backend.src.orb.constants import PIP, WARMUP_BARS
from backend.src.orb.evaluate import evaluate_day
from backend.src.orb.metrics import summarize
from backend.src.orb.models import (
    Bar1m,
    Bar15,
    DayResult,
    FunnelStats,
    RangeLevels,
    ReplaySummary,
    Trade,
)
from backend.src.orb.range import (
    collect_window_spreads,
    has_session_window,
    range_from_1m,
    range_from_15m,
)


@dataclass(frozen=True)
class ReplayResult:
    trades: list[Trade]
    day_results: list[DayResult]
    window_days: frozenset[str]
    funnel: FunnelStats
    spread_med: float
    spread_window: list[float]
    summary: ReplaySummary


def replay_from_cache(
    cache_root: Path,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    use_1m_range: bool = False,
) -> ReplayResult:
    """キャッシュから全窓日をリプレイする。

    Args:
        cache_root: ``USD_JPY`` キャッシュルート（BID/ASK/15min/1min を含む）
        date_from / date_to: inclusive ``YYYY-MM-DD`` フィルタ（省略時は全期間）
        use_1m_range: True ならレンジを1分足60本から算出（設計書 §6.2 経路）。
            False（既定）はオラクルと同じ15分足4本経路。
    """
    bid = load_bars_15m(cache_root, "BID")
    ask = load_bars_15m(cache_root, "ASK")
    minutes = load_minutes_by_day(cache_root)
    ask_by_t = {b.open_time: b for b in ask}

    days: dict[str, list[int]] = {}
    for i, b in enumerate(bid):
        if i < WARMUP_BARS:
            continue
        days.setdefault(b.local_close.date().isoformat(), []).append(i)

    trades: list[Trade] = []
    day_results: list[DayResult] = []
    spread_window: list[float] = []
    window_days: set[str] = set()
    funnel: dict[str, int] = {"no_range": 0, "no_fill": 0, "ambiguous_both": 0, "fills": 0}

    for day, idxs in sorted(days.items()):
        if (date_from and day < date_from) or (date_to and day > date_to):
            continue

        day_bars = [bid[i] for i in idxs]
        result = _eval_one(
            day,
            day_bars,
            minutes.get(day, []),
            ask_by_t=ask_by_t,
            spread_window=spread_window,
            window_days=window_days,
            use_1m_range=use_1m_range,
        )
        if result is None:
            continue
        day_results.append(result)
        if result.skip_reason is not None:
            funnel[result.skip_reason] += 1
        elif result.trade is not None:
            trades.append(result.trade)
            funnel["fills"] += 1

    med_spread = statistics.median(spread_window) if spread_window else 0.0
    trades_net = [replace(t, r_net=t.r - med_spread / t.risk_pips) for t in trades]
    # day_results 内の trade も r_net 付与
    day_results_net: list[DayResult] = []
    trade_by_day = {t.day: t for t in trades_net}
    for dr in day_results:
        if dr.trade is not None and dr.day in trade_by_day:
            day_results_net.append(
                DayResult(
                    day=dr.day,
                    skip_reason=dr.skip_reason,
                    trade=trade_by_day[dr.day],
                    range_levels=dr.range_levels,
                )
            )
        else:
            day_results_net.append(dr)

    funnel_stats = FunnelStats(
        no_range=funnel["no_range"],
        no_fill=funnel["no_fill"],
        ambiguous_both=funnel["ambiguous_both"],
        fills=funnel["fills"],
    )
    summary = summarize(trades_net, window_days, med_spread, funnel_stats)
    return ReplayResult(
        trades=trades_net,
        day_results=day_results_net,
        window_days=frozenset(window_days),
        funnel=funnel_stats,
        spread_med=med_spread,
        spread_window=spread_window,
        summary=summary,
    )


def _eval_one(
    day: str,
    day_bars: list[Bar15],
    day_minutes: list[Bar1m],
    *,
    ask_by_t: dict[datetime, Bar15],
    spread_window: list[float],
    window_days: set[str],
    use_1m_range: bool,
) -> DayResult | None:
    if not has_session_window(day_bars):
        return None
    window_days.add(day)
    spread_window.extend(collect_window_spreads(day_bars, ask_by_t, pip=PIP))

    levels: RangeLevels | None = None
    if use_1m_range:
        levels = range_from_1m(day_minutes)
        # 1分足欠損時は15分足で代替（設計書 §6.2）。両方取れなければ no_range。
        if levels is None:
            levels = range_from_15m(day_bars)
            if levels is None:
                return DayResult(day=day, skip_reason="no_range", trade=None, range_levels=None)
    return evaluate_day(day, day_bars, day_minutes, range_levels=levels)


def compare_range_sources(
    cache_root: Path,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, object]]:
    """全窓日で 15分足由来レンジと 1分足由来レンジの差分を返す。

    空リストなら全一致。不一致要素は ``day`` / ``from_15m`` / ``from_1m`` を含む。
    """
    bid = load_bars_15m(cache_root, "BID")
    minutes = load_minutes_by_day(cache_root)

    days: dict[str, list[int]] = {}
    for i, b in enumerate(bid):
        if i < WARMUP_BARS:
            continue
        days.setdefault(b.local_close.date().isoformat(), []).append(i)

    mismatches: list[dict[str, object]] = []
    for day, idxs in sorted(days.items()):
        if (date_from and day < date_from) or (date_to and day > date_to):
            continue
        day_bars = [bid[i] for i in idxs]
        if not has_session_window(day_bars):
            continue
        r15 = range_from_15m(day_bars)
        r1 = range_from_1m(minutes.get(day, []))
        if r15 is None and r1 is None:
            continue
        if r15 is None or r1 is None or r15.high != r1.high or r15.low != r1.low:
            mismatches.append(
                {
                    "day": day,
                    "from_15m": None if r15 is None else {"high": r15.high, "low": r15.low},
                    "from_1m": None if r1 is None else {"high": r1.high, "low": r1.low},
                }
            )
    return mismatches
