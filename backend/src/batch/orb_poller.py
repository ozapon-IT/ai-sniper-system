"""ロンドンORB デモ: poller バッチ（Europe/London 9:02 + 9:00〜16:00・5 分ごと起動）。

設計書: docs/ai-dev/london-orb-demo-design.md §3（構成）・§6.1（開場ゲート）・
        §6.3（推定滑り）・§6.4（EOD）・§6.5（稼働率）

1 起動で `run_seconds`（既定 300 = 5 分）ぶん、`interval_sec`（既定 5 秒・`ORB_POLL_INTERVAL_SEC`）
間隔で ticker を取得し、仮想ブローカーへ観測点を流す。11:00 取消・SL 監視・EOD は
状態機械側（`backend/src/orb/broker.py`）が内包する。

🔴 **スケジュールは 16:00 の回まで含めること。** EOD は「16:00 以降**最初**の観測」で確定するため
（§6.4）、15:55 起動の回だけでは 16:00 の観測を取り切れない。

🔴 **状態遷移はその場で永続化する**（#304）。`run_seconds`(300s) は起動間隔(300s)と同値で、
実行時間は 301.3s 実測。起動どうしは必ず重なるため、終了時にまとめて save すると後発の
起動が **1 周期古い状態**を読み、ARMED から 2 度目の fill を判定して滑り統計（§6.3）を汚す。
`run_seconds` を縮める案は 11:00 跨ぎの欠測を生むため採らない（#302 レビューで検証済み）。

本バッチは Public API の GET のみを使う（発注系 POST なし・設計書 §3.2）。
"""

from __future__ import annotations

import logging
import os
import time as _time
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

from backend.src.adapters.orb_quotes import (
    MARKET_OPEN,
    fetch_market_status,
    fetch_observation,
    london_now,
)
from backend.src.clients.gmo_client import GmoFxClient
from backend.src.gmo_errors import GmoApiError
from backend.src.logging_config import configure_logging
from backend.src.orb.broker import apply_observation
from backend.src.orb.broker_models import BrokerEvent, BrokerSession, TickerObservation
from backend.src.orb.constants import LON
from backend.src.orb.event_log import append_events, notify_slack_for_events
from backend.src.orb.session_store import (
    SAVE_FAILED,
    SAVE_REJECTED,
    SAVE_WRITTEN,
    load_session,
    save_session,
    save_session_checked,
)

# 🔴 **モジュールトップで呼ぶ**（#316）。ORB のログは稼働開始から CloudWatch に
# 1 行も出ていなかった。原因は 2 段ある:
#   ① `lambda_handler` の中の `logging.basicConfig` は **no-op**。Lambda は起動時に
#      root へハンドラを入れており、`basicConfig` は root にハンドラがあると何もしない。
#   ② それでも既定ハンドラ経由で出るはずだが、**plain text 形式の Python ランタイムの
#      既定ログレベルは WARN**（AWS 公式）。INFO は CloudWatch へ送られない。
#      > When your function's log format is set to plain text, the default log-level
#      > setting for Python runtimes is WARN.
#      <https://docs.aws.amazon.com/lambda/latest/dg/python-logging.html>（2026-08-18 参照）
# Terraform は `LOG_LEVEL` を渡している（`modules/orb_demo/main.tf`）が、①のせいで
# 誰も読んでいなかった。`configure_logging()` はレベルを INFO にし、Lambda の既定
# ハンドラを JSON の stdout ハンドラへ置き換える（夜型と同じ見え方になる）。
configure_logging()

logger = logging.getLogger(__name__)

DEFAULT_RUN_SECONDS = 300.0
TERMINAL_STATES = {"CLOSED", "SKIPPED", "CANCELLED", "ANOMALY", "UNCERTAIN", "IDLE"}


def _has_transition(events: Iterable[BrokerEvent]) -> bool:
    """状態遷移を伴うイベントが含まれるか。

    `market_closed`（開場ゲート）は `state_from == state_to` の記録専用イベントで、
    閉場中は 5 秒ごとに出る。これを遷移として扱うと即 save と S3 の全件
    read-modify-write が毎ポール走る（#304 レビュー Nit）。
    """
    return any(ev.state_from != ev.state_to for ev in events)


