---
description: sandbox 固定ワークツリーで実行・検証・スパイクを行う（使い捨て実験レーン）
argument-hint: "[テーマ 任意]"
---

`sandbox` 固定ワークツリーで実行・検証・スパイクを行う。research（読む）の姉妹レーン（動かす）。

## 手順
1. **ワークツリー特定**: `git worktree list --porcelain` から末尾が `/sandbox` のパスを得て `cd`。無ければ未セットアップ（`wt/sandbox` を `origin/main` から作成）を案内して停止。
2. **安全確認**: ダーティなら `git status -sb` を表示して停止。クリーンなら `git fetch origin --prune`。
3. **作業内容**:
   - **モック実走**: `SKIP_DYNAMODB=1 SKIP_S3=1` で Lambda ハンドラを直接叩く。
   - **LLM スモーク**: プロンプト変更や新モデルの動作確認。
   - **テスト実行**: `pytest` / `npm run dev` での動作検証（`.venv` はメインリポジトリ共用）。
   - **依存アップグレード試行**: パッケージ更新の動作確認。
   - **`/api-spike`（E04〜E06）の常設ホーム**: GMO FX API の実仕様確認スパイク。
4. **1行サマリ**: `sandbox / <テーマ or branch> / clean|dirty`。

## 厳守
- **原則 PR にしない**（良い結果は feature レーンへ移植）。
- **GMO は GET のみ**: 発注系 POST はスタブ。`TRADING_PHASE=spike` かつ `ALLOW_LIVE_ORDERS=True` の two-key が揃うときのみ実発注可（スパイク E06 限定）。
- `AGENTS.md` の安全ガードレール（署名パス・レートリミット・封じ込め）は sandbox でも不変。
- 他ワークツリーがチェックアウト中のブランチは checkout しない。
