/** 手元専用モードの設定読み取りと SDK クライアント生成（#393）。サーバー専用。
 *
 * 🔴 認証は SSO プロファイル（`fromIni`）だけ。環境変数の AWS_ACCESS_KEY_ID 等は読まない。
 * さらに、解決された認証情報に sessionToken が無い（= 長期アクセスキー）なら拒否する。
 */
import "server-only";
import { InvokeCommand, LambdaClient } from "@aws-sdk/client-lambda";
import { fromIni } from "@aws-sdk/credential-providers";
import { DataSourceError } from "./data-source";
import {
  LambdaInvokeDataSource,
  createLambdaInvoker,
  type LambdaSender,
} from "./lambda-data-source";
import type { EnvLike } from "../local-mode";

type CredentialProvider = ReturnType<typeof fromIni>;
type Credentials = Awaited<ReturnType<CredentialProvider>>;

export interface LambdaModeConfig {
  functionName: string;
  region: string;
  profile: string;
}

/** 必須設定を env から読む。欠けていれば（値は出さず）変数名だけで失敗する。 */
export function readLambdaModeConfig(env: EnvLike): LambdaModeConfig {
  const read = (name: string): string => {
    const value = env[name]?.trim();
    if (!value) throw new DataSourceError(`手元専用モードの設定 ${name} が未設定です`);
    return value;
  };
  return {
    functionName: read("READ_API_FUNCTION_NAME"),
    region: read("AWS_REGION"),
    profile: read("AWS_PROFILE"),
  };
}

/** 一時認証情報（sessionToken あり）だけを通す。長期キーは拒否（ガードレール #8・Issue #393）。 */
export function requireTemporaryCredentials(
  provider: CredentialProvider,
): CredentialProvider {
  return async (identityProperties) => {
    const credentials: Credentials = await provider(identityProperties);
    if (!credentials.sessionToken) {
      throw new DataSourceError(
        "長期アクセスキーは使えません。SSO プロファイル（aws sso login）を指定してください",
      );
    }
    return credentials;
  };
}

export function createLambdaDataSource(config: LambdaModeConfig): LambdaInvokeDataSource {
  const client = new LambdaClient({
    region: config.region,
    credentials: requireTemporaryCredentials(fromIni({ profile: config.profile })),
  });
  const sender: LambdaSender = client;
  return new LambdaInvokeDataSource(
    createLambdaInvoker(sender, config.functionName, (input) => new InvokeCommand(input)),
    () => client.destroy(),
  );
}
