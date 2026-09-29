"""ConnectionsAppService: the transport-facing view of
:class:`stonks.connections.service.ConnectionService`.

Thin on purpose: every call opens a state connection, runs the scoped
service call and turns its rows into response models. Domain errors become
app errors (404 for missing *or* not-yours ids, never 403). Credentials go
in through :meth:`connect_with_keys` and never come back out: no view here
has a field that could hold them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from stonks.accounts import AccountsError, NotFound, Scope
from stonks.app.context import AppContext
from stonks.app.errors import ConfigurationError, NotFoundError, ValidationError
from stonks.connections.base import (
    ConnectionsError,
    ProviderAuthError,
    ProviderError,
    ProviderNotConfigured,
)
from stonks.connections.models import (
    BrokerAccountRow,
    ConnectionRecord,
    ConnectionStatus,
    SyncStatus,
)
from stonks.connections.service import ConnectionService
from stonks.connections.settings import ConnectionsConfig
from stonks.logging import get_logger
from stonks.security import SecretBox, SecretBoxError

_log = get_logger("stonks.app.connections")

#: Longest credential value accepted (API keys and secrets are far shorter).
CREDENTIAL_MAX = 1024


class ProviderUnavailableError(ConfigurationError):
    """The provider could not be reached or throttled us (503)."""

    title = "Provider unavailable"


# ---- request models --------------------------------------------------------------


class ConnectWithKeysRequest(BaseModel):
    """API-key connect. ``fields`` are the provider's ``credential_fields``
    (plus ``paper`` for brokers with a paper endpoint). They are write-only:
    never logged, stored only sealed, never returned."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    provider: str = Field(min_length=1, max_length=64)
    fields: dict[str, SecretStr] = Field(max_length=16)
    label: str | None = Field(default=None, max_length=120)


class StartPortalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=64)
    #: Where the provider sends the browser back (``https``, or ``http`` on
    #: loopback). ``connection_id`` and a one-time ``state`` are appended.
    redirect_uri: str = Field(min_length=1, max_length=2048)
    #: Re-link an existing pending or broken connection of this provider.
    connection_id: str | None = Field(default=None, max_length=64)
    label: str | None = Field(default=None, max_length=120)


class LinkAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_account_id: str = Field(min_length=1, max_length=200)
    #: An existing broker-kind portfolio of yours; omitted: a new one.
    portfolio_id: str | None = Field(default=None, max_length=64)
    #: Name of the new portfolio (ignored with ``portfolio_id``).
    name: str | None = Field(default=None, max_length=120)


# ---- views ---------------------------------------------------------------------------


class ProviderView(BaseModel):
    name: str
    display_name: str
    auth_flow: str
    capabilities: list[str]
    #: What ``POST /api/connections/keys`` expects in ``fields``.
    credential_fields: list[str]
    can_trade: bool
    #: An admin turned it on and it is configured: only these can be connected.
    enabled: bool
    #: The provider offers paper (simulated money) accounts.
    has_paper: bool
    #: Trading needs a gateway an admin runs on the server (IBKR). eToro
    #: and the other API providers need none.
    needs_gateway: bool


class ConnectionView(BaseModel):
    id: str
    provider: str
    label: str | None
    status: ConnectionStatus
    last_sync_at: datetime | None
    last_sync_status: SyncStatus | None
    last_error: str | None
    consecutive_failures: int
    next_sync_at: datetime | None
    created_at: datetime
    updated_at: datetime
    #: Broker accounts this connection has seen.
    accounts_count: int = 0

    @classmethod
    def of(cls, r: ConnectionRecord, accounts_count: int = 0) -> ConnectionView:
        return cls(
            accounts_count=accounts_count,
            id=r.id,
            provider=r.provider,
            label=r.label,
            status=r.status,
            last_sync_at=r.last_sync_at,  # type: ignore[arg-type]
            last_sync_status=r.last_sync_status,
            last_error=r.last_error,
            consecutive_failures=r.consecutive_failures,
            next_sync_at=r.next_sync_at,  # type: ignore[arg-type]
            created_at=r.created_at,  # type: ignore[arg-type]
            updated_at=r.updated_at,  # type: ignore[arg-type]
        )


