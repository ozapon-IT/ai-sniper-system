"""週次集計（純粋・I/O 非依存）。

設計書: docs/ai-dev/london-orb-demo-design.md §1.2（評価原則）・§6.3（滑り）・§6.5（稼働率）・§9（中止条件）

🔴 **損益を出さない。** §1.2 の評価原則により、R 値・pips 損益は S3 の記録にのみ存在させ、
週次サマリには**一切載せない**。ファットテール依存で n が小さいうちの損益は無情報であり、
サマリに出すと「今週は勝った/負けた」で判断してしまうため。

中止条件（§9）は**警告のみで halt しない**（判断はユーザー）。halt するのは日次の
判定不一致だけ（§8.2 / ORB-6）。

#361 のスプレッド max / 0.5 超件数 / 合算コストは**併記のみ**。`breaches` に足さない。
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal

from backend.src.orb.broker_models import BrokerSession, SlippageRecord

SpreadSide = Literal["entry", "exit"]

# 中止条件の凍結値（§9）。変更は仕様書（結果ノート §9）の改訂を経由する。
SLIP_P90_LIMIT = 5.84
SLIP_P50_LIMIT = 3.0
OUTAGE_WEEKLY_LIMIT = 2
# 現行のスプレッド前提（ORB-1b #268 実測。Phase 0 の 0.2 ではない・§6.3）
SPREAD_BASELINE_PIPS = 0.5

# §6.5「障害による機会損失」= シグナル判定または記録が不能・不確定になった日。
# 休場（market_holiday）とデータが正常に無い日は障害ではない。
OUTAGE_SKIP_REASONS = frozenset({"system_outage"})

# 🔴 **滑り母集団から除外する日**（§6.3・#304）。稼働初日 2026-08-14 は poller の
# 起動どうしが重なり、後発が 1 周期古い `ARMED` から **2 度目の fill** を判定していた。
# 真 fill 159.185（滑り 0.6pips）と偽 fill 159.168（滑り 2.3pips）が並立し、
# どちらが記録に残るかが非決定的だったため、この日の `entry_slip` は信用できない。
# 分類（§8.2 の一致検証）は両系列とも同一なので verify 側の除外は不要。
# 日付除外は設計 §1.2 の約定 n からも除く（T001）。一方 `slip_n` は不利滑りの
# 観測点数（fill + SL）であり、日付除外に加え `poll_gap_ms is None` も除く
# （定義 B・#359）。`slip_n` と設計 n は別カウント。週次 Slack の `n=` は `slip_n`。
SLIP_EXCLUDED_DATES: frozenset[str] = frozenset({"2026-08-14"})


@dataclass
class WeeklySummary:
    week: str  # ISO 週（YYYY-Www）
    days: list[str] = field(default_factory=list)
    funnel: dict[str, int] = field(default_factory=dict)
    slip_n: int = 0
    slip_p50: float | None = None
    slip_p90: float | None = None
    spread_n: int = 0
    spread_p50: float | None = None
    spread_p90: float | None = None
    spread_max: float | None = None
    spread_max_date: str | None = None
    spread_max_side: SpreadSide | None = None
    spread_over_baseline_n: int = 0
    spread_excess_p50: float | None = None  # 実測 − 現行前提 0.5pips
    combined_n: int = 0
    combined_p50: float | None = None
    combined_p90: float | None = None
    combined_max: float | None = None
    outage_days: list[str] = field(default_factory=list)
    verify: dict[str, int] = field(default_factory=dict)
    halt: dict[str, Any] = field(default_factory=dict)
    breaches: list[str] = field(default_factory=list)

    def to_jsonable(self) -> dict[str, Any]:
        max_at: dict[str, str] | None = None
        if self.spread_max_date is not None and self.spread_max_side is not None:
            max_at = {"date": self.spread_max_date, "side": self.spread_max_side}
        return {
            "week": self.week,
            "days": self.days,
            "funnel": self.funnel,
            "slippage": {
                "n": self.slip_n,
                "p50": self.slip_p50,
                "p90": self.slip_p90,
                "limits": {"p50": SLIP_P50_LIMIT, "p90": SLIP_P90_LIMIT},
            },
            "spread": {
                "n": self.spread_n,
                "p50": self.spread_p50,
                "p90": self.spread_p90,
                "max": self.spread_max,
                "max_at": max_at,
                "over_baseline_n": self.spread_over_baseline_n,
                "excess_vs_baseline_p50": self.spread_excess_p50,
                "baseline": SPREAD_BASELINE_PIPS,
            },
            "combined_cost": {
                "n": self.combined_n,
                "p50": self.combined_p50,
                "p90": self.combined_p90,
                "max": self.combined_max,
                "baseline": SPREAD_BASELINE_PIPS,
            },
            "outage": {"days": self.outage_days, "limit": OUTAGE_WEEKLY_LIMIT},
            "verify": self.verify,
            "halt": self.halt,
            "breaches": self.breaches,
        }


def week_key(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def week_days(anchor: date) -> list[str]:
    """`anchor` を含む ISO 週の月〜金（London 暦日）。"""
    monday = anchor - timedelta(days=anchor.weekday())
    return [(monday + timedelta(days=i)).isoformat() for i in range(5)]


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _upper_pct(values: list[float], q: float) -> float | None:
    """上側パーセンタイル。**nearest-rank（切り上げ）で保守側に寄せる**。

    🔴 線形補間や切り捨てだと n が小さいとき裾を**過小評価**する。
    例: [1,1,1,1,6.5] の p90 は切り捨てなら 1.0 になり、中止条件（p90 > 5.84・§9）が
    鳴るべき場面で鳴らない。安全閾値なので「早めに鳴る」側へ倒す。
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = math.ceil(q * len(ordered))
    idx = min(len(ordered), max(1, rank)) - 1
    return ordered[idx]


