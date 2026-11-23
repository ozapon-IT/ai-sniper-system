# AI Sniper System — Agent Rules

GMOコイン外国為替FX API 連携・完全サーバーレスの予約型 AI トレードシステム。
本ファイルは Cursor / AI エージェントが実装を進める際の最上位ルールである。

Cursor 利用時のインデックスは [CURSOR.md](CURSOR.md)。スラッシュコマンドは `.cursor/commands/`（`.claude/commands/` への symlink）。

## 最重要原則
- 仕様の唯一の正は `docs/system_requirements.md`（Ver.3.7）。実装判断は必ず該当 § を参照する。
- API の実フィールド名・レスポンス形式が未確定なら **推測で書かない**。スパイク（Issue E04〜E06）で実レスポンスを確認し、結果を `docs/spikes/`（付録B の実体、`docs/system_requirements.md` 付録B 参照）に追記してから実装する。
- 1 Issue = 1 ブランチ = 1 PR。PR 本文に対応 Issue 番号と参照 § を明記する。

## 🔴 安全ガードレール（違反禁止）
1. **実発注の封じ込め**: Phase 1（モック）では発注系 POST（`ifoOrder` / `closeOrder` / `cancelOrders`）を実送信しない。必ずスタブ経由。実送信は独立した2条件 `ALLOW_LIVE_ORDERS=True` **かつ** `TRADING_PHASE`∈{`2`,`spike`}（Phase 2 / E06 スパイク）を**両方**満たすときのみ許可する（two-key interlock。既定はいずれも安全側で、単一フラグの設定ミスでは送信されない）。`MVP_MODE`（100通貨固定。§4.2）は封じ込めとは別責務で、封じ込め判定に使わない。
2. **署名パス非対称**: HMAC-SHA256 署名のパスは `/v1/...`（`/private` を含めない）。リクエスト URL は `/private/v1/...`。両者を別引数で扱う（§7.2）。
3. **レートリミット**: POST 1回/秒・GET 6回/秒。連続 POST の各間に `sleep(>=1s)` を必ず挿入（§7.2, 6.2）。
4. **キャンセル・ファースト順序**: 手仕舞いは [新規注文 cancel → 決済 OCO cancel → 成行 close] の順。逆順禁止（§2, 6.2）。
5. **損切り・利確の決定権**: `stop_loss` と `take_profit` は LLM に出させず Python が決定論的に算出する（§3.2・Ver 3.6 / RD-12）。保有窓ボラ `ATR_h = ATR14 × √(HOLD_HOURS/24)`（`HOLD_HOURS` 既定 8）を単位に `stop_loss = entry ∓ ATR_h × atr_multiplier`（倍率 0.6〜1.0・LLM 経路は `sl_profile` マップで 0.7/0.8 のみ）、`take_profit = entry ± rr_target × |entry − stop_loss|`（rr_target 1.0〜1.5・既定は `RR_TARGET_DEFAULT`（1.0）・範外クランプ）。LLM は **`sl_profile` / `rr_target` のみ**出力する（`atr_multiplier` は Python 導出）。
6. **時刻は常に JST**: cron は `timezone: Asia/Tokyo` を明示。判断・発注は PM 8:30（月〜金）、手仕舞いは翌 AM 4:30（火〜土・単一 cron）。素の `today - 1` で前日足を取らない（直近確定取引日を遡って取得、§3.1）。**例外: ロンドンORBサブシステム**（`docs/ai-dev/london-orb-demo-design.md`）は判定基準そのものが Europe/London のため、当該サブシステムの cron に限り EventBridge Scheduler の `ScheduleExpressionTimezone: Europe/London` を明示して定義する（IANA タイムゾーン指定・DST 自動調整は AWS 公式ドキュメントで裏取り済み・参照日 2026-08-08）。ログ・記録のタイムスタンプは従来どおり UTC/JST を併記し、**素の JST 固定時刻でロンドン時刻を近似することを禁止**する。
7. **エンドポイントは定数のみ**: `gmo_fx_endpoints.py` の定数を使い、パスのハードコード禁止。ホストは `forex-api.coin.z.com`、IFD-OCO は `ifoOrder`（`ifdocoOrder` / `ifdOrder` ではない）。
8. **シークレット秘匿**: API キー等をログ・コミットに出さない。Secrets Manager / SSM 経由で取得し、1 invocation 内で 1 回だけ取得してキャッシュ。`.env` は commit 禁止。
9. **永続化は S3 / DynamoDB**: Lambda ローカルファイルに状態を残さない（§1.5）。書き込み失敗時は Slack アラート。
10. **発注前バリデーション必須**: 発注前に §3.4（価格序列・R:R 下限・tick 丸め・最小乖離・現値乖離上限 ≤ `MAX_ENTRY_DISTANCE_ATR × ATR14`（既定 0.3）・ロット下限100）を通す。違反時は発注せずログ記録。

