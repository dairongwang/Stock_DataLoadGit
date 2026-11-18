"""
stock_chart - a standalone web dashboard with candlestick charts of the stock
data: one candle per trading day, ISO week or calendar month (open / high /
low / close) with the volume underneath.

It is a separate application from stock_dataload: its own package, config
(``chart.ini``), requirements, tests and process (``python -m stock_chart``,
port 8070).  The only thing it shares with the data pipeline is the **data
contract** - read-only access to the daily table of the SQLite database:

    Stock_Daily (Ticker TEXT, TradeDate TEXT 'YYYY-MM-DD', Market TEXT,
                 Open REAL, High REAL, Low REAL, Close REAL, Volume INTEGER)
    UNIQUE (Ticker, TradeDate)

and, optionally, the pipeline's weekly / monthly aggregates (Stock_Weekly,
Stock_Monthly); without them the weekly / monthly candles are aggregated from
the daily rows.

The only thing it WRITES are its own stock lists (watchlist, Selected list,
screen lists loaded from text files) in the tables Chart_List / Chart_List_Item
- of the same database by default, or of the file [lists] sqlite_path names.

Modules
-------
config      ChartSettings <- chart.ini + environment variables
repository  read-only queries (tickers, daily rows / weekly / monthly candles of one ticker, quotes)
lists       the stock lists saved in the database (watchlist, Selected, screen lists)
app         the Flask app: the chart page with the lists, the JSON / CSV API, /health, optional basic auth
"""

__version__ = "1.3.0"

from .config import ChartSettings, load_settings  # noqa: F401
