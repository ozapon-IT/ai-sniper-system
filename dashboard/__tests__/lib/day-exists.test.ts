import { afterEach, describe, expect, it, vi } from "vitest";
import { dayExists } from "../../lib/data-source";
import { DataSourceError, MockDataSource } from "../../lib/read-api";

afterEach(() => vi.restoreAllMocks());

describe("dayExists（#413）", () => {
  it("合成データにある日は true、無い日は false", async () => {
    expect(await dayExists("2099-01-07")).toBe(true);
    expect(await dayExists("1999-01-01")).toBe(false);
  });

  it("読み取りに失敗したら null（fail-open。404 に倒さない）", async () => {
    vi.spyOn(MockDataSource.prototype, "getDay").mockRejectedValue(new DataSourceError("boom"));
    expect(await dayExists("2099-01-07")).toBeNull();
  });
});
