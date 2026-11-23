import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DayDetail } from "../../components/day-detail";
import type { DayResponse } from "../../lib/read-api";

const base: DayResponse = {
  date: "2099-01-07",
  plan: { order_type: "LIMIT", entry_price: 100.5, take_profit: 100.9, stop_loss: 100.1, rr_target: 1.2, lot: 100 },
  direction: "LONG",
  status: "stubbed",
  sniper_logs: null,
  trade_note: "## ノート",
  closeout: {
    status: "ok",
    mode: "weekday",
    executions_fetched: true,
    executions_count: 2,
    pnl: { realized_total: "-1200", loss_gain_total: "-1000", fee_total: "0", settled_swap_total: "-200", close_count: 1 },
    scope: "day",
    exit_reasons: { time_stop: 1 },
    note_complete: true,
  },
  equity: null,
  lot: 100,
};

describe("DayDetail", () => {
  it("判断・手仕舞い・ノートの 3 セクションを出す", () => {
    render(<DayDetail day={base} />);
    expect(screen.getByRole("heading", { level: 2, name: "判断" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "手仕舞い" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "トレードノート" })).toBeInTheDocument();
  });

  it("口座残高（有効純資産）は円・桁区切りで表示する（#411）", () => {
    render(<DayDetail day={{ ...base, equity: 123456 }} />);
    expect(screen.getByText("口座残高（判断時点・有効純資産）").nextElementSibling).toHaveTextContent("123,456 円");
    expect(screen.queryByTestId("day-equity-unrecorded")).not.toBeInTheDocument();
  });

  it("口座残高が無い日（取得前に止まった日・過去ログ）は「未記録」（0 円にしない）", () => {
    render(<DayDetail day={{ ...base, equity: null }} />);
    expect(screen.getByText("口座残高（判断時点・有効純資産）").nextElementSibling).toHaveTextContent("未記録");
    expect(screen.queryByTestId("day-equity")).not.toBeInTheDocument();
  });

  it("R:R は plan.rr_target をそのまま表示する（画面で計算し直さない）", () => {
    render(<DayDetail day={base} />);
    // エントリー 100.5 / TP 100.9 / SL 100.1 から計算すると 1.0 だが、API の値 1.2 をそのまま出す
    expect(screen.getByText("R:R（目標）").nextElementSibling).toHaveTextContent("1.2");
  });

  it("日次の損益は「日次」、桁区切りで円表示", () => {
    render(<DayDetail day={base} />);
    expect(screen.getByTestId("closeout-scope")).toHaveTextContent("日次");
    expect(screen.getByTestId("closeout-pnl")).toHaveTextContent("-1,200 円");
    expect(screen.getByText("時間切れ手仕舞い 1 件")).toBeInTheDocument();
  });

  it("金曜（scope=week）は週次（5 取引日）と明示し、金曜 1 日分に見せない", () => {
    const friday: DayResponse = {
      ...base,
      closeout: { ...base.closeout!, mode: "friday", scope: "week", executions_count: 8 },
    };
    render(<DayDetail day={friday} />);
    expect(screen.getByTestId("closeout-scope")).toHaveTextContent("週次（直近 5 取引日の合計）");
  });

  it("見送り日: プランなしの案内と、約定 0 件・損益 0 の手仕舞い", () => {
    const noEntry: DayResponse = {
      ...base,
      direction: "NO_ENTRY",
      status: "no_entry",
      plan: null,
      closeout: {
        ...base.closeout!,
        executions_count: 0,
        pnl: { realized_total: "0", loss_gain_total: "0", fee_total: "0", settled_swap_total: "0", close_count: 0 },
        exit_reasons: {},
      },
    };
    render(<DayDetail day={noEntry} rationaleSummary="値動きが乏しく見送り" />);
    expect(screen.getByText("方向").nextElementSibling).toHaveTextContent("見送り");
    expect(screen.getByText(/発注プランはありません/)).toBeInTheDocument();
    expect(screen.getByText("値動きが乏しく見送り")).toBeInTheDocument();
    expect(screen.getByText("決済なし")).toBeInTheDocument();
    expect(screen.getByTestId("closeout-pnl")).toHaveTextContent("0 円");
  });

  it("発注前の確認で中止した skip 日: 判断なしの案内（休場とは書かない）", () => {
    const skipped: DayResponse = {
      ...base,
      direction: null,
      status: "skipped",
      plan: null,
      trade_note: null,
      lot: null,
    };
    render(<DayDetail day={skipped} />);
    expect(screen.getByText(/発注前の確認で中止したため/)).toBeInTheDocument();
    expect(screen.getByText("状態").nextElementSibling).toHaveTextContent("スキップ（発注前の確認で中止）");
    expect(document.body).not.toHaveTextContent("休場");
    // 取引日なので手仕舞いログはある（この入力では base の closeout をそのまま表示）
    expect(screen.getByTestId("closeout-pnl")).toHaveTextContent("-1,200 円");
  });

  it("手仕舞いログが無い日（直近の取引日で手仕舞い前）: 翌 4:30 に実行される旨", () => {
    render(<DayDetail day={{ ...base, closeout: null, trade_note: null }} />);
    expect(screen.getByText(/手仕舞いは翌 4:30（JST）に実行されます/)).toBeInTheDocument();
    expect(screen.getByText("ノートはありません。")).toBeInTheDocument();
  });

  it.each(["step3_failed", "step3_residual", "step3_unverified"])(
    "建玉が残る可能性のある status（%s）は強調表示する",
    (status) => {
      render(<DayDetail day={{ ...base, closeout: { ...base.closeout!, status } }} />);
      expect(screen.getByRole("alert")).toHaveTextContent("損切りなしで残っている可能性");
    },
  );

  it("正常な手仕舞いでは alert を出さない", () => {
    render(<DayDetail day={base} />);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("価格なしの plan（発注の途中で停止）: 価格欄を作らず注意を出す", () => {
    const partial: DayResponse = { ...base, plan: { sl_profile: "wide", rr_target: 1.2 } };
    render(<DayDetail day={partial} />);
    expect(screen.getByRole("note")).toHaveTextContent("価格");
    expect(screen.queryByText("エントリー")).toBeNull();
    expect(screen.queryByText("損切り（SL）")).toBeNull();
  });

  it("手仕舞いの途中停止（閉場）: 損益・決済理由の欄を出さない", () => {
    const closed: DayResponse = {
      ...base,
      closeout: {
        status: "market_closed",
        mode: "weekday",
        executions_fetched: null,
        executions_count: null,
        pnl: null,
        scope: "day",
        exit_reasons: null,
        note_complete: null,
      },
    };
    render(<DayDetail day={closed} />);
    const section = screen.getByRole("heading", { name: "手仕舞い" }).closest("section")!;
    expect(within(section).getByText(/閉場のため中断/)).toBeInTheDocument();
    expect(within(section).queryByText("実現損益")).toBeNull();
    expect(within(section).queryByText("決済理由")).toBeNull();
  });

  it("ノートなしの日", () => {
    render(<DayDetail day={{ ...base, trade_note: null }} />);
    expect(screen.getByText("ノートはありません。")).toBeInTheDocument();
  });
});
