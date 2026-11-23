import { describe, expect, it } from "vitest";
import { formatDecimal, scopeLabel } from "../../lib/format";

describe("formatDecimal（浮動小数にしない）", () => {
  it.each([
    ["0", "0"],
    ["-120", "-120"],
    ["1234567", "1,234,567"],
    ["-1234.5", "-1,234.5"],
    ["12345678901234567890.123456789", "12,345,678,901,234,567,890.123456789"], // 浮動小数なら丸まる桁
    ["1E+2", "1E+2"], // 指数表記は加工せずそのまま
  ])("%s → %s", (input, expected) => {
    expect(formatDecimal(input)).toBe(expected);
  });
});

describe("scopeLabel", () => {
  it("週次は 5 取引日の合計と明示する", () => {
    expect(scopeLabel("week")).toContain("5 取引日");
    expect(scopeLabel("day")).toBe("日次");
  });
});
