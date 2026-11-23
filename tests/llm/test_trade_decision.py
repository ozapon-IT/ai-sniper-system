"""TradeDecision のユニットテスト（付録A.1, §3.2 / RD-12）。"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from backend.src.engine.enrich import RR_TARGET_DEFAULT, RR_TARGET_MAX, RR_TARGET_MIN
from backend.src.llm.trade_decision import TradeDecision, no_entry


def _long() -> TradeDecision:
    return TradeDecision(
        direction="LONG",
        execution_required=True,
        order_type="LIMIT",
        entry_price=159.8,
        referenced_pivot_level="S1",
        rationale="押し目買い",
        sl_profile="standard",
        rr_target=1.0,
    )


class TestSlProfileMapping:
    def test_standard_maps_to_0_7(self) -> None:
        d = TradeDecision(
            direction="LONG", execution_required=False, rationale="x", sl_profile="standard"
        )
        assert d.atr_multiplier == 0.7

    def test_wide_maps_to_0_8(self) -> None:
        d = TradeDecision(
            direction="LONG", execution_required=False, rationale="x", sl_profile="wide"
        )
        assert d.atr_multiplier == 0.8

    def test_invalid_sl_profile_value_raises(self) -> None:
        with pytest.raises(ValidationError):
            TradeDecision(
                direction="LONG",
                execution_required=False,
                rationale="x",
                sl_profile="STANDARD",  # type: ignore[arg-type]
            )


class TestRrTargetClamp:
    def test_above_max_clamped_to_1_5(self) -> None:
        d = TradeDecision(
            direction="LONG",
            execution_required=False,
            rationale="x",
            sl_profile="standard",
            rr_target=3.0,
        )
        assert d.rr_target == RR_TARGET_MAX == 1.5

    def test_below_min_clamped_to_1_0(self) -> None:
        d = TradeDecision(
            direction="LONG",
            execution_required=False,
            rationale="x",
            sl_profile="standard",
            rr_target=0.5,
        )
        assert d.rr_target == RR_TARGET_MIN == 1.0

    @pytest.mark.parametrize("value", [1.0, 1.25, 1.5])
    def test_in_range_unchanged(self, value: float) -> None:
        d = TradeDecision(
            direction="LONG",
            execution_required=False,
            rationale="x",
            sl_profile="standard",
            rr_target=value,
        )
        assert d.rr_target == value


class TestRrTargetDefaultFromEnv:
    """RR_TARGET_DEFAULT env が rr_target 省略時の既定に反映される（#209 / §4.2）。"""

    def test_omitted_rr_target_uses_env(self) -> None:
        with patch.dict(os.environ, {"RR_TARGET_DEFAULT": "1.2"}, clear=False):
            d = TradeDecision(
                direction="LONG",
                execution_required=False,
                rationale="x",
                sl_profile="standard",
            )
        assert d.rr_target == 1.2

    def test_env_above_max_clamped(self) -> None:
        with patch.dict(os.environ, {"RR_TARGET_DEFAULT": "2.0"}, clear=False):
            d = TradeDecision(
                direction="LONG",
                execution_required=False,
                rationale="x",
                sl_profile="standard",
            )
        assert d.rr_target == RR_TARGET_MAX == 1.5

    def test_env_below_min_clamped(self) -> None:
        with patch.dict(os.environ, {"RR_TARGET_DEFAULT": "0.5"}, clear=False):
            d = TradeDecision(
                direction="LONG",
                execution_required=False,
                rationale="x",
                sl_profile="standard",
            )
        assert d.rr_target == RR_TARGET_MIN == 1.0

    def test_env_invalid_falls_back_to_constant(self) -> None:
        with patch.dict(os.environ, {"RR_TARGET_DEFAULT": "not-a-number"}, clear=False):
            d = TradeDecision(
                direction="LONG",
                execution_required=False,
                rationale="x",
                sl_profile="standard",
            )
        assert d.rr_target == RR_TARGET_DEFAULT == 1.0

    def test_no_entry_uses_env(self) -> None:
        with patch.dict(os.environ, {"RR_TARGET_DEFAULT": "1.2"}, clear=False):
            d = no_entry("env 既定確認")
        assert d.rr_target == 1.2


class TestNoStopLossOrLot:
    def test_dump_has_no_stop_loss(self) -> None:
        """LLM 出力に stop_loss が含まれない（付録A準拠, §3.2 / #36）。"""
        dumped = _long().model_dump()
        assert "stop_loss" not in dumped

    def test_dump_has_no_lot_fields(self) -> None:
        """ロット（v_raw/v_safe/size/lot）も LLM 出力に含まれない。"""
        dumped = _long().model_dump()
        for forbidden in ("stop_loss", "v_raw", "v_safe", "size", "lot"):
            assert forbidden not in dumped

    def test_dump_includes_derived_atr_multiplier(self) -> None:
        """model_dump には sl_profile 由来の atr_multiplier が含まれる（監査ログ用）。"""
        dumped = _long().model_dump()
        assert dumped["sl_profile"] == "standard"
        assert dumped["atr_multiplier"] == 0.7


class TestShouldEnter:
    def test_should_enter_true(self) -> None:
        assert _long().should_enter is True

    def test_no_entry_direction_blocks_entry(self) -> None:
        d = TradeDecision(
            direction="NO_ENTRY",
            execution_required=True,
            rationale="様子見",
            sl_profile="standard",
        )
        assert d.should_enter is False

    def test_execution_required_false_blocks_entry(self) -> None:
        d = TradeDecision(
            direction="LONG",
            execution_required=False,
            rationale="見送り",
            sl_profile="standard",
        )
        assert d.should_enter is False


class TestNoEntryHelper:
    def test_no_entry_is_safe(self) -> None:
        d = no_entry("テスト見送り")
        assert d.direction == "NO_ENTRY"
        assert d.execution_required is False
        assert d.should_enter is False
        assert d.rationale == "テスト見送り"
        assert d.sl_profile == "standard"
        assert d.atr_multiplier == 0.7


class TestActionableRequiresOrderFields:
    """execution_required=True かつ LONG/SHORT には発注データを必須化（安全ガード）。"""

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"order_type": "LIMIT"},
            {"entry_price": 159.8},
            {},
        ],
    )
    def test_actionable_without_order_fields_raises(self, kwargs: dict[str, object]) -> None:
        with pytest.raises(ValidationError):
            TradeDecision(
                direction="LONG",
                execution_required=True,
                rationale="不完全な発注指示",
                sl_profile="standard",
                **kwargs,  # type: ignore[arg-type]
            )

    def test_actionable_with_all_fields_ok(self) -> None:
        d = TradeDecision(
            direction="SHORT",
            execution_required=True,
            order_type="STOP",
            entry_price=160.5,
            referenced_pivot_level="R1",
            rationale="戻り売り",
            sl_profile="wide",
        )
        assert d.should_enter is True
        assert d.atr_multiplier == 0.8

    def test_no_entry_without_order_fields_ok(self) -> None:
        d = TradeDecision(
            direction="NO_ENTRY",
            execution_required=False,
            rationale="様子見",
            sl_profile="standard",
        )
        assert d.should_enter is False

    def test_long_not_executing_without_fields_ok(self) -> None:
        d = TradeDecision(
            direction="LONG",
            execution_required=False,
            rationale="検討のみ",
            sl_profile="standard",
        )
        assert d.should_enter is False

    def test_sl_profile_required(self) -> None:
        with pytest.raises(ValidationError):
            TradeDecision(direction="LONG", execution_required=False, rationale="x")
