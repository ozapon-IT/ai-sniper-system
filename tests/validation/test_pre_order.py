"""発注前バリデーション テスト（§3.4）."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from backend.src.validation.pre_order import (
    ValidationError,
    ValidationReasonCode,
    validate_pre_order,
)

# 共通の正常ケース値
# atr=1.0 → 現値乖離上限 0.3×1.0=0.3。VALID_* の乖離は 0.01 で上限内（cap は tripしない）。
VALID_LONG = {
    "side": "LONG",
    "entry_price": 150.080,
    "take_profit": 150.580,
    "stop_loss": 149.780,
    "lot": 100,
    "current_price": 150.090,
    "atr": 1.0,
}

VALID_SHORT = {
    "side": "SHORT",
    "entry_price": 150.120,
    "take_profit": 149.620,
    "stop_loss": 150.420,
    "lot": 100,
    "current_price": 150.110,
    "atr": 1.0,
}


class TestPriceOrder:
    """方向別価格序列チェック（§3.4）."""

    def test_long_valid_passes(self) -> None:
        validate_pre_order(**VALID_LONG)

    def test_short_valid_passes(self) -> None:
        validate_pre_order(**VALID_SHORT)

    def test_long_sl_ge_entry_rejected(self) -> None:
        """LONG で SL≥Entry のケースが reject される（受け入れ条件）."""
        with pytest.raises(ValidationError) as exc_info:
            validate_pre_order(
                **{**VALID_LONG, "stop_loss": 150.200}  # SL > Entry
            )
        assert exc_info.value.reason_code == ValidationReasonCode.PRICE_ORDER_VIOLATION

    def test_long_sl_equals_entry_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            validate_pre_order(
                **{**VALID_LONG, "stop_loss": 150.080}  # SL == Entry
            )
        assert exc_info.value.reason_code == ValidationReasonCode.PRICE_ORDER_VIOLATION

    def test_long_tp_le_entry_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            validate_pre_order(
                **{**VALID_LONG, "take_profit": 149.900}  # TP < Entry
            )
        assert exc_info.value.reason_code == ValidationReasonCode.PRICE_ORDER_VIOLATION

    def test_short_sl_le_entry_rejected(self) -> None:
        """SHORT で SL≤Entry のケースが reject される."""
        with pytest.raises(ValidationError) as exc_info:
            validate_pre_order(
                **{**VALID_SHORT, "stop_loss": 149.900}  # SL < Entry
            )
        assert exc_info.value.reason_code == ValidationReasonCode.PRICE_ORDER_VIOLATION

    def test_short_tp_ge_entry_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            validate_pre_order(
                **{**VALID_SHORT, "take_profit": 150.500}  # TP > Entry
            )
        assert exc_info.value.reason_code == ValidationReasonCode.PRICE_ORDER_VIOLATION

    def test_invalid_side_rejected(self) -> None:
        """不正な side 値（BUY 等の typo）は即 reject される."""
        with pytest.raises(ValidationError) as exc_info:
            validate_pre_order(**{**VALID_LONG, "side": "BUY"})  # type: ignore[arg-type]
        assert exc_info.value.reason_code == ValidationReasonCode.PRICE_ORDER_VIOLATION


class TestRRRatio:
    """R:R 下限チェック（§3.4）."""

    def test_rr_05_with_min_rr_10_rejected(self) -> None:
        """R:R=0.5（MIN_RR=1.0）のケースが reject される（受け入れ条件）."""
        with pytest.raises(ValidationError) as exc_info:
            validate_pre_order(
                side="LONG",
                entry_price=150.080,
                take_profit=150.330,  # reward=0.250
                stop_loss=149.580,  # risk=0.500 → R:R=0.5
                lot=100,
                current_price=150.090,
                atr=1.0,
                min_rr_ratio=1.0,
            )
        assert exc_info.value.reason_code == ValidationReasonCode.RR_RATIO_BELOW_MINIMUM

    def test_rr_exactly_10_passes(self) -> None:
        """R:R=1.0（MIN_RR=1.0）は通過する."""
        validate_pre_order(
            side="LONG",
            entry_price=150.080,
            take_profit=150.380,  # reward=0.300
            stop_loss=149.780,  # risk=0.300 → R:R=1.0
            lot=100,
            current_price=150.090,
            atr=1.0,
            min_rr_ratio=1.0,
        )

    def test_rr_15_passes(self) -> None:
        validate_pre_order(**VALID_LONG)  # reward=0.500, risk=0.300 → R:R≈1.67

    def test_rr_from_env_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """MIN_RR_RATIO 環境変数が反映される."""
        monkeypatch.setenv("MIN_RR_RATIO", "2.0")
        with pytest.raises(ValidationError) as exc_info:
            validate_pre_order(**VALID_LONG)  # R:R≈1.67 < 2.0
        assert exc_info.value.reason_code == ValidationReasonCode.RR_RATIO_BELOW_MINIMUM


class TestTickSize:
    """呼値刻みチェック（§3.4）."""

    def test_entry_price_not_tick_aligned_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            validate_pre_order(**{**VALID_LONG, "entry_price": 150.0801})
        assert exc_info.value.reason_code == ValidationReasonCode.TICK_SIZE_VIOLATION

    def test_take_profit_not_tick_aligned_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            validate_pre_order(**{**VALID_LONG, "take_profit": 150.5804})
        assert exc_info.value.reason_code == ValidationReasonCode.TICK_SIZE_VIOLATION

    def test_stop_loss_not_tick_aligned_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            validate_pre_order(**{**VALID_LONG, "stop_loss": 149.8005})
        assert exc_info.value.reason_code == ValidationReasonCode.TICK_SIZE_VIOLATION

    def test_all_tick_aligned_passes(self) -> None:
        validate_pre_order(**VALID_LONG)


class TestMinEntryDistance:
    """現値からの最小乖離チェック（§3.4）."""

    def test_entry_too_close_to_current_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            validate_pre_order(
                **{**VALID_LONG, "current_price": 150.081}  # distance=0.001 < 0.003
            )
        assert exc_info.value.reason_code == ValidationReasonCode.MIN_ENTRY_DISTANCE_VIOLATION

    def test_entry_exactly_at_min_distance_passes(self) -> None:
        validate_pre_order(
            **{**VALID_LONG, "current_price": 150.083}  # distance=0.003 == min
        )

    def test_entry_above_min_distance_passes(self) -> None:
        validate_pre_order(**VALID_LONG)  # distance=0.010 > 0.003


class TestMaxEntryDistance:
    """現値からの乖離上限チェック（§3.4・Ver 3.6）."""

    def test_entry_too_far_from_current_rejected(self) -> None:
        # atr=0.02 → 上限=0.3×0.02=0.006。乖離 0.01 > 0.006 → 棄却
        with pytest.raises(ValidationError) as exc_info:
            validate_pre_order(**{**VALID_LONG, "atr": 0.02})
        assert exc_info.value.reason_code == ValidationReasonCode.MAX_ENTRY_DISTANCE_VIOLATION

    def test_entry_within_cap_passes(self) -> None:
        # atr=1.0 → 上限=0.3。乖離 0.01 <= 0.3 → 通過
        validate_pre_order(**VALID_LONG)

    def test_atr_is_required_fail_loud(self) -> None:
        """atr 未指定は TypeError（安全ガードの無音スキップ防止・CodeRabbit #179）."""
        kwargs = {k: v for k, v in VALID_LONG.items() if k != "atr"}
        with pytest.raises(TypeError):
            validate_pre_order(**kwargs)  # type: ignore[arg-type]


