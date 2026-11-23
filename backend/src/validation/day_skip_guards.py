"""RD-8 エッジガード day-skip hard rule（§3.3 / Issue #194）。

判断バッチ冒頭（LLM 呼び出し前）で機械判定可能な day-skip を評価する。
閾値は IS 由来の初回固定値（§7.4 凍結ベースラインとは別軸の RD 追補 hard rule）。
G1 既定値のみ RD-17（#219）でフォワード実測（GMO 通常スプレッド 0.5 pips への改定）に基づき
0.5 → 1.0 に recalibrate 済み（§6 限界 #3 の監視トリガー該当・恣意的チューニングではない）。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# USD/JPY: 1 pip = 0.01 円（position_sizing.py と同じ前提）
USD_JPY_PIP = 0.01


@dataclass
class EdgeGuardDaySkipResult:
    """エッジガード day-skip 判定結果（LLM 呼び出し前 hard rule）。"""

    skip: bool
    reason: str | None = None  # wide_spread | high_vol_regime | no_momentum_signal
    details: dict[str, Any] = field(default_factory=dict)


def _env_float(name: str, default: str) -> float:
    raw = os.environ.get(name, default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "edge_guard_env_invalid",
            extra={"env_name": name, "raw": raw, "fallback": default},
        )
        return float(default)


def evaluate_edge_guard_day_skip(
    *,
    current_price_mid: float,
    spread_pips: float,
    atr_14d: float,
    session_open: float | None,
) -> EdgeGuardDaySkipResult:
    """G1〜G3 エッジガード day-skip（評価順 G1 → G2 → G3、最初の該当理由を返す）。

    session_open が None のとき G3 は pass（RD-5 degrade と同方針 — 欠損で NO_ENTRY に倒さない）。
    """
    # RD-17 #219: 価格どうしの引き算由来の float ノイズ（真値 0.5 が 0.5000000000023874 /
    # 0.49999999999954525 に揺れる）で境界判定が非決定的になるため、0.01 pip 精度に丸めて判定する。
    spread_pips = round(spread_pips, 2)
    max_spread_pips = _env_float("MAX_DECISION_SPREAD_PIPS", "1.0")
    if spread_pips >= max_spread_pips:
        details = {
            "spread_pips": spread_pips,
            "threshold_pips": max_spread_pips,
        }
        logger.info({"event": "edge_guard_day_skip", "guard": "G1", **details})
        return EdgeGuardDaySkipResult(
            skip=True,
            reason="wide_spread_day_skip",
            details=details,
        )

    max_atr_pips = _env_float("MAX_ATR_DAY_SKIP_PIPS", "160")
    atr_pips = atr_14d / USD_JPY_PIP
    if atr_pips >= max_atr_pips:
        details = {
            "atr_pips": atr_pips,
            "threshold_pips": max_atr_pips,
            "atr_14d": atr_14d,
        }
        logger.info({"event": "edge_guard_day_skip", "guard": "G2", **details})
        return EdgeGuardDaySkipResult(
            skip=True,
            reason="high_vol_regime_day_skip",
            details=details,
        )

    min_momentum_atr = _env_float("MIN_SESSION_MOMENTUM_ATR", "0.05")
    if session_open is not None:
        session_move = abs(current_price_mid - session_open)
        momentum_threshold = min_momentum_atr * atr_14d
        if session_move < momentum_threshold:
            details = {
                "session_move": session_move,
                "momentum_threshold": momentum_threshold,
                "min_momentum_atr": min_momentum_atr,
                "session_open": session_open,
                "current_price_mid": current_price_mid,
            }
            logger.info({"event": "edge_guard_day_skip", "guard": "G3", **details})
            return EdgeGuardDaySkipResult(
                skip=True,
                reason="no_momentum_signal_day_skip",
                details=details,
            )

    return EdgeGuardDaySkipResult(skip=False)
