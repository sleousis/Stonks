"""TickRequest: when a tick is scoped to its own tickers (TO-04)."""

from __future__ import annotations

import pytest

from stonks.app.ticks import TickRequest


@pytest.mark.parametrize(
    ("request_", "scoped"),
    [
        (TickRequest(), False),
        (TickRequest(tickers=["BTC-USD.CC"]), True),
        (TickRequest(asset_class="crypto"), True),
        # the scheduler sends the configured universe as tickers, unscoped
        (TickRequest(tickers=["AAPL.US"], scoped=False), False),
        (TickRequest(scoped=True), True),
    ],
)
def test_explicit_tickers_scope_the_tick_unless_told_otherwise(request_, scoped):
    assert request_.is_scoped is scoped
