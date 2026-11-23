"""手仕舞いバッチのユニットテスト（§6.2）."""

from __future__ import annotations

import logging
import time
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from ai_sniper.batches.__main__ import main
from backend.src.batch.cancel_first import CancelFirstResult
from backend.src.batch.closeout_batch import lambda_handler
from backend.src.gmo_errors import GmoApiError
from backend.src.llm.trade_note import Execution, ExecutionFetch, TradeNoteResult
from backend.src.persistence.dynamodb_store import (
    JOB_CLOSEOUT,
    LockAcquireOutcome,
    LockReleaseOutcome,
)

JST = ZoneInfo("Asia/Tokyo")

_SAT_CLOSEOUT_NOW = datetime(2025, 6, 28, 4, 35, 0, tzinfo=JST)
_TUE_CLOSEOUT_NOW = datetime(2025, 6, 24, 4, 35, 0, tzinfo=JST)
_NOW_JST = _SAT_CLOSEOUT_NOW
_TODAY = _NOW_JST.date()
# 手仕舞い実行日（土曜 = 2025-06-28）の前確定取引日 = 金曜 2025-06-27
_PREV_TRADE_DATE = _TODAY.replace(day=27)

_OK_RESULT = CancelFirstResult()
_CLOSED_RESULT = CancelFirstResult(market_closed=True, retry_next=True)
_STEP1_FAILED = CancelFirstResult(step1_failed=True)
_STEP2_FAILED = CancelFirstResult(step2_failed=True)

_NOTE_RESULT = TradeNoteResult(
    note_markdown="## テストノート",
    input_tokens=10,
    output_tokens=20,
    model="claude-haiku-4-5",
)

_EXECUTIONS = [
    Execution(
        execution_id="ex-1",
        order_id="ord-1",
        symbol="USD_JPY",
        side="BUY",
        price="155.000",
        size="100",
        timestamp="2025-06-27T11:30:00.000Z",
        settle_type="OPEN",
        position_id="pos-1",
        loss_gain="0",
        fee="0",
        settled_swap="0",
    )
]


def _patch_stack(
    cancel_result: CancelFirstResult = _OK_RESULT,
    executions: list[Execution] | None = None,
    note_result: TradeNoteResult = _NOTE_RESULT,
    morning_log: dict[str, Any] | None = None,
    order_id: str | None = "ord-1",
    existing_dashboard: dict[str, Any] | None = None,
) -> list[Any]:
    """夜バッチのフルスタックをまとめてパッチするパッチリストを返す。"""
    return [
        patch("backend.src.batch.closeout_batch.run_cancel_first", return_value=cancel_result),  # 0
        patch("backend.src.batch.closeout_batch.GmoFxClient"),                                   # 1
        patch("backend.src.batch.closeout_batch._get_today_jst", return_value=_TODAY),           # 2
        patch("backend.src.batch.closeout_batch._get_now_jst", return_value=_NOW_JST),           # 3
        patch("backend.src.batch.closeout_batch._resolve_prev_trade_date", return_value=_PREV_TRADE_DATE),  # 4
        patch(
            "backend.src.batch.closeout_batch.s3_logger.load_decision_log",
            return_value=morning_log,
        ),                                                                                          # 5
        patch(
            "backend.src.batch.closeout_batch.dynamodb_store.get_order_id",
            return_value=order_id,
        ),                                                                                          # 6
        patch(
            "backend.src.batch.closeout_batch.fetch_execution_window",
            return_value=ExecutionFetch(
                executions=executions if executions is not None else _EXECUTIONS
            ),
        ),                                                                                        # 7
        patch(
            "backend.src.batch.closeout_batch.fetch_weekly_executions",
            return_value=ExecutionFetch(
                executions=executions if executions is not None else _EXECUTIONS
            ),
        ),                                                                                        # 8
        patch(
            "backend.src.batch.closeout_batch.generate_trade_note",
            return_value=note_result,
        ),                                                                                          # 9
        patch(
            "backend.src.batch.closeout_batch.s3_logger.save_closeout_log",
            return_value=True,
        ),                                                                                          # 10
        patch(
            "backend.src.batch.closeout_batch.s3_logger.save_trade_note",
            return_value=True,
        ),                                                                                          # 11
        patch(
            "backend.src.batch.closeout_batch.s3_logger.load_dashboard",
            return_value=existing_dashboard,
        ),                                                                                          # 12
        patch(
            "backend.src.batch.closeout_batch.s3_logger.save_dashboard",
            return_value=True,
        ),                                                                                          # 13
        patch(
            "backend.src.batch.closeout_batch.send_slack_alert",
            return_value=True,
        ),                                                                                          # 14 — 失敗パス（_handle_closeout_failure）
        patch(
            "backend.src.batch.lock_acquire.send_slack_alert",
            return_value=True,
        ),                                                                                          # 15 — ロック障害（notify_lock_unavailable）
    ]


@pytest.fixture(autouse=True)
def _mock_gmo_credentials() -> Any:
    """全テストで get_gmo_credentials を自動モックする（Secrets Manager 不要）。"""
    with patch(
        "backend.src.batch.closeout_batch.get_gmo_credentials",
        return_value=("test_key", "test_secret"),
    ):
        yield


@pytest.fixture(autouse=True)
def mock_closeout_lock() -> Any:
    """全テストで closeout 冪等ロック（#208）を自動モックする（DynamoDB 不要）。

    既定は acquire 成功。skip 経路を試すテストは
    `mock_closeout_lock.acquire.return_value = LockAcquireOutcome(acquired=False, lock_exists=True)`
    で上書きする。
    """
    default_outcome = LockAcquireOutcome(acquired=True)
    with (
        patch(
            "backend.src.batch.closeout_batch.dynamodb_store.acquire_lock_outcome",
            return_value=default_outcome,
        ) as acquire,
        patch(
            "backend.src.batch.closeout_batch.dynamodb_store.release_lock_outcome",
            return_value=LockReleaseOutcome.RELEASED,
        ) as release,
        patch(
            "backend.src.batch.closeout_batch.dynamodb_store.update_summary",
            return_value=True,
        ) as update,
    ):
        yield SimpleNamespace(acquire=acquire, release=release, update=update)


# ---------------------------------------------------------------------------
# lambda_handler — cancel_first 結果
# ---------------------------------------------------------------------------


class TestLambdaHandlerCancelFirst:
    def test_ok_weekday(self) -> None:
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6],
            ps[7], ps[8], ps[9], ps[10], ps[11], ps[12], ps[13],
        ):
            resp = lambda_handler({"mode": "weekday"}, None)
        assert resp["status"] == "ok"
        assert resp["mode"] == "weekday"

    def test_market_closed(self) -> None:
        """weekday + market_closed は従来どおり early return（#226）。"""
        ps = _patch_stack(cancel_result=_CLOSED_RESULT)
        with ps[0], ps[1], ps[2], ps[3], ps[4], ps[10], ps[14]:
            resp = lambda_handler({"mode": "weekday"}, None)
        assert resp["status"] == "market_closed"
        assert resp["retry_next"] is True

    def test_step1_failed(self) -> None:
        ps = _patch_stack(cancel_result=_STEP1_FAILED)
        with ps[0], ps[1], ps[2], ps[3], ps[4], ps[10], ps[14]:
            resp = lambda_handler({}, None)
        assert resp["status"] == "step1_failed"

    def test_step2_failed(self) -> None:
        ps = _patch_stack(cancel_result=_STEP2_FAILED)
        with ps[0], ps[1], ps[2], ps[3], ps[4], ps[10], ps[14]:
            resp = lambda_handler({}, None)
        assert resp["status"] == "step2_failed"

    def test_default_mode_weekday_on_tuesday(self) -> None:
        """火曜 4:30 手仕舞いで event 未指定時は weekday モードになること。"""
        tue_today = _TUE_CLOSEOUT_NOW.date()
        tue_prev = date(2025, 6, 23)  # 月曜
        ps = _patch_stack()
        with (
            ps[0], ps[1],
            patch("backend.src.batch.closeout_batch._get_today_jst", return_value=tue_today),
            patch("backend.src.batch.closeout_batch._get_now_jst", return_value=_TUE_CLOSEOUT_NOW),
            patch("backend.src.batch.closeout_batch._resolve_prev_trade_date", return_value=tue_prev),
            ps[5], ps[6], ps[7], ps[8], ps[9], ps[10], ps[11], ps[12], ps[13],
        ):
            resp = lambda_handler({}, None)
        assert resp["mode"] == "weekday"

    def test_auto_friday_mode_on_saturday(self) -> None:
        """土曜 4:30 手仕舞いで event 未指定時は friday モード（週次総括）になること。"""
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6],
            ps[7] as mock_window,
            ps[8] as mock_weekly,
            ps[9], ps[10], ps[11], ps[12], ps[13],
        ):
            resp = lambda_handler({}, None)
        assert resp["mode"] == "friday"
        mock_weekly.assert_called_once()
        mock_window.assert_not_called()

    def test_invalid_event_mode_rejected(self) -> None:
        """#210 L-6: 手動 invoke の不正 mode は weekday に化けず error で返す。"""
        with patch("backend.src.batch.closeout_batch._run") as mock_run:
            resp = lambda_handler({"mode": "fridayy"}, None)
        mock_run.assert_not_called()
        assert resp["status"] == "error"
        assert resp["reason"] == "invalid_mode"
        assert "fridayy" in resp["detail"]

    def test_friday_market_closed_generates_weekly_note_only(
        self, mock_closeout_lock: Any
    ) -> None:
        """#226: friday + market_closed + 休場日は cancel スキップし週次ノートのみ生成する。"""
        ps = _patch_stack(cancel_result=_CLOSED_RESULT)
        with (
            ps[0] as mock_cancel,
            ps[1], ps[2], ps[3], ps[4], ps[5], ps[6],
            ps[7] as mock_window,
            ps[8] as mock_weekly,
            ps[9] as mock_note,
            ps[10] as mock_evlog, ps[11], ps[12], ps[13],
            ps[14] as mock_alert,
            patch(
                "backend.src.batch.closeout_batch.is_market_holiday",
                return_value=True,
            ) as mock_holiday,
        ):
            resp = lambda_handler({"mode": "friday"}, None)

        mock_holiday.assert_called_once_with(_PREV_TRADE_DATE)

        persisted = mock_evlog.call_args[0][0]
        assert persisted["cancel_skipped"] is True
        assert persisted["market_closed"] is True
        assert mock_evlog.call_args[1]["today"] == _PREV_TRADE_DATE

        assert resp["status"] == "ok"
        assert resp["mode"] == "friday"
        assert resp["cancel_skipped"] is True
        assert resp["market_closed"] is True
        assert resp["note_generated"] is True
        assert resp["step1_cancelled"] == []
        assert resp["step2_cancelled"] == []
        assert resp["step3_closed"] == []

        mock_cancel.assert_called_once()
        mock_weekly.assert_called_once()
        mock_window.assert_not_called()
        mock_note.assert_called_once()
        assert mock_note.call_args[0][0].is_friday_mode is True

        mock_alert.assert_called_once()
        alert_msg = mock_alert.call_args[0][0]
        assert "cancel-first をスキップ" in alert_msg
        assert "週次ノートのみ" in alert_msg

        mock_closeout_lock.release.assert_not_called()
        mock_closeout_lock.update.assert_called_once_with(
            today=_PREV_TRADE_DATE,
            summary={
                "status": "ok",
                "mode": "friday",
                "cancel_skipped": True,
                "market_closed": True,
            },
            job_type=JOB_CLOSEOUT,
        )

    def test_friday_market_closed_non_holiday_keeps_retry_path(
        self, mock_closeout_lock: Any
    ) -> None:
        """#226: friday + market_closed でも非休場（API 障害等）は failure・ロック解放。"""
        ps = _patch_stack(cancel_result=_CLOSED_RESULT)
        with (
            ps[0],
            ps[1], ps[2], ps[3], ps[4],
            ps[7] as mock_window,
            ps[8] as mock_weekly,
            ps[9] as mock_note,
            ps[10], ps[14],
            patch(
                "backend.src.batch.closeout_batch.is_market_holiday",
                return_value=False,
            ) as mock_holiday,
        ):
            resp = lambda_handler({"mode": "friday"}, None)

        mock_holiday.assert_has_calls(
            [((_PREV_TRADE_DATE,),), ((_TODAY,),)],
            any_order=False,
        )

        assert resp["status"] == "market_closed"
        assert resp["retry_next"] is True
        assert resp["lock_released"] is True
        mock_weekly.assert_not_called()
        mock_window.assert_not_called()
        mock_note.assert_not_called()
        mock_closeout_lock.release.assert_called_once_with(
            today=_PREV_TRADE_DATE, job_type=JOB_CLOSEOUT
        )
        mock_closeout_lock.update.assert_not_called()

    def test_cancelled_ids_in_response(self) -> None:
        cancel_result = CancelFirstResult(
            step1_cancelled_ids=["ord-1"],
            step2_cancelled_ids=["ord-2"],
            step3_closed_ids=["pos-1"],
        )
        ps = _patch_stack(cancel_result=cancel_result)
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6],
            ps[7], ps[8], ps[9], ps[10], ps[11], ps[12], ps[13],
        ):
            resp = lambda_handler({"mode": "weekday"}, None)
        assert resp["step1_cancelled"] == ["ord-1"]
        assert resp["step2_cancelled"] == ["ord-2"]
        assert resp["step3_closed"] == ["pos-1"]


