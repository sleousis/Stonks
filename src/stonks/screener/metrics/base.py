"""The screen metric seam.

A :class:`ScreenMetric` turns what the lake knew on a date into one number
per ticker, for example a trailing return or a P/E ratio. Metrics read
through :class:`~stonks.screener.data.ScreenData`, which loads each kind
of input once per screen and only what was known on the date (P12).

Adding a metric is one new class in a :mod:`stonks.screener.metrics`
module; the registry (:mod:`stonks.screener.registry`) finds it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar, Literal

if TYPE_CHECKING:  # pragma: no cover
    from stonks.screener.data import ScreenData

MetricGroup = Literal["price", "fundamental"]
#: How a value reads: a plain ratio, a fraction shown as a percent, or a
#: money amount in the instrument's currency.
MetricUnit = Literal["ratio", "percent", "money"]


class ScreenMetric(ABC):
    """One screenable number per ticker. A ticker with no value (no data,
    a loss for P/E, a stale price) is left out of the result."""

    id: ClassVar[str]
    label: ClassVar[str]
    group: ClassVar[MetricGroup]
    unit: ClassVar[MetricUnit]
    description: ClassVar[str]

    @abstractmethod
    def compute(self, data: ScreenData) -> dict[str, float]:
        """Values by ticker, for the tickers of ``data`` that have one."""
