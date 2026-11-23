"""tests/llm/test_trade_note.py — TradeNote 生成テスト（E14, §6.2）。"""

from __future__ import annotations

import datetime
import re
from typing import Any
from unittest.mock import MagicMock, patch

import httpx

from backend.src.engine.trading_day import JST
from backend.src.llm.trade_note import (
    CancelFirstSnapshot,
    Execution,
    SkipIndicator,
    TradeNoteInput,
    TradeNoteResult,
    TradePlan,
    _build_note_prompt,
    _build_plan_section,
    _plan_row,
    build_authoritative_weekly_section,
    find_cross_day_value_leaks,
    find_discipline_violations,
    generate_trade_note,
)

# ---------------------------------------------------------------------------
# フェイク LLM クライアント
# ---------------------------------------------------------------------------

def _make_llm_client(
    text: str = "## 狙撃プラン\ntest\n## 約定結果\ntest\n## 自己評価\ntest",
    *,
    stop_reason: object = "end_turn",
) -> Any:
    usage = MagicMock()
    usage.input_tokens = 100
    usage.output_tokens = 200

    content_block = MagicMock()
    content_block.text = text

    response = MagicMock()
    response.content = [content_block]
    response.usage = usage
    # 実 SDK は非ストリーミング応答に必ず stop_reason を入れる。MagicMock の
    # 自動属性に任せると「未知の値」が素通りしてしまうため明示する（#309）。
    response.stop_reason = stop_reason

    client = MagicMock()
    client.messages.create.return_value = response
    return client


# ---------------------------------------------------------------------------
# 平日ノート生成
# ---------------------------------------------------------------------------

class TestGenerateTradeNoteWeekday:
    def test_returns_markdown_with_required_sections(self) -> None:
        now = datetime.datetime(2026, 6, 25, 4, 35, 0, tzinfo=JST)  # 水曜 AM 4:35
        executions = [
            Execution(
                execution_id="ex001",
                order_id="ord001",
                symbol="USD_JPY",
                side="BUY",
                price="145.00",
                size="1000",
                timestamp="2026-06-24T09:30:00",
            )
        ]
        inp = TradeNoteInput(
            decision_plan="USD/JPY ロング狙い。R1=145.50 でエントリー予定。",
            executions=executions,
            now_jst=now,
            is_friday_mode=False,
        )
        client = _make_llm_client()
        result = generate_trade_note(inp, llm_client=client)

        assert isinstance(result, TradeNoteResult)
        assert "狙撃プラン" in result.note_markdown
        assert "約定結果" in result.note_markdown
        assert "自己評価" in result.note_markdown
        assert result.input_tokens == 100
        assert result.output_tokens == 200
        assert result.is_complete is True

    def test_no_auto_parameter_change_in_prompt(self) -> None:
        """LLM へ送るプロンプトに自動パラメータ変更の指示が含まれないこと（§6.2）。"""
        now = datetime.datetime(2026, 6, 25, 4, 35, 0, tzinfo=JST)
        inp = TradeNoteInput(
            decision_plan="見送り",
            executions=[],
            now_jst=now,
        )
        client = _make_llm_client()
        generate_trade_note(inp, llm_client=client)

        call_args = client.messages.create.call_args
        system_text = call_args.kwargs.get("system", "")
        assert "自動変更提案は絶対に行わない" in system_text

    def test_llm_failure_returns_fallback_with_incomplete(self) -> None:
        now = datetime.datetime(2026, 6, 25, 4, 35, 0, tzinfo=JST)
        inp = TradeNoteInput(decision_plan="test", executions=[], now_jst=now)

        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("API timeout")

        result = generate_trade_note(inp, llm_client=client)
        assert result.note_markdown  # fallback が返る
        assert result.input_tokens == 0
        assert result.output_tokens == 0
        assert result.is_complete is False


# ---------------------------------------------------------------------------
# 土曜（friday モード）ノート生成
# ---------------------------------------------------------------------------

class TestGenerateTradeNoteFridayMode:
    def test_weekly_summary_section_included(self) -> None:
        now = datetime.datetime(2026, 6, 27, 4, 35, 0, tzinfo=JST)  # 土曜 AM 4:35

        note_text = (
            "## 狙撃プラン\ntest\n"
            "## 約定結果\ntest\n"
            "## 自己評価\ntest\n"
            "## 週次総括\n今週の振り返り。"
        )
        client = _make_llm_client(note_text)
        inp = TradeNoteInput(
            decision_plan="週次モード",
            executions=[],
            now_jst=now,
            is_friday_mode=True,
        )
        result = generate_trade_note(inp, llm_client=client)
        assert "週次総括" in result.note_markdown

    def test_friday_mode_prompt_contains_weekly_instruction(self) -> None:
        now = datetime.datetime(2026, 6, 27, 4, 35, 0, tzinfo=JST)
        inp = TradeNoteInput(
            decision_plan="週次",
            executions=[],
            now_jst=now,
            is_friday_mode=True,
        )
        client = _make_llm_client()
        generate_trade_note(inp, llm_client=client)

        call_args = client.messages.create.call_args
        messages = call_args.kwargs.get("messages", [])
        user_content = messages[0]["content"] if messages else ""
        assert "週次総括" in user_content

    def test_weekday_mode_no_weekly_section_in_prompt(self) -> None:
        now = datetime.datetime(2026, 6, 25, 4, 35, 0, tzinfo=JST)
        inp = TradeNoteInput(
            decision_plan="平日",
            executions=[],
            now_jst=now,
            is_friday_mode=False,
        )
        client = _make_llm_client()
        generate_trade_note(inp, llm_client=client)

        call_args = client.messages.create.call_args
        messages = call_args.kwargs.get("messages", [])
        user_content = messages[0]["content"] if messages else ""
        assert "週次総括" not in user_content


# ---------------------------------------------------------------------------
# #309: max_tokens 打ち切りの検知 と モード別の出力上限
# ---------------------------------------------------------------------------

class TestStopReasonDetection:
    """HTTP 200 + stop_reason で返る打ち切り・refusal を検知すること（#309）。"""

    def _run(self, stop_reason: object, *, is_friday_mode: bool = False) -> TradeNoteResult:
        now = datetime.datetime(2026, 6, 25, 4, 35, 0, tzinfo=JST)
        inp = TradeNoteInput(
            decision_plan="test",
            executions=[],
            now_jst=now,
            is_friday_mode=is_friday_mode,
        )
        return generate_trade_note(inp, llm_client=_make_llm_client(stop_reason=stop_reason))

    def test_end_turn_is_complete(self) -> None:
        assert self._run("end_turn").is_complete is True

    def test_stop_sequence_is_complete(self) -> None:
        assert self._run("stop_sequence").is_complete is True

    def test_max_tokens_marks_incomplete(self) -> None:
        """2026-08-15 の週次ノートが踏んだ経路。例外は飛ばず HTTP 200 で返る。"""
        result = self._run("max_tokens", is_friday_mode=True)
        assert result.is_complete is False
        # 本文自体は保存する（途中まででも証跡として残す）
        assert result.note_markdown

    def test_refusal_marks_incomplete(self) -> None:
        assert self._run("refusal").is_complete is False

    def test_unknown_stop_reason_marks_incomplete(self) -> None:
        """未知の値は fail-safe で不完全に倒すこと。"""
        assert self._run("some_future_reason").is_complete is False

    def test_missing_stop_reason_marks_incomplete(self) -> None:
        """欠落（None）も不完全に倒すこと。"""
        assert self._run(None).is_complete is False

    def test_non_string_stop_reason_marks_incomplete(self) -> None:
        """MagicMock 等の非文字列が素通りしないこと。"""
        assert self._run(MagicMock()).is_complete is False


