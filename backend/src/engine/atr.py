"""ATR（Average True Range）算出（§3.2）。

直近 14 日間の日足 True Range の単純平均を返す。
True Range = max(H-L, |H-prev_C|, |L-prev_C|)
"""

from __future__ import annotations

ATR_PERIOD = 14


def _true_range(high: float, low: float, prev_close: float) -> float:
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def calculate_atr(candles: list[dict[str, str]], period: int = ATR_PERIOD) -> float:
    """日足ローソク足リスト（時系列昇順: 古い→新しい）から ATR を算出する。

    candles は必ず昇順で渡すこと。降順だと prev_close が逆転し ATR を誤計算する。
    period 本分の TR を平均する。最低 period+1 本必要。
    """
    if len(candles) < period + 1:
        raise ValueError(
            f"ATR 算出には最低 {period + 1} 本の日足が必要です（受け取り: {len(candles)} 本）"
        )

    # 最新 period+1 本を使用
    subset = candles[-(period + 1) :]
    tr_values: list[float] = []
    for i in range(1, len(subset)):
        high = float(subset[i]["high"])
        low = float(subset[i]["low"])
        prev_c = float(subset[i - 1]["close"])
        tr_values.append(_true_range(high, low, prev_c))

    return sum(tr_values) / len(tr_values)