# ---------------------------------------------------------------------------
# 失敗系 3 パスの監査ログ・強アラート（Issue #206 監査 H-1・§2 / §5.2 / §7.3）
# ---------------------------------------------------------------------------


class TestFailureAuditAndAlert:
    @pytest.mark.parametrize(
        ("cancel_result", "status"),
        [
            (_CLOSED_RESULT, "market_closed"),
            (_STEP1_FAILED, "step1_failed"),
            (_STEP2_FAILED, "step2_failed"),
        ],
    )
    def test_failure_saves_audit_log_and_alerts(
        self,
        cancel_result: CancelFirstResult,
        status: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """失敗 3 パスすべてで S3 監査ログ・Slack 強アラート・error ログが揃うこと。"""
        ps = _patch_stack(cancel_result=cancel_result)
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4],
            ps[10] as mock_evlog,
            ps[14] as mock_alert,
            caplog.at_level(logging.ERROR),
        ):
            resp = lambda_handler({"mode": "weekday"}, None)

        assert resp["status"] == status

        # S3 監査ログ: prev_trade_date キーで status/reason 入り payload が残ること（§5.2）
        mock_evlog.assert_called_once()
        payload = mock_evlog.call_args[0][0]
        assert payload["status"] == status
        assert payload["reason"]
        assert payload["date"] == _PREV_TRADE_DATE.isoformat()
        assert payload["run_date"] == _TODAY.isoformat()
        assert mock_evlog.call_args[1]["today"] == _PREV_TRADE_DATE

        # 直接 Slack 強アラートが送られること（§2）
        mock_alert.assert_called_once()
        assert status in mock_alert.call_args[0][0]
        assert "建玉が残っている可能性があります" in mock_alert.call_args[0][0]

        # error レベルの構造化ログ（#143 の { $.level = "error" } フィルタ → アラーム発火）
        error_events = [
            r
            for r in caplog.records
            if r.levelno == logging.ERROR
            and r.getMessage() == f"closeout_{status}"
        ]
        assert len(error_events) == 1

    def test_market_closed_retry_next_in_audit_log(self) -> None:
        """weekday + market_closed の監査ログと応答に retry_next が含まれること（§7.3）。"""
        ps = _patch_stack(cancel_result=_CLOSED_RESULT)
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4],
            ps[10] as mock_evlog,
            ps[14],
        ):
            resp = lambda_handler({"mode": "weekday"}, None)
        assert resp["retry_next"] is True
        assert mock_evlog.call_args[0][0]["retry_next"] is True

    def test_failure_response_intact_when_save_fails(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """S3 保存が失敗（False）しても例外にならず応答形が維持されること。"""
        ps = _patch_stack(cancel_result=_CLOSED_RESULT)
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4],
            patch(
                "backend.src.batch.closeout_batch.s3_logger.save_closeout_log",
                return_value=False,
            ),
            ps[14] as mock_alert,
            caplog.at_level(logging.ERROR),
        ):
            resp = lambda_handler({"mode": "weekday"}, None)
        assert resp["status"] == "market_closed"
        assert resp["retry_next"] is True
        mock_alert.assert_called_once()
        audit_fail = [
            r
            for r in caplog.records
            if r.getMessage() == "closeout_failure_audit_log_save_failed"
        ]
        assert len(audit_fail) == 1
        assert audit_fail[0].status == "market_closed"
        assert audit_fail[0].date == _PREV_TRADE_DATE.isoformat()


# ---------------------------------------------------------------------------
# closeout 冪等ロック（Issue #208 監査 M-2 / M-3・§6）
# ---------------------------------------------------------------------------


class TestCloseoutIdempotencyLock:
    def test_lock_acquired_with_prev_trade_date_and_closeout_job(
        self, mock_closeout_lock: Any
    ) -> None:
        """ロックは対象取引日（prev_trade_date）キー・JOB_CLOSEOUT で獲得されること。"""
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6],
            ps[7], ps[8], ps[9], ps[10], ps[11], ps[12], ps[13],
        ):
            resp = lambda_handler({"mode": "weekday"}, None)
        assert resp["status"] == "ok"
        mock_closeout_lock.acquire.assert_called_once_with(
            today=_PREV_TRADE_DATE, job_type=JOB_CLOSEOUT
        )

    def test_duplicate_delivery_skips_without_side_effects(
        self,
        mock_closeout_lock: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """M-3: 二重配信（ロック既存）で skip し、POST も S3 書き込みも一切しないこと。"""
        mock_closeout_lock.acquire.return_value = LockAcquireOutcome(
            acquired=False, lock_exists=True
        )
        ps = _patch_stack()
        with (
            ps[0] as mock_cancel,
            ps[1] as mock_client, ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8],
            ps[9] as mock_gen,
            ps[10] as mock_evlog,
            ps[11] as mock_note_save,
            ps[12],
            ps[13] as mock_dash,
            caplog.at_level(logging.WARNING),
        ):
            resp = lambda_handler({"mode": "weekday"}, None)

        assert resp["status"] == "skipped"
        assert resp["reason"] == "closeout_lock_exists"
        assert resp["prev_trade_date"] == _PREV_TRADE_DATE.isoformat()

        # キャンセル・ファースト（POST）・LLM・S3 のいずれにも到達しないこと
        mock_cancel.assert_not_called()
        mock_client.assert_not_called()
        mock_gen.assert_not_called()
        mock_evlog.assert_not_called()
        mock_note_save.assert_not_called()
        mock_dash.assert_not_called()

        # 監査ログ closeout_lock_exists が残ること
        skip_events = [
            r for r in caplog.records if r.getMessage() == "closeout_lock_exists"
        ]
        assert len(skip_events) == 1
        assert skip_events[0].prev_trade_date == _PREV_TRADE_DATE.isoformat()

    def test_lock_backend_error_fails_loudly_without_s3_write(
        self,
        mock_closeout_lock: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """#213: DDB 障害は closeout_lock_exists に潰さず失敗扱い・強アラート。

        S3 には一切書かない（処理済み取引日の確定証跡を上書きしないため M-2 完全保護）。
        closeout_daily は retry=0 のため、障害を benign skip にすると手仕舞いが止まり
        建玉が残るのに気づけない。lock_exists=False（バックエンド障害）は別扱いにする。
        """
        mock_closeout_lock.acquire.return_value = LockAcquireOutcome(
            acquired=False, lock_exists=False
        )
        ps = _patch_stack()
        with (
            ps[0] as mock_cancel,
            ps[1] as mock_client,
            ps[2], ps[3], ps[4],
            ps[10] as mock_evlog,
            ps[11] as mock_note_save,
            ps[15] as mock_slack,
            caplog.at_level(logging.ERROR),
        ):
            resp = lambda_handler({"mode": "weekday"}, None)

        # benign skip ではなく失敗扱い
        assert resp["status"] == "lock_unavailable"
        assert resp["reason"] == "lock_backend_error"
        assert resp["prev_trade_date"] == _PREV_TRADE_DATE.isoformat()

        # 人間へ強アラート・GMO 接続や S3 書き込みには到達しないこと（M-2 完全保護）
        mock_slack.assert_called_once()
        mock_client.assert_not_called()
        mock_cancel.assert_not_called()
        mock_evlog.assert_not_called()
        mock_note_save.assert_not_called()

        # error 監査ログ closeout_lock_unavailable が残ること
        err_events = [
            r for r in caplog.records if r.getMessage() == "closeout_lock_unavailable"
        ]
        assert len(err_events) == 1

    def test_holiday_reopen_does_not_overwrite_processed_trade_date(
        self, mock_closeout_lock: Any
    ) -> None:
        """M-2: 休場明け（月曜休場→火曜実行）で prev_trade_date が処理済みの金曜に
        解決されても、既存ロックにより確定済みノート・ログを上書きしないこと。"""
        mock_closeout_lock.acquire.return_value = LockAcquireOutcome(
            acquired=False, lock_exists=True
        )
        tue_today = _TUE_CLOSEOUT_NOW.date()          # 火曜 2025-06-24
        fri_prev = date(2025, 6, 20)                  # 月曜休場を遡った金曜（処理済み）
        ps = _patch_stack()
        with (
            ps[0], ps[1],
            patch("backend.src.batch.closeout_batch._get_today_jst", return_value=tue_today),
            patch("backend.src.batch.closeout_batch._get_now_jst", return_value=_TUE_CLOSEOUT_NOW),
            patch("backend.src.batch.closeout_batch._resolve_prev_trade_date", return_value=fri_prev),
            ps[5], ps[6], ps[7], ps[8], ps[9],
            ps[10] as mock_evlog,
            ps[11] as mock_note_save,
            ps[12], ps[13],
        ):
            resp = lambda_handler({}, None)

        assert resp["status"] == "skipped"
        assert resp["prev_trade_date"] == fri_prev.isoformat()
        mock_closeout_lock.acquire.assert_called_once_with(
            today=fri_prev, job_type=JOB_CLOSEOUT
        )
        # 金曜キーの確定証跡（週次総括ノート・closeout ログ）が上書きされないこと
        mock_evlog.assert_not_called()
        mock_note_save.assert_not_called()

    def test_market_closed_releases_lock(self, mock_closeout_lock: Any) -> None:
        """weekday + market_closed は未処理確実 → ロック解放して再試行余地を残すこと（#138 準拠）。"""
        ps = _patch_stack(cancel_result=_CLOSED_RESULT)
        with ps[0], ps[1], ps[2], ps[3], ps[4], ps[10], ps[14]:
            resp = lambda_handler({"mode": "weekday"}, None)
        assert resp["status"] == "market_closed"
        assert resp["lock_released"] is True
        mock_closeout_lock.release.assert_called_once_with(
            today=_PREV_TRADE_DATE, job_type=JOB_CLOSEOUT
        )

    @pytest.mark.parametrize(
        ("cancel_result", "status"),
        [
            (_STEP1_FAILED, "step1_failed"),
            (_STEP2_FAILED, "step2_failed"),
        ],
    )
    def test_step_failures_hold_lock(
        self,
        cancel_result: CancelFirstResult,
        status: str,
        mock_closeout_lock: Any,
    ) -> None:
        """step1/step2 失敗は POST 試行済み（未確定）→ ロック保持すること（fail-safe）。"""
        ps = _patch_stack(cancel_result=cancel_result)
        with ps[0], ps[1], ps[2], ps[3], ps[4], ps[10], ps[14]:
            resp = lambda_handler({}, None)
        assert resp["status"] == status
        assert resp["lock_released"] is False
        mock_closeout_lock.release.assert_not_called()

    def test_success_marks_lock_summary(self, mock_closeout_lock: Any) -> None:
        """成功時にロックレコードへ完了印（status=ok）を追記すること。"""
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6],
            ps[7], ps[8], ps[9], ps[10], ps[11], ps[12], ps[13],
        ):
            resp = lambda_handler({"mode": "weekday"}, None)
        assert resp["status"] == "ok"
        mock_closeout_lock.update.assert_called_once_with(
            today=_PREV_TRADE_DATE,
            summary={"status": "ok", "mode": "weekday"},
            job_type=JOB_CLOSEOUT,
        )
        mock_closeout_lock.release.assert_not_called()


