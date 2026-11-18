"""The pure functions of static/candlestick.js, run with Node.js (skipped without node)."""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest

from stock_chart.config import APP_ROOT

from .helpers import make_series

SCRIPT = APP_ROOT / "stock_chart" / "static" / "candlestick.js"
NODE = shutil.which("node")


def run_js(code: str):
    """Run *code* with ``F`` = the candlestick module; the code prints JSON."""
    prog = f"const F = require({json.dumps(str(SCRIPT))});\n{code}"
    out = subprocess.run([NODE, "-e", prog], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout)


@unittest.skipUnless(NODE, "node.js is not installed")
class ChartFunctionTests(unittest.TestCase):
    def test_scripts_parse(self):
        for name in ("candlestick.js", "app.js"):
            subprocess.run([NODE, "--check", str(SCRIPT.with_name(name))], check=True, timeout=60)

    def test_formatting_and_ticks(self):
        r = run_js("""console.log(JSON.stringify({
            p: [F.formatPrice(1234.5), F.formatPrice(92.2), F.formatPrice(0.05123), F.formatPrice(null)],
            v: [F.formatVolume(19632000), F.formatVolume(999), F.formatVolume(1500), F.formatVolume(2.5e9)],
            pct: [F.formatPercent(1.234), F.formatPercent(-0.5), F.formatPercent(null)],
            ticks: F.niceTicks(88.88, 156.0, 5), flat: F.niceTicks(5, 5, 4).ticks}));""")
        self.assertEqual(r["p"], ["1,234.50", "92.20", "0.0512", "-"])
        self.assertEqual(r["v"], ["19.6M", "999", "1.50K", "2.50B"])
        self.assertEqual(r["pct"], ["+1.23%", "-0.50%", "-"])
        self.assertEqual((r["ticks"]["step"], r["ticks"]["ticks"]), (20, [100, 120, 140]))
        self.assertTrue(r["flat"])

    def test_ranges_summary_and_averages(self):
        rows = [list(r[1:]) for r in make_series("AAA", 300)]
        r = run_js(f"""const rows = {json.dumps(rows)};
            console.log(JSON.stringify({{
              w1m: F.windowForRange(rows, "1M"), w6m: F.windowForRange(rows, "6M"), all: F.windowForRange(rows, "ALL"),
              ytd: F.windowForRange(rows, "YTD"), empty: F.windowForRange([], "1Y"),
              s: F.summarize(rows, 290, 300), sma: F.sma(rows, 3).slice(0, 4),
              merged: F.mergeRows(rows, 0, 5), month: F.addMonths("2025-03-31", -1)}}));""")
        last = rows[-1][0]
        self.assertEqual(r["all"], [0, 300])
        self.assertEqual(r["w1m"][1], 300)
        first_1m = rows[r["w1m"][0]][0]
        self.assertTrue(19 <= 300 - r["w1m"][0] <= 23, first_1m)            # about a month of trading days
        self.assertTrue(125 <= 300 - r["w6m"][0] <= 132)
        self.assertEqual(rows[r["ytd"][0]][0][:4], last[:4])
        self.assertEqual(rows[r["ytd"][0] - 1][0][:4], str(int(last[:4]) - 1))
        self.assertEqual(r["empty"], [0, 0])
        window = rows[290:300]
        s = r["s"]
        self.assertEqual((s["days"], s["close"], s["first"], s["last"]), (10, window[-1][4], window[0][0], window[-1][0]))
        self.assertAlmostEqual(s["change"], window[-1][4] - rows[289][4])
        self.assertEqual(s["high"], max(x[2] for x in window))
        self.assertEqual(s["low"], min(x[3] for x in window))
        self.assertEqual(s["volume"], sum(x[5] for x in window))
        self.assertEqual(r["sma"][:2], [None, None])
        self.assertAlmostEqual(r["sma"][2], sum(x[4] for x in rows[:3]) / 3)
        m = r["merged"]
        self.assertEqual(m, [rows[0][0], rows[0][1], max(x[2] for x in rows[:5]), min(x[3] for x in rows[:5]),
                             rows[4][4], sum(x[5] for x in rows[:5])])
        self.assertEqual(r["month"], "2025-02-28")

    def test_time_axis_labels(self):
        rows = [list(r[1:]) for r in make_series("AAA", 1300)]
        r = run_js(f"""const rows = {json.dumps(rows)};
            const t = (n, w) => F.timeLabels(rows, 0, n, w / n, 70).map(l => l.text);
            console.log(JSON.stringify({{y5: t(1300, 1200), y5n: t(1300, 400), y1: t(251, 1200), m1: t(21, 1200)}}));""")
        self.assertEqual(r["y5"][:3], ["2025", "Jul", "2026"])                       # 5 years wide: half-years
        self.assertTrue(all(x.isdigit() for x in r["y5n"]), r["y5n"])                # 5 years narrow: years
        self.assertIn("Feb", r["y1"])                                                # 1 year: months
        self.assertTrue(r["y1"][0].endswith(rows[0][0][:4]))                         # the first names the year
        self.assertGreaterEqual(len(r["m1"]), 4)                                     # 1 month: weeks
        self.assertTrue(r["m1"][0].startswith("Jan 2, 2025"), r["m1"])

    def test_weekly_and_monthly_labels(self):
        r = run_js("""console.log(JSON.stringify({
            weeks: ["2025-01-06", "2024-12-30", "2021-01-04", "2020-12-28", "2026-12-28"].map(F.isoWeek),
            wk: F.periodLabel(["2025-01-06", 1, 2, 0.5, 1.5, 10, "2025-01-06", "2025-01-10", 5, true], "weekly", true),
            wk1: F.periodLabel(["2025-01-13", 1, 2, 0.5, 1.5, 10, "2025-01-15", "2025-01-15", 1, null], "weekly", true),
            wkShort: F.periodLabel(["2025-01-06", 1, 2, 0.5, 1.5, 10], "weekly", false),
            month: F.periodLabel(["2025-03-01", 1, 2, 0.5, 1.5, 10, "2025-03-03", "2025-03-31", 21, true], "monthly", true),
            day: F.periodLabel(["2025-03-04", 1, 2, 0.5, 1.5, 10], "daily", true)}));""")
        self.assertEqual([(w["year"], w["week"]) for w in r["weeks"]],
                         [(2025, 2), (2025, 1), (2021, 1), (2020, 53), (2026, 53)])
        self.assertEqual(r["wk"], "Week 2, 2025 (Jan 6 – Jan 10)")
        self.assertEqual(r["wk1"], "Week 3, 2025 (Jan 15)")
        self.assertEqual(r["wkShort"], "Week 2, 2025")
        self.assertEqual((r["month"], r["day"]), ("Mar 2025", "2025-03-04"))

    def test_axis_labels_of_weekly_and_monthly_candles(self):
        rows = [list(r[1:]) for r in make_series("AAA", 1300)]
        r = run_js(f"""const rows = {json.dumps(rows)};
            const months = [], weeks = [];
            let m = null, w = null;
            for (const row of rows) {{                         // one candle per month / ISO week (its first day)
              if (row[0].slice(0, 7) !== m) {{ m = row[0].slice(0, 7); months.push([m + "-01", ...row.slice(1)]); }}
              const iso = F.isoWeek(row[0]), key = iso.year + "-" + iso.week;
              if (key !== w) {{ w = key; weeks.push(row); }}
            }}
            console.log(JSON.stringify({{
              m: F.timeLabels(months, 0, months.length, 1200 / months.length, 70, "monthly").map(l => l.text),
              w: F.timeLabels(weeks, 0, 12, 1200 / 12, 70, "weekly").map(l => l.text),
              nm: months.length}}));""")
        self.assertTrue(r["m"] and all(not any(ch.isdigit() for ch in t) or t.isdigit() for t in r["m"]), r["m"])
        self.assertIn("2026", r["m"])                                                 # years / month names only
        self.assertTrue(r["w"][0].startswith("Jan 2, 2025"), r["w"])                # wide weeks: labelled by date
        self.assertGreaterEqual(len(r["w"]), 6)


if __name__ == "__main__":
    unittest.main()
