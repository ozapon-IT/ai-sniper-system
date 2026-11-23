---
description: research 固定ワークツリーで調査・スパイク・仕様確認を行う（読み取り中心）
argument-hint: "[調べたいテーマ 任意]"
---

`research` 固定ワークツリーで安全に調査する。feature ブランチを汚さず、実装と並行して下調べ・設計検討・仕様読みができる。

## 手順
1. **ワークツリー特定**: `git worktree list --porcelain` から末尾が `/research` のパスを得て `cd`。無ければ未セットアップを案内。
2. **安全確認**: ダーティなら停止。クリーンなら **先に `git switch wt/research`**（別ブランチ上で誤って ff しないよう、対象ブランチを固定）。
   その後 `git fetch origin --prune` → `git merge --ff-only origin/main` で最新 main へ追従（ff 不可なら強制せず通知のみ）。
3. **調査**: 引数のテーマがあれば着手（コード探索・`docs/system_requirements.md` の該当 § 参照・必要なら WebSearch）。
   - GMO 実 API を叩く検証が必要になったら `/api-spike` を案内（口座状況・封じ込めに注意）。
4. **成果物**: 調査メモは「実装」ではないので、結論は Issue コメント or `docs/` に残す形を提案する。
5. **1行サマリ**: `research / テーマ / 主要な発見`。

## 厳守
- research では **実装ブランチを切らない・push しない**（コードを書きたくなったら feature-1/2 へ）。
- **read-only 原則**。実発注・本番副作用ゼロ。GMO へは GET のみ（POST 発注系は呼ばない）。
