import { Suspense } from "react";
import { HistoryContent, first } from "../../components/history-content";
import Loading from "../loading";

/** 履歴画面。`before`（その日より前）と `limit` でページ送りする。 */
export default function HistoryPage({ searchParams }: PageProps<"/history">) {
  return (
    <>
      <h1 className="mb-4 text-2xl font-semibold">履歴</h1>
      <Suspense fallback={<Loading />}>
        {/* searchParams は Suspense の内側で await する（静的シェルを最大化・Cache Components） */}
        {searchParams.then((q) => (
          <HistoryContent before={first(q.before)} limit={first(q.limit)} />
        ))}
      </Suspense>
    </>
  );
}
