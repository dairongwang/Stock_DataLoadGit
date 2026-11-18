"""WSGI entry point for production servers, e.g.

    waitress-serve --listen=127.0.0.1:8070 wsgi:app
    gunicorn -b 127.0.0.1:8070 -w 2 wsgi:app

Configuration: chart.ini next to this file, or $STOCK_CHART_CONFIG / the STOCK_CHART_* variables.
"""

from stock_chart.app import create_app, setup_logging
from stock_chart.config import load_settings

settings = load_settings()
setup_logging(settings)
app = create_app(settings)
