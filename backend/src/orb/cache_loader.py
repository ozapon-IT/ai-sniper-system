"""ローカル klines キャッシュの読み込み（API 非依存）。

キャッシュは git 管理外のため、呼び出し側が ``cache_root`` を渡す。
既定想定: ``<repo>/backtest/cache/USD_JPY``（メインチェックアウト側に存在）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from backend.src.orb.constants import (
    BAR_MINUTES_15,
    LON,
    MINUTE_LOAD_HI,
    MINUTE_LOAD_LO,
)
from backend.src.orb.models import Bar1m, Bar15


def load_bars_15m(cache_root: Path, price_type: str = "BID") -> list[Bar15]:
    """``{cache_root}/{price_type}/15min/*.json`` を時系列昇順で読む。"""
    bars: list[Bar15] = []
    root = cache_root / price_type / "15min"
    for path in sorted(root.glob("*.json")):
        payload = json.loads(path.read_text())
        for cd in payload["candles"]:
            open_time = datetime.fromtimestamp(int(cd["openTime"]) / 1000, tz=UTC)
            local_close = (open_time + timedelta(minutes=BAR_MINUTES_15)).astimezone(LON)
            bars.append(
                Bar15(
                    open_time=open_time,
                    open=float(cd["open"]),
                    high=float(cd["high"]),
                    low=float(cd["low"]),
                    close=float(cd["close"]),
                    local_close=local_close,
                )
            )
    bars.sort(key=lambda b: b.open_time)
    return bars


def load_minutes_by_day(cache_root: Path) -> dict[str, list[Bar1m]]:
    """``{cache_root}/BID/1min/*.json`` を London 日付でグループ化。

    openTime が ``7:45〜16:30``（London）のバーのみ（オラクルと同一）。
    """
    out: dict[str, list[Bar1m]] = {}
    root = cache_root / "BID" / "1min"
    for path in sorted(root.glob("*.json")):
        payload = json.loads(path.read_text())
        for cd in payload["candles"]:
            loc = datetime.fromtimestamp(int(cd["openTime"]) / 1000, tz=UTC).astimezone(LON)
            if not (MINUTE_LOAD_LO <= loc.time() <= MINUTE_LOAD_HI):
                continue
            day = loc.date().isoformat()
            out.setdefault(day, []).append(
                Bar1m(
                    local_open=loc,
                    open=float(cd["open"]),
                    high=float(cd["high"]),
                    low=float(cd["low"]),
                    close=float(cd["close"]),
                )
            )
    for day in out:
        out[day].sort(key=lambda x: x.local_open)
    return out
