---
description: ワークツリーのメタ整理と、マージ済みローカルブランチの安全な削除（固定4ツリーは残す）
---

並列開発で溜まる「マージ済みブランチ」「消えたワークツリーのメタ」を安全に掃除する。固定4ワークツリーは削除しない。

## 手順
1. **メイン**（`git worktree list` 先頭）へ `cd`。
2. `git worktree prune`（**実体が消えたワークツリーのメタデータのみ**整理）。
3. `git worktree list` で固定4つ（feature-1 / feature-2 / review / research）＋ main が健在か確認。
   欠けていれば `docs/ai-dev/README.md` の再作成手順を案内（**自動作成はしない**）。
4. **ローカル `main` を最新化して基準を揃える**: `git switch main`（メイン worktree。既に main ならスキップ）→ `git fetch --prune` → `git pull --ff-only origin main`。
   抽出（`git branch --merged`）と削除（`git branch -d`＝HEAD 基準で判定）の**基準をローカル `main` に統一**する（`origin/main` だけで抽出すると、ローカル main 遅れ時に `-d` が「未マージ」と見なして削除に失敗するため）。ff 不可なら強制せず通知のみ。
5. **マージ済みなのにレーンが居座る feature/fix 枝の回収**: 各固定WTについて、現在ブランチが `feature/*` または `fix/*` かつ
   `git -C <path> merge-base --is-ancestor HEAD main` が真かつ clean（`git -C <path> status --porcelain` が空）なら、
   ユーザー確認の上で `git -C <path> switch wt/<lane>` に戻す。
   （ff は feature-1・feature-2・main-sync に委ねる。ここでは switch-back のみ）
   これで当該枝が「使用中」から外れ、次の削除候補抽出に反映される。
   dirty なレーンは触らず警告のみ。
   <!-- squash-merge を導入すると --is-ancestor が効かなくなる（枝 tip が main の ancestor にならない）。
        将来 squash 化する場合は `gh pr list --state merged --head <branch>` での判定に切り替えること。
        現状は merge-commit 運用なので問題ない。 -->
6. **削除候補**を抽出: `git branch --merged main` から除外する: `main`、`wt/*`、現在ブランチ、**いずれかのワークツリーで使用中のブランチ**（`git worktree list` に出るもの）。
7. 候補を一覧で提示し、**ユーザー確認を取ってから** `git branch -d <名>`（`main` に完全に取り込まれたもののみ。`-D` は使わない）。
8. **ダーティなワークツリーがあれば警告のみ**（中身は触らない）。
9. **サマリ**: 残ワークツリー / 削除したブランチ / 要注意点（未マージで残したもの等）。

## 厳守
- 固定ワークツリーに対して `git worktree remove` を**しない**。
- 未マージブランチを `-D` で強制削除**しない**。確認なしで何も消さない。
- リモートブランチは触らない（削除はマージ時に GitHub 側で）。
- レーンの switch-back は確認必須・clean 限定。dirty なレーンや `wt/*` 以外への切替・force はしない。
