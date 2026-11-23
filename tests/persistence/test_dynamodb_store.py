"""tests/persistence/test_dynamodb_store.py — DynamoDB 冪等ロック・orderId のテスト（§1.5）。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import botocore.exceptions
import pytest

from backend.src.persistence import dynamodb_store
from backend.src.persistence.dynamodb_store import LockReleaseOutcome

TODAY = date(2025, 6, 28)


def _conditional_check_failed() -> Exception:
    """実際の boto3 が投げる ClientError（error code 判定）を再現する。"""
    return botocore.exceptions.ClientError(
        error_response={
            "Error": {
                "Code": "ConditionalCheckFailedException",
                "Message": "The conditional request failed",
            }
        },
        operation_name="PutItem",
    )


# ---------------------------------------------------------------------------
# ファイル fallback — acquire_lock
# ---------------------------------------------------------------------------


def test_acquire_lock_file_fallback_first_call(tmp_path, monkeypatch):
    monkeypatch.setattr(dynamodb_store, "LOCAL_PERSIST_DIR", str(tmp_path))

    result = dynamodb_store.acquire_lock(TODAY, job_type="decision")

    assert result is True
    record_path = tmp_path / "dynamodb" / "2025-06-28-decision.json"
    assert record_path.exists()
    data = json.loads(record_path.read_text())
    assert data["order_id"] == "pending"
    assert data["job_type"] == "decision"


def test_acquire_lock_file_fallback_duplicate_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr(dynamodb_store, "LOCAL_PERSIST_DIR", str(tmp_path))

    assert dynamodb_store.acquire_lock(TODAY, job_type="decision") is True
    assert dynamodb_store.acquire_lock(TODAY, job_type="decision") is False


def test_acquire_lock_different_job_type(tmp_path, monkeypatch):
    monkeypatch.setattr(dynamodb_store, "LOCAL_PERSIST_DIR", str(tmp_path))

    assert dynamodb_store.acquire_lock(TODAY, job_type="decision") is True
    assert dynamodb_store.acquire_lock(TODAY, job_type="closeout") is True


def test_acquire_lock_outcome_file_fallback_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dynamodb_store, "LOCAL_PERSIST_DIR", str(tmp_path))

    outcome = dynamodb_store.acquire_lock_outcome(TODAY, job_type="decision")

    assert outcome == dynamodb_store.LockAcquireOutcome(acquired=True, lock_exists=False)


def test_acquire_lock_outcome_file_fallback_duplicate_returns_lock_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dynamodb_store, "LOCAL_PERSIST_DIR", str(tmp_path))

    assert dynamodb_store.acquire_lock_outcome(TODAY, job_type="decision") == (
        dynamodb_store.LockAcquireOutcome(acquired=True, lock_exists=False)
    )
    outcome = dynamodb_store.acquire_lock_outcome(TODAY, job_type="decision")

    assert outcome == dynamodb_store.LockAcquireOutcome(acquired=False, lock_exists=True)


# ---------------------------------------------------------------------------
# ファイル fallback — write_order_id / get_order_id
# ---------------------------------------------------------------------------


def test_write_and_get_order_id_file_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(dynamodb_store, "LOCAL_PERSIST_DIR", str(tmp_path))

    dynamodb_store.acquire_lock(TODAY, job_type="decision")
    dynamodb_store.write_order_id(TODAY, order_id="ORD-12345", job_type="decision")

    retrieved = dynamodb_store.get_order_id(TODAY, job_type="decision")
    assert retrieved == "ORD-12345"


def test_write_order_id_without_lock_returns_false(tmp_path, monkeypatch):
    """ロック未取得時は write_order_id が False を返す。"""
    monkeypatch.setattr(dynamodb_store, "LOCAL_PERSIST_DIR", str(tmp_path))

    result = dynamodb_store.write_order_id(TODAY, order_id="ORD-99", job_type="decision")
    assert result is False


def test_get_order_id_returns_none_for_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(dynamodb_store, "LOCAL_PERSIST_DIR", str(tmp_path))

    dynamodb_store.acquire_lock(TODAY, job_type="decision")
    # pending のままだと None
    assert dynamodb_store.get_order_id(TODAY, job_type="decision") is None


def test_get_order_id_returns_none_when_no_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(dynamodb_store, "LOCAL_PERSIST_DIR", str(tmp_path))

    assert dynamodb_store.get_order_id(TODAY, job_type="decision") is None


# ---------------------------------------------------------------------------
# DynamoDB モック — acquire_lock
# ---------------------------------------------------------------------------


def test_acquire_lock_dynamodb_success(monkeypatch):
    monkeypatch.setattr(dynamodb_store, "LOCAL_PERSIST_DIR", "")
    mock_ddb = MagicMock()
    with patch.object(dynamodb_store, "_boto3_client", return_value=mock_ddb):
        result = dynamodb_store.acquire_lock(TODAY, job_type="decision")

    assert result is True
    mock_ddb.put_item.assert_called_once()
    call_kwargs = mock_ddb.put_item.call_args[1]
    assert call_kwargs["Item"]["date"]["S"] == "2025-06-28"
    assert "ConditionExpression" in call_kwargs
    assert call_kwargs["Item"]["job_type"]["S"] == "decision"
    assert call_kwargs["Item"]["order_id"]["S"] == "pending"


def test_acquire_lock_dynamodb_conditional_check_failure(monkeypatch):
    monkeypatch.setattr(dynamodb_store, "LOCAL_PERSIST_DIR", "")
    mock_ddb = MagicMock()

    mock_ddb.put_item.side_effect = _conditional_check_failed()
    with patch.object(dynamodb_store, "_boto3_client", return_value=mock_ddb):
        result = dynamodb_store.acquire_lock(TODAY, job_type="decision")

    assert result is False


def test_acquire_lock_outcome_dynamodb_conditional_check_failure(monkeypatch):
    monkeypatch.setattr(dynamodb_store, "LOCAL_PERSIST_DIR", "")
    mock_ddb = MagicMock()
    mock_ddb.put_item.side_effect = _conditional_check_failed()
    with patch.object(dynamodb_store, "_boto3_client", return_value=mock_ddb):
        outcome = dynamodb_store.acquire_lock_outcome(TODAY, job_type="decision")

    assert outcome == dynamodb_store.LockAcquireOutcome(acquired=False, lock_exists=True)


def test_acquire_lock_outcome_dynamodb_backend_failure(monkeypatch):
    monkeypatch.setattr(dynamodb_store, "LOCAL_PERSIST_DIR", "")
    mock_ddb = MagicMock()
    mock_ddb.put_item.side_effect = RuntimeError("ddb unavailable")
    with patch.object(dynamodb_store, "_boto3_client", return_value=mock_ddb):
        outcome = dynamodb_store.acquire_lock_outcome(TODAY, job_type="decision")

    assert outcome == dynamodb_store.LockAcquireOutcome(acquired=False, lock_exists=False)


# ---------------------------------------------------------------------------
# DynamoDB モック — write_order_id
# ---------------------------------------------------------------------------


def test_write_order_id_dynamodb(monkeypatch):
    monkeypatch.setattr(dynamodb_store, "LOCAL_PERSIST_DIR", "")
    mock_ddb = MagicMock()
    with patch.object(dynamodb_store, "_boto3_client", return_value=mock_ddb):
        result = dynamodb_store.write_order_id(TODAY, order_id="ORD-99", job_type="decision")

    assert result is True
    mock_ddb.update_item.assert_called_once()
    call_kwargs = mock_ddb.update_item.call_args[1]
    assert call_kwargs["ExpressionAttributeValues"][":oid"]["S"] == "ORD-99"
    assert "ConditionExpression" in call_kwargs


def test_write_order_id_dynamodb_no_lock_returns_false(monkeypatch):
    """DynamoDB 側でロックなし（ConditionalCheckFailed）の場合 False を返す。"""
    monkeypatch.setattr(dynamodb_store, "LOCAL_PERSIST_DIR", "")
    mock_ddb = MagicMock()

    mock_ddb.update_item.side_effect = _conditional_check_failed()
    with patch.object(dynamodb_store, "_boto3_client", return_value=mock_ddb):
        result = dynamodb_store.write_order_id(TODAY, order_id="ORD-99", job_type="decision")

    assert result is False


# ---------------------------------------------------------------------------
# DynamoDB モック — get_order_id
# ---------------------------------------------------------------------------


def test_get_order_id_dynamodb_success(monkeypatch):
    monkeypatch.setattr(dynamodb_store, "LOCAL_PERSIST_DIR", "")
    mock_ddb = MagicMock()
    mock_ddb.get_item.return_value = {
        "Item": {
            "date": {"S": "2025-06-28"},
            "job_type": {"S": "decision"},
            "order_id": {"S": "ORD-777"},
        }
    }
    with patch.object(dynamodb_store, "_boto3_client", return_value=mock_ddb):
        result = dynamodb_store.get_order_id(TODAY, job_type="decision")

    assert result == "ORD-777"
    call_kwargs = mock_ddb.get_item.call_args[1]
    assert call_kwargs["Key"]["date"]["S"] == "2025-06-28"
    assert call_kwargs["Key"]["job_type"]["S"] == "decision"


def test_get_order_id_dynamodb_pending_returns_none(monkeypatch):
    monkeypatch.setattr(dynamodb_store, "LOCAL_PERSIST_DIR", "")
    mock_ddb = MagicMock()
    mock_ddb.get_item.return_value = {
        "Item": {"date": {"S": "2025-06-28"}, "order_id": {"S": "pending"}}
    }
    with patch.object(dynamodb_store, "_boto3_client", return_value=mock_ddb):
        result = dynamodb_store.get_order_id(TODAY)

    assert result is None


# ---------------------------------------------------------------------------
# DynamoDB モック — update_summary
# ---------------------------------------------------------------------------


def test_update_summary_dynamodb(monkeypatch):
    monkeypatch.setattr(dynamodb_store, "LOCAL_PERSIST_DIR", "")
    mock_ddb = MagicMock()
    with patch.object(dynamodb_store, "_boto3_client", return_value=mock_ddb):
        result = dynamodb_store.update_summary(TODAY, {"status": "ok", "pnl": "100"})

    assert result is True
    mock_ddb.update_item.assert_called_once()
    call_kwargs = mock_ddb.update_item.call_args[1]
    assert call_kwargs["Key"]["date"]["S"] == "2025-06-28"
    # attr_values に status と pnl が含まれること
    attr_vals = call_kwargs["ExpressionAttributeValues"]
    values = {v["S"] for v in attr_vals.values()}
    assert "ok" in values
    assert "100" in values


# ---------------------------------------------------------------------------
# 冪等ロック — 朝バッチ 2 回目実行スキップの統合検証
# ---------------------------------------------------------------------------


def test_idempotency_lock_prevents_second_run(tmp_path, monkeypatch):
    """同一 YYYY-MM-DD + morning で 2 回目実行時に冪等ロックにより発注スキップ（受け入れ条件）。"""
    monkeypatch.setattr(dynamodb_store, "LOCAL_PERSIST_DIR", str(tmp_path))

    first = dynamodb_store.acquire_lock(TODAY, job_type="decision")
    second = dynamodb_store.acquire_lock(TODAY, job_type="decision")

    assert first is True
    assert second is False  # 2 回目はスキップ


# ---------------------------------------------------------------------------
# update_summary — ファイル fallback
# ---------------------------------------------------------------------------


def test_update_summary_file_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(dynamodb_store, "LOCAL_PERSIST_DIR", str(tmp_path))

    dynamodb_store.acquire_lock(TODAY, job_type="decision")
    result = dynamodb_store.update_summary(
        TODAY, {"status": "ok", "pnl": "100"}, job_type="decision"
    )

    assert result is True
    data = json.loads((tmp_path / "dynamodb" / "2025-06-28-decision.json").read_text())
    assert data["status"] == "ok"
    assert data["pnl"] == "100"


# ---------------------------------------------------------------------------
# release_lock_outcome — ファイル fallback（#138・#406）
# ---------------------------------------------------------------------------


def test_release_lock_pending_allows_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未約定確実の失敗後に解放でき、同日の再獲得（再試行）が可能になる。"""
    monkeypatch.setattr(dynamodb_store, "LOCAL_PERSIST_DIR", str(tmp_path))

    assert dynamodb_store.acquire_lock(TODAY, job_type="decision") is True
    assert dynamodb_store.release_lock_outcome(TODAY, job_type="decision") is LockReleaseOutcome.RELEASED
    assert not (tmp_path / "dynamodb" / "2025-06-28-decision.json").exists()
    # 再試行: ロックを再獲得できる
    assert dynamodb_store.acquire_lock(TODAY, job_type="decision") is True


