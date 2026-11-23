# Terraform（IaC）

AI Sniper System のサーバーレス構成を Terraform で定義しています。
全体像は [docs/architecture.md](../../docs/architecture.md)、要件は [docs/system_requirements.md](../../docs/system_requirements.md) の §1.1・§1.6・§7.2 を参照してください。

## ディレクトリ構成

```text
infra/terraform/
├── modules/
│   ├── serverless_app/          # 夜型スナイパー（Lambda・Scheduler・S3・DynamoDB・Secrets・監視）
│   └── orb_demo/                # ロンドン ORB デモ（Lambda・Scheduler・DynamoDB・監視）
├── environments/staging/        # 環境ごとの backend 設定とモジュールの呼び出し
│   ├── backend.tf               # tfstate の保存先（S3 + DynamoDB ロック）
│   ├── main.tf                  # 2 つのモジュールを呼び出す
│   └── phase2.auto.tfvars       # この環境の運用設定（スケジュール・実発注の可否）
├── scripts/build_lambda.sh      # Lambda のデプロイパッケージを作る
├── lambda-requirements.txt      # Lambda に同梱する依存
└── lambda-constraints.txt       # 依存のバージョン固定
```

環境はディレクトリで分けます。新しい環境は `environments/staging/` を雛形に複製します。

## 作成されるリソース

### `modules/serverless_app`（夜型スナイパー）

| 種類 | 内容 |
|---|---|
| Lambda ×6 | `decision-batch` / `closeout-batch` / `closeout-watchdog` / `read-api` / `kill-switch` / `slack-notifier`。デプロイパッケージは共通 |
| EventBridge Scheduler ×3 | 判断 `cron(30 20 ? * MON-FRI *)`・手仕舞い `cron(30 4 ? * TUE-SAT *)`・監視 `cron(0 5 ? * TUE-SAT *)`。タイムゾーンは `Asia/Tokyo`。リトライと DLQ（SQS）つき |
| S3 | ログ・ノート・集約 JSON。バージョニング・暗号化・パブリックアクセスの全ブロック・TLS 必須 |
| DynamoDB | 当日ロック・注文 ID・サマリ（PK `date` / SK `job_type`）。ポイントインタイムリカバリと暗号化 |
| Secrets Manager + KMS | GMO の資格情報・Slack Webhook の「箱」と、専用の KMS キー |
| SNS + CloudWatch | スケジューラの取りこぼし・ターゲットのエラー・DLQ・Lambda のエラー・アプリの ERROR ログのアラーム |
| IAM | Lambda ごとの最小権限ロール |

### `modules/orb_demo`（ロンドン ORB デモ）

| 種類 | 内容 |
|---|---|
| Lambda ×4 | `orb-session-open` / `orb-poller` / `orb-verify` / `orb-weekly` |
| EventBridge Scheduler ×6 | 専用のスケジュールグループ。タイムゾーンは `Europe/London` |
| DynamoDB ×1 | ORB のセッション用（夜型とは別のテーブル） |
| CloudWatch | スケジューラと Lambda のアラーム |

夜型のモジュールから ORB に渡すシークレットは **Slack だけ**です。GMO と Anthropic のシークレットは渡さないため、ORB の Lambda は構造的に発注できません。

> [!NOTE]
> `aws_cloudwatch_event_rule`（EventBridge のルール）はタイムゾーンを指定できず UTC 固定のため、使いません。JST やロンドン時刻での起動は EventBridge Scheduler で定義します。

## シークレットの扱い

- **シークレットの値は Terraform の入力にも tfstate にも書きません。** `sensitive = true` にしても state には平文で残るためです。Terraform が作るのは「箱」だけで、値は CLI やコンソールから別途登録します。
- Lambda には**シークレットの名前だけ**を環境変数で渡し、実行時に Secrets Manager から取得します（1 回の実行につき 1 回）。
- IAM は Lambda ごとに、必要なシークレットの**完全な ARN だけ**に `GetSecretValue` を許可します（ワイルドカードは使いません）。`read-api` にはシークレットの権限を一切与えません。
- 専用の KMS キーで暗号化したシークレットを読む Lambda には、Secrets Manager 経由に限定した `kms:Decrypt` を付けます。

## 運用設定と安全側の既定値

- **モジュールの既定値は安全側**です（スケジュールは無効、`allow_live_orders = false`、`trading_phase = "1"`）。新しい環境やテストは、何も設定しなければ発注しない状態から始まります。two-key interlock（[AGENTS.md](../../AGENTS.md)）と同じ考え方です。
- 環境ごとの設定は `environments/<環境>/phase2.auto.tfvars` に書きます。Terraform が自動で読み込むので、素の `terraform apply` でこの値が使われます。
- 判断・手仕舞い・監視のスケジュールは、それぞれ別の変数で有効・無効を切り替えます。判断を有効にするなら手仕舞いと監視も有効でなければならない、という制約をモジュールの validation で課しています。取引を止めても監視が止まらないようにするためです。
- 設定の値は `tests/infra/test_operating_posture.py` が固定しています。設定を変えるときは、tfvars とテストを同じ PR で更新します。運用の設定がレビューを経ずに変わらないようにするための仕組みです。

## デプロイ手順

前提:
- AWS の認証は IAM Identity Center（SSO）を使い、長期のアクセスキーは使いません。
- tfstate 用の S3 バケットと、ロック用の DynamoDB テーブルを事前に作っておきます。
- `your-*` で始まる名前（`backend.tf` の tfstate バケット・ロックテーブル、`main.tf` のログバケット・ジョブロックテーブル、シークレット名）を自分の環境の値に置き換えます。
- Terraform は mise で `1.15.7` を使います（[mise.toml](../../mise.toml)）。

```bash
aws sso login --profile <your-profile>
```

```bash
export AWS_PROFILE=<your-profile>
```

```bash
./infra/terraform/scripts/build_lambda.sh
```

```bash
cd infra/terraform/environments/staging
```

```bash
terraform init
```

```bash
terraform fmt -check -recursive && terraform validate
```

```bash
terraform plan
```

```bash
terraform apply
```

注意点:
- **運用設定を `-var` で渡さないでください。** `-var` は `*.auto.tfvars` より優先されるため、渡した値が一時的に効き、次の素の `apply` でファイルの値に戻ってしまいます。設定を変えるときは tfvars を編集します。
- **`build_lambda.sh` と `terraform apply` は同じチェックアウトで実行します。** `source_code_hash` はビルド済みの zip から計算されるため、別の作業ツリーで apply すると意図しないビルドがデプロイされます。
- **plan は必ず目で確認します。** スケジュールが無効になっていないか、`source_code_hash` が意図した版かを見てから apply します。

## 設計上の判断

- **HTTP で公開しない**: Lambda Function URL・API Gateway・CloudFront は作りません。呼び出しは EventBridge Scheduler と IAM 認証の invoke だけです（仕様書 §1.6.1）。CI の `scripts/check_no_public_endpoints.py` で追加を検出します。
- **スケジューラの実行ロール**: 信頼ポリシーに条件キーを付けるときは、公式ドキュメントで仕様を確認してから書きます。`aws:SourceArn` を推測の形式で書いたため、すべての起動が失敗したことがあります（[scheduler.tf](modules/serverless_app/scheduler.tf) のコメント参照）。
