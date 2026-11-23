"""休場日カレンダー管理（§3.5）。

判断バッチ起動時に is_market_holiday(date) を呼び、True の場合は早期 return する。
US 単独祝日（薄商い）は is_us_thin_market(date) で判定し、LLM へ注意文として渡す。
JSON は config/market_holidays.json で管理し、年次更新はそちらのみ変更する。
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

_HOLIDAYS_JSON = Path(__file__).parent / "market_holidays.json"


def _load_holiday_sets(path: Path = _HOLIDAYS_JSON) -> tuple[frozenset[date], frozenset[date]]:
    """market_holidays.json を読み込み (full_close, us_thin) の frozenset タプルを返す。"""
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    full_close = frozenset(date.fromisoformat(e["date"]) for e in data.get("full_close", []))
    us_thin = frozenset(date.fromisoformat(e["date"]) for e in data.get("us_thin", []))
    return full_close, us_thin


# モジュールロード時に一度だけ読み込む（Lambda コールドスタート最適化）
_FULL_CLOSE_HOLIDAYS, _US_THIN_HOLIDAYS = _load_holiday_sets()


def is_market_holiday(target_date: date) -> bool:
    """指定日が FX 実質休場日（元日・クリスマス・Good Friday 等）かどうかを返す（§3.5）。

    True の場合は判断バッチを早期 return する。
    US 単独祝日（薄商い）は含まない（is_us_thin_market を参照）。

    Args:
        target_date: チェック対象日（JST 基準の date）。

    Returns:
        FX 実質閉場日なら True、それ以外なら False。
    """
    result = target_date in _FULL_CLOSE_HOLIDAYS
    if result:
        logger.info(
            "market_holiday_detected",
            extra={"date": target_date.isoformat(), "skipped": "market_holiday"},
        )
    return result


def is_us_thin_market(target_date: date) -> bool:
    """指定日が US 単独祝日による薄商い日かどうかを返す（§3.5）。

    FX(USD/JPY) は開いているが NY 勢が少なく流動性が低い日。
    早期 return はせず、LLM へ注意フラグを渡すために使用する。

    Args:
        target_date: チェック対象日（JST 基準の date）。

    Returns:
        US 薄商い日なら True、通常なら False。
    """
    result = target_date in _US_THIN_HOLIDAYS
    if result:
        logger.info(
            "us_thin_market_detected",
            extra={"date": target_date.isoformat(), "us_thin": True},
        )
    return result