# ---------------------------------------------------------------------------
# 取引日解決
# ---------------------------------------------------------------------------


class TestTradeDateResolution:
    def test_prev_trade_date_used_for_decision_log(self) -> None:
        """前確定取引日で load_decision_log が呼ばれること（土曜実行なら金曜のログを参照）。"""
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4],
            ps[5] as mock_load_decision,
            ps[6], ps[7], ps[8], ps[9], ps[10], ps[11], ps[12], ps[13],
        ):
            lambda_handler({"mode": "weekday"}, None)
        mock_load_decision.assert_called_once_with(_PREV_TRADE_DATE)

    def test_prev_trade_date_used_for_order_id(self) -> None:
        """前確定取引日で get_order_id が呼ばれること。"""
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5],
            ps[6] as mock_get_oid,
            ps[7], ps[8], ps[9], ps[10], ps[11], ps[12], ps[13],
        ):
            lambda_handler({"mode": "weekday"}, None)
        mock_get_oid.assert_called_once_with(today=_PREV_TRADE_DATE)

    def test_prev_trade_date_passed_to_execution_window(self) -> None:
        """weekday モードで fetch_execution_window に prev_trade_date が渡されること。"""
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6],
            ps[7] as mock_window,
            ps[8], ps[9], ps[10], ps[11], ps[12], ps[13],
        ):
            lambda_handler({"mode": "weekday"}, None)
        call_kwargs = mock_window.call_args[1]
        assert call_kwargs["prev_trade_date"] == _PREV_TRADE_DATE


# ---------------------------------------------------------------------------
# E14: generate_trade_note 呼び出し確認
# ---------------------------------------------------------------------------


class TestNoteGeneration:
    def test_generate_trade_note_called_on_weekday(self) -> None:
        """weekday モードで generate_trade_note が呼ばれること（§6.2 E14 結線）。"""
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8],
            ps[9] as mock_gen,
            ps[10], ps[11], ps[12], ps[13],
        ):
            lambda_handler({"mode": "weekday"}, None)
        mock_gen.assert_called_once()

    def test_generate_trade_note_called_on_friday(self) -> None:
        """friday モードで generate_trade_note が呼ばれること（週次モード §6.2）。"""
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8],
            ps[9] as mock_gen,
            ps[10], ps[11], ps[12], ps[13],
        ):
            lambda_handler({"mode": "friday"}, None)
        mock_gen.assert_called_once()
        call_args = mock_gen.call_args[0][0]
        assert call_args.is_friday_mode is True

    def test_incomplete_note_alerts_on_slack(self) -> None:
        """打ち切られたノートを Slack で気付けること（#309）。

        S3 の note_complete: false は誰も見ないため、通知が唯一の検知手段になる。
        """
        incomplete = TradeNoteResult(
            note_markdown="## 途中で切れたノート",
            input_tokens=2570,
            output_tokens=4096,
            model="claude-haiku-4-5",
            is_complete=False,
        )
        ps = _patch_stack(note_result=incomplete)
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8],
            ps[9], ps[10], ps[11], ps[12], ps[13],
            ps[14] as mock_slack,
            ps[15],
        ):
            lambda_handler({"mode": "friday"}, None)

        messages = [c.args[0] for c in mock_slack.call_args_list]
        assert any("トレードノートが不完全" in m for m in messages), messages

    def test_cross_day_warning_alerts_and_is_persisted(self) -> None:
        """他日の計画値の混入を Slack と closeout ログの両方に残すこと（#334）。

        ノートは §6.2 の改善サイクルの入力なので、誤った計画値のまま読まれると
        振り返りの前提が狂う。監査は closeout ログを見るため証跡側にも残す（§5.2）。
        """
        contaminated = TradeNoteResult(
            note_markdown="## 週次総括\n| 2026-08-24 | SL=159.065 |",
            input_tokens=2570,
            output_tokens=1200,
            model="claude-haiku-4-5",
            is_complete=True,
            cross_day_warnings=(
                "L5: 2026-08-24 の記述に 159.065 が出ているが、"
                "この値は 2026-08-27 の計画値であり 2026-08-24 のものではない",
            ),
        )
        ps = _patch_stack(note_result=contaminated)
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9],
            ps[10] as mock_save_log,
            ps[11], ps[12], ps[13],
            ps[14] as mock_slack,
            ps[15],
        ):
            lambda_handler({"mode": "friday"}, None)

        messages = [c.args[0] for c in mock_slack.call_args_list]
        assert any("他日の計画値が混入した疑い" in m for m in messages), messages
        assert any("159.065" in m for m in messages), messages

        payload = mock_save_log.call_args_list[-1].args[0]
        assert payload["note_cross_day_warnings"], payload
        # 🔴 生成は正常終了しているので「不完全」とは別扱い
        assert payload["note_complete"] is True

    def test_discipline_warning_alerts_and_is_persisted(self) -> None:
        """規律違反フレーズを Slack と closeout ログの両方に残すこと（#342）。

        ノートは §6.2 の改善サイクルの入力なので、示唆に引きずられて
        パラメータを変えるリスクがある。監査は closeout ログを見る。
        """
        flagged = TradeNoteResult(
            note_markdown="## 自己評価\n今後の検証対象とすること。",
            input_tokens=400,
            output_tokens=200,
            model="claude-haiku-4-5",
            is_complete=True,
            discipline_warnings=("L2: 検証対象にする提案（「検証対象」）",),
        )
        ps = _patch_stack(note_result=flagged)
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9],
            ps[10] as mock_save_log,
            ps[11], ps[12], ps[13],
            ps[14] as mock_slack,
            ps[15],
        ):
            lambda_handler({"mode": "weekday"}, None)

        messages = [c.args[0] for c in mock_slack.call_args_list]
        assert any("戦略見直しの示唆" in m for m in messages), messages
        assert any("検証対象" in m for m in messages), messages

        payload = mock_save_log.call_args_list[-1].args[0]
        assert payload["note_discipline_warnings"], payload
        assert payload["note_complete"] is True

    def test_clean_note_omits_discipline_key(self) -> None:
        """違反が無ければキー自体を書かないこと（#342・オオカミ少年防止）。"""
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9],
            ps[10] as mock_save_log,
            ps[11], ps[12], ps[13],
            ps[14] as mock_slack,
            ps[15],
        ):
            lambda_handler({"mode": "weekday"}, None)

        messages = [c.args[0] for c in mock_slack.call_args_list]
        assert not any("戦略見直しの示唆" in m for m in messages), messages
        payload = mock_save_log.call_args_list[-1].args[0]
        assert "note_discipline_warnings" not in payload, payload

    def test_clean_note_omits_cross_day_key(self) -> None:
        """混入が無ければキー自体を書かないこと（#334・オオカミ少年防止）。"""
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9],
            ps[10] as mock_save_log,
            ps[11], ps[12], ps[13],
            ps[14] as mock_slack,
            ps[15],
        ):
            lambda_handler({"mode": "friday"}, None)

        messages = [c.args[0] for c in mock_slack.call_args_list]
        assert not any("他日の計画値" in m for m in messages), messages
        payload = mock_save_log.call_args_list[-1].args[0]
        assert "note_cross_day_warnings" not in payload, payload

    def test_complete_note_does_not_alert(self) -> None:
        """正常なノートでは通知しないこと（#309・オオカミ少年防止）。"""
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8],
            ps[9], ps[10], ps[11], ps[12], ps[13],
            ps[14] as mock_slack,
            ps[15],
        ):
            lambda_handler({"mode": "friday"}, None)

        messages = [c.args[0] for c in mock_slack.call_args_list]
        assert not any("トレードノートが不完全" in m for m in messages), messages

    def test_incomplete_note_alert_is_sent_after_persistence(self) -> None:
        """Slack 通知は S3 保存の**後**であること（#309 レビュー指摘）。

        send_slack_alert は最大5秒ブロックする外向き I/O。証跡より先に置くと
        「通知は出たが明細は残っていない」を作り得る（#245 と同型）。
        文面の「保存済み」もこの順序でようやく事実になる。
        """
        incomplete = TradeNoteResult(
            note_markdown="## 途中で切れたノート",
            input_tokens=2570,
            output_tokens=4096,
            model="claude-haiku-4-5",
            is_complete=False,
        )
        calls: list[str] = []
        ps = _patch_stack(note_result=incomplete)
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9],
            ps[10] as mock_save_log,
            ps[11] as mock_save_note,
            ps[12], ps[13],
            ps[14] as mock_slack,
            ps[15],
        ):
            mock_save_log.side_effect = lambda *a, **k: calls.append("save_closeout_log") or True
            mock_save_note.side_effect = lambda *a, **k: calls.append("save_trade_note") or True
            mock_slack.side_effect = lambda *a, **k: calls.append("slack") or True
            lambda_handler({"mode": "friday"}, None)

        assert "slack" in calls, calls
        assert calls.index("save_closeout_log") < calls.index("slack"), calls
        assert calls.index("save_trade_note") < calls.index("slack"), calls

    def test_incomplete_note_still_persists_closeout_log(self) -> None:
        """ノートが不完全でも約定明細・損益は保存されること（税務証跡 §5.2）。"""
        incomplete = TradeNoteResult(
            note_markdown="## 途中で切れたノート",
            input_tokens=2570,
            output_tokens=4096,
            model="claude-haiku-4-5",
            is_complete=False,
        )
        ps = _patch_stack(note_result=incomplete)
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8],
            ps[9],
            ps[10] as mock_save_log,
            ps[11], ps[12], ps[13], ps[14], ps[15],
        ):
            lambda_handler({"mode": "friday"}, None)

        payload = mock_save_log.call_args[0][0]  # 確定版（最後の書き込み）
        assert payload["note_complete"] is False
        assert payload["executions"]  # 明細は残る

    def test_fetch_weekly_executions_called_on_friday(self) -> None:
        """friday モードで fetch_weekly_executions が呼ばれること（window は呼ばれないこと）。"""
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6],
            ps[7] as mock_window,
            ps[8] as mock_weekly,
            ps[9], ps[10], ps[11], ps[12], ps[13],
        ):
            lambda_handler({"mode": "friday"}, None)
        mock_weekly.assert_called_once()
        mock_window.assert_not_called()

    def test_fetch_execution_window_called_on_weekday(self) -> None:
        """weekday モードで fetch_execution_window が呼ばれること（weekly は呼ばれないこと）。"""
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6],
            ps[7] as mock_window,
            ps[8] as mock_weekly,
            ps[9], ps[10], ps[11], ps[12], ps[13],
        ):
            lambda_handler({"mode": "weekday"}, None)
        mock_window.assert_called_once()
        mock_weekly.assert_not_called()

    def test_morning_rationale_passed_to_note(self) -> None:
        """朝バッチの rationale がノート生成に渡されること。"""
        ps = _patch_stack(morning_log={"rationale": "USD/JPY ロング狙い", "status": "stubbed"})
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8],
            ps[9] as mock_gen,
            ps[10], ps[11], ps[12], ps[13],
        ):
            lambda_handler({"mode": "weekday"}, None)
        call_args = mock_gen.call_args[0][0]
        assert call_args.decision_plan == "USD/JPY ロング狙い"

    def test_morning_log_runtime_error_falls_back_and_continues(self) -> None:
        """load_decision_log が RuntimeError を送出しても E15 まで継続し、エラーが payload に記録されること。"""
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4],
            patch(
                "backend.src.batch.closeout_batch.s3_logger.load_decision_log",
                side_effect=RuntimeError("S3 読み込み失敗"),
            ),
            ps[6], ps[7], ps[8], ps[9],
            ps[10] as mock_evlog,
            ps[11], ps[12], ps[13],
        ):
            resp = lambda_handler({"mode": "weekday"}, None)
        assert resp["status"] == "ok"
        assert "decision_log_error" in resp
        # save_closeout_log まで到達すること。証跡先行保存により
        # ノート経路では 2 回呼ばれる（LLM 前 + 確定版）
        assert mock_evlog.call_count == 2

    def test_no_morning_log_uses_fallback_plan(self) -> None:
        """朝バッチログが無くてもフォールバックでノート生成が呼ばれること。"""
        ps = _patch_stack(morning_log=None)
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8],
            ps[9] as mock_gen,
            ps[10], ps[11], ps[12], ps[13],
        ):
            lambda_handler({"mode": "weekday"}, None)
        mock_gen.assert_called_once()
        call_args = mock_gen.call_args[0][0]
        assert "なし" in call_args.decision_plan


