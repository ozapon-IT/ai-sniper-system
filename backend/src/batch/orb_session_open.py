"""ロンドンORB デモ: session-open バッチ（Europe/London 9:01）。

設計書: docs/ai-dev/london-orb-demo-design.md §6.1（状態機械）・§6.2（レンジ確定）・§6.5（稼働率）

処理:
  1. `ORB_DEMO_ENABLED=false` / 休場カレンダー / `halt` フラグ → SKIPPED で早期 return
  2. 市場状態が OPEN か確認（`CLOSE` / `MAINTENANCE` は `market_closed` で SKIPPED）
  3. 1 分足 60 本（主経路）でレンジ確定。欠損時は 15 分足 4 本へフォールバック（§6.2）
  4. `open_session` で IDLE → ARMED / SKIPPED、DynamoDB + S3 へ永続化

🔴 **9:01 起動を守ること。** klines は足が開いた 4〜6 秒後から進行中の足を返し、
確定は閉じてから最大 +5.4 秒（ORB-1b #268 実測・付録B §6.1）。9:00:00 ちょうどに読むと
8:59 足の未確定値を掴む。確定足フィルタ（`fetch_confirmed_*`）も併用する。

本バッチは発注系 POST を一切送らない（Public API のみ・設計書 §3.2）。
"""

from __future__ import annotations

import logging
import os
import time as _time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Any

from backend.src.adapters.orb_quotes import (
    MARKET_OPEN,
    fetch_confirmed_1m,
    fetch_confirmed_15m,
    fetch_market_status,
    klines_file_date,
    london_now,
    same_london_day,
    same_london_day_15,
)
from backend.src.clients.gmo_client import GmoFxClient
from backend.src.gmo_errors import GmoApiError
from backend.src.logging_config import configure_logging
from backend.src.orb.broker import open_session
from backend.src.orb.broker_models import BrokerEvent, BrokerSession, SkipReason
from backend.src.orb.constants import JST, LON
from backend.src.orb.event_log import append_events, notify_slack_for_events
from backend.src.orb.models import RangeLevels
from backend.src.orb.range import range_from_1m, range_from_15m
from backend.src.orb.session_store import get_halt, save_session

# 🔴 **モジュールトップで呼ぶ**（#316）。`lambda_handler` の中で `logging.basicConfig` を
# 呼んでも **Lambda では no-op**（起動時に root へハンドラが入っているため）で、
# ORB のログは稼働開始から CloudWatch に 1 行も出ていなかった。
configure_logging()

logger = logging.getLogger(__name__)

RANGE_RETRY_MAX = 3
RANGE_RETRY_SLEEP_SEC = 60.0


def is_orb_demo_enabled() -> bool:
    """アプリ側マスタースイッチ（設計書 §10）。

    Terraform は Lambda env に必ず ``true`` / ``false`` を渡す。未設定はローカル
    （テスト・リハーサル）向けに実行する。明示 ``false`` だけが停止。
    """
    raw = os.environ.get("ORB_DEMO_ENABLED")
    if raw is None:
        return True
    return raw.strip().lower() == "true"


def _is_market_holiday(session_date: str) -> bool:
    """FX 実質休場日か（`config/market_holidays.json` の `full_close`）。"""
    try:
        from config.market_calendar import is_market_holiday
    except ImportError:  # pragma: no cover - 設定が無い環境
        return False
    try:
        return bool(is_market_holiday(datetime.fromisoformat(session_date).date()))
    except Exception:  # noqa: BLE001 - カレンダー不備で本番を止めない
        logger.warning("market holiday check failed for %s", session_date)
        return False


def resolve_range(
    client: GmoFxClient,
    *,
    session_date: str,
    now: datetime,
) -> tuple[RangeLevels | None, str]:
    """レンジを確定する。1 分足 60 本が主・15 分足 4 本がフォールバック（§6.2）。

    Returns:
        ``(levels, source)``。``source`` は ``"1m"`` / ``"15m"`` / ``"none"``。
    """
    date_str = klines_file_date(now.astimezone(JST))

    minutes = same_london_day(
        fetch_confirmed_1m(client, date_str=date_str, now=now), session_date
    )
    levels = range_from_1m(minutes)
    if levels is not None:
        return levels, "1m"

    # 1 分足欠損。718 営業日で 1 件（2025-06-20）の既知ギャップあり（付録B §2.2 訂正）。
    logger.warning(
        "orb session-open: 1m range unavailable (bars=%d) — falling back to 15m", len(minutes)
    )
    bars15 = same_london_day_15(
        fetch_confirmed_15m(client, date_str=date_str, now=now), session_date
    )
    levels = range_from_15m(bars15)
    if levels is not None:
        return levels, "15m"
    return None, "none"


