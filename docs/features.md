# 機能一覧

実装済みの機能をカテゴリ別にまとめた一覧です。全体の構成は [architecture.md](architecture.md)、要件の詳細は [system_requirements.md](system_requirements.md) にあります。
表中の「§」は仕様書の節番号です。

**目次**

1. [意思決定エンジン](#1-意思決定エンジン3-4)
2. [発注前バリデーション](#2-発注前バリデーション34)
3. [LLM 連携](#3-llm-連携3-62-付録-a)
4. [外部データ](#4-外部データ31-33)
5. [バッチ](#5-バッチ2-61-62)
6. [GMO API 基盤](#6-gmo-api-基盤14-72)
7. [永続化](#7-永続化15-52)
8. [シークレット](#8-シークレット72)
9. [ダッシュボード](#9-ダッシュボード16-63)
10. [Kill Switch](#10-kill-switch16)
11. [監視・アラート](#11-監視アラート)
12. [インフラ（Terraform）](#12-インフラterraform11-16)
13. [安全ガードレール](#13-安全ガードレール)
14. [開発・検証基盤](#14-開発検証基盤71)
15. [ロンドン ORB デモ](#15-ロンドン-orb-デモ)

---

## 1. 意思決定エンジン（§3, §4）

Python が価格構造（ピボット・ATR）を計算し、その上で LLM が文脈を解釈するハイブリッド構成の中核です。**価格と数量は Python が決めます。**

| 機能 | 概要 | 実装 |
|---|---|---|
| ピボット | P / R1 / S1 / R2 / S2（§3.1） | [engine/pivot.py](../backend/src/engine/pivot.py) |
| ATR | 直近 14 日の日足の True Range の平均（§3.2） | [engine/atr.py](../backend/src/engine/atr.py) |
| 損切り・利確の計算 | 保有時間に換算した `ATR_h` を単位に計算。LLM には出力させない（§3.2） | [engine/enrich.py](../backend/src/engine/enrich.py) |
| ロット計算 | 許容リスクから数量を計算。100 通貨固定時も「固定しなければ何通貨か」を記録（§4.1） | [engine/position_sizing.py](../backend/src/engine/position_sizing.py) |
| レバレッジ上限・証拠金チェック | `V_safe = min(V_rounded, V_leverage_cap)`。証拠金が足りなければ NO_ENTRY（§4.2） | [engine/leverage_cap.py](../backend/src/engine/leverage_cap.py) |
| 直近の確定取引日 | 進行中の足を除外し、月曜は金曜の足を使う。年をまたぐ取得にも対応（§3.1） | [adapters/klines.py](../backend/src/adapters/klines.py) |

## 2. 発注前バリデーション（§3.4）

価格の並び、R:R の下限、tick への丸め、現在値からの乖離（上限・下限）、ロットの下限（100 通貨・100 単位）を検査します。
違反したら理由コードを付けて**発注しません**。

- 実装: [validation/pre_order.py](../backend/src/validation/pre_order.py)、見送り判定は [validation/day_skip_guards.py](../backend/src/validation/day_skip_guards.py)

## 3. LLM 連携（§3, §6.2, 付録 A）

| 機能 | 概要 | 実装 |
|---|---|---|
| 判断 | Anthropic API の構造化出力（`messages.parse` + Pydantic）で `TradeDecision` を取得。理由を先に書かせるフィールド順 | [llm/client.py](../backend/src/llm/client.py) / [llm/trade_decision.py](../backend/src/llm/trade_decision.py) |
| プロンプト | ピボット・ATR・4 時間足・当日の値動き・経済指標・米国マクロを組み立てる | [llm/prompt.py](../backend/src/llm/prompt.py) |
| トレードノート | 約定結果と前夜の判断を比べた振り返りを Markdown で生成。土曜は週次総括 | [llm/trade_note.py](../backend/src/llm/trade_note.py) |
| モック | テスト・シミュレーション用の固定出力（§7.1） | [llm/mock_llm.py](../backend/src/llm/mock_llm.py) |

## 4. 外部データ（§3.1, §3.3）

| 機能 | 概要 | 実装 |
|---|---|---|
| ローソク足・銘柄情報 | 日足・4 時間足・5 分足、tick size と最小ロット | [adapters/klines.py](../backend/src/adapters/klines.py) |
| 経済指標カレンダー（必須） | 保有時間内の USD/JPY 高インパクト指標。取得に失敗したら NO_ENTRY | [adapters/economic_calendar.py](../backend/src/adapters/economic_calendar.py) |
| 米国マクロ（補助） | 米 10 年債利回り・S&P 500 先物・日経 225 先物（yfinance）。取得に失敗しても続行 | [adapters/macro_context.py](../backend/src/adapters/macro_context.py) |

## 5. バッチ（§2, §6.1, §6.2）

| 機能 | 流れ | 実装 |
|---|---|---|
| 判断バッチ | 休場日の確認 → 二重起動の確認（有効注文・建玉・当日ロック）→ データ収集 → 見送り判定 → LLM → 損切り・利確・ロットの計算 → 検査 → IFD-OCO 発注 | [batch/decision_batch.py](../backend/src/batch/decision_batch.py) |
| 手仕舞いバッチ | 当日ロック → 市場の状態の確認 → キャンセル・ファースト → 約定明細の取得 → 証跡の保存 → ノート生成 → 集約 JSON の更新 | [batch/closeout_batch.py](../backend/src/batch/closeout_batch.py) |
| キャンセル・ファースト | 新規注文の取消 → 決済注文の取消 → 成行決済（10 件ずつ）→ 建玉一覧の差分で確認。POST の間は 1 秒以上あける | [batch/cancel_first.py](../backend/src/batch/cancel_first.py) |
| CLI | `python -m ai_sniper.batches decision\|closeout` でローカルから実行 | [ai_sniper/batches/__main__.py](../ai_sniper/batches/__main__.py) |

## 6. GMO API 基盤（§1.4, §7.2）

| 機能 | 概要 | 実装 |
|---|---|---|
| HTTP クライアント | Public / Private の GET・POST。発注の two-key interlock を持つ | [clients/gmo_client.py](../backend/src/clients/gmo_client.py) |
| HMAC-SHA256 署名 | 署名パス（`/v1/...`）と URL パス（`/private/v1/...`）を別引数で扱う。タイムスタンプは送信直前に取得 | [signer.py](../backend/src/signer.py) |
| 401 時の再署名 | 1 回だけ署名し直して再送 | [retry.py](../backend/src/retry.py) |
| 業務エラーの検出 | HTTP 200 でもボディの status がエラーなら例外にする | [gmo_errors.py](../backend/src/gmo_errors.py) |
| レートリミッタ | POST 1 回/秒・GET 6 回/秒 | [rate_limiter.py](../backend/src/rate_limiter.py) |
| エンドポイント定数 | パスを直接書くことを禁止し、署名パスへの変換もここで行う | [gmo_fx_endpoints.py](../backend/src/gmo_fx_endpoints.py) |
| Pydantic モデル | 注文・口座・建玉・約定・ローソク足 | [models.py](../backend/src/models.py) |

## 7. 永続化（§1.5, §5.2）

| 機能 | 概要 | 実装 |
|---|---|---|
| S3 | 判断・手仕舞いのログ、ノート、ダッシュボード用の集約 JSON、Kill Switch のログ。書き込みに失敗したら Slack にアラート | [persistence/s3_logger.py](../backend/src/persistence/s3_logger.py) |
| DynamoDB | 当日ロック（条件付き書き込みで原子的に獲得）、注文 ID、サマリ | [persistence/dynamodb_store.py](../backend/src/persistence/dynamodb_store.py) |

## 8. シークレット（§7.2）

Secrets Manager から GMO の資格情報・Anthropic の API キー・Slack Webhook を取得します。1 回の実行で 1 回だけ取得してキャッシュします。
アプリケーション層の Bearer トークンは使わず、呼び出し元の認証は IAM に任せています（§1.6.1）。

- 実装: [lambda_auth.py](../backend/src/lambda_auth.py)

## 9. ダッシュボード（§1.6, §6.3）

| 機能 | 概要 | 実装 |
|---|---|---|
| Read API Lambda | 当日・履歴・日別の詳細を返す。**IAM invoke 専用**で HTTP 公開しない。シークレットも DynamoDB の権限も持たない | [read_api/handler.py](../backend/src/read_api/handler.py) |
| ダッシュボード | Next.js の 3 画面（当日 / 履歴 / 日次詳細）。**公開デモ（Vercel）は合成データだけ**で、AWS の資格情報を置かない。実データは手元専用モード（127.0.0.1 限定・Read API を IAM で invoke）でだけ表示。存在しない日付は 404・CSP などのセキュリティヘッダー付き | [dashboard/](../dashboard/) |

ダッシュボードは運用に必須ではなく、日々の確認は S3 のログとトレードノートで行います。実データはローカルでだけ表示し、公開しているのは合成データのデモだけです。

## 10. Kill Switch（§1.6）

残っている注文の取消と全建玉の成行決済を行う、緊急停止用の Lambda です。
建玉が無ければ何もせずに終了するため、手仕舞いバッチと同時に動いても安全です。IAM invoke 専用で、スケジュールでは起動しません。
two-key interlock が閉じている環境では、invoke してもスタブとして動き、建玉は減りません。

- 実装: [kill_switch/handler.py](../backend/src/kill_switch/handler.py)

## 11. 監視・アラート

| 機能 | 概要 | 実装 |
|---|---|---|
| Slack 通知 | CloudWatch アラームを SNS 経由で受け取り、整形して Slack に送る | [alerting/slack_notifier.py](../backend/src/alerting/slack_notifier.py) |
| 手仕舞いの欠測監視 | 手仕舞いの 30 分後（火〜土 5:00 JST）に、その朝の手仕舞いログがあるかを確認。無ければ強いアラート。ログはあるがノートが未完成のとき、約定明細を取得できなかったときも、それぞれ区別して通知する | [monitor/closeout_watchdog.py](../backend/src/monitor/closeout_watchdog.py) |
| 構造化ログ | JSON 形式。タイムスタンプと LLM のトークン消費数を含む | [logging_config.py](../backend/src/logging_config.py) |

欠測監視のスケジュールは、取引のスケジュールとは**別の変数**で有効・無効を管理します。取引を止めても監視は止まらないようにするためです。

## 12. インフラ（Terraform・§1.1, §1.6）

Terraform モジュールは 2 つで、合計 **Lambda 10 関数・EventBridge Scheduler 9 本**です。

### `modules/serverless_app`（夜型スナイパー）
- **Lambda ×6**: decision_batch / closeout_batch / closeout_watchdog / read_api / kill_switch / slack_notifier
- **EventBridge Scheduler ×3**（TZ=`Asia/Tokyo`）: 判断 `cron(30 20 ? * MON-FRI *)`・手仕舞い `cron(30 4 ? * TUE-SAT *)`・監視 `cron(0 5 ? * TUE-SAT *)`。リトライ・DLQ・confused deputy 対策つき
- **S3**（バージョニング・暗号化・TLS 必須）、**DynamoDB**、**Secrets Manager + KMS**、**SNS + KMS**、**SQS（DLQ）**
- **CloudWatch アラーム**: スケジューラの取りこぼし・ターゲットのエラー・DLQ への到達・Lambda のエラー・アプリの ERROR ログ

### `modules/orb_demo`（ロンドン ORB デモ・§15）
- **Lambda ×4**: orb_session_open / orb_poller / orb_verify / orb_weekly
- **EventBridge Scheduler ×6**（TZ=`Europe/London`）
- **DynamoDB ×1**（夜型とは別のテーブル）
- 夜型のモジュールから渡すのは **Slack のシークレットだけ**です。GMO・Anthropic のシークレットは渡しません。

### 設定の管理
- スケジュールの有効・無効、`allow_live_orders`、`trading_phase` は、環境ごとの `*.auto.tfvars` に宣言してレビューできる差分として残します。コマンドラインの `-var` では渡しません（次の `apply` で元に戻ってしまうため）。
- モジュールの既定値は安全側（スケジュール無効・実発注なし）です。判断を有効にするなら手仕舞いと監視も有効でなければならない、という制約を validation で課しています。

実装: [modules/serverless_app](../infra/terraform/modules/serverless_app/) / [modules/orb_demo](../infra/terraform/modules/orb_demo/)

## 13. 安全ガードレール

詳細は [AGENTS.md](../AGENTS.md) にあります。

- **two-key interlock**: `ALLOW_LIVE_ORDERS=true` **かつ** `TRADING_PHASE` が `2` または `spike` のときだけ実発注。既定は安全側で、クライアント層とバッチ層の両方で判定
- 署名パスの非対称、レートリミット、キャンセル・ファーストの順序、休場カレンダー（[config/market_calendar.py](../config/market_calendar.py)）
- **時刻は JST で統一**。例外はロンドン ORB デモのスケジュールだけで、判定の基準がロンドン時刻のため `Europe/London` を明示しています。ログのタイムスタンプはどちらも UTC と JST を併記します

## 14. 開発・検証基盤（§7.1）

| 機能 | 概要 | 実装 |
|---|---|---|
| 連続シミュレーション | 発注をスタブにしたまま N 営業日分のバッチを流し、外部通信が 0 件であることを確認 | [scripts/run_local_simulation.py](../scripts/run_local_simulation.py) |
| テスト | 署名パス・計算・バッチ・LLM・永続化・統合テスト。外部サービスはすべてモック | [tests/](../tests/) |
| import 境界チェック | 夜型と ORB のコードが絡まないことを AST で検査 | [scripts/check_import_boundary.py](../scripts/check_import_boundary.py) |
| 公開リソースのチェック | Function URL・API Gateway などが Terraform に追加されたら失敗させる | [scripts/check_no_public_endpoints.py](../scripts/check_no_public_endpoints.py) |
| CI | ruff / mypy / 公開リソース / import 境界 / 署名パス / シミュレーション / pytest / ダッシュボードの lint・型・Vitest・ビルド・Playwright（E2E・axe）・バンドル検査 | [.github/workflows/ci.yml](../.github/workflows/ci.yml) |
| バックテスト | 過去データで判断から決済までを再現する。判断は Claude のサブエージェントが行う（`/backtest`）。Lambda には載せないローカル専用で、公開版では基盤ライブラリを同梱しない | [backtest/](../backtest/) |
| API スパイク | 実際のレスポンスを確認してから実装するための手順とスクリプト（記録は非公開） | [docs/spikes/README.md](spikes/README.md) |

## 15. ロンドン ORB デモ

夜型スナイパーとは**別の売買ルール**を、実発注なしで前向きに検証するサブシステムです。
LLM を使わない決定論ルール（ロンドン 8:00〜9:00 のレンジを抜けたらエントリー、損切りはレンジの反対側、利確なし、16:00 に手仕舞い）を、ticker をポーリングする仮想ブローカーで評価します。
GMO クライアント・S3 / DynamoDB・Slack・構造化ログ・Terraform の書き方は夜型と共通です。

| 機能 | 概要 | 実装 |
|---|---|---|
| セッション開始 | ロンドン 9:01 にレンジを確定し、仮想の OCO を置く（9:00 だと 8:59 の足が未確定のことがあるため） | [batch/orb_session_open.py](../backend/src/batch/orb_session_open.py) |
| ポーリング | 9:02、9:00〜15:55 の 5 分ごと、16:00 の 3 系統で約定を判定し、推定の滑りを記録 | [batch/orb_poller.py](../backend/src/batch/orb_poller.py) |
| 一致検証 | 16:30 に 1 分足のリプレイと突き合わせ、**1 件でも食い違えば自動停止** | [batch/orb_verify.py](../backend/src/batch/orb_verify.py) |
| 週次集計 | 金曜 17:00 に滑りの分布・稼働率・停止条件を集計 | [batch/orb_weekly.py](../backend/src/batch/orb_weekly.py) |
| 記録 | ノートを生成して S3 に保存 | [batch/orb_note_batch.py](../backend/src/batch/orb_note_batch.py) |
| 評価のコア | レンジ・約定の解決・セッションの状態・滑りの指標・リプレイなど | [orb/](../backend/src/orb/) |
| Public API アダプタ | `/public/v1/status`・`ticker`・`klines`（認証不要） | [adapters/orb_quotes.py](../backend/src/adapters/orb_quotes.py) |

### 夜型との分離
- **GMO のシークレットを Lambda に渡しません。** Public API だけで完結するため、two-key interlock より手前の段階で、構造的に発注できません。
- ただし、デプロイパッケージは夜型と共通なので、**ORB の Lambda にも夜型のコードは入っています**。発注できないことは、コードが無いことではなく、IAM・環境変数・S3 のプレフィックス・専用テーブル・専用のスケジュールグループを分けることで保証しています。
- 共通パッケージのため、夜型のコードだけを直しても ORB の Lambda が再デプロイされます。
- リポジトリは分けていません。GMO クライアントやエラー処理など、障害から得た知見が詰まった共通モジュールを複製すると、片方だけが直る事態になるためです。代わりに、次の import 境界を CI で検査しています。
  1. 夜型のコードは ORB を import しない
  2. ORB は `llm`・`engine`・`validation` を import しない（判断を持たない決定論ルールのため）
  3. ORB が使ってよい夜型のモジュールは許可リストで管理する
  4. ORB の評価コアは夜型の I/O 実装（`clients`・`persistence`）を import しない

### 評価の考え方
損益では優位性を判定しません。少ない件数の損益は少数の大きな値動きに左右され、判断材料にならないためです。
代わりに、**シグナルの一致率・稼働率・推定滑りの分布**の 3 つで、仮想執行の仕組みが正しく動くかを確かめます。

---

## 開発フェーズ

| フェーズ | 内容 | 状況 |
|---|---|---|
| Phase 0 | API の仕様確認（スパイク）と基盤づくり | 完了 |
| Phase 1 | モックでの運用（実発注なし） | 完了（1 週間連続でエラーなし） |
| Phase 2 | 最小ロット（100 通貨）での実取引 | 実発注・損切りの約定・Kill Switch の動作を確認済み |
| ロンドン ORB デモ | 実発注なしの前向きテスト | 観測を実施済み |
