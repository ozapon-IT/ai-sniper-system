"""ORB-1 スパイク: ロンドンORBデモの前提を Public API 実測で確定する（Issue #262）.

設計書: docs/ai-dev/london-orb-demo-design.md §6.2（レンジ確定）・§3.3（レートリミット）

🔴 **本スクリプトは Public API の GET のみを使用する**（`GmoFxClient` を資格情報なしで生成）。
`public_get` のみを参照し、`private_get` / `private_post` / `PRIVATE_*` への参照はゼロ。
加えて API シークレットが空のため、仮に private 経路を呼んでも署名認証に通らない。
この 2 点で発注系 POST が発生しない（設計書 §3.2 の封じ込め第1層）。
※ クライアントのメソッド自体は資格情報が空でも呼び出し可能（例外は出ず 401 になる）。
   封じ込めの根拠は「参照ゼロ + 空資格情報」であって「メソッドが使えないこと」ではない。

測定項目:
  A. `GET /public/v1/status` — 市場状態（ORB poller の開場判定の基礎）
  B. `GET /public/v1/ticker` — USD_JPY の気配・`status` フィールド・スプレッド
  C. `GET /public/v1/klines`（`interval=1min` / `priceType=BID`）の構造検証
     - ORB セッション（London 8:00 レンジ開始 〜 16:00 EOD）が単一の
       `date=YYYYMMDD` ファイル（JST 06:00 起点）に収まるか。夏(BST)・冬(GMT)の両方で確認
     - レンジ窓の 1 分足 60 本（openTime London 8:00〜8:59）が欠損なく存在するか
     - EOD 参照足（openTime London 15:59 = close 16:00）が存在するか
  D. ローカルキャッシュ全期間での上記カバレッジ分布（`--cache-root` 指定時のみ）

実行例:
    PYTHONPATH=. python scripts/spike_orb_public_api.py
    PYTHONPATH=. python scripts/spike_orb_public_api.py \
        --cache-root backtest/cache/USD_JPY
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from backend.src.clients.gmo_client import GmoFxClient
from backend.src.gmo_fx_endpoints import PUBLIC_KLINES, PUBLIC_STATUS, PUBLIC_TICKER

LONDON = ZoneInfo("Europe/London")
JST = ZoneInfo("Asia/Tokyo")
SYMBOL = "USD_JPY"
PIP = 0.01

# 凍結ルール（ORB ルール定義 §2-3）の時刻。
RANGE_START = time(8, 0)      # レンジ形成開始（この足の openTime から）
RANGE_LAST_OPEN = time(8, 59)  # レンジ最終 1 分足の openTime（close = 9:00）
EOD_LAST_OPEN = time(15, 59)   # EOD 参照 1 分足の openTime（close = London 16:00）
EXPECTED_RANGE_BARS = 60


def _to_london(open_time_ms: str | int) -> datetime:
    """klines の openTime（エポック ms・UTC）を Europe/London の datetime に変換する."""
    return datetime.fromtimestamp(int(open_time_ms) / 1000, tz=UTC).astimezone(LONDON)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M %Z")


def probe_market_status(client: GmoFxClient) -> None:
    """A. 市場状態（`data.status` が OPEN/CLOSE/MAINTENANCE）."""
    resp = client.public_get(PUBLIC_STATUS)
    print(f"\n=== A. GET {PUBLIC_STATUS} ===")
    print(f"HTTP {resp.status_code}  body={json.dumps(resp.json(), ensure_ascii=False)}")


def probe_ticker(client: GmoFxClient) -> None:
    """B. USD_JPY の気配とシンボル別 status・スプレッド."""
    resp = client.public_get(PUBLIC_TICKER)
    print(f"\n=== B. GET {PUBLIC_TICKER} ===")
    print(f"HTTP {resp.status_code}")
    body: dict[str, Any] = resp.json()
    for row in body.get("data", []):
        if row.get("symbol") != SYMBOL:
            continue
        ask, bid = float(row["ask"]), float(row["bid"])
        print(f"  raw={json.dumps(row, ensure_ascii=False)}")
        print(f"  spread = {(ask - bid) / PIP:.2f} pips  (ask {ask} / bid {bid})")
        print(f"  symbol status = {row.get('status')!r}")


def analyse_day(client: GmoFxClient, date_yyyymmdd: str) -> dict[str, Any]:
    """C. 1 分足 1 日分（JST 06:00 起点）の ORB セッション被覆を検証する."""
    resp = client.public_get(
        PUBLIC_KLINES,
        params={
            "symbol": SYMBOL,
            "priceType": "BID",
            "interval": "1min",
            "date": date_yyyymmdd,
        },
    )
    body: dict[str, Any] = resp.json()
    candles = body.get("data", []) if isinstance(body, dict) else []
    return summarise_candles(date_yyyymmdd, candles, http_status=resp.status_code)


def summarise_candles(
    date_yyyymmdd: str, candles: list[dict[str, Any]], *, http_status: int | None = None
) -> dict[str, Any]:
    """1 日分の 1 分足から ORB セッションの被覆状況を要約する（API/キャッシュ共通）."""
    if not candles:
        return {"date": date_yyyymmdd, "http": http_status, "bars": 0, "error": "no data"}

    london_times = [_to_london(c["openTime"]) for c in candles]
    first, last = london_times[0], london_times[-1]

    # レンジ窓（openTime が London 8:00〜8:59 のもの）。ファイル内の London 日付は
    # セッション日と一致する想定（本スパイクの検証対象そのもの）。
    session_date = max(
        (dt.date() for dt in london_times if RANGE_START <= dt.time() <= RANGE_LAST_OPEN),
        default=None,
    )
    range_bars = [
        dt
        for dt in london_times
        if dt.date() == session_date and RANGE_START <= dt.time() <= RANGE_LAST_OPEN
    ]
    eod_bar = [
        dt for dt in london_times if dt.date() == session_date and dt.time() == EOD_LAST_OPEN
    ]

    # セッション全体（レンジ開始 8:00 〜 EOD 足の close 16:00）が同一ファイルに収まるか。
    session_covered = session_date is not None and (
        first <= datetime.combine(session_date, RANGE_START, tzinfo=LONDON)
        and last >= datetime.combine(session_date, EOD_LAST_OPEN, tzinfo=LONDON)
    )

    return {
        "date": date_yyyymmdd,
        "http": http_status,
        "bars": len(candles),
        "tz": first.tzname(),
        "first_london": _fmt(first),
        "last_london": _fmt(last),
        "first_jst": _fmt(first.astimezone(JST)),
        "last_jst": _fmt(last.astimezone(JST)),
        "session_date": session_date.isoformat() if session_date else None,
        "range_bars": len(range_bars),
        "range_complete": len(range_bars) == EXPECTED_RANGE_BARS,
        "eod_bar_present": bool(eod_bar),
        "session_in_one_file": session_covered,
    }


def print_day(label: str, r: dict[str, Any]) -> None:
    if r.get("error"):
        print(f"  [{label}] date={r['date']} HTTP={r['http']} -> {r['error']}")
        return
    ok = "OK " if (r["range_complete"] and r["eod_bar_present"] and r["session_in_one_file"]) else "NG "
    print(
        f"  [{label}] {ok} date={r['date']} tz={r['tz']} bars={r['bars']}\n"
        f"        file span (London): {r['first_london']} -> {r['last_london']}\n"
        f"        file span (JST)   : {r['first_jst']} -> {r['last_jst']}\n"
        f"        session_date={r['session_date']} range_bars={r['range_bars']}/{EXPECTED_RANGE_BARS}"
        f" eod_bar={r['eod_bar_present']} session_in_one_file={r['session_in_one_file']}"
    )


def scan_cache(cache_root: Path) -> None:
    """D. ローカル 1 分足キャッシュ全期間でレンジ窓の被覆分布を出す."""
    d = cache_root / "BID" / "1min"
    files = sorted(d.glob("*.json"))
    print(f"\n=== D. cache scan: {d} ({len(files)} files) ===")
    if not files:
        print("  1min キャッシュなし（--cache-root がメインチェックアウトを指しているか確認）")
        return

    complete = 0
    incomplete: list[tuple[str, int]] = []
    no_session = 0
    eod_missing: list[str] = []
    not_one_file = 0
    for f in files:
        payload = json.loads(f.read_text())
        candles = payload.get("data", payload.get("candles", []))
        r = summarise_candles(f.stem, candles)
        if r.get("error") or r["session_date"] is None:
            no_session += 1
            continue
        if r["range_complete"]:
            complete += 1
        else:
            incomplete.append((r["date"], r["range_bars"]))
        if not r["eod_bar_present"]:
            eod_missing.append(r["date"])
        if not r["session_in_one_file"]:
            not_one_file += 1

    scanned = len(files) - no_session
    print(f"  レンジ窓を含む日: {scanned} / ファイル {len(files)}（窓なし={no_session}=週末等）")
    if scanned:
        pct = 100 * complete / scanned
        print(f"  レンジ 60 本完全: {complete} / {scanned}（{pct:.1f}%）")
    else:
        print("  レンジ 60 本完全: n/a（対象日なし）")
    print(f"  EOD 足欠損: {len(eod_missing)} 日")
    print(f"  セッションが単一ファイルに収まらない日: {not_one_file} 日")
    if incomplete:
        print(f"  欠損日（最大20件表示・date, 実本数）: {incomplete[:20]}")
    if eod_missing:
        print(f"  EOD 欠損日（最大20件）: {eod_missing[:20]}")


def main() -> None:
    p = argparse.ArgumentParser(description="ORB-1 Public API spike (read-only)")
    p.add_argument(
        "--days",
        nargs="*",
        default=["20260807", "20260114"],
        help="1分足を検証する日付（YYYYMMDD）。既定は夏(BST)・冬(GMT)の各1日",
    )
    p.add_argument("--cache-root", type=Path, default=None, help="1分足キャッシュのルート")
    args = p.parse_args()

    now_utc = datetime.now(UTC)
    print("=" * 78)
    print("ORB-1 スパイク（Public API・GET のみ / 発注系 POST なし）")
    print(f"実行時刻: {_fmt(now_utc.astimezone(JST))} / {_fmt(now_utc.astimezone(LONDON))}")
    print("=" * 78)

    # 資格情報を渡さない = private 経路を使えない状態で生成する（封じ込め第1層）。
    with GmoFxClient() as client:
        probe_market_status(client)
        probe_ticker(client)

        print(f"\n=== C. GET {PUBLIC_KLINES} (interval=1min / priceType=BID) ===")
        for day in args.days:
            print_day("api", analyse_day(client, day))

    if args.cache_root:
        scan_cache(args.cache_root.expanduser())


if __name__ == "__main__":
    main()
