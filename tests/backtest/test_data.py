"""backtest/data.py（BT-1）のテスト。

取得・保存・先読みの実体は fxlab に移し、そのテストも fx-backtest-lab の
tests/test_store.py・tests/test_data.py に移した。ここでは backtest 側が使う
キャッシュのレイアウト（context.py が読む形）だけを確認する。
"""

from __future__ import annotations

from pathlib import Path

import fxlab.store

from backtest.data import SYMBOL_DEFAULT, KlinesStore, prefetch_range


def test_reexports_fxlab_store() -> None:
    assert KlinesStore is fxlab.store.KlinesStore
    assert prefetch_range is fxlab.store.prefetch_range


def test_cache_layout(tmp_path: Path) -> None:
    store = KlinesStore(cache_dir=tmp_path, symbol=SYMBOL_DEFAULT)
    store.save("BID", "5min", "20260102", [])
    assert (tmp_path / "USD_JPY" / "BID" / "5min" / "20260102.json").exists()
    assert store.load("BID", "5min", "20260102") == []
    assert store.load("ASK", "5min", "20260102") is None
