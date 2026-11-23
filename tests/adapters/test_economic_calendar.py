"""Tests for backend/src/adapters/economic_calendar.py — §3.3"""

from __future__ import annotations

import logging
import struct
from datetime import date, datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
import requests

from backend.src.adapters.economic_calendar import (
    CalendarResult,
    EconomicCalendarSource,
    EconomicEvent,
    ForexFactorySource,
    ImpactLevel,
    build_source,
    evaluate_indicator_day_skip,
    holding_window,
)

JST = ZoneInfo("Asia/Tokyo")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TARGET_DATE = date(2026, 6, 28)


def _make_event(
    name: str = "CPI",
    hour: int = 21,
    minute: int = 30,
    impact: ImpactLevel = ImpactLevel.HIGH,
    currency: str = "USD",
) -> EconomicEvent:
    dt = datetime(2026, 6, 28, hour, minute, 0, tzinfo=JST)
    return EconomicEvent(name=name, datetime_jst=dt, impact=impact, currency=currency)


SAMPLE_API_RESPONSE = [
    {
        "title": "CPI m/m",
        "country": "USD",
        # ET offset -04:00 in summer → 08:30 ET = 12:30 UTC = 21:30 JST
        "date": "2026-06-28T08:30:00-04:00",
        "impact": "High",
        "forecast": "0.3%",
        "previous": "0.4%",
    },
    {
        "title": "Medium Event",
        "country": "USD",
        "date": "2026-06-28T10:00:00-04:00",
        "impact": "Medium",
        "forecast": None,
        "previous": None,
    },
    {
        "title": "EUR Event",
        "country": "EUR",
        "date": "2026-06-28T09:00:00-04:00",
        "impact": "High",
        "forecast": None,
        "previous": None,
    },
    {
        "title": "Old Event",
        "country": "USD",
        "date": "2026-06-27T08:30:00-04:00",
        "impact": "High",
        "forecast": None,
        "previous": None,
    },
]


# ---------------------------------------------------------------------------
# ForexFactorySource unit tests
# ---------------------------------------------------------------------------


