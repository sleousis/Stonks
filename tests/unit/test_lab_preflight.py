"""Lab preflight (BL-37): coverage, flags and membership checked before a run."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from stonks.backtest.costs import CostModelSettings
from stonks.core.interval import Interval
from stonks.lab.dataset import LabDataset
from stonks.lab.preflight import PreflightError, run_preflight
from stonks.store.lake import DuckDBLake


class _Needs60:
    required_history_bars = 60


@pytest.fixture
def lake(tmp_path):
    lk = DuckDBLake(tmp_path / "lake.duckdb")
    lk.migrate()
    yield lk
    lk.close()


def _bars(lake, ticker, start, periods):
    days = pd.bdate_range(start, periods=periods)
    lake.upsert_bars(
        pd.DataFrame(
            {
                "ticker": ticker,
                "timestamp": days,
                "open": 10.0,
                "high": 10.0,
                "low": 10.0,
                "close": 10.0,
                "adj_close": 10.0,
                "volume": 100,
            }
        ),
        Interval.DAY_1,
    )


def _costs():
    return CostModelSettings.model_validate({"default": {"fee_bps": 1.0}})


def _ds(lake, universe=("A.US",), start=date(2024, 1, 1), end=date(2024, 6, 28), **kw):
    kw.setdefault("costs", _costs())
    kw.setdefault("benchmark", "none")
    return LabDataset(lake=lake, universe=list(universe), start=start, end=end, **kw)


def _codes(report):
    return {i.code for i in report.issues}


def test_clean_run_only_warns_about_the_static_universe(lake):
    _bars(lake, "A.US", "2023-06-01", 280)
    report = run_preflight(_ds(lake))
    assert _codes(report) == {"static_universe"}
    assert report.ok
    assert report.errors == []


def test_empty_universe_is_fatal(lake):
    report = run_preflight(_ds(lake, universe=()))
    assert "empty_universe" in _codes(report)
    assert not report.ok
    with pytest.raises(PreflightError, match="universe is empty"):
        report.raise_for_errors()


def test_no_data_at_all_is_fatal(lake):
    report = run_preflight(_ds(lake, universe=("A.US", "B.US")))
    assert "no_data" in _codes(report)
    assert not report.ok


def test_missing_ticker_and_late_start_warn(lake):
    _bars(lake, "A.US", "2024-01-01", 130)
    _bars(lake, "LATE.US", "2024-04-01", 60)
    report = run_preflight(_ds(lake, universe=("A.US", "LATE.US", "NONE.US")))
    assert {"missing_data", "late_start"} <= _codes(report)
    assert report.ok
    missing = next(i for i in report.issues if i.code == "missing_data")
    assert missing.details["tickers"] == ["NONE.US"]
    late = next(i for i in report.issues if i.code == "late_start")
    assert late.details["tickers"] == ["LATE.US"]


def test_required_history_beyond_coverage(lake):
    _bars(lake, "A.US", "2024-01-01", 40)  # 40 bars, strategy needs 60
    report = run_preflight(_ds(lake, end=date(2024, 2, 23)), _Needs60)
    assert "insufficient_history" in _codes(report)


def test_short_warmup_when_history_starts_with_the_window(lake):
    _bars(lake, "A.US", "2024-01-01", 130)
    report = run_preflight(_ds(lake), _Needs60())
    assert "short_warmup" in _codes(report)
    assert "insufficient_history" not in _codes(report)


def test_zero_costs_and_missing_benchmark_warn(lake):
    _bars(lake, "A.US", "2023-06-01", 280)
    report = run_preflight(_ds(lake, costs=None, benchmark="SPY.US"))
    assert {"zero_costs", "missing_benchmark"} <= _codes(report)
    _bars(lake, "SPY.US", "2023-06-01", 280)
    report = run_preflight(_ds(lake, costs=None, benchmark="SPY.US"))
    assert "missing_benchmark" not in _codes(report)


def test_statement_flags_and_quarantined_bars_warn(lake):
    _bars(lake, "A.US", "2023-06-01", 280)
    lake.replace_statement_flags(
        pd.DataFrame(
            [
                {
                    "ticker": "A.US",
                    "period_end": date(2024, 3, 31),
                    "frequency": "Q",
                    "check_id": "balance_identity",
                    "severity": "error",
                    "detail": "x",
                    "flagged_at": pd.Timestamp("2024-05-01"),
                }
            ]
        )
    )
    lake.con.execute(
        "INSERT INTO quarantined_bars (ticker, timestamp, interval, reasons, quarantined_at) "
        "VALUES ('A.US', TIMESTAMP '2024-02-01', '1d', 'ohlc', TIMESTAMP '2024-02-02')"
    )
    report = run_preflight(_ds(lake))
    assert {"statement_flags", "quarantined_bars"} <= _codes(report)


def test_long_window_without_delisted_names_warns(lake):
    _bars(lake, "A.US", "2018-01-01", 1700)
    lake.upsert_instrument_profile(pd.DataFrame([{"id": "A.US", "asset_class": "equity"}]))
    ds = _ds(lake, start=date(2018, 1, 1), end=date(2024, 6, 28))
    assert "no_delisted" in _codes(run_preflight(ds))
    lake.upsert_instrument_profile(
        pd.DataFrame([{"id": "D.US", "is_delisted": True, "delisted_date": date(2020, 1, 1)}])
    )
    ds = _ds(lake, universe=("A.US", "D.US"), start=date(2018, 1, 1), end=date(2024, 6, 28))
    assert "no_delisted" not in _codes(run_preflight(ds))


def test_membership_members_missing_from_the_dataset_warn(lake):
    _bars(lake, "A.US", "2023-06-01", 280)
    lake.upsert_universe_membership(
        pd.DataFrame(
            [
                {"universe_id": "idx", "ticker": "A.US", "start_date": date(2020, 1, 1)},
                {
                    "universe_id": "idx",
                    "ticker": "DEAD.US",
                    "start_date": date(2020, 1, 1),
                    "end_date": date(2024, 3, 1),
                },
            ]
        )
    )
    report = run_preflight(_ds(lake), universe_id="idx")
    assert "static_universe" not in _codes(report)
    gap = next(i for i in report.issues if i.code == "membership_gap")
    assert gap.details["tickers"] == ["DEAD.US"]
    with pytest.raises(PreflightError, match="unknown universe"):
        run_preflight(_ds(lake), universe_id="nope").raise_for_errors()


def test_strict_mode_turns_warnings_into_errors(lake):
    _bars(lake, "A.US", "2023-06-01", 280)
    report = run_preflight(_ds(lake), strict=True)
    assert not report.ok
    with pytest.raises(PreflightError, match="static_universe"):
        report.raise_for_errors()


def test_no_lake_skips_the_preflight():
    ds = LabDataset(lake=None, universe=["X.US"], start=date(2024, 1, 1), end=date(2024, 6, 1))
    report = run_preflight(ds)
    assert report.skipped
    assert report.ok


def test_report_round_trips_to_a_dict(lake):
    _bars(lake, "A.US", "2023-06-01", 280)
    d = run_preflight(_ds(lake)).to_dict()
    assert d["ok"] is True
    assert d["issues"][0]["code"] == "static_universe"
