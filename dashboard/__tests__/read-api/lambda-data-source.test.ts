import { describe, expect, it, vi } from "vitest";
import {
  DataSourceError,
  LambdaInvokeDataSource,
  createLambdaInvoker,
  type ReadApiEvent,
  ReadApiInvokeError,
  type ReadApiInvoker,
} from "../../lib/read-api";
import { requireTemporaryCredentials } from "../../lib/read-api/lambda-config";
import dashboard from "./fixtures/dashboard.json";
import dayFixture from "./fixtures/day_with_closeout.json";
import history from "./fixtures/history.json";

const FUNCTION_NAME = "secret-func-name-xyz";
const ARN = "arn:aws:lambda:ap-northeast-1:123456789012:function:secret-func-name-xyz";

function respond(statusCode: number, body: unknown): ReadApiInvoker {
  return vi.fn(async () => ({ statusCode, body: JSON.stringify(body) }));
}

describe("LambdaInvokeDataSource", () => {
  it("getDashboard / getHistory / getDay が read-api の契約どおり parse される", async () => {
    expect(await new LambdaInvokeDataSource(respond(200, dashboard)).getDashboard()).toEqual(
      dashboard,
    );
    expect(await new LambdaInvokeDataSource(respond(200, history)).getHistory()).toEqual(history);
    const day = await new LambdaInvokeDataSource(respond(200, dayFixture)).getDay(
      dayFixture.date,
    );
    expect(day?.date).toBe(dayFixture.date);
  });

  it("handler.py のルーティングに合うイベント（rawPath / http.method / query）を送る", async () => {
    const invoke = vi.fn<ReadApiInvoker>(async () => ({
      statusCode: 200,
      body: JSON.stringify(history),
    }));
    await new LambdaInvokeDataSource(invoke).getHistory({ limit: 5, before: "2099-01-10" });
    expect(invoke).toHaveBeenCalledWith({
      rawPath: "/history",
      requestContext: { http: { method: "GET" } },
      queryStringParameters: { limit: "5", before: "2099-01-10" },
    } satisfies ReadApiEvent);
  });

  it("getDay の 404（記録なし）は null、それ以外の失敗は DataSourceError", async () => {
    expect(await new LambdaInvokeDataSource(respond(404, { error: "x" })).getDay("2099-01-06")).toBe(
      null,
    );
    await expect(
      new LambdaInvokeDataSource(respond(500, { error: "x" })).getDay("2099-01-06"),
    ).rejects.toBeInstanceOf(DataSourceError);
    // 404 を null にするのは getDay だけ（history の 404 は取得失敗）。
    await expect(
      new LambdaInvokeDataSource(respond(404, { error: "x" })).getHistory(),
    ).rejects.toBeInstanceOf(DataSourceError);
  });

  it("契約に合わない応答・JSON でない応答は DataSourceError", async () => {
    await expect(
      new LambdaInvokeDataSource(respond(200, { nope: 1 })).getDashboard(),
    ).rejects.toBeInstanceOf(DataSourceError);
    await expect(
      new LambdaInvokeDataSource(async () => ({ statusCode: 200, body: "<html>" })).getDashboard(),
    ).rejects.toBeInstanceOf(DataSourceError);
  });

  it("不正な引数は invoke せずに DataSourceError", async () => {
    const invoke = vi.fn<ReadApiInvoker>();
    const ds = new LambdaInvokeDataSource(invoke);
    await expect(ds.getHistory({ limit: 0 })).rejects.toBeInstanceOf(DataSourceError);
    await expect(ds.getHistory({ before: "x" })).rejects.toBeInstanceOf(DataSourceError);
    await expect(ds.getDay("../etc")).rejects.toBeInstanceOf(DataSourceError);
    expect(invoke).not.toHaveBeenCalled();
  });

  it("🔴 AWS のエラー本文（関数名・ARN）が DataSourceError に漏れない", async () => {
    const awsError = Object.assign(
      new Error(`User is not authorized to perform lambda:InvokeFunction on ${ARN} (${FUNCTION_NAME})`),
      { name: "AccessDeniedException" },
    );
    const ds = new LambdaInvokeDataSource(async () => {
      throw awsError;
    });
    const error = await ds.getDashboard().catch((e: unknown) => e);
    expect(error).toBeInstanceOf(DataSourceError);
    const text = JSON.stringify({
      message: (error as Error).message,
      stack: (error as Error).stack,
      cause: (error as Error).cause,
    });
    expect(text).not.toContain(FUNCTION_NAME);
    expect(text).not.toContain("arn:aws");
    expect((error as Error).message).toContain("AccessDeniedException");
  });
});

