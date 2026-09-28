"""What a broker must offer to trade options live (roadmap 17.8).

:class:`OptionBroker` is an optional capability next to the ``Broker``
protocol, checked with ``isinstance`` like the others. ``IbkrBroker``
implements it. A broker without it never receives an option order.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Protocol, runtime_checkable

from stonks.core.combos import ComboOrder
from stonks.execution.brokers.base import MarginPreview
from stonks.ingest.option_schemas import OptionQuoteRow
from stonks.options.chain import OptionQuote
from stonks.options.live.events import OptionEvent


@runtime_checkable
class OptionBroker(Protocol):
    def place_combo(self, combo: ComboOrder) -> None:
        """Send a multi-leg order as one unit (all legs or none). Like
        ``place_order`` it never returns a fill."""
        ...

    def option_quotes(self, contract_ids: Sequence[str], as_of: date) -> Mapping[str, OptionQuote]:
        """Live quotes with the broker's Greeks, per share. A contract with
        no quote is left out."""
        ...

    def option_chain(
        self, underlying: str, as_of: date, *, max_expiry_days: int, strike_band: float
    ) -> Sequence[OptionQuoteRow]:
        """Today's chain of ``underlying`` within the horizon and the band."""
        ...

    def what_if_combo(self, combo: ComboOrder) -> MarginPreview:
        """The broker's margin preview of a combo. Nothing is sent."""
        ...

    def option_events(self) -> Sequence[OptionEvent]:
        """Assignments, exercises and expiries the broker reports."""
        ...
