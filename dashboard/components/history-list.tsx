import Link from "next/link";
import type { HistorySummary } from "../lib/read-api";
import { decisionStatusLabel, directionLabel } from "../lib/format";

/** 履歴一覧。損益は出さない（行ごとに S3 を読むことになるため・#392 の方針）。 */
export function HistoryList({ rows }: { rows: readonly HistorySummary[] }) {
  if (rows.length === 0) {
    return <p>これ以上の記録はありません。</p>;
  }
  return (
    <ul className="divide-y divide-neutral-300 dark:divide-neutral-700 rounded-lg border border-neutral-300 dark:border-neutral-700">
      {rows.map((row) => (
        <li key={row.date} className="p-3">
          <Link
            href={`/day/${row.date}`}
            className="block underline decoration-1 underline-offset-2"
          >
            <span className="font-semibold">{row.date}</span>
          </Link>
          <p className="mt-1 break-words">
            {directionLabel(row.direction)}・{decisionStatusLabel(row.status)}
            {row.has_note ? "・ノートあり" : ""}
          </p>
          {row.rationale_summary ? (
            <p className="mt-1 break-words text-sm text-neutral-600 dark:text-neutral-300">
              {row.rationale_summary}
            </p>
          ) : null}
        </li>
      ))}
    </ul>
  );
}