## コーディング規約
- Python 3.x / 型注釈必須 / `ruff` + `black` / 構造化ログ（JSON、タイムスタンプ・LLM トークン消費数を含む。税務証跡 §5.2）。
- 各機能にユニットテスト。特に **署名パス一致テスト** は CI 必須（§7.1）。
- 環境変数: `RISK_PERCENT` / `MAX_LEVERAGE` / `MIN_RR_RATIO` / `MVP_MODE` / `HOLD_HOURS` / `RR_TARGET_DEFAULT` / `MAX_ENTRY_DISTANCE_ATR`（§4.2）/ `ALLOW_LIVE_ORDERS`・`TRADING_PHASE`（封じ込めの two-key・既定は安全側）。`.env.example` を維持。
- **外部依存の一次情報確認（推測禁止）**: 外部ライブラリ / SDK / クラウドサービス API を使うとき、**名前・シグネチャ・引数・レスポンス形式・制約・既定値・条件のセマンティクス**が非自明なら、記憶や推測で書かず**公式一次情報（ベンダー公式ドキュメント）で必ず裏取り**する。非自明な挙動に依存する実装は、**出典 URL（参照日付き）を PR 本文または該当コードのコメントに残す**。ドキュメントは更新されるため参照日を添える。※GMO FX API の未確定フィールドは従来どおりスパイク＋付録B（§api-spike）で確認する（裏取り先が「実レスポンス」か「公式ドキュメント」かの違い）。実例: EventBridge Scheduler 実行ロールの `aws:SourceArn` は schedule-group ARN 必須（公式 Important 明記）を読まず名前プレフィックス形式で書き、全 cron 発火が permanent 失敗した。

## フェーズ進行
- Phase 0（基盤スパイク E01〜E06）→ Phase 1（モック E07〜E17）→ Phase 2（実取引 E18〜E19）→ Dashboard → 横断。
- Phase 1 は「モックで 1 週間連続エラーなし」が完了条件（§7.1）。これを満たすまで Phase 2 の実発注に進まない。

## Definition of Done（全 Issue 共通）
- [ ] 参照 § の受け入れ条件を満たす
- [ ] テスト緑（署名パス・該当ロジック）
- [ ] 安全ガードレールに違反なし（特に 1・2・3）
- [ ] 新発見の API 仕様は `docs/spikes/`（付録B の実体）に反映
- [ ] 外部ライブラリ/SDK/クラウドAPI の非自明な挙動は公式一次情報で裏取り済み・出典 URL（参照日付き）を記載
- [ ] PR 本文に Issue 番号と参照 § を記載

## Cursor Cloud specific instructions

Cloud Agent の VM では起動時に update script（`python3 -m venv .venv` → `pip install` → `mise install` → `npm ci --prefix dashboard`）が自動実行され、依存は導入済み。標準コマンドは `README.md` / `Makefile` / `.github/workflows/ci.yml` を参照。以下は自明でない前提のみ。

🔴 **Python の依存は `make install` で入れること**（#322）。`pip install -e ".[dev]"` は
pyproject の `>=` から解決するため、**Lambda に載る依存を本番と違う版で入れてしまう**
（#320 では anthropic 1.0.0 の httpx2 移行で CI が全滅した）。`make install` は
`infra/terraform/lambda-{requirements,constraints}.txt` からランタイムを入れ、本番と同じ版に揃える。

⚠️ **update script が `pip install -e ".[dev]"` のままの環境では、`tests/infra/test_runtime_pins.py`
が落ちる。** これは誤検知ではなく「本番と違う SDK が入っている」という正しい検知である。
その VM で **`make install` を一度実行すれば解消する**（起動スクリプト自体はリポジトリ外のため、
リポジトリ側からは変更できない）。

- **Python は venv（`/workspace/.venv`）** に導入され、ログインシェルで `~/.bashrc` により自動 activate される（`python` / `pytest` / `ruff` / `mypy` がそのまま使える）。ローカルは Python **3.12** で動くが、CI と Lambda ランタイムのターゲットは **3.11**（`pyproject.toml` の `requires-python>=3.11`・ruff/mypy は py311）。
- **Node は mise 管理（24.18.0・`mise.toml`）** でログインシェルに自動 activate される。VM の既定 node は 22 系だが mise 側が優先される。ダッシュボードの作業は `dashboard/` で `npm run dev`（Next.js・:3000）/ `npm run build`。
- **テスト・lint・シミュレーションは外部サービス不要**: `pytest`（全モック・AWS/GMO/Anthropic 資格情報なしで緑）、`ruff check backend/ tests/`、`mypy backend/src/`、`MOCK_MODE=True python scripts/run_local_simulation.py --days 7`（§7.1 完了ゲート）はネットワーク・シークレットなしで通る。
- **`.env`** は `.env.example` からコピー済み。既定は安全側（`ALLOW_LIVE_ORDERS=false` / `TRADING_PHASE=1`）で発注系 POST は two-key interlock によりスタブ化される（AGENTS.md 安全ガードレール #1）。実 API を叩くバッチ（`python -m ai_sniper.batches decision|closeout`）や `/api-spike` には GMO/Anthropic の資格情報（Secrets もしくは `ALLOW_LOCAL_*=true` + env 値）が必要。
- **ダッシュボード（`dashboard/`）** は既定で合成データの `MockDataSource` を表示する（`DASHBOARD_DATA_SOURCE=lambda` の手元専用モードのみ実データ）。バックエンド無しで UI 確認が可能。
