"""Why did or didn't we trade (roadmap 23.7).

:func:`explain` reads one :func:`~stonks.portfolio.pipeline.build_orders`
run and names, per ticker, the step that kept it out of the book or
trimmed it:

- ``universe``: a strategy scored it, but it is outside the book's universe;
- ``rank``: ``single_winner`` handed the book to another pick;
- ``constructor``: the constructor gave it no weight;
- ``buffer``: the target was inside the no-trade band, or the trade too small;
- ``stale_price``: an opening order dropped for a stale price;
- ``risk_rule``: a named risk rule cut or trimmed the order;
- ``lots``: the lot rule (roadmap 23.1) rounded the order, or skipped it
  as smaller than one tradable lot;
- ``held``: held, and nothing asked for a trade;
- ``traded``: the order went through untouched.

The tick adds steps that run after the pipeline (``scope``, ``external``,
``halt``) with :func:`mark`. Pure: no database, no lake.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

from stonks.core.types import Portfolio

if TYPE_CHECKING:
    from stonks.portfolio.pipeline import PipelineResult

DecisionStep = Literal[
    "universe",
    "rank",
    "constructor",
    "buffer",
    "stale_price",
    "risk_rule",
    "lots",
    "scope",
    "external",
    "halt",
    "held",
    "traded",
]
DecisionOutcome = Literal["traded", "trimmed", "kept_out", "held"]

#: A plain sentence per step, for the console, MCP and the assistant.
STEP_TEXT: dict[str, str] = {
    "universe": "outside the book's universe",
    "rank": "another pick ranked higher",
    "constructor": "the constructor gave it no weight",
    "buffer": "inside the no-trade buffer, or the trade was too small",
    "stale_price": "no fresh price, so the buy was dropped",
    "risk_rule": "a risk rule changed the order",
    "lots": "rounded to tradable lots",
    "scope": "outside this run's tickers",
    "external": "the trade would cross a holding outside Stonks",
    "halt": "a halt blocked new positions",
    "held": "held, no trade needed",
    "traded": "traded as planned",
}

_EPS = 1e-12


@dataclass(frozen=True)
class TickerDecision:
    """One ticker in one book on one run."""

    ticker: str
    step: DecisionStep
    outcome: DecisionOutcome
    #: The strategy that owned the decision (largest share or best score).
    strategy_id: str | None = None
    #: Every strategy of the book that scored the ticker.
    strategies: tuple[str, ...] = ()
    score: float | None = None
    detail: dict[str, Any] = field(default_factory=dict[str, Any])

    def summary(self) -> str:
        text = STEP_TEXT.get(self.step, self.step)
        rule = self.detail.get("rule")
        if self.step == "risk_rule" and rule:
            text = f"the risk rule {rule} {'trimmed' if self.outcome == 'trimmed' else 'cut'} it"
        if self.step == "lots" and self.outcome == "kept_out":
            text = "smaller than one tradable lot, so it was skipped"
        return f"{self.ticker}: {text}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "step": self.step,
            "outcome": self.outcome,
            "strategy_id": self.strategy_id,
            "strategies": list(self.strategies),
            "score": self.score,
            "detail": dict(self.detail),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TickerDecision:
        return cls(
            ticker=str(data["ticker"]),
            step=data["step"],
            outcome=data["outcome"],
            strategy_id=data.get("strategy_id"),
            strategies=tuple(data.get("strategies") or ()),
            score=data.get("score"),
            detail=dict(data.get("detail") or {}),
        )


def explain(
    raw_signals: Mapping[str, Mapping[str, float]],
    book_signals: Mapping[str, Mapping[str, float]],
    result: PipelineResult,
    portfolio: Portfolio,
    prices: Mapping[str, float],
    *,
    universe: Collection[str] | None = None,
    constructor: str | None = None,
) -> list[TickerDecision]:
    """One decision per ticker the run touched: scored by a strategy of the
    book (before or after the universe cut), held, targeted or ordered.
    ``raw_signals`` are the book's strategies' scores before the universe
    cut, ``book_signals`` what went into the pipeline."""
    target = result.target_book
    single = "winner_strategy_id" in target.meta or result.decided_by is not None
    winner = result.decided_by or target.meta.get("winner_strategy_id")
    equity = portfolio.total_value(prices) if prices else 0.0
    tickers = {
        *(t for scores in raw_signals.values() for t in scores),
        *(t for t, q in portfolio.positions.items() if abs(q) > _EPS),
        *target.weights,
        *(o.ticker for o in result.proposed),
        *(o.ticker for o in result.orders),
    }
    out: list[TickerDecision] = []
    for ticker in sorted(tickers):
        scores = {sid: float(s[ticker]) for sid, s in book_signals.items() if ticker in s}
        raw = {sid: float(s[ticker]) for sid, s in raw_signals.items() if ticker in s}
        held = float(portfolio.positions.get(ticker, 0.0))
        final = [o for o in result.orders if o.ticker == ticker]
        adjustments = [a for a in result.adjustments if a.ticker == ticker]
        owner = _owner(ticker, result, scores or raw, final)
        base = TickerDecision(
            ticker=ticker,
            step="held",
            outcome="held",
            strategy_id=owner,
            strategies=tuple(sorted(scores or raw)),
            score=max((scores or raw).values()) if (scores or raw) else None,
        )
        out.append(
            _decide(
                base,
                final=final,
                adjustments=adjustments,
                result=result,
                scores=scores,
                raw=raw,
                held=held,
                single=single,
                winner=winner,
                weight=target.weights.get(ticker),
                current=(held * prices[ticker] / equity)
                if equity > 0 and ticker in prices
                else None,
                universe=universe,
                constructor=constructor,
            )
        )
    return out


def mark(
    decisions: Sequence[TickerDecision],
    tickers: Iterable[str],
    step: DecisionStep,
    detail: Mapping[str, Any] | None = None,
) -> list[TickerDecision]:
    """``tickers`` kept out by a step after the pipeline (scope, external
    holdings, a halt). A ticker the pipeline did not see gets its own row."""
    wanted = set(tickers)
    extra = dict(detail or {})
    out: list[TickerDecision] = []
    for d in decisions:
        if d.ticker in wanted:
            d = replace(d, step=step, outcome="kept_out", detail={**d.detail, **extra})
            wanted.discard(d.ticker)
        out.append(d)
    out.extend(
        TickerDecision(ticker=t, step=step, outcome="kept_out", detail=dict(extra))
        for t in sorted(wanted)
    )
    return out


# ---- helpers ---------------------------------------------------------------------


def _decide(
    base: TickerDecision,
    *,
    final: Sequence[Any],
    adjustments: Sequence[Any],
    result: PipelineResult,
    scores: Mapping[str, float],
    raw: Mapping[str, float],
    held: float,
    single: bool,
    winner: str | None,
    weight: float | None,
    current: float | None,
    universe: Collection[str] | None,
    constructor: str | None,
) -> TickerDecision:
    ticker = base.ticker
    if adjustments:
        last = adjustments[-1]
        outcome: DecisionOutcome = "trimmed" if final else "kept_out"
        return replace(
            base,
            step="risk_rule",
            outcome=outcome,
            detail={
                "rule": last.rule,
                "side": last.side,
                "original_quantity": last.original_quantity,
                "adjusted_quantity": last.adjusted_quantity,
                "reason": last.reason,
                "rules": [a.rule for a in adjustments],
            },
        )
    lot = _lot_change(result, ticker)
    if lot is not None:
        return replace(
            base,
            step="lots",
            outcome="trimmed" if final else "kept_out",
            detail={
                "side": lot.side,
                "original_quantity": lot.requested,
                "adjusted_quantity": lot.sized,
            },
        )
    if final:
        order = final[0]
        return replace(
            base,
            step="traded",
            outcome="traded",
            detail={"side": order.side, "quantity": order.quantity},
        )
    if ticker in result.stale_buys:
        return replace(base, step="stale_price", outcome="kept_out")
    if any(o.ticker == ticker for o in result.proposed):
        # dropped by a rule that recorded no adjustment
        return replace(base, step="risk_rule", outcome="kept_out", detail={"rule": None})
    if raw and not scores and abs(held) <= _EPS:
        in_universe = universe is None or ticker in universe
        return replace(
            base,
            step="universe",
            outcome="kept_out",
            detail={"in_universe": in_universe},
        )
    if weight is not None and abs(weight) > _EPS:
        if single:
            return replace(base, step="held", outcome="held" if abs(held) > _EPS else "kept_out")
        return replace(
            base,
            step="buffer",
            outcome="held" if abs(held) > _EPS else "kept_out",
            detail={
                "target_weight": round(float(weight), 6),
                "current_weight": round(current, 6) if current is not None else None,
            },
        )
    if scores:
        if single:
            return replace(
                base,
                step="rank",
                outcome="kept_out" if abs(held) <= _EPS else "held",
                detail={"winner": winner, "best_score": _best(result)},
            )
        return replace(
            base,
            step="constructor",
            outcome="kept_out" if abs(held) <= _EPS else "held",
            detail={"constructor": constructor},
        )
    return base


def _lot_change(result: PipelineResult, ticker: str) -> Any:
    """The lot rule's change to ``ticker``'s order, when it made one."""
    if result.lots is None:
        return None
    return next((c for c in result.lots.changes if c.ticker == ticker), None)


def _owner(
    ticker: str,
    result: PipelineResult,
    scores: Mapping[str, float],
    final: Sequence[Any],
) -> str | None:
    for order in final:
        if order.strategy_id:
            return str(order.strategy_id)
    shares = result.attribution.get(ticker) or result.target_book.attribution.get(ticker)
    if shares:
        return min(shares, key=lambda sid: (-abs(shares[sid]), sid))
    if scores:
        return min(scores, key=lambda sid: (-scores[sid], sid))
    return None


def _best(result: PipelineResult) -> float | None:
    picks = result.target_book.meta.get("picks") or []
    return float(picks[0][0]) if picks else result.winner_return
