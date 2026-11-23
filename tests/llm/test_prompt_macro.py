"""マクロ文脈が build_user_prompt に正しく描画されることのテスト（Issue #107 / §3.3）。"""

from __future__ import annotations

from backend.src.adapters.macro_context import MacroIndicator, MacroResult
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


def _full_macro() -> MacroResult:
    return MacroResult(
        indicators=[
            MacroIndicator(label="US10Y", latest_value=4.25, overnight_change_pct=0.03),
            MacroIndicator(label="S&P先物", latest_value=5200.0, overnight_change_pct=-0.45),
            MacroIndicator(label="日経先物", latest_value=38000.0, overnight_change_pct=0.20),
        ],
        source_ok=True,
    )


def test_macro_line_present_when_context_provided() -> None:
    """macro_context に指標があるとき、プロンプトにマクロ行が含まれる。"""
    prompt = build_user_prompt(_base_ctx(macro_context=_full_macro()))
    assert "オーバーナイト米国:" in prompt
    assert "US10Y=4.25%(+0.03%)" in prompt
    assert "S&P先物=5200.00(-0.45%)" in prompt
    assert "日経先物=38000.00(+0.20%)" in prompt


def test_macro_line_absent_when_context_is_none() -> None:
    """macro_context=None（デフォルト）のとき、マクロ行はプロンプトに含まれない。"""
    prompt = build_user_prompt(_base_ctx())
    assert "オーバーナイト米国:" not in prompt


def test_macro_line_absent_when_source_failed() -> None:
    """source_ok=False（全指標取得失敗）のとき、マクロ行はプロンプトに含まれない。"""
    failed = MacroResult(indicators=[], source_ok=False)
    prompt = build_user_prompt(_base_ctx(macro_context=failed))
    assert "オーバーナイト米国:" not in prompt


def test_macro_line_partial_indicators() -> None:
    """一部指標のみ取得できた場合、取得できた指標のみ描画される。"""
    partial = MacroResult(
        indicators=[
            MacroIndicator(label="US10Y", latest_value=4.30, overnight_change_pct=-0.02),
        ],
        source_ok=True,
    )
    prompt = build_user_prompt(_base_ctx(macro_context=partial))
    assert "US10Y=4.30%(-0.02%)" in prompt
    assert "S&P先物" not in prompt
    assert "日経先物" not in prompt


def test_macro_context_default_is_none() -> None:
    """macro_context フィールドのデフォルトは None。"""
    ctx = _base_ctx()
    assert ctx.macro_context is None
