/** Read API のパス定数（§1.6 / AGENTS.md #7 の思想: パス直書き禁止）。
 *
 * 正の定義は `backend/src/read_api/handler.py` のルーティング。
 * 旧 SPA（`frontend/`・#394 で撤去）の endpoints.ts と同じ方針でここへ集約する。
 */
export const READ_API_PATHS = {
  /** 当日スナップショット。 */
  dashboard: "/dashboard",
  /** 履歴一覧（?limit=N&before=YYYY-MM-DD）。 */
  history: "/history",
  /** 日次詳細。date は YYYY-MM-DD。 */
  day: (date: string): string => `/day/${encodeURIComponent(date)}`,
} as const;