def run_session_open(
    *,
    client: GmoFxClient | None = None,
    now: datetime | None = None,
    retry_max: int = RANGE_RETRY_MAX,
    retry_sleep_sec: float = RANGE_RETRY_SLEEP_SEC,
    sleep: Callable[[float], None] = _time.sleep,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """session-open を実行し、結果サマリを返す。

    Args:
        client: 省略時は Public 専用クライアントを内部生成する。
        now: セッション日・イベント時刻の基準。省略時は現在時刻（Europe/London）。
        retry_max / retry_sleep_sec: レンジ取得のリトライ（§6.2）。
        sleep: リトライ待機。テストで差し替える。
        clock: **確定足フィルタの基準時刻**を返す。リトライごとに読み直すため、
            待機中に確定した足を次の試行で拾える。省略時は `now` 固定なら `now`、
            そうでなければ実時計（`london_now`）。
    """
    ref = (now or london_now()).astimezone(LON)
    session_date = ref.date().isoformat()
    tick = clock or ((lambda: ref) if now is not None else london_now)
    owns_client = client is None
    client = client or GmoFxClient()

    try:
        return _run(
            client,
            ref=ref,
            session_date=session_date,
            retry_max=retry_max,
            retry_sleep_sec=retry_sleep_sec,
            sleep=sleep,
            clock=tick,
        )
    finally:
        if owns_client:
            client.close()


def _persist(session: BrokerSession, events: list[BrokerEvent]) -> None:
    save_session(session)
    if events:
        append_events(session.session_date, events)
        notify_slack_for_events(events)


def _skip(
    session_date: str,
    reason: SkipReason,
    ref: datetime,
    *,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    session, events = open_session(
        session_date, halt=False, levels=None, skip_reason=reason, ts=ref
    )
    if detail:
        events = [replace(e, detail={**e.detail, **detail}) for e in events]
    _persist(session, events)
    out: dict[str, Any] = {"status": "skipped", "session_date": session_date, "reason": reason}
    if detail:
        out.update(detail)
    return out


def _run(
    client: GmoFxClient,
    *,
    ref: datetime,
    session_date: str,
    retry_max: int,
    retry_sleep_sec: float,
    sleep: Callable[[float], None],
    clock: Callable[[], datetime],
) -> dict[str, Any]:
    if not is_orb_demo_enabled():
        return _skip(session_date, "disabled", ref)

    if get_halt():
        session, events = open_session(session_date, halt=True, levels=None, ts=ref)
        _persist(session, events)
        return {"status": "skipped", "session_date": session_date, "reason": "halted"}

    if _is_market_holiday(session_date):
        return _skip(session_date, "market_holiday", ref)

    try:
        status = fetch_market_status(client)
    except GmoApiError as exc:
        # 🔴 **API の業務エラーを `market_closed` に落とさない**（#327）。
        # `OUTAGE_SKIP_REASONS` は `system_outage` のみなので、`market_closed` にすると
        # 週次の機会損失（§6.5）に計上されず、その日が無通知で消える。稼働率は §1.2 の
        # 合否基準なので、ここを取り違えると安全弁が鳴るべきときに黙る。
        logger.error("orb session-open: market status API error — %s", exc)
        return _skip(session_date, "system_outage", ref, detail={"error": str(exc)})
    if status != MARKET_OPEN:
        # 休場カレンダー由来の `market_holiday` とは別物として数える（§6.5 の稼働集計）。
        # メンテ・予期せぬ閉場を「正常な休場」に混ぜない。
        logger.warning("orb session-open: market status=%s — skip", status)
        return _skip(session_date, "market_closed", ref, detail={"market_status": status})

    levels: RangeLevels | None = None
    source = "none"
    for attempt in range(1, retry_max + 1):
        # 確定足の基準は毎回読み直す（待機中に確定した足を次の試行で拾うため）
        try:
            levels, source = resolve_range(client, session_date=session_date, now=clock())
        except GmoApiError as exc:
            # 業務エラーもリトライ対象にし、尽きたら下の `system_outage` に落ちる（#327）。
            # 「足が無い」と同じ扱いだが、ログには API エラーとして残す。
            logger.warning("orb session-open: klines API error (attempt %d) — %s", attempt, exc)
            levels, source = None, "none"
        if levels is not None:
            break
        if attempt < retry_max:
            logger.warning(
                "orb session-open: range unavailable (attempt %d/%d) — retry in %.0fs",
                attempt,
                retry_max,
                retry_sleep_sec,
            )
            sleep(retry_sleep_sec)

    if levels is None:
        # §6.2: リトライ後も取れない = データ欠損/障害。稼働率に計上する。
        return _skip(session_date, "system_outage", ref)

    session, events = open_session(session_date, halt=False, levels=levels, ts=ref)
    _persist(session, events)
    return {
        "status": "armed",
        "session_date": session_date,
        "range_source": source,
        "buy_stop": levels.buy_stop,
        "sell_stop": levels.sell_stop,
        "range_high": levels.high,
        "range_low": levels.low,
    }


def lambda_handler(event: dict[str, Any], context: object = None) -> dict[str, Any]:  # noqa: ARG001
    """EventBridge Scheduler（`ScheduleExpressionTimezone: Europe/London` 9:01）から起動。"""
    return run_session_open()


if __name__ == "__main__":  # pragma: no cover
    print(run_session_open())
