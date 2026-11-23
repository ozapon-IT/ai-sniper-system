import { Suspense } from "react";
import { LatestTradingDay } from "../components/latest-trading-day";
import Loading from "./loading";

/** 当日画面（直近の取引日）。本体は components/latest-trading-day.tsx。 */
export default function Home() {
  return (
    <Suspense fallback={<Loading />}>
      <LatestTradingDay />
    </Suspense>
  );
}
