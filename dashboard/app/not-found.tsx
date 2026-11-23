import Link from "next/link";

export default function NotFound() {
  return (
    <div className="space-y-3">
      <h1 className="text-2xl font-semibold">記録が見つかりません</h1>
      <p>指定された日の記録はありません（休場・未実行、または日付が不正です）。</p>
      <Link href="/history" className="underline underline-offset-2">
        履歴に戻る
      </Link>
    </div>
  );
}
