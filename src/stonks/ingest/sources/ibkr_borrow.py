"""IBKR's public short stock file as a ``DataSource`` (roadmap 19.3).

Interactive Brokers publishes one pipe-separated file per market each
morning (``usa.txt``, ``uk.txt``, ...) on its public FTP site, readable
with the shared ``shortstock`` login and no password. It is not an account
credential. Each row is one stock with its borrow fee, rebate and the
shares IBKR can lend::

    #BOF|2026.09.28|09:45:03
    #SYM|CUR|NAME|CON|ISIN|REBATERATE|FEERATE|AVAILABLE|FIGI|
    AAPL|USD|APPLE INC|265598|US0378331005|4.57|0.25|>10000000|BBG000B9XRY4|
    #EOF|1

:func:`parse_short_stock_file` turns it into vendor-neutral
:class:`BorrowRateRow` values: our tickers (``BRK B`` is ``BRK-B.US``),
yearly fractions instead of percents, and ``>10000000`` read as its floor.
Columns are found by the header names, so a new column never breaks it.
The FTP transport uses the standard library and is injected in tests.
"""

from __future__ import annotations

import ftplib
import re
import io
from collections.abc import Callable, Iterable
from datetime import date, datetime
from typing import Any

from stonks.execution.brokers.ibkr.contracts import ticker_for_symbol
from stonks.ingest.schemas import BorrowRateRow, FinancialStatementsBundle, RawPriceBar
from stonks.ingest.sources.base import (
    DataSource,
    DataSourceError,
    UnsupportedCapabilityError,
)
from stonks.logging import get_logger

_log = get_logger("stonks.ingest.sources.ibkr_borrow")

SOURCE_ID = "ibkr_borrow"
DEFAULT_HOST = "ftp2.interactivebrokers.com"
#: IBKR's shared, public login for the short stock files (no password).
DEFAULT_USER = "shortstock"

#: IBKR file name (without ``.txt``) -> our exchange suffix.
SHORT_STOCK_MARKETS: dict[str, str] = {
    "usa": "US",
    "canada": "TO",
    "uk": "LSE",
    "germany": "XETRA",
    "france": "PA",
    "dutch": "AS",
    "belgium": "BR",
    "spain": "MC",
    "italy": "MI",
    "swiss": "SW",
}

TextFetcher = Callable[[str], str]

#: The symbol part of a stock ticker we name (``AAPL``, ``BRK-B``, ``BT-A``).
_LISTED = re.compile(r"^[A-Z0-9]{1,6}(-[A-Z0-9]{1,3})?$")
#: An ISIN. The file masks some (``XXXXXXX3AB46``): those are dropped.
_ISIN = re.compile(r"^(?!XX)[A-Z]{2}[A-Z0-9]{9}[0-9]$")


class ShortStockFileError(DataSourceError):
    """The file is not a short stock file we can read."""


def parse_short_stock_file(
    text: str,
    market: str,
    *,
    source: str = SOURCE_ID,
    fallback_day: date | None = None,
) -> list[BorrowRateRow]:
    """Rows of one market's file. Rows that do not parse (a short line, a
    symbol no ticker maps to) are skipped and counted in the log."""
    suffix = SHORT_STOCK_MARKETS.get(market.strip().lower())
    if suffix is None:
        raise ShortStockFileError(
            f"unknown short stock market {market!r}; choose one of {sorted(SHORT_STOCK_MARKETS)}"
        )
    day: date | None = None
    header: list[str] | None = None
    rows: list[BorrowRateRow] = []
    skipped = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#BOF"):
            day = _bof_day(line)
            continue
        if line.startswith("#SYM"):
            header = [c.strip().upper() for c in line.lstrip("#").split("|")]
            continue
        if line.startswith("#"):
            continue
        if header is None:
            raise ShortStockFileError("the short stock file has no #SYM header line")
        when = day or fallback_day
        if when is None:
            raise ShortStockFileError("the short stock file has no #BOF date line")
        row = _row(line.split("|"), header, suffix, when, source)
        if row is None:
            skipped += 1
            continue
        rows.append(row)
    if header is None:
        raise ShortStockFileError("the short stock file has no #SYM header line")
    if skipped:
        _log.info("ibkr_borrow.rows_skipped", market=market, skipped=skipped, kept=len(rows))
    return rows


