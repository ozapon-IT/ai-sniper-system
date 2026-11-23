<!-- description: 発注・手仕舞い・ロット計算など資金が動く処理の安全規約 -->
<!-- 適用対象: **/batch/**, **/orders/**, **/strategy/**, **/risk/** を編集する時 -->

# トレード安全ルール

参照: `docs/system_requirements.md` §2, §3.2, §3.4, §4, §6.2

## 実発注の封じ込め
- Phase 1（モック）では発注系 POST を **実送信しない**（スタブ必須）。
- 実送信は二重条件 `ALLOW_LIVE_ORDERS=True` **かつ** `TRADING_PHASE`∈{`2`,`spike`} の両方を満たすときのみ（two-key interlock・既定は安全側）。発注直前にこの判定を必ず通す。単一フラグの設定ミスで送信されてはならない。
- `MVP_MODE`（**100通貨固定**・§4.2）は封じ込めとは別責務。封じ込め判定に MVP_MODE を使わない。

## 損切り・利確・ロットの決定権は Python（Ver 3.6）
- `stop_loss` / `take_profit` は **どちらも LLM 出力に含めない**。保有窓ボラ `ATR_h = ATR14 × √(HOLD_HOURS/24)`（`HOLD_HOURS` 既定 8）を単位に Python が算出（§3.2）:
  - `stop_loss = entry ∓ ATR_h × atr_multiplier`（mult は **0.6〜1.0・既定 0.7**、範外はクランプ）。tick 丸めは entry から遠ざける向き（LONG=切り下げ / SHORT=切り上げ）で `D_stoploss_pips` と発注価格を一致させる。
  - `take_profit = entry ± rr_target × |entry − stop_loss|`（rr_target は **1.0〜1.5・既定 1.0**、範外はクランプ）。TP は SL と対称に「entry から遠ざける向き」に丸める（LONG=切り上げ / SHORT=切り下げ。ROUND_DOWN 固定だと rr_target=1.0 で R:R が 1.0 を割り得る）。
  - LLM が出力するのは **`sl_profile`**（`standard`→0.7 / `wide`→0.8）と **`rr_target`** のみ。`atr_multiplier` は Python が `sl_profile` から導出（RD-12 #203）。
- ロット: `D_stoploss_pips = |entry - stop_loss| / 0.01` → `V_raw` → `V_safe = min(V_rounded, V_leverage_cap)` → 証拠金チェック（§4.1, 4.2）。

## 発注前バリデーション（§3.4、全項目必須）
- 価格序列: LONG は `SL < Entry < TP` / SHORT は `TP < Entry < SL`
- R:R 下限: `|TP - Entry| >= MIN_RR_RATIO * |Entry - SL|`（Ver 3.6 は construction で常時通過するが防御として残す）
- 現値乖離上限（Ver 3.6）: `|Entry − current_mid| <= MAX_ENTRY_DISTANCE_ATR × ATR14`（既定 0.3）
- tick 丸め・現値からの最小乖離・ロット下限（100通貨・100単位）
- 違反時は **発注せず** ログ記録（必要に応じ Slack 通知）。

## 手仕舞い（キャンセル・ファースト、順序厳守）
1. 市場状態確認（閉場ならアラート＋次回再試行）
   - 🔴 判定は **`GET /public/v1/status` の `data.status == "OPEN"`**（#207 (c)）。
     `CLOSE` / `MAINTENANCE` / 未知の値・取得失敗はすべて閉場扱い（fail-safe）。
   - 🔴 **参照系 GET の成否で開閉を判定しない**。閉場中でも `openPositions` /
     `activeOrders` は HTTP 200 + `status:0` を返す（2026-08-08 実測・
     付録B `docs/spikes/market_status_closed.md`）。代理判定では閉場を検知できない。
   - エンベロープの `status`（API 成否）と `data.status`（市場状態）は**同名で別物**。
   - 参照系 GET の健全性チェックは **API 障害の検知**として残す（両方満たしたときのみ続行）。
2. 残存新規注文 cancel（`cancelOrders` に `rootOrderIds`＝**親注文ID の number 配列・最大10件**。`orderId` というパラメータは存在しない）
3. 決済 OCO（子注文）cancel
   - ⚠️ **理由は ERR-423 ではない**。「残したまま close すると ERR-423 で決済不可」は
     **2026-08-11 の実発注スパイクで否定された**（`orderedSize` 満額のまま満額 close しても
     `status:0` / `EXECUTED` で通る・n=2。付録B `docs/spikes/ordered_size_err423_latency.md` 実験1）。
   - 🔴 **それでも順序は厳守**。根拠は ①Step3 が扱う状態を単純にする ②TP/SL の約定と
     `closeOrder` のレースを避ける ③再送ゲートの二重受付抑止、の 3 点。加えて実測スコープは
     **MARKET / 満額 / 単一建玉 / 開場中**のみで、分割決済・複数建玉・`LIMIT` 決済・閉場前後は
     未検証。守るコストは POST 1 回に対し、外して条件付きで拒否されると
     **SL/TP を消した建玉が翌 4:30 まで無防備に残る**（#207）。非対称性が大きい。
4. 成行 close（`closeOrder` に `settlePosition`＝`[{positionId, size}]` を**最大10件ずつ**。**トップレベル `size` は送らない**＝ERR-5214。**FX API に建玉一括決済（`closeBulkOrder`）は存在しない** — 暗号資産API側の名称・#230）
5. 全 POST 後に `openPositions` を**短ポーリング**（最大 3 回・1 秒間隔）で再取得し差分で決済確認
   - `closeOrder` の成功は**約定を意味しない**。`status` は `WAITING`（受付のみ）の**こともあれば**
     `EXECUTED`（同期で約定）の**こともある**（`MARKET` 決済で `EXECUTED` を実測・2026-08-11）。
     どちらが返るかに依存せず、**必ず差分で確認する**。
   - 🔴 **`activeOrders` と突き合わせて判断しない。** 参照系 GET は同一スナップショットを
     再利用し、`openPositions` と `activeOrders` は独立にドリフトするため、
     **決済済みの建玉が「残っている」ように見える**合成像が出る（付録B
     `docs/spikes/ordered_size_err423_latency.md` 実験2・n=3）。判定は単一エンドポイントで行う。
- すべての POST 間に `sleep(>=1s)`。
- 🔴 **Step3 の失敗は「無防備な建玉」を意味する**（#207）。Step2 で SL/TP を消した直後のため、
  決済に失敗した建玉は**損切りなしで翌 4:30 まで残る**。`closeout_batch` は例外を捕捉して
  監査ログ＋Slack 強アラート（建玉ID 明記）に落とし、`status` を `step3_failed` にする。
  約定明細は**取りに行ってから**失敗を返すこと（early return すると証跡が永久欠損する・#245）。
- 🔴 **`closeOrder` の再送は「確定拒否」のときだけ**（#207 L1）。GMO が応答を返して明示的に
  拒否した（`GmoApiError`）＝未受付が確実な場合のみ再送可。タイムアウト・接続断・HTTP エラーは
  「受付済みかもしれない」ので**再送しない**（`CloseOrderTransportError`）。
  再送前のゲート: 建玉が残っている **かつ** `orderedSize == 0`。`openPositions` が引けないなら
  **撃たない**（見えないのに撃たない）。判断軸は #138「未約定確実な失敗のみ」と同じ。
- 🔴 **`step3_residual_ids` は「短ポーリング後も消えなかった建玉」**（#207 L1）。
  `closeOrder` は受付（`status:WAITING`）であり即時約定ではないため、直後 1 回の GET では
  一時的な約定待ちと区別できない。ポーリング後に残る＝**本当に閉じられていない**＝強アラート対象。
- 🔴 **`run_cancel_first` 自体は Step3 失敗を例外のまま送出し続けること**。kill switch が
  `GmoApiError.message_codes` で fail-closed（HTTP 500）を判定しているため、結果フラグ化すると
  **緊急停止が「決済成功」を誤報する**（#136）。捕捉は `closeout_batch` 側でのみ行う。
- 公式仕様の出典は付録B `docs/spikes/cancel_first_official_spec.md`（<https://api.coin.z.com/fxdocs/>・2026-07-30 参照）。

## 冪等性
- 判断バッチ（PM 8:30・§6.1）: 発注前に `activeOrders` / `openPositions` / DynamoDB 当日ロックを確認し二重発注を防ぐ。
- 当日ロックのライフサイクル（§6.1, #138）: 成功・スタブ・未確定失敗（5xx/タイムアウト）はロック保持。**未約定確実な失敗（4xx / body `status != 0`）のみ解放**して当日再試行を許す。解放対象は `order_id=pending` のレコードに限る。
- 🔴 **ロック解放には IAM の `dynamodb:DeleteItem` が要る**（#406）。`release_lock` は条件付きの `delete_item` で、権限が無いと本番で AccessDenied になり、**解放だけが静かに効かない**（モックの単体テストは IAM を通らず検知できない）。判断・手仕舞いのロールに `DeleteItem` を付け（`iam.tf`・テーブル単位）、`tests/infra/test_dynamodb_iam.py` で 4 操作に固定する。解放の結果は「解放 / 条件不成立 / 障害」に区別し（`release_lock_outcome`）、**障害だけ Slack に通知**する。条件不成立（ロック不在・実 orderId 記録済み）は設計どおりの拒否で通知しない。条件式（`attribute_exists AND order_id = pending`）は変えない。
- 🔴 **判断済みマーカー**（#404・§6.1）: 見送りの日は当日ロックを取らないため、同じ日の 2 回目の起動が LLM を呼び直して発注まで進み得た。結論（`no_entry` 系）が出たら別レコード（`job_type=decision_concluded`）を書き、2 回目は冪等チェックより前に `decision_already_concluded` で見送る。
  - **書かないもの**: 回復し得る失敗（`skipped` の各理由・`calendar_degraded`・**LLM が判断を返せず安全側の NO_ENTRY に倒したとき**＝`LlmResult.fallback` が `call_failed` / `refusal` / `parse_failed`）。マーカーで当日を締めると再起動での回復を止める。`generate_decision` は失敗を `no_entry` の判断として返すので、LLM の見送りと区別するには `fallback` を見る（`input_tokens == 0` や `rationale` の文言で推測しない）。新しい `no_entry` の終了パスを足すときは、結論か回復し得る失敗かを決めて `_mark_decision_concluded` の要否を判断する。
  - マーカーは**当日ロックの代わりではない**。発注に進んだ日はロックが担う。ロックのライフサイクル（上）は変えない。
  - 読み取り失敗は fail-open（発注には当日ロックの獲得が別途要る）。書き込み失敗は判断結果を変えず error ログのみ。
  - やり直しは invoke の payload `{"force_redecide": true}`（`is True` のみ有効）で**マーカーの確認だけ**を飛ばす。冪等チェックと当日ロックは通す。DynamoDB テーブルを手で編集しない（当日ロックと同じテーブル）。
- 休場日（`config/market_holidays.json`）は早期 return。
