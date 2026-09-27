"""Pure parts of the trader workspace services (roadmap 13): paper figures,
monthly returns, watchlist ticker cleaning and CSV cells."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from stonks.app.exports import filename, to_csv
from stonks.app.leaderboard import monthly_returns, paper_figures
from stonks.app.watchlists import WatchlistCreate, WatchlistUpdate
from stonks.production.pnl import PnlRow


def _rows(values: list[float], days: list[date]) -> list[PnlRow]:
    peak = 0.0
    out = []
    for i, (d, v) in enumerate(zip(days, values, strict=True)):
        peak = max(peak, v)
        prev = values[i - 1] if i else None
        out.append(
            PnlRow(
                day=d,
                total_value=v,
                daily_change=None if prev is None else v - prev,
                daily_return=None if prev is None else v / prev - 1,
                cumulative_return=v / values[0] - 1,
                drawdown=v / peak - 1,
            )
        )
    return out


def test_paper_figures_need_two_days():
    empty = paper_figures([], trades=0)
    assert empty.days == 0 and empty.sharpe is None and empty.latest_value is None
    one = paper_figures(_rows([100.0], [date(2026, 1, 2)]), trades=3)
    assert one.days == 1 and one.latest_value == 100.0 and one.total_return is None
    assert one.trades == 3


def test_paper_figures_of_a_curve():
    days = [date(2026, 1, d) for d in (2, 5, 6, 7)]
    figs = paper_figures(_rows([100.0, 102.0, 101.0, 104.0], days), trades=2)
    assert figs.total_return == pytest.approx(0.04)
    assert figs.max_drawdown == pytest.approx(101 / 102 - 1)
    assert figs.sharpe is not None and figs.sharpe > 0
    assert figs.volatility is not None and figs.cagr is not None


def test_monthly_returns_chain_month_ends():
    days = [date(2026, 1, 30), date(2026, 1, 31), date(2026, 2, 27), date(2026, 3, 31)]
    months = monthly_returns(_rows([100.0, 110.0, 99.0, 99.0], days))
    assert [m.month for m in months] == ["2026-01", "2026-02", "2026-03"]
    assert months[0].value == pytest.approx(0.10)
    assert months[1].value == pytest.approx(-0.10)
    assert months[2].value == pytest.approx(0.0)


def test_watchlist_tickers_are_cleaned():
    body = WatchlistCreate(name="  x ", tickers=["aapl.us", "AAPL.US", " ", "btc-usd.cc"])
    assert body.name == "x" and body.tickers == ["AAPL.US", "BTC-USD.CC"]
    assert WatchlistUpdate(tickers=None).tickers is None
    with pytest.raises(ValidationError):
        WatchlistCreate(name="x", tickers=["bad ticker"])
    with pytest.raises(ValidationError):
        WatchlistUpdate(name="   ")


def test_csv_cells_and_names():
    text = to_csv(["a"], [["+1"], [False], [[1, 2]]])
    assert text.splitlines() == ["a", "'+1", "false", '"[1, 2]"']
    assert filename("pnl", "", date(2026, 9, 27)) == "stonks-pnl-all-2026-09-27.csv"
