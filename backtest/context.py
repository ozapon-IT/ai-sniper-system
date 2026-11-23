"""営業日ごとの PromptContext 構築（BT-1, 設計書 §3 / §6.1）。

**カットオフの唯一実装点**。判断時刻 t（`SessionWindow.decision_time`・バックテスト既定 20:30 JST）に対し、
``openTime_ms + interval_ms <= t_ms`` を満たす確定足のみを使う。
日足・4時間足・5分足すべての確定判定は本番 `get_confirmed_candles` をそのまま再利用し、
本番朝バッチと同一の条件でルックアヘッドを構造的に排除する。

完全オフライン: このモジュールは `KlinesStore`（ローカルキャッシュ）のみを読み、
ネットワークアクセスを一切行わない。キャッシュ欠損時は None（当日スキップ）。

本番との差分（BT-1 時点の制限。レポートに明記すること）:
  - current_price_mid: ticker の代わりに判断時刻直前の確定 5 分足 BID/ASK close の mid。
  - equity_jpy: 口座履歴は存在しないため固定値（既定 500,000。サイジングは §4 で別途）。
  - economic_events: 過去の指標カレンダー復元は未実装 → 空リスト。
  - macro_context: 同上 → None（本番の取得失敗時 degrade と同じ扱い）。
  - decision_date_jst: 判断バッチは現在時刻行に判断日の月日を出す（#376・例 ``6/29 20:30 (月)``）。
    バックテストは空のまま渡す。現在時刻行は時刻と曜日のみで、判断日の月日は出ない。
  - 休場日カレンダー（config/market_holidays.json）は 2026 年以降のみ収録。
    2023〜2025 年の休場日は kline 欠損（キャッシュにデータなし）として自然にスキップされる。
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import structlog

from backend.src.adapters.klines import (
    SessionSplitOhlc,
    get_confirmed_candles,
    split_session_ohlc_from_confirmed,
    tokyo_europe_boundary_ms,
)
from backend.src.engine.atr import ATR_PERIOD, calculate_atr
from backend.src.engine.pivot import calculate_pivot
from backend.src.llm.prompt import PromptContext
from backend.src.validation.day_skip_guards import USD_JPY_PIP
from backtest.data import KlinesStore
from config.market_calendar import is_market_holiday, is_us_thin_market

_log = structlog.get_logger(__name__)

_JST = ZoneInfo("Asia/Tokyo")

SYMBOL = "USD_JPY"

# 本番判断バッチの判断時刻（夜型 PM 8:30・engine.NIGHT_SESSION と同じ）
DECISION_TIME_JST = time(20, 30)

# バックテスト用の固定有効純資産（プロンプトの参考値。サイジング検証は BT-2 以降）
DEFAULT_EQUITY_JPY = 500_000.0

_RECENT_4H_COUNT = 6  # 本番 get_recent_4h_context と同じ直近 6 本
_MS_5MIN = 5 * 60 * 1_000
_MS_BY_INTERVAL = {"1day": 24 * 3_600 * 1_000, "4hour": 4 * 3_600 * 1_000, "5min": _MS_5MIN}

_WEEKDAY_NAMES = ["月", "火", "水", "木", "金", "土", "日"]


@dataclass(frozen=True)
class DayContext:
    """1 営業日分の LLM 入力と監査メタ。

    used_candles_ms は文脈に入った全ローソク足の (openTime_ms, interval_ms)。
    「openTime + interval <= cutoff」の事後検証（テスト・監査）に使う。
    spread_pips は判断時刻直前の確定 5 分足 BID/ASK から算出（本番 ticker 相当）。
    """

    day: date
    cutoff_ms: int
    prompt_context: PromptContext
    spread_pips: float
    used_candles_ms: tuple[tuple[int, int], ...]


def _skip(day: date, reason: str) -> None:
    _log.info(
        "backtest_context_skip",
        day=day.isoformat(),
        reason=reason,
        timestamp_ms=int(_time.time() * 1000),
        llm_tokens=None,
    )


def _load_confirmed_yearly(
    store: KlinesStore,
    interval: str,
    year: int,
    cutoff_ms: int,
    min_needed: int,
) -> list[dict[str, str]] | None:
    """当年キャッシュの確定足を返し、不足時のみ前年キャッシュを前置して連結する。

    本番 get_confirmed_daily_candles / get_recent_4h_context の年跨ぎ連結と同じ方針。
    当年キャッシュ欠損は None（fail-closed: 不完全データで文脈を作らない）。
    前年キャッシュ欠損は「不足のまま」返し、本数チェックは呼び出し側で行う。
    """
    candles = store.load("BID", interval, str(year))
    if candles is None:
        return None
    confirmed = get_confirmed_candles(candles, cutoff_ms, interval)
    if len(confirmed) < min_needed:
        prev = store.load("BID", interval, str(year - 1))
        if prev is not None:
            confirmed = get_confirmed_candles(prev, cutoff_ms, interval) + confirmed
    return confirmed


def _mid_price_at_cutoff(
    store: KlinesStore,
    day: date,
    cutoff_ms: int,
) -> tuple[float, int, float] | None:
    """判断時刻直前の確定 5 分足から BID/ASK mid と spread を返す。

    戻り値: (mid, openTime_ms, spread_pips)。5 分足の date=YYYYMMDD ファイルは
    JST 06:00 起点（data.py 参照）のため、朝 08:00 判断なら当日ファイル内に
    必ず直前足が含まれる。
    """
    bid = store.load("BID", "5min", day.strftime("%Y%m%d"))
    ask = store.load("ASK", "5min", day.strftime("%Y%m%d"))
    if bid is None or ask is None:
        return None
    # 確定条件は本番 get_confirmed_candles（openTime + interval <= cutoff）を全 interval で共有
    confirmed_bid = get_confirmed_candles(bid, cutoff_ms, "5min")
    if not confirmed_bid:
        return None
    last_bid = confirmed_bid[-1]
    open_time = int(last_bid["openTime"])
    ask_by_time = {int(c["openTime"]): c for c in ask}
    last_ask = ask_by_time.get(open_time)
    if last_ask is None:
        return None
    bid_close = float(last_bid["close"])
    ask_close = float(last_ask["close"])
    mid = (bid_close + ask_close) / 2
    raw_spread_pips = (ask_close - bid_close) / USD_JPY_PIP
    if raw_spread_pips < 0:
        _log.warning(
            "negative_spread_normalized",
            bid_close=bid_close,
            ask_close=ask_close,
            raw_spread_pips=raw_spread_pips,
            open_time=open_time,
            timestamp_ms=int(_time.time() * 1000),
            llm_tokens=None,
        )
    # 本番 _fetch_ticker と同じく float ノイズを 0.01 pip 精度に丸める（RD-17 #219）
    spread_pips = round(abs(raw_spread_pips), 2)
    return mid, open_time, spread_pips


def _session_stats_at(
    store: KlinesStore, day: date, cutoff_ms: int
) -> tuple[float, float, float] | None:
    """当日セッションの (始値, 高値, 安値)。夜型の当日モメンタム判断用（RD-2 #167 / RD-5 #170）。

    5 分足ファイルは JST 06:00 起点（data.py）。cutoff 以前の確定足のみから算出する
    （最古 open・確定足の high 最大・low 最小）。本番 get_session_ohlc と同一のカットオフ条件
    （openTime + 5min <= cutoff）で、判断時刻以降の値動きを構造的に含まない（ルックアヘッド排除）。
    当日足がない・確定足が無ければ None（判断ソース側で degrade）。
    """
    split = _split_session_stats_at(store, day, cutoff_ms)
    if split is None:
        return None
    return split.session_open, split.session_high, split.session_low


def _split_session_stats_at(
    store: KlinesStore, day: date, cutoff_ms: int
) -> SessionSplitOhlc | None:
    """当日セッションの単一ブロック + 東京/欧州分割（RD-9 #195）。本番 get_split_session_ohlc と同一ロジック。"""
    bid = store.load("BID", "5min", day.strftime("%Y%m%d"))
    if bid is None:
        return None
    confirmed = get_confirmed_candles(bid, cutoff_ms, "5min")
    if not confirmed:
        return None
    return split_session_ohlc_from_confirmed(confirmed, tokyo_europe_boundary_ms(day))


