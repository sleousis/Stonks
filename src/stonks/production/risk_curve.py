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

A flow in another currency is converted on its own day. When no rate
converts it (or no rates can be read), only that flow is unknown: the step
into its day counts as flat, and every other flow is still taken out. A
flat step is a guess too, but it neither trips the breaker on money the
owner moved nor hides a drawdown behind a deposit. An unknown flow outside
the curve's window changes nothing.

Only the risk inputs change. Every page that shows a value history keeps
reading :func:`stonks.production.pnl.load_pnl`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import date
from typing import TYPE_CHECKING

from stonks.fx import FxRates, normalize_currency
from stonks.insights.returns import Flow
from stonks.logging import get_logger
from stonks.store.state import SqliteState

if TYPE_CHECKING:
    from stonks.insights.flows import RecordedFlow

_log = get_logger("stonks.production.risk_curve")


def flow_adjusted_curve(
    points: Sequence[tuple[date, float]],
    flows: Sequence[Flow],
    *,
    as_of: date | None = None,
    flat_days: Iterable[date] = (),
) -> list[tuple[date, float]]:
    """``points`` (``(day, value)``, oldest first) with the external
    ``flows`` taken out, in today's money (see the module docstring).

    A flow on or before the first point is starting capital. A flow after
    the last point and up to ``as_of`` only rescales the curve. A step whose
    base is not positive (a book emptied and refilled) counts as flat, and
    so does the step into the first point on or after each of
    ``flat_days`` (days with a flow of unknown value)."""
    flat = sorted(flat_days)
    if not points or (not flows and not flat):
        return list(points)
    first, last = points[0][0], points[-1][0]
    inside = sorted((f for f in flows if f.day > first), key=lambda f: f.day)
    index = [1.0]
    i = 0
    prev_day, prev_value = points[0]
    for day, value in points[1:]:
        flow = 0.0
        while i < len(inside) and inside[i].day <= day:
            flow += inside[i].amount
            i += 1
        base = prev_value + flow
        unknown = any(prev_day < d <= day for d in flat)
        step = value / base if base > 0 and value >= 0 and not unknown else 1.0
        index.append(index[-1] * step)
        prev_day, prev_value = day, value
    pending = sum(
        f.amount for f in inside[i:] if f.day > last and (as_of is None or f.day <= as_of)
    )
    anchor = points[-1][1] + pending
    if index[-1] <= 0 or anchor <= 0:
        return list(points)
    scale = anchor / index[-1]
    return [(day, ix * scale) for (day, _), ix in zip(points, index, strict=True)]


FxLoader = Callable[[Iterable[str]], FxRates]


def risk_flows(
    recorded: Sequence[RecordedFlow], fx_loader: FxLoader | None
) -> tuple[list[Flow], list[date]]:
    """``(flows, unknown_days)``: each recorded flow as a signed amount in
    its account currency, and the days of the flows no rate converts. A
    missing rate (or a loader that fails) only leaves its own flow out."""
    foreign = [f for f in recorded if not _same(f.currency, f.account_currency)]
    fx: FxRates | None = None
    if foreign and fx_loader is not None:
        codes = {f.currency for f in foreign} | {f.account_currency for f in foreign}
        try:
            fx = fx_loader(sorted(codes))
        except Exception as exc:  # no rates read: the foreign flows are unknown
            _log.warning("risk_curve.fx_unavailable", error=str(exc))
    flows: list[Flow] = []
    unknown: list[date] = []
    for f in recorded:
        if _same(f.currency, f.account_currency):
            flows.append(Flow(f.day, f.signed))
            continue
        value = fx.convert(f.signed, f.currency, f.account_currency, f.day) if fx else None
        if value is None:
            unknown.append(f.day)
        else:
            flows.append(Flow(f.day, value))
    return flows, unknown


def _same(a: str, b: str) -> bool:
    return normalize_currency(a) == normalize_currency(b)


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
    currency that no rate converts counts its day as flat (logged), and the
    other flows are still taken out (see the module docstring)."""
    from stonks.insights import flows as recorded

    flows, unknown = risk_flows(recorded.recorded_flows(state, portfolio_id), fx_loader)
    if unknown:
        _log.warning(
            "risk_curve.fx_missing",
            portfolio_id=portfolio_id,
            days=sorted({d.isoformat() for d in unknown}),
        )
    return flow_adjusted_curve(points, flows, as_of=as_of, flat_days=unknown)
