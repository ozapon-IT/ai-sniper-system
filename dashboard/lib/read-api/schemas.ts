/** Read API レスポンス契約の zod スキーマ（§6.3）。
 *
 * 正は Python 側 `backend/src/read_api/handler.py` が実際に返す JSON（snake_case）。
 * フィールド名・型を変えると Python 側の契約テスト（tests/lambda/test_read_api_contract.py）
 * と、そのフィクスチャを parse する TS 側テストの双方が落ちる。
 *
 * - plan / lot / sniper_logs の中身は判断・手仕舞いバッチが書く自由形式 JSON で、
 *   Read API 側に固定スキーマが無い。推測でフィールドを書かず `unknown` の record とする。
 * - 旧 SPA（`frontend/`・#394 で撤去）の parse.ts が弾いていた不正（日付形式・boolean 型・direction の
 *   ホワイトリスト外・数値型不一致）は、同じく弾く。ただし現行は不正な履歴行を黙って
 *   除外していたのに対し、こちらはレスポンス全体を不正として扱う（壊れた応答を
 *   部分的に表示しない）。
 *
 * 出典: zod 4 `z.record(keyType, valueType)` は 2 引数必須、列挙キーの一部だけを許すときは
 * `z.partialRecord`（https://zod.dev/api#records・参照日 2026-11-22）。
 */
import { z } from "zod";

/** YYYY-MM-DD（現行 parse.ts の HISTORY_DATE_RE と同じ形式チェック）。 */
export const dateString = z
  .string()
  .regex(/^\d{4}-\d{2}-\d{2}$/, "YYYY-MM-DD")
  .refine((v) => {
    // 2099-02-30 のような実在しない日付は handler（date.fromisoformat）も 400 にする。
    const d = new Date(`${v}T00:00:00Z`);
    return !Number.isNaN(d.getTime()) && d.toISOString().slice(0, 10) === v;
  }, "実在する日付ではない");

/** 売買方向。handler は未設定時 null を返し得るため nullable は利用側スキーマで付ける。 */
export const directionSchema = z.enum(["LONG", "SHORT", "NO_ENTRY"]);

/** バッチが書く自由形式オブジェクト（中身の契約は未固定）。 */
const freeFormObject = z.record(z.string(), z.unknown());

/** GET /dashboard */
export const dashboardResponseSchema = z.object({
  equity: z.number().nullable(),
  today_plan: freeFormObject.nullable(),
  trade_note: z.string().nullable(),
  // バッチは logs キーを書かない（summary/dashboard.json にのみあり、無ければ handler は null を返す）。
  sniper_logs: z.array(freeFormObject).nullable(),
  status: z.string(),
  last_updated: z.string().nullable(),
  lot: z.unknown(),
});

/** Decimal を文字列で保存した金額（closeout_batch の str(Decimal)）。
 * 🔴 数値にしない。浮動小数にして丸めない（表示時に整形する）。指数表記（"1E+2"）も取り得る。 */
export const decimalString = z.string().regex(/^-?\d+(\.\d+)?([eE][+-]?\d+)?$/, "decimal string");

/** 決済理由（backend/src/llm/trade_note.py の ExitReason）。ID は返さず理由ごとの件数にする。 */
const exitReasonSchema = z.enum(["time_stop", "take_profit", "stop_loss", "unknown"]);

/** 手仕舞いの要約（/day の closeout）。handler のホワイトリスト出力で、各項目は欠け得る。
 *
 * 🔴 `scope` は **executions_count / pnl / exit_reasons の 3 項目すべて**の集計範囲。
 * 金曜（mode="friday"）は土曜の手仕舞いが直近 5 取引日分の約定を集めて作るため "week"
 * （金曜 1 日分ではない）。画面では "week" のとき「週次（5 取引日）」と明示すること（#399）。 */
export const closeoutSummarySchema = z.object({
  // 実行した回: ok / step3_failed / step3_residual / step3_unverified、
  // 途中で止まった回: market_closed / step1_failed / step2_failed。語彙追加に備え string。
  status: z.string().nullable(),
  mode: z.enum(["weekday", "friday"]).nullable(),
  executions_fetched: z.boolean().nullable(),
  executions_count: z.number().int().nonnegative().nullable(),
  pnl: z
    .object({
      realized_total: decimalString.nullable(),
      loss_gain_total: decimalString.nullable(),
      fee_total: decimalString.nullable(),
      settled_swap_total: decimalString.nullable(),
      close_count: z.number().int().nonnegative().nullable(),
    })
    .nullable(),
  scope: z.enum(["day", "week"]).nullable(),
  exit_reasons: z.partialRecord(exitReasonSchema, z.number().int().positive()).nullable(),
  note_complete: z.boolean().nullable(),
});

/** GET /history の 1 行。 */
export const historySummarySchema = z.object({
  date: dateString,
  direction: directionSchema.nullable(),
  execution_required: z.boolean().nullable(),
  status: z.string().nullable(),
  rationale_summary: z.string(),
  has_note: z.boolean(),
});

/** GET /history */
export const historyResponseSchema = z.object({
  summaries: z.array(historySummarySchema),
  count: z.number().int().nonnegative(),
});

/** GET /day/{date} */
export const dayResponseSchema = z.object({
  date: dateString,
  plan: freeFormObject.nullable(),
  direction: directionSchema.nullable(),
  // 判断ログが書く値: no_entry / skipped / ok / stubbed / failed / persistence_failed
  // （executed / no_fill は ORB サブシステムの語彙）。語彙追加で全画面が壊れないよう string のまま。
  status: z.string().nullable(),
  // 判断・手仕舞いログのどちらにも logs キーは無く、実データでは常に null。
  sniper_logs: z.array(freeFormObject).nullable(),
  trade_note: z.string().nullable(),
  /** 手仕舞いログが無い日は null。 */
  closeout: closeoutSummarySchema.nullable(),
  equity: z.number().nullable(),
  lot: z.unknown(),
});

export type CloseoutSummary = z.infer<typeof closeoutSummarySchema>;
export type DashboardResponse = z.infer<typeof dashboardResponseSchema>;
export type HistorySummary = z.infer<typeof historySummarySchema>;
export type HistoryResponse = z.infer<typeof historyResponseSchema>;
export type DayResponse = z.infer<typeof dayResponseSchema>;