class TestRequestTimeout:
    """SDK 既定タイムアウトが Lambda より長い穴を塞ぐこと。

    実測（anthropic 0.112.0）: DEFAULT_TIMEOUT の read/write は 600 秒、
    DEFAULT_MAX_RETRIES は 2。既定のままだと最悪 600×3=1800 秒粘り、
    closeout Lambda の 120 秒を大きく超える。timeout 90 は据え置き、
    ノート経路の再試行だけ 1 回にする（#351）。積 90×2 を 120 に収める
    ために timeout を下げない（設計 §5）。
    """

    def _construct_client(self) -> Any:
        import anthropic

        from backend.src.llm import trade_note as tn

        now = datetime.datetime(2026, 6, 25, 4, 35, 0, tzinfo=JST)
        inp = TradeNoteInput(decision_plan="t", executions=[], now_jst=now)
        with (
            patch.object(anthropic, "Anthropic") as mock_ctor,
            patch(
                "backend.src.lambda_auth.get_anthropic_api_key",
                return_value="sk-test",
            ),
        ):
            mock_ctor.return_value = _make_llm_client()
            tn.generate_trade_note(inp)  # llm_client を渡さず実体生成パスを通す
        return mock_ctor

    def test_read_timeout_is_explicit(self) -> None:
        timeout = self._construct_client().call_args.kwargs["timeout"]
        assert timeout.read == 90.0
        assert timeout.write == 90.0

    def test_connect_timeout_stays_tight(self) -> None:
        """connect まで緩めないこと。

        float を渡すと 4 要素すべてに同じ値が入り、SDK 既定の connect=5 秒より
        **緩くなる**。待ちたいのは生成であって接続確立ではない。
        """
        timeout = self._construct_client().call_args.kwargs["timeout"]
        assert timeout.connect == 5.0

    def test_retries_are_one(self) -> None:
        """529 を 1 回だけ拾う（#351）。SDK 既定の 2 には戻さない。"""
        kwargs = self._construct_client().call_args.kwargs
        assert kwargs["max_retries"] == 1

    def test_timeout_is_not_lowered_to_fit_hang_multiplier(self) -> None:
        """ハング×再試行の積が Lambda 120 秒を超えても、timeout 90 を維持すること。

        設計 §5: hang-multiplier 天井（``timeout × (max_retries+1) < 120``）は
        **要求しない**。529 の再試行は ``retry-after`` 数秒で終わる。ハング時に
        90×2 が 120 を超えるのは受け入れる（#311 証跡先行・#341 実測）。
        timeout を下げて積を 120 に収める回帰を、積が 120 を超えることで検知する。
        """
        kwargs = self._construct_client().call_args.kwargs
        assert kwargs["timeout"].read == 90.0
        assert kwargs["max_retries"] == 1
        assert kwargs["timeout"].read * (kwargs["max_retries"] + 1) > 120


class TestMaxTokensByMode:
    """friday は平日より大きい上限を使うこと（#309）。"""

    def _sent_max_tokens(self, *, is_friday_mode: bool) -> int:
        now = datetime.datetime(2026, 6, 27, 4, 35, 0, tzinfo=JST)
        inp = TradeNoteInput(
            decision_plan="test",
            executions=[],
            now_jst=now,
            is_friday_mode=is_friday_mode,
        )
        client = _make_llm_client()
        generate_trade_note(inp, llm_client=client)
        max_tokens = client.messages.create.call_args.kwargs["max_tokens"]
        assert isinstance(max_tokens, int)
        return max_tokens

    def test_friday_uses_larger_budget_than_weekday(self) -> None:
        weekday = self._sent_max_tokens(is_friday_mode=False)
        friday = self._sent_max_tokens(is_friday_mode=True)
        assert friday > weekday

    def test_friday_budget_exceeds_observed_truncation_point(self) -> None:
        """建玉 2 件で 2048 に到達した実測（2026-08-15）を上回ること。"""
        assert self._sent_max_tokens(is_friday_mode=True) > 2048

    def test_friday_budget_bounded_for_lambda_timeout(self) -> None:
        """closeout Lambda の 120 秒に収まる範囲に留めること。

        実測スループットは約 85〜94 tokens/秒（2026-08-29 CloudWatch 参照）だが、
        退行に気付くため保守側の 79 tokens/秒で評価する。上限に張り付いても
        LLM 呼び出しが 60 秒を超えないこと。

        🔴 60 秒の根拠は「非LLM区間 30 秒未満」という不変条件の側にある（#339）。
        LLM は SDK read timeout 90 秒で頭打ちになるので、Lambda が LLM 起因で
        死ぬのは非LLM区間が 30 秒を超えたときだけ。実測は 2.9〜5.1 秒。
        """
        assert self._sent_max_tokens(is_friday_mode=True) / 79 < 60

    def test_generated_log_carries_max_tokens(self) -> None:
        """正常系のログにも上限を出すこと（#339）。

        output_tokens だけだと「上限にどれだけ近いか」をコード版を知らずに
        判定できず、打ち切り（#309）が起きてからしか気付けない。上限引き上げの
        撤退線は output_tokens / max_tokens 比で引くため、両方が同じ行に要る。
        """
        now = datetime.datetime(2026, 6, 27, 4, 35, 0, tzinfo=JST)
        inp = TradeNoteInput(
            decision_plan="test", executions=[], now_jst=now, is_friday_mode=True
        )
        with patch("backend.src.llm.trade_note._log") as mock_log:
            generate_trade_note(inp, llm_client=_make_llm_client())

        call = next(
            c for c in mock_log.info.call_args_list
            if c.args and c.args[0] == "trade_note_generated"
        )
        assert call.kwargs["max_tokens"] == self._sent_max_tokens(is_friday_mode=True)
        assert call.kwargs["output_tokens"] == 200


# ---------------------------------------------------------------------------
# #318: 見送り理由がプロンプトに載ること / 週次総括の規律
# ---------------------------------------------------------------------------

class TestNoEntryReasonInPrompt:
    """無発注日の理由を LLM に渡すこと（#318）。

    渡さないと「無発注日の判断根拠は記録なし」と書かれる。判断ログには
    実際には残っているため事実誤認になる（2026-08-22 の週次で発生）。
    """

    def _weekly_user_prompt(self, plans: tuple[Any, ...]) -> str:
        from backend.src.llm.trade_note import TradeNoteInput, generate_trade_note

        now = datetime.datetime(2026, 8, 22, 4, 30, 0, tzinfo=JST)  # 土曜
        inp = TradeNoteInput(
            decision_plan="週次",
            executions=[],
            now_jst=now,
            is_friday_mode=True,
            plans=plans,
        )
        client = _make_llm_client()
        generate_trade_note(inp, llm_client=client)
        return client.messages.create.call_args.kwargs["messages"][0]["content"]

    def test_hard_rule_reason_is_passed(self) -> None:
        """エッジガード等の day-skip 種別がプロンプトに載ること。"""
        from backend.src.llm.trade_note import TradePlan

        prompt = self._weekly_user_prompt(
            (
                TradePlan(
                    trade_date="2026-08-18(火)",
                    status="no_entry",
                    no_entry_reason="high_vol_regime_day_skip",
                ),
            )
        )
        assert "high_vol_regime_day_skip" in prompt

    def test_llm_rationale_is_passed_when_reason_absent(self) -> None:
        """LLM 判断による見送り（reason なし）は rationale が載ること。"""
        from backend.src.llm.trade_note import TradePlan

        rationale = "03:00 FOMC 議事録が保有窓内に直撃し方向性が読みにくい"
        prompt = self._weekly_user_prompt(
            (
                TradePlan(
                    trade_date="2026-08-20(木)",
                    status="no_entry",
                    no_entry_reason=rationale,
                ),
            )
        )
        assert rationale in prompt

    def test_no_entry_without_reason_still_marked_no_entry(self) -> None:
        """理由が無い日も NO_ENTRY 自体は落とさないこと（後方互換）。"""
        from backend.src.llm.trade_note import TradePlan

        prompt = self._weekly_user_prompt(
            (TradePlan(trade_date="2026-08-11(火)", status="no_entry"),)
        )
        assert "NO_ENTRY" in prompt

    def test_log_unavailable_is_not_confused_with_no_entry(self) -> None:
        """判断ログを読めなかった日と「発注しなかった日」を混同しないこと（#244 の規律）。"""
        from backend.src.llm.trade_note import TradePlan

        prompt = self._weekly_user_prompt(
            (
                TradePlan(
                    trade_date="2026-08-19(水)",
                    status="no_entry",
                    no_entry_reason="should_not_appear",
                    log_available=False,
                ),
            )
        )
        assert "取得できず" in prompt
        assert "should_not_appear" not in prompt


