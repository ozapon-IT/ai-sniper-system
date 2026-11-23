# CURSOR.md

Cursor 用のプロジェクトメモリ。ルールの実体は `AGENTS.md`（プロジェクト共通の唯一の正・ツール非依存）に置き、ここから取り込む。
二重管理を避けるため、ルール本文は AGENTS.md 側を編集すること。

## プロジェクト規約・安全ガードレール（必読）

@AGENTS.md

## パス別の詳細ルール

Cursor は `.cursor/rules/*.mdc` の glob で該当ファイル編集時に自動スコープする。詳細の正は `.claude/rules/`。

- GMO FX API クライアント（`gmo_*.py` / `clients/` / `api/`）を触る時 → @.claude/rules/gmo-api.md
- 発注・手仕舞い・ロット計算（`batch/` / `orders/` / `strategy/` / `risk/`）を触る時 → @.claude/rules/trading-safety.md

## スラッシュコマンド

Agent 入力で `/` を入力し、以下から選択する（実体は `.cursor/commands/` → `.claude/commands/` への symlink）。

| コマンド | 用途 |
|----------|------|
| `/feature-1` `[Issue番号]` | 実装レーンA の準備・着手 |
| `/feature-2` `[Issue番号]` | 実装レーンB の準備・着手 |
| `/sandbox` `[テーマ]` | 実行・検証・スパイク |
| `/research` `[テーマ]` | 調査・仕様読み |
| `/main-sync` | main 最新化・全ワークツリー俯瞰 |
| `/cleanup` | マージ済みブランチ整理 |
| `/implement-issue` `<Issue番号>` | Issue を安全に実装 |
| `/api-spike` | GMO API 実仕様確認・付録B 記録 |
| `/address-review` `[PR番号]` | CodeRabbit 指摘対応 |
| `/backtest` `<開始日> <終了日> [チャンク]` | バックテスト実行 |


## サブエージェント（/backtest）

`/backtest` の判断フェーズは `backtest-decider` サブエージェントに委譲する。

- **Cursor**: Task ツールで `subagent_type: backtest-decider` を起動。起動前に `.cursor/skills/backtest-decider/SKILL.md`（正: `.claude/agents/backtest-decider.md`）を Read する。
- **Claude Code**: `.claude/agents/backtest-decider.md` のネイティブサブエージェントを起動。

## 補足

- Issue 番号は「E番号 + 7」（E01 = #8、E02 = #9 …）。最初の着手は `/implement-issue 8`。
- PR レビューは CodeRabbit（`.coderabbit.yaml`）。観点は AGENTS.md のガードレールに準拠。
- Claude Code 利用時は [CLAUDE.md](CLAUDE.md)。
