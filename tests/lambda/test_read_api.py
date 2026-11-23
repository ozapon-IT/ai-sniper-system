"""Read API Lambda テスト（§1.6, §6.3）.

受け入れ条件:
  - 認証なし GET が 401 を返す
  - 正しい Bearer で dashboard データが返る
  - CORS ヘッダを返さない（HTTP 公開しない・#394）
  - レスポンス JSON が §6.3 表示項目をすべて含む
"""

from __future__ import annotations

import importlib
import json
from typing import Any
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# テスト用フィクスチャ
# ---------------------------------------------------------------------------

VALID_TOKEN = "test-secret-token-xyz"

SAMPLE_DASHBOARD = {
    "status": "executed",
    "equityJpy": 500000,
    "lastUpdated": "2026-06-28T10:00:00+09:00",
    "plan": {"direction": "LONG", "entry_price": 150.0},
    "lot": 100,
    "tradeNoteMarkdown": "## 本日の振り返り\nUSD/JPY ロング成功。",
    "logs": [{"event": "order_placed", "timestamp": "2026-06-28T07:30:00+09:00"}],
}


def _make_event(
    method: str = "GET",
    path: str = "/dashboard",
    token: str | None = VALID_TOKEN,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    event: dict[str, Any] = {
        "httpMethod": method,
        "rawPath": path,
        "headers": headers,
    }
    if query:
        event["queryStringParameters"] = query
    return event


@pytest.fixture(autouse=True)
def _reset_token_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """各テストでシークレットキャッシュをリセットし、環境変数を設定する."""
    # キャッシュは lambda_auth に移動したためそちらをリセットする
    import backend.src.lambda_auth as _auth

    _auth.reset_token_cache()
    importlib.invalidate_caches()


@pytest.fixture()
def handler() -> Any:
    """handler モジュールをインポートして返す（キャッシュリセット後）."""
    import backend.src.lambda_auth as _auth

    _auth.reset_token_cache()
    from backend.src.read_api import handler as h

    return h


# ---------------------------------------------------------------------------
# 認証テスト
# ---------------------------------------------------------------------------


class TestNoAppLayerAuth:
    """🔴 アプリ層の認証を持たないこと（Ver 3.7 / §1.6.1・#177）。

    認証境界は IAM（``lambda:InvokeFunction``）であり、本関数は HTTP 公開しない。
    公開リソースの追加は CI（``scripts/check_no_public_endpoints.py``）が落とす。
    """

    def test_no_auth_header_succeeds(self, handler: Any) -> None:
        with patch("backend.src.read_api.handler.s3_logger") as mock_s3:
            mock_s3.load_dashboard.return_value = SAMPLE_DASHBOARD
            resp = handler.lambda_handler(_make_event(token=None), None)
        assert resp["statusCode"] == 200

    def test_bogus_token_is_ignored(self, handler: Any) -> None:
        with patch("backend.src.read_api.handler.s3_logger") as mock_s3:
            mock_s3.load_dashboard.return_value = SAMPLE_DASHBOARD
            resp = handler.lambda_handler(_make_event(token="bogus"), None)
        assert resp["statusCode"] == 200

    def test_handler_does_not_reference_verify_bearer(self, handler: Any) -> None:
        """認証が「うっかり復活」していないことをソースで固定する.

        ⚠️ 補助線であり完全ではない（別名 import や lambda_auth.verify_bearer の
        直接参照はすり抜ける）。**本命の統制は CI の公開禁止**
        （scripts/check_no_public_endpoints.py）であり、これは早期検知用。
        """
        assert not hasattr(handler, "verify_bearer")


# ---------------------------------------------------------------------------
# CORS を持たないことのテスト（#394）
# ---------------------------------------------------------------------------


class TestNoCORS:
    def test_response_has_no_cors_headers(self, handler: Any) -> None:
        """HTTP 公開しないため CORS ヘッダは返さない（旧 SPA 向けの名残を撤去済み）."""
        with patch("backend.src.read_api.handler.s3_logger") as mock_s3:
            mock_s3.load_dashboard.return_value = SAMPLE_DASHBOARD
            resp = handler.lambda_handler(_make_event(), None)
        assert not any(k.lower().startswith("access-control-") for k in resp["headers"])

    def test_cors_helpers_removed(self, handler: Any) -> None:
        assert not hasattr(handler, "_cors_headers")
        assert not hasattr(handler, "_handle_preflight")
        assert not hasattr(handler, "CORS_ALLOW_ORIGIN")


# ---------------------------------------------------------------------------
# /dashboard エンドポイントテスト
# ---------------------------------------------------------------------------


class TestDashboardEndpoint:
    def test_returns_200_with_required_fields(self, handler: Any) -> None:
        """§6.3 表示項目（equity, today_plan, trade_note, sniper_logs）をすべて含む."""
        with patch("backend.src.read_api.handler.s3_logger") as mock_s3:
            mock_s3.load_dashboard.return_value = SAMPLE_DASHBOARD
            event = _make_event(path="/dashboard")
            resp = handler.lambda_handler(event, None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert "equity" in body
        assert "today_plan" in body
        assert "trade_note" in body
        assert "sniper_logs" in body
        assert body["equity"] == 500000
        assert body["sniper_logs"] == SAMPLE_DASHBOARD["logs"]
        assert "振り返り" in body["trade_note"]

    def test_returns_200_when_no_dashboard_data(self, handler: Any) -> None:
        """ダッシュボードデータが未生成の場合は null 値で 200 を返す."""
        with patch("backend.src.read_api.handler.s3_logger") as mock_s3:
            mock_s3.load_dashboard.return_value = None
            event = _make_event(path="/dashboard")
            resp = handler.lambda_handler(event, None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["equity"] is None
        assert body["sniper_logs"] == []

    def test_returns_500_on_s3_error(self, handler: Any) -> None:
        with patch("backend.src.read_api.handler.s3_logger") as mock_s3:
            mock_s3.load_dashboard.side_effect = RuntimeError("S3 error")
            event = _make_event(path="/dashboard")
            resp = handler.lambda_handler(event, None)
        assert resp["statusCode"] == 500


# ---------------------------------------------------------------------------
# /history エンドポイントテスト
# ---------------------------------------------------------------------------


class TestHistoryEndpoint:
    def _make_decision_log(self, d: str, direction: str = "LONG") -> dict[str, Any]:
        return {
            "date": d,
            "direction": direction,
            "execution_required": True,
            "status": "executed",
            "rationale": f"Test rationale for {d}",
        }

    def test_returns_summaries_list(self, handler: Any) -> None:
        with patch("backend.src.read_api.handler.s3_logger") as mock_s3:
            mock_s3.list_s3_keys.return_value = [
                "logs/decision/2026-06-27.json",
                "logs/decision/2026-06-26.json",
            ]
            mock_s3.load_log.side_effect = [
                self._make_decision_log("2026-06-27"),
                self._make_decision_log("2026-06-26", "SHORT"),
            ]
            event = _make_event(path="/history")
            resp = handler.lambda_handler(event, None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert "summaries" in body
        assert len(body["summaries"]) == 2
        assert body["summaries"][0]["date"] == "2026-06-27"

    def test_before_filter(self, handler: Any) -> None:
        with patch("backend.src.read_api.handler.s3_logger") as mock_s3:
            mock_s3.list_s3_keys.return_value = [
                "logs/decision/2026-06-28.json",
                "logs/decision/2026-06-27.json",
            ]
            mock_s3.load_log.return_value = self._make_decision_log("2026-06-27")
            event = _make_event(path="/history", query={"before": "2026-06-28"})
            resp = handler.lambda_handler(event, None)

        body = json.loads(resp["body"])
        dates = [s["date"] for s in body["summaries"]]
        assert "2026-06-28" not in dates
        assert "2026-06-27" in dates

    def test_limit_parameter(self, handler: Any) -> None:
        keys = [f"logs/decision/2026-06-{i:02d}.json" for i in range(1, 11)]
        logs = [self._make_decision_log(f"2026-06-{i:02d}") for i in range(1, 11)]
        with patch("backend.src.read_api.handler.s3_logger") as mock_s3:
            mock_s3.list_s3_keys.return_value = keys
            mock_s3.load_log.side_effect = logs
            event = _make_event(path="/history", query={"limit": "3"})
            resp = handler.lambda_handler(event, None)

        body = json.loads(resp["body"])
        assert len(body["summaries"]) == 3

    def test_invalid_limit_returns_400(self, handler: Any) -> None:
        event = _make_event(path="/history", query={"limit": "abc"})
        resp = handler.lambda_handler(event, None)
        assert resp["statusCode"] == 400

    def test_invalid_before_returns_400(self, handler: Any) -> None:
        event = _make_event(path="/history", query={"before": "not-a-date"})
        resp = handler.lambda_handler(event, None)
        assert resp["statusCode"] == 400


# ---------------------------------------------------------------------------
# /day/{YYYY-MM-DD} エンドポイントテスト
# ---------------------------------------------------------------------------


class TestDayEndpoint:
    DECISION_LOG = {
        "date": "2026-06-27",
        "direction": "LONG",
        "status": "executed",
        "equity_jpy": 500000,
        "lot": 100,
        "plan": {"entry_price": 150.0},
    }

    def test_returns_day_details(self, handler: Any) -> None:
        with patch("backend.src.read_api.handler.s3_logger") as mock_s3:
            mock_s3.load_decision_log.return_value = self.DECISION_LOG
            mock_s3.load_closeout_log.return_value = None
            mock_s3.load_log.return_value = None
            mock_s3.s3_key_trade_note.return_value = "notes/2026-06-27.json"
            event = _make_event(path="/day/2026-06-27")
            resp = handler.lambda_handler(event, None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["date"] == "2026-06-27"
        assert body["direction"] == "LONG"
        assert body["equity"] == 500000

    def test_not_found_returns_404(self, handler: Any) -> None:
        with patch("backend.src.read_api.handler.s3_logger") as mock_s3:
            mock_s3.load_decision_log.return_value = None
            mock_s3.load_closeout_log.return_value = None
            mock_s3.load_log.return_value = None
            mock_s3.s3_key_trade_note.return_value = "notes/2099-01-01.json"
            event = _make_event(path="/day/2099-01-01")
            resp = handler.lambda_handler(event, None)
        assert resp["statusCode"] == 404

    def test_invalid_date_format_returns_400(self, handler: Any) -> None:
        event = _make_event(path="/day/not-a-date")
        resp = handler.lambda_handler(event, None)
        assert resp["statusCode"] == 400


# ---------------------------------------------------------------------------
# ルーティングテスト
# ---------------------------------------------------------------------------


class TestRouting:
    def test_unknown_path_returns_404(self, handler: Any) -> None:
        event = _make_event(path="/unknown")
        resp = handler.lambda_handler(event, None)
        assert resp["statusCode"] == 404


# ---------------------------------------------------------------------------
# limit / before バリデーション追加テスト（CodeRabbit 指摘対応）
# ---------------------------------------------------------------------------


class TestHistoryValidation:
    def test_negative_limit_returns_400(self, handler: Any) -> None:
        event = _make_event(path="/history", query={"limit": "-1"})
        resp = handler.lambda_handler(event, None)
        assert resp["statusCode"] == 400

    def test_zero_limit_returns_400(self, handler: Any) -> None:
        event = _make_event(path="/history", query={"limit": "0"})
        resp = handler.lambda_handler(event, None)
        assert resp["statusCode"] == 400

    def test_limit_capped_at_100(self, handler: Any) -> None:
        """limit=500 を渡しても返るサマリはちょうど 100 件（101件超の入力で確認）."""
        # 101 件分のキーとログを生成（月の範囲が 31 日なので年月を分けて生成）
        entries = [(2025, m, d) for m in range(1, 13) for d in range(1, 10)][:101]
        keys = [f"logs/decision/{y:04d}-{m:02d}-{d:02d}.json" for y, m, d in entries]
        logs = [
            {
                "date": f"{y:04d}-{m:02d}-{d:02d}",
                "direction": "LONG",
                "status": "executed",
                "rationale": "",
            }
            for y, m, d in entries
        ]
        with patch("backend.src.read_api.handler.s3_logger") as mock_s3:
            mock_s3.list_s3_keys.return_value = keys
            mock_s3.load_log.side_effect = logs
            event = _make_event(path="/history", query={"limit": "500"})
            resp = handler.lambda_handler(event, None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert len(body["summaries"]) == 100
        dates = [s["date"] for s in body["summaries"]]
        assert dates == sorted(dates, reverse=True)  # 降順ソート確認
        assert dates[0] == "2025-12-02"  # 最新エントリが先頭
        assert "2025-01-01" not in dates  # 最古エントリ（101番目）は除外

    def test_invalid_before_date_value_returns_400(self, handler: Any) -> None:
        """フォーマットは正しいが存在しない日付（2026-02-31）は 400."""
        event = _make_event(path="/history", query={"before": "2026-02-31"})
        resp = handler.lambda_handler(event, None)
        assert resp["statusCode"] == 400

    def test_history_500_on_list_keys_error(self, handler: Any) -> None:
        with patch("backend.src.read_api.handler.s3_logger") as mock_s3:
            mock_s3.list_s3_keys.side_effect = RuntimeError("S3 error")
            event = _make_event(path="/history")
            resp = handler.lambda_handler(event, None)
        assert resp["statusCode"] == 500

    def test_history_500_on_load_log_error(self, handler: Any) -> None:
        with patch("backend.src.read_api.handler.s3_logger") as mock_s3:
            mock_s3.list_s3_keys.return_value = ["logs/decision/2026-06-27.json"]
            mock_s3.load_log.side_effect = RuntimeError("corrupt entry")
            event = _make_event(path="/history")
            resp = handler.lambda_handler(event, None)
        assert resp["statusCode"] == 500

    def test_history_descending_sort(self, handler: Any) -> None:
        """list_s3_keys が昇順で返しても、サマリは降順に並んでいる."""
        with patch("backend.src.read_api.handler.s3_logger") as mock_s3:
            # あえて昇順に渡す
            mock_s3.list_s3_keys.return_value = [
                "logs/decision/2026-06-25.json",
                "logs/decision/2026-06-26.json",
                "logs/decision/2026-06-27.json",
            ]
            mock_s3.load_log.side_effect = [
                {"date": "2026-06-25", "direction": "LONG", "status": "executed", "rationale": ""},
                {"date": "2026-06-26", "direction": "LONG", "status": "executed", "rationale": ""},
                {"date": "2026-06-27", "direction": "LONG", "status": "executed", "rationale": ""},
            ]
            event = _make_event(path="/history")
            resp = handler.lambda_handler(event, None)
        body = json.loads(resp["body"])
        dates = [s["date"] for s in body["summaries"]]
        assert dates == sorted(dates, reverse=True)


# ---------------------------------------------------------------------------
# /day 追加テスト（CodeRabbit 指摘対応）
# ---------------------------------------------------------------------------


class TestDayValidation:
    def test_invalid_date_value_returns_400(self, handler: Any) -> None:
        """フォーマットは正しいが存在しない日付（2026-02-31）は 400."""
        event = _make_event(path="/day/2026-02-31")
        resp = handler.lambda_handler(event, None)
        assert resp["statusCode"] == 400

    def test_day_500_on_load_log_error(self, handler: Any) -> None:
        with patch("backend.src.read_api.handler.s3_logger") as mock_s3:
            mock_s3.s3_key_closeout_log.return_value = "logs/closeout/2026-06-27.json"
            mock_s3.s3_key_trade_note.return_value = "notes/2026-06-27.json"
            mock_s3.load_decision_log.side_effect = RuntimeError("corrupt")
            event = _make_event(path="/day/2026-06-27")
            resp = handler.lambda_handler(event, None)
        assert resp["statusCode"] == 500


# ---------------------------------------------------------------------------
# Lambda Function URL 互換テスト（CodeRabbit 指摘対応）
# ---------------------------------------------------------------------------


def _make_function_url_event(
    method: str = "GET",
    path: str = "/dashboard",
    token: str | None = VALID_TOKEN,
) -> dict[str, Any]:
    """Lambda Function URL 形式（requestContext.http）のイベントを生成する."""
    headers: dict[str, str] = {}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    return {
        "requestContext": {
            "http": {
                "method": method,
                "path": path,
            }
        },
        "rawPath": path,
        "headers": headers,
    }


class TestFunctionURLCompat:
    def test_function_url_shape_no_auth_succeeds(self, handler: Any) -> None:
        """Function URL 形式のイベントでも認証は要求しない（形状互換のみの検査）.

        ⚠️ 本テストは**イベント形状のパース互換**を見るものであり、Function URL を
        作ってよいという意味ではない。作成は §1.6.1 で禁止されており CI が落とす。
        """
        with patch("backend.src.read_api.handler.s3_logger") as mock_s3:
            mock_s3.load_dashboard.return_value = SAMPLE_DASHBOARD
            resp = handler.lambda_handler(_make_function_url_event(token=None), None)
        assert resp["statusCode"] == 200

    def test_function_url_valid_token_returns_200(self, handler: Any) -> None:
        with patch("backend.src.read_api.handler.s3_logger") as mock_s3:
            mock_s3.load_dashboard.return_value = SAMPLE_DASHBOARD
            event = _make_function_url_event(token=VALID_TOKEN)
            resp = handler.lambda_handler(event, None)
        assert resp["statusCode"] == 200


# ---------------------------------------------------------------------------
# Secrets Manager フォールバック & キャッシュテスト（CodeRabbit 指摘対応）
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# バッチが実際に書くキーを読むテスト（#397）
# ---------------------------------------------------------------------------


class TestHandlerReadsKeysBatchesWrite:
    """#397: handler はバッチが実際に書くキーを読む。"""

    def test_plan_built_from_decision_log_keys(self, handler: Any) -> None:
        plan = handler._plan_from_decision(
            {
                "direction": "LONG",
                "execution_required": True,
                "entry_price": 1.5,
                "stop_loss": 1.4,
                "lot": 100,
                "rationale": "x",
                "plan": {"a": 1},
            }
        )
        # 判断ログに plan キーは無い。プラン系キーだけを抜き、無関係キーは含めない。
        assert plan == {"entry_price": 1.5, "stop_loss": 1.4, "lot": 100}

    def test_plan_none_on_no_entry_days(self, handler: Any) -> None:
        defaults = {
            "referenced_pivot_level": "NONE",
            "sl_profile": "standard",
            "atr_multiplier": 0.7,
            "rr_target": 1.0,
        }
        # LLM 見送り / LONG だが execution_required=False / エッジガード day-skip
        assert (
            handler._plan_from_decision(
                {**defaults, "direction": "NO_ENTRY", "execution_required": False}
            )
            is None
        )
        assert (
            handler._plan_from_decision(
                {**defaults, "direction": "LONG", "execution_required": False}
            )
            is None
        )
        assert (
            handler._plan_from_decision(
                {"referenced_pivot_level": "NONE", "execution_required": False}
            )
            is None
        )

    def test_plan_partial_when_order_stopped_midway(self, handler: Any) -> None:
        # validation_failed 等: entry_price / SL / TP は result にしか書かれず価格なしの plan になる
        plan = handler._plan_from_decision(
            {
                "direction": "LONG",
                "execution_required": True,
                "sl_profile": "wide",
                "rr_target": 1.2,
            }
        )
        assert plan == {"sl_profile": "wide", "rr_target": 1.2}

    def test_plan_none_when_no_plan_keys(self, handler: Any) -> None:
        assert (
            handler._plan_from_decision(
                {"status": "skipped", "reason": "market_holiday"}
            )
            is None
        )
        assert handler._plan_from_decision(None) is None

    def test_has_note_follows_notes_object(self, handler: Any) -> None:
        with patch("backend.src.read_api.handler.s3_logger") as mock_s3:
            mock_s3.s3_key_trade_note.side_effect = (
                lambda d: f"notes/{d.isoformat()}.json"
            )
            mock_s3.list_s3_keys.side_effect = lambda prefix, suffix="": (
                ["logs/decision/2099-01-06.json", "logs/decision/2099-01-05.json"]
                if prefix == "logs/decision/"
                else ["notes/2099-01-06.json"]
            )
            mock_s3.load_log.return_value = {"direction": "LONG"}
            resp = handler.lambda_handler(_make_event(path="/history"), None)
        flags = {
            s["date"]: s["has_note"] for s in json.loads(resp["body"])["summaries"]
        }
        assert flags == {"2099-01-06": True, "2099-01-05": False}


# ---------------------------------------------------------------------------
# /day の closeout 要約（#399）
# ---------------------------------------------------------------------------


class TestCloseoutSummary:
    def test_none_when_no_closeout_log(self, handler: Any) -> None:
        assert handler._closeout_summary(None) is None
        assert handler._closeout_summary({}) is None

    def test_weekday_scope_is_day(self, handler: Any) -> None:
        out = handler._closeout_summary(
            {
                "status": "ok",
                "mode": "weekday",
                "pnl": {"realized_total": "-120", "close_count": 1},
            }
        )
        assert out["scope"] == "day"
        assert out["pnl"]["realized_total"] == "-120"  # Decimal は文字列のまま

    def test_friday_scope_is_week(self, handler: Any) -> None:
        out = handler._closeout_summary(
            {"status": "ok", "mode": "friday", "pnl": {"realized_total": "310"}}
        )
        assert out["scope"] == "week"

    def test_unknown_mode_has_no_scope(self, handler: Any) -> None:
        out = handler._closeout_summary(
            {"status": "ok", "mode": "weird", "pnl": {"realized_total": "1"}}
        )
        assert out["mode"] is None
        assert out["scope"] is None

    def test_exit_reasons_counted_without_position_ids(self, handler: Any) -> None:
        out = handler._closeout_summary(
            {
                "exit_reasons": {
                    "111": "time_stop",
                    "222": "time_stop",
                    "333": "stop_loss",
                    "444": "new_reason",
                }
            }
        )
        assert out["exit_reasons"] == {"time_stop": 2, "stop_loss": 1, "unknown": 1}

    def test_failure_payload_has_no_pnl(self, handler: Any) -> None:
        out = handler._closeout_summary(
            {"status": "market_closed", "mode": "weekday", "lock_released": True}
        )
        assert out["pnl"] is None
        assert out["exit_reasons"] is None
        assert out["executions_count"] is None

    def test_non_string_decimal_is_dropped_not_floated(self, handler: Any) -> None:
        out = handler._closeout_summary(
            {"mode": "weekday", "pnl": {"realized_total": -120.0}}
        )
        assert out["pnl"]["realized_total"] is None
