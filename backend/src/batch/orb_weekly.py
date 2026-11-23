"""ロンドンORB デモ: 週次監視バッチ（Europe/London 金曜 17:00）。

設計書: docs/ai-dev/london-orb-demo-design.md §9（中止条件）・§6.5（稼働率）・§7.1（S3）

集計は `backend/src/orb/weekly.py`（純粋）。本モジュールは読み出し・保存・Slack 送信のみ。

🔴 **halt の可視化がここにしか無い。** ORB-6 で halt 継続中の日次アラートを抑止したため、
halt が続いていることは**本サマリでしか分からない**（解除忘れの検出点）。

🔴 **損益は出さない**（§1.2）。中止条件に抵触しても **halt はしない**（判断はユーザー）。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from backend.src.adapters.orb_quotes import london_now
from backend.src.batch.orb_verify import s3_key_orb_verify
from backend.src.logging_config import configure_logging
from backend.src.orb.note_store import list_note_dates
from backend.src.orb.session_store import get_halt_detail, load_session
from backend.src.orb.weekly import WeeklySummary, build_summary, render_slack, week_days, week_key
from backend.src.persistence.s3_logger import load_log, save_log

# 🔴 **モジュールトップで呼ぶ**（#316）。`lambda_handler` の中で `logging.basicConfig` を
# 呼んでも **Lambda では no-op**（起動時に root へハンドラが入っているため）で、
# ORB のログは稼働開始から CloudWatch に 1 行も出ていなかった。
configure_logging()

logger = logging.getLogger(__name__)


def s3_key_orb_weekly(week: str) -> str:
    return f"orb-demo/weekly/{week}.json"


def _alert(message: str) -> None:
    try:
        from backend.src.alerting.slack_alert import send_slack_alert

        send_slack_alert(message)
    except Exception:  # noqa: BLE001
        logger.exception("orb weekly: slack alert failed")


def run_weekly(
    *,
    now: datetime | None = None,
    notify: bool = True,
) -> dict[str, Any]:
    """当該 ISO 週（月〜金）を集計し、S3 保存 + Slack 1 通を送る。"""
    ref = (now or london_now())
    anchor = ref.date()
    days = week_days(anchor)

    sessions = {day: _safe_load_session(day) for day in days}
    reports = {day: _safe_load_report(day) for day in days}

    summary: WeeklySummary = build_summary(
        anchor=anchor,
        sessions=sessions,
        verify_reports=reports,
        halt_detail=_safe_halt_detail(),
        now=ref,
        demo_first_day=_demo_first_day(),
    )

    payload = summary.to_jsonable()
    save_log(payload, s3_key_orb_weekly(summary.week))

    if notify:
        _alert(render_slack(summary))

    return {
        "status": "ok",
        "week": summary.week,
        "days": len(days),
        "slip_n": summary.slip_n,
        "outage_days": len(summary.outage_days),
        "breaches": summary.breaches,
        "halted": summary.halt.get("halt", False),
    }


def _demo_first_day() -> str | None:
    """デモが記録を残した最初の日。稼働開始前の `no_record` を障害に数えないため。

    ノートは verify が当日に書くので「その日デモが動いた」の証跡になる。
    ※ セッションはあるがノート生成に失敗した日が先頭にあると、その日は障害判定の
    起点にならない（過小計上）。稼働開始直後の誤発火を防ぐ方を優先する。
    """
    try:
        return min(list_note_dates(), default=None)
    except Exception:  # noqa: BLE001
        logger.warning("orb weekly: note listing failed — treating no_record as non-outage")
        return None


def _safe_load_session(day: str) -> Any:
    try:
        return load_session(day)
    except Exception:  # noqa: BLE001 - 1 日読めなくても週次を落とさない
        logger.warning("orb weekly: session load failed: %s", day)
        return None


def _safe_load_report(day: str) -> dict[str, Any] | None:
    try:
        return load_log(s3_key_orb_verify(day))
    except Exception:  # noqa: BLE001
        logger.warning("orb weekly: verify report load failed: %s", day)
        return None


def _safe_halt_detail() -> dict[str, Any]:
    try:
        return get_halt_detail()
    except Exception:  # noqa: BLE001
        logger.warning("orb weekly: halt detail unavailable")
        return {"halt": False, "reason": "", "since": None}


def lambda_handler(event: dict[str, Any], context: object = None) -> dict[str, Any]:  # noqa: ARG001
    """EventBridge Scheduler（`Europe/London` 金曜 17:00）から起動。"""
    return run_weekly()


if __name__ == "__main__":  # pragma: no cover
    print(run_weekly(notify=False))
    print()
    print(week_key(london_now().date()))
