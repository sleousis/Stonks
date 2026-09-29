"""DEGIRO exports: Transactions, Account statement and Portfolio.

DEGIRO (flatexDEGIRO) offers no API, and its client agreement forbids
automated tools and giving the login to anyone else. So Stonks reads only
the CSV files a person exports by hand from the DEGIRO web trader. See
``docs/design/degiro.md``.

What each file gives:

- **Transactions**: every trade with its local price, the value in the
  account currency, the exchange rate, the AutoFX cost, the transaction
  fees and the total. One trade activity per line, in the account
  currency: the price is the value per share in that currency, the fee is
  the transaction fees plus the AutoFX cost, the amount is the total. The
  local price and currency stay in the description.
- **Account statement**: every cash movement. Dividends and dividend tax,
  deposits, withdrawals, interest, fees, and currency conversions not tied
  to a trade become activities. Trades and the lines that belong to them
  (their fees and AutoFX legs, all carrying the trade's order id) are left
  out: they come from Transactions, so importing both never counts a trade
  twice. Transfers inside DEGIRO (the cash fund, the flatex bank account)
  and pending reservations are left out too.
- **Portfolio**: the holdings on the day of the export, with the cash
  lines. It sets the holdings; activities after that day build on it.

The exports name the columns in the account's language. A money column
comes with an unnamed neighbour: in Transactions the named column holds
the amount and the next one its currency, in the Account statement and
Portfolio the named column holds the currency and the next one the amount.
Newer Transactions files put the account currency in the header instead
(``Total EUR``). Dates are day first (``31-12-2025``). Most languages use a
decimal comma (``"1234,56"``), some a point, French a space between
thousands.

Every instrument comes in by ISIN, with the reference exchange and the
trading currency. The lake's ``instruments`` identifiers map it to our
ticker. What does not map stays by its ISIN and is listed as not covered.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from stonks.connections.base import Activity, ActivityKind
from stonks.connections.statement_csv import MAX_ROWS, ParsedRow, StatementError, decimal_point
from stonks.connections.statement_presets.base import (
    InstrumentResolver,
    Listing,
    ParsedHolding,
    PresetParse,
    StatementPreset,
)
from stonks.connections.statement_presets.registry import register_preset
from stonks.execution.brokers.symbols import to_canonical_ticker

BROKER = "DEGIRO"
LOCALES = ("en", "nl", "de", "fr", "es", "it", "pt")

_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_CCY = re.compile(r"^[A-Z]{3}$")
_DATE_FORMATS = ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y-%m-%d")
#: Currencies a header may end with ("Total EUR", "Waarde in CHF").
_HEADER_CURRENCIES = frozenset(
    [
        "eur",
        "usd",
        "gbp",
        "chf",
        "sek",
        "nok",
        "dkk",
        "pln",
        "czk",
        "huf",
        "ron",
        "cad",
        "aud",
        "nzd",
        "jpy",
        "hkd",
        "sgd",
    ]
)

#: DEGIRO's exchange codes (the "Reference exchange" column) -> our ticker
#: suffix. A code not here leaves the pick to the trading currency.
EXCHANGE_SUFFIX: dict[str, str] = {
    **dict.fromkeys(("NDQ", "NSY", "NYSE", "NASDAQ", "ASE", "BATS", "OTC"), "US"),
    "EAM": "AS",
    "EPA": "PA",
    "EBR": "BR",
    "ELI": "LS",
    "EID": "IR",
    "XET": "XETRA",
    **dict.fromkeys(("FRA", "TDG"), "F"),
    "LSE": "LSE",
    **dict.fromkeys(("MIL", "BIT"), "MI"),
    **dict.fromkeys(("MAD", "BME"), "MC"),
    **dict.fromkeys(("SWX", "VTX"), "SW"),
    "WBO": "VI",
    **dict.fromkeys(("OMX", "OMXS"), "ST"),
    "OSL": "OL",
    **dict.fromkeys(("CSE", "OMXC"), "CO"),
    **dict.fromkeys(("HEL", "OMXH"), "HE"),
    "WSE": "WAR",
    "TOR": "TO",
    "TSV": "V",
    "ASX": "AU",
    "HKS": "HK",
}


def _fold(text: str) -> str:
    """Lower case, no accents, single spaces: ``Änderung`` -> ``anderung``."""
    plain = unicodedata.normalize("NFKD", text.replace("﻿", ""))
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    return " ".join(plain.lower().split())


# ---- headers --------------------------------------------------------------------------
#
# Field -> header names per locale, folded. A header matches a name when it
# is the name, or the name followed by more words ("Value EUR", "Bolsa de",
# "Costes de transaccion y/o externos EUR"). Longer names are tried first,
# so "Local value" never reads as "Value".

_TX_HEADERS: dict[str, dict[str, tuple[str, ...]]] = {
    "en": {
        "date": ("date",), "time": ("time",), "product": ("product",), "isin": ("isin",),
        "exchange": ("reference exchange", "exchange"), "venue": ("venue", "execution venue"),
        "quantity": ("quantity", "number"), "price": ("price",),
        "local_value": ("local value",), "value": ("value",), "rate": ("exchange rate",),
        "autofx": ("autofx fee", "autofx"),
        "fees": ("transaction and/or third party fees", "transaction and/or",
                 "transaction costs", "transaction fees"),
        "total": ("total",), "order_id": ("order id",),
    },
    "nl": {
        "date": ("datum",), "time": ("tijd",), "product": ("product",), "isin": ("isin",),
        "exchange": ("beurs",), "venue": ("uitvoeringsplaats",), "quantity": ("aantal",),
        "price": ("koers",), "local_value": ("lokale waarde",), "value": ("waarde",),
        "rate": ("wisselkoers",), "autofx": ("autofx kosten", "autofx"),
        "fees": ("transactiekosten en/of kosten van derden", "transactiekosten"),
        "total": ("totaal",), "order_id": ("order id",),
    },
    "de": {
        "date": ("datum",), "time": ("uhrzeit", "zeit"), "product": ("produkt",),
        "isin": ("isin",), "exchange": ("referenzborse", "borse"),
        "venue": ("ausfuhrungsort",), "quantity": ("anzahl",), "price": ("kurs",),
        "local_value": ("wert in lokalwahrung", "lokaler wert"), "value": ("wert",),
        "rate": ("wechselkurs",), "autofx": ("autofx-gebuhr", "autofx gebuhr", "autofx"),
        "fees": ("transaktionsgebuhren und/oder fremdkosten",
                 "transaktionskosten und/oder kosten dritter", "transaktionsgebuhren",
                 "transaktionskosten"),
        "total": ("gesamt",), "order_id": ("order-id", "order id", "auftrags-id"),
    },
    "fr": {
        "date": ("date",), "time": ("heure",), "product": ("produit",),
        "isin": ("code isin", "isin"), "exchange": ("bourse de reference", "bourse"),
        "venue": ("lieu d'execution", "lieu d execution", "lieu"),
        "quantity": ("quantite",), "price": ("cours",), "local_value": ("valeur locale",),
        "value": ("valeur",), "rate": ("taux de change",),
        "autofx": ("frais autofx", "autofx"),
        "fees": ("frais de courtage", "frais de transaction", "frais de"),
        "total": ("total",), "order_id": ("id ordre", "id de l'ordre", "id d'ordre"),
    },
    "es": {
        "date": ("fecha",), "time": ("hora",), "product": ("producto",), "isin": ("isin",),
        "exchange": ("bolsa de referencia", "bolsa"), "venue": ("centro de ejecucion",),
        "quantity": ("numero", "cantidad"), "price": ("precio",),
        "local_value": ("valor local",), "value": ("valor",), "rate": ("tipo de cambio",),
        "autofx": ("comision autofx", "autofx"),
        "fees": ("costes de transaccion", "comision"),
        "total": ("total",), "order_id": ("id orden", "id de orden", "id de la orden"),
    },
    "it": {
        "date": ("data",), "time": ("ora",), "product": ("prodotto",), "isin": ("isin",),
        "exchange": ("borsa di riferimento", "borsa"),
        "venue": ("sede di esecuzione", "luogo di esecuzione"), "quantity": ("quantita",),
        "price": ("quotazione", "prezzo"), "local_value": ("valore locale",),
        "value": ("valore",), "rate": ("tasso di cambio",),
        "autofx": ("commissione autofx", "costo autofx", "autofx"),
        "fees": ("costi di transazione", "commissioni"),
        "total": ("totale",), "order_id": ("id ordine",),
    },
    "pt": {
        "date": ("data",), "time": ("hora",), "product": ("produto",), "isin": ("isin",),
        "exchange": ("bolsa de referencia", "bolsa"), "venue": ("local de execucao",),
        "quantity": ("quantidade",), "price": ("preco",), "local_value": ("valor local",),
        "value": ("valor",), "rate": ("taxa de cambio",),
        "autofx": ("custo autofx", "taxa autofx", "autofx"),
        "fees": ("custos de transacao", "comissao"),
        "total": ("total",), "order_id": ("id da ordem", "id ordem"),
    },
}  # fmt: skip

_ACCOUNT_HEADERS: dict[str, dict[str, tuple[str, ...]]] = {
    "en": {
        "date": ("date",), "time": ("time",), "value_date": ("value date",),
        "product": ("product",), "isin": ("isin",), "description": ("description",),
        "fx": ("fx",), "change": ("change",), "balance": ("balance",),
        "order_id": ("order id",),
    },
    "nl": {
        "date": ("datum",), "time": ("tijd",), "value_date": ("valutadatum",),
        "product": ("product",), "isin": ("isin",), "description": ("omschrijving",),
        "fx": ("fx",), "change": ("mutatie",), "balance": ("saldo",),
        "order_id": ("order id",),
    },
    "de": {
        "date": ("datum",), "time": ("uhrzeit", "uhrze", "zeit"),
        "value_date": ("valutadatum", "wertdatum"), "product": ("produkt",),
        "isin": ("isin",), "description": ("beschreibung",), "fx": ("fx",),
        "change": ("anderung",), "balance": ("saldo", "kontostand"),
        "order_id": ("order-id", "order id", "auftrags-id"),
    },
    "fr": {
        "date": ("date",), "time": ("heure",), "value_date": ("date de valeur", "date de"),
        "product": ("produit",), "isin": ("code isin", "isin"),
        "description": ("description",), "fx": ("fx",),
        "change": ("mouvements", "mouvement"), "balance": ("solde",),
        "order_id": ("id ordre", "id de l'ordre", "id d'ordre"),
    },
    "es": {
        "date": ("fecha",), "time": ("hora",), "value_date": ("fecha valor",),
        "product": ("producto",), "isin": ("isin",), "description": ("descripcion",),
        "fx": ("tipo de cambio", "tipo", "fx"), "change": ("variacion", "importe"),
        "balance": ("saldo",), "order_id": ("id orden", "id de orden", "id de la orden"),
    },
    "it": {
        "date": ("data",), "time": ("ora",), "value_date": ("data valuta", "data valore"),
        "product": ("prodotto",), "isin": ("isin",), "description": ("descrizione",),
        "fx": ("tasso di cambio", "cambio", "fx"), "change": ("variazioni", "variazione"),
        "balance": ("saldo",), "order_id": ("id ordine",),
    },
    "pt": {
        "date": ("data",), "time": ("hora",), "value_date": ("data valor", "data de valor"),
        "product": ("produto",), "isin": ("isin",), "description": ("descricao",),
        "fx": ("t.", "taxa", "fx"), "change": ("mudanca", "variacao"),
        "balance": ("saldo",), "order_id": ("id da ordem", "id ordem"),
    },
}  # fmt: skip

_PORTFOLIO_HEADERS: dict[str, dict[str, tuple[str, ...]]] = {
    "en": {
        "product": ("product",), "symbol": ("symbol/isin", "symbol"),
        "quantity": ("quantity", "amount"), "price": ("closing price", "closing"),
        "currency": ("currency",), "local_value": ("local value",),
        "value": ("value in", "value"),
    },
    "nl": {
        "product": ("product",), "symbol": ("symbool/isin", "symbool"),
        "quantity": ("aantal",), "price": ("slotkoers",), "currency": ("valuta",),
        "local_value": ("lokale waarde",), "value": ("waarde in", "waarde"),
    },
    "de": {
        "product": ("produkt",), "symbol": ("symbol/isin", "symbol"),
        "quantity": ("anzahl",), "price": ("schlusskurs",), "currency": ("wahrung",),
        "local_value": ("wert in lokalwahrung", "lokaler wert", "wert"),
        "value": ("wert in",),
    },
    "fr": {
        "product": ("produit",), "symbol": ("symbole/isin", "symbole", "code isin"),
        "quantity": ("quantite",), "price": ("cours de cloture", "cloture"),
        "currency": ("devise",), "local_value": ("valeur locale",),
        "value": ("valeur en", "valeur"),
    },
    "es": {
        "product": ("producto",), "symbol": ("simbolo/isin", "symbol/isin", "ticker/isin"),
        "quantity": ("cantidad", "numero"), "price": ("precio de cierre", "cierre"),
        "currency": ("divisa", "moneda"), "local_value": ("valor local",),
        "value": ("valor en", "valor"),
    },
    "it": {
        "product": ("prodotto",), "symbol": ("simbolo/isin", "simbolo"),
        "quantity": ("quantita",), "price": ("prezzo di chiusura", "chiusura"),
        "currency": ("valuta",), "local_value": ("valore locale",),
        "value": ("valore in", "valore"),
    },
    "pt": {
        "product": ("produto",), "symbol": ("simbolo/isin", "simbolo"),
        "quantity": ("quantidade",), "price": ("preco de fecho", "fecho"),
        "currency": ("moeda",), "local_value": ("valor local",),
        "value": ("valor em", "valor"),
    },
}  # fmt: skip

_TX_REQUIRED = ("date", "product", "isin", "quantity", "price", "value", "total")
_ACCOUNT_REQUIRED = ("date", "product", "isin", "description", "change")
_PORTFOLIO_REQUIRED = ("product", "symbol", "quantity", "price", "local_value")


@dataclass(frozen=True)
class _Column:
    index: int
    #: The currency the header names ("Total EUR"), if any.
    currency: str | None


def _header_matches(header: str, name: str) -> str | None:
    """``None`` when ``header`` is not ``name`` (or ``name`` plus more
    words), else the currency the header ends with, or ``""``."""
    if header == name:
        return ""
    if not header.startswith(name + " "):
        return None
    last = header.rsplit(" ", 1)[-1].strip("()")
    return last.upper() if last in _HEADER_CURRENCIES else ""


def _locate(headers: Sequence[str], table: Mapping[str, tuple[str, ...]]) -> dict[str, _Column]:
    """Each field's column. A header is used once, longer names first."""
    folded = [_fold(h) for h in headers]
    names = sorted(
        ((name, field) for field, options in table.items() for name in options),
        key=lambda pair: -len(pair[0]),
    )
    found: dict[str, _Column] = {}
    used: set[int] = set()
    for name, field in names:
        if field in found:
            continue
        for i, header in enumerate(folded):
            if i in used or not header:
                continue
            code = _header_matches(header, name)
            if code is not None:
                found[field] = _Column(i, code or None)
                used.add(i)
                break
    return found


