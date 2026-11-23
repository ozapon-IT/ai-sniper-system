// 手元専用モード（#393）の起動口。実データ（read-api を IAM で invoke）を 127.0.0.1 だけで見る。
// 必須の env（READ_API_FUNCTION_NAME / AWS_REGION / AWS_PROFILE）は `.env.local`（git 管理外）に置く。
// 🔴 常に `-H 127.0.0.1` を付け、`DASHBOARD_BIND_HOST` で宣言する。`next start`（本番ビルド）では使えない。
import { spawn } from "node:child_process";

const child = spawn("npx", ["next", "dev", "-H", "127.0.0.1"], {
  stdio: "inherit",
  env: {
    ...process.env,
    DASHBOARD_DATA_SOURCE: "lambda",
    DASHBOARD_BIND_HOST: "127.0.0.1",
  },
});
child.on("exit", (code) => process.exit(code ?? 1));
