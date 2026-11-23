"""release_lock_outcome（#406）: 解放の結果を「解放 / 条件不成立 / 障害」に区別する。

条件不成立（ロック不在・実 orderId 記録済み）は設計どおりの拒否。AccessDenied などの障害とは
別に扱い、障害だけを運用者に通知する。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import botocore.exceptions
import pytest

from backend.src.persistence import dynamodb_store
from backend.src.persistence.dynamodb_store import LockReleaseOutcome

DAY = date(2099, 1, 7)


def _client_error(code: str) -> botocore.exceptions.ClientError:
    return botocore.exceptions.ClientError({"Error": {"Code": code, "Message": "x"}}, "DeleteItem")


class TestDynamoDbMode:
    @pytest.fixture(autouse=True)
    def _ddb(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dynamodb_store, "LOCAL_PERSIST_DIR", "")

    def _client(self, monkeypatch: pytest.MonkeyPatch, error: Exception | None) -> MagicMock:
        ddb = MagicMock()
        if error is not None:
            ddb.delete_item.side_effect = error
        monkeypatch.setattr(dynamodb_store, "_boto3_client", lambda: ddb)
        return ddb

    def test_released(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ddb = self._client(monkeypatch, None)
        assert dynamodb_store.release_lock_outcome(DAY) is LockReleaseOutcome.RELEASED
        kwargs = ddb.delete_item.call_args.kwargs
        # 条件式は変えない（order_id が pending のレコードだけ削除する）
        assert kwargs["ConditionExpression"] == "attribute_exists(#d) AND order_id = :pending"
        assert kwargs["ExpressionAttributeValues"] == {":pending": {"S": "pending"}}

    def test_condition_failure_is_refused_not_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._client(monkeypatch, _client_error("ConditionalCheckFailedException"))
        assert dynamodb_store.release_lock_outcome(DAY) is LockReleaseOutcome.REFUSED

    @pytest.mark.parametrize(
        "code",
        ["AccessDeniedException", "ProvisionedThroughputExceededException", "InternalServerError"],
    )
    def test_other_errors_are_failed(self, monkeypatch: pytest.MonkeyPatch, code: str) -> None:
        """AccessDenied（DeleteItem が無い）を含む障害は FAILED。条件不成立とは区別する。"""
        self._client(monkeypatch, _client_error(code))
        assert dynamodb_store.release_lock_outcome(DAY) is LockReleaseOutcome.FAILED

    def test_non_client_error_is_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._client(monkeypatch, RuntimeError("network"))
        assert dynamodb_store.release_lock_outcome(DAY) is LockReleaseOutcome.FAILED


class TestLocalMode:
    @pytest.fixture(autouse=True)
    def _local(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
        monkeypatch.setattr(dynamodb_store, "LOCAL_PERSIST_DIR", str(tmp_path))
        return tmp_path

    def test_pending_lock_is_released(self) -> None:
        dynamodb_store.acquire_lock_outcome(DAY)
        assert dynamodb_store.release_lock_outcome(DAY) is LockReleaseOutcome.RELEASED
        assert dynamodb_store.acquire_lock_outcome(DAY).acquired is True  # 再取得できる

    def test_missing_lock_is_refused(self) -> None:
        assert dynamodb_store.release_lock_outcome(DAY) is LockReleaseOutcome.REFUSED

    def test_lock_with_real_order_id_is_refused(self) -> None:
        """実 orderId 記録済みロックは発注成功の証跡。削除すると二重発注を許すので拒否する。"""
        dynamodb_store.acquire_lock_outcome(DAY)
        dynamodb_store.write_order_id(DAY, "123456")
        assert dynamodb_store.release_lock_outcome(DAY) is LockReleaseOutcome.REFUSED
        assert dynamodb_store.acquire_lock_outcome(DAY).lock_exists is True  # ロックは残る

    def test_corrupt_payload_is_failed(self, _local: Path) -> None:
        path = dynamodb_store._local_path(DAY, dynamodb_store.JOB_DECISION)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")
        assert dynamodb_store.release_lock_outcome(DAY) is LockReleaseOutcome.FAILED

    def test_unlink_error_is_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        dynamodb_store.acquire_lock_outcome(DAY)
        original = Path.unlink

        def boom(self: Path, *a: object, **k: object) -> None:
            raise PermissionError("denied")

        monkeypatch.setattr(Path, "unlink", boom)
        assert dynamodb_store.release_lock_outcome(DAY) is LockReleaseOutcome.FAILED
        monkeypatch.setattr(Path, "unlink", original)
        assert json.loads(
            dynamodb_store._local_path(DAY, dynamodb_store.JOB_DECISION).read_text()
        )["order_id"] == "pending"
