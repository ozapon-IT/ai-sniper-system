import Link from "next/link";
import { notFound } from "next/navigation";
import { getDataSource } from "../lib/data-source";
import { dateString } from "../lib/read-api";
import { DayDetail } from "./day-detail";

export async function DayContent({ date }: { date: string }) {
  if (!dateString.safeParse(date).success) notFound();
  const day = await getDataSource().getDay(date);
  if (day === null) notFound();
  return (
    <>
      <p className="mb-2">
        <Link href="/history" className="underline underline-offset-2">
          ← 履歴
        </Link>
      </p>
      <h1 className="mb-4 text-2xl font-semibold">{date}</h1>
      <DayDetail day={day} />
    </>
  );
}
