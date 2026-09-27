"""Option chain ingest (roadmap 17.1).

``ingest_option_quotes`` asks a :class:`DataSource` for the EOD option
quotes of each underlying and writes them through :class:`OptionStore`.
One ``ingest_runs`` row (kind ``options``) per run; a failing underlying
is a soft fail (logged and counted), as for bars.

Size control (design section 2): ``max_expiry_days`` drops far expiries
and ``strike_band`` keeps strikes within that fraction of the
underlying's price (the vendor's ``underlying_price``, else the lake's
close of the day). Both are off when ``None``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

import requests
from pydantic import ValidationError

from stonks.ingest.option_schemas import OptionQuoteRow
from stonks.ingest.sources.base import DataSource, DataSourceError
from stonks.logging import get_logger
from stonks.options.store import OptionStore

if TYPE_CHECKING:  # pragma: no cover
    from stonks.store.lake import DuckDBLake

_log = get_logger("stonks.options.ingest")

_SOFT_ERRORS = (DataSourceError, requests.RequestException, json.JSONDecodeError, ValidationError)


@dataclass(frozen=True)
class OptionIngestResult:
    run_id: int
    status: str
    rows: int
    ok: tuple[str, ...] = ()
    failed: dict[str, str] = field(default_factory=dict[str, str])


def filter_rows(
    rows: Iterable[OptionQuoteRow],
    *,
    max_expiry_days: int | None = None,
    strike_band: float | None = None,
    closes: dict[date, float] | None = None,
) -> list[OptionQuoteRow]:
    """The rows inside the expiry horizon and the strike band."""
    out: list[OptionQuoteRow] = []
    for r in rows:
        if r.expiry < r.as_of:
            continue
        if max_expiry_days is not None and (r.expiry - r.as_of).days > max_expiry_days:
            continue
        if strike_band is not None:
            spot = r.underlying_price or (closes or {}).get(r.as_of)
            if spot and abs(r.strike / spot - 1.0) > strike_band:
                continue
        out.append(r)
    return out


def _closes(
    lake: DuckDBLake, ticker: str, since: date | None, until: date | None
) -> dict[date, float]:
    frame = lake.get_prices(ticker, since or date(1900, 1, 1), until or date(2999, 12, 31))
    return {d: float(c) for d, c in zip(frame["date"], frame["close"], strict=True)}


def ingest_option_quotes(
    source: DataSource,
    lake: DuckDBLake,
    underlyings: Sequence[str],
    *,
    since: date | None = None,
    until: date | None = None,
    max_expiry_days: int | None = None,
    strike_band: float | None = None,
) -> OptionIngestResult:
    store = OptionStore(lake)
    run_id = lake.open_ingest_run(source.source_id, "options")
    ok: list[str] = []
    failed: dict[str, str] = {}
    total = 0
    for underlying in underlyings:
        try:
            rows = list(source.fetch_option_quotes(underlying, since, until))
            closes = _closes(lake, underlying, since, until) if strike_band is not None else None
            rows = filter_rows(
                rows, max_expiry_days=max_expiry_days, strike_band=strike_band, closes=closes
            )
            total += store.upsert_quotes(rows, source.source_id)
            ok.append(underlying)
            _log.info("options.ingest.ok", underlying=underlying, rows=len(rows), run_id=run_id)
        except _SOFT_ERRORS as exc:
            failed[underlying] = f"{type(exc).__name__}: {exc}"
            _log.warning(
                "options.ingest.failed", underlying=underlying, error=str(exc), run_id=run_id
            )
    status = "ok" if not failed else ("partial" if ok else "error")
    lake.close_ingest_run(
        run_id,
        len(ok),
        len(failed),
        status,
        error=next(iter(failed.values())) if failed else None,
    )
    return OptionIngestResult(run_id, status, total, tuple(ok), failed)
