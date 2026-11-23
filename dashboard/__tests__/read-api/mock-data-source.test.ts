import { describe, expect, it } from "vitest";
import {
  DataSourceError,
  MockDataSource,
  READ_API_PATHS,
  dashboardResponseSchema,
  historyResponseSchema,
} from "../../lib/read-api";

const ds = new MockDataSource();

describe("MockDataSource", () => {
  it("getDashboard はスキーマに適合する", async () => {
    expect(dashboardResponseSchema.safeParse(await ds.getDashboard()).success).toBe(true);
  });

  it("getHistory は新しい順で limit / before を守る", async () => {
    const all = await ds.getHistory();
    expect(historyResponseSchema.safeParse(all).success).toBe(true);
    const dates = all.summaries.map((s) => s.date);
    expect(dates).toEqual([...dates].sort().reverse());
    expect((await ds.getHistory({ limit: 2 })).summaries).toHaveLength(2);
    const before = (await ds.getHistory({ before: dates[0] })).summaries.map((s) => s.date);
    expect(before).not.toContain(dates[0]);
  });

  it("不正な引数は DataSourceError", async () => {
    await expect(ds.getHistory({ limit: 0 })).rejects.toBeInstanceOf(DataSourceError);
    await expect(ds.getHistory({ before: "x" })).rejects.toBeInstanceOf(DataSourceError);
    await expect(ds.getDay("2099-02-30")).rejects.toBeInstanceOf(DataSourceError);
    await expect(ds.getDay("x")).rejects.toBeInstanceOf(DataSourceError);
  });

  it("getDay: 履歴にある日は詳細、無い日は null", async () => {
    const [first] = (await ds.getHistory()).summaries;
    expect((await ds.getDay(first.date))?.date).toBe(first.date);
    expect(await ds.getDay("2099-12-31")).toBeNull();
  });

  it("架空データは 2099 年のみ（実運用の日付を含まない）", async () => {
    const days = await Promise.all(
      (await ds.getHistory()).summaries.map((d) => ds.getDay(d.date)),
    );
    const text = JSON.stringify([await ds.getDashboard(), await ds.getHistory(), days]);
    const years = [...text.matchAll(/\b(\d{4})-\d{2}-\d{2}/g)].map((m) => m[1]);
    expect(years.length).toBeGreaterThan(0);
    expect(new Set(years)).toEqual(new Set(["2099"]));
  });
});

it("status は判断ログの語彙（ORB 側の executed / no_fill を使わない）", async () => {
  const known = ["no_entry", "skipped", "ok", "stubbed", "failed", "persistence_failed"];
  for (const row of (await ds.getHistory()).summaries) {
    expect(known).toContain(row.status);
    // has_note は /day の trade_note の有無と一致する
    expect((await ds.getDay(row.date))?.trade_note !== null).toBe(row.has_note);
  }
});

it("金曜は 3 項目とも週の集計（scope=week）、見送り日・スキップ日にも手仕舞いがある", async () => {
  const friday = await ds.getDay("2099-01-09");
  expect(new Date("2099-01-09T00:00:00Z").getUTCDay()).toBe(5); // 実際に金曜
  expect(friday?.closeout?.mode).toBe("friday");
  expect(friday?.closeout?.scope).toBe("week");
  const noEntry = (await ds.getDay("2099-01-08"))?.closeout; // 見送り日
  expect(noEntry?.status).toBe("ok");
  expect(noEntry?.executions_count).toBe(0);
  expect(noEntry?.pnl?.realized_total).toBe("0");
  expect(noEntry?.exit_reasons).toEqual({});
  expect((await ds.getDay("2099-01-07"))?.closeout?.scope).toBe("day");
  // 取引日に発注前の確認で中止した日（skipped）も手仕舞いは動く
  expect((await ds.getDay("2099-01-06"))?.status).toBe("skipped");
  expect((await ds.getDay("2099-01-06"))?.closeout?.executions_count).toBe(0);
});

it("直近の取引日は手仕舞い前で closeout が null（翌 4:30 に実行される）", async () => {
  const latest = (await ds.getHistory({ limit: 1 })).summaries[0];
  expect(latest.date).toBe("2099-01-12");
  expect((await ds.getDay(latest.date))?.closeout).toBeNull();
});

it("金曜のノートには確定表（Markdown テーブル）が入る", async () => {
  expect((await ds.getDay("2099-01-09"))?.trade_note).toContain("| 取引日 | 方向 |");
});

it("合成日付はすべて平日", async () => {
  for (const row of (await ds.getHistory()).summaries) {
    const dow = new Date(`${row.date}T00:00:00Z`).getUTCDay();
    expect([1, 2, 3, 4, 5]).toContain(dow);
  }
});

describe("READ_API_PATHS", () => {
  it("handler のルーティングと一致する", () => {
    expect(READ_API_PATHS.dashboard).toBe("/dashboard");
    expect(READ_API_PATHS.history).toBe("/history");
    expect(READ_API_PATHS.day("2099-01-05")).toBe("/day/2099-01-05");
  });
});
