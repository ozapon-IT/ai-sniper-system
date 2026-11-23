"""Read API Lambda ハンドラ（§1.6, §6.3）.

エンドポイント:
  GET /dashboard           — 当日スナップショット
  GET /history             — 過去日サマリ一覧（?limit=N&before=YYYY-MM-DD）
  GET /day/{YYYY-MM-DD}    — 指定日の詳細

🔴 認証: **アプリ層の認証は持たない**（Ver 3.7 / §1.6.1・#177）。認証境界は IAM
（`lambda:InvokeFunction`）であり、**本関数を HTTP 公開してはならない**。
呼び出しは EventBridge / 運用者の `aws lambda invoke` のみを想定する。

CORS は持たない（HTTP 公開しないため不要。旧 SPA の整理とあわせて撤去済み・#394）。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Any

from backend.src.lambda_auth import reset_token_cache
from backend.src.persistence import s3_logger

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# レスポンス構築ヘルパー
# ---------------------------------------------------------------------------


def _response(status_code: int, body: Any) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False),
    }


# ---------------------------------------------------------------------------
# エンドポイント実装
# ---------------------------------------------------------------------------


def _handle_dashboard(event: dict[str, Any]) -> dict[str, Any]:
    """GET /dashboard — 当日スナップショット."""
    try:
        data = s3_logger.load_dashboard()
    except RuntimeError as exc:
        logger.error("dashboard_load_error", extra={"error": str(exc)})
        return _response(500, {"error": "dashboard data unavailable"})

    payload: dict[str, Any] = {
        "equity": data.get("equityJpy") if data else None,
        "today_plan": data.get("plan") if data else None,
        "trade_note": data.get("tradeNoteMarkdown") if data else None,
        "sniper_logs": data.get("logs") if data else [],
        "status": data.get("status", "idle") if data else "idle",
        "last_updated": data.get("lastUpdated") if data else None,
        "lot": data.get("lot") if data else None,
    }
    return _response(200, payload)


def _handle_history(event: dict[str, Any]) -> dict[str, Any]:
    """GET /history — 過去日サマリ一覧（?limit=N&before=YYYY-MM-DD）."""
    params: dict[str, str] = event.get("queryStringParameters") or {}

    limit_raw = params.get("limit", "20")
    try:
        limit = int(limit_raw)
    except ValueError:
        return _response(400, {"error": "limit must be an integer"})
    if limit < 1:
        return _response(400, {"error": "limit must be between 1 and 100"})
    limit = min(limit, 100)

    before_raw = params.get("before", "")
    if before_raw and not _DATE_RE.match(before_raw):
        return _response(400, {"error": "before must be YYYY-MM-DD"})

    if before_raw:
        try:
            before_date: date | None = date.fromisoformat(before_raw)
        except ValueError:
            return _response(400, {"error": f"invalid date: {before_raw}"})
    else:
        before_date = None

    try:
        decision_keys = s3_logger.list_s3_keys(prefix="logs/decision/", suffix=".json")
    except Exception as exc:
        logger.error("history_list_error", extra={"error": str(exc)})
        return _response(500, {"error": "history unavailable"})

    date_keys: list[tuple[date, str]] = []
    for key in decision_keys:
        # logs/decision/YYYY-MM-DD.json
        basename = key.rsplit("/", 1)[-1].replace(".json", "")
        if not _DATE_RE.match(basename):
            continue
        d = date.fromisoformat(basename)
        if before_date and d >= before_date:
            continue
        date_keys.append((d, key))

    date_keys = sorted(date_keys, key=lambda x: x[0], reverse=True)[:limit]

    # has_note はノート実体 notes/{date}.json の存在で判定する（#397）。
    # 判断ログには trade_note_markdown / tradeNoteMarkdown は書かれない。
    try:
        note_keys = set(s3_logger.list_s3_keys(prefix="notes/", suffix=".json"))
    except Exception as exc:
        logger.error("history_notes_list_error", extra={"error": str(exc)})
        return _response(500, {"error": "history unavailable"})

    summaries: list[dict[str, Any]] = []
    for d, key in date_keys:
        try:
            raw = s3_logger.load_log(key)
        except RuntimeError as exc:
            logger.error("history_load_error", extra={"key": key, "error": str(exc)})
            return _response(500, {"error": "history unavailable"})
        if raw is None:
            continue
        summaries.append(
            {
                "date": d.isoformat(),
                "direction": raw.get("direction"),
                "execution_required": raw.get("execution_required"),
                "status": raw.get("status"),
                "rationale_summary": (raw.get("rationale") or "")[:200],
                "has_note": s3_logger.s3_key_trade_note(d) in note_keys,
            }
        )

    return _response(200, {"summaries": summaries, "count": len(summaries)})


# decision_batch が判断ログに実際に書くプラン系キー（#397）。判断ログに `plan` キーは無い。
_PLAN_KEYS = (
    "order_type",
    "entry_price",
    "take_profit",
    "stop_loss",
    "atr_h",
    "atr_multiplier",
    "sl_profile",
    "rr_target",
    "referenced_pivot_level",
    "lot",
)


def _plan_from_decision(decision: dict[str, Any] | None) -> dict[str, Any] | None:
    """判断ログから「発注しようとしたプラン」を組み立てる。

    見送り日（no_entry）の判断ログにも referenced_pivot_level="NONE" / sl_profile /
    rr_target といった既定値が入るため、`TradeDecision.should_enter` と同じ条件
    （execution_required かつ LONG/SHORT）のときだけ plan を返す。
    発注に至らず途中で止まった日は entry_price 等が無く、価格なしの部分的な内容になり得る。
    """
    if (
        not decision
        or decision.get("execution_required") is not True
        or decision.get("direction") not in ("LONG", "SHORT")
    ):
        return None
    plan = {k: decision[k] for k in _PLAN_KEYS if decision.get(k) is not None}
    return plan or None


# 手仕舞いログ（closeout_batch.py の payload）の exit_reasons が取り得る値（ExitReason）。
_EXIT_REASONS = ("time_stop", "take_profit", "stop_loss", "unknown")
# 画面に返す損益項目（Decimal を str で保存したもの。浮動小数にせず文字列のまま返す）。
_PNL_DECIMAL_KEYS = ("realized_total", "loss_gain_total", "fee_total", "settled_swap_total")


def _closeout_summary(closeout: dict[str, Any] | None) -> dict[str, Any] | None:
    """手仕舞いログから画面向けの要約を作る（#399）。

    🔴 ホワイトリスト方式。注文 ID・建玉 ID・約定明細（executions）・step*_cancelled /
    step3_closed などの ID 配列は返さない。exit_reasons は建玉 ID を落として理由ごとの件数にする。

    🔴 金曜（mode="friday"）は `executions_count` / `pnl` / `exit_reasons` の **3 項目すべてが
    週の集計**。土曜の手仕舞いが直近 5 取引日分の約定を集め（fetch_weekly_executions）、その
    約定から 3 項目を作って金曜の日付キーへ保存する（closeout_batch._save_closeout_payload）。
    そのため `scope`（"day" | "week"）がこの 3 項目の集計範囲を表す。"week" のときに金曜 1 日分
    として表示させない。
    """
    if not closeout:
        return None

    mode = closeout.get("mode")
    scope = {"weekday": "day", "friday": "week"}.get(mode) if isinstance(mode, str) else None

    pnl_raw = closeout.get("pnl")
    pnl: dict[str, Any] | None = None
    if isinstance(pnl_raw, dict):
        pnl = {
            k: (pnl_raw[k] if isinstance(pnl_raw.get(k), str) else None) for k in _PNL_DECIMAL_KEYS
        }
        close_count = pnl_raw.get("close_count")
        pnl["close_count"] = close_count if isinstance(close_count, int) else None

    reasons_raw = closeout.get("exit_reasons")
    exit_reasons: dict[str, int] | None = None
    if isinstance(reasons_raw, dict):
        exit_reasons = {}
        for reason in reasons_raw.values():
            key = reason if reason in _EXIT_REASONS else "unknown"
            exit_reasons[key] = exit_reasons.get(key, 0) + 1

    return {
        "status": closeout.get("status"),
        "mode": mode if mode in ("weekday", "friday") else None,
        "executions_fetched": closeout.get("executions_fetched"),
        "executions_count": closeout.get("executions_count"),
        "pnl": pnl,
        "scope": scope,
        "exit_reasons": exit_reasons,
        "note_complete": closeout.get("note_complete"),
    }


def _handle_day(event: dict[str, Any], day_str: str) -> dict[str, Any]:
    """GET /day/{YYYY-MM-DD} — 指定日の詳細."""
    if not _DATE_RE.match(day_str):
        return _response(400, {"error": "path must be /day/YYYY-MM-DD"})

    try:
        d = date.fromisoformat(day_str)
    except ValueError:
        return _response(400, {"error": f"invalid date: {day_str}"})

    note_key = s3_logger.s3_key_trade_note(d)

    try:
        decision = s3_logger.load_decision_log(d)
        closeout = s3_logger.load_closeout_log(d)
        note = s3_logger.load_log(note_key)
    except RuntimeError as exc:
        logger.error("day_load_error", extra={"date": day_str, "error": str(exc)})
        return _response(500, {"error": "day data unavailable"})

    if decision is None and closeout is None:
        return _response(404, {"error": f"no data for {day_str}"})

    payload: dict[str, Any] = {
        "date": day_str,
        "plan": _plan_from_decision(decision),
        "direction": decision.get("direction") if decision else None,
        "status": decision.get("status") if decision else None,
        "sniper_logs": closeout.get("logs") if closeout else (decision.get("logs") if decision else []),
        # closeout_batch の note_payload はノート本文をキー note_markdown で保存する（#397）。
        "trade_note": note.get("note_markdown") if note else None,
        "closeout": _closeout_summary(closeout),
        "equity": decision.get("equity_jpy") if decision else None,
        "lot": decision.get("lot") if decision else None,
    }
    return _response(200, payload)


# ---------------------------------------------------------------------------
# Lambda エントリポイント
# ---------------------------------------------------------------------------


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """API Gateway / Lambda Function URL のリクエストを処理する."""
    # invocation ごとにキャッシュをリセット（トークンローテーション対応、AGENTS.md #8）
    reset_token_cache()

    raw_path = event.get("rawPath") or event.get("path") or "/"

    # 🔴 アプリ層の認証は持たない（Ver 3.7 / §1.6.1・#177）。
    # 認証境界は **IAM**（`lambda:InvokeFunction`）であり、本関数は HTTP 公開しない。
    # Function URL / API Gateway の新規追加は CI で検出する
    # （scripts/check_no_public_endpoints.py）。

    # ルーティング
    if raw_path == "/dashboard" or raw_path == "/dashboard/":
        return _handle_dashboard(event)

    if raw_path == "/history" or raw_path == "/history/":
        return _handle_history(event)

    if raw_path.startswith("/day/"):
        day_match = re.match(r"^/day/(\d{4}-\d{2}-\d{2})/?$", raw_path)
        if day_match:
            return _handle_day(event, day_match.group(1))
        return _response(400, {"error": "path must be /day/YYYY-MM-DD"})

    return _response(404, {"error": f"not found: {raw_path}"})
