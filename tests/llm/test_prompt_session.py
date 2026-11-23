"""RD-12 #203: sl_profile・ボラシグナル・プロンプト改訂テスト。"""

from __future__ import annotations

import re

from backend.src.llm.prompt import SYSTEM_PROMPT, PromptContext, build_user_prompt
from backend.src.llm.sl_profile import ATR14_WIDE_THRESHOLD


def _base_ctx(**kwargs: object) -> PromptContext:
    defaults: dict[str, object] = {
        "current_time_jst": "20:30",
        "day_of_week": "火",
        "symbol": "USD_JPY",
        "current_price_mid": 155.0,
        "pivot_p": 155.0,
        "pivot_r1": 155.5,
        "pivot_r2": 156.0,
        "pivot_s1": 154.5,
        "pivot_s2": 154.0,
        "atr_14d": 0.4,
        "prev_day_change": 0.1,
        "equity_jpy": 100_000.0,
    }
    defaults.update(kwargs)
    return PromptContext(**defaults)  # type: ignore[arg-type]


class TestSystemPromptDisclosure:
    def test_discloses_sl_and_tp_formula(self) -> None:
        assert "ATR_h" in SYSTEM_PROMPT
        assert "stop_loss  = entry ∓ atr_multiplier × ATR_h" in SYSTEM_PROMPT
        assert "take_profit = entry ± rr_target × |entry − stop_loss|" in SYSTEM_PROMPT

    def test_discloses_min_rr_constraint(self) -> None:
        assert "MIN_RR" in SYSTEM_PROMPT

    def test_output_fields_use_sl_profile_not_atr_multiplier(self) -> None:
        assert "order_type / entry_price / sl_profile / rr_target / execution_required" in (
            SYSTEM_PROMPT
        )
        assert "atr_multiplier / execution_required" not in SYSTEM_PROMPT
        assert "利確価格(take_profit)・ロット数は **出力しない**" in SYSTEM_PROMPT

    def test_sl_profile_mapping_disclosed(self) -> None:
        assert "standard → atr_multiplier 0.7" in SYSTEM_PROMPT
        assert "wide → atr_multiplier 0.8" in SYSTEM_PROMPT

    def test_rr_target_range_and_conservative_guidance(self) -> None:
        assert "rr_target" in SYSTEM_PROMPT
        assert "1.0〜1.5" in SYSTEM_PROMPT
        assert "既定は 1.0" in SYSTEM_PROMPT
        assert "明確な方向根拠があるときのみ 1.2" in SYSTEM_PROMPT

    def test_entry_anchor_guidance(self) -> None:
        assert "0.1×ATR" in SYSTEM_PROMPT
        assert "LIMIT" in SYSTEM_PROMPT
        assert "STOP" in SYSTEM_PROMPT
        assert "0.3×ATR" in SYSTEM_PROMPT

    def test_night_session_framing(self) -> None:
        assert "当日セッション情報" in SYSTEM_PROMPT

    def test_hold_window_timing_structure(self) -> None:
        assert "21:30（冬22:30）" in SYSTEM_PROMPT
        assert "NYカット" in SYSTEM_PROMPT
        assert "ロンドンフィキシング" in SYSTEM_PROMPT
        assert "DST" in SYSTEM_PROMPT

    def test_rd11_session_context_is_reference_only(self) -> None:
        assert "方向判断の参考情報" in SYSTEM_PROMPT
        assert "レンジ内であること自体は見送り理由にしない" in SYSTEM_PROMPT
        assert "hard rule" in SYSTEM_PROMPT

    def test_rd11_no_entry_requires_active_reason(self) -> None:
        assert "積極的理由" in SYSTEM_PROMPT
        assert "方向根拠の矛盾" in SYSTEM_PROMPT

    def test_rd12_no_anchor_phrase(self) -> None:
        """RD-12: 「変える必要はない」アンカー文言を除去。"""
        assert "変える必要はない" not in SYSTEM_PROMPT

    def test_rd12_vol_signal_reference(self) -> None:
        assert "ボラティリティシグナル" in SYSTEM_PROMPT


class TestVolatilitySignalBlock:
    def test_signal_block_present(self) -> None:
        prompt = build_user_prompt(_base_ctx())
        assert "ボラティリティシグナル" in prompt
        assert "推奨 sl_profile: STANDARD" in prompt

    def test_wide_recommendation_when_atr_high(self) -> None:
        prompt = build_user_prompt(_base_ctx(atr_14d=ATR14_WIDE_THRESHOLD))
        assert "推奨 sl_profile: WIDE" in prompt

    def test_session_range_ratio_when_high_low_available(self) -> None:
        prompt = build_user_prompt(
            _base_ctx(session_open=154.0, session_high=155.2, session_low=153.8)
        )
        assert "当日レンジ幅/ATR14=" in prompt

    def test_no_absolute_date_in_vol_block(self) -> None:
        prompt = build_user_prompt(
            _base_ctx(session_open=154.0, session_high=155.2, session_low=153.8)
        )
        assert not re.search(r"\d{4}-\d{2}-\d{2}", prompt)
        assert not re.search(r"20\d{2}年", prompt)


class TestSessionRendering:
    def test_session_block_present_with_full_stats(self) -> None:
        prompt = build_user_prompt(
            _base_ctx(session_open=154.0, session_high=155.2, session_low=153.8)
        )
        assert "当日セッション(06:00〜, 東京+ロンドン序盤)" in prompt
        assert "始値=154.0" in prompt

    def test_session_block_absent_when_open_missing(self) -> None:
        prompt = build_user_prompt(_base_ctx())
        assert "当日セッション" not in prompt

    def test_split_block_with_break_up(self) -> None:
        prompt = build_user_prompt(
            _base_ctx(
                session_open=154.0,
                tokyo_open=154.0,
                tokyo_high=155.0,
                tokyo_low=153.8,
                europe_open=155.1,
                europe_high=156.0,
                europe_low=154.5,
            )
        )
        assert "東京レンジブレイク: 上ブレイク" in prompt