class TestWeeklySummaryDiscipline:
    """週次総括の規律（#318 副次）。

    2026-08-22 の週次ノートは「勝率50%」を自分で計算し、
    「発注基準の再検討推奨」と改善提案まで書いた。どちらも既存の
    system プロンプトが禁止していたが、週次セクションの指示が
    「振り返ること」の 1 文しか無く、埋め合わせを誘発していた。
    """

    def _weekly_prompts(self) -> tuple[str, str]:
        from backend.src.llm.trade_note import TradeNoteInput, generate_trade_note

        now = datetime.datetime(2026, 8, 22, 4, 30, 0, tzinfo=JST)
        inp = TradeNoteInput(
            decision_plan="週次", executions=[], now_jst=now, is_friday_mode=True
        )
        client = _make_llm_client()
        generate_trade_note(inp, llm_client=client)
        kwargs = client.messages.create.call_args.kwargs
        return kwargs.get("system", ""), kwargs["messages"][0]["content"]

    def test_forbids_self_computed_statistics(self) -> None:
        _, user = self._weekly_prompts()
        assert "勝率" in user
        assert "統計値を自分で計算しない" in user

    def test_forbids_parameter_change_suggestions(self) -> None:
        _, user = self._weekly_prompts()
        assert "再検討" in user  # 「〜の再検討推奨」を名指しで禁止している
        assert "変更提案" in user

    def test_forbids_claiming_reasons_are_unrecorded(self) -> None:
        """「判断根拠が記録されていない」と書かせないこと（本件の再発防止）。"""
        _, user = self._weekly_prompts()
        assert "判断根拠が記録されていない" in user

    def test_friday_keeps_next_week_outlook_ban(self) -> None:
        """週次固有の「来週の見通し」禁止は friday に残ること（#342）。"""
        _, user = self._weekly_prompts()
        assert "来週の相場見通し" in user

    def test_weekday_mode_has_no_weekly_instructions(self) -> None:
        """平日ノートに週次固有の追加指示が漏れないこと。

        規律の禁止本体（統計値・パラメータ見直し）は #342 で共通部へ移した。
        ここで見るのは「週次総括」「来週の見通し」など friday 専用の指示。
        """
        from backend.src.llm.trade_note import TradeNoteInput, generate_trade_note

        now = datetime.datetime(2026, 8, 20, 4, 30, 0, tzinfo=JST)
        inp = TradeNoteInput(
            decision_plan="平日", executions=[], now_jst=now, is_friday_mode=False
        )
        client = _make_llm_client()
        generate_trade_note(inp, llm_client=client)
        user = client.messages.create.call_args.kwargs["messages"][0]["content"]
        assert "追加指示（週次総括）" not in user
        assert "来週の相場見通し" not in user


class TestWeekdayDiscipline:
    """平日ノートにも規律の詳細ブロックが届くこと（#342）。

    #318 の禁止文言は ``is_friday_mode`` の内側にあり、平日には
    system の一行「戦略パラメータの自動変更提案は絶対に行わない」しか
    届いていなかった。2026-08-31 の平日ノートが「保有窓の期間設定を
    今後の検証対象とすること」と書いて実データで漏れた。
    """

    def _weekday_user(self) -> str:
        from backend.src.llm.trade_note import TradeNoteInput, generate_trade_note

        now = datetime.datetime(2026, 8, 31, 4, 30, 0, tzinfo=JST)
        inp = TradeNoteInput(
            decision_plan="平日", executions=[], now_jst=now, is_friday_mode=False
        )
        client = _make_llm_client()
        generate_trade_note(inp, llm_client=client)
        return str(client.messages.create.call_args.kwargs["messages"][0]["content"])

    def test_weekday_prompt_forbids_parameter_change_suggestions(self) -> None:
        """🔴 修正前の平日プロンプトにはこの文言が無く、本テストは落ちる。"""
        user = self._weekday_user()
        assert "再検討" in user
        assert "検証対象" in user
        assert "変更提案" in user

    def test_weekday_prompt_forbids_self_computed_statistics(self) -> None:
        user = self._weekday_user()
        assert "統計値を自分で計算しない" in user

    def test_weekday_prompt_forbids_unprovided_speculation(self) -> None:
        """保有中の値動きは渡していない。反転／レンジの推測を禁ずること。"""
        user = self._weekday_user()
        assert "保有中の値動き" in user
        assert "反転" in user

    def test_weekday_discipline_is_a_heading(self) -> None:
        """共通部は他セクションと同じ ``##`` 見出しであること（#342 レビュー）。"""
        user = self._weekday_user()
        assert "## 書かないこと（平日・週次共通）" in user


# ---------------------------------------------------------------------------
# #342 案2: 禁止フレーズ検出（WARN のみ）
# ---------------------------------------------------------------------------

#: 2026-08-31 の平日ノートで実際に出た表現（Issue #342 の再現素材。公開版では損益の記述を除いた）。
_WEEKDAY_SUGGESTION_NOTE = (
    "## 狙撃プラン\ntest\n"
    "## 約定結果\ntest\n"
    "## 自己評価\n"
    "（架空）時間切れ手仕舞いにより決済。\n"
    "保有窓の期間設定と、期間内の達成確率の関係を今後の検証対象とすること。\n"
)

#: 2026-09-02 平日ノート（3 例目）。
_WEEKDAY_IMPROVEMENT_NOTE = (
    "## 自己評価\n"
    "### 今後への示唆:\n"
    "- 指値注文の到達を視認レベルで確認できる体制の整備\n"
    "- 取得できるティックデータの粒度・期間を検証\n"
)


