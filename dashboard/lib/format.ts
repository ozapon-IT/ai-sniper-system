import type { CloseoutSummary, DayResponse } from "./read-api";

/** 売買方向の表示名。null は判断ログの方向が無い日（休場の skip）。 */
export function directionLabel(direction: DayResponse["direction"]): string {
  switch (direction) {
    case "LONG":
      return "買い（LONG）";
    case "SHORT":
      return "売り（SHORT）";
    case "NO_ENTRY":
      return "見送り";
    default:
      return "判断なし";
  }
}

/** 判断ログの status（decision_batch が書く語彙）の表示名。未知の値はそのまま出す。 */
export function decisionStatusLabel(status: string | null): string {
  const labels: Record<string, string> = {
    stubbed: "発注（スタブ）",
    ok: "発注済み",
    no_entry: "見送り",
    skipped: "スキップ（発注前の確認で中止）",
    failed: "発注失敗",
    persistence_failed: "発注後の記録に失敗",
  };
  if (status === null) return "不明";
  return labels[status] ?? status;
}

/** 手仕舞いログの status の表示名。未知の値はそのまま出す。 */
export function closeoutStatusLabel(status: string | null): string {
  const labels: Record<string, string> = {
    ok: "正常終了",
    step3_failed: "成行決済に失敗（建玉が残っている可能性）",
    step3_unverified: "決済の結果を確認できず",
    step3_residual: "建玉が残存",
    market_closed: "閉場のため中断（次回再試行）",
    step1_failed: "新規注文のキャンセルに失敗",
    step2_failed: "決済 OCO のキャンセルに失敗",
  };
  if (status === null) return "不明";
  return labels[status] ?? status;
}

export const EXIT_REASON_LABELS: Record<string, string> = {
  time_stop: "時間切れ手仕舞い",
  take_profit: "利確（TP）",
  stop_loss: "損切り（SL）",
  unknown: "判定不能",
};

/** Decimal 文字列を桁区切りで整形する。🔴 浮動小数に変換しない（丸め・誤差を避ける）。
 * 指数表記など想定外の形はそのまま返す。 */
export function formatDecimal(value: string): string {
  const m = /^(-?)(\d+)(\.\d+)?$/.exec(value);
  if (!m) return value;
  const [, sign, int, frac = ""] = m;
  return `${sign}${int.replace(/\B(?=(\d{3})+(?!\d))/g, ",")}${frac}`;
}

/** 口座残高（有効純資産・円）。判断ログの float をカンマ区切りで表示する（#411）。 */
export function formatEquity(value: number): string {
  return value.toLocaleString("ja-JP", { maximumFractionDigits: 0 });
}

/** 損益の集計範囲の見出し。week は金曜 1 日分ではなく直近 5 取引日の合計（#399）。 */
export function scopeLabel(scope: CloseoutSummary["scope"]): string {
  if (scope === "week") return "週次（直近 5 取引日の合計）";
  if (scope === "day") return "日次";
  return "集計範囲不明";
}

/** unknown の値が有限の数値なら返す（plan は自由形式 JSON のため型を確かめる）。 */
export function asNumber(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

export function asString(v: unknown): string | null {
  return typeof v === "string" ? v : null;
}