# ---------------------------------------------------------------------------
# E15: 永続化呼び出し確認
# ---------------------------------------------------------------------------


class TestTrailBeforeLLM:
    """税務証跡（§5.2）を LLM より先に確定させること。

    ノート生成は Anthropic API 呼び出しで、SDK の既定タイムアウトは 600 秒と
    Lambda（120 秒）より長かった。ここでハングすると Lambda が先に落ちる。
    従来は closeout ログが 1 行も残らず、latestExecutions は直近 1 日しか
    遡れないため明細が永久欠損した（.claude/rules/gmo-api.md / #245）。

    #311 で SDK 側に 90 秒を明示した後もこの順序は緩めない。非LLM区間が
    30 秒を超えると不変条件が崩れる余地が残るため（#339）。
    """

    def test_trail_is_saved_before_llm_is_called(self) -> None:
        """save_closeout_log が generate_trade_note より先に呼ばれること。"""
        calls: list[str] = []
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8],
            ps[9] as mock_gen,
            ps[10] as mock_save_log,
            ps[11], ps[12], ps[13],
        ):
            mock_save_log.side_effect = lambda *a, **k: calls.append("save") or True
            mock_gen.side_effect = lambda *a, **k: calls.append("llm") or _NOTE_RESULT
            lambda_handler({"mode": "weekday"}, None)

        assert calls == ["save", "llm", "save"], calls

    def test_first_save_carries_the_full_trail(self) -> None:
        """LLM 前の書き込みだけで証跡として完全であること。

        ここが欠けると「先に書いた」意味がない。明細・損益・決済理由の
        3 点が揃っていることを見る。
        """
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9],
            ps[10] as mock_save_log,
            ps[11], ps[12], ps[13],
        ):
            lambda_handler({"mode": "weekday"}, None)

        first = mock_save_log.call_args_list[0][0][0]
        assert first["executions"], first
        assert first["pnl"]["open_count"] == 1
        assert "exit_reasons" in first
        assert first["executions_fetched"] is True
        # ノートはまだ無いので未生成として書かれる
        assert first["note_generated"] is False
        assert first["note_pending"] is True

    def test_final_save_clears_note_pending(self) -> None:
        """確定版では note_pending が消えること（残っていれば異常の印）。"""
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9],
            ps[10] as mock_save_log,
            ps[11], ps[12], ps[13],
        ):
            lambda_handler({"mode": "weekday"}, None)

        final = mock_save_log.call_args_list[-1][0][0]
        assert "note_pending" not in final
        assert final["note_generated"] is True
        assert final["llm_total_tokens"] == 30

    def test_trail_survives_llm_failure(self) -> None:
        """LLM が落ちても明細は保存済みであること。

        Lambda タイムアウト（プロセス強制終了）は再現できないが、
        「LLM に入った時点で証跡は既に S3 にある」という不変条件は
        1 回目の書き込み内容で検証できる。
        """
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8],
            patch(
                "backend.src.batch.closeout_batch.generate_trade_note",
                side_effect=RuntimeError("LLM timeout"),
            ),
            ps[10] as mock_save_log,
            ps[11], ps[12], ps[13],
        ):
            lambda_handler({"mode": "weekday"}, None)

        first = mock_save_log.call_args_list[0][0][0]
        assert first["executions"], "LLM 失敗時に明細が失われている"
        assert first["pnl"]["open_count"] == 1

        final = mock_save_log.call_args_list[-1][0][0]
        assert final["note_generated"] is False
        assert "LLM timeout" in final["note_error"]
        assert final["executions"], "確定版でも明細が残ること"

    def test_fetch_failure_writes_only_once(self) -> None:
        """約定取得に失敗した日は LLM を呼ばないので二重書き込みしないこと。"""
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6],
            patch(
                "backend.src.batch.closeout_batch.fetch_execution_window",
                side_effect=RuntimeError("boom"),
            ),
            ps[8],
            ps[9] as mock_gen,
            ps[10] as mock_save_log,
            ps[11], ps[12], ps[13],
        ):
            lambda_handler({"mode": "weekday"}, None)

        mock_gen.assert_not_called()
        mock_save_log.assert_called_once()
        payload = mock_save_log.call_args[0][0]
        assert payload["executions_fetched"] is False
        assert "note_pending" not in payload


