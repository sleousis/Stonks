"""``list`` universes: a static set of tickers, optionally with dated spans.

Plain ``tickers`` are members from ``start_date`` (default 1900-01-01) with
no end, so a list carries survivorship bias unless its ``spans`` say when
names joined and left. :func:`parse_list_csv` turns a CSV export into a
spec.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks.universes.base import (
    EARLIEST,
    Materialized,
    MembershipSpan,
    RefreshContext,
    UniverseProvider,
)


class SpanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str = Field(min_length=1, max_length=64)
    start_date: date
    end_date: date | None = None

    @model_validator(mode="after")
    def _order(self) -> Self:
        if self.end_date is not None and self.end_date <= self.start_date:
            raise ValueError(f"{self.ticker}: end_date must be after start_date")
        return self


class ListSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tickers: list[str] = Field(default_factory=list, max_length=100_000)
    spans: list[SpanIn] = Field(default_factory=list, max_length=100_000)
    #: When plain ``tickers`` became members.
    start_date: date = EARLIEST

    @model_validator(mode="after")
    def _not_empty(self) -> Self:
        self.tickers = list(dict.fromkeys(t.strip() for t in self.tickers if t.strip()))
        if not self.tickers and not self.spans:
            raise ValueError("a list universe needs tickers or spans")
        return self


class ListProvider(UniverseProvider):
    kind = "list"
    spec_model = ListSpec

    def materialize(self, spec: ListSpec, ctx: RefreshContext) -> Materialized:
        spans = [MembershipSpan(t, spec.start_date) for t in spec.tickers]
        spans += [MembershipSpan(s.ticker, s.start_date, s.end_date) for s in spec.spans]
        return Materialized(spans=spans)


def parse_list_csv(content: str) -> dict[str, Any]:
    """A ``list`` spec from CSV text. A ``ticker`` column (else the first
    column) names the tickers; optional ``start_date`` / ``end_date``
    columns make dated spans. Blank lines and ``#`` comments are skipped."""
    lines = [ln for ln in content.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        raise ValueError("the CSV is empty")
    reader = csv.reader(io.StringIO("\n".join(lines)))
    rows = [[c.strip() for c in r] for r in reader]
    header = [h.lower() for h in rows[0]]
    if "ticker" in header:
        body = rows[1:]
        t_col = header.index("ticker")
    else:
        body, t_col, header = rows, 0, []
    s_col = header.index("start_date") if "start_date" in header else None
    e_col = header.index("end_date") if "end_date" in header else None
    tickers: list[str] = []
    spans: list[dict[str, Any]] = []
    for row in body:
        if t_col >= len(row) or not row[t_col]:
            continue
        ticker = row[t_col]
        start = row[s_col] if s_col is not None and s_col < len(row) else ""
        end = row[e_col] if e_col is not None and e_col < len(row) else ""
        if start or end:
            # an exit without a known start still ends the membership
            spans.append(
                {
                    "ticker": ticker,
                    "start_date": start or EARLIEST.isoformat(),
                    "end_date": end or None,
                }
            )
        else:
            tickers.append(ticker)
    spec = ListSpec.model_validate({"tickers": tickers, "spans": spans})
    return spec.model_dump(mode="json", exclude_defaults=True)
