import type { DashboardResponse, DayResponse, HistoryResponse } from "./schemas";

/** GET /history のクエリ（handler: limit 1〜100・既定 20 / before は YYYY-MM-DD で、その日より前）。 */
export interface HistoryQuery {
  limit?: number;
  before?: string;
}

/** 通信失敗・応答不正など、「記録なし」以外の取得失敗。 */
export class DataSourceError extends Error {
  /** true なら、呼び出し側は DataSource（とその AWS クライアント）を捨てて作り直すこと。
   * 認証情報の解決がハングした等、使い回すクライアントが回復しない失敗に付く（#393）。 */
  readonly resetClient: boolean;

  constructor(message: string, options?: { cause?: unknown; resetClient?: boolean }) {
    super(message, options);
    this.name = "DataSourceError";
    this.resetClient = options?.resetClient ?? false;
  }
}

/** 画面がデータの取得方法（モック / HTTP）を意識しないための境界。 */
export interface DashboardDataSource {
  getDashboard(): Promise<DashboardResponse>;
  getHistory(query?: HistoryQuery): Promise<HistoryResponse>;
  /** 記録なし（Read API の 404）は null。それ以外の失敗は DataSourceError を投げる。 */
  getDay(date: string): Promise<DayResponse | null>;
}
