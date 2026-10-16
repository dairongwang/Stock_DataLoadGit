"""Unit tests for stock_dataload.market_calendar."""

import unittest
from datetime import date

from stock_dataload.market_calendar import (CalendarError, easter_sunday, is_market_open,
                                            parse_holiday, us_market_holidays)


class EasterTests(unittest.TestCase):
    def test_known_easter_sundays(self):
        self.assertEqual(easter_sunday(2024), date(2024, 3, 31))
        self.assertEqual(easter_sunday(2025), date(2025, 4, 20))
        self.assertEqual(easter_sunday(2026), date(2026, 4, 5))
        self.assertEqual(easter_sunday(2000), date(2000, 4, 23))


class HolidayTests(unittest.TestCase):
    def test_2025_full_holiday_calendar(self):
        expected = {
            date(2025, 1, 1),    # New Year's Day
            date(2025, 1, 20),   # Martin Luther King Jr. Day
            date(2025, 2, 17),   # Washington's Birthday
            date(2025, 4, 18),   # Good Friday
            date(2025, 5, 26),   # Memorial Day
            date(2025, 6, 19),   # Juneteenth
            date(2025, 7, 4),    # Independence Day
            date(2025, 9, 1),    # Labor Day
            date(2025, 11, 27),  # Thanksgiving
            date(2025, 12, 25),  # Christmas
        }
        self.assertEqual(set(us_market_holidays(2025)), expected)

    def test_saturday_holiday_observed_on_friday(self):
        # 4 July 2026 is a Saturday -> observed Friday 3 July
        self.assertIn(date(2026, 7, 3), us_market_holidays(2026))
        self.assertNotIn(date(2026, 7, 4), us_market_holidays(2026))

    def test_sunday_holiday_observed_on_monday(self):
        # 25 Dec 2022 was a Sunday -> observed Monday 26 Dec
        self.assertIn(date(2022, 12, 26), us_market_holidays(2022))

    def test_new_year_on_saturday_not_observed(self):
        # 1 Jan 2022 was a Saturday; NYSE did NOT close on 31 Dec 2021
        self.assertNotIn(date(2022, 1, 1), us_market_holidays(2022))
        self.assertNotIn(date(2021, 12, 31), us_market_holidays(2021))

    def test_juneteenth_only_from_2022(self):
        self.assertNotIn(date(2021, 6, 18), us_market_holidays(2021))  # Fri (would be observed)
        self.assertIn(date(2023, 6, 19), us_market_holidays(2023))


class IsMarketOpenTests(unittest.TestCase):
    def test_regular_weekday_is_open(self):
        self.assertTrue(is_market_open(date(2025, 1, 10)))   # Friday
        self.assertTrue(is_market_open(date(2025, 1, 23)))   # Thursday

    def test_weekend_is_closed(self):
        self.assertFalse(is_market_open(date(2025, 1, 11)))  # Saturday
        self.assertFalse(is_market_open(date(2025, 1, 12)))  # Sunday

    def test_holiday_is_closed(self):
        self.assertFalse(is_market_open(date(2025, 1, 1)))
        self.assertFalse(is_market_open(date(2025, 11, 27)))
        self.assertFalse(is_market_open(date(2025, 4, 18)))  # Good Friday

    def test_extra_holidays_respected(self):
        mourning = date(2025, 1, 9)  # ad-hoc closure (e.g. day of mourning)
        self.assertTrue(is_market_open(mourning))
        self.assertFalse(is_market_open(mourning, extra_holidays={mourning}))


class ParseHolidayTests(unittest.TestCase):
    def test_accepted_formats(self):
        self.assertEqual(parse_holiday("2025-01-09"), date(2025, 1, 9))
        self.assertEqual(parse_holiday("20250109"), date(2025, 1, 9))
        self.assertEqual(parse_holiday("2025/01/09"), date(2025, 1, 9))
        self.assertEqual(parse_holiday(" 2025-01-09 "), date(2025, 1, 9))

    def test_date_passes_through(self):
        self.assertEqual(parse_holiday(date(2025, 1, 9)), date(2025, 1, 9))

    def test_invalid_raises(self):
        with self.assertRaises(CalendarError):
            parse_holiday("Jan 9th")
        with self.assertRaises(CalendarError):
            parse_holiday("")


if __name__ == "__main__":
    unittest.main()
