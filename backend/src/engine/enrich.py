"""Python エンリッチ: stop_loss / take_profit の決定論的算出（§3.2・Ver 3.6）。

保有窓ボラ ``ATR_h = ATR14 × √(HOLD_HOURS/24)`` を単位に、LLM が出力した
``entry_price`` / ``atr_multiplier`` / ``rr_target`` から SL・TP を Python が確定する。
LLM は stop_loss / take_profit を出力しない（AGENTS.md ガードレール #5・付録 A.1/A.2）。

- SL = entry ∓ ATR_h × atr_multiplier（倍率 0.6〜1.0・既定 0.7・範外クランプ）
- TP = entry ± rr_target × |entry − SL|（rr_target 1.0〜1.5・既定 1.0・範外クランプ）

tick 丸めはどちらも「entry から遠ざける向き」（SL: LONG 切り下げ / SHORT 切り上げ、
TP: LONG 切り上げ / SHORT 切り下げ）。これにより ``D_stoploss_pips`` がロット計算と
発注価格で常に一致し、R:R は construction 上で常に rr_target 以上になる（reject 構造ゼロ）。
"""

from __future__ import annotations

import math
import os
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Literal

# USD/JPY の最小呼値刻み（小数点以下3桁 = 0.001。E05 #12 で確定）。
USD_JPY_TICK_SIZE = Decimal("0.001")

# 保有窓（時間）。ATR_h = ATR14 × √(HOLD_HOURS/24) の分子（§3.2・env HOLD_HOURS）。
HOLD_HOURS_DEFAULT = 8.0

# atr_multiplier の有効範囲・既定（§3.2・付録 A.1）。範囲外は拒否ではなく **クランプ**。
ATR_MULT_MIN = 0.6
ATR_MULT_MAX = 1.0
ATR_MULT_DEFAULT = 0.7

# rr_target の有効範囲・既定（§3.2・付録 A.1）。既定は env RR_TARGET_DEFAULT で上書き可。
RR_TARGET_MIN = 1.0
RR_TARGET_MAX = 1.5
RR_TARGET_DEFAULT = 1.0

Side = Literal["LONG", "SHORT"]


def resolve_hold_hours(hold_hours: float | None = None) -> float:
    """保有窓（時間）を解決する。明示指定 > env HOLD_HOURS > 既定 8。非正/不正は既定へフォールバック。"""
    if hold_hours is not None:
        return hold_hours if hold_hours > 0 else HOLD_HOURS_DEFAULT
    raw = os.environ.get("HOLD_HOURS")
    if raw is None:
        return HOLD_HOURS_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        return HOLD_HOURS_DEFAULT
    return value if value > 0 else HOLD_HOURS_DEFAULT


def resolve_rr_target_default() -> float:
    """rr_target の既定値を解決する（env RR_TARGET_DEFAULT。不正は 1.0）。範囲外はクランプ。"""
    raw = os.environ.get("RR_TARGET_DEFAULT")
    if raw is None:
        return RR_TARGET_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        return RR_TARGET_DEFAULT
    return _clamp(value, RR_TARGET_MIN, RR_TARGET_MAX)


def calculate_atr_h(atr: float, hold_hours: float | None = None) -> float:
    """保有窓ボラ ATR_h = ATR14 × √(HOLD_HOURS/24) を算出する（§3.2）。

    数理コアとして自身でも atr を検証する（非正/非有限は ValueError。直接呼び出し時の防御）。
    """
    _require_positive_atr(atr)
    hh = resolve_hold_hours(hold_hours)
    return atr * math.sqrt(hh / 24.0)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _require_side(side: str) -> None:
    if side not in {"LONG", "SHORT"}:
        raise ValueError(f"side は 'LONG' または 'SHORT' でなければなりません: {side!r}")


def _require_positive_atr(atr: float) -> None:
    if not (math.isfinite(atr) and atr > 0):
        raise ValueError(f"atr は正の有限値でなければなりません: {atr!r}")


def _round_away_from_entry(price: Decimal, side: Side, *, is_stop: bool) -> Decimal:
    """呼値刻みへ「entry から遠ざける向き」に丸める（§3.2）。

    SL は LONG 切り下げ / SHORT 切り上げ、TP は LONG 切り上げ / SHORT 切り下げ。
    SL/TP とも reward/risk を悲観側にし、丸め誤差で R:R が rr_target を割るのを防ぐ。
    """
    if is_stop:
        rounding = ROUND_DOWN if side == "LONG" else ROUND_UP
    else:
        rounding = ROUND_UP if side == "LONG" else ROUND_DOWN
    return price.quantize(USD_JPY_TICK_SIZE, rounding=rounding)


def calculate_stop_loss(
    entry_price: float,
    atr: float,
    atr_multiplier: float,
    side: Side,
    *,
    hold_hours: float | None = None,
) -> float:
    """stop_loss を ATR_h 基準で決定論的に算出し tick 丸めを適用する（§3.2・Ver 3.6）。

    Args:
        entry_price: エントリー価格（tick 丸め済みを渡す）
        atr: 日足 ATR（14期間）。非正・非有限は ValueError。
        atr_multiplier: LLM 出力の倍率（0.6〜1.0・範囲外はクランプ）
        side: "LONG" or "SHORT"
        hold_hours: 保有窓（省略時は env HOLD_HOURS、既定 8）

    Returns:
        tick 丸め済みの stop_loss 価格（float）
    """
    _require_side(side)
    _require_positive_atr(atr)
    mult = _clamp(atr_multiplier, ATR_MULT_MIN, ATR_MULT_MAX)
    atr_h = calculate_atr_h(atr, hold_hours)
    stop_raw = entry_price - atr_h * mult if side == "LONG" else entry_price + atr_h * mult
    stop_dec = _round_away_from_entry(Decimal(str(stop_raw)), side, is_stop=True)
    return float(stop_dec)


def calculate_take_profit(
    entry_price: float,
    stop_loss: float,
    rr_target: float,
    side: Side,
) -> float:
    """take_profit を SL 幅から決定論的に算出し tick 丸めを適用する（§3.2・Ver 3.6）。

    TP = entry ± rr_target × |entry − stop_loss|。R:R を construction 上で担保する。

    Args:
        entry_price: エントリー価格（tick 丸め済み）
        stop_loss: Python 算出済みの stop_loss（tick 丸め済み）
        rr_target: LLM 出力の R:R 目標（1.0〜1.5・範囲外はクランプ）
        side: "LONG" or "SHORT"

    Returns:
        tick 丸め済みの take_profit 価格（float）
    """
    _require_side(side)
    rr = _clamp(rr_target, RR_TARGET_MIN, RR_TARGET_MAX)
    risk = abs(entry_price - stop_loss)
    tp_raw = entry_price + rr * risk if side == "LONG" else entry_price - rr * risk
    tp_dec = _round_away_from_entry(Decimal(str(tp_raw)), side, is_stop=False)
    return float(tp_dec)
