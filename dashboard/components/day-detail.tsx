import type { DayResponse } from "../lib/read-api";
import {
  EXIT_REASON_LABELS,
  asNumber,
  asString,
  closeoutStatusLabel,
  decisionStatusLabel,
  directionLabel,
  formatDecimal,
  formatEquity,
  scopeLabel,
} from "../lib/format";
import { TradeNote } from "./trade-note";

const card = "rounded-lg border border-neutral-300 dark:border-neutral-700 p-4";
const muted = "text-neutral-600 dark:text-neutral-300";

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5 py-1 sm:flex-row sm:gap-4">
      <dt className={`sm:w-40 shrink-0 ${muted}`}>{label}</dt>
      <dd className="break-words">{children}</dd>
    </div>
  );
}

/** 狙撃プラン。plan は判断ログのプラン系キーで、発注の途中で止まった日は価格が無い（#398）。
 * R:R は plan.rr_target をそのまま表示する（画面で計算し直さない: SL/TP の決定権は Python）。 */
function PlanCard({ plan, lot }: { plan: DayResponse["plan"]; lot: unknown }) {
  if (plan === null) {
    return <p className={muted}>発注プランはありません（見送り、または発注に至らなかった日）。</p>;
  }
  const entry = asNumber(plan.entry_price);
  const tp = asNumber(plan.take_profit);
  const sl = asNumber(plan.stop_loss);
  const hasPrices = entry !== null && tp !== null && sl !== null;
  const rr = asNumber(plan.rr_target);
  const lotValue = asNumber(plan.lot) ?? asNumber(lot);
  return (
    <div>
      {!hasPrices && (
        <p role="note" className="mb-2 rounded border border-amber-600 p-2">
          価格（エントリー・利確・損切り）が記録されていません。発注の途中で停止した日です。
        </p>
      )}
      <dl>
        {asString(plan.order_type) && <Row label="注文種別">{asString(plan.order_type)}</Row>}
        {entry !== null && <Row label="エントリー">{entry}</Row>}
        {tp !== null && <Row label="利確（TP）">{tp}</Row>}
        {sl !== null && <Row label="損切り（SL）">{sl}</Row>}
        {rr !== null && <Row label="R:R（目標）">{rr}</Row>}
        {asString(plan.sl_profile) && <Row label="SL プロファイル">{asString(plan.sl_profile)}</Row>}
        {lotValue !== null && <Row label="ロット">{lotValue} 通貨</Row>}
      </dl>
    </div>
  );
}

/** 建玉が損切りなしで残っている可能性がある手仕舞いの status（.claude/rules/trading-safety.md・#207）。 */
const AT_RISK_STATUSES = new Set(["step3_failed", "step3_residual", "step3_unverified"]);

/** 手仕舞いの要約。🔴 scope が "week"（金曜）のとき、約定件数・損益・決済理由の 3 項目は
 * すべて直近 5 取引日の集計で、金曜 1 日分ではない（#399）。 */
