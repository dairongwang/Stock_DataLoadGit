"""Unit tests for stock_dataload.quality (strategy pattern + Data_Error table)."""

import sqlite3
import unittest
from datetime import date

from stock_dataload.quality import (DATA_ERROR_TABLE, DataErrorRepository, DataQualityChecker,
                                    DataRow, DateIsValidStrategy, DatasetValidationStrategy,
                                    HighAboveLowStrategy, MissingMarketDayStrategy,
                                    NumericColumnsStrategy, QualityContext, QualityIssue,
                                    RowValidationStrategy, SEVERITY_ALERT, SEVERITY_ERROR,
                                    default_strategies)

from .helpers import TempProjectTestCase


def row(ticker="AAA", d="20250110", open_="1", high="2", low="0.5", close="1.5", vol="100",
        line_no=2):
    return DataRow(line_no, {"ticker": ticker, "date": d, "open": open_, "high": high,
                             "low": low, "close": close, "vol": vol})


class DataRowTests(unittest.TestCase):
    def test_from_sequence(self):
        r = DataRow.from_sequence(5, [" AAA ", "20250110", "1", "2", "0.5", "1.5", "100"])
        self.assertEqual(r.line_no, 5)
        self.assertEqual(r.ticker, "AAA")
        self.assertEqual(r.trade_date, "20250110")
        self.assertEqual(r.values["vol"], "100")

    def test_from_sequence_short_row_pads_empty(self):
        r = DataRow.from_sequence(3, ["AAA", "20250110"])
        self.assertEqual(r.values["open"], "")
        self.assertEqual(r.values["vol"], "")


class QualityIssueTests(unittest.TestCase):
    def test_alert_property_and_str(self):
        err = QualityIssue("r1", "broken", ticker="AAA", line_no=7)
        alert = QualityIssue("r2", "missing", severity=SEVERITY_ALERT)
        self.assertFalse(err.alert)
        self.assertTrue(alert.alert)
        self.assertIn("[r1]", str(err))
        self.assertIn("AAA", str(err))
        self.assertIn("line 7", str(err))


class DateIsValidStrategyTests(unittest.TestCase):
    def setUp(self):
        self.strategy = DateIsValidStrategy()
        self.ctx = QualityContext()

    def test_valid_date_passes(self):
        self.assertEqual(self.strategy.validate_row(row(), self.ctx), [])

    def test_bad_dates_flagged(self):
        for bad in ("2025-01-10", "20251399", "20250230", "notadate", "", "202501"):
            issues = self.strategy.validate_row(row(d=bad), self.ctx)
            self.assertEqual(len(issues), 1, bad)
            issue = issues[0]
            self.assertEqual(issue.rule, "date_is_valid")
            self.assertEqual(issue.severity, SEVERITY_ERROR)
            self.assertEqual(issue.column, "date")
            self.assertEqual(issue.value, bad)
            self.assertEqual(issue.line_no, 2)


class NumericColumnsStrategyTests(unittest.TestCase):
    def setUp(self):
        self.strategy = NumericColumnsStrategy()
        self.ctx = QualityContext()

    def test_numeric_row_passes(self):
        self.assertEqual(self.strategy.validate_row(row(), self.ctx), [])
        self.assertEqual(self.strategy.validate_row(row(open_="-1.5e3", vol="2E6"), self.ctx), [])

    def test_each_non_numeric_column_flagged(self):
        for col in ("open", "high", "low", "close", "vol"):
            r = row(**{("open_" if col == "open" else col): "abc"})
            issues = self.strategy.validate_row(r, self.ctx)
            self.assertEqual(len(issues), 1, col)
            self.assertEqual(issues[0].column, col)
            self.assertIn("not a number", issues[0].message)

    def test_missing_value_flagged(self):
        issues = self.strategy.validate_row(row(close=""), self.ctx)
        self.assertEqual(len(issues), 1)
        self.assertIn("missing", issues[0].message)

    def test_multiple_bad_columns_all_reported(self):
        issues = self.strategy.validate_row(row(open_="x", vol=""), self.ctx)
        self.assertEqual({i.column for i in issues}, {"open", "vol"})


class HighAboveLowStrategyTests(unittest.TestCase):
    def setUp(self):
        self.strategy = HighAboveLowStrategy()
        self.ctx = QualityContext()

    def test_high_above_low_passes(self):
        self.assertEqual(self.strategy.validate_row(row(high="2", low="0.5"), self.ctx), [])

    def test_high_equal_low_passes(self):
        # illiquid stocks legitimately trade flat - only inverted values are wrong
        self.assertEqual(self.strategy.validate_row(row(high="1", low="1"), self.ctx), [])

    def test_high_below_low_flagged(self):
        issues = self.strategy.validate_row(row(high="0.4", low="0.5"), self.ctx)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].rule, "high_above_low")
        self.assertIn("smaller than low", issues[0].message)

    def test_non_numeric_values_left_to_numeric_strategy(self):
        self.assertEqual(self.strategy.validate_row(row(high="x", low="1"), self.ctx), [])
        self.assertEqual(self.strategy.validate_row(row(high="2", low=""), self.ctx), [])


