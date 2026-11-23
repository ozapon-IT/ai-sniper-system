"""ORB-6 日次一致検証のテスト（#274）。

設計書 §8.2 / §8.2.1（三値写像）・§9（中止条件）・§6.4（EOD 観測点差）

受け入れ条件: **人工的な不一致を注入すると halt + Slack が発火する**。
"""

from __future__ import annotations

import importlib
from datetime import datetime, timedelta
from typing import Any

import pytest

from backend.src.batch import orb_verify
from backend.src.orb.broker_models import BrokerSession, SlippageRecord
from backend.src.orb.constants import LON
from backend.src.orb.models import RangeLevels

SESSION_DATE = "2026-08-10"  # 月曜・BST
LEVELS = RangeLevels(high=150.00, low=149.80, buy_stop=150.01, sell_stop=149.79)


def _lon(h: int, m: int, s: int = 0) -> datetime:
    return datetime(2026, 8, 10, h, m, s, tzinfo=LON)


def _ms(dt: datetime) -> str:
    return str(int(dt.timestamp() * 1000))


def _minutes(
    *, high_0830: float = 150.00, low_0815: float = 149.80, breakout_at: int | None = None
) -> list[dict[str, str]]:
    """London 8:00〜16:30 の 1 分足。`breakout_at` 分に買いストップ超えを差し込む。"""
    out: list[dict[str, str]] = []
    t = _lon(8, 0)
    end = _lon(16, 30)
    while t < end:
        base, hi, lo = 149.90, 149.91, 149.89
        if t.hour == 8 and t.minute == 30:
            hi = high_0830
        if t.hour == 8 and t.minute == 15:
            lo = low_0815
        if breakout_at is not None and t.hour == 9 and t.minute == breakout_at:
            hi, base = 150.20, 150.10
        out.append(
            {
                "openTime": _ms(t),
                "open": f"{base:.3f}",
                "high": f"{hi:.3f}",
                "low": f"{lo:.3f}",
                "close": f"{base:.3f}",
            }
        )
        t += timedelta(minutes=1)
    return out


def _bars15(eod_close: float = 150.40) -> list[dict[str, str]]:
    """London 8:00〜16:00 の 15 分足（EOD 足 = openTime 15:45 / close 16:00）。"""
    out: list[dict[str, str]] = []
    t = _lon(8, 0)
    while t < _lon(16, 0):
        c = eod_close if (t.hour == 15 and t.minute == 45) else 149.90
        out.append(
            {
                "openTime": _ms(t),
                "open": "149.90",
                "high": "150.00" if (t.hour == 8 and t.minute == 30) else "149.95",
                "low": "149.80" if (t.hour == 8 and t.minute == 15) else "149.85",
                "close": f"{c:.3f}",
            }
        )
        t += timedelta(minutes=15)
    return out


class FakeClient:
    def __init__(
        self,
        minutes: list[dict[str, str]],
        bars15: list[dict[str, str]],
        *,
        api_error: bool = False,
    ) -> None:
        self._1m = minutes
        self._15m = bars15
        # GMO は業務エラーでも HTTP 200 を返す（#327 / .claude/rules/gmo-api.md）
        self._api_error = api_error

    def public_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if self._api_error:
            return _Resp(
                {
                    "status": 1,
                    "messages": [
                        {"message_code": "ERR-5003", "message_string": "Requests are too many."}
                    ],
                }
            )
        data = self._1m if (params or {}).get("interval") == "1min" else self._15m
        return _Resp({"status": 0, "data": data})

    def close(self) -> None:  # pragma: no cover
        pass


class _Resp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.fixture(autouse=True)
def _local(tmp_path, monkeypatch):
    """永続化をローカルへ。Slack はキャプチャ。"""
    monkeypatch.setenv("LOCAL_PERSIST_DIR", str(tmp_path))
    for name in (
        "backend.src.orb.session_store",
        "backend.src.orb.event_log",
        "backend.src.orb.note_store",
        "backend.src.persistence.s3_logger",
    ):
        mod = importlib.import_module(name)
        monkeypatch.setattr(mod, "LOCAL_PERSIST_DIR", str(tmp_path), raising=False)
    sent: list[str] = []
    monkeypatch.setattr(orb_verify, "_alert", sent.append)
    return sent