class TestDisciplineViolations:
    """禁止フレーズ検出（#342 案2）。WARN のみで失敗にしない。"""

    def test_detects_hold_hours_suggestion(self) -> None:
        """🔴 2026-08-31 の実ノート（検証対象）を検出すること。"""
        hits = find_discipline_violations(_WEEKDAY_SUGGESTION_NOTE)
        assert hits, "実データの見直し示唆を検出できていない"
        assert any("検証対象" in h for h in hits), hits

    def test_detects_future_suggestion_heading(self) -> None:
        """🔴 2026-09-02 の「今後への示唆」見出しを検出すること。"""
        hits = find_discipline_violations(_WEEKDAY_IMPROVEMENT_NOTE)
        assert hits, "改善提案セクションを検出できていない"
        assert any("今後への示唆" in h for h in hits), hits

    def test_detects_win_rate_and_hold_time(self) -> None:
        """#318 デプロイ前の週次（勝率・平均保有時間）も拾うこと。"""
        note = "## 週次総括\n勝率：50%\n平均保有時間：約2時間\n発注基準の再検討推奨\n"
        hits = find_discipline_violations(note)
        joined = "\n".join(hits)
        assert "勝率" in joined
        assert "平均保有時間" in joined
        assert "再検討" in joined

    def test_clean_note_produces_no_warning(self) -> None:
        """事実の記録だけなら鳴らないこと。"""
        clean = (
            "## 自己評価\n"
            "時間切れ手仕舞いにより決済。実現損益は -0.50 円。\n"
            "計画どおりの強制決済であり、待つ・待たないという選択肢は無い。\n"
        )
        assert find_discipline_violations(clean) == ()

    def test_negated_review_does_not_false_positive(self) -> None:
        """正しい否定形で鳴らないこと（Issue 本文の誤検知例）。"""
        line = "設計どおりの決済であり見直しの余地はない。\n"
        assert find_discipline_violations(line) == ()

    def test_negated_reconsideration_does_not_false_positive(self) -> None:
        """「再検討は行わない」は提案ではない。"""
        line = "戦略パラメータの再検討は行わない（人間レビュー専用）。\n"
        assert find_discipline_violations(line) == ()

    def test_negated_forms_beyond_the_original_two(self) -> None:
        """行単位の固定9語では拾えない否定形で鳴らないこと（#342 レビュー）。

        プロンプトが「示唆しない」と強く指示すると、LLM はまさにこの否定形を
        書きやすい。テストに書いた2形（余地はない / は行わない）だけでは足りない。
        """
        silent = (
            "決済タイミングの改善案は存在しない。",
            "時間切れ手仕舞いのため、再検討の必要はない。",
            "検証対象ではない。",
            "勝率は与えられていないため記載しない。",
            "パラメータの再検討は人間レビューに委ねる。",
            "改善案は本ノートの範囲外。",
        )
        for line in silent:
            assert find_discipline_violations(line) == (), line

    def test_mixed_clause_does_not_skip_the_violation(self) -> None:
        """同一行の別句にある違反を、否定句に引きずられて見逃さないこと。"""
        cases = (
            (
                "統計値は自分で計算しないが、保有窓の期間設定は今後の検証対象としたい。",
                "検証対象",
            ),
            (
                "戦略変更提案は行わない。なお平均保有時間は約2時間であった。",
                "平均保有時間",
            ),
            (
                "数値の創作は禁止されている。発注基準の再検討を推奨する。",
                "再検討",
            ),
        )
        for line, phrase in cases:
            hits = find_discipline_violations(line)
            assert hits, f"見逃し: {line}"
            assert any(phrase in h for h in hits), (line, hits)

    def test_generate_keeps_is_complete_on_hit(self) -> None:
        """検出しても is_complete は下げないこと（#334 と同じ扱い）。"""
        inp = TradeNoteInput(
            decision_plan="平日",
            executions=[],
            now_jst=datetime.datetime(2026, 8, 31, 4, 30, 0, tzinfo=JST),
            is_friday_mode=False,
        )
        client = _make_llm_client(_WEEKDAY_SUGGESTION_NOTE)
        result = generate_trade_note(inp, llm_client=client)
        assert result.is_complete is True
        assert result.discipline_warnings
        assert any("検証対象" in w for w in result.discipline_warnings)

    def test_generate_omits_warnings_on_clean_note(self) -> None:
        inp = TradeNoteInput(
            decision_plan="平日",
            executions=[],
            now_jst=datetime.datetime(2026, 8, 26, 4, 30, 0, tzinfo=JST),
            is_friday_mode=False,
        )
        result = generate_trade_note(inp, llm_client=_make_llm_client())
        assert result.discipline_warnings == ()


class TestPlanRowTableSafety:
    """見送り理由がテーブルを壊さないこと（#318）。

    rationale は LLM 生成の散文で、改行やパイプが入り得る。
    崩れると他の日の計画値まで読めなくなる。
    """

    def _row(self, reason: str) -> str:
        from backend.src.llm.trade_note import TradePlan, _plan_row

        return _plan_row(
            TradePlan(trade_date="2026-08-20(木)", status="no_entry", no_entry_reason=reason)
        )

    def test_newlines_are_collapsed(self) -> None:
        row = self._row("FOMC が保有窓内。\n方向感が読めない。")
        assert "\n" not in row
        assert "FOMC が保有窓内。 方向感が読めない。" in row

    def test_pipes_are_replaced(self) -> None:
        row = self._row("A|B の判断")
        # セル内のパイプは列区切りに化けるため置換する
        assert row.count("|") == 8  # 7列ぶんの区切り + 行頭
        assert "A/B の判断" in row

    def test_reason_is_not_truncated(self) -> None:
        """長文でも切り詰めないこと（肝が末尾にある場合に落ちるため）。"""
        long_reason = "あ" * 400
        assert long_reason in self._row(long_reason)


class TestWeekdayPlanSectionNoEntry:
    """平日ノートでも見送り理由を出すこと（#318 レビュー Nit）。

    判断ログの rationale はハードルール日だと
    「エッジガード day-skip hard rule（§3.3 / RD-8 #194）」という総称しか入らず、
    G2（高ボラ）か G3（モメンタム不足）かが分からない。種別は reason にしかない。
    """

    def _section(self, **kw: Any) -> str:
        from backend.src.llm.trade_note import TradePlan, _build_plan_section

        return _build_plan_section(TradePlan(trade_date="2026-08-18(火)", **kw))

    def test_no_entry_reason_is_shown(self) -> None:
        section = self._section(status="no_entry", no_entry_reason="high_vol_regime_day_skip")
        assert "high_vol_regime_day_skip" in section
        assert "発注していない" in section

    def test_no_entry_without_reason_is_marked_unknown(self) -> None:
        """理由が取れない日を「計画値が無い」と混同しないこと。"""
        section = self._section(status="no_entry")
        assert "発注していない" in section
        assert "取得できていない" in section

    def test_missing_plan_is_still_distinct_from_no_entry(self) -> None:
        """status が no_entry でない計画値なしは従来の文面のままであること。"""
        section = self._section(status="")
        assert "判断ログに計画値が無い" in section
        assert "発注していない" not in section

    def test_sl_tp_inference_is_still_forbidden(self) -> None:
        """どちらの分岐でも SL/TP の推測禁止が残ること。"""
        for kw in ({"status": "no_entry", "no_entry_reason": "x"}, {"status": ""}):
            assert "推測して書かないこと" in self._section(**kw)

    def test_reason_is_table_safe(self) -> None:
        """改行入りの rationale でも 1 行に畳まれること。"""
        section = self._section(status="no_entry", no_entry_reason="A\nB")
        assert "A B" in section


# ---------------------------------------------------------------------------
# #334: 週次ノートの他日混入（Python 描画の確定表 + 転記ミス検出）
# ---------------------------------------------------------------------------

#: 2026-08-24 週の事象を再現した架空の計画値（Issue #334 の再現素材。公開版では価格を置き換えた）。
#: 08-24 の行に 08-27 の SL/TP が書かれたのが事象。
_WEEK_PLANS = (
    TradePlan(
        direction="LONG",
        order_type="LIMIT",
        entry_price="100.165",
        stop_loss="99.768",
        take_profit="100.642",
        trade_date="2026-08-24(月)",
        status="ok",
    ),
    TradePlan(
        direction="LONG",
        order_type="LIMIT",
        entry_price="100.295",
        stop_loss="99.905",
        take_profit="100.763",
        trade_date="2026-08-25(火)",
        status="ok",
    ),
    TradePlan(
        trade_date="2026-08-26(水)",
        status="no_entry",
        no_entry_reason="indicator_day_skip",
    ),
    TradePlan(
        direction="LONG",
        order_type="LIMIT",
        entry_price="100.44",
        stop_loss="100.065",
        take_profit="100.89",
        trade_date="2026-08-27(木)",
        status="ok",
    ),
    TradePlan(
        trade_date="2026-08-28(金)",
        status="no_entry",
        no_entry_reason="indicator_day_skip",
    ),
)

