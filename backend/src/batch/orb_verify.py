"""ロンドンORB デモ: 日次一致検証バッチ（Europe/London 16:30）。

設計書: docs/ai-dev/london-orb-demo-design.md §6.4（EOD 観測点差）・**§8.2 / §8.2.1**・§9

**本ジョブがデモの安全弁。** 当日を判定コア（1分足リプレイ）で再計算し、仮想ブローカーの
記録と突合する。分類不一致が **1 件でも**出たら `halt` をセットして翌セッション以降の
仮想発注を止め、Slack 強アラートと不一致レポートを残す（§9）。

不一致に**数えない**もの（§8.2.1 の三値写像・ORB-3 `signals_match_replay` が判定）:
  - `defer`: デモ `UNCERTAIN`（11:00 境界の欠測など）— リプレイ確定待ち
  - `defer(fill_before_order_window)`: リプレイ `fill_et` が London 09:00 / 09:01 バーで
    デモが `no_fill` または逆方向（注文未設置 / #307 残置 42 秒窓）。分類不一致に数えない
  - 既知乖離: リプレイが 11:00 台 fill / デモ `no_fill`、`both_filled` ↔ `ambiguous_both`

fill 価格・EOD 価格の差は**不一致ではない**（観測点定義の差）。滑り（§6.3）と
`eod_gap_pips`（§6.4）として別欄に集計する。

本バッチは Public API の GET のみを使う（発注系 POST なし・設計書 §3.2）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from backend.src.adapters.orb_quotes import (
    fetch_confirmed_1m,
    fetch_confirmed_15m,
    klines_file_date,
    london_now,
    same_london_day,
    same_london_day_15,
)
from backend.src.batch.orb_note_batch import run_note_generation
from backend.src.clients.gmo_client import GmoFxClient
from backend.src.gmo_errors import GmoApiError
from backend.src.logging_config import configure_logging
from backend.src.orb.broker import (
    demo_signal_class,
    is_fill_before_order_window_pair,
    signals_match_replay,
)
from backend.src.orb.broker_models import BrokerSession
from backend.src.orb.constants import JST, LON, PIP
from backend.src.orb.evaluate import evaluate_day
from backend.src.orb.models import DayResult, RangeLevels, Trade
from backend.src.orb.range import eod_close_price, range_from_1m, range_from_15m
from backend.src.orb.session_store import get_halt, load_session, set_halt
from backend.src.persistence.s3_logger import save_log

# 🔴 **モジュールトップで呼ぶ**（#316）。`lambda_handler` の中で `logging.basicConfig` を
# 呼んでも **Lambda では no-op**（起動時に root へハンドラが入っているため）で、
# ORB のログは稼働開始から CloudWatch に 1 行も出ていなかった。
configure_logging()

logger = logging.getLogger(__name__)

# レンジ・ストップ価格の一致判定の許容（tick = 0.001 の半分未満なら同値とみなす）
PRICE_TOLERANCE = 5e-4

# デモが意図的に（または障害で）仮想発注しなかった日。リプレイと比較しない（`defer`）。
# `no_range` は判定そのものなので**含めない**。
NO_COMPARISON_SKIP_REASONS = frozenset(
    {"halted", "market_holiday", "market_closed", "system_outage", "disabled"}
)


def s3_key_orb_verify(session_date: str) -> str:
    return f"orb-demo/verify/{session_date}.json"


def _levels_match(replay: RangeLevels | None, demo: RangeLevels | None) -> bool:
    if replay is None or demo is None:
        return replay is None and demo is None
    return all(
        abs(a - b) < PRICE_TOLERANCE
        for a, b in (
            (replay.high, demo.high),
            (replay.low, demo.low),
            (replay.buy_stop, demo.buy_stop),
            (replay.sell_stop, demo.sell_stop),
        )
    )


def _alert(message: str) -> None:
    try:
        from backend.src.alerting.slack_alert import send_slack_alert

        send_slack_alert(message)
    except Exception:  # noqa: BLE001
        logger.exception("orb verify: slack alert failed")


def run_verify(
    session_date: str | None = None,
    *,
    client: GmoFxClient | None = None,
    now: datetime | None = None,
    generate_note: bool = True,
) -> dict[str, Any]:
    """日次一致検証を実行する。

    Returns:
        `verdict` は `match` / `mismatch` / `defer` / `no_session`。
        `mismatch` のときだけ halt をセットする。
    """
    ref = (now or london_now()).astimezone(LON)
    day = session_date or ref.date().isoformat()

    demo = load_session(day)
    if demo is None:
        logger.warning("orb verify: session not found: %s", day)
        return {"status": "no_session", "session_date": day, "verdict": "no_session"}

    # 🔴 デモが**意図的に仮想発注しなかった**日は突合対象にならない。
    # リプレイは klines さえあれば通常どおりシグナルを出すため、比較すると必ず不一致になる。
    #
    #   - `halted`         : halt 中。mismatch にすると解除まで毎夕 Slack が鳴る
    #   - `market_holiday` : 休場カレンダー該当日。**GMO は薄商いで klines が残る**ため
    #                        リプレイは普通にシグナルを出す（ORB-8 のリハーサルで
    #                        2026-04-03 グッドフライデーが実際に mismatch → halt した）
    #   - `market_closed`  : API の市場状態が OPEN でない
    #   - `system_outage`  : データ取得障害。§6.5 の機会損失として既に計上済みで、
    #                        「判定の不一致」ではない
    #   - `disabled`       : `ORB_DEMO_ENABLED=false`。マスタースイッチで止めた日
    #
    # `no_range` は**判定そのもの**なので比較対象に残す（レンジ本数の食い違いは本物のバグ）。
    if demo.skip_reason in NO_COMPARISON_SKIP_REASONS:
        logger.info(
            "orb verify: %s was skipped (%s) — no comparison", day, demo.skip_reason
        )
        result = _report(
            day,
            demo,
            None,
            verdict="defer",
            reason=f"skipped_{demo.skip_reason}",
            eod_close=None,
        )
        _persist(day, result, generate_note=generate_note, eod_close=None)
        return result

    owns_client = client is None
    client = client or GmoFxClient()
    try:
        date_str = klines_file_date(ref.astimezone(JST))
        minutes = same_london_day(fetch_confirmed_1m(client, date_str=date_str, now=ref), day)
        bars15 = same_london_day_15(fetch_confirmed_15m(client, date_str=date_str, now=ref), day)
    except GmoApiError as exc:
        # 🔴 **klines の業務エラーで halt しない**（#327）。比較材料が無いのだから判定を
        # 下してはいけない。`defer` にしてレポートは残す。
        #
        # #327 で klines がエラーを送出するようになる前は、空配列 → `evaluate_day` が
        # `None` → `defer(replay_no_window)` に落ちていた。捕まえないと例外が
        # `run_verify` を突き抜けて Lambda エラーになり、**レポートもノートも残らず
        # 週次では `missing`** になる（「verify が動かなかった」ことすら見えない）。
        # 理由コードは `replay_no_window`（窓日でない）と区別する。
        logger.error("orb verify: klines API error — %s", exc)
        result = _report(
            day, demo, None, verdict="defer", reason="replay_api_error", eod_close=None
        )
        _persist(day, result, generate_note=generate_note, eod_close=None)
        return result
    finally:
        if owns_client:
            client.close()

    # レンジ経路をデモ（ORB-4 session-open）と揃える: 1分足60本が主・15分足4本がフォールバック。
    # 揃えないと「経路差」と「本物のデータ経路バグ」が区別できず range_levels_differ の意味が濁る。
    replay_levels = range_from_1m(minutes) or range_from_15m(bars15)
    replay = evaluate_day(day, bars15, minutes, range_levels=replay_levels)
    eod_close = eod_close_price(bars15)

    if replay is None:
        # 窓日でない（リプレイ側が当日を評価対象外とした）
        result = _report(
            day, demo, None, verdict="defer", reason="replay_no_window", eod_close=eod_close
        )
        _persist(day, result, generate_note=generate_note, eod_close=eod_close)
        return result

    verdict = signals_match_replay(replay, demo)
    reason = "" if verdict == "match" else "signal_class"
    # レンジ価格不一致は写像の match / 本クラスの defer より優先（§8.2.1 / #360）。
    # UNCERTAIN 等の他 defer は新たに halt させない。
    levels_differ = not _levels_match(replay.range_levels, demo.levels)
    this_class = is_fill_before_order_window_pair(replay, demo)
    range_overrides = verdict in {"match", "mismatch"} or (verdict == "defer" and this_class)
    if levels_differ and range_overrides:
        verdict = "mismatch"
        reason = "range_levels_differ"
    elif verdict == "defer" and this_class:
        reason = "fill_before_order_window"

    result = _report(day, demo, replay, verdict=verdict, reason=reason, eod_close=eod_close)

    if verdict == "mismatch":
        # **新規** の不一致でだけ通知する。既に halt 済みなら再通知しない（Slack 汚染防止）。
        already_halted = get_halt()
        set_halt(True, reason=f"verify_mismatch:{day}:{reason}")
        result["newly_halted"] = not already_halted
        if already_halted:
            logger.warning("orb verify: mismatch on %s while already halted — no re-alert", day)
        else:
            _alert(
                f"🔴 [ORB demo] 判定不一致で halt しました（{day}）\n"
                f"リプレイ: {result['replay']['signal']} / デモ: {result['demo']['signal']}\n"
                f"理由: {reason}\n"
                f"レポート: {s3_key_orb_verify(day)}\n"
                f"復旧手順: docs/runbook/orb_halt.md"
            )

    _persist(day, result, generate_note=generate_note, eod_close=eod_close)
    return result


def _replay_signal(replay: DayResult | None) -> str:
    if replay is None:
        return "no_window"
    if replay.skip_reason is not None:
        return replay.skip_reason
    if replay.trade is None:
        return "unknown"
    return "long" if replay.trade.direction == 1 else "short"


def _fill_time_contained(trade: Trade | None, demo: BrokerSession) -> bool | None:
    """デモの fill 観測時刻がリプレイの 1 分足バーに包含されるか（設計書 §8.2）。

    **halt 対象にはしない**（ポーリング解像度の問題であって分類の誤りではない）。
    記録として残し、外れ続けるなら観測間隔の見直し材料にする。
    """
    if trade is None or demo.entry_ts is None:
        return None
    try:
        bar_open = datetime.fromisoformat(trade.fill_et)
    except ValueError:
        return None
    if bar_open.tzinfo is None:
        return None
    bar_open = bar_open.astimezone(LON)
    observed = demo.entry_ts.astimezone(LON)
    return bar_open <= observed < bar_open + timedelta(minutes=1)


def _report(
    day: str,
    demo: BrokerSession,
    replay: DayResult | None,
    *,
    verdict: str,
    reason: str,
    eod_close: float | None,
) -> dict[str, Any]:
    """突合レポート（S3 保存 + 戻り値）。価格差は不一致ではなく観測点差として別欄に出す。"""
    trade = replay.trade if replay is not None else None
    price_diffs: dict[str, Any] = {}
    if trade is not None and demo.entry_price is not None:
        price_diffs["entry_diff_pips"] = round((demo.entry_price - trade.entry) / PIP, 2)
    if trade is not None and demo.exit_price is not None:
        price_diffs["exit_diff_pips"] = round((demo.exit_price - trade.exit) / PIP, 2)
    if eod_close is not None and demo.exit_reason == "eod" and demo.exit_price is not None:
        price_diffs["eod_gap_pips"] = round((demo.exit_price - eod_close) / PIP, 2)
    contained = _fill_time_contained(trade, demo)
    if contained is not None:
        price_diffs["fill_time_in_replay_bar"] = contained

    return {
        "status": "ok",
        "session_date": day,
        "verdict": verdict,
        "reason": reason,
        "halted": verdict == "mismatch",
        "replay": {
            "signal": _replay_signal(replay),
            "range_high": None if replay is None or replay.range_levels is None else replay.range_levels.high,
            "range_low": None if replay is None or replay.range_levels is None else replay.range_levels.low,
            "entry": None if trade is None else trade.entry,
            "exit": None if trade is None else trade.exit,
            "outcome": None if trade is None else trade.outcome,
            "fill_et": None if trade is None else trade.fill_et,
        },
        "demo": {
            "signal": demo_signal_class(demo),
            "state": demo.state,
            "range_high": None if demo.levels is None else demo.levels.high,
            "range_low": None if demo.levels is None else demo.levels.low,
            "entry": demo.entry_price,
            "exit": demo.exit_price,
            "exit_reason": demo.exit_reason,
        },
        # 🔴 価格差は不一致ではない（観測点定義の差・§6.3 / §6.4）
        "observation_diffs": price_diffs,
        "eod_close": eod_close,
    }


def _persist(
    day: str, result: dict[str, Any], *, generate_note: bool, eod_close: float | None
) -> None:
    save_log(result, s3_key_orb_verify(day))
    if generate_note:
        note = ""
        if result["verdict"] == "mismatch":
            note = (
                f"⚠️ 一致検証で **不一致**（{result['reason'] or 'signal_class'}）。"
                f"halt 済み。詳細は `{s3_key_orb_verify(day)}`。"
            )
        elif result["verdict"] == "defer":
            code = result.get("reason") or ""
            if code == "fill_before_order_window":
                note = (
                    "判定保留（`fill_before_order_window`・09:00/09:01 バーは"
                    "注文設置前 / #307 残置窓）。分類不一致には数えない（§8.2.1）。"
                )
            else:
                note = "判定保留（リプレイ確定待ち）。分類不一致には数えない（§8.2.1）。"
        run_note_generation(day, eod_close=eod_close, note=note)


def lambda_handler(event: dict[str, Any], context: object = None) -> dict[str, Any]:  # noqa: ARG001
    """EventBridge Scheduler（`Europe/London` 16:30）から起動。"""
    session_date = str(event.get("session_date", "")) if event else ""
    return run_verify(session_date or None)


if __name__ == "__main__":  # pragma: no cover
    print(run_verify())