def _save(session: BrokerSession) -> None:
    importlib.import_module("backend.src.orb.session_store").save_session(session)


def _halt() -> bool:
    return importlib.import_module("backend.src.orb.session_store").get_halt()


def _long_demo(entry: float = 150.02, exit_price: float = 150.42) -> BrokerSession:
    return BrokerSession(
        session_date=SESSION_DATE,
        state="CLOSED",
        levels=LEVELS,
        direction="long",
        entry_price=entry,
        entry_ts=_lon(9, 20),
        sl_price=149.79,
        exit_price=exit_price,
        exit_ts=_lon(16, 0, 2),
        exit_reason="eod",
        entry_slip=SlippageRecord(
            trigger_price=150.01,
            observed_bid=entry,
            slip_pips=1.0,
            spread_pips=0.5,
            poll_gap_ms=5000,
            price_gap=0.001,
        ),
    )


class TestMatch:
    def test_long_match_does_not_halt(self, _local) -> None:
        _save(_long_demo())
        client = FakeClient(_minutes(breakout_at=20), _bars15())
        res = orb_verify.run_verify(SESSION_DATE, client=client, now=_lon(16, 30))
        assert res["verdict"] == "match"
        assert res["halted"] is False
        assert _halt() is False
        assert _local == []

    def test_price_differences_are_not_mismatch(self, _local) -> None:
        """fill / EOD の価格差は観測点定義の差であり不一致ではない（§6.3 / §6.4）。"""
        _save(_long_demo(entry=150.05, exit_price=150.55))
        client = FakeClient(_minutes(breakout_at=20), _bars15(eod_close=150.40))
        res = orb_verify.run_verify(SESSION_DATE, client=client, now=_lon(16, 30))
        assert res["verdict"] == "match"
        diffs = res["observation_diffs"]
        assert diffs["entry_diff_pips"] != 0
        assert diffs["eod_gap_pips"] == pytest.approx(15.0)  # 150.55 − 150.40
        assert _halt() is False

    def test_no_fill_match(self, _local) -> None:
        _save(BrokerSession(session_date=SESSION_DATE, state="CANCELLED", levels=LEVELS))
        client = FakeClient(_minutes(), _bars15())  # ブレイクなし
        res = orb_verify.run_verify(SESSION_DATE, client=client, now=_lon(16, 30))
        assert res["verdict"] == "match"
        assert _halt() is False