#: 実際に生成された壊れたノートと同じ形（08-24 の行に 08-27 の SL/TP）。
_CONTAMINATED_NOTE = (
    "## 狙撃プラン\ntest\n"
    "## 約定結果\ntest\n"
    "## 自己評価\ntest\n"
    "## 週次総括\n"
    "### 取引日ごとの結果\n"
    "| 2026-08-24 | 月 | LONG LIMIT 100.165 で新規。計画 SL=100.065 / TP=100.89 。 |\n"
    "| 2026-08-25 | 火 | LONG LIMIT 100.295 で新規。計画 SL=99.905 / TP=100.763 。 |\n"
    "| 2026-08-27 | 木 | LONG LIMIT 100.44 で新規。計画 SL=100.065 / TP=100.89 。 |\n"
)


def _friday_input(note_plans: tuple[TradePlan, ...] = _WEEK_PLANS) -> TradeNoteInput:
    return TradeNoteInput(
        decision_plan="週次モード",
        executions=[],
        now_jst=datetime.datetime(2026, 8, 29, 4, 30, 0, tzinfo=JST),  # 土曜 AM 4:30
        is_friday_mode=True,
        plans=note_plans,
    )


class TestCrossDayValueLeak:
    """他日の計画値の混入を検出する（#334 案2）。"""

    def test_detects_real_contamination(self) -> None:
        """🔴 2026-08-29 に実際に起きた混入を検出すること。

        08-24 の行の 100.065 / 100.89 は 08-27 の SL/TP。
        """
        leaks = find_cross_day_value_leaks(_CONTAMINATED_NOTE, _WEEK_PLANS)
        assert leaks, "実データの混入を検出できていない"
        joined = "\n".join(leaks)
        assert "100.065" in joined
        assert "100.89" in joined
        # 🔴 鳴るのは 08-24 の行だけ。08-25 / 08-27 の行は自分の値なので正しい。
        #    （警告文には所有日として 2026-08-27 が現れるため、"含まれない" では検査できない）
        assert all("2026-08-24 の記述に" in leak for leak in leaks), joined
        assert len(leaks) == 2, joined

    def test_clean_note_produces_no_warning(self) -> None:
        """各日が自分の値だけを書いていれば鳴らないこと（誤検知の固定）。"""
        clean = (
            "## 週次総括\n"
            "| 2026-08-24 | 月 | LONG LIMIT 100.165 / SL 99.768 / TP 100.642 |\n"
            "| 2026-08-25 | 火 | LONG LIMIT 100.295 / SL 99.905 / TP 100.763 |\n"
            "| 2026-08-27 | 木 | LONG LIMIT 100.44 / SL 100.065 / TP 100.89 |\n"
        )
        assert find_cross_day_value_leaks(clean, _WEEK_PLANS) == ()

    def test_pnl_and_pips_do_not_false_positive(self) -> None:
        """損益・pips は計画価格と一致しない限り鳴らないこと。

        「知らない数値」ではなく「**他日の計画値**」だけを拾う設計なので、
        3 桁の実現損益（139.766 円）や pips（37.5）は無関係に通る。
        """
        line = (
            "## 週次総括\n"
            "| 2026-08-24 | 月 | 実現損益 +139.766 円 / SL幅 39.7 pips / 保有 8.0 時間 |\n"
        )
        assert find_cross_day_value_leaks(line, _WEEK_PLANS) == ()

    def test_short_date_form_is_matched(self) -> None:
        """``08-24`` のような短縮形の行でも検出すること。"""
        leaks = find_cross_day_value_leaks(
            "08-24 の計画は SL=100.065 だった\n", _WEEK_PLANS
        )
        assert len(leaks) == 1

    def test_line_with_multiple_dates_is_skipped(self) -> None:
        """1 行に複数の取引日があるときは判定を見送ること（誤検知回避）。"""
        line = "2026-08-24 と 2026-08-27 を比較すると 100.065 の位置が異なる\n"
        assert find_cross_day_value_leaks(line, _WEEK_PLANS) == ()

    def test_single_day_week_cannot_leak(self) -> None:
        """取引日が 1 日なら「他日」が存在せず常に空であること。"""
        one = (_WEEK_PLANS[0],)
        assert find_cross_day_value_leaks(_CONTAMINATED_NOTE, one) == ()

    def test_authoritative_section_is_not_scanned(self) -> None:
        """Python が描画した確定表は検査対象外（自分の出力で鳴らさない）。"""
        note = "## 週次総括\n概況のみ。\n" + build_authoritative_weekly_section(_WEEK_PLANS)
        assert find_cross_day_value_leaks(note, _WEEK_PLANS) == ()


class TestAuthoritativeWeeklySection:
    """週次の計画値表を Python が描画する（#334 案1）。"""

    def test_each_day_row_carries_only_its_own_values(self) -> None:
        """🔴 各日の SL/TP がその日の行にだけ出ること（本 Issue の受け入れ条件）。"""
        section = build_authoritative_weekly_section(_WEEK_PLANS)
        rows = {
            line.split("|")[1].strip(): line
            for line in section.splitlines()
            if line.startswith("| 2026-")
        }

        assert "99.768" in rows["2026-08-24(月)"]
        assert "100.642" in rows["2026-08-24(月)"]
        # 08-27 の値が 08-24 の行に無いこと（事象そのもの）
        assert "100.065" not in rows["2026-08-24(月)"]
        assert "100.89" not in rows["2026-08-24(月)"]

        assert "99.905" in rows["2026-08-25(火)"]
        assert "100.065" in rows["2026-08-27(木)"]
        assert "100.89" in rows["2026-08-27(木)"]

    def test_no_entry_days_keep_their_reason(self) -> None:
        """見送り日は理由付きで残ること（#318 の退行防止）。"""
        section = build_authoritative_weekly_section(_WEEK_PLANS)
        assert "indicator_day_skip" in section

    def test_appended_to_friday_note(self) -> None:
        """friday モードのノート末尾に確定表が付くこと。"""
        client = _make_llm_client(_CONTAMINATED_NOTE)
        result = generate_trade_note(_friday_input(), llm_client=client)
        assert "取引日ごとの計画値（Python 生成・LLM 未経由）" in result.note_markdown
        # 確定表の 08-24 行には正しい値が入る（本文が壊れていても）
        row = next(
            line
            for line in result.note_markdown.splitlines()
            if line.startswith("| 2026-08-24(月)")
        )
        assert "99.768" in row and "100.642" in row

    def test_appended_to_fallback_when_llm_fails(self) -> None:
        """🔴 LLM 生成が失敗しても確定表を残すこと（§5.2）。

        計画値は判断ログから分かるので、空のノートより手掛かりを残す方がよい。
        この経路が後から外れても気付けるように固定する。
        """
        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("api down")

        result = generate_trade_note(_friday_input(), llm_client=client)

        assert result.is_complete is False
        assert "LLM生成失敗" in result.note_markdown
        # 生成に失敗しても計画値の表は残る
        assert "取引日ごとの計画値（Python 生成・LLM 未経由）" in result.note_markdown
        row = next(
            line
            for line in result.note_markdown.splitlines()
            if line.startswith("| 2026-08-24(月)")
        )
        assert "99.768" in row and "100.642" in row

    def test_fallback_without_plans_has_no_table(self) -> None:
        """計画値が無い失敗ノートには表を付けないこと（空表を作らない）。"""
        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("api down")
        inp = TradeNoteInput(
            decision_plan="週次",
            executions=[],
            now_jst=datetime.datetime(2026, 8, 29, 4, 30, 0, tzinfo=JST),
            is_friday_mode=True,
        )
        result = generate_trade_note(inp, llm_client=client)
        assert "Python 生成・LLM 未経由" not in result.note_markdown

    def test_not_appended_to_weekday_note(self) -> None:
        """平日ノートには付けないこと（計画値 1 件で取り違えの相手がいない）。"""
        inp = TradeNoteInput(
            decision_plan="平日",
            executions=[],
            now_jst=datetime.datetime(2026, 8, 26, 4, 30, 0, tzinfo=JST),
            is_friday_mode=False,
            plans=(_WEEK_PLANS[0],),
        )
        result = generate_trade_note(inp, llm_client=_make_llm_client())
        assert "Python 生成・LLM 未経由" not in result.note_markdown


