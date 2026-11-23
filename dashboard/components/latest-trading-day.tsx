import { getDataSource } from "../lib/data-source";
import { DayDetail } from "./day-detail";

/** 当日画面の本体。`/dashboard` は使わず、直近の取引日を `getHistory({limit:1})` + `getDay` で組む（#392）。
 * 判断は 20:30（JST）なので、日中に出るのは前日分。見出しは「今日」ではなく取引日を出す。 */
export async function LatestTradingDay() {
  const ds = getDataSource();
  const { summaries } = await ds.getHistory({ limit: 1 });
  const latest = summaries[0];
  if (!latest) {
    return (
      <>
        <h1 className="mb-4 text-2xl font-semibold">直近の取引日</h1>
        <p>記録がまだありません。</p>
      </>
    );
  }
  const day = await ds.getDay(latest.date);
  return (
    <>
      <h1 className="mb-1 text-2xl font-semibold">直近の取引日（{latest.date}）</h1>
      <p className="mb-4 text-neutral-600 dark:text-neutral-300">
        判断は 20:30（JST）に行われるため、日中に表示されるのは前日分です。
      </p>
      {day ? (
        <DayDetail day={day} rationaleSummary={latest.rationale_summary} />
      ) : (
        <p>この日の詳細は見つかりませんでした。</p>
      )}
    </>
  );
}
