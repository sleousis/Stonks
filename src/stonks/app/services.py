"""Services — one container wiring every service onto one AppContext and
one JobRunner. Transports build it once and call into it."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from stonks.app.catalog import CatalogService, PackageStrategySource, StrategySource
from stonks.app.context import AppContext
from stonks.app.ingest import IngestService
from stonks.app.jobs import Job, JobRunner, JobStore
from stonks.app.lab import LabService
from stonks.app.market import MarketDataService
from stonks.app.orders import OrdersService
from stonks.app.pagination import Page
from stonks.app.portfolio import PortfolioService
from stonks.app.strategies import StrategyService
from stonks.app.ticks import TickService
from stonks.logging import get_logger

_log = get_logger("stonks.app.services")


def default_strategy_sources() -> list[StrategySource]:
    return [PackageStrategySource("stonks.strategies.examples", name="examples")]


class JobService:
    """Transport-facing view of the job runner."""

    def __init__(self, runner: JobRunner) -> None:
        self._runner = runner
        self._store = runner.store

    @property
    def kinds(self) -> list[str]:
        return self._runner.kinds

    def list(
        self, *, status: str | None = None, kind: str | None = None, limit: int, offset: int
    ) -> Page[Job]:
        return self._store.list(status=status, kind=kind, limit=limit, offset=offset)

    def get(self, job_id: str) -> Job:
        return self._store.get(job_id)

    def cancel(self, job_id: str) -> Job:
        job = self._store.cancel(job_id)
        _log.info("job.cancelled", job_id=job_id)
        return job

    def submit(self, kind: str, params: dict[str, Any]) -> Job:
        return self._runner.submit(kind, params)

    def wait(self, job_id: str, timeout: float | None = None) -> Job:
        return self._runner.wait(job_id, timeout=timeout)


@dataclass
class Services:
    context: AppContext
    runner: JobRunner
    jobs: JobService
    catalog: CatalogService
    portfolio: PortfolioService
    strategies: StrategyService
    market: MarketDataService
    orders: OrdersService
    ingest: IngestService
    ticks: TickService
    lab: LabService

    @classmethod
    def create(
        cls,
        context: AppContext,
        *,
        strategy_sources: Sequence[StrategySource] | None = None,
    ) -> Services:
        settings = context.settings
        runner = JobRunner(
            JobStore(settings.state.path), max_workers=settings.api.max_concurrent_jobs
        )
        catalog = CatalogService(
            sources=list(strategy_sources)
            if strategy_sources is not None
            else default_strategy_sources()
        )
        strategies = StrategyService(context, catalog)
        orders = OrdersService(context)
        return cls(
            context=context,
            runner=runner,
            jobs=JobService(runner),
            catalog=catalog,
            portfolio=PortfolioService(context),
            strategies=strategies,
            market=MarketDataService(context),
            orders=orders,
            ingest=IngestService(context, runner),
            ticks=TickService(context, orders, runner),
            lab=LabService(context, strategies, runner),
        )

    def start(self) -> None:
        """Migrate both stores and fail jobs a previous process left behind."""
        self.context.start()
        recovered = self.runner.store.recover_interrupted()
        if recovered:
            _log.warning("jobs.recovered_interrupted", count=recovered)

    def shutdown(self, wait: bool = True) -> None:
        self.runner.shutdown(wait=wait)
        self.context.close()
