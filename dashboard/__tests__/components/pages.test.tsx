import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { DayContent } from "../../components/day-content";
import { HistoryContent } from "../../components/history-content";
import { LatestTradingDay } from "../../components/latest-trading-day";
import ErrorPage from "../../app/error";
import NotFound from "../../app/not-found";

// notFound() は NEXT_HTTP_ERROR_FALLBACK;404 を throw する（公式: not-found.md）
async function expectNotFound(p: Promise<unknown>) {
  await expect(p).rejects.toMatchObject({ digest: expect.stringContaining("404") });
}

describe("当日画面（直近の取引日）", () => {
  it("getHistory + getDay で最新日を組み、「今日」ではなく取引日を見出しに出す", async () => {
    render(await LatestTradingDay());
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("直近の取引日（2099-01-12）");
    expect(screen.getByText(/前日分/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "手仕舞い" })).toBeInTheDocument();
    // 直近の取引日は、手仕舞い（翌 4:30）がまだ動いていない状態がいちばん多い
    expect(screen.getByText(/手仕舞いは翌 4:30（JST）に実行されます/)).toBeInTheDocument();
  });
});

describe("履歴画面", () => {
  it("新しい順に並び、各行は日次詳細へのリンク", async () => {
    render(await HistoryContent({}));
    const links = screen.getAllByRole("link", { name: /2099-01-/ });
    expect(links.map((l) => l.getAttribute("href"))).toEqual([
      "/day/2099-01-12",
      "/day/2099-01-09",
      "/day/2099-01-08",
      "/day/2099-01-07",
      "/day/2099-01-06",
    ]);
  });

  it("limit で区切り、before でページ送りできる", async () => {
    const { unmount } = render(await HistoryContent({ limit: "2" }));
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    const next = screen.getByRole("link", { name: "さらに古い日へ" });
    expect(next).toHaveAttribute("href", "/history?before=2099-01-09&limit=2");
    unmount();

    render(await HistoryContent({ before: "2099-01-08", limit: "2" }));
    expect(screen.getAllByRole("link", { name: /2099-01-/ }).map((l) => l.textContent)).toEqual([
      "2099-01-07",
      "2099-01-06",
    ]);
    expect(screen.getByRole("link", { name: "先頭へ" })).toBeInTheDocument();
  });

  it.each([
    ["limit が範囲外", { limit: "0" }],
    ["limit が 101", { limit: "101" }],
    ["limit が数値でない", { limit: "abc" }],
    ["before が日付でない", { before: "x" }],
    ["before が実在しない日付", { before: "2099-02-30" }],
  ])("%s は不正指定として案内を出す", async (_n, q) => {
    render(await HistoryContent(q));
    expect(screen.getByRole("alert")).toHaveTextContent("指定が不正です");
  });
});

describe("日次詳細画面", () => {
  it("存在する日は詳細を出す", async () => {
    render(await DayContent({ date: "2099-01-07" }));
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("2099-01-07");
    expect(screen.getByRole("link", { name: /履歴/ })).toHaveAttribute("href", "/history");
  });

  it("記録の無い日・形式不正・実在しない日付は 404", async () => {
    await expectNotFound(DayContent({ date: "2099-12-31" }));
    await expectNotFound(DayContent({ date: "abc" }));
    await expectNotFound(DayContent({ date: "2099-02-30" }));
  });
});

describe("error.tsx / not-found.tsx", () => {
  it("取得失敗: alert と再試行ボタン（retry を呼ぶ。reset ではデータを取り直さない）", async () => {
    const retry = vi.fn();
    render(<ErrorPage error={new Error("boom")} retry={retry} />);
    expect(screen.getByRole("alert")).toHaveTextContent("データを取得できませんでした");
    expect(screen.queryByText("boom")).toBeNull(); // 内部のエラーメッセージは出さない
    await userEvent.click(screen.getByRole("button", { name: "再試行" }));
    expect(retry).toHaveBeenCalledOnce();
  });

  it("記録なし: 履歴へ戻るリンク", () => {
    render(<NotFound />);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("記録が見つかりません");
    expect(screen.getByRole("link", { name: "履歴に戻る" })).toHaveAttribute("href", "/history");
  });
});
