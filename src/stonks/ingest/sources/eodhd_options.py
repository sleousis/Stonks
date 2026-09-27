"""EODHD US options adapter (roadmap 17.1).

EODHD sells US options as a Marketplace product ("US Stock Options Data
API" by Unicornbay, about 6,000 US underlyings, two years of end-of-day
history, bid, ask, last, volume, open interest, implied vol and the five
Greeks). It is a separate subscription, not part of the All-In-One plan.

Endpoint: ``GET {base}/mp/unicornbay/options/eod`` with
``filter[underlying_symbol]``, ``filter[tradetime_from]``,
``filter[tradetime_to]``, ``page[offset]`` and ``page[limit]`` (1000 at
most). The JSON body is ``{"meta": {"offset", "limit", "total"}, "data":
[{"attributes": {...}}], "links": {"next": ...}}``; each ``attributes``
holds ``contract`` (the compact OCC symbol), ``underlying_symbol``,
``exp_date``, ``type``, ``strike``, ``bid``, ``ask``, ``last``, ``volume``,
``open_interest``, ``volatility``, ``delta``, ``gamma``, ``theta``,
``vega``, ``rho`` and ``tradetime``.

The adapter maps those into :class:`OptionQuoteRow`: the underlying keeps
our ticker (``AAPL.US``), a zero vol or Greek becomes ``None``, and rows it
cannot read are dropped and counted in the log. Only US underlyings are
served.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date
from typing import Any

from pydantic import ValidationError

from stonks.core.options import parse_occ_symbol
from stonks.ingest.option_schemas import OptionQuoteRow
from stonks.ingest.sources.base import DataSourceError, UnsupportedCapabilityError
from stonks.logging import get_logger

_log = get_logger("stonks.ingest.sources.eodhd_options")

EOD_PATH = "/mp/unicornbay/options/eod"
PAGE_LIMIT = 1000
#: Guard against a vendor that never stops paging.
MAX_PAGES = 500

_RIGHTS = {"call": "call", "put": "put", "c": "call", "p": "put"}


def vendor_symbol(underlying: str) -> str:
    """``AAPL.US`` -> ``AAPL``. Raises for a non-US underlying."""
    root, _, suffix = underlying.partition(".")
    if suffix and suffix.upper() != "US":
        raise UnsupportedCapabilityError(
            f"EODHD options cover US underlyings only, got {underlying!r}"
        )
    return root.upper()


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None  # NaN is missing


def _positive(value: Any) -> float | None:
    out = _num(value)
    return out if out is not None and out > 0 else None


def _date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def parse_eod_row(underlying: str, attributes: dict[str, Any]) -> OptionQuoteRow | None:
    """One ``attributes`` object as a row, or ``None`` when unreadable."""
    right = _RIGHTS.get(str(attributes.get("type", "")).lower())
    expiry = _date(attributes.get("exp_date"))
    as_of = _date(attributes.get("tradetime"))
    strike = _positive(attributes.get("strike"))
    occ = attributes.get("contract")
    if occ and (right is None or expiry is None or strike is None):
        try:
            _, expiry_occ, right_occ, strike_occ = parse_occ_symbol(str(occ))
        except ValueError:
            return None
        right, expiry, strike = right or right_occ, expiry or expiry_occ, strike or strike_occ
    if right is None or expiry is None or strike is None or as_of is None:
        return None
    iv = _positive(attributes.get("volatility"))

    def greek(name: str) -> float | None:
        return _num(attributes.get(name)) if iv is not None else None

    try:
        return OptionQuoteRow(
            underlying=underlying,
            expiry=expiry,
            strike=strike,
            right=right,  # type: ignore[arg-type]
            currency=attributes.get("currency") or "USD",
            exchange=attributes.get("exchange") or None,
            occ_symbol=str(occ) if occ else None,
            as_of=as_of,
            bid=_positive(attributes.get("bid")),
            ask=_positive(attributes.get("ask")),
            last=_positive(attributes.get("last")),
            volume=_num(attributes.get("volume")),
            open_interest=_num(attributes.get("open_interest")),
            underlying_price=_positive(attributes.get("underlying_price")),
            iv=iv,
            delta=greek("delta"),
            gamma=greek("gamma"),
            theta=greek("theta"),
            vega=greek("vega"),
            rho=greek("rho"),
        )
    except ValidationError:
        return None


def parse_eod_page(underlying: str, payload: Any) -> tuple[list[OptionQuoteRow], int, bool]:
    """``(rows, rows_on_page, has_more)`` of one page. Raises
    :class:`DataSourceError` for a body that is not an options page."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise DataSourceError(f"unexpected EODHD options payload for {underlying!r}")
    data: list[Any] = payload["data"]
    rows: list[OptionQuoteRow] = []
    dropped = 0
    for item in data:
        attrs = item.get("attributes") if isinstance(item, dict) else None
        row = parse_eod_row(underlying, attrs) if isinstance(attrs, dict) else None
        if row is None:
            dropped += 1
        else:
            rows.append(row)
    if dropped:
        _log.warning("eodhd.options.rows_dropped", underlying=underlying, dropped=dropped)
    meta = payload.get("meta") or {}
    links = payload.get("links") or {}
    total = meta.get("total")
    offset = int(meta.get("offset") or 0)
    if isinstance(total, int | float):
        has_more = offset + len(data) < total
    else:
        has_more = bool(links.get("next"))
    return rows, len(data), has_more and len(data) > 0


def fetch_option_quotes(
    get: Callable[[str, dict[str, str]], Any],
    base_url: str,
    underlying: str,
    since: date | None = None,
    until: date | None = None,
) -> Iterator[OptionQuoteRow]:
    """Every page of the EOD endpoint for ``underlying`` in ``[since,
    until]``. ``get(url, params)`` is the source's retrying JSON getter."""
    params: dict[str, str] = {"filter[underlying_symbol]": vendor_symbol(underlying)}
    if since is not None:
        params["filter[tradetime_from]"] = since.isoformat()
    if until is not None:
        params["filter[tradetime_to]"] = until.isoformat()
    url = f"{base_url}{EOD_PATH}"
    offset = 0
    for _ in range(MAX_PAGES):
        page = get(url, {**params, "page[offset]": str(offset), "page[limit]": str(PAGE_LIMIT)})
        rows, count, has_more = parse_eod_page(underlying, page)
        yield from rows
        if not has_more:
            return
        offset += count
    _log.warning("eodhd.options.page_limit_reached", underlying=underlying, pages=MAX_PAGES)
