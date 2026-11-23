"""JST 単位明示ヘルパー（#343 / #345）。"""

from __future__ import annotations

from backend.src.engine.trading_day import jst_dated_time_label, jst_month_day, jst_time_label


def test_appends_jst_to_clock() -> None:
    assert jst_time_label("23:30") == "23:30 JST"
    assert jst_time_label("9:05") == "9:05 JST"


def test_empty_stays_empty() -> None:
    """無い情報を作らない（空時刻に JST を付けない）。"""
    assert jst_time_label("") == ""


def test_month_day_drops_zero_padding() -> None:
    """現在時刻行と日付付きラベルが同じ月日を使う（#376）。"""
    assert jst_month_day("2026-12-10") == "12/10"
    assert jst_month_day("2026-08-03") == "8/3"
    assert jst_month_day(" 2026-12-09 ") == "12/9"


def test_month_day_empty_when_unreadable() -> None:
    assert jst_month_day("") == ""
    assert jst_month_day("   ") == ""
    assert jst_month_day("not-a-date") == ""


def test_dated_label_drops_zero_padding() -> None:
    """ノート用。月日はゼロ埋めしない（#374）。"""
    assert jst_dated_time_label("2026-12-10", "04:00") == "12/10 04:00 JST"
    assert jst_dated_time_label("2026-08-03", "23:00") == "8/3 23:00 JST"


def test_dated_label_without_date_matches_clock_label() -> None:
    """date_jst が無い過去ログは HH:MM JST のまま。"""
    assert jst_dated_time_label("", "23:30") == "23:30 JST"
    assert jst_dated_time_label("not-a-date", "23:30") == "23:30 JST"
    assert jst_dated_time_label("2026-12-10", "") == ""