def test_release_lock_refused_when_order_id_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """実 orderId 記録済みロックは発注成功の証跡であり削除を拒否する（二重発注防止）。"""
    monkeypatch.setattr(dynamodb_store, "LOCAL_PERSIST_DIR", str(tmp_path))

    dynamodb_store.acquire_lock(TODAY, job_type="decision")
    dynamodb_store.write_order_id(TODAY, order_id="ORD-1", job_type="decision")

    assert dynamodb_store.release_lock_outcome(TODAY, job_type="decision") is LockReleaseOutcome.REFUSED
    assert (tmp_path / "dynamodb" / "2025-06-28-decision.json").exists()
    # ロックは残るため再獲得も不可（二重発注されない）
    assert dynamodb_store.acquire_lock(TODAY, job_type="decision") is False


def test_release_lock_without_lock_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dynamodb_store, "LOCAL_PERSIST_DIR", str(tmp_path))

    assert dynamodb_store.release_lock_outcome(TODAY, job_type="decision") is LockReleaseOutcome.REFUSED


def test_release_lock_local_race_unlink_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """read-check-act の間に別プロセスがファイルを削除しても例外を伝播させず FAILED。"""
    monkeypatch.setattr(dynamodb_store, "LOCAL_PERSIST_DIR", str(tmp_path))
    # チェック通過後・unlink 直前に別プロセスが消した状況: 読み取りは pending を返すが実体なし
    monkeypatch.setattr(
        dynamodb_store, "_local_read", lambda today, job_type: {"order_id": "pending"}
    )

    assert dynamodb_store.release_lock_outcome(TODAY, job_type="decision") is LockReleaseOutcome.FAILED


