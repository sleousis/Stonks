"""S&P 500 constituents and changes from Wikipedia's "List of S&P 500
companies" page.

The page has two tables: ``constituents`` (the current members) and
``changes`` (dated additions and removals, back to the 1970s but only
complete for recent decades). The whole page shape is parsed here with the
standard library's HTML parser, and symbols map to canonical ids
(``BRK.B`` becomes ``BRK-B.US``). Nothing outside this module sees the
page's columns.

Wikipedia is community edited: treat the history as good, not perfect.
A page that no longer has the tables raises :class:`IndexSourceError`
instead of returning an empty universe.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from html.parser import HTMLParser
from typing import Any

import requests

from stonks.logging import get_logger
from stonks.universes.base import IndexChange, IndexHistory
from stonks.universes.index_sources.base import IndexSource, IndexSourceError

PAGE_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
#: Wikipedia asks API clients to identify themselves.
_USER_AGENT = "stonks-research/0.1 (index constituents import)"
_DATE_FORMATS = ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d")

_log = get_logger("stonks.universes.index_sources.wikipedia_sp500")


class WikipediaSp500Source(IndexSource):
    source_id = "wikipedia_sp500"
    index_ids = ("sp500",)

    def __init__(self, session: Any = None, timeout_seconds: float = 30.0) -> None:
        self._session = session or requests.Session()
        self._timeout = timeout_seconds

    def fetch(self, index_id: str) -> IndexHistory:
        if index_id not in self.index_ids:
            raise IndexSourceError(
                f"{self.source_id} serves {list(self.index_ids)}, not {index_id!r}"
            )
        try:
            response = self._session.get(
                PAGE_URL, headers={"User-Agent": _USER_AGENT}, timeout=self._timeout
            )
            response.raise_for_status()
        except Exception as exc:  # transport errors of any client
            raise IndexSourceError(f"could not fetch the S&P 500 page: {exc}") from exc
        history, dropped = parse_sp500_page(response.text, as_of=date.today())
        if dropped:
            _log.warning("wikipedia_sp500.rows_dropped", dropped=dropped)
        return history


def canonical_ticker(symbol: str) -> str:
    """``BRK.B`` to ``BRK-B.US`` (US listings, share class after a dash)."""
    return f"{symbol.strip().upper().replace('.', '-')}.US"


def parse_sp500_page(html: str, *, as_of: date) -> tuple[IndexHistory, int]:
    """The history on the page and the number of change rows dropped as
    unreadable."""
    tables = _tables(html)
    constituents = tables.get("constituents")
    if not constituents:
        raise IndexSourceError("the page has no constituents table: its layout changed")
    members = [canonical_ticker(r[0]) for r in constituents if r and _is_symbol(r[0])]
    if not members:
        raise IndexSourceError("the constituents table has no symbols: its layout changed")
    changes: list[IndexChange] = []
    dropped = 0
    for row in tables.get("changes", []):
        if len(row) < 5:
            continue
        day = _parse_date(row[0])
        added, removed = row[1].strip(), row[3].strip()
        if day is None:
            dropped += 1
            continue
        if added and _is_symbol(added):
            changes.append(IndexChange(canonical_ticker(added), day, "add"))
        if removed and _is_symbol(removed):
            changes.append(IndexChange(canonical_ticker(removed), day, "remove"))
    history = IndexHistory(
        index_id="sp500",
        as_of=as_of,
        constituents=tuple(dict.fromkeys(members)),
        changes=tuple(dict.fromkeys(changes)),
        source=WikipediaSp500Source.source_id,
    )
    return history, dropped


_SYMBOL = re.compile(r"^[A-Za-z][A-Za-z0-9.\-]{0,9}$")


def _is_symbol(text: str) -> bool:
    return bool(_SYMBOL.match(text.strip()))


def _parse_date(text: str) -> date | None:
    cleaned = re.sub(r"\[.*?\]", "", text).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def _tables(html: str) -> dict[str, list[list[str]]]:
    parser = _TableParser()
    parser.feed(html)
    parser.close()
    return parser.tables


class _TableParser(HTMLParser):
    """Body rows (rows with at least one ``td``) of every ``table`` with an
    ``id``, as cell texts. ``rowspan`` on a ``td`` repeats the cell in the
    rows below; footnote markers (``<sup>``) are dropped."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: dict[str, list[list[str]]] = {}
        self._table: str | None = None
        self._depth = 0
        self._row: list[str] | None = None
        self._row_has_td = False
        self._cell: list[str] | None = None
        self._cell_rowspan = 1
        self._sup = 0
        #: column index -> (text, rows left) carried down by rowspan
        self._carry: dict[int, tuple[str, int]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "table":
            if self._table is None and a.get("id"):
                self._table = a["id"]
                self._depth = 1
                self.tables[self._table] = []
                self._carry = {}
            elif self._table is not None:
                self._depth += 1
            return
        if self._table is None or self._depth != 1:
            return
        if tag == "tr":
            self._row, self._row_has_td = [], False
            self._fill_carry()
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
            self._row_has_td = self._row_has_td or tag == "td"
            try:
                self._cell_rowspan = max(1, int(a.get("rowspan") or 1))
            except ValueError:
                self._cell_rowspan = 1
        elif tag == "sup":
            self._sup += 1

    def handle_endtag(self, tag: str) -> None:
        if self._table is None:
            return
        if tag == "table":
            self._depth -= 1
            if self._depth == 0:
                self._table = None
            return
        if self._depth != 1:
            return
        if tag == "sup":
            self._sup = max(0, self._sup - 1)
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            text = " ".join("".join(self._cell).split())
            if self._cell_rowspan > 1:
                self._carry[len(self._row)] = (text, self._cell_rowspan - 1)
            self._row.append(text)
            self._cell = None
            self._fill_carry()
        elif tag == "tr" and self._row is not None:
            if self._row_has_td:
                self.tables[self._table].append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None and not self._sup:
            self._cell.append(data)

    def _fill_carry(self) -> None:
        """Insert cells carried down by a rowspan at their column."""
        if self._row is None:
            return
        while len(self._row) in self._carry and self._cell is None:
            col = len(self._row)
            text, left = self._carry[col]
            if left <= 0:
                del self._carry[col]
                break
            self._row.append(text)
            if left == 1:
                del self._carry[col]
            else:
                self._carry[col] = (text, left - 1)
