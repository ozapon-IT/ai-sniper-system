import { NextResponse, type NextRequest } from "next/server";
import { dayExists } from "./lib/data-source";
import { isRequestAllowed } from "./lib/local-mode";
import { dateString } from "./lib/read-api";

const DAY_PATH = /^\/day\/([^/]+)\/?$/;

/** 手元専用モード（#393）のとき、実 bind が 127.0.0.1 でない・Host / Origin が loopback 以外の
 * リクエストを 403 で落とす。手元専用モードでなければ何もしない。
 * 出典: node_modules/next/dist/docs/01-app/03-api-reference/03-file-conventions/proxy.md
 * （next@16.4.6 同梱・`middleware` は `proxy` に改名・参照日 2026-11-23）。 */
export async function proxy(request: NextRequest): Promise<NextResponse> {
  const allowed = isRequestAllowed(process.env, {
    host: request.headers.get("host"),
    origin: request.headers.get("origin"),
  });
  if (!allowed) {
    return new NextResponse("Forbidden", { status: 403 });
  }
  // 🔴 記録のない日付はレンダリング前に 404 にする（#413）。`notFound()` はストリーミング開始後だと
  // 200 + noindex にしかならない（Cache Components は静的シェルで 200 を先に送る）。
  // 出典: node_modules/next/dist/docs/01-app/02-guides/streaming.md「The HTTP contract」
  // （next@16.4.6 同梱・参照日 2026-11-23）— 「request-time の 404 は Proxy で」と明記。
  const match = DAY_PATH.exec(request.nextUrl.pathname);
  if (match) {
    let date = match[1];
    try {
      date = decodeURIComponent(date);
    } catch {
      // 不正なパーセントエンコードは日付として不正 → 下で 404
    }
    const exists = dateString.safeParse(date).success ? await dayExists(date) : false;
    // exists === null（読めなかった）は画面側に任せる。false のときだけ 404。
    if (exists === false) {
      // 存在しないパスへ rewrite すると、ステータス 404 で app/not-found.tsx が描画される。
      return NextResponse.rewrite(new URL("/__not-found__", request.url), { status: 404 });
    }
  }
  return NextResponse.next();
}

// 静的アセット（_next/static・_next/image・favicon）は秘匿データを含まないため通さない。
// 出典: 同 proxy.md の「Matcher」節（否定先読みの例）。
export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
