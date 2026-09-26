"""StatementService: the statement audit's flags (BL-36) for the transports.

``stonks audit statements`` (and every ``stonks ingest fundamentals``)
writes ``statement_flags``; this reads them. Readers of statements can skip
flagged periods with ``DuckDBLake.get_statements_as_of(..., exclude_flagged=True)``.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel

from stonks.app.context import AppContext
from stonks.app.pagination import Page


class StatementFlagView(BaseModel):
    ticker: str
    period_end: date
    #: ``A`` (annual) or ``Q`` (quarterly).
    frequency: str
    #: e.g. ``balance_identity``, ``net_income_mismatch``.
    check_id: str
    severity: Literal["error", "warning"]
    #: What failed, with the figures.
    detail: str
    flagged_at: datetime


class StatementService:
    def __init__(self, context: AppContext) -> None:
        self._ctx = context

    def flags(
        self,
        *,
        ticker: str | None = None,
        severity: str | None = None,
        limit: int,
        offset: int,
    ) -> Page[StatementFlagView]:
        """Flags ordered by ticker, period and check; newest audit per ticker."""
        with self._ctx.lake() as lake:
            df = lake.get_statement_flags(ticker, severity=severity)
        rows = df.to_dict("records")
        items = [
            StatementFlagView(
                ticker=r["ticker"],
                period_end=r["period_end"],
                frequency=r["frequency"],
                check_id=r["check_id"],
                severity=r["severity"],
                detail=r["detail"],
                flagged_at=r["flagged_at"],
            )
            for r in rows[offset : offset + limit]
        ]
        return Page(items=items, total=len(rows), limit=limit, offset=offset)
