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
           (promote_eod_daily: Stock_EOD_Stage -> daily table, adjusted prices)
trading_calendar  No_Trading_Day / Market tables: weekends, holidays and closures per market
symbols    Stock_Symbol table: market and first / last trade date per symbol (trading window)
file_formats  custom file format definitions (formats.json) + FormatStore
readers    csv / text / excel / json / xml readers
converter  convert files with a different layout/type into the default format
audit      App_Log / App_Error tables (activity + operational error tracking)
auth       user accounts, roles and password hashing (App_User table)
accounts   two-factor authentication (authenticator app / e-mail / sms), e-mail verification,
           password reset - shared by staff users and portal customers (Auth_* tables)
messaging  outbound e-mail / sms: console + memory stubs, SMTP, Twilio
customers  customers of the public portal, API keys with token balances, orders, API usage
payments   Stripe Checkout + PayPal Checkout, and the fake gateway (in-process stub of both APIs)
oauth      sign in with Google / Apple / Facebook, and the fake identity provider (stub)
httpclient the JSON-over-HTTP transport shared by the providers
dataapi    the data behind the public data API: daily / weekly / monthly rows per ticker and year
eod_api    download end-of-day prices from the EOD data API ([eod_api]) into a local folder
eod_stage  the staging table of the EOD API data (Stock_EOD_Stage): validate, type, upsert
eod_market a whole market (NYSE, ...): one API call per stock, straight into Stock_EOD_Stage, retries, resume
eod_bulk   one trading day of every stock of a market in one call (eod-bulk-last-day) -> Stock_EOD_Stage
bulk_schedule  daily schedules of the one-day bulk load (once per trading day at a set time)
load_limits  the daily limits: a stock 5 loads, a whole market 3 loads, a text file 100 symbols / 5 loads a day
eod_symbols  the staging table of the exchange symbol lists (Stock_Symbol_Stage)
correction data correction of staging rows (dashboard page) + Data_Correction table
quality    strategy-based data quality checks + Data_Error table
market_calendar  US market trading calendar (weekends + holidays)
cli        command line interface (``python -m stock_dataload``)
dashboard  Flask web dashboard for business users
portal     Flask public site: token shop, customer accounts and the token-metered data API
"""

__version__ = "1.5.0"

from .audit import AuditLogger                         # noqa: F401
from .auth import UserStore                            # noqa: F401
from .config import Settings, load_settings            # noqa: F401
from .converter import ConversionSpec, FileConverter   # noqa: F401
from .correction import CorrectionService              # noqa: F401
from .accounts import TwoFactorService                 # noqa: F401
from .customers import CustomerStore                   # noqa: F401
from .dataapi import DataApiService                    # noqa: F401
from .file_formats import FileFormat, FormatStore      # noqa: F401
from .db import StageDatabase                          # noqa: F401
from .loader import FolderLoadResult, LoadResult, StageLoader   # noqa: F401
from .promotion import PromotionResult, PromotionService       # noqa: F401
from .symbols import SymbolRegistry                            # noqa: F401
from .trading_calendar import TradingCalendar                  # noqa: F401
from .quality import (DataErrorRepository, DataQualityChecker,  # noqa: F401
                      QualityIssue, ValidationStrategy)
