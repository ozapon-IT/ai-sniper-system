import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

const PAGES = [
  "/",
  "/history",
  "/history?limit=2",
  "/day/2099-01-09",
  "/day/2099-01-08",
  "/day/2099-01-07",
  "/day/2099-01-06",
  "/day/2099-01-12",
  "/day/2099-12-31",
];

for (const scheme of ["light", "dark"] as const) {
  for (const path of PAGES) {
    test(`axe: 重大な違反なし ${path}（${scheme}）`, async ({ page }) => {
      await page.emulateMedia({ colorScheme: scheme });
      await page.goto(path);
      await page.getByRole("heading", { level: 1 }).waitFor();
      const results = await new AxeBuilder({ page }).analyze();
      expect(results.passes.length).toBeGreaterThan(0); // axe が実際にルールを走らせたことの確認
      const serious = results.violations.filter(
        (v) => v.impact === "serious" || v.impact === "critical",
      );
      expect(serious.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
    });
  }
}

test.describe("スマホ幅 375px", () => {
  test.use({ viewport: { width: 375, height: 812 } });
  for (const path of PAGES) {
    test(`横スクロールが出ない ${path}`, async ({ page }) => {
      await page.goto(path);
      await page.getByRole("heading", { level: 1 }).waitFor();
      const { scrollWidth, clientWidth } = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
      }));
      expect(scrollWidth).toBeLessThanOrEqual(clientWidth);
    });
  }
});
