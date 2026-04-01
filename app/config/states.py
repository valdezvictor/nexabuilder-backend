"""
Config-driven parser registry.
Toggle between 'bulk' and 'live' parsers per state.
"""

from app.parsers.az_licensing import AzParser
from app.parsers.bulk_parser import BulkCSVParser, BulkExcelParser
from app.parsers.ca_licensing import CaParser
from app.parsers.la_licensing import LaParser
from app.parsers.nv_licensing import NvParser
from app.parsers.ut_licensing import UtParser, UtParserFail

STATE_PARSER_CONFIG = {
    "CA": {
        "mode": "live",
        "live": CaParser,
    },
    "UT": {
        "mode": "bulk",  # options: "bulk" or "live"
        "bulk_csv": lambda: BulkCSVParser("data/utah_contractors.csv"),
        "bulk_excel": lambda: BulkExcelParser("data/utah_contractors.xlsx"),
        "live": UtParser,
        "fail": UtParserFail,
    },
    "AZ": {
        "mode": "live",
        "live": AzParser,
    },
    "LA": {
        "mode": "live",
        "live": LaParser,
    },
    "NV": {
        "mode": "live",
        "live": NvParser,
    },
}
