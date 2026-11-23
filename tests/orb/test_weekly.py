"""ORB-7 週次監視のテスト（#276）。

設計書 §1.2（損益を出さない）・§6.3・§6.5・§9（中止条件）

受け入れ条件:
  - **閾値超過の合成データで警告が発火する**
  - **サマリに損益が含まれない**
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from backend.src.orb import weekly
from backend.src.orb.broker_models import BrokerSession, SlippageRecord
from backend.src.orb.constants import LON
from backend.src.orb.models import RangeLevels

LEVELS = RangeLevels(high=150.00, low=149.80, buy_stop=150.01, sell_stop=149.79)
# 2026-08-10 は月曜。週は 2026-W33（月〜金 = 08-10〜08-14）
MONDAY = datetime(2026, 8, 10, 17, 0, tzinfo=LON)


def _day(i: int) -> str:
    """滑り系フィクスチャの営業日（2026-08-03〜07）。

    🔴 **#304 の除外日 2026-08-14 を含まない週**を使う。含めると
    `adverse_slips` がその日を落とすぶん、母集団の件数が黙って 1 減り、
    パーセンタイル・中止条件のテストが意図せず弱くなる。
    """
    return f"2026-08-0{3 + i}"


def _slip(
    slip_pips: float,
    spread: float = 0.5,
    *,
    poll_gap_ms: int | None = 5000,
) -> SlippageRecord:
    return SlippageRecord(
        trigger_price=150.01,
        observed_bid=150.02,
        slip_pips=slip_pips,
        spread_pips=spread,
        poll_gap_ms=poll_gap_ms,
        price_gap=0.001,
    )


def _filled(
    day: str,
    slip: float,
    spread: float = 0.5,
    *,
    poll_gap_ms: int | None = 5000,
) -> BrokerSession:
    return BrokerSession(
        session_date=day,
        state="CLOSED",
        levels=LEVELS,
        direction="long",
        entry_price=150.02,
        sl_price=149.79,
        exit_price=150.40,
        exit_reason="eod",
        entry_slip=_slip(slip, spread, poll_gap_ms=poll_gap_ms),
    )


def _skipped(day: str, reason: str) -> BrokerSession:
    return BrokerSession(
        session_date=day, state="SKIPPED", levels=None, skip_reason=reason
    )


def _summary(
    sessions: dict[str, BrokerSession | None],
    *,
    reports: dict[str, dict[str, Any] | None] | None = None,
    halt: dict[str, Any] | None = None,
    now: datetime = MONDAY,
) -> weekly.WeeklySummary:
    return weekly.build_summary(
        anchor=now.date(),
        sessions=sessions,
        verify_reports=reports or dict.fromkeys(sessions, {"verdict": "match"}),
        halt_detail=halt or {"halt": False, "reason": "", "since": None},
        now=now,
    )


class TestWeekWindow:
    def test_week_days_are_monday_to_friday(self) -> None:
        days = weekly.week_days(datetime(2026, 8, 12, tzinfo=LON).date())  # 水曜
        assert days == [
            "2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14",
        ]

    def test_week_key_is_iso(self) -> None:
        assert weekly.week_key(datetime(2026, 8, 10, tzinfo=LON).date()) == "2026-W33"


class TestDistributions:
    def test_slip_percentiles_with_n(self) -> None:
        sessions = {
            _day(i): _filled(_day(i), slip) for i, slip in enumerate([1.0, 2.0, 3.0, 4.0, 9.0])
        }
        s = _summary(sessions)
        assert s.slip_n == 5
        assert s.slip_p50 == pytest.approx(3.0)
        assert s.slip_p90 == pytest.approx(9.0)

    def test_spread_excess_uses_current_baseline(self) -> None:
        """前提は ORB-1b 実測の 0.5pips（Phase 0 の 0.2 ではない・§6.3）。"""
        assert weekly.SPREAD_BASELINE_PIPS == 0.5
        sessions = {"2026-08-10": _filled("2026-08-10", 1.0, spread=0.9)}
        s = _summary(sessions)
        assert s.spread_p50 == pytest.approx(0.9)
        assert s.spread_excess_p50 == pytest.approx(0.4)

    def test_funnel_counts(self) -> None:
        sessions: dict[str, BrokerSession | None] = {
            "2026-08-10": _filled("2026-08-10", 1.0),
            "2026-08-11": _skipped("2026-08-11", "no_range"),
            "2026-08-12": BrokerSession(session_date="2026-08-12", state="CANCELLED", levels=LEVELS),
            "2026-08-13": BrokerSession(
                session_date="2026-08-13", state="ANOMALY", levels=LEVELS, exit_reason="both_filled"
            ),
            "2026-08-14": None,
        }
        s = _summary(sessions)
        assert s.funnel == {
            "fill": 1,
            "no_range": 1,
            "no_fill": 1,
            "both_filled": 1,
            "no_record": 1,
        }


class TestSlipPopulation:
    """§9 の母集団は「全 fill + SL イベント」。EOD は入れない。定義 B（#359）。"""

    def _sl_exit(
        self,
        day: str,
        *,
        entry_slip: float,
        sl_slip: float,
        entry_gap_ms: int | None = 5000,
        exit_gap_ms: int | None = 5000,
    ) -> BrokerSession:
        return BrokerSession(
            session_date=day,
            state="CLOSED",
            levels=LEVELS,
            direction="long",
            entry_price=150.02,
            sl_price=149.79,
            exit_price=149.782,
            exit_reason="sl",
            entry_slip=_slip(entry_slip, poll_gap_ms=entry_gap_ms),
            exit_slip=_slip(sl_slip, poll_gap_ms=exit_gap_ms),
        )

    def test_eod_exit_slip_is_excluded(self) -> None:
        """EOD の exit_slip は trigger=entry_price なので中身が結果 pips。混ぜない。

        混ぜると勝ち EOD の週ほど中止条件が誤発火し、§1.2（損益を出さない）も実質破れる。
        """
        eod = BrokerSession(
            session_date="2026-08-10",
            state="CLOSED",
            levels=LEVELS,
            direction="long",
            entry_price=150.02,
            sl_price=149.79,
            exit_price=150.55,
            exit_reason="eod",
            entry_slip=_slip(1.0),
            exit_slip=_slip(53.0),  # ← 実体は +53pips の損益
        )
        assert weekly.adverse_slips(eod) == [1.0]

        s = _summary({_day(i): eod for i in range(5)})
        assert s.slip_n == 5  # entry のみ（exit は除外）
        assert s.slip_p50 == pytest.approx(1.0)
        assert s.slip_p90 == pytest.approx(1.0)
        assert s.breaches == [], "EOD の損益で中止条件が誤発火している"

    def test_both_filled_exit_slip_is_excluded(self) -> None:
        anomaly = BrokerSession(
            session_date="2026-08-10",
            state="ANOMALY",
            levels=LEVELS,
            direction="long",
            entry_price=150.02,
            exit_reason="both_filled",
            entry_slip=_slip(1.0),
            exit_slip=_slip(40.0),
        )
        assert weekly.adverse_slips(anomaly) == [1.0]

    def test_sl_slip_sign_is_normalised_to_adverse_positive(self) -> None:
        """SL の不利すべりは生値が負。反転しないと上側 p90 に永久に掛からない。"""
        session = self._sl_exit("2026-08-10", entry_slip=1.0, sl_slip=-0.8)
        assert weekly.adverse_slips(session) == [1.0, 0.8]

    def test_adverse_sl_slip_can_breach(self) -> None:
        """SL の不利すべりが大きければ中止条件が鳴る（符号修正の実効確認）。"""
        sessions = {
            _day(i): self._sl_exit(_day(i), entry_slip=1.0, sl_slip=-6.5)
            for i in range(5)
        }
        s = _summary(sessions)
        assert s.slip_p90 == pytest.approx(6.5)
        assert "slip_p90>5.84" in s.breaches

    def test_favourable_sl_slip_stays_negative(self) -> None:
        """有利側（SL より良い価格で約定）は負のまま。上側閾値を押し上げない。"""
        session = self._sl_exit("2026-08-10", entry_slip=1.0, sl_slip=0.4)
        assert weekly.adverse_slips(session) == [1.0, -0.4]

    def test_excluded_day_contributes_nothing(self) -> None:
        """🔴 稼働初日 2026-08-14 は滑り母集団から除外する（#304・§6.3）。

        poller の起動重なりで 2 度目の fill を判定しており、どちらの `entry_slip` が
        記録に残るかが非決定的だった。測定そのものが信用できないので日付除外する。
        日付除外は定義 B（観測点）より強い。gap が測れていても空を返す（T3）。
        """
        assert "2026-08-14" in weekly.SLIP_EXCLUDED_DATES
        assert weekly.adverse_slips(_filled("2026-08-14", 2.3, poll_gap_ms=5000)) == []
        assert weekly.adverse_slips(_filled("2026-08-13", 2.3)) == [2.3]

    def test_excluded_day_is_not_counted_in_n(self) -> None:
        s = _summary({"2026-08-13": _filled("2026-08-13", 1.0), "2026-08-14": _filled("2026-08-14", 2.3)})
        assert s.slip_n == 1
        assert s.slip_p90 == pytest.approx(1.0)

    def test_null_poll_gap_entry_is_excluded(self) -> None:
        """T1: `poll_gap_ms is None` の entry は母集団に入れない（定義 B・#359）。"""
        session = _filled("2026-08-13", 3.2, poll_gap_ms=None)
        assert weekly.adverse_slips(session) == []

    def test_null_poll_gap_sl_exit_is_excluded_independently(self) -> None:
        """T2: entry と SL exit は独立。exit の gap 空だけ落とす。"""
        session = self._sl_exit(
            "2026-08-13",
            entry_slip=1.0,
            sl_slip=-0.8,
            entry_gap_ms=5000,
            exit_gap_ms=None,
        )
        assert weekly.adverse_slips(session) == [1.0]

    def test_measured_five_second_gap_is_kept(self) -> None:
        """T4: 通常の ~5s ギャップは残す（§12.2 の測定済みギャップは補正・除去しない）。"""
        session = _filled("2026-08-13", 1.2, poll_gap_ms=5000)
        assert weekly.adverse_slips(session) == [1.2]

    def test_t012_like_null_entry_keeps_measured_sl_exit(self) -> None:
        """T5: 08-31 型。entry 空ギャップは落とし、測れている SL exit 0.2 は残す。"""
        session = self._sl_exit(
            "2026-08-31",
            entry_slip=3.2,
            sl_slip=-0.2,
            entry_gap_ms=None,
            exit_gap_ms=5016,
        )
        assert weekly.adverse_slips(session) == [0.2]

    def test_mixed_week_counts_only_countable_and_slack_says_observation(self) -> None:
        """T6: 空ギャップと通常ギャップを混ぜても slip_n は countable だけ。Slack は観測点。"""
        sessions = {
            "2026-08-03": _filled("2026-08-03", 3.2, poll_gap_ms=None),
            "2026-08-04": _filled("2026-08-04", 1.2, poll_gap_ms=5000),
            "2026-08-05": _filled("2026-08-05", 0.3, poll_gap_ms=5016),
        }
        s = _summary(sessions)
        assert s.slip_n == 2
        assert s.slip_p50 == pytest.approx(0.75)
        assert s.slip_p90 == pytest.approx(1.2)
        text = weekly.render_slack(s)
        assert "観測点" in text
        assert f"n={s.slip_n}" in text
        assert "約定 n とは別" in text