def build_day_context(
    day: date,
    store: KlinesStore,
    *,
    equity_jpy: float = DEFAULT_EQUITY_JPY,
    decision_time: time = DECISION_TIME_JST,
) -> DayContext | None:
    """指定営業日の判断時刻時点における PromptContext を構築する。

    構築不能（休場日・キャッシュ欠損・確定足不足）なら None を返し理由をログに残す。
    本番朝バッチの skip（no_confirmed_candle 等）と同じ「見送り」セマンティクス。
    """
    if is_market_holiday(day):
        _skip(day, "market_holiday")
        return None

    cutoff_dt = datetime.combine(day, decision_time, tzinfo=_JST)
    cutoff_ms = int(cutoff_dt.timestamp() * 1_000)

    daily = _load_confirmed_yearly(store, "1day", day.year, cutoff_ms, min_needed=ATR_PERIOD + 1)
    if daily is None:
        _skip(day, "daily_cache_missing")
        return None
    if len(daily) < ATR_PERIOD + 1:
        _skip(day, "insufficient_daily_candles")
        return None

    candles_4h = _load_confirmed_yearly(
        store, "4hour", day.year, cutoff_ms, min_needed=_RECENT_4H_COUNT
    )
    if candles_4h is None:
        _skip(day, "4h_cache_missing")
        return None
    recent_4h = candles_4h[-_RECENT_4H_COUNT:]

    mid = _mid_price_at_cutoff(store, day, cutoff_ms)
    if mid is None:
        _skip(day, "no_5min_price")
        return None
    current_price_mid, mid_open_time, spread_pips = mid

    # 本番 morning_batch と同一の導出（直近確定日足 → Pivot / ATR / 前日騰落）
    latest = daily[-1]
    atr = calculate_atr(daily)
    pivot = calculate_pivot(
        high=float(latest["high"]),
        low=float(latest["low"]),
        close=float(latest["close"]),
    )
    prev_close = float(latest["close"])
    prev_open = float(latest["open"])
    prev_day_change = (prev_close - prev_open) / prev_open if prev_open != 0 else 0.0

    session_split = _split_session_stats_at(
        store, day, cutoff_ms
    )  # 当日モメンタム（RD-2 #167 / RD-5 #170 / RD-9 #195）

    ctx = PromptContext(
        current_time_jst=cutoff_dt.strftime("%H:%M"),
        day_of_week=_WEEKDAY_NAMES[day.weekday()],
        symbol=SYMBOL,
        current_price_mid=current_price_mid,
        pivot_p=pivot.p,
        pivot_r1=pivot.r1,
        pivot_r2=pivot.r2,
        pivot_s1=pivot.s1,
        pivot_s2=pivot.s2,
        atr_14d=atr,
        prev_day_change=prev_day_change,
        equity_jpy=equity_jpy,
        economic_events=[],  # BT-1 制限: 過去カレンダー未実装（モジュール docstring）
        recent_4h_candles=recent_4h,
        us_thin_market=is_us_thin_market(day),
        macro_context=None,  # BT-1 制限: 本番の取得失敗 degrade と同じ扱い
        session_open=session_split.session_open if session_split else None,
        session_high=session_split.session_high if session_split else None,
        session_low=session_split.session_low if session_split else None,
        tokyo_open=session_split.tokyo_open if session_split else None,
        tokyo_high=session_split.tokyo_high if session_split else None,
        tokyo_low=session_split.tokyo_low if session_split else None,
        europe_open=session_split.europe_open if session_split else None,
        europe_high=session_split.europe_high if session_split else None,
        europe_low=session_split.europe_low if session_split else None,
    )

    # 監査メタ: 文脈に寄与した全ローソク足（ATR/Pivot に使う日足は末尾 period+1 本）
    used: list[tuple[int, int]] = [
        (int(c["openTime"]), _MS_BY_INTERVAL["1day"]) for c in daily[-(ATR_PERIOD + 1) :]
    ]
    used.extend((int(c["openTime"]), _MS_BY_INTERVAL["4hour"]) for c in recent_4h)
    used.append((mid_open_time, _MS_5MIN))

    return DayContext(
        day=day,
        cutoff_ms=cutoff_ms,
        prompt_context=ctx,
        spread_pips=spread_pips,
        used_candles_ms=tuple(used),
    )
