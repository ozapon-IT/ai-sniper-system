import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import tsconfigPaths from "vite-tsconfig-paths";
import { defineConfig } from "vitest/config";

// 出典: node_modules/next/dist/docs/01-app/02-guides/testing/vitest.md（next@16.4.6 同梱・参照日 2026-11-22）
export default defineConfig({
  plugins: [tsconfigPaths(), react()],
  test: {
    // `server-only` は Client Component から import されると投げる仕組み（Next のビルドが解決する）。
    // vitest はその解決を持たないため、テストでは空モジュールに差し替える。
    alias: { "server-only": fileURLToPath(new URL("./vitest.server-only-stub.ts", import.meta.url)) },
    environment: "jsdom",
    include: ["__tests__/**/*.test.{ts,tsx}"],
    setupFiles: ["./vitest.setup.ts"],
  },
});
