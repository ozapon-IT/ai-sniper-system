import {
  DataSourceError,
  type DashboardDataSource,
  type HistoryQuery,
} from "./data-source";
import {
  dashboardResponseSchema,
  dateString,
  dayResponseSchema,
  historyResponseSchema,
  type DashboardResponse,
  type DayResponse,
  type HistorySummary,
} from "./schemas";

/** 公開デモ用の合成データ。
 *
 * 🔴 日付・価格・損益・ID はすべて架空。実際の取引ログ由来の値を使わない。
 * 日付は実運用の期間と重ならない 2099 年（実在しない遠い未来）に固定している。
 */
// 判断・手仕舞いは平日だけ動くので、日付はすべて平日（2099-01-06 火〜01-12 月）。
// 休場日は判断ログも手仕舞いログも残らず /history に出ない（/day は 404）ので、合成データに休場日は無い。
const SYNTHETIC_DAYS: readonly HistorySummary[] = [
  // 直近の取引日: 判断済みだが、手仕舞い（翌 4:30 JST）はまだ動いていない
  { date: "2099-01-12", direction: "LONG", execution_required: true, status: "stubbed", rationale_summary: "［架空］押し目買い。", has_note: false },
  { date: "2099-01-09", direction: "SHORT", execution_required: true, status: "stubbed", rationale_summary: "［架空］上値が重く戻り売り。", has_note: true },
  { date: "2099-01-08", direction: "NO_ENTRY", execution_required: false, status: "no_entry", rationale_summary: "［架空］値動きが乏しく見送り。", has_note: true },
  { date: "2099-01-07", direction: "LONG", execution_required: true, status: "stubbed", rationale_summary: "［架空］押し目買い。", has_note: true },
  // 取引日に発注前の確認で中止した日（判断ログは `_skip` の最小形で方向なし。手仕舞いは動く）
  { date: "2099-01-06", direction: null, execution_required: null, status: "skipped", rationale_summary: "", has_note: true },
];

/** 金曜（週次集計の手仕舞い）として扱う合成日。2099-01-09 は実際に金曜。 */
const SYNTHETIC_FRIDAY = "2099-01-09";
/** 架空の口座残高（円）。実際の金額を連想させない並びの値にする（#411）。 */
const SYNTHETIC_EQUITY_JPY = 123456;

/** 判断済みで手仕舞いがまだ動いていない合成日（closeout は null になる）。 */
const SYNTHETIC_PENDING_CLOSEOUT = "2099-01-12";

/** 金曜のノートには Python が付ける確定表（Markdown テーブル）が入る（backend/src/llm/trade_note.py）。
 * 幅の広い表の表示（375px で表の中だけスクロール）をデモで確認できるよう、同じ列構成にしている。 */
const FRIDAY_NOTE = [
  "## ［架空］週次総括",
  "",
  "デモ用の合成ノートです。数値は末尾の確定表を参照してください。",
  "",
  "## 計画値（取引日ごと・判断バッチの確定値）",
  "",
  "| 取引日 | 方向 | エントリー | SL | TP | SL幅 | TP幅 |",
  "|---|---|---|---|---|---|---|",
  "| 2099-01-06 | 判断ログなし | - | - | - | - | - |",
  "| 2099-01-07 | LONG LIMIT | 100.5 | 100.1 | 100.9 | 40 pips | 40 pips |",
  "| 2099-01-08 | NO_ENTRY | - | - | - | - | - |",
  "| 2099-01-09 | SHORT LIMIT | 100.5 | 100.9 | 100.1 | 40 pips | 40 pips |",
].join("\n");

