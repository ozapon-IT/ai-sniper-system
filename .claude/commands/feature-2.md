---
description: feature-2 固定ワークツリーで開発を開始/再開する（移動・main同期・状態確認）
argument-hint: "[Issue番号 任意]"
---

`feature-2` 固定ワークツリーで開発を始める／続ける。並列開発のもう片側レーン（feature-1 と独立な Issue 用）。

## 手順
1. **ワークツリー特定**: `git worktree list --porcelain` から末尾が `/feature-2` のパスを得る。
   無ければ未セットアップ。`docs/ai-dev/README.md` の作成手順を案内して停止（このコマンドでは作らない）。
2. そのパスへ `cd`。**以降の git/実装はすべてこのパスで行う**（メインリポジトリを汚さない）。
3. **安全確認**: 作業ツリーがダーティなら `git status -sb` を表示して停止。勝手に stash/破棄しない。
4. `git fetch origin --prune`。
5. **現在ブランチで分岐**:
   - `feature/*` or `fix/*` 上 → **まずマージ済み判定**: `git merge-base --is-ancestor HEAD origin/main`（手順4の fetch 後）。
     <!-- squash-merge を導入すると --is-ancestor が効かなくなる（枝 tip が main の ancestor にならない）。
          将来 squash 化する場合は `gh pr list --state merged --head <branch>` での判定に切り替えること。
          現状は merge-commit 運用なので問題ない。 -->
     - **マージ済み かつ clean** → 完了レーン。自己修復: `git switch wt/feature-2` → `git merge --ff-only origin/main`。
       旧ブランチはローカルに残るので「`/cleanup` で削除できます」と1行案内し、
       そのまま下の「新規タスク開始」へフォールスルー（引数に Issue があれば着手）。
     - **マージ済み だが dirty** → 切替も破棄もしない。`git status -sb` を表示し
       「マージ済みブランチに未コミット変更あり。退避してから戻してください」と停止。
     - **未マージ** → 継続作業。`git status -sb` ＋ 直近3コミットを表示して準備完了。
       さらに `git rev-list HEAD..origin/main --count` で main の進行コミット数を取得し、1以上なら
       「`origin/main` が N コミット進んでいます（自動マージはしません）」と1行ヒントを表示する。
       **PR 後の CodeRabbit 指摘対応もこのレーンで `/address-review <PR>`**（review レーンは廃止）。
   - プレースホルダ `wt/feature-2` or `main` 上 → 新規タスク開始:
     - 引数に Issue 番号があれば、この場所で `/implement-issue <番号>` の流れに入る
       （**`origin/main` 起点**で `feature/E..` を切る。例: `git switch -c feature/E<n>-... origin/main`）。
     - 無ければ「どの Issue に着手するか」を尋ねる。
6. **依存の確認**（必要時のみ）: テスト実行なら Python はメインリポジトリの `.venv` を共用（`<main>/.venv/bin/pytest`）。dashboard を触るなら当ワークツリーで `npm ci` が要る。
7. **1行サマリ**: `feature-2 / <branch> / clean|dirty`。

## 厳守
- 他ワークツリーが現在チェックアウト中のブランチは checkout しない（git が拒否。別タスクにする）。
- **feature-1 と同じ Issue を二重で着手しない**（並列レーンの衝突防止）。
- `AGENTS.md` の安全ガードレール厳守。発注系 POST は Phase 1 スタブ。1 Issue = 1 ブランチ = 1 PR。