class TestPersistence:
    def test_save_closeout_log_called(self) -> None:
        """save_closeout_log が呼ばれること（§6.2 E15 結線）。

        証跡先行保存（税務証跡 §5.2）により、ノートを生成する経路では
        LLM の前と後で 2 回呼ばれる。
        """
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9],
            ps[10] as mock_evlog,
            ps[11], ps[12], ps[13],
        ):
            lambda_handler({"mode": "weekday"}, None)
        assert mock_evlog.call_count == 2

    def test_save_trade_note_called(self) -> None:
        """save_trade_note が呼ばれること（§6.2 E15 結線）。"""
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9],
            ps[10],
            ps[11] as mock_note_save,
            ps[12], ps[13],
        ):
            lambda_handler({"mode": "weekday"}, None)
        mock_note_save.assert_called_once()

    def test_save_dashboard_called(self) -> None:
        """save_dashboard が呼ばれること（§6.3 ダッシュボード snapshot）。"""
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9],
            ps[10], ps[11], ps[12],
            ps[13] as mock_dash,
        ):
            lambda_handler({"mode": "weekday"}, None)
        mock_dash.assert_called_once()

    def test_dashboard_preserves_decision_plan(self) -> None:
        """既存 dashboard の plan/lot は保持されること（朝バッチが書き込んだ値を上書きしない）。"""
        existing = {
            "status": "idle",
            "equityJpy": 500000,
            "plan": {"date": "2025-06-27", "direction": "LONG", "rationale": "テスト"},
            "lot": {"mvpFixed": 100, "vRawProd": 200, "vSafeProd": 150},
            "tradeNoteMarkdown": "",
            "logs": [],
            "lastUpdated": "2025-06-27T07:35:00+09:00",
        }
        ps = _patch_stack(existing_dashboard=existing)
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9],
            ps[10], ps[11], ps[12],
            ps[13] as mock_dash,
        ):
            lambda_handler({"mode": "weekday"}, None)
        saved = mock_dash.call_args[0][0]
        assert saved["plan"] == existing["plan"]
        assert saved["lot"] == existing["lot"]
        assert saved["tradeNoteMarkdown"] == _NOTE_RESULT.note_markdown

    def test_save_closeout_log_keyed_by_prev_trade_date(self) -> None:
        """save_closeout_log が prev_trade_date を today= に使うこと（土曜実行でも金曜付）。"""
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9],
            ps[10] as mock_evlog,
            ps[11], ps[12], ps[13],
        ):
            lambda_handler({"mode": "weekday"}, None)
        call_kwargs = mock_evlog.call_args[1]
        assert call_kwargs["today"] == _PREV_TRADE_DATE

    def test_save_trade_note_keyed_by_prev_trade_date(self) -> None:
        """save_trade_note が prev_trade_date を today= に使うこと（土曜実行でも金曜付）。"""
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9],
            ps[10],
            ps[11] as mock_note_save,
            ps[12], ps[13],
        ):
            lambda_handler({"mode": "weekday"}, None)
        call_kwargs = mock_note_save.call_args[1]
        assert call_kwargs["today"] == _PREV_TRADE_DATE

    def test_save_trade_note_not_called_on_note_failure(self) -> None:
        """generate_trade_note 失敗時に save_trade_note が呼ばれないこと。"""
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8],
            patch(
                "backend.src.batch.closeout_batch.generate_trade_note",
                side_effect=Exception("LLM error"),
            ),
            ps[10],
            ps[11] as mock_note_save,
            ps[12], ps[13],
        ):
            resp = lambda_handler({"mode": "weekday"}, None)
        mock_note_save.assert_not_called()
        assert resp["status"] == "ok"  # note 失敗でも手仕舞い自体は ok

    def test_save_dashboard_not_called_on_note_failure(self) -> None:
        """generate_trade_note 失敗時に save_dashboard が呼ばれないこと（既存 note を保護）。"""
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8],
            patch(
                "backend.src.batch.closeout_batch.generate_trade_note",
                side_effect=Exception("LLM error"),
            ),
            ps[10], ps[11], ps[12],
            ps[13] as mock_dash,
        ):
            lambda_handler({"mode": "weekday"}, None)
        mock_dash.assert_not_called()

    def test_dashboard_skipped_on_load_failure(self) -> None:
        """load_dashboard が RuntimeError を送出した場合、save_dashboard が呼ばれないこと。"""
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9],
            ps[10], ps[11],
            patch(
                "backend.src.batch.closeout_batch.s3_logger.load_dashboard",
                side_effect=RuntimeError("S3 読み込み失敗"),
            ),
            ps[13] as mock_dash,
        ):
            lambda_handler({"mode": "weekday"}, None)
        mock_dash.assert_not_called()


# ---------------------------------------------------------------------------
# Phase 1 封じ込め確認
# ---------------------------------------------------------------------------


class TestPhase1Containment:
    def test_no_live_post_in_phase1(self) -> None:
        """Phase 1 では GmoFxClient.private_post が呼ばれないこと（AGENTS.md #1）。

        execution fetch（private_get）経路は通し、private_post だけをスパイする。
        """
        mock_client = MagicMock()
        # get_executions / get_latest_executions は空リストを返す（GETのみ）
        mock_client.get_executions.return_value = []
        mock_client.get_latest_executions.return_value = []

        ps = _patch_stack()
        with (
            ps[0],  # run_cancel_first スタブ
            patch(
                "backend.src.batch.closeout_batch.GmoFxClient",
                return_value=MagicMock(
                    __enter__=lambda s: mock_client,
                    __exit__=MagicMock(return_value=False),
                ),
            ),
            ps[2], ps[3], ps[4], ps[5], ps[6],
            # fetch_execution_window は real mock_client 経由で動かす
            patch(
                "backend.src.batch.closeout_batch.fetch_execution_window",
                return_value=ExecutionFetch(),
            ),
            ps[8],
            ps[9], ps[10], ps[11], ps[12], ps[13],
        ):
            lambda_handler({"mode": "weekday"}, None)

        mock_client.private_post.assert_not_called()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCli:
    def test_cli_weekday_exits_zero(self) -> None:
        """weekday モードで正常完了した場合に main() が exit code 0 で終了すること."""
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6],
            ps[7], ps[8], ps[9], ps[10], ps[11], ps[12], ps[13],
            patch("sys.argv", ["batches", "closeout", "--mode", "weekday"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 0

    def test_cli_friday_mode_exits_zero(self) -> None:
        """friday モードで正常完了した場合に main() が exit code 0 で終了すること."""
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6],
            ps[7], ps[8], ps[9], ps[10], ps[11], ps[12], ps[13],
            patch("sys.argv", ["batches", "closeout", "--mode", "friday"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 0

    def test_cli_step1_failed_exits_nonzero(self) -> None:
        """Step1 失敗時は main() が exit code 1 で終了すること."""
        ps = _patch_stack(cancel_result=_STEP1_FAILED)
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[10], ps[14],
            patch("sys.argv", ["batches", "closeout", "--mode", "weekday"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 1

    def test_cli_market_closed_exits_zero(self) -> None:
        """市場閉場（正常な再試行ケース）は exit code 0 で終了すること."""
        ps = _patch_stack(cancel_result=_CLOSED_RESULT)
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[10], ps[14],
            patch("sys.argv", ["batches", "closeout", "--mode", "weekday"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 0

    def test_cli_invalid_mode_rejected(self) -> None:
        """不正な --mode は argparse エラー（SystemExit 2）になること."""
        with (
            patch("sys.argv", ["batches", "closeout", "--mode", "invalid"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# 2026-08-05 ノート捏造事故の回帰固定（付録B docs/spikes/executions_api_spec.md）
#
# 実際には約定（BUY 100 @150.681）→ 決済（SELL 100 @150.781）が完了していたのに
# executions_count: 0 となり、LLM が「約定件数 0件 / ポジション未保有 / 押し目に
# 届かず未約定」という事実と反対のノートを生成した。
# ---------------------------------------------------------------------------

_ENTRY_EXEC = Execution(
    execution_id="ex-open", order_id="1000201", symbol="USD_JPY", side="BUY",
    price="150.681", size="100", timestamp="2025-06-27T11:30:00.000Z",
    settle_type="OPEN", position_id="pos-1", loss_gain="0", fee="0", settled_swap="0",
)
_CLOSE_EXEC = Execution(
    execution_id="ex-close", order_id="1000202", symbol="USD_JPY", side="SELL",
    price="150.781", size="100", timestamp="2025-06-27T19:29:00.000Z",
    settle_type="CLOSE", position_id="pos-1", loss_gain="10.0", fee="-0.63",
    settled_swap="0",
)


class TestExecutionFabricationRegression:
    def test_close_execution_and_pnl_reach_the_note(self) -> None:
        """決済約定（別 orderId）と実損益が LLM 入力に届くこと。"""
        ps = _patch_stack(executions=[_ENTRY_EXEC, _CLOSE_EXEC])
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8],
            ps[9] as mock_gen,
            ps[10], ps[11], ps[12], ps[13],
        ):
            lambda_handler({"mode": "weekday"}, None)

        note_input = mock_gen.call_args[0][0]
        assert {e.settle_type for e in note_input.executions} == {"OPEN", "CLOSE"}

        from backend.src.llm.trade_note import _build_note_prompt

        _, user_prompt = _build_note_prompt(note_input)
        # 実損益 10.0 + (-0.63) = 9.37 円
        assert "9.37" in user_prompt
        assert "150.781" in user_prompt

    def test_closeout_log_persists_executions_and_pnl(self) -> None:
        """税務証跡として約定明細と損益が S3 ログに残ること（§5.2 / #25 DoD）。"""
        ps = _patch_stack(executions=[_ENTRY_EXEC, _CLOSE_EXEC])
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9],
            ps[10] as mock_evlog,
            ps[11], ps[12], ps[13],
        ):
            lambda_handler({"mode": "weekday"}, None)

        payload = mock_evlog.call_args[0][0]
        assert payload["executions_count"] == 2
        assert payload["executions_fetched"] is True
        assert [e["execution_id"] for e in payload["executions"]] == ["ex-open", "ex-close"]
        assert payload["pnl"]["realized_total"] == "9.37"
        assert payload["pnl"]["close_count"] == 1

    def test_execution_fetch_failure_produces_no_note(self) -> None:
        """約定取得が失敗したらノートを生成しない（不完全な税務証跡を作らない §6.2）。

        修正前は API エラーが空リストに化けてこの分岐に入らず、「約定なし」ノートが
        平然と生成されていた。
        """
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6],
            patch(
                "backend.src.batch.closeout_batch.fetch_execution_window",
                side_effect=RuntimeError("GMO API error (latestExecutions): status=1"),
            ),
            ps[8],
            ps[9] as mock_gen,
            ps[10] as mock_evlog,
            ps[11] as mock_note_save,
            ps[12], ps[13],
        ):
            lambda_handler({"mode": "weekday"}, None)

        mock_gen.assert_not_called()
        mock_note_save.assert_not_called()

        payload = mock_evlog.call_args[0][0]
        assert payload["note_generated"] is False
        assert "status=1" in payload["note_error"]

    def test_failed_fetch_does_not_persist_empty_executions(self) -> None:
        """取得失敗時に executions:[] を書かない（土曜の週次集約が「約定なし」と誤読する）。"""
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6],
            patch(
                "backend.src.batch.closeout_batch.fetch_execution_window",
                side_effect=RuntimeError("boom"),
            ),
            ps[8], ps[9],
            ps[10] as mock_evlog,
            ps[11], ps[12], ps[13],
        ):
            lambda_handler({"mode": "weekday"}, None)

        payload = mock_evlog.call_args[0][0]
        assert payload["executions_fetched"] is False
        assert "executions" not in payload
        assert "pnl" not in payload
        # 0 件ではなく「不明」— count だけ見て「約定なし」と読まれないこと
        assert payload["executions_count"] is None

    def test_data_gaps_are_passed_to_note_and_log(self) -> None:
        """「取得できなかった範囲」がノート入力と監査ログの両方に伝わること。"""
        gap = "latestExecutions が上限 100 件に達したため、これより古い約定は取得できていない"
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6],
            patch(
                "backend.src.batch.closeout_batch.fetch_execution_window",
                return_value=ExecutionFetch(executions=[], gaps=(gap,)),
            ),
            ps[8],
            ps[9] as mock_gen,
            ps[10] as mock_evlog,
            ps[11], ps[12], ps[13],
        ):
            lambda_handler({"mode": "weekday"}, None)

        assert mock_gen.call_args[0][0].data_gaps == (gap,)
        assert mock_evlog.call_args[0][0]["execution_data_gaps"] == [gap]

    def test_empty_executions_prompt_does_not_assert_no_trade(self) -> None:
        """約定 0 件でも「約定しなかった」と断定させないプロンプトになっていること。"""
        from backend.src.llm.trade_note import TradeNoteInput, _build_note_prompt

        _, user_prompt = _build_note_prompt(
            TradeNoteInput(decision_plan="p", executions=[], now_jst=_NOW_JST)
        )

        assert "断定できるわけではない" in user_prompt


