"""Kill Switch Lambda ハンドラ（§1.6, §6.3）.

機能: 全建玉の一括成行決済（緊急停止）

🔴 認証: **アプリ層の認証は持たない**（Ver 3.7 / §1.6.1・#177）。認証境界は IAM
（`lambda:InvokeFunction`）であり、**本関数を HTTP 公開してはならない**。
公開すると無認証で全建玉を成行決済できる状態になる。

NOTE: CORS 系ヘルパーは HTTP 公開を前提とした名残であり、invoke 経路では使われない。
冪等: 手仕舞いバッチと同時発火しても安全。建玉なし・決済済みは no-op 正常終了。
順序: キャンセル・ファースト → closeOrder（settlePosition バッチ・手仕舞いバッチと同一、§6.2, AGENTS.md #4）
発注封じ込め: Phase 1 ではスタブ（ALLOW_LIVE_ORDERS=True かつ TRADING_PHASE∈{2,spike}
              の両方を満たすときのみ実 POST、AGENTS.md #1）。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

from backend.src.batch.cancel_first import ForexClient, run_cancel_first
from backend.src.clients.gmo_client import GmoFxClient, live_orders_allowed
from backend.src.gmo_errors import GmoApiError
from backend.src.lambda_auth import get_gmo_credentials, reset_token_cache
from backend.src.persistence import s3_logger

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# 設定（ランタイムで os.environ を読む: テスト時の monkeypatch に追従するため）
# ---------------------------------------------------------------------------

# GMO が「建玉なし」「決済済み」を示す message_code（no-op 扱い・完全一致のみ）。
# 実コードは "ERR-XXXX" 形式（付録B E04/E06 で実確認）だが、「建玉なし」系の具体コードは
# 付録Bで未確定のため空にしている（推測で追加しない、AGENTS.md）。スパイク/Phase 2 で
# 実レスポンスを採取し付録Bに記録してから追加すること。空の間はすべて fail-closed
# （失敗として 500 を返す）。旧実装の {"5008","5009","4001","4002"} は GMO 実形式と
# 不一致な推測値で、かつ例外文字列への素の部分一致だったため、注文 ID 等の数値断片と
# 偶然一致して決済失敗を偽成功として吸収するバグがあった（Issue #136）。
_NO_POSITION_ERROR_CODES: frozenset[str] = frozenset()


def _is_live_mode() -> bool:
    """実発注を行う条件を満たすか判定する（two-key interlock, AGENTS.md #1）.

    判定は gmo_client.live_orders_allowed() に委譲する。ここで独自パースすると
    資金ゲート側と判定が食い違い、緊急停止が half-live で 500 失敗する（Issue #135）。
    """
    return live_orders_allowed()


# ---------------------------------------------------------------------------
# CORS / レスポンスヘルパー
# ---------------------------------------------------------------------------


def _cors_headers(origin: str | None) -> dict[str, str]:
    allowed = os.environ.get("CORS_ALLOW_ORIGIN", "")
    if not allowed:
        return {}
    if (origin or "") == allowed:
        return {
            "Access-Control-Allow-Origin": allowed,
            "Access-Control-Allow-Headers": "Authorization,Content-Type",
            "Access-Control-Allow-Methods": "POST,OPTIONS",
        }
    return {}


def _response(
    status_code: int,
    body: Any,
    origin: str | None = None,
) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            **_cors_headers(origin),
        },
        "body": json.dumps(body, ensure_ascii=False),
    }


def _origin(event: dict[str, Any]) -> str | None:
    headers: dict[str, str] = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    return headers.get("origin")


def _handle_preflight(event: dict[str, Any]) -> dict[str, Any]:
    """CORS preflight (OPTIONS) レスポンス."""
    origin = _origin(event)
    allowed = os.environ.get("CORS_ALLOW_ORIGIN", "")
    if allowed and origin == allowed:
        return {
            "statusCode": 204,
            "headers": {
                "Access-Control-Allow-Origin": allowed,
                "Access-Control-Allow-Headers": "Authorization,Content-Type",
                "Access-Control-Allow-Methods": "POST,OPTIONS",
            },
            "body": "",
        }
    return {
        "statusCode": 403,
        "headers": {},
        "body": json.dumps({"error": "CORS origin not allowed"}),
    }


# ---------------------------------------------------------------------------
# Kill Switch 本体
# ---------------------------------------------------------------------------


def _run_kill_switch(symbol: str) -> dict[str, Any]:
    """キャンセル・ファーストシーケンスを実行する（手仕舞いバッチと同一順序、§6.2）.

    Returns:
        結果サマリ辞書（S3 ログ / Lambda レスポンス共用）。
    """
    if not _is_live_mode():
        # Phase 1 スタブ: 実 POST せずモック結果を返す（AGENTS.md #1）
        logger.info(
            "kill_switch_stub_mode",
            extra={
                "allow_live_orders": os.environ.get("ALLOW_LIVE_ORDERS", "False"),
                "trading_phase": os.environ.get("TRADING_PHASE", "1"),
                "rule": "AGENTS.md #1: two-key interlock",
                "llm_tokens": 0,
            },
        )
        return {
            "mode": "stub",
            "step1_cancelled": [],
            "step2_cancelled": [],
            "step3_closed": [],
            "market_closed": False,
            "step1_failed": False,
            "step2_failed": False,
        }

    # Secrets Manager から GMO 認証情報を取得（AGENTS.md #8、環境変数直読み禁止）
    api_key, api_secret = get_gmo_credentials()
    with GmoFxClient(api_key=api_key, api_secret=api_secret) as client:
        result = _run_cancel_first_safe(client, symbol)

    return {
        "mode": "live",
        "step1_cancelled": result.get("step1_cancelled", []),
        "step2_cancelled": result.get("step2_cancelled", []),
        "step3_closed": result.get("step3_closed", []),
        "market_closed": result.get("market_closed", False),
        "step1_failed": result.get("step1_failed", False),
        "step2_failed": result.get("step2_failed", False),
    }


def _run_cancel_first_safe(client: ForexClient, symbol: str) -> dict[str, Any]:
    """cancel_first を実行し、GMO「建玉なし」系エラーのみ no-op として吸収する（§1.6）.

    判定は GmoApiError.message_codes への完全一致のみ。判定不能な例外は fail-closed
    で再送出する（緊急停止では「成功と誤認」より「失敗として再試行/通知」が安全、
    Issue #136）。建玉ゼロの通常ケースは run_cancel_first がエラーなしで空結果を
    返すため、この吸収経路に依存しない。
    """
    try:
        result = run_cancel_first(client=client, symbol=symbol)
        return {
            "step1_cancelled": result.step1_cancelled_ids,
            "step2_cancelled": result.step2_cancelled_ids,
            "step3_closed": result.step3_closed_ids,
            "market_closed": result.market_closed,
            "step1_failed": result.step1_failed,
            "step2_failed": result.step2_failed,
        }
    except Exception as exc:
        # GMO「建玉なし」「決済済み」エラーは no-op 正常終了（§1.6 冪等性）
        if _is_no_position_error(exc):
            logger.info(
                "kill_switch_no_position_noop",
                extra={
                    "error": str(exc),
                    "rule": "§1.6 冪等性",
                    "timestamp": datetime.now(tz=UTC).isoformat(),
                    "llm_tokens": 0,
                },
            )
            return {
                "step1_cancelled": [],
                "step2_cancelled": [],
                "step3_closed": [],
                "market_closed": False,
                "step1_failed": False,
                "step2_failed": False,
            }
        raise


def _is_no_position_error(exc: BaseException) -> bool:
    """GMO の「建玉なし」「決済済み」エラーか、構造化コードの完全一致で判定する.

    True の条件（すべて満たす場合のみ・それ以外は fail-closed）:
    - 例外が GmoApiError（messages[].message_code を構造化保持）である
    - message_codes が 1 件以上ある
    - **すべての** code が _NO_POSITION_ERROR_CODES に完全一致する
      （未知コードが混在する場合は本物の失敗の可能性があるため no-op にしない）

    例外文字列への部分一致は行わない。4桁コードの素の部分一致は注文 ID・サイズ・
    タイムスタンプの数値断片（例 size:"14001"）と偶然一致し、本物の決済失敗を
    偽成功として吸収する（Issue #136）。
    """
    if not isinstance(exc, GmoApiError):
        return False
    if not exc.message_codes:
        return False
    return all(code in _NO_POSITION_ERROR_CODES for code in exc.message_codes)


# ---------------------------------------------------------------------------
# S3 ログ
# ---------------------------------------------------------------------------


def _save_kill_switch_log(payload: dict[str, Any]) -> None:
    """Kill Switch 実行ログを S3 に保存する（§1.5 永続化）."""
    try:
        s3_logger.save_kill_switch_log(payload)
    except Exception as exc:
        logger.error(
            "kill_switch_log_save_failed",
            extra={"error": str(exc), "llm_tokens": 0},
        )


# ---------------------------------------------------------------------------
# Lambda エントリポイント
# ---------------------------------------------------------------------------


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """API Gateway / Lambda Function URL のリクエストを処理する."""
    # invocation ごとにキャッシュをリセット（トークンローテーション対応、AGENTS.md #8）
    reset_token_cache()
    ts = datetime.now(tz=UTC).isoformat()

    http_method = (
        event.get("httpMethod")
        or event.get("requestContext", {}).get("http", {}).get("method", "POST")
    ).upper()

    # CORS preflight
    if http_method == "OPTIONS":
        return _handle_preflight(event)

    origin = _origin(event)

    # POST のみ受け付ける
    if http_method != "POST":
        return _response(405, {"error": "Method Not Allowed"}, origin)

    # 🔴 アプリ層の認証は持たない（Ver 3.7 / §1.6.1・#177）。
    # 認証境界は **IAM**（`lambda:InvokeFunction`）であり、本関数は HTTP 公開しない。
    # 旧実装は Secrets Manager の共有トークンを Authorization: Bearer で検証していたが、
    # 静的クライアントに長期の共有秘密を置けない以上その方式では #177 を解けないため廃止した。
    #
    # 🔴 **この関数を HTTP 公開してはならない。** 公開すると無認証で全建玉を成行決済できる。
    # Function URL / API Gateway の新規追加は CI（scripts/check_no_public_endpoints.py）で検出する。
    symbol = os.environ.get("TRADE_SYMBOL", "USD_JPY")

    logger.info(
        "kill_switch_triggered",
        extra={"symbol": symbol, "timestamp": ts, "llm_tokens": 0},
    )

    try:
        result = _run_kill_switch(symbol)
    except Exception as exc:
        logger.exception(
            "kill_switch_error",
            extra={"error": str(exc), "timestamp": ts, "llm_tokens": 0},
        )
        payload = {
            "status": "error",
            "symbol": symbol,
            "timestamp": ts,
            "error": str(exc),
            "llm_tokens": 0,
        }
        _save_kill_switch_log(payload)
        # 内部詳細（Secrets Manager / GMO エラー文）をクライアントへ露出しない
        return _response(500, {"error": "kill switch failed"}, origin)

    has_cancel_failure = bool(result.get("step1_failed") or result.get("step2_failed"))
    # 🔴 閉場は「失敗」ではないが「**何もしていない**」。"ok" で返すと
    # 緊急停止を押した人が「建玉は決済された」と誤読する。
    # #207 (c) で Step0 が公式ステータス判定になり、週末・メンテ中は必ずこの経路に入るため
    # 区別が不可欠になった（従来は参照系 GET が落ちた稀なケースのみ）。
    market_closed = bool(result.get("market_closed"))
    if has_cancel_failure:
        run_status = "partial_failure"
    elif market_closed:
        run_status = "market_closed"
    else:
        run_status = "ok"
    payload = {
        "status": run_status,
        "symbol": symbol,
        "timestamp": ts,
        "llm_tokens": 0,
        **result,
    }
    _save_kill_switch_log(payload)

    logger.info(
        "kill_switch_completed",
        extra={
            "symbol": symbol,
            "mode": result.get("mode"),
            "step1_cancelled": result.get("step1_cancelled"),
            "step2_cancelled": result.get("step2_cancelled"),
            "step3_closed": result.get("step3_closed"),
            "timestamp": ts,
            "llm_tokens": 0,
        },
    )
    return _response(500 if has_cancel_failure else 200, payload, origin)
