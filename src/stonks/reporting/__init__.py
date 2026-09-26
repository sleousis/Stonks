"""Static HTML reporting (roadmap 4.1), behind ``stonks report``."""

from stonks.reporting.data import ReportData, build_report
from stonks.reporting.render import render_html

__all__ = ["ReportData", "build_report", "render_html"]
