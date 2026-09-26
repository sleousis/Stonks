"""Static HTML reporting (roadmap 4.1), behind ``stonks report``."""

from stonks.reporting.data import ReportData, build_report
from stonks.reporting.render import render_html
from stonks.reporting.tearsheet import TearSheet, render_tear_sheet, render_tear_sheet_page

__all__ = [
    "ReportData",
    "TearSheet",
    "build_report",
    "render_html",
    "render_tear_sheet",
    "render_tear_sheet_page",
]
