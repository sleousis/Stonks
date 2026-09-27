"""The intraday strategies on today's bar-based backtester at 1m, and a
lab run over session windows (21.3.1).

Eight NYSE sessions of synthetic 1m bars: every session forms an opening
range, breaks out above it, drifts up and eases into the close. The
opening range breakout buys
after the breakout, fills at the next bar's open (P21), and is flat before
every close.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from stonks.core.interval import Interval
from stonks.lab.backtesting import run_backtest
from stonks.lab.dataset import LabDataset
from stonks.lab.objectives import FinalReturnObjective
from stonks.lab.runner import LabRunner
from stonks.lab.survival.base import SurvivalSuite
from stonks.lab.survival.oos import OutOfSampleTest
from stonks.lab.survival.walk_forward import WalkForwardConfig
from stonks.lab.tuning.grid import GridTuner
from stonks.strategies.examples.intraday_momentum import IntradayMomentum
from stonks.strategies.examples.intraday_orb import OpeningRangeBreakout
from stonks.strategies.examples.intraday_vwap_reversion import VwapReversion
from tests.minute_bars import minute_lake, session_frame, session_of

T = "A.US"
DAYS = [date(2024, 3, d) for d in (4, 5, 6, 7, 8, 11, 12, 13)]


def _path(i: int) -> float:
    if i < 30:
        return 100.0 + (0.5 if i % 2 else -0.5)
    if i < 180:
        return 100.0 + 2.0 * (i - 30) / 150
    if i <= 205:
        return 100.4  # a dip below VWAP
    return 100.0  # a close below the next open's first half hour


def _utc(ts: datetime) -> datetime:
    return ts.astimezone(UTC).replace(tzinfo=None) if ts.tzinfo else ts


@pytest.fixture(scope="module")
def lake(tmp_path_factory):
    frames = [session_frame(T, d, _path) for d in DAYS]
    lake = minute_lake(frames, path=tmp_path_factory.mktemp("lake") / "lake.duckdb")
    yield lake
    lake.close()


def _dataset(lake, **kw) -> LabDataset:
    return LabDataset(
        lake=lake,
        universe=[T],
        start=DAYS[0],
        end=DAYS[-1],
        train_ratio=0.5,
        interval=Interval.MIN_1,
        benchmark="none",
        **kw,
    ).with_sessions()


@pytest.mark.parametrize("cls", [OpeningRangeBreakout, VwapReversion, IntradayMomentum])
def test_each_strategy_trades_inside_the_session_and_is_flat_at_every_close(cls, lake):
    ds = _dataset(lake)
    report = run_backtest(cls({"ticker": T}), ds, ds.full_window)
    trades = report.trades
    assert trades, cls.__name__
    for trade in trades:
        assert not trade.is_open
        entry, exit_ = _utc(trade.entry_ts), _utc(trade.exit_ts)
        session = session_of(T, entry.date())
        # entries fill at a bar's open after the decision, exits before the close
        assert session.open < entry < exit_ < session.close
        assert exit_ <= session.close - timedelta(minutes=4)
    if cls is not VwapReversion:  # VWAP reversion may trade again after a reversion
        assert len({_utc(t.entry_ts).date() for t in trades}) == len(trades)


def test_orb_buys_after_the_breakout_and_fills_on_the_next_bar(lake):
    ds = _dataset(lake)
    report = run_backtest(OpeningRangeBreakout({"ticker": T}), ds, ds.full_window)
    first = report.trades[0]
    entry = _utc(first.entry_ts)
    session = session_of(T, entry.date())
    minute = (entry - session.open) / timedelta(minutes=1)
    # the path first closes above the range high (100.5 plus the bar spread)
    # after minute 70; the fill is one bar after the decision
    assert 70 < minute < 110
    assert first.mfe_pct is not None and first.mfe_pct > 0  # the drift after the breakout


def test_a_lab_run_splits_by_session_and_tunes_the_strategy(lake):
    ds = LabDataset(
        lake=lake,
        universe=[T],
        start=DAYS[0],
        end=DAYS[-1],
        train_ratio=0.5,
        interval=Interval.MIN_1,
        benchmark="none",
    )
    runner = LabRunner(
        tuner=GridTuner(grid_size=2),
        objective=FinalReturnObjective(),
        suite=SurvivalSuite(tests=[OutOfSampleTest(min_sharpe=-10.0, max_drawdown_limit=-0.99)]),
        budget=2,
        preflight=False,
    )
    result = runner.run(strategy_cls=OpeningRangeBreakout, dataset=ds, fixed_params={"ticker": T})
    assert result.best_params is not None
    assert result.verdict in ("pass", "fail")
    manifest_ds = result.manifest["dataset"]
    assert manifest_ds["sessions"] == len(DAYS)
    # 4 train sessions, 4 validation sessions (the survival suite adds the
    # one-session embargo of the label horizon on top)
    assert manifest_ds["train_window"] == [str(DAYS[0]), str(DAYS[3])]
    assert manifest_ds["val_window"] == [str(DAYS[4]), str(DAYS[-1])]
    suite_ds = ds.with_sessions().for_strategy(result.strategy)
    assert suite_ds.val_window == (DAYS[5], DAYS[-1])


def test_walk_forward_folds_on_the_sessions(lake):
    ds = _dataset(lake).for_strategy(OpeningRangeBreakout({"ticker": T}))
    folds = WalkForwardConfig(n_splits=2, test_days=1).folds_for(ds)
    assert [f.test_start for f in folds] == [DAYS[-2], DAYS[-1]]
    assert all(
        isinstance(f.train_end, date) and not isinstance(f.train_end, datetime) for f in folds
    )
    assert [f.train_end for f in folds] == [DAYS[-4], DAYS[-3]]
