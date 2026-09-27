"""Services — one container wiring every service onto one AppContext and
one JobRunner. Transports build it once and call into it."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from stonks.app.alerts import AlertService
from stonks.app.backups import BackupService
from stonks.app.brokers import BrokerConnector, BrokerService
from stonks.app.catalog import CatalogService, LabCatalogSource, StrategySource
from stonks.app.connections import ConnectionsAppService
from stonks.app.context import AppContext
from stonks.app.errors import ConflictError, NotFoundError
from stonks.app.factors import FactorService
from stonks.app.ingest import IngestService
from stonks.app.insights import InsightsService
from stonks.app.jobs import Job, JobRunner, JobStore
from stonks.app.lab import LabService
from stonks.app.market import MarketDataService
from stonks.app.notifications import NotificationsAppService
from stonks.app.operations import OperationsService
from stonks.app.orders import OrdersService
from stonks.app.ownership import check_owner, owner_filter, owner_of
from stonks.app.pagination import Page
from stonks.app.portfolio import PortfolioService
from stonks.app.schedule import ScheduleService
from stonks.app.signals import SignalService
from stonks.app.strategies import StrategyService
from stonks.app.stream_tokens import IssuedStreamToken, StreamTokenSigner
from stonks.app.studio import RuleStrategySource, StudioService, user_strategies_dir
from stonks.app.subscriptions import SubscriptionService
from stonks.app.ticks import TickService
from stonks.app.trial_ledger import TrialLedgerService
from stonks.app.universes import UniverseService
from stonks.app.user_strategies import UserStrategyFinder, install, uninstall
from stonks.auth.policy import Permission, require
from stonks.auth.principal import Principal
from stonks.auth.service import AuthService
from stonks.config import configured_secrets
from stonks.lab.offload.executor import make_lab_executor
from stonks.logging import get_logger
from stonks.production.tick import recover_interrupted_ticks

_log = get_logger("stonks.app.services")


def default_strategy_sources() -> list[StrategySource]:
    """The lab's catalog (examples + ``MacroRegimeFilter``, the same list
    ``stonks lab run`` resolves from) plus the Studio's ``RuleStrategy``."""
    return [LabCatalogSource(), RuleStrategySource()]


def _auth_service(context: AppContext) -> AuthService:
    """Sign-in for every transport, from ``[auth]``. The legacy
    ``STONKS_API_TOKEN`` is read on each call so tests can swap it."""

    def legacy() -> str | None:
        token = context.settings.api.token
        return token.get_secret_value() if token is not None else None

    return AuthService(context.state, settings=context.settings.auth, legacy_token=legacy)


def _configured_secrets(context: AppContext) -> list[str]:
    return configured_secrets(context.settings)


