"""The replay of recent sessions for the service layer (roadmap 23.15).

:func:`run_replay` replays strategies over the tick universe
(``[production].universe`` on the day). The live stage gate adds it as the
``recent_replay`` check of every promotion, and the subscription service
runs it before an approve or auto subscription starts or restarts.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime

from stonks.app.context import AppContext
from stonks.production.live.gates import GateCheck
from stonks.production.live.replay import ReplayReport, replay_check
from stonks.store.state import SqliteState


def run_replay(
    context: AppContext,
    state: SqliteState,
    strategy_ids: Sequence[str],
    *,
    as_of: date | None = None,
) -> ReplayReport:
    from stonks.production.universe import EmptyUniverseError, production_tickers

    settings = context.settings
    day = as_of or datetime.now(UTC).date()
    with context.lake() as lake:
        try:
            universe = production_tickers(lake, settings.production.universe, day)
        except EmptyUniverseError:
            universe = []
        return replay_check(
            lake,
            context.registry_on(state),
            list(strategy_ids),
            universe,
            day,
            settings.production.live.replay,
        )


def replay_gate_check(report: ReplayReport) -> GateCheck:
    """The replay as a gate check (``None`` passed: shown, not blocking)."""
    detail = report.detail if report.passed is not None else f"unavailable: {report.detail}"
    return GateCheck("recent_replay", report.passed, detail, value=report.as_dict())


def book_strategies(state: SqliteState, portfolio_id: str) -> list[str]:
    """The strategies a portfolio's book trades (enabled paper, approve and
    auto subscriptions)."""
    rows = state.sql(
        "SELECT strategy_id FROM subscriptions WHERE portfolio_id = ? AND enabled = 1"
        " AND mode IN ('paper', 'approve', 'auto') ORDER BY strategy_id",
        [portfolio_id],
    )
    return [r["strategy_id"] for r in rows]
