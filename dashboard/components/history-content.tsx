import Link from "next/link";
import { getDataSource } from "../lib/data-source";
import { dateString } from "../lib/read-api";
import { HistoryList } from "./history-list";

export const DEFAULT_LIMIT = 20;
export const MAX_LIMIT = 100; // handler と同じ上限

export function first(v: string | string[] | undefined): string | undefined {
  return Array.isArray(v) ? v[0] : v;
}

export async function HistoryContent({ before, limit: limitRaw }: { before?: string; limit?: string }) {
  const limit = limitRaw === undefined ? DEFAULT_LIMIT : Number(limitRaw);
  const valid =
    Number.isInteger(limit) &&
    limit >= 1 &&
    limit <= MAX_LIMIT &&
    (before === undefined || dateString.safeParse(before).success);
  if (!valid) {
    return (
      <p role="alert">
        指定が不正です（limit は 1〜{MAX_LIMIT}、before は YYYY-MM-DD）。{" "}
        <Link href="/history" className="underline underline-offset-2">
          先頭に戻る
        </Link>
      </p>
    );
  }

  const { summaries } = await getDataSource().getHistory({ limit, before });
  const last = summaries[summaries.length - 1];
  const nextHref =
    summaries.length === limit && last ? `/history?before=${last.date}&limit=${limit}` : null;
  return (
    <div className="space-y-4">
      <HistoryList rows={summaries} />
      <nav aria-label="ページ送り" className="flex gap-4">
        {before !== undefined && (
          <Link href={`/history?limit=${limit}`} className="underline underline-offset-2">
            先頭へ
          </Link>
        )}
        {nextHref && (
          <Link href={nextHref} className="underline underline-offset-2">
            さらに古い日へ
          </Link>
        )}
      </nav>
    </div>
  );
}
