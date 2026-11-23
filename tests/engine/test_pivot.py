"""pivot.py のユニットテスト（§3.1）。"""

import pytest

from backend.src.engine.pivot import PivotLevels, calculate_pivot, pivot_from_candle


class TestCalculatePivot:
    def test_known_values(self) -> None:
        """Issue #14 受け入れ条件: P=146.667, R1=147.334 が得られること。

        H=147.0, L=146.0, C=147.0 で計算すると
        P = round((147+146+147)/3, 0.001) = round(146.666...) = 146.667
        R1 = round(2*146.667 - 146, 0.001) = round(147.334) = 147.334
        S1 = round(2*146.667 - 147, 0.001) = round(146.334) = 146.334
        HL = round(147-146, 0.001) = 1.000
        R2 = 146.667 + 1.000 = 147.667
        S2 = 146.667 - 1.000 = 145.667
        """
        result = calculate_pivot(high=147.0, low=146.0, close=147.0)
        assert result.p == pytest.approx(146.667, abs=0.001)
        assert result.r1 == pytest.approx(147.334, abs=0.001)
        assert result.s1 == pytest.approx(146.334, abs=0.001)
        assert result.r2 == pytest.approx(147.667, abs=0.001)
        assert result.s2 == pytest.approx(145.667, abs=0.001)

    def test_symmetric_hl(self) -> None:
        """H と L が終値対称の場合の基本算出。"""
        result = calculate_pivot(high=150.0, low=148.0, close=149.0)
        expected_p = round((150.0 + 148.0 + 149.0) / 3, 3)
        assert result.p == pytest.approx(expected_p, abs=0.001)

    def test_returns_pivot_levels_dataclass(self) -> None:
        result = calculate_pivot(high=147.0, low=146.0, close=146.5)
        assert isinstance(result, PivotLevels)

    def test_r2_greater_than_r1_greater_than_p(self) -> None:
        result = calculate_pivot(high=147.0, low=146.0, close=146.5)
        assert result.r2 > result.r1 > result.p

    def test_s2_less_than_s1_less_than_p(self) -> None:
        result = calculate_pivot(high=147.0, low=146.0, close=146.5)
        assert result.s2 < result.s1 < result.p


class TestPivotFromCandle:
    def test_from_dict(self) -> None:
        candle = {"high": "147.0", "low": "146.0", "close": "147.0", "open": "146.5"}
        result = pivot_from_candle(candle)
        assert result.p == pytest.approx(146.667, abs=0.001)
        assert result.r1 == pytest.approx(147.334, abs=0.001)
