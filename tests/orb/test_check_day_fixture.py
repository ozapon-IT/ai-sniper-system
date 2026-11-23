"""検算日 2023-11-08 の CI 固定テスト（キャッシュ不要）。

全 714 日リプレイは git 管理外キャッシュ依存のため CI では skip される。
本テストは当日バーの最小フィクスチャで evaluate_day の合格数値を恒久固定する。
公開版のフィクスチャは、シード固定のランダムウォークで作った**合成データ**（実際の相場ではない）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backend.src.orb.constants import BAR_MINUTES_15, LON
from backend.src.orb.evaluate import evaluate_day
from backend.src.orb.models import Bar1m, Bar15
from backend.src.orb.range import range_from_1m, range_from_15m

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "check_day_2023_11_08.json"
DAY = "2023-11-08"
SPREAD_MED = 0.2  # 想定スプレッド（pips）

EXPECTED = {
    "direction": 1,
    "outcome": "eod",
    "range_high": 145.031,
    "range_low": 144.879,
    "buy_stop": 145.041,
    "sell_stop": 144.869,
    "entry": 145.041,
    "sl": 144.869,
    "risk_pips": 17.2,
    "exit": 145.295,
    "r": 1.4767,
    "r_net": 1.465,
}


def _load_bars() -> tuple[list[Bar15], list[Bar1m]]:
    payload = json.loads(FIXTURE.read_text())
    bars15: list[Bar15] = []
    for cd in payload["bid_15min"]:
        open_time = datetime.fromtimestamp(int(cd["openTime"]) / 1000, tz=UTC)
        bars15.append(
            Bar15(
                open_time=open_time,
                open=float(cd["open"]),
                high=float(cd["high"]),
                low=float(cd["low"]),
                close=float(cd["close"]),
                local_close=(open_time + timedelta(minutes=BAR_MINUTES_15)).astimezone(LON),
            )
        )
    mins: list[Bar1m] = []
    for cd in payload["bid_1min"]:
        loc = datetime.fromtimestamp(int(cd["openTime"]) / 1000, tz=UTC).astimezone(LON)
        mins.append(
            Bar1m(
                local_open=loc,
                open=float(cd["open"]),
                high=float(cd["high"]),
                low=float(cd["low"]),
                close=float(cd["close"]),
            )
        )
    return bars15, mins


class TestCheckDayFixture:
    def test_fixture_file_exists(self) -> None:
        assert FIXTURE.is_file()

    def test_15m_and_1m_ranges_match(self) -> None:
        bars15, mins = _load_bars()
        r15 = range_from_15m(bars15)
        r1 = range_from_1m(mins)
        assert r15 is not None and r1 is not None
        assert r15.high == r1.high
        assert r15.low == r1.low

    def test_evaluate_day_matches_golden(self) -> None:
        bars15, mins = _load_bars()
        result = evaluate_day(DAY, bars15, mins)
        assert result is not None
        assert result.skip_reason is None
        trade = result.trade
        assert trade is not None
        exp = EXPECTED
        assert trade.direction == exp["direction"]
        assert trade.outcome == exp["outcome"]
        assert trade.range_high == pytest.approx(exp["range_high"])
        assert trade.range_low == pytest.approx(exp["range_low"])
        assert trade.buy_stop == pytest.approx(exp["buy_stop"])
        assert trade.sell_stop == pytest.approx(exp["sell_stop"])
        assert trade.entry == pytest.approx(exp["entry"])
        assert trade.sl == pytest.approx(exp["sl"])
        assert trade.risk_pips == pytest.approx(exp["risk_pips"])
        assert trade.exit == pytest.approx(exp["exit"])
        assert trade.r == pytest.approx(exp["r"], abs=5e-5)
        r_net = trade.r - SPREAD_MED / trade.risk_pips
        assert round(r_net, 3) == exp["r_net"]

    def test_1m_range_path_same_as_15m(self) -> None:
        """本番経路（1分足レンジ）でも検算日の数値が一致する。"""
        bars15, mins = _load_bars()
        levels = range_from_1m(mins)
        assert levels is not None
        result = evaluate_day(DAY, bars15, mins, range_levels=levels)
        assert result is not None and result.trade is not None
        assert result.trade.entry == pytest.approx(EXPECTED["entry"])
        assert result.trade.exit == pytest.approx(EXPECTED["exit"])
        assert result.trade.r == pytest.approx(EXPECTED["r"], abs=5e-5)
