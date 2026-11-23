"""同じ取引日の 2 回目の起動が、1 回目の判断ログを上書きしない（#402, §5.2 / §6.1）。

判断ログ（logs/decision/{date}.json）は税務証跡であり、翌朝の手仕舞い・トレードノートの
材料でもある。Scheduler の retry_attempts=2・at-least-once 配信・手動再実行で 2 回目が
起きたとき、`_skip` / no_entry の軽いログが 1 回目の発注結果のログを上書きしてはならない。

保存は本物の ``save_log``（LOCAL_PERSIST_DIR のファイル）を使い、AWS / GMO / LLM は使わない。
発注は Phase 1 のスタブ。
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from backend.src.batch import decision_batch
from backend.src.batch.decision_batch import _run, _save_decision_log
from backend.src.persistence import s3_logger
from backend.src.persistence.dynamodb_store import LockAcquireOutcome, LockReleaseOutcome
from tests.batch.test_decision_log_trading_phase import (
    _CANDLE,
    _NARROW,
    NOW,
    TODAY,
    _apply_phase,
    _calendar,
    _client,
    _llm,
    _ok_body,
    _post,
)

pytestmark = pytest.mark.real_decision_log_read


@pytest.fixture(autouse=True)
def _local_persist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """保存・読み込みともに tmp のローカルファイルを使う（AWS 資格情報を探しに行かない）。"""
    monkeypatch.setattr(s3_logger, "LOCAL_PERSIST_DIR", str(tmp_path))
    return tmp_path


def _log_path(root: Path) -> Path:
    return root / s3_logger.s3_key_decision_log(TODAY)


def _read(root: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(_log_path(root).read_text(encoding="utf-8"))
    return data


def _run_day(
    monkeypatch: pytest.MonkeyPatch,
    *,
    kind: str = "enter",
    active: list[Any] | None = None,
    lock: LockAcquireOutcome | None = None,
) -> dict[str, Any]:
    """判断バッチを 1 回走らせる（Phase 1・発注はスタブ・保存は本物）。"""
    _apply_phase(monkeypatch, "1", "")
    client = _client(active_orders=active, ticker=_NARROW, post=_post(200, _ok_body()))
    outcome = lock if lock is not None else LockAcquireOutcome(acquired=True)
    patches = [
        patch("backend.src.batch.decision_batch.time.sleep"),
        patch(
            "backend.src.batch.decision_batch.get_confirmed_daily_candles",
            return_value=[_CANDLE] * 15,
        ),
        patch("backend.src.batch.decision_batch.generate_decision", MagicMock(return_value=_llm(kind))),
        patch("backend.src.batch.decision_batch._fetch_macro_context", return_value=None),
        patch("backend.src.batch.decision_batch._fetch_economic_events", return_value=_calendar()),
        patch("backend.src.batch.decision_batch.get_split_session_ohlc", return_value=None),
        patch("backend.src.batch.decision_batch.is_us_thin_market", return_value=False),
        patch("backend.src.batch.decision_batch.send_slack_alert", return_value=True),
        patch("backend.src.batch.lock_acquire.send_slack_alert", return_value=True),
        patch(
            "backend.src.batch.decision_batch.dynamodb_store.acquire_lock_outcome",
            return_value=outcome,
        ),
        patch("backend.src.batch.decision_batch.dynamodb_store.write_order_id", return_value=True),
        patch("backend.src.batch.decision_batch.dynamodb_store.release_lock_outcome", return_value=LockReleaseOutcome.RELEASED),
        patch("backend.src.batch.decision_batch.dynamodb_store.update_summary", return_value=True),
    ]
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        return _run(client=client, today=TODAY, now_jst=NOW)


class TestSecondRunDoesNotOverwrite:
    def test_first_run_saves_stubbed_log(
        self, monkeypatch: pytest.MonkeyPatch, _local_persist: Path
    ) -> None:
        result = _run_day(monkeypatch)
        saved = _read(_local_persist)
        assert result["status"] == "stubbed"
        assert saved["status"] == "stubbed"
        assert saved["entry_price"] == pytest.approx(156.5)

    def test_second_run_lock_exists_keeps_first_log(
        self, monkeypatch: pytest.MonkeyPatch, _local_persist: Path
    ) -> None:
        """ロック既存の経路（Phase 1 の想定）。2 回目の dynamodb_lock_exists が上書きしない。"""
        _run_day(monkeypatch)
        first = _read(_local_persist)

        second = _run_day(
            monkeypatch, lock=LockAcquireOutcome(acquired=False, lock_exists=True)
        )

        assert second["status"] == "skipped"  # 2 回目の戻り値は skip のまま
        assert second["reason"] == "dynamodb_lock_exists"
        assert _read(_local_persist) == first  # 1 回目の判断ログがそのまま残る

    def test_second_run_lock_unavailable_keeps_first_log(
        self, monkeypatch: pytest.MonkeyPatch, _local_persist: Path
    ) -> None:
        _run_day(monkeypatch)
        first = _read(_local_persist)
        second = _run_day(
            monkeypatch, lock=LockAcquireOutcome(acquired=False, lock_exists=False)
        )
        assert second["reason"] == "dynamodb_lock_unavailable"
        assert _read(_local_persist) == first

    def test_second_run_active_orders_exist_keeps_first_log(
        self, monkeypatch: pytest.MonkeyPatch, _local_persist: Path
    ) -> None:
        """冪等チェックの経路（Phase 2 の想定: 1 回目の注文が残っている）。"""
        _run_day(monkeypatch)
        first = _read(_local_persist)

        second = _run_day(monkeypatch, active=[{"orderId": 1}])

        assert second["status"] == "skipped"
        assert second["reason"] == "active_orders_exist"
        assert _read(_local_persist) == first

    def test_second_run_with_unreadable_log_keeps_first_log(
        self, monkeypatch: pytest.MonkeyPatch, _local_persist: Path
    ) -> None:
        """読み込み権限が無い・S3 障害などで既存ログを読めなくても、1 回目のログを消さない。"""
        _run_day(monkeypatch)
        first = _read(_local_persist)
        with patch.object(
            decision_batch,
            "_read_existing_decision_log",
            side_effect=RuntimeError("S3 読み込み失敗: AccessDenied 相当"),
        ):
            second = _run_day(
                monkeypatch, lock=LockAcquireOutcome(acquired=False, lock_exists=True)
            )
        assert second["reason"] == "dynamodb_lock_exists"
        assert _read(_local_persist) == first

    def test_second_run_llm_no_entry_keeps_first_log(
        self, monkeypatch: pytest.MonkeyPatch, _local_persist: Path
    ) -> None:
        """2 回目の LLM が見送りと判断しても、1 回目の発注結果のログを上書きしない。"""
        _run_day(monkeypatch)
        first = _read(_local_persist)
        second = _run_day(monkeypatch, kind="no_entry")
        assert second["status"] == "no_entry"
        assert _read(_local_persist) == first

    def test_skip_does_not_overwrite_no_entry(
        self, monkeypatch: pytest.MonkeyPatch, _local_persist: Path
    ) -> None:
        _run_day(monkeypatch, kind="no_entry")
        first = _read(_local_persist)
        assert first["status"] == "no_entry"
        _run_day(monkeypatch, active=[{"orderId": 1}])
        assert _read(_local_persist) == first

    def test_retry_after_skip_overwrites_with_order_result(
        self, monkeypatch: pytest.MonkeyPatch, _local_persist: Path
    ) -> None:
        """1 回目が skip（一過性の理由）で、再試行が発注まで進んだなら、結果のログが勝つ。"""
        _run_day(monkeypatch, active=[{"orderId": 1}])
        assert _read(_local_persist)["status"] == "skipped"
        _run_day(monkeypatch)
        assert _read(_local_persist)["status"] == "stubbed"

    def test_retry_after_no_entry_overwrites_with_order_result(
        self, monkeypatch: pytest.MonkeyPatch, _local_persist: Path
    ) -> None:
        _run_day(monkeypatch, kind="no_entry")
        _run_day(monkeypatch)
        assert _read(_local_persist)["status"] == "stubbed"


class TestSaveDecisionLogRanking:
    """`_save_decision_log` の重み: 発注結果 > no_entry > skipped。"""

    @staticmethod
    def _seed(root: Path, status: str) -> None:
        s3_logger.save_decision_log({"status": status, "marker": "first"}, today=TODAY)
        assert _read(root)["status"] == status

    @pytest.mark.parametrize(
        ("existing", "new"),
        [
            ("stubbed", "skipped"),
            ("stubbed", "no_entry"),
            ("ok", "skipped"),
            ("failed", "no_entry"),
            ("persistence_failed", "skipped"),
            ("no_entry", "skipped"),
            ("something_new", "skipped"),  # 未知の status は重い側に倒す
        ],
    )
    def test_lighter_status_does_not_overwrite(
        self, _local_persist: Path, existing: str, new: str
    ) -> None:
        self._seed(_local_persist, existing)
        saved = _save_decision_log({"status": new, "marker": "second"}, today=TODAY)
        assert saved is False
        assert _read(_local_persist)["marker"] == "first"

    @pytest.mark.parametrize(
        ("existing", "new"),
        [
            ("skipped", "skipped"),  # 同じ重みは最新で上書き（最新の理由を残す）
            ("no_entry", "no_entry"),
            ("skipped", "no_entry"),
            ("skipped", "stubbed"),
            ("no_entry", "stubbed"),
            ("stubbed", "ok"),
        ],
    )
    def test_same_or_heavier_status_overwrites(
        self, _local_persist: Path, existing: str, new: str
    ) -> None:
        self._seed(_local_persist, existing)
        saved = _save_decision_log({"status": new, "marker": "second"}, today=TODAY)
        assert saved is True
        assert _read(_local_persist)["marker"] == "second"

    def test_saves_when_no_existing_log(self, _local_persist: Path) -> None:
        assert _save_decision_log({"status": "skipped", "marker": "x"}, today=TODAY) is True
        assert _read(_local_persist)["marker"] == "x"

    def test_order_outcome_is_saved_without_reading(self, _local_persist: Path) -> None:
        """発注結果の保存は既存ログを読まない（通常の発注経路に S3 読み込みを足さない）。"""
        with patch.object(decision_batch, "_read_existing_decision_log") as read:
            assert _save_decision_log({"status": "stubbed"}, today=TODAY) is True
        read.assert_not_called()

    def test_read_failure_does_not_save_and_alerts(
        self, _local_persist: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """既存ログを読めないときは、軽い status の保存をしない（fail-closed）。error ログと Slack で気づく。"""
        with (
            patch.object(
                decision_batch,
                "_read_existing_decision_log",
                side_effect=RuntimeError("S3 読み込み失敗"),
            ),
            patch.object(decision_batch, "send_slack_alert", return_value=True) as slack,
            caplog.at_level("ERROR"),
        ):
            saved = _save_decision_log({"status": "skipped", "marker": "x"}, today=TODAY)
        assert saved is False
        assert not _log_path(_local_persist).exists()
        slack.assert_called_once()
        assert any(
            r.getMessage() == "decision_log_existing_read_failed_not_saving"
            for r in caplog.records
        )

    def test_read_failure_still_saves_order_outcome(self, _local_persist: Path) -> None:
        """発注結果は読み込みをしないので、読み込みが壊れていても必ず保存される。"""
        with patch.object(
            decision_batch,
            "_read_existing_decision_log",
            side_effect=RuntimeError("S3 読み込み失敗"),
        ):
            assert _save_decision_log({"status": "stubbed", "marker": "x"}, today=TODAY) is True
        assert _read(_local_persist)["marker"] == "x"

    def test_not_overwritten_is_logged(
        self, _local_persist: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._seed(_local_persist, "stubbed")
        with caplog.at_level("WARNING"):
            _save_decision_log({"status": "skipped", "reason": "active_orders_exist"}, today=TODAY)
        record = next(r for r in caplog.records if r.getMessage() == "decision_log_not_overwritten")
        assert record.existing_status == "stubbed"  # type: ignore[attr-defined]
        assert record.new_reason == "active_orders_exist"  # type: ignore[attr-defined]
