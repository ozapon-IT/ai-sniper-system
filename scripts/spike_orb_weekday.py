"""ORB-1b スパイク: 開場中でないと取れない項目を実測する（Issue #268）.

設計書: docs/ai-dev/london-orb-demo-design.md §6.2（確定遅延・リトライ）・§6.3（スプレッド前提）
付録B: docs/spikes/london_orb_api_spec.md §5.1（未確定項目）

🔴 **Public API の GET のみ**（`GmoFxClient` を資格情報なしで生成し `public_get` のみ参照）。
発注系 POST は発生しない（設計書 §3.2 の封じ込め第1層）。

モード:
  ``--mode delay``   1分足の確定遅延を実測する（**開場中のみ有効**）。
                     klines をポーリングし、新しい足が初めて現れた時刻と足の閉じ時刻の差を測る。
  ``--mode spread``  直近 N 営業日の 1分足 BID/ASK から **London 窓のスプレッド分布**を出す。
                     開場・閉場によらず実行可（過去日の確定データを使う）。

実行例:
    PYTHONPATH=. python scripts/spike_orb_weekday.py --mode delay --minutes 10
    PYTHONPATH=. python scripts/spike_orb_weekday.py --mode spread --days 5
"""

from __future__ import annotations

import argparse
import statistics
import time as _time
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from backend.src.clients.gmo_client import GmoFxClient
from backend.src.gmo_fx_endpoints import PUBLIC_KLINES, PUBLIC_STATUS, PUBLIC_TICKER

LONDON = ZoneInfo("Europe/London")
JST = ZoneInfo("Asia/Tokyo")
SYMBOL = "USD_JPY"
PIP = 0.01

# ORB の各窓（Europe/London）
RANGE_START, RANGE_END = time(8, 0), time(9, 0)
ORDER_WINDOW_END = time(11, 0)
EOD_TIME = time(16, 0)


def _fetch_klines(client: GmoFxClient, date_yyyymmdd: str, price_type: str) -> list[dict[str, Any]]:
    resp = client.public_get(
        PUBLIC_KLINES,
        params={
            "symbol": SYMBOL,
            "priceType": price_type,
            "interval": "1min",
            "date": date_yyyymmdd,
        },
    )
    body = resp.json()
    return body.get("data", []) if isinstance(body, dict) else []


def _market_status(client: GmoFxClient) -> str:
    data = client.public_get(PUBLIC_STATUS).json().get("data", {})
    return str(data.get("status", "UNKNOWN"))


def _usdjpy_ticker(client: GmoFxClient) -> dict[str, Any] | None:
    for row in client.public_get(PUBLIC_TICKER).json().get("data", []):
        if row.get("symbol") == SYMBOL:
            return dict(row)
    return None


# ---------------------------------------------------------------- delay


