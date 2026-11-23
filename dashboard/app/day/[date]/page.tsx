import { Suspense } from "react";
import { DayContent } from "../../../components/day-content";
import Loading from "../../loading";

/** 日次詳細画面（判断・手仕舞い・ノート）。記録なし（404）は not-found.tsx。 */
export default function DayPage({ params }: PageProps<"/day/[date]">) {
  return (
    <Suspense fallback={<Loading />}>
      {/* params は Suspense の内側で await する（Cache Components） */}
      {params.then(({ date }) => (
        <DayContent date={date} />
      ))}
    </Suspense>
  );
}
