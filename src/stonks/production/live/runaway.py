"""The ``runaway`` halt (roadmap 19.6).

When ``max_orders_per_run`` sees a live run that tries to close more
positions than its ceiling, it tags each close with a ``runaway``
adjustment. :func:`trip_runaway` turns those tags into a ``runaway`` halt
(mode ``buys``) on the portfolio and alerts its owner and the admins. The
halt stays until a person clears it with a reason, like every halt.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

from stonks.logging import get_logger
from stonks.production.halts import Halt, notify_trip, trip_halt
from stonks.production.rules import RiskAdjustment
from stonks.production.rules.max_orders import RUNAWAY
from stonks.store.state import SqliteState

_log = get_logger("stonks.production.live.runaway")

ACTOR = "system"


def runaway_reason(adjustments: Iterable[RiskAdjustment]) -> str | None:
    """The reason of the first ``runaway`` adjustment, or ``None``."""
    return next((a.reason for a in adjustments if a.rule == RUNAWAY), None)


def trip_runaway(
    state: SqliteState,
    portfolio_id: str,
    adjustments: Iterable[RiskAdjustment],
    *,
    on: date,
    notify: bool = True,
) -> Halt | None:
    """Open the portfolio's ``runaway`` halt when ``adjustments`` flag one.
    Returns the halt in force (new or already open), else ``None``."""
    reason = runaway_reason(adjustments)
    if reason is None:
        return None
    halt, created = trip_halt(
        state, "runaway", reason=reason, actor=ACTOR, portfolio_id=portfolio_id, on=on
    )
    if created:
        _log.warning("live.runaway_halt", halt_id=halt.id, portfolio_id=portfolio_id)
        if notify:
            notify_trip(state, halt)
    return halt
