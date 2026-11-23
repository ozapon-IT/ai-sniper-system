import { expect, test } from "@playwright/test";

test("主要導線: 当日 → 履歴 → 日次詳細", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("直近の取引日（2099-01-12）");

  await page.getByRole("navigation", { name: "メイン" }).getByRole("link", { name: "履歴" }).click();
  await expect(page).toHaveURL(/\/history$/);
  await expect(page.getByRole("heading", { level: 1, name: "履歴" })).toBeVisible();

  await page.getByRole("link", { name: "2099-01-07" }).click();
  await expect(page).toHaveURL(/\/day\/2099-01-07$/);
  await expect(page.getByRole("heading", { level: 1, name: "2099-01-07" })).toBeVisible();
  await expect(page.getByRole("heading", { level: 2, name: "手仕舞い" })).toBeVisible();
});

test("履歴のページ送り（limit / before）", async ({ page }) => {
  await page.goto("/history?limit=2");
  await expect(page.getByRole("link", { name: /^2099-01-/ })).toHaveCount(2);
  await page.getByRole("link", { name: "さらに古い日へ" }).click();
  await expect(page).toHaveURL(/before=2099-01-09&limit=2/);
  await expect(page.getByRole("link", { name: "2099-01-07" })).toBeVisible();
  await expect(page.getByRole("link", { name: "2099-01-12" })).toHaveCount(0);
});

test("存在しない日付は 404 画面", async ({ page }) => {
  await page.goto("/day/2099-12-31");
  await expect(page.getByRole("heading", { level: 1, name: "記録が見つかりません" })).toBeVisible();
  await page.getByRole("link", { name: "履歴に戻る" }).click();
  await expect(page).toHaveURL(/\/history$/);
});

test("金曜は損益が週次（5 取引日）と明示される", async ({ page }) => {
  await page.goto("/day/2099-01-09");
  await expect(page.getByTestId("closeout-scope")).toContainText("週次（直近 5 取引日の合計）");
});

test("直近の取引日は手仕舞い前の案内を出す", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText(/手仕舞いは翌 4:30（JST）に実行されます/)).toBeVisible();
});

test("金曜のノートの確定表が表として表示される", async ({ page }) => {
  await page.goto("/day/2099-01-09");
  await expect(page.getByRole("table")).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "取引日" })).toBeVisible();
});