def _detect(
    headers: Sequence[str],
    tables: Mapping[str, Mapping[str, tuple[str, ...]]],
    required: Sequence[str],
) -> tuple[str, dict[str, _Column]] | None:
    """The locale whose names cover the required fields and the most
    others, with the columns. Ties go to the earlier locale."""
    best: tuple[int, str, dict[str, _Column]] | None = None
    for locale in LOCALES:
        cols = _locate(headers, tables[locale])
        if not all(r in cols for r in required):
            continue
        if best is None or len(cols) > best[0]:
            best = (len(cols), locale, cols)
    return None if best is None else (best[1], best[2])


# ---- reading the file -----------------------------------------------------------------


class _Skip(Exception):
    pass


def _rows(text: str) -> list[list[str]]:
    body = text.removeprefix("﻿")
    first = body.split("\n", 1)[0]
    delimiter = ";" if first.count(";") > first.count(",") else ","
    rows = [[c.strip() for c in r] for r in csv.reader(io.StringIO(body), delimiter=delimiter)]
    if len(rows) - 1 > MAX_ROWS:
        raise StatementError(f"a statement may hold at most {MAX_ROWS} rows")
    if not rows:
        raise StatementError("the file is empty")
    return rows


def _cell(row: Sequence[str], col: _Column | None) -> str:
    if col is None or col.index >= len(row):
        return ""
    return row[col.index]


