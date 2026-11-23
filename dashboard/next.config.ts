import type { NextConfig } from "next";

/** CSP（#413 → #415）。出典: node_modules/next/dist/docs/01-app/02-guides/content-security-policy.md
 * "Without Nonces" / "Subresource Integrity (Experimental)" / "Static vs Dynamic Rendering with CSP"
 * （next@16.4.6 同梱・参照日 2026-11-23）。
 *
 * 判断（#415）: 候補 A（"Without Nonces"）を強制で採る。
 * - nonce は動的レンダリング必須で、Cache Components の静的シェル（PPR）と両立しない（B/nonce は不採用）。
 * - SRI（experimental.sri）は実験的。`script-src 'self'` だけで Next のインラインスクリプト（RSC ペイロード等）が
 *   動くかも未検証のため見送る（再検討する場合は Report-Only で実測してから）。
 * - A の `'unsafe-inline'` は注入された inline スクリプトを止められない（XSS 防御としては弱い）。
 *   それでも効くのは「外部オリジンからのスクリプト/スタイル/画像/フォント読み込み」「connect-src による外部送信」
 *   「<base> 書き換え・フォーム送信先・埋め込み・プラグイン」。XSS の入口自体は小さい
 *   （ノートは react-markdown の skipHtml で表示し、dangerouslySetInnerHTML も使わない）。
 * - 事前に Report-Only で観測する案（C）は、違反の受け口（report-to）を作らない方針のため、
 *   ブラウザ上でしか観測できない。代わりに e2e（e2e/security.spec.ts）で本番ビルドの全主要画面の
 *   securitypolicyviolation / コンソールエラーが 0 であることを確認したうえで強制する。
 * - `'unsafe-eval'` は dev サーバー（`next dev`・手元専用モード `npm run dev:local`）のみ。本番には入れない。
 * - `upgrade-insecure-requests` は入れない。手元専用モードは http://127.0.0.1 で、付けると崩れ得る。
 *   本番（Vercel）は HTTPS 配信のため不要。
 * - style-src の `'unsafe-inline'`: Tailwind v4 / next/font / React のインライン style 属性のため必要。
 * - Vercel プレビューのツールバー（vercel.live）は許可していない（未検証）。プレビューでツールバーが出ないだけで
 *   画面は崩れない。本番は対象外。必要になったら preview 限定で足す。 */
const isDev = process.env.NODE_ENV === "development";
const CSP = [
  "default-src 'self'",
  `script-src 'self' 'unsafe-inline'${isDev ? " 'unsafe-eval'" : ""}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' blob: data:",
  "font-src 'self'",
  "connect-src 'self'",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "frame-ancestors 'none'",
].join("; ");

/** セキュリティ用レスポンスヘッダー（#413）。全パスに付ける（手元専用モードの 127.0.0.1 も同じ）。
 * 出典: node_modules/next/dist/docs/01-app/03-api-reference/05-config/01-next-config-js/headers.md
 * （next@16.4.6 同梱・参照日 2026-11-23）。 */
const SECURITY_HEADERS = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  {
    key: "Permissions-Policy",
    value: "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
  },
  {
    key: "Content-Security-Policy",
    value: CSP,
  },
];

const nextConfig: NextConfig = {
  /* config options here */
  cacheComponents: true,
  partialPrefetching: true,
  async headers() {
    return [{ source: "/:path*", headers: SECURITY_HEADERS }];
  },
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
