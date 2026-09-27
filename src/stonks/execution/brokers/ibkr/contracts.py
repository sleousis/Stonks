"""Our tickers onto IBKR contracts, with a ``conId`` cache (roadmap 19.2).

Our tickers are EODHD style (``AAPL.US``, ``VOD.LSE``). IBKR trades a
contract identified by its ``conId``. :class:`ContractResolver`:

1. returns a cached answer younger than ``max_age``;
2. else asks IBKR by ISIN when the instrument has one, then by symbol,
   ``SMART`` routing, the primary exchange and the currency;
3. keeps the answer only when exactly one stock matches and its currency
   (and primary exchange, when known) agrees with the instrument. Anything
   else refuses the ticker (``UnsupportedTickerError``). Ambiguity is never
   guessed.

The answer is a :class:`ResolvedContract`. ``spec()`` gives the shared
``InstrumentSpec`` (tick size, currency, exchange and the ``ibkr`` broker
id), so no IBKR type leaves this package. The cache is SQLite
``broker_contracts`` (migration 029) or memory in tests. Positions map back
by ``conId`` (:meth:`ContractResolver.ticker_for`).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from stonks.core.clock import SYSTEM_CLOCK, Clock
from stonks.core.instruments import InstrumentSpec
from stonks.execution.brokers.base import UnsupportedTickerError
from stonks.execution.brokers.ibkr.client import (
    IbApiError,
    IbClient,
    IbContract,
    IbContractDetails,
    IbContractQuery,
)
from stonks.execution.brokers.ibkr.errors import classify, to_broker_error
from stonks.logging import get_logger
from stonks.store.state import SqliteState

_log = get_logger("stonks.execution.brokers.ibkr.contracts")

BROKER = "ibkr"


@dataclass(frozen=True)
class Market:
    """How one EODHD exchange suffix trades at IBKR."""

    currency: str
    #: IBKR's primary exchange (``None`` for the US, where it comes from the
    #: instrument's own listing).
    primary_exchange: str | None
    #: How a share class separator in our symbol is written at IBKR.
    class_separator: str = " "


#: EODHD suffix -> IBKR market. Stocks and ETFs only in this phase.
MARKETS: dict[str, Market] = {
    "US": Market("USD", None, " "),
    "LSE": Market("GBP", "LSE", "."),
    "XETRA": Market("EUR", "IBIS"),
    "F": Market("EUR", "FWB"),
    "PA": Market("EUR", "SBF"),
    "AS": Market("EUR", "AEB"),
    "BR": Market("EUR", "ENEXT.BE"),
    "MC": Market("EUR", "BM"),
    "MI": Market("EUR", "BVME"),
    "SW": Market("CHF", "EBS"),
    "TO": Market("CAD", "TSE"),
}

#: A US listing as the lake names it -> IBKR's primary exchange.
US_PRIMARY: dict[str, str] = {
    "NASDAQ": "NASDAQ",
    "NYSE": "NYSE",
    "NYSE ARCA": "ARCA",
    "NYSE MKT": "AMEX",
    "AMEX": "AMEX",
    "BATS": "BATS",
}


@dataclass(frozen=True)
class InstrumentProfile:
    """What the lake knows about a ticker that helps find its contract."""

    ticker: str
    isin: str | None = None
    #: The listing venue as the lake names it (``NASDAQ``, ``NYSE``, ``LSE``).
    exchange: str | None = None
    currency: str | None = None


InstrumentLookup = Callable[[str], InstrumentProfile | None]


@dataclass(frozen=True)
class ResolvedContract:
    ticker: str
    contract: IbContract
    min_tick: float
    verified_at: datetime
    price_magnifier: int = 1
    isin: str | None = None

    @property
    def con_id(self) -> int:
        return self.contract.con_id

    def spec(self) -> InstrumentSpec:
        c = self.contract
        return InstrumentSpec.spot(
            self.ticker,
            currency=c.currency,
            exchange=c.primary_exchange or c.exchange,
            tick_size=self.min_tick,
        ).with_broker_id(BROKER, str(c.con_id))


class ContractCache(Protocol):
    def get(self, ticker: str) -> ResolvedContract | None: ...

    def by_con_id(self, con_id: int) -> ResolvedContract | None: ...

    def put(self, resolved: ResolvedContract) -> None: ...


class MemoryContractCache:
    def __init__(self) -> None:
        self._by_ticker: dict[str, ResolvedContract] = {}

    def get(self, ticker: str) -> ResolvedContract | None:
        return self._by_ticker.get(ticker)

    def by_con_id(self, con_id: int) -> ResolvedContract | None:
        return next((r for r in self._by_ticker.values() if r.con_id == con_id), None)

    def put(self, resolved: ResolvedContract) -> None:
        for ticker, r in list(self._by_ticker.items()):
            if r.con_id == resolved.con_id and ticker != resolved.ticker:
                del self._by_ticker[ticker]
        self._by_ticker[resolved.ticker] = resolved


class SqliteContractCache:
    """``broker_contracts`` rows of one broker."""

    def __init__(self, state: SqliteState, broker: str = BROKER) -> None:
        self.state = state
        self.broker = broker

    def get(self, ticker: str) -> ResolvedContract | None:
        rows = self.state.sql(
            "SELECT * FROM broker_contracts WHERE broker = ? AND ticker = ?", [self.broker, ticker]
        )
        return _from_row(rows[0]) if rows else None

    def by_con_id(self, con_id: int) -> ResolvedContract | None:
        rows = self.state.sql(
            "SELECT * FROM broker_contracts WHERE broker = ? AND con_id = ?",
            [self.broker, str(con_id)],
        )
        return _from_row(rows[0]) if rows else None

    def put(self, resolved: ResolvedContract) -> None:
        c = resolved.contract
        with self.state.transaction():
            # a conId now mapped to another ticker (a ticker change) moves
            self.state.execute(
                "DELETE FROM broker_contracts WHERE broker = ? AND con_id = ? AND ticker != ?",
                [self.broker, str(c.con_id), resolved.ticker],
            )
            self.state.execute(
                """
                INSERT INTO broker_contracts (broker, ticker, con_id, symbol, sec_type, exchange,
                    primary_exchange, currency, min_tick, price_magnifier, trading_class, isin,
                    verified_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (broker, ticker) DO UPDATE SET
                    con_id = excluded.con_id, symbol = excluded.symbol,
                    sec_type = excluded.sec_type, exchange = excluded.exchange,
                    primary_exchange = excluded.primary_exchange, currency = excluded.currency,
                    min_tick = excluded.min_tick, price_magnifier = excluded.price_magnifier,
                    trading_class = excluded.trading_class, isin = excluded.isin,
                    verified_at = excluded.verified_at
                """,
                [
                    self.broker,
                    resolved.ticker,
                    str(c.con_id),
                    c.symbol,
                    c.sec_type,
                    c.exchange,
                    c.primary_exchange,
                    c.currency,
                    resolved.min_tick,
                    resolved.price_magnifier,
                    c.trading_class,
                    resolved.isin,
                    resolved.verified_at.isoformat(),
                ],
            )


def _from_row(row: Any) -> ResolvedContract:
    contract = IbContract(
        con_id=int(row["con_id"]),
        symbol=row["symbol"],
        sec_type=row["sec_type"],
        currency=row["currency"],
        exchange=row["exchange"],
        primary_exchange=row["primary_exchange"],
        trading_class=row["trading_class"],
    )
    return ResolvedContract(
        ticker=row["ticker"],
        contract=contract,
        min_tick=float(row["min_tick"]),
        verified_at=datetime.fromisoformat(row["verified_at"]),
        price_magnifier=int(row["price_magnifier"]),
        isin=row["isin"],
    )


def split_ticker(ticker: str) -> tuple[str, str]:
    """``("BRK-B", "US")`` for ``BRK-B.US``."""
    base, dot, suffix = ticker.rpartition(".")
    if not dot or not base or not suffix:
        raise UnsupportedTickerError(f"{ticker!r} has no exchange suffix")
    return base, suffix.upper()


def ib_symbol(base: str, market: Market) -> str:
    """Our symbol as IBKR writes it: ``BRK-B`` is ``BRK B`` in the US."""
    return base.upper().replace("-", market.class_separator)


def build_query(
    ticker: str, profile: InstrumentProfile | None, *, by_isin: bool
) -> IbContractQuery:
    base, suffix = split_ticker(ticker)
    market = MARKETS.get(suffix)
    if market is None:
        raise UnsupportedTickerError(f"{ticker}: exchange {suffix} does not trade through IBKR")
    primary = market.primary_exchange
    if primary is None and profile is not None and profile.exchange:
        primary = US_PRIMARY.get(profile.exchange.upper())
    currency = market.currency
    if profile is not None and profile.currency and profile.currency.upper() != "GBX":
        currency = profile.currency.upper()
    isin = profile.isin if (by_isin and profile is not None and profile.isin) else None
    return IbContractQuery(
        symbol=ib_symbol(base, market),
        currency=currency,
        primary_exchange=primary,
        isin=isin,
    )


class ContractResolver:
    def __init__(
        self,
        client: IbClient,
        *,
        cache: ContractCache | None = None,
        lookup: InstrumentLookup | None = None,
        clock: Clock = SYSTEM_CLOCK,
        max_age: timedelta = timedelta(days=7),
    ) -> None:
        self.client = client
        self.cache: ContractCache = cache if cache is not None else MemoryContractCache()
        self.lookup = lookup
        self.clock = clock
        self.max_age = max_age

    def resolve(self, ticker: str) -> ResolvedContract:
        cached = self.cache.get(ticker)
        if cached is not None and self.clock.now() - cached.verified_at < self.max_age:
            return cached
        resolved = self._look_up(ticker)
        self.cache.put(resolved)
        if cached is not None and cached.con_id != resolved.con_id:
            _log.warning(
                "ibkr.contract.changed", ticker=ticker, old=cached.con_id, new=resolved.con_id
            )
        return resolved

    def spec(self, ticker: str) -> InstrumentSpec:
        return self.resolve(ticker).spec()

    def ticker_for(self, con_id: int) -> str | None:
        """Our ticker for an IBKR contract, when this cache resolved it."""
        found = self.cache.by_con_id(con_id)
        return found.ticker if found is not None else None

    # ---- internals ------------------------------------------------------------

    def _look_up(self, ticker: str) -> ResolvedContract:
        profile = self.lookup(ticker) if self.lookup is not None else None
        symbol_query = build_query(ticker, profile, by_isin=False)
        matches: list[IbContractDetails] = []
        if profile is not None and profile.isin:
            matches = self._matches(build_query(ticker, profile, by_isin=True), symbol_query)
        if not matches:
            matches = self._matches(symbol_query, symbol_query)
        if len(matches) != 1:
            why = "no IBKR contract" if not matches else f"{len(matches)} IBKR contracts"
            _log.warning("ibkr.contract.refused", ticker=ticker, matches=len(matches))
            raise UnsupportedTickerError(
                f"{ticker}: {why} match (symbol {symbol_query.symbol}, "
                f"{symbol_query.currency}); refusing to guess"
            )
        found = matches[0]
        return ResolvedContract(
            ticker=ticker,
            contract=found.contract,
            min_tick=found.min_tick,
            verified_at=self.clock.now(),
            price_magnifier=max(1, found.price_magnifier),
            isin=found.isin or (profile.isin if profile is not None else None),
        )

    def _matches(
        self, query: IbContractQuery, expected: IbContractQuery
    ) -> list[IbContractDetails]:
        """Stocks in the expected currency (and primary exchange, when known)."""
        details = self._details(query)
        keep: list[IbContractDetails] = []
        for d in details:
            c = d.contract
            if c.sec_type != "STK" or c.currency != expected.currency:
                continue
            if (
                expected.primary_exchange is not None
                and c.primary_exchange is not None
                and c.primary_exchange != expected.primary_exchange
            ):
                continue
            keep.append(d)
        # one listing reached by several routes is still one contract
        unique = {d.contract.con_id: d for d in keep}
        return list(unique.values())

    def _details(self, query: IbContractQuery) -> Sequence[IbContractDetails]:
        try:
            return self.client.contract_details(query)
        except IbApiError as exc:
            if classify(exc.code) == "not_found":
                return []
            raise to_broker_error(exc, action="contract lookup") from exc
        except (ConnectionError, TimeoutError) as exc:
            raise to_broker_error(exc, action="contract lookup") from exc


def lake_instrument_lookup(lake: Any) -> InstrumentLookup:
    """An :data:`InstrumentLookup` over the lake's ``instruments`` table
    (ISIN, listing venue, currency). Missing rows give ``None``."""

    def lookup(ticker: str) -> InstrumentProfile | None:
        try:
            df = lake.sql("SELECT isin, exchange, currency FROM instruments WHERE id = ?", [ticker])
        except Exception as exc:  # a lake without the table or column
            _log.warning("ibkr.contract.lookup_failed", ticker=ticker, error=str(exc))
            return None
        if df.empty:
            return None
        row = df.iloc[0]

        def text(value: Any) -> str | None:
            return str(value) if isinstance(value, str) and value else None

        return InstrumentProfile(
            ticker=ticker,
            isin=text(row["isin"]),
            exchange=text(row["exchange"]),
            currency=text(row["currency"]),
        )

    return lookup