def _money(
    row: Sequence[str], headers: Sequence[str], col: _Column | None
) -> tuple[str, str | None]:
    """A money column's amount text and currency. The currency is in the
    header, or in the unnamed neighbour; either the named cell or its
    neighbour is the currency code, and the other the amount."""
    if col is None:
        return "", None
    here = _cell(row, col)
    nxt = col.index + 1
    neighbour = (
        row[nxt] if nxt < len(row) and (nxt >= len(headers) or not headers[nxt].strip()) else ""
    )
    if _CCY.match(here.upper()):
        return neighbour, here.upper()
    if col.currency:
        return here, col.currency
    return here, neighbour.upper() if _CCY.match(neighbour.upper()) else None


def _file_decimal(values: Sequence[str]) -> str:
    """The file's decimal mark. DEGIRO writes no thousands marks (French
    files use a space), so any lone comma in a number is a decimal comma.
    With both marks the last one wins, as in the generic import."""
    comma = point = 0
    for v in values:
        if "," in v and "." in v:
            if v.rfind(",") > v.rfind("."):
                comma += 1
            else:
                point += 1
        elif "," in v:
            comma += 1
        elif "." in v:
            point += 1
    return "," if comma > point else "."


def _number(text: str, decimal: str) -> float | None:
    raw = text.replace(" ", "").replace(" ", "").replace(" ", "").strip()
    if not raw:
        return None
    if "," in raw and "." in raw:
        clean = decimal_point(raw)
    elif "," in raw:
        clean = raw.replace(",", ".") if decimal == "," else decimal_point(raw)
    else:
        clean = raw
    try:
        return float(clean)
    except ValueError:
        raise _Skip(f"{text!r} is not a number") from None


