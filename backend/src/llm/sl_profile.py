"""損切り幅プロファイル sl_profile（RD-12 #203, §3.2）。

LLM は離散ノブ ``sl_profile``（standard / wide）のみ出力し、Python が
``atr_multiplier``（0.7 / 0.8）へ決定論マップする。閾値は RD1-7 監査に基づき
固定（§7.4 チューニング禁止）。
"""

from __future__ import annotations

from typing import Literal

# RD1-7（2024Q2・同一ウィンドウ）で atr_multiplier=0.8 が使われた日の ATR14 下限。
# IS 損失関数での再探索は禁止（Issue #203 / §7.4）。
ATR14_WIDE_THRESHOLD = 1.2

RecommendedSlProfile = Literal["STANDARD", "WIDE"]
SlProfile = Literal["standard", "wide"]

SL_PROFILE_TO_ATR: dict[SlProfile, float] = {
    "standard": 0.7,
    "wide": 0.8,
}


def compute_recommended_sl_profile(atr_14d: float) -> RecommendedSlProfile:
    """判断時点の ATR14 から Python 推奨 sl_profile を決定する（user prompt 注入用）。"""
    if atr_14d >= ATR14_WIDE_THRESHOLD:
        return "WIDE"
    return "STANDARD"


def session_range_over_atr(
    session_high: float | None,
    session_low: float | None,
    atr_14d: float,
) -> float | None:
    """当日レンジ幅 / ATR14。高安が取れないときは None（degrade）。"""
    if session_high is None or session_low is None or atr_14d <= 0:
        return None
    return (session_high - session_low) / atr_14d


def resolve_atr_multiplier(sl_profile: SlProfile) -> float:
    """sl_profile → atr_multiplier（enrich / engine 用）。"""
    return SL_PROFILE_TO_ATR[sl_profile]
