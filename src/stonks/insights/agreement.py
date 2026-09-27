"""Strategy agreement: for each holding, what every active strategy's
latest signal says about it.

A strategy's signal for a ticker is its ``estimate_return`` on the latest
priced day, the same call the tick's signal phase makes. A positive
estimate agrees with a long holding and disagrees with a short one; a zero
or missing estimate is no view. A strategy never scores an asset class it
does not trade, nor a holding no ticker maps to.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date
from typing import Any

from stonks.insights.book import Book, Holding, Side
from stonks.insights.models import HoldingAgreement, Opinion, Stance
from stonks.logging import get_logger

_log = get_logger("stonks.insights.agreement")


def judge(side: Side, expected: float | None, ticker: str, as_of: date) -> tuple[Stance, str]:
    """The stance of one estimate on one holding, with the reason."""
    day = as_of.isoformat()
    if expected is None or math.isnan(expected):
        return "no_view", f"no estimate for {ticker} on {day}"
    if expected == 0:
        return "no_view", f"expects no move for {ticker} on {day}"
    shown = f"{expected:+.2%}"
    held = f"you hold it {side}"
    if (expected > 0) == (side == "long"):
        return "agree", f"expects {shown} for {ticker} on {day}; {held}"
    return "disagree", f"expects {shown} for {ticker} on {day}; {held}"


def _opinion(
    strategy_id: str, strategy: Any, holding: Holding, as_of: date | None, lake: Any
) -> Opinion:
    def say(stance: Stance, reason: str, expected: float | None = None) -> Opinion:
        return Opinion(
            strategy_id=strategy_id, stance=stance, expected_return=expected, reason=reason
        )

    if holding.ticker is None:
        return say("not_applicable", f"not covered: no ticker maps to {holding.symbol}")
    classes = tuple(getattr(strategy, "applicable_asset_classes", ("equity",)))
    if holding.asset_class not in classes:
        kind = holding.asset_class or "of an unknown asset class"
        return say(
            "not_applicable", f"trades {', '.join(classes)} only; {holding.ticker} is {kind}"
        )
    if as_of is None:
        return say("no_view", f"no prices for {holding.ticker} yet")
    try:
        expected = strategy.estimate_return(holding.ticker, as_of, lake)
    except Exception as exc:  # one strategy must not break the page
        _log.warning(
            "insights.estimate_failed",
            strategy_id=strategy_id,
            ticker=holding.ticker,
            error=str(exc),
        )
        return say("error", f"could not score {holding.ticker} ({type(exc).__name__})")
    value = None if expected is None else float(expected)
    stance, reason = judge(holding.side, value, holding.ticker, as_of)
    if value is not None and math.isnan(value):
        value = None
    return say(stance, reason, value)


def strategy_agreement(
    book: Book, strategies: Mapping[str, Any], as_of: date | None, lake: Any
) -> list[HoldingAgreement]:
    """One row per holding, in book order, with every strategy's opinion."""
    rows: list[HoldingAgreement] = []
    for h in book.holdings:
        opinions = [_opinion(sid, s, h, as_of, lake) for sid, s in strategies.items()]
        rows.append(
            HoldingAgreement(
                symbol=h.symbol,
                ticker=h.ticker,
                side=h.side,
                agree=sum(o.stance == "agree" for o in opinions),
                disagree=sum(o.stance == "disagree" for o in opinions),
                opinions=opinions,
            )
        )
    return rows
