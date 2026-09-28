"""Backtest tear sheets (BL-22) behind ``stonks report --backtest``.

A target is a backtest job id (its stored request is re-run: the job
result keeps only the API view, not the report a tear sheet needs), a
registered strategy id, or a catalog strategy (id, class name or
``module:Class``) with a window. Every backtest goes through
:func:`stonks.app.lab.backtest_report`, so costs and the benchmark are the
API's. A registered strategy whose lab run drew a parameter heatmap
(22.5) gets it on its tear sheet, read from the artifact's ``meta.json``.
Every tear sheet ends with the factor attribution of its P&L (22.4) over
the backtest's universe, when the lake can give style factor returns.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd

from stonks.app.catalog import CatalogService, class_path_of
from stonks.app.context import AppContext
from stonks.app.errors import NotFoundError, ValidationError
from stonks.app.jobs import JobStore
from stonks.app.lab import BACKTEST_JOB, BacktestRequest, backtest_report
from stonks.app.strategies import StrategyRef, StrategyService
from stonks.factors.style import style_factor_returns
from stonks.lab.catalog import resolve_strategy
from stonks.lab.heatmap import ParameterHeatmap
from stonks.logging import get_logger
from stonks.production.universe import EmptyUniverseError, window_tickers
from stonks.reporting.factor_attribution import (
    attribute_returns,
    render_factor_attribution_section,
    returns_from_curve,
)
from stonks.reporting.sections import strategy_report_sections
from stonks.reporting.tearsheet import TearSheet, render_tear_sheet_page

__all__ = ["TearSheetWindow", "render_backtest_tear_sheet", "tear_sheet_request"]

_log = get_logger("stonks.app.tearsheets")


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
    ref = request.strategy
    title = ref.strategy_id or f"{ref.class_path} {ref.params or ''}".strip()
    with context.lake() as lake:
        report, _ = backtest_report(context.settings, strategy, request, lake)
        attribution = _factor_attribution(lake, request, report, title)
    heatmap = _registered_heatmap(context, ref.strategy_id) if ref.strategy_id else None
    sections = tuple(strategy_report_sections(strategy))
    if attribution:
        sections = (*sections, attribution)
    return render_tear_sheet_page(
        TearSheet(title=title, report=report, heatmap=heatmap, sections=sections)
    )


def _factor_attribution(lake: Any, request: BacktestRequest, report: Any, title: str) -> str:
    """The factor attribution section of ``report``; empty (logged) when
    the universe gives no factor returns. A stored universe counts each
    name only while it was a member (22.10)."""
    try:
        universe = list(request.universe)
        membership = None
        universe_id = None
        if not universe and request.universe_id:
            universe_id = request.universe_id
            universe = lake.members_between(universe_id, request.start, request.end)
            spans = lake.get_universe_membership(universe_id)
            membership = pd.DataFrame(spans[["ticker", "start_date", "end_date"]])
        if not universe:
            return ""
        factors = style_factor_returns(
            lake,
            universe,
            request.start,
            request.end,
            membership=membership,
            universe_id=universe_id,
        )
        returns = returns_from_curve(report.equity_dates, report.equity_curve)
        return render_factor_attribution_section(attribute_returns(returns, factors), title)
    except Exception as exc:  # attribution is extra: the tear sheet still renders
        _log.warning("tearsheet.factor_attribution_failed", error=str(exc))
        return ""


def _registered_heatmap(context: AppContext, strategy_id: str) -> ParameterHeatmap | None:
    """The heatmap the strategy's lab run stored in its ``meta.json``;
    ``None`` when there is none or it can't be read."""
    with context.registry() as registry:
        handle = next((h for h in registry.list_all() if h.id == strategy_id), None)
    if handle is None:
        return None
    try:
        meta = json.loads((handle.artifact_path / "meta.json").read_text())
        data = meta.get("heatmap")
        return ParameterHeatmap.from_dict(data) if data else None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _log.warning("tearsheet.heatmap.unreadable", strategy_id=strategy_id, error=str(exc))
        return None


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