class MissingMarketDayStrategyTests(unittest.TestCase):
    def setUp(self):
        self.strategy = MissingMarketDayStrategy()

    def ctx(self, expected=("AAA", "BBB"), **kw):
        return QualityContext(market="NASDAQ", source_file="NASDAQ_20250110.txt",
                              expected_tickers=frozenset(expected), **kw)

    def test_no_expected_tickers_means_no_alerts(self):
        issues = self.strategy.validate_dataset([row()], QualityContext())
        self.assertEqual(issues, [])

    def test_missing_ticker_on_open_day_is_alerted(self):
        issues = self.strategy.validate_dataset([row("AAA", "20250110")], self.ctx())
        self.assertEqual(len(issues), 1)
        issue = issues[0]
        self.assertEqual(issue.rule, "missing_market_day")
        self.assertEqual(issue.severity, SEVERITY_ALERT)
        self.assertTrue(issue.alert)
        self.assertEqual(issue.ticker, "BBB")
        self.assertEqual(issue.trade_date, "20250110")
        self.assertIn("market was open", issue.message)

    def test_all_tickers_covered_no_alert(self):
        rows = [row("AAA", "20250110"), row("BBB", "20250110")]
        self.assertEqual(self.strategy.validate_dataset(rows, self.ctx()), [])

    def test_weekend_day_is_not_alerted(self):
        rows = [row("AAA", "20250111")]   # Saturday
        self.assertEqual(self.strategy.validate_dataset(rows, self.ctx()), [])

    def test_holiday_is_not_alerted(self):
        rows = [row("AAA", "20250101")]   # New Year's Day
        self.assertEqual(self.strategy.validate_dataset(rows, self.ctx()), [])

    def test_extra_holiday_is_not_alerted(self):
        rows = [row("AAA", "20250110")]
        ctx = self.ctx(extra_holidays=frozenset({date(2025, 1, 10)}))
        self.assertEqual(self.strategy.validate_dataset(rows, ctx), [])

    def test_ticker_with_only_invalid_rows_counts_as_missing(self):
        rows = [row("AAA", "20250110"), row("BBB", "20250110", high="x")]
        ctx = self.ctx()
        ctx.invalid_row_keys.add(("BBB", "20250110"))
        issues = self.strategy.validate_dataset(rows, ctx)
        self.assertEqual([i.ticker for i in issues], ["BBB"])

    def test_multiple_days_checked_independently(self):
        rows = [row("AAA", "20250109"), row("BBB", "20250109"), row("AAA", "20250110")]
        issues = self.strategy.validate_dataset(rows, self.ctx())
        self.assertEqual([(i.ticker, i.trade_date) for i in issues], [("BBB", "20250110")])

    def test_custom_market_open_callable(self):
        ctx = self.ctx(market_open=lambda d: False)     # market never open
        self.assertEqual(self.strategy.validate_dataset([row("AAA", "20250110")], ctx), [])


class DataQualityCheckerTests(unittest.TestCase):
    def test_default_strategy_set(self):
        checker = DataQualityChecker()
        self.assertEqual(checker.strategy_names(),
                         ["date_is_valid", "numeric_columns", "high_above_low", "before_first_trade_date",
                          "after_last_trade_date", "missing_market_day"])
        from stock_dataload.quality import AfterLastTradeDateStrategy, BeforeFirstTradeDateStrategy
        self.assertEqual([type(s) for s in default_strategies()],
                         [DateIsValidStrategy, NumericColumnsStrategy, HighAboveLowStrategy,
                          BeforeFirstTradeDateStrategy, AfterLastTradeDateStrategy, MissingMarketDayStrategy])

    def test_add_and_remove_strategy(self):
        class VolumeCapStrategy(RowValidationStrategy):
            name = "volume_cap"

            def validate_row(self, r, ctx):
                if float(r.values["vol"] or 0) > 1000:
                    return [QualityIssue(self.name, "volume too big", line_no=r.line_no)]
                return []

        checker = DataQualityChecker()
        checker.add_strategy(VolumeCapStrategy())
        self.assertIn("volume_cap", checker.strategy_names())
        issues = checker.check([row(vol="5000")])
        self.assertIn("volume_cap", {i.rule for i in issues})
        self.assertTrue(checker.remove_strategy("volume_cap"))
        self.assertNotIn("volume_cap", checker.strategy_names())
        self.assertFalse(checker.remove_strategy("volume_cap"))    # already gone

    def test_add_strategy_validates_input(self):
        checker = DataQualityChecker()
        with self.assertRaises(TypeError):
            checker.add_strategy(object())
        class Nameless(RowValidationStrategy):
            name = ""
            def validate_row(self, r, ctx):  # pragma: no cover
                return []
        with self.assertRaises(ValueError):
            checker.add_strategy(Nameless())
        with self.assertRaises(ValueError):
            checker.add_strategy(DateIsValidStrategy())     # duplicate name

    def test_check_runs_row_rules_then_dataset_rules(self):
        checker = DataQualityChecker()
        ctx = QualityContext(expected_tickers=frozenset({"AAA", "BBB"}))
        rows = [row("AAA", "20250110"), row("BBB", "20250110", high="oops")]
        issues = checker.check(rows, ctx)
        rules = [i.rule for i in issues]
        self.assertIn("numeric_columns", rules)
        # BBB's broken row marked invalid -> missing market day alert for BBB
        self.assertIn(("BBB", "20250110"), ctx.invalid_row_keys)
        self.assertIn("missing_market_day", rules)
        alerts = [i for i in issues if i.alert]
        self.assertEqual([a.ticker for a in alerts], ["BBB"])

    def test_check_with_custom_strategy_list(self):
        checker = DataQualityChecker(strategies=[HighAboveLowStrategy()])
        issues = checker.check([row(d="garbage", high="1", low="2")])
        self.assertEqual([i.rule for i in issues], ["high_above_low"])

    def test_check_clean_rows_no_issues(self):
        checker = DataQualityChecker()
        self.assertEqual(checker.check([row(), row("BBB")]), [])

    def test_dataset_strategy_subclass_contract(self):
        class WholeFileRule(DatasetValidationStrategy):
            name = "whole_file"
            def validate_dataset(self, rows, ctx):
                return [QualityIssue(self.name, f"{len(rows)} rows seen")]
        checker = DataQualityChecker(strategies=[WholeFileRule()])
        issues = checker.check([row(), row("BBB")])
        self.assertEqual(issues[0].message, "2 rows seen")


