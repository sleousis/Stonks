"""Market breadth (roadmap 23.14): advances and declines, the share above the
50 and 200 day averages, new highs and lows, and distribution days on the
index, computed from daily bars with plain words. Display only."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from stonks.breadth import BreadthSettings, market_breadth

DAYS = pd.bdate_range("2025-01-01", periods=260)


def _bars(ticker: str, closes: list[float] | np.ndarray, volumes: list[float] | None = None):
    n = len(closes)
    return pd.DataFrame(
        {
            "ticker": ticker,
            "date": [d.date() for d in DAYS[-n:]],
            "close": list(closes),
            "volume": volumes if volumes is not None else [1_000.0] * n,
        }
    )


def _universe() -> pd.DataFrame:
    up = np.linspace(50, 150, 260)  # steady riser: new high today
    down = np.linspace(150, 50, 260)  # steady faller: new low today
    flat = np.full(260, 100.0)
    young = np.linspace(10, 11, 30)  # too short for the averages
    return pd.concat(
        [
            _bars("UP.US", up),
            _bars("DOWN.US", down),
            _bars("FLAT.US", flat),
            _bars("YOUNG.US", young),
        ]
    )


def test_counts_advances_declines_and_averages() -> None:
    b = market_breadth(_universe(), None, settings=BreadthSettings(index=None))
    assert b.as_of == DAYS[-1].date()
    assert b.members == 4
    assert (b.advancers, b.decliners, b.unchanged) == (2, 1, 1)
    assert b.advance_decline_ratio == pytest.approx(2.0)
    assert b.above_50.eligible == 3  # YOUNG has 30 bars
    assert b.above_50.count == 1
    assert b.above_50.pct == pytest.approx(1 / 3)
    assert b.above_200.eligible == 3
    assert (b.new_highs, b.new_lows) == (1, 1)
    assert b.distribution_days is None
    assert b.lines and all(line.text and "—" not in line.text for line in b.lines)


def test_as_of_reads_no_later_bar() -> None:
    cut = DAYS[-10].date()
    b = market_breadth(_universe(), None, settings=BreadthSettings(index=None), as_of=cut)
    assert b.as_of == cut


def test_distribution_days_on_the_index() -> None:
    closes = [100.0] * 40
    volumes = [1_000.0] * 40
    # three down days on higher volume in the last 25 sessions, one down day
    # on lower volume, and one heavy down day too long ago
    moves = {-35: (0.02, 9_000), -20: (0.003, 5_000), -12: (0.01, 900), -8: (0.005, 1_500),
             -3: (0.01, 2_000)}  # fmt: skip
    for i, (drop, vol) in moves.items():
        closes[i] = closes[i - 1] * (1 - drop)
        volumes[i] = vol
        for j in range(i + 1, 0):
            closes[j] = closes[i]
    index = _bars("SPY.US", closes, volumes)
    b = market_breadth(_universe(), index, settings=BreadthSettings(index="SPY.US"))
    assert b.index == "SPY.US"
    assert b.distribution_days == 3
    assert len(b.distribution_dates) == 3
    assert any("distribution" in line.text for line in b.lines)


def test_empty_lake_gives_an_empty_card() -> None:
    empty = pd.DataFrame(columns=["ticker", "date", "close", "volume"])
    b = market_breadth(empty, None, settings=BreadthSettings(index=None))
    assert b.as_of is None and b.members == 0
    assert b.advance_decline_ratio is None and b.above_50.pct is None
    assert b.lines[0].text.startswith("No price data")


def test_settings_are_bounded() -> None:
    with pytest.raises(ValidationError):
        BreadthSettings(distribution_window=0)
    assert BreadthSettings().index == "SPY.US"
    assert date(2025, 1, 1) < DAYS[-1].date()
