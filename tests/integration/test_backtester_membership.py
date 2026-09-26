"""RS-05: a backtest on a stored universe trades each name only while it is a
member (point in time, P14). A name that joins later can't be bought early,
and a holding that leaves the universe is sold."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pandas as pd
import pytest

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.types import Order, Portfolio
from stonks.lab.backtesting import run_backtest
from stonks.lab.dataset import LabDataset
from stonks.lab.preflight import run_preflight
from stonks.store.lake import DuckDBLake

UNIVERSE_ID = "idx"
DAYS = [d.date() for d in pd.bdate_range("2026-01-05", periods=40)]
JOIN = DAYS[20]
LEAVE = DAYS[25]


class _BuyEverything:
    """Scores every ticker 1.0 and buys 10 shares of each pick not held."""

    id = "buy_everything"
    applicable_asset_classes = ("equity",)
    label_horizon_bars = 0
    required_history_bars = 0

    def __init__(self, params=None) -> None:
        self.params = dict(params or {})

    def estimate_return(self, ticker, as_of, lake):
        return 1.0

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of,
    ) -> list[Order]:
        return [
            Order(
                client_id=f"buy:{t}:{as_of.isoformat()}",
                ticker=t,
                side="buy",
                quantity=10.0,
                order_type="market",
            )
            for _, t in my_picks
            if portfolio.positions.get(t, 0.0) <= 0
        ]

    def fit(self, dataset) -> None:
        return None


@pytest.fixture
def lake(tmp_path):
    lk = DuckDBLake(tmp_path / "lake.duckdb")
    lk.migrate()
    rows = [
        {
            "ticker": t,
            "date": d,
            "open": 10.0,
            "high": 10.5,
            "low": 9.5,
            "close": 10.0,
            "adj_close": 10.0,
            "volume": 1e6,
        }
        for t in ("A.US", "B.US", "C.US")
        for d in DAYS
    ]
    lk.upsert_prices(pd.DataFrame(rows))
    lk.upsert_universe_membership(
        pd.DataFrame(
            [
                {"universe_id": UNIVERSE_ID, "ticker": "A.US", "start_date": DAYS[0]},
                {"universe_id": UNIVERSE_ID, "ticker": "B.US", "start_date": JOIN},
                {
                    "universe_id": UNIVERSE_ID,
                    "ticker": "C.US",
                    "start_date": DAYS[0],
                    "end_date": LEAVE,
                },
            ]
        )
    )
    yield lk
    lk.close()


def _run(lake, **config):
    broker = SimulatedBroker(Portfolio(cash=1e6))
    cfg = BacktestConfig(start=DAYS[0], end=DAYS[-1], universe=["A.US", "B.US", "C.US"], **config)
    Backtester([_BuyEverything()], broker, lake, cfg).run()
    return broker


def _first_fill(broker, ticker, side="buy"):
    fills = [f for f in broker.fills if f.ticker == ticker and f.side == side]
    return fills[0].filled_at.date() if fills else None


def test_a_name_that_joins_later_is_never_bought_before_it_joins(lake):
    broker = _run(lake, universe_id=UNIVERSE_ID)
    assert _first_fill(broker, "A.US") == DAYS[1]
    assert _first_fill(broker, "B.US") > JOIN


def test_a_holding_that_leaves_the_universe_is_sold(lake):
    broker = _run(lake, universe_id=UNIVERSE_ID)
    sold = _first_fill(broker, "C.US", side="sell")
    assert sold is not None and LEAVE < sold <= DAYS[27]
    assert broker.fetch_portfolio().positions.get("C.US", 0.0) == pytest.approx(0.0)
    # and it is never bought back once it has left
    buys = [f for f in broker.fills if f.ticker == "C.US" and f.side == "buy"]
    assert all(f.filled_at.date() <= LEAVE for f in buys)


def test_without_a_universe_id_every_ticker_trades_from_the_start(lake):
    broker = _run(lake)
    assert _first_fill(broker, "B.US") == DAYS[1]


def test_the_pipeline_honours_membership_too(lake):
    broker = _run(lake, universe_id=UNIVERSE_ID, construction="equal_weight_top_n")
    assert _first_fill(broker, "B.US") > JOIN
    assert broker.fetch_portfolio().positions.get("C.US", 0.0) == pytest.approx(0.0)


def test_lab_backtests_use_the_dataset_universe_id(lake):
    ds = LabDataset(
        lake=lake,
        universe=["A.US", "B.US", "C.US"],
        start=DAYS[0],
        end=DAYS[-1],
        universe_id=UNIVERSE_ID,
        benchmark="none",
    )
    report = run_backtest(_BuyEverything(), ds, ds.full_window)
    entries = {t.ticker: t.entry_ts.date() for t in report.trades}
    assert entries["B.US"] > JOIN


def test_preflight_warns_about_tickers_that_are_never_members(lake):
    ds = LabDataset(
        lake=lake,
        universe=["A.US", "B.US", "Z.US"],
        start=DAYS[0],
        end=DAYS[-1],
        universe_id=UNIVERSE_ID,
    )
    report = run_preflight(ds)
    issue = next(i for i in report.issues if i.code == "not_members")
    assert issue.details["tickers"] == ["Z.US"]


def test_preflight_notes_membership_is_enforced_point_in_time(lake):
    ds = LabDataset(
        lake=lake,
        universe=["A.US", "B.US", "C.US"],
        start=DAYS[0],
        end=DAYS[-1],
        universe_id=UNIVERSE_ID,
    )
    codes = {i.code for i in run_preflight(ds).issues}
    assert "static_universe" not in codes
    assert "not_members" not in codes