def measure_delay(client: GmoFxClient, *, minutes: int, interval_sec: float) -> None:
    """1分足の**確定**遅延を実測する（開場中のみ）。

    🔴 測るのは「出現」ではなく「値が動かなくなる時刻」。
    klines は足が開いた直後から**進行中の足**を返し、その足の high/low/close は
    その分が終わるまでティック更新される（E05 §5）。したがって §6.2 が必要とするのは
    ``最後に OHLC が変化した時刻 − 足の閉じ時刻`` である。

    - 値 <= 0: 閉じ時刻には既に確定している（9:01 に 8:59 足を読んで安全）
    - 値 > 0 : 閉じたあとも値が動く（リトライ設計の見直しが要る）
    """
    status = _market_status(client)
    print(f"market status = {status}")
    if status != "OPEN":
        print("🔴 閉場中のため確定遅延は測れない（新しい足が生成されない）。中止。")
        return

    today_jst = datetime.now(tz=JST)
    # 分足ファイルは JST 06:00 起点の 1 日分（付録B §2 / E05）
    file_date = (today_jst - timedelta(days=1)) if today_jst.time() < time(6, 0) else today_jst
    date_str = file_date.strftime("%Y%m%d")
    print(f"klines date={date_str} (JST 06:00 起点) / poll {interval_sec}s x {minutes}min\n")

    # openTime -> (最後に観測した OHLC, 最後に値が変化した観測時刻, 初出時刻)
    last_ohlc: dict[int, tuple[str, str, str, str]] = {}
    last_change: dict[int, datetime] = {}
    first_seen: dict[int, datetime] = {}
    throttled = 0
    deadline = _time.time() + minutes * 60

    while _time.time() < deadline:
        t_req = datetime.now(tz=UTC)
        try:
            candles = _fetch_klines(client, date_str, "BID")
        except Exception as exc:  # noqa: BLE001 — スパイク用に握って継続
            print(f"  fetch error: {exc!r}")
            _time.sleep(interval_sec)
            continue
        if not candles:
            throttled += 1
            print(f"  empty/throttled response ({throttled})")
            _time.sleep(interval_sec)
            continue

        # 末尾数本だけ追えば足りる（過去足は不変・付録B §2）
        for cd in candles[-4:]:
            ot = int(cd["openTime"])
            ohlc = (str(cd["open"]), str(cd["high"]), str(cd["low"]), str(cd["close"]))
            if ot not in last_ohlc:
                first_seen[ot] = t_req
                last_ohlc[ot] = ohlc
                last_change[ot] = t_req
            elif last_ohlc[ot] != ohlc:
                last_ohlc[ot] = ohlc
                last_change[ot] = t_req
        _time.sleep(interval_sec)

    print()
    rows: list[tuple[datetime, float, float]] = []
    for ot, changed_at in sorted(last_change.items()):
        bar_open = datetime.fromtimestamp(ot / 1000, tz=UTC)
        bar_close = bar_open + timedelta(minutes=1)
        # 閉じる前から観測し、閉じたあとも観測を続けた足だけが有効サンプル
        if first_seen[ot] >= bar_close or max(last_change.values()) < bar_close:
            continue
        settle = (changed_at - bar_close).total_seconds()
        appear = (first_seen[ot] - bar_open).total_seconds()
        rows.append((bar_open, appear, settle))

    if not rows:
        print("有効サンプルなし（観測時間を延ばす）")
        return

    print("=== 足ごと（London 時刻） ===")
    for bar_open, appear, settle in rows:
        print(
            f"  {bar_open.astimezone(LONDON):%H:%M} open+{appear:5.1f}s に出現 /"
            f" 最終更新は close {settle:+6.1f}s"
        )

    settles = sorted(s for _, _, s in rows)
    n = len(settles)
    print()
    print(f"=== 確定遅延（最終更新 − 閉じ時刻） n={n} ===")
    print(f"  min / med / max = {settles[0]:+.1f}s / {statistics.median(settles):+.1f}s / {settles[-1]:+.1f}s")
    print(f"  ※ ポーリング間隔 {interval_sec}s の粒度で上振れする")
    print(f"  throttle/空レスポンス回数 = {throttled}")

    worst = settles[-1]
    print()
    print("→ 設計書 §6.2 の判定:")
    margin = 60.0 - worst
    if worst <= 0:
        print(f"  最悪 {worst:+.1f}s ≤ 0。足は閉じた時点で確定している")
        print("  **London 9:01 時点で 8:59 足（9:00 close）は確定済み** — リトライ設計は余裕あり")
    elif worst < 60:
        print(f"  最悪 {worst:+.1f}s（閉じたあとも更新あり）。9:01 までの余裕 {margin:.1f}s")
        print("  9:01 読みは成立するが、9:00 ちょうどに読むと未確定を掴む可能性がある")
    else:
        print(f"  🔴 最悪 {worst:+.1f}s ≥ 60s。9:01 に確定していない — 設計書 §6.2 の見直しが要る")


# ---------------------------------------------------------------- spread


def _window_label(t: time) -> str | None:
    if RANGE_START <= t < RANGE_END:
        return "range_08_09"
    if RANGE_END <= t < ORDER_WINDOW_END:
        return "order_09_11"
    if ORDER_WINDOW_END <= t < EOD_TIME:
        return "hold_11_16"
    return None


