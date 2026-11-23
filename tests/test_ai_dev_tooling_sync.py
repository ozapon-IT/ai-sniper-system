"""AI 開発ツール（Cursor / Claude Code）の設定同期を担保するテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


class TestCursorToolingSync:
    def test_cursor_md_exists_and_references_agents(self) -> None:
        cursor_md = (_ROOT / "CURSOR.md").read_text(encoding="utf-8")
        assert "AGENTS.md" in cursor_md
        assert ".cursor/commands/" in cursor_md

    def test_cursor_commands_symlink_points_to_claude_commands(self) -> None:
        commands_path = _ROOT / ".cursor" / "commands"
        assert commands_path.is_symlink()
        resolved = commands_path.resolve()
        expected = (_ROOT / ".claude" / "commands").resolve()
        assert resolved == expected

    def test_cursor_rules_exist(self) -> None:
        rules_dir = _ROOT / ".cursor" / "rules"
        assert (rules_dir / "gmo-api.mdc").is_file()
        assert (rules_dir / "trading-safety.mdc").is_file()

        gmo = (rules_dir / "gmo-api.mdc").read_text(encoding="utf-8")
        trading = (rules_dir / "trading-safety.mdc").read_text(encoding="utf-8")
        assert "globs:" in gmo
        assert "../../.claude/rules/gmo-api.md" in gmo
        assert (rules_dir / "../../.claude/rules/gmo-api.md").resolve().is_file()
        assert "globs:" in trading
        assert "../../.claude/rules/trading-safety.md" in trading
        assert (rules_dir / "../../.claude/rules/trading-safety.md").resolve().is_file()

    def test_backtest_decider_skill_matches_agent_model(self) -> None:
        # backtest.subagent.BACKTEST_DECIDER_MODEL は DEFAULT_DECISION_MODEL をそのまま代入した値。
        # backtest は fxlab（プライベート）に依存し CI では入らないことがあるため、本番側を直接見る
        from backend.src.llm.client import DEFAULT_DECISION_MODEL as BACKTEST_DECIDER_MODEL

        agent_md = (_ROOT / ".claude" / "agents" / "backtest-decider.md").read_text(
            encoding="utf-8"
        )
        skill_md = (_ROOT / ".cursor" / "skills" / "backtest-decider" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        assert f"model: {BACKTEST_DECIDER_MODEL}" in agent_md
        assert BACKTEST_DECIDER_MODEL in skill_md
        assert "stop_loss・take_profit・ロット数は絶対に出力しない" in agent_md
        assert "stop_loss・take_profit・ロット数は絶対に出力しない" in skill_md
        assert "rr_target" in agent_md
        assert "rr_target" in skill_md
        assert "## 厳守事項" in skill_md, "「厳守事項」セクションが見つかりません"
        strict_section = skill_md.split("## 厳守事項", 1)[1].split("## 完了報告", 1)[0]
        assert "出力できるフィールド" in strict_section, "許可フィールドの記述が見つかりません"
        fields_block = strict_section.split("出力できるフィールド", 1)[1]
        # 許可フィールドの箇条書き全体（次の `- **` 箇条書きまで）を対象にする
        allowed_bullet = fields_block.split("\n- **", 1)[0]
        assert "take_profit" not in allowed_bullet, "take_profit が許可フィールドに含まれています"
        assert "rr_target" in allowed_bullet, "rr_target が許可フィールドに含まれていません"

    def test_claude_commands_cover_expected_set(self) -> None:
        commands_dir = _ROOT / ".claude" / "commands"
        expected = {
            "implement-issue.md",
            "api-spike.md",
            "address-review.md",
            "backtest.md",
            "feature-1.md",
            "feature-2.md",
            "sandbox.md",
            "research.md",
            "main-sync.md",
            "cleanup.md",
        }
        actual = {p.name for p in commands_dir.glob("*.md")}
        assert expected <= actual

    @pytest.mark.parametrize(
        "command_file",
        [
            "implement-issue.md",
            "api-spike.md",
            "address-review.md",
        ],
    )
    def test_commands_use_tool_neutral_args(self, command_file: str) -> None:
        content = (_ROOT / ".claude" / "commands" / command_file).read_text(encoding="utf-8")
        assert "$ARGUMENTS" not in content
        assert "上記引数" in content