class TestBreaches:
    """受け入れ条件: 閾値超過の合成データで警告が発火する（§9）。"""

    def test_slip_p90_breach(self) -> None:
        sessions = {
            _day(i): _filled(_day(i), slip)
            for i, slip in enumerate([1.0, 1.0, 1.0, 1.0, 6.5])  # p90 = 6.5 > 5.84
        }
        s = _summary(sessions)
        assert "slip_p90>5.84" in s.breaches
        assert "⚠️ *中止条件に抵触*" in weekly.render_slack(s)

    def test_slip_p50_breach(self) -> None:
        sessions = {
            _day(i): _filled(_day(i), 3.5) for i in range(5)
        }
        s = _summary(sessions)
        assert "slip_p50>3.0" in s.breaches

    def test_outage_breach_counts_only_failures(self) -> None:
        """休場は障害ではない。`system_outage` / `uncertain` のみ数える（§6.5）。"""
        sessions: dict[str, BrokerSession | None] = {
            "2026-08-10": _skipped("2026-08-10", "system_outage"),
            "2026-08-11": _skipped("2026-08-11", "system_outage"),
            "2026-08-12": BrokerSession(session_date="2026-08-12", state="UNCERTAIN", levels=LEVELS),
            "2026-08-13": _skipped("2026-08-13", "market_holiday"),
            "2026-08-14": _skipped("2026-08-14", "market_closed"),
        }
        s = _summary(sessions)
        assert s.outage_days == ["2026-08-10", "2026-08-11", "2026-08-12"]
        assert "outage>2/week" in s.breaches

    def test_no_breach_is_clean(self) -> None:
        sessions = {_day(i): _filled(_day(i), 1.0) for i in range(5)}
        s = _summary(sessions)
        assert s.breaches == []
        assert "中止条件に抵触" not in weekly.render_slack(s)

    def test_breach_does_not_halt(self) -> None:
        """中止条件は警告のみ（halt するのは日次の判定不一致だけ・§9）。"""
        sessions = {_day(i): _filled(_day(i), 9.0) for i in range(5)}
        text = weekly.render_slack(_summary(sessions))
        assert "警告のみ" in text
        assert "halt しました" not in text


