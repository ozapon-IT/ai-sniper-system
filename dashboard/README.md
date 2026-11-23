# dashboard

AI Sniper System のダッシュボード（Next.js App Router + TypeScript strict + Tailwind CSS）。
Node は repo ルートの `mise.toml`（24 系）に合わせる（`mise install`）。

## コマンド

```bash
npm ci               # 依存導入
npm run dev          # 開発サーバ（http://localhost:3000）
npm run lint         # ESLint
npm run typecheck    # next typegen && tsc --noEmit（route 型を生成してから型チェック）
npm test             # Vitest + React Testing Library（__tests__/）
npm run build        # 本番ビルド
npm run test:e2e     # Playwright（e2e/）。webServer が build + start を行う
```

`typecheck` が `next typegen` を先に走らせるのは、`LayoutProps` / `PageProps` などの route 型が
`next dev` / `next build` / `next typegen` でしか生成されないため
（出典: `node_modules/next/dist/docs/01-app/03-api-reference/06-cli/next.md`、next@16.4.6 同梱）。

## Playwright のブラウザ

通常は `npx playwright install chromium` で入る同梱ブラウザを使う。
同梱ブラウザの版が合わない環境では、環境変数 `PW_CHROMIUM_PATH` に Chromium の実行ファイルを指定できる（任意・未設定なら無視）。

## CI

`.github/workflows/ci.yml` の `dashboard` ジョブが lint / typecheck / test / build / e2e（Playwright・axe・375px）を実行する。

## 公開デモ（Vercel・合成データのみ・#394）

`MockDataSource`（日付・価格・損益はすべて合成）だけを公開する。**AWS の認証情報・`READ_API_FUNCTION_NAME`・`DASHBOARD_DATA_SOURCE` など実データ用の環境変数は Vercel に置かない**
（未設定なら `getDataSource()` は常に合成データになる）。

手作業（初回のみ）: Vercel のアカウント作成 → GitHub リポジトリを連携 → Project の **Root Directory を `dashboard`** に指定 → Environment Variables は空のままデプロイ。

デプロイ後の確認:

```bash
DEMO_URL=https://<project>.vercel.app npm run test:e2e:demo   # 3 画面 + HTML に AWS の痕跡が無いこと
npm run build && npm run check:bundle                          # 成果物（.next/static・.next/server）に ARN・キー・実リソース名が無いこと
```

## 手元専用モード（実データ・#393）

自分の PC からだけ read-api の実データ（当日・履歴・日次詳細）を見る。**HTTP は公開しない**（§1.6.1）。
Next のサーバー側が AWS SDK で read-api Lambda を IAM で直接 invoke し、ブラウザには HTML だけが渡る。

```bash
aws sso login --profile <SSOプロファイル>
# dashboard/.env.local（git 管理外）に書く:
#   READ_API_FUNCTION_NAME=<read-api の関数名>
#   AWS_REGION=<リージョン>
#   AWS_PROFILE=<SSOプロファイル>
npm run dev:local     # http://127.0.0.1:3000
```

- 認証は SSO プロファイル（`fromIni`）だけ。環境変数のアクセスキーは読まない。`sessionToken` の無い長期キーは拒否する。
- 次の場合は**起動を拒否**する（`instrumentation.ts` → `lib/local-mode.ts`。`next dev` はプロセスが終了、`next start` は待ち受けは続くが全リクエストが 500 になり実データは返さない）: dev サーバー以外（本番ビルド・`next start`・`NODE_ENV=development next start` を含む）、`npm run dev:local` 以外での起動（127.0.0.1 の宣言なし）、**実際の bind が 127.0.0.1 でない**（`.env.local` に宣言を書いて `next dev` を既定の 0.0.0.0 で起動した場合など）。実 bind と dev サーバー判定は Next の内部変数（`__NEXT_PRIVATE_ORIGIN` / `__NEXT_DEV_SERVER`）で確かめ、**見えなければ拒否**する。出典は同梱ソース（`next/dist/server/lib/start-server.js` L281・`next/dist/cli/next-dev.js` L407・参照日 2026-11-23）。`next` は exact pin のため、上げるときはここを再確認し、`npm run dev:local` で実機確認すること。
- 受信時にも `proxy.ts` が検査する。実 bind が 127.0.0.1 でなければ全拒否。Host が 127.0.0.1 / localhost 以外、Origin が Host と同じホスト・ポートでない場合は 403。Host / Origin の検査が防ぐのは**ブラウザ経由**（DNS rebinding・他オリジンのページ）だけで、LAN からの直接アクセスを塞ぐのは bind の検証。静的アセットは proxy を通らない。
- 1 回の呼び出しは認証情報の解決を含めて 15 秒で打ち切る。LambdaClient はプロセスで 1 回だけ作る。初期化の失敗はキャッシュせず、タイムアウトしたときはクライアントを `destroy()` して次のリクエストで作り直す（未解決の認証情報 Promise を SDK が使い回すため）。
- 関数名・ARN・認証情報はサーバー側だけ。エラー表示にも出さない（`npm run build` → `npm run check:bundle` で配布バンドルを検査。`.env.local` があれば関数名の値そのものも探し、無ければスキップしたと表示する）。

呼び出し元ロールに必要な権限は read-api の invoke だけ（Kill Switch など他の関数は含めない）:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "lambda:InvokeFunction",
      "Resource": "arn:aws:lambda:<region>:<account>:function:<read-api の関数名>"
    }
  ]
}
```

### 口座残高（equity）の扱い

日次詳細（`/day/{date}` → `DayDetail`）の「口座残高（判断時点・有効純資産）」は、判断バッチが判断ログ
（`logs/decision/{date}.json`）に書く `equity_jpy`（円・数値）を表示する（#411）。

- **書かれる日**: 判断バッチが GMO `assets` から有効純資産を取得できた日。口座取得後の終了パス
  （判断・見送り・ロック失敗など）には入る。ロット計算に使う値と同じで、ログに書く値を足しただけ
  （発注・ロット・バリデーションの挙動は変えない）。
- **書かれない日（画面は「未記録」）**: 休場・冪等チェック・ロック・口座取得失敗など、取得前に止まった日と、
  このキーを足す前の過去ログ。推測した値は入れず、遡って埋めない。
- **時点**: 20:30 の判断時点（その日の取引前）に取得した残高。同じ画面の手仕舞いの損益（金曜は直近 5 取引日の
  合計）は反映されていない。手仕舞い後の残高ではない。
- 型は数値（float）。zod の `equity: z.number().nullable()` はそのまま使う。
- `summary/dashboard.json` の `equityJpy` は常に 0 で、**画面は使わない**（`/dashboard` は使わない・#392・#114 のメモ）。
  この Issue では触らない。値を入れる必要が出たら別 Issue で decision 側の upsert を検討する。
- 公開デモ（合成データ）の残高は架空の値（`MockDataSource`）。実際の金額は公開物に出さない。
