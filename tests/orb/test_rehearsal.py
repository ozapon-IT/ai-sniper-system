"""ORB-8 通しリハーサルのテスト（#278）。

harness 自体の性質を固定する。実データ 5 営業日の通しは
`docs/runbook/orb_rehearsal.md` の手順（キャッシュ依存・CI 外）。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from backend.src.orb.constants import LON
from backend.src.orb.models import Bar1m
from scripts.orb_rehearsal import CacheClient, synthesize_observations


def _bar(minute: int, o: float, h: float, low: float, c: float) -> Bar1m:
    return Bar1m(
        local_open=datetime(2026, 8, 10, 9, minute, tzinfo=LON),
        open=o,
        high=h,
        low=low,
        close=c,
    )


class TestSynthesis:
    def test_cadence_matches_production_poller(self) -> None:
        """刻みは 5 秒。粗いと 11:00 跨ぎが欠測扱いになり本番では出ない defer が出る。"""
        obs = synthesize_observations([_bar(0, 150.0, 150.1, 149.9, 150.05)])
        assert len(obs) == 12  # 60s / 5s
        gaps = {
            int((b.ts - a.ts).total_seconds()) for a, b in zip(obs, obs[1:], strict=False)
        }
        assert gaps == {5}

    def test_gap_below_boundary_threshold(self) -> None:
        """連続バー間の間隔も `_BOUNDARY_GAP_MS`（10 秒）未満であること。"""
        from backend.src.orb.broker import _BOUNDARY_GAP_MS

        obs = synthesize_observations([_bar(0, 150.0, 150.1, 149.9, 150.05), _bar(1, 150.05, 150.2, 150.0, 150.1)])
        gaps_ms = [
            int((b.ts - a.ts).total_seconds() * 1000)
            for a, b in zip(obs, obs[1:], strict=False)
        ]
        assert max(gaps_ms) < _BOUNDARY_GAP_MS

    def test_extremes_are_preserved(self) -> None:
        """高安が観測列に現れる（トリガー判定に必要）。"""
        bar = _bar(0, 150.0, 150.30, 149.70, 150.05)
        bids = [o.bid for o in synthesize_observations([bar])]
        assert max(bids) == pytest.approx(150.30)
        assert min(bids) == pytest.approx(149.70)

    def test_up_bar_visits_low_before_high(self) -> None:
        """陽線は安値が先という heuristic（実際の順序は不明・runbook に明記）。"""
        bids = [o.bid for o in synthesize_observations([_bar(0, 150.0, 150.3, 149.7, 150.2)])]
        assert bids.index(149.70) < bids.index(150.30)

    def test_down_bar_visits_high_before_low(self) -> None:
        bids = [o.bid for o in synthesize_observations([_bar(0, 150.2, 150.3, 149.7, 150.0)])]
        assert bids.index(150.30) < bids.index(149.70)

    def test_spread_is_applied(self) -> None:
        obs = synthesize_observations([_bar(0, 150.0, 150.1, 149.9, 150.05)], spread_pips=0.5)
        assert all(round((o.ask - o.bid) / 0.01, 2) == 0.5 for o in obs)

    def test_all_observations_are_open(self) -> None:
        """開場ゲートで落ちないよう status=OPEN（合成なので閉場は模擬しない）。"""
        obs = synthesize_observations([_bar(0, 150.0, 150.1, 149.9, 150.05)])
        assert {o.status for o in obs} == {"OPEN"}


class TestCacheClient:
    def test_serves_klines_from_cache(self, tmp_path) -> None:
        root = tmp_path / "USD_JPY"
        d = root / "BID" / "1min"
        d.mkdir(parents=True)
        (d / "20260810.json").write_text(
            '{"candles": [{"openTime": "1", "open": "1", "high": "1", "low": "1", "close": "1"}]}',
            encoding="utf-8",
        )
        client = CacheClient(root)
        body = client.public_get("/public/v1/klines", {"interval": "1min", "date": "20260810"}).json()
        assert len(body["data"]) == 1

    def test_missing_file_is_empty_not_error(self, tmp_path) -> None:
        client = CacheClient(tmp_path)
        body = client.public_get("/public/v1/klines", {"interval": "1min", "date": "19990101"}).json()
        assert body["data"] == []

    def test_status_is_open(self, tmp_path) -> None:
        assert CacheClient(tmp_path).public_get("/public/v1/status").json()["data"]["status"] == "OPEN"


class TestContainment:
    def test_no_forbidden_refs(self) -> None:
        from pathlib import Path

        path = Path(__file__).resolve().parents[2] / "scripts" / "orb_rehearsal.py"
        forbidden = (
            "ifoOrder", "ifdOrder", "closeOrder", "cancelOrders",
            "ALLOW_LIVE_ORDERS", "TRADING_PHASE", "MVP_MODE",
            "anthropic", "private_get", "private_post", "PRIVATE_",
        )
        text = path.read_text()
        assert [t for t in forbidden if t in text] == []