class TestHaltVisibility:
    """ORB-6 で日次アラートを抑止したため、halt はここでしか見えない。"""

    def test_halt_shows_days_and_reason(self) -> None:
        since = (MONDAY - timedelta(days=3)).astimezone(UTC).isoformat()
        s = _summary(
            {"2026-08-10": _skipped("2026-08-10", "halted")},
            halt={"halt": True, "reason": "verify_mismatch:2026-08-07", "since": since},
        )
        assert s.halt["halt"] is True
        assert s.halt["days"] == 3
        text = weekly.render_slack(s)
        assert "halt 継続中" in text
        assert "3 日" in text
        assert "verify_mismatch:2026-08-07" in text
        assert "orb_halt.md" in text

    def test_halt_without_since_is_unknown_not_crash(self) -> None:
        """`since` を持たない古いレコードでも落ちない。"""
        s = _summary(
            {"2026-08-10": _skipped("2026-08-10", "halted")},
            halt={"halt": True, "reason": "manual", "since": None},
        )
        assert s.halt["days"] is None
        assert "不明" in weekly.render_slack(s)

    def test_no_halt_is_green(self) -> None:
        s = _summary({"2026-08-10": _filled("2026-08-10", 1.0)})
        assert "🟢 halt なし" in weekly.render_slack(s)


