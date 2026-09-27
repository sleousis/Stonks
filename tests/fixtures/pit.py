"""Test helper for spies that inspect the lake a backtest ran on (BL-49).

The engine hands strategies a ``PointInTimeLake`` view per decision bar. A
spy that checks *which* lake it got (a permuted or perturbed copy), or reads
that lake whole, looks through the view with :func:`underlying`. Real
strategies never do this.
"""

from __future__ import annotations

from typing import Any


def underlying(lake: Any) -> Any:
    """The lake behind a point-in-time view, or ``lake`` itself."""
    session = getattr(lake, "pit_session", None)
    return lake if session is None else session.lake
