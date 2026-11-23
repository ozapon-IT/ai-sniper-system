"""engine/leverage_cap.py のユニットテスト（§4.2）。"""

from __future__ import annotations

import pytest

from backend.src.engine.leverage_cap import NO_ENTRY, calculate_leverage_cap, calculate_v_safe


class TestCalculateLeverageCap:
    def test_basic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """MAX_LEVERAGE=25, equity=100000, price=150 → cap=floor(2500000/15000)*100=16600。"""
        monkeypatch.setenv("MAX_LEVERAGE", "25")
        cap = calculate_leverage_cap(equity=100_000.0, current_price=150.0)
        # (100000 * 25) / (150 * 100) = 2500000 / 15000 = 166.67 → floor → 166 → *100 = 16600
        assert cap == 16600

    def test_returns_multiple_of_100(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MAX_LEVERAGE", "25")
        cap = calculate_leverage_cap(equity=100_000.0, current_price=151.0)
        assert cap % 100 == 0


class TestCalculateVSafe:
    def test_v_rounded_below_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """V_rounded が cap より小さければ V_safe = V_rounded。"""
        monkeypatch.setenv("MAX_LEVERAGE", "25")
        # V_leverage_cap = 16600 (above)
        v_safe = calculate_v_safe(
            v_rounded=2000,
            equity=100_000.0,
            current_price=150.0,
            available_amount=500_000.0,
        )
        assert v_safe == 2000

    def test_v_rounded_above_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """V_rounded が cap を超える場合は cap で制限される（§受け入れ条件）。"""
        monkeypatch.setenv("MAX_LEVERAGE", "25")
        # cap = 16600
        v_safe = calculate_v_safe(
            v_rounded=20_000,
            equity=100_000.0,
            current_price=150.0,
            available_amount=5_000_000.0,
        )
        assert v_safe == 16600

    def test_insufficient_margin_returns_no_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """証拠金不足時は NO_ENTRY (0) を返す。"""
        monkeypatch.setenv("MAX_LEVERAGE", "25")
        v_safe = calculate_v_safe(
            v_rounded=2000,
            equity=100_000.0,
            current_price=150.0,
            available_amount=0.0,  # 証拠金なし
        )
        assert v_safe == NO_ENTRY

    def test_zero_v_rounded_returns_no_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """V_rounded=0 は NO_ENTRY。"""
        monkeypatch.setenv("MAX_LEVERAGE", "25")
        v_safe = calculate_v_safe(
            v_rounded=0,
            equity=100_000.0,
            current_price=150.0,
            available_amount=500_000.0,
        )
        assert v_safe == NO_ENTRY


class TestMaxLeverageValidation:
    @pytest.mark.parametrize("bad_value", ["0", "-5", "-0.1"])
    def test_non_positive_max_leverage_raises(
        self, bad_value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MAX_LEVERAGE が 0 以下なら ValueError（ゼロ除算/計算破綻を fail-loud で防ぐ）。"""
        monkeypatch.setenv("MAX_LEVERAGE", bad_value)
        with pytest.raises(ValueError, match="MAX_LEVERAGE"):
            calculate_leverage_cap(equity=100_000.0, current_price=150.0)

    def test_non_positive_max_leverage_raises_in_v_safe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """calculate_v_safe 側でも 0 以下の MAX_LEVERAGE は ValueError。"""
        monkeypatch.setenv("MAX_LEVERAGE", "0")
        with pytest.raises(ValueError, match="MAX_LEVERAGE"):
            calculate_v_safe(
                v_rounded=2000,
                equity=100_000.0,
                current_price=150.0,
                available_amount=500_000.0,
            )

    def test_default_max_leverage_is_10(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """MAX_LEVERAGE 未設定時の既定は 10（安全側）。"""
        monkeypatch.delenv("MAX_LEVERAGE", raising=False)
        cap = calculate_leverage_cap(equity=100_000.0, current_price=100.0)
        # (100000 * 10) / (100 * 100) = 1000000 / 10000 = 100 → *100 = 10000
        assert cap == 10000