class TestForexFactorySourceFetch:
    def _mock_response(self, json_data: list, status_code: int = 200) -> MagicMock:
        mock = MagicMock()
        mock.json.return_value = json_data
        mock.raise_for_status.side_effect = (
            None
            if status_code == 200
            else requests.HTTPError(response=MagicMock(status_code=status_code))
        )
        return mock

    def test_filters_to_high_impact_usd_jpy_on_target_date(self):
        source = ForexFactorySource()
        with patch("requests.get", return_value=self._mock_response(SAMPLE_API_RESPONSE)):
            result = source.fetch(TARGET_DATE)

        assert result.source_ok is True
        assert len(result.events) == 1
        event = result.events[0]
        assert event.name == "CPI m/m"
        assert event.impact == ImpactLevel.HIGH
        assert event.currency == "USD"
        # ET summer offset -04:00 → 08:30-04:00 = 12:30 UTC = 21:30 JST
        assert event.datetime_jst.hour == 21
        assert event.datetime_jst.minute == 30

    def test_http_error_returns_degraded_result(self):
        source = ForexFactorySource()
        with patch("requests.get", side_effect=requests.HTTPError("503")):
            result = source.fetch(TARGET_DATE)

        assert result.source_ok is False
        assert result.events == []

    def test_timeout_returns_degraded_result(self):
        source = ForexFactorySource()
        with patch("requests.get", side_effect=requests.Timeout("timeout")):
            result = source.fetch(TARGET_DATE)

        assert result.source_ok is False
        assert result.events == []

    def test_medium_impact_excluded(self):
        source = ForexFactorySource()
        medium_only = [SAMPLE_API_RESPONSE[1]]  # Medium Event
        with patch("requests.get", return_value=self._mock_response(medium_only)):
            result = source.fetch(TARGET_DATE)

        assert result.source_ok is True
        assert result.events == []

    def test_eur_currency_excluded(self):
        source = ForexFactorySource()
        eur_only = [SAMPLE_API_RESPONSE[2]]  # EUR Event
        with patch("requests.get", return_value=self._mock_response(eur_only)):
            result = source.fetch(TARGET_DATE)

        assert result.events == []

    def test_different_date_excluded(self):
        source = ForexFactorySource()
        old_only = [SAMPLE_API_RESPONSE[3]]  # previous day
        with patch("requests.get", return_value=self._mock_response(old_only)):
            result = source.fetch(TARGET_DATE)

        assert result.events == []

    def test_missing_date_degrades_source(self) -> None:
        """空 date は fail-closed: source_ok=False を返す（§3.3）。"""
        no_date_item = {
            "title": "CPI m/m",
            "country": "USD",
            "date": "",
            "impact": "High",
        }
        source = ForexFactorySource()
        with patch("requests.get", return_value=self._mock_response([no_date_item])):
            result = source.fetch(TARGET_DATE)

        assert result.source_ok is False
        assert result.events == []

    # -- 行単位隔離（§3.3 / #224）: mixed valid/invalid week -------------

    VALID_TARGET_ROW = {
        "title": "CPI m/m",
        "country": "USD",
        "date": "2026-06-28T08:30:00-04:00",
        "impact": "High",
        "forecast": "0.3%",
        "previous": "0.4%",
    }

    def test_broken_irrelevant_currency_row_is_isolated(self) -> None:
        """EUR 行の date 破損は skip され、同週の有効行は生き残る（#224）。"""
        broken_eur = {
            "title": "EUR Event",
            "country": "EUR",
            "date": "not-a-date",
            "impact": "High",
        }
        source = ForexFactorySource()
        with patch(
            "requests.get",
            return_value=self._mock_response([broken_eur, self.VALID_TARGET_ROW]),
        ):
            result = source.fetch(TARGET_DATE)

        assert result.source_ok is True
        assert len(result.events) == 1
        assert result.events[0].name == "CPI m/m"

    def test_broken_other_day_row_is_isolated(self) -> None:
        """他日と読める行は impact 破損でも skip（週全体を倒さない）。"""
        broken_other_day = {
            "title": "Old Event",
            "country": "USD",
            "date": "2026-06-27T08:30:00-04:00",
            "impact": "???",
        }
        source = ForexFactorySource()
        with patch(
            "requests.get",
            return_value=self._mock_response([broken_other_day, self.VALID_TARGET_ROW]),
        ):
            result = source.fetch(TARGET_DATE)

        assert result.source_ok is True
        assert len(result.events) == 1

    def test_broken_non_high_row_is_isolated(self) -> None:
        """非 High と読める行は date 破損でも skip。"""
        broken_medium = {
            "title": "Medium Event",
            "country": "USD",
            "date": None,
            "impact": "Medium",
        }
        source = ForexFactorySource()
        with patch(
            "requests.get",
            return_value=self._mock_response([broken_medium, self.VALID_TARGET_ROW]),
        ):
            result = source.fetch(TARGET_DATE)

        assert result.source_ok is True
        assert len(result.events) == 1

    def test_potentially_relevant_broken_date_degrades(self) -> None:
        """対象日 USD/JPY High とあり得る行の date 破損は fail-closed。"""
        broken_relevant = {
            "title": "NFP",
            "country": "USD",
            "date": "garbage",
            "impact": "High",
        }
        source = ForexFactorySource()
        with patch(
            "requests.get",
            return_value=self._mock_response([self.VALID_TARGET_ROW, broken_relevant]),
        ):
            result = source.fetch(TARGET_DATE)

        assert result.source_ok is False
        assert result.events == []

    def test_target_day_usd_unknown_impact_degrades(self) -> None:
        """対象日 USD 行の未知 impact は Low へ黙殺せず fail-closed（#224）。"""
        unknown_impact = {
            "title": "CPI m/m",
            "country": "USD",
            "date": "2026-06-28T08:30:00-04:00",
            "impact": "high-impact!!",
        }
        source = ForexFactorySource()
        with patch("requests.get", return_value=self._mock_response([unknown_impact])):
            result = source.fetch(TARGET_DATE)

        assert result.source_ok is False

    def test_target_day_high_missing_currency_degrades(self) -> None:
        """対象日 High 行の currency 欠損は fail-closed（従来は黙って除外）。"""
        no_currency = {
            "title": "Mystery Event",
            "country": "",
            "date": "2026-06-28T08:30:00-04:00",
            "impact": "High",
        }
        source = ForexFactorySource()
        with patch("requests.get", return_value=self._mock_response([no_currency])):
            result = source.fetch(TARGET_DATE)

        assert result.source_ok is False

    def test_non_dict_row_degrades(self) -> None:
        """行が dict ですらない場合は分類不能 → fail-closed。"""
        source = ForexFactorySource()
        with patch(
            "requests.get",
            return_value=self._mock_response(["corrupted", self.VALID_TARGET_ROW]),
        ):
            result = source.fetch(TARGET_DATE)

        assert result.source_ok is False

    def test_lowercase_target_currency_is_normalized(self) -> None:
        """小文字の対象通貨は正規化され High 指標として扱われる（#224）。"""
        lowercase_usd = {
            "title": "CPI m/m",
            "country": "usd",
            "date": "2026-06-28T08:30:00-04:00",
            "impact": "High",
            "forecast": "0.3%",
            "previous": "0.4%",
        }
        source = ForexFactorySource()
        with patch("requests.get", return_value=self._mock_response([lowercase_usd])):
            result = source.fetch(TARGET_DATE)

        assert result.source_ok is True
        assert len(result.events) == 1
        assert result.events[0].currency == "USD"

    def test_whitespace_padded_target_currency_is_normalized(self) -> None:
        """空白付きの対象通貨は正規化され High 指標として扱われる（#224）。

        時刻は翌 03:00 JST（保有窓の内側）。同日 03:00 は窓の外（#379）。
        """
        padded_jpy = {
            "title": "BOJ Rate Decision",
            "country": " JPY ",
            "date": "2026-06-29T03:00:00+09:00",
            "impact": "High",
            "forecast": None,
            "previous": None,
        }
        source = ForexFactorySource()
        with patch("requests.get", return_value=self._mock_response([padded_jpy])):
            result = source.fetch(TARGET_DATE)

        assert result.source_ok is True
        assert len(result.events) == 1
        assert result.events[0].currency == "JPY"

    def test_lowercase_irrelevant_currency_still_excluded(self) -> None:
        """小文字の対象外通貨は正規化後も除外される（#224 逆パターン）。"""
        lowercase_eur = {
            "title": "EUR Event",
            "country": "eur",
            "date": "2026-06-28T08:30:00-04:00",
            "impact": "High",
        }
        source = ForexFactorySource()
        with patch("requests.get", return_value=self._mock_response([lowercase_eur])):
            result = source.fetch(TARGET_DATE)

        assert result.source_ok is True
        assert result.events == []

    def test_jpy_high_impact_included(self):
        """JPY High は翌 03:00 JST（保有窓の内側）なら採用する（#379）。"""
        jpy_event = {
            "title": "BOJ Rate Decision",
            "country": "JPY",
            "date": "2026-06-29T03:00:00+09:00",
            "impact": "High",
            "forecast": None,
            "previous": None,
        }
        source = ForexFactorySource()
        with patch("requests.get", return_value=self._mock_response([jpy_event])):
            result = source.fetch(TARGET_DATE)

        assert len(result.events) == 1
        assert result.events[0].currency == "JPY"


