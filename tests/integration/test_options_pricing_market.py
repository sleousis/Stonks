"""Treasury rates and known dividends in the options backtest (roadmap
17.7): ``OptionMarketData.from_lake`` loads the curve and the dividend
forecast, the engine prices with them, and data dated after a day never
changes what the backtest did on that day (P12).
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, timedelta

import pandas as pd

from stonks.backtest.options_engine import (
    OptionMarketData,
    OptionsBacktestConfig,
    OptionsBacktester,
)
from stonks.options.chain import OptionQuote, snapshots
from stonks.options.dividends import DividendRecord, KnownDividendForecast
from stonks.options.rates import (
    RateCurveSettings,
    TreasuryRateCurve,
    bond_equivalent_to_continuous,
)
from stonks.options.strategies import resolve_option_strategy
from stonks.options.synthetic import SyntheticChainSpec, SyntheticOptionSource

START = date(2025, 1, 2)
STRIKES = [float(k) for k in range(60, 165, 5)]


def business_days(n: int) -> list[date]:
    out, d = [], START
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = business_days(120)
CLOSES = {
    d: round(100.0 * math.exp(0.0008 * i + 0.01 * math.sin(i / 3.0)), 2) for i, d in enumerate(DAYS)
}
MID = DAYS[60]


def base_data() -> OptionMarketData:
    src = SyntheticOptionSource(
        {"X.US": CLOSES},
        SyntheticChainSpec(vol=0.25, horizon_days=70),
        fixed_strikes={"X.US": STRIKES},
    )
    quotes = [
        OptionQuote(
            r.contract,
            r.as_of,
            r.bid,
            r.ask,
            r.last,
            r.volume,
            r.open_interest,
            r.underlying_price,
            r.iv,
            r.delta,
        )
        for r in src.fetch_option_quotes("X.US")
    ]
    return OptionMarketData(closes={"X.US": CLOSES}, chains=snapshots(quotes))


def curve(level: float, shocked_after: date | None = None, shock: float = 0.0) -> TreasuryRateCurve:
    points = {
        tenor: [
            (d, level + (shock if shocked_after is not None and d > shocked_after else 0.0))
            for d in DAYS
        ]
        for tenor in (0.25, 1.0)
    }
    return TreasuryRateCurve(points)


def dividends(shocked_after: date | None = None) -> KnownDividendForecast:
    """A quarterly 0.8 dividend declared a month ahead; after the shock day
    every newly declared one is 8.0."""
    rows = []
    for ex in (DAYS[20], DAYS[80], DAYS[110]):
        declared = ex - timedelta(days=30)
        amount = 8.0 if shocked_after is not None and declared > shocked_after else 0.8
        rows.append(DividendRecord(ex, amount, declared))
    return KnownDividendForecast({"X.US": rows})


def config(**kw) -> OptionsBacktestConfig:
    return OptionsBacktestConfig(
        **{"start": DAYS[0], "end": DAYS[-1], "underlyings": ["X.US"], **kw}
    )


def run(data: OptionMarketData, **kw):
    cls = resolve_option_strategy("covered_call")
    return OptionsBacktester(cls(), data, config(drop_quote_days=0.3, **kw)).run()


def test_the_rate_curve_moves_model_marks():
    data = base_data()
    low = run(replace(data, rates=curve(0.0)))
    high = run(replace(data, rates=curve(20.0)))
    assert low.report.equity_curve != high.report.equity_curve


def test_no_curve_keeps_the_flat_configured_rate():
    data = base_data()
    flat = run(data, rate=0.0)
    zero_curve = run(replace(data, rates=curve(0.0)))
    assert flat.report.equity_curve == zero_curve.report.equity_curve


def test_data_dated_after_a_day_does_not_change_that_day():
    data = replace(base_data(), rates=curve(4.0), dividends=dividends())
    shocked = replace(
        base_data(), rates=curve(4.0, MID, shock=15.0), dividends=dividends(shocked_after=MID)
    )
    a, b = run(data), run(shocked)
    cut = DAYS.index(MID) + 1
    assert a.report.equity_curve[:cut] == b.report.equity_curve[:cut]
    fills_a = [f for f in a.fills if f.as_of <= MID]
    fills_b = [f for f in b.fills if f.as_of <= MID]
    assert fills_a == fills_b
    # the shock is real: it changes the run after the cut
    assert a.report.equity_curve[cut:] != b.report.equity_curve[cut:]


def test_from_lake_loads_the_curve_and_the_dividends(lake):
    days = DAYS[:25]
    lake.upsert_prices(
        pd.DataFrame(
            [
                {
                    "ticker": "X.US",
                    "date": d,
                    "open": CLOSES[d],
                    "high": CLOSES[d],
                    "low": CLOSES[d],
                    "close": CLOSES[d],
                    "adj_close": CLOSES[d],
                    "volume": 1e6,
                }
                for d in days
            ]
        )
    )
    lake.upsert_bond_yields(
        pd.DataFrame(
            {
                "ticker": ["US3M.GBOND"] * len(days),
                "date": days,
                "yield_to_maturity": [4.2] * len(days),
                "clean_price": [None] * len(days),
            }
        )
    )
    lake.upsert_dividends(
        pd.DataFrame(
            {
                "ticker": ["X.US"],
                "ex_date": [days[15]],
                "amount": [0.8],
                "currency": ["USD"],
                "pay_date": [None],
                "record_date": [None],
                "declaration_date": [days[2]],
            }
        )
    )
    data = OptionMarketData.from_lake(
        lake,
        ["X.US"],
        days[0],
        days[-1],
        rate_settings=RateCurveSettings(tickers={"US3M.GBOND": 0.25}),
    )
    assert isinstance(data.rates, TreasuryRateCurve)
    assert data.rates.zero_rate(days[5], 0.1) == bond_equivalent_to_continuous(4.2)
    assert isinstance(data.dividends, KnownDividendForecast)
    assert data.dividends.known_dividends("X.US", days[5], days[-1]) == [(days[15], 0.8)]
    assert data.dividends.known_dividends("X.US", days[1], days[-1]) == []