def _date(text: str) -> date:
    value = text.strip()[:10]
    if not value:
        raise _Skip("no date")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise _Skip(f"date {text!r} is not a DEGIRO date (day-month-year)")


def _digest(parts: Sequence[object], seen: Counter[str]) -> str:
    """A stable id for a line: its meaning plus how many identical lines
    came before it in the same file (two real identical fills stay two)."""
    key = "|".join(str(p) for p in parts)
    seen[key] += 1
    return hashlib.sha256(f"{key}|{seen[key]}".encode()).hexdigest()[:24]


def _amount_texts(
    rows: Sequence[Sequence[str]], headers: Sequence[str], cols: Sequence[_Column | None]
) -> list[str]:
    out: list[str] = []
    for r in rows:
        for c in cols:
            text = _money(r, headers, c)[0] if c is not None else ""
            if text:
                out.append(text)
    return out


# ---- Transactions ---------------------------------------------------------------------


@register_preset
class DegiroTransactions(StatementPreset):
    id = "degiro_transactions"
    broker = BROKER
    label = "Transactions"
    kind = "activities"
    how_to_export = (
        "In the DEGIRO web trader open Inbox, then Transactions. Pick the dates, from your "
        "first trade to today, and choose Export, then CSV."
    )

    def detect(self, headers: Sequence[str]) -> str | None:
        found = _detect(headers, _TX_HEADERS, _TX_REQUIRED)
        return found[0] if found else None

    def parse(self, text: str, resolver: InstrumentResolver) -> PresetParse:
        rows = _rows(text)
        headers = rows[0]
        found = _detect(headers, _TX_HEADERS, _TX_REQUIRED)
        if found is None:
            raise StatementError("these are not the headers of a DEGIRO Transactions export")
        locale, cols = found
        body = [(n, r) for n, r in enumerate(rows[1:], start=2) if any(r)]
        money = [cols.get(k) for k in ("quantity", "price", "local_value", "value", "rate",
                                       "autofx", "fees", "total")]  # fmt: skip
        decimal = _file_decimal(_amount_texts([r for _, r in body], headers, money))
        reads: list[tuple[int, dict[str, Any] | None, str | None]] = []
        for n, row in body:
            try:
                reads.append((n, _read_trade(row, headers, cols, decimal), None))
            except _Skip as skip:
                reads.append((n, None, str(skip)))
        listings = {r["listing"] for _, r, _ in reads if r is not None and r["listing"]}
        tickers = resolver.resolve(sorted(listings, key=lambda li: (li.isin, str(li.suffix))))
        out = PresetParse(preset=self.id, kind="activities", locale=locale)
        seen: Counter[str] = Counter()
        for n, read, reason in reads:
            if read is None:
                out.rows.append(ParsedRow(n, None, reason))
                continue
            listing = read["listing"]
            ticker = tickers.get(listing) if listing else None
            if ticker is None and read["isin"]:
                out.unmapped[read["isin"]] = read["product"]
            out.rows.append(ParsedRow(n, _trade_activity(read, ticker, seen)))
        out.notes.append(
            "Trades are in the account currency: the price is the value per share, and the "
            "fee is the transaction fees plus the AutoFX cost."
        )
        return out