class TestNoPnl:
    """受け入れ条件: サマリに損益が含まれない（§1.2）。"""

    def test_slack_has_no_pnl_terms(self) -> None:
        sessions = {_day(i): _filled(_day(i), 1.0) for i in range(5)}
        text = weekly.render_slack(_summary(sessions))
        for term in ("result_pips", "R)", "勝率", "損益:", "pips 損益", "利益", "EV"):
            assert term not in text, f"損益系の語 {term!r} がサマリに出ている"
        assert "損益でエッジを判定しない" in text

    def test_payload_has_no_pnl_fields(self) -> None:
        sessions = {_day(i): _filled(_day(i), 1.0) for i in range(5)}
        payload = _summary(sessions).to_jsonable()
        flat = str(payload)
        for term in ("result_pips", "rr_actual", "exit_price", "entry_price"):
            assert term not in flat, f"損益に繋がる値 {term!r} が payload に出ている"


class TestVerifyRollup:
    def test_verdicts_are_counted(self) -> None:
        sessions = {_day(i): _filled(_day(i), 1.0) for i in range(3)}
        reports: dict[str, dict[str, Any] | None] = {
            "2026-08-10": {"verdict": "match"},
            "2026-08-11": {"verdict": "defer"},
            "2026-08-12": None,
        }
        s = _summary(sessions, reports=reports)
        assert s.verify == {"match": 1, "defer": 1, "missing": 1}