class TestCrossDayWarningsPlumbing:
    """検出結果が TradeNoteResult に載ること（closeout 側の通知の前提）。"""

    def test_contaminated_note_sets_warnings(self) -> None:
        client = _make_llm_client(_CONTAMINATED_NOTE)
        result = generate_trade_note(_friday_input(), llm_client=client)
        assert result.cross_day_warnings, "混入を検出できていない"
        # 🔴 生成自体は正常終了しているので is_complete は下げない
        assert result.is_complete is True

    def test_clean_note_has_no_warnings(self) -> None:
        clean = "## 週次総括\n今週は 3 回エントリーした。詳細は確定表のとおり。\n"
        client = _make_llm_client(clean)
        result = generate_trade_note(_friday_input(), llm_client=client)
        assert result.cross_day_warnings == ()

    def test_prompt_tells_llm_not_to_transcribe(self) -> None:
        """プロンプトに「表を作らない」指示が入ること（案3 は単独では採らない補強）。"""
        client = _make_llm_client()
        generate_trade_note(_friday_input(), llm_client=client)
        # friday の追加指示は user 側に載る（system は共通規律）
        user = client.messages.create.call_args.kwargs["messages"][0]["content"]
        assert "取引日ごとの計画値の表を作らない" in user
        assert "書き写さない" in user
        # 🔴 「書くこと」側も揃っていること。ここが「取引日ごとの結果」を書けと
        #    言ったままだと、直後で表を禁じても LLM が表を描く余地が残る
        #    （今回壊れたのはまさにその表）。
        assert "取引日ごとの結果を**散文で**" in user
        assert "数値は末尾の確定表に載る" in user


# ---------------------------------------------------------------------------
# #333: 見送りの指標名・エッジガード判定値をノートに出す
# ---------------------------------------------------------------------------

#: 2026-08-26 の実データ（指標 day-skip）。名称に "/" を含む（m/m・q/q）。
_INDICATOR_SKIP_PLAN = TradePlan(
    trade_date="2026-08-26(水)",
    status="no_entry",
    no_entry_reason="indicator_day_skip",
    skip_indicators=(
        SkipIndicator(name="Core PCE Price Index m/m", time_jst="21:30", currency="USD"),
        SkipIndicator(name="Prelim GDP q/q", time_jst="21:30", currency="USD"),
    ),
)

#: 2026-08-18 の実データ（G2 高ボラ）。閾値の**真上 0.42 pips** で弾かれた日。
_EDGE_GUARD_PLAN = TradePlan(
    trade_date="2026-08-18(火)",
    status="no_entry",
    no_entry_reason="high_vol_regime_day_skip",
    edge_guard_details=(
        ("atr_pips", "160.421"),
        ("threshold_pips", "160.0"),
        ("atr_14d", "1.60421"),
    ),
)


class TestSkipIndicatorsInNote:
    """指標 day-skip の対象指標名がノートに出ること（#333）。"""

    def test_weekly_row_lists_indicator_names(self) -> None:
        row = _plan_row(_INDICATOR_SKIP_PLAN)
        assert "Core PCE Price Index m/m" in row
        assert "Prelim GDP q/q" in row
        assert "21:30" in row
        assert "USD" in row
        # 種別も従来どおり残る（#318 の退行防止）
        assert "indicator_day_skip" in row

    def test_weekday_section_lists_indicator_names(self) -> None:
        section = _build_plan_section(_INDICATOR_SKIP_PLAN)
        assert "Core PCE Price Index m/m" in section
        assert "Prelim GDP q/q" in section

    def test_indicator_names_with_slash_stay_readable(self) -> None:
        """🔴 指標名の "/" と区切りが混ざらないこと。

        ``m/m`` ``q/q`` を含むため、スラッシュ区切りにすると境界が読めなくなる。
        """
        row = _plan_row(_INDICATOR_SKIP_PLAN)
        assert "m/m（21:30 JST・USD）、Prelim GDP q/q" in row

    def test_table_is_not_broken_by_indicator_text(self) -> None:
        """表のセル数が崩れないこと（``_table_safe`` を通していること）。"""
        nasty = TradePlan(
            trade_date="2026-08-26(水)",
            status="no_entry",
            no_entry_reason="indicator_day_skip",
            skip_indicators=(
                SkipIndicator(name="悪い | 指標\n改行あり", time_jst="21:30", currency="USD"),
            ),
        )
        row = _plan_row(nasty)
        assert "\n" not in row
        assert row.count("|") == _plan_row(_INDICATOR_SKIP_PLAN).count("|")


class TestSkipIndicatorTimezone:
    """指標の発表時刻に JST を明示すること（#343）。

    🔴 2026-09-01 の本番ノートで **「23:00 UTC」** と書かれた。判断ログは
    ``time_jst: "23:00"`` で JST であり、9 時間ずれた時刻が税務証跡に残った。
    描画が時刻の値だけを並べ、タイムゾーンを落としていたため、LLM が補完した。
    ``AGENTS.md`` 安全ガードレール #6「時刻は常に JST」の趣旨にも反する。
    """

    def test_weekly_row_labels_time_as_jst(self) -> None:
        row = _plan_row(_INDICATOR_SKIP_PLAN)
        assert "21:30 JST" in row

    def test_weekday_section_labels_time_as_jst(self) -> None:
        section = _build_plan_section(_INDICATOR_SKIP_PLAN)
        assert "21:30 JST" in section

    #: 素の ``HH:MM`` が区切り（``・``）や閉じ括弧に接している形。
    #: この形だと読み手にも LLM もタイムゾーンが分からない。
    _BARE_TIME = re.compile(r"\d{1,2}:\d{2}[・）]")

    def test_bare_time_without_timezone_never_appears(self) -> None:
        """🔴 これが #343 の本体。**素の時刻**を出すと LLM がタイムゾーンを補完する。

        特定の時刻ではなく **形** を禁じる（``21:30`` 決め打ちだと、別の時刻に
        変わったときに素通りする）。実際 2026-09-01 は ``23:00`` が UTC と誤記された。
        """
        row = _plan_row(_INDICATOR_SKIP_PLAN)
        assert self._BARE_TIME.search(row) is None, row

    def test_bare_time_form_is_rejected_for_any_clock_value(self) -> None:
        """時刻の値を変えても素の形が出ないこと（``21:30`` への依存を断つ）。"""
        for clock in ("9:05", "23:00", "00:30"):
            plan = TradePlan(
                trade_date="2026-09-01(火)",
                status="no_entry",
                no_entry_reason="indicator_day_skip",
                skip_indicators=(
                    SkipIndicator(name="CPI y/y", time_jst=clock, currency="USD"),
                ),
            )
            row = _plan_row(plan)
            assert self._BARE_TIME.search(row) is None, row
            assert f"{clock} JST" in row

    def test_time_without_currency_still_labels_jst(self) -> None:
        """通貨が無くても時刻には JST が付くこと（連結の分岐漏れ防止）。"""
        plan = TradePlan(
            trade_date="2026-09-01(火)",
            status="no_entry",
            no_entry_reason="indicator_day_skip",
            skip_indicators=(SkipIndicator(name="CPI y/y", time_jst="21:30"),),
        )
        row = _plan_row(plan)
        assert "CPI y/y（21:30 JST）" in row
        assert self._BARE_TIME.search(row) is None, row

    def test_date_jst_renders_month_day_without_padding(self) -> None:
        """date_jst があるログだけ ``M/D HH:MM JST``（#374）。（保有中）は付けない。"""
        plan = TradePlan(
            trade_date="2026-12-09(水)",
            status="no_entry",
            no_entry_reason="indicator_day_skip",
            skip_indicators=(
                SkipIndicator(
                    name="ISM Manufacturing PMI",
                    time_jst="00:00",
                    currency="USD",
                    date_jst="2026-12-02",
                ),
            ),
        )
        row = _plan_row(plan)
        assert "ISM Manufacturing PMI（12/2 00:00 JST・USD）" in row
        assert "（保有中）" not in row

    def test_real_2026_09_01_case(self) -> None:
        """本番で誤記が出た当日の入力を、そのまま再現する（#343）。"""
        plan = TradePlan(
            trade_date="2026-09-01(火)",
            status="no_entry",
            no_entry_reason="indicator_day_skip",
            skip_indicators=(
                SkipIndicator(
                    name="ISM Manufacturing PMI", time_jst="23:00", currency="USD"
                ),
            ),
        )
        row = _plan_row(plan)
        assert "ISM Manufacturing PMI（23:00 JST・USD）" in row

    def test_indicator_without_time_has_no_timezone_label(self) -> None:
        """時刻が無い指標に JST を付けない（無い情報を作らない）。"""
        plan = TradePlan(
            trade_date="2026-09-01(火)",
            status="no_entry",
            no_entry_reason="indicator_day_skip",
            skip_indicators=(SkipIndicator(name="CPI y/y", currency="USD"),),
        )
        row = _plan_row(plan)
        assert "CPI y/y（USD）" in row
        assert "JST" not in row

    def test_indicator_with_only_name_is_unchanged(self) -> None:
        plan = TradePlan(
            trade_date="2026-09-01(火)",
            status="no_entry",
            no_entry_reason="indicator_day_skip",
            skip_indicators=(SkipIndicator(name="CPI y/y"),),
        )
        row = _plan_row(plan)
        assert "CPI y/y" in row
        assert "（" not in row.split("CPI y/y")[1].split("|")[0]


