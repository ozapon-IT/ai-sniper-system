"""JST タイムゾーン定数（§6 AGENTS.md #6）。

直近確定取引日ロジック（§3.1）は `adapters/klines.py` に一本化されている
（`get_confirmed_daily_candles` / `get_latest_confirmed_daily`）。
E05 スパイク（docs/spikes/E05_klines_appendix_b.md）で klines の `date` パラメータは
**YYYY（年）形式のみ有効・YYYYMMDD は 404**、かつ「1本取得 API は存在しない」と確定した。
このため日単位フェッチ（YYYYMMDD）を前提にしていた旧 `get_latest_confirmed_candle` は
本番から呼べない・API 契約に反する死んだコードとなり削除した。実パスは klines 側で検証する
（tests/adapters/test_klines.py::test_monday_returns_friday_candle 等）。

本モジュールは複数バッチが共有する JST 定数と、描画時の単位明示ヘルパーを提供する。
"""

from __future__ import annotations

import datetime

# 日本標準時（UTC+9）。バッチ全体で時刻を JST に固定する（AGENTS.md #6, §6）。
JST = datetime.timezone(datetime.timedelta(hours=9))


def jst_time_label(time_jst: str) -> str:
    """素の ``HH:MM`` に ``JST`` を添える（#343 / #345）。

    値の加工ではなく単位の明示。空なら空文字（無い情報を作らない）。
    ``time_jst`` は判断バッチが ``strftime("%H:%M")`` で書く素の ``HH:MM`` なので
    二重表記は起きない。判断プロンプトとノート描画で同じヘルパーを使い、
    素の ``HH:MM`` を出す経路を増やさない。
    """
    return f"{time_jst} JST" if time_jst else ""


def jst_month_day(date_jst: str) -> str:
    """``YYYY-MM-DD`` をゼロ埋めなしの ``M/D`` にする。空・読めないときは空。

    現在時刻行と :func:`jst_dated_time_label` が同じ整形を使う（#376）。
    前後の空白は無視する。日付を作れないときは空文字（無い情報を作らない）。
    """
    text = date_jst.strip()
    if not text:
        return ""
    try:
        parsed = datetime.date.fromisoformat(text)
    except ValueError:
        return ""
    return f"{parsed.month}/{parsed.day}"


def jst_dated_time_label(date_jst: str, time_jst: str) -> str:
    """``YYYY-MM-DD`` があるとき ``M/D HH:MM JST``。無ければ :func:`jst_time_label`。

    ノートの日付付き描画用（#374）。``time_jst`` 自体には日付を
    埋め込まない（ノートの ``HH:MM`` 連結を壊さない）。月日は :func:`jst_month_day`。
    月日が空、または時刻が空のときは日付を足さない。
    """
    time_label = jst_time_label(time_jst)
    month_day = jst_month_day(date_jst)
    if not time_label or not month_day:
        return time_label
    return f"{month_day} {time_label}"
