"""ORB-8 ローカル通しリハーサル（Issue #278 / 設計書 §11・§1.3）。

過去 N 営業日ぶんの klines キャッシュでパイプライン全体を駆動する:
    session-open → poller（合成観測列）→ verify → ノート生成 → 週次集計

**ネットワーク不要**（`ORB_CACHE_ROOT` のキャッシュを読む）。永続化は `LOCAL_PERSIST_DIR`
（既定は一時ディレクトリ）へ隔離するので、本番の S3 / DynamoDB には触れない。

🔴 **このリハーサルで検証できないこと**（レポートにも出す）:
  - 推定滑りの妥当性 — 観測列は 1 分足から**合成**したもので実際の気配ではない
  - ポーリング欠測・throttle・実 API のレイテンシ
検証できるのは**配管**（バッチが例外なく通る・永続化が噛み合う・ノートが出る・verify が走る）。

実行例:
    ORB_CACHE_ROOT=backtest/cache/USD_JPY \
        PYTHONPATH=. python scripts/orb_rehearsal.py --days 5
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

CACHE_ENV = "ORB_CACHE_ROOT"


def _default_cache_root() -> Path | None:
    env = os.environ.get(CACHE_ENV)
    if env:
        return Path(env).expanduser()
    for candidate in (
        Path.cwd() / "backtest" / "cache" / "USD_JPY",
        Path(__file__).resolve().parents[1] / "backtest" / "cache" / "USD_JPY",
    ):
        if (candidate / "BID" / "1min").is_dir():
            return candidate
    return None


class CacheClient:
    """キャッシュから klines を返すだけの疑似クライアント（`public_get` のみ）。

    ORB デモの本番アダプタ（`adapters/orb_quotes.py`）が期待する形に合わせる。
    ticker は使わない（poller は合成観測列を直接流すため）。
    """

    def __init__(self, cache_root: Path, *, market_status: str = "OPEN") -> None:
        self.cache_root = cache_root
        self.market_status = market_status

    def public_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if path.endswith("/status"):
            return _Resp({"status": 0, "data": {"status": self.market_status}})
        if path.endswith("/ticker"):
            return _Resp({"status": 0, "data": []})
        p = params or {}
        interval = str(p.get("interval", "1min"))
        date_str = str(p.get("date", ""))
        return _Resp({"status": 0, "data": self._candles(interval, date_str)})

    def _candles(self, interval: str, date_str: str) -> list[dict[str, str]]:
        path = self.cache_root / "BID" / interval / f"{date_str}.json"
        if not path.exists():
            return []
        payload = json.loads(path.read_text())
        return list(payload.get("candles", payload.get("data", [])))

    def close(self) -> None:
        pass


class _Resp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


def synthesize_observations(
    bars: list[Any], spread_pips: float = 0.5, interval_sec: int = 5
) -> list[Any]:
    """1 分足から観測列を合成する。

    🔴 **忠実なティック復元ではない。** 各バーの価格経路を
    `open → (安値・高値のうち先に触れたと仮定する方) → もう一方 → close` と置く。
    どちらが先かは終値の向きで決める（陽線なら安値が先）という**heuristic** であり、
    実際の順序は分からない。**滑りの評価には使えない**（§12 限界2 と同種の割り切り）。

    🔴 **刻みは本番 poller と同じ 5 秒**にする（既定 `ORB_POLL_INTERVAL_SEC`）。
    粗い刻みにすると 11:00 跨ぎが `_BOUNDARY_GAP_MS`（10 秒）を超えて `UNCERTAIN` になり、
    **本番では起きない `defer` がリハーサルで出る**（実際 15 秒刻みで発生した）。
    境界の状態機械を検証したいので、刻みだけは production と揃える。
    """
    from backend.src.orb.broker_models import TickerObservation

    out: list[TickerObservation] = []
    spread = spread_pips * 0.01
    slots = max(1, 60 // interval_sec)
    for bar in bars:
        up = bar.close >= bar.open
        first, second = (bar.low, bar.high) if up else (bar.high, bar.low)
        # 価格経路を 4 区画に分け、各区画を 5 秒刻みで埋める（極値と順序は保つ）
        path = [bar.open, first, second, bar.close]
        for i in range(slots):
            bid = path[min(len(path) - 1, i * len(path) // slots)]
            ts = bar.local_open + timedelta(seconds=i * interval_sec)
            out.append(
                TickerObservation(ts=ts, bid=bid, ask=round(bid + spread, 5), status="OPEN")
            )
    return out


def _business_days(cache_root: Path, count: int) -> list[str]:
    """キャッシュにある直近 N 営業日（1分足と15分足の両方があるもの）。"""
    one = {p.stem for p in (cache_root / "BID" / "1min").glob("*.json")}
    fifteen = {p.stem for p in (cache_root / "BID" / "15min").glob("*.json")}
    both = sorted(one & fifteen)
    return both[-count:]


def _session_date_for(client: CacheClient, date_str: str) -> str | None:
    """当該ファイルが属する London セッション日（レンジ窓を持つ日）。"""
    from backend.src.adapters.orb_quotes import fetch_confirmed_1m
    from backend.src.orb.constants import ORDER_TIME, RANGE_START

    bars = fetch_confirmed_1m(
        client, date_str=date_str, now=datetime.now(tz=UTC) + timedelta(days=3650)
    )
    days = {
        b.local_open.date().isoformat()
        for b in bars
        if RANGE_START <= b.local_open.time() < ORDER_TIME
    }
    return max(days) if days else None


def run_day(cache_root: Path, date_str: str) -> dict[str, Any]:
    """1 営業日を通しで駆動する。例外は捕捉して結果に載せる。"""
    from backend.src.adapters.orb_quotes import fetch_confirmed_1m
    from backend.src.batch import orb_note_batch, orb_poller, orb_session_open, orb_verify
    from backend.src.orb.constants import LON
    from backend.src.orb.note_store import load_note
    from backend.src.orb.session_store import load_session, save_session

    client = CacheClient(cache_root)
    result: dict[str, Any] = {"file_date": date_str, "errors": []}
    far_future = datetime.now(tz=UTC) + timedelta(days=3650)

    # 🔴 各日の前に halt をクリアする。本番は「1 件の不一致で以降を止める」のが正しいが、
    # リハーサルの目的は**全日の配管を見ること**。halt を持ち越すと 1 日の不一致で
    # 残り全部が SKIPPED(halted) になり、他の日のバグが隠れる。
    # halt 機構そのものは ORB-6 のテストで検証済み。
    from backend.src.orb.session_store import set_halt as _clear_halt

    _clear_halt(False, reason=f"rehearsal day {date_str}")

    try:
        session_date = _session_date_for(client, date_str)
        if session_date is None:
            result["skipped"] = "no_range_window_in_file"
            return result
        result["session_date"] = session_date
        ref = datetime.fromisoformat(f"{session_date}T09:01:00").replace(tzinfo=LON)

        # 1. session-open
        opened = orb_session_open.run_session_open(client=client, now=ref)
        result["session_open"] = opened.get("status")
        result["range_source"] = opened.get("range_source")
        result["skip_reason"] = opened.get("reason")

        # 2. poller（合成観測列で駆動）
        session = load_session(session_date)
        if session is not None and session.state == "ARMED":
            bars = [
                b
                for b in fetch_confirmed_1m(client, date_str=date_str, now=far_future)
                if b.local_open.date().isoformat() == session_date
            ]
            obs = synthesize_observations(bars)
            final, events = orb_poller.drive_session(session, obs)
            save_session(final)
            from backend.src.orb.event_log import append_events

            if events:
                append_events(session_date, events)
            result["observations"] = len(obs)
            result["events"] = len(events)
            result["final_state"] = final.state
        else:
            result["final_state"] = None if session is None else session.state

        # 3. verify（ノート生成も内包）
        verified = orb_verify.run_verify(
            session_date,
            client=client,
            now=ref.replace(hour=16, minute=30),
        )
        result["verdict"] = verified.get("verdict")
        result["verify_reason"] = verified.get("reason")

        # 4. ノートが出ているか
        note = load_note(session_date)
        result["note"] = note is not None
        if note is None:
            # verify が not_terminal 等で生成しなかった場合は明示的に試す
            gen = orb_note_batch.run_note_generation(session_date)
            result["note_retry"] = gen.get("status")
            result["note"] = load_note(session_date) is not None

    except Exception as exc:  # noqa: BLE001 - リハーサルなので握って報告する
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        result["traceback"] = traceback.format_exc(limit=4)
    return result


def run_rehearsal(cache_root: Path, *, days: int, persist_dir: Path) -> dict[str, Any]:
    os.environ["LOCAL_PERSIST_DIR"] = str(persist_dir)
    import importlib

    for name in (
        "backend.src.orb.session_store",
        "backend.src.orb.event_log",
        "backend.src.orb.note_store",
        "backend.src.persistence.s3_logger",
    ):
        mod = importlib.import_module(name)
        mod.LOCAL_PERSIST_DIR = str(persist_dir)  # type: ignore[attr-defined]

    # 🔴 前回の実行で残った halt をクリアしてから始める。
    # 残っていると全日 SKIPPED(halted) → verify は defer（許容）となり、
    # **poller を 1 度も回さずに「全日 PASS」になる**（実際に再現した）。
    from backend.src.orb.session_store import set_halt

    set_halt(False, reason="rehearsal start (clean slate)")

    targets = _business_days(cache_root, days)
    per_day = [run_day(cache_root, d) for d in targets]

    # 5. 週次集計（最終日の週）
    weekly_result: dict[str, Any] = {}
    try:
        from backend.src.batch import orb_weekly
        from backend.src.orb.constants import LON

        last = next(
            (r["session_date"] for r in reversed(per_day) if r.get("session_date")), None
        )
        if last:
            anchor = datetime.fromisoformat(f"{last}T17:00:00").replace(tzinfo=LON)
            weekly_result = orb_weekly.run_weekly(now=anchor, notify=False)
    except Exception as exc:  # noqa: BLE001
        weekly_result = {"error": f"{type(exc).__name__}: {exc}"}

    return {
        "days": per_day,
        "weekly": weekly_result,
        "persist_dir": str(persist_dir),
        "requested_days": days,
        "available_days": len(targets),
    }


def _print_report(report: dict[str, Any]) -> int:
    days = [d for d in report["days"] if not d.get("skipped")]
    errors = [d for d in days if d["errors"]]
    notes = [d for d in days if d.get("note")]
    verdicts = [d.get("verdict") for d in days]
    bad_verdicts = [v for v in verdicts if v not in {"match", "defer"}]
    # 🔴 halt でスキップされた日は「配管を通した」ことにならない
    halted = [d for d in days if d.get("skip_reason") == "halted"]
    weekly = report.get("weekly", {})
    weekly_ok = bool(weekly) and not weekly.get("error") and weekly.get("status") == "ok"
    requested = report.get("requested_days", len(days))
    enough_days = report.get("available_days", len(days)) >= requested

    print("=" * 74)
    print("ORB-8 通しリハーサル レポート")
    print("=" * 74)
    for d in report["days"]:
        if d.get("skipped"):
            print(f"  {d['file_date']}  skip（{d['skipped']}）")
            continue
        mark = "NG" if d["errors"] else "ok"
        print(
            f"  [{mark}] {d.get('session_date')}  open={d.get('session_open')}"
            f"/{d.get('range_source')}  state={d.get('final_state')}"
            f"  obs={d.get('observations')}  verdict={d.get('verdict')}"
            f"  note={'yes' if d.get('note') else 'NO'}"
        )
        for e in d["errors"]:
            print(f"        !! {e}")

    print()
    weekly = report["weekly"]
    print(f"週次集計: {weekly}")
    if weekly.get("breaches"):
        print(
            "  ⚠️ 上記 breach は**合成の副作用**。fill 価格にバーの極値を使うため滑りが過大に出る。"
            "\n     リハーサルでは中止条件の値を評価しない（配管の確認が目的）。"
        )
    print()
    print("=== 受け入れ条件（設計書 §11） ===")
    ok_errors = not errors
    ok_notes = bool(days) and len(notes) == len(days)
    ok_verify = bool(days) and not bad_verdicts
    ok_not_halted = not halted
    checks = [
        (ok_days := enough_days, f"要求 {requested} 営業日ぶんのキャッシュがある"
         f"（実際 {report.get('available_days', len(days))} 日）"),
        (ok_errors, f"{len(days)} 日連続でエラーなし"),
        (ok_not_halted, f"halt でスキップされた日がない（{len(halted)} 日）"),
        (ok_notes, f"全ノート生成（{len(notes)}/{len(days)}）"),
        (ok_verify, f"一致検証全通過（verdict: {verdicts}）"),
        (weekly_ok, f"週次集計が正常終了（{weekly.get('status') or weekly.get('error')}）"),
    ]
    for ok, label in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if not ok_days:
        print("        → キャッシュが足りない。--days を減らすかキャッシュを取得する")
    if not ok_not_halted:
        print("        → halt が残っている / 途中で halt した。poller を回せていない日がある")
    print()
    print("⚠️ 本リハーサルで**検証できない**もの: 推定滑りの妥当性 / ポーリング欠測 /")
    print("   throttle / 実 API レイテンシ。観測列は 1 分足からの合成であり実際の気配ではない。")
    print("   ここで確認できるのは配管（例外なく通る・永続化・ノート・verify）のみ。")
    return 0 if all(ok for ok, _ in checks) else 1


def main() -> int:
    p = argparse.ArgumentParser(description="ORB デモの通しリハーサル（ネットワーク不要）")
    p.add_argument("--days", type=int, default=5, help="遡る営業日数")
    p.add_argument("--cache-root", type=Path, default=None)
    p.add_argument("--persist-dir", type=Path, default=None, help="既定は一時ディレクトリ")
    args = p.parse_args()

    cache_root = args.cache_root or _default_cache_root()
    if cache_root is None or not (cache_root / "BID" / "1min").is_dir():
        print(f"🔴 klines キャッシュが見つからない（{CACHE_ENV} を設定する）")
        return 2

    persist = args.persist_dir or Path(tempfile.mkdtemp(prefix="orb-rehearsal-"))
    report = run_rehearsal(cache_root, days=args.days, persist_dir=persist)
    return _print_report(report)


if __name__ == "__main__":
    raise SystemExit(main())
