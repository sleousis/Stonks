"""Operator drills and the soak report (roadmap 19.11) for the CLI.

- :func:`run_kill_switch_drill_scratch` runs the kill switch drill on a
  fresh scratch state DB in a temporary folder, through the real
  ``HaltService.engage_kill`` (global scope, stop-all), with the simulated
  working-order broker unless a test passes another. Production data is
  never read or written, and no real order is ever sent.
- :func:`live_soak_report` reads the paper soak report of one portfolio
  from the configured state DB (read-only).
- :func:`reconcile_portfolio` syncs one portfolio's open orders with its
  broker now (the runbooks' manual reconcile).
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from datetime import date
from pathlib import Path

from stonks.accounts import Scope
from stonks.app.context import AppContext
from stonks.config import Settings, StateConfig
from stonks.execution.reconcile import StartupReconcile
from stonks.production.drills import (
    DRILL_REASON,
    DrillReport,
    SimulatedWorkingBroker,
    run_kill_switch_drill,
)
from stonks.production.soak import SoakReport, soak_report
from stonks.store.state import SqliteState

__all__ = ["live_soak_report", "reconcile_portfolio", "run_kill_switch_drill_scratch"]


def run_kill_switch_drill_scratch(
    settings: Settings,
    *,
    broker: object | None = None,
    ticker: str = "AAPL.US",
    reference_price: float = 100.0,
    cancel_timeout: float = 10.0,
) -> DrillReport:
    """The drill on a throwaway copy of the schema. ``settings`` only lends
    everything but the state path, which points into a temporary folder
    deleted afterwards."""
    from stonks.app.halts import HaltService, KillSwitchRequest

    drill_broker = broker if broker is not None else SimulatedWorkingBroker()
    with tempfile.TemporaryDirectory(prefix="stonks-drill-", ignore_cleanup_errors=True) as tmp:
        scratch = settings.model_copy(
            update={"state": StateConfig(path=Path(tmp) / "state.sqlite")}
        )
        context = AppContext(scratch)
        service = HaltService(context, brokers=lambda _portfolio_id: drill_broker)

        def engage(_state: SqliteState) -> int:
            view = service.engage_kill(
                Scope.service("drill"),
                KillSwitchRequest(scope="global", buys_only=False, reason=DRILL_REASON),
            )
            return view.id

        with context.state() as state:
            state.migrate()
            return run_kill_switch_drill(
                state,
                drill_broker,
                ticker=ticker,
                reference_price=reference_price,
                engage=engage,
                cancel_timeout=cancel_timeout,
            )


def live_soak_report(
    settings: Settings,
    *,
    portfolio_id: str,
    days: int = 20,
    end: date | None = None,
    model_portfolio_id: str | None = None,
) -> SoakReport:
    with AppContext(settings).state() as state:
        return soak_report(
            state,
            portfolio_id=portfolio_id,
            days=days,
            end=end,
            model_portfolio_id=model_portfolio_id,
        )


def reconcile_portfolio(
    settings: Settings,
    *,
    portfolio_id: str,
    brokers: Callable[[str], object | None] | None = None,
) -> StartupReconcile | None:
    """Reconcile one portfolio's open orders with its broker now, the way
    the submit job does before it sends (``startup_reconcile``). ``None``
    when the portfolio trades at no external broker. It only reads the
    broker, it never sends or cancels."""
    from stonks.app.halts import settings_brokers
    from stonks.execution.reconcile import startup_reconcile

    context = AppContext(settings)
    broker = (brokers or settings_brokers(context))(portfolio_id)
    if broker is None:
        return None
    with context.state() as state:
        state.migrate()
        return startup_reconcile(broker, state, portfolio_id=portfolio_id)  # type: ignore[arg-type]
