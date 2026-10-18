"""
stock_dataload - load stock trading data files into a database staging table.

Python port of the SSIS package ``Stock_DataLoad/LoadStage.dtsx``.

Modules
-------
formats    default file format definition + header alias table
config     settings (config.ini)
db         staging table access (sqlite)
loader     load files from the loading folder / bulk load a folder into the staging table
promotion  active tables (daily / weekly / monthly) + the promotion procedures
trading_calendar  No_Trading_Day / Market tables: weekends, holidays and closures per market
symbols    Stock_Symbol table: market and first / last trade date per symbol (trading window)
file_formats  custom file format definitions (formats.json) + FormatStore
readers    csv / text / excel / json / xml readers
converter  convert files with a different layout/type into the default format
audit      App_Log / App_Error tables (activity + operational error tracking)
auth       user accounts, roles and password hashing (App_User table)
correction data correction of staging rows (dashboard page) + Data_Correction table
quality    strategy-based data quality checks + Data_Error table
market_calendar  US market trading calendar (weekends + holidays)
cli        command line interface (``python -m stock_dataload``)
dashboard  Flask web dashboard for business users
"""

__version__ = "1.4.0"

from .audit import AuditLogger                         # noqa: F401
from .auth import UserStore                            # noqa: F401
from .config import Settings, load_settings            # noqa: F401
from .converter import ConversionSpec, FileConverter   # noqa: F401
from .correction import CorrectionService              # noqa: F401
from .file_formats import FileFormat, FormatStore      # noqa: F401
from .db import StageDatabase                          # noqa: F401
from .loader import FolderLoadResult, LoadResult, StageLoader   # noqa: F401
from .promotion import PromotionResult, PromotionService       # noqa: F401
from .symbols import SymbolRegistry                            # noqa: F401
from .trading_calendar import TradingCalendar                  # noqa: F401
from .quality import (DataErrorRepository, DataQualityChecker,  # noqa: F401
                      QualityIssue, ValidationStrategy)