class TestEdgeGuardDetailsInNote:
    """エッジガードの判定値がノートに出ること（#333）。"""

    def test_weekly_row_shows_threshold_and_value(self) -> None:
        """🔴 閾値の真上で弾かれた日と大きく超過した日を区別できること。"""
        row = _plan_row(_EDGE_GUARD_PLAN)
        assert "atr_pips=160.421" in row
        assert "threshold_pips=160.0" in row
        assert "high_vol_regime_day_skip" in row

    def test_weekday_section_shows_threshold_and_value(self) -> None:
        section = _build_plan_section(_EDGE_GUARD_PLAN)
        assert "atr_pips=160.421" in section
        assert "threshold_pips=160.0" in section

    def test_key_order_follows_the_log(self) -> None:
        """キー順は判断ログのまま（G1/G2/G3 で入るキーが違うため並べ替えない）。"""
        row = _plan_row(_EDGE_GUARD_PLAN)
        assert row.index("atr_pips=") < row.index("threshold_pips=") < row.index("atr_14d=")


class TestSkipDetailFallback:
    """値が無い日は従来どおりに倒れること（#333・#318 の退行防止）。"""

    def test_llm_judgement_skip_falls_back_to_rationale(self) -> None:
        """LLM 判断の見送り（skip_indicators なし）は rationale だけが出ること。"""
        plan = TradePlan(
            trade_date="2026-08-20(木)",
            status="no_entry",
            no_entry_reason="FOMC 議事録待ちのため見送り",
        )
        row = _plan_row(plan)
        assert "FOMC 議事録待ちのため見送り" in row
        assert "対象指標" not in row
        assert "判定値" not in row

    def test_no_entry_without_any_reason_is_unchanged(self) -> None:
        plan = TradePlan(trade_date="2026-08-21(金)", status="no_entry")
        row = _plan_row(plan)
        assert "NO_ENTRY（発注していない）" in row
        assert "理由:" not in row

    def test_entry_day_is_unaffected(self) -> None:
        """発注した日の行は従来どおり（見送り用の項目が混ざらない）。"""
        row = _plan_row(_WEEK_PLANS[0])
        assert "99.768" in row
        assert "対象指標" not in row


class TestSkipDetailPlumbing:
    """判断ログ → TradePlan の運搬（無加工であること）。"""

    def test_values_are_carried_verbatim(self) -> None:
        """🔴 値を丸めない・単位変換しない（TradePlan 既存方針）。

        ATR は割り算の結果なので端数が長い。ここで丸めると「閾値の真上で
        弾かれた日」の判断材料が失われる（本 Issue の動機そのもの）。
        """
        from backend.src.batch.closeout_batch import _build_trade_plan

        plan = _build_trade_plan(
            {
                "status": "no_entry",
                "reason": "high_vol_regime_day_skip",
                "skip_indicators": [],
                "edge_guard_details": {
                    "atr_pips": 160.4213456789012,
                    "threshold_pips": 160.0,
                },
            },
            trade_date=datetime.date(2026, 8, 18),
        )
        details = dict(plan.edge_guard_details)
        assert details["atr_pips"] == "160.4213456789012"
        assert details["threshold_pips"] == "160.0"

    def test_missing_fields_produce_empty_tuples(self) -> None:
        """フィールドが無い / 型が違うログでも落ちないこと。"""
        from backend.src.batch.closeout_batch import _build_trade_plan

        for bad in ({}, {"skip_indicators": "x", "edge_guard_details": 3}):
            plan = _build_trade_plan(bad, trade_date=datetime.date(2026, 8, 18))
            assert plan.skip_indicators == ()
            assert plan.edge_guard_details == ()

    def test_indicator_entries_that_are_not_dicts_are_skipped(self) -> None:
        from backend.src.batch.closeout_batch import _build_trade_plan

        plan = _build_trade_plan(
            {
                "status": "no_entry",
                "skip_indicators": ["not-a-dict", {"name": "CPI y/y"}],
            },
            trade_date=datetime.date(2026, 8, 18),
        )
        assert [i.name for i in plan.skip_indicators] == ["CPI y/y"]
        assert plan.skip_indicators[0].date_jst == ""

    def test_date_jst_is_carried_verbatim(self) -> None:
        """date_jst は無加工で運ぶ。無いキーは空（過去ログの HH:MM 描画を壊さない）。"""
        from backend.src.batch.closeout_batch import _build_trade_plan

        plan = _build_trade_plan(
            {
                "status": "no_entry",
                "skip_indicators": [
                    {
                        "name": "FOMC Statement",
                        "time_jst": "04:00",
                        "date_jst": "2026-12-10",
                        "currency": "USD",
                    }
                ],
            },
            trade_date=datetime.date(2026, 12, 9),
        )
        ind = plan.skip_indicators[0]
        assert ind.time_jst == "04:00"
        assert ind.date_jst == "2026-12-10"
        assert ind.currency == "USD"


# ---------------------------------------------------------------------------
# #346: cancel-first 生結果をノート入力へ載せる
# ---------------------------------------------------------------------------


def _step1_unfilled_input(
    *,
    executions: list[Execution] | None = None,
    plans: tuple[TradePlan, ...] = (),
) -> TradeNoteInput:
    return TradeNoteInput(
        decision_plan="LIMIT 159.82",
        executions=[] if executions is None else executions,
        now_jst=datetime.datetime(2026, 9, 3, 4, 30, tzinfo=JST),
        plans=plans,
        cancel_first=CancelFirstSnapshot(
            step1_cancelled=("1000501",),
            step2_cancelled=(),
            step3_closed=(),
            step3_residual=(),
        ),
    )


