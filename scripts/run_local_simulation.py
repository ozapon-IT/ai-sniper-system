"""フェーズ1ローカル連続シミュレーション（§7.1）。

POST スタブ化下で N 営業日分の判断/手仕舞いバッチを連続実行し、
SimulationReport を JSON で stdout に出力する。

Usage:
    MOCK_MODE=True python scripts/run_local_simulation.py --days 7
    MOCK_MODE=True python scripts/run_local_simulation.py --days 1  # CI 用短縮版
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

# プロジェクトルートを sys.path に追加（スクリプト直実行時）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.src.adapters.economic_calendar import CalendarResult  # noqa: E402
from backend.src.batch.cancel_first import CancelFirstResult  # noqa: E402
from backend.src.llm.trade_note import ExecutionFetch  # noqa: E402
from backend.src.persistence.dynamodb_store import (  # noqa: E402
    LockAcquireOutcome,
    LockReleaseOutcome,
)
from config.market_calendar import is_market_holiday  # noqa: E402

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 週次フロー定義（夜型スケジュール Ver 3.6）
#   月: 判断 20:30 のみ（手仕舞い cron は火〜土 4:30 のため月曜 4:30 なし）
#   火〜金: 手仕舞い 4:30（前営業日分）→ 判断 20:30
#   土: 手仕舞い 4:30 週次総括（シミュレーションは営業日ループのため通常は未実行）
# ---------------------------------------------------------------------------

_WEEKDAY_NAMES = ["月", "火", "水", "木", "金", "土", "日"]


def _closeout_mode(d: date) -> str | None:
    """指定日の手仕舞いバッチモードを返す。None なら手仕舞いなし（月曜は cron 対象外）。"""
    if d.weekday() == 0:  # 月曜
        return None
    if d.weekday() == 5:  # 土曜 — 週次総括
        return "friday"
    if 1 <= d.weekday() <= 4:  # 火〜金 — 平日手仕舞い
        return "weekday"
    return None  # 日曜


# ---------------------------------------------------------------------------
# モッククライアント生成
# ---------------------------------------------------------------------------

_MOCK_CANDLE = {
    "openTime": "1751212800000",
    "open": "155.500",
    "high": "156.500",
    "low": "155.200",
    "close": "156.000",
    "volume": "9999",
}
_MOCK_CANDLES_15 = [_MOCK_CANDLE] * 15
# G1 day-skip（0.5 pips）を避ける狭スプレッド。mid ≈ 156.4995。
_MOCK_TICKER = [{"bid": "156.499", "ask": "156.500"}]
# E04 確定形（#89/#92）: assets の data は単一 dict・equity/availableAmount。
# list や旧名 available だと _fetch_account が None を返し朝バッチが no_account_data で早期 skip する。
_MOCK_ACCOUNT = {"equity": "500000", "availableAmount": "400000"}


def _build_mock_client() -> MagicMock:
    """外部 HTTP を一切発生させないモッククライアントを返す。"""
    from backend.src.gmo_fx_endpoints import (
        PRIVATE_ACCOUNT_ASSETS,
        PRIVATE_ACTIVE_ORDERS,
        PRIVATE_OPEN_POSITIONS,
        PUBLIC_KLINES,
        PUBLIC_TICKER,
    )

    client = MagicMock()

    def _public_get(path: str, params: dict[str, Any] | None = None) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        if path == PUBLIC_KLINES:
            interval = (params or {}).get("interval", "1day")
            # date=YYYY 形式で年全体を返す。status=0 が必須（YYYYMMDD は 404）
            if interval == "1day":
                resp.json.return_value = {"status": 0, "data": _MOCK_CANDLES_15}
            else:
                resp.json.return_value = {"status": 0, "data": [_MOCK_CANDLE] * 7}
        elif path == PUBLIC_TICKER:
            resp.json.return_value = {"status": 0, "data": _MOCK_TICKER}
        else:
            # 想定外パスを status:0 で成功扱いにしない（private_get と同じ fail-fast・モックドリフト検知）。
            raise AssertionError(f"Unexpected public_get path in Phase 1 simulation: {path}")
        return resp

    def _private_get(path: str, params: dict[str, Any] | None = None) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        # GMO 実 API は成功時に必ず "status": 0 を返す。これが無いと冪等性チェック・
        # 口座取得が fail-closed で早期 skip し、発注パイプラインが一度も動かない（§7.1）。
        if path in (PRIVATE_ACTIVE_ORDERS, PRIVATE_OPEN_POSITIONS):
            resp.json.return_value = {"status": 0, "data": {"list": []}}
        elif path == PRIVATE_ACCOUNT_ASSETS:
            resp.json.return_value = {"status": 0, "data": _MOCK_ACCOUNT}
        else:
            # 想定外パスを status:0 で成功扱いするとモックドリフトを検知できない。
            # 新規/誤 private endpoint は Phase 1 検証で即失敗させる（gmo_fx_endpoints.py の定数のみ許可）。
            raise AssertionError(f"Unexpected private_get path in Phase 1 simulation: {path}")
        return resp

    client.public_get.side_effect = _public_get
    client.private_get.side_effect = _private_get
    client.__enter__ = lambda s: s
    client.__exit__ = MagicMock(return_value=False)
    return client


# ---------------------------------------------------------------------------
# モック LLM 決定
# ---------------------------------------------------------------------------

_MOCK_LLM_DECISION = {
    "direction": "LONG",
    "execution_required": True,
    "order_type": "LIMIT",
    # 現値(mid≈156.4995)近傍アンカー（§3.4 現値乖離上限 0.3×ATR14≈0.39 の内側・Ver 3.6）。
    # SL/TP は Python が ATR_h/rr_target から決定論算出する（LLM は出さない）。
    "entry_price": 156.300,
    "referenced_pivot_level": "R1",
    "sl_profile": "standard",
    "rr_target": 1.0,
    "rationale": "シミュレーション用モック判断",
    "sentiment_summary": None,
}


def _make_llm_result() -> Any:
    from backend.src.llm.client import LlmResult
    from backend.src.llm.trade_decision import TradeDecision

    decision = TradeDecision(**_MOCK_LLM_DECISION)
    result = MagicMock(spec=LlmResult)
    result.decision = decision
    result.input_tokens = 100
    result.output_tokens = 50
    result.model = "mock"
    return result


# ---------------------------------------------------------------------------
# 1日分のシミュレーション実行
# ---------------------------------------------------------------------------


def _simulate_day(
    sim_date: date,
    external_api_counter: list[int],
) -> dict[str, Any]:
    """1営業日分のバッチを実行し結果 dict を返す。"""
    from zoneinfo import ZoneInfo

    from backend.src.batch.closeout_batch import _run as closeout_run
    from backend.src.batch.decision_batch import _run as decision_run

    jst = ZoneInfo("Asia/Tokyo")
    now_decision = __import__("datetime").datetime(
        sim_date.year, sim_date.month, sim_date.day, 20, 30, 0, tzinfo=jst
    )

    patches: dict[str, Any] = {
        "backend.src.batch.decision_batch.is_market_holiday": MagicMock(return_value=False),
        "backend.src.batch.decision_batch.get_confirmed_daily_candles": MagicMock(
            return_value=_MOCK_CANDLES_15
        ),
        "backend.src.batch.decision_batch.generate_decision": MagicMock(
            return_value=_make_llm_result()
        ),
        "backend.src.batch.decision_batch.dynamodb_store.acquire_lock_outcome": MagicMock(
            return_value=LockAcquireOutcome(acquired=True)
        ),
        "backend.src.batch.decision_batch.dynamodb_store.write_order_id": MagicMock(
            return_value=True
        ),
        "backend.src.batch.decision_batch.s3_logger.save_decision_log": MagicMock(),
        "backend.src.batch.decision_batch.time.sleep": MagicMock(),
        "backend.src.batch.decision_batch._fetch_economic_events": MagicMock(
            return_value=([], CalendarResult())
        ),
        # マクロ補助入力は degrade（None）。実ネットワーク（yfinance）を踏ませない。
        "backend.src.batch.decision_batch._fetch_macro_context": MagicMock(return_value=None),
        "backend.src.batch.decision_batch.is_us_thin_market": MagicMock(return_value=False),
    }

    mock_client = _build_mock_client()

    # 想定外の外部 HTTP を遮断し即例外で止める（Phase 1 安全ガードレール）
    # モック漏れがあれば実ネットワーク送信前にカウント＋RuntimeError で即停止する。
    import requests

    def _spy_send(self: Any, *args: Any, **kwargs: Any) -> Any:
        external_api_counter[0] += 1
        req = args[0] if args else kwargs.get("request")
        method = getattr(req, "method", "UNKNOWN")
        url = getattr(req, "url", "UNKNOWN")
        raise RuntimeError(
            f"Phase 1 simulation: unexpected external HTTP request detected: {method} {url}"
        )

    # 手仕舞い（4:30 — 本番 cron より先に実行）
    closeout_result: dict[str, Any] | None = None
    mode = _closeout_mode(sim_date)
    if mode is not None:
        closeout_patches = {
            "backend.src.batch.closeout_batch.is_market_holiday": MagicMock(return_value=False),
            # 実物の CancelFirstResult を返す（MagicMock だと未設定フィールドが
            # MagicMock のまま payload に載り、S3 保存時に JSON 化できない。
            # 実データクラスなら新フィールドが増えても既定値で追随する＝モックドリフトに強い）
            "backend.src.batch.closeout_batch.run_cancel_first": MagicMock(
                return_value=CancelFirstResult()
            ),
            "backend.src.batch.closeout_batch.fetch_execution_window": MagicMock(
                return_value=ExecutionFetch()
            ),
            "backend.src.batch.closeout_batch.fetch_weekly_executions": MagicMock(
                return_value=ExecutionFetch()
            ),
            "backend.src.batch.closeout_batch.generate_trade_note": MagicMock(
                return_value=MagicMock(
                    note_markdown="シミュレーション用トレードノート",
                    is_complete=True,
                    input_tokens=50,
                    output_tokens=20,
                    model="mock",
                    # 🔴 明示しないと MagicMock の自動属性が truthy になり、
                    #    混入なしでも警告扱いで payload / Slack に出る（#309 と同型）。
                    cross_day_warnings=(),
                )
            ),
            "backend.src.batch.closeout_batch.s3_logger.load_decision_log": MagicMock(
                return_value=None
            ),
            "backend.src.batch.closeout_batch.s3_logger.save_closeout_log": MagicMock(
                return_value=True
            ),
            "backend.src.batch.closeout_batch.s3_logger.save_trade_note": MagicMock(
                return_value=True
            ),
            "backend.src.batch.closeout_batch.s3_logger.load_dashboard": MagicMock(
                return_value={}
            ),
            "backend.src.batch.closeout_batch.s3_logger.save_dashboard": MagicMock(
                return_value=True
            ),
            "backend.src.batch.closeout_batch.dynamodb_store.get_order_id": MagicMock(
                return_value=None
            ),
            # closeout 冪等ロック（#208/#213）: シミュレーションは日ごとに独立実行のため常に獲得成功。
            # 実装は acquire_lock_outcome を呼ぶため、bool ではなく LockAcquireOutcome を返す。
            "backend.src.batch.closeout_batch.dynamodb_store.acquire_lock_outcome": MagicMock(
                return_value=LockAcquireOutcome(acquired=True, lock_exists=False)
            ),
            "backend.src.batch.closeout_batch.dynamodb_store.release_lock_outcome": MagicMock(
                return_value=LockReleaseOutcome.RELEASED
            ),
            "backend.src.batch.closeout_batch.dynamodb_store.update_summary": MagicMock(
                return_value=True
            ),
            "backend.src.batch.closeout_batch.get_gmo_credentials": MagicMock(
                return_value=("mock_key", "mock_secret")
            ),
        }

        now_closeout = __import__("datetime").datetime(
            sim_date.year, sim_date.month, sim_date.day, 4, 35, 0, tzinfo=jst
        )
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(requests.Session, "send", _spy_send))
            stack.enter_context(
                patch("backend.src.batch.closeout_batch._get_today_jst", return_value=sim_date)
            )
            stack.enter_context(
                patch("backend.src.batch.closeout_batch._get_now_jst", return_value=now_closeout)
            )
            for k, v in closeout_patches.items():
                stack.enter_context(patch(k, v))
            closeout_result = closeout_run(mode=mode)  # type: ignore[arg-type]

    # 判断バッチ（20:30）
    decision_result: dict[str, Any] = {}
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(requests.Session, "send", _spy_send))
        stack.enter_context(
            patch("backend.src.clients.gmo_client.GmoFxClient", return_value=mock_client)
        )
        for k, v in patches.items():
            stack.enter_context(patch(k, v))
        decision_result = decision_run(
            client=mock_client,
            today=sim_date,
            now_jst=now_decision,
        )

    return {
        "date": sim_date.isoformat(),
        "weekday": _WEEKDAY_NAMES[sim_date.weekday()],
        "decision": decision_result,
        "closeout": closeout_result,
    }


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------


def _next_business_days(start: date, n: int) -> list[date]:
    """start から n 営業日（月〜金・非休場）を返す。"""
    days: list[date] = []
    d = start
    while len(days) < n:
        if d.weekday() < 5 and not is_market_holiday(d):
            days.append(d)
        d += timedelta(days=1)
    return days


def main() -> None:
    parser = argparse.ArgumentParser(description="フェーズ1ローカル連続シミュレーション（§7.1）")
    parser.add_argument("--days", type=int, default=7, help="シミュレーション営業日数（デフォルト: 7）")
    parser.add_argument(
        "--start",
        type=date.fromisoformat,
        default=None,
        help="開始日 (YYYY-MM-DD, デフォルト: 直近月曜)",
    )
    parser.add_argument("--output", type=Path, default=None, help="レポート JSON 出力パス")
    args = parser.parse_args()

    if args.days < 1:
        parser.error("--days は 1 以上を指定してください")

    # MOCK_MODE 確認（受け入れ条件）
    if os.environ.get("MOCK_MODE", "").lower() not in {"true", "1", "yes"}:
        print(
            "ERROR: MOCK_MODE=True を設定してください（例: MOCK_MODE=True python scripts/run_local_simulation.py）",
            file=sys.stderr,
        )
        sys.exit(1)

    # two-key interlock を安全側に強制上書き（setdefault では親環境が残るため強制）
    os.environ["ALLOW_LIVE_ORDERS"] = "False"
    os.environ["TRADING_PHASE"] = "1"
    # Phase 1 = MVP（100通貨固定, §4.2）。100通貨固定経路を必ず検証するため強制上書きする
    # （setdefault だと親環境の MVP_MODE=False が残り経路が検証されない）。
    os.environ["MVP_MODE"] = "True"

    # 開始日決定
    start_date = args.start
    if start_date is None:
        today = date.today()
        # 直近月曜を探す
        days_since_monday = today.weekday()
        start_date = today - timedelta(days=days_since_monday)

    sim_days = _next_business_days(start_date, args.days)

    print(
        f"=== フェーズ1シミュレーション開始: {sim_days[0]} 〜 {sim_days[-1]} ({args.days} 営業日) ===",
        file=sys.stderr,
    )

    day_results: list[dict[str, Any]] = []
    external_api_counter: list[int] = [0]  # モック違反検知用（現状 0 固定）

    for d in sim_days:
        print(f"  [{d} ({_WEEKDAY_NAMES[d.weekday()]})] 実行中...", file=sys.stderr)
        day_result = _simulate_day(d, external_api_counter)
        day_results.append(day_result)

        decision_status = day_result["decision"].get("status", "unknown")
        closeout_status = day_result["closeout"].get("status") if day_result["closeout"] else "—"
        print(
            f"    判断: {decision_status} / 手仕舞い: {closeout_status}",
            file=sys.stderr,
        )

    # 集計
    # シミュレーション対象は非休場の営業日のみ（_next_business_days が休場を除外）。
    # LLM 結果を「発注あり」で固定しているため、decision は発注スタブ（stubbed）まで到達するのが正常。
    # skipped（冪等性/口座取得の早期 return）も no_entry（意思決定/バリデーションのドリフト）も
    # このハーネスでは異常兆候なので許容しない。stubbed 以外は failed 扱いにする（§7.1・統合テストと一致）。
    failed_days = [
        r for r in day_results if r["decision"].get("status") != "stubbed"
    ]
    closeout_with_note = [
        r
        for r in day_results
        if r["closeout"] is not None
        and r["closeout"].get("status") not in {None, "market_closed"}
        and r["closeout"].get("note_generated") is True
    ]

    report: dict[str, Any] = {
        "simulation_days": args.days,
        "start_date": sim_days[0].isoformat(),
        "end_date": sim_days[-1].isoformat(),
        "successful_days": args.days - len(failed_days),
        "failed_days": len(failed_days),
        "external_api_calls": external_api_counter[0],
        "closeout_notes_generated": len(closeout_with_note),
        "days": day_results,
    }

    report_json = json.dumps(report, ensure_ascii=False, indent=2)

    if args.output:
        args.output.write_text(report_json, encoding="utf-8")
        print(f"レポート出力: {args.output}", file=sys.stderr)
    else:
        print(report_json)

    # 受け入れ条件チェック
    if report["failed_days"] != 0:
        print(
            f"ERROR: failed_days={report['failed_days']} (期待値: 0)",
            file=sys.stderr,
        )
        sys.exit(1)
    if report["external_api_calls"] != 0:
        print(
            f"ERROR: external_api_calls={report['external_api_calls']} (期待値: 0)",
            file=sys.stderr,
        )
        sys.exit(1)

    print("=== シミュレーション完了: 全受け入れ条件 OK ===", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