# ---------------------------------------------------------------------------
# #207 (a): Step3（決済）失敗の捕捉・記録・強アラート
#
# キャンセル・ファーストは Step2 で決済 OCO（SL/TP）を先に消す。その直後の Step3 が
# 失敗すると建玉は SL/TP なしで残る。従来は例外が未捕捉で Lambda ごと落ち、
# 「無防備 かつ 無記録」で翌 4:30 まで放置されていた。
# ---------------------------------------------------------------------------


class TestStep3FailureHandling:
    @staticmethod
    def _step3_raises_stack(error: Exception | None = None) -> list[Any]:
        ps = _patch_stack()
        ps[0] = patch(
            "backend.src.batch.closeout_batch.run_cancel_first",
            side_effect=error or GmoApiError(
                "GMO API error (closeOrder): status=1 codes=['ERR-423']",
                status=1,
                message_codes=("ERR-423",),
            ),
        )
        return ps

    def test_lambda_does_not_crash_and_reports_failure(self) -> None:
        """Step3 例外で Lambda がエラー終了せず、status=step3_failed を返すこと。"""
        ps = self._step3_raises_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9],
            ps[10], ps[11], ps[12], ps[13], ps[14],
            patch(
                "backend.src.batch.closeout_batch.list_open_position_ids",
                return_value=["700101"],
            ),
        ):
            resp = lambda_handler({"mode": "weekday"}, None)

        assert resp["status"] == "step3_failed"

    def test_strong_alert_names_the_unprotected_positions(self) -> None:
        """アラートに建玉ID と「損切りが無い」旨が含まれること（人が手動決済するため）。"""
        ps = self._step3_raises_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9],
            ps[10], ps[11], ps[12], ps[13],
            ps[14] as mock_alert,
            patch(
                "backend.src.batch.closeout_batch.list_open_position_ids",
                return_value=["700101"],
            ),
        ):
            lambda_handler({"mode": "weekday"}, None)

        message = mock_alert.call_args[0][0]
        assert "700101" in message
        assert "損切りが無い状態" in message
        assert "ERR-423" in message

    def test_executions_are_still_fetched_and_persisted(self) -> None:
        """🔴 Step3 失敗でも約定明細を取得・永続化すること。

        early return すると当日の約定明細を取得できず、latestExecutions は直近1日しか
        遡れないため税務証跡が永久欠損する（#245 と同型）。
        """
        ps = self._step3_raises_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6],
            ps[7] as mock_window,
            ps[8], ps[9],
            ps[10] as mock_evlog,
            ps[11], ps[12], ps[13], ps[14],
            patch(
                "backend.src.batch.closeout_batch.list_open_position_ids",
                return_value=[],
            ),
        ):
            lambda_handler({"mode": "weekday"}, None)

        mock_window.assert_called_once()
        payload = mock_evlog.call_args[0][0]
        assert payload["executions_count"] == 1
        assert payload["executions_fetched"] is True
        assert "executions" in payload

    def test_audit_log_records_error_and_at_risk_positions(self) -> None:
        """監査ログに失敗理由と無防備な建玉 ID が残ること。"""
        ps = self._step3_raises_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9],
            ps[10] as mock_evlog,
            ps[11], ps[12], ps[13], ps[14],
            patch(
                "backend.src.batch.closeout_batch.list_open_position_ids",
                return_value=["700101"],
            ),
        ):
            lambda_handler({"mode": "weekday"}, None)

        payload = mock_evlog.call_args[0][0]
        assert payload["status"] == "step3_failed"
        assert "ERR-423" in payload["step3_error"]
        assert payload["step3_at_risk_position_ids"] == ["700101"]

    def test_lock_summary_is_not_marked_ok(self, mock_closeout_lock: Any) -> None:
        """ロックの完了印を "ok" で塗り潰さないこと（レビュー指摘 3）。"""
        ps = self._step3_raises_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9],
            ps[10], ps[11], ps[12], ps[13], ps[14],
            patch(
                "backend.src.batch.closeout_batch.list_open_position_ids",
                return_value=[],
            ),
        ):
            lambda_handler({"mode": "weekday"}, None)

        assert mock_closeout_lock.update.call_args.kwargs["summary"]["status"] == "step3_failed"
        # #138: cancel/close の POST を試行済みで結果未確定 → ロックは保持する
        mock_closeout_lock.release.assert_not_called()

    def test_at_risk_probe_failure_still_sends_alert(self) -> None:
        """建玉再取得に失敗しても、アラート自体は必ず送ること（送れないほうが害が大きい）。"""
        ps = self._step3_raises_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9],
            ps[10], ps[11], ps[12], ps[13],
            ps[14] as mock_alert,
            patch(
                "backend.src.batch.closeout_batch.list_open_position_ids",
                side_effect=RuntimeError("GET failed"),
            ),
        ):
            resp = lambda_handler({"mode": "weekday"}, None)

        assert resp["status"] == "step3_failed"
        message = mock_alert.call_args[0][0]
        assert "建玉ID を特定できず" in message


class TestStep3VerificationFailed(TestStep3FailureHandling):
    """#207 レビュー指摘 1: 例外にならなくても「決済結果を確認できない」なら通知する。

    closeOrder が受理されたのに決済後の建玉再取得に失敗した場合、決済が成立したか
    不明なまま建玉が SL/TP なしで残りうる。従来は payload に出るだけで人に届かなかった。
    """

    _UNVERIFIED = CancelFirstResult(step3_verification_failed=True)

    def test_alert_is_sent_on_verification_failure(self) -> None:
        ps = _patch_stack(cancel_result=self._UNVERIFIED)
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9],
            ps[10], ps[11], ps[12], ps[13],
            ps[14] as mock_alert,
            patch(
                "backend.src.batch.closeout_batch.list_open_position_ids",
                return_value=["700101"],
            ),
        ):
            resp = lambda_handler({"mode": "weekday"}, None)

        # 「失敗した」ではなく「確認できなかった」— 人が確認すべき内容が違う
        assert resp["status"] == "step3_unverified"
        message = mock_alert.call_args[0][0]
        assert "決済結果を確認できませんでした" in message
        assert "700101" in message

    def test_status_is_not_ok_in_log_and_lock(self, mock_closeout_lock: Any) -> None:
        ps = _patch_stack(cancel_result=self._UNVERIFIED)
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9],
            ps[10] as mock_evlog,
            ps[11], ps[12], ps[13], ps[14],
            patch(
                "backend.src.batch.closeout_batch.list_open_position_ids",
                return_value=[],
            ),
        ):
            lambda_handler({"mode": "weekday"}, None)

        assert mock_evlog.call_args[0][0]["status"] == "step3_unverified"
        summary = mock_closeout_lock.update.call_args.kwargs["summary"]
        assert summary["status"] == "step3_unverified"

    def test_persistent_residual_now_alerts(self) -> None:
        """residual は通知対象（#207 (b) L1 で意味が変わった）。

        L1 導入前の `step3_residual_ids` は「closeOrder 直後の 1 回の GET で見えた建玉」で、
        約定待ちの一時的な残存を含むため通知対象外にしていた。
        L1 で**短ポーリング後も消えなかった建玉のみ**を指すようになったので、
        これは「本当に閉じられていない」＝ SL/TP なしの可能性がある状態であり通知する。
        """
        residual_only = CancelFirstResult(step3_residual_ids=["700101"])
        ps = _patch_stack(cancel_result=residual_only)
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9],
            ps[10] as mock_evlog,
            ps[11], ps[12], ps[13],
            ps[14] as mock_alert,
        ):
            resp = lambda_handler({"mode": "weekday"}, None)

        assert resp["status"] == "step3_residual"
        message = mock_alert.call_args[0][0]
        assert "建玉が閉じていません" in message
        assert "700101" in message
        payload = mock_evlog.call_args[0][0]
        assert payload["step3_residual"] == ["700101"]
        assert payload["step3_at_risk_position_ids"] == ["700101"]

    def test_marker_is_persisted_for_unprotected_positions(
        self, mock_closeout_lock: Any
    ) -> None:
        """#207 (b) L3: 無防備な建玉を DynamoDB に残す（Slack は流れるが記録は残る）。"""
        residual_only = CancelFirstResult(step3_residual_ids=["700101", "700102"])
        ps = _patch_stack(cancel_result=residual_only)
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9],
            ps[10], ps[11], ps[12], ps[13], ps[14],
        ):
            lambda_handler({"mode": "weekday"}, None)

        summary = mock_closeout_lock.update.call_args.kwargs["summary"]
        assert summary["unprotected_position_ids"] == "700101,700102"
        assert summary["unprotected_since"]

    def test_no_marker_on_clean_run(self, mock_closeout_lock: Any) -> None:
        """正常時はマーカーを書かないこと（誤検知で運用を疲弊させない）。"""
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9],
            ps[10], ps[11], ps[12], ps[13], ps[14],
        ):
            lambda_handler({"mode": "weekday"}, None)

        summary = mock_closeout_lock.update.call_args.kwargs["summary"]
        assert "unprotected_position_ids" not in summary
        assert summary["status"] == "ok"


class TestStep3FailureAuditFidelity:
    """#207 レビュー指摘 2: 分からないことを「0 件」と書かない。"""

    def test_step1_step2_are_unknown_not_empty_on_exception(self) -> None:
        """cancel-first が例外送出したとき step1/step2 の取消実績は null（不明）。

        空リストだと「1件も取り消していない」と読めるが、実際には Step2 が SL/TP を
        取消済みの可能性が高い（だからこそ建玉が無防備になる）。
        """
        ps = TestStep3FailureHandling._step3_raises_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9],
            ps[10] as mock_evlog,
            ps[11], ps[12], ps[13], ps[14],
            patch(
                "backend.src.batch.closeout_batch.list_open_position_ids",
                return_value=["700101"],
            ),
        ):
            lambda_handler({"mode": "weekday"}, None)

        payload = mock_evlog.call_args[0][0]
        assert payload["step1_cancelled"] is None
        assert payload["step2_cancelled"] is None

    def test_normal_run_still_records_actual_cancel_ids(self) -> None:
        """正常時は従来どおり実際の取消 ID を記録すること（null 化は失敗時のみ）。"""
        ok = CancelFirstResult(step1_cancelled_ids=["1"], step2_cancelled_ids=["2"])
        ps = _patch_stack(cancel_result=ok)
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9],
            ps[10] as mock_evlog,
            ps[11], ps[12], ps[13], ps[14],
        ):
            lambda_handler({"mode": "weekday"}, None)

        payload = mock_evlog.call_args[0][0]
        assert payload["step1_cancelled"] == ["1"]
        assert payload["step2_cancelled"] == ["2"]


