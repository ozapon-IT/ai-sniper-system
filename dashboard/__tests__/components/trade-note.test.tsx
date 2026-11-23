import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TradeNote } from "../../components/trade-note";

describe("TradeNote の安全な描画", () => {
  it("Markdown の見出し・箇条書きを描画する", () => {
    render(<TradeNote markdown={"## 振り返り\n\n- 一つ目\n- **二つ目**"} />);
    expect(screen.getByRole("heading", { level: 2, name: "振り返り" })).toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
  });

  it("生 HTML（script / img / iframe / onerror）を要素として描画しない", () => {
    const { container } = render(
      <TradeNote
        markdown={
          '<script>window.__xss = 1</script>\n\n<img src=x onerror="window.__xss = 2">\n\n<iframe src="https://example.com"></iframe>\n\n本文'
        }
      />,
    );
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("iframe")).toBeNull();
    expect(container.querySelector("[onerror]")).toBeNull();
    expect((window as unknown as { __xss?: number }).__xss).toBeUndefined();
    expect(screen.getByText("本文")).toBeInTheDocument();
  });

  it("リンクと画像は要素にしない（javascript: URL を含む）が、文字は残る", () => {
    const { container } = render(
      <TradeNote markdown={"[クリック](javascript:alert(1)) と ![画像](https://example.com/a.png)"} />,
    );
    expect(container.querySelector("a")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain("クリック");
  });

  it("確定表（GFM テーブル）を表として描画し、スクロールできる入れ物で包む", () => {
    const md = [
      "## 計画値",
      "",
      "| 取引日 | 方向 | エントリー | SL | TP | SL幅 | TP幅 |",
      "|---|---|---|---|---|---|---|",
      "| 2099-01-07 | LONG LIMIT | 100.5 | 100.1 | 100.9 | 40 pips | 40 pips |",
    ].join("\n");
    render(<TradeNote markdown={md} />);
    const table = screen.getByRole("table");
    expect(within(table).getAllByRole("columnheader")).toHaveLength(7);
    expect(within(table).getByRole("cell", { name: "LONG LIMIT" })).toBeInTheDocument();
    // 横長の表はページ全体ではなく入れ物の中でスクロールする（キーボード操作可）
    const region = screen.getByRole("region", { name: "ノート内の表" });
    expect(region).toHaveAttribute("tabindex", "0");
    expect(region.className).toContain("overflow-x-auto");
  });

  it("表のセルに混ぜた生 HTML も要素にしない", () => {
    const { container } = render(
      <TradeNote markdown={"| a |\n|---|\n| <img src=x onerror=alert(1)> |"} />,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("[onerror]")).toBeNull();
  });
});
