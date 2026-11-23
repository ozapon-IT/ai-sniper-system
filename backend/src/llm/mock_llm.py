"""フェーズ1モック LLM（§7.1）。

実 API を叩かず、決定論的に妥当な TradeDecision を返す。
判断バッチのモック稼働（1週間連続エラーなし）やテストで使用する。
トークン消費数は税務証跡（§5.2）の経路を壊さないようダミー値（>0）を返す。
"""

from __future__ import annotations

from backend.src.engine.enrich import resolve_rr_target_default
from backend.src.llm.sl_profile import SlProfile, compute_recommended_sl_profile

from .client import LlmResult
from .prompt import PromptContext
from .trade_decision import TradeDecision

# モックのダミートークン消費数（>0。§5.2 の記録経路の検証用）
_MOCK_INPUT_TOKENS = 320
_MOCK_OUTPUT_TOKENS = 48

# 現値近傍アンカーのオフセット（§3.4 現値乖離上限 0.3×ATR14 の内側に収める・Ver 3.6）
_MOCK_ENTRY_OFFSET_ATR = 0.05


def generate_decision_mock(context: PromptContext) -> LlmResult:
    """現値近傍の押し目買い LONG 判断を決定論的に返す（モック・Ver 3.6）。

    entry を現値から 0.05×ATR14 下（押し目 LIMIT）に置き、§3.4 の現値乖離上限
    （0.3×ATR14）の内側に収める。SL/TP は Python が決定論算出する（LLM は出さない）。
    """
    # 入力（経済指標の有無）と矛盾しないよう sentiment を切り替える
    sentiment_summary = (
        "モック: 本日は高インパクト指標あり"
        if context.economic_events
        else "モック: 特筆すべき高インパクト指標なし"
    )
    offset = _MOCK_ENTRY_OFFSET_ATR * context.atr_14d
    recommended = compute_recommended_sl_profile(context.atr_14d)
    sl_profile: SlProfile = "wide" if recommended == "WIDE" else "standard"
    decision = TradeDecision(
        direction="LONG",
        execution_required=True,
        order_type="LIMIT",
        entry_price=context.current_price_mid - offset,
        referenced_pivot_level="NONE",
        rationale="モック判断: 現値近傍の押し目買い（固定ロジック・Ver 3.6）。",
        sentiment_summary=sentiment_summary,
        sl_profile=sl_profile,
        rr_target=resolve_rr_target_default(),
    )
    return LlmResult(
        decision=decision,
        input_tokens=_MOCK_INPUT_TOKENS,
        output_tokens=_MOCK_OUTPUT_TOKENS,
        model="mock",
    )