def _bof_day(line: str) -> date | None:
    parts = line.split("|")
    if len(parts) < 2:
        return None
    try:
        return datetime.strptime(parts[1].strip(), "%Y.%m.%d").date()
    except ValueError:
        return None


def _row(
    cells: list[str], header: list[str], suffix: str, day: date, source: str
) -> BorrowRateRow | None:
    def cell(name: str) -> str | None:
        try:
            value = cells[header.index(name)].strip()
        except (ValueError, IndexError):
            return None
        return value or None

    symbol = cell("SYM")
    fee = _percent(cell("FEERATE"))
    if symbol is None or fee is None or fee < 0:
        return None
    ticker = ticker_for_symbol(symbol, suffix)
    if ticker is None or not _LISTED.match(ticker.rpartition(".")[0]):
        return None  # a bond CUSIP or an odd listing: not a stock we name
    currency = (cell("CUR") or "").upper()
    isin = cell("ISIN")
    return BorrowRateRow(
        ticker=ticker,
        as_of=day,
        fee_rate_annual=fee,
        rebate_rate_annual=_percent(cell("REBATERATE")),
        available_shares=_available(cell("AVAILABLE")),
        currency=currency if len(currency) == 3 and currency.isalpha() else None,
        isin=isin if isin is not None and _ISIN.match(isin.upper()) else None,
        source=source,
    )


def _percent(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value) / 100.0
    except ValueError:
        return None


def _available(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value.lstrip("><=").replace(",", ""))
    except ValueError:
        return None
    return number if number >= 0 else None


def ftp_fetcher(
    host: str = DEFAULT_HOST, user: str = DEFAULT_USER, timeout: float = 30.0
) -> TextFetcher:
    """Reads one file from IBKR's public FTP site (standard library)."""

    def fetch(name: str) -> str:
        buffer = io.BytesIO()
        with ftplib.FTP(host, timeout=timeout) as ftp:
            ftp.login(user=user, passwd="")
            ftp.retrbinary(f"RETR {name}", buffer.write)
        return buffer.getvalue().decode("utf-8", errors="replace")

    return fetch


class IbkrBorrowDataSource(DataSource):
    """Borrow rates only. Every other capability is unsupported. Built from
    ``[sources.ibkr_borrow]`` by :meth:`from_config`. It is not one of the
    ``--source`` price sources, since it serves no bars."""

    source_id = SOURCE_ID

    def __init__(
        self,
        *,
        host: str = DEFAULT_HOST,
        user: str = DEFAULT_USER,
        timeout_seconds: float = 30.0,
        fetch_text: TextFetcher | None = None,
    ) -> None:
        self._fetch = fetch_text or ftp_fetcher(host, user, timeout_seconds)

    @classmethod
    def from_config(cls, cfg: Any) -> IbkrBorrowDataSource:
        """From ``[sources.ibkr_borrow]`` (``IbkrBorrowSourceConfig``)."""
        return cls(host=cfg.host, user=cfg.user, timeout_seconds=cfg.timeout_seconds)

    def fetch_borrow_rates(self, market: str) -> Iterable[BorrowRateRow]:
        name = market.strip().lower()
        if name not in SHORT_STOCK_MARKETS:
            raise ShortStockFileError(
                f"unknown short stock market {market!r}; "
                f"choose one of {sorted(SHORT_STOCK_MARKETS)}"
            )
        try:
            text = self._fetch(f"{name}.txt")
        except (OSError, EOFError, ftplib.Error) as exc:
            raise DataSourceError(f"short stock file {name}: {type(exc).__name__}: {exc}") from exc
        return parse_short_stock_file(text, name, source=self.source_id)

    def list_tickers(self, exchange: str) -> list[str]:
        raise UnsupportedCapabilityError(f"{self.source_id} serves borrow rates only")

    def fetch_prices(
        self, ticker: str, since: date | None = None, until: date | None = None
    ) -> Iterable[RawPriceBar]:
        raise UnsupportedCapabilityError(f"{self.source_id} serves borrow rates only")

    def fetch_fundamentals(self, ticker: str) -> FinancialStatementsBundle:
        raise UnsupportedCapabilityError(f"{self.source_id} serves borrow rates only")