def _read_trade(
    row: Sequence[str], headers: Sequence[str], cols: Mapping[str, _Column], decimal: str
) -> dict[str, Any]:
    day = _date(_cell(row, cols["date"]))
    isin = _cell(row, cols["isin"]).upper()
    product = _cell(row, cols["product"])
    if not isin and not product:
        raise _Skip("a trade needs a product")
    quantity = _number(_cell(row, cols["quantity"]), decimal)
    if not quantity:
        raise _Skip("a trade needs a quantity")
    price_text, local_ccy = _money(row, headers, cols["price"])
    local_text, local_ccy2 = _money(row, headers, cols.get("local_value"))
    value_text, value_ccy = _money(row, headers, cols["value"])
    total_text, total_ccy = _money(row, headers, cols["total"])
    fees_text, fees_ccy = _money(row, headers, cols.get("fees"))
    autofx_text, _ = _money(row, headers, cols.get("autofx"))
    local_ccy = local_ccy or local_ccy2
    price = _number(price_text, decimal)
    value = _number(value_text, decimal)
    total = _number(total_text, decimal)
    fees = abs(_number(fees_text, decimal) or 0.0)
    autofx = abs(_number(autofx_text, decimal) or 0.0)
    account_ccy = total_ccy or value_ccy or fees_ccy or local_ccy
    if total is None:
        if value is None:
            raise _Skip("a trade needs a value or a total")
        total = value - fees - autofx
    if value is not None and local_ccy and account_ccy and local_ccy != account_ccy:
        # the price in the account currency, so price, fee and amount agree
        unit = abs(value) / abs(quantity)
    else:
        unit = price if price is not None else (abs(value) / abs(quantity) if value else None)
    exchange = _cell(row, cols.get("exchange"))
    return {
        "day": day,
        "time": _cell(row, cols.get("time")),
        "isin": isin if _ISIN.match(isin) else "",
        "product": product,
        "exchange": exchange,
        "quantity": quantity,
        "price": unit,
        "local_price": price,
        "local_ccy": local_ccy,
        "local_value": _number(local_text, decimal),
        "amount": total,
        "fee": fees + autofx,
        "currency": account_ccy,
        "rate": _number(_cell(row, cols.get("rate")), decimal),
        "order_id": _cell(row, cols.get("order_id")),
        "listing": (
            Listing(isin, EXCHANGE_SUFFIX.get(exchange.strip().upper()), local_ccy)
            if _ISIN.match(isin)
            else None
        ),
    }


def _trade_activity(read: Mapping[str, Any], ticker: str | None, seen: Counter[str]) -> Activity:
    parts = [read["product"]]
    if read["exchange"]:
        parts.append(f"on {read['exchange']}")
    if read["local_price"] is not None and read["local_ccy"]:
        parts.append(f"at {read['local_price']:g} {read['local_ccy']}")
    if read["rate"]:
        parts.append(f"rate {read['rate']:g}")
    raw_symbol = read["isin"] or str(read["product"])[:40].upper()
    digest = _digest(
        (read["order_id"], read["day"].isoformat(), read["time"], raw_symbol, read["quantity"],
         read["local_price"], read["amount"]),
        seen,
    )  # fmt: skip
    return Activity(
        provider_activity_id=f"degiro:tx:{digest}",
        account_id="csv",
        kind="trade",
        trade_date=read["day"],
        raw_symbol=raw_symbol,
        ticker=ticker,
        quantity=float(read["quantity"]),
        price=read["price"],
        amount=float(read["amount"]),
        fee=float(read["fee"]),
        currency=read["currency"],
        description=", ".join(str(p) for p in parts)[:200],
    )


