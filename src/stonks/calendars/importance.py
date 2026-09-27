"""How much an economic release matters (roadmap 20.9).

Vendors do not agree on an importance field, and the EODHD calendar has
none, so importance is rated from the release name at read time. An
:class:`ImportanceRater` maps a release type ("CPI", "Non Farm Payrolls")
to ``low``, ``medium`` or ``high``. :class:`KeywordImportance` is the
default: whole-word rules for the releases that move markets. A source
with its own rating can plug in another rater.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Literal, get_args

Importance = Literal["low", "medium", "high"]

#: Lowest first. A threshold passes its own level and every one after it.
IMPORTANCE_LEVELS: tuple[Importance, ...] = get_args(Importance)

#: Plain words for each threshold, for the console.
IMPORTANCE_LABELS: dict[str, str] = {
    "low": "All releases",
    "medium": "Medium and high importance",
    "high": "High importance only",
}


def at_least(importance: Importance, threshold: Importance) -> bool:
    """Whether ``importance`` reaches ``threshold``."""
    return IMPORTANCE_LEVELS.index(importance) >= IMPORTANCE_LEVELS.index(threshold)


class ImportanceRater(ABC):
    @abstractmethod
    def rate(self, event_type: str) -> Importance:
        """The importance of a release by its type name."""


def _words(*patterns: str) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(patterns) + r")\b", re.IGNORECASE)


_HIGH = _words(
    r"cpi",
    r"consumer price index",
    r"inflation rate",
    r"non[- ]?farm payrolls",
    r"rate decision",
    r"fomc",
    r"monetary policy statement",
    r"gdp",
    r"unemployment rate",
    r"pce price index",
)
_MEDIUM = _words(
    r"retail sales",
    r"pmi",
    r"ppi",
    r"producer price index",
    r"jobless claims",
    r"initial claims",
    r"industrial production",
    r"consumer confidence",
    r"consumer sentiment",
    r"trade balance",
    r"durable goods orders",
    r"housing starts",
    r"building permits",
    r"employment change",
    r"adp",
    r"average hourly earnings",
    r"zew",
    r"ifo",
)


class KeywordImportance(ImportanceRater):
    """High for inflation, jobs, growth and central bank decisions, medium
    for the other widely watched figures, low for the rest."""

    def rate(self, event_type: str) -> Importance:
        if _HIGH.search(event_type):
            return "high"
        if _MEDIUM.search(event_type):
            return "medium"
        return "low"


def default_rater() -> ImportanceRater:
    return KeywordImportance()