describe("失敗理由の伝え方（指摘 #3）", () => {
  const invokeThrowing = (error: unknown) =>
    new LambdaInvokeDataSource(async () => {
      throw error;
    });

  it("自前の例外（固定文言）は理由がそのまま届く", async () => {
    for (const own of [
      new ReadApiInvokeError("read-api が関数エラーを返しました（本文は出しません）"),
      new DataSourceError("長期アクセスキーは使えません。SSO プロファイル（aws sso login）を指定してください"),
    ]) {
      const error = await invokeThrowing(own).getDashboard().catch((e: unknown) => e);
      expect((error as Error).message).toContain(own.message);
    }
  });

  it("SDK が自前の例外を cause に包んでも理由が届く", async () => {
    const wrapped = Object.assign(new Error(`wrapped ${ARN}`), {
      name: "CredentialsProviderError",
      cause: new DataSourceError("長期アクセスキーは使えません"),
    });
    const error = await invokeThrowing(wrapped).getDashboard().catch((e: unknown) => e);
    expect((error as Error).message).toContain("長期アクセスキーは使えません");
    expect((error as Error).message).not.toContain("arn:aws");
  });

  it("タイムアウトだけが resetClient を立てる（クライアント作り直しの合図）", async () => {
    const timeout = await invokeThrowing(
      new ReadApiInvokeError("15 秒以内に応答がありませんでした", { timedOut: true }),
    )
      .getDashboard()
      .catch((e: unknown) => e);
    expect((timeout as DataSourceError).resetClient).toBe(true);
    const other = await invokeThrowing(new ReadApiInvokeError("FunctionError"))
      .getDashboard()
      .catch((e: unknown) => e);
    expect((other as DataSourceError).resetClient).toBe(false);
  });

  it("destroy() は注入された後始末（client.destroy）を呼ぶ", () => {
    const dispose = vi.fn();
    new LambdaInvokeDataSource(async () => ({ statusCode: 200, body: "{}" }), dispose).destroy();
    expect(dispose).toHaveBeenCalledTimes(1);
  });

  it("AWS 由来の例外は種別 name だけ（本文は出さない）", async () => {
    const aws = Object.assign(new Error(`denied ${ARN}`), { name: "AccessDeniedException" });
    const error = await invokeThrowing(aws).getDashboard().catch((e: unknown) => e);
    expect((error as Error).message).toContain("AccessDeniedException");
    expect((error as Error).message).not.toContain("arn:aws");
  });
});

describe("createLambdaInvoker", () => {
  const make = (input: unknown) => ({ input });

  it("InvokeCommand（RequestResponse・JSON ペイロード）で呼び、statusCode / body を返す", async () => {
    const send = vi.fn(async () => ({
      Payload: new TextEncoder().encode(JSON.stringify({ statusCode: 200, body: "{}" })),
    }));
    const invoke = createLambdaInvoker({ send }, FUNCTION_NAME, make);
    const event: ReadApiEvent = {
      rawPath: "/dashboard",
      requestContext: { http: { method: "GET" } },
    };
    expect(await invoke(event)).toEqual({ statusCode: 200, body: "{}" });
    const [command, options] = send.mock.calls[0] as unknown as [
      { input: { FunctionName: string; InvocationType: string; Payload: Uint8Array } },
      { abortSignal: AbortSignal },
    ];
    expect(command.input.FunctionName).toBe(FUNCTION_NAME);
    expect(command.input.InvocationType).toBe("RequestResponse");
    expect(JSON.parse(new TextDecoder().decode(command.input.Payload))).toEqual(event);
    expect(options.abortSignal).toBeInstanceOf(AbortSignal);
  });

  it("🔴 認証情報の解決でハングしても timeout で打ち切る（指摘 #4）", async () => {
    // send が永遠に解決しない（SSO 解決待ち等）。abortSignal だけでは止まらない経路を Promise.race で止める。
    const never = createLambdaInvoker({ send: () => new Promise(() => {}) }, FUNCTION_NAME, make, 20);
    const event: ReadApiEvent = {
      rawPath: "/dashboard",
      requestContext: { http: { method: "GET" } },
    };
    const error = await never(event).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ReadApiInvokeError);
    expect((error as Error).message).toContain("応答がありませんでした");
  });

  it("FunctionError・不正ペイロードは本文を含まない例外にする", async () => {
    const leaky = new TextEncoder().encode(
      JSON.stringify({ errorMessage: `boom ${ARN}`, stackTrace: [FUNCTION_NAME] }),
    );
    const event: ReadApiEvent = {
      rawPath: "/dashboard",
      requestContext: { http: { method: "GET" } },
    };
    const withFnError = createLambdaInvoker(
      { send: async () => ({ FunctionError: "Unhandled", Payload: leaky }) },
      FUNCTION_NAME,
      make,
    );
    const err1 = await withFnError(event).catch((e: Error) => e);
    expect(err1).toBeInstanceOf(ReadApiInvokeError);
    expect((err1 as Error).message).not.toContain(ARN);
    expect((err1 as Error).message).not.toContain(FUNCTION_NAME);
    const bad = createLambdaInvoker(
      { send: async () => ({ Payload: leaky }) },
      FUNCTION_NAME,
      make,
    );
    const err2 = await bad(event).catch((e: Error) => e);
    expect(err2).toBeInstanceOf(ReadApiInvokeError);
    expect((err2 as Error).message).not.toContain(ARN);
  });
});

describe("requireTemporaryCredentials（長期キー拒否）", () => {
  it("sessionToken ありは通し、なし（長期アクセスキー）は拒否する", async () => {
    const temp = requireTemporaryCredentials(async () => ({
      accessKeyId: "ASIAXXXX",
      secretAccessKey: "s",
      sessionToken: "t",
    }));
    await expect(temp()).resolves.toMatchObject({ accessKeyId: "ASIAXXXX" });
    const longTerm = requireTemporaryCredentials(async () => ({
      accessKeyId: "AKIAXXXX",
      secretAccessKey: "s",
    }));
    await expect(longTerm()).rejects.toBeInstanceOf(DataSourceError);
  });
});
