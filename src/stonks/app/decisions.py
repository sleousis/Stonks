"""Why did or didn't we trade (roadmap 23.7): the decision rows of one of
your portfolios, for the console's "Why not X", the MCP tool and the
assistant. The rows come from :mod:`stonks.production.decisions`; the
caller resolves the portfolio (``PortfolioService.resolve`` in the API,
which answers 404 for another user's book)."""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel

from stonks.app.context import AppContext
from stonks.app.pagination import Page
from stonks.portfolio.explain import STEP_TEXT
from stonks.production.decisions import StoredDecision, count_decisions, load_decisions


class TradeDecisionView(BaseModel):
    """One ticker in one book on one tick: the step that kept it out or
    trimmed it. ``step`` is one of universe, rank, constructor, buffer,
    stale_price, risk_rule, lots, scope, external, halt, held or traded."""

    tick_id: str
    portfolio_id: str
    as_of: date
    ticker: str
    step: str
    #: traded, trimmed, kept_out or held.
    outcome: str
    #: The strategy that owned the decision.
    strategy_id: str | None
    #: Every strategy of the book that scored the ticker.
    strategies: list[str]
    score: float | None
    #: The rule and quantities, or the weights, behind the step.
    detail: dict[str, Any]
    #: One plain sentence.
    summary: str
    #: What the step means.
    step_text: str

    @classmethod
    def of(cls, row: StoredDecision) -> TradeDecisionView:
        d = row.decision
        return cls(
            tick_id=row.tick_id,
            portfolio_id=row.portfolio_id,
            as_of=row.as_of,
            ticker=d.ticker,
            step=d.step,
            outcome=d.outcome,
            strategy_id=d.strategy_id,
            strategies=list(d.strategies),
            score=d.score,
            detail=d.detail,
            summary=d.summary(),
            step_text=STEP_TEXT.get(d.step, d.step),
        )


class DecisionService:
    def __init__(self, context: AppContext) -> None:
        self._context = context

    def list(
        self,
        portfolio_id: str,
        *,
        ticker: str | None = None,
        strategy_id: str | None = None,
        tick_id: str | None = None,
        since: date | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Page[TradeDecisionView]:
        """Newest first. ``strategy_id`` matches rows that strategy owned or scored."""
        with self._context.state() as state:
            rows = load_decisions(
                state,
                portfolio_id,
                ticker=ticker,
                strategy_id=strategy_id,
                tick_id=tick_id,
                since=since,
                limit=limit,
                offset=offset,
            )
            total = count_decisions(
                state,
                portfolio_id,
                ticker=ticker,
                strategy_id=strategy_id,
                tick_id=tick_id,
                since=since,
            )
        return Page[TradeDecisionView](
            items=[TradeDecisionView.of(r) for r in rows], total=total, limit=limit, offset=offset
        )