# ---------------------------------------------------------------------------
# evaluate_indicator_day_skip — §3.3 / RD-7 #172
# ---------------------------------------------------------------------------


class TestEvaluateIndicatorDaySkip:
    def test_21_30_employment_triggers_skip(self) -> None:
        result = CalendarResult(events=[_make_event(hour=21, minute=30)], source_ok=True)
        skip = evaluate_indicator_day_skip(result)
        assert skip.skip is True
        assert skip.reason == "indicator_day_skip"
        assert len(skip.triggering_events) == 1

    def test_23_30_indicator_does_not_skip(self) -> None:
        result = CalendarResult(events=[_make_event(hour=23, minute=30)], source_ok=True)
        skip = evaluate_indicator_day_skip(result)
        assert skip.skip is False

    def test_23_00_boundary_in_window(self) -> None:
        result = CalendarResult(events=[_make_event(hour=23, minute=0)], source_ok=True)
        skip = evaluate_indicator_day_skip(result)
        assert skip.skip is True

    def test_20_30_boundary_in_window(self) -> None:
        result = CalendarResult(events=[_make_event(hour=20, minute=30)], source_ok=True)
        skip = evaluate_indicator_day_skip(result)
        assert skip.skip is True

    def test_eur_high_impact_in_window_does_not_skip(self) -> None:
        result = CalendarResult(
            events=[_make_event(hour=21, minute=30, currency="EUR")],
            source_ok=True,
        )
        skip = evaluate_indicator_day_skip(result)
        assert skip.skip is False

    def test_no_events_does_not_skip(self) -> None:
        result = CalendarResult(events=[], source_ok=True)
        skip = evaluate_indicator_day_skip(result)
        assert skip.skip is False

    def test_calendar_degraded_triggers_skip(self) -> None:
        result = CalendarResult(events=[], source_ok=False)
        skip = evaluate_indicator_day_skip(result)
        assert skip.skip is True
        assert skip.reason == "calendar_degraded"

    def test_morning_event_does_not_skip(self) -> None:
        result = CalendarResult(events=[_make_event(hour=7, minute=30)], source_ok=True)
        skip = evaluate_indicator_day_skip(result)
        assert skip.skip is False


# ---------------------------------------------------------------------------
# build_source factory
# ---------------------------------------------------------------------------


class TestBuildSource:
    def test_forexfactory_returns_correct_type(self) -> None:
        source = build_source("forexfactory")
        assert isinstance(source, ForexFactorySource)

    def test_unknown_source_falls_back_to_forexfactory(self) -> None:
        source = build_source("unknown_source")
        assert isinstance(source, ForexFactorySource)

    def test_case_insensitive(self) -> None:
        source = build_source("ForexFactory")
        assert isinstance(source, ForexFactorySource)

    def test_none_falls_back_to_forexfactory(self) -> None:
        source = build_source(None)
        assert isinstance(source, ForexFactorySource)

    def test_empty_string_falls_back_to_forexfactory(self) -> None:
        source = build_source("")
        assert isinstance(source, ForexFactorySource)


# ---------------------------------------------------------------------------
# EconomicCalendarSource is abstract
# ---------------------------------------------------------------------------


