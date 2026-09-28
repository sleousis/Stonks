"""A live broker chain behind the option chain seam (roadmap 17.8).

:class:`BrokerOptionChainSource` is a :class:`DataSource` whose only
capability is ``fetch_option_quotes``: today's chain, read from a broker
that offers :class:`~stonks.options.live.broker.OptionBroker` (IBKR with
the OPRA market data add-on). ``stonks options ingest`` and the live
options job read it like the EODHD source, rows in the same columns.

A broker holds no history: a range that does not include today returns
nothing. Everything else a ``DataSource`` offers is unsupported.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

from stonks.core.clock import SYSTEM_CLOCK, Clock, today
from stonks.ingest.option_schemas import OptionQuoteRow
from stonks.ingest.schemas import FinancialStatementsBundle, RawPriceBar
from stonks.ingest.sources.base import DataSource, UnsupportedCapabilityError
from stonks.options.live.broker import OptionBroker


class BrokerOptionChainSource(DataSource):
    def __init__(
        self,
        broker: OptionBroker,
        *,
        source_id: str = "ibkr",
        max_expiry_days: int = 120,
        strike_band: float = 0.3,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.broker = broker
        self.source_id = source_id
        self.max_expiry_days = max_expiry_days
        self.strike_band = strike_band
        self.clock = clock

    def list_tickers(self, exchange: str) -> list[str]:
        raise UnsupportedCapabilityError(f"{self.source_id} lists no tickers ({exchange})")

    def fetch_prices(
        self, ticker: str, since: date | None = None, until: date | None = None
    ) -> Iterable[RawPriceBar]:
        raise UnsupportedCapabilityError(f"{self.source_id} serves no bars ({ticker})")

    def fetch_fundamentals(self, ticker: str) -> FinancialStatementsBundle:
        raise UnsupportedCapabilityError(f"{self.source_id} serves no statements ({ticker})")

    def fetch_option_quotes(
        self, underlying: str, since: date | None = None, until: date | None = None
    ) -> Iterable[OptionQuoteRow]:
        day = today(self.clock)
        if (since is not None and since > day) or (until is not None and until < day):
            return []
        return list(
            self.broker.option_chain(
                underlying,
                day,
                max_expiry_days=self.max_expiry_days,
                strike_band=self.strike_band,
            )
        )
