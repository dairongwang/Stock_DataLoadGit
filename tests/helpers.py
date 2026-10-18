"""Shared helpers for the test-suite: isolated folders + sqlite db per test."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from stock_dataload.config import Settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLES = PROJECT_ROOT / "samples"
ORIGINAL = SAMPLES / "NASDAQ_20250123.txt"
REORDERED = SAMPLES / "NASDAQ_20250123_reordered.txt"
BROKER = SAMPLES / "NASDAQ_20250123_broker.csv"

DEFAULT_HEADER = "<ticker>,<date>,<open>,<high>,<low>,<close>,<vol>\n"


def make_settings(root: Path) -> Settings:
    s = Settings()
    s.base_dir = root
    s.conversion_dir = root / "conversion"
    s.converted_dir = root / "converted"
    s.loading_dir = root / "loading"
    s.archive_dir = root / "archive"
    s.error_dir = root / "error"
    s.skipped_dir = root / "skipped"
    s.work_dir = root / "work"
    s.log_dir = root / "logs"
    s.sqlite_path = root / "test.db"
    s.secret_key_file = root / ".secret_key"
    s.batch_size = 1000
    s.formats_file = root / "formats.json"
    shipped = PROJECT_ROOT / "formats.json"
    if shipped.exists():
        # only the shipped example formats - user-created ones must not leak into tests
        from stock_dataload.file_formats import FormatStore
        store = FormatStore(shipped)
        isolated = FormatStore(s.formats_file)
        isolated._formats = {f.name: f for f in store.all() if f.builtin}
        isolated.save()
    s.ensure_folders()
    return s


def register_listed(settings: Settings, market: str, tickers, first_trade_date: str = "2020-01-02") -> None:
    """Give the tickers a first trade date in Stock_Symbol - only then are they
    checked for missing market days."""
    from stock_dataload.symbols import SymbolRegistry
    registry = SymbolRegistry(settings)
    for ticker in tickers:
        registry.set(ticker, market, first_trade_date=first_trade_date)


class TempProjectTestCase(unittest.TestCase):
    """Every test gets a fresh temp folder structure and sqlite database."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="stock_dataload_test_"))
        self.settings = make_settings(self.root)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, relpath: str, text: str) -> Path:
        p = self.root / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="")
        return p