class TestMismatchTriggersHalt:
    """受け入れ条件: 人工的な不一致で halt + Slack（§11 / §9）。"""

    def test_signal_class_mismatch_halts_and_alerts(self, _local) -> None:
        # リプレイは 9:20 に long fill するのに、デモは未約定として記録している
        _save(BrokerSession(session_date=SESSION_DATE, state="CANCELLED", levels=LEVELS))
        client = FakeClient(_minutes(breakout_at=20), _bars15())

        res = orb_verify.run_verify(SESSION_DATE, client=client, now=_lon(16, 30))

        assert res["verdict"] == "mismatch"
        assert res["reason"] == "signal_class"
        assert res["halted"] is True
        assert res["replay"]["signal"] == "long"
        assert res["demo"]["signal"] == "no_fill"
        assert _halt() is True
        assert len(_local) == 1
        assert "halt しました" in _local[0]
        assert "orb_halt.md" in _local[0]

    def test_t4_0920_no_fill_still_halts(self, _local) -> None:
        """T4: 09:20 の no_fill は本物の取りこぼし。defer に弱めない。"""
        _save(BrokerSession(session_date=SESSION_DATE, state="CANCELLED", levels=LEVELS))
        client = FakeClient(_minutes(breakout_at=20), _bars15())
        res = orb_verify.run_verify(SESSION_DATE, client=client, now=_lon(16, 30))
        assert res["verdict"] == "mismatch"
        assert res["reason"] == "signal_class"
        assert res["halted"] is True
        assert _halt() is True
        assert len(_local) == 1

    def test_t6_0900_range_mismatch_still_halts(self, _local) -> None:
        """T6: 09:00 fill + デモ no_fill でもレンジ価格が違えば halt。"""
        wrong = RangeLevels(high=151.00, low=150.80, buy_stop=151.01, sell_stop=150.79)
        _save(BrokerSession(session_date=SESSION_DATE, state="CANCELLED", levels=wrong))
        client = FakeClient(_minutes(breakout_at=0), _bars15())
        res = orb_verify.run_verify(SESSION_DATE, client=client, now=_lon(16, 30))
        assert res["verdict"] == "mismatch"
        assert res["reason"] == "range_levels_differ"
        assert res["halted"] is True
        assert _halt() is True
        assert len(_local) == 1

    def test_range_levels_mismatch_halts(self, _local) -> None:
        """分類が合っていてもレンジ価格が違えばデータ経路のバグ → halt。"""
        wrong = RangeLevels(high=151.00, low=150.80, buy_stop=151.01, sell_stop=150.79)
        _save(BrokerSession(session_date=SESSION_DATE, state="CANCELLED", levels=wrong))
        client = FakeClient(_minutes(), _bars15())

        res = orb_verify.run_verify(SESSION_DATE, client=client, now=_lon(16, 30))
        assert res["verdict"] == "mismatch"
        assert res["reason"] == "range_levels_differ"
        assert _halt() is True

    def test_report_is_persisted(self, _local, tmp_path) -> None:
        _save(BrokerSession(session_date=SESSION_DATE, state="CANCELLED", levels=LEVELS))
        client = FakeClient(_minutes(breakout_at=20), _bars15())
        orb_verify.run_verify(SESSION_DATE, client=client, now=_lon(16, 30))

        report = tmp_path / orb_verify.s3_key_orb_verify(SESSION_DATE)
        assert report.exists(), "不一致レポートが保存されていない"
        import json

        payload = json.loads(report.read_text(encoding="utf-8"))
        assert payload["verdict"] == "mismatch"
        assert payload["demo"]["signal"] == "no_fill"


class TestDeferDoesNotHalt:
    def test_uncertain_defers(self, _local) -> None:
        """`UNCERTAIN` は判定保留（§8.2.1）。halt しない。"""
        _save(BrokerSession(session_date=SESSION_DATE, state="UNCERTAIN", levels=LEVELS))
        client = FakeClient(_minutes(breakout_at=20), _bars15())
        res = orb_verify.run_verify(SESSION_DATE, client=client, now=_lon(16, 30))
        assert res["verdict"] == "defer"
        assert res["halted"] is False
        assert _halt() is False
        assert _local == []

    def test_both_filled_matches_ambiguous(self, _local) -> None:
        """デモ `both_filled` ↔ リプレイ fill は既知乖離として一致扱い（§8.2.1）。"""
        _save(
            BrokerSession(
                session_date=SESSION_DATE,
                state="ANOMALY",
                levels=LEVELS,
                exit_reason="both_filled",
            )
        )
        client = FakeClient(_minutes(breakout_at=20), _bars15())
        res = orb_verify.run_verify(SESSION_DATE, client=client, now=_lon(16, 30))
        assert res["verdict"] == "match"
        assert _halt() is False

    def test_t1_0900_no_fill_defers_without_halt(self, _local) -> None:
        """T1: リプレイ 09:00 fill + デモ no_fill → defer(fill_before_order_window)。"""
        _save(BrokerSession(session_date=SESSION_DATE, state="CANCELLED", levels=LEVELS))
        client = FakeClient(_minutes(breakout_at=0), _bars15())
        res = orb_verify.run_verify(SESSION_DATE, client=client, now=_lon(16, 30))
        assert res["verdict"] == "defer"
        assert res["reason"] == "fill_before_order_window"
        assert res["halted"] is False
        assert res["replay"]["signal"] == "long"
        assert res["demo"]["signal"] == "no_fill"
        assert _halt() is False
        assert _local == []

    def test_t2_0900_opposite_direction_defers(self, _local) -> None:
        """T2: 09:00 long fill + デモ short → defer。halt / Slack なし。"""
        _save(
            BrokerSession(
                session_date=SESSION_DATE,
                state="CLOSED",
                levels=LEVELS,
                direction="short",
                entry_price=149.78,
                exit_reason="eod",
            )
        )
        client = FakeClient(_minutes(breakout_at=0), _bars15())
        res = orb_verify.run_verify(SESSION_DATE, client=client, now=_lon(16, 30))
        assert res["verdict"] == "defer"
        assert res["reason"] == "fill_before_order_window"
        assert res["halted"] is False
        assert _halt() is False
        assert _local == []

    def test_t3_0901_no_fill_defers_without_halt(self, _local) -> None:
        """T3: リプレイ 09:01 fill + デモ no_fill → defer（#307 42 秒窓バー）。"""
        _save(BrokerSession(session_date=SESSION_DATE, state="CANCELLED", levels=LEVELS))
        client = FakeClient(_minutes(breakout_at=1), _bars15())
        res = orb_verify.run_verify(SESSION_DATE, client=client, now=_lon(16, 30))
        assert res["verdict"] == "defer"
        assert res["reason"] == "fill_before_order_window"
        assert res["halted"] is False
        assert _halt() is False
        assert _local == []

    def test_t5_0900_same_direction_is_match(self, _local) -> None:
        """T5: 09:00 fill + デモ同方向（遅延 fill）は match。halt しない。"""
        _save(_long_demo())
        client = FakeClient(_minutes(breakout_at=0), _bars15())
        res = orb_verify.run_verify(SESSION_DATE, client=client, now=_lon(16, 30))
        assert res["verdict"] == "match"
        assert res["halted"] is False
        assert _halt() is False
        assert _local == []

    def test_t8_uncertain_plus_0900_keeps_existing_defer_reason(self, _local) -> None:
        """T8: UNCERTAIN + 09:00 fill は既存 defer。reason を本クラスにしない。"""
        _save(BrokerSession(session_date=SESSION_DATE, state="UNCERTAIN", levels=LEVELS))
        client = FakeClient(_minutes(breakout_at=0), _bars15())
        res = orb_verify.run_verify(SESSION_DATE, client=client, now=_lon(16, 30))
        assert res["verdict"] == "defer"
        assert res["reason"] == "signal_class"
        assert res["halted"] is False
        assert _halt() is False
        assert _local == []


