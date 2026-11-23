"""sl_profile 決定論ロジックのユニットテスト（RD-12 #203）。"""

from __future__ import annotations

import pytest

from backend.src.llm.sl_profile import (
    ATR14_WIDE_THRESHOLD,
    compute_recommended_sl_profile,
    resolve_atr_multiplier,
    session_range_over_atr,
)


def test_recommended_standard_below_threshold() -> None:
    assert compute_recommended_sl_profile(ATR14_WIDE_THRESHOLD - 0.01) == "STANDARD"


def test_recommended_wide_at_threshold() -> None:
    assert compute_recommended_sl_profile(ATR14_WIDE_THRESHOLD) == "WIDE"


def test_session_range_ratio() -> None:
    assert session_range_over_atr(155.0, 153.0, 1.0) == 2.0


def test_session_range_ratio_degrades_when_missing() -> None:
    assert session_range_over_atr(None, 153.0, 1.0) is None


def test_resolve_atr_multiplier_mapping() -> None:
    assert resolve_atr_multiplier("standard") == 0.7
    assert resolve_atr_multiplier("wide") == 0.8


def test_resolve_atr_multiplier_rejects_unknown_profile() -> None:
    with pytest.raises(KeyError):
        resolve_atr_multiplier("STANDARD")  # type: ignore[arg-type]
