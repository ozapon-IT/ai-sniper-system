"use client";

// 再試行は `retry()`（サーバーのデータを取り直して再描画）を使う。`reset()` はエラー状態を消して
// 再描画するだけで再取得しないため、取得失敗の再試行には不適切。
// 出典: next@16.4.6 同梱 docs/01-app/03-api-reference/03-file-conventions/error.md
// の `retry` / `reset` の節（"In most cases, you should use `retry()` instead"・`retry` は
// v16.3.0 で安定化。参照日 2026-11-22）。
export default function ErrorPage({ retry }: { error: Error; retry: () => void }) {
  return (
    <div role="alert" className="space-y-3">
      <h1 className="text-2xl font-semibold">データを取得できませんでした</h1>
      <p>通信の失敗、または応答が不正でした。しばらくしてからもう一度お試しください。</p>
      <button
        type="button"
        onClick={() => retry()}
        className="rounded border border-neutral-500 px-3 py-1"
      >
        再試行
      </button>
    </div>
  );
}
