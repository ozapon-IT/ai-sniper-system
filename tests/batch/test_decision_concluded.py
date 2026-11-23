"""判断済みマーカー（#404）: 見送りの日は、同じ日の 2 回目の起動が判断をやり直さない。

見送り（no_entry）の経路は当日ロックを取らない（発注に進まないため）。そのため従来は、
同じ日の 2 回目の起動が冪等チェックを通って LLM を呼び直し、今度は発注と判断すれば
発注まで進み得た。結論（見送りを含む）が出た日はマーカーを書き、2 回目は見送る。

保存は本物の ``save_log`` / ``dynamodb_store``（LOCAL_PERSIST_DIR のファイル）を使う。
AWS / GMO / LLM は使わない。発注は Phase 1 のスタブ。
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from backend.src.adapters.economic_calendar import CalendarResult
from backend.src.batch.decision_batch import _run, lambda_handler
from backend.src.llm.client import generate_decision as real_generate_decision
from backend.src.persistence import dynamodb_store, s3_logger
from backend.src.persistence.dynamodb_store import LockAcquireOutcome, LockReleaseOutcome
from tests.batch.test_decision_log_trading_phase import (
    _CANDLE,
    _NARROW,
    _WIDE,
    NOW,
    TODAY,
    _apply_phase,
    _calendar,
    _client,
    _llm,
    _ok_body,
    _post,
)

pytestmark = [pytest.mark.real_decision_marker, pytest.mark.real_decision_log_read]


@pytest.fixture(autouse=True)
def _local_persist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(s3_logger, "LOCAL_PERSIST_DIR", str(tmp_path))
    monkeypatch.setattr(dynamodb_store, "LOCAL_PERSIST_DIR", str(tmp_path))
    return tmp_path


class _Day:
    """1 回の判断バッチ実行の結果と、副作用の観測点。"""

    def __init__(self, result: dict[str, Any], llm: MagicMock, client: MagicMock, lock: MagicMock):
        self.result = result
        self.llm = llm
        self.client = client
        self.lock = lock


def _run_day(
    monkeypatch: pytest.MonkeyPatch,
    *,
    kind: str = "enter",
    active: list[Any] | None = None,
    ticker: list[dict[str, str]] = _NARROW,
    calendar: tuple[list[Any], CalendarResult] | None = None,
    spread_limit: str | None = None,
    lock: LockAcquireOutcome | None = None,
    stop_loss_error: bool = False,
    lot_zero: bool = False,
    force_redecide: bool = False,
    llm_override: Any = None,
    phase: str = "1",
    allow: str = "",
    post: MagicMock | None = None,
    release_outcome: LockReleaseOutcome = LockReleaseOutcome.RELEASED,
) -> _Day:
    _apply_phase(monkeypatch, phase, allow, spread_limit=spread_limit)
    client = _client(
        active_orders=active, ticker=ticker, post=post if post is not None else _post(200, _ok_body())
    )
    llm = llm_override if llm_override is not None else MagicMock(return_value=_llm(kind))
    acquire = MagicMock(return_value=lock if lock is not None else LockAcquireOutcome(acquired=True))
    events = calendar if calendar is not None else _calendar()
    patches = [
        patch("backend.src.batch.decision_batch.time.sleep"),
        patch(
            "backend.src.batch.decision_batch.get_confirmed_daily_candles",
            return_value=[_CANDLE] * 15,
        ),
        patch("backend.src.batch.decision_batch.generate_decision", llm),
        patch("backend.src.batch.decision_batch._fetch_macro_context", return_value=None),
        patch("backend.src.batch.decision_batch._fetch_economic_events", return_value=events),
        patch("backend.src.batch.decision_batch.get_split_session_ohlc", return_value=None),
        patch("backend.src.batch.decision_batch.is_us_thin_market", return_value=False),
        patch("backend.src.batch.decision_batch.send_slack_alert", return_value=True),
        patch("backend.src.batch.decision_batch.dynamodb_store.acquire_lock_outcome", acquire),
        patch("backend.src.batch.decision_batch.dynamodb_store.write_order_id", return_value=True),
        patch("backend.src.batch.decision_batch.dynamodb_store.release_lock_outcome", return_value=release_outcome),
        patch("backend.src.batch.decision_batch.dynamodb_store.update_summary", return_value=True),
    ]
    if stop_loss_error:
        patches.append(
            patch(
                "backend.src.batch.decision_batch.calculate_stop_loss",
                side_effect=ValueError("atr"),
            )
        )
    if lot_zero:
        patches.append(patch("backend.src.batch.decision_batch.resolve_order_size", return_value=0))
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        result = _run(client=client, today=TODAY, now_jst=NOW, force_redecide=force_redecide)
    return _Day(result, llm, client, acquire)


def _decision_log(root: Path) -> dict[str, Any]:
    path = root / s3_logger.s3_key_decision_log(TODAY)
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _concluded() -> bool:
    return dynamodb_store.is_decision_concluded(TODAY)


# (名前, _run_day の引数, 期待する reason)。いずれも「結論が出た」見送り。
_CONCLUSIVE: list[tuple[str, dict[str, Any], str | None]] = [
    ("llm_no_entry", {"kind": "no_entry"}, None),
    ("indicator_day_skip", {"calendar": _calendar("21:30")}, "indicator_day_skip"),
    ("edge_guard", {"ticker": _WIDE, "spread_limit": "1.0"}, "wide_spread_day_skip"),
    ("validation_failed", {"kind": "far"}, "validation_failed"),
    ("missing_order_type", {"kind": "drop_order_type"}, "missing_order_type"),
    ("sl_tp_calculation_failed", {"stop_loss_error": True}, "sl_tp_calculation_failed"),
    ("lot_zero", {"lot_zero": True}, "lot_zero"),
]


class TestConclusivePathsWriteTheMarker:
    @pytest.mark.parametrize(("name", "kwargs", "reason"), _CONCLUSIVE, ids=[c[0] for c in _CONCLUSIVE])
    def test_marker_written(
        self,
        monkeypatch: pytest.MonkeyPatch,
        name: str,
        kwargs: dict[str, Any],
        reason: str | None,
    ) -> None:
        day = _run_day(monkeypatch, **kwargs)
        assert day.result["status"] == "no_entry", name
        if reason is not None:
            assert day.result["reason"] == reason
        assert _concluded() is True

    @pytest.mark.parametrize(("name", "kwargs", "reason"), _CONCLUSIVE, ids=[c[0] for c in _CONCLUSIVE])
    def test_second_run_does_not_decide_again(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _local_persist: Path,
        name: str,
        kwargs: dict[str, Any],
        reason: str | None,
    ) -> None:
        """2 回目は LLM を呼ばず・発注せず・ロックも取らず、1 回目の判断ログを残す。"""
        _run_day(monkeypatch, **kwargs)
        first_log = _decision_log(_local_persist)

        second = _run_day(monkeypatch, kind="enter")  # 2 回目は発注と判断し得る状況

        assert second.result["status"] == "skipped"
        assert second.result["reason"] == "decision_already_concluded"
        second.llm.assert_not_called()
        second.lock.assert_not_called()
        second.client.private_post.assert_not_called()
        assert _decision_log(_local_persist) == first_log  # 1 回目の見送りのログが残る


class TestTransientOutcomesAreNotConcluded:
    """回復し得る失敗は「結論」にしない。再起動で判断できる。"""

    def test_skipped_paths_do_not_mark(self, monkeypatch: pytest.MonkeyPatch) -> None:
        day = _run_day(monkeypatch, active=[{"orderId": 1}])  # active_orders_exist
        assert day.result["reason"] == "active_orders_exist"
        assert _concluded() is False

    def test_retry_after_transient_skip_can_still_decide(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _run_day(monkeypatch, active=[{"orderId": 1}])
        second = _run_day(monkeypatch)
        assert second.result["status"] == "stubbed"
        second.llm.assert_called_once()

    def test_calendar_degraded_does_not_mark(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """カレンダー取得失敗の fail-safe 見送りは、再起動で回復し得るので結論にしない。"""
        day = _run_day(monkeypatch, calendar=([], CalendarResult(events=[], source_ok=False)))
        assert day.result["status"] == "no_entry"
        assert day.result["reason"] == "calendar_degraded"
        assert _concluded() is False

    def test_retry_after_calendar_degraded_can_still_decide(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _run_day(monkeypatch, calendar=([], CalendarResult(events=[], source_ok=False)))
        second = _run_day(monkeypatch)
        assert second.result["status"] == "stubbed"

    def test_lock_skip_does_not_mark(self, monkeypatch: pytest.MonkeyPatch) -> None:
        day = _run_day(monkeypatch, lock=LockAcquireOutcome(acquired=False, lock_exists=True))
        assert day.result["reason"] == "dynamodb_lock_exists"
        assert _concluded() is False

    def test_order_path_does_not_mark(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """発注に進んだ日は当日ロックが担う（マーカーは見送りの結論のためのもの）。"""
        day = _run_day(monkeypatch)
        assert day.result["status"] == "stubbed"
        assert _concluded() is False


class TestMarkerFailureAndRedo:
    def test_marker_write_failure_keeps_the_result_and_logs_error(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            patch(
                "backend.src.batch.decision_batch.dynamodb_store.mark_decision_concluded",
                return_value=False,
            ),
            caplog.at_level("ERROR"),
        ):
            day = _run_day(monkeypatch, kind="no_entry")
        assert day.result["status"] == "no_entry"  # 判断結果は変えない
        assert any(r.getMessage() == "decision_concluded_marker_write_failed" for r in caplog.records)
        assert _concluded() is False  # 記録できなければ従来どおり再起動で判断をやり直し得る

    def test_marker_read_failure_is_fail_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """マーカーを読めないときは判断を止めない（発注には当日ロックの獲得が別途要る）。"""
        _run_day(monkeypatch, kind="no_entry")
        with patch.object(dynamodb_store, "_local_read", side_effect=OSError("io")):
            second = _run_day(monkeypatch)
        assert second.result["status"] == "stubbed"


# ---------------------------------------------------------------------------
# LLM が判断を返せなかった（#404 レビュー）: 結論ではないのでマーカーを書かない
# ---------------------------------------------------------------------------


class _Msgs:
    def __init__(self, behaviour: str) -> None:
        self.behaviour = behaviour

    def parse(self, **kwargs: object) -> object:
        if self.behaviour == "call_failed":
            raise RuntimeError("529 overloaded 相当の一時障害")
        resp = MagicMock()
        resp.usage = MagicMock(input_tokens=10, output_tokens=5)
        resp.stop_reason = "refusal" if self.behaviour == "refusal" else "end_turn"
        resp.parsed_output = None  # refusal でも parse 失敗でも判断は無い
        return resp


def _real_llm_returning(behaviour: str) -> MagicMock:
    """本物の generate_decision に、指定の挙動のフェイククライアントを渡す。"""
    fake = MagicMock()
    fake.messages = _Msgs(behaviour)
    return MagicMock(side_effect=lambda ctx: real_generate_decision(ctx, client=fake))


class TestLlmFallbackIsNotConcluded:
    @pytest.mark.parametrize("behaviour", ["call_failed", "refusal", "parse_failed"])
    def test_no_marker_and_fallback_is_logged(
        self, monkeypatch: pytest.MonkeyPatch, _local_persist: Path, behaviour: str
    ) -> None:
        day = _run_day(monkeypatch, llm_override=_real_llm_returning(behaviour))
        assert day.result["status"] == "no_entry"
        assert _concluded() is False  # 判断できなかっただけ。再起動で回復し得る
        assert _decision_log(_local_persist)["llm_fallback"] == behaviour  # rationale の文言に頼らない

    @pytest.mark.parametrize("behaviour", ["call_failed", "refusal", "parse_failed"])
    def test_restart_can_decide_again_after_llm_recovers(
        self, monkeypatch: pytest.MonkeyPatch, behaviour: str
    ) -> None:
        """LLM が復旧していれば、2 回目の起動で判断し直せる（Runbook §2.2 の手動 invoke）。"""
        _run_day(monkeypatch, llm_override=_real_llm_returning(behaviour))
        second = _run_day(monkeypatch)  # 復旧: 通常の判断（発注）
        assert second.result["status"] == "stubbed"
        second.llm.assert_called_once()

    def test_real_llm_no_entry_has_no_fallback_and_marks(
        self, monkeypatch: pytest.MonkeyPatch, _local_persist: Path
    ) -> None:
        """LLM が実際に見送りと判断したときは fallback なし・結論としてマーカーを書く。"""
        _run_day(monkeypatch, kind="no_entry")
        assert _decision_log(_local_persist)["llm_fallback"] is None
        assert _concluded() is True


# ---------------------------------------------------------------------------
# 手動のやり直し: invoke の payload {"force_redecide": true}（マーカーの確認だけを飛ばす）
# ---------------------------------------------------------------------------


class TestForceRedecide:
    def test_skips_only_the_marker_check(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _run_day(monkeypatch, kind="no_entry")
        assert _concluded() is True
        with caplog.at_level("WARNING"):
            second = _run_day(monkeypatch, force_redecide=True)
        assert second.result["status"] == "stubbed"  # 判断をやり直した
        second.llm.assert_called_once()
        assert any(r.getMessage() == "decision_batch_force_redecide" for r in caplog.records)

    def test_without_force_the_same_day_stays_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _run_day(monkeypatch, kind="no_entry")
        assert _run_day(monkeypatch).result["reason"] == "decision_already_concluded"

    def test_does_not_bypass_the_idempotency_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """注文が残っていれば、やり直しでも冪等チェックで見送る。"""
        _run_day(monkeypatch, kind="no_entry")
        second = _run_day(monkeypatch, force_redecide=True, active=[{"orderId": 1}])
        assert second.result["reason"] == "active_orders_exist"
        second.llm.assert_not_called()

    def test_does_not_bypass_the_daily_lock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """当日ロックが既にあれば（発注済みの日）、やり直しでも発注しない。"""
        _run_day(monkeypatch, kind="no_entry")
        second = _run_day(
            monkeypatch,
            force_redecide=True,
            lock=LockAcquireOutcome(acquired=False, lock_exists=True),
        )
        assert second.result["reason"] == "dynamodb_lock_exists"
        second.client.private_post.assert_not_called()


class TestLambdaHandlerForceRedecidePayload:
    @staticmethod
    def _invoke(event: Any) -> MagicMock:
        with (
            patch("backend.src.batch.decision_batch._run", return_value={"status": "ok"}) as run,
            patch("backend.src.batch.decision_batch.is_market_holiday", return_value=False),
            patch("backend.src.batch.decision_batch.get_gmo_credentials", return_value=("k", "s")),
            patch("backend.src.batch.decision_batch.GmoFxClient"),
            patch("backend.src.batch.decision_batch._get_today_jst", return_value=TODAY),
            patch("backend.src.batch.decision_batch._get_now_jst", return_value=NOW),
        ):
            lambda_handler(event, None)
        return run

    def test_true_enables_it(self) -> None:
        assert self._invoke({"force_redecide": True}).call_args.kwargs["force_redecide"] is True

    @pytest.mark.parametrize(
        "event",
        [{}, None, {"force_redecide": False}, {"force_redecide": "true"}, {"force_redecide": 1}],
    )
    def test_anything_else_leaves_it_off(self, event: Any) -> None:
        """Scheduler の input は null。文字列 "true" や 1 でも有効にしない。"""
        assert self._invoke(event).call_args.kwargs["force_redecide"] is False