def _emit_summary(result: dict[str, Any]) -> dict[str, Any]:
    """1 起動ぶんのサマリを構造化ログに出し、そのまま返す。

    🔴 **戻り値は EventBridge の非同期呼び出しで捨てられる**（#316）。ログに残さないと
    `polls` / `errors` / `rejected_transitions` / `save_failures` をどこからも観測できず、
    §6.5 の「poller カバレッジ（期待観測数に対する実観測数）」が計測できない。

    ※ 週次の機会損失（§6.5 / §9）は `skip_reason=system_outage` などセッション記録から
    算出しており CloudWatch を見ていないので、**そちらは動いていた**。壊れていたのは
    ログ観測とカバレッジ、および #304 で入れたガードのカウンタ。
    """
    logger.info("orb_poller_summary", extra=result)
    return result


def poll_interval_sec() -> float:
    """`ORB_POLL_INTERVAL_SEC`（既定 5・下限 1・設計書 §3.3）。"""
    try:
        val = float(os.environ.get("ORB_POLL_INTERVAL_SEC", "5"))
    except ValueError:
        val = 5.0
    return max(1.0, val)


def drive_session(
    session: BrokerSession,
    observations: Iterable[TickerObservation],
) -> tuple[BrokerSession, list[BrokerEvent]]:
    """観測列でセッションを進める（純粋・**リプレイ駆動 E2E テストの入口**）。

    終端状態に達したらそこで打ち切る。
    """
    events: list[BrokerEvent] = []
    cur = session
    for obs in observations:
        if cur.state in TERMINAL_STATES:
            break
        cur, evs = apply_observation(cur, obs)
        events.extend(evs)
    return cur, events


