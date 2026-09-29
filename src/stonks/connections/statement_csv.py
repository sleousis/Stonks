"""CSV statements from brokers without an API (roadmap 23.17).

A person maps the columns of their broker's CSV export onto the fields of
an :class:`~stonks.connections.base.Activity`, the same shape every broker
connection syncs. :func:`parse_statement` turns each row into an activity
or says why it was skipped. Nothing here touches the database: the import
service writes the activities into ``broker_activities``.

Duplicate protection rests on the activity id. It is a hash of the row's
meaning (kind, day, symbol, quantity, price, amount, fee) plus how many
identical rows came before it in the same file. The same file imported
twice gives the same ids, while two genuinely identical fills in one file
stay two activities.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks.connections.base import ACTIVITY_KINDS, Activity, ActivityKind
from stonks.execution.brokers.symbols import to_canonical_ticker

#: Most rows one CSV may hold.
MAX_ROWS = 20_000
#: Date formats tried in order when the mapping names none.
DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d.%m.%Y", "%Y%m%d")

_CANONICAL = re.compile(r"^[A-Z0-9][A-Z0-9-]*\.[A-Z]{2,5}$")
_NUMBER_JUNK = re.compile(r"[\s$€£]")


class StatementError(ValueError):
    """The CSV or the mapping cannot be read. The message is safe to show."""


class ColumnMapping(BaseModel):
    """Which CSV column holds which field. Only ``date`` is required, plus
    either a ``type`` column (with ``types`` mapping its values to kinds) or
    one fixed ``kind`` for every row."""

    model_config = ConfigDict(extra="forbid")

    date: str = Field(min_length=1, max_length=120)
    type: str | None = Field(default=None, max_length=120)
    kind: ActivityKind | None = None
    symbol: str | None = Field(default=None, max_length=120)
    quantity: str | None = Field(default=None, max_length=120)
    price: str | None = Field(default=None, max_length=120)
    amount: str | None = Field(default=None, max_length=120)
    fee: str | None = Field(default=None, max_length=120)
    currency: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=120)
    #: Values of the type column (upper-cased) to activity kinds.
    types: dict[str, ActivityKind] = Field(default_factory=dict, max_length=100)
    #: Values of the type column that mean a sale: the quantity turns negative.
    sell_values: list[str] = Field(default_factory=list, max_length=50)
    #: A strptime format such as ``%d/%m/%Y``; blank tries the common ones.
    date_format: str | None = Field(default=None, max_length=40)
    #: The listing exchange of bare symbols (``NASDAQ``, ``LSE``); blank
    #: means US listings for USD rows.
    exchange: str | None = Field(default=None, max_length=20)
    #: The currency of rows without a currency column.
    default_currency: str = Field(default="USD", min_length=3, max_length=3)

    @model_validator(mode="after")
    def _kind_source(self) -> Self:
        if self.type is None and self.kind is None:
            raise ValueError("give a type column or a kind for every row")
        self.types = {k.strip().upper(): v for k, v in self.types.items()}
        self.sell_values = [v.strip().upper() for v in self.sell_values]
        return self

    def columns(self) -> list[str]:
        fields = (
            self.date,
            self.type,
            self.symbol,
            self.quantity,
            self.price,
            self.amount,
            self.fee,
            self.currency,
            self.description,
        )
        return [c for c in fields if c]


@dataclass(frozen=True)
class ParsedRow:
    """One CSV row: its line number, the activity it maps to, or why not."""

    line: int
    activity: Activity | None
    skipped: str | None = None


def read_headers(text: str, *, sniff: bool = False) -> list[str]:
    """The first line's cells. ``sniff`` also takes a semicolon as the
    delimiter when the first line has more semicolons than commas."""
    body = _strip_bom(text)
    first = body.split("\n", 1)[0]
    delimiter = ";" if sniff and first.count(";") > first.count(",") else ","
    reader = csv.reader(io.StringIO(body), delimiter=delimiter)
    return [h.strip() for h in next(reader, [])]


def parse_statement(
    text: str, mapping: ColumnMapping, *, max_rows: int = MAX_ROWS
) -> list[ParsedRow]:
    reader = csv.DictReader(io.StringIO(_strip_bom(text)))
    headers = [h.strip() for h in (reader.fieldnames or [])]
    missing = [c for c in mapping.columns() if c not in headers]
    if missing:
        raise StatementError(f"no column named {', '.join(repr(m) for m in missing)} in the CSV")
    seen: Counter[str] = Counter()
    out: list[ParsedRow] = []
    for n, raw in enumerate(reader, start=2):
        if len(out) >= max_rows:
            raise StatementError(f"a statement may hold at most {max_rows} rows")
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        if not any(row.values()):
            continue
        try:
            out.append(ParsedRow(n, _activity(row, mapping, seen)))
        except _Skip as skip:
            out.append(ParsedRow(n, None, str(skip)))
    return out


def guess_mapping(headers: Sequence[str]) -> ColumnMapping:
    """A first mapping from common header names; the person checks it."""

    def pick(*names: str) -> str | None:
        lowered = {h.lower().strip(): h for h in headers}
        for name in names:
            if name in lowered:
                return lowered[name]
        for name in names:
            for low, h in lowered.items():
                if name in low:
                    return h
        return None

    date_col = pick("date", "trade date", "settlement date", "time") or (
        headers[0] if headers else "Date"
    )
    return ColumnMapping(
        date=date_col,
        type=pick("type", "action", "activity", "transaction type"),
        kind=None if pick("type", "action", "activity", "transaction type") else "trade",
        symbol=pick("symbol", "ticker", "instrument", "security"),
        quantity=pick("quantity", "shares", "qty", "units"),
        price=pick("price", "unit price"),
        amount=pick("amount", "net amount", "value", "total"),
        fee=pick("fee", "fees", "commission"),
        currency=pick("currency", "ccy"),
        description=pick("description", "notes", "memo"),
        types={
            "BUY": "trade",
            "BOUGHT": "trade",
            "SELL": "trade",
            "SOLD": "trade",
            "DIVIDEND": "dividend",
            "DIV": "dividend",
            "INTEREST": "interest",
            "FEE": "fee",
            "DEPOSIT": "deposit",
            "WITHDRAWAL": "withdrawal",
            "SPLIT": "split",
        },
        sell_values=["SELL", "SOLD"],
    )


# ---- one row ------------------------------------------------------------------------


class _Skip(Exception):
    pass


def _activity(row: dict[str, str], m: ColumnMapping, seen: Counter[str]) -> Activity:
    day = _date(row.get(m.date, ""), m.date_format)
    type_value = row.get(m.type, "").upper() if m.type else ""
    kind = m.types.get(type_value) if m.type else m.kind
    if kind is None:
        kind = m.kind
    if kind is None or kind not in ACTIVITY_KINDS:
        raise _Skip(f"type {type_value or '(blank)'!r} is not mapped to a kind")
    raw_symbol = (row.get(m.symbol, "") if m.symbol else "").upper() or None
    quantity = _number(row, m.quantity)
    price = _number(row, m.price)
    amount = _number(row, m.amount)
    fee = _number(row, m.fee)
    currency = ((row.get(m.currency, "") if m.currency else "") or m.default_currency).upper()
    if kind == "trade":
        if raw_symbol is None or quantity is None:
            raise _Skip("a trade needs a symbol and a quantity")
        quantity = abs(quantity)
        if type_value in m.sell_values or (not m.sell_values and amount and amount > 0):
            quantity = -quantity
        if amount is None and price is not None:
            amount = -(quantity * price) - (fee or 0.0)
    elif amount is None:
        raise _Skip(f"a {kind} needs an amount")
    ticker = _ticker(raw_symbol, m.exchange, currency) if raw_symbol else None
    key = "|".join(
        str(x) for x in (kind, day.isoformat(), raw_symbol, quantity, price, amount, fee, currency)
    )
    seen[key] += 1
    digest = hashlib.sha256(f"{key}|{seen[key]}".encode()).hexdigest()[:24]
    return Activity(
        provider_activity_id=f"csv:{digest}",
        account_id="csv",
        kind=kind,
        trade_date=day,
        raw_symbol=raw_symbol,
        ticker=ticker,
        quantity=quantity,
        price=price,
        amount=amount,
        fee=fee,
        currency=currency,
        description=(row.get(m.description, "") if m.description else "")[:200] or None,
    )


def _ticker(symbol: str, exchange: str | None, currency: str) -> str | None:
    if _CANONICAL.match(symbol):
        return symbol
    return to_canonical_ticker(symbol, exchange=exchange, currency=currency)


def _date(value: str, fmt: str | None) -> date:
    text = value.strip()[:19]
    if not text:
        raise _Skip("no date")
    formats = (fmt,) if fmt else DATE_FORMATS
    for f in formats:
        try:
            return datetime.strptime(text[: len(datetime(2000, 1, 1).strftime(f))], f).date()
        except ValueError:
            continue
    raise _Skip(f"date {value!r} does not match {fmt or 'a known format'}")


def _number(row: dict[str, str], column: str | None) -> float | None:
    if not column:
        return None
    text = decimal_point(_NUMBER_JUNK.sub("", row.get(column, "")))
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    try:
        value = float(text.strip("()"))
    except ValueError:
        raise _Skip(f"{column} {row.get(column)!r} is not a number") from None
    return -value if negative else value


def decimal_point(text: str) -> str:
    """``text`` with a point as its only decimal mark and no thousands
    marks. With both marks the last one is the decimal mark (``1.234,56``,
    ``1,234.56``). A lone comma followed by other than three digits is a
    decimal comma (``12,5``). Anything else keeps commas as thousands."""
    if "," not in text:
        return text
    if "." in text:
        if text.rfind(",") > text.rfind("."):
            return text.replace(".", "").replace(",", ".")
        return text.replace(",", "")
    head, _, tail = text.rpartition(",")
    digits = tail.rstrip(")")
    if text.count(",") == 1 and digits.isdigit() and len(digits) != 3:
        return f"{head}.{tail}"
    return text.replace(",", "")


def _strip_bom(text: str) -> str:
    return text.removeprefix("﻿")