class BrokerAccountView(BaseModel):
    connection_id: str
    external_account_id: str
    name: str
    institution: str | None
    number_mask: str | None
    currency: str
    #: The portfolio mirroring this account, if linked.
    portfolio_id: str | None

    @classmethod
    def of(cls, r: BrokerAccountRow) -> BrokerAccountView:
        return cls(**{f: getattr(r, f) for f in cls.model_fields})


class PortalLinkView(BaseModel):
    connection_id: str
    #: One-time provider URL to open in the browser.
    url: str
    expires_at: datetime


class LinkResultView(BaseModel):
    connection_id: str
    external_account_id: str
    portfolio_id: str


class PortfolioSyncView(BaseModel):
    portfolio_id: str
    external_account_id: str
    snapshot_id: int | None
    positions: int
    #: Raw symbols no ticker maps to ("not covered").
    unmapped: list[str]
    activities_new: int
    activities_seen: int
    error: str | None


class SyncResultView(BaseModel):
    connection_id: str
    status: SyncStatus
    portfolios: list[PortfolioSyncView]
    error: str | None
    next_sync_at: datetime | None
    accounts_seen: int


class DisconnectView(BaseModel):
    connection_id: str
    #: Portfolios that mirrored this connection; unlinked and archived.
    archived_portfolios: list[str]
    #: Whether the provider-side user was removed (None: the provider has none).
    remote_removed: bool | None
    remote_error: str | None


# ---- service ------------------------------------------------------------------------


def _config_from(context: AppContext) -> ConnectionsConfig:
    existing = getattr(context.settings, "connections", None)
    if isinstance(existing, ConnectionsConfig):
        return existing
    return ConnectionsConfig.load()


@contextmanager
def translate_errors() -> Iterator[None]:
    """Domain errors -> app errors. Messages from the connections layer are
    already credential-free (see ``connections.service``)."""
    try:
        yield
    except NotFound as exc:
        raise NotFoundError(str(exc)) from None
    except ProviderNotConfigured as exc:
        raise ConfigurationError(str(exc)) from None
    except ProviderAuthError as exc:
        raise ValidationError(str(exc)) from None
    except ProviderError as exc:
        raise ProviderUnavailableError(str(exc)) from None
    except (ConnectionsError, AccountsError) as exc:
        raise ValidationError(str(exc)) from None
    except SecretBoxError as exc:
        # Key names only (never key material), but keep the text generic.
        _log.error("connections.secret_box_error", error_type=type(exc).__name__)
        raise ConfigurationError(
            "credential encryption is not configured on the server (STONKS_SECRET_KEYS)"
        ) from None


