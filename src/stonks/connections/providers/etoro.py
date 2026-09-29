"""eToro as a connection, over eToro's official public API only.

See ``docs/design/etoro.md`` for what the API allows and what Stonks
leaves out. In short:

- **Keys.** The person makes a key pair in eToro (Settings > Trading > API
  Key Management) for one environment, Demo or Real, with Read or Write
  permission. Stonks seals ``api_key`` and ``user_key`` per connection,
  plus the ``paper`` flag: ``true`` (the default) for a demo key, ``false``
  for a real one. A key for the other environment is refused at connect.
- **Sync.** The account id, cash, equity and positions from the P&L
  endpoint, and closed trades as activities (each position's open as a buy,
  each close as a sell), so Insights, the behaviour report and tax lots
  work. Instrument ids map to tickers through an in-memory catalog.
- **Trading** is off until ``[connections.etoro] trading = true``. Then
  :meth:`EtoroConnection.trader` returns an ``EtoroBroker`` for the linked
  account. The demo account trades no real money (the Broker paper stage).
  A real account trades real money: it opens positions only with
  ``allow_real_money = true`` and at the Real money stages (the stage
  guard), and may always close.
- **Pacing.** Reads and orders take tokens from per-connection buckets
  set below eToro's limits, and a ``429`` backs off, as eToro's terms ask.

Nothing here runs unless an admin lists ``etoro`` in
``[connections].enabled_providers``.
"""

from __future__ import annotations

from datetime import date
from typing import Any, ClassVar, Self

from stonks.connections.base import (
    AccountBalances,
    Activity,
    BrokerConnection,
    Capability,
    CapabilityMissing,
    Credentials,
    ExternalAccount,
    ExternalPosition,
    ProviderContext,
    ProviderError,
    RateLimit,
    mask_number,
)
from stonks.connections.ratelimit import RateLimiter, limiter_for
from stonks.connections.registry import register_provider
from stonks.connections.settings import ConnectionsConfig
from stonks.execution.brokers.etoro.account import (
    SETTLEMENT_REAL,
    Env,
    EtoroAccount,
    EtoroPosition,
    parse_time,
)
from stonks.execution.brokers.etoro.broker import EtoroBroker
from stonks.execution.brokers.etoro.client import EtoroClient
from stonks.execution.brokers.etoro.instruments import EtoroInstrument, catalog_for

#: Every eToro connection together, and one connection (reads). The
#: per-connection figure follows ``[connections.etoro].reads_per_minute``.
_APP_PER_MINUTE = 6000
_WRITE_SCOPES = ("{env}:write", "trade.{env}:write")


def env_of(credentials: Credentials) -> Env:
    """``paper = false`` names a real key. Anything else is a demo key."""
    return "real" if (credentials.get("paper") or "true").strip().lower() == "false" else "demo"


