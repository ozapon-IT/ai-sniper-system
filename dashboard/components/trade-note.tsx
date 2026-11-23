import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

// トレードノートは LLM が書く外部由来テキスト。生 HTML は描画しない。
// 出典（参照日 2026-11-22・いずれも導入した版に同梱の readme）:
//  - react-markdown@10.1.0: Security / `skipHtml` / `allowedElements` / `unwrapDisallowed` /
//    `urlTransform`。既定で HTML は React 要素に変換されず（dangerouslySetInnerHTML 不使用）、
//    既定の urlTransform が危険な URL を除く。urlTransform は上書きしない。
//  - remark-gfm@4.0.1: GFM（autolink literals・footnotes・strikethrough・tables・tasklists）を
//    追加する remark プラグイン。react-markdown 単体は表を解釈しない。ノートには Python が付ける
//    確定表（Markdown テーブル。backend/src/llm/trade_note.py）が入り、数値はそこにある。
// 許可する要素を明示し、画像・リンクなど外部へ出る要素は許可しない（中身のテキストは残す）。
// GFM が足す del / input / footnote 用の要素も許可リストに無いので文字だけが残る。
const ALLOWED_ELEMENTS = [
  "h1",
  "h2",
  "h3",
  "h4",
  "p",
  "ul",
  "ol",
  "li",
  "strong",
  "em",
  "code",
  "pre",
  "blockquote",
  "hr",
  "br",
  "table",
  "thead",
  "tbody",
  "tr",
  "th",
  "td",
];

/** react-markdown が渡す hast の `node` を DOM 属性に流さない。 */
function omitNode<T extends { node?: unknown }>(props: T): Omit<T, "node"> {
  const { node, ...rest } = props;
  void node;
  return rest;
}

export function TradeNote({ markdown }: { markdown: string }) {
  return (
    <div className="note space-y-3 break-words">
      <Markdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        allowedElements={ALLOWED_ELEMENTS}
        unwrapDisallowed
        components={{
          // 横長の表はこの入れ物の中だけでスクロールさせ、ページ全体に横スクロールを出さない。
          // キーボードで操作できるよう tabIndex=0（axe: scrollable-region-focusable）。
          table: (props) => (
            <div
              className="overflow-x-auto"
              tabIndex={0}
              role="region"
              aria-label="ノート内の表"
            >
              <table className="w-max min-w-full border-collapse text-sm" {...omitNode(props)} />
            </div>
          ),
          th: (props) => (
            <th
              className="border border-neutral-300 dark:border-neutral-700 px-2 py-1 text-left whitespace-nowrap"
              {...omitNode(props)}
            />
          ),
          td: (props) => (
            <td
              className="border border-neutral-300 dark:border-neutral-700 px-2 py-1 whitespace-nowrap"
              {...omitNode(props)}
            />
          ),
        }}
      >
        {markdown}
      </Markdown>
    </div>
  );
}
