"""The ``BrokerConnection`` seam: a user's read (later trade) link to a broker
or aggregator. Types here are ours; adapters translate vendor JSON into them
and vendor types never cross this module.

Two auth flows:

- ``api_key``: the user pastes keys (Alpaca). :meth:`BrokerConnection.open`
  takes them as :class:`Credentials`.
- ``portal``: the provider hosts the login (SnapTrade). The adapter also
  implements :class:`PortalFlow`: register a provider-side user, build the
  connection-portal URL, and later remove the user again.

See ``docs/design/accounts-and-modes.md`` section 6.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Self

from stonks.ingest.redact import redact_secrets

if TYPE_CHECKING:  # pragma: no cover
    from stonks.connections.ratelimit import RateLimiter
    from stonks.connections.settings import ConnectionsConfig
    from stonks.core.protocols import Broker


class Capability(StrEnum):
    READ_BALANCES = "read_balances"
    READ_POSITIONS = "read_positions"
    READ_ORDERS = "read_orders"
    READ_ACTIVITY = "read_activity"
    TRADE = "trade"
    SHORT = "short"
    OPTIONS = "options"

    @property
    def is_read(self) -> bool:
        return self.value.startswith("read_")


AuthFlow = Literal["api_key", "portal"]
ActivityKind = Literal[
    "trade", "dividend", "interest", "fee", "deposit", "withdrawal", "split", "other"
]
ACTIVITY_KINDS: frozenset[str] = frozenset(
    {"trade", "dividend", "interest", "fee", "deposit", "withdrawal", "split", "other"}
)


# ---- errors -----------------------------------------------------------------


class ConnectionsError(RuntimeError):
    """Base for broker-connection failures. Messages are safe to show and to
    log: they never contain credentials."""


class ProviderDisabled(ConnectionsError):
    """The provider is unknown or not in ``[connections].enabled_providers``."""


class ProviderNotConfigured(ConnectionsError):
    """The provider is enabled but its app-level settings are missing."""


class CapabilityMissing(ConnectionsError):
    """The provider doesn't offer what was asked (e.g. trading)."""


class ProviderError(ConnectionsError):
    """A provider call failed. ``retryable`` failures move the next sync
    (network, 5xx); non-retryable ones need attention."""

    retryable: bool = False

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ProviderUnavailable(ProviderError):
    retryable = True


class ProviderAuthError(ProviderError):
    """Credentials were refused or revoked: the user has to reconnect."""


class RateLimited(ProviderError):
    """The provider (or our own limiter) said slow down."""

    retryable = True

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message, status=429)
        self.retry_after = retry_after


# ---- credentials -------------------------------------------------------------


class Credentials:
    """String fields a provider needs (API keys, aggregator user secrets).
    Never printed: ``repr`` lists field names only. Only the sync worker and
    the connect flow open them."""

    __slots__ = ("_fields",)

    def __init__(self, fields: Mapping[str, str]) -> None:
        clean: dict[str, str] = {}
        for name, value in fields.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise ConnectionsError("credential fields must be strings")
            clean[name] = value
        self._fields = clean

    def __getitem__(self, name: str) -> str:
        try:
            return self._fields[name]
        except KeyError:
            raise ConnectionsError(f"credential field {name!r} is missing") from None

    def get(self, name: str, default: str | None = None) -> str | None:
        return self._fields.get(name, default)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._fields))

    def secret_values(self) -> list[str]:
        """Every value long enough to be a secret, for scrubbing error text."""
        return [v for v in self._fields.values() if len(v) >= 6]

    def redact(self, text: str) -> str:
        return redact_secrets(text, self.secret_values())

    def to_bytes(self) -> bytes:
        return json.dumps(self._fields, sort_keys=True).encode()

    @classmethod
    def from_bytes(cls, raw: bytes) -> Credentials:
        data = json.loads(raw.decode())
        if not isinstance(data, dict):
            raise ConnectionsError("stored credentials are malformed")
        return cls(data)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Credentials) and other._fields == self._fields

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return f"Credentials(fields={list(self.names())!r})"


# ---- value types ---------------------------------------------------------------


@dataclass(frozen=True)
class RateLimit:
    """Token buckets per provider: one shared by every connection of the app,
    one per connection."""

    per_minute: int
    per_connection_per_minute: int

    def __post_init__(self) -> None:
        if self.per_minute <= 0 or self.per_connection_per_minute <= 0:
            raise ValueError("rate limits must be positive")


@dataclass(frozen=True)
class ExternalAccount:
    id: str
    name: str
    currency: str = "USD"
    institution: str | None = None
    number_mask: str | None = None  # last 4 characters only, never the full number


@dataclass(frozen=True)
class AccountBalances:
    currency: str
    cash: float
    buying_power: float | None = None
    total_value: float | None = None  # provider's own total, when it reports one


