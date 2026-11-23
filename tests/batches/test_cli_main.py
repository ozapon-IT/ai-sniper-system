"""CLI エントリポイント `python -m ai_sniper.batches` の回帰テスト。"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from ai_sniper.batches.__main__ import main


class TestBatchesCliDecision:
    def test_decision_dispatches_lambda_handler(self) -> None:
        with (
            patch(
                "backend.src.batch.decision_batch.lambda_handler",
                return_value={"status": "skipped"},
            ) as mock_handler,
            patch("sys.argv", ["batches", "decision"]),
            pytest.raises(SystemExit) as exc,
        ):
            main()
        mock_handler.assert_called_once_with({}, None)
        assert exc.value.code == 0

    def test_removed_morning_alias_rejected(self) -> None:
        """morning エイリアスは削除済み（argparse エラー・SystemExit 2）。"""
        with (
            patch("sys.argv", ["batches", "morning"]),
            pytest.raises(SystemExit) as exc,
        ):
            main()
        assert exc.value.code == 2


class TestBatchesCliCloseout:
    def test_closeout_dispatches_run(self) -> None:
        with (
            patch(
                "backend.src.batch.closeout_batch._run",
                return_value={"status": "ok"},
            ) as mock_run,
            patch("sys.argv", ["batches", "closeout", "--mode", "weekday"]),
            pytest.raises(SystemExit) as exc,
        ):
            main()
        mock_run.assert_called_once_with(mode="weekday")
        assert exc.value.code == 0

    def test_removed_evening_alias_rejected(self) -> None:
        """evening エイリアスは削除済み（argparse エラー・SystemExit 2）。"""
        with (
            patch("sys.argv", ["batches", "evening", "--mode", "weekday"]),
            pytest.raises(SystemExit) as exc,
        ):
            main()
        assert exc.value.code == 2
