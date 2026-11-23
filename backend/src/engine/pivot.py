"""ピボットポイント算出（§3.1）。

P  = (H + L + C) / 3
R1 = 2P - L
S1 = 2P - H
R2 = P + (H - L)
S2 = P - (H - L)

丸め方針: P を 0.001 に量子化してから R1/S1/R2/S2 を算出する。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal


@dataclass(frozen=True)
class PivotLevels:
    p: float
    r1: float
    s1: float
    r2: float
    s2: float


def calculate_pivot(high: float, low: float, close: float) -> PivotLevels:
    """§3.1 の式でピボットポイントを算出する。

    P を先に 0.001 丸めし、残りのラインは丸め済み P から導出する。
    """
    precision = Decimal("0.001")
    h = Decimal(str(high))
    low_ = Decimal(str(low))
    c = Decimal(str(close))

    p = ((h + low_ + c) / Decimal("3")).quantize(precision, rounding=ROUND_HALF_UP)
    hl_range = (h - low_).quantize(precision, rounding=ROUND_HALF_UP)

    return PivotLevels(
        p=float(p),
        r1=float((Decimal("2") * p - low_).quantize(precision, rounding=ROUND_HALF_UP)),
        s1=float((Decimal("2") * p - h).quantize(precision, rounding=ROUND_HALF_UP)),
        r2=float((p + hl_range).quantize(precision, rounding=ROUND_HALF_UP)),
        s2=float((p - hl_range).quantize(precision, rounding=ROUND_HALF_UP)),
    )


def pivot_from_candle(candle: dict[str, str]) -> PivotLevels:
    """KLine ローソク足辞書からピボットを算出する。"""
    return calculate_pivot(
        high=float(candle["high"]),
        low=float(candle["low"]),
        close=float(candle["close"]),
    )
