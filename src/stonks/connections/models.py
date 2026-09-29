"""Rows and results of the connections service. No secrets live in any of
these types: credentials stay sealed in ``broker_credentials`` and are only
opened inside the service."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ConnectionStatus = Literal["pending", "active", "error"]
SyncStatus = Literal["ok", "partial", "error"]


@dataclass(frozen=True)
class ConnectionRecord:
    id: str
    user_id: str
    provider: str
    label: str | None
    status: ConnectionStatus
    external_user_id: str | None
    last_sync_at: str | None
    last_sync_status: SyncStatus | None
    last_error: str | None
    consecutive_failures: int
    next_sync_at: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Any) -> ConnectionRecord:
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            provider=row["provider"],
            label=row["label"],
            status=row["status"],
            external_user_id=row["external_user_id"],
            last_sync_at=row["last_sync_at"],
            last_sync_status=row["last_sync_status"],
            last_error=row["last_error"],
            consecutive_failures=int(row["consecutive_failures"]),
            next_sync_at=row["next_sync_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass(frozen=True)
class BrokerAccountRow:
    connection_id: str
    external_account_id: str
    name: str
    institution: str | None
    number_mask: str | None
    currency: str
    portfolio_id: str | None  # the linked portfolio, if any


@dataclass(frozen=True)
class ProviderInfo:
    """What a UI needs to offer a provider. ``enabled`` is true when an
    admin turned it on and it is configured: only those can be connected."""

    name: str
    display_name: str
    auth_flow: str
    capabilities: tuple[str, ...]
    credential_fields: tuple[str, ...]
    can_trade: bool
    enabled: bool = True
    has_paper: bool = False
    #: Trading needs a gateway on the server (IBKR), set up by an admin.
    needs_gateway: bool = False


@dataclass(frozen=True)
class PortalLink:
    connection_id: str
    url: str
    expires_at: str


@dataclass(frozen=True)
class PortfolioSyncResult:
    portfolio_id: str
    external_account_id: str
    snapshot_id: int | None = None
    positions: int = 0
    unmapped: tuple[str, ...] = ()  # raw symbols no ticker maps to ("not covered")
    activities_new: int = 0
    activities_seen: int = 0
    error: str | None = None


@dataclass(frozen=True)
class SyncResult:
    connection_id: str
    status: SyncStatus
    portfolios: tuple[PortfolioSyncResult, ...] = ()
    error: str | None = None
    next_sync_at: str | None = None
    accounts_seen: int = 0

    @property
    def ok(self) -> bool:
        return self.status != "error"

    @property
    def unmapped(self) -> tuple[str, ...]:
        return tuple(sorted({s for p in self.portfolios for s in p.unmapped}))


@dataclass(frozen=True)
class DisconnectResult:
    connection_id: str
    archived_portfolios: tuple[str, ...]
    remote_removed: bool | None  # None: the provider has no remote user
    remote_error: str | None = None


@dataclass(frozen=True)
class ImportEnvResult:
    status: Literal["imported", "already_imported", "not_applicable"]
    message: str
    connection_id: str | None = None
    portfolio_id: str | None = None
    external_account_id: str | None = None
    subscriptions_switched: tuple[str, ...] = field(default_factory=tuple)
