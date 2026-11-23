"""load_decision_log_if_exists（#402）: 存在確認は ListObjectsV2、あるときだけ GetObject。

存在しないキーの GetObject は、s3:ListBucket の付き方次第で 404 でなく 403 になり得る。
この Lambda の s3:ListBucket は s3:prefix 条件付き（modules/serverless_app/iam.tf）なので、
404 になることに頼らず、Prefix=正確なキー の一覧で存在を確かめる。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from backend.src.persistence import s3_logger

DAY = date(2099, 1, 7)
KEY = "logs/decision/2099-01-07.json"


def _client(listed: list[str], body: dict[str, Any] | None = None) -> MagicMock:
    client = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Contents": [{"Key": k} for k in listed]}]
    client.get_paginator.return_value = paginator
    if body is not None:
        client.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps(body).encode("utf-8"))
        }
    return client


@pytest.fixture(autouse=True)
def _s3_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(s3_logger, "LOCAL_PERSIST_DIR", "")


def test_missing_key_returns_none_without_get_object(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client([])
    monkeypatch.setattr(s3_logger, "_boto3_client", lambda: client)
    assert s3_logger.load_decision_log_if_exists(DAY) is None
    client.get_object.assert_not_called()  # 404/403 の違いに頼らない


def test_lists_with_exact_key_prefix_within_allowed_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """s3:prefix 条件（logs/decision/*）を満たす Prefix で一覧する。"""
    client = _client([])
    monkeypatch.setattr(s3_logger, "_boto3_client", lambda: client)
    s3_logger.load_decision_log_if_exists(DAY)
    prefix = client.get_paginator.return_value.paginate.call_args.kwargs["Prefix"]
    assert prefix == KEY
    assert prefix.startswith("logs/decision/")


def test_other_key_with_same_prefix_is_not_a_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prefix 一致の別キー（例: .json.bak）を存在とみなさない。"""
    client = _client([KEY + ".bak"])
    monkeypatch.setattr(s3_logger, "_boto3_client", lambda: client)
    assert s3_logger.load_decision_log_if_exists(DAY) is None
    client.get_object.assert_not_called()


def test_existing_key_is_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client([KEY], {"status": "stubbed"})
    monkeypatch.setattr(s3_logger, "_boto3_client", lambda: client)
    assert s3_logger.load_decision_log_if_exists(DAY) == {"status": "stubbed"}
    assert client.get_object.call_args.kwargs["Key"] == KEY


def test_listing_failure_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    client.get_paginator.return_value.paginate.side_effect = Exception("AccessDenied")
    monkeypatch.setattr(s3_logger, "_boto3_client", lambda: client)
    with pytest.raises(RuntimeError):
        s3_logger.load_decision_log_if_exists(DAY)
    client.get_object.assert_not_called()


def test_read_failure_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client([KEY])
    client.get_object.side_effect = Exception("boom")
    monkeypatch.setattr(s3_logger, "_boto3_client", lambda: client)
    with pytest.raises(RuntimeError):
        s3_logger.load_decision_log_if_exists(DAY)


def test_local_mode_uses_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(s3_logger, "LOCAL_PERSIST_DIR", str(tmp_path))
    assert s3_logger.load_decision_log_if_exists(DAY) is None
    path = tmp_path / KEY
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"status": "ok"}), encoding="utf-8")
    assert s3_logger.load_decision_log_if_exists(DAY) == {"status": "ok"}
