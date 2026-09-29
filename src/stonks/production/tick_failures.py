"""Tick failures that happened before the tick recorded its row.

:func:`~stonks.production.tick.run_tick` alerts the operator from its own
error handler, which starts once the ``tick_runs`` row exists. A failure
before that (building the plan, the universe, a ``database is locked`` on
the row insert) alerted nobody. The entry points raise it as
:class:`PreTickError` so the scheduler alerts on it at once instead of
waiting for the deadline watchdog.
"""

from __future__ import annotations

from typing import Any


class PreTickError(RuntimeError):
    """A tick failed before ``run_tick`` recorded its row, so it did not
    alert. The message is ``"<original type>: <original message>"``."""

    @classmethod
    def wrap(cls, exc: BaseException) -> PreTickError:
        return cls(f"{type(exc).__name__}: {exc}")


def tick_row_count(state: Any) -> int | None:
    """How many ``tick_runs`` rows exist, or ``None`` when the read fails."""
    try:
        return int(state.sql("SELECT COUNT(*) AS n FROM tick_runs")[0]["n"])
    except Exception:
        return None


def tick_row_added(state: Any, before: int | None) -> bool:
    """Whether a tick row appeared since ``before`` (so ``run_tick`` reached
    its error handler and alerted). Unknown counts read as no row: a second
    alert is better than none."""
    after = tick_row_count(state)
    return before is not None and after is not None and after > before
