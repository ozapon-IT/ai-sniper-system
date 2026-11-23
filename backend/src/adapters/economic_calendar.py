"""Economic calendar adapter — §3.3

Fetches high-impact USD/JPY events inside the holding window
``[decision date 20:30:00, next day 04:30:00]`` JST (#379) and enforces
the indicator day-skip hard rule (JST [20:30, 23:00] ∪ America/New_York
[07:30, 10:00], #374). Day-skip clock constants are not the window bounds.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

# DST は使うときに IANA の America/New_York へ変換する。+13/+14 の手計算はしない（#374）。
# ZoneInfo はシステムの tzdb を引く。tzdata パッケージは依存に入れていない。
# America/New_York の解決失敗は import では落ちず calendar_degraded（NO_ENTRY）。
# 原因は calendar_degrade_cause=tz_unresolved。再帰で取得失敗に畳まない（#376）。
# Asia/Tokyo はここで import 時に解決する。失敗するとモジュール import が落ち、
# 取得ラッパに専用の失敗分岐は置かない（#377 レビュー）。
# https://docs.python.org/3/library/zoneinfo.html （参照日 2026-11-05）
TZ_KEY_NEW_YORK = "America/New_York"
DEGRADE_CAUSE_TZ_UNRESOLVED = "tz_unresolved"
DEGRADE_CAUSE_CALENDAR_SOURCE = "calendar_source"

JST = ZoneInfo("Asia/Tokyo")

# 保有窓の両端（JST、秒まで）。判断日 D から [D 20:30:00, D+1 04:30:00] を作る（#379）。
# day-skip の JST_SKIP_START（20:30）とは別ルールなので定数を共有しない。
HOLDING_WINDOW_START = (20, 30, 0)
HOLDING_WINDOW_END = (4, 30, 0)

# day-skip の和集合。どちらも日をまたがないので分精度の閉区間で足りる（#374）。
# 保有窓の内外は fetch 側（秒まで比較）。ここは渡された CalendarResult の時刻帯だけを見る。
JST_SKIP_START = (20, 30)
JST_SKIP_END = (23, 0)
ET_SKIP_START = (7, 30)
ET_SKIP_END = (10, 0)


class ImpactLevel(StrEnum):
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"
    HOLIDAY = "Holiday"


@dataclass
class EconomicEvent:
    name: str
    datetime_jst: datetime  # timezone-aware, Asia/Tokyo
    impact: ImpactLevel
    currency: str
    forecast: str | None = None
    previous: str | None = None


@dataclass
class CalendarResult:
    events: list[EconomicEvent] = field(default_factory=list)
    source_ok: bool = True  # False when fetch failed (degraded mode)


def holding_window(decision_date: date) -> tuple[datetime, datetime]:
    """保有窓 ``[D 20:30:00 JST, D+1 04:30:00 JST]``（両端含む、秒まで）。

    判断日 D だけを受け、``HOLDING_WINDOW_START`` と ``HOLDING_WINDOW_END`` から
    両端を作る。任意の始点時刻は受け取らない（#379）。
    始点は Lambda の起動時刻ではなくスケジュール上の 20:30:00（コールドスタートで
    20:30 ちょうどを発表済みにしない。#374）。
    ``datetime`` を渡しても時刻は使わず、その暦日の ``date`` と同じ窓になる。
    """
    d = decision_date
    end_day = d + timedelta(days=1)
    start = datetime(d.year, d.month, d.day, *HOLDING_WINDOW_START, tzinfo=JST)
    end = datetime(end_day.year, end_day.month, end_day.day, *HOLDING_WINDOW_END, tzinfo=JST)
    return start, end


def _parse_feed_datetime(raw_dt: str) -> datetime | None:
    """オフセット付き ISO を JST にする。オフセット無しは None。

    naive を UTC とみなすと、窓の外だと証明したことになり USD/JPY High が
    ``source_ok=True`` のまま消える（#375 レビュー）。``Z`` と ``+00:00`` は
    オフセットがあるので尊重する。読めない値と ``OverflowError`` も None。
    """
    try:
        parsed = datetime.fromisoformat(raw_dt)
    except (ValueError, TypeError, OverflowError):
        return None
    if parsed.tzinfo is None:
        return None
    try:
        return parsed.astimezone(JST)
    except (ValueError, OverflowError, OSError):
        return None


def _eastern_zone() -> ZoneInfo:
    """米国東部。import 時には作らない。"""
    return ZoneInfo(TZ_KEY_NEW_YORK)


def _in_holding_window(dt_jst: datetime, start: datetime, end: datetime) -> bool:
    return start <= dt_jst <= end


class EconomicCalendarSource(ABC):
    @abstractmethod
    def fetch(self, decision_date: date) -> CalendarResult:
        """Return events in ``[D 20:30:00, D+1 04:30:00]`` JST for *decision_date*."""


class ForexFactorySource(EconomicCalendarSource):
    """Fetches the faireconomy weekly JSON mirror of ForexFactory.

    URL is non-SLA guaranteed; failures degrade gracefully (§3.3 degrade rule).
    Only USD and JPY high-impact events inside the holding window are retained.
    A parsed span that misses the holding window is ``source_ok=False`` (#375).
    An empty payload, or a payload with no readable datetime, is the same.
    """

    URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    TIMEOUT = 10
    TARGET_CURRENCIES = {"USD", "JPY"}

    def fetch(self, decision_date: date) -> CalendarResult:
        try:
            resp = requests.get(self.URL, timeout=self.TIMEOUT)
            resp.raise_for_status()
            raw: list[dict[str, Any]] = resp.json()
        except Exception as exc:
            logger.error({"event": "economic_calendar_fetch_failed", "error": str(exc)})
            return CalendarResult(events=[], source_ok=False)

        if not isinstance(raw, list):
            logger.error(
                {"event": "economic_calendar_parse_error", "error": "payload is not a list"}
            )
            return CalendarResult(events=[], source_ok=False)

        window_start, window_end = holding_window(decision_date)
        events: list[EconomicEvent] = []
        feed_instants: list[datetime] = []

        for item in raw:
            try:
                # 日時はここで 1 回だけパースし、分類にも同じ結果を渡す（#376）。
                dt_jst: datetime | None = None
                if isinstance(item, dict):
                    raw_dt = item.get("date")
                    if isinstance(raw_dt, str) and raw_dt:
                        dt_jst = _parse_feed_datetime(raw_dt)
                event = self._classify_item(item, window_start, window_end, dt_jst)
            except Exception as exc:
                # Fail closed: unreadable row may be an in-window USD/JPY
                # high-impact event we can't rule out (§3.3 / #224 / #374)
                logger.error(
                    {"event": "economic_calendar_parse_error", "item": item, "error": str(exc)}
                )
                return CalendarResult(events=[], source_ok=False)

            if dt_jst is not None:
                feed_instants.append(dt_jst)
            if event is not None:
                events.append(event)

        feed_min_iso, feed_max_iso = _feed_span(feed_instants)
        # 最小〜最大が保有窓と重ならないときは「指標なし」にしない（#375 指摘 1）。
        # 時刻が 0 件（空配列、または日時を 1 つも読めない）も同じ（#375 指摘 14）。
        covers_window = _feed_covers_holding_window(feed_instants, window_start, window_end)
        logger.info(
            {
                "event": "economic_calendar_fetched",
                "target_date": window_start.date().isoformat(),
                "high_impact_count": len(events),
                "feed_min_iso": feed_min_iso,
                "feed_max_iso": feed_max_iso,
                "feed_covers_window": covers_window,
            }
        )
        if not covers_window:
            return CalendarResult(events=[], source_ok=False)
        return CalendarResult(events=events, source_ok=True)

    def _classify_item(
        self,
        item: dict[str, Any],
        window_start: datetime,
        window_end: datetime,
        dt_jst: datetime | None,
    ) -> EconomicEvent | None:
        """行単位の分類（§3.3 / #224 行単位隔離、#374 で「他日」を保有窓の外に読み替え）。

        ``dt_jst`` は呼び出し側が 1 回パースした JST（読めなければ None）。ここでは再パースしない。

        - 読めたフィールドで「他通貨・保有窓の外・非 High」と証明できる行 → None
          （パース不能フィールドが混在していても warning ログのみで skip）。
        - 保有窓の中であり得る USD/JPY High の行に読めない必須フィールドが
          ある場合のみ ValueError（週全体 degrade・fail-closed 維持）。
        """
        currency_raw = item.get("country")
        currency = (
            currency_raw.strip().upper()
            if isinstance(currency_raw, str) and currency_raw.strip()
            else None
        )

        impact: ImpactLevel | None = None
        impact_raw = item.get("impact")
        if isinstance(impact_raw, str):
            try:
                impact = ImpactLevel(impact_raw.capitalize())
            except ValueError:
                impact = None

        provably_irrelevant = (
            (currency is not None and currency not in self.TARGET_CURRENCIES)
            or (dt_jst is not None and not _in_holding_window(dt_jst, window_start, window_end))
            or (impact is not None and impact is not ImpactLevel.HIGH)
        )
        unreadable = [
            name
            for name, value in (("date", dt_jst), ("impact", impact), ("country", currency))
            if value is None
        ]
        if provably_irrelevant:
            if unreadable:
                logger.warning(
                    {
                        "event": "economic_calendar_row_skipped",
                        "unreadable_fields": unreadable,
                        "item": item,
                    }
                )
            return None

        if dt_jst is None or impact is None or currency is None:
            raise ValueError(f"potentially relevant row unreadable: fields={unreadable}")

        return EconomicEvent(
            name=item.get("title", ""),
            datetime_jst=dt_jst,
            impact=impact,
            currency=currency,
            forecast=item.get("forecast") or None,
            previous=item.get("previous") or None,
        )


def _feed_span(instants: list[datetime]) -> tuple[str | None, str | None]:
    """読めた時刻の最小・最大。``datetime.isoformat()`` のみをログする。

    フィードの生文字列は小数秒が長くなり得る。判定に使う丸め後の時刻だけを残す。
    """
    if not instants:
        return None, None
    return min(instants).isoformat(), max(instants).isoformat()


def _feed_covers_holding_window(
    instants: list[datetime],
    window_start: datetime,
    window_end: datetime,
) -> bool:
    """読めた時刻の最小〜最大が保有窓と重なるか（両端含む）。

    時刻が 0 件のときは False。空の成功応答も、日時を 1 つも読めない応答も、
    静かな日とはみなさず ``source_ok=False`` にする（#375 指摘 14）。
    ``feed_max < window_start`` または ``feed_min > window_end`` のときも False。
    終点ちょうど（``feed_min == window_end``）は重なる。
    前週ファイルや、最終行が窓の開始より前の週を「指標なし」にしない（#375 指摘 1）。
    """
    if not instants:
        return False
    feed_min = min(instants)
    feed_max = max(instants)
    return feed_max >= window_start and feed_min <= window_end


def build_source(source_name: str | None = "forexfactory") -> EconomicCalendarSource:
    normalized = (source_name or "forexfactory").strip().lower()
    sources: dict[str, type[EconomicCalendarSource]] = {
        "forexfactory": ForexFactorySource,
    }
    cls = sources.get(normalized)
    if cls is None:
        logger.warning(
            {"event": "unknown_calendar_source", "source": source_name, "fallback": "forexfactory"}
        )
        cls = ForexFactorySource
    return cls()


# ---------------------------------------------------------------------------
# Hard rules — §3.3 (Ver 3.6 / RD-7、#374 で DST の和集合)
# ---------------------------------------------------------------------------


@dataclass
class IndicatorDaySkipResult:
    """day-skip 判定結果（LLM 呼び出し前 hard rule）。"""

    skip: bool
    reason: str | None = None  # "calendar_degraded" | "indicator_day_skip"
    triggering_events: list[EconomicEvent] = field(default_factory=list)
    # reason が calendar_degraded のときだけ埋める（#376）。reason 自体は変えない。
    # tz_unresolved: day-skip 判定時の America/New_York
    # calendar_source: フィード取得・範囲・パースの失敗
    calendar_degrade_cause: str | None = None
    calendar_tz_key: str | None = None


def _clock_minutes(dt: datetime, tz: ZoneInfo) -> int:
    local = dt.astimezone(tz)
    return local.hour * 60 + local.minute


def _in_clock_band(
    dt: datetime,
    tz: ZoneInfo,
    start: tuple[int, int],
    end: tuple[int, int],
) -> bool:
    """``tz`` の時計で ``[start, end]``（両端 inclusive、分精度）に入るか。"""
    minutes = _clock_minutes(dt, tz)
    start_m = start[0] * 60 + start[1]
    end_m = end[0] * 60 + end[1]
    return start_m <= minutes <= end_m


def _in_day_skip_window(event_time: datetime, et: ZoneInfo) -> bool:
    """JST [20:30, 23:00] ∪ 米国東部 [07:30, 10:00]（両端 inclusive）。

    夏（EDT）は両帯が一致する。冬（EST）の和は JST [20:30, 翌 00:00]。
    保有窓の外は fetch が落としているので、ここでは時刻帯だけを見る（#374）。
    """
    if _in_clock_band(event_time, JST, JST_SKIP_START, JST_SKIP_END):
        return True
    return _in_clock_band(event_time, et, ET_SKIP_START, ET_SKIP_END)


def _degraded_day_skip(
    *,
    cause: str,
    tz_key: str | None = None,
    error_type: str | None = None,
) -> IndicatorDaySkipResult:
    """calendar_degraded。警告には cause・tz_key・例外型名だけを出す（#376）。

    例外メッセージ全文は出さない（tzdb のパスやフィード本文を載せない）。
    この警告だけメッセージをイベント名にし、cause・tz_key・error_type は extra のキーにする
    （#376）。同モジュールの他の dict ログは変えない。
    """
    extra: dict[str, object] = {
        "action": "NO_ENTRY_conservative",
        "cause": cause,
        "tz_key": tz_key,
    }
    if error_type is not None:
        extra["error_type"] = error_type
    logger.warning("economic_calendar_degraded", extra=extra)
    return IndicatorDaySkipResult(
        skip=True,
        reason="calendar_degraded",
        calendar_degrade_cause=cause,
        calendar_tz_key=tz_key,
    )


def evaluate_indicator_day_skip(result: CalendarResult) -> IndicatorDaySkipResult:
    """判断バッチ day-skip hard rule（LLM 呼び出し前, §3.3 / RD-7 #172 / #374 / #376）。

    - カレンダー取得失敗（source_ok=False）→ ``calendar_source``（NO_ENTRY・保守的 degrade）
    - ``America/New_York`` が解決できないときも ``tz_unresolved``。
      ``_eastern_zone()`` の例外は種別を問わず捕まえる。再帰しない。
      入力の ``source_ok`` は書き換えない（フィード成功なら true のまま）
    - 高インパクト USD/JPY が JST [20:30, 23:00] または米国東部 [07:30, 10:00]
      に 1 件でもあれば skip
    - 和に入らない保有窓内の指標だけ → skip しない（プロンプト soft 判断へ）
    """
    if not result.source_ok:
        return _degraded_day_skip(cause=DEGRADE_CAUSE_CALENDAR_SOURCE)
    try:
        et = _eastern_zone()
    except Exception as exc:
        # try の中身は _eastern_zone() だけ。壊れた TZif は ValueError のほか、
        # 途中で切れたファイルの struct.error とフッタ改行の AssertionError も出す。
        # 種別を問わず tz_unresolved。source_ok は書き換えない。
        # error_type は型名のみで、例外メッセージは出さない（#376 / #378 レビュー）。
        # https://github.com/python/cpython/blob/3.11/Lib/zoneinfo/_common.py
        # （struct.unpack は 158 行付近、assert は 114 行付近。参照日 2026-11-06）
        return _degraded_day_skip(
            cause=DEGRADE_CAUSE_TZ_UNRESOLVED,
            tz_key=TZ_KEY_NEW_YORK,
            error_type=type(exc).__name__,
        )

    triggering = [
        ev
        for ev in result.events
        if ev.impact == ImpactLevel.HIGH
        and ev.currency in ForexFactorySource.TARGET_CURRENCIES
        and _in_day_skip_window(ev.datetime_jst, et)
    ]
    if triggering:
        for ev in triggering:
            logger.info(
                {
                    "event": "indicator_day_skip",
                    "indicator": ev.name,
                    "indicator_time_jst": ev.datetime_jst.isoformat(),
                    "currency": ev.currency,
                }
            )
        return IndicatorDaySkipResult(
            skip=True,
            reason="indicator_day_skip",
            triggering_events=triggering,
        )

    return IndicatorDaySkipResult(skip=False)
