/** 手元専用モードの DataSource: read-api Lambda を IAM で直接 invoke する（#393・§1.6.1）。
 *
 * 🔴 サーバー側（Next の Server Component）だけで使う。ブラウザへは「画面の HTML / RSC ペイロード」しか
 * 渡らず、AWS の認証情報・関数名・ARN は渡らない。失敗時の例外メッセージにも関数名・ARN・
 * AWS のエラー本文（ARN を含み得る）を入れない（画面・ログに流さない）。
 *
 * 出典（参照日 2026-11-23）:
 * - InvokeCommand / `Payload` は Uint8Array（`transformToString()` 付き）、`FunctionError` は
 *   関数内エラー時のみ付く: https://docs.aws.amazon.com/AWSJavaScriptSDK/v3/latest/client/lambda/command/InvokeCommand/
 *   （同梱の型定義 `@aws-sdk/client-lambda/dist-types/commands/InvokeCommand.d.ts` でも確認）
 * - 認証情報: `fromIni` は SSO プロファイルと role_arn + source_profile の連鎖の両方を解決する
 *   （`fromSSO` は SSO 直接プロファイルのみ）:
 *   https://docs.aws.amazon.com/AWSJavaScriptSDK/v3/latest/client/credential-providers/
 *   （同梱 `@aws-sdk/credential-providers/README.md` の fromIni / fromSSO の節）
 */
import type { ZodType } from "zod";
import { DataSourceError, type DashboardDataSource, type HistoryQuery } from "./data-source";
import { READ_API_PATHS } from "./endpoints";
import {
  dashboardResponseSchema,
  dateString,
  dayResponseSchema,
  historyResponseSchema,
  type DashboardResponse,
  type DayResponse,
  type HistoryResponse,
} from "./schemas";

/** read-api（handler.py）へ渡すイベント。API Gateway HTTP API v2 形（rawPath / requestContext.http）。 */
export interface ReadApiEvent {
  rawPath: string;
  requestContext: { http: { method: "GET" } };
  queryStringParameters?: Record<string, string>;
}

/** read-api の応答（handler.py は statusCode / body の dict を返す）。 */
export interface ReadApiResponse {
  statusCode: number;
  body: string;
}

/** 呼び出し層（invoker）が投げる、**画面・ログに出してよい固定文言**の例外。
 * AWS 由来の例外（本文に ARN・関数名を含み得る）はこれに包まず、種別（name）だけを使う。 */
export class ReadApiInvokeError extends Error {
  /** 応答待ちの上限を超えた（認証情報の解決のハングを含む）。クライアントの作り直しが要る。 */
  readonly timedOut: boolean;

  constructor(message: string, options?: { timedOut?: boolean }) {
    super(message);
    this.name = "ReadApiInvokeError";
    this.timedOut = options?.timedOut ?? false;
  }
}

/** Lambda を呼ぶ関数。SDK への依存はここの外（createLambdaInvoker）に閉じ込める。 */
export type ReadApiInvoker = (event: ReadApiEvent) => Promise<ReadApiResponse>;

export class LambdaInvokeDataSource implements DashboardDataSource {
  /** `dispose` は AWS クライアントの後始末（`client.destroy()`）。作り直すときに呼ぶ。 */
  constructor(
    private readonly invoke: ReadApiInvoker,
    private readonly dispose: () => void = () => {},
  ) {}

  destroy(): void {
    this.dispose();
  }

  getDashboard(): Promise<DashboardResponse> {
    return this.get(READ_API_PATHS.dashboard, undefined, dashboardResponseSchema);
  }

  async getHistory(query: HistoryQuery = {}): Promise<HistoryResponse> {
    const params: Record<string, string> = {};
    if (query.limit !== undefined) {
      if (!Number.isInteger(query.limit) || query.limit < 1 || query.limit > 100) {
        throw new DataSourceError("limit は 1〜100 の整数で指定してください");
      }
      params.limit = String(query.limit);
    }
    if (query.before !== undefined) {
      if (!dateString.safeParse(query.before).success) {
        throw new DataSourceError("before は YYYY-MM-DD で指定してください");
      }
      params.before = query.before;
    }
    return this.get(READ_API_PATHS.history, params, historyResponseSchema);
  }

  async getDay(date: string): Promise<DayResponse | null> {
    if (!dateString.safeParse(date).success) {
      throw new DataSourceError("date は YYYY-MM-DD で指定してください");
    }
    return this.get(READ_API_PATHS.day(date), undefined, dayResponseSchema, true);
  }

