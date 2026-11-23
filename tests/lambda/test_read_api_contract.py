"""Read API の契約テスト（Issue #391, §6.3）.

handler.py の実レスポンスを JSON フィクスチャとして固定し、TS 側
（dashboard/__tests__/read-api/contract.test.ts）が zod スキーマで parse する。

- フィールド名・型を変えると、本テストがフィクスチャとの差分で落ちる。
- 意図した変更は `UPDATE_READ_API_FIXTURES=1 pytest tests/lambda/test_read_api_contract.py`
  でフィクスチャを再生成し、TS 側スキーマも合わせて更新する。
- 入力値はすべて架空（2099 年・合成価格）。実際の取引ログ由来の値を使わない。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

FIXTURE_DIR = (
    Path(__file__).resolve().parents[2]
    / "dashboard"
    / "__tests__"
    / "read-api"
    / "fixtures"
)

SYNTH_DASHBOARD = {
    "status": "idle",
    "equityJpy": 1000000,
    "lastUpdated": "2099-01-08T20:30:00+09:00",
    "plan": {"direction": "LONG", "note": "synthetic"},
    "lot": 100,
    "tradeNoteMarkdown": "## synthetic note",
    "logs": [
        {"timestamp": "2099-01-08T20:30:00+09:00", "event": "synthetic_demo_event"}
    ],
}
# 入力のキー構成は実バッチが S3 に書く形に合わせる（PR #396 レビュー）:
#  - 判断ログ（decision_batch.py の log_base / _skip）には plan / logs /
#    trade_note_markdown は書かれない。equity_jpy（有効純資産・float）は口座を取得できた日だけ
#    書かれる（#411）。取得前に止まった日・過去のログには無く、/day の equity は null になる。
#    skip 日は direction / rationale も無い。
#  - 手仕舞いログ（closeout_batch.py の payload）にも logs は書かれない。
#  - logs を書くのは summary/dashboard.json（_upsert_dashboard）のみ。
SYNTH_DECISIONS = {
    "logs/decision/2099-01-06.json": {
        "status": "stubbed",
        "date": "2099-01-06",
        "direction": "SHORT",
        "execution_required": True,
        "rationale": "synthetic rationale",
        "order_type": "LIMIT",
        "sl_profile": "standard",
        "atr_multiplier": 0.7,
        "rr_target": 1.0,
        "entry_price": 100.5,
        "take_profit": 100.9,
        "stop_loss": 100.1,
        "atr_h": 0.57,
        "lot": 100,
        # 口座取得後の日だけ入る（#411）。架空の値（実際の残高を連想させない）。
        "equity_jpy": 123456.0,
    },
    # 見送り日も log_base の既定値（NONE / standard / 0.7 / 1.0）は入る（decision_batch.py）
    "logs/decision/2099-01-05.json": {
        "status": "no_entry",
        "date": "2099-01-05",
        "direction": "NO_ENTRY",
        "execution_required": False,
        "rationale": "synthetic rationale",
        "referenced_pivot_level": "NONE",
        "order_type": None,
        "sl_profile": "standard",
        "atr_multiplier": 0.7,
        "rr_target": 1.0,
    },
    # 取引日に発注前の確認で中止した日（decision_batch._skip: active_orders_exist 等）。
    # 休場日は判断ログを保存せず早期 return するため、休場日の skipped は存在しない。
    "logs/decision/2099-01-02.json": {
        "status": "skipped",
        "reason": "active_orders_exist",
        "date": "2099-01-02",
    },
}
SYNTH_DAY_DECISION = SYNTH_DECISIONS["logs/decision/2099-01-06.json"]
_CLOSEOUT_COMMON = {
    "date": "2099-01-06",
    "run_date": "2099-01-07",
    "prev_trade_date": "2099-01-06",
    "timestamp": "2099-01-07T04:30:05+09:00",
    "timestamp_ms": 4070000000000,
    "llm_total_tokens": 0,
}
# 手仕舞いログ（closeout_batch.py の _build_closeout_payload / _handle_closeout_failure）の実キー構成。
# 注文 ID・建玉 ID・約定明細は handler が返してはならない（契約テストで検証する）。
SYNTH_DAY_CLOSEOUT = {
    **_CLOSEOUT_COMMON,
    "status": "ok",
    "mode": "weekday",
    "step1_cancelled": ["900000001"],
    "step2_cancelled": ["900000002"],
    "step3_closed": ["900000003"],
    "step3_residual": [],
    "step3_verification_failed": False,
    "executions_count": 2,
    "executions_fetched": True,
    "note_generated": True,
    "note_complete": True,
    "executions": [
        {"executionId": 910000001, "orderId": 900000001, "positionId": 920000001}
    ],
    "pnl": {
        "open_count": 1,
        "close_count": 1,
        "loss_gain_total": "-100",
        "fee_total": "0",
        "settled_swap_total": "-20",
        "realized_total": "-120",
        "unparsed_fields": [],
    },
    "exit_reasons": {"920000001": "time_stop"},
}
SYNTH_CLOSEOUT_STEP3_FAILED = {
    **SYNTH_DAY_CLOSEOUT,
    "status": "step3_failed",
    "step1_cancelled": None,
    "step2_cancelled": None,
    "step3_closed": [],
    "step3_error": "synthetic close error",
    "step3_at_risk_position_ids": ["920000001"],
    "note_complete": False,
    "exit_reasons": {"920000001": "unknown"},
}
SYNTH_CLOSEOUT_MARKET_CLOSED = {
    **_CLOSEOUT_COMMON,
    "status": "market_closed",
    "reason": "市場閉場（§6.2 Step0）。次回 cron で再試行（§7.3）",
    "mode": "weekday",
    "lock_released": True,
}
# 金曜: 土曜の手仕舞いが直近 5 取引日分を集約し、その pnl を金曜の日付キーへ保存する
SYNTH_CLOSEOUT_FRIDAY = {
    **SYNTH_DAY_CLOSEOUT,
    "date": "2099-01-09",  # 実際の金曜（土曜 01-10 に実行された週次手仕舞いが金曜キーへ保存）
    "prev_trade_date": "2099-01-09",
    "run_date": "2099-01-10",
    "mode": "friday",
    "executions_count": 8,
    "pnl": {
        "open_count": 4,
        "close_count": 4,
        "loss_gain_total": "350",
        "fee_total": "0",
        "settled_swap_total": "-40",
        "realized_total": "310",
        "unparsed_fields": [],
    },
    "exit_reasons": {
        "920000001": "time_stop",
        "920000002": "take_profit",
        "920000003": "take_profit",
        "920000004": "stop_loss",
    },
}
# notes/{date}.json（closeout_batch の note_payload）。本文のキーは note_markdown
SYNTH_NOTE = {
    "date": "2099-01-06",
    "run_date": "2099-01-07",
    "mode": "mock",
    "note_markdown": "## synthetic note",
    "is_complete": True,
}
# dashboard.json に logs キーが無い場合（handler は sniper_logs=null を返す）
SYNTH_DASHBOARD_NO_LOGS = {k: v for k, v in SYNTH_DASHBOARD.items() if k != "logs"}


def _event(path: str) -> dict[str, Any]:
    return {"httpMethod": "GET", "rawPath": path, "headers": {}}


def _body(path: str, setup: Any) -> dict[str, Any]:
    from backend.src.read_api import handler

    with patch("backend.src.read_api.handler.s3_logger") as s3:
        setup(s3)
        resp = handler.lambda_handler(_event(path), None)
    assert resp["statusCode"] == 200
    body: dict[str, Any] = json.loads(resp["body"])
    return body


def _dashboard(s3: Any) -> None:
    s3.load_dashboard.return_value = SYNTH_DASHBOARD


SYNTH_NOTE_KEYS = ["notes/2099-01-06.json"]  # 2099-01-06 のみノートあり


def _history(s3: Any) -> None:
    from backend.src.persistence import s3_logger as real

    s3.s3_key_trade_note.side_effect = real.s3_key_trade_note
    s3.list_s3_keys.side_effect = lambda prefix, suffix="": (
        list(SYNTH_DECISIONS) if prefix == "logs/decision/" else SYNTH_NOTE_KEYS
    )
    s3.load_log.side_effect = lambda key: SYNTH_DECISIONS[key]


def _day_decision_only(s3: Any) -> None:
    s3.s3_key_trade_note.return_value = "notes/2099-01-06.json"
    s3.load_decision_log.return_value = SYNTH_DAY_DECISION
    s3.load_closeout_log.return_value = None
    s3.load_log.return_value = None


def _day_no_entry(s3: Any) -> None:
    s3.s3_key_trade_note.return_value = "notes/2099-01-05.json"
    s3.load_decision_log.return_value = SYNTH_DECISIONS["logs/decision/2099-01-05.json"]
    s3.load_closeout_log.return_value = None
    s3.load_log.return_value = None


# 見送り日も手仕舞いバッチは毎回ログを保存する: 約定 0 件・損益 "0"・exit_reasons 空
SYNTH_CLOSEOUT_NO_ENTRY = {
    **SYNTH_DAY_CLOSEOUT,
    "date": "2099-01-05",
    "prev_trade_date": "2099-01-05",
    "run_date": "2099-01-06",
    "executions_count": 0,
    "pnl": {
        "open_count": 0,
        "close_count": 0,
        "loss_gain_total": "0",
        "fee_total": "0",
        "settled_swap_total": "0",
        "realized_total": "0",
        "unparsed_fields": [],
    },
    "exit_reasons": {},
}


def _no_entry_with_closeout(s3: Any) -> None:
    _day_no_entry(s3)
    s3.load_closeout_log.return_value = SYNTH_CLOSEOUT_NO_ENTRY


def _with_closeout(closeout: dict[str, Any]) -> Any:
    def setup(s3: Any) -> None:
        _day_decision_only(s3)
        s3.load_closeout_log.return_value = closeout
        s3.load_log.return_value = SYNTH_NOTE

    return setup


_day_with_closeout = _with_closeout(SYNTH_DAY_CLOSEOUT)


def _dashboard_no_logs(s3: Any) -> None:
    s3.load_dashboard.return_value = SYNTH_DASHBOARD_NO_LOGS


def _dashboard_empty(s3: Any) -> None:
    s3.load_dashboard.return_value = None


CASES = {
    "dashboard.json": ("/dashboard", _dashboard),
    "dashboard_empty.json": ("/dashboard", _dashboard_empty),
    "history.json": ("/history", _history),
    "dashboard_no_logs.json": ("/dashboard", _dashboard_no_logs),
    "day_decision_only.json": ("/day/2099-01-06", _day_decision_only),
    "day_no_entry.json": ("/day/2099-01-05", _day_no_entry),
    "day_with_closeout.json": ("/day/2099-01-06", _day_with_closeout),
    "day_closeout_step3_failed.json": (
        "/day/2099-01-06",
        _with_closeout(SYNTH_CLOSEOUT_STEP3_FAILED),
    ),
    "day_closeout_market_closed.json": (
        "/day/2099-01-06",
        _with_closeout(SYNTH_CLOSEOUT_MARKET_CLOSED),
    ),
    "day_closeout_friday.json": (
        "/day/2099-01-09",
        _with_closeout(SYNTH_CLOSEOUT_FRIDAY),
    ),
    "day_closeout_no_entry.json": ("/day/2099-01-05", _no_entry_with_closeout),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_response_matches_committed_fixture(name: str) -> None:
    path, setup = CASES[name]
    actual = _body(path, setup)
    fixture = FIXTURE_DIR / name
    if os.environ.get("UPDATE_READ_API_FIXTURES") == "1":
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        fixture.write_text(
            json.dumps(actual, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    assert (
        fixture.exists()
    ), f"fixture missing: {fixture} (UPDATE_READ_API_FIXTURES=1 で生成)"
    expected = json.loads(fixture.read_text(encoding="utf-8"))
    assert (
        actual == expected
    ), "Read API のレスポンスが TS 契約フィクスチャとずれた。TS スキーマも更新すること"


@pytest.mark.parametrize(
    "name",
    sorted(
        n
        for n in CASES
        if n.startswith("day_closeout") or n == "day_with_closeout.json"
    ),
)
def test_closeout_summary_never_exposes_ids(name: str) -> None:
    """注文 ID・建玉 ID・約定明細を /day のレスポンスに含めない（#399）。"""
    path, setup = CASES[name]
    text = json.dumps(_body(path, setup))
    for secret in (
        "900000001",
        "900000002",
        "900000003",
        "910000001",
        "920000001",
        "920000004",
    ):
        assert secret not in text
    for key in (
        'executions"',
        "orderId",
        "positionId",
        "step3_closed",
        "step3_at_risk",
    ):
        assert key not in text


def test_day_equity_is_returned_when_recorded_and_null_otherwise() -> None:
    """#411: /day の equity は判断ログの equity_jpy（float）。無い日・過去ログは null。"""
    recorded = _body("/day/2099-01-06", _day_decision_only)
    assert recorded["equity"] == 123456.0
    assert isinstance(recorded["equity"], float)

    # 口座取得前に止まった日・過去ログ（equity_jpy キー無し）は null（推測値を返さない）
    assert _body("/day/2099-01-05", _day_no_entry)["equity"] is None
