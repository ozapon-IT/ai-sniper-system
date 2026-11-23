"""engine/enrich.py のユニットテスト（§3.2・Ver 3.6）。"""

from __future__ import annotations

import math

import pytest

from backend.src.engine.enrich import (
    ATR_MULT_MAX,
    ATR_MULT_MIN,
    HOLD_HOURS_DEFAULT,
    RR_TARGET_MAX,
    RR_TARGET_MIN,
    calculate_atr_h,
    calculate_stop_loss,
    calculate_take_profit,
    resolve_hold_hours,
)

_ROOT = math.sqrt(HOLD_HOURS_DEFAULT / 24.0)  # √(8/24) ≈ 0.57735


class TestAtrH:
    def test_formula(self) -> None:
        """ATR_h = ATR14 × √(HOLD_HOURS/24)。既定 HOLD_HOURS=8 → √(1/3)。"""
        assert calculate_atr_h(1.0) == pytest.approx(_ROOT)
        assert calculate_atr_h(0.5) == pytest.approx(0.5 * _ROOT)

    def test_hold_hours_override(self) -> None:
        assert calculate_atr_h(1.0, hold_hours=24.0) == pytest.approx(1.0)
        assert calculate_atr_h(1.0, hold_hours=6.0) == pytest.approx(0.5)

    def test_hold_hours_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOLD_HOURS", "24")
        assert resolve_hold_hours() == pytest.approx(24.0)
        assert calculate_atr_h(1.0) == pytest.approx(1.0)

    def test_hold_hours_env_invalid_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOLD_HOURS", "0")
        assert resolve_hold_hours() == pytest.approx(HOLD_HOURS_DEFAULT)
        monkeypatch.setenv("HOLD_HOURS", "not-a-number")
        assert resolve_hold_hours() == pytest.approx(HOLD_HOURS_DEFAULT)


class TestCalculateStopLoss:
    def test_long_basic(self) -> None:
        """LONG: entry=150.0, ATR=0.50, mult=0.7 → SL = entry - 0.7×ATR_h（entry から遠ざけ丸め）。"""
        result = calculate_stop_loss(150.000, 0.50, 0.7, "LONG")
        raw = 150.0 - 0.7 * 0.50 * _ROOT
        assert result == pytest.approx(math.floor(raw * 1000) / 1000, abs=1e-9)
        assert result < 150.0

    def test_short_basic(self) -> None:
        result = calculate_stop_loss(150.000, 0.50, 0.7, "SHORT")
        raw = 150.0 + 0.7 * 0.50 * _ROOT
        assert result == pytest.approx(math.ceil(raw * 1000) / 1000, abs=1e-9)
        assert result > 150.0

    def test_clamps_multiplier_below(self) -> None:
        """mult=0.3（下限以下）は 0.6 にクランプされる。"""
        assert calculate_stop_loss(150.0, 0.50, 0.3, "LONG") == pytest.approx(
            calculate_stop_loss(150.0, 0.50, ATR_MULT_MIN, "LONG"), abs=1e-9
        )

    def test_clamps_multiplier_above(self) -> None:
        """mult=2.0（上限超え）は 1.0 にクランプされる。"""
        assert calculate_stop_loss(150.0, 0.50, 2.0, "LONG") == pytest.approx(
            calculate_stop_loss(150.0, 0.50, ATR_MULT_MAX, "LONG"), abs=1e-9
        )

    def test_tick_aligned(self) -> None:
        """SL は 0.001 の倍数（D_stoploss_pips とロット計算が発注価格と一致する前提・§3.2）。"""
        result = calculate_stop_loss(150.000, 0.333, 0.7, "LONG")
        assert round(result, 3) == result

    def test_nonpositive_atr_raises(self) -> None:
        """ATR 非正・非有限は防御的に ValueError（SL=entry 退化を防ぐ）。"""
        for bad in (0.0, -0.5, float("nan"), float("inf")):
            with pytest.raises(ValueError, match="atr は"):
                calculate_stop_loss(150.0, bad, 0.7, "LONG")

    def test_invalid_side_raises(self) -> None:
        with pytest.raises(ValueError, match="side は"):
            calculate_stop_loss(150.0, 0.5, 0.7, "BUY")  # type: ignore[arg-type]


class TestCalculateTakeProfit:
    def test_long_rr1(self) -> None:
        """LONG rr=1.0: TP = entry + 1.0×|entry−SL|（entry から遠ざけ = 切り上げ）。"""
        entry, sl = 150.000, 149.800  # risk=0.200
        tp = calculate_take_profit(entry, sl, 1.0, "LONG")
        assert tp == pytest.approx(150.200, abs=1e-9)
        # R:R は construction 上 rr_target 以上（丸めで割らない）
        assert abs(tp - entry) >= abs(entry - sl)

    def test_short_rr1(self) -> None:
        entry, sl = 150.000, 150.200  # risk=0.200
        tp = calculate_take_profit(entry, sl, 1.0, "SHORT")
        assert tp == pytest.approx(149.800, abs=1e-9)
        assert abs(tp - entry) >= abs(entry - sl)

    def test_rr_target_clamped(self) -> None:
        entry, sl = 150.000, 149.800
        # rr=0.5 → 1.0 にクランプ / rr=3.0 → 1.5 にクランプ
        assert calculate_take_profit(entry, sl, 0.5, "LONG") == pytest.approx(
            calculate_take_profit(entry, sl, RR_TARGET_MIN, "LONG"), abs=1e-9
        )
        assert calculate_take_profit(entry, sl, 3.0, "LONG") == pytest.approx(
            calculate_take_profit(entry, sl, RR_TARGET_MAX, "LONG"), abs=1e-9
        )

    def test_rr_construction_never_below_target(self) -> None:
        """丸め後も reward/risk >= rr_target を満たす（reject 構造ゼロの根拠）。"""
        for entry, atr, mult, rr, side in [
            (155.123, 0.44, 0.7, 1.0, "LONG"),
            (155.123, 0.44, 0.7, 1.0, "SHORT"),
            (99.037, 1.23, 0.9, 1.2, "LONG"),
            (149.951, 0.08, 0.6, 1.5, "SHORT"),
        ]:
            sl = calculate_stop_loss(entry, atr, mult, side)  # type: ignore[arg-type]
            tp = calculate_take_profit(entry, sl, rr, side)  # type: ignore[arg-type]
            reward, risk = abs(tp - entry), abs(entry - sl)
            assert reward >= rr * risk - 1e-9

    def test_invalid_side_raises(self) -> None:
        with pytest.raises(ValueError, match="side は"):
            calculate_take_profit(150.0, 149.8, 1.0, "BUY")  # type: ignore[arg-type]