def test_source_is_abstract():
    with pytest.raises(TypeError):
        EconomicCalendarSource()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# 保有窓と day-skip の和集合（#374）。上の 32 件のアサーションは変えない。
# ---------------------------------------------------------------------------


def _feed_row(
    date: str,
    *,
    title: str = "Indicator",
    country: str = "USD",
    impact: str = "High",
) -> dict[str, object]:
    return {"title": title, "country": country, "date": date, "impact": impact}


def _fetch_rows(decision_date: date, rows: list[dict[str, object]]) -> CalendarResult:
    source = ForexFactorySource()
    mock = MagicMock()
    mock.json.return_value = rows
    mock.raise_for_status.return_value = None
    with patch("requests.get", return_value=mock):
        return source.fetch(decision_date)


def _day(year: int, month: int, day: int) -> date:
    return date(year, month, day)


def test_holding_window_ignores_datetime_clock() -> None:
    """``datetime`` の時刻は使わず、同じ暦日の ``date`` と同じ窓になる（#379）。"""
    # 21:07 は窓の始点 20:30 ではない。時刻を始点にするとこの比較は落ちる。
    clock = datetime(2026, 6, 28, 21, 7, tzinfo=JST)
    assert holding_window(clock) == holding_window(date(2026, 6, 28))


class TestHoldingWindowTable:
    """第6節の合成時刻。ネットワークは使わない。"""

    @pytest.mark.parametrize(
        ("when", "raw_date", "included", "skip"),
        [
            (_day(2026, 12, 1), "2026-12-01T10:00:00-05:00", True, True),
            (_day(2026, 12, 1), "2026-12-01T20:30:00+09:00", True, True),
            (_day(2026, 12, 1), "2026-12-01T20:29:00+09:00", False, False),
            (_day(2026, 12, 1), "2026-12-02T04:30:00+09:00", True, False),
            (_day(2026, 12, 1), "2026-12-02T04:30:01+09:00", False, False),
            (_day(2026, 12, 1), "2026-12-02T04:31:00+09:00", False, False),
            (_day(2026, 12, 2), "2026-12-02T00:00:00+09:00", False, False),
            (_day(2026, 8, 20), "2026-08-20T03:00:00+09:00", False, False),
            (_day(2026, 8, 3), "2026-08-03T10:00:00-04:00", True, True),
            (_day(2026, 9, 1), "2026-09-01T10:00:00-04:00", True, True),
            (_day(2026, 8, 3), "2026-08-03T23:30:00+09:00", True, False),
            (_day(2026, 12, 1), "2026-12-01T23:30:00+09:00", True, True),
            (_day(2026, 10, 30), "2026-10-30T10:00:00-04:00", True, True),
            (_day(2026, 11, 2), "2026-11-02T10:00:00-05:00", True, True),
            (_day(2026, 3, 6), "2026-03-06T10:00:00-05:00", True, True),
            (_day(2026, 3, 9), "2026-03-09T10:00:00-04:00", True, True),
        ],
        ids=[
            "dec1_ism_0000_jst_skips",
            "dec1_2030_exact_skips",
            "dec1_2029_excluded",
            "dec1_0430_soft",
            "dec1_0430_01_excluded",
            "dec1_0431_excluded",
            "dec2_midnight_already_released",
            "aug20_0300_already_released",
            "aug3_ism_2300_skips",
            "sep1_ism_2300_skips",
            "aug3_2330_soft",
            "dec1_2330_winter_skips",
            "oct30_edt_1000_skips",
            "nov2_est_1000_skips",
            "mar6_est_1000_skips",
            "mar9_edt_1000_skips",
        ],
    )
    def test_window_and_skip(
        self,
        when: date,
        raw_date: str,
        included: bool,
        skip: bool,
    ) -> None:
        # 対象行が窓の外だけだと範囲が窓と重ならず source_ok=False になる。
        # 窓の内外の判定は、窓をまたぐ別行を足したフィードで見る（#375 指摘 1）。
        # 判断日 21:30 は [20:30, 翌 04:30] の内側。以前の ``when + 1h``（20:30 起点）と同じ時刻。
        cover = f"{when.isoformat()}T21:30:00+09:00"
        result = _fetch_rows(
            when,
            [
                _feed_row(raw_date),
                _feed_row(cover, title="Span cover", country="EUR", impact="Low"),
            ],
        )
        assert result.source_ok is True
        assert (len(result.events) == 1) is included
        decision = evaluate_indicator_day_skip(result)
        assert decision.skip is skip
        if included and skip:
            assert len(decision.triggering_events) == 1


