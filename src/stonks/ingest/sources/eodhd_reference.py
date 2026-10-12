"""EODHD reference endpoints: pure parsers plus the request helpers
:class:`~stonks.ingest.sources.eodhd.EodhdDataSource` calls. Shapes were
captured live on 2026-10-12 (docs/backfill.md has the costs).

- ``GET /fundamentals/{CODE}.INDX``: ``General`` (``Name``), ``Components``
  (the current members, ``{Code, Exchange, Weight}``) and
  ``HistoricalTickerComponents`` (``{Code, StartDate, EndDate}``, no
  exchange). 10 calls.
- ``GET /exchange-details/{EX}?from&to``: ``Timezone``, ``OperatingMIC``,
  ``TradingHours`` (``Open``, ``Close``, ``OpenUTC``, ``CloseUTC``,
  ``WorkingDays``), ``ExchangeHolidays`` and ``ExchangeEarlyCloseDays``
  (``{Holiday, Date, Type, EarlyClose}``). ``from``/``to`` widen the
  holiday window (the default is about six months each way).
- ``GET /ust/{bill-rates|yield-rates|real-yield-rates|long-term-rates}
  ?filter[year]``: ``{"data": [...], "links": {"next"}}``, one year a call.
- ``GET /calendar/ipos?from&to`` and ``/calendar/splits?from&to``:
  ``{"ipos": [...]}`` and ``{"splits": [...]}``.
- ``GET /id-mapping?filter[ex]&page[limit]&page[offset]``: ``{"meta":
  {"total"}, "data": [{symbol, isin, figi, lei, cusip, cik}]}``.

Nothing outside this module sees the vendor's field names.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from datetime import date, time
from typing import Any

from stonks.ingest.reference_schemas import (
    ExchangeDetailsRow,
    ExchangeHolidayRow,
    HolidayKind,
    IdentifierPage,
    IndexChangeRow,
    IndexComponentsRow,
    InstrumentIdentifiersRow,
    IpoEventRow,
    IpoStatus,
    SplitEventRow,
    TreasuryCurve,
    TreasuryMeasure,
    TreasuryRateRow,
)
from stonks.ingest.sources.base import DataSourceError
from stonks.ingest.sources.eodhd import (
    _check_free_tier,
    _coerce_optional_float,
    _log_parse_drops,
    _log_unknown_vendor_value,
    _parse_date,
)

__all__ = [
    "IndexChangeRow",
    "fetch_exchange_details",
    "fetch_identifiers",
    "fetch_index_components",
    "fetch_ipos",
    "fetch_splits",
    "fetch_treasury",
    "index_id_for",
    "parse_exchange_details",
    "parse_index_components",
    "parse_ipos",
    "parse_splits",
    "parse_treasury",
    "vendor_index_code",
]

Getter = Callable[[str, dict[str, str]], Any]

#: Vendor index codes whose canonical id is a name Stonks already uses.
_INDEX_ALIASES: dict[str, str] = {
    "GSPC": "sp500",
    "NDX": "nasdaq100",
    "DJI": "dow_jones_industrial",
    "RUT": "russell2000",
}
_ALIAS_CODES = {v: k for k, v in _INDEX_ALIASES.items()}

#: Each Treasury curve's path.
TREASURY_PATHS: dict[TreasuryCurve, str] = {
    "bill": "/ust/bill-rates",
    "par_nominal": "/ust/yield-rates",
    "par_real": "/ust/real-yield-rates",
    "long_term": "/ust/long-term-rates",
}

#: Bill columns and the measure each one is.
_BILL_MEASURES: dict[str, TreasuryMeasure] = {
    "discount": "bank_discount",
    "coupon": "coupon_equivalent",
    "avg_discount": "bank_discount_avg",
    "avg_coupon": "coupon_equivalent_avg",
}

#: Long-term ``rate_type`` values as ``(curve, tenor)``.
_LONG_TERM: dict[str, tuple[TreasuryCurve, str]] = {
    "BC_20year": ("long_term", "20Y"),
    "Over_10_Years": ("long_term", "10Y+"),
    "Real_Rate": ("long_term_real", "10Y+"),
}

_IPO_STATUS: dict[str, IpoStatus] = {
    "expected": "expected",
    "filed": "filed",
    "priced": "priced",
    "amended": "amended",
    "withdrawn": "withdrawn",
}

_HOLIDAY_KINDS: dict[str, HolidayKind] = {"official": "official", "bank": "bank"}

_WEEKDAYS = {"mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 7}

#: Rows per ``/id-mapping`` page (the vendor maximum).
ID_PAGE_LIMIT = 1000


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _items(value: Any) -> list[Any]:
    """EODHD sends lists as ``{"0": {...}, "1": {...}}`` or as a list."""
    if isinstance(value, dict):
        return list(value.values())
    if isinstance(value, list):
        return value
    return []


def _time(value: Any) -> time | None:
    text = _text(value)
    if text is None:
        return None
    try:
        return time.fromisoformat(text)
    except ValueError:
        return None


def _positive(value: Any) -> float | None:
    number = _coerce_optional_float(value)
    return number if number is not None and number > 0 else None


# ---- indices ------------------------------------------------------------------------


def index_id_for(code: str) -> str:
    """The canonical ``lower_snake_case`` id of a vendor index code."""
    code = code.strip().upper()
    if code in _INDEX_ALIASES:
        return _INDEX_ALIASES[code]
    return re.sub(r"[^a-z0-9]+", "_", code.lower()).strip("_")[:64] or "index"


def vendor_index_code(index_id: str) -> str:
    """The vendor code of a canonical index id (the inverse of
    :func:`index_id_for` for codes made of letters and digits)."""
    return _ALIAS_CODES.get(index_id, index_id.upper())


def parse_index_components(payload: Any, code: str, *, as_of: date) -> IndexComponentsRow:
    """Members on ``as_of`` and the dated changes of one index. Members are
    the history's open spans (the current components when there is no
    history). Past members carry no exchange, so they take the exchange of
    the current component with the same code, else the exchange most
    current components trade on."""
    _check_free_tier(payload)
    if not isinstance(payload, dict):
        raise DataSourceError(f"unexpected index payload for {code}: {type(payload).__name__}")
    general = _dict(payload.get("General"))
    members: list[str] = []
    exchange_of: dict[str, str] = {}
    for row in _items(payload.get("Components")):
        if not isinstance(row, dict):
            continue
        symbol, exchange = _text(row.get("Code")), _text(row.get("Exchange"))
        if symbol is None or exchange is None:
            continue
        exchange_of[symbol.upper()] = exchange.upper()
        members.append(f"{symbol.upper()}.{exchange.upper()}")
    common = Counter(exchange_of.values()).most_common(1)
    fallback = common[0][0] if common else ("US" if general.get("CountryISO") == "US" else None)
    changes: list[IndexChangeRow] = []
    current: list[str] = []
    kept = dropped = 0
    for row in _items(payload.get("HistoricalTickerComponents")):
        symbol = _text(row.get("Code")) if isinstance(row, dict) else None
        start = _parse_date(row.get("StartDate")) if isinstance(row, dict) else None
        exchange = exchange_of.get(symbol.upper(), fallback) if symbol else None
        if symbol is None or start is None or exchange is None:
            dropped += 1
            continue
        ticker = f"{symbol.upper()}.{exchange}"
        changes.append(IndexChangeRow(ticker=ticker, change_date=start, action="add"))
        end = _parse_date(row.get("EndDate"))
        if end is not None:
            changes.append(IndexChangeRow(ticker=ticker, change_date=end, action="remove"))
        if end is None or end > as_of:
            current.append(ticker)
        kept += 1
    _log_parse_drops("parse_index_components", code, kept, dropped)
    # The history's open spans are the members it agrees with. Components
    # can lag it by days, and a snapshot that disagrees with the changes
    # would drop the names in between, so it is used only without a history.
    return IndexComponentsRow(
        index_id=index_id_for(code),
        as_of=as_of,
        name=_text(general.get("Name")),
        members=tuple(dict.fromkeys(current if kept else members)),
        changes=tuple(dict.fromkeys(changes)),
    )


def fetch_index_components(get: Getter, code: str, as_of: date) -> IndexComponentsRow:
    payload = get(f"/fundamentals/{code.upper()}.INDX", {"fmt": "json"})
    return parse_index_components(payload, code, as_of=as_of)


# ---- exchanges ----------------------------------------------------------------------


def _holiday(row: Any, early: bool) -> ExchangeHolidayRow | None:
    if not isinstance(row, dict):
        return None
    day = _parse_date(row.get("Date"))
    if day is None:
        return None
    raw = (_text(row.get("Type")) or "").lower()
    kind = _HOLIDAY_KINDS.get(raw, "other")
    if raw and raw not in _HOLIDAY_KINDS:
        _log_unknown_vendor_value("eodhd.exchange.unknown_holiday_type", raw)
    return ExchangeHolidayRow(
        holiday_date=day,
        name=_text(row.get("Holiday")),
        kind=kind,
        early_close=_time(row.get("EarlyClose")) if early else None,
    )


def parse_exchange_details(payload: Any, exchange: str) -> ExchangeDetailsRow:
    _check_free_tier(payload)
    if not isinstance(payload, dict):
        raise DataSourceError(
            f"unexpected exchange details for {exchange}: {type(payload).__name__}"
        )
    hours = _dict(payload.get("TradingHours"))
    days = _text(hours.get("WorkingDays")) or ""
    working = tuple(
        sorted({_WEEKDAYS[d[:3]] for d in days.lower().split(",") if d.strip()[:3] in _WEEKDAYS})
    )
    holidays: dict[date, ExchangeHolidayRow] = {}
    for row in _items(payload.get("ExchangeHolidays")):
        parsed = _holiday(row, early=False)
        if parsed is not None:
            holidays[parsed.holiday_date] = parsed
    for row in _items(payload.get("ExchangeEarlyCloseDays")):
        parsed = _holiday(row, early=True)
        if parsed is not None:
            holidays[parsed.holiday_date] = parsed
    mics = tuple(m.strip() for m in (_text(payload.get("OperatingMIC")) or "").split(",") if m)
    return ExchangeDetailsRow(
        exchange=exchange.upper(),
        name=_text(payload.get("Name")),
        operating_mics=tuple(m for m in mics if m),
        country=_text(payload.get("Country")),
        currency=_text(payload.get("Currency")),
        timezone=_text(payload.get("Timezone")),
        open_local=_time(hours.get("Open")),
        close_local=_time(hours.get("Close")),
        open_utc=_time(hours.get("OpenUTC")),
        close_utc=_time(hours.get("CloseUTC")),
        working_days=working,
        holidays=tuple(holidays[d] for d in sorted(holidays)),
    )


def fetch_exchange_details(
    get: Getter, exchange: str, start: date, end: date
) -> ExchangeDetailsRow:
    params = {"from": start.isoformat(), "to": end.isoformat(), "fmt": "json"}
    payload = get(f"/exchange-details/{exchange.upper()}", params)
    return parse_exchange_details(payload, exchange)


# ---- US Treasury --------------------------------------------------------------------


def _tenor(raw: str) -> str:
    """``4WK`` to ``4W``; ``1M``, ``1.5M`` and ``10Y`` stay."""
    tenor = raw.strip().upper().replace(" ", "")
    return tenor[:-1] if tenor.endswith("WK") else tenor


def parse_treasury(curve: TreasuryCurve, payload: Any) -> list[TreasuryRateRow]:
    """Rows of one Treasury page. ``long_term`` pages also hold the
    long-term real rate (curve ``long_term_real``)."""
    _check_free_tier(payload)
    items = payload.get("data") if isinstance(payload, dict) else None
    out: list[TreasuryRateRow] = []
    kept = dropped = 0
    for row in items if isinstance(items, list) else []:
        day = _parse_date(row.get("date")) if isinstance(row, dict) else None
        if day is None:
            dropped += 1
            continue
        if curve == "bill":
            tenor = _text(row.get("tenor"))
            if tenor is None:
                dropped += 1
                continue
            for column, measure in _BILL_MEASURES.items():
                out.append(
                    TreasuryRateRow(
                        curve="bill",
                        tenor=_tenor(tenor),
                        measure=measure,
                        observation_date=day,
                        rate=_coerce_optional_float(row.get(column)),
                        maturity_date=_parse_date(row.get("maturity_date")),
                        cusip=_text(row.get("cusip")),
                    )
                )
        elif curve == "long_term":
            raw = _text(row.get("rate_type"))
            mapped = _LONG_TERM.get(raw or "")
            if mapped is None:
                if raw:
                    _log_unknown_vendor_value("eodhd.treasury.unknown_rate_type", raw)
                dropped += 1
                continue
            out.append(
                TreasuryRateRow(
                    curve=mapped[0],
                    tenor=mapped[1],
                    observation_date=day,
                    rate=_coerce_optional_float(row.get("rate")),
                )
            )
        else:
            tenor = _text(row.get("tenor"))
            if tenor is None:
                dropped += 1
                continue
            out.append(
                TreasuryRateRow(
                    curve=curve,
                    tenor=_tenor(tenor),
                    observation_date=day,
                    rate=_coerce_optional_float(row.get("rate")),
                )
            )
        kept += 1
    _log_parse_drops("parse_treasury", curve, kept, dropped)
    return out


def fetch_treasury(get: Getter, curve: TreasuryCurve, year: int) -> list[TreasuryRateRow]:
    """Every row of ``curve`` in ``year``, following the next links."""
    path = TREASURY_PATHS.get(curve)
    if path is None:
        raise DataSourceError(f"no Treasury endpoint for curve {curve!r}")
    out: list[TreasuryRateRow] = []
    offset = 0
    while True:
        params = {"filter[year]": str(year), "fmt": "json"}
        if offset:
            params["page[offset]"] = str(offset)
        payload = get(path, params)
        items = payload.get("data") if isinstance(payload, dict) else None
        out.extend(parse_treasury(curve, payload))
        links = payload.get("links") if isinstance(payload, dict) else None
        has_next = isinstance(links, dict) and bool(links.get("next"))
        if not has_next or not isinstance(items, list) or not items:
            return out
        offset += len(items)


# ---- IPO and split calendars --------------------------------------------------------


def parse_ipos(payload: Any) -> list[IpoEventRow]:
    _check_free_tier(payload)
    items = payload.get("ipos") if isinstance(payload, dict) else None
    out: list[IpoEventRow] = []
    dropped = 0
    for row in items if isinstance(items, list) else []:
        ticker = _text(row.get("code")) if isinstance(row, dict) else None
        if ticker is None:
            dropped += 1
            continue
        raw = (_text(row.get("deal_type")) or "").lower()
        status = _IPO_STATUS.get(raw, "other")
        if raw and raw not in _IPO_STATUS:
            _log_unknown_vendor_value("eodhd.ipos.unknown_deal_type", raw)
        out.append(
            IpoEventRow(
                ticker=ticker,
                name=_text(row.get("name")),
                currency=_text(row.get("currency")),
                filing_date=_parse_date(row.get("filing_date")),
                amended_date=_parse_date(row.get("amended_date")),
                listing_date=_parse_date(row.get("start_date")),
                price_low=_positive(row.get("price_from")),
                price_high=_positive(row.get("price_to")),
                offer_price=_positive(row.get("offer_price")),
                shares=_positive(row.get("shares")),
                status=status,
            )
        )
    _log_parse_drops("parse_ipos", "*", len(out), dropped)
    return out


def parse_splits(payload: Any) -> list[SplitEventRow]:
    _check_free_tier(payload)
    items = payload.get("splits") if isinstance(payload, dict) else None
    out: list[SplitEventRow] = []
    dropped = 0
    for row in items if isinstance(items, list) else []:
        ticker = _text(row.get("code")) if isinstance(row, dict) else None
        day = _parse_date(row.get("split_date")) if isinstance(row, dict) else None
        if ticker is None or day is None:
            dropped += 1
            continue
        flag = (_text(row.get("optionable")) or "").upper()
        out.append(
            SplitEventRow(
                ticker=ticker,
                split_date=day,
                old_shares=_positive(row.get("old_shares")),
                new_shares=_positive(row.get("new_shares")),
                optionable=True if flag == "Y" else False if flag == "N" else None,
            )
        )
    _log_parse_drops("parse_splits", "*", len(out), dropped)
    return out


def fetch_ipos(get: Getter, start: date, end: date) -> list[IpoEventRow]:
    params = {"from": start.isoformat(), "to": end.isoformat(), "fmt": "json"}
    return parse_ipos(get("/calendar/ipos", params))


def fetch_splits(get: Getter, start: date, end: date) -> list[SplitEventRow]:
    params = {"from": start.isoformat(), "to": end.isoformat(), "fmt": "json"}
    return parse_splits(get("/calendar/splits", params))


# ---- identifiers --------------------------------------------------------------------


def _cik(value: Any) -> str | None:
    text = _text(value)
    if text is None or not text.isdigit():
        return None
    return text.zfill(10)


def fetch_identifiers(
    get: Getter, exchange: str, *, offset: int, limit: int = ID_PAGE_LIMIT
) -> IdentifierPage:
    """One page of an exchange's identifiers. Rows with no identifier at
    all are left out (they would only say nothing)."""
    params = {
        "filter[ex]": exchange.upper(),
        "page[limit]": str(limit),
        "page[offset]": str(offset),
        "fmt": "json",
    }
    payload = get("/id-mapping", params)
    _check_free_tier(payload)
    if not isinstance(payload, dict):
        raise DataSourceError(f"unexpected id-mapping payload: {type(payload).__name__}")
    meta = _dict(payload.get("meta"))
    rows: list[InstrumentIdentifiersRow] = []
    items = _items(payload.get("data"))
    for row in items:
        ticker = _text(row.get("symbol")) if isinstance(row, dict) else None
        if ticker is None:
            continue
        ids = InstrumentIdentifiersRow(
            ticker=ticker,
            isin=_text(row.get("isin")),
            cusip=_text(row.get("cusip")),
            cik=_cik(row.get("cik")),
            open_figi=_text(row.get("figi")),
            lei=_text(row.get("lei")),
        )
        if any((ids.isin, ids.cusip, ids.cik, ids.open_figi, ids.lei)):
            rows.append(ids)
    total = _coerce_optional_float(meta.get("total"))
    return IdentifierPage(
        rows=tuple(rows), size=len(items), total=int(total) if total is not None else 0
    )