class TestKlinesApiErrorDefers:
    """🔴 klines の業務エラーで halt せず、レポートを残すこと（#327）。

    #327 で klines が例外を送出するようになる前は、空配列 → `evaluate_day` が `None`
    → `defer(replay_no_window)` に落ちていた（`has_session_window([])` が False）。
    捕まえないと例外が `run_verify` を突き抜けて **Lambda エラー**になり、レポートも
    ノートも残らず週次では `missing` になる。**比較材料が無い日に判定を下さない**のが要点。
    """

    def test_defers_without_halting(self, _local) -> None:
        _save(BrokerSession(session_date=SESSION_DATE, state="ARMED", levels=LEVELS))
        client = FakeClient(_minutes(breakout_at=20), _bars15(), api_error=True)

        res = orb_verify.run_verify(SESSION_DATE, client=client, now=_lon(16, 30))

        assert res["verdict"] == "defer"
        assert res["halted"] is False
        assert _halt() is False, "比較できない日に halt している"
        assert _local == [], "Slack 強アラートが飛んでいる"

    def test_reason_is_distinct_from_no_window(self, _local) -> None:
        """「窓日でない」と「API エラー」を区別できること。"""
        _save(BrokerSession(session_date=SESSION_DATE, state="ARMED", levels=LEVELS))
        client = FakeClient(_minutes(breakout_at=20), _bars15(), api_error=True)
        res = orb_verify.run_verify(SESSION_DATE, client=client, now=_lon(16, 30))
        assert res["reason"] == "replay_api_error"

    def test_report_is_persisted(self, _local) -> None:
        """週次で `missing` にならないよう、レポートは残すこと。"""
        from backend.src.persistence.s3_logger import load_log

        _save(BrokerSession(session_date=SESSION_DATE, state="ARMED", levels=LEVELS))
        client = FakeClient(_minutes(breakout_at=20), _bars15(), api_error=True)
        orb_verify.run_verify(SESSION_DATE, client=client, now=_lon(16, 30))

        saved = load_log(orb_verify.s3_key_orb_verify(SESSION_DATE))
        assert saved is not None and saved["verdict"] == "defer"