@dataclass(frozen=True)
class ExternalPosition:
    """A holding as the provider reports it. ``ticker`` is our canonical
    ticker, or ``None`` when the symbol doesn't map ("not covered"); such
    holdings are kept with their raw symbol, never dropped."""

    raw_symbol: str
    quantity: float
    ticker: str | None = None
    price: float | None = None
    market_value: float | None = None
    currency: str | None = None
    description: str | None = None

    @property
    def value(self) -> float | None:
        if self.market_value is not None:
            return self.market_value
        if self.price is not None:
            return self.price * self.quantity
        return None


@dataclass(frozen=True)
class Activity:
    provider_activity_id: str
    account_id: str
    kind: ActivityKind
    trade_date: date | None
    raw_symbol: str | None = None
    ticker: str | None = None
    quantity: float | None = None
    price: float | None = None
    amount: float | None = None
    fee: float | None = None
    currency: str | None = None
    settle_date: date | None = None
    description: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in ACTIVITY_KINDS:
            raise ValueError(f"unknown activity kind {self.kind!r}")
        if not self.provider_activity_id:
            raise ValueError("activities need the provider's id (for idempotent upserts)")


@dataclass
class ProviderContext:
    """What an adapter gets besides credentials: app-level config, the
    shared rate limiter, the connection id it acts for, and an optional
    vendor transport (tests inject a mock here; nothing else does)."""

    config: ConnectionsConfig
    connection_id: str = ""
    limiter: RateLimiter | None = None
    transport: Any = None
    sleep: Callable[[float], None] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# ---- the seam -------------------------------------------------------------------


class BrokerConnection(ABC):
    provider: ClassVar[str]
    display_name: ClassVar[str]
    auth_flow: ClassVar[AuthFlow]
    capabilities: ClassVar[frozenset[Capability]]
    rate_limit: ClassVar[RateLimit]
    #: For ``api_key`` flows: the fields a user supplies.
    credential_fields: ClassVar[tuple[str, ...]] = ()
    #: The provider offers paper (simulated money) accounts.
    has_paper: ClassVar[bool] = False

    @classmethod
    @abstractmethod
    def open(cls, credentials: Credentials, context: ProviderContext) -> Self:
        """An adapter bound to one connection's credentials."""

    @classmethod  # noqa: B027 - optional hook, not abstract
    def check_configured(cls, config: ConnectionsConfig) -> None:
        """Raise :class:`ProviderNotConfigured` when app-level settings are
        missing. API-key providers need none."""

    @classmethod
    def supports(cls, capability: Capability) -> bool:
        return capability in cls.capabilities

    @classmethod
    def require(cls, capability: Capability) -> None:
        if capability not in cls.capabilities:
            raise CapabilityMissing(f"{cls.provider} does not offer {capability.value}")

    @abstractmethod
    def accounts(self) -> list[ExternalAccount]: ...

    @abstractmethod
    def balances(self, account_id: str) -> AccountBalances: ...

    @abstractmethod
    def positions(self, account_id: str) -> list[ExternalPosition]: ...

    def orders(self, account_id: str, since: datetime) -> list[Any]:
        self.require(Capability.READ_ORDERS)
        raise NotImplementedError  # pragma: no cover

    def activities(self, account_id: str, since: date) -> list[Activity]:
        self.require(Capability.READ_ACTIVITY)
        raise NotImplementedError  # pragma: no cover

    def trader(self, account_id: str) -> Broker:
        """A ``Broker`` placing orders in this account; only with
        :attr:`Capability.TRADE` (step S6 wires it into the tick)."""
        self.require(Capability.TRADE)
        raise NotImplementedError  # pragma: no cover

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release network resources."""


class PortalFlow(ABC):
    """Hosted-login providers (OAuth or an aggregator's connection portal)."""

    @classmethod
    @abstractmethod
    def register_user(cls, context: ProviderContext, external_user_id: str) -> Credentials:
        """Create the provider-side user; returns the credentials to seal."""

    @classmethod
    @abstractmethod
    def portal_url(
        cls,
        context: ProviderContext,
        external_user_id: str,
        credentials: Credentials,
        redirect_uri: str,
    ) -> str:
        """A one-time URL where the user logs into their broker (read-only)."""

    @classmethod
    @abstractmethod
    def unregister_user(
        cls, context: ProviderContext, external_user_id: str, credentials: Credentials
    ) -> None:
        """Remove the provider-side user and every authorization it holds."""


def mask_number(number: str | None) -> str | None:
    """Last four characters of an account number, never more."""
    if not number:
        return None
    tail = str(number).strip()[-4:]
    return f"…{tail}" if tail else None


def first_str(values: Iterable[Any]) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
