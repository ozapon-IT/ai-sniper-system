"""pytest 共通フィクスチャ。"""

from __future__ import annotations

import os
from collections.abc import Generator
from unittest.mock import patch

import pytest

_EDGE_GUARD_TEST_CLASSES = frozenset(
    {
        "TestEdgeGuardDaySkipHardRule",
        "TestEdgeGuardDaySkipRequiredCase",
        "TestEdgeGuardPrepare",
    }
)


@pytest.fixture(autouse=True)
def _disable_edge_guards_unless_explicit(
    request: pytest.FixtureRequest,
) -> Generator[None, None, None]:
    """エッジガード day-skip 専用テスト以外では G1〜G3 を無効化する。

    既存統合テストの足・スプレッドは G2/G3 に該当し得るため、
    RD-8 専用クラスと day_skip_guards ユニットテストのみ実閾値を使う。
    """
    module_name = request.node.module.__name__ if request.node.module else ""
    if module_name.endswith("test_day_skip_guards"):
        yield
        return
    cls_name = request.node.cls.__name__ if request.node.cls else ""
    if cls_name in _EDGE_GUARD_TEST_CLASSES:
        yield
        return
    with patch.dict(
        os.environ,
        {
            "MAX_DECISION_SPREAD_PIPS": "99",
            "MAX_ATR_DAY_SKIP_PIPS": "9999",
            "MIN_SESSION_MOMENTUM_ATR": "0",
        },
        clear=False,
    ):
        yield


@pytest.fixture(autouse=True)
def _no_real_existing_decision_log_read(
    request: pytest.FixtureRequest,
) -> Generator[None, None, None]:
    """判断バッチの「既存の判断ログの読み込み」（#402）を既定でスタブにする（なし=None）。

    判断ログの保存だけをモックしている既存テストで、実 boto3 の get_object に行くと
    資格情報の探索で 1 件あたり秒単位に遅くなる。読み込みの挙動そのものを検証する
    テスト（test_decision_log_no_overwrite）だけは実関数を使う。
    """
    if request.node.get_closest_marker("real_decision_log_read"):
        yield
        return
    with patch("backend.src.batch.decision_batch._read_existing_decision_log", return_value=None):
        yield


@pytest.fixture(autouse=True)
def _no_real_decision_concluded_marker(
    request: pytest.FixtureRequest,
) -> Generator[None, None, None]:
    """判断バッチの「判断済みマーカー」（#404）の DynamoDB 読み書きを既定でスタブにする。

    マーカー無し（False）・書き込み成功（True）。スタブにしないと、判断バッチを走らせる既存
    テストが実 boto3 の DynamoDB 呼び出しに行き、資格情報の探索で秒単位に遅くなる。
    実関数を使うテストは `@pytest.mark.real_decision_marker`。
    """
    if request.node.get_closest_marker("real_decision_marker"):
        yield
        return
    with (
        patch(
            "backend.src.persistence.dynamodb_store.is_decision_concluded",
            return_value=False,
        ),
        patch(
            "backend.src.persistence.dynamodb_store.mark_decision_concluded",
            return_value=True,
        ),
    ):
        yield
