import { expect, test } from "@playwright/test";

// 存在しない日付は HTTP 404、全ページにセキュリティヘッダー（#413）。
// 出典: node_modules/next/dist/docs/01-app/02-guides/streaming.md「The HTTP contract」（参照日 2026-11-23）

const REQUIRED_HEADERS: Record<string, RegExp> = {
  "x-content-type-options": /^nosniff$/,
  "x-frame-options": /^DENY$/,
  "referrer-policy": /strict-origin/,
  "permissions-policy": /camera=\(\)/,
  "content-security-policy": /frame-ancestors 'none'/,
};

// CSP の読み込み制限（#415）。出典: node_modules/next/dist/docs/01-app/02-guides/content-security-policy.md（参照日 2026-11-23）
const CSP_DIRECTIVES = [/default-src 'self'/, /script-src 'self' 'unsafe-inline'(?!.*unsafe-eval)/, /style-src 'self' 'unsafe-inline'/, /connect-src 'self'/, /object-src 'none'/];

test("存在しない日付は 404 で、見つかりません画面とヘッダーが付く", async ({ page }) => {
  for (const path of ["/day/1999-01-01", "/day/not-a-date", "/day/%E0%A4%A"]) {
    const res = await page.goto(path);
    expect(res?.status(), path).toBe(404);
    await expect(page.getByRole("heading", { level: 1 })).toContainText("記録が見つかりません");
    for (const [name, re] of Object.entries(REQUIRED_HEADERS)) {
      expect(res?.headers()[name], `${path} ${name}`).toMatch(re);
    }
  }
});

test("存在する日付は 200 で、コンソールエラー無しにヘッダーが付く", async ({ page }) => {
  const errors: string[] = [];
  page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
  page.on("pageerror", (e) => errors.push(e.message));
  const res = await page.goto("/day/2099-01-07");
  expect(res?.status()).toBe(200);
  await expect(page.getByRole("heading", { level: 1, name: "2099-01-07" })).toBeVisible();
  for (const [name, re] of Object.entries(REQUIRED_HEADERS)) {
    expect(res?.headers()[name], name).toMatch(re);
  }
  expect(errors).toEqual([]);
});

// 主要画面で CSP 違反（securitypolicyviolation / コンソールエラー）が 0 件であること（#415）。
// 違反の受け口（report-to）は作らない方針のため、ブラウザ側のイベントで拾う。
for (const path of ["/", "/history", "/day/2099-01-07", "/day/1999-01-01"]) {
  test(`CSP 違反が出ない: ${path}`, async ({ page }) => {
    const problems: string[] = [];
    await page.addInitScript(() => {
      document.addEventListener("securitypolicyviolation", (e) => {
        console.error(`CSPVIOLATION ${e.violatedDirective} ${e.blockedURI}`);
      });
    });
    page.on("console", (m) => m.type() === "error" && problems.push(m.text()));
    page.on("pageerror", (e) => problems.push(e.message));
    const res = await page.goto(path);
    const csp = res?.headers()["content-security-policy"] ?? "";
    for (const re of CSP_DIRECTIVES) expect(csp, `${path} CSP`).toMatch(re);
    await page.waitForLoadState("networkidle");
    // 404 はブラウザ自身が「Failed to load resource」を出すため、CSP 違反だけを見る。
    const relevant = res?.status() === 404 ? problems.filter((t) => t.includes("CSPVIOLATION")) : problems;
    expect(relevant).toEqual([]);
  });
}

// アプリ内遷移（RSC の fetch は connect-src の対象）でも CSP 違反が出ないこと（#415 レビュー指摘 1）。
// Next は RSC 取得に失敗するとページ全体読み込みに切り替えるため、遷移が成功するだけでは検知できない。
// そこで document の読み込み回数が増えない（= MPA 遷移に落ちていない）ことも確認する。
test("アプリ内遷移（/ → 履歴 → 日次詳細）で CSP 違反が出ず、MPA 遷移に落ちない", async ({ page }) => {
  const problems: string[] = [];
  await page.addInitScript(() => {
    document.addEventListener("securitypolicyviolation", (e) => {
      console.error(`CSPVIOLATION ${e.violatedDirective} ${e.blockedURI}`);
    });
  });
  page.on("console", (m) => m.type() === "error" && problems.push(m.text()));
  page.on("pageerror", (e) => problems.push(e.message));
  let documentLoads = 0;
  page.on("request", (r) => r.resourceType() === "document" && documentLoads++);

  await page.goto("/");
  await page.waitForLoadState("networkidle");
  expect(documentLoads).toBe(1);
  await page.getByRole("navigation", { name: "メイン" }).getByRole("link", { name: "履歴" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "履歴" })).toBeVisible();
  await page.getByRole("link", { name: "2099-01-07" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "2099-01-07" })).toBeVisible();
  await page.waitForLoadState("networkidle");

  expect(documentLoads, "RSC 取得が止まり MPA 遷移に落ちている").toBe(1);
  expect(problems).toEqual([]);
});