def _countable_slip(slip: SlippageRecord | None) -> SlippageRecord | None:
    """§6.3 定義 B: 記録があり、かつ直前観測からの間隔が測れている。

    `poll_gap_ms is None` は起動初回（直前観測が無い）だけ。測定済みの ≈5 秒ギャップは残す。
    """
    if slip is None or slip.poll_gap_ms is None:
        return None
    return slip


def _countable_adverse_records(
    session: BrokerSession,
) -> list[tuple[SpreadSide, SlippageRecord, float]]:
    """§9 定義 B の観測点。(side, record, adverse_slip)。"""
    if session.session_date in SLIP_EXCLUDED_DATES:
        return []
    out: list[tuple[SpreadSide, SlippageRecord, float]] = []
    entry = _countable_slip(session.entry_slip)
    if entry is not None:
        out.append(("entry", entry, entry.slip_pips))
    if session.exit_reason == "sl":
        exit_slip = _countable_slip(session.exit_slip)
        if exit_slip is not None:
            out.append(("exit", exit_slip, -exit_slip.slip_pips))
    return out


def _spread_excess_pips(spread_pips: float) -> float:
    """前提からの上振れ。ちょうど baseline は 0（厳密 `>` と同じ境界）。"""
    return max(spread_pips - SPREAD_BASELINE_PIPS, 0.0)


def _combined_cost(adverse_slip: float, spread_pips: float) -> float:
    """同じ観測点の合算（定義 B / ペアリング A）。中止条件には使わない。"""
    return adverse_slip + _spread_excess_pips(spread_pips)


def adverse_slips(session: BrokerSession) -> list[float]:
    """中止条件（§9）の滑り母集団。**不利方向を正に正規化**して返す。

    🔴 **EOD / `both_filled` の `exit_slip` は入れない。** ブローカーは EOD の `exit_slip` を
    `trigger = entry_price` で作るため（`broker.py` の EOD 分岐）、中身は滑りではなく
    **結果 pips そのもの**である。これを混ぜると:
      - 中止条件が勝ち週ほど誤発火する（p50/p90 が損益に支配される）
      - §1.2「損益をサマリに出さない」も実質的に破れる（数値が損益になる）
    設計 §9 の母集団は「**全 fill + SL イベント**」であり EOD は対象外。

    🔴 **符号の正規化**: エントリーの不利すべりは正（トリガーより不利な価格で約定）だが、
    SL の不利すべりは同じ式だと**負**になる（long は SL より下で約定するため）。
    そのまま上側 p90 で監視すると **SL の不利すべりが永久に閾値へ掛からない**ので、
    SL 側は符号を反転して「不利 = 正」に揃える。

    🔴 `SLIP_EXCLUDED_DATES` の日は空を返す（測定そのものが信用できない日・§6.3）。
    日付除外は設計 n からも除く。`slip_n` は観測点数であり、ここに加えて
    `poll_gap_ms is None` の観測点も除く（定義 B・#359）。entry と SL exit は独立。
    """
    return [pips for _, _, pips in _countable_adverse_records(session)]


def _funnel_key(session: BrokerSession) -> str:
    if session.state == "SKIPPED":
        return session.skip_reason or "skipped_other"
    if session.state == "CANCELLED":
        return "no_fill"
    if session.state == "ANOMALY":
        return "both_filled"
    if session.state == "UNCERTAIN":
        return "uncertain"
    if session.direction is not None:
        return "fill"
    return "other"


