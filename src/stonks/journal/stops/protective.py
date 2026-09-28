"""The protective stop Stonks placed for an entry (roadmap 19.10). A stop's
decision context names its ``entry_client_id`` and its client id is the
entry's plus ``:stop``. The first one placed is the trade's initial stop:
a later resize keeps or moves it, but the risk taken was the first."""

from __future__ import annotations

from typing import TYPE_CHECKING

from stonks.journal.stops import FoundStop, StopSource, register_stop_source

if TYPE_CHECKING:
    from stonks.journal.trips import Ledger, LedgerOrder


@register_stop_source
class ProtectiveStop(StopSource):
    name = "protective_stop"
    priority = 20

    def find(self, entry: LedgerOrder, ledger: Ledger) -> FoundStop | None:
        prefix = f"{entry.client_id}:stop"
        candidates = [
            o
            for o in ledger.stop_orders
            if o.stop_price is not None
            and o.side != entry.side
            and (
                o.context.get("entry_client_id") == entry.client_id
                or o.client_id == prefix
                or o.client_id.startswith(f"{prefix}:")
            )
        ]
        if not candidates:
            return None
        first = min(candidates, key=lambda o: (o.created_at, o.client_id))
        assert first.stop_price is not None
        return FoundStop(first.stop_price, self.name)
