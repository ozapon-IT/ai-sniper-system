"""休場日カレンダーのユニットテスト（§3.5 / §7.1）。"""

from datetime import date
from pathlib import Path

from config.market_calendar import _load_holiday_sets, is_market_holiday, is_us_thin_market


class TestIsMarketHoliday:
    def test_new_year_2026_is_holiday(self) -> None:
        """2026-01-01 は元日なので True を返す（受け入れ条件）。"""
        assert is_market_holiday(date(2026, 1, 1)) is True

    def test_christmas_2026_is_holiday(self) -> None:
        """クリスマスは FX 実質休場日。"""
        assert is_market_holiday(date(2026, 12, 25)) is True

    def test_good_friday_2026_is_holiday(self) -> None:
        """Good Friday は FX 主要市場が休場。"""
        assert is_market_holiday(date(2026, 4, 3)) is True

    def test_normal_weekday_is_not_holiday(self) -> None:
        """通常平日は False を返す（受け入れ条件）。"""
        assert is_market_holiday(date(2026, 6, 15)) is False

    def test_saturday_not_in_list_returns_false(self) -> None:
        """土曜日は JSON に含まれなければ False（週末管理は別途）。"""
        assert is_market_holiday(date(2026, 1, 3)) is False

    def test_return_type_is_bool(self) -> None:
        """戻り値が必ず bool であることを確認。"""
        assert isinstance(is_market_holiday(date(2026, 1, 1)), bool)
        assert isinstance(is_market_holiday(date(2026, 6, 15)), bool)

    def test_us_thin_days_are_not_full_close(self) -> None:
        """US 単独祝日は full_close ではなく is_market_holiday=False になること（§3.5）。"""
        us_only = [
            date(2026, 1, 19),  # MLK Day
            date(2026, 2, 16),  # Presidents Day
            date(2026, 5, 25),  # Memorial Day
            date(2026, 7, 3),   # Independence Day observed
            date(2026, 9, 7),   # Labor Day
            date(2026, 11, 26), # Thanksgiving
        ]
        for d in us_only:
            assert is_market_holiday(d) is False, f"{d} should not be full_close"


class TestIsUsThinMarket:
    def test_mlk_day_is_us_thin(self) -> None:
        """MLK Day は US 薄商い日。"""
        assert is_us_thin_market(date(2026, 1, 19)) is True

    def test_presidents_day_is_us_thin(self) -> None:
        assert is_us_thin_market(date(2026, 2, 16)) is True

    def test_memorial_day_is_us_thin(self) -> None:
        assert is_us_thin_market(date(2026, 5, 25)) is True

    def test_independence_day_observed_is_us_thin(self) -> None:
        assert is_us_thin_market(date(2026, 7, 3)) is True

    def test_labor_day_is_us_thin(self) -> None:
        assert is_us_thin_market(date(2026, 9, 7)) is True

    def test_thanksgiving_is_us_thin(self) -> None:
        assert is_us_thin_market(date(2026, 11, 26)) is True

    def test_normal_weekday_is_not_us_thin(self) -> None:
        """通常平日は False。"""
        assert is_us_thin_market(date(2026, 6, 15)) is False

    def test_christmas_is_not_us_thin(self) -> None:
        """クリスマスは full_close であり us_thin ではない。"""
        assert is_us_thin_market(date(2026, 12, 25)) is False

    def test_return_type_is_bool(self) -> None:
        assert isinstance(is_us_thin_market(date(2026, 1, 19)), bool)
        assert isinstance(is_us_thin_market(date(2026, 6, 15)), bool)

    def test_us_thin_and_full_close_are_disjoint(self) -> None:
        """full_close と us_thin が重複しないこと。"""
        full_close, us_thin = _load_holiday_sets()
        assert full_close.isdisjoint(us_thin), "full_close と us_thin に重複があります"


class TestLoadHolidaySets:
    def test_loads_nonempty_sets(self) -> None:
        """JSON から full_close/us_thin ともに 1 件以上読み込めること。"""
        full_close, us_thin = _load_holiday_sets()
        assert len(full_close) > 0
        assert len(us_thin) > 0

    def test_all_entries_are_date_objects(self) -> None:
        """全エントリが datetime.date であること。"""
        full_close, us_thin = _load_holiday_sets()
        for h in full_close | us_thin:
            assert isinstance(h, date)

    def test_new_year_2026_in_full_close(self) -> None:
        """2026-01-01 が full_close に含まれること。"""
        full_close, _ = _load_holiday_sets()
        assert date(2026, 1, 1) in full_close

    def test_mlk_day_in_us_thin(self) -> None:
        """MLK Day が us_thin に含まれること。"""
        _, us_thin = _load_holiday_sets()
        assert date(2026, 1, 19) in us_thin

    def test_2027_mlk_day_in_us_thin(self) -> None:
        """2027 年 MLK Day が us_thin に含まれること（2027 年カバレッジ）。"""
        _, us_thin = _load_holiday_sets()
        assert date(2027, 1, 18) in us_thin

    def test_2027_christmas_eve_not_in_full_close(self) -> None:
        """2027-12-24（early close）は full_close ではないこと。"""
        full_close, _ = _load_holiday_sets()
        assert date(2027, 12, 24) not in full_close

    def test_2027_christmas_eve_in_us_thin(self) -> None:
        """2027-12-24（early close）は us_thin に含まれること。"""
        _, us_thin = _load_holiday_sets()
        assert date(2027, 12, 24) in us_thin

    def test_custom_json_path(self, tmp_path: Path) -> None:
        """カスタムパスの JSON を読み込めること（両カテゴリ対応）。"""
        json_file = tmp_path / "holidays.json"
        json_file.write_text(
            '{"full_close": [{"date": "2030-01-01", "name": "test"}],'
            ' "us_thin": [{"date": "2030-01-20", "name": "mlk"}]}',
            encoding="utf-8",
        )
        full_close, us_thin = _load_holiday_sets(json_file)
        assert date(2030, 1, 1) in full_close
        assert date(2030, 1, 20) in us_thin

    def test_missing_key_returns_empty_set(self, tmp_path: Path) -> None:
        """us_thin キーがない旧形式でも full_close は読める。"""
        json_file = tmp_path / "holidays.json"
        json_file.write_text(
            '{"full_close": [{"date": "2030-01-01", "name": "test"}]}',
            encoding="utf-8",
        )
        full_close, us_thin = _load_holiday_sets(json_file)
        assert date(2030, 1, 1) in full_close
        assert len(us_thin) == 0