def halt_days(since: str | None, now: datetime) -> int | None:
    """halt 継続日数。`since` が無ければ None（古いレコード）。"""
    if not since:
        return None
    try:
        started = datetime.fromisoformat(since)
    except ValueError:
        return None
    if started.tzinfo is None:
        return None
    return max(0, (now - started).days)


def build_summary(
    *,
    anchor: date,
    sessions: dict[str, BrokerSession | None],
    verify_reports: dict[str, dict[str, Any] | None],
    halt_detail: dict[str, Any],
    now: datetime,
    demo_first_day: str | None = None,
) -> WeeklySummary:
    """週次サマリを組み立てる（純粋）。

    Args:
        sessions: セッション日 → `BrokerSession`（無い日は None）。
        verify_reports: セッション日 → verify レポート（無い日は None）。
        halt_detail: `session_store.get_halt_detail()` の戻り。
        demo_first_day: デモが記録を残した**最初の日**（`YYYY-MM-DD`）。
            🔴 これより前の `no_record` は「**まだ動いていなかった日**」であって障害ではない。
            None（記録が一件も無い）なら `no_record` を障害に数えない。
            これを入れないと、稼働開始直後の週は過去日がすべて機会損失に計上され、
            **初週から「中止条件に抵触」が誤発火**する（2026-08-14 の初回サマリで実際に出た）。
            安全弁の警告を初日から無意味にしないための扱い。
            ※ 起点をノートから求めると、S3 の列挙が失敗した場合に `None` になり
            その週の `no_record` を過少計上する。より硬い起点は
            `orb-demo/sessions/*.jsonl`（session-open の時点で残る）。
    """
    summary = WeeklySummary(week=week_key(anchor), days=list(sessions))
    slips: list[float] = []
    combined: list[float] = []
    spreads: list[float] = []
    funnel: dict[str, int] = {}
    verdicts: dict[str, int] = {}

    for day, session in sessions.items():
        if session is None:
            # session-open がそもそも走っていない（記録が無い）
            funnel["no_record"] = funnel.get("no_record", 0) + 1
            # 稼働開始前の日は障害ではない（上記 demo_first_day の注記）
            if demo_first_day is not None and day >= demo_first_day:
                summary.outage_days.append(day)
            continue
        key = _funnel_key(session)
        funnel[key] = funnel.get(key, 0) + 1

        if key in OUTAGE_SKIP_REASONS or key == "uncertain":
            # §6.5: 判定不能・不確定は「障害による機会損失」
            summary.outage_days.append(day)

        # 中止条件の母集団は fill + SL のみ・不利方向を正に正規化（EOD は除外）
        # 合算は同じ走査の同じ観測点（#361・定義 B / ペアリング A）
        for _, rec, pips in _countable_adverse_records(session):
            slips.append(pips)
            combined.append(_combined_cost(pips, rec.spread_pips))
        # スプレッドは観測点の ask−bid なのでトリガー基準に依存しない。全記録から採る。
        spread_obs: list[tuple[SlippageRecord | None, SpreadSide]] = [
            (session.entry_slip, "entry"),
            (session.exit_slip, "exit"),
        ]
        for slip, side in spread_obs:
            if slip is None:
                continue
            spreads.append(slip.spread_pips)
            if slip.spread_pips > SPREAD_BASELINE_PIPS:
                summary.spread_over_baseline_n += 1
            # タイは日付昇順・同日は entry 先。最初の 1 件だけ残す（#361）
            if _is_new_spread_max(
                current_max=summary.spread_max,
                current_date=summary.spread_max_date,
                current_side=summary.spread_max_side,
                candidate=slip.spread_pips,
                candidate_date=day,
                candidate_side=side,
            ):
                summary.spread_max = slip.spread_pips
                summary.spread_max_date = day
                summary.spread_max_side = side

    for report in verify_reports.values():
        verdict = "missing" if report is None else str(report.get("verdict", "unknown"))
        verdicts[verdict] = verdicts.get(verdict, 0) + 1

    summary.funnel = funnel
    summary.verify = verdicts
    summary.slip_n = len(slips)
    summary.slip_p50 = _median(slips)
    summary.slip_p90 = _upper_pct(slips, 0.9)
    summary.combined_n = len(combined)
    summary.combined_p50 = _median(combined)
    summary.combined_p90 = _upper_pct(combined, 0.9)
    summary.combined_max = max(combined) if combined else None
    summary.spread_n = len(spreads)
    summary.spread_p50 = _median(spreads)
    summary.spread_p90 = _upper_pct(spreads, 0.9)
    if summary.spread_p50 is not None:
        summary.spread_excess_p50 = round(summary.spread_p50 - SPREAD_BASELINE_PIPS, 2)

    summary.halt = {
        "halt": bool(halt_detail.get("halt")),
        "reason": halt_detail.get("reason", ""),
        "since": halt_detail.get("since"),
        "days": halt_days(halt_detail.get("since"), now) if halt_detail.get("halt") else None,
    }
    summary.breaches = _breaches(summary)
    return summary


