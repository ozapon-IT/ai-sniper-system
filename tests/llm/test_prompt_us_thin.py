"""us_thin_market フラグが build_user_prompt に反映されることのテスト（Issue #46 / §3.5）。"""

from __future__ import annotations

from backend.src.llm.prompt import PromptContext, build_user_prompt


def _base_ctx(**kwargs: object) -> PromptContext:
    return PromptContext(
        current_time_jst="07:30",
        day_of_week="月",
        symbol="USD_JPY",
        current_price_mid=155.0,
        pivot_p=155.0,
        pivot_r1=155.5,
        pivot_r2=156.0,
        pivot_s1=154.5,
        pivot_s2=154.0,
        atr_14d=0.4,
        prev_day_change=0.1,
        equity_jpy=100_000.0,
        **kwargs,  # type: ignore[arg-type]
    )


def test_us_thin_market_warning_present_when_flag_true() -> None:
    """us_thin_market=True のとき薄商い注意文がプロンプトに含まれる。"""
    prompt = build_user_prompt(_base_ctx(us_thin_market=True))
    assert "US 祝日（薄商い）" in prompt
    assert "NO_ENTRY を優先" in prompt


def test_us_thin_market_warning_absent_when_flag_false() -> None:
    """us_thin_market=False（デフォルト）のとき薄商い注意文がプロンプトに含まれない。"""
    prompt = build_user_prompt(_base_ctx(us_thin_market=False))
    assert "US 祝日" not in prompt


def test_us_thin_market_default_is_false() -> None:
    """us_thin_market フィールドのデフォルトは False。"""
    ctx = _base_ctx()
    assert ctx.us_thin_market is False