# ---- Account statement ----------------------------------------------------------------

#: How each kind of Account statement line reads (folded), per meaning.
#: Checked in this order and the first match wins: "dividend tax" before
#: "dividend", the money market fund before a trade. A name matches at the
#: start of the description, or anywhere when it is longer than six letters.
_ACCOUNT_KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("reservation", ("reservation", "reservering", "reservierung", "reserva",
                     "prenotazione")),
    ("internal", ("degiro cash sweep", "cash sweep", "money market fund", "geldmarktfonds",
                  "overboeking van uw geldrekening", "overboeking naar uw geldrekening",
                  "flatexdegiro bank", "virement depuis votre compte", "virement vers votre compte",
                  "transferir desde su cuenta", "transferir a su cuenta", "fondo monetario",
                  "fonds monetaire")),
    ("corporate", ("aandelensplit", "aktiensplit", "stock split", "isin-wijziging",
                   "wijziging isin", "isin-anderung", "isin change", "fusie", "merger",
                   "spin-off", "uitbetaling certificaat")),
    ("trade", ("buy ", "sell ", "koop ", "verkoop ", "kauf ", "verkauf ", "achat ", "vente ",
               "compra ", "venta ", "venda ", "acquisto ", "vendita ")),
    ("trade_fee", ("degiro transaction", "transaction and/or third", "transactiekosten",
                   "transaktionsgebuhr", "transaktionskosten", "frais degiro de courtage",
                   "frais de courtage", "frais de transaction", "costes de transaccion",
                   "comision de transaccion", "costi di transazione",
                   "commissioni di transazione", "comissoes de transacao",
                   "custos de transacao")),
    ("dividend_tax", ("dividend tax", "dividendbelasting", "dividendensteuer",
                      "quellensteuer", "impots sur dividende", "impot sur dividende",
                      "retencion del dividendo", "ritenuta sul dividendo",
                      "imposto sobre dividendo", "imposto sobre dividendos")),
    ("dividend", ("dividend", "dividende", "dividendo", "fund distribution",
                  "fondsausschuttung", "capital return", "remboursement de capital",
                  "kapitalruckzahlung")),
    ("fx", ("fx credit", "fx debit", "fx withdrawal", "fx deposit", "valuta creditering",
            "valuta debitering", "wahrungswechsel", "operation de change", "credit de change",
            "debit de change", "cambio de divisa", "credito fx", "prelievo fx",
            "accredito cambio valuta", "addebito cambio valuta", "cambio valuta",
            "credito de cambio", "debito de cambio", "levantamento de cambio",
            "deposito de cambio")),
    ("interest", ("flatex interest", "interest", "rente", "zinsen", "interets", "interet",
                  "intereses", "interes", "interessi", "juros")),
    ("withdrawal", ("processed flatex withdrawal", "flatex withdrawal", "withdrawal",
                    "terugstorting", "opname", "auszahlung", "retrait", "retirada",
                    "prelievo", "levantamento")),
    ("deposit", ("ideal deposit", "flatex deposit", "sofort deposit", "deposit", "storting",
                 "einzahlung", "versement de fonds", "depot", "ingreso", "deposito",
                 "versamento")),
    ("fee", ("exchange connection fee", "connection fee", "aansluitingskosten",
             "anschlussgebuhr", "handelsmodalitaten", "realtimekurse", "frais de connexion",
             "comision de conectividad", "costi di connessione", "custo de conectividade",
             "transactiebelasting", "stamp duty", "financial transaction tax", "adr/gdr",
             "fee", "kosten", "gebuhr", "frais", "comision", "commissione", "custo")),
)  # fmt: skip

_KIND_OF: dict[str, ActivityKind] = {
    "dividend": "dividend",
    "dividend_tax": "dividend",
    "interest": "interest",
    "withdrawal": "withdrawal",
    "deposit": "deposit",
    "fee": "fee",
    "fx": "other",
}

_SKIP_REASONS = {
    "trade": "part of a trade: trades come from the Transactions export",
    "trade_fee": "part of a trade: trades come from the Transactions export",
    "corporate": "a corporate action: its trades come from the Transactions export",
    "reservation": "a reservation, booked again when it settles",
    "internal": "a transfer inside DEGIRO (the cash fund or the flatex bank account)",
}


def _meaning(description: str) -> str | None:
    text = _fold(description)
    for meaning, names in _ACCOUNT_KINDS:
        for name in names:
            if text.startswith(name) or (len(name) > 6 and name in text):
                return meaning
    return None