def _is_new_spread_max(
    *,
    current_max: float | None,
    current_date: str | None,
    current_side: SpreadSide | None,
    candidate: float,
    candidate_date: str,
    candidate_side: SpreadSide,
) -> bool:
    """スプレッド max の identity 更新判定。同値なら早い日付、同日は entry を残す。"""
    if current_max is None:
        return True
    if candidate > current_max:
        return True
    if candidate < current_max:
        return False
    if current_date is None or candidate_date < current_date:
        return True
    if candidate_date > current_date:
        return False
    return candidate_side == "entry" and current_side != "entry"


def _breaches(s: WeeklySummary) -> list[str]:
    """中止条件（§9）。**警告のみ・halt はしない**。"""
    out: list[str] = []
    if s.slip_p90 is not None and s.slip_p90 > SLIP_P90_LIMIT:
        out.append(f"slip_p90>{SLIP_P90_LIMIT}")
    if s.slip_p50 is not None and s.slip_p50 > SLIP_P50_LIMIT:
        out.append(f"slip_p50>{SLIP_P50_LIMIT}")
    if len(s.outage_days) > OUTAGE_WEEKLY_LIMIT:
        out.append(f"outage>{OUTAGE_WEEKLY_LIMIT}/week")
    return out


def _fmt(value: float | None, unit: str = "pips") -> str:
    return "—" if value is None else f"{value:.2f}{unit}"


def render_slack(summary: WeeklySummary) -> str:
    """Slack 1 通ぶんの本文。**損益は含めない**（§1.2）。"""
    lines: list[str] = [f"*[ORB demo] 週次サマリ {summary.week}*"]

    halt = summary.halt
    if halt.get("halt"):
        days = halt.get("days")
        days_txt = "不明" if days is None else f"{days} 日"
        lines.append(
            f"🔴 *halt 継続中（{days_txt}）*　理由: `{halt.get('reason') or '不明'}`\n"
            f"　→ 解除手順: docs/runbook/orb_halt.md"
        )
    else:
        lines.append("🟢 halt なし")

    funnel = " / ".join(f"{k}={v}" for k, v in sorted(summary.funnel.items())) or "記録なし"
    lines.append(f"*ファネル*: {funnel}")

    lines.append(
        f"*推定滑り（不利側・fill+SL・観測点 n={summary.slip_n}）*: "
        f"p50 {_fmt(summary.slip_p50)} / p90 {_fmt(summary.slip_p90)}"
        f"　[上限 p50 {SLIP_P50_LIMIT} / p90 {SLIP_P90_LIMIT}]"
    )
    lines.append("　（日付除外 + poll_gap 空除外。§1.2 の約定 n とは別）")
    lines.append(
        f"*スプレッド（観測点 n={summary.spread_n}）*: "
        f"p50 {_fmt(summary.spread_p50)} / p90 {_fmt(summary.spread_p90)} / max {_fmt(summary.spread_max)}"
    )
    identity = ""
    if summary.spread_max is not None and summary.spread_max_date and summary.spread_max_side:
        identity = f"identity {summary.spread_max_date} {summary.spread_max_side}　"
    lines.append(
        f"　{identity}前提 {SPREAD_BASELINE_PIPS}pips との差 {_fmt(summary.spread_excess_p50)}"
        f"　{SPREAD_BASELINE_PIPS}超 {summary.spread_over_baseline_n}/{summary.spread_n}"
    )
    lines.append(
        f"*合算コスト（滑り+スプレッド乖離・観測点 n={summary.combined_n}）*: "
        f"p50 {_fmt(summary.combined_p50)} / p90 {_fmt(summary.combined_p90)} / max {_fmt(summary.combined_max)}"
    )
    lines.append("　（中止条件には使わない・§6.3）")

    outage = ", ".join(summary.outage_days) if summary.outage_days else "なし"
    lines.append(f"*障害による機会損失*: {len(summary.outage_days)} 日（{outage}）")

    verify = " / ".join(f"{k}={v}" for k, v in sorted(summary.verify.items())) or "記録なし"
    lines.append(f"*一致検証*: {verify}")

    if summary.breaches:
        lines.append("⚠️ *中止条件に抵触*: " + ", ".join(summary.breaches))
        lines.append("　（警告のみ。継続可否はユーザー判断・§9）")

    lines.append("_損益は本サマリに出しません（§1.2: 損益でエッジを判定しない）。_")
    return "\n".join(lines)
