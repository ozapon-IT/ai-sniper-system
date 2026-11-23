import { describe, expect, it } from "vitest";
import {
  dashboardResponseSchema,
  dayResponseSchema,
  historyResponseSchema,
} from "../../lib/read-api";

const validRow = {
  date: "2099-01-05",
  direction: "LONG",
  execution_required: true,
  status: "executed",
  rationale_summary: "synthetic",
  has_note: false,
};
const history = (row: Record<string, unknown>) => ({ summaries: [row], count: 1 });

// 旧 SPA（frontend/・#394 で撤去）の parse.ts が弾いていた不正を zod 版でも弾く。
describe("履歴行の不正を弾く", () => {
  it("正常行は通る", () => {
    expect(historyResponseSchema.safeParse(history(validRow)).success).toBe(true);
  });
  it.each([
    ["日付形式不正", { date: "2099/01/05" }],
    ["日付が文字列でない", { date: 20990105 }],
    ["実在しない日付", { date: "2099-02-30" }],
    ["direction がホワイトリスト外", { direction: "BUY" }],
    ["execution_required が boolean でない", { execution_required: "true" }],
    ["has_note が boolean でない", { has_note: 1 }],
    ["has_note 欠落", { has_note: undefined }],
  ])("%s", (_name, patch) => {
    expect(historyResponseSchema.safeParse(history({ ...validRow, ...patch })).success).toBe(false);
  });
  it("summaries が配列でない", () => {
    expect(historyResponseSchema.safeParse({ summaries: {}, count: 0 }).success).toBe(false);
  });
});

describe("dashboard の不正を弾く", () => {
  const ok = {
    equity: 1,
    today_plan: null,
    trade_note: null,
    sniper_logs: [],
    status: "idle",
    last_updated: null,
    lot: null,
  };
  it("正常は通る", () => {
    expect(dashboardResponseSchema.safeParse(ok).success).toBe(true);
  });
  it.each([
    ["equity が文字列", { equity: "100" }],
    ["equity が NaN", { equity: Number.NaN }],
    ["sniper_logs が配列でも null でもない", { sniper_logs: "x" }],
    ["sniper_logs に非オブジェクト", { sniper_logs: [1] }],
    ["status 欠落", { status: undefined }],
    ["today_plan が配列", { today_plan: [] }],
  ])("%s", (_name, patch) => {
    expect(dashboardResponseSchema.safeParse({ ...ok, ...patch }).success).toBe(false);
  });
  it("sniper_logs が null でも通る（実データ）", () => {
    expect(dashboardResponseSchema.safeParse({ ...ok, sniper_logs: null }).success).toBe(true);
  });
  it("オブジェクトでない", () => {
    expect(dashboardResponseSchema.safeParse(null).success).toBe(false);
  });
});

describe("day の不正を弾く", () => {
  it("日付形式不正・direction 不正", () => {
    const ok = {
      date: "2099-01-05",
      plan: null,
      direction: "LONG",
      status: null,
      sniper_logs: [],
      trade_note: null,
      closeout: null,
      equity: null,
      lot: null,
    };
    expect(dayResponseSchema.safeParse(ok).success).toBe(true);
    expect(dayResponseSchema.safeParse({ ...ok, date: "x" }).success).toBe(false);
    expect(dayResponseSchema.safeParse({ ...ok, direction: "UP" }).success).toBe(false);
  });
});

describe("closeout の不正を弾く", () => {
  const closeout = {
    status: "ok",
    mode: "friday",
    executions_fetched: true,
    executions_count: 2,
    pnl: { realized_total: "-120", loss_gain_total: "-100", fee_total: "0", settled_swap_total: "-20", close_count: 1 },
    scope: "week",
    exit_reasons: { time_stop: 1 },
    note_complete: true,
  };
  const day = (c: unknown) => ({
    date: "2099-01-05",
    plan: null,
    direction: "LONG",
    status: null,
    sniper_logs: null,
    trade_note: null,
    closeout: c,
    equity: null,
    lot: null,
  });
  it("正常は通る", () => {
    expect(dayResponseSchema.safeParse(day(closeout)).success).toBe(true);
    expect(dayResponseSchema.safeParse(day(null)).success).toBe(true);
  });
  it.each([
    ["損益が数値（浮動小数）", { pnl: { ...closeout.pnl, realized_total: -120 } }],
    ["損益が数値でない文字列", { pnl: { ...closeout.pnl, realized_total: "abc" } }],
    ["scope が範囲外", { scope: "month" }],
    ["mode が範囲外", { mode: "sunday" }],
    ["exit_reasons に未知の理由", { exit_reasons: { weird: 1 } }],
    ["exit_reasons の件数が整数でない", { exit_reasons: { time_stop: 1.5 } }],
  ])("%s", (_name, patch) => {
    expect(dayResponseSchema.safeParse(day({ ...closeout, ...patch })).success).toBe(false);
  });
  it("指数表記の Decimal も受ける", () => {
    const p = { ...closeout.pnl, realized_total: "1E+2" };
    expect(dayResponseSchema.safeParse(day({ ...closeout, pnl: p })).success).toBe(true);
  });
});
