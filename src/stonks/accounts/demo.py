"""The demo portfolio (roadmap 23.17): a sample book a new person can open
to see what Stonks shows, before any real data or money.

Everything here is computed from the synthetic random walk the Strategy
Studio smoke checks use (:func:`stonks.strategies.rules.sample.synthetic_bars`).
Nothing is written to ``portfolios``, orders, fills, snapshots or the
lake. Only the seed is stored (``demo_portfolios``), so the same person
sees the same sample each time, and removing it deletes one row.

The instruments are made up and end in ``.DEMO``, so they can never be
mistaken for a real listing or reach an order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from stonks.strategies.rules.sample import synthetic_bars

#: What every demo view says about itself.
DEMO_LABEL = "Sample data"
DEMO_NAME = "Demo portfolio"
#: Starting money of the sample book.
DEMO_START_VALUE = 100_000.0
#: Trading days of history in the sample.
DEMO_DAYS = 250


@dataclass(frozen=True)
class DemoInstrument:
    ticker: str
    name: str
    sector: str
    weight: float  # share of the starting money


DEMO_INSTRUMENTS: tuple[DemoInstrument, ...] = (
    DemoInstrument("ACME.DEMO", "Acme Robotics (sample)", "Technology", 0.22),
    DemoInstrument("BLUE.DEMO", "Bluewater Utilities (sample)", "Utilities", 0.16),
    DemoInstrument("CRST.DEMO", "Crest Health (sample)", "Health Care", 0.18),
    DemoInstrument("DUNE.DEMO", "Dune Energy (sample)", "Energy", 0.12),
    DemoInstrument("ELMS.DEMO", "Elm Street Retail (sample)", "Consumer", 0.14),
)


@dataclass(frozen=True)
class DemoPosition:
    ticker: str
    name: str
    sector: str
    quantity: float
    cost: float  # average price paid
    price: float  # last close
    value: float
    weight: float  # share of the total value
    pnl: float
    pnl_pct: float


@dataclass(frozen=True)
class DemoPoint:
    day: date
    value: float


@dataclass(frozen=True)
class DemoBook:
    name: str
    label: str
    seed: int
    as_of: date
    currency: str
    start_value: float
    cash: float
    total_value: float
    total_return: float
    day_change: float
    positions: tuple[DemoPosition, ...]
    curve: tuple[DemoPoint, ...]


def build_demo(*, seed: int, as_of: date) -> DemoBook:
    """The sample book for ``seed``, marked on the last weekday on or
    before ``as_of``. Pure: the same inputs give the same book."""
    last = pd.Timestamp(as_of)
    while last.weekday() >= 5:
        last -= pd.Timedelta(days=1)
    days = pd.bdate_range(end=last, periods=DEMO_DAYS)
    closes: dict[str, np.ndarray] = {}
    for i, inst in enumerate(DEMO_INSTRUMENTS):
        frame = synthetic_bars(DEMO_DAYS, seed=seed * 101 + i)
        closes[inst.ticker] = frame["close"].to_numpy(dtype=float)
    quantities = {
        inst.ticker: float(np.floor(DEMO_START_VALUE * inst.weight / closes[inst.ticker][0]))
        for inst in DEMO_INSTRUMENTS
    }
    cost = {t: float(c[0]) for t, c in closes.items()}
    cash = DEMO_START_VALUE - sum(quantities[t] * cost[t] for t in quantities)
    values = np.full(DEMO_DAYS, cash)
    for ticker, qty in quantities.items():
        values = values + qty * closes[ticker]
    total = float(values[-1])
    positions = []
    for inst in DEMO_INSTRUMENTS:
        qty = quantities[inst.ticker]
        price = float(closes[inst.ticker][-1])
        paid = cost[inst.ticker]
        positions.append(
            DemoPosition(
                ticker=inst.ticker,
                name=inst.name,
                sector=inst.sector,
                quantity=qty,
                cost=round(paid, 4),
                price=round(price, 4),
                value=qty * round(price, 4),
                weight=0.0,
                pnl=(round(price, 4) - round(paid, 4)) * qty,
                pnl_pct=price / paid - 1,
            )
        )
    invested = sum(p.value for p in positions)
    total = cash + invested
    positions = [DemoPosition(**{**p.__dict__, "weight": p.value / total}) for p in positions]
    curve = tuple(
        DemoPoint(day=d.date(), value=float(v)) for d, v in zip(days, values, strict=True)
    )
    curve = (*curve[:-1], DemoPoint(day=curve[-1].day, value=total))
    return DemoBook(
        name=DEMO_NAME,
        label=DEMO_LABEL,
        seed=seed,
        as_of=curve[-1].day,
        currency="USD",
        start_value=DEMO_START_VALUE,
        cash=cash,
        total_value=total,
        total_return=total / DEMO_START_VALUE - 1,
        day_change=total - float(values[-2]),
        positions=tuple(sorted(positions, key=lambda p: -p.value)),
        curve=curve,
    )