class TestLotValidation:
    """ロットバリデーション（§3.4）."""

    def test_lot_below_100_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            validate_pre_order(**{**VALID_LONG, "lot": 50})
        assert exc_info.value.reason_code == ValidationReasonCode.LOT_BELOW_MINIMUM

    def test_lot_not_multiple_of_100_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            validate_pre_order(**{**VALID_LONG, "lot": 150})
        assert exc_info.value.reason_code == ValidationReasonCode.LOT_NOT_MULTIPLE_OF_100

    def test_lot_100_passes(self) -> None:
        validate_pre_order(**VALID_LONG)

    def test_lot_1000_passes(self) -> None:
        validate_pre_order(**{**VALID_LONG, "lot": 1000})


class TestOrderTypeEntryPosition:
    """order_type と entry 価格位置の整合（§3.4 / 付録 A.2 発注フロー 3）."""

    def test_long_limit_above_current_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            validate_pre_order(
                **{**VALID_LONG, "entry_price": 150.100, "current_price": 150.090}
            )
        assert (
            exc_info.value.reason_code
            == ValidationReasonCode.ORDER_TYPE_ENTRY_POSITION_VIOLATION
        )

    def test_long_stop_below_current_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            validate_pre_order(
                order_type="STOP",
                **VALID_LONG,
            )
        assert (
            exc_info.value.reason_code
            == ValidationReasonCode.ORDER_TYPE_ENTRY_POSITION_VIOLATION
        )

    def test_long_stop_above_current_passes(self) -> None:
        validate_pre_order(
            side="LONG",
            order_type="STOP",
            entry_price=150.100,
            take_profit=150.600,
            stop_loss=149.800,
            lot=100,
            current_price=150.090,
            atr=1.0,
        )

    def test_short_limit_below_current_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            validate_pre_order(
                **{**VALID_SHORT, "entry_price": 150.100, "current_price": 150.110}
            )
        assert (
            exc_info.value.reason_code
            == ValidationReasonCode.ORDER_TYPE_ENTRY_POSITION_VIOLATION
        )

    def test_short_stop_above_current_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            validate_pre_order(order_type="STOP", **VALID_SHORT)
        assert (
            exc_info.value.reason_code
            == ValidationReasonCode.ORDER_TYPE_ENTRY_POSITION_VIOLATION
        )

    def test_short_stop_below_current_passes(self) -> None:
        validate_pre_order(
            side="SHORT",
            order_type="STOP",
            entry_price=150.100,
            take_profit=149.600,
            stop_loss=150.400,
            lot=100,
            current_price=150.110,
            atr=1.0,
        )

    def test_unsupported_order_type_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            validate_pre_order(
                **VALID_LONG,
                order_type="MARKET",  # type: ignore[arg-type]
            )
        assert (
            exc_info.value.reason_code
            == ValidationReasonCode.ORDER_TYPE_ENTRY_POSITION_VIOLATION
        )