class TestTradePlanWiring:
    """#249: 判断ログの計画値が構造化されてノート生成に届くこと。"""

    _DECISION_LOG = {
        "rationale": "Pivot P=150.593付近の押し目を150.53で拾い、S1=150.23の手前を意識",
        "direction": "LONG",
        "order_type": "LIMIT",
        "entry_price": 150.53,
        "stop_loss": 149.923,
        "take_profit": 151.259,
        "lot": 100,
        "sl_profile": "wide",
        "rr_target": 1.2,
    }

    def test_plan_reaches_note_input(self) -> None:
        ps = _patch_stack(morning_log=self._DECISION_LOG)
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8],
            ps[9] as mock_gen,
            ps[10], ps[11], ps[12], ps[13], ps[14],
        ):
            lambda_handler({"mode": "weekday"}, None)

        plan = mock_gen.call_args[0][0].plans[0]
        # 🔴 rationale 中の S1(150.23) ではなく実際の SL が届くこと
        assert plan.stop_loss == "149.923"
        assert plan.take_profit == "151.259"
        assert plan.entry_price == "150.53"
        assert plan.direction == "LONG"

    def test_missing_decision_log_yields_empty_plan(self) -> None:
        """判断ログが無くても落ちず、空の計画値になること（プロンプト側で推測禁止）。"""
        ps = _patch_stack(morning_log=None)
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8],
            ps[9] as mock_gen,
            ps[10], ps[11], ps[12], ps[13], ps[14],
        ):
            lambda_handler({"mode": "weekday"}, None)

        assert mock_gen.call_args[0][0].plans[0].is_empty is True


# ---------------------------------------------------------------------------
# 決済理由の結線（Issue #280）
# ---------------------------------------------------------------------------

_ROOT_ORDER_ID = "1000401"
_POS_ID = "700201"

_CLOSED_PAIR = [
    Execution(
        execution_id="ex-open",
        order_id=_ROOT_ORDER_ID,  # IFD-OCO は OPEN.orderId == rootOrderId
        symbol="USD_JPY",
        side="BUY",
        price="150.00",
        size="100",
        timestamp="2025-06-27T11:30:00.000Z",
        settle_type="OPEN",
        position_id=_POS_ID,
        loss_gain="0",
        fee="-0.300",
        settled_swap="0",
    ),
    Execution(
        execution_id="ex-close",
        order_id="1000402",  # 決済は別 orderId
        symbol="USD_JPY",
        side="SELL",
        price="150.40",
        size="100",
        timestamp="2025-06-27T19:30:49.000Z",
        settle_type="CLOSE",
        position_id=_POS_ID,
        loss_gain="40.0",
        fee="-0.300",
        settled_swap="0",
    ),
]

_DECISION_LOG = {
    "rationale": "押し目 LONG",
    "direction": "LONG",
    "order_type": "LIMIT",
    "entry_price": 150.00,
    "stop_loss": 149.322,
    "take_profit": 150.814,
    "lot": 100,
    "sl_profile": "wide",
    "rr_target": 1.2,
    "status": "ok",
}


class TestExitReasonWiring:
    """cancel-first の結果が決済理由としてノートと S3 に届くこと（#280）。"""

    def _run(
        self, cancel_result: CancelFirstResult, close_price: str | None = None
    ) -> tuple[Any, Any]:
        executions = _CLOSED_PAIR
        if close_price is not None:
            executions = [
                _CLOSED_PAIR[0],
                Execution(**{**_CLOSED_PAIR[1].__dict__, "price": close_price}),
            ]
        ps = _patch_stack(
            cancel_result=cancel_result,
            executions=executions,
            morning_log=_DECISION_LOG,
        )
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8],
            ps[9] as mock_gen,
            ps[10] as mock_log,
            ps[11], ps[12], ps[13], ps[14],
        ):
            lambda_handler({"mode": "weekday"}, None)
        return mock_gen, mock_log

    def test_cancelled_root_becomes_time_stop(self) -> None:
        """2026-08-10 相当の回帰: step2 が OPEN の orderId を取り消していれば時間切れ。

        修正前はこの材料が LLM に渡らず、「計画 TP まで待たずに決済した」という
        存在しない裁量判断がノートに書かれた。
        """
        mock_gen, mock_log = self._run(
            CancelFirstResult(
                step2_cancelled_ids=[_ROOT_ORDER_ID], step3_closed_ids=[_POS_ID]
            )
        )

        evidence = mock_gen.call_args[0][0].closeout_evidence
        assert evidence is not None
        assert _ROOT_ORDER_ID in evidence.cancelled_root_order_ids

        payload = mock_log.call_args.kwargs.get("payload") or mock_log.call_args[0][0]
        assert payload["exit_reasons"] == {_POS_ID: "time_stop"}

    def test_step3_failure_yields_unknown_not_false_oco(self) -> None:
        """🔴 cancel-first が例外で落ちた回は、空集合ではなく「判定不能」にする。

        結果は空の CancelFirstResult に差し替わる。これを空集合として渡すと
        「取り消し対象に無い＝OCO が発火した」と誤断定する。
        """
        ps = _patch_stack(executions=_CLOSED_PAIR, morning_log=_DECISION_LOG)
        with (
            patch(
                "backend.src.batch.closeout_batch.run_cancel_first",
                side_effect=RuntimeError("closeOrder timeout"),
            ),
            ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8],
            ps[9] as mock_gen,
            ps[10] as mock_log,
            ps[11], ps[12], ps[13], ps[14],
            patch("backend.src.batch.closeout_batch._probe_open_position_ids", return_value=[]),
            patch("backend.src.batch.closeout_batch._notify_step3_failure"),
        ):
            lambda_handler({"mode": "weekday"}, None)

        assert mock_gen.call_args[0][0].closeout_evidence is None
        payload = mock_log.call_args.kwargs.get("payload") or mock_log.call_args[0][0]
        assert payload["exit_reasons"] == {_POS_ID: "unknown"}

    def test_oco_fire_at_tp_is_recorded(self) -> None:
        """cancel-first が何も触っていない＋決済価格が計画 TP 以上 → 利確 OCO 発火。

        E18 の観測対象 #1（OCO 発火）が起きたとき、この経路で記録される。
        """
        ps = _patch_stack(
            cancel_result=CancelFirstResult(),
            executions=[
                _CLOSED_PAIR[0],
                Execution(
                    **{**_CLOSED_PAIR[1].__dict__, "price": "150.814"}
                ),
            ],
            morning_log=_DECISION_LOG,
        )
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8],
            ps[9], ps[10] as mock_log, ps[11], ps[12], ps[13], ps[14],
        ):
            lambda_handler({"mode": "weekday"}, None)

        payload = mock_log.call_args.kwargs.get("payload") or mock_log.call_args[0][0]
        assert payload["exit_reasons"] == {_POS_ID: "take_profit"}

    def test_verification_failed_does_not_become_oco_fire(self) -> None:
        """🔴 決済後の建玉再取得に失敗した回は OCO 発火に化けない（レビュー指摘）。

        cancel_first は fail-closed で step3_closed_ids=[] を返す。step2 も空
        （OCO が先に消えた経路）だと「cancel-first 未介入＝OCO 発火」に落ち、
        成行の約定価格が計画 TP に達していれば利確と断定してしまう。

        🔴 決済価格は**計画 TP ちょうど**にすること。SL/TP の帯域内の価格だと
        修正前でも unknown になり、この回帰を捕まえられない。
        """
        _, mock_log = self._run(
            CancelFirstResult(
                step2_cancelled_ids=[],
                step3_closed_ids=[],
                step3_verification_failed=True,
            ),
            close_price="150.814",  # = 計画 TP
        )
        payload = mock_log.call_args.kwargs.get("payload") or mock_log.call_args[0][0]
        assert payload["exit_reasons"] == {_POS_ID: "unknown"}

    def test_verification_failed_keeps_positive_time_stop(self) -> None:
        """検証不能でも step2 の取消は成功しているので time_stop は保たれる。"""
        _, mock_log = self._run(
            CancelFirstResult(
                step2_cancelled_ids=[_ROOT_ORDER_ID],
                step3_closed_ids=[],
                step3_verification_failed=True,
            ),
            close_price="150.814",  # 計画 TP でも正の ID 証拠が勝つ
        )
        payload = mock_log.call_args.kwargs.get("payload") or mock_log.call_args[0][0]
        assert payload["exit_reasons"] == {_POS_ID: "time_stop"}


# ---------------------------------------------------------------------------
# #346: cancel-first 生結果を TradeNoteInput へ配線する
# ---------------------------------------------------------------------------


class TestCancelFirstSnapshotWiring:
    """S3 と同じ None / 空 / ID 規則でスナップショットがノートに届くこと（#346）。"""

    def test_2026_09_02_step1_reaches_note_input(self) -> None:
        """🔴 修正前は TradeNoteInput に cancel_first が無く、本テストは落ちる。"""
        from backend.src.llm.trade_note import _build_note_prompt

        ps = _patch_stack(
            cancel_result=CancelFirstResult(step1_cancelled_ids=["1000501"]),
            executions=[],
        )
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8],
            ps[9] as mock_gen,
            ps[10], ps[11], ps[12], ps[13], ps[14],
        ):
            lambda_handler({"mode": "weekday"}, None)

        note_input = mock_gen.call_args[0][0]
        assert note_input.cancel_first is not None
        assert note_input.cancel_first.step1_cancelled == ("1000501",)
        _, user = _build_note_prompt(note_input)
        assert "1000501" in user

    def test_empty_cancel_is_empty_tuple_not_none(self) -> None:
        """従来の全日（cancel 対象なし）は空 tuple。#280 の空 frozenset も変えない。"""
        ps = _patch_stack(cancel_result=CancelFirstResult(), executions=[])
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8],
            ps[9] as mock_gen,
            ps[10], ps[11], ps[12], ps[13], ps[14],
        ):
            lambda_handler({"mode": "weekday"}, None)

        note_input = mock_gen.call_args[0][0]
        assert note_input.cancel_first is not None
        assert note_input.cancel_first.step1_cancelled == ()
        assert note_input.cancel_first.step2_cancelled == ()
        assert note_input.cancel_first.step3_closed == ()
        assert note_input.closeout_evidence is not None
        assert note_input.closeout_evidence.cancelled_root_order_ids == frozenset()

    def test_step3_error_marks_step1_unknown(self) -> None:
        """Step3 例外時は step1 を空 tuple にせず None（S3 の null と一致）。"""
        ps = _patch_stack(executions=_CLOSED_PAIR, morning_log=_DECISION_LOG)
        with (
            patch(
                "backend.src.batch.closeout_batch.run_cancel_first",
                side_effect=RuntimeError("closeOrder timeout"),
            ),
            ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8],
            ps[9] as mock_gen,
            ps[10], ps[11], ps[12], ps[13], ps[14],
            patch("backend.src.batch.closeout_batch._probe_open_position_ids", return_value=[]),
            patch("backend.src.batch.closeout_batch._notify_step3_failure"),
        ):
            lambda_handler({"mode": "weekday"}, None)

        note_input = mock_gen.call_args[0][0]
        assert note_input.closeout_evidence is None
        assert note_input.cancel_first is not None
        assert note_input.cancel_first.step1_cancelled is None
        assert note_input.cancel_first.step2_cancelled is None

    def test_cancel_skipped_is_none(self) -> None:
        """#226 note-only はセクション無し（「対象なし」と書かない）。"""
        ps = _patch_stack(cancel_result=_CLOSED_RESULT)
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8],
            ps[9] as mock_gen,
            ps[10], ps[11], ps[12], ps[13], ps[14],
            patch(
                "backend.src.batch.closeout_batch.is_market_holiday",
                return_value=True,
            ),
        ):
            lambda_handler({"mode": "friday"}, None)

        assert mock_gen.call_args[0][0].cancel_first is None

    def test_step2_step3_day_keeps_exit_reasons(self) -> None:
        """step2/step3 日の exit_reasons と決済理由セクションが壊れないこと。"""
        from backend.src.llm.trade_note import _build_note_prompt

        ps = _patch_stack(
            cancel_result=CancelFirstResult(
                step2_cancelled_ids=[_ROOT_ORDER_ID], step3_closed_ids=[_POS_ID]
            ),
            executions=_CLOSED_PAIR,
            morning_log=_DECISION_LOG,
        )
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8],
            ps[9] as mock_gen,
            ps[10] as mock_log,
            ps[11], ps[12], ps[13], ps[14],
        ):
            lambda_handler({"mode": "weekday"}, None)

        note_input = mock_gen.call_args[0][0]
        assert note_input.closeout_evidence is not None
        assert note_input.closeout_evidence.cancelled_root_order_ids == frozenset(
            {_ROOT_ORDER_ID}
        )
        assert note_input.cancel_first is not None
        assert note_input.cancel_first.step2_cancelled == (_ROOT_ORDER_ID,)
        assert note_input.cancel_first.step3_closed == (_POS_ID,)
        payload = mock_log.call_args.kwargs.get("payload") or mock_log.call_args[0][0]
        assert payload["exit_reasons"] == {_POS_ID: "time_stop"}
        _, user = _build_note_prompt(note_input)
        assert "時間切れ手仕舞い" in user
        assert "## cancel-first 結果" in user


