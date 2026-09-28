"""FilingEvents: hold a stock for a few days after a chosen kind of company
filing (roadmap 23.13).

The events are current reports (8-K) with chosen item codes, read from the
lake's ``corporate_filings`` (``stonks ingest edgar``) by their SEC
acceptance time. A filing counts from the first decision made after its
acceptance (P12), and orders fill at the next open: an earnings release
accepted at 16:30 New York time, after the close, is traded the next
morning, never at a price from before it.

A ticker is a pick while its latest matching filing is at most
``hold_days`` calendar days old at the decision. That makes each filing an
entry for :mod:`stonks.lab.survival.event_study`, which measures the drift
after it against simply holding the same names (``stonks lab ic --strategy
filing_events --events``). The default asks the classic question: is there
drift after earnings releases (item 2.02)?

Decide: equal weight across the current picks, bought with the free cash,
and sell names that stop being picks.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from stonks.core.params import ParameterSpec
from stonks.core.types import Order, Portfolio
from stonks.ingest.filing_schemas import CURRENT_REPORT_ITEMS
from stonks.strategies.base import BaseStrategy


class FilingEvents(BaseStrategy):
    id = "filing_events"
    summary = "Holds a stock for a few days after a chosen kind of company filing, such as an earnings release."
    hypothesis = (
        "Investors underreact to news in company filings, so prices keep "
        "drifting for some days after a current report such as an earnings "
        "release (post-announcement drift, Ball and Brown 1968, Bernard and "
        "Thomas 1989). The sign of the drift depends on the news, so holding "
        "every filer long mostly tests whether filers beat non-filers. Fails "
        "when the news is priced within the day."
    )
    alpha_family = "sentiment"
    premise = "none"
    label_horizon_bars = 5

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(
                name="items",
                kind="categorical",
                default="2.02",
                bounds=None,
                tunable=False,
                description="Current report item codes that count as events, comma separated.",
            ),
            ParameterSpec(
                name="hold_days",
                kind="int",
                default=7,
                bounds=(1, 60),
                description="Calendar days a filing stays a pick after its acceptance.",
            ),
        ]

    def _items(self) -> list[str]:
        items = [i.strip() for i in str(self.params["items"]).split(",") if i.strip()]
        unknown = [i for i in items if i not in CURRENT_REPORT_ITEMS]
        if unknown:
            raise ValueError(f"unknown current report items {unknown}")
        return items

    def estimate_return(self, ticker: str, as_of: date, lake: Any) -> float | None:
        reader = getattr(lake, "get_corporate_filings", None)
        if not callable(reader):
            return None
        filings: Any = reader([ticker], items=self._items())
        if not isinstance(filings, pd.DataFrame) or filings.empty:
            return None
        known = pd.Series(pd.to_datetime(filings["known_at"]))
        # the engine and the tick pass a datetime: a date less one is a TypeError
        day = as_of.date() if isinstance(as_of, datetime) else as_of
        close = pd.Timestamp(datetime.combine(day, datetime.max.time()))
        seen = [
            date.fromisoformat(str(k)[:10])
            for k in known
            if pd.notna(k) and pd.Timestamp(k) <= close
        ]
        if not seen:
            return None
        age = day - max(seen)
        hold = int(self.params["hold_days"])
        if age > timedelta(days=hold):
            return None
        # fresher filings rank first, always positive while held
        return 1.0 - age.days / (hold + 1)

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of: date,
    ) -> list[Order]:
        picked = {t for _, t in my_picks}
        orders = [
            Order(
                client_id=f"{self.id}:sell:{t}:{as_of.isoformat()}",
                ticker=t,
                side="sell",
                quantity=qty,
                order_type="market",
                strategy_id=self.id,
            )
            for t, qty in portfolio.positions.items()
            if qty > 0 and t not in picked
        ]
        new = [t for t in sorted(picked) if portfolio.positions.get(t, 0.0) <= 0]
        if not new or portfolio.cash <= 0:
            return orders
        budget = portfolio.cash / len(new)
        for ticker in new:
            price = prices.get(ticker)
            if not price or price <= 0:
                continue
            orders.append(
                Order(
                    client_id=f"{self.id}:buy:{ticker}:{as_of.isoformat()}",
                    ticker=ticker,
                    side="buy",
                    quantity=budget / price,
                    order_type="market",
                    strategy_id=self.id,
                )
            )
        return orders