// 実データの /day: plan は判断ログのプラン系キー（発注に至った日のみ）、ノートは notes/{date}.json
// の有無（has_note と一致）。sniper_logs は判断・手仕舞いログに書かれず常に null（#397）。
// equity は判断ログに口座取得後だけ書かれる（#411）。
//
// 手仕舞いバッチは取引日なら見送り日・スキップ日も含め毎回、手仕舞いログを保存する
// （約定 0 件・損益 "0" になる）。closeout が null になるのは、手仕舞いがまだ動いていない
// 直近の取引日（20:30〜翌 4:30）か、手仕舞いログが保存されなかった日。
// 金曜は executions_count / pnl / exit_reasons の 3 項目すべてが直近 5 取引日分の集計（scope="week"）。
function closeoutFor(summary: HistorySummary): DayResponse["closeout"] {
  if (summary.date === SYNTHETIC_PENDING_CLOSEOUT) return null;
  const friday = summary.date === SYNTHETIC_FRIDAY;
  const noTrade = summary.execution_required !== true && !friday;
  return {
    status: "ok",
    mode: friday ? "friday" : "weekday",
    executions_fetched: true,
    executions_count: friday ? 8 : noTrade ? 0 : 2,
    pnl: {
      realized_total: friday ? "310" : noTrade ? "0" : "-120",
      loss_gain_total: friday ? "350" : noTrade ? "0" : "-100",
      fee_total: "0",
      settled_swap_total: friday ? "-40" : noTrade ? "0" : "-20",
      close_count: friday ? 4 : noTrade ? 0 : 1,
    },
    scope: friday ? "week" : "day",
    exit_reasons: friday
      ? { time_stop: 1, take_profit: 2, stop_loss: 1 }
      : noTrade
        ? {}
        : { time_stop: 1 },
    note_complete: true,
  };
}

function dayDetail(summary: HistorySummary): DayResponse {
  return {
    date: summary.date,
    plan: summary.execution_required
      ? {
          order_type: "LIMIT",
          sl_profile: "standard",
          rr_target: 1.0,
          entry_price: 100.5,
          take_profit: 100.9,
          stop_loss: 100.1,
          lot: 100,
        }
      : null,
    direction: summary.direction,
    status: summary.status,
    sniper_logs: null,
    closeout: closeoutFor(summary),
    trade_note: summary.has_note
      ? summary.date === SYNTHETIC_FRIDAY
        ? FRIDAY_NOTE
        : "## ［架空］振り返り\n\nデモ用の合成ノートです。"
      : null,
    // 架空の残高（公開デモは合成データのみ。実際の金額を連想させない値）。
    // 判断が無い日（direction が null の skip 日）は取得前に止まった想定で null → 「未記録」。
    equity: summary.direction === null ? null : SYNTHETIC_EQUITY_JPY,
    lot: summary.execution_required ? 100 : null,
  };
}

/** API 無しで UI を動かすための DataSource。返す値は本物のスキーマで検証してから返す。 */
export class MockDataSource implements DashboardDataSource {
  async getDashboard(): Promise<DashboardResponse> {
    return dashboardResponseSchema.parse({
      // 実データでは summary/dashboard.json に equityJpy 既定値 0 / plan・lot 未設定 / logs [] しか
      // 書かれず、/dashboard は equity 0・today_plan null・lot null・sniper_logs [] になる。
      equity: 0,
      today_plan: null,
      trade_note: "## ［架空］本日の振り返り\n\nデモ用の合成ノートです。",
      sniper_logs: [],
      status: "idle",
      last_updated: "2099-01-08T20:30:00+09:00",
      lot: null,
    });
  }

  async getHistory(query: HistoryQuery = {}) {
    const { limit = 20, before } = query;
    if (!Number.isInteger(limit) || limit < 1) {
      throw new DataSourceError("limit must be a positive integer");
    }
    if (before !== undefined && !dateString.safeParse(before).success) {
      throw new DataSourceError("before must be YYYY-MM-DD");
    }
    const rows = SYNTHETIC_DAYS.filter((d) => before === undefined || d.date < before).slice(
      0,
      Math.min(limit, 100),
    );
    return historyResponseSchema.parse({ summaries: rows, count: rows.length });
  }

  async getDay(date: string): Promise<DayResponse | null> {
    if (!dateString.safeParse(date).success) {
      throw new DataSourceError("date must be YYYY-MM-DD");
    }
    const found = SYNTHETIC_DAYS.find((d) => d.date === date);
    return found ? dayResponseSchema.parse(dayDetail(found)) : null;
  }
}
