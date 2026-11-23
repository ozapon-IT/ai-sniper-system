"""ロック解放の障害（条件不成立ではない）は Slack で通知する（#406）。

`release_lock_outcome` は例外を握りつぶして FAILED を返すため、IAM の `dynamodb:DeleteItem` が無い
（AccessDenied）と、仕様の「ロックを解放して再試行を可能にする」だけが静かに効かない。
ロックが残ると同日（手仕舞いは同朝）の再試行ができないので、運用者に気づかせる。
条件不成立（ロック不在・実 orderId 記録済み）は設計どおりの拒否で、通知しない。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from backend.src.batch.closeout_batch import _handle_closeout_failure
from backend.src.persistence import dynamodb_store
from backend.src.persistence.dynamodb_store import LockReleaseOutcome
from tests.batch.test_decision_concluded import _run_day
from tests.batch.test_decision_log_trading_phase import NOW, TODAY, _post


@pytest.fixture(autouse=True)
def _local(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    from backend.src.persistence import s3_logger

    monkeypatch.setattr(s3_logger, "LOCAL_PERSIST_DIR", str(tmp_path))
    monkeypatch.setattr(dynamodb_store, "LOCAL_PERSIST_DIR", str(tmp_path))


def _slack_alerts() -> Any:
    return patch("backend.src.batch.lock_acquire.send_slack_alert", return_value=True)


class TestDecisionOrderRejected:
    """判断: 発注が未約定確実に失敗（HTTP 4xx）→ ロックを解放して当日の再試行を許す（#138）。"""

    @staticmethod
    def _rejected(monkeypatch: pytest.MonkeyPatch, outcome: LockReleaseOutcome) -> tuple[Any, Any]:
        with _slack_alerts() as slack:
            day = _run_day(
                monkeypatch,
                phase="2",
                allow="true",
                post=_post(400, {"status": 1, "messages": [{"message_code": "ERR-X"}]}),
                release_outcome=outcome,
            )
        return day, slack

    def test_release_failure_alerts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        day, slack = self._rejected(monkeypatch, LockReleaseOutcome.FAILED)
        assert day.result["status"] == "failed"
        assert day.result["lock_released"] is False
        slack.assert_called_once()
        message = slack.call_args.args[0]
        assert "ロックの解放に失敗" in message
        assert "job=decision" in message
        assert f"date={TODAY.isoformat()}" in message
        assert "dynamodb:DeleteItem" in message

    def test_refused_does_not_alert(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """条件不成立（ロック不在・実 orderId 記録済み）は設計どおりの拒否。通知しない。"""
        day, slack = self._rejected(monkeypatch, LockReleaseOutcome.REFUSED)
        assert day.result["lock_released"] is False
        slack.assert_not_called()

    def test_released_does_not_alert(self, monkeypatch: pytest.MonkeyPatch) -> None:
        day, slack = self._rejected(monkeypatch, LockReleaseOutcome.RELEASED)
        assert day.result["lock_released"] is True
        slack.assert_not_called()

    def test_alert_does_not_change_the_order_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        day, _ = self._rejected(monkeypatch, LockReleaseOutcome.FAILED)
        assert day.result["status"] == "failed"  # 通知は発注結果を変えない
        day.client.private_post.assert_called_once()  # 再送もしない


class TestCloseoutMarketClosed:
    """手仕舞い: market_closed（POST を 1 本も送っていない）→ ロックを解放して同朝の再実行を許す。"""

    @staticmethod
    def _closed(outcome: LockReleaseOutcome) -> tuple[Any, Any, Any]:
        with (
            _slack_alerts() as lock_slack,
            patch("backend.src.batch.closeout_batch.send_slack_alert", return_value=True) as batch_slack,
            patch(
                "backend.src.batch.closeout_batch.dynamodb_store.release_lock_outcome",
                return_value=outcome,
            ) as release,
            patch("backend.src.batch.closeout_batch.s3_logger.save_closeout_log", return_value=True),
        ):
            response = _handle_closeout_failure(
                "market_closed",
                mode="weekday",
                today=TODAY,
                prev_trade_date=TODAY,
                ts=NOW.isoformat(),
                ts_ms=0,
            )
        assert release.call_args.kwargs["job_type"] == dynamodb_store.JOB_CLOSEOUT
        return response, lock_slack, batch_slack

    def test_release_failure_alerts_in_addition_to_the_closed_alert(self) -> None:
        response, lock_slack, batch_slack = self._closed(LockReleaseOutcome.FAILED)
        assert response["lock_released"] is False
        lock_slack.assert_called_once()
        assert "job=closeout" in lock_slack.call_args.args[0]
        batch_slack.assert_called_once()  # 従来の「手仕舞いバッチ失敗」通知は残る

    def test_refused_does_not_alert_about_release(self) -> None:
        response, lock_slack, batch_slack = self._closed(LockReleaseOutcome.REFUSED)
        assert response["lock_released"] is False
        lock_slack.assert_not_called()
        batch_slack.assert_called_once()

    def test_released_does_not_alert_about_release(self) -> None:
        response, lock_slack, _ = self._closed(LockReleaseOutcome.RELEASED)
        assert response["lock_released"] is True
        lock_slack.assert_not_called()

    def test_other_failures_do_not_try_to_release(self) -> None:
        """step1_failed / step2_failed は POST 試行済み（結果未確定）なのでロックを保持する。"""
        with (
            patch("backend.src.batch.closeout_batch.send_slack_alert", return_value=True),
            patch(
                "backend.src.batch.closeout_batch.dynamodb_store.release_lock_outcome"
            ) as release,
            patch("backend.src.batch.closeout_batch.s3_logger.save_closeout_log", return_value=True),
        ):
            response = _handle_closeout_failure(
                "step1_failed",
                mode="weekday",
                today=TODAY,
                prev_trade_date=TODAY,
                ts=NOW.isoformat(),
                ts_ms=0,
            )
        release.assert_not_called()
        assert response["lock_released"] is False