@register_provider("etoro")
class EtoroConnection(BrokerConnection):
    display_name: ClassVar[str] = "eToro"
    auth_flow = "api_key"
    credential_fields = ("api_key", "user_key")
    has_paper: ClassVar[bool] = True
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {
            Capability.READ_BALANCES,
            Capability.READ_POSITIONS,
            Capability.READ_ACTIVITY,
            Capability.TRADE,
        }
    )
    rate_limit: ClassVar[RateLimit] = RateLimit(
        per_minute=_APP_PER_MINUTE, per_connection_per_minute=50
    )

    def __init__(self, account: EtoroAccount, context: ProviderContext) -> None:
        self._account = account
        self._context = context
        self._config = context.config.etoro
        self._catalog = catalog_for(self._config.base_url, self._config.instrument_overrides)
        self._id: str | None = None
        self._scopes: tuple[str, ...] = ()

    @classmethod
    def capabilities_for(cls, config: ConnectionsConfig) -> frozenset[Capability]:
        """Trading only when ``[connections.etoro] trading`` is on."""
        if config.etoro.trading:
            return cls.capabilities
        return cls.capabilities - {Capability.TRADE}

    @classmethod
    def open(cls, credentials: Credentials, context: ProviderContext) -> Self:
        config = context.config.etoro
        client = EtoroClient(
            config,
            api_key=credentials["api_key"],
            user_key=credentials["user_key"],
            transport=context.transport,
            read_limiter=_reads(context, config.reads_per_minute),
            write_limiter=limiter_for(
                "etoro_orders", RateLimit(_APP_PER_MINUTE, config.orders_per_minute)
            ),
            connection_key=context.connection_id,
            sleep=context.sleep,
        )
        return cls(EtoroAccount(client, env_of(credentials)), context)

    @property
    def env(self) -> Env:
        return self._account.env

    # ---- reads -----------------------------------------------------------------------

    def accounts(self) -> list[ExternalAccount]:
        account_id = self._account_id()
        # a key for the other environment is refused here, at connect
        self._account.snapshot()
        cid = account_id.split("-", 1)[1]
        return [
            ExternalAccount(
                id=account_id,
                name=f"eToro {'demo' if self.env == 'demo' else 'real'}",
                currency="USD",
                institution="eToro",
                number_mask=mask_number(cid),
            )
        ]

    def balances(self, account_id: str) -> AccountBalances:
        self._check(account_id)
        snap = self._account.snapshot()
        return AccountBalances(
            currency="USD",
            cash=snap.available_cash,
            buying_power=snap.available_cash,
            total_value=snap.equity,
        )

    def positions(self, account_id: str) -> list[ExternalPosition]:
        self._check(account_id)
        snap = self._account.snapshot()
        held = [*snap.positions, *snap.mirror_positions]
        found = self._catalog.by_ids(self._account.client, [p.instrument_id for p in held])
        groups: dict[int, list[EtoroPosition]] = {}
        for p in held:
            groups.setdefault(p.instrument_id, []).append(p)
        out: list[ExternalPosition] = []
        for iid, rows in groups.items():
            inst = found.get(iid)
            quantity = sum(p.signed_units for p in rows)
            if abs(quantity) < 1e-12:
                continue
            value = sum(p.amount + (p.pnl or 0.0) for p in rows)
            price = next((p.close_rate for p in rows if p.close_rate), None)
            out.append(
                ExternalPosition(
                    raw_symbol=inst.symbol if inst else str(iid),
                    ticker=inst.ticker if inst else None,
                    quantity=quantity,
                    price=price,
                    market_value=value,
                    currency="USD",
                    description=_describe(inst, rows),
                )
            )
        return out

    def activities(self, account_id: str, since: date) -> list[Activity]:
        """Buys from each position's open and sells from each close.
        eToro lists no dividends, fees or cash moves for the trading
        account, so there are none (``docs/design/etoro.md``, gaps)."""
        self._check(account_id)
        snap = self._account.snapshot()
        closed = self._account.history(since)
        ids = [p.instrument_id for p in snap.positions] + [
            _int(r.get("instrumentId")) for r in closed
        ]
        found = self._catalog.by_ids(self._account.client, [i for i in ids if i >= 0])
        out: dict[str, Activity] = {}
        open_ids = {p.position_id for p in snap.positions}
        for p in snap.positions:
            act = _open_activity(account_id, found.get(p.instrument_id), p)
            if act is not None:
                out[act.provider_activity_id] = act
        opened: dict[int, list[dict[str, Any]]] = {}
        for row in closed:
            act = _close_activity(account_id, found.get(_int(row.get("instrumentId"))), row)
            if act is None:
                continue
            out[act.provider_activity_id] = act
            pid = _int(row.get("positionId"))
            if pid not in open_ids:
                opened.setdefault(pid, []).append(row)
        for pid, rows in opened.items():
            act = _closed_open_activity(
                account_id, found.get(_int(rows[0].get("instrumentId"))), pid, rows
            )
            if act is not None:
                out.setdefault(act.provider_activity_id, act)
        return sorted(
            out.values(), key=lambda a: (a.trade_date or date.min, a.provider_activity_id)
        )

    # ---- trading ---------------------------------------------------------------------

    def trader(self, account_id: str) -> EtoroBroker:
        """An ``EtoroBroker`` for ``account_id``. Refused while trading is
        off or the key cannot write. A real account without
        ``allow_real_money`` may only close."""
        if Capability.TRADE not in self.capabilities_for(self._context.config):
            raise CapabilityMissing(
                "eToro trading is off: an admin sets [connections.etoro] trading = true"
            )
        self._check(account_id)
        if self._scopes and not any(
            s.endswith(tail.format(env=self.env)) for s in self._scopes for tail in _WRITE_SCOPES
        ):
            raise ProviderError(
                f"this eToro key cannot trade: make a {self.env} key with Write permission",
                status=403,
            )
        broker = EtoroBroker(
            self._account,
            self._catalog,
            account_id=account_id,
            state=self._context.extra.get("state"),
        )
        if self.env == "real" and not self._config.allow_real_money:
            broker.refuse_opens(
                "real-money opens at eToro need [connections.etoro] allow_real_money = true"
            )
        return broker

    def close(self) -> None:
        """The trader may outlive the connection object: the client stays open."""

    # ---- plumbing --------------------------------------------------------------------

    def _account_id(self) -> str:
        if self._id is None:
            self._id, self._scopes = self._account.account_id()
        return self._id

    def _check(self, account_id: str) -> None:
        if account_id != self._account_id():
            raise ProviderError(f"unknown eToro account {account_id!r}", status=404)