class TestHaltDoesNotReAlert:
    """halt 継続中に毎夕 Slack が鳴らないこと（レビュー指摘・実再現）。"""

    def test_skipped_by_halt_is_not_compared(self, _local) -> None:
        """`SKIPPED(halted)` は仮想発注していないので突合対象外 → `defer`。"""
        importlib.import_module("backend.src.orb.session_store").set_halt(True, reason="前日")
        _save(
            BrokerSession(
                session_date=SESSION_DATE, state="SKIPPED", levels=None, skip_reason="halted"
            )
        )
        client = FakeClient(_minutes(breakout_at=20), _bars15())

        res = orb_verify.run_verify(SESSION_DATE, client=client, now=_lon(16, 30))

        assert res["verdict"] == "defer"
        assert res["reason"] == "skipped_halted"
        assert res["halted"] is False
        assert _local == [], "halt 中の日で Slack が鳴っている"
        assert _halt() is True  # halt 自体は維持

    @pytest.mark.parametrize(
        "reason", ["market_holiday", "market_closed", "system_outage", "disabled"]
    )
    def test_deliberate_skips_are_not_compared(self, _local, reason: str) -> None:
        """デモが仮想発注しなかった日はリプレイと比較しない（ORB-8 で発見）。

        🔴 休場日（グッドフライデー等）は GMO が**薄商いで klines を残す**ため、
        リプレイは普通にシグナルを出す。比較すると必ず不一致になり、
        **休場のたびにデモが halt する**（2026-04-03 で実際に再現）。
        """
        _save(
            BrokerSession(
                session_date=SESSION_DATE, state="SKIPPED", levels=None, skip_reason=reason
            )
        )
        client = FakeClient(_minutes(breakout_at=20), _bars15())

        res = orb_verify.run_verify(SESSION_DATE, client=client, now=_lon(16, 30))

        assert res["verdict"] == "defer"
        assert res["reason"] == f"skipped_{reason}"
        assert _halt() is False, f"{reason} で halt している"
        assert _local == []

    def test_no_range_is_still_compared(self, _local) -> None:
        """`no_range` は判定そのものなので比較対象に残す（本数の食い違いは本物のバグ）。"""
        _save(
            BrokerSession(
                session_date=SESSION_DATE, state="SKIPPED", levels=None, skip_reason="no_range"
            )
        )
        client = FakeClient(_minutes(breakout_at=20), _bars15())
        res = orb_verify.run_verify(SESSION_DATE, client=client, now=_lon(16, 30))
        assert res["verdict"] == "mismatch"
        assert _halt() is True

    def test_mismatch_while_already_halted_does_not_realert(self, _local) -> None:
        """halt 中に（SKIPPED でない）不一致が出ても再通知しない。"""
        importlib.import_module("backend.src.orb.session_store").set_halt(True, reason="前日")
        _save(BrokerSession(session_date=SESSION_DATE, state="CANCELLED", levels=LEVELS))
        client = FakeClient(_minutes(breakout_at=20), _bars15())

        res = orb_verify.run_verify(SESSION_DATE, client=client, now=_lon(16, 30))

        assert res["verdict"] == "mismatch"
        assert res["newly_halted"] is False
        assert _local == []
        assert _halt() is True

    def test_first_mismatch_alerts_once(self, _local) -> None:
        _save(BrokerSession(session_date=SESSION_DATE, state="CANCELLED", levels=LEVELS))
        client = FakeClient(_minutes(breakout_at=20), _bars15())
        res = orb_verify.run_verify(SESSION_DATE, client=client, now=_lon(16, 30))
        assert res["newly_halted"] is True
        assert len(_local) == 1