class TestCancelFirstSnapshotPrompt:
    """cancel-first の生 ID がプロンプトに届くこと（#346）。

    本番で ``step1_cancelled`` に注文 ID が入り約定 0 件だった日に、
    ノート入力に一次証拠が無く、LLM がティックデータ欠損を創作した。
    """

    def test_2026_09_02_step1_unfilled_reaches_prompt(self) -> None:
        """🔴 修正前は ID も確定事実もティック禁止も出ず、本テストは落ちる。"""
        system, user = _build_note_prompt(_step1_unfilled_input())
        assert "1000501" in user
        assert "確定事実" in user
        assert "ティックデータを取得していない" in system
        assert "例外（#346）" in system
        assert "## cancel-first 結果" in user

    def test_none_cancel_first_omits_section(self) -> None:
        """現行テストのまま（cancel_first=None）はセクションを出さない。"""
        now = datetime.datetime(2026, 9, 3, 4, 30, tzinfo=JST)
        system, user = _build_note_prompt(
            TradeNoteInput(decision_plan="p", executions=[], now_jst=now)
        )
        assert "## cancel-first 結果" not in user
        assert "例外（#346）" not in system
        assert "確定事実" not in system
        assert "断定できるわけではない" in user
        assert "ティックデータを取得していない" in system

    def test_empty_snapshot_has_no_confirmed_exception(self) -> None:
        """空スナップショットの日に「確定事実」例外が出ないこと。"""
        now = datetime.datetime(2026, 9, 3, 4, 30, tzinfo=JST)
        system, user = _build_note_prompt(
            TradeNoteInput(
                decision_plan="p",
                executions=[],
                now_jst=now,
                cancel_first=CancelFirstSnapshot(
                    step1_cancelled=(),
                    step2_cancelled=(),
                    step3_closed=(),
                    step3_residual=(),
                ),
            )
        )
        assert "## cancel-first 結果" in user
        assert "（対象なし）" in user
        assert "例外（#346）" not in system
        assert "確定事実" not in system
        assert "断定しない" in system
        assert "ティックデータを取得していない" in system

    def test_partial_fill_limits_unfilled_claim_to_cancelled_id(self) -> None:
        """Step1 非空かつ OPEN 約定がある日は、当日全体を約定なしと書かせない。"""
        open_fill = Execution(
            execution_id="ex-other",
            order_id="1000502",
            symbol="USD_JPY",
            side="BUY",
            price="159.50",
            size="100",
            timestamp="2026-09-02T12:00:00.000Z",
            settle_type="OPEN",
            position_id="pos-other",
        )
        system, user = _build_note_prompt(_step1_unfilled_input(executions=[open_fill]))
        assert "1000501" in user
        assert "例外（#346）" in system
        assert "キャンセルした注文 ID についてのみ未約定" in user
        assert "ティックデータを取得していない" in system

    def test_heading_includes_trade_date_when_plan_has_one(self) -> None:
        """friday ライブ日の見出しに対象取引日が付くこと（過去日は集約しない）。"""
        _, user = _build_note_prompt(
            _step1_unfilled_input(
                plans=(TradePlan(trade_date="2026-09-02(水)"),),
            )
        )
        assert "対象取引日 2026-09-02(水)" in user


# ---------------------------------------------------------------------------
# #351: trade_note 経路の SDK 再試行（529 overloaded → 200）
# ---------------------------------------------------------------------------

#: Anthropic Messages API の成功ボディ（#351 MockTransport）。
#: 必須フィールドのみ。出典: anthropic.types.message.Message（0.112.0）。
_ANTHROPIC_MESSAGE_OK: dict[str, Any] = {
    "id": "msg_test_351",
    "type": "message",
    "role": "assistant",
    "content": [
        {
            "type": "text",
            "text": "## 狙撃プラン\nok\n## 約定結果\nok\n## 自己評価\nok",
        }
    ],
    "model": "claude-haiku-4-5",
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 10, "output_tokens": 20},
}

#: 529 overloaded の公式エラー形。
#: 出典: https://docs.anthropic.com/en/api/errors （2026-09-05 参照）。
_ANTHROPIC_OVERLOADED: dict[str, Any] = {
    "type": "error",
    "error": {"type": "overloaded_error", "message": "Overloaded"},
}


def _generate_via_real_sdk(
    statuses: list[int], *, retry_after: str = "1"
) -> tuple[Any, list[int], Any]:
    """``generate_trade_note`` の実 ``Anthropic()`` 構築パスを MockTransport で通す。

    ``llm_client`` を渡さず、本番と同じ ``timeout`` / ``max_retries`` が効く経路を使う。
    ``http_client`` だけ差し替えて SDK の再試行実装を本物で動かす（#351）。
    戻り値は ``(result, seen_statuses, sleep_mock)``。
    """
    import anthropic
    from anthropic import Anthropic as RealAnthropic

    from backend.src.llm import trade_note as tn

    remaining = list(statuses)
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        status = remaining.pop(0) if remaining else 500
        seen.append(status)
        if status == 200:
            return httpx.Response(200, json=_ANTHROPIC_MESSAGE_OK)
        return httpx.Response(
            status,
            json=_ANTHROPIC_OVERLOADED,
            headers={"retry-after": retry_after},
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))

    def ctor(*args: Any, **kwargs: Any) -> Any:
        del args
        kwargs["http_client"] = http_client
        return RealAnthropic(**kwargs)

    inp = TradeNoteInput(
        decision_plan="t",
        executions=[],
        now_jst=datetime.datetime(2026, 6, 25, 4, 35, 0, tzinfo=JST),
    )
    try:
        with (
            patch.object(anthropic, "Anthropic", side_effect=ctor),
            patch(
                "backend.src.lambda_auth.get_anthropic_api_key",
                return_value="sk-test",
            ),
            # SDK の retry-after 待ちを実時間で踏まない（値の尊重は別テスト）。
            patch("anthropic._base_client.time.sleep") as sleep,
        ):
            result = tn.generate_trade_note(inp)
    finally:
        http_client.close()
    return result, seen, sleep


class TestSdkRetryOnOverloaded:
    """529 overloaded を SDK が 1 回だけ再試行すること（#351）。

    🔴 ``max_retries=0`` の現行 main では 529 の直後に例外へ倒れ、
    ``test_overloaded_529_then_200_is_retried`` は RED だった（PR コメント参照）。
    ``max_retries=1`` で 529→200 が通り GREEN になる。
    """

    def test_overloaded_529_then_200_is_retried(self) -> None:
        """🔴 修正前（max_retries=0）は 529 を再試行せず、本テストは落ちる。"""
        result, seen, _sleep = _generate_via_real_sdk([529, 200])
        assert seen == [529, 200], seen
        assert result.is_complete is True
        assert "狙撃プラン" in result.note_markdown
        assert "LLM生成失敗" not in result.note_markdown

    def test_exhausted_retries_still_fallback_incomplete(self) -> None:
        """再試行を使い切っても #309 の fallback（is_complete=False）に倒れること。"""
        result, seen, _sleep = _generate_via_real_sdk([529, 529])
        assert seen == [529, 529], seen
        assert result.is_complete is False
        assert "LLM生成失敗" in result.note_markdown
        assert result.input_tokens == 0
        assert result.output_tokens == 0

    def test_retry_after_is_honored_without_custom_cap(self) -> None:
        """SDK の ``retry-after`` をそのまま待つこと（独自 cap なし・ロック 3）。

        ヘッダ 45 秒は SDK 内部上限（60 秒）の内側。独自 cap を置くと
        この値が短く切り詰められるので、45 がそのまま sleep に届くことを固定する。
        出典: anthropic._base_client._calculate_retry_timeout（0.112.0）/
        https://platform.claude.com/docs/en/api/sdks/python#retries （2026-09-05 参照）。
        """
        _result, seen, sleep = _generate_via_real_sdk([529, 200], retry_after="45")
        assert seen == [529, 200], seen
        assert sleep.call_count == 1
        assert sleep.call_args.args[0] == 45.0