def _reads(context: ProviderContext, per_minute: int) -> RateLimiter:
    limiter = context.limiter
    if limiter is not None and limiter.limit.per_connection_per_minute <= per_minute:
        return limiter
    return limiter_for("etoro_reads", RateLimit(_APP_PER_MINUTE, per_minute))


def _describe(inst: EtoroInstrument | None, rows: list[EtoroPosition]) -> str | None:
    notes: list[str] = []
    if any(p.settlement != SETTLEMENT_REAL for p in rows):
        notes.append("CFD")
    if any(p.leverage > 1 for p in rows):
        notes.append("leveraged")
    if any(p.mirror_id for p in rows):
        notes.append("copy trading")
    name = inst.name if inst and inst.name else None
    if not notes:
        return name
    return f"{name or ''} ({', '.join(notes)})".strip()


def _open_activity(
    account_id: str, inst: EtoroInstrument | None, p: EtoroPosition
) -> Activity | None:
    if p.opened_at is None:
        return None
    units = p.initial_units or p.units
    sign = 1.0 if p.is_buy else -1.0
    return Activity(
        provider_activity_id=f"open:{p.position_id}",
        account_id=account_id,
        kind="trade",
        trade_date=p.opened_at.date(),
        raw_symbol=inst.symbol if inst else str(p.instrument_id),
        ticker=inst.ticker if inst else None,
        quantity=sign * units,
        price=p.open_rate,
        amount=-(p.initial_amount or units * p.open_rate),
        currency="USD",
        description=_label(p.leverage, p.settlement),
    )


def _close_activity(
    account_id: str, inst: EtoroInstrument | None, row: dict[str, Any]
) -> Activity | None:
    pid = _int(row.get("positionId"))
    when = parse_time(row.get("closeTimestamp"))
    units = _num(row.get("units"))
    if pid < 0 or when is None or units is None:
        return None
    sign = -1.0 if bool(row.get("isBuy", True)) else 1.0
    invested = _num(row.get("investment")) or 0.0
    profit = _num(row.get("netProfit")) or 0.0
    fees = _num(row.get("fees"))
    return Activity(
        provider_activity_id=f"close:{pid}:{when.isoformat()}",
        account_id=account_id,
        kind="trade",
        trade_date=when.date(),
        raw_symbol=inst.symbol if inst else str(row.get("instrumentId")),
        ticker=inst.ticker if inst else None,
        quantity=sign * units,
        price=_num(row.get("closeRate")),
        amount=invested + profit,
        fee=abs(fees) if fees else None,
        currency="USD",
        description=_label(_int(row.get("leverage"), default=1), SETTLEMENT_REAL),
    )


def _closed_open_activity(
    account_id: str, inst: EtoroInstrument | None, pid: int, rows: list[dict[str, Any]]
) -> Activity | None:
    """The open of a position that is fully closed now, from its closes."""
    when = parse_time(rows[0].get("openTimestamp"))
    if when is None:
        return None
    units = sum(_num(r.get("units")) or 0.0 for r in rows)
    sign = 1.0 if bool(rows[0].get("isBuy", True)) else -1.0
    return Activity(
        provider_activity_id=f"open:{pid}",
        account_id=account_id,
        kind="trade",
        trade_date=when.date(),
        raw_symbol=inst.symbol if inst else str(rows[0].get("instrumentId")),
        ticker=inst.ticker if inst else None,
        quantity=sign * units,
        price=_num(rows[0].get("openRate")),
        amount=-sum(_num(r.get("investment")) or 0.0 for r in rows),
        currency="USD",
        description=_label(_int(rows[0].get("leverage"), default=1), SETTLEMENT_REAL),
    )


def _label(leverage: int, settlement: int) -> str | None:
    if leverage > 1:
        return f"eToro, leverage x{leverage}"
    if settlement != SETTLEMENT_REAL:
        return "eToro CFD"
    return None


def _num(value: Any) -> float | None:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any, *, default: int = -1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


__all__ = ["EtoroConnection", "env_of"]
