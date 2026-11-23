"""backtest/engine.py のユニットテスト（BT-2）。

受け入れ条件（#152）:
  - 同一バー SL/TP 競合（SL 優先）・ギャップ約定・no_fill・週次クローズ時刻
  - pre_order reject は「発注せず記録」（ガードレール #10 と同挙動）
  - stop_loss は enrich.py 経由でのみ決定（ガードレール #5）
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from backend.src.engine.enrich import calculate_stop_loss, calculate_take_profit
from backend.src.llm.trade_decision import TradeDecision
from backtest.data import KlinesStore
from backtest.engine import (
    MORNING_SESSION,
    NIGHT_SESSION,
    SESSION_VOL_ATR_MULT,
    SESSION_VOL_HOLD_HOURS,
    SESSION_VOL_RR_TARGET,
    Bar5m,
    SessionWindow,
    SimConfig,
    TradeResult,
    closeout_ms_for,
    load_session_bars,
    round_tick_entry,
    simulate_day,
)


def session_vol_stop_loss(
    entry: float, atr: float, side: str, *, hold_hours: float, atr_mult: float
) -> float:
    """engine が使う本番 enrich.calculate_stop_loss（数理の単一の正）。"""
    return calculate_stop_loss(entry, atr, atr_mult, side, hold_hours=hold_hours)  # type: ignore[arg-type]


def session_vol_take_profit(entry: float, sl: float, side: str, *, rr_target: float) -> float:
    return calculate_take_profit(entry, sl, rr_target, side)  # type: ignore[arg-type]

_JST = ZoneInfo("Asia/Tokyo")

_DAY = date(2026, 7, 1)  # 水曜・非休場日
_CUTOFF_MS = int(datetime.combine(_DAY, time(8, 0), tzinfo=_JST).timestamp() * 1_000)

# テスト共通条件（Ver 3.6 session_vol）: entry=155.5、既定 atr_mult=0.7・rr_target=1.0。
# _ATR は SL 幅 = 0.7×ATR_h = 0.7×ATR14×√(8/24) = 0.6 になる値を選ぶ（従来 SL=154.900 を維持）。
#   LONG:  SL=154.900（risk 60pips）/ TP=156.100（reward 60pips, R:R=1.0）。
_ATR = 0.6 / (0.7 * math.sqrt(8.0 / 24.0))  # ≈ 1.48462（ATR_h≈0.857・SL 幅 0.6）
_EQUITY = 500_000.0
_CURRENT = 155.8


@pytest.fixture(autouse=True)
def _fixed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """サイジング・バリデーションの環境変数を決定論に固定する。"""
    monkeypatch.setenv("RISK_PERCENT", "0.01")
    monkeypatch.setenv("MAX_LEVERAGE", "10")
    monkeypatch.setenv("MIN_RR_RATIO", "1.0")
    monkeypatch.setenv("MVP_MODE", "False")


def _jst_ms(d: date, hour: int, minute: int = 0) -> int:
    return int(datetime.combine(d, time(hour, minute), tzinfo=_JST).timestamp() * 1_000)


def _bar(
    t_ms: int,
    o: float,
    h: float,
    lo: float,
    c: float,
    spread: float = 0.010,
) -> Bar5m:
    """BID OHLC を指定し ASK は spread 分上に平行シフトしたバーを作る。"""
    return Bar5m(
        open_time_ms=t_ms,
        bid_open=o,
        bid_high=h,
        bid_low=lo,
        bid_close=c,
        ask_open=o + spread,
        ask_high=h + spread,
        ask_low=lo + spread,
        ask_close=c + spread,
    )


def _decision(
    direction: str = "LONG",
    order_type: str = "LIMIT",
    entry: float = 155.5,
    tp: float = 156.5,  # session_vol では engine が TP を上書きするため無視（後方互換の呼び出し用）
) -> TradeDecision:
    del tp  # SL/TP は engine の session_vol（本番 enrich）が決定論算出する（Ver 3.6）
    return TradeDecision(
        rationale="test",
        direction=direction,  # type: ignore[arg-type]
        order_type=order_type,  # type: ignore[arg-type]
        entry_price=entry,
        sl_profile="standard",
        rr_target=1.0,
        execution_required=True,
    )


def _run(
    bars: list[Bar5m] | None,
    decision: TradeDecision | None = None,
    config: SimConfig | None = None,
) -> TradeResult:
    return simulate_day(
        day=_DAY,
        decision=decision or _decision(),
        atr=_ATR,
        equity_jpy=_EQUITY,
        current_price=_CURRENT,
        cutoff_ms=_CUTOFF_MS,
        bars=bars,
        config=config,
    )


class TestEntryFill:
    def test_limit_long_fill_then_tp(self) -> None:
        bars = [
            # ask_low = 155.61 > entry → 未約定
            _bar(_jst_ms(_DAY, 8, 0), 155.8, 155.9, 155.6, 155.7),
            # ask_low = 155.50 = entry → touch 約定
            _bar(_jst_ms(_DAY, 8, 5), 155.7, 155.75, 155.49, 155.6),
            # bid_high = 156.6 >= TP 156.1 → 利確
            _bar(_jst_ms(_DAY, 8, 10), 155.6, 156.6, 155.55, 156.4),
        ]
        r = _run(bars)
        assert r.status == "tp"
        assert r.filled_at_ms == _jst_ms(_DAY, 8, 5)
        assert r.fill_price == 155.5  # LIMIT は指値どおり
        assert r.exit_price == pytest.approx(156.1)  # session_vol TP（rr=1.0 × SL 幅 0.6）
        assert r.pnl_pips == pytest.approx(60.0)
        assert r.r_multiple == pytest.approx(1.0, rel=1e-3)

    def test_no_fill_is_cancelled(self) -> None:
        bars = [_bar(_jst_ms(_DAY, 8, 0), 155.8, 155.9, 155.6, 155.7)] * 3
        r = _run(bars)
        assert r.status == "no_fill"
        assert r.filled_at_ms is None
        assert r.pnl_pips is None

    def test_stop_short_fill_then_tp(self) -> None:
        # SHORT STOP: entry 155.0 を bid が下抜けたら売り。SL=155.6, TP=154.4（session_vol）
        decision = _decision(direction="SHORT", order_type="STOP", entry=155.0)
        bars = [
            # bid_low = 155.1 > 155.0 → 未約定
            _bar(_jst_ms(_DAY, 8, 0), 155.3, 155.4, 155.1, 155.2),
            # bid_low = 154.95 <= 155.0 → 約定（始値 155.2 は entry より有利側 → 指値どおり）
            _bar(_jst_ms(_DAY, 8, 5), 155.2, 155.25, 154.95, 155.0),
            # ask_low = 153.96 <= TP 154.4 → 利確
            _bar(_jst_ms(_DAY, 8, 10), 155.0, 155.05, 153.95, 154.1),
        ]
        r = _run(bars, decision=decision)
        assert r.status == "tp"
        assert r.fill_price == 155.0
        assert r.exit_price == pytest.approx(154.4)  # session_vol TP（rr=1.0 × SL 幅 0.6）
        assert r.pnl_pips == pytest.approx(60.0)

    def test_strict_mode_requires_penetration(self) -> None:
        touch_only = [_bar(_jst_ms(_DAY, 8, 0), 155.7, 155.8, 155.49, 155.6)]  # ask_low = 155.50
        r = _run(touch_only, config=SimConfig(fill_mode="strict"))
        assert r.status == "no_fill"

        penetrated = [_bar(_jst_ms(_DAY, 8, 0), 155.7, 155.8, 155.489, 155.6)]  # ask_low = 155.499
        r2 = _run(penetrated, config=SimConfig(fill_mode="strict"))
        assert r2.status == "closeout"  # 約定して手仕舞いまで保有

    def test_stop_long_gap_entry_fills_at_open(self) -> None:
        # STOP LONG entry 155.9。バー始値 ask=156.01 が entry を上抜けてギャップ → 始値で約定
        decision = _decision(order_type="STOP", entry=155.9)
        bars = [_bar(_jst_ms(_DAY, 8, 0), 156.0, 156.2, 155.95, 156.1)]
        r = _run(bars, decision=decision)
        assert r.status == "closeout"  # SL 155.3 / TP 156.5 には届かず手仕舞い
        assert r.fill_price == pytest.approx(156.01)  # ask_open（不利側）


class TestOcoPriority:
    def test_same_bar_tp_and_sl_prefers_sl(self) -> None:
        bars = [
            _bar(_jst_ms(_DAY, 8, 0), 155.7, 155.75, 155.49, 155.6),  # 約定
            # bid_low 154.85 <= SL 154.9 かつ bid_high 156.6 >= TP 156.1 → SL 優先
            _bar(_jst_ms(_DAY, 8, 5), 155.6, 156.6, 154.85, 155.0),
        ]
        r = _run(bars)
        assert r.status == "sl"
        assert r.exit_price == pytest.approx(154.9)
        assert r.pnl_pips == pytest.approx(-60.0)
        assert r.r_multiple == pytest.approx(-1.0, rel=1e-3)

    def test_entry_and_sl_in_same_bar(self) -> None:
        # 1 本のバーで ask_low が entry まで下げ、bid_low が SL も割る → エントリー→即 SL
        bars = [_bar(_jst_ms(_DAY, 8, 0), 155.7, 155.8, 154.8, 155.0)]
        r = _run(bars)
        assert r.status == "sl"
        assert r.filled_at_ms == r.exit_at_ms

    def test_gap_below_sl_exits_at_open(self) -> None:
        bars = [
            _bar(_jst_ms(_DAY, 8, 0), 155.7, 155.75, 155.49, 155.6),  # 約定
            # 始値 154.5 が SL 154.9 を下回ってギャップ → 始値（不利側）で exit
            _bar(_jst_ms(_DAY, 8, 5), 154.5, 154.6, 154.4, 154.45),
        ]
        r = _run(bars)
        assert r.status == "sl"
        assert r.exit_price == pytest.approx(154.5)
        assert r.pnl_pips == pytest.approx(-100.0)

    def test_sl_exit_applies_slippage_adversely(self) -> None:
        bars = [
            _bar(_jst_ms(_DAY, 8, 0), 155.7, 155.75, 155.49, 155.6),
            _bar(_jst_ms(_DAY, 8, 5), 155.0, 155.1, 154.85, 154.95),
        ]
        r = _run(bars, config=SimConfig(slippage_pips=1.0))
        assert r.status == "sl"
        assert r.exit_price == pytest.approx(154.89)  # SL 154.9 - 1pip
        assert r.pnl_pips == pytest.approx(-61.0)


class TestCloseout:
    def test_closeout_at_last_session_bar(self) -> None:
        bars = [
            _bar(_jst_ms(_DAY, 8, 0), 155.7, 155.75, 155.49, 155.6),  # 約定
            _bar(_jst_ms(_DAY, 12, 0), 155.6, 155.9, 155.5, 155.85),
            _bar(_jst_ms(_DAY, 23, 55), 155.85, 155.95, 155.8, 155.9),  # 最終バー
        ]
        r = _run(bars)
        assert r.status == "closeout"
        # exit 時刻は最終バーの終了時刻（= 翌日 00:00 の cron 手仕舞い時刻）
        assert r.exit_at_ms == _jst_ms(_DAY, 23, 55) + 5 * 60 * 1_000
        assert r.exit_price == pytest.approx(155.9)  # bid_close
        assert r.pnl_pips == pytest.approx(40.0)


class TestNonTradeStatuses:
    def test_no_entry(self) -> None:
        decision = TradeDecision(
            rationale="見送り",
            direction="NO_ENTRY",
            execution_required=False,
            sl_profile="standard",
        )
        r = _run([], decision=decision)
        assert r.status == "no_entry"

    def test_rr_never_rejects_under_session_vol(self) -> None:
        """session_vol は TP を rr_target×|entry−SL| で construction するため R:R reject は起きない。

        旧数理では decision.take_profit が近すぎると R:R 違反で reject し得たが、Ver 3.6 では
        TP が SL 幅から決定論算出されるため R:R は常に rr_target 以上（reject 構造ゼロ・§3.4）。
        """
        for entry, direction in [(155.5, "LONG"), (156.0, "SHORT")]:
            decision = _decision(direction=direction, entry=entry)
            r = _run(None, decision=decision)  # bars=None → no_data（rejected ではない）
            assert r.status != "rejected", f"session_vol で reject: {r.detail}"

    def test_lot_zero_when_equity_insufficient(self) -> None:
        r = simulate_day(
            day=_DAY,
            decision=_decision(),
            atr=_ATR,
            equity_jpy=5_000.0,  # l_max=50 円 → v_rounded=0
            current_price=_CURRENT,
            cutoff_ms=_CUTOFF_MS,
            bars=[],
        )
        assert r.status == "lot_zero"

    def test_no_data_when_bars_missing(self) -> None:
        r = _run(None)
        assert r.status == "no_data"

    @pytest.mark.parametrize("bad_atr", [0.0, -0.1, float("nan"), float("inf")])
    def test_non_positive_atr_is_rejected_not_crash(self, bad_atr: float) -> None:
        """ATR 非正/非有限は sizing の ValueError で run を落とさず rejected で記録する。"""
        r = simulate_day(
            day=_DAY,
            decision=_decision(),
            atr=bad_atr,
            equity_jpy=_EQUITY,
            current_price=_CURRENT,
            cutoff_ms=_CUTOFF_MS,
            bars=[],
        )
        assert r.status == "rejected"
        assert "ATR" in r.detail


class TestSimConfigValidation:
    def test_negative_slippage_rejected(self) -> None:
        """負の slippage は悲観側が有利側に反転するため拒否（実行時検証）。"""
        with pytest.raises(ValueError, match="slippage_pips"):
            SimConfig(slippage_pips=-1.0)

    @pytest.mark.parametrize("ratio", [0.0, -0.5, 1.5])
    def test_available_ratio_out_of_range_rejected(self, ratio: float) -> None:
        with pytest.raises(ValueError, match="available_ratio"):
            SimConfig(available_ratio=ratio)

    def test_unknown_fill_mode_rejected(self) -> None:
        with pytest.raises(ValueError, match="fill_mode"):
            SimConfig(fill_mode="optimistic")  # type: ignore[arg-type]

    def test_nonpositive_hold_hours_rejected(self) -> None:
        with pytest.raises(ValueError, match="hold_hours"):
            SimConfig(hold_hours=0.0)

    @pytest.mark.parametrize("mult", [0.5, 1.1])
    def test_session_vol_atr_mult_out_of_range_rejected(self, mult: float) -> None:
        """凍結範囲 [0.6, 1.0] 外はクランプせず拒否（ズレの黙認を防ぐ）。"""
        with pytest.raises(ValueError, match="session_vol_atr_mult"):
            SimConfig(session_vol_atr_mult=mult)

    @pytest.mark.parametrize("rr", [0.9, 1.6])
    def test_session_vol_rr_target_out_of_range_rejected(self, rr: float) -> None:
        with pytest.raises(ValueError, match="session_vol_rr_target"):
            SimConfig(session_vol_rr_target=rr)


class TestSessionVolMath:
    """新数理 SL/TP（RD-2 #167）。ATR_h = ATR14×√(hold/24)・R:R construction 担保。"""

    def test_stop_loss_uses_scaled_atr(self) -> None:
        """SL 幅 = atr_mult × ATR14 × √(hold/24)。0.7×√(8/24) ≈ 0.404×ATR14。"""
        atr14 = 1.0
        sl = session_vol_stop_loss(155.0, atr14, "LONG", hold_hours=8.0, atr_mult=0.7)
        expected_width = 0.7 * atr14 * math.sqrt(8.0 / 24.0)
        assert 155.0 - sl == pytest.approx(expected_width, abs=1e-3)  # tick 丸め許容
        assert sl < 155.0  # LONG は entry 下

    def test_stop_loss_short_is_above_entry(self) -> None:
        sl = session_vol_stop_loss(155.0, 1.0, "SHORT", hold_hours=8.0, atr_mult=0.7)
        assert sl > 155.0

    @pytest.mark.parametrize("bad_mult", [0.5, 1.5])
    def test_stop_loss_clamps_atr_mult(self, bad_mult: float) -> None:
        """関数レベルでも 0.6〜1.0 にクランプ（SimConfig を経由しない直接呼び出し防御）。"""
        clamped = max(0.6, min(1.0, bad_mult))
        sl = session_vol_stop_loss(155.0, 1.0, "LONG", hold_hours=8.0, atr_mult=bad_mult)
        expected = 155.0 - clamped * math.sqrt(8.0 / 24.0)
        assert 155.0 - sl == pytest.approx(155.0 - round_tick_entry(expected, "LONG"), abs=1e-6)

    def test_take_profit_enforces_rr(self) -> None:
        """TP 距離 = rr_target × SL 距離（R:R を construction で担保 → reject ゼロ）。"""
        entry, sl = 155.0, 154.6  # risk 0.4
        tp = session_vol_take_profit(entry, sl, "LONG", rr_target=1.0)
        assert tp - entry == pytest.approx(entry - sl, abs=1e-3)
        assert tp > entry

    def test_take_profit_short(self) -> None:
        entry, sl = 155.0, 155.4
        tp = session_vol_take_profit(entry, sl, "SHORT", rr_target=1.0)
        assert entry - tp == pytest.approx(sl - entry, abs=1e-3)
        assert tp < entry

    def test_frozen_constants(self) -> None:
        """凍結パラメータの既定値（チューニング防止のゴールデン）。"""
        assert (SESSION_VOL_HOLD_HOURS, SESSION_VOL_ATR_MULT, SESSION_VOL_RR_TARGET) == (
            8.0,
            0.7,
            1.0,
        )


