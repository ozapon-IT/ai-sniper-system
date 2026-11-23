import { expect, test } from "@playwright/test";

// 公開デモ（合成データ版）の 3 画面が見えること、実データ・AWS の痕跡が無いことの確認（#394）。

test("当日 → 履歴 → 日次詳細の 3 画面が見られる", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("直近の取引日（2099-01-12）");

  await page.getByRole("navigation", { name: "メイン" }).getByRole("link", { name: "履歴" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "履歴" })).toBeVisible();

  await page.getByRole("link", { name: "2099-01-07" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "2099-01-07" })).toBeVisible();
  await expect(page.getByRole("heading", { level: 2, name: "手仕舞い" })).toBeVisible();
});

test("合成データであることが画面に明記され、HTML に AWS の痕跡が無い", async ({ page, request }) => {
  await page.goto("/");
  await expect(page.getByText(/合成|架空|デモ/).first()).toBeVisible();
  for (const path of ["/", "/history", "/day/2099-01-07"]) {
    const html = await (await request.get(path)).text();
    expect(html).not.toMatch(/arn:aws|AKIA[0-9A-Z]{16}|READ_API_FUNCTION_NAME|execute-api|lambda-url|ai-sniper(?!-system\b)[-/][a-z0-9]/);
  }
});

// 存在しない日付は 404、セキュリティヘッダーが付くこと（#413）。
test("存在しない日付は 404 で、セキュリティヘッダーが付く", async ({ request }) => {
  const missing = await request.get("/day/1999-01-01");
  expect(missing.status()).toBe(404);
  const ok = await request.get("/day/2099-01-07");
  expect(ok.status()).toBe(200);
  for (const res of [missing, ok]) {
    const h = res.headers();
    expect(h["x-content-type-options"]).toBe("nosniff");
    expect(h["x-frame-options"]).toBe("DENY");
    expect(h["referrer-policy"]).toMatch(/strict-origin/);
    expect(h["permissions-policy"]).toMatch(/camera=\(\)/);
    expect(h["content-security-policy"]).toMatch(/frame-ancestors 'none'/);
    // 読み込み制限（#415）。本番に dev 専用の 'unsafe-eval' が入っていないこと。
    expect(h["content-security-policy"]).toMatch(/default-src 'self'/);
    expect(h["content-security-policy"]).toMatch(/script-src 'self' 'unsafe-inline'(?!.*unsafe-eval)/);
    expect(h["content-security-policy"]).toMatch(/connect-src 'self'/);
  }
});
