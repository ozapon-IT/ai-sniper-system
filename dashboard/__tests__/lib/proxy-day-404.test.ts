import { NextRequest } from "next/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { proxy } from "../../proxy";
import { dayExists } from "../../lib/data-source";

// #413: 記録の無い日付は proxy で 404（レンダリング前）。読めないときは fail-open（画面に任せる）。
vi.mock("../../lib/data-source", () => ({ dayExists: vi.fn() }));
const mockedDayExists = vi.mocked(dayExists);

function req(path: string): NextRequest {
  return new NextRequest(`http://localhost:3000${path}`, { headers: { host: "localhost:3000" } });
}

afterEach(() => {
  vi.clearAllMocks();
  vi.unstubAllEnvs();
});

describe("proxy: /day/<date>", () => {
  it("記録なし（false）は 404 に rewrite する", async () => {
    mockedDayExists.mockResolvedValue(false);
    const res = await proxy(req("/day/1999-01-01"));
    expect(res.status).toBe(404);
    // 空の 404 ではなく、not-found 画面へ rewrite していること
    expect(res.headers.get("x-middleware-rewrite")).toContain("/__not-found__");
  });

  it("不正な日付はデータ取得なしで 404", async () => {
    const res = await proxy(req("/day/not-a-date"));
    expect(res.status).toBe(404);
    expect(mockedDayExists).not.toHaveBeenCalled();
  });

  it("不正なパーセントエンコードでも例外にならず 404", async () => {
    const res = await proxy(req("/day/%E0%A4%A"));
    expect(res.status).toBe(404);
  });

  it("記録あり（true）は素通し", async () => {
    mockedDayExists.mockResolvedValue(true);
    const res = await proxy(req("/day/2099-01-07"));
    expect(res.headers.get("x-middleware-next")).toBe("1");
  });

  it("判定不能（null）は fail-open で素通し（404 にしない）", async () => {
    mockedDayExists.mockResolvedValue(null);
    const res = await proxy(req("/day/2099-01-07"));
    expect(res.headers.get("x-middleware-next")).toBe("1");
  });

  it("手元専用モードで許可されないリクエストは、データを読まずに 403", async () => {
    // dev サーバー判定が無い（next dev 以外）ため isRequestAllowed が拒否する
    vi.stubEnv("DASHBOARD_DATA_SOURCE", "lambda");
    const res = await proxy(new NextRequest("http://localhost:3000/day/2099-01-07"));
    expect(res.status).toBe(403);
    expect(mockedDayExists).not.toHaveBeenCalled();
  });
});
