import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// connection() はリクエストの外では使えない。リクエスト時まで待つ呼び出しがあることだけを確かめる。
const connection = vi.fn(async () => {});
vi.mock("next/server", () => ({ connection: () => connection() }));

const createLambdaDataSource = vi.fn();
const readLambdaModeConfig = vi.fn(() => ({ functionName: "f", region: "r", profile: "p" }));
vi.mock("../../lib/read-api/lambda-config", () => ({
  createLambdaDataSource: (...args: unknown[]) => createLambdaDataSource(...args),
  readLambdaModeConfig: () => readLambdaModeConfig(),
}));

const LOCAL_ENV = {
  DASHBOARD_DATA_SOURCE: "lambda",
  DASHBOARD_BIND_HOST: "127.0.0.1",
  __NEXT_DEV_SERVER: "1",
  __NEXT_PRIVATE_ORIGIN: "http://127.0.0.1:3000",
};

describe("getDataSource（手元専用モードの LambdaClient キャッシュ・指摘 #5）", () => {
  beforeEach(() => {
    vi.resetModules();
    createLambdaDataSource.mockReset();
    connection.mockClear();
    for (const [k, v] of Object.entries(LOCAL_ENV)) vi.stubEnv(k, v);
  });
  afterEach(() => vi.unstubAllEnvs());

  it("画面（リクエスト）を何度描画しても LambdaClient は 1 回だけ作る", async () => {
    const inner = { getDashboard: vi.fn(async () => "d"), getHistory: vi.fn(), getDay: vi.fn() };
    createLambdaDataSource.mockReturnValue(inner);
    const { getDataSource } = await import("../../lib/data-source");
    await getDataSource().getDashboard();
    await getDataSource().getDashboard();
    await getDataSource().getDashboard();
    // 🔴 保留中の import が片付いてから数える（片付く前だと、捨てた側の組み立てを見逃す・再レビュー A）
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(createLambdaDataSource).toHaveBeenCalledTimes(1);
  });

  it("同時リクエストでも 1 回だけ作り、設定不備でも未処理の rejection を出さない", async () => {
    const unhandled = vi.fn();
    process.on("unhandledRejection", unhandled);
    try {
      createLambdaDataSource.mockImplementation(() => {
        throw new Error("config missing");
      });
      const { getDataSource } = await import("../../lib/data-source");
      const results = await Promise.allSettled([
        getDataSource().getDashboard(),
        getDataSource().getDashboard(),
      ]);
      expect(results.map((r) => r.status)).toEqual(["rejected", "rejected"]);
      await new Promise((resolve) => setTimeout(resolve, 20));
      expect(createLambdaDataSource).toHaveBeenCalledTimes(1);
      expect(unhandled).not.toHaveBeenCalled();
    } finally {
      process.off("unhandledRejection", unhandled);
    }
  });

  it("🔴 実データ取得の前に、リクエスト時まで待つ（connection・再レビュー B）", async () => {
    const inner = { getDashboard: vi.fn(async () => "d"), getHistory: vi.fn(), getDay: vi.fn() };
    createLambdaDataSource.mockReturnValue(inner);
    const { getDataSource } = await import("../../lib/data-source");
    await getDataSource().getDashboard();
    expect(connection).toHaveBeenCalledTimes(1);
    expect(connection.mock.invocationCallOrder[0]).toBeLessThan(
      inner.getDashboard.mock.invocationCallOrder[0],
    );
  });

  it("合成データの経路では connection を呼ばない（公開デモの静的生成を変えない）", async () => {
    vi.unstubAllEnvs();
    const { getDataSource } = await import("../../lib/data-source");
    await getDataSource().getHistory({ limit: 1 });
    expect(connection).not.toHaveBeenCalled();
  });

  it("失敗した初期化はキャッシュせず、次のリクエストで作り直す", async () => {
    createLambdaDataSource.mockImplementationOnce(() => {
      throw new Error("config missing");
    });
    const inner = { getDashboard: vi.fn(async () => "ok"), getHistory: vi.fn(), getDay: vi.fn() };
    createLambdaDataSource.mockReturnValue(inner);
    const { getDataSource } = await import("../../lib/data-source");
    await expect(getDataSource().getDashboard()).rejects.toThrow("config missing");
    await expect(getDataSource().getDashboard()).resolves.toBe("ok");
    expect(createLambdaDataSource).toHaveBeenCalledTimes(2);
  });

  it("🔴 タイムアウトしたら即キャッシュから外し、遅延して destroy、次のリクエストで作り直す（新-1・C）", async () => {
    const { DataSourceError, INVOKE_TIMEOUT_MS } = await import("../../lib/read-api");
    const hung = {
      getDashboard: vi.fn(async () => {
        throw new DataSourceError("timeout", { resetClient: true });
      }),
      destroy: vi.fn(),
    };
    const healthy = { getDashboard: vi.fn(async () => "ok"), destroy: vi.fn() };
    createLambdaDataSource.mockReturnValueOnce(hung).mockReturnValueOnce(healthy);
    const { getDataSource } = await import("../../lib/data-source");
    vi.useFakeTimers({ toFake: ["setTimeout"] });
    try {
      await expect(getDataSource().getDashboard()).rejects.toThrow("timeout");
      // キャッシュからは即座に外れ、次のリクエストは新しいクライアントを使う…
      await expect(getDataSource().getDashboard()).resolves.toBe("ok");
      expect(createLambdaDataSource).toHaveBeenCalledTimes(2);
      // …が、destroy は遅らせる（通信中の別リクエストを切らない・再レビュー C）。
      await vi.advanceTimersByTimeAsync(INVOKE_TIMEOUT_MS - 1);
      expect(hung.destroy).not.toHaveBeenCalled();
      await vi.advanceTimersByTimeAsync(1);
      expect(hung.destroy).toHaveBeenCalledTimes(1);
      expect(healthy.destroy).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("遅延させた destroy が例外を投げても握り潰す（uncaughtException にしない）", async () => {
    const { DataSourceError, INVOKE_TIMEOUT_MS } = await import("../../lib/read-api");
    const hung = {
      getDashboard: vi.fn(async () => {
        throw new DataSourceError("timeout", { resetClient: true });
      }),
      destroy: vi.fn(() => {
        throw new Error("destroy failed");
      }),
    };
    createLambdaDataSource.mockReturnValue(hung);
    const { getDataSource } = await import("../../lib/data-source");
    vi.useFakeTimers({ toFake: ["setTimeout"] });
    try {
      await expect(getDataSource().getDashboard()).rejects.toThrow("timeout");
      await expect(vi.advanceTimersByTimeAsync(INVOKE_TIMEOUT_MS)).resolves.not.toThrow();
      expect(hung.destroy).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("タイムアウト以外の失敗ではクライアントを捨てない", async () => {
    const { DataSourceError } = await import("../../lib/read-api");
    const inner = {
      getDashboard: vi.fn(async () => {
        throw new DataSourceError("HTTP 500");
      }),
      destroy: vi.fn(),
    };
    createLambdaDataSource.mockReturnValue(inner);
    const { getDataSource } = await import("../../lib/data-source");
    await expect(getDataSource().getDashboard()).rejects.toThrow("HTTP 500");
    await expect(getDataSource().getDashboard()).rejects.toThrow("HTTP 500");
    expect(createLambdaDataSource).toHaveBeenCalledTimes(1);
    expect(inner.destroy).not.toHaveBeenCalled();
  });

  it("起動条件を満たさない（実 bind が 0.0.0.0）ときは DataSource を返さず拒否する", async () => {
    vi.stubEnv("__NEXT_PRIVATE_ORIGIN", "http://localhost:3000");
    const { getDataSource } = await import("../../lib/data-source");
    expect(() => getDataSource()).toThrow();
    expect(createLambdaDataSource).not.toHaveBeenCalled();
  });
});
