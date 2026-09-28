"""The :class:`Factor` seam (roadmap 22.2).

A factor turns the lake into a date-by-ticker panel of numbers that should
rank future returns. Two kinds ship:

- :class:`ExpressionFactor`: a formula in the expression language,
  evaluated as DuckDB SQL (:mod:`stonks.factors.engine`);
- fundamentals factors (:mod:`stonks.factors.library.fundamentals`): the
  point-in-time value, quality and forensic scores of
  :mod:`stonks.features.fundamentals`.

Every factor answers two questions: ``panel`` (a whole window, for
research and tear sheets) and ``values_at`` (one decision, for trading).
Both are point in time: a value on date ``t`` uses only data known at the
close of ``t`` (P12).

``direction`` is the sign of the hypothesis: ``+1`` when higher values
should earn more, ``-1`` when lower values should. Tear sheets report raw
values; :class:`~stonks.strategies.examples.factor_strategy.FactorStrategy`
and ranking helpers use the direction.

A factor taken from a paper carries its :class:`Provenance` (roadmap 23.13):
the paper, its sample years, the year it was published and the statistic it
reported. Tear sheets then split the IC into in-sample, post-sample and
post-publication periods, the decay McLean and Pontiff (2016) measured.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Literal

import pandas as pd

from stonks.core.interval import Interval
from stonks.factors.engine import PanelRequest, latest_values, panel_from_lake
from stonks.factors.expression import Node, lookback, parse_factor

__all__ = ["ExpressionFactor", "Factor", "FactorKind", "Period", "Provenance"]

FactorKind = Literal["expression", "fundamental"]
#: Where a date falls against a paper: before its sample, inside it, after
#: the sample but before publication, or after publication.
Period = Literal["pre_sample", "in_sample", "post_sample", "post_publication"]


@dataclass(frozen=True)
class Provenance:
    """Where a published factor comes from. Years are calendar years."""

    #: Authors, year, title and journal.
    paper: str
    #: The year the paper was published (its journal issue).
    published: int
    #: First and last year of the paper's sample.
    sample_start: int
    sample_end: int
    #: What the paper reported, in its own terms (a spread, a t-stat).
    reported: str
    #: The headline t-statistic, when the paper gives one.
    t_stat: float | None = None

    def __post_init__(self) -> None:
        if not self.paper.strip():
            raise ValueError("a provenance needs a paper")
        if self.sample_start > self.sample_end:
            raise ValueError("the sample must start before it ends")
        if self.published < self.sample_end:
            raise ValueError("a paper is published after its sample ends")

    def period_of(self, day: date) -> Period:
        """Where ``day`` falls against the paper's sample and publication."""
        if day.year < self.sample_start:
            return "pre_sample"
        if day.year <= self.sample_end:
            return "in_sample"
        if day.year < self.published:
            return "post_sample"
        return "post_publication"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Factor(ABC):
    """A named, reusable signal over a universe (module doc)."""

    #: Unique id (``KMID``, ``ROC20``, ``piotroski_f``).
    id: str
    #: One line on what it measures.
    description: str
    #: Group in the catalog (``kbar``, ``price``, ``momentum``, ``value``, ...).
    family: str
    #: ``+1``: higher should earn more; ``-1``: lower should.
    direction: int = 1
    #: Why it should work and when it fails (P1).
    hypothesis: str = ""
    kind: FactorKind
    #: Lake tables beyond bars that feed it (they enter the cache fingerprint).
    tables: tuple[str, ...] = ()
    #: Asset classes it applies to (fundamentals exist for equities only).
    asset_classes: tuple[str, ...] = ("equity", "crypto", "commodity", "bond")
    #: The paper it comes from, for a published factor.
    provenance: Provenance | None = None

    @property
    def cache_token(self) -> str:
        """Identifies the computation for the panel cache (a version prefix
        lets a rule change retire old panels)."""
        return f"v1:{self.kind}:{self.id}"

    @property
    def lookback_bars(self) -> int:
        """Bars of history before a date the factor needs."""
        return 0

    #: True when every date of a window can be computed in one pass (an
    #: expression); false when each date is computed on its own, so callers
    #: should pass the sampled dates only.
    whole_window: bool = True

    @abstractmethod
    def panel(
        self, lake: Any, request: PanelRequest, dates: pd.DatetimeIndex | None = None
    ) -> pd.DataFrame:
        """Dates by ``request.universe`` tickers over ``request``'s window
        (or at ``dates`` only, when given)."""

    @abstractmethod
    def values_at(
        self,
        lake: Any,
        tickers: Sequence[str],
        as_of: datetime,
        interval: Interval = Interval.DAY_1,
    ) -> dict[str, float]:
        """``{ticker: value}`` known at a decision on ``as_of`` (``lake`` is a
        point-in-time view in a backtest). Tickers with no value are left
        out."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "family": self.family,
            "description": self.description,
            "direction": self.direction,
            "hypothesis": self.hypothesis,
            "expression": getattr(self, "expression", None),
            "lookback_bars": self.lookback_bars,
            "asset_classes": list(self.asset_classes),
            "provenance": self.provenance.to_dict() if self.provenance else None,
        }


class ExpressionFactor(Factor):
    """A factor defined by a formula (:mod:`stonks.factors.expression`)."""

    kind: FactorKind = "expression"

    def __init__(
        self,
        id: str,
        expression: str,
        *,
        description: str = "",
        family: str = "custom",
        direction: int = 1,
        hypothesis: str = "",
        provenance: Provenance | None = None,
    ) -> None:
        if direction not in (1, -1):
            raise ValueError(f"direction must be 1 or -1, got {direction}")
        self.id = id
        self.node: Node = parse_factor(expression)
        #: Canonical form of the formula.
        self.expression = str(self.node)
        self.description = description or self.expression
        self.family = family
        self.direction = direction
        self.hypothesis = hypothesis
        self.provenance = provenance

    @classmethod
    def adhoc(cls, text: str) -> ExpressionFactor:
        """An unnamed formula typed by a person; its id is its canonical form."""
        node = parse_factor(text)
        return cls(str(node), text, description="custom expression")

    @property
    def cache_token(self) -> str:
        # the formula, not the name: renaming a factor keeps its panels
        return f"v1:expression:{self.expression}"

    @property
    def lookback_bars(self) -> int:
        return lookback(self.node)

    def panel(
        self, lake: Any, request: PanelRequest, dates: pd.DatetimeIndex | None = None
    ) -> pd.DataFrame:
        out = panel_from_lake(self.node, lake, request)
        return out if dates is None else out.reindex(dates)

    def values_at(
        self,
        lake: Any,
        tickers: Sequence[str],
        as_of: datetime,
        interval: Interval = Interval.DAY_1,
    ) -> dict[str, float]:
        return latest_values(self.node, lake, tickers, as_of, interval=interval)

    def __repr__(self) -> str:
        return f"ExpressionFactor({self.id!r}, {self.expression!r})"
