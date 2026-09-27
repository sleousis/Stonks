"""Daily borrow rates (roadmap 19.3): the vendor-neutral ``borrow_rates``
lake table, IBKR's short stock file parser, the ``ibkr_borrow`` source, the
pipeline run and the lake-backed ``BorrowSource``. No network."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.execution.borrow import BorrowQuote, LakeBorrowSource
from stonks.ingest.pipeline import IngestPipeline
from stonks.ingest.schemas import BorrowRateRow
from stonks.ingest.sources.base import DataSourceError, UnsupportedCapabilityError
from stonks.ingest.sources.ibkr_borrow import (
    IbkrBorrowDataSource,
    ShortStockFileError,
    parse_short_stock_file,
)
from stonks.store.lake import DuckDBLake

USA = """#BOF|2026.09.28|09:45:03
#SYM|CUR|NAME|CON|ISIN|REBATERATE|FEERATE|AVAILABLE|FIGI|
AAPL|USD|APPLE INC|265598|US0378331005|4.5700|0.2500|>10000000|BBG000B9XRY4|
BRK B|USD|BERKSHIRE HATHAWAY INC-CL B|72063691|US0846707026|4.5700|0.2500|3500000|BBG000DWG505|
GME|USD|GAMESTOP CORP-CLASS A|36285627|US36467W1099|-12.3100|16.8200|15000|BBG000BB5BF6|
NOPE|USD|NOTHING LEFT INC|1|US0000000001|0.0000|55.0000|0||
BAD|USD|BROKEN ROW|2|
049323AB4|USD|CB ATLAS FINL 06.625% 27|557991763|XXXXXXX3AB46|3.63|0.25|300000||
XYZ|USD|MASKED ISIN INC|3|XXXXXXX51012|3.63|0.25|100000||
#EOF|5
"""

UK = """#BOF|2026.09.28|07:00:01
#SYM|CUR|NAME|CON|ISIN|REBATERATE|FEERATE|AVAILABLE|
BT.A|GBP|BT GROUP PLC|123|GB0030913577|4.0000|0.5000|900000|
#EOF|1
"""

DAY = date(2026, 9, 28)


def test_parse_short_stock_file_maps_symbols_rates_and_availability():
    rows = parse_short_stock_file(USA, "usa")
    by = {r.ticker: r for r in rows}
    assert set(by) == {"AAPL.US", "BRK-B.US", "GME.US", "NOPE.US", "XYZ.US"}
    assert by["XYZ.US"].isin is None  # masked in the file
    aapl = by["AAPL.US"]
    assert aapl.as_of == DAY
    assert aapl.fee_rate_annual == pytest.approx(0.0025)  # percent -> fraction
    assert aapl.rebate_rate_annual == pytest.approx(0.0457)
    assert aapl.available_shares == 10_000_000  # ">10000000" is a floor
    assert aapl.isin == "US0378331005"
    assert aapl.currency == "USD"
    assert aapl.source == "ibkr_borrow"
    assert by["GME.US"].rebate_rate_annual == pytest.approx(-0.1231)
    assert by["NOPE.US"].available_shares == 0
    assert by["NOPE.US"].isin == "US0000000001"


def test_parse_uses_the_market_share_class_separator():
    (row,) = parse_short_stock_file(UK, "uk")
    assert row.ticker == "BT-A.LSE"
    assert row.currency == "GBP"


def test_parse_refuses_a_file_without_a_header_or_an_unknown_market():
    with pytest.raises(ShortStockFileError):
        parse_short_stock_file("AAPL|USD|x\n", "usa")
    with pytest.raises(ShortStockFileError, match="market"):
        parse_short_stock_file(USA, "atlantis")


def test_parse_falls_back_to_the_given_day_without_a_bof_line():
    text = "\n".join(USA.splitlines()[1:])
    rows = parse_short_stock_file(text, "usa", fallback_day=date(2026, 9, 1))
    assert {r.as_of for r in rows} == {date(2026, 9, 1)}
    with pytest.raises(ShortStockFileError, match="date"):
        parse_short_stock_file(text, "usa")


def test_source_fetches_through_the_injected_transport():
    asked: list[str] = []

    def fetch(name: str) -> str:
        asked.append(name)
        return USA

    source = IbkrBorrowDataSource(fetch_text=fetch)
    rows = list(source.fetch_borrow_rates("usa"))
    assert asked == ["usa.txt"]
    assert len(rows) == 5
    with pytest.raises(UnsupportedCapabilityError):
        source.fetch_prices("AAPL.US")
    with pytest.raises(DataSourceError):
        list(source.fetch_borrow_rates("atlantis"))


def test_transport_errors_become_soft_fails():
    def fetch(name: str) -> str:
        raise OSError("connection refused")

    source = IbkrBorrowDataSource(fetch_text=fetch)
    with pytest.raises(DataSourceError, match="usa"):
        list(source.fetch_borrow_rates("usa"))


def test_pipeline_writes_the_lake_idempotently_and_soft_fails_per_market(tmp_path):
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    files = {"usa.txt": USA, "uk.txt": UK}

    def fetch(name: str) -> str:
        if name not in files:
            raise OSError("no such file")
        return files[name]

    pipe = IngestPipeline(IbkrBorrowDataSource(fetch_text=fetch), lake)
    result = pipe.run_borrow_rates(["usa", "uk", "germany"])
    assert (result.status, result.tickers_ok, result.tickers_failed) == ("partial", 2, 1)
    assert result.kind == "borrow"
    again = pipe.run_borrow_rates(["usa"])
    assert again.status == "ok"
    df = lake.get_borrow_rates(as_of=DAY)
    assert len(df) == 6
    rate = lake.borrow_rate("GME.US", DAY)
    assert rate is not None
    assert rate["fee_rate_annual"] == pytest.approx(0.1682)
    assert rate["as_of"] == DAY


def _lake_with(tmp_path, rows):
    import pandas as pd

    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    lake.upsert_borrow_rates(pd.DataFrame([r.model_dump() for r in rows]))
    return lake


def _row(ticker, day, fee, available=1_000_000.0, source="ibkr_borrow"):
    return BorrowRateRow(
        ticker=ticker,
        as_of=day,
        fee_rate_annual=fee,
        available_shares=available,
        source=source,
    )


def test_borrow_rate_reads_the_latest_on_or_before_the_day(tmp_path):
    lake = _lake_with(
        tmp_path,
        [
            _row("AAPL.US", date(2026, 9, 1), 0.003),
            _row("AAPL.US", date(2026, 9, 3), 0.004),
            _row("AAPL.US", date(2026, 9, 5), 0.009, source="other"),
        ],
    )
    assert lake.borrow_rate("AAPL.US", date(2026, 8, 31)) is None
    assert lake.borrow_rate("AAPL.US", date(2026, 9, 2))["fee_rate_annual"] == pytest.approx(0.003)
    assert lake.borrow_rate("AAPL.US", date(2026, 9, 9))["fee_rate_annual"] == pytest.approx(0.009)
    only = lake.borrow_rate("AAPL.US", date(2026, 9, 9), source="ibkr_borrow")
    assert only["fee_rate_annual"] == pytest.approx(0.004)
    assert lake.borrow_rate("AAPL.US", date(2026, 9, 9), max_age_days=4) is not None
    stale = lake.borrow_rate("AAPL.US", date(2026, 9, 20), source="ibkr_borrow", max_age_days=5)
    assert stale is None


def test_lake_borrow_source_quotes_easy_hard_and_none(tmp_path):
    lake = _lake_with(
        tmp_path,
        [
            _row("AAPL.US", DAY, 0.0025),
            _row("GME.US", DAY, 0.1682, available=15_000.0),
            _row("NOPE.US", DAY, 0.55, available=0.0),
        ],
    )
    source = LakeBorrowSource(lake, hard_fee_rate=0.03, max_age_days=5)
    assert source.has_history
    assert source.quote("AAPL.US", DAY) == BorrowQuote("easy", 0.0025, 1_000_000.0)
    assert source.quote("GME.US", DAY) == BorrowQuote("hard", 0.1682, 15_000.0)
    assert source.quote("NOPE.US", DAY) == BorrowQuote("none", 0.55, 0.0)
    assert source.quote("MSFT.US", DAY) is None  # no data, no short
    assert source.quote("AAPL.US", date(2026, 10, 30)) is None  # too old
