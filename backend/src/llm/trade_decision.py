"""LLM 出力スキーマ TradeDecision（付録 A.1, §3・Ver 3.6 / RD-12）。

LLM が決めるのは direction / order_type / entry_price / rr_target /
referenced_pivot_level / sl_profile / rationale のみ。
`stop_loss` / `take_profit` とロット（V_raw/V_safe）は **含めない** ── Python が
決定論で算出する（§3.2, AGENTS.md #5 / #1）。

RD-12 (#203): LLM は離散 ``sl_profile``（standard / wide）を出力し、
``atr_multiplier`` は Python がマップする（0.7 / 0.8）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator

from backend.src.engine.enrich import (
    RR_TARGET_MAX,
    RR_TARGET_MIN,
    resolve_rr_target_default,
)
from backend.src.llm.sl_profile import SlProfile, resolve_atr_multiplier

Direction = Literal["LONG", "SHORT", "NO_ENTRY"]
OrderType = Literal["LIMIT", "STOP"]
PivotLevel = Literal["R1", "S1", "P", "R2", "S2", "NONE"]


class TradeDecision(BaseModel):
    """朝の意思決定 LLM の構造化出力（付録 A.1・Ver 3.6 / RD-12）。

    フィールド順は CoT-first: 分析→推論→結論→詳細→リスク→最終ゲート。
    """

    # --- 分析・推論（先行生成）---
    sentiment_summary: str | None = None
    rationale: str
    # --- 結論 ---
    direction: Direction
    referenced_pivot_level: PivotLevel = "NONE"
    # --- 発注詳細（SL/TP は Python が算出。LLM は sl_profile / rr_target のみ）---
    order_type: OrderType | None = None
    entry_price: float | None = None
    sl_profile: SlProfile
    rr_target: float = Field(default_factory=resolve_rr_target_default, validate_default=True)
    # --- 最終ゲート ---
    execution_required: bool

    @computed_field  # type: ignore[prop-decorator]
    @property
    def atr_multiplier(self) -> float:
        """sl_profile から決定論的に導出（§3.2・RD-12）。enrich / engine は本プロパティを使用。"""
        return resolve_atr_multiplier(self.sl_profile)

    @field_validator("rr_target", mode="after")
    @classmethod
    def _clamp_rr_target(cls, v: float) -> float:
        """rr_target を [1.0, 1.5] にクランプする（§3.2, 付録A）。範囲外でも拒否しない。"""
        return max(RR_TARGET_MIN, min(RR_TARGET_MAX, v))

    @model_validator(mode="after")
    def _require_order_fields_when_actionable(self) -> TradeDecision:
        """発注対象（execution_required=True かつ NO_ENTRY 以外）には発注データを必須化する。"""
        if self.execution_required and self.direction != "NO_ENTRY":
            missing = [
                name
                for name, value in (
                    ("order_type", self.order_type),
                    ("entry_price", self.entry_price),
                )
                if value is None
            ]
            if missing:
                raise ValueError(
                    "actionable な決定には "
                    f"{', '.join(missing)} が必須です "
                    f"(execution_required=True, direction={self.direction})"
                )
        return self

    @property
    def should_enter(self) -> bool:
        """発注処理へ進むか（execution_required かつ NO_ENTRY 以外）。"""
        return self.execution_required and self.direction != "NO_ENTRY"


def no_entry(rationale: str) -> TradeDecision:
    """発注を見送る安全側の TradeDecision を生成する（refusal / パース失敗 / 休場 等）。"""
    return TradeDecision(
        direction="NO_ENTRY",
        execution_required=False,
        referenced_pivot_level="NONE",
        rationale=rationale,
        sl_profile="standard",
        rr_target=resolve_rr_target_default(),
    )
