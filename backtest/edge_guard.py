"""RD-8 エッジガード day-skip をバックテスト経路に貫通（Issue #198）。

本番 `decision_batch` と同一の `evaluate_edge_guard_day_skip` を
`build_day_context` 直後（LLM / ルール判断前）で評価する。
"""

from __future__ import annotations

from backend.src.validation.day_skip_guards import (
    EdgeGuardDaySkipResult,
    evaluate_edge_guard_day_skip,
)
from backtest.context import DayContext

EDGE_GUARD_NO_ENTRY_RATIONALE = "エッジガード day-skip hard rule（§3.3 / RD-8 #194）"


def evaluate_day_context_edge_guard(day_ctx: DayContext) -> EdgeGuardDaySkipResult:
    """DayContext から本番同型入力で G1〜G3 を評価する。"""
    ctx = day_ctx.prompt_context
    return evaluate_edge_guard_day_skip(
        current_price_mid=ctx.current_price_mid,
        spread_pips=day_ctx.spread_pips,
        atr_14d=ctx.atr_14d,
        session_open=ctx.session_open,
    )
