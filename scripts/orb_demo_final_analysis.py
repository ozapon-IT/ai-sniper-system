"""ロンドンORB デモの観測完了分析を S3 証跡から再集計する（設計書 §1.2・§6.3・§6.5・§8.2・§9）。

`s3://your-log-bucket/orb-demo/` をローカルへ同期したディレクトリを入力に、
`docs/ai-dev/london-orb-demo-result-2026-09-12.md` の §2〜§4 を再生成する。

🔴 **読取のみ。** S3 / DynamoDB には触れない。ネットワークを使うのは
`--fetch-klines` を明示したときの GMO Public API（klines GET・1 秒間隔）だけ。

集計の定義（レポート §7「再現手順」と同一）:
  - 滑り母集団 = 全 entry の `slip_pips` + SL exit の `−slip_pips`（不利 = 正）。
    EOD / `both_filled` の exit_slip は入れない（`weekly.adverse_slips` と同じ）
  - 定義 A = `weekly.SLIP_EXCLUDED_DATES` の日を除外（改訂前・併記）
  - 定義 B = A に加え `poll_gap_ms` が空の観測点を除外（設計 §6.3 / コードの正・#359）
  - p50 = median / p90 = nearest-rank（`weekly._upper_pct` と同じ切り上げ）

実行例:
    aws s3 sync s3://your-log-bucket/orb-demo/ /tmp/orb-demo/
    PYTHONPATH=. python scripts/orb_demo_final_analysis.py --s3-dir /tmp/orb-demo
    # 独立リプレイ + セッション内パス（klines を Public API から取得・read-only）
    PYTHONPATH=. python scripts/orb_demo_final_analysis.py --s3-dir /tmp/orb-demo \
        --klines-dir /tmp/orb-klines --fetch-klines
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from pathlib import Path

from backend.src.orb.weekly import SLIP_EXCLUDED_DATES, SPREAD_BASELINE_PIPS

PIP = 0.01
# BST（UTC+1）の期間は JST = London + 8h。GMT 期（10 月最終日曜〜）は +9h になる。
_JST_OFFSET_BST = timedelta(hours=8)
_FRONTMATTER_KEY_RE = re.compile(r"^\s*([a-z_]+):\s*(.*)$")


@dataclass
class SessionRow:
    """1 セッション（= 1 営業日）の突合済みレコード。"""

    date: str
    weekday: str
    n_fill_events: int
    other_events: list[str]
    armed_ts: str
    fill_ts: str
    exit_ts: str
    direction: str
    trigger: float
    fill_price: float
    sl_price: float
    entry_slip: float
    entry_spread: float
    entry_gap_ms: int | None
    entry_price_gap: float | None
    exit_reason: str
    exit_price: float
    exit_slip: float | None
    exit_spread: float
    exit_gap_ms: int | None
    exit_price_gap: float | None
    range_high: float
    range_low: float
    buy_stop: float
    sell_stop: float
    verdict: str
    reason: str
    halted: bool
    replay_signal: str
    replay_entry: float | None
    replay_exit: float | None
    replay_outcome: str | None
    replay_fill_et: str | None
    entry_diff: float | None
    exit_diff: float | None
    eod_gap: float | None
    in_bar: bool | None
    eod_close: float | None
    range_pips: float
    risk_pips: float
    result_pips: float
    r_multiple: float
    hold_min: float
    fill_vs_replay_bar_s: float | None


# --------------------------------------------------------------------------- 読み込み


def _load_notes(notes_dir: Path) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for path in sorted(notes_dir.glob("*.md")):
        parts = path.read_text(encoding="utf-8").split("---")
        if len(parts) < 3:
            continue
        kv: dict[str, str] = {}
        for line in parts[1].splitlines():
            m = _FRONTMATTER_KEY_RE.match(line)
            if m:
                kv[m.group(1)] = m.group(2).strip()
        out[path.stem] = kv
    return out


def load_rows(s3_dir: Path) -> tuple[list[SessionRow], list[str]]:
    """sessions / verify / notes を突合して 1 日 1 行にする。戻り値の第 2 要素は不整合。"""
    sessions = {
        p.stem: [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line]
        for p in sorted((s3_dir / "sessions").glob("*.jsonl"))
    }
    verify = {
        p.stem: json.loads(p.read_text(encoding="utf-8"))
        for p in sorted((s3_dir / "verify").glob("*.json"))
    }
    notes = _load_notes(s3_dir / "notes")
    issues: list[str] = []
    for label, keys in (("verify", set(verify)), ("notes", set(notes))):
        if keys != set(sessions):
            issues.append(f"{label} の日付集合が sessions と不一致: {sorted(keys ^ set(sessions))}")

    rows: list[SessionRow] = []
    for day, events in sorted(sessions.items()):
        armed = [e for e in events if e["reason"] == "armed"]
        fills = [e for e in events if e["reason"] == "filled"]
        exits = [e for e in events if e["reason"] in ("sl", "eod", "both_filled")]
        others = [
            e["reason"] for e in events if e["reason"] not in ("armed", "filled", "sl", "eod")
        ]
        if len(armed) != 1 or not fills or not exits:
            issues.append(
                f"{day}: 遷移が想定形でない（armed={len(armed)} fill={len(fills)} exit={len(exits)}）"
            )
            continue
        fill, last_exit = fills[0], exits[-1]  # 08-14 の 2 件目 fill は #304 で拒否された系列
        fd, xd, ad = fill["detail"], last_exit["detail"], armed[0]["detail"]
        v = verify.get(day, {})
        rp, od = v.get("replay", {}), v.get("observation_diffs", {})
        fill_ts = datetime.fromisoformat(fill["ts"])
        exit_ts = datetime.fromisoformat(last_exit["ts"])
        armed_ts = datetime.fromisoformat(armed[0]["ts"])
        sign = 1 if fd["direction"] == "long" else -1
        risk_pips = abs(fd["fill_price"] - fd["sl_price"]) / PIP
        result_pips = sign * (xd["exit_price"] - fd["fill_price"]) / PIP
        bar_delay: float | None = None
        if rp.get("fill_et"):
            bar_delay = (fill_ts - datetime.fromisoformat(rp["fill_et"])).total_seconds()
        row = SessionRow(
            date=day,
            weekday=datetime.fromisoformat(day).strftime("%a"),
            n_fill_events=len(fills),
            other_events=others,
            armed_ts=armed_ts.isoformat(),
            fill_ts=fill_ts.isoformat(),
            exit_ts=exit_ts.isoformat(),
            direction=fd["direction"],
            trigger=fd["trigger_price"],
            fill_price=fd["fill_price"],
            sl_price=fd["sl_price"],
            entry_slip=fd["slip_pips"],
            entry_spread=fd["spread_pips"],
            entry_gap_ms=fd.get("poll_gap_ms"),
            entry_price_gap=fd.get("price_gap"),
            exit_reason=last_exit["reason"],
            exit_price=xd["exit_price"],
            exit_slip=xd.get("slip_pips"),
            exit_spread=xd["spread_pips"],
            exit_gap_ms=xd.get("poll_gap_ms"),
            exit_price_gap=xd.get("price_gap"),
            range_high=ad["range_high"],
            range_low=ad["range_low"],
            buy_stop=ad["buy_stop"],
            sell_stop=ad["sell_stop"],
            verdict=v.get("verdict", "missing"),
            reason=v.get("reason", ""),
            halted=bool(v.get("halted")),
            replay_signal=rp.get("signal", "missing"),
            replay_entry=rp.get("entry"),
            replay_exit=rp.get("exit"),
            replay_outcome=rp.get("outcome"),
            replay_fill_et=rp.get("fill_et"),
            entry_diff=od.get("entry_diff_pips"),
            exit_diff=od.get("exit_diff_pips"),
            eod_gap=od.get("eod_gap_pips"),
            in_bar=od.get("fill_time_in_replay_bar"),
            eod_close=v.get("eod_close"),
            range_pips=round((ad["range_high"] - ad["range_low"]) / PIP, 1),
            risk_pips=round(risk_pips, 1),
            result_pips=round(result_pips, 1),
            r_multiple=round(result_pips / risk_pips, 3),
            hold_min=(exit_ts - fill_ts).total_seconds() / 60,
            fill_vs_replay_bar_s=bar_delay,
        )
        rows.append(row)
        issues.extend(_check_row(row, notes.get(day, {})))
    return rows, issues


def _close(a: float | None, b: float | None, tol: float) -> bool:
    return a is not None and b is not None and abs(a - b) <= tol


def _check_row(r: SessionRow, note: dict[str, str]) -> list[str]:
    """sessions ↔ verify ↔ notes のフィールド単位の突合（レポート A.1）。"""
    out: list[str] = []
    sign = 1 if r.direction == "long" else -1
    if r.n_fill_events != 1:
        out.append(f"{r.date}: filled イベントが {r.n_fill_events} 件")
    if r.other_events:
        out.append(f"{r.date}: 想定外イベント {r.other_events}")
    if r.verdict != "match" or r.halted:
        out.append(f"{r.date}: verdict={r.verdict} halted={r.halted}")
    if r.replay_signal != r.direction or r.replay_outcome != r.exit_reason:
        out.append(
            f"{r.date}: リプレイ {r.replay_signal}/{r.replay_outcome} ≠ デモ {r.direction}/{r.exit_reason}"
        )
    if not _close(r.trigger, r.replay_entry, 5e-4):
        out.append(f"{r.date}: trigger {r.trigger} ≠ replay.entry {r.replay_entry}")
    if r.exit_reason == "sl" and not _close(r.sl_price, r.replay_exit, 5e-4):
        out.append(f"{r.date}: sl_price {r.sl_price} ≠ replay.exit {r.replay_exit}")
    if not _close(sign * (r.entry_diff or 0.0), r.entry_slip, 0.011):
        out.append(f"{r.date}: entry_diff {r.entry_diff} と entry slip {r.entry_slip} が不整合")
    if r.exit_reason == "sl" and not _close(sign * (r.exit_diff or 0.0), r.exit_slip, 0.011):
        out.append(f"{r.date}: exit_diff {r.exit_diff} と exit slip {r.exit_slip} が不整合")
    if (
        r.exit_reason == "eod"
        and r.eod_close is not None
        and not _close(round((r.exit_price - r.eod_close) / PIP, 2), r.eod_gap, 0.011)
    ):
        out.append(f"{r.date}: eod_gap {r.eod_gap} を再計算できない")
    checks = (
        ("result_pips", r.result_pips),
        ("sl_pips", r.risk_pips),
        ("spread_at_fill_pips", r.entry_spread),
        ("slip_entry_pips", r.entry_slip),
    )
    for key, expected in checks:
        raw = note.get(key, "")
        try:
            if not _close(float(raw), expected, 0.051):
                out.append(f"{r.date}: note.{key}={raw} ≠ sessions {expected:.2f}")
        except ValueError:
            out.append(f"{r.date}: note.{key} が数値でない ({raw!r})")
    if (note.get("exit_reason") == "time") != (r.exit_reason == "eod"):
        out.append(f"{r.date}: note.exit_reason={note.get('exit_reason')} ≠ {r.exit_reason}")
    return out


# --------------------------------------------------------------------------- 集計


def upper_pct(values: list[float], q: float) -> float | None:
    """nearest-rank（切り上げ）。`weekly._upper_pct` と同一。"""
    if not values:
        return None
    ordered = sorted(values)
    rank = math.ceil(q * len(ordered))
    return ordered[min(len(ordered), max(1, rank)) - 1]


def slip_population(rows: list[SessionRow], *, exclude_null_gap: bool) -> list[float]:
    """滑り母集団（不利 = 正）。定義 A は `exclude_null_gap=False`、定義 B は True。"""
    out: list[float] = []
    for r in rows:
        if r.date in SLIP_EXCLUDED_DATES:
            continue
        if not (exclude_null_gap and r.entry_gap_ms is None):
            out.append(r.entry_slip)
        if (
            r.exit_reason == "sl"
            and r.exit_slip is not None
            and not (exclude_null_gap and r.exit_gap_ms is None)
        ):
            out.append(-r.exit_slip)
    return out


def _fmt_dist(values: list[float]) -> str:
    if not values:
        return "n=0"
    p90 = upper_pct(values, 0.9)
    p95 = upper_pct(values, 0.95)
    return (
        f"n={len(values)} mean={statistics.mean(values):.2f} p50={statistics.median(values):.2f} "
        f"p90={p90:.2f} p95={p95:.2f} max={max(values):.2f}"
    )


def _bins(values: list[float], edges: list[tuple[float, float]], labels: list[str]) -> str:
    counts = [0] * len(labels)
    for v in values:
        v = round(v, 2)
        for i, (lo, hi) in enumerate(edges):
            if (lo == hi == 0 and v == 0) or (lo != hi and lo < v <= hi):
                counts[i] += 1
                break
    return " / ".join(f"{lab}: {c}" for lab, c in zip(labels, counts, strict=True))


def _edge_line(rows: list[SessionRow], label: str) -> str:
    """参考指標（§1.2: 合否・継続判断に使わない）。"""
    if not rows:
        return f"{label}: n=0"
    rs = [r.r_multiple for r in rows]
    wins = sum(1 for x in rs if x > 0)
    t = float("nan")
    if len(rs) > 1 and statistics.stdev(rs) > 0:
        t = statistics.mean(rs) / (statistics.stdev(rs) / math.sqrt(len(rs)))
    return (
        f"{label}: n={len(rs)} win={wins} ({wins / len(rs):.0%}) sumR={sum(rs):+.2f} "
        f"meanR={statistics.mean(rs):+.3f} medR={statistics.median(rs):+.3f} t={t:+.2f} "
        f"sum_pips={sum(r.result_pips for r in rows):+.1f}"
    )


def print_report(rows: list[SessionRow], issues: list[str]) -> None:
    n_raw = len(rows)
    design = [r for r in rows if r.date not in SLIP_EXCLUDED_DATES]
    print(f"== 対象: {rows[0].date} 〜 {rows[-1].date} / セッション {n_raw} 日 ==")
    print(f"\n== A.1 突合（不整合 {len(issues)} 件）==")
    for line in issues:
        print("  ", line)

    print("\n== KPI ==")
    print(f"設計 n = {len(design)}（生 fill {n_raw}・除外 {sorted(SLIP_EXCLUDED_DATES)}）")
    print(
        f"verify match = {sum(r.verdict == 'match' for r in rows)}/{n_raw}・halted = {sum(r.halted for r in rows)}"
    )
    for label, flag in (
        ("定義 A（改訂前・併記）", False),
        ("定義 B（設計 §6.3 / コードの正）", True),
    ):
        pop = slip_population(rows, exclude_null_gap=flag)
        print(f"滑り {label}: {_fmt_dist(pop)}  [上限 p50 3.0 / p90 5.84]")

    print("\n== A.2 分布（pips）==")
    edges = [(0, 0), (0, 0.5), (0.5, 1.0), (1.0, 2.0), (2.0, 3.0), (3.0, 99)]
    labels = ["=0", "(0,0.5]", "(0.5,1]", "(1,2]", "(2,3]", ">3"]
    entries = [r.entry_slip for r in design]
    sl_exits = [-r.exit_slip for r in rows if r.exit_reason == "sl" and r.exit_slip is not None]
    print("entry slip:", _fmt_dist(entries), "|", _bins(entries, edges, labels))
    print("SL exit slip:", _fmt_dist(sl_exits), "|", _bins(sl_exits, edges, labels))
    for direction in ("long", "short"):
        print(
            f"entry slip {direction}:",
            _fmt_dist([r.entry_slip for r in design if r.direction == direction]),
        )
    spreads = [r.entry_spread for r in rows] + [r.exit_spread for r in rows]
    print(
        "spread（fill/exit 観測点）:",
        _fmt_dist(spreads),
        "|",
        _bins(
            spreads,
            [(0, 0.5), (0.5, 1.0), (1.0, 2.0), (2.0, 99)],
            ["<=0.5", "(0.5,1]", "(1,2]", ">2"],
        ),
    )
    over = [
        (r.date, "entry", round(r.entry_spread, 1))
        for r in rows
        if r.entry_spread > SPREAD_BASELINE_PIPS + 0.01
    ]
    over += [
        (r.date, "exit", round(r.exit_spread, 1))
        for r in rows
        if r.exit_spread > SPREAD_BASELINE_PIPS + 0.01
    ]
    print(f"  前提 {SPREAD_BASELINE_PIPS} 超: {len(over)}/{len(spreads)} → {over}")
    combined = [r.entry_slip + max(r.entry_spread - SPREAD_BASELINE_PIPS, 0.0) for r in design] + [
        -r.exit_slip + max(r.exit_spread - SPREAD_BASELINE_PIPS, 0.0)
        for r in design
        if r.exit_reason == "sl" and r.exit_slip is not None
    ]
    print("合算 slip + max(spread−前提, 0)（参考・中止条件には使わない）:", _fmt_dist(combined))
    gaps = [g for r in rows for g in (r.entry_gap_ms, r.exit_gap_ms) if g is not None]
    print(
        f"poll_gap_ms: n={len(gaps)} min={min(gaps)} p50={statistics.median(gaps):.0f} max={max(gaps)}"
        f"  None(entry)={[r.date for r in rows if r.entry_gap_ms is None]}"
    )
    price_gaps: list[tuple[float, float, float]] = []  # (slip, spread, |gap|)
    for r in design:
        if r.entry_price_gap is not None:
            price_gaps.append((r.entry_slip, r.entry_spread, abs(r.entry_price_gap) / PIP))
        if r.exit_reason == "sl" and r.exit_price_gap is not None and r.exit_slip is not None:
            price_gaps.append((-r.exit_slip, r.exit_spread, abs(r.exit_price_gap) / PIP))
    violations = [pg for pg in price_gaps if pg[0] > pg[2] + 1e-6]
    print(
        "|price_gap|:",
        _fmt_dist([pg[2] for pg in price_gaps]),
        f"| slip<=|gap| 違反 {len(violations)} 件 | corr(slip,|gap|)={_corr([p[0] for p in price_gaps], [p[2] for p in price_gaps]):+.2f}"
        f" corr(spread,|gap|)={_corr([p[1] for p in price_gaps], [p[2] for p in price_gaps]):+.2f}",
    )
    eod_gaps = [r.eod_gap for r in rows if r.exit_reason == "eod" and r.eod_gap is not None]
    print(
        f"eod_gap_pips: n={len(eod_gaps)} values={eod_gaps} mean={statistics.mean(eod_gaps):+.2f}"
    )
    late = sorted(
        r.fill_vs_replay_bar_s
        for r in rows
        if r.in_bar is False and r.fill_vs_replay_bar_s is not None
    )
    print(
        f"fill_time_in_replay_bar=false: {sum(r.in_bar is False for r in rows)}/{n_raw}（遅れ秒 {[round(x) for x in late]}）"
    )

    print("\n== A.3 exit 種別 ==")
    for kind in ("sl", "eod"):
        sub = [r for r in rows if r.exit_reason == kind]
        print(
            f"{kind}: n={len(sub)} match={sum(r.verdict == 'match' for r in sub)} entry slip {_fmt_dist([r.entry_slip for r in sub if r.date not in SLIP_EXCLUDED_DATES])}"
        )

    print("\n== ファネル ==")
    print(
        f"sessions {n_raw} → armed {n_raw} → filled {n_raw} → closed {sum(r.exit_reason in ('sl', 'eod') for r in rows)}"
        f"（sl {sum(r.exit_reason == 'sl' for r in rows)} / eod {sum(r.exit_reason == 'eod' for r in rows)}）"
        f"・verify match {sum(r.verdict == 'match' for r in rows)}"
    )

    print("\n== B. 参考指標（損益で判定しない・§1.2）==")
    print(_edge_line(rows, "all"))
    print(_edge_line(design, "design n"))
    for direction in ("long", "short"):
        print(_edge_line([r for r in rows if r.direction == direction], direction))
    for kind in ("eod", "sl"):
        print(_edge_line([r for r in rows if r.exit_reason == kind], f"exit={kind}"))
    print("W/L:", "".join("W" if r.r_multiple > 0 else "L" for r in rows))
    print(
        f"range pips median={statistics.median(r.range_pips for r in rows):.1f} "
        f"risk pips median={statistics.median(r.risk_pips for r in rows):.1f} "
        f"fill<09:15: {sum(1 for r in rows if datetime.fromisoformat(r.fill_ts).time() < dtime(9, 15))}/{n_raw} "
        f"weekday×dir: {dict(Counter((r.weekday, r.direction) for r in rows))}"
    )


def _corr(a: list[float], b: list[float]) -> float:
    if len(a) < 2:
        return float("nan")
    ma, mb = statistics.mean(a), statistics.mean(b)
    sa = math.sqrt(sum((x - ma) ** 2 for x in a))
    sb = math.sqrt(sum((y - mb) ** 2 for y in b))
    if sa == 0 or sb == 0:
        return float("nan")
    return sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True)) / (sa * sb)


# --------------------------------------------------------------------------- 独立リプレイ（任意）


def fetch_klines(days: list[str], out_dir: Path, *, sleep_sec: float = 1.0) -> None:
    """GMO Public API から BID 1min / 15min klines を取得して保存する（GET のみ・read-only）。"""
    from backend.src.clients.gmo_client import GmoFxClient
    from backend.src.gmo_errors import raise_for_gmo_status
    from backend.src.gmo_fx_endpoints import PUBLIC_KLINES

    out_dir.mkdir(parents=True, exist_ok=True)
    client = GmoFxClient()
    try:
        for day in days:
            ds = day.replace("-", "")
            for interval in ("1min", "15min"):
                target = out_dir / f"{ds}_{interval}.json"
                if target.exists():
                    continue
                resp = client.public_get(
                    PUBLIC_KLINES,
                    params={
                        "symbol": "USD_JPY",
                        "priceType": "BID",
                        "interval": interval,
                        "date": ds,
                    },
                )
                raise_for_gmo_status(resp, context=f"public/klines:{interval}")
                target.write_text(json.dumps(resp.json().get("data", [])), encoding="utf-8")
                time.sleep(sleep_sec)
    finally:
        client.close()


def replay_and_paths(rows: list[SessionRow], klines_dir: Path) -> None:
    """判定コアをローカルで走らせ verify レポートと突合し、1 分足からセッション内パスを出す。"""
    from backend.src.orb.constants import BAR_MINUTES_15, EOD_TIME, LON
    from backend.src.orb.evaluate import evaluate_day
    from backend.src.orb.models import Bar1m, Bar15
    from backend.src.orb.range import eod_close_price, range_from_1m, range_from_15m

    mismatches = 0
    print("\n== 独立リプレイ（klines 再取得 → evaluate_day）vs verify ==")
    for r in rows:
        ds = r.date.replace("-", "")
        raw_1m = json.loads((klines_dir / f"{ds}_1min.json").read_text(encoding="utf-8"))
        raw_15m = json.loads((klines_dir / f"{ds}_15min.json").read_text(encoding="utf-8"))
        minutes = sorted(
            (
                Bar1m(
                    local_open=datetime.fromtimestamp(int(c["openTime"]) / 1000, tz=UTC).astimezone(
                        LON
                    ),
                    open=float(c["open"]),
                    high=float(c["high"]),
                    low=float(c["low"]),
                    close=float(c["close"]),
                )
                for c in raw_1m
            ),
            key=lambda b: b.local_open,
        )
        minutes = [b for b in minutes if b.local_open.date().isoformat() == r.date]
        bars15: list[Bar15] = []
        for c in raw_15m:
            open_utc = datetime.fromtimestamp(int(c["openTime"]) / 1000, tz=UTC)
            bars15.append(
                Bar15(
                    open_time=open_utc,
                    open=float(c["open"]),
                    high=float(c["high"]),
                    low=float(c["low"]),
                    close=float(c["close"]),
                    local_close=(open_utc + timedelta(minutes=BAR_MINUTES_15)).astimezone(LON),
                )
            )
        bars15 = sorted(
            (b for b in bars15 if b.local_close.date().isoformat() == r.date),
            key=lambda b: b.local_close,
        )
        levels = range_from_1m(minutes) or range_from_15m(bars15)
        result = evaluate_day(r.date, bars15, minutes, range_levels=levels)
        trade = None if result is None else result.trade
        signal = (
            "no_window"
            if result is None
            else (result.skip_reason or ("long" if trade and trade.direction == 1 else "short"))
        )
        ok = (
            levels is not None
            and signal == r.replay_signal
            and _close(levels.high, r.range_high, 5e-4)
            and _close(levels.low, r.range_low, 5e-4)
            and (
                trade is None
                or (
                    _close(trade.entry, r.replay_entry, 5e-4)
                    and _close(trade.exit, r.replay_exit, 5e-4)
                    and trade.outcome == r.replay_outcome
                    and trade.fill_et == r.replay_fill_et
                )
            )
            and _close(eod_close_price(bars15), r.eod_close, 5e-4)
        )
        mismatches += 0 if ok else 1
        # セッション内パス: デモ fill 以降の MFE/MAE（R）・反対側到達・トリガー再テスト
        fill_ts = datetime.fromisoformat(r.fill_ts)
        exit_ts = datetime.fromisoformat(r.exit_ts)
        fill_min = fill_ts.replace(second=0, microsecond=0)
        risk = abs(r.fill_price - r.sl_price)
        is_long = r.direction == "long"
        window = [
            b
            for b in minutes
            if fill_min <= b.local_open <= exit_ts and b.local_open.time() < EOD_TIME
        ]
        if is_long:
            mfe = max((b.high - r.fill_price) for b in window) / risk
            mae = max((r.fill_price - b.low) for b in window) / risk
        else:
            mfe = max((r.fill_price - b.low) for b in window) / risk
            mae = max((b.high - r.fill_price) for b in window) / risk
        after = [b for b in window if b.local_open > fill_min]
        retest_min: float | None = None
        for b in after:
            if (b.low <= r.trigger) if is_long else (b.high >= r.trigger):
                retest_min = (b.local_open - fill_min).total_seconds() / 60
                break
        opp_level = r.sell_stop if is_long else r.buy_stop
        opp_5m = any(
            ((b.low <= opp_level) if is_long else (b.high >= opp_level))
            for b in window
            if b.local_open < fill_ts + timedelta(minutes=5)
        )
        print(
            f"{r.date} replay_ok={ok} {r.direction:5s} R={r.r_multiple:+.2f} {r.exit_reason:3s} "
            f"MFE={mfe:.2f}R MAE={mae:.2f}R retest={'—' if retest_min is None else f'{round(retest_min)}min'} "
            f"opp<=5m={opp_5m}"
        )
    print(f"独立リプレイ不一致: {mismatches}/{len(rows)}")


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--s3-dir", type=Path, required=True, help="aws s3 sync した orb-demo/ のローカルコピー"
    )
    parser.add_argument(
        "--klines-dir", type=Path, help="YYYYMMDD_{1min,15min}.json（GMO klines の data 配列）"
    )
    parser.add_argument(
        "--fetch-klines",
        action="store_true",
        help="--klines-dir に無い日を Public API から取得する",
    )
    parser.add_argument(
        "--out", type=Path, help="突合済みマスタ（master.json）の出力先ディレクトリ"
    )
    args = parser.parse_args(argv)

    rows, issues = load_rows(args.s3_dir)
    if not rows:
        print("sessions が空です", file=sys.stderr)
        return 1
    print_report(rows, issues)
    if args.klines_dir is not None:
        if args.fetch_klines:
            fetch_klines([r.date for r in rows], args.klines_dir)
        replay_and_paths(rows, args.klines_dir)
    if args.out is not None:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "master.json").write_text(
            json.dumps([asdict(r) for r in rows], ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"\nmaster.json → {args.out / 'master.json'}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
