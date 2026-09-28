"""The second-source price check for the service layer (roadmap 23.6).

:func:`run_configured_price_check` is what the scheduled ``price_check`` job
runs on every backend: the configured second source (``[production.
price_check] source``, Yahoo by default) for signalled tickers and for held
tickers of portfolios at no IB Gateway, and IBKR's marks for held tickers of
a portfolio on a gateway (``use_broker_marks``). :class:`PriceCheckService`
reads the newest check for Health and runs one on demand.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel

from stonks.app.context import AppContext
from stonks.logging import get_logger
from stonks.production.price_check import (
    BrokerMarks,
    DataSourceCloses,
    PriceCheckReport,
    SecondSource,
    build_groups,
    checks_enabled,
    latest_check,
    run_price_checks,
)
from stonks.store.state import SqliteState

_log = get_logger("stonks.app.price_checks")


class PriceCheckItemView(BaseModel):
    ticker: str
    #: ``ok``, ``gap`` (opening orders held) or ``unknown`` (not compared).
    status: str
    detail: str
    source: str = ""
    vendor_date: date | None = None
    vendor_close: float | None = None
    second_close: float | None = None
    close_gap: float | None = None
    adjustment_gap: float | None = None


class PriceCheckView(BaseModel):
    id: int
    as_of: date
    checked_at: datetime
    source: str
    #: ``clean``, ``gaps``, ``systematic`` (the operational halt opened) or
    #: ``unavailable`` (nothing compared).
    status: str
    tickers_checked: int
    tickers_compared: int
    #: Tickers whose opening orders the tick holds on ``as_of``.
    held: list[str]
    items: list[PriceCheckItemView]
    detail: str | None = None
    halt_id: int | None = None


class PriceCheckRunBody(BaseModel):
    #: The session checked; default today (UTC).
    as_of: date | None = None


class PriceCheckRunView(BaseModel):
    ran: bool
    #: Why nothing ran (``disabled``, ``no_tickers``).
    reason: str | None = None
    check: PriceCheckView | None = None


def _view(row: dict[str, Any] | None) -> PriceCheckView | None:
    return PriceCheckView.model_validate(row) if row is not None else None


def ibkr_marks(
    settings: Any, state: SqliteState
) -> tuple[Callable[[str], SecondSource | None], Callable[[], None]]:
    """``(marks, close)``: the IBKR marks source of a portfolio's gateway
    (one read-only broker per gateway, built on first use), and a closer."""
    from stonks.execution.brokers.base import BrokerError, close_broker
    from stonks.execution.brokers.ibkr.factory import connect_ibkr, pick_gateway

    config = settings.brokers.ibkr
    built: dict[str, Any] = {}

    def marks(portfolio_id: str) -> SecondSource | None:
        if not config.gateways or not any(
            portfolio_id in gw.portfolios for gw in config.gateways.values()
        ):
            return None
        try:
            name, _ = pick_gateway(config, portfolio_id=portfolio_id)
            if name not in built:
                built[name] = connect_ibkr(
                    config, gateway=name, portfolio_id=portfolio_id, role="reconcile", state=state
                )
        except BrokerError as exc:
            _log.warning("price_check.marks_unavailable", portfolio_id=portfolio_id, error=str(exc))
            return None
        return BrokerMarks(built[name], name=f"ibkr:{name}")

    def close() -> None:
        for broker in built.values():
            close_broker(broker)

    return marks, close


def run_configured_price_check(
    settings: Any,
    state: SqliteState,
    lake: Any,
    as_of: date,
    *,
    now: datetime | None = None,
    source: SecondSource | None = None,
    marks: Callable[[str], SecondSource | None] | None = None,
) -> tuple[PriceCheckReport | None, str | None]:
    """``(report, None)`` after a run, or ``(None, reason)`` when skipped."""
    config = settings.production.price_check
    if not config.enabled:
        return None, "disabled"
    if not checks_enabled(state):
        return None, "no_price_checks_table"
    close: Callable[[], None] = lambda: None  # noqa: E731
    if source is None:
        from stonks.ingest.sources.registry import build_source

        source = DataSourceCloses(build_source(config.source, settings.sources))
    if marks is None and config.use_broker_marks and settings.brokers.ibkr.gateways:
        marks, close = ibkr_marks(settings, state)
    try:
        groups = build_groups(state, as_of, config, source, marks)
        if not any(tickers for _, tickers in groups):
            return None, "no_tickers"
        report = run_price_checks(state, lake, groups, as_of, config, now=now)
    finally:
        close()
    return report, None


class PriceCheckService:
    def __init__(self, context: AppContext) -> None:
        self._ctx = context

    def latest(self) -> PriceCheckView | None:
        with self._ctx.state() as state:
            return _view(latest_check(state))

    def run(self, as_of: date | None = None) -> PriceCheckRunView:
        """Run the configured price check now (the scheduled job). Trusted
        callers only: a systematic gap opens the operational halt."""
        day = as_of or datetime.now(UTC).date()
        with self._ctx.state() as state, self._ctx.lake() as lake:
            report, reason = run_configured_price_check(self._ctx.settings, state, lake, day)
            if report is None:
                return PriceCheckRunView(ran=False, reason=reason)
            return PriceCheckRunView(ran=True, check=_view(latest_check(state, day)))