class TestIntegrationWithIfoOrder:
    """バリデーション通過後のみ ifoOrder が呼ばれる（統合テスト）."""

    def test_ifo_order_called_after_validation_passes(self) -> None:
        mock_send_order = MagicMock(return_value={"status": 0, "data": "order123"})

        def pipeline(order_fn: MagicMock) -> dict:
            validate_pre_order(**VALID_LONG)
            return order_fn()

        result = pipeline(mock_send_order)
        mock_send_order.assert_called_once()
        assert result["data"] == "order123"

    def test_ifo_order_not_called_when_validation_fails(self) -> None:
        mock_send_order = MagicMock()

        def pipeline(order_fn: MagicMock) -> None:
            try:
                validate_pre_order(
                    **{**VALID_LONG, "stop_loss": 150.200}  # SL > Entry → violation
                )
            except ValidationError:
                return
            order_fn()

        pipeline(mock_send_order)
        mock_send_order.assert_not_called()


class TestLoggingOnViolation:
    """違反理由が実行ログ JSON に記録される（受け入れ条件）."""

    def test_violation_logged_as_json(self) -> None:
        with patch("backend.src.validation.pre_order.logger") as mock_logger:
            with pytest.raises(ValidationError):
                validate_pre_order(**{**VALID_LONG, "stop_loss": 150.200})
            mock_logger.warning.assert_called_once()
            log_arg = mock_logger.warning.call_args[0][0]
            assert log_arg["event"] == "pre_order_validation_failed"
            assert log_arg["reason_code"] == ValidationReasonCode.PRICE_ORDER_VIOLATION.value
            assert "detail" in log_arg

    def test_passed_logged_as_json(self) -> None:
        with patch("backend.src.validation.pre_order.logger") as mock_logger:
            validate_pre_order(**VALID_LONG)
            mock_logger.info.assert_called_once()
            log_arg = mock_logger.info.call_args[0][0]
            assert log_arg["event"] == "pre_order_validation_passed"
            assert "rr_ratio" in log_arg
