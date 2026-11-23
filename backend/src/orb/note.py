"""セッションノート生成（`BrokerSession` → Markdown）。純粋・I/O 非依存。

設計書: docs/ai-dev/london-orb-demo-design.md §7.1（frontmatter 写像）・§6.3（滑り）・§6.4（EOD）

frontmatter はバックテスト記録の形式と**互換**にする（Markdown ノートとしてそのまま集計できる）。
`mode: demo` / `setup: london_orb` 固定。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from backend.src.orb.broker_models import BrokerSession
from backend.src.orb.constants import JST, PIP

# 記録システム §4 のファネル段階。手法の手順を段階分解した名前。
Stage = str  # "gate" | "range" | "order" | "entry"

_WEEKDAY_JA = ("月", "火", "水", "木", "金", "土", "日")

# ノートを生成してよい状態（終端のみ）。進行中に生成すると stage=entry なのに
# result=skip という不整合なノートができる。
TERMINAL_STATES = frozenset({"CLOSED", "SKIPPED", "CANCELLED", "ANOMALY", "UNCERTAIN"})


@dataclass(frozen=True)
class NoteContext:
    """ノート生成の補助入力（ボットが持たない検算値）。

    `eod_close` は「London 16:00 に確定する 15 分足終値」＝バックテストの EOD 基準。
    仮想決済（16:00 以降最初の観測 BID）との差 `eod_gap_pips` は**観測点定義の差**であり
    滑りではない（§6.4）。算出は verify ジョブ（ORB-6）の責務なので、ここでは
    渡されたときだけ埋め、無ければ null のままにする。
    """

    eod_close: float | None = None
    note: str = ""


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.5f}".rstrip("0").rstrip(".")
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _jst_hhmm(ts: datetime | None) -> str:
    """記録システム §2 の `entry_time` / `exit_time` は JST（HH:MM）。"""
    return "" if ts is None else ts.astimezone(JST).strftime("%H:%M")


def _jst_display(ts: datetime | None, session_date: str) -> str:
    """本文表示用の JST 時刻。**セッション日を跨いだら「翌」を付ける**。

    EOD（London 16:00）は JST で翌 00:00 / 01:00 になる。frontmatter は
    記録システム §2 準拠で HH:MM のままにするが、本文で `00:00` とだけ書くと
    「セッション日の未明」と読み違えるため、本文側では日跨ぎを明示する。
    """
    if ts is None:
        return ""
    local = ts.astimezone(JST)
    prefix = "翌 " if local.date().isoformat() > session_date else ""
    return f"{prefix}{local.strftime('%H:%M')} JST"


def stage_of(session: BrokerSession) -> Stage:
    """記録システム §4 のファネル段階へ写像する（設計書 §7.1）。

    gate（休場・障害・halt）/ range（no_range）/ order（発注したが成立せず）/ entry（成立）
    """
    if session.state == "SKIPPED":
        if session.skip_reason == "no_range":
            return "range"
        return "gate"
    if session.state in {"CANCELLED", "ANOMALY", "UNCERTAIN"}:
        return "order"
    if session.direction is not None:
        return "entry"
    return "gate"


def note_skip_reason(session: BrokerSession) -> str:
    """ノート用の見送り理由コード（型 `SkipReason` より広い）。

    `both_filled` / `uncertain` は状態機械の終端であり `SkipReason` には無いが、
    ファネル集計上は区別したいのでノート側のコードとして新設する（記録システム §4-1）。
    """
    if session.state == "ANOMALY" and session.exit_reason == "both_filled":
        return "both_filled"
    if session.state == "UNCERTAIN":
        return "uncertain"
    if session.state == "CANCELLED":
        return "no_fill"
    return session.skip_reason or ""


def result_pips(session: BrokerSession) -> float | None:
    """実現 pips（方向込み）。成立していなければ None。"""
    if session.direction is None or session.entry_price is None or session.exit_price is None:
        return None
    sign = 1.0 if session.direction == "long" else -1.0
    return (session.exit_price - session.entry_price) * sign / PIP


def sl_pips(session: BrokerSession) -> float | None:
    """リスク幅 pips（= |entry − SL|）。"""
    if session.entry_price is None or session.sl_price is None:
        return None
    return abs(session.entry_price - session.sl_price) / PIP


def _result_label(pips: float | None) -> str:
    if pips is None:
        return "skip"
    if pips > 0:
        return "win"
    if pips < 0:
        return "lose"
    return "be"


def _exit_reason_label(session: BrokerSession) -> str:
    """記録システムの `exit_reason` 語彙へ寄せる（EOD は `time`）。"""
    if session.exit_reason == "eod":
        return "time"
    return session.exit_reason or ""


def build_frontmatter(session: BrokerSession, ctx: NoteContext | None = None) -> dict[str, Any]:
    """記録システム §2 互換の frontmatter を組み立てる。"""
    ctx = ctx or NoteContext()
    day = date.fromisoformat(session.session_date)
    pips = result_pips(session)
    risk = sl_pips(session)
    levels = session.levels
    e_slip = session.entry_slip
    x_slip = session.exit_slip

    eod_gap = None
    if ctx.eod_close is not None and session.exit_reason == "eod" and session.exit_price is not None:
        eod_gap = (session.exit_price - ctx.eod_close) / PIP

    return {
        "type": "trade",
        "mode": "demo",
        "date": session.session_date,
        "weekday": _WEEKDAY_JA[day.weekday()],
        "entry_time": _jst_hhmm(session.entry_ts),
        "exit_time": _jst_hhmm(session.exit_ts),
        "direction": session.direction or "",
        "setup": "london_orb",
        "entry_price": session.entry_price,
        "exit_price": session.exit_price,
        "sl_pips": None if risk is None else round(risk, 1),
        "result_pips": None if pips is None else round(pips, 1),
        # TP を置かないルールのため計画 R:R は存在しない（§2 凍結ルール）
        "rr_planned": None,
        "rr_actual": None if (pips is None or not risk) else round(pips / risk, 3),
        "result": _result_label(pips),
        "entry_tf": "ticker_5s",
        "exit_reason": _exit_reason_label(session),
        # 自動運用のため常に true。false になり得るのは実装バグ＝§8 の停止対象（設計書 §7.1）
        "rule_compliance": True,
        "skip_reason": note_skip_reason(session),
        "stage": stage_of(session),
        "raw": {
            "ref_high": None if levels is None else levels.high,
            "ref_low": None if levels is None else levels.low,
            "buy_stop": None if levels is None else levels.buy_stop,
            "sell_stop": None if levels is None else levels.sell_stop,
            "eod_close": ctx.eod_close,
            "slip_entry_pips": None if e_slip is None else round(e_slip.slip_pips, 2),
            "slip_exit_pips": None if x_slip is None else round(x_slip.slip_pips, 2),
            "spread_at_fill_pips": None if e_slip is None else round(e_slip.spread_pips, 2),
            "eod_gap_pips": None if eod_gap is None else round(eod_gap, 2),
            "poll_gap_ms": None if e_slip is None else e_slip.poll_gap_ms,
        },
        "tags": ["trade", "orb", "demo"],
    }


def _render_frontmatter(fm: dict[str, Any]) -> list[str]:
    lines = ["---"]
    for key, value in fm.items():
        if key == "raw":
            lines.append("raw:")
            for rk, rv in value.items():
                lines.append(f"  {rk}: {_fmt(rv)}")
        elif key == "tags":
            lines.append(f"tags: [{', '.join(value)}]")
        else:
            lines.append(f"{key}: {_fmt(value)}")
    lines.append("---")
    return lines


def _summary_line(session: BrokerSession, pips: float | None) -> str:
    """冒頭サマリー1行（記録システム §3-1: 後から読み返す索引）。"""
    stage = stage_of(session)
    if stage == "entry" and pips is not None:
        return (
            f"{session.direction} 成立・{_exit_reason_label(session)} 決済で {pips:+.1f}pips"
            f"（{session.entry_price} → {session.exit_price}）。"
        )
    reason = note_skip_reason(session)
    labels = {
        "no_range": "レンジ4本/60本が揃わず見送り",
        "no_fill": "11:00 まで未約定で両側取消",
        "market_holiday": "休場カレンダーにより見送り",
        "market_closed": "市場が開いておらず見送り（メンテ/閉場）",
        "system_outage": "データ取得障害により見送り",
        "halted": "halt フラグにより新規仮想発注を停止中",
        "disabled": "ORB デモのマスタースイッチがオフのため見送り",
        "both_filled": "両側到達（異常）— 即時仮想クローズ",
        "uncertain": "11:00 境界の欠測で判定保留（リプレイ確定待ち）",
    }
    return labels.get(reason, "見送り") + "。"


def render_note(session: BrokerSession, ctx: NoteContext | None = None) -> str:
    """セッションノート（Markdown）を生成する。"""
    ctx = ctx or NoteContext()
    fm = build_frontmatter(session, ctx)
    pips = result_pips(session)
    levels = session.levels
    lines: list[str] = []
    lines += _render_frontmatter(fm)
    lines.append("")
    lines.append(f"# {session.session_date} ロンドンORB（デモ）")
    lines.append("")
    lines.append(_summary_line(session, pips))
    lines.append("")

    lines.append("## レンジ（London 8:00〜9:00）")
    lines.append("")
    if levels is None:
        lines.append("レンジ未確定。")
    else:
        lines.append(f"- 高値 / 安値: `{levels.high}` / `{levels.low}`（幅 {levels.width_pips:.1f}pips）")
        lines.append(f"- 買いストップ / 売りストップ: `{levels.buy_stop}` / `{levels.sell_stop}`（±1.0pip）")
    lines.append("")

    lines.append("## 執行")
    lines.append("")
    lines.append(f"- 状態: `{session.state}` / 段階: `{stage_of(session)}`")
    if session.direction is not None:
        lines.append(
            f"- エントリー: {session.direction} `{session.entry_price}`"
            f"（{_jst_display(session.entry_ts, session.session_date)}）/ SL `{session.sl_price}`"
        )
        lines.append(
            f"- 決済: `{session.exit_price}`"
            f"（{_jst_display(session.exit_ts, session.session_date)}・"
            f"{_exit_reason_label(session)}）"
        )
        if pips is not None:
            lines.append(f"- 結果: **{pips:+.1f}pips**（{fm['rr_actual']}R）")
    else:
        lines.append(f"- 見送り理由: `{note_skip_reason(session)}`")
    lines.append("")

    lines.append("## 推定滑り（§6.3）")
    lines.append("")
    lines.append("> モックの滑りは**気配ベースの下界近似**であり、真の滑りは実注文でのみ実測可能。")
    lines.append("")
    if session.entry_slip is None and session.exit_slip is None:
        lines.append("記録なし（未約定）。")
    else:
        lines.append("| 区分 | 推定滑り(pips) | スプレッド(pips) | 直前観測からの間隔(ms) |")
        lines.append("|---|---|---|---|")
        for label, slip in (("エントリー", session.entry_slip), ("決済", session.exit_slip)):
            if slip is None:
                continue
            lines.append(
                f"| {label} | {slip.slip_pips:+.2f} | {slip.spread_pips:.2f} | "
                f"{'' if slip.poll_gap_ms is None else slip.poll_gap_ms} |"
            )
    lines.append("")

    if ctx.note:
        lines.append("## 備考")
        lines.append("")
        lines.append(ctx.note)
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "*本ノートは ORB デモ（モック・実発注なし）の自動生成記録です。"
        "損益でエッジを判定しません（設計書 §1.2）。*"
    )
    return "\n".join(lines) + "\n"


def vault_filename(session: BrokerSession, *, sequence: int | None = None) -> str:
    """ノート保存先のファイル名（デモ系列の命名規則）。

    歴史的な命名。運用終了後は使わない。
    成立は `T001_YYYY-MM-DD_long.md`（**連番は成立のみ消費**）、
    見送りは `SK_YYYY-MM-DD.md`。
    """
    if stage_of(session) == "entry" and session.direction is not None:
        if sequence is None:
            raise ValueError("成立ノートには sequence が必要")
        return f"T{sequence:03d}_{session.session_date}_{session.direction}.md"
    return f"SK_{session.session_date}.md"