class TestSessionVolSimulation:
    """simulate_day の session_vol SL/TP 算出（凍結 vs decision 倍率・#187）。"""

    def test_frozen_mode_overrides_decision_tp_and_sl(self) -> None:
        """凍結ベースライン（既定）では decision の倍率を無視し cfg 凍結値で算出。"""
        # decision は TP=160.0（遠すぎ）を出すが、session_vol は新数理で上書きする
        decision = _decision(direction="LONG", entry=155.5, tp=160.0)
        result = _run(bars=None, decision=decision, config=SimConfig())
        # bars=None なので no_data だが SL/TP は base に確定済み
        assert result.status in ("no_data", "lot_zero", "rejected")
        expected_sl = session_vol_stop_loss(155.5, _ATR, "LONG", hold_hours=8.0, atr_mult=0.7)
        expected_tp = session_vol_take_profit(155.5, expected_sl, "LONG", rr_target=1.0)
        assert result.stop_loss == expected_sl
        assert result.take_profit == expected_tp
        assert result.take_profit != round_tick_entry(160.0, "LONG")  # decision の TP は使われない

    def test_frozen_mode_uses_cfg_params_not_decision(self) -> None:
        """凍結モードでは cfg.session_vol_* を使い、decision の倍率は無視する。

        decision と cfg を **意図的に別値** にし、SL/TP が cfg 側で算出されることを検証する
        （CodeRabbit #179: 既定値一致で契約が判別不能になるのを防ぐゴールデン）。
        """
        # decision: sl_profile=wide（→0.8）/ rr_target=1.5（上限）。cfg: atr_mult=0.6 / rr=1.0。
        decision = TradeDecision(
            rationale="test",
            direction="LONG",
            order_type="LIMIT",
            entry_price=155.5,
            sl_profile="wide",
            rr_target=1.5,
            execution_required=True,
        )
        cfg = SimConfig(session_vol_atr_mult=0.6, session_vol_rr_target=1.0)
        result = _run(bars=None, decision=decision, config=cfg)
        # cfg 側（0.6 / 1.0）で算出されること
        cfg_sl = session_vol_stop_loss(155.5, _ATR, "LONG", hold_hours=8.0, atr_mult=0.6)
        cfg_tp = session_vol_take_profit(155.5, cfg_sl, "LONG", rr_target=1.0)
        assert result.stop_loss == cfg_sl
        assert result.take_profit == cfg_tp
        # decision 側（0.8 / 1.5）とは異なること（契約の判別）
        dec_sl = session_vol_stop_loss(155.5, _ATR, "LONG", hold_hours=8.0, atr_mult=0.8)
        assert result.stop_loss != dec_sl

    def test_decision_mode_uses_decision_params(self) -> None:
        """use_decision_sizing_params=True では decision の倍率で SL/TP を算出（本番同挙動）。"""
        decision = TradeDecision(
            rationale="test",
            direction="LONG",
            order_type="LIMIT",
            entry_price=155.5,
            sl_profile="wide",
            rr_target=1.5,
            execution_required=True,
        )
        cfg = SimConfig(
            use_decision_sizing_params=True,
            session_vol_atr_mult=0.6,
            session_vol_rr_target=1.0,
        )
        result = _run(bars=None, decision=decision, config=cfg)
        dec_sl = session_vol_stop_loss(155.5, _ATR, "LONG", hold_hours=8.0, atr_mult=0.8)
        dec_tp = session_vol_take_profit(155.5, dec_sl, "LONG", rr_target=1.5)
        assert result.stop_loss == dec_sl
        assert result.take_profit == dec_tp


