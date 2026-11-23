"""klines キャッシュ（BT-1, 設計書 §5）。実体は fxlab（fx-backtest-lab）。

取得・保存・先読みは fxlab.store / fxlab.gmo に移した。fxlab の取得クライアントは
Public klines の GET 専用（6 回/秒・HTTP 200 の業務エラーも検査）で、発注経路を持たない。

キャッシュの場所は既定で ``backtest/cache``（``~/.cache/fxlab`` へのリンクでよい）。
レイアウトは ``<cache>/<symbol>/<priceType>/<interval>/<date>.json``。5 分足の
YYYYMMDD ファイルは JST 06:00 起点の 1 日分（公式 https://api.coin.z.com/fxdocs/#kline
2026-07-02 参照）。

    python -m backtest.data --from 2024-01-01 --to 2024-03-31
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from fxlab.gmo import INTRADAY_MIN_DATE, PRICE_TYPES, GmoPublicClient, PriceType, validate_date_str
from fxlab.store import KlinesStore, ensure_cached, prefetch_range

SYMBOL_DEFAULT = "USD_JPY"

__all__ = [
    "INTRADAY_MIN_DATE",
    "PRICE_TYPES",
    "SYMBOL_DEFAULT",
    "KlinesStore",
    "PriceType",
    "ensure_cached",
    "prefetch_range",
    "validate_date_str",
]


def _main() -> None:
    parser = argparse.ArgumentParser(description="GMO klines をローカルキャッシュへ先読みする")
    parser.add_argument("--from", dest="start", required=True, help="開始日 YYYY-MM-DD")
    parser.add_argument("--to", dest="end", required=True, help="終了日 YYYY-MM-DD")
    parser.add_argument("--cache-dir", default="backtest/cache", help="キャッシュディレクトリ")
    parser.add_argument("--intraday-interval", default="5min", choices=["1min", "5min", "15min"])
    parser.add_argument(
        "--refresh-current", action="store_true", help="当年・当日ファイルを再取得する"
    )
    args = parser.parse_args()

    store = KlinesStore(cache_dir=Path(args.cache_dir), symbol=SYMBOL_DEFAULT)
    with GmoPublicClient() as client:
        stats = prefetch_range(
            store,
            client,
            start=date.fromisoformat(args.start),
            end=date.fromisoformat(args.end),
            intraday_interval=args.intraday_interval,
            refresh_current=args.refresh_current,
        )
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    _main()
