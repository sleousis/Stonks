"""The equity curve the drawdown rules read, with deposits and withdrawals
taken out.

A book's value history counts a deposit as a gain and a withdrawal as a
loss. A deposit after a loss would hide the drawdown, and a withdrawal
could trip the circuit breaker on money the owner moved. The risk rules
therefore read a performance index built like the time-weighted return
(:mod:`stonks.insights.returns`): a flow lands at the start of its day,
and the step into day ``t`` is ``V_t / (V_{t-1} + F_t)``.

The index is expressed in today's money: it is scaled so its last point
equals the last value plus any flow recorded since (up to ``as_of``). The
rules append today's live account value to the curve, and that value
already holds those flows, so the two line up. Drawdowns, week and month
losses read off this curve are the index's own.

Only the risk inputs change. Every page that shows a value history keeps
reading :func:`stonks.production.pnl.load_pnl`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import date

from stonks.fx import FxRateMissing, FxRates
from stonks.insights.returns import Flow
from stonks.logging import get_logger
from stonks.store.state import SqliteState

_log = get_logger("stonks.production.risk_curve")


def flow_adjusted_curve(
    points: Sequence[tuple[date, float]],
    flows: Sequence[Flow],
    *,
    as_of: date | None = None,
) -> list[tuple[date, float]]:
    """``points`` (``(day, value)``, oldest first) with the external
    ``flows`` taken out, in today's money (see the module docstring).

    A flow on or before the first point is starting capital. A flow after
    the last point and up to ``as_of`` only rescales the curve. A step whose
    base is not positive (a book emptied and refilled) counts as flat."""
    if not points or not flows:
        return list(points)
    first, last = points[0][0], points[-1][0]
    inside = sorted((f for f in flows if f.day > first), key=lambda f: f.day)
    index = [1.0]
    i = 0
    prev_value = points[0][1]
    for day, value in points[1:]:
        flow = 0.0
        while i < len(inside) and inside[i].day <= day:
            flow += inside[i].amount
            i += 1
        base = prev_value + flow
        step = value / base if base > 0 and value >= 0 else 1.0
        index.append(index[-1] * step)
        prev_value = value
    pending = sum(
        f.amount for f in inside[i:] if f.day > last and (as_of is None or f.day <= as_of)
    )
    anchor = points[-1][1] + pending
    if index[-1] <= 0 or anchor <= 0:
        return list(points)
    scale = anchor / index[-1]
    return [(day, ix * scale) for (day, _), ix in zip(points, index, strict=True)]


FxLoader = Callable[[Iterable[str]], FxRates]


def adjusted_curve(
    state: SqliteState,
    portfolio_id: str,
    points: Sequence[tuple[date, float]],
    *,
    as_of: date | None = None,
    fx_loader: FxLoader | None = None,
) -> list[tuple[date, float]]:
    """``points`` of ``portfolio_id`` with its recorded deposits and
    withdrawals (manual and broker-synced) taken out. A flow in another
    currency with no rate leaves the curve as it is (logged): a guessed
    flow would bend it either way."""
    from stonks.insights.flows import external_flows

    try:
        flows = external_flows(state, portfolio_id, fx_loader=fx_loader)
    except FxRateMissing as exc:
        _log.warning(
            "risk_curve.fx_missing", portfolio_id=portfolio_id, currency=exc.source, error=str(exc)
        )
        return list(points)
    return flow_adjusted_curve(points, flows, as_of=as_of)
