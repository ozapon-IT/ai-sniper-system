"""モック LLM のユニットテスト（§7.1, §5.2）。"""

from __future__ import annotations

import pytest

from backend.src.llm.mock_llm import generate_decision_mock
from backend.src.llm.prompt import PromptContext


def _ctx() -> PromptContext:
    return PromptContext(
        current_time_jst="2026-06-29T07:30:00",
        day_of_week="Monday",
        symbol="USD_JPY",
        current_price_mid=159.812,
        pivot_p=159.9,
        pivot_r1=160.45,
        pivot_r2=160.8,
        pivot_s1=159.18,
        pivot_s2=158.6,
        atr_14d=0.42,
        prev_day_change=0.65,
        equity_jpy=100_000.0,
    )


def test_mock_returns_valid_trade_decision() -> None:
    result = generate_decision_mock(_ctx())
    d = result.decision
    assert d.direction == "LONG"
    assert d.should_enter is True
    # 現値近傍アンカー（現値 159.812 から 0.05×ATR 下の押し目・§3.4 乖離上限内・Ver 3.6）
    assert d.entry_price == pytest.approx(159.812 - 0.05 * 0.42)
    # SL/TP は Python 決定論算出のためスキーマに存在しない（§3.2）
    assert "stop_loss" not in d.model_dump()
    assert "take_profit" not in d.model_dump()


def test_mock_token_usage_is_positive() -> None:
    """モックでもトークン消費数が 0 より大きい値として記録される（§5.2 受け入れ条件）。"""
    result = generate_decision_mock(_ctx())
    assert result.input_tokens > 0
    assert result.output_tokens > 0


def test_mock_atr_multiplier_in_range() -> None:
    result = generate_decision_mock(_ctx())
    assert 0.6 <= result.decision.atr_multiplier <= 1.0
    assert 1.0 <= result.decision.rr_target <= 1.5