# ---------------------------------------------------------------- batch


@pytest.fixture()
def local(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_PERSIST_DIR", str(tmp_path))
    for name in (
        "backend.src.orb.session_store",
        "backend.src.persistence.s3_logger",
    ):
        mod = importlib.import_module(name)
        monkeypatch.setattr(mod, "LOCAL_PERSIST_DIR", str(tmp_path), raising=False)
    from backend.src.batch import orb_weekly

    sent: list[str] = []
    monkeypatch.setattr(orb_weekly, "_alert", sent.append)
    return orb_weekly, sent, tmp_path


class TestWeeklyBatch:
    def test_saves_payload_and_notifies_once(self, local) -> None:
        orb_weekly, sent, tmp_path = local
        importlib.import_module("backend.src.orb.session_store").save_session(
            _filled("2026-08-10", 1.0)
        )
        res = orb_weekly.run_weekly(now=MONDAY)

        assert res["status"] == "ok"
        assert res["week"] == "2026-W33"
        assert len(sent) == 1, "Slack は 1 通にまとめる（§9）"

        saved = tmp_path / orb_weekly.s3_key_orb_weekly("2026-W33")
        assert saved.exists()

    def test_notify_false_skips_slack(self, local) -> None:
        orb_weekly, sent, _ = local
        orb_weekly.run_weekly(now=MONDAY, notify=False)
        assert sent == []

    def test_halt_state_reaches_slack(self, local) -> None:
        orb_weekly, sent, _ = local
        store = importlib.import_module("backend.src.orb.session_store")
        store.set_halt(True, reason="verify_mismatch:2026-08-07")
        orb_weekly.run_weekly(now=MONDAY)
        assert "halt 継続中" in sent[0]


class TestHaltSince:
    def test_since_is_set_on_transition_and_preserved(self, local) -> None:
        """`since` は False→True の遷移でのみ更新（再セットで継続日数をリセットしない）。"""
        _, _, _ = local
        store = importlib.import_module("backend.src.orb.session_store")
        store.set_halt(True, reason="1回目")
        first = store.get_halt_detail()["since"]
        assert first is not None

        store.set_halt(True, reason="2回目")
        assert store.get_halt_detail()["since"] == first, "再セットで since が上書きされている"

        store.set_halt(False, reason="解除")
        assert store.get_halt_detail()["halt"] is False


class TestContainment:
    def test_no_forbidden_refs(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        targets = [root / "backend/src/orb/weekly.py", root / "backend/src/batch/orb_weekly.py"]
        forbidden = (
            "ifoOrder", "ifdOrder", "closeOrder", "cancelOrders",
            "ALLOW_LIVE_ORDERS", "TRADING_PHASE", "MVP_MODE",
            "gmo_client", "anthropic", "private_get", "private_post", "PRIVATE_",
        )
        hits = [f"{p.name}:{t}" for p in targets for t in forbidden if t in p.read_text()]
        assert hits == []


class TestNoRecordBeforeStart:
    """稼働開始前の `no_record` を障害に数えない（2026-08-14 初回サマリの誤発火）。"""

    def test_no_records_at_all_is_not_outage(self) -> None:
        """記録が一件も無い＝まだ動いていない。初週から中止条件を鳴らさない。"""
        s = _summary(dict.fromkeys(weekly.week_days(MONDAY.date())), reports={})
        assert s.funnel == {"no_record": 5}
        assert s.outage_days == []
        assert s.breaches == [], "稼働開始前に中止条件が誤発火している"
        assert "中止条件に抵触" not in weekly.render_slack(s)

    def test_missing_day_after_start_is_outage(self) -> None:
        """稼働後に記録が飛んだ日は障害として数える。"""
        sessions: dict[str, BrokerSession | None] = {
            "2026-08-10": _filled("2026-08-10", 1.0),
            "2026-08-11": None,
            "2026-08-12": None,
            "2026-08-13": None,
            "2026-08-14": None,
        }
        s = weekly.build_summary(
            anchor=MONDAY.date(),
            sessions=sessions,
            verify_reports=dict.fromkeys(sessions, {"verdict": "match"}),
            halt_detail={"halt": False, "reason": "", "since": None},
            now=MONDAY,
            demo_first_day="2026-08-10",
        )
        assert s.outage_days == ["2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14"]
        assert "outage>2/week" in s.breaches

    def test_days_before_start_are_excluded(self) -> None:
        """開始日より前の欠落だけが除外される（開始日以降は数える）。"""
        sessions: dict[str, BrokerSession | None] = {
            "2026-08-10": None,  # 稼働前
            "2026-08-11": None,  # 稼働前
            "2026-08-12": _filled("2026-08-12", 1.0),
            "2026-08-13": None,  # 稼働後の欠落
            "2026-08-14": None,  # 稼働後の欠落
        }
        s = weekly.build_summary(
            anchor=MONDAY.date(),
            sessions=sessions,
            verify_reports=dict.fromkeys(sessions, {"verdict": "match"}),
            halt_detail={"halt": False, "reason": "", "since": None},
            now=MONDAY,
            demo_first_day="2026-08-12",
        )
        assert s.outage_days == ["2026-08-13", "2026-08-14"]


class TestSpreadMaxAndCombined:
    """#361: スプレッド max / 0.5 超 / 合算は表示のみ。halt / breaches / 閾値は不変。"""

    def _eod(
        self,
        day: str,
        *,
        entry_spread: float,
        exit_spread: float,
        slip: float = 1.0,
    ) -> BrokerSession:
        return BrokerSession(
            session_date=day,
            state="CLOSED",
            levels=LEVELS,
            direction="long",
            entry_price=150.02,
            sl_price=149.79,
            exit_price=150.40,
            exit_reason="eod",
            entry_slip=_slip(slip, entry_spread),
            exit_slip=_slip(53.0, exit_spread),
        )

    def _sl(
        self,
        day: str,
        *,
        entry_slip: float,
        sl_slip: float,
        entry_spread: float = 0.5,
        exit_spread: float = 0.5,
        entry_gap_ms: int | None = 5000,
        exit_gap_ms: int | None = 5000,
    ) -> BrokerSession:
        return BrokerSession(
            session_date=day,
            state="CLOSED",
            levels=LEVELS,
            direction="long",
            entry_price=150.02,
            sl_price=149.79,
            exit_price=149.782,
            exit_reason="sl",
            entry_slip=_slip(entry_slip, entry_spread, poll_gap_ms=entry_gap_ms),
            exit_slip=_slip(sl_slip, exit_spread, poll_gap_ms=exit_gap_ms),
        )

    def test_t1_spread_max_identity_when_p90_drops_max(self) -> None:
        """T1: n=10 の nearest-rank p90 は max を落とす。identity は最後の日の exit。"""
        days = [_day(i) for i in range(5)]
        sessions = {
            days[0]: self._eod(days[0], entry_spread=0.5, exit_spread=0.5),
            days[1]: self._eod(days[1], entry_spread=0.5, exit_spread=0.5),
            days[2]: self._eod(days[2], entry_spread=0.5, exit_spread=0.5),
            days[3]: self._eod(days[3], entry_spread=0.5, exit_spread=1.2),
            days[4]: self._eod(days[4], entry_spread=0.5, exit_spread=5.0),
        }
        s = _summary(sessions)
        assert s.spread_n == 10
        assert s.spread_max == pytest.approx(5.0)
        assert s.spread_max_date == days[4]
        assert s.spread_max_side == "exit"
        assert s.spread_p90 is not None
        assert s.spread_p90 < s.spread_max
        payload = s.to_jsonable()
        assert payload["spread"]["max"] == pytest.approx(5.0)
        assert payload["spread"]["max_at"] == {"date": days[4], "side": "exit"}

    def test_t2_over_baseline_is_strict_gt(self) -> None:
        """T2: ちょうど 0.5 は数えず、厳密 `>` だけ。Slack は {over}/{spread_n}。"""
        sessions = {
            "2026-08-03": _filled("2026-08-03", 1.0, spread=0.5),
            "2026-08-04": _filled("2026-08-04", 1.0, spread=0.5),
            "2026-08-05": _filled("2026-08-05", 1.0, spread=0.5),
            "2026-08-06": _filled("2026-08-06", 1.0, spread=0.6),
            "2026-08-07": _filled("2026-08-07", 1.0, spread=1.2),
        }
        s = _summary(sessions)
        assert s.spread_n == 5
        assert s.spread_over_baseline_n == 2
        text = weekly.render_slack(s)
        assert f"{weekly.SPREAD_BASELINE_PIPS}超 2/5" in text

    def test_t3_excluded_day_stays_in_spread_not_combined(self) -> None:
        """T3: 08-14 の巨大スプレッドは max に残る。合算 / slip_n には入らない。"""
        sessions = {
            "2026-08-13": _filled("2026-08-13", 1.0, spread=0.5),
            "2026-08-14": _filled("2026-08-14", 2.3, spread=9.0),
        }
        s = _summary(sessions)
        assert s.spread_max == pytest.approx(9.0)
        assert s.spread_max_date == "2026-08-14"
        assert s.spread_max_side == "entry"
        assert s.slip_n == 1
        assert s.combined_n == 1
        assert s.combined_max == pytest.approx(1.0)

    def test_t4_definition_b_pairs_same_observation(self) -> None:
        """T4: 08-31 型。合算 n=1 は SL の adverse + **exit の** spread 乖離。"""
        session = self._sl(
            "2026-08-31",
            entry_slip=3.2,
            sl_slip=-0.2,
            entry_spread=5.0,
            exit_spread=1.2,
            entry_gap_ms=None,
            exit_gap_ms=5016,
        )
        s = _summary({"2026-08-31": session})
        assert s.slip_n == 1
        assert s.combined_n == 1
        assert s.combined_max == pytest.approx(0.2 + 0.7)
        assert s.combined_p50 == pytest.approx(0.9)
        assert s.spread_n == 2, "スプレッド母集団は空ギャップの entry も残す"

    def test_t5_q1_combined_is_6_3_and_not_a_breach(self) -> None:
        """T5: 08-28 型。raw SL −1.8 + spread 5.0 → 1.8 + 4.5 = 6.3。breaches は空。"""
        session = self._sl(
            "2026-08-28",
            entry_slip=1.0,
            sl_slip=-1.8,
            entry_spread=0.5,
            exit_spread=5.0,
            entry_gap_ms=None,
            exit_gap_ms=5000,
        )
        s = _summary({"2026-08-28": session})
        assert s.combined_n == 1
        assert s.combined_max == pytest.approx(6.3)
        assert s.combined_p50 == pytest.approx(6.3)
        assert s.combined_p90 == pytest.approx(6.3)
        assert s.slip_n == 1
        assert s.slip_p90 == pytest.approx(1.8)
        assert s.breaches == []

    def test_t6_combined_p90_does_not_warn(self) -> None:
        """T6: 合算 6.3 が p90 でも Slack の中止条件行は出ない。"""
        session = self._sl(
            "2026-08-28",
            entry_slip=1.0,
            sl_slip=-1.8,
            entry_spread=0.5,
            exit_spread=5.0,
            entry_gap_ms=None,
            exit_gap_ms=5000,
        )
        s = _summary({"2026-08-28": session})
        text = weekly.render_slack(s)
        assert "p90 6.30pips" in text
        assert "max 6.30pips" in text
        assert "中止条件には使わない" in text
        assert "中止条件に抵触" not in text
        assert "⚠️" not in text

    def test_t7_slip_p90_breach_still_fires(self) -> None:
        """T7: 主指標の滑り p90>5.84 は従来どおり breaches。"""
        sessions = {_day(i): _filled(_day(i), slip) for i, slip in enumerate([1.0, 1.0, 1.0, 1.0, 6.5])}
        s = _summary(sessions)
        assert "slip_p90>5.84" in s.breaches
        assert "⚠️ *中止条件に抵触*" in weekly.render_slack(s)

    def test_t8_combined_n_equals_slip_n_on_mixed_week(self) -> None:
        """T8: 除外日 + 08-31 型の混在週でも combined_n == slip_n。"""
        sessions = {
            "2026-08-14": _filled("2026-08-14", 2.3, spread=9.0),
            "2026-08-31": self._sl(
                "2026-08-31",
                entry_slip=3.2,
                sl_slip=-0.2,
                entry_spread=5.0,
                exit_spread=0.2,
                entry_gap_ms=None,
                exit_gap_ms=5016,
            ),
            "2026-09-01": _filled("2026-09-01", 1.2, spread=0.5),
        }
        s = _summary(sessions)
        assert s.combined_n == s.slip_n
        assert s.slip_n == 2  # 08-31 SL + 09-01 entry（08-14 と空ギャップ entry は落ちる）

    def test_t9_spread_max_tie_prefers_earlier_then_entry(self) -> None:
        """T9: 同値なら早い日付。同日なら entry。走査順に依存しない。"""
        later_first = {
            "2026-08-07": _filled("2026-08-07", 1.0, spread=5.0),
            "2026-08-03": _filled("2026-08-03", 1.0, spread=5.0),
        }
        s = _summary(later_first)
        assert s.spread_max == pytest.approx(5.0)
        assert s.spread_max_date == "2026-08-03"
        assert s.spread_max_side == "entry"

        same_day = {
            "2026-08-05": self._eod("2026-08-05", entry_spread=5.0, exit_spread=5.0),
        }
        s2 = _summary(same_day)
        assert s2.spread_max_date == "2026-08-05"
        assert s2.spread_max_side == "entry"

    def test_empty_week_json_nulls_and_zero_counts(self) -> None:
        s = _summary({})
        payload = s.to_jsonable()
        assert payload["spread"]["max"] is None
        assert payload["spread"]["max_at"] is None
        assert payload["spread"]["over_baseline_n"] == 0
        assert payload["combined_cost"]["n"] == 0
        assert payload["combined_cost"]["max"] is None
        text = weekly.render_slack(s)
        assert "identity" not in text


class TestSlackBold:
    """`*text*（...` は Slack が太字にせず生のアスタリスクを出す（実投稿で確認）。

    🔴 **halt あり／なしの両方**を通すこと。片方だけだと、halt 継続中の行に
    同じパターンが残っていても検出できない（レビュー指摘・実際に見逃した）。
    """

    def _assert_bold_ok(self, text: str) -> None:
        import re

        for line in text.splitlines():
            for m in re.finditer(r"\*[^*\n]+\*", line):
                after = line[m.end() : m.end() + 1]
                assert after in ("", " ", ":", "　"), (
                    f"太字の閉じ記号の直後が {after!r}: {line}"
                )

    def test_bold_markers_without_halt(self) -> None:
        self._assert_bold_ok(
            weekly.render_slack(_summary({"2026-08-10": _filled("2026-08-10", 1.0)}))
        )

    @pytest.mark.parametrize("since", [None, "2026-08-07T00:00:00+00:00"])
    def test_bold_markers_with_halt(self, since: str | None) -> None:
        """halt 継続中の行（継続日数が既知／不明の両方）。"""
        s = _summary(
            {"2026-08-10": _skipped("2026-08-10", "halted")},
            halt={"halt": True, "reason": "verify_mismatch:2026-08-07", "since": since},
        )
        text = weekly.render_slack(s)
        assert "halt 継続中" in text
        self._assert_bold_ok(text)
