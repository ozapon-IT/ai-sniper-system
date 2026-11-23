import { defineConfig, devices } from "@playwright/test";

// 公開デモ（Vercel）に対するスモークテスト（#394）。webServer は起動せず、DEMO_URL の実環境を叩く。
//   DEMO_URL=https://<project>.vercel.app npm run test:e2e:demo
const baseURL = process.env.DEMO_URL;
if (!baseURL) throw new Error("DEMO_URL を指定してください（例: https://<project>.vercel.app）");

export default defineConfig({
  testDir: "./e2e-demo",
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  reporter: "list",
  use: { baseURL, trace: "off" },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        launchOptions: { executablePath: process.env.PW_CHROMIUM_PATH || undefined },
      },
    },
  ],
});
