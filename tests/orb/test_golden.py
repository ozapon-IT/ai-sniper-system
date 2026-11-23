"""§8.1 ゴールデンテスト — Phase 0 オラクルとの全一致。

キャッシュ（git 管理外）が無い環境（CI 等）では skip。
ローカル実行手順: docs/runbook/orb_golden_test.md

検算日の CI 固定は ``test_check_day_fixture.py``（フィクスチャ同梱）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.src.orb.replay import compare_range_sources, replay_from_cache

# ゴールデン値（Phase0_結果_ドル円ロンドンORB.md §2・§6 / 設計書 §8.1）
GOLDEN = {
    "days": 714,
    "fills": 626,
    "sl_pct": 47.3,
    "eod_r_mean": 1.395,
    "spread_med": 0.2,
    "risk_pips_med": 26.1,
    "ev_gross": 0.2624,
    "ev_net": 0.2537,
    "ambiguous_both": 1,
    "sd": 2.039,
    "se": 0.081,
    "t": 3.11,
}

CHECK_DAY = "2023-11-08"
CHECK_DAY_EXPECTED = {
    "direction": 1,
    "outcome": "eod",
    "range_high": 150.725,
    "range_low": 150.596,
    "buy_stop": 150.735,
    "sell_stop": 150.586,
    "entry": 150.735,
    "sl": 150.586,
    "risk_pips": 14.9,
    "exit": 150.851,
    "r": 0.7785,
    "r_net": 0.765,
}


def _default_cache_roots() -> list[Path]:
    """ORB_CACHE_ROOT → リポジトリ内 cache の順。マシン固有パスは埋めない。"""
    roots: list[Path] = []
    env = os.environ.get("ORB_CACHE_ROOT")
    if env:
        roots.append(Path(env))
    here = Path(__file__).resolve().parents[2]
    roots.append(here / "backtest" / "cache" / "USD_JPY")
    return roots


def _resolve_cache_root() -> Path | None:
    for root in _default_cache_roots():
        if (
            (root / "BID" / "15min").is_dir()
            and (root / "BID" / "1min").is_dir()
            and any((root / "BID" / "15min").glob("*.json"))
            and any((root / "BID" / "1min").glob("*.json"))
        ):
            return root
    return None


CACHE_ROOT = _resolve_cache_root()
_requires_cache = pytest.mark.skipif(
    CACHE_ROOT is None,
    reason="USD_JPY klines cache not found (see docs/runbook/orb_golden_test.md)",
)


@pytest.fixture(scope="module")
def replay():
    if CACHE_ROOT is None:
        pytest.skip("USD_JPY klines cache not found (see docs/runbook/orb_golden_test.md)")
    return replay_from_cache(CACHE_ROOT)


@_requires_cache
class TestGoldenFullReplay:
    def test_window_days_and_fills(self, replay) -> None:
        s = replay.summary
        assert s.days == GOLDEN["days"]
        assert s.fills == GOLDEN["fills"]

    def test_funnel_ambiguous(self, replay) -> None:
        assert replay.funnel.ambiguous_both == GOLDEN["ambiguous_both"]

    def test_sl_and_eod_stats(self, replay) -> None:
        s = replay.summary
        assert s.sl_pct == GOLDEN["sl_pct"]
        assert s.eod_r_mean == GOLDEN["eod_r_mean"]

    def test_spread_and_risk_medians(self, replay) -> None:
        s = replay.summary
        assert s.spread_med == GOLDEN["spread_med"]
        assert s.risk_pips_med == GOLDEN["risk_pips_med"]

    def test_ev_and_t(self, replay) -> None:
        s = replay.summary
        assert s.ev_gross == GOLDEN["ev_gross"]
        assert s.ev_net == GOLDEN["ev_net"]
        assert s.sd == GOLDEN["sd"]
        assert s.se == GOLDEN["se"]
        assert s.t == GOLDEN["t"]


@_requires_cache
class TestProductionPathReplay:
    """本番 session-open 経路（1分足レンジ + 15分フォールバック）もゲート数値を再現すること。"""

    def test_1m_range_path_matches_oracle_path(self, replay) -> None:
        assert CACHE_ROOT is not None
        prod = replay_from_cache(CACHE_ROOT, use_1m_range=True)
        assert prod.summary == replay.summary
        assert prod.funnel == replay.funnel
        assert [(t.day, t.direction, t.outcome, t.r, t.r_net) for t in prod.trades] == [
            (t.day, t.direction, t.outcome, t.r, t.r_net) for t in replay.trades
        ]


@_requires_cache
class TestGoldenCheckDay:
    def test_2023_11_08_long_eod(self, replay) -> None:
        trade = next((t for t in replay.trades if t.day == CHECK_DAY), None)
        assert trade is not None, f"{CHECK_DAY} trade missing"
        exp = CHECK_DAY_EXPECTED
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
        assert trade.r_net is not None
        assert round(trade.r_net, 3) == exp["r_net"]


@_requires_cache
class TestRangeEquivalence:
    """設計書 §6.2: 1分足60本 == 15分足4本 を全窓日で検証。

    両方算出可能な日は H/L 完全一致が必須。
    1分足欠損で 15分のみ取れる日は設計どおりフォールバック対象（吸収せず記録）。
    """

    def test_when_both_available_hl_identical(self) -> None:
        assert CACHE_ROOT is not None
        mismatches = compare_range_sources(CACHE_ROOT)
        hl_diff = [m for m in mismatches if m["from_15m"] is not None and m["from_1m"] is not None]
        assert hl_diff == [], (
            f"両方算出可能なのに H/L 不一致 {len(hl_diff)} 件 "
            f"(先頭: {hl_diff[:3]!r})。オラクル(15分足)が正。"
            "設計書 §6.2 の改訂が必要 — 実装で吸収しない。"
        )

    def test_1m_coverage_gaps_are_documented(self) -> None:
        """1分足欠損日は実装で埋めず、既知ギャップとして明示する。"""
        assert CACHE_ROOT is not None
        mismatches = compare_range_sources(CACHE_ROOT)
        only_15m = [
            m["day"] for m in mismatches if m["from_15m"] is not None and m["from_1m"] is None
        ]
        only_1m = [
            m["day"] for m in mismatches if m["from_15m"] is None and m["from_1m"] is not None
        ]
        # 既知: 2025-06-20 の 1min キャッシュが London 21:xx の 59 本のみ（ORB 窓ゼロ）
        assert only_15m == ["2025-06-20"], (
            f"予期しない 1分足欠損: {only_15m!r}（設計書 §6.2.1 に追記が必要）"
        )
        assert only_1m == [], f"15分欠損だが1分あり: {only_1m!r}"


class TestNoForbiddenImports:
    """発注系 POST / LLM / two-key への参照ゼロ（設計書 §3.2）。"""

    def test_orb_package_has_no_forbidden_refs(self) -> None:
        orb_root = Path(__file__).resolve().parents[2] / "backend" / "src" / "orb"
        forbidden = (
            "ifoOrder",
            "ifdOrder",
            "closeOrder",
            "cancelOrders",
            "ALLOW_LIVE_ORDERS",
            "TRADING_PHASE",
            "gmo_client",
            "anthropic",
            "trade_decision",
            "private_post",
            "PRIVATE_",
        )
        hits: list[str] = []
        for path in orb_root.rglob("*.py"):
            text = path.read_text()
            for token in forbidden:
                if token in text:
                    hits.append(f"{path.name}:{token}")
        assert hits == [], f"forbidden refs in orb/: {hits}"