@register_preset
class DegiroAccountStatement(StatementPreset):
    id = "degiro_account"
    broker = BROKER
    label = "Account statement"
    kind = "activities"
    how_to_export = (
        "In the DEGIRO web trader open Inbox, then Account statement. Pick the dates and "
        "choose Export, then CSV. Import the Transactions export too: trades come from there."
    )

    def detect(self, headers: Sequence[str]) -> str | None:
        found = _detect(headers, _ACCOUNT_HEADERS, _ACCOUNT_REQUIRED)
        return found[0] if found else None

    def parse(self, text: str, resolver: InstrumentResolver) -> PresetParse:
        rows = _rows(text)
        headers = rows[0]
        found = _detect(headers, _ACCOUNT_HEADERS, _ACCOUNT_REQUIRED)
        if found is None:
            raise StatementError("these are not the headers of a DEGIRO Account statement export")
        locale, cols = found
        body = [(n, r) for n, r in enumerate(rows[1:], start=2) if any(r)]
        decimal = _file_decimal(
            _amount_texts([r for _, r in body], headers, [cols.get("change")])
            + [t for _, r in body if (t := _cell(r, cols.get("fx")))]
        )
        # The moments of trade lines: a currency leg booked at the same
        # moment belongs to the trade even without an order id.
        trade_moments = {
            (_cell(r, cols["date"]), _cell(r, cols.get("time")))
            for _, r in body
            if _meaning(_cell(r, cols["description"])) in ("trade", "trade_fee")
        }
        reads: list[tuple[int, dict[str, Any] | None, str | None]] = []
        in_trades = 0
        for n, row in body:
            try:
                reads.append((n, _read_cash(row, headers, cols, decimal, trade_moments), None))
            except _Skip as skip:
                in_trades += str(skip).startswith("part of a trade")
                reads.append((n, None, str(skip)))
        listings = {
            n: Listing(r["isin"], None, r["currency"])
            for n, r, _ in reads
            if r is not None and r["isin"]
        }
        tickers = resolver.resolve(sorted(set(listings.values()), key=lambda li: li.isin))
        out = PresetParse(preset=self.id, kind="activities", locale=locale)
        seen: Counter[str] = Counter()
        for n, read, reason in reads:
            if read is None:
                out.rows.append(ParsedRow(n, None, reason))
                continue
            ticker = tickers.get(listings[n]) if n in listings else None
            if n in listings and ticker is None:
                out.unmapped[read["isin"]] = read["product"]
            out.rows.append(ParsedRow(n, _cash_activity(read, ticker, seen)))
        if in_trades:
            out.notes.append(
                f"{in_trades} lines belong to trades and are left out. Import the Transactions "
                "export for trades and their fees."
            )
        out.notes.append(
            "Currency conversions not tied to a trade come in as Other, so the cash adds up."
        )
        return out


def _read_cash(
    row: Sequence[str],
    headers: Sequence[str],
    cols: Mapping[str, _Column],
    decimal: str,
    trade_moments: set[tuple[str, str]],
) -> dict[str, Any]:
    day = _date(_cell(row, cols["date"]))
    description = _cell(row, cols["description"])
    meaning = _meaning(description)
    moment = (_cell(row, cols["date"]), _cell(row, cols.get("time")))
    if meaning in _SKIP_REASONS:
        raise _Skip(_SKIP_REASONS[meaning])
    if _cell(row, cols.get("order_id")):
        raise _Skip("part of a trade (it carries the trade's order id)")
    if meaning == "fx" and moment in trade_moments:
        raise _Skip("part of a trade: the currency leg of a trade (AutoFX)")
    if meaning is None:
        raise _Skip(f"{description!r} is not a DEGIRO line Stonks knows")
    amount_text, currency = _money(row, headers, cols["change"])
    amount = _number(amount_text, decimal)
    if not amount:
        raise _Skip("no amount")
    isin = _cell(row, cols["isin"]).upper()
    return {
        "day": day,
        "time": _cell(row, cols.get("time")),
        "isin": isin if _ISIN.match(isin) else "",
        "product": _cell(row, cols["product"]),
        "description": description,
        "meaning": meaning,
        "kind": _KIND_OF[meaning],
        "amount": amount,
        "currency": currency,
        "fx": _number(_cell(row, cols.get("fx")), decimal),
    }


def _cash_activity(read: Mapping[str, Any], ticker: str | None, seen: Counter[str]) -> Activity:
    label = {"dividend_tax": "Dividend tax", "fx": "Currency conversion"}.get(read["meaning"])
    text = f"{label}: {read['description']}" if label else str(read["description"])
    if read["product"]:
        text = f"{text} ({read['product']})"
    if read["fx"]:
        text = f"{text}, rate {read['fx']:g}"
    digest = _digest(
        (read["day"].isoformat(), read["time"], read["isin"], read["description"],
         read["amount"], read["currency"]),
        seen,
    )  # fmt: skip
    return Activity(
        provider_activity_id=f"degiro:acct:{digest}",
        account_id="csv",
        kind=read["kind"],
        trade_date=read["day"],
        raw_symbol=read["isin"] or None,
        ticker=ticker,
        amount=float(read["amount"]),
        currency=read["currency"],
        description=text[:200],
    )