def test_release_lock_local_read_oserror_is_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """exists() 後の読み取り I/O 例外（レース・権限）でも例外を伝播させず FAILED（fail-safe）。"""
    monkeypatch.setattr(dynamodb_store, "LOCAL_PERSIST_DIR", str(tmp_path))

    def _raise_oserror(today: object, job_type: object) -> None:
        raise PermissionError("read denied")

    monkeypatch.setattr(dynamodb_store, "_local_read", _raise_oserror)
    assert dynamodb_store.release_lock_outcome(TODAY, job_type="decision") is LockReleaseOutcome.FAILED


def test_release_lock_local_invalid_payload_is_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """破損 JSON（dict 以外・パース不能）でも例外を伝播させず FAILED（fail-safe）。"""
    monkeypatch.setattr(dynamodb_store, "LOCAL_PERSIST_DIR", str(tmp_path))
    lock_path = tmp_path / "dynamodb" / "2025-06-28-decision.json"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("[not a dict]", encoding="utf-8")

    assert dynamodb_store.release_lock_outcome(TODAY, job_type="decision") is LockReleaseOutcome.FAILED
    assert lock_path.exists()  # ロックは保持される


# ---------------------------------------------------------------------------
# release_lock_outcome — DynamoDB モック（#138・#406）
# ---------------------------------------------------------------------------