class TestReportDetails:
    def test_signal_mismatch_reason_is_explicit(self, _local) -> None:
        """`reason` を空文字のままにしない（レポートだけ読んでも分かるように）。"""
        _save(BrokerSession(session_date=SESSION_DATE, state="CANCELLED", levels=LEVELS))
        client = FakeClient(_minutes(breakout_at=20), _bars15())
        res = orb_verify.run_verify(SESSION_DATE, client=client, now=_lon(16, 30))
        assert res["reason"] == "signal_class"

    def test_fill_time_containment_recorded_not_halted(self, _local) -> None:
        """fill 時刻の包含は記録のみ（§8.2）。外れても halt しない。"""
        # デモの fill 観測は 9:20:xx、リプレイの fill バーは openTime 9:20
        _save(_long_demo())
        client = FakeClient(_minutes(breakout_at=20), _bars15())
        res = orb_verify.run_verify(SESSION_DATE, client=client, now=_lon(16, 30))
        assert res["verdict"] == "match"
        assert res["observation_diffs"]["fill_time_in_replay_bar"] is True

    def test_fill_time_outside_bar_is_recorded_but_not_halt(self, _local) -> None:
        demo = _long_demo()
        _save(
            BrokerSession(
                **{**demo.__dict__, "entry_ts": _lon(9, 25)}  # リプレイのバー(9:20)外
            )
        )
        client = FakeClient(_minutes(breakout_at=20), _bars15())
        res = orb_verify.run_verify(SESSION_DATE, client=client, now=_lon(16, 30))
        assert res["observation_diffs"]["fill_time_in_replay_bar"] is False
        assert res["verdict"] == "match"  # 分類は一致しているので halt しない
        assert _halt() is False


class TestNoteIntegration:
    def test_note_gets_eod_gap(self, _local) -> None:
        """verify が `eod_close` を渡すのでノートに `eod_gap_pips` が入る（§6.4）。"""
        _save(_long_demo(exit_price=150.55))
        client = FakeClient(_minutes(breakout_at=20), _bars15(eod_close=150.40))
        orb_verify.run_verify(SESSION_DATE, client=client, now=_lon(16, 30))

        note = importlib.import_module("backend.src.orb.note_store").load_note(SESSION_DATE)
        assert note is not None
        assert "eod_gap_pips: 15" in note

    def test_fill_before_order_window_note_names_reason(self, _local) -> None:
        _save(BrokerSession(session_date=SESSION_DATE, state="CANCELLED", levels=LEVELS))
        client = FakeClient(_minutes(breakout_at=0), _bars15())
        orb_verify.run_verify(SESSION_DATE, client=client, now=_lon(16, 30))
        note = importlib.import_module("backend.src.orb.note_store").load_note(SESSION_DATE)
        assert note is not None
        assert "fill_before_order_window" in note
        assert "halt 済み" not in note

    def test_mismatch_note_carries_warning(self, _local) -> None:
        _save(BrokerSession(session_date=SESSION_DATE, state="CANCELLED", levels=LEVELS))
        client = FakeClient(_minutes(breakout_at=20), _bars15())
        orb_verify.run_verify(SESSION_DATE, client=client, now=_lon(16, 30))

        note = importlib.import_module("backend.src.orb.note_store").load_note(SESSION_DATE)
        assert note is not None
        assert "不一致" in note and "halt 済み" in note


class TestEdges:
    def test_no_session(self, _local) -> None:
        res = orb_verify.run_verify("1999-01-01", client=FakeClient([], []))
        assert res["verdict"] == "no_session"
        assert _halt() is False

    def test_replay_without_window_defers(self, _local) -> None:
        """リプレイ側が窓日でないと判定した日は保留（halt しない）。"""
        _save(BrokerSession(session_date=SESSION_DATE, state="CANCELLED", levels=LEVELS))
        res = orb_verify.run_verify(
            SESSION_DATE, client=FakeClient([], []), now=_lon(16, 30)
        )
        assert res["verdict"] == "defer"
        assert res["reason"] == "replay_no_window"
        assert _halt() is False


class TestContainment:
    def test_verify_has_no_forbidden_refs(self) -> None:
        from pathlib import Path

        path = Path(__file__).resolve().parents[2] / "backend/src/batch/orb_verify.py"
        forbidden = (
            "ifoOrder", "ifdOrder", "closeOrder", "cancelOrders",
            "ALLOW_LIVE_ORDERS", "TRADING_PHASE", "MVP_MODE",
            "anthropic", "private_get", "private_post", "PRIVATE_",
        )
        text = path.read_text()
        assert [t for t in forbidden if t in text] == []