def measure_spread(client: GmoFxClient, *, days: int) -> None:
    """直近 N 営業日の 1分足 BID/ASK から London 窓のスプレッド分布を出す。"""
    print(f"直近 {days} 営業日の London 窓スプレッド（1分足 close の ASK−BID）\n")
    per_window: dict[str, list[float]] = {}
    per_day: list[tuple[str, int, float]] = []

    probe = datetime.now(tz=JST)
    collected = 0
    back = 0
    while collected < days and back < days * 3 + 10:
        back += 1
        d = probe - timedelta(days=back)
        date_str = d.strftime("%Y%m%d")
        bid = _fetch_klines(client, date_str, "BID")
        if not bid:
            continue
        ask = _fetch_klines(client, date_str, "ASK")
        if not ask:
            continue
        ask_by_ot = {int(c["openTime"]): float(c["close"]) for c in ask}

        day_vals: list[float] = []
        for c in bid:
            ot = int(c["openTime"])
            a = ask_by_ot.get(ot)
            if a is None:
                continue
            lon = datetime.fromtimestamp(ot / 1000, tz=UTC).astimezone(LONDON)
            label = _window_label(lon.time())
            if label is None:
                continue
            spread = (a - float(c["close"])) / PIP
            per_window.setdefault(label, []).append(spread)
            day_vals.append(spread)

        if day_vals:
            collected += 1
            per_day.append((date_str, len(day_vals), statistics.median(day_vals)))

    if not per_day:
        print("データなし")
        return

    print("日別（ORB 窓 8:00〜16:00 London 全体）:")
    for date_str, n, med in per_day:
        print(f"  {date_str}: n={n:4d}  中央値 {med:.2f} pips")

    print()
    print("窓別の分布:")
    for label in ("range_08_09", "order_09_11", "hold_11_16"):
        vals = sorted(per_window.get(label, []))
        if not vals:
            continue
        n = len(vals)
        print(
            f"  {label:12s} n={n:5d}  "
            f"p50={statistics.median(vals):.2f}  "
            f"p90={vals[int(0.9 * (n - 1))]:.2f}  "
            f"max={vals[-1]:.2f} pips"
        )

    allv = sorted(v for vs in per_window.values() for v in vs)
    med_all = statistics.median(allv)
    print()
    print(f"=== ORB 窓 全体: n={len(allv)} / 中央値 {med_all:.2f} pips ===")
    print("→ Phase 0 / 設計書 §6.3 の前提は 0.2 pips")
    if med_all > 0.35:
        print(f"  🔴 実測 {med_all:.2f} pips は前提 0.2 と乖離。§6.3 のコスト前提を更新すべき")
        print(f"  （滑り耐性 5.84pips に対する消費: 追加 {med_all - 0.2:.2f} pips）")
    else:
        print(f"  実測 {med_all:.2f} pips は前提 0.2 と整合")


def main() -> None:
    p = argparse.ArgumentParser(description="ORB-1b weekday spike (read-only Public API)")
    p.add_argument("--mode", choices=["delay", "spread"], required=True)
    p.add_argument("--minutes", type=int, default=10, help="delay: 観測時間（分）")
    p.add_argument("--interval", type=float, default=5.0, help="delay: ポーリング間隔（秒）")
    p.add_argument("--days", type=int, default=5, help="spread: 遡る営業日数")
    args = p.parse_args()

    now = datetime.now(tz=UTC)
    print("=" * 74)
    print("ORB-1b スパイク（Public API・GET のみ / 発注系 POST なし）")
    print(f"実行時刻: {now.astimezone(JST):%Y-%m-%d %H:%M %Z} / {now.astimezone(LONDON):%H:%M %Z}")
    print("=" * 74)

    with GmoFxClient() as client:
        if args.mode == "delay":
            measure_delay(client, minutes=args.minutes, interval_sec=args.interval)
        else:
            measure_spread(client, days=args.days)


if __name__ == "__main__":
    main()
