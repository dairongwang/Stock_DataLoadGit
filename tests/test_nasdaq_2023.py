"""
The real NASDAQ deliveries of 2023 (one default-format file per trading day, ``NASDAQ_2023MMDD.txt``):
bulk load the whole folder into an isolated database, promote it, and check that nothing failed.

The folder is not part of the repository (it is large). The tests look for it in
``$STOCK_DATALOAD_NASDAQ_2023`` or ``../NASDAQ_2023`` next to the project, and are skipped when it
is not there.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from stock_dataload.loader import StageLoader
from stock_dataload.promotion import PromotionService

from .helpers import PROJECT_ROOT, TempProjectTestCase


def _folder() -> Path | None:
    for candidate in (os.environ.get("STOCK_DATALOAD_NASDAQ_2023"), PROJECT_ROOT.parent / "NASDAQ_2023"):
        if candidate and Path(candidate).is_dir() and any(Path(candidate).glob("NASDAQ_2023*.txt")):
            return Path(candidate)
    return None


FOLDER = _folder()


@unittest.skipIf(FOLDER is None, "the NASDAQ_2023 deliveries are not available ($STOCK_DATALOAD_NASDAQ_2023 or "
                                 "../NASDAQ_2023)")
class Nasdaq2023Tests(TempProjectTestCase):
    def test_the_year_loads_and_promotes(self):
        self.settings.skip_bad_rows = True
        result = StageLoader(self.settings).load_folder(FOLDER, patterns=["NASDAQ_2023*.txt"])
        self.assertEqual(result.failed, 0, [r for r in result.results if r.failed][:3])
        self.assertGreater(result.loaded, int(os.environ.get("STOCK_DATALOAD_NASDAQ_2023_MIN_DAYS", 200)))  # ~250 days
        self.assertEqual(result.loaded + result.skipped, len(result.files))   # skipped: dated on a market holiday
        promoted = PromotionService(self.settings).promote_daily(full=True)
        self.assertGreater(promoted.rows_inserted, result.rows_loaded * 0.9)


if __name__ == "__main__":
    unittest.main()