# ---- Portfolio ------------------------------------------------------------------------

_CASH_PRODUCT = re.compile(r"\bcash\b|geldmarkt|liquidit|contanti|efectivo|especes|dinheiro")


@register_preset
class DegiroPortfolio(StatementPreset):
    id = "degiro_portfolio"
    broker = BROKER
    label = "Portfolio"
    kind = "holdings"
    how_to_export = (
        "In the DEGIRO web trader open Portfolio and choose Export, then CSV. It holds your "
        "positions and cash on the day you export it: give that day when you import it."
    )

    def detect(self, headers: Sequence[str]) -> str | None:
        found = _detect(headers, _PORTFOLIO_HEADERS, _PORTFOLIO_REQUIRED)
        return found[0] if found else None

    def parse(self, text: str, resolver: InstrumentResolver) -> PresetParse:
        rows = _rows(text)
        headers = rows[0]
        found = _detect(headers, _PORTFOLIO_HEADERS, _PORTFOLIO_REQUIRED)
        if found is None:
            raise StatementError("these are not the headers of a DEGIRO Portfolio export")
        locale, cols = found
        body = [(n, r) for n, r in enumerate(rows[1:], start=2) if any(r)]
        decimal = _file_decimal(
            _amount_texts(
                [r for _, r in body],
                headers,
                [cols.get(k) for k in ("quantity", "price", "local_value", "value")],
            )
        )
        out = PresetParse(preset=self.id, kind="holdings", locale=locale)
        reads: list[tuple[int, dict[str, Any]]] = []
        for n, row in body:
            try:
                reads.append((n, _read_holding(row, headers, cols, decimal)))
            except _Skip as skip:
                out.skipped.append(ParsedRow(n, None, str(skip)))
        listings = {n: Listing(r["isin"], None, r["currency"]) for n, r in reads if r["isin"]}
        tickers = resolver.resolve(sorted(set(listings.values()), key=lambda li: li.isin))
        seen: Counter[str] = Counter()
        for n, r in reads:
            ticker = tickers.get(listings[n]) if n in listings else r["symbol_ticker"]
            if not r["is_cash"] and ticker is None:
                out.unmapped[r["raw_symbol"]] = r["product"]
            digest = _digest(
                (r["raw_symbol"], r["quantity"], r["price"], r["local_value"], r["currency"]),
                seen,
            )
            out.holdings.append(
                ParsedHolding(
                    line=n,
                    row_id=f"degiro:pos:{digest}",
                    raw_symbol=r["raw_symbol"],
                    ticker=None if r["is_cash"] else ticker,
                    quantity=float(r["quantity"]),
                    price=r["price"],
                    market_value=r["local_value"],
                    currency=r["currency"],
                    description=str(r["product"])[:200] or None,
                    is_cash=r["is_cash"],
                )
            )
        out.notes.append(
            "A Portfolio export sets the holdings and cash on its day. Activities dated after "
            "that day build on it."
        )
        return out


def _read_holding(
    row: Sequence[str], headers: Sequence[str], cols: Mapping[str, _Column], decimal: str
) -> dict[str, Any]:
    product = _cell(row, cols["product"])
    symbol = _cell(row, cols["symbol"]).upper()
    value_text, currency = _money(row, headers, cols["local_value"])
    currency = currency or (_cell(row, cols.get("currency")).upper() or None)
    if currency is None and len(value_text.split()) == 2:
        # "EUR 1234,56" in one cell
        code, value_text = value_text.split()
        currency = code.upper() if _CCY.match(code.upper()) else None
    local_value = _number(value_text, decimal)
    if not symbol and _CASH_PRODUCT.search(_fold(product)):
        if local_value is None:
            raise _Skip("a cash line needs a value")
        code = currency or _cash_currency(product)
        return {
            "product": product,
            "isin": "",
            "raw_symbol": f"CASH:{code or 'UNKNOWN'}",
            "symbol_ticker": None,
            "quantity": local_value,
            "price": 1.0,
            "local_value": local_value,
            "currency": code,
            "is_cash": True,
        }
    if not symbol:
        raise _Skip(f"{product!r} has no symbol or ISIN")
    quantity = _number(_cell(row, cols["quantity"]), decimal)
    if not quantity:
        raise _Skip("a holding needs a quantity")
    isin = symbol if _ISIN.match(symbol) else ""
    return {
        "product": product,
        "isin": isin,
        "raw_symbol": symbol,
        "symbol_ticker": None if isin else to_canonical_ticker(symbol, currency=currency),
        "quantity": quantity,
        "price": _number(_cell(row, cols["price"]), decimal),
        "local_value": local_value,
        "currency": currency,
        "is_cash": False,
    }


def _cash_currency(product: str) -> str | None:
    """``CASH & CASH FUND & FTX CASH (EUR)`` -> ``EUR``."""
    match = re.search(r"\(([A-Z]{3})\)", product.upper())
    return match.group(1) if match else None
