"""Backtest tear sheets (BL-22) behind ``stonks report --backtest``.

A target is a backtest job id (its stored request is re-run: the job
result keeps only the API view, not the report a tear sheet needs), a
registered strategy id, or a catalog strategy (id, class name or
``module:Class``) with a window. Every backtest goes through
:func:`stonks.app.lab.backtest_report`, so costs and the benchmark are the
API's.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from stonks.app.catalog import CatalogService, class_path_of
from stonks.app.context import AppContext
from stonks.app.errors import NotFoundError, ValidationError
from stonks.app.jobs import JobStore
from stonks.app.lab import BACKTEST_JOB, BacktestRequest, backtest_report
from stonks.app.strategies import StrategyRef, StrategyService
from stonks.lab.catalog import resolve_strategy
from stonks.production.universe import EmptyUniverseError, window_tickers
from stonks.reporting.forecast_weights import strategy_report_sections
from stonks.reporting.tearsheet import TearSheet, render_tear_sheet_page

__all__ = ["TearSheetWindow", "render_backtest_tear_sheet", "tear_sheet_request"]


@dataclass(frozen=True)
class TearSheetWindow:
    """What a strategy target needs (a job carries its own)."""

    start: date | None = None
    end: date | None = None
    universe: tuple[str, ...] = ()
    params: dict[str, Any] | None = None
    benchmark: str | None = None


def tear_sheet_request(
    context: AppContext, target: str, window: TearSheetWindow
) -> BacktestRequest:
    """The backtest request for ``target`` (see the module doc).
    ``ValidationError`` names what is missing or wrong."""
    job = _backtest_job(context, target)
    if job is not None:
        request = BacktestRequest.model_validate(job)
        if window.benchmark is not None:
            request = request.model_copy(update={"benchmark": window.benchmark})
        return request
    if window.start is None or window.end is None:
        raise ValidationError(f"{target!r} is not a backtest job: pass --start and --end")
    universe = list(window.universe)
    universe_id: str | None = None
    if not universe:
        configured = context.settings.production.universe
        # A universe id keeps its membership gate (point in time, BE-07).
        universe_id = configured if isinstance(configured, str) else None
        with context.lake() as lake:
            try:
                universe = window_tickers(
                    lake, context.settings.production.universe, window.start, window.end
                )
            except EmptyUniverseError as exc:
                raise ValidationError(f"pass --tickers, or {exc}") from None
    with context.registry() as registry:
        registered = any(h.id == target for h in registry.list_all())
    if registered:
        ref = StrategyRef(strategy_id=target)
    else:
        try:
            cls = resolve_strategy(target)
        except ValueError as exc:
            raise ValidationError(f"no backtest job, registered strategy or {exc}") from None
        ref = StrategyRef(class_path=class_path_of(cls), params=dict(window.params or {}))
    try:
        return BacktestRequest(
            strategy=ref,
            universe=universe,
            universe_id=universe_id,
            start=window.start,
            end=window.end,
            benchmark=window.benchmark,
        )
    except ValueError as exc:  # pydantic ValidationError is a ValueError
        raise ValidationError(str(exc)) from None


def render_backtest_tear_sheet(
    context: AppContext, request: BacktestRequest, catalog: CatalogService
) -> str:
    """Run ``request`` and render its self-contained tear sheet page."""
    strategy = StrategyService(context, catalog).resolve(request.strategy)
    with context.lake() as lake:
        report, _ = backtest_report(context.settings, strategy, request, lake)
    ref = request.strategy
    title = ref.strategy_id or f"{ref.class_path} {ref.params or ''}".strip()
    sections = tuple(strategy_report_sections(strategy))
    return render_tear_sheet_page(TearSheet(title=title, report=report, sections=sections))


def _backtest_job(context: AppContext, target: str) -> dict[str, Any] | None:
    """The stored request of backtest job ``target``; ``None`` when no job
    has that id. ``ValidationError`` for a job of another kind."""
    try:
        job = JobStore(context.settings.state.path).get(target)
    except NotFoundError:
        return None
    if job.kind != BACKTEST_JOB:
        raise ValidationError(f"job {target!r} is a {job.kind} job, not a {BACKTEST_JOB} job")
    return dict(job.params)
