import { describe, expect, it } from "vitest";
import {
  LocalModeError,
  assertLocalModeAllowed,
  isAllowedHost,
  isAllowedOrigin,
  isRequestAllowed,
} from "../../lib/local-mode";

/** dev:local で正しく起動したときの env（実 bind が 127.0.0.1・dev サーバー）。 */
const OK = {
  DASHBOARD_DATA_SOURCE: "lambda",
  NODE_ENV: "development",
  DASHBOARD_BIND_HOST: "127.0.0.1",
  __NEXT_DEV_SERVER: "1",
  __NEXT_PRIVATE_ORIGIN: "http://127.0.0.1:3000",
};
const LOCAL = OK;

describe("assertLocalModeAllowed（起動拒否）", () => {
  it("dev:local で起動した（宣言・dev サーバー・実 bind 127.0.0.1）なら通る", () => {
    expect(() => assertLocalModeAllowed(OK)).not.toThrow();
  });

  it.each(["0.0.0.0", "::", "192.168.1.10", "localhost", ""])("バインド宣言 %j は拒否", (host) => {
    expect(() => assertLocalModeAllowed({ ...OK, DASHBOARD_BIND_HOST: host })).toThrow(
      LocalModeError,
    );
  });

  it("宣言なし（dev:local 以外の起動）は拒否", () => {
    expect(() => assertLocalModeAllowed({ ...OK, DASHBOARD_BIND_HOST: undefined })).toThrow(
      LocalModeError,
    );
  });

  it("🔴 宣言が 127.0.0.1 でも、実際の bind が 0.0.0.0（next dev 既定）なら拒否（指摘 #1）", () => {
    // 0.0.0.0 / 既定で listen すると Next は __NEXT_PRIVATE_ORIGIN を http://localhost:<port> にする
    // （next@16.4.6 で実測）。.env.local に宣言を書いて `npm run dev` で起動しても通さない。
    expect(() =>
      assertLocalModeAllowed({ ...OK, __NEXT_PRIVATE_ORIGIN: "http://localhost:3000" }),
    ).toThrow(LocalModeError);
  });

  it("実 bind を確かめられない（内部変数が無い・壊れている）なら拒否（fail-closed）", () => {
    expect(() =>
      assertLocalModeAllowed({ ...OK, __NEXT_PRIVATE_ORIGIN: undefined }),
    ).toThrow(LocalModeError);
    expect(() => assertLocalModeAllowed({ ...OK, __NEXT_PRIVATE_ORIGIN: "not a url" })).toThrow(
      LocalModeError,
    );
  });

  it("本番ビルド（NODE_ENV=production）は拒否", () => {
    expect(() => assertLocalModeAllowed({ ...OK, NODE_ENV: "production" })).toThrow(
      LocalModeError,
    );
  });

  it("🔴 NODE_ENV=development の `next start`（dev サーバーでない）も拒否（指摘 #2）", () => {
    // next start では __NEXT_DEV_SERVER が付かない（実測）。NODE_ENV だけでは弾けない。
    expect(() => assertLocalModeAllowed({ ...OK, __NEXT_DEV_SERVER: undefined })).toThrow(
      LocalModeError,
    );
  });

  it("手元専用モードでなければ何も検査しない（公開デモ・e2e は従来どおり）", () => {
    expect(() => assertLocalModeAllowed({ NODE_ENV: "production" })).not.toThrow();
    expect(() => assertLocalModeAllowed({ DASHBOARD_DATA_SOURCE: "mock" })).not.toThrow();
  });
});

describe("isAllowedHost / isAllowedOrigin", () => {
  it.each(["127.0.0.1:3000", "localhost:3000", "LOCALHOST", "127.0.0.1"])("%s は許可", (h) => {
    expect(isAllowedHost(h)).toBe(true);
  });

  it.each([
    "0.0.0.0:3000",
    "192.168.1.10:3000",
    "evil.example.com",
    "127.0.0.1.evil.example.com:3000",
    "localhost.evil.example.com",
    "evil.com@127.0.0.1",
    "127.0.0.1/evil",
    "",
    null,
    undefined,
  ])("Host %j は拒否（LAN IP・DNS rebinding・不正形式）", (h) => {
    expect(isAllowedHost(h)).toBe(false);
  });

  it("Origin: 付かないか、Host と同じホスト・ポートの loopback http だけ許可", () => {
    expect(isAllowedOrigin(null, "127.0.0.1:3000")).toBe(true);
    expect(isAllowedOrigin("http://127.0.0.1:3000", "127.0.0.1:3000")).toBe(true);
    expect(isAllowedOrigin("http://localhost:3000", "localhost:3000")).toBe(true);
    expect(isAllowedOrigin("https://127.0.0.1:3000", "127.0.0.1:3000")).toBe(false);
    expect(isAllowedOrigin("http://evil.example.com", "127.0.0.1:3000")).toBe(false);
    expect(isAllowedOrigin("null", "127.0.0.1:3000")).toBe(false);
  });

  it("🔴 別ポートの loopback（別アプリのページ）の Origin は拒否（指摘 #10）", () => {
    expect(isAllowedOrigin("http://localhost:5173", "localhost:3000")).toBe(false);
    expect(isAllowedOrigin("http://127.0.0.1:5173", "127.0.0.1:3000")).toBe(false);
    expect(isAllowedOrigin("http://localhost:3000", "127.0.0.1:3000")).toBe(false);
    expect(isAllowedOrigin("http://127.0.0.1:3000", undefined)).toBe(false);
  });
});

describe("isRequestAllowed", () => {
  it("手元専用モード: loopback の Host / Origin だけ通る", () => {
    expect(isRequestAllowed(LOCAL, { host: "127.0.0.1:3000" })).toBe(true);
    expect(isRequestAllowed(LOCAL, { host: "192.168.1.10:3000" })).toBe(false);
    expect(
      isRequestAllowed(LOCAL, { host: "127.0.0.1:3000", origin: "http://evil.example.com" }),
    ).toBe(false);
    expect(
      isRequestAllowed(LOCAL, { host: "127.0.0.1:3000", origin: "http://127.0.0.1:3000" }),
    ).toBe(true);
  });

  it("手元専用モードの本番ビルド・dev サーバー以外・実 bind が loopback でないときは全拒否", () => {
    const host = { host: "127.0.0.1:3000" };
    expect(isRequestAllowed({ ...LOCAL, NODE_ENV: "production" }, host)).toBe(false);
    expect(isRequestAllowed({ ...LOCAL, __NEXT_DEV_SERVER: undefined }, host)).toBe(false);
    // Host を偽装して LAN から来ても、実 bind が 0.0.0.0 なら通さない（指摘 #1）。
    expect(
      isRequestAllowed({ ...LOCAL, __NEXT_PRIVATE_ORIGIN: "http://localhost:3000" }, host),
    ).toBe(false);
  });

  it("手元専用モードでなければ素通し（公開デモの挙動を変えない）", () => {
    expect(isRequestAllowed({ NODE_ENV: "production" }, { host: "example.com" })).toBe(true);
  });
});