class TestSessionWindow:
    """セッション窓の構築時検証と朝型/夜型プリセット（RD-1 #166）。"""

    def test_session_window_dataclass_default_is_night_preset(self) -> None:
        """SessionWindow() 既定 = 本番と同じ夜型（RunConfig・CLI・context の既定と揃える）。"""
        assert SessionWindow() == NIGHT_SESSION
        assert MORNING_SESSION.as_dict() == {
            "decision_time": "08:00",
            "closeout_time": "00:00",
            "friday_closeout_time": "05:00",
        }

    def test_night_preset(self) -> None:
        assert NIGHT_SESSION.as_dict() == {
            "decision_time": "20:30",
            "closeout_time": "04:30",
            "friday_closeout_time": "04:30",
        }

    def test_from_dict_roundtrip(self) -> None:
        assert SessionWindow.from_dict(NIGHT_SESSION.as_dict()) == NIGHT_SESSION

    @pytest.mark.parametrize("bad_time", [time(5, 30), time(6, 0)])
    def test_decision_before_0605_rejected(self, bad_time: time) -> None:
        """cutoff=06:00 だと確定 5 分足が 0 本（openTime+5分<=cutoff を満たす足なし）。

        5 分足ファイルは JST 06:00 起点のため、判断は最短でも 06:05 が必要。
        """
        with pytest.raises(ValueError, match="decision_time"):
            SessionWindow(decision_time=bad_time)

    def test_decision_at_0605_accepted(self) -> None:
        """境界: 06:05 は確定足 1 本（06:00 足）が得られるため許可される。"""
        assert SessionWindow(decision_time=time(6, 5)).decision_time == time(6, 5)

    @pytest.mark.parametrize("field_name", ["closeout_time", "friday_closeout_time"])
    def test_closeout_after_file_end_rejected(self, field_name: str) -> None:
        """翌 05:55 超の closeout は当日ファイル外 → バー欠落で悲観側規約が破れるため拒否。"""
        with pytest.raises(ValueError, match=field_name):
            SessionWindow(**{field_name: time(6, 0)})

    @pytest.mark.parametrize(
        ("field_name", "bad_time"),
        [
            ("decision_time", time(20, 33)),
            ("closeout_time", time(4, 33)),
            ("friday_closeout_time", time(4, 33)),
        ],
    )
    def test_non_5min_grid_time_rejected(self, field_name: str, bad_time: time) -> None:
        """5 分足グリッド非整合の時刻は拒否（実手仕舞い時刻が設定値からずれるのを防ぐ）。"""
        with pytest.raises(ValueError, match=f"{field_name}.*5 分単位"):
            SessionWindow(**{field_name: bad_time})


