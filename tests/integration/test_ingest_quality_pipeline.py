"""Ingest validation, quarantine, quality summary and source fallback
(roadmap 12.5). Hermetic: canned sources only."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.ingest.pipeline import IngestPipeline
from stonks.ingest.quality import (
    BarQualityChecker,
    quarantined_bars,
    run_quality,
)
from stonks.ingest.quality_config import DataQualityConfig
from stonks.ingest.schemas import FinancialStatementsBundle, IntradayBar, RawPriceBar
from stonks.ingest.sources.base import DataSource, DataSourceError
from stonks.notify.base import Notification, Notifier
from stonks.store.lake import DuckDBLake

START = date(2025, 1, 1)


class _Source(DataSource):
    def __init__(
        self,
        source_id: str,
        prices: dict[str, list[RawPriceBar]] | None = None,
        intraday: dict[str, list[IntradayBar]] | None = None,
        fail_on: set[str] | None = None,
    ):
        self.source_id = source_id
        self._prices = prices or {}
        self._intraday = intraday or {}
        self._fail_on = fail_on or set()
        self.calls: list[str] = []

    def list_tickers(self, exchange: str) -> list[str]:
        return sorted(self._prices)

    def fetch_prices(self, ticker, since=None, until=None) -> Iterable[RawPriceBar]:
        self.calls.append(ticker)
        if ticker in self._fail_on:
            raise DataSourceError(f"{self.source_id} down for {ticker}")
        return list(self._prices.get(ticker, []))

    def fetch_intraday_bars(self, ticker, interval, since=None, until=None):
        self.calls.append(ticker)
        if ticker in self._fail_on:
            raise DataSourceError(f"{self.source_id} down for {ticker}")
        return list(self._intraday.get(ticker, []))

    def fetch_fundamentals(self, ticker: str) -> FinancialStatementsBundle:
        return FinancialStatementsBundle()


class _Recorder(Notifier):
    def __init__(self):
        self.sent: list[Notification] = []

    def _send(self, notification: Notification) -> None:
        self.sent.append(notification)


def _walk(n, seed=3):
    rng = np.random.default_rng(seed)
    return list(100 * np.exp(np.cumsum(rng.normal(0, 0.01, n))))


def _bars(ticker, closes, start=START):
    return [
        RawPriceBar(
            ticker=ticker,
            date=start + timedelta(days=i),
            open=c,
            high=c * 1.01,
            low=c * 0.99,
            close=c,
            adj_close=c,
            volume=1000,
        )
        for i, c in enumerate(closes)
    ]


@pytest.fixture
def lake(tmp_path):
    lk = DuckDBLake(tmp_path / "lake.duckdb")
    lk.migrate()
    yield lk
    lk.close()


def _stored(lake, ticker):
    return lake.get_prices(ticker, START - timedelta(days=400), START + timedelta(days=400))


def test_bad_rows_go_to_quarantine_not_bars(lake):
    closes = [100.0] * 10
    bars = _bars("AAA.US", closes)
    bars[3] = bars[3].model_copy(update={"close": 0.0, "low": 0.0})
    src = _Source("fake", prices={"AAA.US": bars})
    result = IngestPipeline(src, lake).run_prices(["AAA.US"], until=START + timedelta(days=10))

    assert result.status == "ok"
    stored = _stored(lake, "AAA.US")
    assert len(stored) == 9
    assert START + timedelta(days=3) not in set(stored["date"])
    q = quarantined_bars(lake, ticker="AAA.US")
    assert len(q) == 1
    assert q.loc[0, "reasons"] == "non_positive_price"
    assert q.loc[0, "source"] == "fake"
    assert q.loc[0, "run_id"] == result.run_id
    assert q.loc[0, "interval"] == "1d"
    summary = run_quality(lake, result.run_id)
    assert summary["bars_checked"] == 10
    assert summary["bars_quarantined"] == 1
    assert summary["reasons"] == {"non_positive_price": 1}
    assert result.quality == summary


def test_clean_run_records_summary_and_does_not_alert(lake):
    src = _Source("fake", prices={"AAA.US": _bars("AAA.US", _walk(30))})
    notifier = _Recorder()
    result = IngestPipeline(src, lake, notifier=notifier).run_prices(
        ["AAA.US"], until=START + timedelta(days=30)
    )
    assert run_quality(lake, result.run_id)["bars_quarantined"] == 0
    assert notifier.sent == []


def test_quarantine_breach_alerts_via_notifier(lake):
    bars = _bars("AAA.US", [100.0] * 5)
    bars[1] = bars[1].model_copy(update={"high": 50.0})
    notifier = _Recorder()
    result = IngestPipeline(_Source("fake", prices={"AAA.US": bars}), lake, notifier=notifier)
    run = result.run_prices(["AAA.US"])
    [note] = notifier.sent
    assert note.level == "warning"
    assert "1 bar(s) quarantined" in note.message
    assert note.fields["run_id"] == run.run_id
    assert note.fields["reasons"] == {"high_below_low": 1}


def test_spike_judged_against_stored_history(lake):
    closes = _walk(60)
    first = _Source("fake", prices={"AAA.US": _bars("AAA.US", closes[:50])})
    IngestPipeline(first, lake).run_prices(["AAA.US"])
    tail = closes[50:]
    tail[3] = tail[2] * 4
    second = _Source(
        "fake", prices={"AAA.US": _bars("AAA.US", tail, start=START + timedelta(days=50))}
    )
    result = IngestPipeline(second, lake).run_prices(["AAA.US"])
    q = quarantined_bars(lake, run_id=result.run_id)
    assert list(q["reasons"]) == ["price_spike"]
    assert len(_stored(lake, "AAA.US")) == 59


def test_split_recorded_in_lake_is_not_flagged(lake):
    closes = _walk(60)
    closes = closes[:40] + [c / 4 for c in closes[40:]]
    split_day = START + timedelta(days=40)
    lake.upsert_stock_splits(pd.DataFrame([{"ticker": "AAA.US", "date": split_day, "ratio": 4.0}]))
    bars = _bars("AAA.US", closes)
    # Raw closes (adj_close un-adjusted too) so only the split table can explain it.
    result = IngestPipeline(_Source("fake", prices={"AAA.US": bars}), lake).run_prices(
        ["AAA.US"], until=START + timedelta(days=60)
    )
    summary = run_quality(lake, result.run_id)
    assert summary["bars_quarantined"] == 0
    assert summary["warnings"] == {}


def test_unrecorded_split_only_warns(lake):
    closes = _walk(60)
    closes = closes[:40] + [c / 4 for c in closes[40:]]
    result = IngestPipeline(
        _Source("fake", prices={"AAA.US": _bars("AAA.US", closes)}), lake
    ).run_prices(["AAA.US"], until=START + timedelta(days=60))
    summary = run_quality(lake, result.run_id)
    assert summary["bars_quarantined"] == 0
    assert summary["warnings"] == {"extreme_move": 1}
    assert len(_stored(lake, "AAA.US")) == 60


def test_negative_bond_yields_are_kept(lake):
    from stonks.ingest.pipeline import _rows_to_df
    from stonks.ingest.schemas import TickerProfile

    lake.upsert_instrument_profile(
        _rows_to_df([TickerProfile(id="DE10Y.GBOND", asset_class="bond")])
    )
    bars = [
        RawPriceBar(
            ticker="DE10Y.GBOND",
            date=START + timedelta(days=i),
            open=y,
            high=y + 0.02,
            low=y - 0.02,
            close=y,
            adj_close=y,
            volume=0,
        )
        for i, y in enumerate([-0.2, -0.25, -0.1, 0.01, -0.05])
    ]
    result = IngestPipeline(_Source("fake", prices={"DE10Y.GBOND": bars}), lake).run_prices(
        ["DE10Y.GBOND"]
    )
    assert run_quality(lake, result.run_id)["bars_quarantined"] == 0
    assert len(_stored(lake, "DE10Y.GBOND")) == 5


def test_disabled_quality_keeps_every_row(lake):
    bars = _bars("AAA.US", [100.0] * 5)
    bars[1] = bars[1].model_copy(update={"high": 50.0})
    checker = BarQualityChecker(DataQualityConfig(enabled=False))
    IngestPipeline(_Source("fake", prices={"AAA.US": bars}), lake, quality=checker).run_prices(
        ["AAA.US"]
    )
    assert len(_stored(lake, "AAA.US")) == 5
    assert quarantined_bars(lake).empty


def test_intraday_batches_are_validated(lake):
    t0 = datetime(2025, 1, 2, 14, 30)
    rows = [
        IntradayBar(
            ticker="AAA.US",
            timestamp=t0 + timedelta(hours=i),
            open=10.0,
            high=10.1,
            low=9.9,
            close=10.0 if i != 2 else 11.0,
            adj_close=10.0,
            volume=5,
        )
        for i in range(5)
    ]
    src = _Source("fake", intraday={"AAA.US": rows})
    result = IngestPipeline(src, lake).run_intraday_bars(["AAA.US"], Interval.HOUR_1)
    q = quarantined_bars(lake, run_id=result.run_id)
    assert list(q["reasons"]) == ["close_out_of_range"]
    assert list(q["interval"]) == ["1h"]
    stored = lake.get_bars("AAA.US", Interval.HOUR_1, t0, t0 + timedelta(days=1))
    assert len(stored) == 4


# ---- fallback -------------------------------------------------------------------


def test_fallback_serves_ticker_the_primary_failed(lake):
    primary = _Source(
        "eodhd",
        prices={"BBB.US": _bars("BBB.US", [50.0] * 3)},
        fail_on={"AAA.US"},
    )
    fallback = _Source("yahoo", prices={"AAA.US": _bars("AAA.US", [10.0] * 3)})
    notifier = _Recorder()
    result = IngestPipeline(primary, lake, fallback=fallback, notifier=notifier).run_prices(
        ["AAA.US", "BBB.US"]
    )
    assert (result.status, result.tickers_ok, result.tickers_failed) == ("ok", 2, 0)
    assert fallback.calls == ["AAA.US"]
    assert len(_stored(lake, "AAA.US")) == 3
    assert result.quality["supplied_by"] == {"AAA.US": "yahoo"}
    assert run_quality(lake, result.run_id)["supplied_by"] == {"AAA.US": "yahoo"}
    # The run is still recorded under the primary source.
    source = lake.sql("SELECT source FROM ingest_runs WHERE id = ?", [result.run_id])
    assert source.iloc[0, 0] == "eodhd"
    [note] = notifier.sent
    assert "fallback" in note.message


def test_fallback_rows_are_validated_and_attributed(lake):
    bars = _bars("AAA.US", [10.0] * 3)
    bars[0] = bars[0].model_copy(update={"close": -1.0})
    primary = _Source("eodhd", fail_on={"AAA.US"})
    fallback = _Source("yahoo", prices={"AAA.US": bars})
    result = IngestPipeline(primary, lake, fallback=fallback).run_prices(["AAA.US"])
    q = quarantined_bars(lake, run_id=result.run_id)
    assert list(q["source"]) == ["yahoo"]


def test_both_sources_failing_counts_the_ticker_failed(lake):
    primary = _Source("eodhd", fail_on={"AAA.US"})
    fallback = _Source("yahoo", fail_on={"AAA.US"})
    result = IngestPipeline(primary, lake, fallback=fallback).run_prices(["AAA.US"])
    assert (result.status, result.tickers_failed) == ("error", 1)
    err = lake.sql("SELECT error FROM ingest_runs WHERE id = ?", [result.run_id]).iloc[0, 0]
    assert "eodhd down" in err and "yahoo down" in err


def test_without_fallback_failure_is_unchanged(lake):
    primary = _Source("eodhd", fail_on={"AAA.US"})
    result = IngestPipeline(primary, lake).run_prices(["AAA.US"])
    assert (result.status, result.tickers_failed) == ("error", 1)
    assert result.quality["supplied_by"] == {}


def test_primary_success_never_calls_fallback(lake):
    primary = _Source("eodhd", prices={"AAA.US": _bars("AAA.US", [1.0] * 3)})
    fallback = _Source("yahoo")
    IngestPipeline(primary, lake, fallback=fallback).run_prices(["AAA.US"])
    assert fallback.calls == []


# ---- stored spikes (DS-03) --------------------------------------------------------------


@pytest.mark.parametrize("backend", ["duckdb", "parquet"])
def test_a_stored_spike_is_removed_when_the_next_day_reverts_it(tmp_path, backend):
    lake = DuckDBLake(tmp_path / "lake.duckdb", bar_backend=backend)
    lake.migrate()
    try:
        closes = _walk(62)
        closes[60] = closes[59] * 3.0
        day = START + timedelta(days=60)
        # 60 bars, then a one-bar batch with the bad tick: stored, as it
        # cannot be judged yet
        IngestPipeline(_Source("fake", {"AAA.US": _bars("AAA.US", closes[:60])}), lake).run_prices(
            ["AAA.US"]
        )
        spike = _Source("fake", {"AAA.US": _bars("AAA.US", [closes[60]], start=day)})
        assert IngestPipeline(spike, lake).run_prices(["AAA.US"]).quality["bars_quarantined"] == 0
        assert day in set(_stored(lake, "AAA.US")["date"])
        # the next day's one-bar batch takes it back
        revert = _Source(
            "fake", {"AAA.US": _bars("AAA.US", [closes[61]], start=day + timedelta(days=1))}
        )
        result = IngestPipeline(revert, lake).run_prices(["AAA.US"])
        stored = _stored(lake, "AAA.US")
        assert day not in set(stored["date"])
        assert len(stored) == 61
        q = quarantined_bars(lake, run_id=result.run_id)
        assert list(q["reasons"]) == ["price_spike"]
        assert q.loc[0, "timestamp"].date() == day
        assert result.quality["bars_quarantined"] == 1
    finally:
        lake.close()


# ---- edge cases -------------------------------------------------------------------------


def test_a_fallback_row_with_another_ticker_code_is_stored_under_the_requested_one(lake):
    # a fallback adapter that returns its own symbol must not create a
    # second series: rows are stored under the ticker that was asked for
    primary = _Source("eodhd", fail_on={"AAA.US"})
    fallback = _Source("yahoo", prices={"AAA.US": _bars("AAA", [1.0] * 3)})
    IngestPipeline(primary, lake, fallback=fallback).run_prices(["AAA.US"])
    assert lake.sql("SELECT DISTINCT ticker FROM bars").ticker.tolist() == ["AAA.US"]


def test_a_ticker_with_zero_rows_is_ok_but_warned_in_quality(lake):
    src = _Source("fake", prices={"AAA.US": []})
    result = IngestPipeline(src, lake).run_prices(["AAA.US"], until=START)
    assert result.tickers_ok == 1
    assert "AAA.US" in result.quality["warned_tickers"]
    assert result.quality["warnings"] == {"no_data": 1}
