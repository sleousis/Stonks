"""``[ingest.backfill]`` settings for ``stonks ingest backfill``
(docs/backfill.md). A module of its own, with no pipeline imports, so
``stonks.config`` can load it without an import cycle."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field


def _intraday_start() -> dict[str, date]:
    # what EODHD documents for US tickers: 1m from 2004, 5m and 1h from October 2020
    return {"1h": date(2020, 10, 1), "5m": date(2020, 10, 1), "1m": date(2004, 1, 1)}


def _intraday_window_days() -> dict[str, int]:
    # the longest range one EODHD intraday request may span
    return {"1h": 7200, "5m": 600, "1m": 120}


class BackfillSettings(BaseModel):
    """How the backfill spends the vendor's daily allowance and the PC's
    memory. Every field has a safe default for a small machine."""

    model_config = ConfigDict(extra="forbid")

    #: API calls a UTC day may reach, counted the vendor's way (the plan's own
    #: limit still caps it). Other processes on the same key count too.
    daily_budget: int = Field(default=95_000, ge=1)
    #: Requests a minute (EODHD allows 1000).
    requests_per_minute: int = Field(default=900, ge=1, le=1000)
    #: Requests in flight at once.
    network_threads: int = Field(default=2, ge=1, le=2)
    #: Tries per unit before it is marked failed.
    max_attempts: int = Field(default=4, ge=1, le=20)
    #: Wait before the first retry; it doubles with each attempt.
    retry_backoff_minutes: float = Field(default=30.0, gt=0)
    #: How often the vendor's own count of today's calls is read again.
    usage_check_seconds: float = Field(default=600.0, gt=0)
    #: Minutes to wait after midnight UTC before spending the new day.
    reset_margin_minutes: int = Field(default=10, ge=0, le=180)
    #: DuckDB limits while the backfill holds the lake.
    duckdb_memory_limit: str = Field(default="1GB", min_length=2, max_length=16)
    duckdb_threads: int = Field(default=2, ge=1, le=64)

    #: Exchanges whose symbols are not instruments: forex pairs go to
    #: ``fx_rates`` (the fx phase), and money market rates have no asset class.
    skip_exchanges: list[str] = Field(default_factory=lambda: ["FOREX", "MONEY"])
    #: Exchange codes to list besides those the exchange list names.
    extra_exchanges: list[str] = Field(default_factory=list)
    #: Days before the exchanges and their symbol lists are listed again.
    symbols_relist_days: int = Field(default=7, ge=1)

    #: Days the bulk top-up looks back at most.
    topup_days: int = Field(default=10, ge=1, le=60)

    #: Market cap history for delisted stocks too (EODHD's series starts in
    #: 2021, so most delisted names return nothing).
    market_cap_delisted: bool = False
    #: Exchanges whose stocks get insider transactions (10 calls each). Off by
    #: default: on 2026-10-12 EODHD answered empty lists for AAPL both from
    #: ``/insider-transactions`` and inside ``/fundamentals``. SEC filings
    #: (``stonks ingest edgar``) carry US insider trades.
    insider_exchanges: list[str] = Field(default_factory=list)

    #: First day of the earnings and dividend calendars and economic events.
    calendar_start: date = date(2000, 1, 1)
    #: Countries for macro indicators (ISO alpha-3), ``None``: every country.
    macro_countries: list[str] | None = None
    #: Macro indicators, ``None``: every one the source lists.
    macro_indicators: list[str] | None = None
    #: Articles per news request.
    news_limit: int = Field(default=1000, ge=1, le=1000)

    #: Exchanges whose common stocks and ETFs get intraday bars.
    intraday_exchanges: list[str] = Field(default_factory=lambda: ["US"])
    intraday_delisted: bool = False
    intraday_start: dict[str, date] = Field(default_factory=_intraday_start)
    intraday_window_days: dict[str, int] = Field(default_factory=_intraday_window_days)
