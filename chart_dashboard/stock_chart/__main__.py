"""``python -m stock_chart [--config chart.ini] [--host H] [--port P] [--debug]``"""

from .app import main

if __name__ == "__main__":
    raise SystemExit(main())
