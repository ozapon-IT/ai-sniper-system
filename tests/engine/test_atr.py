"""atr.py のユニットテスト（§3.2）。"""

import pytest

from backend.src.engine.atr import ATR_PERIOD, calculate_atr


def _make_candle(high: float, low: float, close: float) -> dict:
    return {"high": str(high), "low": str(low), "close": str(close), "open": str(close)}


class TestCalculateAtr:
    def _make_candles(self, n: int = ATR_PERIOD + 1) -> list[dict]:
        """単純なダミー日足データ（H/L が固定の TR=1.0 になる系列）。"""
        candles = []
        base_close = 146.0
        for _ in range(n):
            high = base_close + 0.5
            low = base_close - 0.5
            candles.append(_make_candle(high, low, base_close))
        return candles

    def test_constant_range_returns_one(self) -> None:
        """H-L が常に 1.0 かつ gap なしなら ATR=1.0。"""
        candles = self._make_candles(ATR_PERIOD + 1)
        result = calculate_atr(candles)
        assert result == pytest.approx(1.0, abs=0.0001)

    def test_requires_minimum_candles(self) -> None:
        with pytest.raises(ValueError, match="ATR 算出には"):
            calculate_atr(self._make_candles(ATR_PERIOD))  # period 本だけでは不足

    def test_uses_last_period_plus_one(self) -> None:
        """追加の足が先頭にあっても最新 period+1 本を使う。"""
        # 先頭に H-L=10 の外れ値足を追加（30本）
        candles = [_make_candle(160.0, 150.0, 155.0)] * 10
        candles += self._make_candles(ATR_PERIOD + 1)
        result = calculate_atr(candles)
        assert result == pytest.approx(1.0, abs=0.0001)

    def test_true_range_uses_prev_close_gap(self) -> None:
        """前日終値とのギャップが H-L より大きい場合に TR に反映される。"""
        # prev_close=146.0, H=146.1, L=146.05 → TR = |146.1 - 146.0| = 0.1... ではなく
        # gap: |146.1 - 146.0|=0.1, |146.05 - 146.0|=0.05, H-L=0.05 → TR=0.1
        # ここでは前日終値=140 からギャップが大きいケースをテスト
        # period 本の TR: 初日(prev_close=140): max(146-145, |146-140|, |145-140|) = 6
        candles = [_make_candle(140.0, 139.0, 140.0)]  # base: close=140
        for _ in range(ATR_PERIOD):
            candles.append(_make_candle(146.0, 145.0, 145.5))
        result = calculate_atr(candles, period=ATR_PERIOD)
        # 最初の TR = max(1, 6, 5) = 6、残り ATR_PERIOD-1 本は max(1, 0.5, 0.5) = 1
        expected = (6.0 + 1.0 * (ATR_PERIOD - 1)) / ATR_PERIOD
        assert result == pytest.approx(expected, abs=0.0001)
