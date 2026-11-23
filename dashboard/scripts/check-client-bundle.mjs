// 配布物に、AWS の認証情報・SDK・Lambda の関数名 / ARN・実リソース名が混ざっていないかを検査する
// （#393・#394・ガードレール #8）。`npm run build` の後に実行する。
//   - クライアント: .next/static（ブラウザへ配るバンドル）。AWS SDK の文字列も禁止。
//   - サーバー: .next/server（Vercel に載る。プリレンダー済み HTML / RSC も含む）。SDK 自体は
//     手元専用モードのために載り得るので、「実在の値」だけを禁止する。
// 関数名は READ_API_FUNCTION_NAME が設定されていれば、その値そのものも探す。
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const DASHBOARD_DIR = fileURLToPath(new URL("..", import.meta.url));
const ROOT = join(DASHBOARD_DIR, ".next", "static");
// Vercel へ載るサーバー側成果物（#394）。AWS SDK はライブラリの文字列（エンドポイントのテンプレート等）を
// 含むため、ここでは「実在の値」だけを探す（アカウント ID 入りの ARN・アクセスキー ID・関数名の値・リソース名）。
const SERVER_ROOT = join(DASHBOARD_DIR, ".next", "server");

// 関数名の値も探すため、`.env.local`（README が関数名を置く場所）を読む。既存の環境変数は上書きされない
// （Node の process.loadEnvFile の仕様）。無ければ何もしない。
// 出典: https://nodejs.org/api/process.html#processloadenvfilepath（参照日 2026-11-23）
for (const name of [".env.local"]) {
  const path = join(DASHBOARD_DIR, name);
  if (existsSync(path)) process.loadEnvFile(path);
}
// 実リソース名: 関数名 ai-sniper-<env>-read-api・S3 バケット・tfstate・シークレット名など（ai-sniper-* / ai-sniper/* の形）。
// リポジトリ名 ai-sniper-system は除外する（ビルドのパスに出るため）。クライアント・サーバーの両方で禁止する。
const RESOURCE_NAME = [/ai-sniper(?!-system\b)[-/][a-z0-9]/, "実リソース名（ai-sniper-*）"];
const FORBIDDEN = [
  [/arn:aws[a-z-]*:/, "ARN"],
  [/@aws-sdk\//, "AWS SDK"],
  [/READ_API_FUNCTION_NAME/, "関数名の環境変数名"],
  [/lambda:InvokeFunction/, "IAM アクション名"],
  [/\b(AKIA|ASIA)[0-9A-Z]{16}\b/, "AWS アクセスキー ID"],
  [/aws_secret_access_key|AWS_SECRET_ACCESS_KEY/i, "AWS シークレットキー"],
  RESOURCE_NAME,
];
const functionName = process.env.READ_API_FUNCTION_NAME?.trim();
if (!functionName) {
  console.warn("注意: READ_API_FUNCTION_NAME が未設定のため、関数名の値そのものの検査はスキップします（パターン検査は実施）");
} else FORBIDDEN.push([new RegExp(functionName.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")), "関数名の値"]);

const SERVER_FORBIDDEN = [
  [/arn:aws[a-z-]*:[a-z0-9-]*:[a-z0-9-]*:\d{12}:/, "アカウント ID 入りの ARN"],
  [/\b(AKIA|ASIA)[0-9A-Z]{16}\b/, "AWS アクセスキー ID"],
  RESOURCE_NAME,
];
if (functionName) SERVER_FORBIDDEN.push(FORBIDDEN[FORBIDDEN.length - 1]);

function* walk(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) yield* walk(path);
    else yield path;
  }
}

if (!existsSync(ROOT)) {
  console.error(`${ROOT} がありません。先に npm run build を実行してください。`);
  process.exit(2);
}

let scannedClient = 0;
let scannedServer = 0;
const problems = [];
for (const file of walk(ROOT)) {
  if (!/\.(js|css|html|json|map|txt)$/.test(file)) continue;
  const text = readFileSync(file, "utf8");
  scannedClient += 1;
  for (const [re, label] of FORBIDDEN) if (re.test(text)) problems.push(`[クライアント] ${file}: ${label}`);
}
if (scannedClient === 0) {
  console.error(`検査対象が 0 件です（${ROOT}）。先に npm run build を実行してください。`);
  process.exit(2);
}
if (existsSync(SERVER_ROOT)) {
  for (const file of walk(SERVER_ROOT)) {
    if (!/\.(js|json|map|html|rsc|txt)$/.test(file)) continue;
    const text = readFileSync(file, "utf8");
    scannedServer += 1;
    for (const [re, label] of SERVER_FORBIDDEN) if (re.test(text)) problems.push(`[サーバー] ${file}: ${label}`);
  }
}
if (problems.length > 0) {
  console.error(`配布物に AWS 関連の文字列・実リソース名があります:\n${problems.join("\n")}`);
  process.exit(1);
}
console.log(`OK: クライアント ${scannedClient} / サーバー ${scannedServer} ファイルに AWS の認証情報・関数名・ARN・実リソース名はありません`);
