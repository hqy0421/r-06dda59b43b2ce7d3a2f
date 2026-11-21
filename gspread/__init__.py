"""Google Spreadsheets Python API"""

__version__ = "6.2.1"
__author__ = "Anton Burnashev"


from .auth import (
    api_key,
    authorize,
    oauth,
    oauth_from_dict,
    service_account,
    service_account_from_dict,
)
from .cell import Cell
from .client import Client
from .exceptions import (
    FilterViewError,
    FilterViewNotFound,
    GSpreadException,
    IncorrectCellLabel,
    NoValidUrlKeyFound,
    SpreadsheetNotFound,
    UnsupportedFilterViewRule,
    WorksheetNotFound,
)
from .filter_view import (
    ColumnFilterRule,
    FilterViewExcludedRow,
    FilterViewExclusion,
    FilterViewExport,
    FilterViewRow,
)
from .http_client import BackOffHTTPClient, HTTPClient
from .spreadsheet import Spreadsheet
from .worksheet import ValueRange, Worksheet

from . import urls as urls
from . import utils as utils

__all__ = [
    # from .auth
    "api_key",
    "authorize",
    "oauth",
    "oauth_from_dict",
    "service_account",
    "service_account_from_dict",

    # from .cell
    "Cell",

    # from .client
    "Client",

    # from .filter_view
    "ColumnFilterRule",
    "FilterViewExcludedRow",
    "FilterViewExclusion",
    "FilterViewExport",
    "FilterViewRow",

    # from .http_client
    "BackOffHTTPClient",
    "HTTPClient",

    # from .spreadsheet
    "Spreadsheet",

    # from .worksheet
    "Worksheet",
    "ValueRange",

    # from .exceptions
    "FilterViewError",
    "FilterViewNotFound",
    "GSpreadException",
    "IncorrectCellLabel",
    "NoValidUrlKeyFound",
    "SpreadsheetNotFound",
    "UnsupportedFilterViewRule",
    "WorksheetNotFound",

    # full module imports
    "urls",
    "utils",
]
