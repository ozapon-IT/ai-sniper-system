"""ORB セッション永続化・イベントログアダプタのテスト（LOCAL_PERSIST_DIR）。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.src.orb import event_log as elog
from backend.src.orb import session_store as store
from backend.src.orb.broker import open_session
from backend.src.orb.broker_models import BrokerEvent
from backend.src.orb.constants import LON, STOP_BUFFER
from backend.src.orb.event_log import (
    append_events,
    load_events,
    notify_slack_for_events,
    s3_key_orb_session_events,
)
from backend.src.orb.models import RangeLevels


def _levels() -> RangeLevels:
    high, low = 150.20, 149.80
    return RangeLevels(
        high=high,
        low=low,
        buy_stop=high + STOP_BUFFER,
        sell_stop=low - STOP_BUFFER,
    )


def test_session_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "LOCAL_PERSIST_DIR", str(tmp_path))

    session, _ = open_session(
        "2024-06-03",
        halt=False,
        levels=_levels(),
        ts=datetime(2024, 6, 3, 9, 1, tzinfo=LON),
    )
    assert store.save_session(session) is True
    loaded = store.load_session("2024-06-03")
    assert loaded is not None
    assert loaded.state == "ARMED"
    assert loaded.levels is not None
    assert loaded.levels.buy_stop == pytest.approx(session.levels.buy_stop)  # type: ignore[union-attr]


def test_halt_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "LOCAL_PERSIST_DIR", str(tmp_path))
    assert store.get_halt() is False
    assert store.set_halt(True, reason="verify_mismatch") is True
    assert store.get_halt() is True
    assert store.set_halt(False, reason="manual_clear") is True
    assert store.get_halt() is False


def test_event_log_append(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(elog, "LOCAL_PERSIST_DIR", str(tmp_path))
    day = "2024-06-03"
    ts = datetime(2024, 6, 3, 9, 1, tzinfo=LON)
    ev = BrokerEvent(
        ts=ts,
        reason="armed",
        state_from="IDLE",
        state_to="ARMED",
        bid=None,
        ask=None,
        detail={"x": 1},
    )
    assert append_events(day, [ev]) is True
    assert append_events(day, [ev]) is True
    rows = load_events(day)
    assert len(rows) == 2
    assert rows[0]["reason"] == "armed"
    assert s3_key_orb_session_events(day) == f"orb-demo/sessions/{day}.jsonl"


def test_notify_slack_flag_invokes_alert() -> None:
    ev = BrokerEvent(
        ts=datetime(2024, 6, 3, 9, 10, tzinfo=LON),
        reason="both_filled",
        state_from="FILLED",
        state_to="ANOMALY",
        bid=150.0,
        ask=150.002,
        detail={"how": "cancel_latency"},
        notify_slack=True,
    )
    with patch(
        "backend.src.alerting.slack_alert.send_slack_alert", return_value=True
    ) as mock_slack:
        notify_slack_for_events([ev])
        mock_slack.assert_called_once()