class ConnectionsAppService:
    def __init__(
        self,
        context: AppContext,
        *,
        config: ConnectionsConfig | None = None,
        box: SecretBox | None = None,
        transports: Mapping[str, Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """``config`` defaults to ``settings.connections`` (once that field
        exists), else ``[connections]`` of the TOML file plus the
        environment. ``box`` and ``transports`` are for tests."""
        self._ctx = context
        self._config = config
        self._box = box
        self._transports = dict(transports or {})
        self._clock = clock

    @property
    def config(self) -> ConnectionsConfig:
        if self._config is None:
            self._config = _config_from(self._ctx)
        return self._config

    @contextmanager
    def _service(self) -> Iterator[ConnectionService]:
        with self._ctx.state() as state, translate_errors():
            kwargs: dict[str, Any] = {"box": self._box, "transports": self._transports}
            if self._clock is not None:
                kwargs["clock"] = self._clock
            yield ConnectionService(state, self.config, **kwargs)

    # ---- reads ---------------------------------------------------------------

    def providers(self, scope: Scope) -> list[ProviderView]:
        with self._service() as svc:
            return [
                ProviderView(
                    name=p.name,
                    display_name=p.display_name,
                    auth_flow=p.auth_flow,
                    capabilities=list(p.capabilities),
                    credential_fields=list(p.credential_fields),
                    can_trade=p.can_trade,
                    enabled=p.enabled,
                    has_paper=p.has_paper,
                    needs_gateway=p.needs_gateway,
                )
                for p in svc.providers(scope)
            ]

    def list(self, scope: Scope) -> list[ConnectionView]:
        with self._service() as svc:
            records = svc.list(scope)
            counts = svc.account_counts([r.id for r in records])
            return [ConnectionView.of(r, counts[r.id]) for r in records]

    def get(self, scope: Scope, connection_id: str) -> ConnectionView:
        with self._service() as svc:
            return _view(svc, svc.get(scope, connection_id))

    def accounts(self, scope: Scope, connection_id: str) -> list[BrokerAccountView]:
        with self._service() as svc:
            return [BrokerAccountView.of(a) for a in svc.accounts(scope, connection_id)]

    # ---- writes (audited by ConnectionService) --------------------------------

    def connect_with_keys(self, scope: Scope, request: ConnectWithKeysRequest) -> ConnectionView:
        fields = {k: v.get_secret_value() for k, v in request.fields.items()}
        if any(len(v) > CREDENTIAL_MAX for v in fields.values()):
            raise ValidationError(f"credential values must be at most {CREDENTIAL_MAX} characters")
        with self._service() as svc:
            record = svc.connect_with_keys(scope, request.provider, fields, label=request.label)
            return _view(svc, record)

    def start_portal(self, scope: Scope, request: StartPortalRequest) -> PortalLinkView:
        with self._service() as svc:
            link = svc.start_portal(
                scope,
                request.provider,
                request.redirect_uri,
                connection_id=request.connection_id,
                label=request.label,
            )
        return PortalLinkView(
            connection_id=link.connection_id,
            url=link.url,
            expires_at=link.expires_at,  # type: ignore[arg-type]
        )

    def complete_portal(
        self, scope: Scope, connection_id: str, state: str, outcome: str | None = None
    ) -> ConnectionView:
        with self._service() as svc:
            record = svc.complete_portal(scope, connection_id, state, outcome=outcome)
            return _view(svc, record)

    def link_account(
        self, scope: Scope, connection_id: str, request: LinkAccountRequest
    ) -> LinkResultView:
        with self._service() as svc:
            portfolio_id = svc.link_account(
                scope,
                connection_id,
                request.external_account_id,
                portfolio_id=request.portfolio_id,
                name=request.name,
            )
        return LinkResultView(
            connection_id=connection_id,
            external_account_id=request.external_account_id,
            portfolio_id=portfolio_id,
        )

    def sync(self, scope: Scope, connection_id: str) -> SyncResultView:
        with self._service() as svc:
            r = svc.sync(scope, connection_id)
        return SyncResultView(
            connection_id=r.connection_id,
            status=r.status,
            portfolios=[
                PortfolioSyncView(
                    portfolio_id=p.portfolio_id,
                    external_account_id=p.external_account_id,
                    snapshot_id=p.snapshot_id,
                    positions=p.positions,
                    unmapped=list(p.unmapped),
                    activities_new=p.activities_new,
                    activities_seen=p.activities_seen,
                    error=p.error,
                )
                for p in r.portfolios
            ],
            error=r.error,
            next_sync_at=r.next_sync_at,  # type: ignore[arg-type]
            accounts_seen=r.accounts_seen,
        )

    def disconnect(self, scope: Scope, connection_id: str) -> DisconnectView:
        with self._service() as svc:
            r = svc.disconnect(scope, connection_id)
        return DisconnectView(
            connection_id=r.connection_id,
            archived_portfolios=list(r.archived_portfolios),
            remote_removed=r.remote_removed,
            remote_error=r.remote_error,
        )


def _view(svc: Any, record: ConnectionRecord) -> ConnectionView:
    return ConnectionView.of(record, svc.account_counts([record.id])[record.id])
