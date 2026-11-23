---
description: CodeRabbit の最新レビュー指摘を検証し、妥当なものだけ最小修正して再push
argument-hint: "[PR番号（省略時は現在ブランチのPR）]"
---

引数: ユーザーがコマンド名の直後にチャットで渡した PR 番号（Claude Code では自動展開される）。以下「上記引数」と呼ぶ。**空の場合は現在ブランチに紐づく PR を使う。**

PR #上記引数（省略時は現在ブランチに紐づく PR）の **CodeRabbit 最新レビュー** の指摘に対応する。

## 大原則
- **各指摘を現行コードに照合してから動く**。妥当なものだけ直し、無効・不要・nit は「理由を添えてスキップ」。
- **変更は最小限**。指摘を満たすために `AGENTS.md` の安全ガードレールを弱めない（安全 > レビュー対応）。
- **最新レビューのみ対象**。過去の解決済みコメントを蒸し返さない（ループ暴走防止）。

## 手順
1. **PR特定**: 上記引数があれば `#<上記引数>`。無ければ `gh pr view --json number,headRefName` で現在ブランチの PR を特定。
2. **指摘収集**（CodeRabbit = `coderabbitai[bot]` のみ対象）:
   - インライン: `gh api repos/{owner}/{repo}/pulls/<PR>/comments --paginate`
   - レビュー本文/サマリ: `gh api repos/{owner}/{repo}/pulls/<PR>/reviews --paginate`
   - 必要なら walkthrough: `gh api repos/{owner}/{repo}/issues/<PR>/comments --paginate`
   - **最新レビュー以降**の未解決コメントに絞る（既に `resolved` / 対応済みは除外）。
3. **トリアージ**: 各指摘を「妥当（直す）/ 無効・現状で対応済み / nit・見送り」に分類。判断は現行コードを実際に開いて確認する。
4. **修正**: 妥当な指摘のみ最小差分で修正。該当領域のルール（`.claude/rules/*.md`）と `AGENTS.md` ガードレールを必ず守る。
5. **検証**: 変更領域に応じて `ruff check` / `mypy` / `pytest`、dashboard 変更なら `npm run build`（`dashboard/`）(Node は mise の 24.x)。緑を確認。
6. **コミット & push**: PR ブランチへ。メッセージに `CodeRabbit 指摘対応` と要点を記載。
7. **サマリをコメント**: `gh pr comment <PR>` で「直した項目」「スキップした項目＋理由」を表で残す（トレース用）。

## やらないこと
- 全指摘の機械的な丸呑み（特に安全・お金に関わる箇所）。疑問があれば修正せず、コメントで論点を残して私に確認を求める。
- 無限ループ化（このコマンドは手動トリガー前提。push 後の再レビューに自動で再実行しない）。
