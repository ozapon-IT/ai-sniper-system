"""RD-8 エッジガード day-skip ユニットテスト（Issue #194）。"""

from __future__ import annotations

import logging
import os
from unittest.mock import patch

import pytest

from backend.src.validation.day_skip_guards import evaluate_edge_guard_day_skip


def _evaluate(**kwargs: float | None) -> bool:
    defaults = {
        "current_price_mid": 156.0,
        "spread_pips": 0.1,
        "atr_14d": 1.0,  # 100 pips
        "session_open": 155.0,
    }
    defaults.update(kwargs)
    return evaluate_edge_guard_day_skip(**defaults).skip  # type: ignore[arg-type]


class TestEdgeGuardG1WideSpread:
    """既定閾値は RD-17（#219）で 0.5 → 1.0 に recalibrate 済み。"""

    def test_just_below_threshold_passes(self) -> None:
        assert not _evaluate(spread_pips=0.99)

    def test_at_threshold_skips(self) -> None:
        assert _evaluate(spread_pips=1.0)

    def test_above_threshold_skips(self) -> None:
        assert _evaluate(spread_pips=1.01)

    def test_gmo_normal_spread_passes(self) -> None:
        # #219 の受け入れ本体: GMO 通常スプレッド 0.5 pips（2026-07 実測）は pass する
        assert not _evaluate(spread_pips=0.5)

    def test_legacy_wide_band_skips(self) -> None:
        # 本来の番人対象: 6:30〜8:00 のワイド帯 ≈12.8 pips は引き続き skip
        assert _evaluate(spread_pips=12.8)


class TestEdgeGuardG1FloatNoiseDeterminism:
    """#219 ①: 価格引き算の float ノイズで真値 0.5 が境界の上下に揺れても判定が決定的であること。

    2026-07-07/08 実測値: skip 側 0.5000000000023874 / pass 側 0.49999999999954525。
    0.01 pip 精度への丸めにより両者は同一値 0.5 として扱われる。
    """

    NOISY_ABOVE = 0.5000000000023874
    NOISY_BELOW = 0.49999999999954525

    @patch.dict(os.environ, {"MAX_DECISION_SPREAD_PIPS": "0.5"}, clear=False)
    def test_noisy_values_get_identical_verdict_at_legacy_threshold(self) -> None:
        # 旧閾値 0.5 では真値 0.5 は >= により決定的に skip（非決定性の解消が本質）
        assert _evaluate(spread_pips=self.NOISY_ABOVE)
        assert _evaluate(spread_pips=self.NOISY_BELOW)

    def test_noisy_values_get_identical_verdict_at_default_threshold(self) -> None:
        # 新既定 1.0 では真値 0.5 は決定的に pass
        assert not _evaluate(spread_pips=self.NOISY_ABOVE)
        assert not _evaluate(spread_pips=self.NOISY_BELOW)

    @patch.dict(os.environ, {"MAX_DECISION_SPREAD_PIPS": "0.5"}, clear=False)
    def test_details_log_rounded_spread(self) -> None:
        # S3 / 構造化ログに float ノイズを残さない
        result = evaluate_edge_guard_day_skip(
            current_price_mid=156.0,
            spread_pips=self.NOISY_ABOVE,
            atr_14d=1.0,
            session_open=155.0,
        )
        assert result.details["spread_pips"] == 0.5


class TestEdgeGuardG2HighVolRegime:
    def test_just_below_threshold_passes(self) -> None:
        assert not _evaluate(atr_14d=1.59)  # 159 pips

    def test_at_threshold_skips(self) -> None:
        assert _evaluate(atr_14d=1.60)  # 160 pips

    def test_above_threshold_skips(self) -> None:
        assert _evaluate(atr_14d=1.61)


class TestEdgeGuardG3NoMomentum:
    def test_just_below_threshold_skips(self) -> None:
        # |156.0 - 155.96| = 0.04 < 0.05 * 1.0
        assert _evaluate(current_price_mid=156.0, session_open=155.96, atr_14d=1.0)

    def test_at_threshold_passes(self) -> None:
        # |156.0 - 155.95| = 0.05 == 0.05 * 1.0 → not <
        assert not _evaluate(current_price_mid=156.0, session_open=155.95, atr_14d=1.0)

    def test_above_threshold_passes(self) -> None:
        assert not _evaluate(current_price_mid=156.0, session_open=155.0, atr_14d=1.0)

    def test_missing_session_open_passes_g3(self) -> None:
        assert not _evaluate(session_open=None)


class TestEdgeGuardEvaluationOrder:
    def test_g1_wins_over_g2_and_g3(self) -> None:
        result = evaluate_edge_guard_day_skip(
            current_price_mid=156.0,
            spread_pips=1.0,
            atr_14d=2.0,
            session_open=156.0,
        )
        assert result.skip
        assert result.reason == "wide_spread_day_skip"

    def test_g2_wins_when_g1_passes(self) -> None:
        result = evaluate_edge_guard_day_skip(
            current_price_mid=156.0,
            spread_pips=0.1,
            atr_14d=1.60,
            session_open=156.0,
        )
        assert result.skip
        assert result.reason == "high_vol_regime_day_skip"


class TestEdgeGuardDetails:
    def test_skip_includes_measured_values(self) -> None:
        result = evaluate_edge_guard_day_skip(
            current_price_mid=156.0,
            spread_pips=1.2,
            atr_14d=1.0,
            session_open=155.0,
        )
        assert result.reason == "wide_spread_day_skip"
        assert result.details["spread_pips"] == pytest.approx(1.2)
        assert result.details["threshold_pips"] == pytest.approx(1.0)

    @patch.dict(os.environ, {"MAX_DECISION_SPREAD_PIPS": "0.8"}, clear=False)
    def test_env_override(self) -> None:
        assert not _evaluate(spread_pips=0.7)
        assert _evaluate(spread_pips=0.8)

    @patch.dict(os.environ, {"MAX_DECISION_SPREAD_PIPS": "not-a-number"}, clear=False)
    def test_invalid_env_falls_back_to_default(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="backend.src.validation.day_skip_guards"):
            assert not _evaluate(spread_pips=0.99)
            assert _evaluate(spread_pips=1.0)
        warn_records = [r for r in caplog.records if r.getMessage() == "edge_guard_env_invalid"]
        assert warn_records
        assert all(r.env_name == "MAX_DECISION_SPREAD_PIPS" for r in warn_records)
        assert all(r.raw == "not-a-number" for r in warn_records)
        assert all(r.fallback == "1.0" for r in warn_records)
