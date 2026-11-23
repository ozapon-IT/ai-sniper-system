"""lock_acquire 共通ヘルパーのユニットテスト。"""

from __future__ import annotations

from unittest.mock import patch

from backend.src.batch.lock_acquire import (
    LockFailureKind,
    classify_lock_failure,
    notify_lock_unavailable,
)
from backend.src.persistence.dynamodb_store import LockAcquireOutcome


def test_classify_lock_failure_acquired() -> None:
    assert classify_lock_failure(LockAcquireOutcome(acquired=True)) is None


def test_classify_lock_failure_lock_exists() -> None:
    outcome = LockAcquireOutcome(acquired=False, lock_exists=True)
    assert classify_lock_failure(outcome) == LockFailureKind.LOCK_EXISTS


def test_classify_lock_failure_unavailable() -> None:
    outcome = LockAcquireOutcome(acquired=False, lock_exists=False)
    assert classify_lock_failure(outcome) == LockFailureKind.UNAVAILABLE


def test_notify_lock_unavailable() -> None:
    with (
        patch("backend.src.batch.lock_acquire.logger") as mock_logger,
        patch("backend.src.batch.lock_acquire.send_slack_alert") as mock_slack,
    ):
        notify_lock_unavailable(
            log_event="test_lock_unavailable",
            log_extra={"date": "2026-07-10"},
            slack_message="test alert",
        )
    mock_logger.error.assert_called_once_with(
        "test_lock_unavailable",
        extra={"date": "2026-07-10"},
    )
    mock_slack.assert_called_once_with("test alert")
