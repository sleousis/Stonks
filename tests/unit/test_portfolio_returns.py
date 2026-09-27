"""Daily return history for the covariance constructors (roadmap 9.5.1, P12).

``returns_lookback`` says how many rows a book's constructor reads, and
``market_history`` builds them from adjusted closes through a point-in-time
view, so nothing after the decision is ever read."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.core.types import Portfolio
from stonks.portfolio.pipeline import BookInput, MarketView, _construction_input
from stonks.portfolio.returns import market_history, returns_lookback
from stonks.portfolio.settings import ConstructionSettings
from stonks.store.lake import DuckDBLake
from stonks.store.pit import PitSession

DATES = pd.bdate_range("2025-01-01", periods=120)
AS_OF = DATES[89].date()


def _lake(future: bool) -> DuckDBLake:
    rng = np.random.default_rng(11)
    lake = DuckDBLake(Path(":memory:"))
    lake.migrate()
    rows = []
    for ticker in ("A.US", "B.US"):
        close = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.01, len(DATES))))
        n = len(DATES) if future else 90
        if future:
            close[90:] *= 5.0  # a jump no decision on AS_OF may see
        rows.append(
            pd.DataFrame(
                {
                    "ticker": ticker,
                    "date": [d.date() for d in DATES[:n]],
                    "open": close[:n],
                    "high": close[:n],
                    "low": close[:n],
                    "close": close[:n],
                    # a 2:1 split adjustment the returns must follow
                    "adj_close": close[:n] / 2.0,
                    "volume": np.arange(n, dtype=float) + 1_000.0,
                }
            )
        )
    lake.upsert_prices(pd.concat(rows, ignore_index=True))
    return lake


@pytest.fixture(scope="module")
def lakes():
    past, planted = _lake(False), _lake(True)
    yield past, planted
    past.close()
    planted.close()


def _view(lake, as_of=AS_OF):
    return PitSession(lake).at(as_of, decision_interval=Interval.DAY_1)


@pytest.mark.parametrize(
    ("method", "params", "expected"),
    [
        ("single_winner", {}, None),
        ("equal_weight_top_n", {}, None),
        ("inverse_vol", {}, None),
        ("hrp", {}, 252),
        ("erc", {"lookback": 40}, 40),
        ("mean_variance_costs", {"lookback": 30}, 30),
    ],
)
def test_returns_lookback_comes_from_the_constructor_settings(method, params, expected):
    assert returns_lookback(ConstructionSettings(method=method, params=params)) == expected


def test_returns_are_daily_adjusted_close_changes_up_to_the_decision(lakes):
    past, _ = lakes
    history = market_history(_view(past), ["A.US", "B.US"], lookback=20)
    assert history.returns is not None
    assert list(history.returns.columns) == ["A.US", "B.US"]
    assert len(history.returns) == 20
    assert history.returns.index.max().date() == AS_OF
    bars = past.get_prices("A.US", DATES[0].date(), AS_OF)
    adj = bars["adj_close"].to_numpy()
    assert history.returns["A.US"].iloc[-1] == pytest.approx(adj[-1] / adj[-2] - 1.0)
    # the decision bar's volume feeds the impact term
    assert history.volumes == {"A.US": 1_089.0, "B.US": 1_089.0}


def test_no_row_after_the_decision_is_read(lakes):
    past, planted = lakes
    clean = market_history(_view(past), ["A.US", "B.US"], lookback=60)
    seen = market_history(_view(planted), ["A.US", "B.US"], lookback=60)
    assert seen.returns is not None and clean.returns is not None
    assert seen.returns.index.max().date() <= AS_OF
    pd.testing.assert_frame_equal(seen.returns, clean.returns)
    assert seen.volumes == clean.volumes


def test_tickers_without_bars_are_left_out(lakes):
    past, _ = lakes
    history = market_history(_view(past), ["A.US", "NONE.US"], lookback=10)
    assert history.returns is not None
    assert list(history.returns.columns) == ["A.US"]
    assert market_history(_view(past), ["NONE.US"], lookback=10).returns is None
    assert market_history(_view(past), [], lookback=10).returns is None


def test_the_pipeline_hands_volumes_and_returns_to_the_constructor():
    returns = pd.DataFrame({"A.US": [0.01, -0.02]}, index=pd.bdate_range("2025-01-01", periods=2))
    market = MarketView(
        as_of=date(2025, 1, 2),
        prices={"A.US": 10.0},
        volumes={"A.US": 5_000.0},
        returns_history=returns,
    )
    book = BookInput(portfolio=Portfolio(cash=1_000.0))
    inp = _construction_input({"s": {"A.US": 1.0}}, book, market)
    assert inp.volumes == {"A.US": 5_000.0}
    assert inp.returns_history is returns