function CloseoutCard({ closeout }: { closeout: NonNullable<DayResponse["closeout"]> }) {
  const reasons = closeout.exit_reasons ? Object.entries(closeout.exit_reasons) : null;
  const atRisk = closeout.status !== null && AT_RISK_STATUSES.has(closeout.status);
  return (
    <div>
      {atRisk && (
        <p
          role="alert"
          className="mb-2 rounded border-2 border-red-700 p-2 font-semibold text-red-800 dark:border-red-400 dark:text-red-300"
        >
          建玉が損切りなしで残っている可能性があります。取引口座で建玉を確認してください。
        </p>
      )}
      <dl>
        <Row label="結果">{closeoutStatusLabel(closeout.status)}</Row>
        {closeout.pnl !== null || closeout.executions_count !== null || reasons !== null ? (
          <Row label="集計範囲">
            <strong data-testid="closeout-scope">{scopeLabel(closeout.scope)}</strong>
          </Row>
        ) : null}
        {closeout.executions_count !== null && (
          <Row label="約定件数">{closeout.executions_count} 件</Row>
        )}
        {closeout.pnl?.realized_total != null && (
          <Row label="実現損益">
            <span data-testid="closeout-pnl">{formatDecimal(closeout.pnl.realized_total)} 円</span>
            <span className={`block text-sm ${muted}`}>
              {closeout.pnl.loss_gain_total != null &&
                `売買損益 ${formatDecimal(closeout.pnl.loss_gain_total)}`}
              {closeout.pnl.fee_total != null && ` / 手数料 ${formatDecimal(closeout.pnl.fee_total)}`}
              {closeout.pnl.settled_swap_total != null &&
                ` / スワップ ${formatDecimal(closeout.pnl.settled_swap_total)}`}
            </span>
          </Row>
        )}
        {reasons !== null && (
          <Row label="決済理由">
            {reasons.length === 0
              ? "決済なし"
              : reasons.map(([k, n]) => `${EXIT_REASON_LABELS[k] ?? k} ${n} 件`).join(" / ")}
          </Row>
        )}
        {closeout.note_complete === false && <Row label="ノート">未完成（生成が途中で止まった）</Row>}
      </dl>
    </div>
  );
}

/** 1 日分の詳細（判断・手仕舞い・ノート）。当日画面と日次詳細画面で共通。
 * 表示項目は Read API の契約にあるものに限る（作り足さない）。 */
export function DayDetail({
  day,
  rationaleSummary,
}: {
  day: DayResponse;
  rationaleSummary?: string;
}) {
  const noDecision = day.direction === null;
  return (
    <div className="space-y-4">
      <section aria-labelledby="decision-h" className={card}>
        <h2 id="decision-h" className="mb-2 text-lg font-semibold">
          判断
        </h2>
        <dl>
          <Row label="方向">{directionLabel(day.direction)}</Row>
          <Row label="状態">{decisionStatusLabel(day.status)}</Row>
          {/* 判断バッチが口座を取得できた日だけ値がある（#411）。取得前に止まった日・過去ログは「未記録」。
              値は 20:30 の判断時点（その日の取引前）の残高で、下の手仕舞いの損益は反映されていない。 */}
          <Row label="口座残高（判断時点・有効純資産）">
            {day.equity !== null ? (
              <span data-testid="day-equity">{formatEquity(day.equity)} 円</span>
            ) : (
              <span data-testid="day-equity-unrecorded" className={muted}>
                未記録
              </span>
            )}
          </Row>
        </dl>
        {rationaleSummary ? <p className={`mt-2 ${muted}`}>{rationaleSummary}</p> : null}
        {noDecision ? (
          <p className={`mt-2 ${muted}`}>
            この日は発注前の確認で中止したため、判断（方向・プラン）はありません。
          </p>
        ) : (
          <div className="mt-3">
            <h3 className="mb-1 font-semibold">狙撃プラン</h3>
            <PlanCard plan={day.plan} lot={day.lot} />
          </div>
        )}
      </section>

      <section aria-labelledby="closeout-h" className={card}>
        <h2 id="closeout-h" className="mb-2 text-lg font-semibold">
          手仕舞い
        </h2>
        {day.closeout ? (
          <CloseoutCard closeout={day.closeout} />
        ) : (
          <p className={muted}>
            手仕舞いは翌 4:30（JST）に実行されます（まだ記録がありません）。実行後も表示されない場合は、
            手仕舞いログが保存されていない可能性があります。
          </p>
        )}
      </section>

      <section aria-labelledby="note-h" className={card}>
        <h2 id="note-h" className="mb-2 text-lg font-semibold">
          トレードノート
        </h2>
        {day.trade_note ? (
          <TradeNote markdown={day.trade_note} />
        ) : (
          <p className={muted}>ノートはありません。</p>
        )}
      </section>
    </div>
  );
}
