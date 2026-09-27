"""The backtest report of a long/short book (roadmap 16.4): financing,
forced orders, exposure over time, long and short attribution."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.report import ExposurePoint, ShortBookReport
from stonks.backtest.shorting import ScaledBorrow, ShortingSettings
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.backtest.trades import with_trades
from stonks.core.types import Order, Portfolio
from stonks.execution.borrow import BorrowQuote, BorrowSettings, FlatBorrow
from stonks.execution.margin import MarginSettings
from tests.unit.trend_helpers import build_lake

DATES = pd.bdate_range("2024-01-01", periods=12)  # Mon 1 Jan .. Tue 16 Jan
FEE = 0.036  # 3.6 % a year: 0.01 % of the short's value per calendar day


class _ShortX:
    id = "short_x"
    supports_short = True

    def estimate_return(self, ticker, as_of, lake):
        return -1.0 if ticker == "X.US" else None

    def decide(self, my_picks, portfolio, prices, as_of):
        if portfolio.positions.get("X.US", 0.0) != 0.0:
            return []
        return [Order(f"{as_of:%Y%m%d}:X:sell", "X.US", "sell", 10.0, strategy_id=self.id)]


class _LongY(_ShortX):
    def estimate_return(self, ticker, as_of, lake):
        return 1.0 if ticker == "Y.US" else None

    def decide(self, my_picks, portfolio, prices, as_of):
        if portfolio.positions.get("Y.US", 0.0) != 0.0:
            return []
        return [Order(f"{as_of:%Y%m%d}:Y:buy", "Y.US", "buy", 10.0, strategy_id="long_y")]


@pytest.fixture(scope="module")
def lake(tmp_path_factory):
    series = {
        "X.US": np.full(len(DATES), 100.0),
        "Y.US": np.linspace(50.0, 55.0, len(DATES)),
    }
    db = build_lake(tmp_path_factory.mktemp("short_report") / "lake.duckdb", series, dates=DATES)
    yield db
    db.close()


def _broker(allow_short: bool) -> SimulatedBroker:
    if not allow_short:
        return SimulatedBroker(Portfolio(cash=100_000.0))
    borrow = FlatBorrow(BorrowSettings(fee_rate_annual={"equity": FEE}))
    return SimulatedBroker(
        Portfolio(cash=100_000.0),
        margin=MarginSettings(model="reg_t").build(),
        borrow=borrow,
        allow_short=True,
    )


def _config(allow_short: bool) -> BacktestConfig:
    return BacktestConfig(
        start=DATES[0].date(),
        end=DATES[-1].date(),
        universe=["X.US", "Y.US"],
        allow_short=allow_short,
    )


def test_long_only_report_has_no_short_book(lake):
    report = Backtester([_LongY()], _broker(False), lake, _config(False)).run()
    assert report.short_book is None


def test_borrow_fees_in_the_report_hand_checked(lake):
    broker = _broker(True)
    report = Backtester([_ShortX()], broker, lake, _config(True)).run()
    book = report.short_book
    assert isinstance(book, ShortBookReport)
    # The short of 10 x 100 opens at the second bar's open (Tue 2 Jan). Each
    # bar charges the calendar days since the last accrual, so it pays from
    # Mon 1 Jan to Tue 16 Jan: 15 days of 1,000 x 3.6 % / 360 = 0.10 a day.
    assert book.borrow_fees == pytest.approx(15 * 0.10)
    assert book.debit_interest == 0.0
    assert book.financing_total == pytest.approx(book.borrow_fees)
    assert sum(-e.amount for e in book.financing) == pytest.approx(book.borrow_fees)
    # the fees are the only loss: the price never moved
    assert report.equity_curve[-1] == pytest.approx(100_000.0 - 15 * 0.10)


def test_exposure_over_time(lake):
    report = Backtester([_ShortX()], _broker(True), lake, _config(True)).run()
    exposure = report.short_book.exposure
    assert len(exposure) == len(report.equity_curve)
    assert exposure[0] == ExposurePoint(exposure[0].timestamp, 0.0, 0.0)
    last = exposure[-1]
    assert last.short == pytest.approx(1_000.0 / report.equity_curve[-1])
    assert last.long == 0.0
    assert last.gross == pytest.approx(last.short)
    assert last.net == pytest.approx(-last.short)
    assert report.short_book.max_short == pytest.approx(max(p.short for p in exposure))


def test_long_and_short_attribution(lake):
    broker = _broker(True)
    report = Backtester([_ShortX(), _LongY()], broker, lake, _config(True)).run()
    report = with_trades(report, broker.fills, marks={"X.US": 100.0, "Y.US": 55.0})
    book = report.short_book
    assert book.n_short_trades == 1 and book.n_long_trades == 1
    # X never moves: the short's P&L is zero before financing
    assert book.short_pnl == pytest.approx(0.0, abs=1e-9)
    # Y rises from its second open to the last close
    assert book.long_pnl > 0


def test_forced_orders_are_listed(lake):
    # a borrow source with history that recalls X after the short opens
    class _Recall(FlatBorrow):
        has_history = True

        def quote(self, ticker, day, asset_class="equity"):
            if day >= date(2024, 1, 5):
                return BorrowQuote("none")
            return BorrowQuote("easy", 0.0)

    broker = SimulatedBroker(
        Portfolio(cash=100_000.0),
        margin=MarginSettings(model="reg_t").build(),
        borrow=_Recall(),
        allow_short=True,
    )
    report = Backtester([_ShortX()], broker, lake, _config(True)).run()
    forced = report.short_book.forced_orders
    assert forced and forced[0].strategy_id == "recall"
    assert report.short_book.n_recalls >= 1
    assert report.short_book.n_margin_calls == 0


def test_scaled_borrow_multiplies_fees():
    inner = FlatBorrow(BorrowSettings(fee_rate_annual={"equity": 0.01}, hard=("H",)))
    scaled = ScaledBorrow(inner, 3.0)
    assert scaled.quote("A", date(2024, 1, 2)).fee_rate_annual == pytest.approx(0.03)
    assert scaled.quote("H", date(2024, 1, 2)).status == "hard"


def test_shorting_settings_build_a_short_broker():
    settings = ShortingSettings(borrow_multiplier=2.0)
    assert settings.margin.model == "reg_t"
    broker = settings.broker(Portfolio(cash=1_000.0))
    assert broker.allow_short
    assert broker.borrow.quote("A", date(2024, 1, 2)).fee_rate_annual == pytest.approx(0.01)
    with pytest.raises(ValueError, match="margin"):
        ShortingSettings(margin=MarginSettings(model="cash"))