# ---------------------------------------------------------------------------
# #318: 判断ログから見送り理由を取り出す
# ---------------------------------------------------------------------------


class TestNoEntryReasonExtraction:
    """`_build_trade_plan` が見送り理由を運ぶこと（#318）。

    運ばないと週次ノートが「無発注日の判断根拠は記録なし」と書く。
    判断ログには残っているので事実誤認になる。
    """

    def _plan(self, log: dict[str, Any] | None) -> Any:
        from backend.src.batch.closeout_batch import _build_trade_plan

        return _build_trade_plan(log, trade_date=_PREV_TRADE_DATE)

    def test_hard_rule_reason_is_extracted(self) -> None:
        """day-skip 種別（reason）を拾うこと。"""
        plan = self._plan({"status": "no_entry", "reason": "high_vol_regime_day_skip"})
        assert plan.no_entry_reason == "high_vol_regime_day_skip"

    def test_rationale_used_when_reason_is_null(self) -> None:
        """LLM 判断による見送りは reason が null で rationale にだけ理由が入る。

        2026-08-20 の FOMC 議事録の日が実例。ここを拾わないと
        「LLM が撃たないと決めた日」の理由が丸ごと落ちる。
        """
        plan = self._plan(
            {"status": "no_entry", "reason": None, "rationale": "FOMC 議事録が保有窓内"}
        )
        assert plan.no_entry_reason == "FOMC 議事録が保有窓内"

    def test_reason_takes_precedence_over_rationale(self) -> None:
        """両方ある日は種別（reason）を優先すること。"""
        plan = self._plan(
            {"status": "no_entry", "reason": "indicator_day_skip", "rationale": "散文"}
        )
        assert plan.no_entry_reason == "indicator_day_skip"

    def test_entry_day_has_no_reason(self) -> None:
        """発注した日に見送り理由が付かないこと。"""
        plan = self._plan(
            {"status": "ok", "direction": "SHORT", "rationale": "下ブレイク"}
        )
        assert plan.no_entry_reason == ""

    def test_missing_log_has_no_reason(self) -> None:
        assert self._plan(None).no_entry_reason == ""

    def test_no_entry_without_any_reason_is_empty(self) -> None:
        """理由が無くても落ちないこと（後方互換）。"""
        assert self._plan({"status": "no_entry"}).no_entry_reason == ""


# ---------------------------------------------------------------------------
# #339 — 予算の内訳を実測として残す
# ---------------------------------------------------------------------------


class TestPhaseElapsedLogging:
    """フェーズ別所要時間の構造化ログ（#339）。

    🔴 このログの目的は Duration の絶対値監視ではない。closeout の予算構造は
    「Lambda 120 秒・LLM は SDK read timeout 90 秒で頭打ち」なので、Lambda が
    LLM 起因で死ぬのは **非LLM区間が 30 秒を超えたときだけ**である。
    `non_llm_seconds` はその不変条件を直接見るための値であり、壊れる前に
    余裕の縮小を検知するために出す。
    """

    @staticmethod
    def _phase_record(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
        records = [
            r for r in caplog.records if r.getMessage() == "closeout_phase_elapsed"
        ]
        assert len(records) == 1, [r.getMessage() for r in caplog.records]
        return records[0]

    def test_phases_are_logged_on_success(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """成功時に 4 区間すべてが数値で出ること。"""
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9],
            ps[10], ps[11], ps[12], ps[13],
            caplog.at_level(logging.INFO),
        ):
            lambda_handler({"mode": "weekday"}, None)

        rec = self._phase_record(caplog)
        for field in (
            "cancel_first_seconds",
            "fetch_seconds",
            "llm_seconds",
            "non_llm_seconds",
            "total_seconds",
        ):
            assert isinstance(getattr(rec, field), float), field
            assert getattr(rec, field) >= 0.0, field

    def test_non_llm_seconds_excludes_the_llm(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """non_llm_seconds が LLM 区間を差し引いた値であること。

        監視の閾値（30 秒）はこの値に対して引くため、ここがずれると
        不変条件の余裕を誤って読む。
        """
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8],
            ps[9] as mock_gen,
            ps[10], ps[11], ps[12], ps[13],
            caplog.at_level(logging.INFO),
        ):
            # LLM 区間だけを意図的に伸ばす
            def _slow(*_a: Any, **_k: Any) -> TradeNoteResult:
                time.sleep(0.05)
                return _NOTE_RESULT

            mock_gen.side_effect = _slow
            lambda_handler({"mode": "weekday"}, None)

        rec = self._phase_record(caplog)
        assert rec.llm_seconds >= 0.05
        assert rec.non_llm_seconds == pytest.approx(
            rec.total_seconds - rec.llm_seconds, abs=0.011
        )
        assert rec.non_llm_seconds < rec.total_seconds

    def test_llm_seconds_is_none_when_llm_is_skipped(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """約定取得に失敗して LLM を呼ばなかった回は 0.0 ではなく None。

        0.0 は「一瞬で終わった」を意味してしまう。executions_count が
        0 と null を区別しているのと同じ規律。
        """
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6],
            patch(
                "backend.src.batch.closeout_batch.fetch_execution_window",
                side_effect=RuntimeError("GMO down"),
            ),
            ps[8], ps[9] as mock_gen,
            ps[10], ps[11], ps[12], ps[13], ps[14],
            caplog.at_level(logging.INFO),
        ):
            lambda_handler({"mode": "weekday"}, None)

        mock_gen.assert_not_called()
        rec = self._phase_record(caplog)
        assert rec.llm_seconds is None
        # 呼んでいない以上、全区間が非LLMである
        assert rec.non_llm_seconds == pytest.approx(rec.total_seconds, abs=0.011)

    def test_llm_seconds_recorded_when_llm_raises(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """LLM が失敗しても区間は計測されること。

        SDK read timeout（90 秒）で諦めた回にこそ「LLM が予算をどれだけ
        食ったか」を知りたい。成功時しか出ないと肝心の回が欠測する。
        """
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8],
            patch(
                "backend.src.batch.closeout_batch.generate_trade_note",
                side_effect=RuntimeError("LLM timeout"),
            ),
            ps[10], ps[11], ps[12], ps[13],
            caplog.at_level(logging.INFO),
        ):
            lambda_handler({"mode": "weekday"}, None)

        rec = self._phase_record(caplog)
        assert isinstance(rec.llm_seconds, float)

    def test_cancel_first_seconds_logged_at_completion(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """金が動く段の実測は closeout_completed 側にも残ること。

        最後の 1 行に集約すると、以降で落ちた回の cancel-first 実測が
        まるごと消える。
        """
        ps = _patch_stack()
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9],
            ps[10], ps[11], ps[12], ps[13],
            caplog.at_level(logging.INFO),
        ):
            lambda_handler({"mode": "weekday"}, None)

        rec = next(
            r for r in caplog.records if r.getMessage() == "closeout_completed"
        )
        assert isinstance(rec.cancel_first_seconds, float)
        assert rec.cancel_first_seconds >= 0.0

    def test_cancel_first_seconds_logged_on_step_failure(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """cancel-first 自身が失敗した回も金の段の実測が残ること。

        この経路は early return するため closeout_completed /
        closeout_phase_elapsed に到達しない。ここで出さないと「GMO が遅くて
        Step1 で落ちた」回の所要時間がまるごと欠測し、遅さが原因かを後から
        切り分けられない。
        """
        ps = _patch_stack(cancel_result=_STEP1_FAILED)
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9],
            ps[10], ps[11], ps[12], ps[13], ps[14],
            caplog.at_level(logging.INFO),
        ):
            lambda_handler({"mode": "weekday"}, None)

        assert not [
            r for r in caplog.records if r.getMessage() == "closeout_phase_elapsed"
        ]
        rec = next(
            r for r in caplog.records if r.getMessage() == "closeout_step1_failed"
        )
        assert isinstance(rec.cancel_first_seconds, float)
        assert rec.cancel_first_seconds >= 0.0

    def test_market_closed_also_carries_cancel_first_seconds(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """閉場での early return でも同じキーが出ること（Step0 の遅さも見たい）。"""
        ps = _patch_stack(cancel_result=_CLOSED_RESULT)
        with (
            ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9],
            ps[10], ps[11], ps[12], ps[13], ps[14],
            caplog.at_level(logging.INFO),
        ):
            lambda_handler({"mode": "weekday"}, None)

        rec = next(
            r for r in caplog.records if r.getMessage() == "closeout_market_closed"
        )
        assert isinstance(rec.cancel_first_seconds, float)
