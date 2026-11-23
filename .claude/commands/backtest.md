# /backtest — サブエージェント判断でバックテストを実行する

LLM 判断を backtest-decider サブエージェント（本番同一モデル `claude-sonnet-4-6`・API キー不要）に
委譲してバックテストを回す（BT-4, 設計書 §2, §4.1, §4.2）。

引数: `<開始日 YYYY-MM-DD> <終了日 YYYY-MM-DD> [チャンクサイズ]`
（チャンクサイズ省略時は 15。`1` = 厳密ステートレスモード: 1 日 = 1 サブエージェント）

## 手順

1. **キャッシュ確認**: 対象期間の klines キャッシュが無ければ
   `python -m backtest.data --from <開始日> --to <終了日>` で先読みする（Public GET のみ）。
2. **prepare**: `python scripts/backtest.py prepare --from <開始日> --to <終了日>` を実行し、
   出力された `run_id` を控える（既定ブラインドモード。OOS 期間は `--unlock-oos` が要る —
   ユーザーの明示同意なしに付けないこと）。
3. **decide（チャンク逐次）**: `python scripts/backtest.py check --run-id <run_id>` で
   pending 一覧を取得し、**名前昇順**にチャンクサイズ件ずつに分割。各チャンクについて:
   - backtest-decider サブエージェントを起動し、run ディレクトリの絶対パスと
     担当ファイル名リスト（昇順）を渡す。
   - **チャンクは並列起動しない**（時系列順の逐次処理がルックアヘッド排除の要件）。
   - サブエージェント完了ごとに `check --run-id` を再実行し、order_violations があれば
     該当チャンクの decisions を削除して再実行する。

## サブエージェントの起動

### Claude Code

`../agents/backtest-decider.md` のネイティブサブエージェントを起動する。

### Cursor

各チャンクについて Task ツールを `subagent_type: backtest-decider` で起動する。
起動前に `../../.cursor/skills/backtest-decider/SKILL.md`（正: `../agents/backtest-decider.md`）を Read する。
run ディレクトリの絶対パスと担当ファイル名リスト（昇順）をプロンプトに含める。

## 手順（続き）

4. **simulate**: 全件完了（pending=0）を確認後、
   `python scripts/backtest.py run --run-id <run_id> --chunk-size <チャンクサイズ>` を実行
   （OOS の場合はここでも `--unlock-oos`）。
5. **報告**: 出力された report.md の 3 列比較・規律メタ（試行回数 / OOS 回数）・免責を
   そのままユーザーに提示する。絶対成績の解釈をしない（相対比較専用・設計書 §6.3）。

## 中断からの再開

decisions はファイルキャッシュなので、途中で止まっても同じ `run_id` に対して
手順 3 から再開すればよい（`check` が未処理分だけを列挙する）。

## 厳守

- サブエージェントに manifest.json / run_meta.json を読ませない（日付の目隠し・§6.3）。
- 判断の再生成でリトライ回数を稼がない（同一 run 内で decisions を書き直すのは
  order_violations の再実行のみ）。プロンプトや期間を変えた再試行は新しい run として
  prepare からやり直す（1 実験 = 1 run・§7-6）。
- 発注系 POST には一切関与しない（バックテストは Public GET とローカルファイルのみ）。
