# CLAUDE.md

Claude Code 用のプロジェクトメモリ。ルールの実体は `AGENTS.md`（プロジェクト共通の唯一の正・ツール非依存）に置き、ここから取り込む。
二重管理を避けるため、ルール本文は AGENTS.md 側を編集すること。

## プロジェクト規約・安全ガードレール（必読）
@AGENTS.md

## パス別の詳細ルール
Claude Code は glob 自動スコープを持たないため、**以下の領域のファイルを編集する時は対応ルールを必ず参照すること**。

- GMO FX API クライアント（`gmo_*.py` / `clients/` / `api/`）を触る時 → @.claude/rules/gmo-api.md
- 発注・手仕舞い・ロット計算（`batch/` / `orders/` / `strategy/` / `risk/`）を触る時 → @.claude/rules/trading-safety.md

## スラッシュコマンド
- `/implement-issue <Issue番号>` — Issue を安全に実装（`.claude/commands/implement-issue.md`）
- `/api-spike` — GMO API の実仕様を安全に確認し付録Bに記録（`.claude/commands/api-spike.md`）

## 補足
- Issue 番号は「E番号 + 7」（E01 = #8、E02 = #9 …）。最初の着手は `/implement-issue 8`。
- PR レビューは CodeRabbit（`.coderabbit.yaml`）。観点は AGENTS.md のガードレールに準拠。
- Cursor 利用時は [CURSOR.md](CURSOR.md)。
