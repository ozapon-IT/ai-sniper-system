"""ロンドンORB デモ: セッションノート生成バッチ。

設計書: docs/ai-dev/london-orb-demo-design.md §7.1

DynamoDB のセッション状態からノートを生成し `orb-demo/notes/YYYY-MM-DD.md` へ保存する。
§3 の構成では **16:30 verify（ORB-6）から呼ばれる**想定だが、単体でも起動できるようにしておく
（当日分の再生成が冪等なため、あとから `eod_close` を渡して上書きできる）。

本バッチは市場データを取りに行かない（DynamoDB → S3 のみ）。
"""

from __future__ import annotations

import logging
from typing import Any

from backend.src.logging_config import configure_logging
from backend.src.orb.note import TERMINAL_STATES, NoteContext, render_note, stage_of
from backend.src.orb.note_store import save_note
from backend.src.orb.session_store import load_session

# 🔴 **モジュールトップで呼ぶ**（#316）。`lambda_handler` の中で `logging.basicConfig` を
# 呼んでも **Lambda では no-op**（起動時に root へハンドラが入っているため）で、
# ORB のログは稼働開始から CloudWatch に 1 行も出ていなかった。
configure_logging()

logger = logging.getLogger(__name__)


def run_note_generation(
    session_date: str,
    *,
    eod_close: float | None = None,
    note: str = "",
    allow_incomplete: bool = False,
) -> dict[str, Any]:
    """指定セッション日のノートを生成・保存する。

    Args:
        session_date: `YYYY-MM-DD`（Europe/London の暦日）。
        eod_close: London 16:00 に確定する 15 分足終値。渡すと `raw.eod_gap_pips` を埋める
            （観測点定義の差・§6.4）。ORB-6 の verify ジョブが供給する。
        note: 「備考」節に入れる自由記述。
        allow_incomplete: 終端状態でないセッション（`ARMED` / `FILLED` の途中）でも
            生成する。既定は拒否 — 保有中に生成すると `stage=entry` なのに
            `result=skip` という不整合なノートが記録に残るため。
    """
    session = load_session(session_date)
    if session is None:
        logger.warning("orb note: session not found: %s", session_date)
        return {"status": "no_session", "session_date": session_date}

    if session.state not in TERMINAL_STATES and not allow_incomplete:
        logger.info("orb note: session %s not terminal (%s) — skip", session_date, session.state)
        return {
            "status": "not_terminal",
            "session_date": session_date,
            "state": session.state,
        }

    markdown = render_note(session, NoteContext(eod_close=eod_close, note=note))
    saved = save_note(session_date, markdown)
    if not saved:
        # 永続化失敗は即時アラート（AGENTS.md 安全ガードレール #9）
        _alert(f"[ORB demo] ノート保存に失敗しました: {session_date}")
    return {
        "status": "ok" if saved else "save_failed",
        "session_date": session_date,
        "stage": stage_of(session),
        "state": session.state,
        "bytes": len(markdown.encode("utf-8")),
    }


def _alert(message: str) -> None:
    """Slack 通知（失敗しても本処理を落とさない）。"""
    try:
        from backend.src.alerting.slack_alert import send_slack_alert

        send_slack_alert(message)
    except Exception:  # noqa: BLE001
        logger.exception("orb note: slack alert failed")


def lambda_handler(event: dict[str, Any], context: object = None) -> dict[str, Any]:  # noqa: ARG001
    """`event["session_date"]` のノートを生成する。"""
    session_date = str(event.get("session_date", "")) if event else ""
    if not session_date:
        return {"status": "error", "reason": "session_date required"}
    eod = event.get("eod_close") if event else None
    return run_note_generation(
        session_date,
        eod_close=float(eod) if eod is not None else None,
        note=str(event.get("note", "")) if event else "",
    )