def test_release_lock_dynamodb_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """delete_item が order_id=pending 限定の ConditionExpression 付きで呼ばれる。"""
    monkeypatch.setattr(dynamodb_store, "LOCAL_PERSIST_DIR", "")
    mock_ddb = MagicMock()
    with patch.object(dynamodb_store, "_boto3_client", return_value=mock_ddb):
        result = dynamodb_store.release_lock_outcome(TODAY, job_type="decision")

    assert result is LockReleaseOutcome.RELEASED
    mock_ddb.delete_item.assert_called_once()
    call_kwargs = mock_ddb.delete_item.call_args[1]
    assert call_kwargs["Key"]["date"]["S"] == "2025-06-28"
    assert call_kwargs["Key"]["job_type"]["S"] == "decision"
    assert "order_id = :pending" in call_kwargs["ConditionExpression"]
    assert call_kwargs["ExpressionAttributeValues"][":pending"]["S"] == "pending"


def test_release_lock_dynamodb_conditional_check_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """orderId 記録済み・ロック不在（条件不成立）は削除されず REFUSED。"""
    monkeypatch.setattr(dynamodb_store, "LOCAL_PERSIST_DIR", "")
    mock_ddb = MagicMock()

    mock_ddb.delete_item.side_effect = _conditional_check_failed()
    with patch.object(dynamodb_store, "_boto3_client", return_value=mock_ddb):
        result = dynamodb_store.release_lock_outcome(TODAY, job_type="decision")

    assert result is LockReleaseOutcome.REFUSED


def test_release_lock_dynamodb_api_error_is_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dynamodb_store, "LOCAL_PERSIST_DIR", "")
    mock_ddb = MagicMock()
    mock_ddb.delete_item.side_effect = RuntimeError("network down")
    with patch.object(dynamodb_store, "_boto3_client", return_value=mock_ddb):
        result = dynamodb_store.release_lock_outcome(TODAY, job_type="decision")

    assert result is LockReleaseOutcome.FAILED


def test_job_type_constants():
    assert dynamodb_store.JOB_DECISION == "decision"
    assert dynamodb_store.JOB_CLOSEOUT == "closeout"


def test_default_table_name_constant_in_source():
    import pathlib

    src = pathlib.Path(dynamodb_store.__file__).read_text(encoding="utf-8")
    assert 'os.environ.get("DYNAMODB_TABLE", "your-job-lock-table")' in src
