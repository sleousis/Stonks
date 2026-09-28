"""Extra tear sheet sections a strategy brings: the forecast weights of a
forecast blend (roadmap 22.7) and the model section of a learning ranker
(roadmap 23.12). Each builder looks through wrappers on its own and
returns an empty list when the strategy has nothing to show."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from stonks.reporting.forecast_weights import strategy_report_sections as forecast_sections
from stonks.reporting.ranker import ranker_sections

__all__ = ["strategy_report_sections"]

_BUILDERS: tuple[Callable[[Any], list[str]], ...] = (forecast_sections, ranker_sections)


def strategy_report_sections(strategy: Any) -> list[str]:
    """Every extra section ``strategy`` brings, already escaped HTML."""
    return [section for build in _BUILDERS for section in build(strategy)]