  private async get<T>(
    path: string,
    params: Record<string, string> | undefined,
    schema: ZodType<T>,
    notFoundIsNull?: false,
  ): Promise<T>;
  private async get<T>(
    path: string,
    params: Record<string, string> | undefined,
    schema: ZodType<T>,
    notFoundIsNull: true,
  ): Promise<T | null>;
  private async get<T>(
    path: string,
    params: Record<string, string> | undefined,
    schema: ZodType<T>,
    notFoundIsNull = false,
  ): Promise<T | null> {
    const event: ReadApiEvent = {
      rawPath: path,
      requestContext: { http: { method: "GET" } },
      ...(params && Object.keys(params).length > 0 ? { queryStringParameters: params } : {}),
    };
    let response: ReadApiResponse;
    try {
      response = await this.invoke(event);
    } catch (error) {
      throw new DataSourceError(`read-api の呼び出しに失敗しました（${failureReason(error)}）`, {
        resetClient: isTimeout(error),
      });
    }
    if (response.statusCode === 404 && notFoundIsNull) return null;
    if (response.statusCode !== 200) {
      throw new DataSourceError(`read-api が HTTP ${response.statusCode} を返しました`);
    }
    let json: unknown;
    try {
      json = JSON.parse(response.body);
    } catch {
      throw new DataSourceError("read-api の応答が JSON ではありません");
    }
    const parsed = schema.safeParse(json);
    if (!parsed.success) {
      throw new DataSourceError("read-api の応答が契約（schemas.ts）に一致しません");
    }
    return parsed.data;
  }
}

/** タイムアウト（= 解決が止まった認証情報の Promise を SDK が使い回し続ける）か。cause も 1 段見る。 */
function isTimeout(error: unknown): boolean {
  return [error, error instanceof Error ? error.cause : undefined].some(
    (candidate) => candidate instanceof ReadApiInvokeError && candidate.timedOut,
  );
}

/** 失敗理由の文字列。自前の例外（固定文言）は message を通し、それ以外（AWS 由来）は種別 name だけ。
 * AWS のエラー本文は ARN・関数名を含み得るため出さない。SDK が包んだ場合に備え cause も 1 段見る。 */
function failureReason(error: unknown): string {
  for (const candidate of [error, error instanceof Error ? error.cause : undefined]) {
    if (candidate instanceof ReadApiInvokeError || candidate instanceof DataSourceError) {
      return candidate.message;
    }
  }
  return error instanceof Error && error.name ? error.name : "UnknownError";
}

/** AWS SDK の Lambda クライアントのうち、ここが使う部分だけ（テストで差し替える）。 */
export interface LambdaSender {
  send(
    command: unknown,
    options?: { abortSignal?: AbortSignal },
  ): Promise<{ FunctionError?: string; Payload?: Uint8Array }>;
}

/** 1 回の invoke（認証情報の解決を含む）の上限。SSO 期限切れ等でハングしても画面を待たせ続けない。
 * `abortSignal` は SDK の HTTP 層にしか届かず、fromIni / SSO の認証情報解決には効かないため、
 * 全体を Promise.race で打ち切る（解決自体は裏で続き得るが、画面は待たない）。 */
export const INVOKE_TIMEOUT_MS = 15_000;

function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(
      () => reject(new ReadApiInvokeError(`${ms / 1000} 秒以内に応答がありませんでした`, { timedOut: true })),
      ms,
    );
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

/** InvokeCommand 経由で read-api を呼ぶ ReadApiInvoker。
 * `makeCommand` は InvokeCommand のコンストラクタ（SDK を静的 import しないための注入口）。 */
export function createLambdaInvoker(
  client: LambdaSender,
  functionName: string,
  makeCommand: (input: {
    FunctionName: string;
    InvocationType: "RequestResponse";
    Payload: Uint8Array;
  }) => unknown,
  timeoutMs: number = INVOKE_TIMEOUT_MS,
): ReadApiInvoker {
  return async (event) => {
    const abort = new AbortController();
    const result = await withTimeout(
      client.send(
        makeCommand({
          FunctionName: functionName,
          InvocationType: "RequestResponse",
          Payload: new TextEncoder().encode(JSON.stringify(event)),
        }),
        { abortSignal: abort.signal },
      ),
      timeoutMs,
    ).catch((error: unknown) => {
      abort.abort(); // タイムアウト時は HTTP 層の通信も止める
      throw error;
    });
    if (result.FunctionError) {
      // 関数内の未処理例外。本文（errorMessage / stackTrace）は画面・ログへ出さない。
      throw new ReadApiInvokeError("read-api が関数エラーを返しました（本文は出しません）");
    }
    const text = new TextDecoder().decode(result.Payload ?? new Uint8Array());
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      throw new ReadApiInvokeError("read-api の応答ペイロードが不正です");
    }
    if (
      typeof parsed !== "object" ||
      parsed === null ||
      typeof (parsed as ReadApiResponse).statusCode !== "number" ||
      typeof (parsed as ReadApiResponse).body !== "string"
    ) {
      throw new ReadApiInvokeError("read-api の応答ペイロードが不正です");
    }
    const { statusCode, body } = parsed as ReadApiResponse;
    return { statusCode, body };
  };
}