class JobService:
    """Transport-facing view of the job runner."""

    def __init__(self, runner: JobRunner, stream_tokens: StreamTokenSigner) -> None:
        self._runner = runner
        self._store = runner.store
        self._tokens = stream_tokens

    @property
    def kinds(self) -> list[str]:
        return self._runner.kinds

    def list(
        self,
        principal: Principal | None = None,
        *,
        status: str | None = None,
        kind: str | None = None,
        limit: int,
        offset: int,
    ) -> Page[Job]:
        """Jobs the caller may see: their own, or every job for admins."""
        return self._store.list(
            status=status,
            kind=kind,
            owner_id=owner_filter(principal),
            limit=limit,
            offset=offset,
        )

    def get(self, job_id: str, principal: Principal | None = None) -> Job:
        """One job; another user's job is ``NotFoundError`` (admins see all)."""
        job = self._store.get(job_id)
        check_owner(job.owner_id, principal, f"no job with id {job_id!r}")
        return job

    def cancel(self, job_id: str, principal: Principal | None = None) -> Job:
        """Cancel a queued job, or ask a running cancellable one (lab run)
        to stop at its next checkpoint; otherwise ``ConflictError``.
        Operator jobs (ticks, ingests, backups) need
        ``Permission.OPERATIONS_RUN``; ``principal=None`` is in-process."""
        if principal is not None:
            kind = self.get(job_id, principal).kind
            require(
                principal,
                Permission.OPERATIONS_RUN
                if self._runner.is_operation(kind)
                else Permission.LAB_RUN,
            )
        job = self._runner.cancel(job_id)
        _log.info("job.cancel", job_id=job_id, status=job.status)
        return job

    def submit(self, kind: str, params: dict[str, Any], principal: Principal | None = None) -> Job:
        return self._runner.submit(kind, params, owner_id=owner_of(principal))

    def wait(self, job_id: str, timeout: float | None = None) -> Job:
        return self._runner.wait(job_id, timeout=timeout)

    def typed_result[M: BaseModel](
        self, job_id: str, kind: str, model: type[M], principal: Principal | None = None
    ) -> M:
        """The result of a succeeded ``kind`` job, validated as ``model``.

        ``NotFoundError`` when there is no such job of that kind;
        ``ConflictError`` while it has not succeeded (queued, running,
        failed or cancelled jobs have no result).
        """
        job = self.get(job_id, principal)
        if job.kind != kind:
            raise NotFoundError(f"no {kind} job with id {job_id!r}")
        if job.status != "succeeded":
            detail = f": {job.error}" if job.error else ""
            raise ConflictError(f"job {job_id} has no result; it is {job.status}{detail}")
        return model.model_validate(job.result)

    def stream_token(self, job_id: str, principal: Principal) -> IssuedStreamToken:
        """A short-lived token that authorizes reading this job's event
        stream only, for ``principal``'s user (see
        :mod:`stonks.app.stream_tokens`)."""
        require(principal, Permission.READ)
        self.get(job_id, principal)  # NotFoundError for an unknown or someone else's job
        token = self._tokens.issue(job_id, principal.user_id)
        _log.info("job.stream_token_issued", job_id=job_id, expires_at=token.expires_at)
        return token

    def verify_stream_token(self, job_id: str, token: str) -> str | None:
        """The user id the token was issued to, or ``None``."""
        return self._tokens.verify(token, job_id)

    def is_tracked(self, job_id: str) -> bool:
        """False once this process will never update the job again."""
        return self._runner.is_tracked(job_id)


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
    operations: OperationsService
    brokers: BrokerService
    studio: StudioService
    alerts: AlertService
    backups: BackupService
    connections: ConnectionsAppService
    notifications: NotificationsAppService
    schedule: ScheduleService
    signals: SignalService
    universes: UniverseService
    auth: AuthService
    subscriptions: SubscriptionService
    insights: InsightsService
    ledger: TrialLedgerService
    factors: FactorService
    _user_finder: UserStrategyFinder | None = field(default=None, repr=False)

    @classmethod
    def create(
        cls,
        context: AppContext,
        *,
        strategy_sources: Sequence[StrategySource] | None = None,
        broker_connector: BrokerConnector | None = None,
    ) -> Services:
        settings = context.settings
        runner = JobRunner(
            JobStore(settings.state.path),
            max_workers=settings.api.max_concurrent_jobs,
            secrets=lambda: _configured_secrets(context),
            # Roadmap 14.9: heavy lab jobs may run in a lab worker process.
            lab_executor=make_lab_executor(
                settings.lab.offload,
                state_path=settings.state.path,
                lake_path=settings.lake.path,
                open_lake=context.lake,
            ),
        )
        catalog = CatalogService(
            sources=list(strategy_sources)
            if strategy_sources is not None
            else default_strategy_sources()
        )
        strategies = StrategyService(context, catalog)
        orders = OrdersService(context)
        lab = LabService(context, strategies, runner)
        portfolio = PortfolioService(context)
        services = cls(
            context=context,
            runner=runner,
            jobs=JobService(
                runner, StreamTokenSigner(ttl_seconds=settings.api.stream_token_ttl_seconds)
            ),
            catalog=catalog,
            portfolio=portfolio,
            strategies=strategies,
            market=MarketDataService(context),
            orders=orders,
            ingest=IngestService(context, runner),
            ticks=TickService(context, orders, runner),
            lab=lab,
            operations=OperationsService(context),
            brokers=BrokerService(
                context, connector=broker_connector, secrets=lambda: _configured_secrets(context)
            ),
            # Wired here (not lazily) so the studio job kinds are registered
            # before recover_interrupted() and the first request.
            studio=StudioService(context, lab, runner),
            alerts=AlertService(context),
            backups=BackupService(context, runner),
            connections=ConnectionsAppService(context),
            notifications=NotificationsAppService(context),
            schedule=ScheduleService(context),
            signals=SignalService(context, strategies, runner),
            universes=UniverseService(context, runner),
            auth=_auth_service(context),
            subscriptions=SubscriptionService(context),
            insights=InsightsService(context, portfolio),
            ledger=TrialLedgerService(context),
            factors=FactorService(context, runner),
        )
        services.schedule.bind(services)
        return services

    def start(self) -> None:
        """Migrate both stores, fail jobs a previous process left behind and,
        when ``api.allow_code_strategies`` is on, let registered code
        strategies be imported by class path (see ``app.user_strategies``)."""
        self.context.start()
        recovered = self.runner.store.recover_interrupted()
        if recovered:
            _log.warning("jobs.recovered_interrupted", count=recovered)
        with self.context.state() as state:  # ticks run here, as jobs (TO-06)
            recover_interrupted_ticks(state)
        settings = self.context.settings
        if settings.api.allow_code_strategies and self._user_finder is None:
            self._user_finder = install(user_strategies_dir(settings))

    def shutdown(self, wait: bool = True) -> None:
        self.runner.shutdown(wait=wait)
        if self._user_finder is not None:
            uninstall(self._user_finder)
            self._user_finder = None
        self.context.close()
