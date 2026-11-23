import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";
import { isSyntheticData } from "../lib/data-source";
import "./globals.css";

export const metadata: Metadata = {
  title: "AI Sniper Dashboard",
  description: "AI Sniper System ダッシュボード",
};

export default function RootLayout({
  children,
}: {
  children: ReactNode;
}) {
  return (
    <html lang="ja" className="h-full antialiased">
      <body className="min-h-full flex flex-col">
        <header className="border-b border-neutral-300 dark:border-neutral-700">
          <div className="mx-auto flex w-full max-w-3xl flex-wrap items-center gap-x-6 gap-y-1 px-4 py-3">
            <span className="font-semibold">AI Sniper</span>
            <nav aria-label="メイン" className="flex gap-4">
              <Link href="/" className="underline underline-offset-2">
                直近の取引日
              </Link>
              <Link href="/history" className="underline underline-offset-2">
                履歴
              </Link>
            </nav>
          </div>
        </header>
        <main className="mx-auto w-full max-w-3xl flex-1 px-4 py-6">{children}</main>
        <footer className="mx-auto w-full max-w-3xl px-4 py-4 text-sm text-neutral-600 dark:text-neutral-300">
          {isSyntheticData()
            ? "表示データはデモ用の合成データです。"
            : "表示データは実データです（手元専用モード・127.0.0.1 限定）。口座残高は判断バッチが取得できた日のみ表示します（無い日は「未記録」）。"}
        </footer>
      </body>
    </html>
  );
}