class TestCloseoutSchedule:
    def test_mon_to_thu_close_next_day_midnight(self) -> None:
        wed = date(2026, 7, 1)
        expected = int(datetime(2026, 7, 2, 0, 0, tzinfo=_JST).timestamp() * 1_000)
        assert closeout_ms_for(wed, MORNING_SESSION) == expected

    def test_friday_closes_saturday_5am(self) -> None:
        fri = date(2026, 7, 3)
        expected = int(datetime(2026, 7, 4, 5, 0, tzinfo=_JST).timestamp() * 1_000)
        assert closeout_ms_for(fri, MORNING_SESSION) == expected

    def test_weekend_raises(self) -> None:
        with pytest.raises(ValueError, match="営業日ではありません"):
            closeout_ms_for(date(2026, 7, 4))  # 土曜

    def test_default_session_matches_no_arg(self) -> None:
        """session 省略は夜型（本番と同じ）。"""
        wed, fri = date(2026, 7, 1), date(2026, 7, 3)
        assert closeout_ms_for(wed, NIGHT_SESSION) == closeout_ms_for(wed)
        assert closeout_ms_for(fri, NIGHT_SESSION) == closeout_ms_for(fri)

    def test_night_session_closes_next_day_0430(self) -> None:
        wed = date(2026, 7, 1)
        expected = int(datetime(2026, 7, 2, 4, 30, tzinfo=_JST).timestamp() * 1_000)
        assert closeout_ms_for(wed, NIGHT_SESSION) == expected

    def test_night_session_friday_closes_saturday_0430(self) -> None:
        fri = date(2026, 7, 3)
        expected = int(datetime(2026, 7, 4, 4, 30, tzinfo=_JST).timestamp() * 1_000)
        assert closeout_ms_for(fri, NIGHT_SESSION) == expected

    def test_night_session_weekend_raises(self) -> None:
        with pytest.raises(ValueError, match="営業日ではありません"):
            closeout_ms_for(date(2026, 7, 5), NIGHT_SESSION)  # 日曜


