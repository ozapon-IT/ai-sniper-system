/** 手元専用モード（#393・§1.6.1）の起動ガードと、受信リクエストの検査。
 *
 * 🔴 実データ（read-api）につなぐのは「自分の PC の 127.0.0.1 だけ」。HTTP の公開はしない。
 * このモジュールは副作用を持たない純関数だけで構成し、env / ヘッダは引数で受ける（テスト容易性）。
 *
 * 守るもの（多重防御）:
 *  1. 起動拒否: 本番ビルド・dev サーバー以外、`dev:local` 以外の起動、**実際の bind が 127.0.0.1 でない**。
 *  2. 受信拒否: 実際の bind が 127.0.0.1 でない、Host が loopback 以外、Origin が Host と不一致。
 *
 * 🔴 実際の bind は Next が listen 後に設定する内部変数 `__NEXT_PRIVATE_ORIGIN` で確かめる
 * （`-H 127.0.0.1` → `http://127.0.0.1:<port>`、`-H 0.0.0.0` / 既定 → `http://localhost:<port>`）。
 * dev サーバー判定の `__NEXT_DEV_SERVER` も同じく内部変数（dev のときだけ "1"。
 * `NODE_ENV=development next start` では NODE_ENV が development のまま動くため、NODE_ENV だけでは
 * 本番ビルドを弾けない）。どちらも公式ドキュメントが無く、依存する根拠は次の同梱ソースと実測:
 *  - `__NEXT_PRIVATE_ORIGIN`: node_modules/next/dist/server/lib/start-server.js L281 が listen 後に設定
 *    （`register` はその後に走る）
 *  - `__NEXT_DEV_SERVER`: node_modules/next/dist/cli/next-dev.js L407 が子プロセスの env に '1' を渡す
 *    （`next start` では付かない）
 *  いずれも next@16.4.6・参照日 2026-11-23（実測も同日）。next を上げるときは上記の場所を再確認する。
 * 拒否の見え方: `next dev` は register で投げてプロセスが終了、`next start` は "Failed to prepare
 * server" を出した後も待ち受けるが、全リクエストが 500 になり実データは返さない。
 * どちらも非公開の内部変数なので、**見えなければ拒否（fail-closed）**する。`next` は exact pin
 * （package.json）で、上げるときは tests/ と実機（README）で再確認すること。
 * Host / Origin 検査は「ブラウザ経由（DNS rebinding・他オリジン）」だけを防ぐ。Host はクライアントが
 * 自由に送れるため、LAN からの直接アクセスは防げない — それを塞ぐのは bind の検証である。
 */

/** 手元専用モード（Lambda Invoke）を選ぶ値。未設定・それ以外は公開デモ用の合成データ。 */
export const LAMBDA_MODE = "lambda";

/** ループバックとして許すホスト名（`localhost` は OS 側で 127.0.0.1 に解決される）。 */
const LOOPBACK_HOSTNAMES: ReadonlySet<string> = new Set(["127.0.0.1", "localhost"]);

export class LocalModeError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "LocalModeError";
  }
}

export type EnvLike = Readonly<Record<string, string | undefined>>;

export function isLocalMode(env: EnvLike): boolean {
  return env.DASHBOARD_DATA_SOURCE === LAMBDA_MODE;
}

/** Next の dev サーバーとして動いているか（`__NEXT_DEV_SERVER=1`。`next start` では付かない）。 */
export function isDevServer(env: EnvLike): boolean {
  return env.NODE_ENV !== "production" && env.__NEXT_DEV_SERVER === "1";
}

/** 実際の bind が 127.0.0.1 か。listen 後に Next が設定する内部変数で確かめる（未設定は false）。 */
export function isBoundToLoopback(env: EnvLike): boolean {
  const origin = env.__NEXT_PRIVATE_ORIGIN;
  if (!origin) return false;
  try {
    return new URL(origin).hostname === "127.0.0.1";
  } catch {
    return false;
  }
}

/** 手元専用モードで起動してよいか。ダメなら LocalModeError（fail-closed）。
 *
 * `DASHBOARD_BIND_HOST` は `scripts/dev-local.mjs` が `next dev -H 127.0.0.1` と対で設定する宣言（第 1 層）。
 * 宣言だけでは自己申告なので、実際の bind（`__NEXT_PRIVATE_ORIGIN`）と dev サーバー判定も確かめる。 */
export function assertLocalModeAllowed(env: EnvLike): void {
  if (!isLocalMode(env)) return;
  if (!isDevServer(env)) {
    throw new LocalModeError(
      "手元専用モード（DASHBOARD_DATA_SOURCE=lambda）は dev サーバー専用です（本番ビルド・`next start` は不可）。`npm run dev:local` を使ってください。",
    );
  }
  if (env.DASHBOARD_BIND_HOST !== "127.0.0.1") {
    throw new LocalModeError(
      "手元専用モードは `npm run dev:local` で起動してください（127.0.0.1 の宣言がありません）。",
    );
  }
  if (!isBoundToLoopback(env)) {
    throw new LocalModeError(
      "手元専用モードは実際に 127.0.0.1 へ bind したときだけ起動できます（`next dev` を `-H` なしや 0.0.0.0 で起動していませんか）。",
    );
  }
}

/** `host:port` / `[::1]:port` から hostname を取り出す。解釈できなければ null。 */
function hostnameOf(hostHeader: string): string | null {
  const trimmed = hostHeader.trim().toLowerCase();
  if (trimmed === "" || /[\s/@\\]/.test(trimmed)) return null;
  try {
    return new URL(`http://${trimmed}`).hostname;
  } catch {
    return null;
  }
}

/** Host ヘッダがループバックか（DNS rebinding・LAN IP 経由のアクセスを拒む）。 */
export function isAllowedHost(hostHeader: string | null | undefined): boolean {
  if (!hostHeader) return false;
  const hostname = hostnameOf(hostHeader);
  return hostname !== null && LOOPBACK_HOSTNAMES.has(hostname);
}

/** Origin ヘッダの検査。付かない（同一オリジンの GET 等）なら許可、付くなら Host と同じ
 * ホスト・ポートの loopback の http のみ（別ポートの loopback = 別アプリのページは拒否）。 */
export function isAllowedOrigin(
  origin: string | null | undefined,
  hostHeader: string | null | undefined,
): boolean {
  if (origin === null || origin === undefined) return true;
  if (!hostHeader) return false;
  try {
    const url = new URL(origin);
    return (
      url.protocol === "http:" &&
      LOOPBACK_HOSTNAMES.has(url.hostname) &&
      url.host === hostHeader.trim().toLowerCase()
    );
  } catch {
    return false; // "null" など解釈できない Origin は拒否
  }
}

/** 受信リクエストを通してよいか。手元専用モードでなければ常に通す（公開デモの挙動は変えない）。 */
export function isRequestAllowed(
  env: EnvLike,
  headers: { host?: string | null; origin?: string | null },
): boolean {
  if (!isLocalMode(env)) return true;
  // 起動時に弾いても、何らかの経路で通ったときの最後の砦（実 bind が loopback でなければ全拒否）。
  if (!isDevServer(env) || !isBoundToLoopback(env)) return false;
  return isAllowedHost(headers.host) && isAllowedOrigin(headers.origin, headers.host);
}
