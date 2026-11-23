"""判断済みマーカー（#404）: mark_decision_concluded / is_decision_concluded。

当日ロック（job_type="decision"）とは別のレコード（job_type="decision_concluded"）。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import botocore.exceptions
import pytest

from backend.src.persistence import dynamodb_store

pytestmark = pytest.mark.real_decision_marker

DAY = date(2099, 1, 7)


def _client_error(code: str) -> botocore.exceptions.ClientError:
    return botocore.exceptions.ClientError({"Error": {"Code": code, "Message": "x"}}, "PutItem")


class TestLocalMode:
    @pytest.fixture(autouse=True)
    def _local(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
        monkeypatch.setattr(dynamodb_store, "LOCAL_PERSIST_DIR", str(tmp_path))
        return tmp_path

    def test_not_concluded_by_default(self) -> None:
        assert dynamodb_store.is_decision_concluded(DAY) is False

    def test_mark_then_concluded(self) -> None:
        assert dynamodb_store.mark_decision_concluded(DAY, status="no_entry", reason="lot_zero")
        assert dynamodb_store.is_decision_concluded(DAY) is True

    def test_mark_is_idempotent_and_keeps_first_record(self, _local: Path) -> None:
        dynamodb_store.mark_decision_concluded(DAY, status="no_entry", reason="first")
        assert dynamodb_store.mark_decision_concluded(DAY, status="no_entry", reason="second")
        item = json.loads(
            dynamodb_store._local_path(DAY, dynamodb_store.JOB_DECISION_CONCLUDED).read_text()
        )
        assert item["reason"] == "first"

    def test_other_day_is_not_concluded(self) -> None:
        dynamodb_store.mark_decision_concluded(DAY, status="no_entry")
        assert dynamodb_store.is_decision_concluded(date(2099, 1, 8)) is False

    def test_is_separate_from_the_order_lock(self) -> None:
        """マーカーは当日ロックとは別のレコード。ロックを取っても結論済みにならず、逆も同じ。"""
        assert dynamodb_store.acquire_lock_outcome(DAY).acquired is True
        assert dynamodb_store.is_decision_concluded(DAY) is False
        dynamodb_store.mark_decision_concluded(DAY, status="no_entry")
        assert dynamodb_store.acquire_lock_outcome(DAY).lock_exists is True  # ロックはそのまま

    def test_marker_does_not_take_the_lock(self) -> None:
        dynamodb_store.mark_decision_concluded(DAY, status="no_entry")
        assert dynamodb_store.acquire_lock_outcome(DAY).acquired is True

    def test_unreadable_marker_is_fail_open(self, _local: Path) -> None:
        path = dynamodb_store._local_path(DAY, dynamodb_store.JOB_DECISION_CONCLUDED)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")
        assert dynamodb_store.is_decision_concluded(DAY) is False


class TestDynamoDbMode:
    @pytest.fixture(autouse=True)
    def _ddb_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dynamodb_store, "LOCAL_PERSIST_DIR", "")

    def test_mark_puts_conditional_item_with_marker_job_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ddb = MagicMock()
        monkeypatch.setattr(dynamodb_store, "_boto3_client", lambda: ddb)
        assert dynamodb_store.mark_decision_concluded(DAY, status="no_entry", reason="lot_zero")
        kwargs: dict[str, Any] = ddb.put_item.call_args.kwargs
        assert kwargs["Item"]["job_type"] == {"S": "decision_concluded"}  # ロックの "decision" と別
        assert kwargs["Item"]["date"] == {"S": "2099-01-07"}
        assert kwargs["Item"]["reason"] == {"S": "lot_zero"}
        assert kwargs["ConditionExpression"] == "attribute_not_exists(#d)"

    def test_already_marked_is_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ddb = MagicMock()
        ddb.put_item.side_effect = _client_error("ConditionalCheckFailedException")
        monkeypatch.setattr(dynamodb_store, "_boto3_client", lambda: ddb)
        assert dynamodb_store.mark_decision_concluded(DAY, status="no_entry") is True

    def test_write_failure_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ddb = MagicMock()
        ddb.put_item.side_effect = _client_error("AccessDeniedException")
        monkeypatch.setattr(dynamodb_store, "_boto3_client", lambda: ddb)
        assert dynamodb_store.mark_decision_concluded(DAY, status="no_entry") is False

    def test_read_uses_strongly_consistent_get(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """直前に書いたマーカーを結果整合の読み取りで見落とさない。"""
        ddb = MagicMock()
        ddb.get_item.return_value = {"Item": {"date": {"S": "2099-01-07"}}}
        monkeypatch.setattr(dynamodb_store, "_boto3_client", lambda: ddb)
        assert dynamodb_store.is_decision_concluded(DAY) is True
        kwargs = ddb.get_item.call_args.kwargs
        assert kwargs["ConsistentRead"] is True
        assert kwargs["Key"]["job_type"] == {"S": "decision_concluded"}

    def test_missing_item_is_not_concluded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ddb = MagicMock()
        ddb.get_item.return_value = {}
        monkeypatch.setattr(dynamodb_store, "_boto3_client", lambda: ddb)
        assert dynamodb_store.is_decision_concluded(DAY) is False

    def test_read_failure_is_fail_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """読めないときは False（判断を止めない）。発注にはどのみち当日ロックの獲得（DynamoDB）が要る。"""
        ddb = MagicMock()
        ddb.get_item.side_effect = _client_error("ProvisionedThroughputExceededException")
        monkeypatch.setattr(dynamodb_store, "_boto3_client", lambda: ddb)
        assert dynamodb_store.is_decision_concluded(DAY) is False
