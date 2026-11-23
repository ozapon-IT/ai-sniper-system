import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import {
  dashboardResponseSchema,
  dayResponseSchema,
  historyResponseSchema,
} from "../../lib/read-api";

/** Python 側 tests/lambda/test_read_api_contract.py が handler の実レスポンスから書き出した JSON。 */
function fixture(name: string): unknown {
  return JSON.parse(readFileSync(join(__dirname, "fixtures", name), "utf-8"));
}

describe("契約: handler.py の実レスポンスを zod スキーマで parse できる", () => {
  it("/dashboard", () => {
    expect(dashboardResponseSchema.parse(fixture("dashboard.json")).equity).toBe(1000000);
  });
  it("/dashboard（データ未生成: null 値）", () => {
    const r = dashboardResponseSchema.parse(fixture("dashboard_empty.json"));
    expect(r.equity).toBeNull();
    expect(r.sniper_logs).toEqual([]);
  });
  it("/history", () => {
    const r = historyResponseSchema.parse(fixture("history.json"));
    expect(r.count).toBe(3);
    // skip 日は direction / execution_required が null
    expect(r.summaries[2].direction).toBeNull();
  });
  it("/dashboard（logs キー無し: sniper_logs は null）", () => {
    expect(dashboardResponseSchema.parse(fixture("dashboard_no_logs.json")).sniper_logs).toBeNull();
  });
  it("/day/{date}（判断ログのみ・実バッチのキー構成）", () => {
    const r = dayResponseSchema.parse(fixture("day_decision_only.json"));
    expect(r.date).toBe("2099-01-06");
    expect(r.sniper_logs).toBeNull();
    // 口座取得後の判断ログには equity_jpy があり、/day の equity に数値で出る（#411）
    expect(r.equity).toBe(123456);
  });
  it("/day/{date}（口座残高なし: 取得前に止まった日・過去ログは equity が null）", () => {
    expect(dayResponseSchema.parse(fixture("day_no_entry.json")).equity).toBeNull();
  });
  it("/day/{date}（見送り日: 既定値だけの plan を返さない）", () => {
    const r = dayResponseSchema.parse(fixture("day_no_entry.json"));
    expect(r.direction).toBe("NO_ENTRY");
    expect(r.plan).toBeNull();
  });
  it("/day/{date} closeout（正常: 日次の損益）", () => {
    const c = dayResponseSchema.parse(fixture("day_with_closeout.json")).closeout;
    expect(c?.scope).toBe("day");
    expect(c?.pnl?.realized_total).toBe("-120"); // 文字列のまま（浮動小数にしない）
    expect(c?.exit_reasons).toEqual({ time_stop: 1 });
  });
  it("/day/{date} closeout（金曜: 損益は週の合計として区別できる）", () => {
    const c = dayResponseSchema.parse(fixture("day_closeout_friday.json")).closeout;
    expect(c?.mode).toBe("friday");
    expect(c?.scope).toBe("week");
    // scope は executions_count / pnl / exit_reasons の 3 項目すべてに掛かる（金曜 1 日分ではない）
    expect(c?.executions_count).toBe(8);
    expect(c?.exit_reasons).toEqual({ stop_loss: 1, take_profit: 2, time_stop: 1 });
  });
  it("/day/{date} closeout（見送り日にも手仕舞いログがある: 約定 0 件・損益 0）", () => {
    const c = dayResponseSchema.parse(fixture("day_closeout_no_entry.json")).closeout;
    expect(c?.status).toBe("ok");
    expect(c?.executions_count).toBe(0);
    expect(c?.pnl?.realized_total).toBe("0");
    expect(c?.exit_reasons).toEqual({});
  });
  it("/day/{date} closeout（Step3 失敗）", () => {
    expect(dayResponseSchema.parse(fixture("day_closeout_step3_failed.json")).closeout?.status).toBe(
      "step3_failed",
    );
  });
  it("/day/{date} closeout（閉場: pnl なし）", () => {
    const c = dayResponseSchema.parse(fixture("day_closeout_market_closed.json")).closeout;
    expect(c?.status).toBe("market_closed");
    expect(c?.pnl).toBeNull();
    expect(c?.exit_reasons).toBeNull();
  });
  it("/day/{date} closeout（手仕舞いログ無し: null）", () => {
    expect(dayResponseSchema.parse(fixture("day_decision_only.json")).closeout).toBeNull();
  });
  it("/day/{date}（判断ログ＋手仕舞いログ）", () => {
    const r = dayResponseSchema.parse(fixture("day_with_closeout.json"));
    expect(r.sniper_logs).toBeNull();
    expect(r.trade_note).toBe("## synthetic note"); // notes/{date}.json の note_markdown（#397）
  });
  it("フィールド名が変わる（equity → equity_jpy）と落ちる", () => {
    const { equity, ...rest } = fixture("dashboard.json") as Record<string, unknown>;
    expect(dashboardResponseSchema.safeParse({ ...rest, equity_jpy: equity }).success).toBe(false);
  });
});