class TestHoldingWindowScenarios:
    def test_sep17_released_fomc_is_outside(self) -> None:
        result = _fetch_rows(
            _day(2026, 9, 17),
            [
                _feed_row("2026-09-17T03:00:00+09:00", title="FOMC"),
                _feed_row("2026-09-17T03:30:00+09:00", title="FOMC Press"),
                # 窓の後。範囲が保有窓と重なるので、発表済みだけなら skip しない。
                _feed_row("2026-09-18T10:00:00+09:00", title="Later", country="EUR", impact="Low"),
            ],
        )
        assert result.source_ok is True
        assert result.events == []
        assert evaluate_indicator_day_skip(result).skip is False

    def test_dec10_released_fomc_is_outside(self) -> None:
        result = _fetch_rows(
            _day(2026, 12, 10),
            [
                _feed_row("2026-12-10T04:00:00+09:00", title="FOMC"),
                _feed_row("2026-12-10T04:30:00+09:00", title="FOMC Press"),
                # 窓の後。範囲が保有窓と重なるので、発表済みだけなら skip しない。
                _feed_row("2026-12-11T10:00:00+09:00", title="Later", country="EUR", impact="Low"),
            ],
        )
        assert result.events == []
        assert evaluate_indicator_day_skip(result).skip is False

    def test_dec9_fomc_is_in_window_and_soft(self) -> None:
        from backend.src.llm.prompt import EconomicEvent as PromptEvent
        from backend.src.llm.prompt import _format_economic_event

        result = _fetch_rows(
            _day(2026, 12, 9),
            [
                _feed_row("2026-12-09T14:00:00-05:00", title="FOMC Statement"),
                _feed_row("2026-12-09T14:30:00-05:00", title="FOMC Press Conference"),
            ],
        )
        assert result.source_ok is True
        assert [
            (ev.name, ev.datetime_jst.hour, ev.datetime_jst.minute) for ev in result.events
        ] == [
            ("FOMC Statement", 4, 0),
            ("FOMC Press Conference", 4, 30),
        ]
        assert result.events[0].datetime_jst.day == 10
        decision = evaluate_indicator_day_skip(result)
        assert decision.skip is False
        lines = [
            _format_economic_event(
                PromptEvent(
                    time_jst=ev.datetime_jst.strftime("%H:%M"),
                    date_jst=ev.datetime_jst.strftime("%Y-%m-%d"),
                    currency=ev.currency,
                    name=ev.name,
                    impact=ev.impact.value,
                )
            )
            for ev in result.events
        ]
        assert "12/10 04:00 JST（保有中） (USD) FOMC Statement" in lines[0]
        assert "12/10 04:30 JST（保有中） (USD) FOMC Press Conference" in lines[1]

    def test_friday_feed_keeps_next_morning_jst_and_drops_monday(self) -> None:
        from backend.src.llm.prompt import EconomicEvent as PromptEvent
        from backend.src.llm.prompt import _format_economic_event

        result = _fetch_rows(
            _day(2026, 11, 6),
            [
                _feed_row("2026-11-06T08:30:00-05:00", title="Non-Farm Payrolls"),
                _feed_row("2026-11-06T11:00:00-05:00", title="BOJ Governor Speaks", country="JPY"),
                _feed_row("2026-11-09T00:00:00+09:00", title="Monday Event"),
            ],
        )
        by_name = {ev.name: ev for ev in result.events}
        assert set(by_name) == {"Non-Farm Payrolls", "BOJ Governor Speaks"}
        nfp = by_name["Non-Farm Payrolls"]
        speech = by_name["BOJ Governor Speaks"]
        assert (nfp.datetime_jst.month, nfp.datetime_jst.day, nfp.datetime_jst.hour) == (11, 6, 22)
        assert nfp.datetime_jst.minute == 30
        assert (speech.datetime_jst.month, speech.datetime_jst.day, speech.datetime_jst.hour) == (
            11,
            7,
            1,
        )
        decision = evaluate_indicator_day_skip(result)
        assert decision.skip is True
        assert [ev.name for ev in decision.triggering_events] == ["Non-Farm Payrolls"]
        line = _format_economic_event(
            PromptEvent(
                time_jst=speech.datetime_jst.strftime("%H:%M"),
                date_jst=speech.datetime_jst.strftime("%Y-%m-%d"),
                currency=speech.currency,
                name=speech.name,
                impact=speech.impact.value,
            )
        )
        assert "11/7 01:00 JST（保有中） (JPY) BOJ Governor Speaks" in line

    def test_mixed_offsets_in_one_payload_are_honored_per_row(self) -> None:
        result = _fetch_rows(
            _day(2026, 11, 2),
            [
                _feed_row("2026-11-02T08:00:00-04:00", title="Offset minus four"),
                _feed_row("2026-11-02T08:00:00-05:00", title="Offset minus five", country="JPY"),
            ],
        )
        hours = {ev.name: ev.datetime_jst.hour for ev in result.events}
        assert hours == {"Offset minus four": 21, "Offset minus five": 22}

    def test_broken_impact_inside_next_morning_fail_closes(self) -> None:
        """翌日 02:00 の壊れ行は 20:30 始点の窓の内側なので週全体を倒す（#224 / #374）。"""
        result = _fetch_rows(
            _day(2026, 12, 1),
            [
                _feed_row("2026-12-01T21:30:00+09:00", title="CPI m/m"),
                {
                    "title": "Mystery",
                    "country": "USD",
                    "date": "2026-12-02T02:00:00+09:00",
                    "impact": "???",
                },
            ],
        )
        assert result.source_ok is False
        assert result.events == []

    def test_empty_json_fail_closes(self) -> None:
        """HTTP 200 の空配列は静かな日とみなさず source_ok=False（#375 指摘 14）。"""
        result = _fetch_rows(_day(2026, 12, 1), [])
        assert result.source_ok is False
        assert result.events == []
        skip = evaluate_indicator_day_skip(result)
        assert skip.skip is True
        assert skip.reason == "calendar_degraded"

    def test_no_readable_datetime_fail_closes(self) -> None:
        """日時を 1 つも読めず USD/JPY High も無いフィードは source_ok=False。"""
        result = _fetch_rows(
            _day(2026, 12, 1),
            [
                _feed_row("2026-12-01T08:30:00", title="EUR naive", country="EUR"),
                {"title": "EUR missing date", "country": "EUR", "impact": "High"},
            ],
        )
        assert result.source_ok is False
        assert result.events == []
        assert evaluate_indicator_day_skip(result).reason == "calendar_degraded"

    def test_feed_outside_holding_window_fail_closes_and_logs_span(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """11 月のフィードと 12/1 判断は重ならない。source_ok=False（#375 指摘 1）。

        フィードの端はログに残す。
        """
        rows = [
            _feed_row("2026-11-01T08:00:00-04:00", title="Early", country="EUR"),
            _feed_row("2026-11-06T18:00:00-05:00", title="Late", country="GBP", impact="Low"),
        ]
        with caplog.at_level(logging.INFO, logger="backend.src.adapters.economic_calendar"):
            result = _fetch_rows(_day(2026, 12, 1), rows)
        assert result.source_ok is False
        assert result.events == []
        skip = evaluate_indicator_day_skip(result)
        assert skip.skip is True
        assert skip.reason == "calendar_degraded"
        # 生のオフセット文字列ではなく、JST に直した isoformat。
        assert "2026-11-01T21:00:00+09:00" in caplog.text
        assert "2026-11-07T08:00:00+09:00" in caplog.text
        assert "2026-11-01T08:00:00-04:00" not in caplog.text

    def test_monday_previous_week_feed_is_calendar_degraded(self) -> None:
        """月曜 12/7 に前週（12/4 まで）しか無いと、その夜の NFP を見逃して発注しない。"""
        result = _fetch_rows(
            _day(2026, 12, 7),
            [
                _feed_row("2026-12-04T08:30:00-05:00", title="Non-Farm Payrolls"),
                _feed_row("2026-12-04T16:00:00-05:00", title="Late Friday", country="JPY"),
            ],
        )
        assert result.source_ok is False
        assert result.events == []
        skip = evaluate_indicator_day_skip(result)
        assert skip.skip is True
        assert skip.reason == "calendar_degraded"

    def test_span_bracketing_window_without_usd_jpy_high_stays_ok(self) -> None:
        """範囲が窓をまたぎ、窓内に USD/JPY High が無い夜は source_ok=True。"""
        result = _fetch_rows(
            _day(2026, 12, 7),
            [
                _feed_row("2026-12-07T10:00:00+09:00", title="Morning", country="EUR"),
                _feed_row(
                    "2026-12-08T10:00:00+09:00",
                    title="After window",
                    country="GBP",
                    impact="Low",
                ),
            ],
        )
        assert result.source_ok is True
        assert result.events == []
        assert evaluate_indicator_day_skip(result).skip is False

    def test_span_touching_window_start_stays_ok(self) -> None:
        """最終行が窓の開始ちょうどなら重なる。"""
        result = _fetch_rows(
            _day(2026, 12, 7),
            [_feed_row("2026-12-07T20:30:00+09:00", title="At start", country="EUR", impact="Low")],
        )
        assert result.source_ok is True
        assert result.events == []

    def test_span_one_second_before_window_fail_closes(self) -> None:
        """最終行が窓の開始の 1 秒前なら重ならない。"""
        result = _fetch_rows(
            _day(2026, 12, 7),
            [
                _feed_row(
                    "2026-12-07T20:29:59+09:00",
                    title="Just before",
                    country="EUR",
                    impact="Low",
                )
            ],
        )
        assert result.source_ok is False
        assert result.events == []
        assert evaluate_indicator_day_skip(result).reason == "calendar_degraded"

    def test_span_starting_after_window_fail_closes(self) -> None:
        """最初の行が窓の終了より後なら重ならない。"""
        result = _fetch_rows(
            _day(2026, 12, 7),
            [_feed_row("2026-12-08T04:30:01+09:00", title="After close")],
        )
        assert result.source_ok is False
        assert result.events == []

    def test_span_touching_window_end_stays_ok(self) -> None:
        """最初の行が窓の終了 04:30:00 ちょうどなら重なる（両端含む・#375 指摘 16）。

        ``feed_min <= window_end`` を ``<`` にすると source_ok=False になり落ちる。
        """
        result = _fetch_rows(
            _day(2026, 12, 7),
            [
                _feed_row(
                    "2026-12-08T04:30:00+09:00",
                    title="At end",
                    country="EUR",
                    impact="Low",
                )
            ],
        )
        assert result.source_ok is True
        assert result.events == []
        assert evaluate_indicator_day_skip(result).skip is False


class TestFeedDatetimeFailClosed:
    """オフセット欠落と極端な date は窓外の証明にしない（#375 レビュー）。"""

    def test_naive_usd_high_is_not_treated_as_utc(self) -> None:
        """``2026-11-06T08:30:00``（USD High）は source_ok=False。オフセット付きは skip。"""
        naive = _fetch_rows(
            _day(2026, 11, 6),
            [_feed_row("2026-11-06T08:30:00", title="Non-Farm Payrolls")],
        )
        assert naive.source_ok is False
        assert naive.events == []
        skip = evaluate_indicator_day_skip(naive)
        assert skip.skip is True
        assert skip.reason == "calendar_degraded"

        aware = _fetch_rows(
            _day(2026, 11, 6),
            [_feed_row("2026-11-06T08:30:00-05:00", title="Non-Farm Payrolls")],
        )
        assert aware.source_ok is True
        assert aware.events[0].datetime_jst.isoformat() == "2026-11-06T22:30:00+09:00"
        assert evaluate_indicator_day_skip(aware).reason == "indicator_day_skip"

    def test_naive_jpy_high_also_fail_closes(self) -> None:
        result = _fetch_rows(
            _day(2026, 11, 6),
            [_feed_row("2026-11-06T08:30:00", title="BOJ", country="JPY")],
        )
        assert result.source_ok is False
        assert result.events == []

    def test_naive_other_currency_or_non_high_is_row_skipped(self) -> None:
        result = _fetch_rows(
            _day(2026, 11, 6),
            [
                _feed_row("2026-11-06T08:30:00", title="EUR", country="EUR"),
                _feed_row("2026-11-06T08:30:00", title="Medium", impact="Medium"),
                _feed_row("2026-11-06T08:30:00-05:00", title="Non-Farm Payrolls"),
            ],
        )
        assert result.source_ok is True
        assert [ev.name for ev in result.events] == ["Non-Farm Payrolls"]

    def test_z_and_explicit_utc_offset_are_honored(self) -> None:
        result = _fetch_rows(
            _day(2026, 11, 6),
            [
                _feed_row("2026-11-06T12:00:00Z", title="From Z"),
                _feed_row("2026-11-06T12:30:00+00:00", title="From Plus Zero", country="JPY"),
            ],
        )
        assert result.source_ok is True
        by_name = {ev.name: ev.datetime_jst for ev in result.events}
        assert by_name["From Z"].isoformat() == "2026-11-06T21:00:00+09:00"
        assert by_name["From Plus Zero"].isoformat() == "2026-11-06T21:30:00+09:00"

    def test_overflow_on_usd_high_fail_closes_without_raising(self) -> None:
        result = _fetch_rows(
            _day(2026, 11, 6),
            [_feed_row("9999-12-31T23:59:59+00:00", title="Far")],
        )
        assert result.source_ok is False
        assert result.events == []

    def test_overflow_on_other_currency_is_isolated(self) -> None:
        result = _fetch_rows(
            _day(2026, 11, 6),
            [
                _feed_row("9999-12-31T23:59:59+00:00", title="Far", country="EUR"),
                _feed_row("2026-11-06T08:30:00-05:00", title="Non-Farm Payrolls"),
            ],
        )
        assert result.source_ok is True
        assert [ev.name for ev in result.events] == ["Non-Farm Payrolls"]

    def test_null_payload_returns_degraded_without_raising(self) -> None:
        source = ForexFactorySource()
        mock = MagicMock()
        mock.json.return_value = None
        mock.raise_for_status.return_value = None
        with patch("requests.get", return_value=mock):
            result = source.fetch(_day(2026, 11, 6))
        assert result.source_ok is False
        assert result.events == []

    def test_long_fraction_is_logged_as_isoformat(self, caplog: pytest.LogCaptureFixture) -> None:
        raw = "2026-12-01T14:00:00." + ("1" * 40) + "-05:00"
        with caplog.at_level(logging.INFO, logger="backend.src.adapters.economic_calendar"):
            result = _fetch_rows(
                _day(2026, 12, 1),
                [_feed_row(raw, title="Late", country="GBP", impact="Low")],
            )
        assert result.source_ok is True
        assert raw not in caplog.text
        assert "2026-12-02T04:00:00.111111+09:00" in caplog.text

    def test_missing_america_new_york_records_tz_cause(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """America/New_York 失敗は再帰せず、source_ok を書き換えない（#376）。"""
        from zoneinfo import ZoneInfo as RealZoneInfo
        from zoneinfo import ZoneInfoNotFoundError

        secret = "NY_TZ_RESOLVE_DETAIL_DO_NOT_LOG"

        def _zone(key: str) -> RealZoneInfo:
            if key == "America/New_York":
                raise ZoneInfoNotFoundError(secret)
            return RealZoneInfo(key)

        result = CalendarResult(events=[_make_event(hour=21, minute=30)], source_ok=True)
        with (
            caplog.at_level(logging.WARNING, logger="backend.src.adapters.economic_calendar"),
            patch("backend.src.adapters.economic_calendar.ZoneInfo", side_effect=_zone),
        ):
            skip = evaluate_indicator_day_skip(result)
        assert result.source_ok is True
        assert skip.skip is True
        assert skip.reason == "calendar_degraded"
        assert skip.calendar_degrade_cause == "tz_unresolved"
        assert skip.calendar_tz_key == "America/New_York"
        warnings = [
            record
            for record in caplog.records
            if record.getMessage() == "economic_calendar_degraded"
        ]
        assert len(warnings) == 1
        warning = warnings[0]
        assert warning.levelno == logging.WARNING
        assert warning.cause == "tz_unresolved"
        assert warning.tz_key == "America/New_York"
        assert warning.error_type == "ZoneInfoNotFoundError"
        rendered = " ".join(str(value) for value in warning.__dict__.values())
        assert secret not in rendered
        assert secret not in caplog.text

    @pytest.mark.parametrize(
        "exc_type",
        [ValueError, OSError, struct.error, AssertionError],
    )
    def test_eastern_zone_resolution_errors_are_tz_unresolved(
        self, exc_type: type[Exception], caplog: pytest.LogCaptureFixture
    ) -> None:
        """解決時の例外は種別を問わず tz_unresolved。source_ok は維持（#376 / #378）。"""
        from zoneinfo import ZoneInfo as RealZoneInfo

        secret = "BROKEN_TZ_DETAIL_DO_NOT_LOG"

        def _zone(key: str) -> RealZoneInfo:
            if key == "America/New_York":
                raise exc_type(secret)
            return RealZoneInfo(key)

        result = CalendarResult(events=[_make_event(hour=21, minute=30)], source_ok=True)
        with (
            caplog.at_level(logging.WARNING, logger="backend.src.adapters.economic_calendar"),
            patch("backend.src.adapters.economic_calendar.ZoneInfo", side_effect=_zone),
        ):
            skip = evaluate_indicator_day_skip(result)
        assert result.source_ok is True
        assert skip.skip is True
        assert skip.reason == "calendar_degraded"
        assert skip.calendar_degrade_cause == "tz_unresolved"
        assert skip.calendar_tz_key == "America/New_York"
        warnings = [
            record
            for record in caplog.records
            if record.getMessage() == "economic_calendar_degraded"
        ]
        assert len(warnings) == 1
        warning = warnings[0]
        assert warning.error_type == exc_type.__name__
        rendered = " ".join(str(value) for value in warning.__dict__.values())
        assert secret not in rendered
        assert secret not in caplog.text

    def test_feed_datetime_parsed_once_per_row(self) -> None:
        """行の日時はループで 1 回だけパースし、分類は同じ結果を使う（#376）。"""
        from backend.src.adapters.economic_calendar import _parse_feed_datetime

        calls: list[str] = []

        def _spy(raw_dt: str) -> datetime | None:
            calls.append(raw_dt)
            return _parse_feed_datetime(raw_dt)

        rows = [
            _feed_row("2026-12-01T10:00:00-05:00", title="ISM"),
            _feed_row("2026-12-01T20:30:00+09:00", title="Other", country="EUR"),
        ]
        with patch(
            "backend.src.adapters.economic_calendar._parse_feed_datetime",
            side_effect=_spy,
        ):
            result = _fetch_rows(_day(2026, 12, 1), rows)
        assert calls == [
            "2026-12-01T10:00:00-05:00",
            "2026-12-01T20:30:00+09:00",
        ]
        assert result.source_ok is True
        assert [ev.name for ev in result.events] == ["ISM"]
        assert result.events[0].datetime_jst.isoformat() == "2026-12-02T00:00:00+09:00"

    def test_source_not_ok_degrade_cause_is_calendar_source(self) -> None:
        result = CalendarResult(events=[], source_ok=False)
        skip = evaluate_indicator_day_skip(result)
        assert skip.skip is True
        assert skip.reason == "calendar_degraded"
        assert skip.calendar_degrade_cause == "calendar_source"
        assert skip.calendar_tz_key is None
        assert result.source_ok is False