def run_poller(
    *,
    client: GmoFxClient | None = None,
    session_date: str | None = None,
    run_seconds: float = DEFAULT_RUN_SECONDS,
    interval_sec: float | None = None,
    now: datetime | None = None,
    sleep: Callable[[float], None] = _time.sleep,
    monotonic: Callable[[], float] = _time.monotonic,
) -> dict[str, Any]:
    """1 起動ぶんポーリングし、結果サマリを返す。"""
    ref = (now or london_now()).astimezone(LON)
    day = session_date or ref.date().isoformat()
    interval = interval_sec if interval_sec is not None else poll_interval_sec()

    loaded = load_session(day)
    if loaded is None:
        logger.info("orb poller: no session for %s (session-open 未実行) — skip", day)
        return _emit_summary({"status": "no_session", "session_date": day})
    if loaded.state in TERMINAL_STATES:
        logger.info("orb poller: session %s already terminal (%s)", day, loaded.state)
        return _emit_summary({"status": "terminal", "session_date": day, "state": loaded.state})
    # `_commit` が閉包で参照するため、Optional を剥がした型で束縛し直す。
    session: BrokerSession = loaded

    owns_client = client is None
    client = client or GmoFxClient()
    events: list[BrokerEvent] = []
    # まだ S3 に確定させていないイベント。**遷移は save 成功が先**（下記 `_commit`）。
    pending: list[BrokerEvent] = []
    polls = 0
    errors = 0
    closed_obs = 0
    rejected = 0
    save_failures = 0

    def _commit() -> str:
        """未確定イベントを永続化する。戻り値は `SAVE_*`（非遷移のみなら WRITTEN 扱い）。

        🔴 **永続状態に書けた遷移だけをイベントログに積む**。書けていない fill を S3 に
        残すと、永続状態は `ARMED` のままなので次起動が別価格で再 fill し、初日に見た
        「`filled` が 2 行」がインフラ障害時に再現する（#304 レビュー指摘）。
        """
        if not pending:
            return SAVE_WRITTEN
        outcome = save_session_checked(session) if _has_transition(pending) else SAVE_WRITTEN
        if outcome == SAVE_WRITTEN:
            events.extend(pending)
            append_events(day, pending)
            notify_slack_for_events(pending)
            pending.clear()
        return outcome

    try:
        try:
            status = fetch_market_status(client)
        except GmoApiError as exc:
            # 🔴 **API の業務エラーを `market_closed` イベントとして残さない**（#327）。
            # 残すと「市場が閉まっていた」記録に化け、後から API 障害と区別できない。
            # 観測が採れなかった事実はサマリの `status` と `polls: 0` で表現する
            # （§6.5 のカバレッジがそのぶん下がるので日次確認で見える）。
            logger.error("orb poller: market status API error — %s", exc)
            return _emit_summary(
                {
                    "status": "api_error",
                    "session_date": day,
                    "state": session.state,
                    "polls": 0,
                    "errors": 1,
                    "events": 0,
                    "error": str(exc),
                }
            )
        if status != MARKET_OPEN:
            # 観測は採らないが、稼働イベントは永続化する（§6.1 開場ゲート / §6.5 稼働率）。
            logger.warning("orb poller: market status=%s — no observations taken", status)
            gate_event = BrokerEvent(
                ts=ref,
                reason="market_closed",
                state_from=session.state,
                state_to=session.state,
                detail={"status": status, "phase": "poller_gate"},
            )
            append_events(day, [gate_event])
            return _emit_summary(
                {
                    "status": "market_closed",
                    "session_date": day,
                    "state": session.state,
                    "market_status": status,
                    "events": 1,
                }
            )

        deadline = monotonic() + run_seconds
        while monotonic() < deadline:
            try:
                obs = fetch_observation(client)
            except Exception as exc:  # noqa: BLE001 - 1 回の失敗で窓を落とさない
                errors += 1
                logger.warning("orb poller: observation failed (%d): %r", errors, exc)
                sleep(interval)
                continue

            polls += 1
            if obs.status != MARKET_OPEN:
                closed_obs += 1
            session, evs = apply_observation(session, obs)
            pending.extend(evs)
            # 🔴 **遷移はその場で永続化する**（#304）。`run_seconds`(300s) は起動間隔
            # (300s) と同値で実行時間は 301.3s なので、起動どうしは必ず重なる。
            # 終了時にまとめて save していたため、後発の起動は毎回 **1 周期古い**
            # 状態を読み、ARMED から 2 度目の fill を判定していた
            # （2026-08-14 実測: 真 fill 159.185 に対し偽 fill 159.168）。
            #
            # 判定は `state_from != state_to`。`if evs:` だと閉場観測の `market_closed`
            # （同状態イベント）でも毎回 save + S3 の全件 read-modify-write が走る。
            if _has_transition(pending):
                outcome = _commit()
                if outcome == SAVE_REJECTED:
                    # 先着の別 invocation が確定済み。こちらの系列は捨て、
                    # 権威ある状態を読み直して続行する（イベントは積まない）。
                    rejected += 1
                    logger.warning(
                        "orb poller: transition rejected (%s) — reloading authoritative session",
                        ",".join(ev.reason for ev in pending),
                    )
                    pending.clear()
                    authoritative = load_session(day)
                    if authoritative is None:
                        break
                    session = authoritative
                    if session.state in TERMINAL_STATES:
                        break
                    sleep(interval)
                    continue
                if outcome == SAVE_FAILED:
                    # 永続化できていないのでイベントも積まない。次の観測で再試行する。
                    save_failures += 1
                    logger.error("orb poller: transition save failed — retrying next observation")
            if session.state in TERMINAL_STATES:
                logger.info("orb poller: session %s reached %s", day, session.state)
                break
            sleep(interval)
    finally:
        if owns_client:
            client.close()

    # 残りを確定させる。非遷移イベント（`market_closed` 等）はここでまとめて積み、
    # save が最後まで失敗した遷移はここで最後の 1 回を試す。
    if _commit() == SAVE_FAILED:
        save_failures += 1
        logger.error(
            "orb poller: transition not persisted (%s) — 次起動が再判定する",
            ",".join(ev.reason for ev in pending),
        )
    # 終端は遷移時に保存済み。ここでの save は `last_open_obs` の更新（稼働率の証跡）が目的。
    elif session.state not in TERMINAL_STATES:
        save_session(session)

    return _emit_summary(
        {
            "status": "ok",
            "session_date": day,
            "state": session.state,
            "polls": polls,
            "errors": errors,
            "closed_observations": closed_obs,
            "events": len(events),
            "rejected_transitions": rejected,
            "save_failures": save_failures,
        }
    )


def lambda_handler(event: dict[str, Any], context: object = None) -> dict[str, Any]:  # noqa: ARG001
    """EventBridge Scheduler（`ScheduleExpressionTimezone: Europe/London`）から起動。

    `event["run_seconds"]` / `event["interval_sec"]` で 1 回分の挙動を上書きできる。
    """
    run_seconds = float(event.get("run_seconds", DEFAULT_RUN_SECONDS)) if event else DEFAULT_RUN_SECONDS
    interval = event.get("interval_sec") if event else None
    return run_poller(
        run_seconds=run_seconds,
        interval_sec=float(interval) if interval is not None else None,
    )


if __name__ == "__main__":  # pragma: no cover
    print(run_poller())