class QualityContextTests(unittest.TestCase):
    def test_default_calendar_used(self):
        ctx = QualityContext()
        self.assertTrue(ctx.is_market_open(date(2025, 1, 10)))
        self.assertFalse(ctx.is_market_open(date(2025, 1, 11)))

    def test_extra_holidays_respected(self):
        ctx = QualityContext(extra_holidays=frozenset({date(2025, 1, 10)}))
        self.assertFalse(ctx.is_market_open(date(2025, 1, 10)))

    def test_custom_callable_wins(self):
        ctx = QualityContext(market_open=lambda d: True)
        self.assertTrue(ctx.is_market_open(date(2025, 1, 11)))  # Saturday, but custom says open


class DataErrorRepositoryTests(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.repo = DataErrorRepository(self.settings)

    def test_ensure_table(self):
        self.repo.ensure_table()
        with sqlite3.connect(self.settings.sqlite_path) as conn:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn(DATA_ERROR_TABLE, names)

    def test_record_and_fetch(self):
        issues = [
            QualityIssue("high_above_low", "high < low", ticker="AAA", trade_date="20250110",
                         column="high", value="high=1,low=2", line_no=3),
            QualityIssue("missing_market_day", "no data", severity=SEVERITY_ALERT, ticker="BBB",
                         trade_date="20250110"),
        ]
        written = self.repo.record(issues, source_file="f.txt", load_id="lid", market="NASDAQ")
        self.assertEqual(written, 2)
        records = self.repo.fetch()
        self.assertEqual(len(records), 2)
        alert, err = records[0], records[1]                  # newest first
        self.assertEqual(err.rule, "high_above_low")
        self.assertEqual(err.severity, "error")
        self.assertEqual(err.alert, 0)
        self.assertEqual(err.ticker, "AAA")
        self.assertEqual(err.column_name, "high")
        self.assertEqual(err.line_no, 3)
        self.assertEqual(err.source_file, "f.txt")
        self.assertEqual(err.load_id, "lid")
        self.assertEqual(err.market, "NASDAQ")
        self.assertEqual(alert.alert, 1)
        self.assertEqual(alert.severity, "alert")

    def test_record_empty_list_is_noop(self):
        self.assertEqual(self.repo.record([]), 0)
        self.assertEqual(self.repo.count(), 0)

    def test_fetch_filters(self):
        self.repo.record([QualityIssue("r", "m", ticker="A")], source_file="a.txt")
        self.repo.record([QualityIssue("r", "m", severity=SEVERITY_ALERT, ticker="B")],
                         source_file="b.txt")
        self.assertEqual(len(self.repo.fetch(source_file="a.txt")), 1)
        self.assertEqual(len(self.repo.fetch(alerts_only=True)), 1)
        self.assertEqual(self.repo.fetch(alerts_only=True)[0].ticker, "B")
        self.assertEqual(len(self.repo.fetch(limit=1)), 1)

    def test_count_filters(self):
        self.repo.record([QualityIssue("r", "m"), QualityIssue("r", "m", severity=SEVERITY_ALERT)],
                         source_file="a.txt")
        self.assertEqual(self.repo.count(), 2)
        self.assertEqual(self.repo.count(source_file="a.txt"), 2)
        self.assertEqual(self.repo.count(source_file="zzz.txt"), 0)
        self.assertEqual(self.repo.count(alerts_only=True), 1)
        self.assertEqual(self.repo.count(source_file="a.txt", alerts_only=True), 1)


if __name__ == "__main__":
    unittest.main()
