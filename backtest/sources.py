"""判断ソース DecisionSource（BT-3, 設計書 §4）。

バックテストにおける「その日のトレード判断」の差し替え点。
LLM（BT-4 サブエージェント）・ルールベース・本番記録リプレイ（BT-5）を
同一プロトコルで供給し、エンジン側を判断方式から独立させる。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from backend.src.llm.mock_llm import generate_decision_mock
from backend.src.llm.prompt import PromptContext
from backend.src.llm.sl_profile import SlProfile
from backend.src.llm.trade_decision import PivotLevel, TradeDecision, no_entry

# ベンチマーク各ソースの sl_profile（standard→0.7）。凍結ベースラインでは SL/TP は engine が
# cfg.session_vol_*（凍結パラメータ）で算出する。LLM / BT-5 リプレイでは decision の倍率を使う
# （#187）。チューニング禁止（設計書 §7.4）。
_RULE_SL_PROFILE: SlProfile = "standard"

# decision の atr_multiplier / rr_target を SL/TP に反映するソース（本番 decision_batch と同挙動）
_DECISION_SIZING_SOURCES = frozenset({"mock", "subagent", "decision_log_replay"})


def uses_decision_sizing_params(source_name: str) -> bool:
    """SL/TP 倍率を decision から取得するか（凍結ベースラインは False）。"""
    return source_name in _DECISION_SIZING_SOURCES


@dataclass(frozen=True)
class DecisionOutput:
    """DecisionSource の出力（判断 + 監査メタ）。

    llm_tokens は実 LLM 消費トークン数。非 LLM ソースは None（0 と偽らない・§5.2）。
    """

    decision: TradeDecision
    llm_tokens: int | None = None
    model: str | None = None


class DecisionSource(Protocol):
    """1 営業日 1 判断を供給するプロトコル。"""

    name: str

    def decide(self, day: date, ctx: PromptContext) -> DecisionOutput: ...


class RuleBasedSource:
    """ピボット回帰ベースライン（全レポート必須掲載のベンチマーク・設計書 §7-3）。

    ルール（凍結・チューニング禁止・全レポート必須のナイーブ・ベンチマーク §7-3）:
      - 現値 > P → LONG:  P への押し目を LIMIT で拾う
      - 現値 < P → SHORT: P への戻りを LIMIT で売る
      - 現値 = P → NO_ENTRY
    SL/TP はエンジンの session_vol（本番 enrich・ATR_h 基準）が決定論算出する（Ver 3.6）。
    """

    name: str = "rule_based"

    def decide(self, day: date, ctx: PromptContext) -> DecisionOutput:
        if not (math.isfinite(ctx.atr_14d) and ctx.atr_14d > 0):
            # 14本フラット等で ATR=0 だと SL=entry となり下流が ValueError になる
            return DecisionOutput(
                decision=no_entry(f"rule_based: ATR 非正または非有限 ({ctx.atr_14d!r})")
            )
        pivot = ctx.pivot_p
        if ctx.current_price_mid > pivot:
            direction = "LONG"
        elif ctx.current_price_mid < pivot:
            direction = "SHORT"
        else:
            return DecisionOutput(decision=no_entry("rule_based: 現値がピボット P と同値"))

        decision = TradeDecision(
            rationale=f"rule_based: P 回帰 {direction}（P={pivot}, ATR={ctx.atr_14d:.4f}）",
            direction=direction,  # type: ignore[arg-type]
            referenced_pivot_level="P",
            order_type="LIMIT",
            entry_price=pivot,
            sl_profile=_RULE_SL_PROFILE,
            execution_required=True,
        )
        return DecisionOutput(decision=decision)


class MockSource:
    """既存 mock_llm（Phase 1 モック稼働と同一ロジック）のラップ。CI・メカニクス検証用。"""

    name: str = "mock"

    def decide(self, day: date, ctx: PromptContext) -> DecisionOutput:
        result = generate_decision_mock(ctx)
        return DecisionOutput(
            decision=result.decision,
            llm_tokens=result.input_tokens + result.output_tokens,
            model=result.model,
        )


# 夜型ベースライン（RD-2 #167）の凍結パラメータ（チューニング禁止・設計書 §7 / 親 #165）。
# エントリーは現値から ±0.05×ATR14 の押し目/戻り LIMIT（no_fill 問題への対策）。
_NIGHT_ENTRY_OFFSET_ATR = 0.05


def _night_decision(
    direction: str,
    entry_price: float,
    rationale: str,
    *,
    pivot_level: PivotLevel = "P",
) -> TradeDecision:
    """夜型ベースライン共通の TradeDecision 構築。

    SL/TP は engine の session_vol（本番 enrich・ATR_h 基準）が決定論算出するため、
    ここでは entry_price / direction のみを与える（take_profit / stop_loss は
    スキーマから排除済み・§3.2 Ver 3.6）。sl_profile は監査ログ上の standard（0.7）。

    pivot_level は判断根拠の監査値（§5.2）。P 回帰型は "P"、当日始値ベースは "NONE"
    （実際に参照した水準を記録し、レポートの根拠追跡を誤らせない・CodeRabbit 指摘 PR #174）。
    """
    return TradeDecision(
        rationale=rationale,
        direction=direction,  # type: ignore[arg-type]
        referenced_pivot_level=pivot_level,
        order_type="LIMIT",
        entry_price=entry_price,
        sl_profile=_RULE_SL_PROFILE,
        execution_required=True,
    )


class NightPTrendSource:
    """夜型ピボット・トレンド追随ベースライン（凍結・RD-2 #167）。

    - 現値 > P → LONG:  現値から 0.05×ATR 下の押し目を LIMIT で拾う
    - 現値 < P → SHORT: 現値から 0.05×ATR 上の戻りを LIMIT で売る
    - 現値 = P → NO_ENTRY
    SL/TP は engine の session_vol が算出（SL=0.7×ATR_h・TP=1.0×SL 幅）。
    """

    name: str = "night_p_trend"

    def decide(self, day: date, ctx: PromptContext) -> DecisionOutput:
        if not (math.isfinite(ctx.atr_14d) and ctx.atr_14d > 0):
            return DecisionOutput(
                decision=no_entry(f"night_p_trend: ATR 非正または非有限 ({ctx.atr_14d!r})")
            )
        offset = _NIGHT_ENTRY_OFFSET_ATR * ctx.atr_14d
        if ctx.current_price_mid > ctx.pivot_p:
            direction, entry = "LONG", ctx.current_price_mid - offset
        elif ctx.current_price_mid < ctx.pivot_p:
            direction, entry = "SHORT", ctx.current_price_mid + offset
        else:
            return DecisionOutput(decision=no_entry("night_p_trend: 現値がピボット P と同値"))
        rationale = (
            f"night_p_trend: 現値 {ctx.current_price_mid} vs P {ctx.pivot_p} → {direction}"
            f"（押し目/戻り LIMIT, ATR={ctx.atr_14d:.4f}）"
        )
        return DecisionOutput(decision=_night_decision(direction, entry, rationale))


class NightDayMomentumSource:
    """夜型・当日モメンタム追随ベースライン（凍結・RD-2 #167）。

    当日（06:00 → 判断時刻）の騰落方向にフォロー:
      - 現値 > 当日始値 → LONG（押し目 LIMIT）
      - 現値 < 当日始値 → SHORT（戻り LIMIT）
    session_open が None（当日足なし）または ATR 非正なら NO_ENTRY（degrade）。
    """

    name: str = "night_day_momentum"

    def decide(self, day: date, ctx: PromptContext) -> DecisionOutput:
        if not (math.isfinite(ctx.atr_14d) and ctx.atr_14d > 0):
            return DecisionOutput(
                decision=no_entry(f"night_day_momentum: ATR 非正または非有限 ({ctx.atr_14d!r})")
            )
        if ctx.session_open is None:
            return DecisionOutput(
                decision=no_entry("night_day_momentum: 当日セッション始値なし（degrade）")
            )
        offset = _NIGHT_ENTRY_OFFSET_ATR * ctx.atr_14d
        if ctx.current_price_mid > ctx.session_open:
            direction, entry = "LONG", ctx.current_price_mid - offset
        elif ctx.current_price_mid < ctx.session_open:
            direction, entry = "SHORT", ctx.current_price_mid + offset
        else:
            return DecisionOutput(decision=no_entry("night_day_momentum: 現値が当日始値と同値"))
        rationale = (
            f"night_day_momentum: 現値 {ctx.current_price_mid} vs 当日始値 {ctx.session_open} "
            f"→ {direction}（押し目/戻り LIMIT, ATR={ctx.atr_14d:.4f}）"
        )
        # 判断根拠は当日始値であり Pivot P ではないため NONE を記録（監査精度・CodeRabbit 指摘）
        return DecisionOutput(
            decision=_night_decision(direction, entry, rationale, pivot_level="NONE")
        )
