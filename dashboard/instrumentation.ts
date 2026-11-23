import { assertLocalModeAllowed } from "./lib/local-mode";

/** サーバー起動時に一度だけ呼ばれる。手元専用モードの前提（本番ビルドでない・127.0.0.1 宣言）を
 * 満たさなければここで投げて起動を拒否する（#393）。
 * 出典: node_modules/next/dist/docs/01-app/03-api-reference/03-file-conventions/instrumentation.md
 * （`register` は新しいサーバーインスタンスの初期化時に一度呼ばれ、完了までリクエストを受けない・
 * next@16.4.6 同梱・参照日 2026-11-23）。 */
export function register(): void {
  assertLocalModeAllowed(process.env);
}
