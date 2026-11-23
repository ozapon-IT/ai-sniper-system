<!-- description: GMO FX API クライアント実装時の必須規約（署名・レートリミット・エンドポイント） -->
<!-- 適用対象: **/gmo_*.py, **/clients/**, **/api/** を編集する時 -->

# GMO FX API 実装ルール

参照: `docs/system_requirements.md` §1.2, §1.4, §7.2

## 署名（最頻出の事故ポイント）
- HMAC-SHA256 の **署名対象パスは `/v1/...`**（`/private` を含めない）。
- リクエスト URL は `/private/v1/...`。署名ユーティリティは「URL パス」と「署名パス」を **別引数** で受け取る。
- `API-TIMESTAMP` は HTTP リクエスト送出直前に取得して署名。HTTP 401 検知時は再署名を **1 回だけ** 行い、リトライ前に ≥1s 待機する（POST 1回/秒に合わせる）。
- 署名パスが `/v1/...` で始まることを検証する **ユニットテストを必須** にする。
- **`Accept` ヘッダ必須**（欠落は署名が正しくても ERR-5010 に化ける・E04 Runbook 参照）。

## エンドポイント
- 定数は `gmo_fx_endpoints.py` に集約（§1.4 の表が唯一の正）。パスのハードコード禁止。
- ベース URL: `https://forex-api.coin.z.com` / Private: `/private/v1/...` / Public: `/public/v1/...`。
- IFD-OCO 発注は `/private/v1/ifoOrder`。`ifdocoOrder` や `ifdOrder`（IFD 単体）と混同しない。

## レートリミット
- Private API: **POST 1回/秒・GET 6回/秒**。連続 POST の各呼び出し間に `sleep(>=1s)`。

## 🔴 HTTP 200 の業務エラー（「約定なし」化けの原因）
- GMO はエラーでも **HTTP 200 + ボディ `{"status": 1, "messages": [...]}`** を返す。
  `raise_for_status()` は通過するため、**必ず `raise_for_gmo_status(resp, context=...)` を通す**。
- これを怠ると API エラーが空リスト＝「データなし」に化ける。2026-08-05 に実際に発生し、
  約定・決済済みなのに `executions_count: 0` となり LLM が事実と反対のノートを生成した。
- 新しい GET ヘルパーを足すときは、HTTP 層とボディ status の**両方**を検査すること。

## 約定取得（executions / latestExecutions）
- `executions` は **`orderId` か `executionId` のみ**。`symbol` / `from` / `to` / `date` は**存在しない**。
- `latestExecutions` は **`symbol` 必須 + `count`（既定＝最大100）**。`orderId` は**存在しない**。
  取得範囲は「**直近1日分・最大100件**」。日付範囲で約定を引くエンドポイントは FX API に無い。
- IFD-OCO の**決済約定はエントリーと別 `orderId`**。エントリー ID だけでは実損益に到達できない。
  OPEN/CLOSE 双方を取れるのは `latestExecutions` のみ。
- 実現損益 = `lossGain` + `fee`（負値）+ `settledSwap`。`timestamp` は **UTC**。
- 🔴 **「次回 cron で再試行」がこの経路だけ成立しない**（#245）。手仕舞い cron は 1 日 1 回だが
  `latestExecutions` は直近 1 日しか遡れず、**翌日には明細を回収できない**（税務証跡の永久欠損）。
  そのため `_get_execution_list` が **invocation 内で**リトライする（最大3試行・1s/2s・時間予算20秒）。
  リトライして安全なのは読み取り専用の GET だから。**発注系 POST をこの経路に通さないこと**。
- `request_with_retry` は **401 再署名しかリトライしない**。5xx・タイムアウト・body `status != 0` は対象外。
- 詳細・出典: `docs/spikes/executions_api_spec.md`（付録B）。

## 未確定フィールドの扱い
- レスポンスの実フィールド名（例 `availableAmount`、IFD-OCO ボディ、`order_type` マッピング）が不明な場合は **推測で実装しない**。
- 参照系 GET で疎通 → 実レスポンスを確認 → `docs/spikes/`（付録B の実体）に記録 → それを基に実装。
