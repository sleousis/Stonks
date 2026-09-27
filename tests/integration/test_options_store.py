"""Option chains in the lake (roadmap 17.1): migration 017, the store's
round trip, the ingest run row and its soft fails, through the synthetic
FakeDataSource path."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from stonks.ingest.option_schemas import OptionQuoteRow
from stonks.ingest.sources.base import DataSourceError
from stonks.options.chain import ChainSnapshot, OptionQuote, snapshots
from stonks.options.ingest import filter_rows, ingest_option_quotes
from stonks.options.store import OptionStore
from stonks.options.synthetic import (
    SyntheticChainSpec,
    SyntheticOptionSource,
    monthly_expiries,
    third_friday,
)

D0 = date(2025, 10, 1)


def closes(n: int = 3, start: float = 100.0) -> dict[date, float]:
    return {D0 + timedelta(days=i): start + i for i in range(n)}


def test_migration_creates_the_option_tables(lake):
    assert {"option_contracts", "option_quotes"} <= set(lake.tables())


def test_ingest_writes_contracts_and_quotes(lake):
    src = SyntheticOptionSource(
        {"AAPL.US": closes()}, fixed_strikes={"AAPL.US": [90, 95, 100, 105, 110]}
    )
    result = ingest_option_quotes(src, lake, ["AAPL.US"], since=D0, until=D0 + timedelta(days=2))
    assert result.status == "ok" and result.ok == ("AAPL.US",) and result.rows > 0
    run = lake.con.execute(
        "SELECT source, kind, status, tickers_ok FROM ingest_runs WHERE id = ?", [result.run_id]
    ).fetchone()
    assert run == ("synthetic", "options", "ok", 1)
    store = OptionStore(lake)
    assert store.quote_days("AAPL.US") == [D0, D0 + timedelta(days=1), D0 + timedelta(days=2)]
    chain = store.chain("AAPL.US", D0)
    assert isinstance(chain, ChainSnapshot) and chain.spot == 100.0
    assert len(chain) == result.rows // 3
    q = chain.by_id()["AAPL.US:2025-10-17:C:100"]
    assert q.two_sided and q.mid is not None and q.iv == pytest.approx(0.25)
    contracts = store.contracts(["AAPL.US:2025-10-17:C:100"])
    c = contracts["AAPL.US:2025-10-17:C:100"]
    assert (c.strike, c.right, c.multiplier) == (100.0, "call", 100.0)
    seen = lake.con.execute(
        "SELECT first_seen, last_seen FROM option_contracts WHERE contract_id = ?",
        ["AAPL.US:2025-10-17:C:100"],
    ).fetchone()
    assert (pd.Timestamp(seen[0]).date(), pd.Timestamp(seen[1]).date()) == (
        D0,
        D0 + timedelta(days=2),
    )


def test_ingest_is_idempotent(lake):
    src = SyntheticOptionSource({"AAPL.US": closes(1)}, SyntheticChainSpec(strikes_each_side=1))
    ingest_option_quotes(src, lake, ["AAPL.US"])
    before = lake.count_rows("option_quotes")
    ingest_option_quotes(src, lake, ["AAPL.US"])
    assert lake.count_rows("option_quotes") == before


def test_failed_underlying_is_a_soft_fail(lake):
    src = SyntheticOptionSource({"AAPL.US": closes(1)}, SyntheticChainSpec(strikes_each_side=1))
    result = ingest_option_quotes(src, lake, ["AAPL.US", "NOPE.US"])
    assert result.status == "partial" and "NOPE.US" in result.failed
    result = ingest_option_quotes(src, lake, ["NOPE.US"])
    assert result.status == "error"


def test_strike_band_uses_the_lake_close_when_the_vendor_sends_none(lake):
    lake.upsert_prices(
        pd.DataFrame(
            [
                {
                    "ticker": "X.US",
                    "date": D0,
                    "open": 100,
                    "high": 100,
                    "low": 100,
                    "close": 100.0,
                    "adj_close": 100.0,
                    "volume": 1,
                }
            ]
        )
    )

    class NoSpot(SyntheticOptionSource):
        def fetch_option_quotes(self, underlying, since=None, until=None):
            return [
                r.model_copy(update={"underlying_price": None})
                for r in super().fetch_option_quotes(underlying, since, until)
            ]

    src = NoSpot({"X.US": {D0: 100.0}}, SyntheticChainSpec(strikes_each_side=6))
    ingest_option_quotes(src, lake, ["X.US"], since=D0, until=D0, strike_band=0.1)
    strikes = {c.strike for c in OptionStore(lake).contracts().values()}
    assert strikes and max(strikes) <= 110 and min(strikes) >= 90


def test_filter_rows():
    def row(expiry: date, strike: float, spot: float | None = 100.0) -> OptionQuoteRow:
        return OptionQuoteRow(
            underlying="A.US",
            expiry=expiry,
            strike=strike,
            right="call",
            as_of=D0,
            underlying_price=spot,
        )

    rows = [
        row(D0 - timedelta(days=1), 100),  # expired
        row(D0 + timedelta(days=400), 100),  # too far
        row(D0 + timedelta(days=30), 150),  # outside the band
        row(D0 + timedelta(days=30), 105),
        row(D0 + timedelta(days=30), 300, spot=None),  # no spot: kept
    ]
    kept = filter_rows(rows, max_expiry_days=365, strike_band=0.3)
    assert [r.strike for r in kept] == [105, 300]


def test_quotes_pick_one_source_per_contract_and_day(lake):
    store = OptionStore(lake)
    row = OptionQuoteRow(
        underlying="A.US",
        expiry=date(2025, 11, 21),
        strike=50,
        right="put",
        as_of=D0,
        bid=1.0,
        ask=1.2,
    )
    store.upsert_quotes([row], "b_vendor")
    store.upsert_quotes([row.model_copy(update={"bid": 2.0, "ask": 2.2})], "a_vendor")
    assert [q.bid for q in store.quotes("A.US", D0, D0)] == [2.0]
    assert [q.bid for q in store.quotes("A.US", D0, D0, source="b_vendor")] == [1.0]
    assert store.upsert_quotes([], "x") == 0
    assert store.contracts([]) == {}


def test_third_friday_and_monthly_expiries():
    assert third_friday(2025, 10) == date(2025, 10, 17)
    assert third_friday(2026, 1) == date(2026, 1, 16)
    assert monthly_expiries(date(2025, 12, 20), 40) == [date(2026, 1, 16)]
    assert monthly_expiries(date(2025, 10, 17), 0) == [date(2025, 10, 17)]


def test_synthetic_source_is_options_only():
    src = SyntheticOptionSource({"A.US": {D0: 10.0}}, fixed_strikes={"A.US": [10, 12, 10]})
    assert src.list_tickers("US") == ["A.US"]
    assert src.strikes("A.US", 50.0) == [10, 12]
    with pytest.raises(DataSourceError):
        src.fetch_prices("A.US")
    with pytest.raises(DataSourceError):
        src.fetch_fundamentals("A.US")


def test_chain_values():
    from stonks.core.options import OptionContract

    c = OptionContract("A.US", date(2025, 11, 21), 50.0, "call")
    q = OptionQuote(c, D0, bid=1.0, ask=1.2, last=1.1, underlying_price=51.0)
    assert q.mid == pytest.approx(1.1) and q.half_spread == pytest.approx(0.1)
    assert q.spread_pct == pytest.approx(0.2 / 1.1) and q.mark == pytest.approx(1.1)
    one_sided = OptionQuote(c, D0, bid=None, ask=0.05, last=0.02)
    assert not one_sided.two_sided and one_sided.mid is None and one_sided.mark == 0.02
    assert one_sided.half_spread is None and one_sided.spread_pct is None
    snaps = snapshots([q, one_sided])
    snap = snaps[("A.US", D0)]
    assert snap.spot == 51.0 and snap.expiries() == [date(2025, 11, 21)]
    assert snap.filter(right="call", two_sided=True) == [q]
    assert snap.filter(right="put") == []
    assert list(snap) == [q, one_sided]


def test_underlyings_summarise_the_stored_chains(lake):
    store = OptionStore(lake)
    assert store.underlyings() == []
    src = SyntheticOptionSource(
        {"AAPL.US": closes(3), "MSFT.US": closes(2, 300.0)},
        fixed_strikes={"AAPL.US": [95, 100, 105], "MSFT.US": [300]},
    )
    ingest_option_quotes(src, lake, ["AAPL.US", "MSFT.US"])
    got = store.underlyings()
    assert [u.underlying for u in got] == ["AAPL.US", "MSFT.US"]
    aapl = got[0]
    assert (aapl.first_day, aapl.last_day, aapl.days) == (D0, D0 + timedelta(days=2), 3)
    assert aapl.contracts > 0 and aapl.sources == ("synthetic",)
    assert store.latest_day("AAPL.US", D0 + timedelta(days=10)) == D0 + timedelta(days=2)
    assert store.latest_day("AAPL.US", D0 + timedelta(days=1)) == D0 + timedelta(days=1)
    assert store.latest_day("AAPL.US", D0 - timedelta(days=1)) is None
    assert store.latest_day("AAPL.US") == D0 + timedelta(days=2)
    assert store.sources(["AAPL.US"], D0, D0 + timedelta(days=5)) == ["synthetic"]
