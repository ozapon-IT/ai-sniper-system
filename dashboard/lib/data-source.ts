import { connection } from "next/server";
import {
  DataSourceError,
  INVOKE_TIMEOUT_MS,
  MockDataSource,
  type DashboardDataSource,
  type DayResponse,
  type HistoryQuery,
  type LambdaInvokeDataSource,
} from "./read-api";
import { assertLocalModeAllowed, isLocalMode } from "./local-mode";

/** 画面が使う DataSource の唯一の取得口。
 *
 * 既定は公開デモ用の MockDataSource（日付・価格・損益はすべて合成）。
 * `DASHBOARD_DATA_SOURCE=lambda`（手元専用モード・#393）のときだけ、read-api を IAM で invoke する
 * 実データにつなぐ。このとき本番ビルド・127.0.0.1 宣言なしの起動は拒否する（local-mode.ts）。 */
export function getDataSource(): DashboardDataSource {
  return selectDataSource({ awaitRequest: true });
}

/** proxy（レンダリング前・リクエスト時のみ走る）用。`connection()` を待たない以外は getDataSource と同じ分岐。 */
function getDataSourceForProxy(): DashboardDataSource {
  return selectDataSource({ awaitRequest: false });
}

function selectDataSource(options: { awaitRequest: boolean }): DashboardDataSource {
  if (!isLocalMode(process.env)) return new MockDataSource();
  assertLocalModeAllowed(process.env);
  return new LazyLambdaDataSource(options);
}

/** 日次詳細に記録があるか。proxy（レンダリング前）で 404 を返すための判定（#413）。
 * 読めなかったとき（DataSourceError 等）は `null`（不明）を返し、判断を画面側に委ねる（fail-open）。
 * proxy はリクエスト時にしか走らないため `connection()` は不要。 */
export async function dayExists(date: string): Promise<boolean | null> {
  try {
    return (await getDataSourceForProxy().getDay(date)) !== null;
  } catch {
    return null;
  }
}

/** 表示中のデータが公開デモ用の合成データか。フッターの注記に使う。 */
export function isSyntheticData(): boolean {
  return !isLocalMode(process.env);
}

/** LambdaClient / fromIni はプロセスで 1 回だけ作る（画面ごとに SSO の認証情報を取り直さない）。
 * 失敗した Promise はキャッシュしない（`aws sso login` 後に再起動なしで復帰できるように）。
 *
 * 🔴 タイムアウト（認証情報の解決がハング）したら捨てて `destroy()` し、次のリクエストで作り直す。
 * SDK の `memoizeIdentityProvider` は未解決の認証情報 Promise を共有し続け、HTTP 層に既定のタイムアウトも
 * 無い（@smithy/core・`DEFAULT_REQUEST_TIMEOUT = 0`）ため、使い回すと再起動まで全画面が失敗し続ける。 */
let cachedLambda: Promise<LambdaInvokeDataSource> | undefined;

/** `target` がまだキャッシュ中の DataSource なら、キャッシュからは**すぐ**外し、`destroy()` は遅らせる。
 * 同じクライアントで通信中の別リクエスト（read-api が遅いだけの正常な呼び出し）のソケットを
 * 切らないため、1 回の invoke の上限（INVOKE_TIMEOUT_MS）が過ぎてから後始末する。
 * 並行リクエストの二重破棄もここで避ける。 */
function discardLambda(target: Promise<LambdaInvokeDataSource>): void {
  if (cachedLambda !== target) return;
  cachedLambda = undefined;
  void target
    .then((ds) => {
      setTimeout(() => {
        try {
          ds.destroy();
        } catch {
          // 後始末の失敗は無視する（タイマー内の例外は uncaughtException になるため）
        }
      }, INVOKE_TIMEOUT_MS).unref();
    })
    .catch(() => {});
}

/** AWS SDK は手元専用モードのときだけ読み込む（合成データの経路に載せない）。 */
class LazyLambdaDataSource implements DashboardDataSource {
  /** `awaitRequest: false` は `connection()` を待たない（proxy 用。レンダリング外のため不要）。 */
  constructor(private readonly options: { awaitRequest: boolean }) {}

  private load(): Promise<LambdaInvokeDataSource> {
    // 🔴 組み立てるのは未キャッシュのときだけ。先に組み立ててから `??=` で捨てると、捨てた側の
    // .then も走ってクライアントが毎回作られ、失敗時は未処理の rejection になる。
    if (cachedLambda) return cachedLambda;
    const created: Promise<LambdaInvokeDataSource> = import("./read-api/lambda-config")
      .then(({ createLambdaDataSource, readLambdaModeConfig }) =>
        createLambdaDataSource(readLambdaModeConfig(process.env)),
      )
      .catch((error: unknown) => {
        if (cachedLambda === created) cachedLambda = undefined;
        throw error;
      });
    cachedLambda = created;
    return created;
  }

  private async call<T>(fn: (ds: LambdaInvokeDataSource) => Promise<T>): Promise<T> {
    // 🔴 リクエスト時まで待つ。`cacheComponents` 有効下で、リクエスト時 API を通らずに AWS SDK
    // （署名の Date.now()・リクエスト ID の crypto.randomUUID()）が走るとプリレンダー中の
    // 不安定値としてエラーになる。合成データの経路（MockDataSource）には入れない。
    // 出典: node_modules/next/dist/docs/01-app/03-api-reference/04-functions/connection.md
    // （next@16.4.6 同梱・参照日 2026-11-23）。
    if (this.options.awaitRequest) await connection();
    const loaded = this.load();
    try {
      return await fn(await loaded);
    } catch (error) {
      if (error instanceof DataSourceError && error.resetClient) discardLambda(loaded);
      throw error;
    }
  }

  getDashboard() {
    return this.call((ds) => ds.getDashboard());
  }

  getHistory(query?: HistoryQuery) {
    return this.call((ds) => ds.getHistory(query));
  }

  getDay(date: string): Promise<DayResponse | null> {
    return this.call((ds) => ds.getDay(date));
  }
}
