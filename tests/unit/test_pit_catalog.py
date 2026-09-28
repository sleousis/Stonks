"""Every catalogued strategy is blind to the future behind a point-in-time
lake (BL-49).

Two lakes share the same past. The planted one also holds future rows of
every kind a strategy may read: bars after the decision (with prices that
would flip any signal), a statement filed later, a later macro print,
share count, split and dividend, a bond yield, a TVL print and a new ticker.
Each strategy answers on a ``PointInTimeLake`` of both at the decision day,
and again when it is asked about a later day by mistake: the answers must
match.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.lab.catalog import strategy_catalog
from stonks.store.lake import DuckDBLake
from stonks.store.pit import PitSession
from stonks.strategies._common import decision_interval

TICKERS = ["A.US", "B.US", "C.US", "SPY.US"]
N_PAST = 420
DATES = pd.bdate_range("2023-01-02", periods=N_PAST + 30)
D = DATES[N_PAST - 1].to_pydatetime()  # the decision day
LATER = DATES[-1].to_pydatetime()


def _bars(future: bool) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    frames = []
    n = len(DATES) if future else N_PAST
    for i, ticker in enumerate(TICKERS):
        close = (40.0 + 15 * i) * np.exp(np.cumsum(rng.normal(0.0006, 0.013, len(DATES))))
        close = close.copy()
        close[N_PAST:] *= 3.0 if i % 2 == 0 else 0.2  # a future that would flip signals
        frames.append(
            pd.DataFrame(
                {
                    "ticker": ticker,
                    "date": [d.date() for d in DATES[:n]],
                    "open": close[:n],
                    "high": close[:n] * 1.01,
                    "low": close[:n] * 0.99,
                    "close": close[:n],
                    "adj_close": close[:n],
                    "volume": 1_000_000.0,
                }
            )
        )
    if future:
        frames.append(
            pd.DataFrame(
                {
                    "ticker": "NEW.US",
                    "date": [d.date() for d in DATES[N_PAST:]],
                    "open": 10.0,
                    "high": 10.0,
                    "low": 10.0,
                    "close": 10.0,
                    "adj_close": 10.0,
                    "volume": 1e6,
                }
            )
        )
    return pd.concat(frames, ignore_index=True)


def _statements(lake: DuckDBLake, future: bool) -> None:
    ends = [date(2022, 3, 31) + pd.DateOffset(months=3 * k) for k in range(8)]
    rows = []
    for ticker in TICKERS[:3]:
        for k, end in enumerate(ends):
            rows.append(
                {
                    "ticker": ticker,
                    "period_end": end.date(),
                    "frequency": "Q",
                    "filing_date": (end + pd.DateOffset(days=40)).date(),
                    "revenue": 100.0 + k,
                    "net_income": 10.0 + k,
                    "ebit": 12.0 + k,
                    "operating_income": 12.0 + k,
                    "gross_profit": 50.0 + k,
                    "total_assets": 500.0,
                    "total_stockholder_equity": 200.0,
                    "common_stock_shares_outstanding": 1e6,
                }
            )
        if future:
            rows.append(
                {
                    "ticker": ticker,
                    "period_end": D.date() - timedelta(days=5),
                    "frequency": "Q",
                    "filing_date": D.date() + timedelta(days=3),
                    "revenue": 1e6,
                    "net_income": -1e6,
                    "ebit": -1e6,
                    "operating_income": -1e6,
                    "gross_profit": -1e6,
                    "total_assets": 1.0,
                    "total_stockholder_equity": -1.0,
                    "common_stock_shares_outstanding": 1.0,
                }
            )
    frame = pd.DataFrame(rows)
    inc = ["ticker", "period_end", "frequency", "filing_date", "revenue", "net_income"]
    lake.upsert_income_statement(frame[[*inc, "ebit", "operating_income", "gross_profit"]])
    lake.upsert_balance_sheet(
        frame[
            [
                "ticker",
                "period_end",
                "frequency",
                "filing_date",
                "total_assets",
                "total_stockholder_equity",
                "common_stock_shares_outstanding",
            ]
        ]
    )


def _metadata(lake: DuckDBLake, future: bool) -> None:
    past = D.date() - timedelta(days=60)
    later = D.date() + timedelta(days=2)
    days = [past, later] if future else [past]
    lake.upsert_shares_outstanding(
        pd.DataFrame({"ticker": "A.US", "date": days, "shares": [1e6, 1.0][: len(days)]})
    )
    lake.upsert_macro_indicators(
        pd.DataFrame(
            {
                "country_iso": "USA",
                "indicator": ["unemployment_rate", "vix", "vix_3m"] * len(days),
                "observation_date": [d for d in days for _ in range(3)],
                "period": "monthly",
                "country_name": "United States",
                "value": [
                    v for v in ([4.0, 15.0, 17.0], [99.0, 90.0, 10.0])[: len(days)] for v in v
                ],
            }
        )
    )
    lake.upsert_bond_yields(
        pd.DataFrame(
            {
                "ticker": ["US10Y.GBOND", "US3M.GBOND"] * len(days),
                "date": [d for d in days for _ in range(2)],
                "yield_to_maturity": [v for v in ([4.0, 3.0], [1.0, 9.0])[: len(days)] for v in v],
                "clean_price": None,
            }
        )
    )
    lake.upsert_defi_tvl(
        pd.DataFrame(
            {
                "chain": "Ethereum",
                "observation_date": days,
                "tvl_usd": [1e9, 1.0][: len(days)],
                "source": "t",
            }
        )
    )
    if future:
        lake.upsert_stock_splits(
            pd.DataFrame({"ticker": ["B.US"], "date": [later], "ratio": [4.0]})
        )
        lake.upsert_dividends(
            pd.DataFrame(
                {
                    "ticker": ["C.US"],
                    "ex_date": [later],
                    "amount": [5.0],
                    "currency": "USD",
                    "pay_date": None,
                    "record_date": None,
                    "declaration_date": None,
                }
            )
        )


def _lake(future: bool) -> DuckDBLake:
    lake = DuckDBLake(Path(":memory:"))
    lake.migrate()
    lake.upsert_prices(_bars(future))
    _statements(lake, future)
    _metadata(lake, future)
    for ticker in [*TICKERS, "NEW.US"]:
        lake.con.execute(
            "INSERT INTO instruments (id, asset_class, sector) VALUES (?, 'equity', 'Tech')",
            [ticker],
        )
    return lake


@pytest.fixture(scope="module")
def lakes():
    past, planted = _lake(False), _lake(True)
    yield past, planted
    past.close()
    planted.close()


def _factory(cls):
    names = {s.name for s in cls.parameter_spec()}
    params = {}
    if "ticker" in names:
        params["ticker"] = "A.US"
    if "interval" in names:
        # an intraday-only strategy keeps its own minute interval
        spec = next(s for s in cls.parameter_spec() if s.name == "interval")
        daily = spec.bounds is None or "1d" in spec.bounds
        params["interval"] = "1d" if daily else spec.default
    if "universe" in names:
        params["universe"] = "A.US,B.US,C.US"
    return lambda: cls(params)


def _answers(factory, lake, asked):
    view = PitSession(lake).at(D, decision_interval=Interval.DAY_1)
    strategy = factory()
    out = {}
    with decision_interval(Interval.DAY_1):
        for ticker in TICKERS:
            try:
                out[ticker] = strategy.estimate_return(ticker, asked, view)
            except Exception as exc:  # a strategy may refuse a daily-only lake
                out[ticker] = f"error: {type(exc).__name__}"
    return out


def test_the_planted_rows_would_change_a_raw_read(lakes):
    past, planted = lakes
    assert len(planted.get_bars("A.US", Interval.DAY_1, DATES[0], DATES[-1])) > len(
        past.get_bars("A.US", Interval.DAY_1, DATES[0], DATES[-1])
    )
    assert len(planted.get_statement_history("income_statement", "A.US")) == 9


@pytest.mark.parametrize("name", sorted(strategy_catalog()))
def test_catalog_strategy_is_blind_to_planted_future_rows(name, lakes):
    past, planted = lakes
    factory = _factory(strategy_catalog()[name])
    assert _answers(factory, past, D) == _answers(factory, planted, D)


@pytest.mark.parametrize("name", sorted(strategy_catalog()))
def test_asking_about_a_later_day_by_mistake_still_reads_no_future(name, lakes):
    past, planted = lakes
    factory = _factory(strategy_catalog()[name])
    assert _answers(factory, past, LATER) == _answers(factory, planted, LATER)


def _raw_answers(factory, lake, asked):
    strategy = factory()
    out = {}
    with decision_interval(Interval.DAY_1):
        for ticker in TICKERS:
            try:
                out[ticker] = strategy.estimate_return(ticker, asked, lake)
            except Exception as exc:
                out[ticker] = f"error: {type(exc).__name__}"
    return out


@pytest.mark.parametrize("name", sorted(strategy_catalog()))
def test_the_proxy_changes_nothing_when_the_lake_holds_no_future(name, lakes):
    past, _ = lakes
    factory = _factory(strategy_catalog()[name])
    assert _answers(factory, past, D) == _raw_answers(factory, past, D)


_LAKE_UNIVERSE = sorted(
    name
    for name, cls in strategy_catalog().items()
    if "universe" in {s.name for s in cls.parameter_spec()}
)


@pytest.mark.parametrize("name", _LAKE_UNIVERSE)
def test_a_lake_wide_universe_never_lists_a_future_name(name, lakes):
    past, planted = lakes
    cls = strategy_catalog()[name]
    base = _factory(cls)

    def factory():
        strategy = base()
        strategy.params["universe"] = ""  # the lake's own names
        return strategy

    assert _answers(factory, past, D) == _answers(factory, planted, D)