class TestGoldenParityWithProduction:
    @pytest.mark.parametrize("price", [155.5005, 155.9999, 156.0, 154.8991])
    @pytest.mark.parametrize("side", ["LONG", "SHORT"])
    def test_round_tick_entry_matches_decision_batch(self, price: float, side: str) -> None:
        """本番 decision_batch._round_tick_entry と同値（バッチは import 禁止のため複製）。

        #238: 丸めは方向依存（LONG 切り下げ / SHORT 切り上げ）なので両方向で同値を確認する。
        """
        from backend.src.batch.decision_batch import _round_tick_entry

        assert round_tick_entry(price, side) == _round_tick_entry(price, side)  # type: ignore[arg-type]


class TestLoadSessionBars:
    def _candle(self, t_ms: int, price: float) -> dict[str, str]:
        return {
            "openTime": str(t_ms),
            "open": f"{price:.3f}",
            "high": f"{price + 0.05:.3f}",
            "low": f"{price - 0.05:.3f}",
            "close": f"{price:.3f}",
        }

    def test_session_window_and_join(self, tmp_path: Path) -> None:
        store = KlinesStore(cache_dir=tmp_path)
        times = [
            _jst_ms(_DAY, 7, 55),  # cutoff 前 → 除外
            _jst_ms(_DAY, 8, 0),
            _jst_ms(_DAY, 23, 55),
            _jst_ms(_DAY + timedelta(days=1), 0, 0),  # closeout 後 → 除外
        ]
        store.save("BID", "5min", _DAY.strftime("%Y%m%d"), [self._candle(t, 155.5) for t in times])
        # ASK は 08:00 と 23:55 のみ（07:55 / 翌 00:00 はどのみち窓外）
        ask_times = [_jst_ms(_DAY, 8, 0), _jst_ms(_DAY, 23, 55)]
        store.save(
            "ASK", "5min", _DAY.strftime("%Y%m%d"), [self._candle(t, 155.51) for t in ask_times]
        )

        bars = load_session_bars(store, _DAY, _CUTOFF_MS, closeout_ms_for(_DAY, MORNING_SESSION))
        assert bars is not None
        assert [b.open_time_ms for b in bars] == ask_times

    def test_unmatched_bid_ask_returns_none(self, tmp_path: Path) -> None:
        """セッション窓内の BID/ASK 時刻集合が不一致 → 当日全体を no_data に倒す。

        片側欠損バーを黙って除外すると、そのバーで起きた SL/TP を見逃すため。
        """
        store = KlinesStore(cache_dir=tmp_path)
        t0, t1 = _jst_ms(_DAY, 8, 0), _jst_ms(_DAY, 8, 5)
        store.save(
            "BID", "5min", _DAY.strftime("%Y%m%d"), [self._candle(t, 155.5) for t in (t0, t1)]
        )
        store.save("ASK", "5min", _DAY.strftime("%Y%m%d"), [self._candle(t0, 155.51)])
        assert load_session_bars(store, _DAY, _CUTOFF_MS, closeout_ms_for(_DAY)) is None

    def test_missing_cache_returns_none(self, tmp_path: Path) -> None:
        store = KlinesStore(cache_dir=tmp_path)
        store.save(
            "BID", "5min", _DAY.strftime("%Y%m%d"), [self._candle(_jst_ms(_DAY, 8, 0), 155.5)]
        )
        assert load_session_bars(store, _DAY, _CUTOFF_MS, closeout_ms_for(_DAY)) is None

    def test_night_window_fits_within_single_day_file(self, tmp_path: Path) -> None:
        """夜窓（20:30 → 翌 4:30）が当日 5 分足ファイル内で完結する（RD-1 受け入れ条件）。

        date=YYYYMMDD ファイルは JST 06:00 起点・翌 05:55 終端（backtest/data.py）のため、
        翌 4:25 バーまでが当日ファイルに含まれ、追加ファイルの読み込みは不要。
        """
        store = KlinesStore(cache_dir=tmp_path)
        next_day = _DAY + timedelta(days=1)
        times = [
            _jst_ms(_DAY, 20, 25),  # cutoff 前 → 除外
            _jst_ms(_DAY, 20, 30),
            _jst_ms(_DAY, 23, 55),
            _jst_ms(next_day, 0, 0),  # 日跨ぎ後も同一ファイル内
            _jst_ms(next_day, 4, 25),  # closeout 直前の最終バー
            _jst_ms(next_day, 4, 30),  # closeout 以降 → 除外
        ]
        candles = [self._candle(t, 155.5) for t in times]
        store.save("BID", "5min", _DAY.strftime("%Y%m%d"), candles)
        store.save("ASK", "5min", _DAY.strftime("%Y%m%d"), candles)

        cutoff = _jst_ms(_DAY, 20, 30)
        bars = load_session_bars(store, _DAY, cutoff, closeout_ms_for(_DAY, NIGHT_SESSION))
        assert bars is not None
        assert [b.open_time_ms for b in bars] == times[1:5]
        # 最終バーの終了時刻 = closeout 時刻（closeout 成行が最終バー close で約定できる）
        assert bars[-1].open_time_ms + 5 * 60 * 1_000 == closeout_ms_for(_DAY, NIGHT_SESSION)
