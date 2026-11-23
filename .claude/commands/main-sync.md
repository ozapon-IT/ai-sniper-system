---
description: main ハブを最新化し、各固定ワークツリーの状態を一覧する
---

メインリポジトリ（`ai-sniper-system` = `main` ハブ）を最新化し、並列ワークツリー全体の状態を俯瞰する。

## 手順
1. **メイン特定**: `git worktree list` の先頭（`/ai-sniper-system`、ブランチ `main`）へ `cd`。
2. **安全確認**: ダーティなら `git status -sb` を表示して停止（勝手に触らない）。
3. `git checkout main`（既に main ならスキップ）。
4. `git pull --ff-only origin main` と `git fetch --prune`。ff 不可・コンフリクト時は **強制せず通知のみ**。
5. **マージ済み残ブランチ**を一覧（削除はしない。`/cleanup` に委ねる）:
   `git branch --merged main` から `main` / `wt/*` / 各ワークツリーで使用中のブランチを除外して表示。
6. **各固定ワークツリーの状態**を一覧: `git worktree list` ＋ 各ツリーの現在ブランチ・clean/dirty・origin との ahead/behind。
7. **ガード付き ff 同期**: 各固定ワークツリー（feature-1 / feature-2 / sandbox / research）について、以下の **4条件を全て満たす場合のみ** `git -C <path> merge --ff-only origin/main` を実行する。
   - **placeholder ブランチ**: 現在ブランチ名が `wt/*` に一致すること（`feature/*` / `fix/*` 等の作業ブランチは **対象外**。作業ブランチは所有者が管理し、マージ済みは `/cleanup` で削除する）。
   - **clean**: `git -C <path> status --porcelain` が空行（未コミット変更なし）
   - **behind > 0**: origin/main より遅れている（追従の意味があること）
   - **ahead == 0**: ローカル独自コミットが無い（fast-forward 可能な形状であること）
   - 上記を **1つでも満たさない**ツリーは **自動スキップ**。理由を1行で表示する。
     スキップ対象が `feature/*` ブランチで、かつ `git -C <path> merge-base --is-ancestor HEAD origin/main` が真なら
     「`<lane>` はマージ済み `feature/E..` 上です。`/<lane>` で `wt/<lane>` に戻すと追従＋次タスクへ進めます」と1行案内する。
     <!-- squash-merge を導入すると --is-ancestor が効かなくなる。将来 squash 化する場合は gh pr list での判定に切り替えること。 -->
     ミューテーション（switch / ff / delete）は行わない（feature-1・feature-2 コマンドに委ねる）。
   - `--ff-only` のみ使用。`--force` / merge コミット / stash / 破棄は**絶対にしない**。
8. **サマリ**: main の最新コミット / 追従状況 / feature-1・feature-2・sandbox・research それぞれの同期結果（追従済み / スキップ＋理由）/ 注意点（ダーティなツリー等）。

## 厳守
- **ff-only**。履歴を書き換えない・force しない。
- 3条件（clean / behind>0 / ahead==0）を全て満たさないツリーは**触らない**。スキップして理由を表示するだけ。
