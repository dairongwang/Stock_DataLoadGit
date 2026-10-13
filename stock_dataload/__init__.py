"""
stock_dataload - load stock trading data files into a database staging table.

Python port of the SSIS package ``Stock_DataLoad/LoadStage.dtsx``.

Modules
-------
formats    default file format definition + header alias table
config     settings (config.ini)
db         staging table access (sqlite)
loader     load files from the loading folder into the staging table
file_formats  custom file format definitions (formats.json) + FormatStore
readers    csv / text / excel / json / xml readers
converter  convert files with a different layout/type into the default format
cli        command line interface (``python -m stock_dataload``)
dashboard  Flask web dashboard for business users
"""

__version__ = "1.0.0"

from .config import Settings, load_settings            # noqa: F401
from .converter import ConversionSpec, FileConverter   # noqa: F401
from .file_formats import FileFormat, FormatStore      # noqa: F401
from .db import StageDatabase                          # noqa: F401
from .loader import StageLoader                        # noqa: F401
