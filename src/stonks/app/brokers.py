"""BrokerService — which broker the production tick trades through and, for
Alpaca, whether the account is reachable.

Nothing here needs Alpaca keys: without them the status reports "not
connected" with the reason. The Alpaca SDK is only imported when a status
check actually connects.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from stonks.app.context import AppContext
from stonks.app.errors import ConflictError
from stonks.config import Settings
from stonks.ingest.redact import redact_secrets
from stonks.logging import get_logger

_log = get_logger("stonks.app.brokers")


class AccountSource(Protocol):
    """The two read-only calls a status check makes."""

    def fetch_account(self) -> Any: ...

    def get_market_clock(self) -> Any: ...


#: Builds a connected Alpaca broker from settings (injectable for tests).
BrokerConnector = Callable[[Settings], AccountSource]


class BrokerInfo(BaseModel):
    kind: Literal["simulated", "alpaca"]
    #: Alpaca paper endpoint (ignored by the simulated broker).
    paper: bool
    allow_live: bool
    #: Whether both Alpaca keys are set; the keys themselves are never exposed.
    credentials_configured: bool


class BrokerAccountView(BaseModel):
    cash: float
    equity: float
    buying_power: float
    currency: str
    status: str
    trading_blocked: bool
    pattern_day_trader: bool
    can_trade: bool


class MarketClockView(BaseModel):
    timestamp: datetime
    is_open: bool
    next_open: datetime
    next_close: datetime


class AlpacaStatus(BaseModel):
    paper: bool
    connected: bool
    account: BrokerAccountView | None = None
    clock: MarketClockView | None = None
    #: Why it is not connected (missing keys, refused live endpoint, API error).
    error: str | None = None


def _connect_alpaca(settings: Settings) -> AccountSource:
    from stonks.core.types import Portfolio
    from stonks.execution.brokers import make_broker

    return make_broker(settings, Portfolio(cash=0.0, positions={}), kind="alpaca")  # type: ignore[return-value]


class BrokerService:
    def __init__(
        self,
        context: AppContext,
        *,
        connector: BrokerConnector | None = None,
        secrets: Callable[[], Iterable[str]] = tuple,
    ) -> None:
        self._ctx = context
        self._connect = connector or _connect_alpaca
        self._secrets = secrets

    def info(self) -> BrokerInfo:
        b = self._ctx.settings.brokers
        return BrokerInfo(
            kind=b.kind,
            paper=b.alpaca.paper,
            allow_live=b.alpaca.allow_live,
            credentials_configured=bool(b.alpaca.api_key and b.alpaca.secret_key),
        )

    def alpaca_status(self) -> AlpacaStatus:
        """Connect and read the account and market clock (read-only calls).
        ``ConflictError`` unless ``[brokers].kind`` is ``alpaca``."""
        settings = self._ctx.settings
        cfg = settings.brokers
        if cfg.kind != "alpaca":
            raise ConflictError(f"the configured broker is {cfg.kind!r}, not 'alpaca'")
        paper = cfg.alpaca.paper
        if not (cfg.alpaca.api_key and cfg.alpaca.secret_key):
            return AlpacaStatus(
                paper=paper,
                connected=False,
                error="Alpaca credentials missing: set ALPACA_API_KEY and ALPACA_SECRET_KEY",
            )
        try:
            broker = self._connect(settings)
            account = broker.fetch_account()
            clock = broker.get_market_clock()
        except Exception as exc:
            error = redact_secrets(f"{type(exc).__name__}: {exc}", self._secrets())
            _log.warning("broker.status_failed", broker="alpaca", error=error)
            return AlpacaStatus(paper=paper, connected=False, error=error)
        return AlpacaStatus(
            paper=paper,
            connected=True,
            account=BrokerAccountView(
                cash=account.cash,
                equity=account.equity,
                buying_power=account.buying_power,
                currency=account.currency,
                status=account.status,
                trading_blocked=account.trading_blocked,
                pattern_day_trader=account.pattern_day_trader,
                can_trade=account.can_trade,
            ),
            clock=MarketClockView(
                timestamp=clock.timestamp,
                is_open=clock.is_open,
                next_open=clock.next_open,
                next_close=clock.next_close,
            ),
        )
