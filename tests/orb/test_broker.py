"""仮想ブローカー状態機械の単体テスト（§6.1 全パス + 境界5種）。

観測列のみで駆動。API / DynamoDB / S3 には依存しない（永続化は別クラス）。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from backend.src.orb.broker import (
    apply_observation,
    apply_observations,
    demo_signal_class,
    is_fill_before_order_window_pair,
    open_session,
    replay_fill_before_order_window,
    should_count_as_mismatch,
    signals_match_replay,
)
from backend.src.orb.broker_models import BrokerSession, DirectionName, TickerObservation
from backend.src.orb.constants import LON, STOP_BUFFER
from backend.src.orb.models import DayResult, Direction, RangeLevels, Trade


def _lon(y: int, m: int, d: int, hh: int, mm: int, ss: int = 0, ms: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, ss, ms * 1000, tzinfo=LON)


def _obs(
    ts: datetime,
    bid: float,
    *,
    ask: float | None = None,
    status: str = "OPEN",
) -> TickerObservation:
    spread = 0.002 if ask is None else ask - bid
    return TickerObservation(
        ts=ts, bid=bid, ask=bid + spread if ask is None else ask, status=status
    )


def _levels(high: float = 150.20, low: float = 149.80) -> RangeLevels:
    return RangeLevels(
        high=high,
        low=low,
        buy_stop=high + STOP_BUFFER,
        sell_stop=low - STOP_BUFFER,
    )


DAY = "2024-06-03"


def _trade(
    levels: RangeLevels,
    *,
    direction: Direction = 1,
    fill_et: str,
) -> Trade:
    entry = levels.buy_stop if direction == 1 else levels.sell_stop
    sl = levels.sell_stop if direction == 1 else levels.buy_stop
    return Trade(
        day=DAY,
        direction=direction,
        outcome="eod",
        r=1.0 if direction == 1 else -1.0,
        risk_pips=20.0,
        entry=entry,
        exit=150.5,
        sl=sl,
        buy_stop=levels.buy_stop,
        sell_stop=levels.sell_stop,
        fill_et=fill_et,
        range_high=levels.high,
        range_low=levels.low,
    )


def _replay(levels: RangeLevels, trade: Trade) -> DayResult:
    return DayResult(day=DAY, skip_reason=None, trade=trade, range_levels=levels)


def _cancelled(levels: RangeLevels) -> BrokerSession:
    return BrokerSession(session_date=DAY, state="CANCELLED", levels=levels)


def _filled(levels: RangeLevels, *, direction: DirectionName) -> BrokerSession:
    return BrokerSession(
        session_date=DAY,
        state="CLOSED",
        levels=levels,
        direction=direction,
        entry_price=levels.buy_stop if direction == "long" else levels.sell_stop,
        exit_reason="eod",
    )


# ---------------------------------------------------------------------------
# 状態遷移の全パス
# ---------------------------------------------------------------------------


class TestOpenSession:
    def test_idle_to_armed(self) -> None:
        session, events = open_session(DAY, halt=False, levels=_levels(), ts=_lon(2024, 6, 3, 9, 1))
        assert session.state == "ARMED"
        assert session.levels is not None
        assert events[-1].reason == "armed"

    def test_idle_to_skipped_no_range(self) -> None:
        session, events = open_session(DAY, halt=False, levels=None, ts=_lon(2024, 6, 3, 9, 1))
        assert session.state == "SKIPPED"
        assert session.skip_reason == "no_range"
        assert events[-1].reason == "no_range"

    def test_idle_to_skipped_holiday(self) -> None:
        session, _ = open_session(
            DAY,
            halt=False,
            levels=_levels(),
            skip_reason="market_holiday",
            ts=_lon(2024, 6, 3, 9, 1),
        )
        assert session.state == "SKIPPED"
        assert session.skip_reason == "market_holiday"

    def test_halt_skips_even_with_levels(self) -> None:
        session, events = open_session(DAY, halt=True, levels=_levels(), ts=_lon(2024, 6, 3, 9, 1))
        assert session.state == "SKIPPED"
        assert session.skip_reason == "halted"
        assert events[-1].reason == "halted"


class TestArmedToFilledToClosed:
    def test_long_then_sl(self) -> None:
        levels = _levels()
        session, _ = open_session(DAY, halt=False, levels=levels, ts=_lon(2024, 6, 3, 9, 1))
        obs_mid = _obs(_lon(2024, 6, 3, 9, 5), 150.00)
        obs_fill = _obs(
            _lon(2024, 6, 3, 9, 10), levels.buy_stop + 0.005, ask=levels.buy_stop + 0.007
        )
        obs_after = _obs(_lon(2024, 6, 3, 9, 10, 5), levels.buy_stop + 0.004)  # cancel 確定
        obs_sl = _obs(_lon(2024, 6, 3, 10, 0), levels.sell_stop - 0.002)

        session, events = apply_observations(session, [obs_mid, obs_fill, obs_after, obs_sl])
        assert session.state == "CLOSED"
        assert session.direction == "long"
        assert session.exit_reason == "sl"
        assert session.entry_price == pytest.approx(obs_fill.bid)
        assert session.entry_slip is not None
        assert session.entry_slip.slip_pips == pytest.approx(0.5)  # +0.005 / 0.01
        assert session.entry_slip.poll_gap_ms == 5 * 60 * 1000
        assert any(e.reason == "filled" for e in events)
        assert any(e.reason == "sl" for e in events)

    def test_short_then_eod(self) -> None:
        levels = _levels()
        session, _ = open_session(DAY, halt=False, levels=levels, ts=_lon(2024, 6, 3, 9, 1))
        obs_fill = _obs(_lon(2024, 6, 3, 9, 30), levels.sell_stop - 0.003)
        obs_clear = _obs(_lon(2024, 6, 3, 9, 30, 5), levels.sell_stop + 0.01)
        obs_hold = _obs(_lon(2024, 6, 3, 12, 0), 150.00)
        obs_eod = _obs(_lon(2024, 6, 3, 16, 0, 1), 150.05)

        session, events = apply_observations(session, [obs_fill, obs_clear, obs_hold, obs_eod])
        assert session.state == "CLOSED"
        assert session.direction == "short"
        assert session.exit_reason == "eod"
        assert session.exit_price == pytest.approx(150.05)
        assert any(e.reason == "eod" for e in events)

    def test_no_fill_at_11(self) -> None:
        levels = _levels()
        session, _ = open_session(DAY, halt=False, levels=levels, ts=_lon(2024, 6, 3, 9, 1))
        # 通常間隔で 10:59 → 11:00
        obs_a = _obs(_lon(2024, 6, 3, 10, 59, 55), 150.00)
        obs_b = _obs(_lon(2024, 6, 3, 11, 0, 0), 150.00)
        session, events = apply_observations(session, [obs_a, obs_b])
        assert session.state == "CANCELLED"
        assert demo_signal_class(session) == "no_fill"
        assert events[-1].reason == "no_fill"


class TestBothFilledPaths:
    def test_gap_crosses_both_while_armed(self) -> None:
        levels = _levels()
        session, _ = open_session(DAY, halt=False, levels=levels, ts=_lon(2024, 6, 3, 9, 1))
        # 区間が sell_stop と buy_stop を両方覆うギャップ
        prev = _obs(_lon(2024, 6, 3, 9, 5), levels.sell_stop - 0.01)
        curr = _obs(_lon(2024, 6, 3, 9, 5, 5), levels.buy_stop + 0.01)
        session, events = apply_observations(session, [prev, curr])
        assert session.state == "ANOMALY"
        assert session.exit_reason == "both_filled"
        assert events[-1].notify_slack is True
        assert demo_signal_class(session) == "both_filled"

    def test_cancel_latency_opposite_hit(self) -> None:
        levels = _levels()
        session, _ = open_session(DAY, halt=False, levels=levels, ts=_lon(2024, 6, 3, 9, 1))
        fill = _obs(_lon(2024, 6, 3, 9, 10), levels.buy_stop + 0.001)
        # 次サイクルで反対側（売りストップ）到達 → both_filled
        opposite = _obs(_lon(2024, 6, 3, 9, 10, 5), levels.sell_stop - 0.001)
        session, events = apply_observations(session, [fill, opposite])
        assert session.state == "ANOMALY"
        assert session.exit_reason == "both_filled"
        assert any(e.reason == "both_filled" and e.notify_slack for e in events)


# ---------------------------------------------------------------------------
# 境界ケース5種
# ---------------------------------------------------------------------------


class TestBoundary1100:
    def test_fill_just_before_1100(self) -> None:
        levels = _levels()
        session, _ = open_session(DAY, halt=False, levels=levels, ts=_lon(2024, 6, 3, 9, 1))
        # 10:59:59.999 は fill 可
        fill = _obs(_lon(2024, 6, 3, 10, 59, 59, 999), levels.buy_stop)
        session, _ = apply_observation(session, fill)
        assert session.state == "FILLED"
        assert session.direction == "long"

    def test_no_fill_at_exactly_1100(self) -> None:
        levels = _levels()
        session, _ = open_session(DAY, halt=False, levels=levels, ts=_lon(2024, 6, 3, 9, 1))
        mid = _obs(_lon(2024, 6, 3, 10, 59, 55), 150.00)
        # 11:00:00.000 ちょうど — 到達していても fill しない
        late = _obs(_lon(2024, 6, 3, 11, 0, 0), levels.buy_stop + 0.05)
        session, _ = apply_observations(session, [mid, late])
        assert session.state == "CANCELLED"

    def test_uncertain_when_gap_straddles_1100(self) -> None:
        levels = _levels()
        session, _ = open_session(DAY, halt=False, levels=levels, ts=_lon(2024, 6, 3, 9, 1))
        early = _obs(_lon(2024, 6, 3, 10, 58, 0), 150.00)
        late = _obs(_lon(2024, 6, 3, 11, 0, 30), 150.00)  # gap > 10s
        session, events = apply_observations(session, [early, late])
        assert session.state == "UNCERTAIN"
        assert session.uncertain_reason == "uncertain_cancel_boundary"
        assert events[-1].detail.get("poll_gap_ms") == 150_000


class TestMarketClosedGate:
    def test_closed_obs_discarded_and_logged(self) -> None:
        levels = _levels()
        session, _ = open_session(DAY, halt=False, levels=levels, ts=_lon(2024, 6, 3, 9, 1))
        # 閉場中の異常スプレッド（付録B §3 の 10pips）を混ぜても fill しない
        closed = _obs(
            _lon(2024, 6, 3, 9, 10),
            levels.buy_stop + 1.0,
            ask=levels.buy_stop + 1.0 + 0.10,
            status="CLOSE",
        )
        session, events = apply_observation(session, closed)
        assert session.state == "ARMED"
        assert session.last_open_obs is None
        assert events[0].reason == "market_closed"
        assert session.entry_slip is None

    def test_open_after_closed_still_works(self) -> None:
        levels = _levels()
        session, _ = open_session(DAY, halt=False, levels=levels, ts=_lon(2024, 6, 3, 9, 1))
        closed = _obs(_lon(2024, 6, 3, 9, 10), levels.buy_stop + 1.0, status="CLOSE")
        opened = _obs(_lon(2024, 6, 3, 9, 10, 5), levels.buy_stop + 0.001, status="OPEN")
        session, _ = apply_observations(session, [closed, opened])
        assert session.state == "FILLED"


class TestPollGapRecording:
    def test_poll_gap_on_fill(self) -> None:
        levels = _levels()
        session, _ = open_session(DAY, halt=False, levels=levels, ts=_lon(2024, 6, 3, 9, 1))
        a = _obs(_lon(2024, 6, 3, 9, 5, 0), 150.00)
        # 欠測: 5s 想定が 20s 空いた
        b = _obs(_lon(2024, 6, 3, 9, 5, 20), levels.buy_stop + 0.002)
        session, _ = apply_observations(session, [a, b])
        assert session.entry_slip is not None
        assert session.entry_slip.poll_gap_ms == 20_000
        assert session.entry_slip.price_gap == pytest.approx(b.bid - a.bid)
        assert session.entry_slip.spread_pips == pytest.approx(0.2)


class TestHaltBoundary:
    def test_halt_true_no_virtual_order(self) -> None:
        session, _ = open_session(DAY, halt=True, levels=_levels(), ts=_lon(2024, 6, 3, 9, 1))
        assert session.state == "SKIPPED"
        assert demo_signal_class(session) == "skipped_other"


# ---------------------------------------------------------------------------
# §8.2 写像
# ---------------------------------------------------------------------------


class TestSignalMatchMapping:
    def test_ambiguous_both_matches_both_filled(self) -> None:
        levels = _levels()
        session, _ = open_session(DAY, halt=False, levels=levels, ts=_lon(2024, 6, 3, 9, 1))
        prev = _obs(_lon(2024, 6, 3, 9, 5), levels.sell_stop - 0.01)
        curr = _obs(_lon(2024, 6, 3, 9, 5, 5), levels.buy_stop + 0.01)
        session, _ = apply_observations(session, [prev, curr])
        replay = DayResult(
            day=DAY,
            skip_reason="ambiguous_both",
            trade=None,
            range_levels=levels,
        )
        assert signals_match_replay(replay, session) == "match"
        assert should_count_as_mismatch(replay, session) is False

    def test_uncertain_is_defer_not_mismatch(self) -> None:
        """§8.2.1: UNCERTAIN は分類不一致に数えない（偽陽性 halt 防止）。"""
        levels = _levels()
        session, _ = open_session(DAY, halt=False, levels=levels, ts=_lon(2024, 6, 3, 9, 1))
        early = _obs(_lon(2024, 6, 3, 10, 58, 0), 150.00)
        late = _obs(_lon(2024, 6, 3, 11, 0, 30), 150.00)
        session, _ = apply_observations(session, [early, late])
        assert session.state == "UNCERTAIN"
        assert demo_signal_class(session) == "uncertain"

        replay_no_fill = DayResult(day=DAY, skip_reason="no_fill", trade=None, range_levels=levels)
        replay_no_range = DayResult(
            day=DAY, skip_reason="no_range", trade=None, range_levels=levels
        )
        replay_both = DayResult(
            day=DAY, skip_reason="ambiguous_both", trade=None, range_levels=levels
        )
        for replay in (replay_no_fill, replay_no_range, replay_both):
            assert signals_match_replay(replay, session) == "defer"
            assert should_count_as_mismatch(replay, session) is False

        trade = Trade(
            day=DAY,
            direction=1,
            outcome="eod",
            r=1.0,
            risk_pips=20.0,
            entry=levels.buy_stop,
            exit=150.5,
            sl=levels.sell_stop,
            buy_stop=levels.buy_stop,
            sell_stop=levels.sell_stop,
            fill_et="2024-06-03T09:10:00+01:00",
            range_high=levels.high,
            range_low=levels.low,
        )
        replay_fill = DayResult(day=DAY, skip_reason=None, trade=trade, range_levels=levels)
        assert signals_match_replay(replay_fill, session) == "defer"
        assert should_count_as_mismatch(replay_fill, session) is False

    def test_cancel_minute_fill_vs_demo_no_fill_is_known_divergence(self) -> None:
        """オラクル 11:00 バー fill ↔ デモ no_fill は既知乖離として match（§8.2.1）。"""
        levels = _levels()
        session, _ = open_session(DAY, halt=False, levels=levels, ts=_lon(2024, 6, 3, 9, 1))
        mid = _obs(_lon(2024, 6, 3, 10, 59, 55), 150.00)
        # 11:00:00 ちょうど — デモは fill せず CANCELLED
        at_cancel = _obs(_lon(2024, 6, 3, 11, 0, 0), levels.buy_stop + 0.05)
        session, _ = apply_observations(session, [mid, at_cancel])
        assert session.state == "CANCELLED"
        assert demo_signal_class(session) == "no_fill"

        trade = Trade(
            day=DAY,
            direction=1,
            outcome="eod",
            r=0.5,
            risk_pips=20.0,
            entry=levels.buy_stop,
            exit=150.5,
            sl=levels.sell_stop,
            buy_stop=levels.buy_stop,
            sell_stop=levels.sell_stop,
            # オラクルは openTime==11:00 のバーを評価対象に含む
            fill_et="2024-06-03T11:00:00+01:00",
            range_high=levels.high,
            range_low=levels.low,
        )
        replay = DayResult(day=DAY, skip_reason=None, trade=trade, range_levels=levels)
        assert signals_match_replay(replay, session) == "match"
        assert should_count_as_mismatch(replay, session) is False

    def test_cancel_latency_both_filled_matches_replay_fill(self) -> None:
        """cancel_latency の both_filled はリプレイ側 fill と match（既知乖離）。"""
        levels = _levels()
        session, _ = open_session(DAY, halt=False, levels=levels, ts=_lon(2024, 6, 3, 9, 1))
        fill = _obs(_lon(2024, 6, 3, 9, 10), levels.buy_stop + 0.001)
        opposite = _obs(_lon(2024, 6, 3, 9, 10, 5), levels.sell_stop - 0.001)
        session, _ = apply_observations(session, [fill, opposite])
        assert session.state == "ANOMALY"

        trade = Trade(
            day=DAY,
            direction=1,
            outcome="sl",
            r=-1.0,
            risk_pips=20.0,
            entry=levels.buy_stop,
            exit=levels.sell_stop,
            sl=levels.sell_stop,
            buy_stop=levels.buy_stop,
            sell_stop=levels.sell_stop,
            fill_et="2024-06-03T09:10:00+01:00",
            range_high=levels.high,
            range_low=levels.low,
        )
        replay = DayResult(day=DAY, skip_reason=None, trade=trade, range_levels=levels)
        assert signals_match_replay(replay, session) == "match"
        assert should_count_as_mismatch(replay, session) is False

    def test_long_matches(self) -> None:
        levels = _levels()
        session, _ = open_session(DAY, halt=False, levels=levels, ts=_lon(2024, 6, 3, 9, 1))
        fill = _obs(_lon(2024, 6, 3, 9, 10), levels.buy_stop)
        clear = _obs(_lon(2024, 6, 3, 9, 10, 5), levels.buy_stop + 0.01)
        eod = _obs(_lon(2024, 6, 3, 16, 0), 150.5)
        session, _ = apply_observations(session, [fill, clear, eod])
        trade = Trade(
            day=DAY,
            direction=1,
            outcome="eod",
            r=1.0,
            risk_pips=20.0,
            entry=levels.buy_stop,
            exit=150.5,
            sl=levels.sell_stop,
            buy_stop=levels.buy_stop,
            sell_stop=levels.sell_stop,
            fill_et="2024-06-03T09:10:00+01:00",
            range_high=levels.high,
            range_low=levels.low,
        )
        replay = DayResult(day=DAY, skip_reason=None, trade=trade, range_levels=levels)
        assert signals_match_replay(replay, session) == "match"

    def test_mismatch_long_vs_short(self) -> None:
        levels = _levels()
        session, _ = open_session(DAY, halt=False, levels=levels, ts=_lon(2024, 6, 3, 9, 1))
        fill = _obs(_lon(2024, 6, 3, 9, 10), levels.buy_stop)
        session, _ = apply_observation(session, fill)
        trade = Trade(
            day=DAY,
            direction=-1,
            outcome="sl",
            r=-1.0,
            risk_pips=20.0,
            entry=levels.sell_stop,
            exit=levels.buy_stop,
            sl=levels.buy_stop,
            buy_stop=levels.buy_stop,
            sell_stop=levels.sell_stop,
            fill_et="2024-06-03T09:10:00+01:00",
            range_high=levels.high,
            range_low=levels.low,
        )
        replay = DayResult(day=DAY, skip_reason=None, trade=trade, range_levels=levels)
        assert signals_match_replay(replay, session) == "mismatch"
        assert should_count_as_mismatch(replay, session) is True

    def test_t1_0900_long_vs_no_fill_is_defer(self) -> None:
        """T1: 09:00 バー fill（BST）+ デモ no_fill → defer。halt カウントしない。"""
        levels = _levels()
        replay = _replay(levels, _trade(levels, fill_et="2024-06-03T09:00:00+01:00"))
        demo = _cancelled(levels)
        assert replay.trade is not None
        assert replay_fill_before_order_window(replay.trade) is True
        assert is_fill_before_order_window_pair(replay, demo) is True
        assert signals_match_replay(replay, demo) == "defer"
        assert should_count_as_mismatch(replay, demo) is False

    def test_t2_0900_long_vs_short_is_defer(self) -> None:
        """T2: 09:00 fill + デモ逆方向 → defer（再ブレイク失敗 / 逆側先着）。"""
        levels = _levels()
        replay = _replay(levels, _trade(levels, fill_et="2024-06-03T09:00:00+01:00"))
        demo = _filled(levels, direction="short")
        assert signals_match_replay(replay, demo) == "defer"
        assert should_count_as_mismatch(replay, demo) is False

    def test_t3_0901_short_vs_no_fill_is_defer_gmt(self) -> None:
        """T3: 09:01 バー fill（GMT）+ デモ no_fill → defer（#307 42 秒窓）。"""
        levels = _levels()
        replay = _replay(levels, _trade(levels, direction=-1, fill_et="2025-01-13T09:01:00+00:00"))
        demo = _cancelled(levels)
        assert replay.trade is not None
        assert replay_fill_before_order_window(replay.trade) is True
        assert signals_match_replay(replay, demo) == "defer"
        assert should_count_as_mismatch(replay, demo) is False

    def test_t4_0920_long_vs_no_fill_is_mismatch(self) -> None:
        """T4: 09:20 fill + デモ no_fill は本物の取りこぼし → mismatch。"""
        levels = _levels()
        replay = _replay(levels, _trade(levels, fill_et="2024-06-03T09:20:00+01:00"))
        demo = _cancelled(levels)
        assert replay.trade is not None
        assert replay_fill_before_order_window(replay.trade) is False
        assert signals_match_replay(replay, demo) == "mismatch"
        assert should_count_as_mismatch(replay, demo) is True

    def test_t5_0900_same_direction_delayed_fill_is_match(self) -> None:
        """T5: 09:00 fill + デモ同方向（遅延 fill）は match のまま。"""
        levels = _levels()
        replay = _replay(levels, _trade(levels, fill_et="2024-06-03T09:00:00+01:00"))
        demo = _filled(levels, direction="long")
        assert is_fill_before_order_window_pair(replay, demo) is False
        assert signals_match_replay(replay, demo) == "match"
        assert should_count_as_mismatch(replay, demo) is False

    def test_t7_1100_known_divergence_stays_match(self) -> None:
        """T7: 11:00 既知乖離は match のまま（09:00 分岐で吞まない）。"""
        levels = _levels()
        replay = _replay(levels, _trade(levels, fill_et="2024-06-03T11:00:00+01:00"))
        demo = _cancelled(levels)
        assert replay.trade is not None
        assert replay_fill_before_order_window(replay.trade) is False
        assert signals_match_replay(replay, demo) == "match"
        assert should_count_as_mismatch(replay, demo) is False

    def test_t8_uncertain_plus_0900_fill_stays_uncertain_defer(self) -> None:
        """T8: UNCERTAIN + 09:00 fill は既存の defer。本クラスに再分類しない。"""
        levels = _levels()
        session, _ = open_session(DAY, halt=False, levels=levels, ts=_lon(2024, 6, 3, 9, 1))
        early = _obs(_lon(2024, 6, 3, 10, 58, 0), 150.00)
        late = _obs(_lon(2024, 6, 3, 11, 0, 30), 150.00)
        session, _ = apply_observations(session, [early, late])
        assert demo_signal_class(session) == "uncertain"
        replay = _replay(levels, _trade(levels, fill_et="2024-06-03T09:00:00+01:00"))
        assert is_fill_before_order_window_pair(replay, session) is False
        assert signals_match_replay(replay, session) == "defer"
        assert should_count_as_mismatch(replay, session) is False

    def test_jst_0900_clock_is_not_london_order_window(self) -> None:
        """素の JST 09:00 表記を London 09:00 と見なさない。"""
        levels = _levels()
        trade = _trade(levels, fill_et="2024-06-03T09:00:00+09:00")
        assert replay_fill_before_order_window(trade) is False
        replay = _replay(levels, trade)
        demo = _cancelled(levels)
        assert signals_match_replay(replay, demo) == "mismatch"

    def test_hhmm_shorthand_0900_is_before_order_window(self) -> None:
        levels = _levels()
        trade = _trade(levels, fill_et="09:00")
        assert replay_fill_before_order_window(trade) is True


class TestTerminalNoop:
    def test_closed_ignores_further_obs(self) -> None:
        levels = _levels()
        session, _ = open_session(DAY, halt=False, levels=levels, ts=_lon(2024, 6, 3, 9, 1))
        fill = _obs(_lon(2024, 6, 3, 9, 10), levels.buy_stop)
        clear = _obs(_lon(2024, 6, 3, 9, 10, 5), levels.buy_stop + 0.01)
        sl = _obs(_lon(2024, 6, 3, 10, 0), levels.sell_stop - 0.01)
        session, _ = apply_observations(session, [fill, clear, sl])
        assert session.state == "CLOSED"
        later, events = apply_observation(session, _obs(_lon(2024, 6, 3, 11, 0), 149.0))
        assert later.state == "CLOSED"
        assert events == []
