"""リプレイ集計（オラクル _summarize 相当）。"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence

from backend.src.orb.models import FunnelStats, ReplaySummary, Trade


def summarize(
    trades: Sequence[Trade],
    window_days: set[str] | frozenset[str],
    med_spread: float,
    funnel: FunnelStats,
) -> ReplaySummary:
    n = len(trades)
    sl_n = sum(1 for t in trades if t.outcome == "sl")
    eod_r = [t.r for t in trades if t.outcome == "eod"]
    r_gross = [t.r for t in trades]
    r_nets = [t.r_net for t in trades if t.r_net is not None]
    risks = [t.risk_pips for t in trades]

    ev_g = statistics.mean(r_gross) if r_gross else 0.0
    ev_n = statistics.mean(r_nets) if r_nets else 0.0
    sd = statistics.stdev(r_nets) if len(r_nets) >= 2 else 0.0
    se = sd / math.sqrt(len(r_nets)) if r_nets else 0.0
    t_val = ev_n / se if se > 0 else 0.0

    return ReplaySummary(
        days=len(window_days),
        fills=n,
        funnel=funnel,
        sl_pct=round(100 * sl_n / n, 1) if n else None,
        eod_r_mean=round(statistics.mean(eod_r), 3) if eod_r else None,
        risk_pips_med=round(statistics.median(risks), 1) if risks else None,
        spread_med=round(med_spread, 2),
        ev_gross=round(ev_g, 4),
        ev_net=round(ev_n, 4),
        sd=round(sd, 3),
        se=round(se, 3),
        t=round(t_val, 2),
    )
